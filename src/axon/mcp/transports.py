"""
MCP transports: stdio, Streamable HTTP (2025-03-26+) and legacy HTTP+SSE (2024-11-05).

A transport moves JSON-RPC messages. ``start(on_message, on_close)`` begins delivery
of incoming messages (dicts) to ``on_message``; ``send(message)`` transmits one
message; ``close()`` releases every resource. Transports never interpret messages.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx

OnMessage = Callable[[dict[str, Any]], None]
OnClose = Callable[[str], None]
HeaderProvider = Callable[[], dict[str, str]]       # extra request headers, asked for on every request
OnUnauthorized = Callable[[], bool]                  # refresh credentials after a 401/403; True = retry once
IS_WINDOWS = os.name == "nt"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024               # one JSON body or one event of an event stream
STOP_SIGNAL_WAITS = ((signal.SIGINT, 0.1), (signal.SIGTERM, 0.4))     # then SIGKILL (POSIX)
GRACEFUL_STOP_WAIT = 1.5                                             # stdin closed, server exits by itself
# What a stdio server keeps under CLAUDE_CODE_MCP_ALLOWLIST_ENV / AXON_MCP_ALLOWLIST_ENV.
_SAFE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP",
             "TMP", "TZ", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
             "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA")


class TransportError(Exception):
    """Raised when a message cannot be delivered."""

    def __init__(self, message: str, status: int | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers or {}        # response headers (e.g. WWW-Authenticate) when there were any


class ResponseTooLarge(TransportError):
    """A response from a remote server passed the size limit; the request fails."""


class SessionExpired(TransportError):
    """Streamable HTTP server answered 404 for our Mcp-Session-Id."""


def log_path_for(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "server"
    from axon.storage import logs_dir
    return logs_dir() / f"mcp-{safe}.log"


def _dispatch(payload: Any, on_message: OnMessage) -> None:
    """Deliver a decoded JSON-RPC payload (single message or batch)."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                on_message(item)
    elif isinstance(payload, dict):
        on_message(payload)


class Transport:
    kind = "base"

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        raise NotImplementedError

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # Optional hooks used by the client after initialization.
    def set_protocol_version(self, version: str) -> None:
        pass

    def after_initialize(self) -> None:
        pass


# ─── stdio ────────────────────────────────────────────────────────────────

def resolve_command(command: str, env: dict[str, str] | None = None) -> str:
    """Resolve an executable through PATH (and PATHEXT on Windows) so that npm/uv
    shims such as ``npx.cmd`` / ``uvx.exe`` are found."""
    if os.path.dirname(command):
        return command
    path = (env or {}).get("PATH") or os.environ.get("PATH")
    found = shutil.which(command, path=path)
    return found or command


