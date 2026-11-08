"""LSP: code intelligence (diagnostics, definition, references, hover) from a language server already on PATH.

A minimal JSON-RPC-over-stdio client. Servers start lazily on first use, are shared per workspace and language,
and shut down after IDLE_SECONDS without a request. Nothing is downloaded or installed."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, display_path, int_arg, output_cap, cap_text, resolve_path

IDLE_SECONDS = 300.0
REQUEST_TIMEOUT_S = 30.0
INIT_TIMEOUT_S = 60.0
MAX_LOCATIONS = 100
MAX_OPEN_DOCS = 40


@dataclass(frozen=True)
class ServerSpec:
    key: str
    exe: str
    argv: tuple[str, ...]
    install: str
    init_options: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


_PYRIGHT = ServerSpec("python", "pyright-langserver", ("--stdio",), "npm i -g pyright  (or pip install pyright)")
_TSLS = ServerSpec("typescript", "typescript-language-server", ("--stdio",),
                   "npm i -g typescript typescript-language-server")
_GOPLS = ServerSpec("go", "gopls", (), "go install golang.org/x/tools/gopls@latest")
# Build scripts and proc macros run project code on load: keep them off for a read-only query.
_RUST = ServerSpec("rust", "rust-analyzer", (), "rustup component add rust-analyzer",
                   {"cargo": {"buildScripts": {"enable": False}}, "procMacro": {"enable": False},
                    "checkOnSave": False})
_CLANGD = ServerSpec("c", "clangd", (), "brew install llvm / apt install clangd")

# extension -> (server, languageId)
EXTENSIONS: dict[str, tuple[ServerSpec, str]] = {
    ".py": (_PYRIGHT, "python"), ".pyi": (_PYRIGHT, "python"),
    ".ts": (_TSLS, "typescript"), ".tsx": (_TSLS, "typescriptreact"), ".js": (_TSLS, "javascript"),
    ".jsx": (_TSLS, "javascriptreact"), ".mjs": (_TSLS, "javascript"), ".cjs": (_TSLS, "javascript"),
    ".go": (_GOPLS, "go"), ".rs": (_RUST, "rust"),
    ".c": (_CLANGD, "c"), ".h": (_CLANGD, "c"), ".cc": (_CLANGD, "cpp"), ".cpp": (_CLANGD, "cpp"),
    ".cxx": (_CLANGD, "cpp"), ".hpp": (_CLANGD, "cpp"), ".hh": (_CLANGD, "cpp"),
}
ALL_SERVERS = [_PYRIGHT, _TSLS, _GOPLS, _RUST, _CLANGD]


def available_servers() -> list[ServerSpec]:
    return [s for s in ALL_SERVERS if shutil.which(s.exe)]


def path_to_uri(p: Path) -> str:
    s = str(p).replace(os.sep, "/")
    if not s.startswith("/"):
        s = "/" + s  # Windows drive path
    return "file://" + quote(s, safe="/:")


def uri_to_path(uri: str) -> Path:
    u = urlparse(uri)
    return Path(url2pathname(unquote(u.path)))


def _utf16_units(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _char_to_utf16(line: str, col0: int) -> int:
    return _utf16_units(line[:col0])


def _utf16_to_char(line: str, units: int) -> int:
    n = 0
    for i, c in enumerate(line):
        if n >= units:
            return i
        n += 2 if ord(c) > 0xFFFF else 1
    return len(line)


class LspError(ToolError):
    pass


class LspClient:
    """One language-server process. Thread-safe: requests are serialised by a lock."""

    def __init__(self, spec: ServerSpec, exe: str, root: Path) -> None:
        self.spec, self.root = spec, root
        self.proc = subprocess.Popen([exe, *spec.argv], cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self._wlock = threading.Lock()
        self.lock = threading.RLock()
        self._next_id = 0
        self._pending: dict[int, list[Any]] = {}  # id -> [Event, result, error]
        self._diag: dict[str, tuple[int, list[dict[str, Any]]]] = {}
        self._diag_cv = threading.Condition()
        self._docs: dict[str, tuple[int, str]] = {}  # uri -> (version, content hash)
        self._stderr_tail = b""
        self.last_used = time.monotonic()
        self.dead = False
        self.capabilities: dict[str, Any] = {}
        threading.Thread(target=self._read_loop, daemon=True, name=f"lsp-{spec.key}").start()
        threading.Thread(target=self._drain_stderr, daemon=True, name=f"lsp-err-{spec.key}").start()

    # ── transport ──
    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        data = b"Content-Length: %d\r\n\r\n" % len(body) + body
        with self._wlock:
            try:
                self.proc.stdin.write(data)  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError) as e:
                self.dead = True
                raise LspError(f"{self.spec.exe} is not running ({e}).{self._why()}") from e

    def _why(self) -> str:
        tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
        return f" Server stderr: {tail[-400:]}" if tail else ""

    def _drain_stderr(self) -> None:
        try:
            for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):  # type: ignore[union-attr]
                self._stderr_tail = (self._stderr_tail + chunk)[-2000:]
        except (OSError, ValueError):
            pass

    def _read_message(self) -> dict[str, Any] | None:
        out = self.proc.stdout
        length = -1
        while True:
            line = out.readline()  # type: ignore[union-attr]
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1])
                except ValueError:
                    length = -1
        if length < 0:
            return {}
        body = b""
        while len(body) < length:
            chunk = out.read(length - len(body))  # type: ignore[union-attr]
            if not chunk:
                return None
            body += chunk
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return {}

    def _read_loop(self) -> None:
        try:
            while True:
                msg = self._read_message()
                if msg is None:
                    break
                if msg:
                    self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.dead = True
            for slot in list(self._pending.values()):
                slot[2] = {"message": f"{self.spec.exe} exited.{self._why()}"}
                slot[0].set()
            with self._diag_cv:
                self._diag_cv.notify_all()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            method, mid = msg["method"], msg.get("id")
            if mid is not None:  # a request from the server: answer so it never blocks on us
                result: Any = None
                if method == "workspace/configuration":
                    result = [None] * len((msg.get("params") or {}).get("items") or [])
                elif method == "workspace/workspaceFolders":
                    result = [{"uri": path_to_uri(self.root), "name": self.root.name}]
                try:
                    self._send({"jsonrpc": "2.0", "id": mid, "result": result})
                except LspError:
                    pass
            elif method == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                with self._diag_cv:
                    seq = self._diag.get(p.get("uri", ""), (0, []))[0] + 1
                    self._diag[p.get("uri", "")] = (seq, list(p.get("diagnostics") or []))
                    self._diag_cv.notify_all()
            return
        slot = self._pending.pop(msg.get("id"), None)  # type: ignore[arg-type]
        if slot is not None:
            slot[1], slot[2] = msg.get("result"), msg.get("error")
            slot[0].set()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        with self._wlock:
            self._next_id += 1
            rid = self._next_id
        slot: list[Any] = [threading.Event(), None, None]
        self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while not slot[0].wait(0.1):  # short slices: Esc (KeyboardInterrupt) lands promptly
            if time.monotonic() >= deadline:
                self._pending.pop(rid, None)
                raise LspError(f"{self.spec.exe} did not answer {method} within {timeout:.0f}s "
                               f"(it may still be indexing; try again).")
        if slot[2]:
            raise LspError(f"{self.spec.exe} {method} failed: {(slot[2] or {}).get('message', slot[2])}")
        return slot[1]

    # ── lifecycle ──
    def initialize(self) -> None:
        params = {
            "processId": os.getpid(), "rootUri": path_to_uri(self.root),
            "workspaceFolders": [{"uri": path_to_uri(self.root), "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": False},
                    "publishDiagnostics": {"relatedInformation": False},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "definition": {"linkSupport": True}, "references": {},
                },
                "workspace": {"workspaceFolders": True, "configuration": True},
            },
            "initializationOptions": self.spec.init_options or None,
        }
        res = self.request("initialize", params, INIT_TIMEOUT_S) or {}
        self.capabilities = res.get("capabilities") or {}
        self.notify("initialized", {})

    def shutdown(self) -> None:
        if self.proc.poll() is None:
            try:
                if not self.dead:
                    self.request("shutdown", {}, 3.0)
                    self.notify("exit", {})
            except ToolError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.dead = True

    # ── documents ──
    def sync(self, path: Path, language_id: str) -> tuple[str, bool]:
        """Open or update the document; returns (uri, changed)."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"Cannot read {path.name}: {e}") from e
        uri = path_to_uri(path)
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        cur = self._docs.get(uri)
        if cur is None:
            if len(self._docs) >= MAX_OPEN_DOCS:
                old = next(iter(self._docs))
                self.notify("textDocument/didClose", {"textDocument": {"uri": old}})
                self._docs.pop(old, None)
            self._docs[uri] = (1, digest)
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id, "version": 1,
                                                                    "text": text}})
            return uri, True
        if cur[1] != digest:
            self._docs[uri] = (cur[0] + 1, digest)
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": cur[0] + 1},
                                                    "contentChanges": [{"text": text}]})
            return uri, True
        return uri, False

    def diagnostics(self, uri: str, changed: bool, wait_s: float) -> list[dict[str, Any]] | None:
        """Diagnostics for a synced document, or None if the server published nothing in time."""
        with self._diag_cv:
            base = self._diag.get(uri, (0, []))[0]
        deadline = time.monotonic() + wait_s
        need_new = changed or base == 0
        seen = base
        settle_until = None
        while True:
            with self._diag_cv:
                seq = self._diag.get(uri, (0, []))[0]
                if seq > seen:
                    seen = seq
                    settle_until = time.monotonic() + 0.5  # servers often publish syntactic, then semantic
                if not need_new:
                    return self._diag[uri][1]
                now = time.monotonic()
                if settle_until is not None and now >= settle_until:
                    return self._diag[uri][1]
                if self.dead or now >= deadline:
                    return self._diag[uri][1] if seq > base else None
                self._diag_cv.wait(timeout=0.1)


