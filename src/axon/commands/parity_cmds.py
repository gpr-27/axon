"""Commands from the Claude Code reference that the terminal client was missing:
/stop /reload-skills /list-agents (/peers) /subtask."""
from __future__ import annotations

import threading
from typing import Any

from axon.commands.common import clip, err, fmt_tokens, header, info, ok, table, usage, typed_only
from axon.commands.registry import CommandResult, command
from axon.ui.theme import GOLD, MINT, ROSE, RST, SLATE, WHITE

CAT = "Agents, Skills & Tools"


@command("stop", category=CAT, description="Stop running subagents (their transcripts are kept)",
         usage="/stop            stop every running subagent\n/stop <id>       stop one task")
def handle_stop(agent: Any, arg: str) -> CommandResult:
    target = arg.strip()
    mgr = getattr(agent, "subagents", None)
    stopped = 0
    if target:
        task = mgr.get_task(target) if mgr is not None and hasattr(mgr, "get_task") else None
        if task is not None and mgr.cancel(task.id):
            ok(f"Stopped {task.id} ({clip(task.title, 50)})")
            return CommandResult(handled=True)
        try:
            from axon.tools.shell import BACKGROUND
            BACKGROUND.kill(target)
            ok(f"Stopped shell {target}")
        except Exception:
            err(f"No running task '{target}'.{typed_only(' See /tasks.')}")
        return CommandResult(handled=True)
    if mgr is not None and hasattr(mgr, "cancel_all"):
        stopped += int(mgr.cancel_all() or 0)
    if stopped:
        ok(f"Stopped {stopped} running task(s).")
    else:
        info("Nothing is running in the background.")
    return CommandResult(handled=True)


@command("reload-skills", category=CAT, description="Re-scan skill and command directories without restarting")
def handle_reload_skills(agent: Any, arg: str) -> CommandResult:
    mgr = getattr(agent, "skills", None)
    if mgr is None or not hasattr(mgr, "discover"):
        err("No skill manager in this session.")
        return CommandResult(handled=True, success=False)
    before = set(getattr(mgr, "skills", {}) or {})
    mgr.discover()
    after = set(mgr.skills)
    added, removed = sorted(after - before), sorted(before - after)
    ok(f"{len(after)} skills available · {len(added)} added · {len(removed)} removed")
    for name in added:
        print(f"    {MINT}+ {name}{RST}")
    for name in removed:
        print(f"    {ROSE}- {name}{RST}")
    return CommandResult(handled=True)


def _roster(agent: Any) -> list[Any]:
    """The session's subagents, rebuilt from the saved transcripts when this process has not seen them run."""
    mgr = getattr(agent, "subagents", None)
    if mgr is None or not hasattr(mgr, "all_tasks"):
        return []
    if not mgr.all_tasks():
        try:
            from axon.agent.subagent import sync_subagents_for_session
            sync_subagents_for_session(agent)
            mgr = agent.subagents
        except Exception:
            pass
    return mgr.all_tasks()


@command("list-agents", category=CAT, aliases=("peers",),
         description="List the subagents of this session, with their status and tokens (open one with /agent <n>)")
def handle_list_agents(agent: Any, arg: str) -> CommandResult:
    tasks = _roster(agent)
    header("Agents", f"main + {len(tasks)} subagent(s)")
    if not tasks:
        info("No subagents yet. Ask for parallel work" + typed_only(", or use /subtask <task>") + ".")
        return CommandResult(handled=True)
    colors = {"running": GOLD, "completed": MINT, "error": ROSE, "exhausted": ROSE, "queued": SLATE, "cancelled": SLATE}
    rows = []
    for t in tasks:
        who = f"{t.id}" + (f" ({t.name})" if getattr(t, "name", "") else "")
        rows.append([f"{WHITE}{who}{RST}", t.agent_type, f"{colors.get(t.status, SLATE)}{t.status}{RST}",
                     clip(t.activity or t.title, 44), f"{t.elapsed_s:.0f}s", fmt_tokens(t.tokens_consumed)])
    table(rows, ["Id", "Type", "Status", "Doing", "Time", "Tokens"])
    typed_only_hint = typed_only("  /agent <n> shows everything it did; /agent <n> <message> talks to it")
    if typed_only_hint:
        print(f"  {SLATE}{typed_only_hint.strip()}{RST}")
    return CommandResult(handled=True)


@command("agent", category=CAT, aliases=("peer",),
         description="Open a subagent (everything it did: prompt, thinking, every tool call and its answer) or message it",
         usage="/agent <name|n>             show its whole transcript, every step in full\n"
               "/agent <name|n> <message>   steer it while it runs, or resume it with its history when it finished\n"
               "/agent                      the list (same as /list-agents)")
