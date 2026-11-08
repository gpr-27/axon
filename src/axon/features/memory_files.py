"""Memory files: scopes, frontmatter, safe writes, trash, index health, staleness, export/import.

Scopes (each is one folder of ``<slug>.md`` files plus a ``MEMORY.md`` index of one-line pointers):

* ``global``  ``~/.axon/memory/``                         every project (private)
* ``project`` ``~/.axon/projects/<key>/memory/``          this project (private; where the model saves)
* ``repo``    ``<workspace>/.axon/memory/``               this project, kept in the repo (shared when committed)

A memory file has frontmatter ``name, description, type (user|feedback|project|reference), scope,
created, updated``. Nothing is deleted or overwritten in place: the previous version goes to the
trash first (``~/.axon/memory-trash/`` for global, ``~/.axon/projects/<key>/memory-trash/<scope>/``
otherwise) and ``restore`` brings it back.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SCOPES = ("global", "project", "repo")
SCOPE_ALIASES = {"user": "global", "auto": "project", "local": "project", "workspace": "repo", "shared": "repo"}
TYPES = ("user", "feedback", "project", "reference")
INDEX = "MEMORY.md"
STALE_DAYS = 90
POINTER_DESC_CHARS = 160
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ITEMS = 500
EXPORT_FORMAT = "axon-memory-export"
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.md$")
_POINTER = re.compile(r"^\s*[-*]\s*\[([^\]]*)\]\(([^)\s]+\.md)\)\s*(?:[-—:]+\s*)?(.*)$")
_CATEGORY_TYPE = {"preferences": "user", "conventions": "feedback", "architecture": "project", "debugging": "reference"}


def norm_scope(scope: str) -> str:
    s = (scope or "").strip().lower()
    s = SCOPE_ALIASES.get(s, s)
    if s not in SCOPES:
        raise ValueError(f"Unknown memory scope '{scope}'. Use global, project or repo.")
    return s


def _home() -> Path:
    from axon import storage
    return storage.axon_home()


def scope_dir(ws: Path, scope: str) -> Path:
    scope = norm_scope(scope)
    if scope == "global":
        return _home() / "memory"
    if scope == "repo":
        return Path(ws) / ".axon" / "memory"
    from axon import storage
    return storage.memory_dir(Path(ws))


def trash_dir(ws: Path, scope: str) -> Path:
    scope = norm_scope(scope)
    if scope == "global":
        return _home() / "memory-trash"
    from axon import storage
    return storage.project_dir(Path(ws)) / "memory-trash" / scope


def index_path(ws: Path, scope: str) -> Path:
    return scope_dir(ws, scope) / INDEX


def today() -> str:
    return date.today().isoformat()


def slugify(text: str, limit: int = 60) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words)[:limit].strip("-") or f"memory-{int(time.time())}"


def one_line(text: str, limit: int = 0) -> str:
    s = " ".join(str(text or "").split())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


# ─── Secrets ────────────────────────────────────────────────────────────────

_SECRET_LABELS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_\-]{16,}"), "API key (sk-…)"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "Google API key"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bglpat-[A-Za-z0-9_\-]{20,}"), "git host token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "Hugging Face token"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
    (re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._\-+/=]{16,}"), "bearer token"),
    (re.compile(r"(?i)\b[A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*\s*[=:]\s*['\"]?[^\s'\"]{6,}"),
     "key/secret/password assignment"),
    (re.compile(r"(?i)://[^/\s:@]+:[^@\s/]{3,}@"), "password in a URL"),
]


def scan_secrets(text: str) -> list[str]:
    """Labels of the secret-looking values in ``text`` (empty when clean)."""
    if not text:
        return []
    found = [label for rx, label in _SECRET_LABELS if rx.search(text)]
    if not found:
        try:
            from axon.session.activity import redact_text
            if redact_text(text) != text:
                found.append("secret value")
        except Exception:
            pass
    return found


def redact(text: str) -> str:
    try:
        from axon.session.activity import redact_text
        return redact_text(text)
    except Exception:
        return text


# ─── Reading ────────────────────────────────────────────────────────────────

@dataclass
class Memory:
    scope: str
    path: Path
    name: str
    description: str
    type: str
    created: str
    updated: str
    body: str
    modern: bool                      # has the name/description/type frontmatter
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.path.stem

    @property
    def file(self) -> str:
        return self.path.name

    def updated_date(self) -> date | None:
        return _parse_date(self.updated)


def _parse_date(value: Any) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(float(s)).date()
    except (ValueError, OverflowError, OSError):
        return None


def derive_description(body: str) -> str:
    for ln in body.splitlines():
        t = ln.strip().lstrip("#").strip()
        if t:
            return one_line(t, POINTER_DESC_CHARS)
    return ""


def parse_memory(path: Path, scope: str) -> Memory | None:
    from axon.agent.memory import split_frontmatter
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:
        return None
    meta, body = split_frontmatter(raw)
    body = body.strip("\n")
    name = str(meta.get("name") or meta.get("title") or "").strip() or path.stem
    desc = str(meta.get("description") or "").strip()
    typ = str(meta.get("type") or "").strip().lower()
    created = str(meta.get("created") or "").strip()
    if not created and meta.get("created_at"):
        d = _parse_date(meta.get("created_at"))
        created = d.isoformat() if d else ""
    updated = str(meta.get("updated") or "").strip()
    if not updated and meta.get("modified"):
        d = _parse_date(meta.get("modified"))
        updated = d.isoformat() if d else ""
    modern = bool(meta.get("name") and meta.get("description") and meta.get("type"))
    if not typ:
        typ = _CATEGORY_TYPE.get(str(meta.get("category") or "").lower(), "project")
    shown_body = re.sub(r"\A#\s+.*?(?:\n+|$)", "", body, count=1).strip() if body.startswith("# ") else body.strip()
    return Memory(scope=scope, path=path, name=name, description=desc or derive_description(shown_body),
                  type=typ, created=created or mtime, updated=updated or mtime, body=shown_body,
                  modern=modern, meta=meta)


def memory_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.glob("*.md")
                      if p.is_file() and p.name != INDEX and not p.name.startswith("."))
    except OSError:
        return []


def list_memories(ws: Path, scopes: tuple[str, ...] | list[str] | None = None, type_: str = "") -> list[Memory]:
    """Every memory, in the order ``/memory`` numbers them: scope, then type, then name."""
    out: list[Memory] = []
    for scope in scopes or SCOPES:
        scope = norm_scope(scope)
        d = scope_dir(ws, scope) if scope != "project" else _project_dir_nocreate(ws)
        if d is None or not d.is_dir():
            continue
        items = [m for m in (parse_memory(p, scope) for p in memory_files(d)) if m is not None]
        items.sort(key=lambda m: (TYPES.index(m.type) if m.type in TYPES else len(TYPES), m.name.lower(), m.file))
        out += items
    if type_:
        out = [m for m in out if m.type == type_]
    return out


def _project_dir_nocreate(ws: Path) -> Path | None:
    import os
    from axon import storage
    if os.environ.get("AXON_AUTO_MEMORY_DIR", "").strip():
        return storage.memory_dir(Path(ws))
    return storage.project_dir(Path(ws), create=False) / "memory"


def find_memory(ws: Path, key: str, items: list[Memory] | None = None) -> Memory | None:
    """By list number, ``scope:id``, id/file name, exact name, or a unique id prefix."""
    items = list_memories(ws) if items is None else items
    key = (key or "").strip()
    if not key:
        return None
    if key.isdigit():
        i = int(key) - 1
        return items[i] if 0 <= i < len(items) else None
    scope = ""
    if ":" in key and key.split(":", 1)[0].lower() in (*SCOPES, *SCOPE_ALIASES):
        scope, key = key.split(":", 1)
        scope = norm_scope(scope)
        items = [m for m in items if m.scope == scope]
    k = key.lower().removesuffix(".md")
    for m in items:
        if m.id.lower() == k or m.name.lower() == k:
            return m
    hits = [m for m in items if m.id.lower().startswith(k)]
    return hits[0] if len(hits) == 1 else None


# ─── Writing ────────────────────────────────────────────────────────────────

def _fm_val(v: str) -> str:
    s = one_line(v).strip("'\"")
    if s.startswith("[") and s.endswith("]"):
        s = f"({s[1:-1]})"
    return s


def render_memory(name: str, description: str, type_: str, scope: str, created: str, updated: str, body: str,
                  extra: dict[str, Any] | None = None) -> str:
    lines = ["---", f"name: {_fm_val(name)}", f"description: {_fm_val(description)}", f"type: {type_}",
             f"scope: {scope}", f"created: {created}", f"updated: {updated}"]
    for k, v in (extra or {}).items():
        if k in ("name", "description", "type", "scope", "created", "updated", "modified") or not re.match(r"^\w+$", str(k)):
            continue
        if isinstance(v, (list, dict)):
            continue
        lines.append(f"{k}: {_fm_val(str(v))}")
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    from axon.session.store import atomic_write_text
    atomic_write_text(path, text)


def trash_copy(ws: Path, scope: str, path: Path, kind: str) -> Path | None:
    """Keep a copy of ``path`` in the trash (kind: replaced | deleted | moved). Returns the copy."""
    try:
        if not path.is_file():
            return None
        d = trash_dir(ws, scope)
        d.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        for old in sorted(d.glob(f"*~*~{path.name}"))[-3:]:    # same bytes already kept recently
            if old.read_bytes() == data:
                return old
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        dest = d / f"{stamp}~{kind}~{path.name}"
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest
    except OSError:
        return None


@dataclass
class WriteResult:
    path: Path
    action: str                      # created | updated | unchanged
    backup: Path | None = None
    redacted: list[str] = field(default_factory=list)
    warning: str | None = None       # index size warning

    def notes(self) -> list[str]:
        out = []
        if self.redacted:
            out.append("Redacted " + ", ".join(self.redacted) + " before saving (secrets are never stored in memory).")
        if self.backup:
            out.append(f"Previous version kept: {self.backup}")
        if self.warning:
            out.append(self.warning)
        return out


def _record(ws: Path, path: Path, action: str, scope: str) -> None:
    try:
        from axon.session.library import record_saved
        record_saved(path, action=action, source="memory", workspace=ws,
                     item={"type": "memory", "name": path.stem, "scope": scope})
    except Exception:
        pass


def write_memory(ws: Path, scope: str, *, name: str, body: str, description: str = "", type_: str = "project",
                 file: str = "", extra: dict[str, Any] | None = None, created: str = "") -> WriteResult:
    """Create or update one memory: secrets redacted, previous version trashed, written atomically, indexed."""
    scope = norm_scope(scope)
    name = one_line(name) or "memory"
    type_ = type_ if type_ in TYPES else "project"
    labels = scan_secrets("\n".join((name, description, body)))
    if labels:
        name, description, body = redact(name), redact(description), redact(body)
    d = scope_dir(ws, scope)
    d.mkdir(parents=True, exist_ok=True)
    fname = file if file and _SAFE_FILE.match(file) else slugify(name) + ".md"
    path = d / fname
    if not file:   # a different memory with the same slug gets a free name
        n = 2
        while path.exists():
            cur = parse_memory(path, scope)
            if cur is not None and (cur.name.lower() == name.lower() or cur.body.strip() == body.strip()):
                break
            path = d / f"{slugify(name)}-{n}.md"
            n += 1
    existing = parse_memory(path, scope) if path.exists() else None
    desc = one_line(description) or derive_description(body)
    stamp = today()
    if existing is not None:
        keep = {k: v for k, v in existing.meta.items() if k not in ("name", "title", "description", "type", "scope")}
        keep.update(extra or {})
        text = render_memory(name, desc, type_, scope, created or existing.created, stamp, body, keep)
    else:
        text = render_memory(name, desc, type_, scope, created or stamp, stamp, body, extra)
    backup = None
    if path.exists():
        try:
            same = path.read_text(encoding="utf-8", errors="replace") == text
        except OSError:
            same = False
        if same:
            upsert_index(ws, scope, name, path.name, desc)
            return WriteResult(path, "unchanged", None, labels)
        backup = trash_copy(ws, scope, path, "replaced")
        if backup is None:
            raise OSError(f"Could not keep the previous version of {path}; nothing was changed.")
    action = "updated" if existing is not None or backup else "created"
    _write_atomic(path, text)
    _record(ws, path, action, scope)
    warning = upsert_index(ws, scope, name, path.name, desc)
    return WriteResult(path, action, backup, labels, warning)


def replace_body(ws: Path, mem: Memory, text: str) -> WriteResult:
    derived = mem.description == derive_description(mem.body)
    return write_memory(ws, mem.scope, name=mem.name, body=text,
                        description=derive_description(text) if derived else mem.description,
                        type_=mem.type, file=mem.file, created=mem.created, extra=mem.meta)


def delete_memory(ws: Path, mem: Memory) -> Path | None:
    """Trash the file (so ``restore`` can bring it back), drop its index line. Returns the trash copy."""
    backup = trash_copy(ws, mem.scope, mem.path, "deleted")
    if backup is None and mem.path.is_file():
        raise OSError(f"Could not keep a copy of {mem.path}; not deleted.")
    _record(ws, mem.path, "deleted", mem.scope)
    try:
        mem.path.unlink()
    except FileNotFoundError:
        pass
    remove_index_entry(ws, mem.scope, mem.file)
    return backup


def move_memory(ws: Path, mem: Memory, dest_scope: str) -> WriteResult:
    dest_scope = norm_scope(dest_scope)
    if dest_scope == mem.scope:
        raise ValueError(f"Already in the {dest_scope} scope.")
    dest = scope_dir(ws, dest_scope) / mem.file
    file = mem.file
    if dest.exists():
        cur = parse_memory(dest, dest_scope)
        if cur is None or cur.body.strip() != mem.body.strip():
            file = f"{mem.path.stem}-{mem.scope}.md"
    res = write_memory(ws, dest_scope, name=mem.name, body=mem.body, description=mem.description,
                       type_=mem.type, file=file, created=mem.created, extra=mem.meta)
    delete_kind = trash_copy(ws, mem.scope, mem.path, "moved")
    if delete_kind is None:
        raise OSError(f"Could not keep a copy of {mem.path}; it was copied to {res.path} but not removed.")
    try:
        mem.path.unlink()
    except FileNotFoundError:
        pass
    remove_index_entry(ws, mem.scope, mem.file)
    return res


def scope_of_path(ws: Path, path: Path) -> str | None:
    """The memory scope whose folder holds ``path`` (a ``*.md`` file directly in it), else None."""
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if p.suffix.lower() != ".md":
        return None
    for scope in SCOPES:
        try:
            if scope_dir(ws, scope).resolve() == p.parent:
                return scope
        except OSError:
            continue
    return None


def guard_write(ws: Path, path: Path, new_text: str) -> tuple[str, list[str]]:
    """For code that writes a memory file itself (the Write/Edit tools): keep the version being replaced in
    the trash and redact secrets. Returns (text to write, notes to show). Other paths pass through."""
    scope = scope_of_path(ws, path)
    if scope is None:
        return new_text, []
    notes: list[str] = []
    labels = scan_secrets(new_text)
    if labels:
        new_text = redact(new_text)
        notes.append(f"Redacted {', '.join(labels)} before saving: secrets are never stored in memory.")
    p = Path(path).expanduser()
    if p.is_file():
        try:
            if p.read_text(encoding="utf-8", errors="replace") != new_text:
                kept = trash_copy(ws, scope, p, "replaced")
                if kept:
                    notes.append(f"Previous version kept: {kept}")
        except OSError:
            pass
    return new_text, notes


# ─── Trash ──────────────────────────────────────────────────────────────────

@dataclass
class TrashEntry:
    scope: str
    path: Path
    original: str
    kind: str
    when: str


def list_trash(ws: Path, scope: str = "") -> list[TrashEntry]:
    out: list[TrashEntry] = []
    for sc in ([norm_scope(scope)] if scope else SCOPES):
        d = trash_dir(ws, sc) if sc == "global" else _project_trash_nocreate(ws, sc)
        if d is None or not d.is_dir():
            continue
        for p in d.iterdir():
            m = re.match(r"^(\d{8}-\d{6}-\d{3})~(replaced|deleted|moved)~(.+)$", p.name)
            if m and p.is_file():
                t = datetime.strptime(m.group(1)[:15], "%Y%m%d-%H%M%S")
                out.append(TrashEntry(sc, p, m.group(3), m.group(2), t.strftime("%Y-%m-%d %H:%M")))
    out.sort(key=lambda e: e.path.name, reverse=True)
    return out


def _project_trash_nocreate(ws: Path, scope: str) -> Path | None:
    from axon import storage
    return storage.project_dir(Path(ws), create=False) / "memory-trash" / scope


def restore(ws: Path, ref: str) -> tuple[Path, Path | None] | None:
    """Bring a trashed file back (``ref``: list number, or a file/name fragment, newest wins).
    Returns (restored path, backup of what it replaced)."""
    entries = list_trash(ws)
    ref = (ref or "").strip()
    if not entries or not ref:
        return None
    if ref.isdigit():
        i = int(ref) - 1
        entry = entries[i] if 0 <= i < len(entries) else None
    else:
        k = ref.lower().removesuffix(".md")
        entry = next((e for e in entries if k in e.original.lower() or k in e.path.name.lower()), None)
    if entry is None:
        return None
    target = scope_dir(ws, entry.scope) / entry.original
    target.parent.mkdir(parents=True, exist_ok=True)
    data = entry.path.read_bytes()
    backup = None
    if target.exists() and target.read_bytes() != data:
        backup = trash_copy(ws, entry.scope, target, "replaced")
    _write_atomic(target, data.decode("utf-8", errors="replace"))
    if target.name != INDEX:
        mem = parse_memory(target, entry.scope)
        if mem is not None:
            upsert_index(ws, entry.scope, mem.name, mem.file, mem.description)
    _record(ws, target, "restored", entry.scope)
    return target, backup


# ─── Index ──────────────────────────────────────────────────────────────────

def pointer_line(name: str, file: str, description: str) -> str:
    desc = one_line(description, POINTER_DESC_CHARS)
    return f"- [{one_line(name)}]({file})" + (f" — {desc}" if desc else "")


def _read_index(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    except OSError:
        return []


def upsert_index(ws: Path, scope: str, name: str, file: str, description: str) -> str | None:
    """Add or replace the pointer for ``file``; returns a size warning when the index is near its limit."""
    from axon.agent.memory import check_memory_index
    idx = index_path(ws, scope)
    lines = _read_index(idx) or ["# Memory index"]
    kept = [ln for ln in lines if not _line_points_to(ln, file)]
    _write_atomic(idx, "\n".join(kept + [pointer_line(name, file, description)]).rstrip() + "\n")
    return check_memory_index(idx)


def remove_index_entry(ws: Path, scope: str, file: str) -> None:
    idx = index_path(ws, scope)
    if not idx.is_file():
        return
    lines = _read_index(idx)
    kept = [ln for ln in lines if not _line_points_to(ln, file)]
    if kept != lines:
        _write_atomic(idx, "\n".join(kept).rstrip() + "\n")


def _line_points_to(line: str, file: str) -> bool:
    m = _POINTER.match(line)
    return bool(m and m.group(2) == file)


def index_pointers(path: Path) -> list[tuple[str, str]]:
    """(file, line) for every pointer line in an index."""
    out = []
    for ln in _read_index(path):
        m = _POINTER.match(ln)
        if m and "/" not in m.group(2) and "://" not in m.group(2):
            out.append((m.group(2), ln))
    return out


def reindex(ws: Path, scope: str, *, migrate: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Rebuild the scope's MEMORY.md from its files. Lines that are not pointers (headings, notes) are kept.
    Legacy files get the standard frontmatter first (their old text is trashed)."""
    from axon.agent.memory import check_memory_index
    scope = norm_scope(scope)
    d = scope_dir(ws, scope)
    rep: dict[str, Any] = {"scope": scope, "dir": str(d), "migrated": [], "added": [], "removed": [], "kept": 0,
                           "changed": False, "warning": None}
    if not d.is_dir():
        return rep
    idx = d / INDEX
    old_lines = _read_index(idx)
    old_ptr = {f for f, _ in index_pointers(idx)}
    mems: list[Memory] = []
    for p in memory_files(d):
        m = parse_memory(p, scope)
        if m is None:
            continue
        if migrate and not m.modern:
            rep["migrated"].append(m.file)
            if not dry_run:
                res = write_memory(ws, scope, name=m.name, body=m.body, description=m.description, type_=m.type,
                                   file=m.file, created=m.created, extra={**m.meta, "updated": m.updated})
                m = parse_memory(res.path, scope) or m
        mems.append(m)
    mems.sort(key=lambda m: (TYPES.index(m.type) if m.type in TYPES else len(TYPES), m.name.lower()))
    prose = [ln for ln in old_lines if not _POINTER.match(ln) or "/" in (_POINTER.match(ln).group(2))]
    while prose and not prose[-1].strip():
        prose.pop()
    if not prose:
        prose = ["# Memory index"]
    new_ptr = [pointer_line(m.name, m.file, m.description) for m in mems]
    text = "\n".join(prose + [""] + new_ptr).rstrip() + "\n" if new_ptr else "\n".join(prose).rstrip() + "\n"
    files = {m.file for m in mems}
    rep["added"] = sorted(files - old_ptr)
    rep["removed"] = sorted(old_ptr - files)
    rep["kept"] = len(files & old_ptr)
    current = idx.read_text(encoding="utf-8", errors="replace") if idx.is_file() else ""
    rep["changed"] = text != current
    if rep["changed"] and not dry_run and (mems or idx.is_file()):
        if idx.is_file():
            trash_copy(ws, scope, idx, "replaced")
        _write_atomic(idx, text)
    rep["warning"] = check_memory_index(idx) if idx.is_file() else None
    return rep


