"""
Slash-command registry and dispatcher.

Every built-in command is declared once with :func:`command` (name, aliases,
category, description, usage, handler). ``/help`` and the autocomplete list are
generated from this registry plus custom markdown commands, skills, plugin
commands and MCP prompts.

Resolution order for ``/name``:
  1. built-in commands and their aliases
  2. custom markdown commands (project > user > plugin)
  3. skills                            (always reachable as ``/skill:<name>``)
  4. MCP prompts                       (``/mcp__<server>__<prompt>``)
Shadowed custom commands stay reachable as ``/cmd:<name>``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from axon.ui.theme import BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, ORANGE, PURPLE, ROSE, RST, SLATE, TEAL, WHITE


@dataclass
class CommandResult:
    handled: bool
    should_exit: bool = False
    message: str | None = None
    success: bool = True     # False when the command failed (``axon -p /cmd`` then exits 1)


Handler = Callable[[Any, str], "CommandResult | None"]


@dataclass(frozen=True)
class Command:
    name: str
    handler: Handler
    category: str
    description: str
    aliases: tuple[str, ...] = ()
    usage: str = ""
    hidden: bool = False


CATEGORIES: list[tuple[str, str]] = [
    ("Session", TEAL),
    ("Context & Cost", GOLD),
    ("Model & Modes", PURPLE),
    ("Code & Review", MINT),
    ("Agents, Skills & Tools", CYAN),
    ("Memory & Project", LBLUE),
    ("System", SLATE),
]

COMMANDS: dict[str, Command] = {}
ALIASES: dict[str, str] = {}


def command(
    name: str,
    *,
    category: str,
    description: str,
    aliases: tuple[str, ...] | list[str] = (),
    usage: str = "",
    hidden: bool = False,
) -> Callable[[Handler], Handler]:
    """Register a built-in command. Duplicate names/aliases are a programming error."""
    def deco(fn: Handler) -> Handler:
        key = name.lower()
        if key in COMMANDS or key in ALIASES:
            raise ValueError(f"Duplicate slash command /{key}")
        cmd = Command(name=key, handler=fn, category=category, description=description,
                      aliases=tuple(a.lower() for a in aliases), usage=usage or f"/{key}", hidden=hidden)
        COMMANDS[key] = cmd
        for a in cmd.aliases:
            if a in COMMANDS or a in ALIASES:
                raise ValueError(f"Duplicate slash alias /{a}")
            ALIASES[a] = key
        return fn
    return deco


_FEATURES: dict[str, Command] | None = None
_FEATURE_ALIASES: dict[str, str] = {}


def feature_table() -> dict[str, Command]:
    """Commands contributed by axon.features modules (loaded once, failures ignored)."""
    global _FEATURES
    if _FEATURES is None:
        table: dict[str, Command] = {}
        aliases: dict[str, str] = {}
        try:
            from axon.features import feature_commands
            for fc in feature_commands():
                name = str(fc.name).lstrip("/").lower()
                if not name or name in table:
                    continue
                table[name] = Command(
                    name=name, handler=fc.handler, category=str(fc.category or "Features"),
                    description=str(fc.description or ""), aliases=tuple(str(a).lstrip("/").lower() for a in fc.aliases),
                    usage=str(fc.usage or f"/{name}"),
                )
                for a in table[name].aliases:
                    aliases.setdefault(a, name)
        except Exception:
            table = {}
        _FEATURES = table
        _FEATURE_ALIASES.clear()
        _FEATURE_ALIASES.update(aliases)
    return _FEATURES


def reload_features() -> None:
    global _FEATURES
    _FEATURES = None


def _ensure_loaded() -> None:
    if "help" not in COMMANDS:
        import axon.commands.builtin  # noqa: F401  (registers every built-in)


def resolve_builtin(name: str) -> Command | None:
    """Feature commands win over built-ins of the same name/alias."""
    _ensure_loaded()
    n = name.lower().lstrip("/")
    feats = feature_table()
    if n in feats:
        return feats[n]
    if n in _FEATURE_ALIASES:
        return feats[_FEATURE_ALIASES[n]]
    if n in COMMANDS:
        return COMMANDS[n]
    if n in ALIASES:
        return COMMANDS[ALIASES[n]]
    return None


def visible_commands() -> list[Command]:
    """Every listed command: features plus built-ins that are not shadowed by a feature."""
    _ensure_loaded()
    feats = feature_table()
    shadow = set(feats) | set(_FEATURE_ALIASES)
    out = list(feats.values())
    out += [c for c in COMMANDS.values() if not c.hidden and c.name not in shadow]
    return out


# ─── Extension sources (guarded) ────────────────────────────────────────────

def _workspace(agent: Any) -> Path | None:
    try:
        ws = agent.settings.workspace
        return ws if isinstance(ws, Path) else (Path(ws) if isinstance(ws, str) else None)
    except Exception:
        return None


def _custom_commands(ws: Path | None) -> dict[str, Any]:
    if ws is None:
        return {}
    try:
        from axon.commands.custom import load_custom_commands
        return load_custom_commands(ws)
    except Exception:
        return {}


def _user_invocable(skills: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in skills.items() if getattr(v, "user_invocable", True) is not False}


def _skills(agent: Any) -> dict[str, Any]:
    """Skills the user can run as /<name> (``user-invocable: false`` ones are model-only)."""
    mgr = getattr(agent, "skills", None)
    sk = getattr(mgr, "skills", None)
    return _user_invocable(sk) if isinstance(sk, dict) else {}


def _mcp_prompts() -> list[dict[str, Any]]:
    """Connected MCP prompts as dicts {server, name, description, arguments, command}."""
    try:
        from axon.mcp import bridge as _b
        raw = _b.list_prompts()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for item in raw or []:
        try:
            if isinstance(item, dict):
                server, name = str(item.get("server", "")), str(item.get("name", ""))
                desc, args = str(item.get("description", "") or ""), list(item.get("arguments") or [])
            else:
                server, name, desc, args = item[0], item[1], item[2], list(item[3] or [])
        except Exception:
            continue
        try:
            cmd = _b.prompt_command_name(server, name)
        except Exception:
            cmd = f"mcp__{re.sub(r'[^A-Za-z0-9_-]', '_', server)}__{re.sub(r'[^A-Za-z0-9_-]', '_', name)}"
        out.append({"server": server, "name": name, "description": desc, "arguments": args, "command": cmd})
    return out


def _mcp_prompt_name(p: dict[str, Any]) -> str:
    return str(p["command"])


def list_command_names(workspace: Path | None = None, agent: Any = None) -> list[tuple[str, str]]:
    """(``/name``, description) for autocomplete: built-ins, custom commands, skills, MCP prompts."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cmd in sorted(visible_commands(), key=lambda c: c.name):
        out.append((f"/{cmd.name}", cmd.description))
        seen.add(cmd.name)
    ws = workspace if workspace is not None else (_workspace(agent) if agent is not None else Path.cwd())
    def taken(n: str) -> bool:  # a built-in (or its alias) wins dispatch, so the extension needs its prefix
        return n in seen or resolve_builtin(n) is not None

    for name, cc in sorted(_custom_commands(ws).items()):
        key = name if not taken(name) else f"cmd:{name}"
        out.append((f"/{key}", cc.description or "custom command"))
        seen.add(key)
    skills = _skills(agent) if agent is not None else {}
    if not skills and ws is not None:
        try:
            from axon.skills.manager import _manager_for
            skills = _user_invocable(_manager_for(ws).skills)  # cached per workspace (autocomplete calls this often)
        except Exception:
            skills = {}
    for name, sk in sorted(skills.items()):
        key = name if not taken(name) else f"skill:{name}"
        out.append((f"/{key}", getattr(sk, "description", "") or "skill"))
        seen.add(key)
    for p in _mcp_prompts():
        out.append((f"/{_mcp_prompt_name(p)}", str(p.get("description", "") or "MCP prompt")))
    return out


