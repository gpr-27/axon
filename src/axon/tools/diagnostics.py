"""Language-server errors appended to the result of Edit, Write and MultiEdit, so the model can fix them at once.

Costs nothing when no language server is installed for the file (one PATH lookup, no thread), and never holds a
tool result for more than TIMEOUT_S: past that the result goes out as it is."""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})
TIMEOUT_S = 2.0
MAX_NEW = 10

_seen: dict[str, set[tuple[int, str]]] = {}   # file -> errors already reported, so only new ones are added
_seen_lock = threading.Lock()


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "lsp_diagnostics_after_edit", True))


def _errors(path: Path, workspace: Path, wait_s: float) -> list[dict[str, Any]] | None:
    from axon.tools import lsp
    entry = lsp.EXTENSIONS.get(path.suffix.lower())
    if entry is None or not shutil.which(entry[0].exe):
        return None
    client = lsp.POOL.get(entry[0], Path(workspace).resolve())
    with client.lock:
        client.last_used = time.monotonic()
        uri, changed = client.sync(path, entry[1])
        diags = client.diagnostics(uri, changed, wait_s)
        client.last_used = time.monotonic()
    return [d for d in (diags or []) if d.get("severity", 1) == 1]


def _format(errors: list[dict[str, Any]], path: Path, workspace: Path) -> str:
    from axon.tools.base import display_path
    key = str(path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    keyed = []
    for d in sorted(errors, key=lambda d: (d["range"]["start"]["line"], d["range"]["start"]["character"])):
        s = d["range"]["start"]
        msg = " ".join(str(d.get("message", "")).split())
        keyed.append(((s["line"] + 1, msg), f"{s['line'] + 1}:{s['character'] + 1} {msg}"))
    with _seen_lock:
        before = _seen.get(key, set())
        _seen[key] = {k for k, _ in keyed}
    new = [text for k, text in keyed if k not in before]
    if not new:
        return ""
    rel = display_path(path, workspace)
    shown = new[:MAX_NEW]
    more = f"\n... and {len(new) - MAX_NEW} more" if len(new) > MAX_NEW else ""
    del lines
    return (f"\n\n[Language server: {len(new)} new error(s) in {rel} after this change]\n"
            + "\n".join(f"{rel}:{t}" for t in shown) + more)


def after_edit(tool_name: str, args: dict[str, Any], content: str, ctx: Any) -> str:
    """*content* with up to MAX_NEW new language-server errors for the edited file appended; unchanged when the
    setting is off, no server is available, nothing is new, or the server does not answer within TIMEOUT_S."""
    try:
        if tool_name not in EDIT_TOOLS or not enabled(ctx.settings):
            return content
        raw = args.get("path") or args.get("file_path")
        if not raw:
            return content
        from axon.tools.base import resolve_path
        path = resolve_path(ctx, raw)
        from axon.tools import lsp
        entry = lsp.EXTENSIONS.get(path.suffix.lower())
        if entry is None or not shutil.which(entry[0].exe) or not path.is_file():
            return content
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["errors"] = _errors(path, Path(ctx.workspace), TIMEOUT_S)
            except Exception:
                box["errors"] = None

        t = threading.Thread(target=work, daemon=True, name="lsp-after-edit")
        t.start()
        t.join(TIMEOUT_S)
        errors = box.get("errors")
        if t.is_alive() or not errors:
            return content
        return content + _format(errors, path, Path(ctx.workspace))
    except Exception:
        return content