def build_argv(command: str, args: list[str], env: dict[str, str] | None = None) -> list[str]:
    """argv with the executable resolved to a full path. On Windows CreateProcess runs a
    fully-qualified ``.cmd``/``.bat`` shim (npx.cmd) through cmd.exe by itself."""
    return [resolve_command(command, env), *args]


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and all of its children."""
    if proc.poll() is not None:
        return
    try:
        from axon.platform import kill_process_tree  # type: ignore[attr-defined]
        kill_process_tree(proc)
        proc.wait(timeout=3)
        return
    except (ImportError, AttributeError):
        pass
    except Exception:
        pass
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """The server runs in its own session: signal its group so a wrapper's child (npx, uvx) hears it too."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def stop_process(proc: subprocess.Popen) -> None:
    """Close stdin and give the server about 1.5 s to exit by itself. Then, on POSIX, SIGINT, SIGTERM 100 ms
    later, SIGKILL after a further 400 ms; on Windows, end the process tree."""
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=GRACEFUL_STOP_WAIT)
        return
    except subprocess.TimeoutExpired:
        pass
    if IS_WINDOWS:
        kill_tree(proc)
        return
    for sig, wait in STOP_SIGNAL_WAITS:
        if proc.poll() is not None:
            return
        _signal_group(proc, sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
    _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


class StdioTransport(Transport):
    """Newline-delimited JSON-RPC over a subprocess's stdin/stdout.
    stderr goes straight to ~/.axon/logs/mcp-<name>.log (never a pipe, so it can't deadlock)."""

    kind = "stdio"

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | Path | None = None,
                 project_dir: str | Path | None = None) -> None:
        self.name = name
        self.project_dir = str(project_dir) if project_dir else None
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None
        self.log_path = log_path_for(name)
        self.proc: subprocess.Popen | None = None
        self._log_file: Any = None
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closing = False

    def _log(self, text: str) -> None:
        try:
            if self._log_file:
                self._log_file.write(text.encode("utf-8", "replace"))
                self._log_file.flush()
        except (OSError, ValueError):
            pass

    def _close_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        full_env = dict(os.environ)
        if any(os.environ.get(v) == "1" for v in ("CLAUDE_CODE_MCP_ALLOWLIST_ENV", "AXON_MCP_ALLOWLIST_ENV")):
            full_env = {k: v for k, v in full_env.items() if k.upper() in _SAFE_ENV}
        if self.project_dir:  # the stable project root, whatever directory the server runs in
            full_env["AXON_PROJECT_DIR"] = self.project_dir
            full_env["CLAUDE_PROJECT_DIR"] = self.project_dir
        full_env.update(self.env)
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        argv = build_argv(self.command, self.args, full_env)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.log_path, "ab")
        self._log(f"\n=== starting: {subprocess.list2cmdline(argv)} (cwd={self.cwd}) ===\n")
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log_file,
                env=full_env, cwd=self.cwd, bufsize=0, **kwargs,
            )
        except FileNotFoundError as e:
            self._log(f"command not found: {e}\n")
            self._close_log()
            raise TransportError(f"Command not found: {self.command} "
                                 f"(is it installed and on PATH?)") from e
        except OSError as e:
            self._log(f"failed to start: {e}\n")
            self._close_log()
            raise TransportError(f"Failed to start '{self.command}': {e}") from e

        def _read() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            stdout = self.proc.stdout
            try:
                for raw in iter(stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        self._log(f"[non-JSON stdout] {line}\n")
                        continue
                    try:
                        _dispatch(payload, on_message)
                    except Exception as e:  # never kill the reader
                        self._log(f"[dispatch error] {e}\n")
            except (OSError, ValueError):
                pass
            code = self.proc.poll() if self.proc else None
            if code is None and self.proc is not None:
                try:
                    code = self.proc.wait(timeout=1)
                except Exception:
                    code = None
            if not self._closing:
                self._log(f"=== process exited (code {code}) ===\n")
            on_close(f"server process exited (code {code}); see {self.log_path}")

        self._reader = threading.Thread(target=_read, name=f"mcp-{self.name}-reader", daemon=True)
        self._reader.start()

    def send(self, message: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            raise TransportError("server process is not running")
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise TransportError(f"write failed: {e}") from e

    def close(self) -> None:
        self._closing = True
        proc = self.proc
        if proc is not None:
            try:
                stop_process(proc)
            except Exception:
                pass
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
        self._close_log()


# ─── SSE parsing ─────────────────────────────────────────────────────────

def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str, str | None]]:
    """Parse a text/event-stream into (event, data, id) tuples."""
    event, data, ev_id = "message", [], None
    for line in lines:
        line = line.rstrip("\r\n") if isinstance(line, str) else line
        if line == "":
            if data:
                yield event, "\n".join(data), ev_id
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event = value or "message"
        elif field_name == "data":
            data.append(value)
        elif field_name == "id":
            ev_id = value
    if data:
        yield event, "\n".join(data), ev_id


def _deliver_sse(lines: Iterable[str], on_message: OnMessage) -> None:
    for event, data, _ in iter_sse(lines):
        if len(data) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge(f"event over {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB")
        if event != "message":
            continue
        try:
            _dispatch(json.loads(data), on_message)
        except ValueError:
            continue


def _bounded_lines(resp: httpx.Response, limit: int = MAX_RESPONSE_BYTES) -> Iterator[str]:
    """``resp.iter_lines()`` that stops reading once one line alone passes the limit (after decompression)."""
    buf = ""
    for chunk in resp.iter_text():
        buf += chunk
        if "\n" not in buf and "\r" not in buf:
            if len(buf) > limit:
                raise ResponseTooLarge(f"event over {limit // (1024 * 1024)} MiB")
            continue
        lines = buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        buf = lines.pop()
        yield from lines
    if buf:
        yield buf