# ─── Help ───────────────────────────────────────────────────────────────────

def render_help(agent: Any, topic: str = "") -> None:
    from axon.commands.common import width
    topic = topic.strip().lstrip("/")
    if topic:
        cmd = resolve_builtin(topic)
        if cmd:
            print(f"\n  {GOLD}{BOLD}/{cmd.name}{RST}  {SLATE}{cmd.category}{RST}")
            print(f"  {WHITE}{cmd.description}{RST}\n")
            for u in cmd.usage.split("\n"):
                print(f"    {CYAN}{u}{RST}")
            if cmd.aliases:
                print(f"\n  {SLATE}Aliases:{RST} {' '.join('/' + a for a in cmd.aliases)}")
            print()
            return
        cc = _custom_commands(_workspace(agent)).get(topic) or _custom_commands(_workspace(agent)).get(topic.removeprefix("cmd:"))
        if cc:
            print(f"\n  {GOLD}{BOLD}/{cc.name}{RST}  {SLATE}custom · {cc.source} · {cc.path}{RST}")
            print(f"  {WHITE}{cc.description or '(no description)'}{RST}")
            if cc.argument_hint:
                print(f"  {SLATE}Arguments:{RST} {cc.argument_hint}")
            print()
            return
        hits = search_commands(topic, agent)
        if hits:
            name_w = max(len(n) for n, _ in hits) + 2
            print(f"\n  {GOLD}{BOLD}Commands matching '{topic}'{RST}  {SLATE}{len(hits)} found{RST}")
            for n, d in hits:
                print(f"    {WHITE}{'/' + n:<{name_w}}{RST} {SLATE}{_one_line_desc(d)}{RST}")
            print(f"\n  {DIM}/help <command> for details{RST}\n")
            return
        print(f"\n  {ROSE}No command named /{topic}.{RST}\n")
        return

    w = width()
    print(f"\n  {GOLD}{BOLD}Axon commands{RST}  {SLATE}/help <command> for details · Tab cycles permission mode{RST}")
    print(f"  {DARK_SLATE}{'─' * min(w, 78)}{RST}")
    by_cat: dict[str, list[Command]] = {}
    for c in visible_commands():
        by_cat.setdefault(c.category, []).append(c)
    name_w = 20
    desc_w = max(30, min(w, 100) - name_w - 8)
    known = {c for c, _ in CATEGORIES}
    extra_cats = [(c, PURPLE) for c in sorted(by_cat) if c not in known]
    for cat, color in CATEGORIES + extra_cats:
        cmds = sorted(by_cat.get(cat, []), key=lambda c: c.name)
        if not cmds:
            continue
        print(f"\n  {color}{BOLD}{cat}{RST}")
        for c in cmds:
            label = f"/{c.name}"
            desc = c.description if len(c.description) <= desc_w else c.description[: desc_w - 1] + "…"
            print(f"    {WHITE}{label:<{name_w}}{RST} {SLATE}{desc}{RST}")

    ws = _workspace(agent)
    customs = _custom_commands(ws)
    if customs:
        print(f"\n  {ORANGE}{BOLD}Custom commands{RST}")
        for name, cc in sorted(customs.items()):
            shown = name if resolve_builtin(name) is None else f"cmd:{name}"
            hint_txt = f" {DIM}{cc.argument_hint}{RST}" if cc.argument_hint else ""
            print(f"    {WHITE}{'/' + shown:<{name_w}}{RST} {SLATE}{(cc.description or 'custom command')[:desc_w]}{RST}{hint_txt}  {DARK_SLATE}({cc.source}){RST}")
    skills = _skills(agent)
    if skills:
        user_sk = [s for s in skills.values() if getattr(s, "scope", "") != "bundled"]
        print(f"\n  {CYAN}{BOLD}Skills{RST}  {SLATE}{len(skills)} available ({len(user_sk)} custom) · /skills to browse · run with /<skill> or /skill:<name>{RST}")
    prompts = _mcp_prompts()
    if prompts:
        print(f"\n  {PURPLE}{BOLD}MCP prompts{RST}")
        for p in prompts[:20]:
            print(f"    {WHITE}{'/' + _mcp_prompt_name(p):<{name_w}}{RST} {SLATE}{str(p.get('description', ''))[:desc_w]}{RST}")

    collisions = shadowed_names(agent)
    if collisions:
        print(f"\n  {GOLD}!{RST} {SLATE}Shadowed by built-ins (use the prefixed form): {', '.join(collisions)}{RST}")
    print(f"\n  {DARK_SLATE}Shortcuts: {WHITE}?{DARK_SLATE} help · {WHITE}#text{DARK_SLATE} quick memory · {WHITE}!cmd{DARK_SLATE} shell · {WHITE}@file{DARK_SLATE} attach · {WHITE}Ctrl+F{DARK_SLATE} transcript · {WHITE}Ctrl+D{DARK_SLATE} exit{RST}\n")


