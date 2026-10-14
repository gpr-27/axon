"""
`axon serve`: a local HTTP API over the SDK (stdlib ``http.server``).

    axon serve [--port 8765] [--host 127.0.0.1] [--token T] [--workspace DIR] [--mode M]

Binds to loopback by default (``--allow-remote`` is required for any other address) and prints a
random bearer token that every request must send as ``Authorization: Bearer <token>``.

Endpoints
  GET  /health                 liveness + version
  GET  /v1/sessions?limit=N    recent sessions of the served workspace
  GET  /v1/usage?days=N        per-call usage log (~/.axon/usage.jsonl) with totals
  POST /v1/query               {"prompt": str, "options": {...}, "stream": true}
                               stream=true  -> text/event-stream of SDK events (`event: <type>`)
                               stream=false -> JSON {"result": {...}, "events": [...]?}
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 4 * 1024 * 1024
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _host_header_name(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class AxonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], token: str, defaults: Any = None, *,
                 allow_remote: bool = False, max_concurrent: int = 4, verbose: bool = False) -> None:
        from axon.features.sdk import AxonOptions
        self.token = token
        self.defaults = defaults if defaults is not None else AxonOptions()
        self.allow_remote = allow_remote
        self.verbose = verbose
        self.slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self.started_at = time.time()
        if ":" in address[0]:
            import socket
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        host_s = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{host_s}:{port}"

    def workspace(self) -> Path:
        ws = getattr(self.defaults, "workspace", None)
        return Path(ws).expanduser().resolve() if ws else Path.cwd()


class _Handler(BaseHTTPRequestHandler):
    server: AxonServer
    server_version = "axon-serve"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:
            sys.stderr.write("axon serve: %s - %s\n" % (self.address_string(), fmt % args))

    # ── helpers ─────────────────────────────────────────────────────
    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self._send_json(status, {"error": {"status": status, "message": message}}, headers)

    def _authorized(self) -> bool:
        if not self.server.allow_remote and not is_loopback(_host_header_name(self.headers.get("Host", ""))):
            self._error(403, "Host header must be a loopback address (DNS-rebinding protection)")
            return False
        auth = self.headers.get("Authorization", "")
        scheme, _, given = auth.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), self.server.token.encode()):
            self._error(401, "missing or invalid bearer token", {"WWW-Authenticate": 'Bearer realm="axon"'})
            return False
        return True

    def _query_params(self) -> dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    @staticmethod
    def _int(value: str | None, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(value))) if value not in (None, "") else default
        except ValueError:
            return default

    # ── routing ─────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        route = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if route == "/health":
                import axon
                self._send_json(200, {"status": "ok", "version": getattr(axon, "__version__", ""),
                                      "uptime_s": round(time.time() - self.server.started_at, 1),
                                      "workspace": str(self.server.workspace())})
            elif route == "/v1/sessions":
                self._send_json(200, {"sessions": list_sessions(self.server.workspace(),
                                                                self._int(self._query_params().get("limit"), 20, 1, 500))})
            elif route == "/v1/usage":
                p = self._query_params()
                self._send_json(200, usage_report(days=self._int(p.get("days"), 30, 0, 36500),
                                                  limit=self._int(p.get("limit"), 200, 0, 100_000)))
            else:
                self._error(404, f"no route for GET {route}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        route = urlsplit(self.path).path.rstrip("/")
        if route != "/v1/query":
            self._error(404, f"no route for POST {route}")
            return
        body = self._read_json()
        if body is None:
            return
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._error(400, "'prompt' must be a non-empty string")
            return
        from axon.features.sdk import AxonOptions, AxonSession, SDKError
        try:
            options = AxonOptions.from_dict(body.get("options"), base=self.server.defaults)
        except SDKError as e:
            self._error(400, str(e))
            return
        if not self.server.slots.acquire(blocking=False):
            self._error(429, "too many concurrent queries", {"Retry-After": "2"})
            return
        try:
            try:
                session = AxonSession(options)
            except SDKError as e:
                self._error(400, str(e))
                return
            try:
                if body.get("stream", True):
                    self._stream(session, prompt)
                else:
                    events = list(session.query(prompt))
                    payload: dict[str, Any] = {"result": events[-1]}
                    if body.get("include_events"):
                        payload["events"] = events
                    self._send_json(200, payload)
            finally:
                session.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.slots.release()

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(411 if length == 0 else 413, "request body must be JSON with a valid Content-Length "
                        f"(max {MAX_BODY_BYTES} bytes)")
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._error(400, f"invalid JSON body: {e}")
            return None
        if not isinstance(data, dict):
            self._error(400, "request body must be a JSON object")
            return None
        return data

    def _stream(self, session: Any, prompt: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gen = session.query(prompt)
        try:
            for i, ev in enumerate(gen):
                self.wfile.write(sse_frame(ev, i).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            session.cancel()  # client went away: interrupt the turn
        finally:
            gen.close()


def sse_frame(event: dict[str, Any], event_id: int | None = None) -> str:
    """One Server-Sent-Events frame: ``id``, ``event: <type>`` and a single-line JSON ``data``."""
    kind = str(event.get("type", "message"))
    if kind == "system" and event.get("subtype"):
        kind = f"system/{event['subtype']}"
    data = json.dumps(event, ensure_ascii=False, default=str)  # JSON never contains raw newlines
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {data}\n\n"


# ─── data endpoints ────────────────────────────────────────────────────────

def list_sessions(workspace: Path, limit: int = 20) -> list[dict[str, Any]]:
    from axon.session.store import SessionStore
    store = SessionStore(workspace=workspace)
    out = []
    for m in store.list_recent(limit=limit):
        out.append({
            "session_id": m.session_id, "title": m.first_prompt, "model": m.model,
            "messages": m.message_count, "total_tokens": m.total_tokens, "cost_usd": m.total_cost,
            "updated_at": datetime.fromtimestamp(m.created_at, timezone.utc).isoformat(timespec="seconds"),
            "starred": m.is_starred, "tags": list(m.tags),
        })
    return out


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def usage_report(days: int = 30, limit: int = 200) -> dict[str, Any]:
    """Records from ~/.axon/usage.jsonl within ``days`` (0 = all) plus totals by model and source."""
    from axon import storage
    records = storage.read_jsonl(storage.user_file("usage.jsonl"))
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        records = [r for r in records if (_parse_ts(r.get("ts")) or cutoff) >= cutoff]
    totals = {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
              "cache_read_tokens": 0, "cache_write_tokens": 0}
    by_model: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}

    def _n(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    for r in records:
        u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        try:
            cost = float(r.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        row = {"input_tokens": _n(u.get("input")), "output_tokens": _n(u.get("output")),
               "cache_read_tokens": _n(u.get("cache_read")), "cache_write_tokens": _n(u.get("cache_write"))}
        for bucket in (totals, by_model.setdefault(str(r.get("model", "?")), {"calls": 0, "cost_usd": 0.0}),
                       by_source.setdefault(str(r.get("source", "?")), {"calls": 0, "cost_usd": 0.0})):
            bucket["calls"] += 1
            bucket["cost_usd"] += cost
            for k, v in row.items():
                bucket[k] = bucket.get(k, 0) + v
    for bucket in [totals, *by_model.values(), *by_source.values()]:
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return {"days": days, "totals": totals, "by_model": by_model, "by_source": by_source,
            "records": records[-limit:] if limit else []}


# ─── CLI ───────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="axon serve", description="Serve the Axon agent over a local HTTP API (SSE).")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT}; 0 = random)")
    p.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback address")
    p.add_argument("--token", help="Bearer token (default: AXON_SERVE_TOKEN or a random one)")
    p.add_argument("--workspace", help="Workspace served (default: current directory)")
    p.add_argument("--model", help="Default model for queries")
    p.add_argument("--mode", choices=["default", "acceptEdits", "plan", "bypass"], default="acceptEdits",
                   help="Permission mode for queries (default: acceptEdits; bypass must be explicit)")
    p.add_argument("--mcp", action="store_true", help="Connect configured MCP servers for each query")
    p.add_argument("--max-concurrent", type=int, default=4, help="Concurrent queries (default 4)")
    p.add_argument("--verbose", action="store_true", help="Log every request to stderr")
    return p


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str | None = None,
                defaults: Any = None, *, allow_remote: bool = False, max_concurrent: int = 4,
                verbose: bool = False) -> AxonServer:
    if not allow_remote and not is_loopback(host):
        raise ValueError(f"refusing to bind to non-loopback host {host!r} without --allow-remote")
    return AxonServer((host, port), token or secrets.token_urlsafe(32), defaults,
                      allow_remote=allow_remote, max_concurrent=max_concurrent, verbose=verbose)


def serve_main(argv: list[str]) -> int:
    import os
    args = _parser().parse_args(argv)
    from axon.features.sdk import AxonOptions, SDKError, build_settings
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    defaults = AxonOptions(workspace=ws, model=args.model, mode=args.mode, connect_mcp=args.mcp)
    try:
        settings = build_settings(defaults)  # fail fast on bad config
    except SDKError as e:
        sys.stderr.write(f"axon serve: {e}\n")
        return 2
    token = args.token or os.environ.get("AXON_SERVE_TOKEN", "").strip() or None
    try:
        server = make_server(args.host, args.port, token, defaults, allow_remote=args.allow_remote,
                             max_concurrent=args.max_concurrent, verbose=args.verbose)
    except ValueError as e:
        sys.stderr.write(f"axon serve: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"axon serve: cannot bind {args.host}:{args.port}: {e}\n")
        return 2
    out = sys.stdout
    lines = [
        f"Axon API listening on {server.url}",
        f"  workspace : {ws}",
        f"  model     : {settings.model}   mode: {settings.mode}",
        f"  token     : {server.token}",
        "",
        "  curl -N -H \"Authorization: Bearer $AXON_TOKEN\" -H \"Content-Type: application/json\" \\",
        f"       -d '{{\"prompt\": \"hello\"}}' {server.url}/v1/query",
        "",
        "  Press Ctrl+C to stop.",
    ]
    if not is_loopback(args.host):
        lines.insert(1, "  WARNING: bound to a non-loopback address; anyone with the token can run tools.")
    try:
        out.write("\n".join(lines) + "\n")
        out.flush()
    except Exception:
        pass
    def _graceful(*_a: Any) -> None:
        raise KeyboardInterrupt
    try:
        import signal
        if threading.current_thread() is threading.main_thread():
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, _graceful)
            if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
                signal.signal(signal.SIGINT, _graceful)
    except (ImportError, ValueError, OSError):
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        try:
            out.write("\nStopping Axon API...\n")
        except Exception:
            pass
    finally:
        server.server_close()
    return 0


def _serve_command(agent: Any, arg: str) -> None:
    port = (arg or "").strip() or str(DEFAULT_PORT)
    ws = getattr(getattr(agent, "settings", None), "workspace", None) or Path.cwd()
    text = (
        "Run the Axon HTTP API in another terminal:\n"
        f"  axon serve --port {port} --workspace \"{ws}\"\n"
        "It binds to 127.0.0.1 and prints a bearer token. Endpoints: GET /health, GET /v1/sessions,\n"
        "GET /v1/usage, POST /v1/query {\"prompt\": ..., \"options\": {...}, \"stream\": true} (SSE).\n"
        "Python: from axon.features.sdk import query, run, AxonOptions"
    )
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


CLI_SUBCOMMANDS = {"serve": serve_main}

try:
    from axon.features import FeatureCommand
    COMMANDS = [FeatureCommand(name="/serve", handler=_serve_command, category="Integrations",
                               description="Show how to run the local Axon HTTP/SSE API (axon serve)",
                               usage="/serve [port]")]
except (ImportError, AttributeError):
    COMMANDS = []

__all__ = ["AxonServer", "make_server", "serve_main", "sse_frame", "usage_report", "list_sessions",
           "is_loopback", "CLI_SUBCOMMANDS", "COMMANDS"]
