"""
Access to everything Axon stored: the activity journal, the session trash and project folders.

Slash commands: ``/activity`` (alias ``/journal``), ``/restore`` (alias ``/trash``), ``/project``
(alias ``/where``). CLI: ``axon activity ...`` and ``axon projects``.
"""
from __future__ import annotations

import shlex
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from axon.features import FeatureCommand

ACTIVITY_USAGE = """/activity                      the last 30 events of this project
/activity <N>                  the last N events
/activity --kind prompt,shell  only these kinds (--kinds lists them)
/activity --session <id>       only one session (id or prefix)
/activity <text> | --grep <t>  events containing text
/activity --since 2h|3d        only recent events
/activity --all-projects       every project's journal
/activity --export <file>      write the selection (.md or .jsonl)
/activity --path               where the journal lives"""


@dataclass
class ActivityQuery:
    limit: int | None = 30
    kinds: list[str] = field(default_factory=list)
    session: str | None = None
    text: str = ""
    since: float | None = None
    all_projects: bool = False
    export: str = ""
    fmt: str = ""
    list_kinds: bool = False
    show_path: bool = False
    show_all: bool = False
    error: str = ""


def _parse_since(v: str) -> float | None:
    v = v.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        if v and v[-1] in mult:
            return time.time() - float(v[:-1]) * mult[v[-1]]
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def parse_activity_args(arg: str | list[str]) -> ActivityQuery:
    """Parse /activity and `axon activity` arguments (pure; easy to test)."""
    try:
        toks = shlex.split(arg) if isinstance(arg, str) else list(arg)
    except ValueError:
        toks = str(arg).split()
    q = ActivityQuery()
    words: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t in ("--kind", "-k", "--type") and nxt:
            q.kinds += [k for k in nxt.split(",") if k]
            i += 1
        elif t.startswith("--kind="):
            q.kinds += [k for k in t.split("=", 1)[1].split(",") if k]
        elif t in ("--session", "-s") and nxt:
            q.session = nxt
            i += 1
        elif t in ("--grep", "-g", "--text") and nxt:
            q.text = nxt
            i += 1
        elif t == "--since" and nxt:
            q.since = _parse_since(nxt)
            if q.since is None:
                q.error = f"cannot read --since {nxt!r} (use 30m, 2h, 7d or an ISO date)"
            i += 1
        elif t in ("--all-projects", "--all-project", "-a", "--global"):
            q.all_projects = True
        elif t in ("--all",):
            q.show_all = True
            q.limit = None
        elif t in ("--export", "-o", "--out") and nxt:
            q.export = nxt
            i += 1
        elif t in ("--jsonl", "--json"):
            q.fmt = "jsonl"
        elif t in ("--md", "--markdown"):
            q.fmt = "md"
        elif t == "--kinds":
            q.list_kinds = True
        elif t == "--path":
            q.show_path = True
        elif t in ("-n", "--limit") and nxt.isdigit():
            q.limit = int(nxt)
            i += 1
        elif t.isdigit() and not words:
            q.limit = int(t)
        else:
            words.append(t)
        i += 1
    if words and not q.text:
        q.text = " ".join(words)
    if q.export and not q.fmt:
        q.fmt = "jsonl" if q.export.lower().endswith((".jsonl", ".json")) else "md"
    if q.export and not q.show_all and q.limit == 30:
        q.limit = None  # exports take everything that matches unless a count was given
    return q


def run_activity(q: ActivityQuery, workspace: Path, out: Any = None) -> int:
    from axon.session import activity
    from axon.ui.theme import CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE
    w = out or sys.stdout

    def p(s: str = "") -> None:
        w.write(s + "\n")

    if q.error:
        p(f"  {ROSE}{q.error}{RST}")
        return 2
    if q.list_kinds:
        p(f"\n  {GOLD}Journal kinds{RST}")
        for k, desc in activity.KINDS.items():
            p(f"    {WHITE}{k:<14}{RST} {SLATE}{desc}{RST}")
        p()
        return 0
    if q.show_path:
        for key, path in activity.journal_files(workspace, q.all_projects):
            p(f"  {path}")
        return 0
    entries = activity.read(workspace, all_projects=q.all_projects, kinds=q.kinds or None, session=q.session,
                            text=q.text or None, limit=q.limit, since=q.since)
    if q.export:
        path = Path(q.export).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        activity.export(entries, path, q.fmt or "md")
        p(f"  {MINT}✓ Exported {len(entries)} event(s) to{RST} {WHITE}{path}{RST}")
        return 0
    where = "all projects" if q.all_projects else str(activity.journal_path(workspace))
    p(f"\n  {GOLD}Activity{RST} {SLATE}· {len(entries)} event(s) · {where}{RST}")
    if not entries:
        p(f"    {SLATE}Nothing recorded yet{' for this filter' if (q.kinds or q.text or q.session) else ''}.{RST}\n")
        return 0
    last_day = ""
    for e in entries:
        t = float(e.get("t") or 0)
        dt = datetime.fromtimestamp(t) if t else None
        day = dt.strftime("%Y-%m-%d") if dt else ""
        if day != last_day:
            p(f"  {DARK_SLATE}── {day} ──{RST}")
            last_day = day
        when = dt.strftime("%H:%M:%S") if dt else "--:--:--"
        proj = f"{DARK_SLATE}{str(e.get('project', ''))[-24:]}{RST} " if q.all_projects else ""
        kind = str(e.get("kind", ""))
        color = ROSE if kind in ("error",) or (isinstance(e.get("data"), dict) and e["data"].get("success") is False) else CYAN
        p(f"    {SLATE}{when}{RST} {proj}{color}{kind:<13}{RST} {WHITE}{activity.summary_line(e, 100)}{RST}")
    p(f"\n  {DARK_SLATE}/activity help · filters: <N> --kind k --session id --since 2h --all-projects --export file.md{RST}\n")
    return 0


