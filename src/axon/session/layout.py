"""
Project-organised storage: metadata, index, generated READMEs and the legacy migration.

Everything a project produces lives under ``~/.axon/projects/<key>/`` (see :mod:`axon.storage`).
This module keeps that tree self-describing:

* ``projects/<key>/project.json``  name, workspace path, created, last_opened, session count,
  git remote/branch (``last_opened`` is updated on every start);
* ``projects/index.json``           every project: name, path, last opened, sessions, size;
* ``~/.axon/README.md`` and ``projects/<key>/README.md``  generated guides, rewritten only when
  their content changes (the renderers are pure functions);
* :func:`migrate_legacy_sessions`   one-time, idempotent move of ``~/.axon/sessions/*.jsonl``
  (older layout) into the owning project's ``sessions/``: copy -> verify (sha256) -> move the
  original to ``~/.axon/backup/<stamp>/`` (never deleted), with a report and journal entries.

All entry points are best-effort: :func:`startup` never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axon import storage

UNSORTED = "_unsorted"
MIGRATION_MARKER = "legacy-sessions-v1.done"
_PROJECT_DIRS = ("sessions", "outputs", "traces", "checkpoints", "memory", "todos", "trash", "screen",
                 "images", "pastes", "exports", "plans", "debug")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_if_changed(path: Path, text: str) -> bool:
    """Atomically write ``text`` unless the file already holds exactly that. Returns True if written."""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    try:
        from axon.session.store import atomic_write_text
        atomic_write_text(path, text)
        return True
    except OSError:
        return False


def _home_display(p: Path) -> str:
    try:
        return "~/" + Path(p).resolve().relative_to(Path.home().resolve()).as_posix()
    except (ValueError, OSError):
        return str(p)


# ─── Project facts ──────────────────────────────────────────────────────────

def project_root(workspace: Path) -> Path:
    root = storage.find_git_root(Path(workspace)) or Path(workspace)
    try:
        return root.resolve()
    except OSError:
        return root


def project_name(workspace: Path) -> str:
    """Human name of a project: the git root (or workspace) folder name."""
    root = project_root(workspace)
    return root.name or str(root)


def git_info(workspace: Path) -> dict[str, str]:
    """{"remote", "branch"} read straight from .git (no subprocess); empty strings when unknown."""
    root = storage.find_git_root(Path(workspace))
    out = {"remote": "", "branch": ""}
    if root is None:
        return out
    git = root / ".git"
    try:
        if git.is_file():  # worktree: "gitdir: <path>"
            line = git.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                git = Path(line.split(":", 1)[1].strip())
                if not git.is_absolute():
                    git = (root / git).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()  # branch names may contain '/', e.g. feature/login
            out["branch"] = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref.rsplit("/", 1)[-1]
        else:
            out["branch"] = head[:12]
    except OSError:
        pass
    try:
        cfg = git / "config"
        if not cfg.is_file() and (git / "commondir").is_file():
            cfg = (git / (git / "commondir").read_text(encoding="utf-8").strip() / "config").resolve()
        section = ""
        for raw in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = raw.strip()
            if ln.startswith("["):
                section = ln
            elif section == '[remote "origin"]' and ln.startswith("url"):
                out["remote"] = ln.split("=", 1)[1].strip()
                break
    except OSError:
        pass
    return out


def _count(d: Path, pattern: str) -> int:
    try:
        return sum(1 for _ in d.glob(pattern))
    except OSError:
        return 0


def session_count(pdir: Path) -> int:
    d = pdir / "sessions"
    try:
        return sum(1 for p in d.glob("*.jsonl") if "_sub_" not in p.name and not p.name.endswith(".traces.jsonl"))
    except OSError:
        return 0


def dir_size(d: Path) -> int:
    total = 0
    try:
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def project_info(workspace: Path, *, detailed: bool = False) -> dict[str, Any]:
    """Facts about the project ``workspace`` belongs to (cheap unless ``detailed``).

    Keys: name, key, workspace, data_dir, data_dir_display, sessions, memory_files, last_opened,
    created, git_remote, git_branch, screen_logs, trash. With ``detailed``: journal_entries,
    size_bytes, size, memory_paths, global_memory_paths."""
    ws = Path(workspace)
    key = storage.project_key(ws)
    pdir = storage.axon_home() / "projects" / key
    meta = _read_json(pdir / "project.json")
    info: dict[str, Any] = {
        "name": str(meta.get("name") or project_name(ws)),
        "key": key,
        "workspace": str(meta.get("workspace") or project_root(ws)),
        "data_dir": str(pdir),
        "data_dir_display": _home_display(pdir),
        "sessions": session_count(pdir),
        "memory_files": _count(pdir / "memory", "*.md"),
        "screen_logs": _count(pdir / "screen", "*.log"),
        "trash": _count(pdir / "trash", "*"),
        "last_opened": str(meta.get("last_opened") or ""),
        "created": str(meta.get("created") or ""),
        "git_remote": str(meta.get("git_remote") or ""),
        "git_branch": str(meta.get("git_branch") or ""),
    }
    if detailed:
        try:
            from axon.session import activity
            info["journal_entries"] = activity.count(ws)
        except Exception:
            info["journal_entries"] = 0
        info["size_bytes"] = dir_size(pdir)
        info["size"] = _human(info["size_bytes"])
        try:
            info["memory_paths"] = sorted(str(p) for p in (pdir / "memory").glob("*.md"))
        except OSError:
            info["memory_paths"] = []
        try:
            info["global_memory_paths"] = sorted(str(p) for p in (storage.axon_home() / "memory").glob("*.md"))
        except OSError:
            info["global_memory_paths"] = []
    return info


def touch_project(workspace: Path) -> dict[str, Any]:
    """Create/refresh ``project.json`` (name, workspace, created, last_opened, sessions, git)."""
    ws = Path(workspace)
    pdir = storage.project_dir(ws)
    path = pdir / "project.json"
    meta = _read_json(path)
    git = git_info(ws)
    meta.update({
        "name": project_name(ws),
        "workspace": str(project_root(ws)),
        "created": meta.get("created") or _now(),
        "last_opened": _now(),
        "sessions": session_count(pdir),
        "git_remote": git["remote"],
        "git_branch": git["branch"],
    })
    write_if_changed(path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return meta


def index_path() -> Path:
    return storage.axon_home() / "projects" / "index.json"


def update_index(workspace: Path | None = None, *, full: bool = False) -> dict[str, Any]:
    """Refresh ``projects/index.json``: every project dir with its name, path, last opened and
    session count; sizes are recomputed for ``workspace`` (or all projects with ``full``)."""
    root = storage.axon_home() / "projects"
    old = _read_json(index_path()).get("projects", {})
    old = old if isinstance(old, dict) else {}
    current = storage.project_key(Path(workspace)) if workspace is not None else None
    projects: dict[str, Any] = {}
    try:
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        meta = _read_json(d / "project.json")
        prev = old.get(d.name, {}) if isinstance(old.get(d.name), dict) else {}
        size = dir_size(d) if (full or d.name == current or "size_bytes" not in prev) else prev.get("size_bytes", 0)
        projects[d.name] = {
            "name": str(meta.get("name") or (Path(str(meta.get("workspace") or d.name)).name if meta else d.name)),
            "workspace": str(meta.get("workspace") or ""),
            "data_dir": str(d),
            "last_opened": str(meta.get("last_opened") or meta.get("created") or ""),
            "sessions": session_count(d),
            "size_bytes": int(size or 0),
        }
    data = {"version": 1, "projects": projects}
    write_if_changed(index_path(), json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return data


# ─── Generated READMEs (pure renderers) ─────────────────────────────────────

def render_home_readme() -> str:
    """Guide to the whole ~/.axon tree. Deterministic (no timestamps or counts)."""
    return """# Axon data folder