# ─── Doctor ─────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    level: str        # error | warn | info
    code: str
    scope: str
    path: Path | None
    message: str


_PATH_TOKEN = re.compile(r"`([^`\s]+)`|(?<![\w/.:~-])((?:~|\.{1,2})?/?(?:[\w.\-]+/)+[\w.\-]+\.\w{1,8})(?![\w/])")
_PATH_EXT = re.compile(r"\.(py|js|jsx|ts|tsx|md|json|toml|ya?ml|rs|go|java|kt|rb|sh|css|html|sql|c|h|cpp|txt|cfg|ini|lock|swift)$", re.I)


def referenced_paths(body: str) -> list[str]:
    """Repo-looking file paths a memory names (backtick spans and bare ``dir/file.ext`` tokens)."""
    out: list[str] = []
    for m in _PATH_TOKEN.finditer(re.sub(r"```.*?```", "", body, flags=re.S)):
        tok = (m.group(1) or m.group(2) or "").strip().rstrip(".,;:)")
        tok = re.sub(r":\d+(?::\d+)?$", "", tok)
        if (not tok or "://" in tok or any(c in tok for c in "*{}<>$=|()") or tok.startswith(("-", "@"))
                or not _PATH_EXT.search(tok) or (m.group(1) and "/" not in tok and "." not in tok)):
            continue
        if tok not in out:
            out.append(tok)
    return out[:8]


