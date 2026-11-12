"""One place for every memory scope: the terminal /memory command and the web Memory page both call this.

Scopes (what the owner sees, in the order they are loaded into the system prompt):

  global   ~/.axon/AXON.md                       your instructions for every project (private)
  project  <folder>/AXON.md                      this project's instructions (committed, shared)
  local    <folder>/AXON.local.md                your private notes for this project (git-ignored automatically)
  repo     <folder>/.axon/memory/*.md            notes kept in the repo (shared if committed)
  auto     ~/.axon/projects/<key>/memory/        notes Axon learned about this project (MEMORY.md index + topic files)

File scopes (global, project, local) are plain markdown you can edit anywhere. Note scopes (repo, auto) are folders
of small topic files, handled by ``memory_files``. Every write removes secrets first, keeps the previous version in
the trash, and is atomic. Nothing here talks to the network or a model.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from axon.features import memory_files as mf

KEYS = ("global", "project", "local", "repo", "auto")
FILE_KEYS = ("global", "project", "local")
LABELS = {"global": "Global", "project": "Project", "local": "Local", "repo": "Repo notes", "auto": "Auto memory"}
BLURB = {
    "global": "your instructions for every project (private, ~/.axon/AXON.md)",
    "project": "this project's instructions, shared through git (AXON.md)",
    "local": "your private notes for this project, git-ignored (AXON.local.md)",
    "repo": "notes kept in the repo, shared if committed (.axon/memory)",
    "auto": "what Axon learned about this project, private (~/.axon/projects/...)",
}
ALIASES = {"user": "global", "g": "global", "p": "project", "l": "local", "private": "local", "personal": "local",
           "shared": "project", "notes": "repo", "learned": "auto", "automemory": "auto", "auto-memory": "auto"}
MAX_FILE_BYTES = 200_000
_NOTE_TYPE = {"global": "user", "repo": "project", "auto": "project"}


def norm(key: str) -> str:
    k = (key or "").strip().lower()
    k = ALIASES.get(k, k)
    if k not in KEYS:
        raise ValueError(f"Unknown memory scope '{key}'. Use global, project, local, repo or auto.")
    return k


def is_key(word: str) -> bool:
    try:
        norm(word)
        return True
    except ValueError:
        return False


def file_path(ws: Path, key: str) -> Path | None:
    from axon import storage
    key = norm(key)
    if key == "global":
        return storage.axon_home() / "AXON.md"
    if key == "project":
        return Path(ws) / "AXON.md"
    if key == "local":
        return Path(ws) / "AXON.local.md"
    return None


def notes_dir(ws: Path, key: str) -> Path | None:
    key = norm(key)
    if key == "repo":
        return mf.scope_dir(ws, "repo")
    if key == "auto":
        return mf.scope_dir(ws, "project")
    return None


def short(p: Any) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def auto_enabled(settings: Any = None) -> bool:
    if os.environ.get("AXON_DISABLE_AUTO_MEMORY", "") in ("1", "true"):
        return False
    if settings is not None:
        return bool(getattr(settings, "auto_memory", True))
    try:                                   # no settings object (the banner): the user's settings file decides
        from axon.commands.common import read_settings_file, settings_file
        return bool((read_settings_file(settings_file("user")) or {}).get("autoMemoryEnabled", True))
    except Exception:
        return True


def _stat(p: Path | None) -> tuple[bool, int, float]:
    try:
        st = p.stat() if p is not None else None
    except OSError:
        st = None
    return (st is not None, int(st.st_size) if st else 0, float(st.st_mtime) if st else 0.0)


def _note_stats(d: Path | None, skip_index: bool = False) -> tuple[int, int, float]:
    n = size = 0
    mtime = 0.0
    try:
        for p in (d.glob("*.md") if d is not None and d.is_dir() else []):
            if skip_index and p.name == mf.INDEX:
                continue
            st = p.stat()
            size += st.st_size
            mtime = max(mtime, st.st_mtime)
            if p.name != mf.INDEX:
                n += 1
    except OSError:
        pass
    return n, size, mtime


def overview(ws: Path, settings: Any = None) -> list[dict[str, Any]]:
    """One row per scope: where it is, whether it exists, how big, when it changed, and whether it reaches the model."""
    ws = Path(ws)
    on = auto_enabled(settings)
    rows: list[dict[str, Any]] = []
    for key in KEYS:
        row: dict[str, Any] = {"key": key, "label": LABELS[key], "blurb": BLURB[key], "kind": "file" if key in FILE_KEYS else "notes"}
        if key in FILE_KEYS:
            p = file_path(ws, key)
            exists, size, mtime = _stat(p)
            empty = exists and size == 0
            if exists and size:
                try:
                    empty = not (p.read_text(encoding="utf-8", errors="replace").strip())    # type: ignore[union-attr]
                except OSError:
                    pass
            row.update(path=str(p), display=short(p), exists=exists, size=size, mtime=mtime, count=0,
                       empty=empty or not exists, loaded=bool(exists and not empty))
        else:
            d = notes_dir(ws, key)
            n, size, mtime = _note_stats(d)
            idx_exists, _, _ = _stat((d / mf.INDEX) if d is not None else None)
            row.update(path=str(d), display=short(d) + "/", exists=bool(d and d.is_dir() and (n or idx_exists)), size=size,
                       mtime=mtime, count=n, empty=n == 0)
            # the index of each notes folder is what the model is given at start; auto only while auto memory is on
            row["loaded"] = bool(n) and (on if key == "auto" else True)
            if key == "auto":
                row["enabled"] = on
        rows.append(row)
    return rows


def summary(ws: Path, settings: Any = None) -> str:
    """'memory: 2 files + 5 notes' for the startup banner ('' when nothing is loaded)."""
    rows = overview(ws, settings)
    files = sum(1 for r in rows if r["kind"] == "file" and r["loaded"])
    try:                                              # nested AXON.md / CLAUDE.md files are loaded too
        from axon.agent.memory import discover_instruction_files
        nodes = discover_instruction_files(Path(ws))
        files = sum(1 for n in nodes if getattr(n, "scope", "") not in ("auto", "global-memory", "repo-memory"))
    except Exception:
        pass
    notes = sum(r["count"] for r in rows if r["kind"] == "notes" and r["loaded"])
    if not files and not notes:
        return ""
    bits = []
    if files:
        bits.append(f"{files} file{'s' if files != 1 else ''}")
    if notes:
        bits.append(f"{notes} note{'s' if notes != 1 else ''}")
    return "memory: " + " + ".join(bits)


def _guard_key(ws: Path, key: str) -> None:
    key = norm(key)
    if key not in FILE_KEYS:
        raise ValueError(f"'{key}' is a folder of notes, not one file: use /memory view {key}, or add, forget.")


def read_scope(ws: Path, key: str) -> str:
    """The text of a file scope, or every note of a notes scope (for viewing)."""
    key = norm(key)
    if key in FILE_KEYS:
        p = file_path(ws, key)
        try:
            return p.read_text(encoding="utf-8", errors="replace") if p is not None and p.is_file() else ""   # type: ignore[union-attr]
        except OSError:
            return ""
    out = []
    for m in mf.list_memories(ws, ("repo",) if key == "repo" else ("project",)):
        out.append(f"## {m.name}  ({m.file})\n{m.body.strip()}\n")
    return "\n".join(out)


def ensure_gitignored(ws: Path, entry: str = "AXON.local.md") -> bool:
    """Add ``entry`` to the folder's .gitignore. True when it was added now (so the caller can say so)."""
    gi = Path(ws) / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        if entry in (ln.strip().lstrip("/") for ln in lines):
            return False
        with open(gi, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write(f"{entry}\n")
        return True
    except OSError:
        return False


def _private(p: Path) -> None:
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def write_scope(ws: Path, key: str, text: str, *, expected_sha: str | None = None, _action: str = "",
                _note: str = "") -> dict[str, Any]:
    """Replace a file scope with ``text``. Secrets are redacted, the old version is kept in the memory trash.
    ``expected_sha``: sha256 of the text the caller read; a different current text raises ``memory_history.Conflict``."""
    from axon.features import memory_history as mh
    from axon.session.store import atomic_write_text
    _guard_key(ws, key)
    key = norm(key)
    if len(text.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
        raise ValueError(f"A memory file can be at most {MAX_FILE_BYTES // 1000} KB.")
    p = file_path(ws, key)
    assert p is not None
    mh.check(f"{LABELS[key]} memory", p, expected_sha)
    labels = mf.scan_secrets(text)
    if labels:
        text = mf.redact(text)
    if text and not text.endswith("\n"):
        text += "\n"
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    if old == text:
        return {"ok": True, "action": "unchanged", "key": key, "path": str(p), "redacted": labels, "gitignored": False}
    if old is not None:
        try:
            mf.trash_copy(ws, "global" if key == "global" else "project", p, "replaced")
        except Exception:
            pass
    atomic_write_text(p, text)
    mh.record(ws, mh.file_key(key), old, text, _action or ("created" if old is None else "updated"), note=_note)
    if key == "global":
        _private(p)
    ignored = ensure_gitignored(ws) if key == "local" else False
    return {"ok": True, "action": "created" if old is None else "updated", "key": key, "path": str(p),
            "redacted": labels, "gitignored": ignored}


def _clean_line(text: str) -> str:
    return " ".join(text.strip().lstrip("-*• ").split())


def append_line(ws: Path, key: str, text: str, *, name: str = "", type_: str = "") -> dict[str, Any]:
    """Add one remembered line to a scope: a bullet at the end of a file scope, a small note in a notes scope."""
    key = norm(key)
    line = _clean_line(text)
    if not line:
        raise ValueError("Nothing to remember.")
    if key in FILE_KEYS:
        cur = read_scope(ws, key)
        if any(_clean_line(x).lower() == line.lower() for x in cur.splitlines()):
            return {"ok": True, "action": "unchanged", "key": key, "path": str(file_path(ws, key)), "redacted": [], "gitignored": False}
        head = "" if cur.strip() else f"# {'Global' if key == 'global' else Path(ws).name} instructions\n\n"
        sep = "" if not cur or cur.endswith("\n") else "\n"
        from axon.features import memory_history as mh
        p0 = file_path(ws, key)
        res = write_scope(ws, key, (cur if cur.strip() else "") + sep + head + f"- {line}\n",
                          expected_sha=mh.sha(cur) if p0 is not None and p0.is_file() else None)
        res["line"] = line
        return res
    typ = type_ if type_ in mf.TYPES else _NOTE_TYPE.get(key, "project")
    wr = mf.write_memory(ws, "repo" if key == "repo" else "project", name=name or mf.derive_description(line)[:80], body=line, type_=typ)
    return {"ok": True, "action": wr.action, "key": key, "path": str(wr.path), "redacted": wr.redacted, "gitignored": False, "line": line}


def delete_scope(ws: Path, key: str) -> dict[str, Any]:
    """Delete a file scope (a copy goes to the memory trash) or every note of a notes scope."""
    key = norm(key)
    if key in FILE_KEYS:
        p = file_path(ws, key)
        assert p is not None
        if not p.is_file():
            return {"ok": True, "removed": 0, "key": key}
        mf.trash_copy(ws, "global" if key == "global" else "project", p, "deleted")
        from axon.features import memory_history as mh
        mh.record(ws, mh.file_key(key), p.read_text(encoding="utf-8", errors="replace"), None, "deleted")
        p.unlink()
        return {"ok": True, "removed": 1, "key": key, "path": str(p)}
    mems = mf.list_memories(ws, ("repo",) if key == "repo" else ("project",))
    for m in mems:
        mf.delete_memory(ws, m)
    return {"ok": True, "removed": len(mems), "key": key}


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def forget_matches(ws: Path, text: str) -> list[dict[str, Any]]:
    """Lines in the file scopes and in the auto / repo notes that contain ``text`` (case-insensitive)."""
    q = _norm_text(text)
    if len(q) < 3:
        return []
    hits: list[dict[str, Any]] = []
    for key in FILE_KEYS:
        p = file_path(ws, key)
        if p is None or not p.is_file():
            continue
        for n, ln in enumerate(read_scope(ws, key).splitlines(), 1):
            if ln.strip() and not ln.lstrip().startswith("#") and q in _norm_text(ln):
                hits.append({"key": key, "path": str(p), "line_no": n, "line": ln.strip()})
    for key in ("auto", "repo"):
        for m in mf.list_memories(ws, ("repo",) if key == "repo" else ("project",)):
            for n, ln in enumerate(m.body.splitlines(), 1):
                if ln.strip() and not ln.lstrip().startswith("#") and q in _norm_text(ln):
                    hits.append({"key": key, "path": str(m.path), "line_no": n, "line": ln.strip(), "memory": m.file})
    return hits


def forget(ws: Path, hits: list[dict[str, Any]]) -> int:
    """Remove the given lines (from :func:`forget_matches`). A note left with no lines is deleted. Returns lines removed."""
    removed = 0
    by_path: dict[str, list[dict[str, Any]]] = {}
    for h in hits:
        by_path.setdefault(h["path"], []).append(h)
    for path, hs in by_path.items():
        key = hs[0]["key"]
        drop = {h["line"] for h in hs}
        if key in FILE_KEYS:
            cur = read_scope(ws, key).splitlines()
            keep = [ln for ln in cur if ln.strip() not in drop]
            removed += len(cur) - len(keep)
            write_scope(ws, key, "\n".join(keep))
            continue
        m = mf.find_memory(ws, f"{'repo' if key == 'repo' else 'project'}:{Path(path).stem}")
        if m is None:
            continue
        cur = m.body.splitlines()
        keep = [ln for ln in cur if ln.strip() not in drop]
        removed += len(cur) - len(keep)
        if not any(ln.strip() and not ln.lstrip().startswith("#") for ln in keep):
            mf.delete_memory(ws, m)
        else:
            mf.replace_body(ws, m, "\n".join(keep))
    return removed


def set_auto(enabled: bool, agents: list[Any] | None = None) -> str | None:
    """Switch auto memory on/off: the user settings file plus every live agent. Returns the file written (or None)."""
    from axon.commands.common import update_settings, update_settings_file
    saved = update_settings_file({"autoMemoryEnabled": bool(enabled)})
    for a in agents or []:
        try:
            update_settings(a, auto_memory=bool(enabled))
            if hasattr(a, "reload_memory"):
                a.reload_memory()
        except Exception:
            pass
    return str(saved) if saved else None


def scan_secrets_in(p: Path) -> list[str]:
    try:
        return mf.scan_secrets(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def stamp(mtime: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""