def handle_activity(agent: Any, arg: str) -> bool:
    from axon.ui.theme import CYAN, RST
    if arg.strip() in ("-h", "--help", "help"):
        print("\n" + "\n".join(f"  {CYAN}{ln}{RST}" for ln in ACTIVITY_USAGE.splitlines()) + "\n")
        return True
    q = parse_activity_args(arg)
    return run_activity(q, Path(agent.settings.workspace)) == 0


def activity_main(argv: list[str]) -> int:
    """`axon activity [N] [--kind k] [--session id] [--since 2h] [--all-projects] [--export file]`."""
    if any(a in ("-h", "--help") for a in argv):
        print(ACTIVITY_USAGE.replace("/activity", "axon activity"))
        return 0
    return run_activity(parse_activity_args(argv), Path.cwd())


# ─── /restore ───────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def handle_restore(agent: Any, arg: str) -> bool:
    from axon.commands.common import is_tty
    from axon.ui.theme import BOLD, CYAN, GOLD, MINT, ROSE, RST, SLATE, WHITE
    store = agent.session
    items = store.list_trash()
    a = arg.strip()
    if not a or a in ("list", "ls"):
        print(f"\n  {GOLD}Trash{RST} {SLATE}· {len(items)} deleted session(s) · {store.trash_root()}{RST}")
        if not items:
            print(f"    {SLATE}Empty. /delete moves sessions here; nothing is destroyed.{RST}\n")
            return True
        for m in items:
            print(f"    {CYAN}{m['id']}{RST}  {SLATE}deleted {_fmt_ts(m['deleted_at'])} · {m['files']} file(s){RST}  "
                  f"{WHITE}{(m['title'] or '')[:50]}{RST}")
        print(f"\n  {SLATE}/restore <id> moves one back · /restore --all restores everything{RST}\n")
        if is_tty() and not a:
            try:
                from axon.ui.picker import pick
                chosen = pick([f"{m['id']}  ({(m['title'] or '')[:40]})" for m in items], title="Restore session")
            except Exception:
                chosen = None
            if chosen:
                a = chosen.split()[0]
            else:
                return True
        else:
            return True
    targets = [m["path"].name for m in items] if a in ("--all", "all") else [a]
    done = 0
    for t in targets:
        sid = store.restore_session(t)
        if sid:
            done += 1
            print(f"  {MINT}✓ Restored {BOLD}{sid}{RST} {SLATE}(/resume {sid}){RST}")
        else:
            print(f"  {ROSE}✗ No trashed session matches {t!r}{RST}")
    print()
    return done > 0


# ─── /project and `axon projects` ───────────────────────────────────────────

def _link(path: str) -> str:
    try:
        from axon.ui.markdown import file_uri
        from axon.ui.theme import hyperlink
        return hyperlink(file_uri(path), path)
    except Exception:
        return path


