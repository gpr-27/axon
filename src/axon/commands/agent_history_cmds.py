"""``/agents history|diff|rollback|pin``: the versions of agent definition files (see axon.agent.agent_history)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from axon.agent import agent_history as ah
from axon.commands.common import err, header, info, ok, table, usage, ws_of
from axon.ui.theme import GOLD, RST, SLATE, WHITE

SUBS = ("history", "diff", "rollback", "pin")
USAGE = ("/agents history <name>             versions of an agent file\n"
         "/agents diff <name> [v1] [v2]      compact diff (default: previous vs latest)\n"
         "/agents rollback <name> <version>  write an old version back (the current text stays as its own version)\n"
         "/agents pin <name> <version|latest|off>   this session keeps using that text even if the file changes")


def _num(s: str) -> int | None:
    s = s.strip().lower().removeprefix("v")
    return int(s) if s.isdigit() else None


def _target(agent: Any, name: str) -> tuple[Any, str] | None:
    from axon.agent.subagent import extra_agent_dirs, load_agent_definitions
    ws = ws_of(agent)
    defs = load_agent_definitions(ws, extra_dirs=extra_agent_dirs(agent.settings))   # loading records new versions
    d = next((x for x in defs if x.name.lower() == ah.base_name(name).lower()), None)
    scope = ah.scope_for(str(d.source), Path(d.path)) if d is not None and d.path else None
    if d is None or scope is None:
        err(f"No file-based agent named '{name}' (built-in and --agents definitions have no history).")
        return None
    return d, scope


def run(agent: Any, sub: str, rest: str) -> None:
    words = rest.split()
    if not words:
        usage(USAGE)
        return
    t = _target(agent, words[0])
    if t is None:
        return
    d, scope = t
    ws = ws_of(agent)
    vs = ah.versions(scope, d.name)
    args = words[1:]
    if sub == "history":
        header(f"{d.name} history", f"{len(vs)} version(s) · {d.source}")
        rows = [[f"{WHITE}v{v['v']}{RST}", f"{SLATE}{v['ts']}{RST}", v["who"], v["sha256"][:8], f"{len(v['text'])} chars"]
                for v in reversed(vs)]
        table(rows, ["Version", "When", "By", "Hash", "Size"])
        pin = ah.load_pins(ws).get(d.name.lower())
        if pin and pin.get("scope") == scope:
            info(f"Pinned to v{pin['v']} in this session.")
        note = ah.pin_note(ws, d)
        if note:
            print(f"    {GOLD}!{RST} {note}")
        return
    if sub == "diff":
        if not vs:
            err("No versions recorded yet.")
            return
        nums = [_num(a) for a in args[:2]]
        if any(n is None for n in nums):
            err("Versions are numbers, e.g. 2 or v2.")
            return
        last = vs[-1]["v"]
        a_n, b_n = (nums + [None, None])[:2] if nums else (None, None)
        if a_n is None:
            a_n, b_n = (vs[-2]["v"] if len(vs) > 1 else last), last
        elif b_n is None:
            b_n = last
        a, b = ah.get_version(scope, d.name, a_n), ah.get_version(scope, d.name, b_n)
        if a is None or b is None:
            err(f"Version not found (have v{vs[0]['v']}..v{last}).")
            return
        lines = ah.diff_text(a["text"], b["text"], f"v{a_n}", f"v{b_n}")
        if not lines:
            info(f"v{a_n} and v{b_n} are identical.")
        for ln in lines:
            print(f"    {ln}")
        return
    if sub == "rollback":
        n = _num(args[0]) if args else None
        if n is None:
            usage("/agents rollback <name> <version>")
            return
        try:
            new = ah.rollback(ws, d, scope, n)
        except ValueError as e:
            err(str(e))
            return
        ok(f"Rolled {d.name} back to v{n}; it is now v{new}.")
        return
    if sub == "pin":
        what = (args[0].lower() if args else "")
        if not what:
            usage("/agents pin <name> <version|latest|off>")
            return
        if what == "off":
            ah.set_pin(ws, d.name, scope, None)
            ok(f"{d.name} follows the file on disk again.")
            return
        n = vs[-1]["v"] if what == "latest" and vs else _num(what)
        if n is None or ah.get_version(scope, d.name, n) is None:
            err("Unknown version; see /agents history " + d.name)
            return
        ah.set_pin(ws, d.name, scope, n)
        ok(f"{d.name} is pinned to v{n} for this session (kept on /resume).")
        note = ah.pin_note(ws, d)
        if note:
            print(f"    {GOLD}!{RST} {note}")