class _Pool:
    def __init__(self) -> None:
        self.clients: dict[tuple[str, str], LspClient] = {}
        self.lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def get(self, spec: ServerSpec, root: Path) -> LspClient:
        exe = shutil.which(spec.exe)
        if not exe:
            raise ToolError(missing_server_message(spec))
        key = (str(root), spec.key)
        with self.lock:
            c = self.clients.get(key)
            if c is not None and (c.dead or c.proc.poll() is not None):
                self.clients.pop(key, None)
                c = None
            if c is None:
                try:
                    c = LspClient(spec, exe, root)
                except OSError as e:
                    raise ToolError(f"Could not start {spec.exe}: {e}") from e
                try:
                    c.initialize()
                except ToolError:
                    c.shutdown()
                    raise
                self.clients[key] = c
            c.last_used = time.monotonic()
            self._arm()
            return c

    def _arm(self) -> None:
        if self._timer is None or not self._timer.is_alive():
            self._timer = threading.Timer(max(0.05, IDLE_SECONDS / 3), self._sweep)
            self._timer.daemon = True
            self._timer.start()

    def _sweep(self) -> None:
        with self.lock:
            self._timer = None
            now = time.monotonic()
            idle = [(k, c) for k, c in self.clients.items() if c.dead or now - c.last_used >= IDLE_SECONDS]
            for k, _ in idle:
                self.clients.pop(k, None)
            remaining = bool(self.clients)
        for _, c in idle:
            threading.Thread(target=c.shutdown, daemon=True).start()
        if remaining:
            with self.lock:
                self._arm()

    def shutdown_all(self) -> None:
        with self.lock:
            cs = list(self.clients.values())
            self.clients.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        for c in cs:
            try:
                c.shutdown()
            except Exception:
                pass


