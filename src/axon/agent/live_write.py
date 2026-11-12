"""Live view of a file being written: the model's tool input arrives as partial JSON (fine-grained tool
streaming), and this reads path, content and line count out of the unfinished text.

Pure functions plus a small per-call tracker; the terminal spinner row and the web page both use it.
Nothing here executes anything: a call is only ever run from the complete, valid JSON the provider parsed."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

# tool name -> (verb shown while it streams, the input keys that hold the text being written)
WRITE_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Write": ("writing", ("content",)),
    "Edit": ("editing", ("new_string",)),
    "MultiEdit": ("editing", ("new_string",)),
    "NotebookEdit": ("writing", ("new_source",)),
}
_PATH_KEYS = ("path", "file_path", "notebook_path")
_TRAILING_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\(u[0-9a-fA-F]{0,3})?$")


def _decode(body: str) -> str:
    """JSON string body (no quotes), possibly cut mid-escape, to text."""
    m = _TRAILING_ESCAPE.search(body)
    if m:
        body = body[:m.start()] + m.group(1)
    try:
        return json.loads('"' + body + '"', strict=False)
    except ValueError:
        return body.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")


def string_values(raw: str, key: str) -> tuple[list[str], bool]:
    """Every value of ``"key": "..."`` found in the partial JSON *raw*, decoded. The flag says whether the
    last one was still open (its closing quote has not arrived)."""
    out: list[str] = []
    open_last = False
    for m in re.finditer(r'"' + re.escape(key) + r'"\s*:\s*"', raw):
        i = m.end()
        n = len(raw)
        while i < n:
            c = raw[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                break
            i += 1
        closed = i < n and raw[i] == '"'
        out.append(_decode(raw[m.end():min(i, n)]))
        open_last = not closed
    return out, open_last


def snapshot(name: str, raw: str, tail_chars: int = 600) -> dict[str, Any] | None:
    """{name, verb, path, lines, chars, tail} for a write-type tool's unfinished input, or None for other tools."""
    spec = WRITE_TOOLS.get(name)
    if spec is None:
        return None
    verb, keys = spec
    path = ""
    for k in _PATH_KEYS:
        vals, still_open = string_values(raw, k)
        if vals and not still_open:
            path = vals[0]
            break
    text = ""
    for k in keys:
        vals, _ = string_values(raw, k)
        text += "".join(vals)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {"name": name, "verb": verb, "path": path, "lines": lines, "chars": len(text), "tail": text[-tail_chars:]}


def label(snap: dict[str, Any] | None) -> str:
    """'writing src/app.py (42 lines so far)', or '' until the path is known."""
    if not snap or not snap.get("path"):
        return ""
    n = int(snap.get("lines") or 0)
    so_far = f" ({n} line{'s' if n != 1 else ''} so far)" if n else ""
    return f"{snap['verb']} {snap['path']}{so_far}"


class LiveWriteTracker:
    """Collects the fragments of each streaming tool call and says, at most every *interval* seconds,
    what to show. Thread-safe (the provider thread feeds it, the spinner thread never touches it)."""

    def __init__(self, interval: float = 0.12) -> None:
        self.interval = interval
        self._calls: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, call_id: str, name: str) -> None:
        if name in WRITE_TOOLS:
            with self._lock:
                self._calls[call_id] = {"name": name, "parts": [], "t": 0.0, "last": None}

    def feed(self, call_id: str, fragment: str, now: float | None = None, force: bool = False) -> dict[str, Any] | None:
        """The new snapshot when one is due and changed, else None."""
        with self._lock:
            c = self._calls.get(call_id)
            if c is None:
                return None
            c["parts"].append(fragment)
            t = time.monotonic() if now is None else now
            if not force and t - c["t"] < self.interval:
                return None
            c["t"] = t
            raw = "".join(c["parts"])
        snap = snapshot(c["name"], raw)
        if snap is None or snap == c["last"]:
            return None
        c["last"] = snap
        return snap

    def finish(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)

    def active(self, call_id: str) -> bool:
        return call_id in self._calls