def handle_project(agent: Any, arg: str) -> bool:
    from axon.session.layout import project_info
    from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, RST, SLATE, WHITE
    ws = Path(agent.settings.workspace)
    info = project_info(ws, detailed=True)
    rows = [
        ("Project", f"{BOLD}{info['name']}{RST}"),
        ("Workspace", _link(info["workspace"])),
        ("Opened in", _link(str(ws)) if str(ws) != info["workspace"] else "(project root)"),
        ("Data folder", _link(info["data_dir"])),
        ("Sessions", f"{info['sessions']} saved · {info['trash']} in trash · current {agent.session.active_session_id}"),
        ("Journal", f"{info['journal_entries']:,} event(s) · activity.jsonl"),
        ("Screen logs", str(info["screen_logs"])),
        ("Size", info["size"]),
        ("Last opened", info["last_opened"] or "now"),
    ]
    if info.get("git_remote") or info.get("git_branch"):
        rows.append(("Git", f"{info.get('git_remote') or '(no remote)'} · {info.get('git_branch') or '?'}"))
    print(f"\n  {GOLD}Current project{RST}")
    for k, v in rows:
        print(f"    {SLATE}{k:<12}{RST} {WHITE}{v}{RST}")
    print(f"\n  {CYAN}Memory{RST}")
    loaded = []
    try:
        loaded = [str(getattr(n, "path", "")) for n in (getattr(agent.memory, "loaded", None) or [])]
    except Exception:
        loaded = []
    proj_mem, glob_mem = info.get("memory_paths") or [], info.get("global_memory_paths") or []
    print(f"    {SLATE}{'project':<12}{RST} {len(proj_mem)} file(s) in {_link(str(Path(info['data_dir']) / 'memory'))}")
    for pth in proj_mem[:12]:
        print(f"      {DARK_SLATE}{Path(pth).name}{RST}")
    print(f"    {SLATE}{'global':<12}{RST} {len(glob_mem)} file(s) (shared by all projects)")
    for pth in glob_mem[:12]:
        print(f"      {DARK_SLATE}{Path(pth).name}{RST}")
    if loaded:
        print(f"    {SLATE}{'loaded now':<12}{RST} " + ", ".join(Path(x).name for x in loaded[:10] if x))
    print(f"\n  {DARK_SLATE}~/.axon/README.md explains every folder · axon projects lists all projects{RST}\n")
    return True


def projects_main(argv: list[str]) -> int:
    """`axon projects`: every project in ~/.axon/projects/index.json."""
    import json as _json
    from axon.session.layout import update_index
    data = update_index(None, full=True)
    projects = data.get("projects", {})
    if "--json" in argv:
        print(_json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not projects:
        print("No projects yet. Run axon inside a folder to create one.")
        return 0
    rows = sorted(projects.items(), key=lambda kv: kv[1].get("last_opened") or "", reverse=True)
    print(f"{'PROJECT':<24} {'LAST OPENED':<20} {'SESSIONS':>8} {'SIZE':>9}  PATH")
    for key, p in rows:
        size = int(p.get("size_bytes") or 0)
        human = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.0f} KB"
        when = str(p.get("last_opened") or "")[:16].replace("T", " ")
        print(f"{str(p.get('name') or key)[:24]:<24} {when:<20} {int(p.get('sessions') or 0):>8} {human:>9}  "
              f"{p.get('workspace') or '?'}")
        print(f"{'':<24} data: {p.get('data_dir')}")
    return 0


LIBRARY_USAGE = """/library                 everything saved for this project: sessions, subagent runs, skills,
                         agents, commands, memory, hooks, plans, todos, file history, outputs
/library <section>       the full list for one section (sessions, subagents, skills, agents,
                         commands, memory, hooks, plans, todos, files, outputs, library, usage)
/library --json          the same as JSON"""


def handle_library(agent: Any, arg: str) -> bool:
    from axon.session.library import show, sync_all
    from axon.ui.theme import CYAN, RST
    if arg.strip() in ("-h", "--help", "help"):
        print("\n" + "\n".join(f"  {CYAN}{ln}{RST}" for ln in LIBRARY_USAGE.splitlines()) + "\n")
        return True
    ws = Path(agent.settings.workspace)
    sync_all(ws)
    return show(ws, arg) == 0


def library_main(argv: list[str]) -> int:
    """`axon library [section] [--json]`."""
    from axon.session.library import show, sync_all
    if any(a in ("-h", "--help") for a in argv):
        print(LIBRARY_USAGE.replace("/library", "axon library"))
        return 0
    sync_all(Path.cwd())
    return show(Path.cwd(), " ".join(argv))


COMMANDS = [
    FeatureCommand(name="/library", handler=handle_library, aliases=("/saved",), category="Session",
                   description="Everything saved: sessions, subagent runs, skills, agents, memory, hooks, plans",
                   usage=LIBRARY_USAGE),
    FeatureCommand(name="/activity", handler=handle_activity, aliases=("/journal",), category="Session",
                   description="Everything you and Axon did: prompts, commands, shell, tools, settings, errors",
                   usage=ACTIVITY_USAGE),
    FeatureCommand(name="/restore", handler=handle_restore, aliases=("/trash",), category="Session",
                   description="List deleted sessions (the trash) and restore them",
                   usage="/restore              list the trash (pick one on a terminal)\n/restore <id>         restore a session\n"
                         "/restore --all        restore every trashed session"),
    FeatureCommand(name="/project", handler=handle_project, aliases=("/where",), category="Session",
                   description="Current project: name, path, data folder, sessions, journal, memory",
                   usage="/project"),
]

CLI_SUBCOMMANDS = {"activity": activity_main, "projects": projects_main, "library": library_main}

__all__ = ["ActivityQuery", "parse_activity_args", "run_activity", "handle_activity", "activity_main",
           "handle_restore", "handle_project", "projects_main", "handle_library", "library_main",
           "COMMANDS", "CLI_SUBCOMMANDS"]