def _read_bounded(resp: httpx.Response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    out = bytearray()
    for chunk in resp.iter_bytes():
        out += chunk
        if len(out) > limit:
            raise ResponseTooLarge(f"response over {limit // (1024 * 1024)} MiB")
    return bytes(out)


def _stream_mode(resp: httpx.Response) -> None:
    """The per-request timer only covers the wait for the first response byte: an event stream may be quiet
    for as long as it likes after that (the tool-call limits still apply)."""
    try:
        timeouts = resp.request.extensions.get("timeout")
        if isinstance(timeouts, dict):
            timeouts["read"] = None
    except (RuntimeError, AttributeError):
        pass


# ─── Streamable HTTP ─────────────────────────────────────────────────────

class StreamableHTTPTransport(Transport):
    """POST each message; responses arrive as JSON bodies or SSE streams.
    Maintains Mcp-Session-Id and MCP-Protocol-Version headers."""

    kind = "http"

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60.0,
                 client: httpx.Client | None = None, name: str = "server",
                 header_provider: HeaderProvider | None = None,
                 on_unauthorized: OnUnauthorized | None = None) -> None:
        self.url = url
        self.name = name
        self.headers = dict(headers or {})
        self.header_provider = header_provider
        self.on_unauthorized = on_unauthorized
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0),
                                              follow_redirects=True)
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._on_message: OnMessage | None = None
        self._on_close: OnClose | None = None
        self._closed = False
        self._get_thread: threading.Thread | None = None
        self._streams: list[httpx.Response] = []
        self._lock = threading.Lock()

    def _headers(self, accept: str) -> dict[str, str]:
        h = {"Accept": accept, **self.headers}
        if self.header_provider is not None:
            try:
                h.update(self.header_provider())
            except Exception as e:  # a failing helper must not kill the request path
                raise TransportError(f"could not compute request headers: {e}") from e
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        self._on_message = on_message
        self._on_close = on_close

    def set_protocol_version(self, version: str) -> None:
        self.protocol_version = version

    def _post(self, message: dict[str, Any]) -> httpx.Response:
        headers = self._headers("application/json, text/event-stream")
        headers["Content-Type"] = "application/json"
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            req = self._client.build_request("POST", self.url, content=body, headers=headers)
            return self._client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise TransportError(f"HTTP request failed: {e}") from e

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise TransportError("transport closed")
        assert self._on_message is not None
        resp = self._post(message)
        if resp.status_code in (401, 403) and self.on_unauthorized is not None:
            resp.close()
            refreshed = False
            try:
                refreshed = bool(self.on_unauthorized())
            except Exception:
                refreshed = False
            if refreshed:
                resp = self._post(message)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code == 404 and self.session_id and message.get("method") != "initialize":
            resp.close()
            raise SessionExpired("MCP session expired", status=404)
        if resp.status_code >= 400:
            try:
                detail = resp.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            resp.close()
            raise TransportError(f"HTTP {resp.status_code} from {self.url}: {detail}".strip(),
                                 status=resp.status_code, headers=dict(resp.headers))
        ctype = resp.headers.get("content-type", "").lower()
        if resp.status_code == 202 or not ctype:
            resp.close()
            return
        if "text/event-stream" in ctype:
            _stream_mode(resp)
            with self._lock:
                self._streams.append(resp)
            on_message = self._on_message

            def _consume() -> None:
                try:
                    _deliver_sse(_bounded_lines(resp), on_message)
                except ResponseTooLarge as e:
                    if message.get("id") is not None:  # the request fails; the connection stays usable
                        on_message({"jsonrpc": "2.0", "id": message["id"],
                                    "error": {"code": -32000, "message": f"{self.name}: {e}"}})
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                finally:
                    resp.close()
                    with self._lock:
                        if resp in self._streams:
                            self._streams.remove(resp)

            threading.Thread(target=_consume, name=f"mcp-{self.name}-sse", daemon=True).start()
            return
        try:
            raw = _read_bounded(resp)
        except httpx.HTTPError as e:
            raise TransportError(f"HTTP request failed: {e}") from e
        finally:
            resp.close()
        if raw.strip():
            try:
                _dispatch(json.loads(raw.decode("utf-8", "replace")), self._on_message)
            except ValueError as e:
                raise TransportError(f"invalid JSON from server: {e}") from e

    def after_initialize(self) -> None:
        """Open the optional GET SSE stream for server-initiated messages."""
        def _listen() -> None:
            try:
                req = self._client.build_request("GET", self.url, headers=self._headers("text/event-stream"))
                resp = self._client.send(req, stream=True)
            except httpx.HTTPError:
                return
            if resp.status_code != 200 or "text/event-stream" not in resp.headers.get("content-type", ""):
                resp.close()
                return
            with self._lock:
                if self._closed:
                    resp.close()
                    return
                self._streams.append(resp)
            _stream_mode(resp)
            try:
                assert self._on_message is not None
                _deliver_sse(_bounded_lines(resp), self._on_message)
            except (httpx.HTTPError, OSError, RuntimeError, TransportError):
                pass
            finally:
                resp.close()

        self._get_thread = threading.Thread(target=_listen, name=f"mcp-{self.name}-get", daemon=True)
        self._get_thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.session_id:
            try:
                self._client.request("DELETE", self.url, headers=self._headers("application/json"),
                                     timeout=3.0)
            except Exception:
                pass
        with self._lock:
            streams, self._streams = self._streams, []
        for r in streams:
            try:
                r.close()
            except Exception:
                pass
        try:
            self._client.close()
        except Exception:
            pass