Generated by Axon; it is rewritten only when the layout changes. Everything Axon records lives
here (or under `AXON_HOME`). Nothing is deleted automatically.

## Global (shared by every project)

| Path | What it is |
|---|---|
| `settings.json`, `config.toml`, `.env`, `mcp.json`, `keybindings.json` | your settings, API keys and MCP servers |
| `AXON.md`, `memory/`, `rules/` | user-wide instructions and memory (intentionally global) |
| `agents/`, `commands/`, `skills/`, `plugins/`, `output-styles/`, `prompts/` | your extensions |
| `history.jsonl` | every prompt you typed, tagged with its project |
| `usage.jsonl` | every model call: model, tokens, cache, cost (`/stats`, `axon report`) |
| `logs/`, `cache/`, `bug-reports/` | logs and scratch data |
| `backup/<timestamp>/` | originals kept by migrations (never deleted) |
| `projects/index.json` | all projects: name, path, last opened, sessions, size (`axon projects`) |

## Per project: `projects/<project-key>/`

`<project-key>` is the project's git root (or folder) path with every non-alphanumeric
character replaced by `-`. Each project folder has its own `README.md`.

| Path | What it is |
|---|---|
| `project.json` | name, workspace path, created, last opened, sessions, git remote/branch |
| `activity.jsonl` | append-only journal of everything: prompts, commands, `!` shell, `#` notes, /btw, queue, permissions, settings, turns, tools, errors, rewinds, deletes, skill runs, subagent results, hook runs, plans, saved files (`/activity`, `axon activity`) |
| `sessions/<id>.jsonl` | full transcripts: messages, thinking, tool calls and results, usage, cost, skill runs, subagent results, hook runs, plans, saved files (`/resume`, `/history all`) |
| `sessions/<id>_sub_<n>.jsonl` | subagent transcripts |
| `library/<type>/<name>/` | a copy of every version of every skill, agent, command, memory, rule, prompt and AXON.md, kept even after the original is edited or deleted (`/library`, `axon library`) |
| `outputs/<id>/` | complete tool and shell outputs that were truncated for the model |
| `screen/<stamp>-<id>.log` | plain-text copy of everything shown in the terminal |
| `traces/<id>.jsonl` | per-turn traces (`/trace`, `axon report`) |
| `checkpoints/<id>/` | file snapshots for `/rewind`; `rewound/` keeps what a rewind replaced |
| `memory/` | auto memory for this project |
| `todos/<id>.json` | todo lists |
| `trash/` | deleted sessions (`/restore` lists and restores them) |
| `images/`, `pastes/`, `exports/`, `plans/`, `debug/` | pasted images, large pastes, `/export` files, plans, debug logs |
| `tips.json` | cached usage tips |