POOL = _Pool()
atexit.register(POOL.shutdown_all)


def missing_server_message(spec: ServerSpec | None = None, suffix: str = "") -> str:
    if spec is not None:
        return (f"No language server for {suffix or spec.key} files: '{spec.exe}' was not found on PATH. "
                f"Install it: {spec.install}.")
    lines = [f"  {s.exe}: {s.install}" for s in ALL_SERVERS]
    return "No supported language server found on PATH. Install one of:\n" + "\n".join(lines)


# ── formatting ──
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _line_of(path: Path, n: int, cache: dict[Path, list[str]]) -> str:
    lines = cache.get(path)
    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            lines = []
        cache[path] = lines
    return lines[n] if 0 <= n < len(lines) else ""


def _fmt_location(loc: dict[str, Any], ws: Path, cache: dict[Path, list[str]]) -> str | None:
    uri = loc.get("targetUri") or loc.get("uri")
    rng = loc.get("targetSelectionRange") or loc.get("targetRange") or loc.get("range")
    if not uri or not rng:
        return None
    path = uri_to_path(uri)
    line0, units = rng["start"]["line"], rng["start"]["character"]
    text = _line_of(path, line0, cache)
    col = _utf16_to_char(text, units) + 1
    from axon.permissions.paths import is_protected_path
    snippet = "" if is_protected_path(path, ws) else text.strip()[:160]
    return f"{display_path(path, ws)}:{line0 + 1}:{col}" + (f"  {snippet}" if snippet else "")


def _hover_text(contents: Any) -> str:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    if isinstance(contents, list):
        return "\n\n".join(t for t in (_hover_text(c) for c in contents) if t)
    return ""