def missing_paths(ws: Path, mem: Memory) -> list[str]:
    import os
    gone = []
    for tok in referenced_paths(mem.body):
        if tok.startswith("~"):
            p = Path(os.path.expanduser(tok))
        elif Path(tok).is_absolute():
            p = Path(tok)
        elif mem.scope == "global":
            continue                      # relative to which project? unknowable
        else:
            p = Path(ws) / tok
        try:
            if not p.exists():
                gone.append(tok)
        except OSError:
            continue
    return gone


def doctor(ws: Path, today_: date | None = None, stale_days: int = STALE_DAYS) -> list[Finding]:
    from axon.agent.memory import check_memory_index
    now = today_ or date.today()
    out: list[Finding] = []
    seen_body: dict[str, Memory] = {}
    for scope in SCOPES:
        d = scope_dir(ws, scope) if scope != "project" else _project_dir_nocreate(ws)
        if d is None or not d.is_dir():
            continue
        idx = d / INDEX
        mems = list_memories(ws, [scope])
        files = {m.file for m in mems}
        ptrs = index_pointers(idx)
        listed = [f for f, _ in ptrs]
        for f in sorted(files - set(listed)):
            out.append(Finding("warn", "orphan", scope, d / f, f"{f} is not in {INDEX} (the model will not see it); /memory reindex adds it"))
        for f in sorted(set(listed) - files):
            out.append(Finding("warn", "broken-link", scope, idx, f"{INDEX} points to {f}, which does not exist; /memory reindex drops it"))
        for f in sorted({f for f in listed if listed.count(f) > 1}):
            out.append(Finding("warn", "duplicate-pointer", scope, idx, f"{INDEX} lists {f} more than once; /memory reindex fixes it"))
        if idx.is_file():
            msg = check_memory_index(idx)
            if msg:
                out.append(Finding("error" if msg.startswith("Error") else "warn", "index-size", scope, idx, msg.removeprefix("Error: ")))
        names: dict[str, Memory] = {}
        for m in mems:
            if not m.modern:
                out.append(Finding("info", "legacy-format", scope, m.path, f"{m.file} has no name/description/type frontmatter; /memory reindex upgrades it"))
            elif m.type not in TYPES:
                out.append(Finding("warn", "bad-type", scope, m.path, f"{m.file} has type '{m.meta.get('type')}', expected one of {', '.join(TYPES)}"))
            prev = names.get(m.name.lower())
            if prev is not None:
                out.append(Finding("warn", "duplicate-name", scope, m.path, f"{m.file} and {prev.file} are both named '{m.name}'"))
            names[m.name.lower()] = m
            key = hashlib.sha1(" ".join(m.body.lower().split()).encode()).hexdigest() if m.body.strip() else ""
            if key and key in seen_body:
                o = seen_body[key]
                out.append(Finding("warn", "duplicate-content", scope, m.path, f"{m.scope}:{m.file} has the same text as {o.scope}:{o.file}"))
            elif key:
                seen_body[key] = m
            labels = scan_secrets(m.body + "\n" + m.description)
            if labels:
                out.append(Finding("error", "secret", scope, m.path, f"{m.file} looks like it contains a {', '.join(labels)}; remove it"))
            up = m.updated_date()
            if up is not None and now - up > timedelta(days=stale_days):
                out.append(Finding("info", "stale", scope, m.path, f"{m.file} was last updated {up.isoformat()} ({(now - up).days} days ago)"))
            gone = missing_paths(ws, m)
            if gone:
                out.append(Finding("warn", "missing-path", scope, m.path, f"{m.file} names path(s) that no longer exist: {', '.join(gone)}"))
    return out