## Retention, backup and restore

- Retention: `cleanup_period_days` defaults to `0` = keep forever. Set it to N > 0 to prune
  sessions, traces and todos older than N days at startup (the journal, memory, screen logs and
  trash are never pruned).
- `/delete` moves a session to `projects/<key>/trash/`; `/restore <id>` moves it back.
- `axon purge [project|caches|all]` deletes data on purpose (asks first).
- Back up: copy this whole folder (Axon only appends to journals and transcripts, so a copy taken
  while Axon runs is consistent up to the last line). Restore: copy it back.
- `/project` (or `/where`) shows the current project's folder; `axon projects` lists all.
"""


def render_project_readme(info: dict[str, Any]) -> str:
    """Guide to one project folder from :func:`project_info` facts (no timestamps)."""
    git = ""
    if info.get("git_remote") or info.get("git_branch"):
        git = f"- Git: {info.get('git_remote') or '(no remote)'} · branch `{info.get('git_branch') or '?'}`\n"
    return f"""# {info.get('name', 'project')}

Axon data for `{info.get('workspace', '')}` (generated; updated when these facts change).

- Project key: `{info.get('key', '')}`
{git}- Sessions: {info.get('sessions', 0)} · memory files: {info.get('memory_files', 0)} · screen logs: {info.get('screen_logs', 0)} · trashed sessions: {info.get('trash', 0)}

| Path | What it is |
|---|---|
| `project.json` | metadata (name, path, last opened, git) |
| `activity.jsonl` | append-only journal of every action and event (`/activity`) |
| `sessions/` | transcripts (`/resume <id>`, `/history all`) |
| `library/` | every version of skills, agents, commands, memory, rules and prompts (`/library`) |
| `outputs/` | full tool/shell outputs that were truncated for the model |
| `screen/` | plain-text copies of the terminal |
| `traces/` | per-turn traces |
| `checkpoints/` | /rewind snapshots (`rewound/` keeps replaced versions) |
| `memory/` | this project's auto memory |
| `todos/` | todo lists |
| `trash/` | deleted sessions (`/restore`) |
| `images/` `pastes/` `exports/` `plans/` `debug/` | attachments, exports and logs |

