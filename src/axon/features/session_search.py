"""
Full-text search across session transcripts (``session.jsonl``). /find searches file names; this
searches what was said and done. Files are streamed line by line (a line is only parsed when it
contains the query), so huge sessions cost no memory. Encrypted transcripts are skipped and listed.
Plain data in, plain data out; /search in axon.commands.backend_cmds renders it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

SNIPPET = 140


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _snippet(text: str, low_query: str) -> str:
    flat = " ".join(text.split())
    at = flat.lower().find(low_query)
    if at < 0:
        return flat[:SNIPPET]
    start = max(0, at - SNIPPET // 3)
    end = min(len(flat), start + SNIPPET)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _transcripts(sessions_root: Path) -> list[Path]:
    try:
        dirs = sorted(d for d in sessions_root.iterdir() if d.is_dir())
    except OSError:
        return []
    return [d / "session.jsonl" for d in dirs if (d / "session.jsonl").is_file()]


def search_sessions(workspace: Path, query: str, all_projects: bool = False, limit: int = 30) -> dict[str, Any]:
    """{"hits": [{session, project, turn, type, snippet, path}], "skipped_encrypted": [...], "files": n, "truncated": bool}.
    ``turn`` counts the user's prompts up to the match (1 = first prompt)."""
    from axon import storage
    q = (query or "").strip().lower()
    out: dict[str, Any] = {"query": query, "hits": [], "skipped_encrypted": [], "files": 0, "truncated": False}
    if not q:
        return out
    if all_projects:
        roots = sorted(p / "sessions" for p in (storage.axon_home() / "projects").glob("*") if (p / "sessions").is_dir())
    else:
        roots = [storage.sessions_dir(Path(workspace), create=False)]
    for root in roots:
        project = root.parent.name
        for path in _transcripts(root):
            out["files"] += 1
            sid = path.parent.name
            turn = 0
            try:
                f = open(path, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                first = True
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    if first:
                        first = False
                        if line.startswith("ENC:v1:"):
                            out["skipped_encrypted"].append({"session": sid, "project": project, "path": str(path)})
                            break
                    is_user = '"type": "user_message"' in line or '"type":"user_message"' in line
                    if is_user:
                        turn += 1
                    if q not in line.lower():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    for text in _strings(entry.get("data")):
                        if q in text.lower():
                            out["hits"].append({"session": sid, "project": project, "turn": turn, "type": entry.get("type", ""),
                                                "snippet": _snippet(text, q), "path": str(path)})
                            if len(out["hits"]) >= limit:
                                out["truncated"] = True
                                return out
                            break
    return out