# ─── Export / import ────────────────────────────────────────────────────────

def export_scope(ws: Path, scope: str, dest: Path | None = None) -> tuple[Path, int, list[str]]:
    """Write every memory of a scope to one JSON file. Secrets are redacted. Returns (path, count, redacted files)."""
    scope = norm_scope(scope)
    mems = list_memories(ws, [scope])
    redacted: list[str] = []
    items = []
    for m in mems:
        text = m.path.read_text(encoding="utf-8", errors="replace")
        if scan_secrets(text):
            redacted.append(m.file)
            text = redact(text)
        items.append({"file": m.file, "name": m.name, "type": m.type, "description": m.description, "content": text})
    payload = {"format": EXPORT_FORMAT, "version": 1, "scope": scope, "exported": datetime.now().isoformat(timespec="seconds"),
               "count": len(items), "memories": items}
    if dest is None:
        dest = _home() / "exports" / f"memory-{scope}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    dest = Path(dest).expanduser()
    _write_atomic(dest, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return dest, len(items), redacted


@dataclass
class ImportItem:
    file: str
    name: str
    type: str
    description: str
    body: str
    status: str = "new"            # new | same | exists
    redacted: list[str] = field(default_factory=list)


def read_import(ws: Path, source: Path, scope: str) -> tuple[list[ImportItem], list[str]]:
    """Parse and sanitise an export file. Nothing is written. Imported text is data: frontmatter is rebuilt from
    four plain fields, secrets are redacted, names are reduced to safe file names."""
    scope = norm_scope(scope)
    problems: list[str] = []
    src = Path(source).expanduser()
    if not src.is_file():
        raise ValueError(f"No such file: {src}")
    if src.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError(f"{src.name} is larger than {MAX_IMPORT_BYTES // 1024 // 1024} MB; refusing to import it.")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ValueError(f"{src.name} is not a memory export ({e}).") from e
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT or not isinstance(data.get("memories"), list):
        raise ValueError(f"{src.name} is not an Axon memory export (format '{EXPORT_FORMAT}' expected).")
    from axon.agent.memory import split_frontmatter
    items: list[ImportItem] = []
    taken: set[str] = set()
    d = scope_dir(ws, scope)
    for raw in data["memories"][:MAX_IMPORT_ITEMS]:
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
            problems.append("skipped an entry without text")
            continue
        meta, body = split_frontmatter(raw["content"])
        name = one_line(str(meta.get("name") or raw.get("name") or Path(str(raw.get("file", "memory"))).stem))
        typ = str(meta.get("type") or raw.get("type") or "project").lower()
        typ = typ if typ in TYPES else "project"
        desc = one_line(str(meta.get("description") or raw.get("description") or ""))
        labels = scan_secrets(f"{name}\n{desc}\n{body}")
        if labels:
            name, desc, body = redact(name), redact(desc), redact(body)
        base = Path(str(raw.get("file") or "")).name
        file = base if _SAFE_FILE.match(base) else slugify(name) + ".md"
        status = "new"
        target = d / file
        if target.exists():
            cur = parse_memory(target, scope)
            if cur is not None and cur.body.strip() == body.strip():
                status = "same"
            else:
                status = "exists"
                n = 2
                while (d / f"{Path(file).stem}-imported{'' if n == 2 else n}.md").exists() or f"{Path(file).stem}-imported.md" in taken:
                    n += 1
                    if n > 50:
                        break
                file = f"{Path(file).stem}-imported.md"
        if file in taken:
            file = f"{Path(file).stem}-{len(taken) + 1}.md"
        taken.add(file)
        items.append(ImportItem(file, name or Path(file).stem, typ, desc or derive_description(body), body.strip(), status, labels))
    if len(data["memories"]) > MAX_IMPORT_ITEMS:
        problems.append(f"only the first {MAX_IMPORT_ITEMS} of {len(data['memories'])} entries were read")
    return items, problems


def apply_import(ws: Path, scope: str, items: list[ImportItem]) -> list[WriteResult]:
    out = []
    for it in items:
        if it.status == "same":
            continue
        out.append(write_memory(ws, scope, name=it.name, body=it.body, description=it.description, type_=it.type,
                                file=it.file, extra={"source": "imported", "imported": today()}))
    return out


# ─── Why ────────────────────────────────────────────────────────────────────

def _tok(text: str) -> int:
    return int(len(text) / 3.7)


_REASON = {
    "managed": "managed policy file (organisation-wide)",
    "user": "your user-level instructions (every project)",
    "project": "project instruction file in the directory chain",
    "auto": "project memory index (MEMORY.md of ~/.axon/projects/<key>/memory)",
    "global-memory": "global memory index (every project)",
    "repo-memory": "repo memory index (<workspace>/.axon/memory)",
}


def why_report(loader: Any, ws: Path | None = None) -> dict[str, Any]:
    """What this session loaded (path, scope, why, tokens), what is waiting, and what exists but was skipped."""
    from axon.agent.memory import (AGENTS_FILES, LOCAL_FILES, PRIMARY_FILES, RULE_DIRS, _ancestor_chain, _file_key,
                                   _rule_files, load_saved_legacy_items, managed_instruction_paths)
    ws = Path(ws or loader.workspace)
    loaded: list[dict[str, Any]] = []

    def row(node: Any, depth: int = 0, parent: str = "") -> None:
        reason = _REASON.get(node.scope, node.scope)
        if depth:
            why = f"@import from {parent}"
        elif node.reason == "path-rule":
            why = f"path rule: edited/read {node.trigger} matched {', '.join(node.paths)}"
        elif node.reason == "on-demand":
            why = f"nested file loaded when {node.trigger} was touched"
        elif node.path.parent.name == "rules" or "rules" in node.path.parts[-3:-1]:
            why = f"rule file ({reason})"
        else:
            why = reason
        loaded.append({"path": str(node.path), "scope": node.scope, "why": why, "depth": depth,
                       "tokens": _tok(node.content), "chars": len(node.content)})
        for ch in node.imports:
            row(ch, depth + 1, node.path.name)

    for n in loader.loaded:
        row(n)
    legacy = load_saved_legacy_items(ws)
    for path, scope, text in legacy:
        loaded.append({"path": str(path), "scope": f"saved-{scope}", "depth": 0, "tokens": _tok(text), "chars": len(text),
                       "why": "saved memory inlined (old format, no index; /memory reindex upgrades it)"})
    loaded_keys = set(loader.seen)
    waiting = [{"path": str(r.path), "paths": list(r.paths),
                "why": f"path rule: loads when a file matching {', '.join(r.paths)} is read or edited"} for r in loader.pending_rules]
    pending_keys = {_file_key(r.path) for r in loader.pending_rules}
    skipped: list[dict[str, str]] = []

    def consider(p: Path, kind: str) -> None:
        try:
            if not p.is_file():
                return
            key = _file_key(p)
        except OSError:
            return
        if key in loaded_keys or key in pending_keys:
            return
        s = str(p).replace("\\", "/")
        if loader._excluded(p):
            why = "matches memory_excludes"
        elif loader.mode == "managed-only" and kind != "managed":
            why = "instruction_files=managed-only"
        elif kind == "agents":
            why = f"AGENTS.md is read only when no AXON.md/CLAUDE.md exists (instruction_files={loader.mode})"
        else:
            try:
                empty = not p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                empty = False
            why = "empty file" if empty else "already loaded through another path or unreadable"
        skipped.append({"path": s, "why": why})

    for p in managed_instruction_paths():
        consider(p, "managed")
    for d in _ancestor_chain(loader.workspace):
        for name in PRIMARY_FILES + LOCAL_FILES:
            consider(d / name, "project")
        for name in AGENTS_FILES:
            consider(d / name, "agents")
        for rd in RULE_DIRS:
            for r in _rule_files(d / rd):
                consider(r, "project")
    if not loader.auto_memory:
        skipped.append({"path": str(_project_index(ws)),
                        "why": "auto memory is off (autoMemoryEnabled=false or AXON_DISABLE_AUTO_MEMORY)"})
    from axon.agent.memory import AUTO_MEMORY_MAX_LINES
    for scope in SCOPES:
        idx = _scope_index_nocreate(ws, scope)
        if idx is None or not idx.is_file() or (scope == "project" and not loader.auto_memory):
            continue
        if _file_key(idx) in loaded_keys:
            n = len(_read_index(idx))
            if n > AUTO_MEMORY_MAX_LINES:
                skipped.append({"path": f"{idx} (lines {AUTO_MEMORY_MAX_LINES + 1}-{n})",
                                "why": f"over the {AUTO_MEMORY_MAX_LINES}-line load limit; trim it or /memory reindex"})
        else:
            skipped.append({"path": str(idx), "why": "index has no pointers yet"})
    total = sum(r["tokens"] for r in loaded)
    return {"loaded": loaded, "waiting": waiting, "skipped": skipped, "warnings": list(loader.warnings), "tokens": total,
            "note": "Subfolder AXON.md files load the first time a tool touches that folder."}


def _scope_index_nocreate(ws: Path, scope: str) -> Path | None:
    d = _home() / "memory" if scope == "global" else (Path(ws) / ".axon" / "memory" if scope == "repo" else _project_dir_nocreate(ws))
    return d / INDEX if d is not None else None


def _project_index(ws: Path) -> Path:
    d = _project_dir_nocreate(ws)
    return (d or Path(".")) / INDEX
