"""Agents, skills & tools: /agents /subagents /main /tasks /skills /hooks /mcp /plugin /tools
/inspect /explain /stats, plus skill and MCP-prompt invocation."""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.commands.common import (
    clip, confirm, err, fmt_tokens, fmt_usd, header, hint, info, is_tty, kv, ok, open_in_editor, section,
    table, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Agents, Skills & Tools"


# ─── Skills & MCP prompts as commands ───────────────────────────────────────

def run_skill(agent: Any, name: str, arguments: str) -> None:
    from axon.commands.custom import make_shell_gate, run_with_overrides
    mgr = agent.skills
    skill = mgr.get(name) if hasattr(mgr, "get") else mgr.skills.get(name)
    if skill is None:
        err(f"Skill '{name}' not found.")
        return
    allowed = list(getattr(skill, "allowed_tools", []) or [])
    try:
        prompt = mgr.execute_skill(skill.name, arguments, allow_shell=make_shell_gate(agent, allowed))
    except TypeError:  # very old SkillManager signature
        prompt = mgr.execute_skill(skill.name)
        if arguments:
            prompt += f"\n\nARGUMENTS: {arguments}"
    print(f"\n  {TEAL}▸ Skill /{skill.name}{RST} {SLATE}{getattr(skill, 'scope', '')}{RST}\n")
    result = run_with_overrides(agent, prompt, model=getattr(skill, "model", None), allowed_tools=allowed)
    record_invocation(agent, "skill_run", skill.name, arguments, result, scope=getattr(skill, "scope", ""),
                      path="" if getattr(skill, "scope", "") == "bundled" else str(getattr(skill, "path", "")),
                      source="user")


def record_invocation(agent: Any, record_type: str, name: str, arguments: str, result: Any, **extra: Any) -> None:
    """Save a skill / custom command / MCP prompt run and its result (never raises)."""
    try:
        from axon.session.library import preview, record_run
        record_run(record_type, {
            "name": name, "args": arguments, **extra,
            "stop_reason": str(getattr(result, "stop_reason", "") or ""),
            "error": getattr(result, "error", None),
            "result": preview(getattr(result, "final_text", "") or ""),
        }, agent=agent)
    except Exception:
        pass


def _parse_prompt_args(spec: list[dict[str, Any]], raw: str) -> dict[str, str]:
    import shlex
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    out: dict[str, str] = {}
    positional = []
    for t in tokens:
        if "=" in t and re.match(r"^[A-Za-z_][\w-]*=", t):
            k, v = t.split("=", 1)
            out[k] = v
        else:
            positional.append(t)
    names = [str(a.get("name")) for a in spec if isinstance(a, dict) and a.get("name")]
    for n in names:
        if n not in out and positional:
            out[n] = positional.pop(0)
    if positional and names:
        out[names[-1]] = (out.get(names[-1], "") + " " + " ".join(positional)).strip()
    return out


def run_mcp_prompt(agent: Any, p: dict[str, Any], arguments: str) -> None:
    from axon.mcp.bridge import get_prompt
    spec = list(p.get("arguments") or [])
    args = _parse_prompt_args(spec, arguments)
    for a in spec:
        if isinstance(a, dict) and a.get("required") and a.get("name") not in args:
            if not is_tty():
                err(f"Missing required argument '{a.get('name')}' for /{p['command']}")
                return
            from axon.commands.common import ask_text
            val = ask_text(f"{a.get('name')} ({a.get('description', '')}):")
            if not val:
                info("Cancelled.")
                return
            args[str(a["name"])] = val
    text = get_prompt(p["server"], p["name"], args)
    print(f"\n  {PURPLE}▸ MCP prompt{RST} {WHITE}{p['server']}/{p['name']}{RST}\n")
    result = agent.run_turn(text)
    record_invocation(agent, "command_run", f"{p['server']}:{p['name']}", arguments, result, kind="mcp_prompt")


# ─── /agents ────────────────────────────────────────────────────────────────

def render_agent_file(name: str, description: str, tools: list[str] | None, model: str | None,
                      system_prompt: str = "") -> str:
    """Agent definition Markdown (Claude-Code compatible frontmatter). ``tools=None`` inherits all tools."""
    fm = [f"name: {name}", f"description: {' '.join(description.split())}"]
    if tools:
        fm.append(f"tools: {', '.join(tools)}")
    fm.append(f"model: {model or 'inherit'}")
    body = system_prompt.strip() or (
        f"You are the {name} subagent. Describe the role, the process to follow and the exact\n"
        "format of the final report here. Keep it focused on one kind of task.")
    return "---\n" + "\n".join(fm) + "\n---\n\n" + body + "\n"


def agent_file_path(ws: Path, name: str, scope: str) -> Path:
    root = (_axon_home() / "agents") if scope == "user" else (Path(ws) / ".axon" / "agents")
    return root / f"{name}.md"


def create_agent_file(ws: Path, name: str, description: str, tools: list[str] | None, model: str | None,
                      scope: str = "project", system_prompt: str = "") -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    if not slug:
        raise ValueError("agent name must contain letters or digits")
    path = agent_file_path(ws, slug, scope)
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    from axon.session.store import atomic_write_text
    atomic_write_text(path, render_agent_file(slug, description or f"Use for {slug.replace('-', ' ')} tasks.", tools, model, system_prompt))
    from axon.session.library import record_saved
    record_saved(path, action="created", source="/agents create", workspace=ws)
    return path


_READONLY_SET = ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch")


def _select_tools(agent: Any) -> list[str] | None | bool:
    """Multi-select tools. Returns a list, None for "all tools", or False when cancelled."""
    from axon.ui.picker import SelectItem, Selector
    reg = getattr(agent, "registry", None)
    names = sorted(t.name for t in reg) if reg is not None else []
    names = [n for n in names if n != "Task"]
    chosen: set[str] = {n for n in names if n in _READONLY_SET}
    state = {"all": False}

    def items() -> list[SelectItem]:
        out = [SelectItem(label="Done", value="__done__", badge=f"{MINT}✓{RST}"),
               SelectItem(label="All tools (inherit from the main agent)", value="__all__",
                          badge="[x]" if state["all"] else "[ ]"),
               SelectItem(label="Read-only preset", value="__ro__", badge=" ", hint=", ".join(_READONLY_SET))]
        for n in names:
            out.append(SelectItem(label=n, value=n, badge="[x]" if (state["all"] or n in chosen) else "[ ]"))
        return out

    def on_key(sel: Any, key: str) -> bool:
        cur = sel.current()
        if cur is None:
            return False
        if key == "enter" and cur.value == "__done__":
            sel.finish("__done__")
            return True
        if key in (" ", "enter"):
            if cur.value == "__all__":
                state["all"] = not state["all"]
            elif cur.value == "__ro__":
                chosen.clear()
                chosen.update(n for n in names if n in _READONLY_SET)
                state["all"] = False
            elif cur.value in chosen:
                chosen.discard(cur.value)
            else:
                chosen.add(cur.value)
                state["all"] = False
            sel.set_items(items())
            return True
        return False

    res = Selector(items(), title="Tools for this agent", subtitle="Space toggles · Enter on Done confirms",
                   on_key=on_key, max_visible=14).run()
    if res != "__done__":
        return False
    if state["all"]:
        return None
    return [n for n in names if n in chosen]


def agent_wizard(agent: Any, name: str = "", scope: str | None = None) -> Path | None:
    """Interactive /agents create: name, description, tools, model, scope, optional generated prompt."""
    from axon.commands.common import ask_text
    from axon.ui.picker import SelectItem, Selector
    ws = ws_of(agent)
    name = name or ask_text("Agent name (e.g. test-runner):")
    if not name:
        return None
    desc = ask_text("When should it be used? (description):") or f"Use for {name} tasks."
    tools = _select_tools(agent)
    if tools is False:
        return None
    small = getattr(agent.settings, "small_model", None)
    model_items = [SelectItem(label="inherit", value=None, hint="same model as the main agent")]
    if small:
        model_items.append(SelectItem(label=str(small), value=str(small), hint="small/fast model"))
    model_items.append(SelectItem(label=str(agent.settings.model), value=str(agent.settings.model), hint="current model"))
    model_items.append(SelectItem(label="Other…", value="__other__"))
    m = Selector(model_items, title="Model").run()
    if m is None:
        return None
    model = m.value
    if model == "__other__":
        model = ask_text("Model id:") or None
    if scope is None:
        sc = Selector([SelectItem(label="Project", value="project", hint=str(ws / ".axon" / "agents")),
                       SelectItem(label="User (all projects)", value="user", hint=str(_axon_home() / "agents"))],
                      title="Save to").run()
        if sc is None:
            return None
        scope = sc.value
    prompt = ""
    if getattr(agent, "provider", None) is not None and confirm("Draft the system prompt with the model?", default=True):
        try:
            from axon.commands.common import one_shot
            prompt = one_shot(agent, "Write the system prompt for a focused coding subagent. Second person, concise: "
                                     "role, step-by-step process, constraints, exact final report format. Max 250 words. "
                                     "Output only the prompt.",
                              f"Agent name: {name}\nPurpose: {desc}\nTools: {', '.join(tools) if tools else 'all'}",
                              max_tokens=600, source="agent_wizard")
        except Exception as e:
            print(f"  {GOLD}!{RST} Could not draft the prompt ({e}); using a template.")
    return create_agent_file(ws, name, desc, tools, model, scope or "project", prompt)


def _load_agent_defs(ws: Path) -> list[Any]:
    from axon.agent.subagent import load_agent_definitions
    return list(load_agent_definitions(ws))


@command("agents", category=CAT, description="List, show, create or edit subagent definitions",
         usage="/agents                         list built-in and custom agents\n/agents show <name>\n/agents create [name] [--global] [description]   (wizard on a terminal)\n/agents edit <name>\n/agents delete <name>")
def handle_agents(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    try:
        defs = _load_agent_defs(ws)
    except (ImportError, AttributeError) as e:
        err(f"Agent definitions are not available in this build ({e}).")
        return CommandResult(handled=True)
    by_name = {d.name.lower(): d for d in defs}

    if sub in ("create", "new", "add"):
        glob = "--global" in rest.split()
        words = [w for w in rest.split() if w != "--global"]
        if is_tty() and len(words) <= 1:
            try:
                path = agent_wizard(agent, words[0] if words else "", "user" if glob else None)
            except (FileExistsError, ValueError) as e:
                err(f"{e} already exists." if isinstance(e, FileExistsError) else str(e))
                return CommandResult(handled=True)
            if path is None:
                info("Agent creation cancelled.")
            else:
                ok(f"Created agent at {WHITE}{path}{RST}")
                if confirm("Open it in your editor now?", default=False):
                    open_in_editor(path)
            return CommandResult(handled=True)
        if not rest:
            usage("/agents create <name> [--global] [description]")
            return CommandResult(handled=True)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", words[0]).strip("-").lower()
        desc = " ".join(words[1:]) or f"Use for {name.replace('-', ' ')} tasks."
        if not name:
            usage("/agents create <name>")
            return CommandResult(handled=True)
        try:
            path = create_agent_file(ws, name, desc, ["Read", "Grep", "Glob"], None, "user" if glob else "project")
        except FileExistsError:
            err(f"{agent_file_path(ws, name, 'user' if glob else 'project')} already exists. Use /agents edit {name}.")
            return CommandResult(handled=True)
        ok(f"Created agent {BOLD}{name}{RST} at {WHITE}{path}{RST}")
        if is_tty() and confirm("Open it in your editor now?", default=True):
            open_in_editor(path)
        return CommandResult(handled=True)

    if sub in ("show", "info", "edit", "delete", "rm", "remove"):
        d = by_name.get(rest.lower())
        if d is None:
            err(f"No agent named '{rest}'.")
            return CommandResult(handled=True)
        src = Path(str(d.source)) if str(d.source).endswith(".md") else None
        if sub in ("show", "info"):
            header(d.name, str(d.source))
            kv("Description", d.description)
            kv("Tools", ", ".join(d.tools) if d.tools else "all parent tools (minus Task)")
            kv("Model", d.model or "inherit")
            if d.system_prompt:
                section("System prompt")
                for ln in str(d.system_prompt).splitlines()[:40]:
                    print(f"    {SLATE}{ln}{RST}")
            print()
            return CommandResult(handled=True)
        if src is None or not src.exists():
            err(f"'{d.name}' is built in; create an override with /agents create {d.name}.")
            return CommandResult(handled=True)
        if sub == "edit":
            open_in_editor(src)
            return CommandResult(handled=True)
        if confirm(f"Delete {src}?", default=not is_tty()):
            src.unlink()
            ok(f"Deleted agent {d.name}")
        return CommandResult(handled=True)

    header("Subagents", f"{len(defs)} available · used by the Task tool")
    rows = []
    for d in sorted(defs, key=lambda x: (str(x.source) != "builtin", x.name)):
        src = str(d.source)
        scope = "built-in" if src == "builtin" else ("project" if str(ws) in src or src == "project" else ("user" if src != "builtin" else src))
        tools = ", ".join(d.tools[:4]) + ("…" if d.tools and len(d.tools) > 4 else "") if d.tools else "all"
        rows.append([f"{WHITE}{d.name}{RST}", f"{SLATE}{scope}{RST}", f"{DARK_SLATE}{tools}{RST}", (d.description or "")[:60]])
    table(rows, ["Name", "Source", "Tools", "Description"])
    hint("\n    /agents create <name> · /agents show <name> · running tasks: /tasks")
    print()
    return CommandResult(handled=True)


# ─── /subagents & /main (open subagent transcripts as chats) ───────────────

def _model_name(agent: Any) -> str:
    m = getattr(getattr(agent, "settings", None), "model", None)
    return m if isinstance(m, str) else "unknown"


def _parent_tasks(agent: Any, curr_id: str, parent_id: str) -> list[Any]:
    from axon.agent.subagent import SubagentManager, sync_subagents_for_session
    if not isinstance(getattr(agent, "subagents", None), SubagentManager):
        agent.subagents = SubagentManager()
    if "_sub_" in curr_id:
        class _ParentView:
            def __init__(self, conv: Any, sess: Any) -> None:
                self.conversation = conv
                self.session = sess
                self.subagents = SubagentManager()
        try:
            view = _ParentView(agent.session.read_conversation(parent_id), agent.session)
            sync_subagents_for_session(view)  # type: ignore[arg-type]
            return view.subagents.all_tasks()
        except Exception:
            return []
    if not agent.subagents.all_tasks():
        sync_subagents_for_session(agent)
    return agent.subagents.all_tasks()


def _clear_screen() -> None:
    if is_tty():
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()


def _open_subagent(agent: Any, task: Any, parent_id: str) -> None:
    from axon.agent.state import Conversation
    from axon.providers.base import AssistantTurn, TextBlock, Usage
    from axon.session.ledger import Ledger
    sub_id = f"{parent_id}_sub_{task.index}"
    store = agent.session
    store.open(sub_id)
    if (store.session_dir / f"{sub_id}.jsonl").exists():
        agent.conversation = store.load(sub_id)
    else:
        msgs = list(task.conversation.messages) if getattr(task, "conversation", None) and task.conversation.messages else [
            {"role": "user", "content": task.prompt},
            {"role": "assistant", "content": task.result_text or "Subagent completed task."},
        ]
        agent.conversation = Conversation(msgs)
        for m in msgs:
            c = m.get("content", "")
            if m.get("role") == "user":
                store.append_user(c if isinstance(c, str) else json.dumps(c, default=str))
            elif m.get("role") == "assistant":
                store.append_turn(AssistantTurn(blocks=[TextBlock(text=c if isinstance(c, str) else json.dumps(c, default=str))],
                                                stop_reason="end_turn", usage=Usage(input=task.input_tokens, output=task.output_tokens)))
    model = getattr(task, "model", "") or _model_name(agent)
    try:
        agent.ledger = store.load_ledger(sub_id, model)
    except Exception:
        agent.ledger = Ledger()
    if hasattr(agent, "subagents"):
        agent.subagents.clear()
    _clear_screen()
    from axon.session.interactive import render_restored_conversation
    render_restored_conversation(agent.conversation, sub_id, ledger=agent.ledger, path=_session_file(agent, sub_id))
    print(f"  {CYAN}⚡ Viewing subagent #{task.index} ({task.title}){RST} · {SLATE}/main returns to the main session{RST}\n")


@command("subagents", category=CAT, description="Open a subagent's transcript from this session as a chat",
         aliases=("sub", "subagent"), usage="/subagents [n | main | queue]")
def handle_subagents(agent: Any, arg: str) -> CommandResult:
    curr_id = agent.session.active_session_id
    parent_id = curr_id.rsplit("_sub_", 1)[0]
    tasks = _parent_tasks(agent, curr_id, parent_id)
    a = arg.strip()
    if a.lower() in ("main", "root", "parent", "0"):
        return handle_main(agent, "")
    if a.lower() in ("q", "queue"):
        from axon.commands.code_cmds import handle_queue
        return handle_queue(agent, "")
    if not tasks:
        info("No subagents have run in this session yet.")
        return CommandResult(handled=True)
    target = None
    if a:
        target = next((t for t in tasks if str(t.index) == a or t.id == a or t.title.lower().startswith(a.lower())), None)
        if target is None:
            err(f"Subagent '{a}' not found.")
            return CommandResult(handled=True)
    else:
        from axon.ui.picker import pick
        icons = {"completed": "✓", "exhausted": "!", "error": "✗"}
        options = [f"[Main] {parent_id}"] + [
            f"[{t.index}] {icons.get(t.status, '▶')} {t.title} ({t.steps} steps)" + (" · viewing" if f"{parent_id}_sub_{t.index}" == curr_id else "")
            for t in tasks
        ]
        sel = pick(options, title="Open subagent", current=options[0])
        if not sel:
            return CommandResult(handled=True)
        if sel.startswith("[Main"):
            return handle_main(agent, "")
        m = re.match(r"\[(\d+)\]", sel)
        target = next((t for t in tasks if m and t.index == int(m.group(1))), None)
    if target is None:
        return CommandResult(handled=True)
    if f"{parent_id}_sub_{target.index}" == curr_id:
        info(f"Already viewing subagent #{target.index}.")
        return CommandResult(handled=True)
    _open_subagent(agent, target, parent_id)
    return CommandResult(handled=True)


@command("main", category=CAT, description="Return from a subagent transcript to the main session",
         aliases=("root", "parent"))
def handle_main(agent: Any, arg: str) -> CommandResult:
    curr_id = agent.session.active_session_id
    if "_sub_" not in curr_id:
        info(f"Already in the main session ({len(agent.conversation.messages)} messages).")
        return CommandResult(handled=True)
    parent_id = curr_id.rsplit("_sub_", 1)[0]
    from axon.session.ledger import Ledger
    agent.conversation = agent.session.load(parent_id)
    try:
        agent.ledger = agent.session.load_ledger(parent_id, _model_name(agent), include_subagents=True)
    except Exception:
        agent.ledger = Ledger()
    try:
        from axon.agent.subagent import sync_subagents_for_session
        sync_subagents_for_session(agent)
    except Exception:
        pass
    _clear_screen()
    from axon.session.interactive import render_restored_conversation
    render_restored_conversation(agent.conversation, parent_id, ledger=agent.ledger, path=_session_file(agent, parent_id))
    print(f"  {MINT}✓ Back in the main session ({parent_id}){RST}\n")
    return CommandResult(handled=True)


def _background_shells() -> list[Any]:
    try:
        from axon.tools.shell import BACKGROUND
        return list(BACKGROUND.all())
    except Exception:
        return []


def _tail(text: str, n: int = 15) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()][-n:]


TASKS_USAGE = """/tasks                   background shells, background turns (Ctrl+B) and subagent tasks
/tasks log <id>          last output lines of a shell (bash_1) or background turn
/tasks kill <id>         stop a background shell"""


@command("tasks", category=CAT, description="Background shells, background turns and subagent tasks",
         aliases=("bashes", "jobs"), usage=TASKS_USAGE)
def handle_tasks(agent: Any, arg: str) -> CommandResult:
    words = arg.split()
    sub = words[0].lower() if words else ""
    shells = {sh.id: sh for sh in _background_shells()}
    turns = getattr(agent, "background_tasks", None)
    turns = turns if isinstance(turns, dict) else {}
    if sub in ("kill", "stop") and len(words) > 1:
        sid = words[1]
        if sid not in shells:
            err(f"No background shell '{sid}'. Known: {', '.join(shells) or 'none'}")
            return CommandResult(handled=True)
        from axon.tools.shell import BACKGROUND
        BACKGROUND.kill(sid)
        ok(f"Stopped {sid} ({clip(shells[sid].command, 60)})")
        return CommandResult(handled=True)
    if sub in ("log", "logs", "tail", "output") and len(words) > 1:
        tid = words[1]
        if tid in shells:
            sh = shells[tid]
            with sh.stream.lock:
                text = sh.stream.buf
            header(f"{tid} · {sh.status}", clip(sh.command, 70))
            lines = _tail(text, 40)
        elif tid in turns:
            rec = turns[tid]
            header(f"Background turn {tid} · {getattr(rec, 'status', '?')}", clip(getattr(rec, "prompt", ""), 70))
            lines = list(getattr(rec, "log", []) or [])[-40:]
            res = getattr(rec, "result", None)
            final = str(getattr(res, "final_text", "") or getattr(rec, "error", "") or "")
            if final.strip():
                lines += ["", f"{getattr(rec, 'status', 'done')} after {float(getattr(rec, 'elapsed_s', 0.0) or 0.0):.1f}s:"]
                lines += _tail(final, 25)
        else:
            err(f"No task '{tid}'.")
            return CommandResult(handled=True)
        for ln in lines or ["(no output yet)"]:
            print(f"    {SLATE}{ln}{RST}")
        print()
        return CommandResult(handled=True)
    if sub:
        usage(TASKS_USAGE)
        return CommandResult(handled=True)

    mgr = getattr(agent, "subagents", None)
    tasks = mgr.all_tasks() if mgr is not None and hasattr(mgr, "all_tasks") else []
    header("Tasks", f"{len(shells)} shell(s) · {len(turns)} background turn(s) · {len(tasks)} subagent task(s)")
    colors = {"running": GOLD, "completed": MINT, "error": ROSE, "exhausted": ROSE, "pending": SLATE, "interrupted": ROSE}
    if shells:
        section("Background shells", TEAL)
        rows = []
        for sh in shells.values():
            st = sh.status
            c = GOLD if st == "running" else (MINT if st.endswith("code 0") else ROSE)
            with sh.stream.lock:
                last = (_tail(sh.stream.buf, 1) or [""])[0]
            rows.append([f"{WHITE}{sh.id}{RST}", f"{c}{st}{RST}", clip(sh.command, 40), f"{time.time() - sh.started:.0f}s",
                         f"{DARK_SLATE}{clip(last, 40)}{RST}"])
        table(rows, ["Id", "Status", "Command", "Age", "Last output"])
    if turns:
        section("Background turns (Ctrl+B)", PURPLE)
        rows = []
        for tid, rec in turns.items():
            st = str(getattr(rec, "status", "?"))
            rows.append([f"{WHITE}{tid}{RST}", f"{colors.get(st, WHITE)}{st}{RST}", clip(getattr(rec, "prompt", ""), 50),
                         f"{float(getattr(rec, 'elapsed_s', 0.0) or 0.0):.1f}s"])
        table(rows, ["Id", "Status", "Prompt", "Time"])
    if tasks:
        section("Subagent tasks", CYAN)
        rows = []
        for t in tasks:
            elapsed = getattr(t, "elapsed_s", 0.0) or ((time.time() - t.start_time) if getattr(t, "start_time", 0) and t.status == "running" else 0.0)
            c = colors.get(t.status, WHITE)
            rows.append([f"#{t.index}", f"{c}{t.status}{RST}", f"{WHITE}{t.title[:40]}{RST}", getattr(t, "agent_type", ""),
                         f"{t.steps} steps", fmt_tokens(getattr(t, "input_tokens", 0) + getattr(t, "output_tokens", 0)), f"{elapsed:.1f}s"])
        table(rows, ["#", "Status", "Title", "Agent", "Steps", "Tokens", "Time"])
    if not (shells or turns or tasks):
        print(f"    {SLATE}No background work. Shells start with Bash run_in_background; Ctrl+B backgrounds a turn. Checklist: /todos{RST}\n")
        return CommandResult(handled=True)
    hint("\n    /tasks log <id> · /tasks kill <shell id> · /subagents <n> opens a subagent transcript")
    print()
    return CommandResult(handled=True)


# ─── /skills ────────────────────────────────────────────────────────────────

@command("skills", category=CAT, description="Browse, create, install and manage skills", aliases=("skill",),
         usage="/skills                         list\n/skills show <name>\n/skills create <name> [template] [--global]\n/skills install <github-url> [--global] [--force]\n/skills edit <name> · /skills delete <name> · /skills reload")
def handle_skills(agent: Any, arg: str) -> CommandResult:
    from axon.skills.interactive import handle_skills_command
    handle_skills_command(agent.skills, ws_of(agent), arg)
    return CommandResult(handled=True)


# ─── /hooks ─────────────────────────────────────────────────────────────────

@command("hooks", category=CAT, description="Configured lifecycle hooks (PreToolUse, Stop, …)")
def handle_hooks(agent: Any, arg: str) -> CommandResult:
    try:
        from axon.hooks import HookManager
        hooks = HookManager(agent.settings, ws_of(agent)).describe()
    except (ImportError, AttributeError) as e:
        err(f"Hooks are not available in this build ({e}).")
        return CommandResult(handled=True)
    header("Hooks", f"{len(hooks)} configured")
    if not hooks:
        print(f"    {SLATE}No hooks configured. Add them to .axon/settings.json, e.g.:{RST}")
        print(f"    {DARK_SLATE}{{\"hooks\": {{\"PostToolUse\": [{{\"matcher\": \"Edit|Write\", \"hooks\": [{{\"type\": \"command\", \"command\": \"ruff format .\"}}]}}]}}}}{RST}\n")
        return CommandResult(handled=True)
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for h in hooks:
        by_event[str(h.get("event", "?"))].append(h)
    for ev, items in by_event.items():
        section(ev, CYAN)
        for h in items:
            print(f"    {GOLD}{str(h.get('matcher') or '*'):<16}{RST} {WHITE}{h.get('command', '')}{RST}  {DARK_SLATE}{h.get('source', '')}{RST}")
    print()
    return CommandResult(handled=True)


# ─── /mcp ───────────────────────────────────────────────────────────────────

@command("mcp", category=CAT, description="MCP servers: list, add, remove, connect, tools, prompts",
         usage="/mcp [list|add|remove|enable|disable|connect|disconnect|tools|resources|prompts|logs|help]")
def handle_mcp(agent: Any, arg: str) -> CommandResult:
    try:
        from axon.mcp.commands import handle_mcp_command
    except (ImportError, AttributeError) as e:
        err(f"MCP support is not available in this build ({e}).")
        return CommandResult(handled=True)
    handle_mcp_command(agent, arg)
    return CommandResult(handled=True)


# ─── /plugin ────────────────────────────────────────────────────────────────

@command("plugin", category=CAT, description="Plugins bundling commands, skills, agents, hooks and MCP",
         aliases=("plugins",), usage="/plugin                         list\n/plugin info <name>\n/plugin create <name> [--global]\n/plugin install <folder | https git URL> [--project]\n/plugin enable|disable|remove <name>")
def handle_plugin(agent: Any, arg: str) -> CommandResult:
    from axon.commands import plugins as pl
    ws = ws_of(agent)
    words = arg.split()
    sub = words[0].lower() if words else ""
    rest = [w for w in words[1:] if not w.startswith("--")]
    flags = {w for w in words[1:] if w.startswith("--")}
    installed = {p.name: p for p in pl.load_plugins(ws)}

    if sub in ("create", "new"):
        if not rest:
            usage("/plugin create <name> [--global]")
            return CommandResult(handled=True)
        root = (_axon_home() / "plugins") if "--global" in flags else (ws / ".axon" / "plugins")
        d = pl.create_plugin(rest[0], root)
        ok(f"Created plugin at {WHITE}{d}{RST}  {SLATE}(try /{d.name}:hello){RST}")
        return CommandResult(handled=True)
    if sub in ("install", "add"):
        if not rest:
            usage("/plugin install <folder | https git URL> [--project]")
            return CommandResult(handled=True)
        root = (ws / ".axon" / "plugins") if "--project" in flags else (_axon_home() / "plugins")
        if not confirm(f"Install plugin from {rest[0]}? Plugins can add commands, skills and hooks that run code.", default=not is_tty()):
            info("Cancelled.")
            return CommandResult(handled=True)
        p = pl.install_plugin(rest[0], root)
        _refresh_extensions(agent)
        ok(f"Installed {BOLD}{p.name}{RST} {SLATE}{p.version}{RST} → {p.path}")
        return CommandResult(handled=True)
    if sub in ("enable", "disable", "remove", "rm", "uninstall", "info", "show"):
        if not rest or rest[0] not in installed:
            err(f"No installed plugin named '{rest[0] if rest else ''}'.")
            return CommandResult(handled=True)
        p = installed[rest[0]]
        if sub in ("info", "show"):
            header(p.name, f"{p.version} · {p.scope} · {'enabled' if p.enabled else 'disabled'}")
            kv("Path", p.path)
            kv("Description", p.description or "-")
            for k, n in p.components().items():
                kv(k.capitalize(), n)
            print()
            return CommandResult(handled=True)
        if sub in ("enable", "disable"):
            pl.set_enabled(p.name, sub == "enable")
            _refresh_extensions(agent)
            ok(f"{'Enabled' if sub == 'enable' else 'Disabled'} plugin {p.name}")
            return CommandResult(handled=True)
        if confirm(f"Remove plugin {p.name} ({p.path})?", default=not is_tty()):
            pl.remove_plugin(p)
            _refresh_extensions(agent)
            ok(f"Removed plugin {p.name}")
        return CommandResult(handled=True)
    if sub == "reload":
        return handle_reload_plugins(agent, "")

    header("Plugins", f"{len(installed)} installed")
    if not installed:
        print(f"    {SLATE}None. Plugins live in ~/.axon/plugins/<name>/ or .axon/plugins/<name>/ with a plugin.json.{RST}")
    for p in installed.values():
        comps = ", ".join(f"{n} {k}" for k, n in p.components().items()) or "empty"
        state = f"{MINT}●{RST}" if p.enabled else f"{DARK_SLATE}○{RST}"
        print(f"    {state} {WHITE}{p.name:<20}{RST} {SLATE}{p.version:<8} {p.scope:<8} {comps}{RST}")
    hint("\n    /plugin create <name> · install <folder|git-url> · info|enable|disable|remove <name>")
    print()
    return CommandResult(handled=True)


def _refresh_extensions(agent: Any) -> None:
    try:
        from axon.commands.custom import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    mgr = getattr(agent, "skills", None)
    if mgr is not None and hasattr(mgr, "discover"):
        mgr.discover()


@command("reload-plugins", category=CAT, description="Re-scan plugins, custom commands and skills", aliases=("reload",))
def handle_reload_plugins(agent: Any, arg: str) -> CommandResult:
    _refresh_extensions(agent)
    try:
        agent.permissions.reload()
    except Exception:
        pass
    from axon.commands.custom import load_custom_commands
    n_cmd = len(load_custom_commands(ws_of(agent)))
    n_sk = len(getattr(agent.skills, "skills", {}) or {})
    ok(f"Reloaded: {n_cmd} custom command(s), {n_sk} skill(s), permission rules")
    return CommandResult(handled=True)


# ─── /tools ─────────────────────────────────────────────────────────────────

_TOOL_GROUPS = [
    ("Files", ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Patch"]),
    ("Search", ["Glob", "Grep", "Ls", "CodeSearch"]),
    ("Shell", ["Bash"]),
    ("Web", ["WebFetch", "WebSearch"]),
    ("Planning & agents", ["TodoWrite", "Task", "ExitPlanMode"]),
]


@command("tools", category=CAT, description="Tools the model can use and their permission",
         aliases=("tool",), usage="/tools [name]")
def handle_tools(agent: Any, arg: str) -> CommandResult:
    tools = {t.name: t for t in agent.registry.all_tools() if not getattr(t, "hidden", False)}
    q = arg.strip().lower()
    if q:
        # exact name first; otherwise prefix matches; otherwise substring matches
        hits = ([t for t in tools.values() if t.name.lower() == q]
                or [t for t in tools.values() if t.name.lower().startswith(q)]
                or [t for t in tools.values() if q in t.name.lower()])
        if not hits:
            err(f"No tool matching '{arg.strip()}'.")
            return CommandResult(handled=True)
        for t in hits:
            header(t.name, ("read-only" if t.readonly else "can modify") + f" · default {t.default_permission}")
            print(f"    {SLATE}{t.description}{RST}")
            props = t.schema.get("properties") or {}
            req = set(t.schema.get("required") or [])
            if props:
                section("Parameters")
                for pn, ps in props.items():
                    mark = f"{ROSE}*{RST}" if pn in req else " "
                    print(f"    {mark}{WHITE}{pn:<16}{RST} {DARK_SLATE}{ps.get('type', 'any'):<8}{RST} {SLATE}{str(ps.get('description', ''))[:80]}{RST}")
            print()
        return CommandResult(handled=True)
    mode = agent.settings.mode
    header("Tools", f"{len(tools)} available · mode {mode}")
    shown: set[str] = set()
    groups = list(_TOOL_GROUPS)
    mcp = sorted(n for n in tools if n.startswith("mcp__"))
    if mcp:
        groups.append(("MCP", mcp))
    other = sorted(n for n in tools if n not in {x for _, g in groups for x in g})
    if other:
        groups.append(("Other", other))
    for title, names in groups:
        present = [tools[n] for n in names if n in tools]
        if not present:
            continue
        section(title)
        for t in present:
            shown.add(t.name)
            try:
                d = agent.permissions.check(t, {}, mode).outcome
            except Exception:
                d = t.default_permission
            dc = {"allow": MINT, "ask": GOLD, "deny": ROSE}.get(d, SLATE)
            print(f"    {WHITE}{t.name:<14}{RST} {dc}{d:<6}{RST} {SLATE}{clip((t.description.splitlines() or [''])[0], 66)}{RST}")
    print()
    hint("    /tools <name> for parameters · rules: /permissions")
    print()
    return CommandResult(handled=True)


# ─── Traces: /inspect /explain /stats ───────────────────────────────────────

def _traces(agent: Any) -> list[Any]:
    tr = getattr(agent, "traces", None)
    if isinstance(tr, list) and tr:
        return tr
    try:
        from axon.agent.trace import load_traces
        return load_traces(agent.session.session_dir, agent.session.active_session_id)
    except Exception:
        return []


def _pick_trace(agent: Any, arg: str) -> Any:
    traces = _traces(agent)
    if not traces:
        info("No turn traces yet - run a prompt first.")
        return None
    a = arg.strip().lower()
    if not a or a == "last":
        return traces[-1]
    try:
        n = int(a)
    except ValueError:
        usage("/inspect [n|last]")
        return None
    t = next((x for x in traces if getattr(x, "turn", None) == n), None)
    if t is None and 1 <= n <= len(traces):
        t = traces[n - 1]
    if t is None:
        err(f"No turn {n} (have {len(traces)}).")
    return t


@command("inspect", category=CAT, description="Re-print the detailed timeline of a turn (default: last)",
         usage="/inspect [n|last]")
def handle_inspect(agent: Any, arg: str) -> CommandResult:
    t = _pick_trace(agent, arg)
    if t is None:
        return CommandResult(handled=True)
    r = getattr(agent, "renderer", None)
    if r is not None and callable(getattr(r, "render_trace", None)):
        r.render_trace(t)
        return CommandResult(handled=True)
    try:
        from axon.ui.render import format_trace
        print(format_trace(t, "detailed"))
        return CommandResult(handled=True)
    except Exception:
        pass
    print(f"\n  {GOLD}{BOLD}Turn {t.turn}{RST} {SLATE}{t.model} · {t.duration_s:.1f}s · {fmt_usd(t.cost, 5)}{RST}")
    for s in t.steps:
        print(f"    {CYAN}{s.index:>2}{RST} {s.kind:<10} {SLATE}{json.dumps(s.data, default=str)[:110]}{RST}")
    print()
    return CommandResult(handled=True)


def _tool_target(data: dict[str, Any]) -> str:
    inp = data.get("input") if isinstance(data.get("input"), dict) else {}
    for k in ("path", "file_path", "notebook_path", "pattern", "query", "url", "command", "description"):
        v = inp.get(k)
        if v:
            s = str(v)
            return s if len(s) <= 60 else s[:57] + "…"
    return ""


def explain_trace(t: Any) -> str:
    """Deterministic, LLM-free narrative of a turn built from its trace."""
    tools = [s for s in t.steps if s.kind == "tool"]
    reads: list[str] = []
    edits: list[str] = []
    cmds: list[str] = []
    searches: list[str] = []
    web: list[str] = []
    other: list[str] = []
    read_tok = 0
    errors = []
    for s in tools:
        d = s.data
        name = str(d.get("name", "?"))
        target = _tool_target(d)
        if d.get("is_error"):
            errors.append(f"{name} {target}".strip())
        if name in ("Read", "Ls"):
            reads.append(Path(target).name if target else name)
            read_tok += int(d.get("est_tokens", 0) or 0)
        elif name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Patch"):
            edits.append(Path(target).name if target else name)
        elif name == "Bash":
            cmds.append(target or "a command")
        elif name in ("Grep", "Glob", "CodeSearch"):
            searches.append(target or name)
        elif name in ("WebFetch", "WebSearch"):
            web.append(target or name)
        else:
            other.append(name)
    def uniq(xs: list[str]) -> list[str]:
        return list(dict.fromkeys(xs))
    parts: list[str] = []
    if reads:
        parts.append(f"read {len(uniq(reads))} file(s) ({', '.join(uniq(reads)[:5])}{', …' if len(uniq(reads)) > 5 else ''}" + (f", ~{fmt_tokens(read_tok)} tok)" if read_tok else ")"))
    if searches:
        parts.append(f"searched {len(searches)} time(s) ({', '.join(uniq(searches)[:3])})")
    if edits:
        parts.append(f"edited {', '.join(uniq(edits)[:6])}")
    if cmds:
        parts.append(f"ran {len(cmds)} command(s) ({'; '.join(uniq(cmds)[:3])})")
    if web:
        parts.append(f"used the web {len(web)} time(s)")
    if other:
        parts.append("also used " + ", ".join(uniq(other)))
    action = "; ".join(parts) if parts else "answered without using tools"
    cache_pct = (t.cache_read_tokens / t.input_tokens * 100) if t.input_tokens else 0.0
    prompt = " ".join(str(t.prompt).split())
    prompt = prompt if len(prompt) <= 70 else prompt[:67] + "…"
    lines = [
        f'Turn {t.turn} ("{prompt}"): {action}.',
        f"{t.llm_calls} model call(s) on {t.model}, {fmt_tokens(t.input_tokens)} input tokens ({cache_pct:.0f}% cached), "
        f"{fmt_tokens(t.output_tokens)} output, {t.duration_s:.1f}s, {fmt_usd(t.cost, 4)}.",
    ]
    if errors:
        lines.append(f"{len(errors)} tool call(s) failed: {', '.join(errors[:4])}.")
    denied = [s for s in tools if isinstance(s.data.get("permission"), dict) and s.data["permission"].get("outcome") == "deny"]
    if denied:
        lines.append(f"{len(denied)} call(s) were blocked by permissions.")
    for kind, label in (("retry", "provider retry"), ("compaction", "context compaction"), ("subagent", "subagent event")):
        n = sum(1 for s in t.steps if s.kind == kind)
        if n:
            lines.append(f"{n} {label}(s).")
    if t.stop_reason and t.stop_reason not in ("end_turn", "stop"):
        lines.append(f"Stopped because: {t.stop_reason}.")
    return " ".join(lines)


@command("explain", category=CAT, description="Plain-language summary of a turn (no LLM call)",
         usage="/explain [n|last]")
def handle_explain(agent: Any, arg: str) -> CommandResult:
    t = _pick_trace(agent, arg)
    if t is None:
        return CommandResult(handled=True)
    print(f"\n  {TEAL}{BOLD}What happened{RST}\n")
    import textwrap
    for ln in textwrap.wrap(explain_trace(t), width=min(100, max(60, __import__('shutil').get_terminal_size((100, 20)).columns - 6))):
        print(f"    {WHITE}{ln}{RST}")
    print()
    return CommandResult(handled=True)


def _session_file(agent: Any, session_id: str) -> Any:
    """Transcript file of a session (the full record the restored view is drawn from)."""
    from axon.ui.history_view import session_file
    return session_file(agent, session_id)
