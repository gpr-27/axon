"""/memory history | restore <note> <version> | redact <note> <version>, and /dream (consolidate notes from past sessions)."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, header, info, ok, usage, ws_of
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, DARK_SLATE, RST, SLATE, WHITE

HISTORY_USAGE = """/memory history [global|project|local|<note>]   who wrote what, when (every version kept)
/memory restore <note|scope> <version>       write an older version back (the current text stays in the history)
/memory redact <note|scope> <version> [value]  scrub a secret from a past version; the audit entry stays"""


def _mh() -> Any:
    from axon.features import memory_history
    return memory_history


def _show_versions(ws: Any, key: str, label: str) -> None:
    mh = _mh()
    vs = mh.versions(ws, key)
    header(f"History: {label}", f"{len(vs)} version{'s' if len(vs) != 1 else ''} kept (newest last)")
    if not vs:
        info("No history yet: it starts with the next write.")
        return
    for e in vs:
        b = e.brief()
        flag = f" {DARK_SLATE}redacted{RST}" if e.redacted else ""
        sess = f" · session {e.session[:8]}" if e.session else ""
        print(f"    {WHITE}v{e.v:<3}{RST} {SLATE}{b['when']}  {e.action:<9} {e.actor}{sess}{RST} {DARK_SLATE}{b['sha']} · {e.size}B{RST}{flag}")
    print(f"    {DARK_SLATE}/memory restore {label.split(' ')[0]} <v> · /memory redact ... <v>{RST}\n")


def handle_history_command(agent: Any, sub: str, rest: str) -> bool:
    """True when the /memory subcommand was one of history | restore <ref> <n> | redact <ref> <n>."""
    ws = ws_of(agent)
    mh = _mh()
    if sub in ("history", "log", "versions"):
        if not rest:
            keys = mh.all_keys(ws)
            header("Memory history", "every write to a memory file or note is logged locally")
            if not keys:
                info("Nothing logged yet.")
            for k in keys:
                key = k.replace("__", "/", 1) if not k.startswith("file-") else k
                print(f"    {WHITE}{key}{RST} {SLATE}{len(mh.versions(ws, key))} versions{RST}")
            print()
            return True
        try:
            key, label = mh.resolve(ws, rest)
        except ValueError as e:
            err(str(e))
            return True
        _show_versions(ws, key, label)
        return True
    if sub in ("restore", "redact"):
        parts = rest.split()
        if len(parts) < 2 or not parts[1].lstrip("v").isdigit():
            return False                       # `/memory restore <n>` is the trash restore
        try:
            key, label = mh.resolve(ws, parts[0])
            v = int(parts[1].lstrip("v"))
            if sub == "restore":
                res = mh.restore_version(ws, key, v)
                ok(f"Restored version {v} of {label}" + (f" {SLATE}(that version had a secret scrubbed){RST}" if res["redacted_version"] else ""))
                getattr(agent, "reload_memory", lambda: None)()
            else:
                res = mh.redact_version(ws, key, v, " ".join(parts[2:]) if len(parts) > 2 else "")
                ok(f"Scrubbed a secret from version {v} of {label}; the history entry is kept" + (", and the current file was scrubbed too" if res["live_scrubbed"] else ""))
        except (ValueError, OSError) as e:
            err(str(e))
        return True
    return False


# ─── /dream ─────────────────────────────────────────────────────────────────

DREAM_USAGE = """/dream                  read this project's past sessions and notes, propose cleaned-up notes (dry run: shows a diff)
/dream --apply [id]     replace the notes with the proposal (the original is kept: /dream --undo [id])
/dream --undo [id]      put the original notes back
/dream --status | --cancel"""


def _print_proposal(ws: Any, p: Any) -> None:
    from axon.features import memory_dream as md
    d = md.diff(ws, p)
    header(f"Dream {p.id}", f"{md.summary_line(d)} · {p.model} · {p.sessions} session(s) read · {p.usage.get('input', 0):,} in / {p.usage.get('output', 0):,} out · ${p.cost:.4f}")
    mark = {"added": "+", "changed": "~", "removed": "-", "unchanged": "="}
    for x in d:
        if x["kind"] == "unchanged":
            continue
        print(f"    {WHITE}{mark[x['kind']]} {x['name']}{RST} {DARK_SLATE}{x['file']} ({x['kind']}){RST}")
        for ln in x["lines"]:
            print(f"        {SLATE}{ln[:110]}{RST}")
        if x.get("more"):
            print(f"        {DARK_SLATE}… {x['more']} more line(s){RST}")
    for i in p.insights:
        print(f"    {SLATE}insight: {i}{RST}")
    print(f"\n    {DARK_SLATE}Nothing changed yet. /dream --apply {p.id} to use it (the original stays restorable).{RST}\n")


@command("dream", category="Memory & Project", description="Consolidate this project's memory notes using past sessions (dry run first)",
         usage=DREAM_USAGE)
def handle_dream(agent: Any, arg: str) -> CommandResult:
    from axon.features import memory_dream as md
    ws = ws_of(agent)
    done = CommandResult(handled=True)
    words = arg.split()
    flags = [w for w in words if w.startswith("--")]
    ids = [w for w in words if not w.startswith("--")]
    if "--cancel" in flags:
        ok("Cancelling the dream.") if md.cancel() else info("No dream is running.")
        return done
    if "--status" in flags:
        j = md.current()
        info(f"Dream {j.state}: {j.message or j.error} ({j.model})" if j else "No dream has run in this session.")
        for p in md.list_proposals(ws)[:5]:
            print(f"    {SLATE}{p.id}  {p.status}  {len(p.notes)} notes  ${p.cost:.4f}{RST}")
        return done
    if "--undo" in flags or "--apply" in flags:
        p = md.load(ws, ids[0] if ids else "")
        if p is None and "--apply" in flags:
            p = _run(agent, ws)
            if p is None:
                return done
            _print_proposal(ws, p)
        if p is None:
            err("No dream found. Run /dream first.")
            return done
        try:
            if "--undo" in flags:
                r = md.undo(ws, p)
                ok(f"Original notes restored ({r['restored']} files).")
            else:
                r = md.apply(ws, p)
                ok(f"Applied dream {p.id}: {r['written']} written, {r['removed']} removed. Original kept in {r['kept_original']} and the memory trash.")
            getattr(agent, "reload_memory", lambda: None)()
        except (ValueError, OSError) as e:
            err(str(e))
        return done
    if flags and not set(flags) <= {"--dry-run"}:
        usage(DREAM_USAGE)
        return done
    p = _run(agent, ws)
    if p is not None:
        _print_proposal(ws, p)
    return done


def _run(agent: Any, ws: Any) -> Any:
    from axon.features import memory_dream as md
    try:
        provider, model = md.resolve_provider(agent)
        job = md.start(ws, provider, model, getattr(agent, "ledger", None),
                       str(getattr(getattr(agent, "session", None), "active_session_id", "") or ""))
    except RuntimeError as e:
        err(str(e))
        return None
    info(f"Dreaming with {model}: reading past sessions and notes (Ctrl+C cancels)…")
    try:
        while job.thread.is_alive():
            job.thread.join(0.2)
    except KeyboardInterrupt:
        md.cancel()
        job.thread.join(5)
    if job.state != "done":
        (info if job.state == "cancelled" else err)(job.error or "Dream cancelled; nothing changed.")
        return None
    return job.proposal