# ─── Legacy HTTP+SSE (2024-11-05) ────────────────────────────────────────

class LegacySSETransport(Transport):
    """GET an SSE stream; the first 'endpoint' event gives the POST URL for messages."""

    kind = "sse"

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60.0,
                 client: httpx.Client | None = None, name: str = "server",
                 connect_timeout: float = 15.0, header_provider: HeaderProvider | None = None,
                 on_unauthorized: OnUnauthorized | None = None) -> None:
        self.url = url
        self.name = name
        self.headers = dict(headers or {})
        self.header_provider = header_provider
        self.on_unauthorized = on_unauthorized
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0, read=None),
                                              follow_redirects=True)
        self.endpoint: str | None = None
        self._endpoint_ready = threading.Event()
        self._error: str | None = None
        self._stream: httpx.Response | None = None
        self._closed = False
        self._error_status: int | None = None
        self._error_headers: dict[str, str] = {}

    def _all_headers(self) -> dict[str, str]:
        h = dict(self.headers)
        if self.header_provider is not None:
            h.update(self.header_provider())
        return h

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        def _listen() -> None:
            try:
                resp = None
                for attempt in range(2):
                    req = self._client.build_request("GET", self.url,
                                                     headers={"Accept": "text/event-stream", **self._all_headers()})
                    resp = self._client.send(req, stream=True)
                    if attempt == 0 and resp.status_code in (401, 403) and self.on_unauthorized is not None:
                        try:
                            refreshed = bool(self.on_unauthorized())
                        except Exception:
                            refreshed = False
                        if refreshed:
                            resp.close()
                            continue
                    break
                assert resp is not None
            except httpx.HTTPError as e:
                self._error = f"SSE connect failed: {e}"
                self._endpoint_ready.set()
                return
            except Exception as e:  # header provider failures
                self._error = f"SSE connect failed: {e}"
                self._endpoint_ready.set()
                return
            if resp.status_code >= 400:
                self._error = f"HTTP {resp.status_code} from {self.url}"
                self._error_status = resp.status_code
                self._error_headers = dict(resp.headers)
                resp.close()
                self._endpoint_ready.set()
                return
            self._stream = resp
            try:
                for event, data, _ in iter_sse(_bounded_lines(resp)):
                    if event == "endpoint":
                        endpoint = urljoin(self.url, data.strip())
                        a, b = urlsplit(endpoint), urlsplit(self.url)
                        if (a.scheme, a.netloc) != (b.scheme, b.netloc):
                            # POSTing there would send our credentials to a host the user never configured
                            self._error = f"SSE endpoint {endpoint[:80]} is not on the server's own origin"
                            self._endpoint_ready.set()
                            return
                        self.endpoint = endpoint
                        self._endpoint_ready.set()
                    elif event == "message":
                        try:
                            _dispatch(json.loads(data), on_message)
                        except ValueError:
                            continue
            except (httpx.HTTPError, OSError, RuntimeError, TransportError):
                pass
            finally:
                resp.close()
                self._endpoint_ready.set()
                if not self._closed:
                    on_close("SSE stream closed")

        threading.Thread(target=_listen, name=f"mcp-{self.name}-sse", daemon=True).start()
        if not self._endpoint_ready.wait(self.connect_timeout):
            raise TransportError("timed out waiting for SSE 'endpoint' event")
        if self.endpoint is None:
            raise TransportError(self._error or "SSE stream ended before 'endpoint' event",
                                 status=self._error_status, headers=self._error_headers)

    def send(self, message: dict[str, Any]) -> None:
        if self._closed or not self.endpoint:
            raise TransportError("SSE transport not connected")
        try:
            resp = self._client.post(self.endpoint, content=json.dumps(message).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **self._all_headers()},
                                     timeout=self.timeout)
        except httpx.HTTPError as e:
            raise TransportError(f"HTTP request failed: {e}") from e
        if resp.status_code >= 400:
            raise TransportError(f"HTTP {resp.status_code} from {self.endpoint}", status=resp.status_code,
                                 headers=dict(resp.headers))

    def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        try:
            self._client.close()
        except Exception:
            pass