def handle_agent(agent: Any, arg: str) -> CommandResult:
    who, _, text = arg.strip().partition(" ")
    if not who:
        return handle_list_agents(agent, "")
    tasks = _roster(agent)
    mgr = agent.subagents
    task = mgr.get_task(who.lstrip("#@")) if tasks else None
    if task is None:
        err(f"No subagent '{who}'. Known: " + (", ".join(t.id for t in tasks) or "none yet") + ".")
        return CommandResult(handled=True, success=False)
    from axon.commands.agent_cmds import _subagent_info, _subagent_log, _subagent_send
    if text.strip():
        return _subagent_send(agent, task.id, text.strip())
    try:
        parent_id = agent.session.active_session_id.rsplit("_sub_", 1)[0]
        _subagent_info(agent, parent_id, task)
    except Exception:
        pass
    _subagent_log(task)
    if task.status == "running":
        info(f"{task.id} is running. Steer it with /agent {task.index} <message>.")
    else:
        info(f"{task.id} has finished. Continue it with /agent {task.index} <message>.")
    return CommandResult(handled=True)


@command("subtask", category=CAT, description="Fork the conversation into a background subagent; the result comes back here",
         usage="/subtask <task>")
def handle_subtask(agent: Any, arg: str) -> CommandResult:
    task = arg.strip()
    if not task:
        usage("/subtask <task>")
        return CommandResult(handled=True)
    from axon.agent.subagent import run_subagent
    from axon.commands.code_cmds import session_context
    from axon.ui.in_flight import push_queued
    context_text, n_ctx = session_context(agent)
    prompt = (f"<conversation_so_far>\n{context_text}\n</conversation_so_far>\n\nYou are a fork of the conversation above. "
              f"Do only this task and report back concisely:\n{task}") if n_ctx else task

    def work() -> None:
        try:
            report = run_subagent(prompt, agent, description=clip(task, 60), unlimited=True)
            note = f"[Subtask finished: {clip(task, 80)}]\n{report}"
        except Exception as e:
            note = f"[Subtask failed: {clip(task, 80)}] {type(e).__name__}: {e}"
        queue = getattr(agent, "message_queue", None)
        if queue is not None:
            push_queued(queue, note, steer=True)

    threading.Thread(target=work, daemon=True, name="subtask").start()
    ok(f"Subtask started in the background: {clip(task, 70)}{typed_only('  (see /list-agents, /stop)')}")
    return CommandResult(handled=True)


@command("tell", category=CAT, aliases=("msg",),
         description="Send a message to a subagent: a running one reads it before its next step, a finished one resumes",
         usage="/tell <id|n> <message>")
def handle_tell(agent: Any, arg: str) -> CommandResult:
    who, _, text = arg.strip().partition(" ")
    text = text.strip()
    if not who or not text:
        usage("/tell <id|n> <message>   (ids: /list-agents)")
        return CommandResult(handled=True)
    from axon.commands.agent_cmds import _subagent_send
    return _subagent_send(agent, who, text)


def edit_subagent_result(agent: Any, query: str) -> CommandResult:
    """Open a finished subagent's report in $EDITOR and keep the edit, in the task and in the main conversation."""
    import tempfile
    from pathlib import Path
    from axon.commands.common import open_in_editor
    mgr = getattr(agent, "subagents", None)
    task = mgr.get_task(query) if mgr is not None and hasattr(mgr, "get_task") and query else None
    if task is None or task.status == "running":
        err(f"No finished subagent '{query}'. See /list-agents.")
        return CommandResult(handled=True, success=False)
    old = task.result_text or ""
    with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix=f"{task.id}-result-", delete=False, encoding="utf-8") as f:
        f.write(old)
        path = Path(f.name)
    try:
        if not open_in_editor(path):
            return CommandResult(handled=True, success=False)
        new = path.read_text(encoding="utf-8").rstrip("\n")
    finally:
        path.unlink(missing_ok=True)
    if new == old.rstrip("\n"):
        info("No changes.")
        return CommandResult(handled=True)
    task.result_text = new
    patched = _patch_result(agent, old, new)
    ok(f"Updated {task.id}'s report" + (f" and {patched} place(s) in the main conversation." if patched else "."))
    return CommandResult(handled=True)


def _patch_result(agent: Any, old: str, new: str) -> int:
    """Replace *old* with *new* inside the main conversation's tool results; returns how many were changed."""
    if not old.strip():
        return 0
    n = 0
    for m in getattr(getattr(agent, "conversation", None), "messages", []) or []:
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str) and old in content:
            m["content"] = content.replace(old, new)
            n += 1
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str) and old in b["content"]:
                    b["content"] = b["content"].replace(old, new)
                    n += 1
    return n
