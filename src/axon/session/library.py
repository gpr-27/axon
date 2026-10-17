"""
The library: everything Axon and you created or ran, saved under the project and shown by ``/library``.

Every event below is written twice: to the session transcript (``sessions/<id>.jsonl``, shown by
``/history``) and to the project's activity journal (``activity.jsonl``, shown by ``/activity``):

    skill_run        a skill ran (typed ``/name`` or called by the model): name, args, scope, result
    subagent_result  a subagent finished: type, prompt, status, final report, tokens, cost, time
    hook_run         a hook ran: event, blocked, message, added context, permission decision
    command_run      a custom command or MCP prompt ran
    plan             a plan was saved, approved or rejected
    saved            a skill, agent, command, memory, rule, output style or instructions file was
                     created, edited or deleted (by you, a command, or the model)

Saved files are also copied, one version per change, to
``~/.axon/projects/<project-key>/library/<type>/<name>/<stamp>-<file>``, so a skill, agent or
memory survives even when the file in the repo is edited or deleted. Identical versions are not
copied twice. Deleted files get a final copy before they go.

Every public function is best-effort and never raises: saving must never break a turn.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

# Session record type -> activity journal kind.
RUN_KINDS: dict[str, str] = {
    "skill_run": "skill",
    "subagent_result": "subagent",
    "hook_run": "hook",
    "command_run": "custom_command",
    "plan": "plan",
    "saved": "saved",
}

SAVED_TYPES = ("skill", "agent", "command", "memory", "rule", "output_style", "prompt", "instructions", "plugin")
MAX_COPY_BYTES = 5 * 1024 * 1024
PREVIEW_CHARS = 4000


# ─── Classifying files ──────────────────────────────────────────────────────

def _home() -> Path:
    from axon import storage
    return storage.axon_home()


def _rel_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    try:
        return path.resolve().relative_to(root.resolve()).parts
    except (ValueError, OSError):
        return None


def classify(path: Path | str, workspace: Path | str | None = None) -> dict[str, str] | None:
    """What kind of library item ``path`` is: {"type", "name", "scope"} or None.

    Recognised: ``.axon``/``.claude`` folders in the project and ``~/.axon`` / ``~/.claude``:
    ``skills/<name>/**``, ``agents/<name>.md``, ``commands/**/<name>.md``, ``memory/*.md``,
    ``rules/*.md``, ``output-styles/*.md``, ``plugins/<name>/**``; the project's auto-memory folder
    ``~/.axon/projects/<key>/memory``; and ``AXON.md`` / ``CLAUDE.md`` / ``AGENTS.md`` files."""
    try:
        p = Path(path).expanduser()
        if workspace is not None and not p.is_absolute():
            p = Path(workspace) / p
        if p.name in ("AXON.md", "CLAUDE.md", "AGENTS.md", "AXON.local.md", "CLAUDE.local.md"):
            home = _home()
            scope = "user" if _rel_parts(p, home) is not None or _rel_parts(p, Path.home() / ".claude") is not None else "project"
            return {"type": "instructions", "name": p.name, "scope": scope}
        if workspace is not None:
            from axon import storage
            auto = storage.project_dir(Path(workspace), create=False) / "memory"
            if _rel_parts(p, auto) is not None and p.suffix == ".md":
                return {"type": "memory", "name": p.stem, "scope": "auto"}
        roots: list[tuple[Path, str]] = [(_home(), "user"), (Path.home() / ".claude", "user")]
        if workspace is not None:
            ws = Path(workspace)
            roots += [(ws / ".axon", "project"), (ws / ".claude", "project")]
        for root, scope in roots:
            parts = _rel_parts(p, root)
            if not parts or len(parts) < 2:
                continue
            head, rest = parts[0], parts[1:]
            if head == "skills":
                return {"type": "skill", "name": rest[0] if len(rest) > 1 else Path(rest[0]).stem, "scope": scope}
            if head == "plugins":
                return {"type": "plugin", "name": rest[0], "scope": scope}
            if p.suffix != ".md":
                continue
            if head == "agents":
                return {"type": "agent", "name": p.stem, "scope": scope}
            if head == "commands":
                return {"type": "command", "name": ":".join([*rest[:-1], p.stem]), "scope": scope}
            if head == "memory":
                return {"type": "memory", "name": p.stem, "scope": "global" if scope == "user" else scope}
            if head == "rules":
                return {"type": "rule", "name": p.stem, "scope": scope}
            if head == "output-styles":
                return {"type": "output_style", "name": p.stem, "scope": scope}
            if head == "prompts":
                return {"type": "prompt", "name": p.stem, "scope": scope}
        return None
    except Exception:
        return None


# ─── Version copies ─────────────────────────────────────────────────────────

def library_dir(workspace: Path | str) -> Path:
    from axon import storage
    return storage.project_dir(Path(workspace)) / "library"


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-")[:120] or "item"


def snapshot(path: Path | str, workspace: Path | str, item: dict[str, str]) -> Path | None:
    """Copy ``path`` into the library (skipped when identical to the newest copy). Returns the copy."""
    try:
        src = Path(path)
        if not src.is_file() or src.stat().st_size > MAX_COPY_BYTES:
            return None
        data = src.read_bytes()
        folder = library_dir(workspace) / _safe(item["type"]) / _safe(item["name"])
        folder.mkdir(parents=True, exist_ok=True)
        existing = sorted(folder.glob(f"*-{_safe(src.name)}"))
        if existing and existing[-1].read_bytes() == data:
            return existing[-1]
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        dest = folder / f"{stamp}-{_safe(src.name)}"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest
    except Exception:
        return None


# ─── Recording ──────────────────────────────────────────────────────────────

def _session_of(agent: Any) -> Any:
    return getattr(agent, "session", None) if agent is not None else None


def record_run(record_type: str, data: dict[str, Any], *, agent: Any = None,
               workspace: Path | str | None = None) -> None:
    """Write one event to the session transcript (when an agent is given) and the activity journal."""
    kind = RUN_KINDS.get(record_type, record_type)
    try:
        from axon.session.activity import redact
        clean = redact(data)
    except Exception:
        clean = data
    store = _session_of(agent)
    if store is not None and getattr(store, "active_session_id", ""):
        try:
            store.append(record_type, clean)
        except Exception:
            pass
    journal = getattr(agent, "journal", None) if agent is not None else None
    if callable(journal):
        try:
            journal(kind, clean)
            return
        except Exception:
            pass
    try:
        from axon.session import activity
        activity.record(kind, clean, workspace=workspace)
    except Exception:
        pass


def record_saved(path: Path | str, *, action: str = "created", source: str = "command",
                 workspace: Path | str | None = None, agent: Any = None,
                 item: dict[str, str] | None = None) -> dict[str, Any] | None:
    """A library file was created/edited/deleted. Copies it into the library and records it.
    Call with ``action="deleted"`` *before* removing the file so its last version is kept.
    Returns the record, or None when the path is not a library item."""
    try:
        if workspace is None and agent is not None:
            workspace = getattr(getattr(agent, "settings", None), "workspace", None)
        if workspace is None:
            workspace = Path.cwd()
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(workspace) / p
        item = item or classify(p, workspace)
        if item is None:
            return None
        copy = snapshot(p, workspace, item)
        rec: dict[str, Any] = {**item, "action": action, "source": source, "path": str(p)}
        if copy is not None:
            rec["copy"] = str(copy)
        record_run("saved", rec, agent=agent, workspace=workspace)
        return rec
    except Exception:
        return None


def library_roots(workspace: Path | str) -> list[Path]:
    """Folders whose files are library items (project and user-wide)."""
    from axon import storage
    ws = Path(workspace)
    kinds = ("skills", "agents", "commands", "memory", "rules", "output-styles", "prompts")
    roots = [base / k for base in (ws / ".axon", ws / ".claude", _home()) for k in kinds]
    return roots + [storage.project_dir(ws, create=False) / "memory"]


def sync_all(workspace: Path | str, agent: Any = None) -> int:
    """Copy every library file that has no identical saved version yet (files made by hand, by a
    shell command, by git, ...). Returns how many new versions were saved."""
    ws = Path(workspace)
    n = 0
    candidates: list[Path] = []
    for root in library_roots(ws):
        try:
            if root.is_dir():
                candidates += [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
        except OSError:
            continue
    candidates += [p for p in (ws / "AXON.md", ws / "CLAUDE.md", ws / "AGENTS.md", _home() / "AXON.md") if p.is_file()]
    for p in candidates[:2000]:
        item = classify(p, ws)
        if item is None:
            continue
        folder = library_dir(ws) / _safe(item["type"]) / _safe(item["name"])
        existing = sorted(folder.glob(f"*-{_safe(p.name)}")) if folder.is_dir() else []
        try:
            if existing and existing[-1].read_bytes() == p.read_bytes():
                continue
        except OSError:
            continue
        if record_saved(p, action="found" if not existing else "changed", source="sync", workspace=ws,
                        agent=agent, item=item):
            n += 1
    return n


def preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + f"\n… [{len(s) - limit} more chars]"


# ─── Collecting everything for /library ─────────────────────────────────────

SECTIONS = ("sessions", "subagents", "skills", "agents", "commands", "memory", "hooks", "plans",
            "todos", "files", "outputs", "library", "usage")


def _files(d: Path, pattern: str = "*") -> list[Path]:
    try:
        return sorted((p for p in d.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _size(paths: Iterable[Path]) -> int:
    n = 0
    for p in paths:
        try:
            n += p.stat().st_size
        except OSError:
            pass
    return n


def collect(workspace: Path | str) -> dict[str, Any]:
    """Everything stored for this project (and the user-wide items it uses), grouped by section."""
    from axon import storage
    from axon.session import activity
    ws = Path(workspace)
    pd = storage.project_dir(ws)
    out: dict[str, Any] = {"project": str(ws), "data_dir": str(pd)}
    journal = activity.read(ws)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for e in journal:
        by_kind.setdefault(str(e.get("kind")), []).append(e)

    sess = _files(pd / "sessions", "*.jsonl")
    main = [p for p in sess if "_sub_" not in p.stem]
    subs = [p for p in sess if "_sub_" in p.stem]
    out["sessions"] = {"count": len(main), "path": str(pd / "sessions"),
                       "items": [{"id": p.stem, "path": str(p), "bytes": p.stat().st_size,
                                  "modified": p.stat().st_mtime} for p in main]}
    out["subagents"] = {"transcripts": len(subs),
                        "runs": [e.get("data", {}) | {"t": e.get("t"), "session_id": e.get("session_id")}
                                 for e in by_kind.get("subagent", [])],
                        "items": [{"id": p.stem, "path": str(p)} for p in subs]}

    skills: list[dict[str, Any]] = []
    try:
        from axon.skills.manager import SkillManager
        for s in SkillManager(ws).discover().values():
            skills.append({"name": s.name, "scope": s.scope, "description": s.description,
                           "path": "" if s.scope == "bundled" else str(s.path)})
    except Exception:
        pass
    out["skills"] = {"items": sorted(skills, key=lambda s: (s["scope"] == "bundled", s["name"])),
                     "runs": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("skill", [])]}

    agents: list[dict[str, Any]] = []
    try:
        from axon.agent.subagent import load_agent_definitions
        for a in load_agent_definitions(ws):
            agents.append({"name": a.name, "scope": a.source, "description": a.description,
                           "path": str(a.path or "")})
    except Exception:
        pass
    out["agents"] = {"items": agents}

    cmds: list[dict[str, Any]] = []
    try:
        from axon.commands.custom import load_custom_commands
        for c in load_custom_commands(ws).values():
            cmds.append({"name": c.name, "scope": c.source, "description": c.description, "path": str(c.path)})
    except Exception:
        pass
    out["commands"] = {"items": sorted(cmds, key=lambda c: c["name"]),
                       "runs": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("custom_command", [])]}

    mem: list[dict[str, Any]] = []
    try:
        from axon.agent.memory import MemoryStore
        for it in MemoryStore(ws).list_all():
            base = MemoryStore(ws).global_memory_dir if it.scope == "global" else MemoryStore(ws).project_memory_dir
            mem.append({"name": it.id, "scope": it.scope, "title": it.title, "path": str(base / f"{it.id}.md")})
    except Exception:
        pass
    for p in _files(pd / "memory", "*.md"):
        mem.append({"name": p.stem, "scope": "auto", "title": p.stem, "path": str(p)})
    for p in (ws / "AXON.md", ws / "CLAUDE.md", ws / "AGENTS.md", ws / ".axon" / "AXON.md", _home() / "AXON.md"):
        if p.is_file():
            mem.append({"name": p.name, "scope": "instructions", "title": str(p), "path": str(p)})
    out["memory"] = {"items": mem, "events": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("memory", [])]}

    out["hooks"] = {"runs": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("hook", [])]}
    plans = _files(pd / "plans", "*.json") + _files(pd / "plans", "*.md")
    out["plans"] = {"items": [{"name": p.stem, "path": str(p)} for p in plans],
                    "events": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("plan", [])]}
    out["todos"] = {"items": [{"name": p.stem, "path": str(p)} for p in _files(pd / "todos", "*.json")]}
    cps = [d for d in (pd / "checkpoints").glob("*") if d.is_dir()] if (pd / "checkpoints").is_dir() else []
    out["files"] = {"sessions_with_checkpoints": len(cps), "path": str(pd / "checkpoints"),
                    "bytes": _size(p for d in cps for p in d.rglob("*") if p.is_file())}
    outs = [p for p in (pd / "outputs").rglob("*") if p.is_file()] if (pd / "outputs").is_dir() else []
    out["outputs"] = {"count": len(outs), "path": str(pd / "outputs"), "bytes": _size(outs)}
    lib = library_dir(ws)
    copies = [p for p in lib.rglob("*") if p.is_file()] if lib.is_dir() else []
    out["library"] = {"copies": len(copies), "path": str(lib),
                      "saved": [e.get("data", {}) | {"t": e.get("t")} for e in by_kind.get("saved", [])]}
    key = storage.project_key(ws)
    usage = [r for r in storage.read_jsonl(storage.user_file("usage.jsonl")) if r.get("project") in (key, str(ws))]
    out["usage"] = {"calls": len(usage), "cost": round(sum(float(r.get("cost") or 0) for r in usage), 6)}
    out["counts"] = {
        "journal_events": len(journal),
        **{f"{k}_events": len(v) for k, v in sorted(by_kind.items())},
    }
    return out


# ─── Rendering ──────────────────────────────────────────────────────────────

def _when(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))
    except (TypeError, ValueError):
        return ""


def _one(s: Any, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(inv: dict[str, Any], section: str | None = None, limit: int = 10) -> list[str]:
    """Plain-text lines for /library and ``axon library``."""
    L: list[str] = []
    want = (lambda s: section in (None, s))
    full = section is not None
    n = 10_000 if full else limit

    def head(title: str, extra: str = "") -> None:
        L.append("")
        L.append(f"{title}" + (f"  ({extra})" if extra else ""))

    if section is None:
        L.append(f"Project   {inv['project']}")
        L.append(f"Saved in  {inv['data_dir']}")
    if want("sessions"):
        s = inv["sessions"]
        head("Sessions", f"{s['count']} · /resume, /history")
        for it in s["items"][:n]:
            L.append(f"  {_when(it['modified'])}  {it['id']}  {it['bytes'] // 1024} KB")
    if want("subagents"):
        s = inv["subagents"]
        head("Subagent runs", f"{len(s['runs'])} recorded · {s['transcripts']} transcripts")
        for r in s["runs"][-n:][::-1]:
            L.append(f"  {_when(r.get('t'))}  {r.get('agent_type', '?')}: {_one(r.get('title') or r.get('prompt'), 50)}"
                     f"  [{r.get('status', '?')}, ${float(r.get('cost') or 0):.4f}, {float(r.get('elapsed_s') or 0):.0f}s]")
            if full and r.get("result"):
                L.extend(f"      {line}" for line in str(r["result"]).splitlines()[:40])
        if not s["runs"]:
            for it in s["items"][:n]:
                L.append(f"  {it['id']}")
    if want("skills"):
        s = inv["skills"]
        own = [x for x in s["items"] if x["scope"] != "bundled"]
        head("Skills", f"{len(own)} yours · {len(s['items']) - len(own)} built in · {len(s['runs'])} runs")
        for x in (s["items"] if full else own[:n]):
            L.append(f"  /{x['name']:<22} {x['scope']:<9} {_one(x['description'], 60)}" + (f"\n      {x['path']}" if full and x["path"] else ""))
        for r in s["runs"][-min(n, 5 if not full else n):][::-1]:
            L.append(f"  ran {_when(r.get('t'))}  /{r.get('name')} {_one(r.get('args'), 40)}  ({r.get('source', '')})")
    if want("agents"):
        items = inv["agents"]["items"]
        own = [x for x in items if x["scope"] in ("user", "project")]
        head("Agents", f"{len(own)} yours · {len(items) - len(own)} built in")
        for x in (items if full else own[:n]):
            L.append(f"  {x['name']:<24} {x['scope']:<9} {_one(x['description'], 60)}")
    if want("commands"):
        s = inv["commands"]
        head("Custom commands", f"{len(s['items'])} · {len(s['runs'])} runs")
        for x in s["items"][:n]:
            L.append(f"  /{x['name']:<22} {x['scope']:<9} {_one(x['description'], 60)}")
    if want("memory"):
        s = inv["memory"]
        head("Memory", f"{len(s['items'])} files · {len(s['events'])} changes · /memory")
        for x in s["items"][:n]:
            L.append(f"  [{x['scope']}] {_one(x['title'], 70)}" + (f"\n      {x['path']}" if full else ""))
    if want("hooks"):
        runs = inv["hooks"]["runs"]
        head("Hook runs", f"{len(runs)}")
        for r in runs[-n:][::-1]:
            L.append(f"  {_when(r.get('t'))}  {r.get('event')}{(' ' + str(r.get('tool_name'))) if r.get('tool_name') else ''}"
                     f"{'  BLOCKED' if r.get('blocked') else ''}  {_one(r.get('message'), 60)}")
    if want("plans"):
        s = inv["plans"]
        head("Plans", f"{len(s['items'])} saved · {len(s['events'])} events")
        for x in s["items"][:n]:
            L.append(f"  {x['name']}  {x['path'] if full else ''}".rstrip())
    if want("todos"):
        head("Todo lists", str(len(inv["todos"]["items"])))
        for x in inv["todos"]["items"][:n if full else 3]:
            L.append(f"  {x['name']}")
    if want("files"):
        f = inv["files"]
        head("File history (/rewind)", f"{f['sessions_with_checkpoints']} sessions · {f['bytes'] // 1024} KB")
    if want("outputs"):
        o = inv["outputs"]
        head("Full tool outputs", f"{o['count']} files · {o['bytes'] // 1024} KB")
    if want("library"):
        s = inv["library"]
        head("Saved versions", f"{s['copies']} copies in {s['path']}")
        for r in s["saved"][-n:][::-1]:
            L.append(f"  {_when(r.get('t'))}  {r.get('action'):<8} {r.get('type')}/{r.get('name')}  ({r.get('source')})")
    if want("usage"):
        u = inv["usage"]
        head("Model calls", f"{u['calls']} · ${u['cost']:.4f} · /stats")
    if section is None:
        L.append("")
        L.append("Details: /library <" + "|".join(SECTIONS) + ">   ·   JSON: axon library --json")
    return L


def show(workspace: Path | str, arg: str = "", out: Any = None) -> int:
    import sys
    out = out or sys.stdout
    toks = arg.split()
    as_json = "--json" in toks
    toks = [t for t in toks if not t.startswith("--")]
    section = toks[0].lower() if toks else None
    if section and section not in SECTIONS:
        matches = [s for s in SECTIONS if s.startswith(section)]
        if len(matches) != 1:
            out.write(f"Unknown section '{section}'. Sections: {', '.join(SECTIONS)}\n")
            return 1
        section = matches[0]
    inv = collect(workspace)
    if as_json:
        data = inv if section is None else {section: inv.get(section)}
        out.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
        return 0
    out.write("\n".join(render(inv, section)) + "\n")
    return 0


__all__ = ["RUN_KINDS", "SECTIONS", "classify", "snapshot", "record_run", "record_saved", "preview",
           "collect", "render", "show", "library_dir"]