def search_commands(word: str, agent: Any = None, limit: int = 12) -> list[tuple[str, str]]:
    """Commands whose name or description mentions ``word`` (what ``/help <word>`` lists):
    name hits first, then description hits. [(name without '/', description)]."""
    w = word.strip().lower()
    if not w:
        return []
    try:
        entries = [(n.lstrip("/"), d or "") for n, d in list_command_names(_workspace(agent), agent)]
    except Exception:
        entries = [(c.name, c.description) for c in visible_commands()]
    by_name = [(n, d) for n, d in entries if w in n.lower()]
    by_desc = [(n, d) for n, d in entries if w not in n.lower() and w in d.lower()]
    return (by_name + by_desc)[:limit]


def shadowed_names(agent: Any) -> list[str]:
    out = []
    for name in _custom_commands(_workspace(agent)):
        if resolve_builtin(name):
            out.append(f"/cmd:{name}")
    for name in _skills(agent):
        if resolve_builtin(name):
            out.append(f"/skill:{name}")
    return out


# ─── Dispatch ───────────────────────────────────────────────────────────────

_IMAGE_OR_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _looks_like_path(token: str) -> bool:
    t = token.strip("'\"")
    body = t[1:]
    if "/" in body or "\\" in body:
        return True
    if _IMAGE_OR_FILE_EXT.search(t):
        return True
    try:
        return Path(t).exists()
    except (OSError, ValueError):
        return False


