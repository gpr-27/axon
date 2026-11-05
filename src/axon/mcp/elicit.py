"""
MCP elicitation in the terminal: a server can ask you for structured input mid-task.

* form mode: the server describes a flat form (``requestedSchema``); you fill in each field, then accept,
  decline or cancel;
* url mode: the server wants you to finish something in a browser (sign-in); you choose whether to open
  the link.

Dialogs only appear in an interactive session; ``-p`` runs and SDK runs decline (the handler is not
installed, so the capability is not even advertised).
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from typing import Any, Callable

_dialog_lock = threading.Lock()
MAX_URL_CHARS = 8000   # Claude Code's cap on a URL passed to the system URL handler (escaped, 4x for specials)


def _ask(prompt: str) -> str:
    return input(prompt)


def _coerce(field: dict[str, Any], raw: str) -> Any:
    kind = field.get("type", "string")
    if kind == "boolean":
        low = raw.strip().lower()
        if low in ("y", "yes", "true", "1"):
            return True
        if low in ("n", "no", "false", "0"):
            return False
        raise ValueError("answer yes or no")
    if kind == "integer":
        return int(raw)
    if kind == "number":
        return float(raw)
    if kind == "array":
        return [p.strip() for p in raw.split(",") if p.strip()]
    if field.get("enum") and raw not in [str(e) for e in field["enum"]]:
        raise ValueError("choose one of: " + ", ".join(str(e) for e in field["enum"]))
    return raw


def _escaped_len(url: str) -> int:
    return sum(4 if c in "%&'\"\\ $`;|<>()*?[]{}!#" else 1 for c in url)


def terminal_elicitation(server: str, params: dict[str, Any], *, ask: Callable[[str], str] | None = None,
                         opener: Callable[[str], Any] | None = None,
                         out: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Answer one ``elicitation/create`` request. Returns {"action": accept|decline|cancel, "content": {...}}."""
    ask = ask or _ask
    say = out or (lambda m: print(m, file=sys.stderr if not sys.stdout.isatty() else sys.stdout))
    mode = params.get("mode") or "form"
    message = str(params.get("message") or "")
    with _dialog_lock:
        try:
            say(f"\n  MCP server '{server}' asks for input:")
            if message:
                say(f"  {message}")
            if mode == "url":
                url = str(params.get("url") or "")
                say(f"  {url}")
                if not url.startswith(("http://", "https://")):
                    return {"action": "decline"}
                if _escaped_len(url) > MAX_URL_CHARS:
                    say("  (that link is too long to open from here; declining)")
                    return {"action": "decline"}
                answer = ask("  Open this link in your browser? [y/N/c=cancel]: ").strip().lower()
                if answer in ("c", "cancel"):
                    return {"action": "cancel"}
                if answer not in ("y", "yes"):
                    return {"action": "decline"}
                try:
                    (opener or webbrowser.open)(url)
                except Exception:
                    say("  Could not open a browser; open the link manually.")
                return {"action": "accept"}
            schema = params.get("requestedSchema") or {}
            props = schema.get("properties") if isinstance(schema, dict) else None
            required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
            content: dict[str, Any] = {}
            for key, field in (props or {}).items():
                field = field if isinstance(field, dict) else {}
                label = str(field.get("title") or key)
                hint = f" ({field['description']})" if field.get("description") else ""
                opts = f" [{'/'.join(str(e) for e in field['enum'])}]" if field.get("enum") else ""
                default = field.get("default")
                suffix = f" [default: {default}]" if default is not None else ("" if key in required else " (optional)")
                while True:
                    raw = ask(f"  {label}{hint}{opts}{suffix}: ").strip()
                    if raw.lower() in ("/cancel", "cancel"):
                        return {"action": "cancel"}
                    if not raw:
                        if default is not None:
                            content[key] = default
                            break
                        if key in required:
                            say("    this field is required")
                            continue
                        break
                    try:
                        content[key] = _coerce(field, raw)
                        break
                    except ValueError as e:
                        say(f"    {e}")
            choice = ask("  Submit? [Y/n/c=cancel]: ").strip().lower()
            if choice in ("c", "cancel"):
                return {"action": "cancel"}
            if choice in ("n", "no"):
                return {"action": "decline"}
            return {"action": "accept", "content": content}
        except (EOFError, KeyboardInterrupt):
            return {"action": "cancel"}