See `../../README.md` for the whole layout.
"""


def write_readmes(workspace: Path | None = None) -> list[Path]:
    """Regenerate ~/.axon/README.md and the project README when their content changed."""
    written: list[Path] = []
    home = storage.ensure_dir(storage.axon_home())
    if write_if_changed(home / "README.md", render_home_readme()):
        written.append(home / "README.md")
    if workspace is not None:
        info = project_info(Path(workspace))
        p = storage.project_dir(Path(workspace)) / "README.md"
        if write_if_changed(p, render_project_readme(info)):
            written.append(p)
    return written


# ─── Legacy migration ───────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _owner_of(path: Path, history: dict[str, str]) -> str | None:
    """Workspace recorded in a transcript (session_meta), else the project in history.jsonl."""
    try:
        from axon.session.store import iter_records
        for i, rec in enumerate(iter_records(path)):
            if rec.get("type") == "session_meta":
                ws = (rec.get("data") or {}).get("workspace")
                if ws:
                    return str(ws)
            if i > 50:
                break
    except Exception:
        pass
    sid = path.name[:-6] if path.name.endswith(".jsonl") else path.stem
    sid = sid.removesuffix(".traces").split("_sub_")[0]
    return history.get(sid)


def _history_owners() -> dict[str, str]:
    out: dict[str, str] = {}
    for rec in storage.read_jsonl(storage.user_file("history.jsonl")):
        sid, proj = str(rec.get("session") or ""), str(rec.get("project") or "")
        if sid and proj and sid not in out:
            out[sid] = proj
    return out


def migrate_legacy_sessions(*, force: bool = False) -> dict[str, Any]:
    """Move ``~/.axon/sessions/*`` into ``projects/<owner>/sessions`` (traces into ``traces/``).

    Per file: copy to a temp file next to the destination, verify the sha256, rename into place,
    then move the original into ``~/.axon/backup/<stamp>/sessions/``. Unknown owners go to
    ``projects/_unsorted/``. Guarded by ``~/.axon/migrations/legacy-sessions-v1.done``; safe to
    re-run (already-migrated files are no longer in the legacy folder). Returns the report dict."""
    home = storage.axon_home()
    marker = home / "migrations" / MIGRATION_MARKER
    report: dict[str, Any] = {"ran": False, "moved": [], "errors": [], "report": "", "backup": ""}
    if marker.exists() and not force:
        return report
    legacy = home / "sessions"
    files = sorted(p for p in legacy.glob("*.jsonl") if p.is_file()) if legacy.is_dir() else []
    if not files:
        _write_marker(marker, "")
        return report
    report["ran"] = True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = home / "backup" / stamp / "sessions"
    report["backup"] = str(backup)
    history = _history_owners()
    owners: dict[str, str | None] = {}
    # main transcripts first so subagent/trace files can inherit their owner
    files.sort(key=lambda p: ("_sub_" in p.name or p.name.endswith(".traces.jsonl"), p.name))
    for src in files:
        base_sid = src.name[:-6].removesuffix(".traces").split("_sub_")[0]
        owner = _owner_of(src, history) or owners.get(base_sid)
        owners.setdefault(base_sid, owner)
        if owner:
            pdir = storage.project_dir(Path(owner))
        else:
            pdir = storage.ensure_dir(home / "projects" / UNSORTED)
        if src.name.endswith(".traces.jsonl"):
            dest_dir, name = pdir / "traces", src.name.replace(".traces.jsonl", ".jsonl")
        else:
            dest_dir, name = pdir / "sessions", src.name
        storage.ensure_dir(dest_dir)
        dest = dest_dir / name
        try:
            digest = _sha256(src)
            if dest.exists() and _sha256(dest) != digest:
                stem = name[:-6]
                dest = dest_dir / f"{stem}_legacy.jsonl"
                n = 2
                while dest.exists() and _sha256(dest) != digest:
                    dest = dest_dir / f"{stem}_legacy{n}.jsonl"
                    n += 1
            if not dest.exists():
                tmp = dest_dir / f".{name}.migrating"
                shutil.copy2(src, tmp)
                if _sha256(tmp) != digest:
                    tmp.unlink(missing_ok=True)
                    raise OSError("copy verification failed (sha256 mismatch)")
                os.replace(tmp, dest)
            if _sha256(dest) != digest:
                raise OSError("destination verification failed")
            storage.ensure_dir(backup)
            kept = backup / src.name
            shutil.move(str(src), str(kept))
            item = {"from": str(src), "to": str(dest), "backup": str(kept), "owner": owner or UNSORTED,
                    "sha256": digest}
            report["moved"].append(item)
            try:
                from axon.session import activity
                activity.record("migration", item, workspace=Path(owner) if owner else home / "projects" / UNSORTED,
                                session_id=base_sid)
            except Exception:
                pass
        except OSError as e:
            report["errors"].append({"file": str(src), "error": str(e)})
    report["report"] = str(_write_report(home, stamp, report))
    if not report["errors"]:
        _write_marker(marker, report["report"])
    return report


def _write_marker(marker: Path, report_path: str) -> None:
    try:
        storage.ensure_dir(marker.parent)
        marker.write_text(json.dumps({"done": _now(), "report": report_path}) + "\n", encoding="utf-8")
    except OSError:
        pass


def _legacy_notes(home: Path) -> list[str]:
    notes = []
    mem = home / "memory"
    if mem.is_dir():
        files = sorted(p.name for p in mem.glob("*.md"))
        if files:
            notes.append(f"Global memory left in place (it is shared by all projects): {', '.join(files)}. "
                         "Move a note into projects/<key>/memory/ yourself if it belongs to one project.")
    for name in ("research", "images", "paste-cache", "exports", "debug"):
        d = home / name
        if d.is_dir() and any(d.iterdir()):
            notes.append(f"{name}/ at the root is legacy data from older versions; it was left untouched "
                         "(new files go to projects/<key>/).")
    return notes


def _write_report(home: Path, stamp: str, report: dict[str, Any]) -> Path:
    d = storage.ensure_dir(home / "backup" / stamp)
    lines = [f"# Axon storage migration {stamp}", "",
             "Legacy transcripts from ~/.axon/sessions were copied into their project folders, verified "
             "(sha256) and the originals moved here (nothing was deleted).", "",
             "| File | Project | Now at | Original kept at |", "|---|---|---|---|"]
    for m in report["moved"]:
        lines.append(f"| {Path(m['from']).name} | {m['owner']} | {m['to']} | {m['backup']} |")
    if report["errors"]:
        lines += ["", "## Not migrated (left in place)", ""]
        lines += [f"- {e['file']}: {e['error']}" for e in report["errors"]]
    notes = _legacy_notes(home)
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    path = d / "MIGRATION_REPORT.md"
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (d / "migration.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return path


# ─── Startup ────────────────────────────────────────────────────────────────

def startup(workspace: Path) -> dict[str, Any]:
    """Run at every start: migrate legacy data once, refresh project.json + index.json and the
    generated READMEs. Never raises; returns {"migration": report, "readmes": [...]}."""
    out: dict[str, Any] = {"migration": {}, "readmes": []}
    t0 = time.time()
    for step in ("migrate", "touch", "readme", "index"):
        try:
            if step == "migrate":
                out["migration"] = migrate_legacy_sessions()
            elif step == "touch":
                out["project"] = touch_project(workspace)
            elif step == "readme":
                out["readmes"] = write_readmes(workspace)
            else:
                update_index(workspace)
        except Exception as e:  # pragma: no cover - defensive
            out.setdefault("errors", []).append(f"{step}: {type(e).__name__}: {e}")
    out["duration_s"] = round(time.time() - t0, 3)
    return out


__all__ = [
    "UNSORTED", "project_name", "project_root", "git_info", "project_info", "touch_project", "update_index",
    "index_path", "render_home_readme", "render_project_readme", "write_readmes", "write_if_changed",
    "migrate_legacy_sessions", "startup", "session_count", "dir_size",
]