def _journal_command(agent: Any, label: str, arg: str, res: CommandResult, started: float, status: str = "") -> None:
    """Every slash command and its outcome goes to the activity journal (axon.session.activity)."""
    try:
        import time as _time
        from axon.session import activity
        kind = "memory" if label == "# memory" else "command"
        if label == "/keys":  # never journal a key typed as an argument
            from axon.providers.keystore import redact_keys_arg
            arg = redact_keys_arg(arg)
        data: dict[str, Any] = {"name": label, "arg": arg, "success": bool(res.success),
                                "duration_s": round(_time.time() - started, 3)}
        if kind == "memory":
            data = {"scope": "project", "text": arg, "success": bool(res.success)}
        if res.message:
            data["message"] = str(res.message)[:2000]
        if res.should_exit:
            data["exit"] = True
        if status:
            data["status"] = status
        activity.record(kind, data, agent=agent)
    except Exception:
        pass


def _run_handler(fn: Handler, agent: Any, arg: str, label: str) -> CommandResult:
    import time as _time
    started = _time.time()
    try:
        res = fn(agent, arg)
    except KeyboardInterrupt:
        print(f"\n  {SLATE}⏹ {label} cancelled.{RST}\n")
        out = CommandResult(handled=True)
        _journal_command(agent, label, arg, out, started, "cancelled")
        return out
    except Exception as e:  # never let a command crash the REPL
        print(f"\n  {ROSE}✗ {label} failed:{RST} {type(e).__name__}: {e}\n")
        out = CommandResult(handled=True, message=f"{type(e).__name__}: {e}", success=False)
        _journal_command(agent, label, arg, out, started)
        return out
    out = res if isinstance(res, CommandResult) else CommandResult(handled=True, success=res is not False)
    _journal_command(agent, label, arg, out, started)
    return out  # a handler may return False on failure