class LspTool(Tool):
    name: ClassVar[str] = "LSP"
    description: ClassVar[str] = (
        "Code intelligence from a language server on PATH (pyright, typescript-language-server, gopls, "
        "rust-analyzer, clangd). action: diagnostics (errors/warnings of a file, no position needed), definition, "
        "references, hover. Position is 1-based line and column. Prefer it over Grep for symbols."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["diagnostics", "definition", "references", "hover"]},
            "path": {"type": "string", "description": "File path"},
            "line": {"type": "integer", "minimum": 1, "description": "1-based line (not for diagnostics)"},
            "column": {"type": "integer", "minimum": 1, "description": "1-based column (not for diagnostics)"},
        },
        "required": ["action", "path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    @property
    def hidden(self) -> bool:  # type: ignore[override]
        """Not advertised when no supported language server is installed (saves tokens on every turn)."""
        return not available_servers()

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action not in ("diagnostics", "definition", "references", "hover"):
            raise ToolError("LSP 'action' must be one of diagnostics, definition, references, hover.")
        raw = args.get("path") or args.get("file_path") or ""
        if not str(raw).strip():
            raise ToolError("LSP requires 'path'.")
        p = resolve_path(ctx, raw)
        if not p.is_file():
            raise ToolError(f"File not found: {raw}")
        entry = EXTENSIONS.get(p.suffix.lower())
        if entry is None:
            raise ToolError(f"No language server mapping for '{p.suffix or p.name}' files. Supported: "
                            f"{', '.join(sorted(EXTENSIONS))}.")
        spec, lang = entry
        if not shutil.which(spec.exe):
            raise ToolError(missing_server_message(spec, p.suffix))
        line = col = 0
        if action != "diagnostics":
            line = int_arg(args, "line", 0, lo=1, strict=True) if args.get("line") is not None else 0
            col = int_arg(args, "column", 0, lo=1, strict=True) if args.get("column") is not None else 0
            if not line or not col:
                raise ToolError(f"LSP {action} requires 'line' and 'column' (both 1-based).")
        root = Path(ctx.workspace).resolve()
        client = POOL.get(spec, root)
        with client.lock:
            client.last_used = time.monotonic()
            uri, changed = client.sync(p, lang)
            if action == "diagnostics":
                diags = client.diagnostics(uri, changed, 20.0)
                out = self._format_diagnostics(diags, p, ctx)
            else:
                text_lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
                n_lines = len(text_lines) - (1 if text_lines and text_lines[-1] == "" else 0)
                if line > max(1, n_lines):
                    raise ToolError(f"{raw} has only {n_lines} lines.")
                units = _char_to_utf16(text_lines[line - 1], col - 1)
                pos = {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": units}}
                out = self._query(client, action, pos, ctx)
            client.last_used = time.monotonic()
        return cap_text(out, output_cap(ctx, 30_000))

    def _format_diagnostics(self, diags: list[dict[str, Any]] | None, p: Path, ctx: ToolContext) -> str:
        rel = display_path(p, ctx.workspace)
        if diags is None:
            return f"No diagnostics were published for {rel} in time (the server may still be indexing); try again."
        if not diags:
            return f"No diagnostics for {rel}."
        text = p.read_text(encoding="utf-8", errors="replace").split("\n")
        rows = []
        for d in sorted(diags, key=lambda d: (d["range"]["start"]["line"], d["range"]["start"]["character"])):
            s = d["range"]["start"]
            ln = text[s["line"]] if s["line"] < len(text) else ""
            code = f" [{d['code']}]" if d.get("code") not in (None, "") else ""
            src = f" ({d['source']})" if d.get("source") else ""
            msg = " ".join(str(d.get("message", "")).split())
            rows.append(f"{rel}:{s['line'] + 1}:{_utf16_to_char(ln, s['character']) + 1} "
                        f"{_SEVERITY.get(d.get('severity', 1), 'error')}{code}{src}: {msg}")
        n_err = sum(1 for d in diags if d.get("severity", 1) == 1)
        return f"{len(diags)} diagnostic(s), {n_err} error(s):\n" + "\n".join(rows)

    def _query(self, client: LspClient, action: str, pos: dict[str, Any], ctx: ToolContext) -> str:
        ws = Path(ctx.workspace)
        cache: dict[Path, list[str]] = {}
        if action == "hover":
            res = client.request("textDocument/hover", pos)
            text = _hover_text((res or {}).get("contents")).strip() if res else ""
            return text or "No hover information at that position."
        if action == "definition":
            res = client.request("textDocument/definition", pos)
        else:
            res = client.request("textDocument/references", {**pos, "context": {"includeDeclaration": True}})
        locs = [res] if isinstance(res, dict) else list(res or [])
        rows = [r for r in (_fmt_location(l, ws, cache) for l in locs[:MAX_LOCATIONS]) if r]
        if not rows:
            return f"No {action} found at that position."
        more = f"\n[... {len(locs) - MAX_LOCATIONS} more not shown]" if len(locs) > MAX_LOCATIONS else ""
        label = "definition(s)" if action == "definition" else "reference(s)"
        return f"{len(locs)} {label}:\n" + "\n".join(rows) + more

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        from axon.tools.base import default_summary
        return default_summary(output, is_error)