def dispatch_command(line: Any, agent: Any) -> CommandResult | None:
    """Dispatch a slash command (accepts ``(line, agent)`` or ``(agent, line)``).

    Returns None when the line is not a command (so it should go to the model)."""
    if not isinstance(line, str):
        line, agent = agent, line
    if not isinstance(line, str):
        return None
    stripped = re.sub(r"^[│┃•\|\s]+", "", line).strip()
    if not stripped:
        return None
    _ensure_loaded()
    if stripped in ("?", "/?"):
        help_cmd = resolve_builtin("help") or COMMANDS["help"]
        return _run_handler(help_cmd.handler, agent, "", "/help")

    if stripped.startswith("#") and len(stripped) > 1 and stripped[1] not in "#!":
        from axon.commands.memory_cmds import quick_memory
        return _run_handler(lambda a, t: quick_memory(a, t), agent, stripped[1:].strip(), "# memory")

    if not stripped.startswith("/"):
        return None

    first, _, rest = stripped.partition(" ")
    arg = rest.strip()
    name = first[1:]
    cmd = resolve_builtin(name)
    if cmd is not None:
        if arg in ("--help", "-h") and cmd.name != "help":
            # every built-in answers `/cmd --help` with its description, usage and aliases
            return _run_handler(lambda a, _t: render_help(a, cmd.name), agent, "", f"/{cmd.name}")
        return _run_handler(cmd.handler, agent, arg, f"/{cmd.name}")

    if not name or _looks_like_path(first):
        return None  # a pasted path / image, not a command

    lname = name.lower()
    ws = _workspace(agent)

    # 2. custom commands
    customs = _custom_commands(ws)
    cc_key = name[4:] if lname.startswith("cmd:") else name
    if cc_key in customs:
        from axon.commands.custom import run_custom_command
        return _run_handler(lambda a, t: run_custom_command(a, customs[cc_key], t), agent, arg, f"/{cc_key}")

    # 3. skills
    skill_key = name[6:] if lname.startswith("skill:") else name
    skills = _skills(agent)
    if skill_key in skills or any(k.lower() == skill_key.lower() for k in skills):
        from axon.commands.agent_cmds import run_skill
        return _run_handler(lambda a, t: run_skill(a, skill_key, t), agent, arg, f"/{skill_key}")

    # 4. MCP prompts
    if lname.startswith("mcp__"):
        for p in _mcp_prompts():
            if _mcp_prompt_name(p).lower() == lname:
                from axon.commands.agent_cmds import run_mcp_prompt
                return _run_handler(lambda a, t: run_mcp_prompt(a, p, t), agent, arg, f"/{name}")

    matches = suggest_commands(lname, agent)
    if matches:
        width = max(len(n) for n, _ in matches) + 1
        rows = "".join(f"    {WHITE}/{n:<{width}}{RST} {SLATE}{d}{RST}\n" for n, d in matches)
        print(f"\n  {ROSE}Unknown command /{name}.{RST} {SLATE}Did you mean:{RST}\n{rows}"
              f"  {SLATE}/help lists every command · /help <word> searches them{RST}\n")
    else:
        print(f"\n  {ROSE}Unknown command /{name}.{RST} {SLATE}/help lists every command · /help <word> searches them.{RST}\n")
    unknown = CommandResult(handled=True)
    import time as _time
    _journal_command(agent, f"/{name}", arg, unknown, _time.time(), "unknown")
    return unknown


def suggest_commands(name: str, agent: Any = None, limit: int = 3) -> list[tuple[str, str]]:
    """Commands the user probably meant: close spellings first (typos), then commands whose name or
    description mentions the word ("/analytics" → /stats, /report, /insights). [(name, description)]."""
    import difflib
    try:
        entries = [(n.lstrip("/"), d or "") for n, d in list_command_names(_workspace(agent) if agent is not None else None, agent)]
    except Exception:
        entries = [(k, c.description) for k, c in COMMANDS.items()]
    desc = {}
    for n, d in entries:
        desc.setdefault(n.lower(), d)
    out: list[str] = []
    best = _suggest(name, agent)
    if best:
        out.append(best)
    for m in difflib.get_close_matches(name, list(desc), n=limit, cutoff=0.6):
        out.append(_FEATURE_ALIASES.get(m) or ALIASES.get(m, m))
    stem = name.lower().rstrip("s")
    stems = {stem, stem[:-2] if stem.endswith("ic") else stem, stem.removesuffix("ing")}
    if len(stem) >= 3:
        scored = []
        for n, d in desc.items():
            text = f"{n} {d}".lower()
            hits = sum(1 for s in stems if len(s) >= 3 and s in text)
            if hits:
                scored.append((-hits, len(n), n))
        out += [n for _, _, n in sorted(scored)]
    seen: list[str] = []
    for n in out:
        if n and n not in seen and n != name:
            seen.append(n)
    return [(n, _one_line_desc(desc.get(n, ""))) for n in seen[:limit]]


def _one_line_desc(text: str, limit: int = 70) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _suggest(name: str, agent: Any) -> str | None:
    import difflib
    pool = list(COMMANDS) + list(ALIASES) + list(feature_table()) + list(_FEATURE_ALIASES)
    try:
        pool += list(_custom_commands(_workspace(agent))) + list(_skills(agent))
    except Exception:
        pass
    m = difflib.get_close_matches(name, pool, n=1, cutoff=0.7)
    if not m:
        return None
    return _FEATURE_ALIASES.get(m[0]) or ALIASES.get(m[0], m[0])
