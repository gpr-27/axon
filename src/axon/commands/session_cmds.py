"""Session commands: /clear /resume /sessions /delete /rename /tag /star /branch /rewind /export /history /exit."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.commands.common import (
    confirm, err, estimate_tokens, header, hint, info, is_tty, ok, reprint_banner, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, LBLUE, MINT, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Session"


@command("exit", category=CAT, description="Save the session and quit", aliases=("quit",))
def handle_exit(agent: Any, arg: str) -> CommandResult:
    return CommandResult(handled=True, should_exit=True)


@command("clear", category=CAT, description="Start a fresh session (the old one stays resumable)",
         aliases=("reset", "new"), usage="/clear [name for the session being left]")
def handle_clear(agent: Any, arg: str) -> CommandResult:
    old_id = agent.session.active_session_id
    had_messages = bool(agent.conversation.messages)
    title = arg.strip()
    if title and had_messages:
        try:
            agent.session.rename_session(title, old_id)
        except Exception:
            pass
    if is_tty():
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()
    if hasattr(agent, "reset_for_new_session"):
        new_id = agent.reset_for_new_session(None)
    else:
        agent.conversation.messages.clear()
        for attr in ("ledger", "subagents", "file_state", "todos", "message_queue", "checkpoints"):
            obj = getattr(agent, attr, None)
            if obj is not None and hasattr(obj, "clear"):
                obj.clear()
        new_id = agent.session.open(None)
    cp = getattr(agent, "checkpoints", None)
    if cp is not None and hasattr(cp, "set_session"):
        cp.set_session(new_id)
    reprint_banner(agent)
    print(f"  {MINT}⚡ New session:{RST} {BOLD}{WHITE}{new_id}{RST}")
    if had_messages:
        print(f"  {SLATE}/resume {old_id} (previous session{', ' + title if title else ''}){RST}")
    print()
    return CommandResult(handled=True)


@command("resume", category=CAT, description="Resume a previous session of this workspace",
         aliases=("continue",), usage="/resume [session-id or prefix]")
def handle_resume_cmd(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_resume
    handle_resume(agent, arg)
    return CommandResult(handled=True)


@command("sessions", category=CAT, description="List saved sessions with tokens and cost",
         aliases=("session",), usage="/sessions\n/sessions delete <id>\n/sessions clear")
def handle_sessions(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_session_delete, handle_sessions_list
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    if sub in ("delete", "rm", "del", "remove"):
        handle_session_delete(agent.session, parts[1] if len(parts) > 1 else "")
    elif sub == "clear":
        if confirm("Delete ALL saved sessions of this workspace?", default=not is_tty()):
            handle_session_delete(agent.session, "--all")
    else:
        handle_sessions_list(agent.session)
    return CommandResult(handled=True)


@command("delete", category=CAT, description="Delete a saved session (picker, id, or --all)",
         usage="/delete [session-id | --all]")
def handle_delete(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_session_delete
    handle_session_delete(agent.session, arg.strip())
    return CommandResult(handled=True)


def suggest_session_title(agent: Any) -> str:
    """A short title for the session: one cheap LLM call over the first prompts, else the first prompt."""
    from axon.session.store import clean_session_topic, is_user_prompt
    prompts = [str(m.get("content")) if isinstance(m.get("content"), str) else " ".join(
        str(b.get("text", "")) for b in m.get("content") or [] if isinstance(b, dict))
        for m in agent.conversation.messages if isinstance(m, dict) and is_user_prompt(m)]
    if not prompts:
        return ""
    fallback = clean_session_topic(prompts[0])[:48]
    if getattr(agent, "provider", None) is None:
        return fallback
    try:
        from axon.commands.common import one_shot
        text = one_shot(agent, "Name this coding session in 3-6 words, Title Case, no quotes or punctuation at the end.",
                        "\n".join(f"- {p[:300]}" for p in prompts[:6]), max_tokens=30, source="title")
    except Exception:
        return fallback
    title = " ".join(text.strip().strip('"\'').split())[:64]
    return title or fallback


@command("rename", category=CAT, description="Rename the current session (no name: generate one)", aliases=("title",),
         usage="/rename [new title]   (without a title one is generated from the conversation)")
def handle_rename(agent: Any, arg: str) -> CommandResult:
    title = arg.strip() or suggest_session_title(agent)
    if not title:
        usage("/rename <new session title>   (nothing to name yet)")
        return CommandResult(handled=True)
    title = agent.session.rename_session(title) or title
    mark = getattr(agent, "mark_titled", None)
    if callable(mark):
        mark(title)
    ok(f"Renamed session to {BOLD}{title}{RST}")
    return CommandResult(handled=True)


@command("tag", category=CAT, description="Tag the current session (use '-' to clear)", usage="/tag <name> | /tag -")
def handle_tag(agent: Any, arg: str) -> CommandResult:
    if not arg.strip():
        usage("/tag <name>   (or /tag - to remove)")
        return CommandResult(handled=True)
    tag = agent.session.tag_session(arg.strip())
    ok(f"Tagged session #{tag}" if tag else "Removed session tag")
    return CommandResult(handled=True)


@command("star", category=CAT, description="Toggle the favourite star on this session", aliases=("fav",))
def handle_star(agent: Any, arg: str) -> CommandResult:
    on = agent.session.star_session()
    print(f"\n  {GOLD}{'★ Starred' if on else '☆ Unstarred'}{RST} {SLATE}{agent.session.active_session_id}{RST}\n")
    return CommandResult(handled=True)


@command("branch", category=CAT, description="Fork the conversation into a new session", aliases=("fork",),
         usage="/branch [name]")
def handle_branch_cmd(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_branch
    handle_branch(agent, arg)
    return CommandResult(handled=True)


# ─── /rewind ───────────────────────────────────────────────────────────────

REWIND_MODES: list[tuple[str, str]] = [
    ("code_and_conversation", "Restore code and conversation"),
    ("conversation", "Restore conversation only"),
    ("code", "Restore code only"),
    ("summarize_from_here", "Summarize from here (compress later messages, keep code)"),
]


@dataclass
class RewindPoint:
    index: int              # 1-based number of the user prompt in the conversation
    prompt: str
    ts: float               # when the prompt was sent (0.0 when unknown)
    files_changed: int      # files edited from this prompt until the next one
    message_index: int = 0  # position of the prompt in conversation.messages


def _prompt_text(m: dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _prompt_times(agent: Any, n: int) -> list[float]:
    """Send time of the last ``n`` prompts: session transcript first, then in-memory traces."""
    times: list[float] = []
    sess = getattr(agent, "session", None)
    if sess is not None and hasattr(sess, "user_message_times"):
        try:
            times = list(sess.user_message_times())
        except Exception:
            times = []
    if len(times) < n:
        traces = [t for t in (getattr(agent, "traces", None) or []) if getattr(t, "agent_name", "main") == "main"]
        tr = [float(getattr(t, "started_at", 0.0) or 0.0) for t in traces]
        if len(tr) > len(times):
            times = tr
    times = times[-n:] if n else []
    return [0.0] * (n - len(times)) + times


def rewind_points(agent: Any) -> list[RewindPoint]:
    """Every user prompt of the conversation, oldest first, with time and files changed after it."""
    from axon.session.store import is_user_prompt
    msgs = list(getattr(agent.conversation, "messages", []) or [])
    idxs = [i for i, m in enumerate(msgs) if isinstance(m, dict) and is_user_prompt(m)]
    times = _prompt_times(agent, len(idxs))
    cp = getattr(agent, "checkpoints", None)
    snaps = cp.list_snapshots() if cp is not None and hasattr(cp, "list_snapshots") else []
    points: list[RewindPoint] = []
    for k, mi in enumerate(idxs):
        ts = times[k]
        nxt = next((t for t in times[k + 1:] if t), float("inf"))
        files: set[str] = set()
        if ts:
            for sn in snaps:
                if (sn.turn_id == -1 and k == len(idxs) - 1) or ts <= sn.timestamp < nxt:
                    files.update(str(p) for p in sn.files)
        points.append(RewindPoint(index=k + 1, prompt=_prompt_text(msgs[mi]), ts=ts, files_changed=len(files), message_index=mi))
    return points


def _summarize_tail(agent: Any, tail: list[dict[str, Any]]) -> str:
    try:
        from axon.agent.context import _render_for_summary
        transcript = _render_for_summary(tail, limit_chars=60_000)
    except Exception:
        transcript = "\n".join(f"{m.get('role')}: {str(m.get('content'))[:800]}" for m in tail)
    from axon.commands.common import one_shot
    return one_shot(agent, "Summarise this part of a coding session so it can replace the original messages. "
                           "Keep decisions, file paths, commands, results, errors and open questions. Max 300 words.",
                    transcript, max_tokens=700, source="rewind_summary")


def apply_rewind(agent: Any, index: int, mode: str) -> dict[str, Any]:
    """Rewind to before prompt ``index`` (1-based). Modes: code_and_conversation | conversation | code |
    summarize_from_here. Returns {"reverted": [...], "removed": n, "prompt": text, "summary": str|None}."""
    if mode not in {m for m, _ in REWIND_MODES}:
        raise ValueError(f"unknown rewind mode {mode!r}")
    points = rewind_points(agent)
    pt = next((p for p in points if p.index == index), None)
    if pt is None:
        raise ValueError(f"no prompt #{index} (have {len(points)})")
    out: dict[str, Any] = {"reverted": [], "removed": 0, "prompt": pt.prompt, "summary": None}
    msgs = list(agent.conversation.messages)
    if mode in ("code", "code_and_conversation"):
        cp = getattr(agent, "checkpoints", None)
        if cp is not None and pt.ts and hasattr(cp, "rewind_to"):
            out["reverted"] = cp.rewind_to(pt.ts)
        elif cp is not None and not pt.ts:
            out["reverted"] = ["Skipped code restore: the prompt's time is unknown (resumed without a transcript)"]
    if mode in ("conversation", "code_and_conversation"):
        kept = msgs[:pt.message_index]
        out["removed"] = len(msgs) - len(kept)
        agent.conversation.messages[:] = kept
    elif mode == "summarize_from_here":
        tail = msgs[pt.message_index:]
        if tail:
            summary = _summarize_tail(agent, tail) or "(no summary)"
            out["summary"] = summary
            kept = msgs[:pt.message_index] + [
                {"role": "user", "content": f"[Summary of the conversation from prompt #{index} onward]\n{summary}"},
                {"role": "assistant", "content": "Understood. I will continue from this summary."},
            ]
            out["removed"] = len(tail)
            agent.conversation.messages[:] = kept
    if mode != "code":
        sess = getattr(agent, "session", None)
        if sess is not None and hasattr(sess, "append_rewind"):
            try:
                sess.append_rewind(list(agent.conversation.messages), mode, index)
            except Exception:
                pass
        traces = getattr(agent, "traces", None)
        if isinstance(traces, list) and mode != "summarize_from_here" and pt.ts:
            # a turn's trace starts just before its prompt is recorded, so cut after the previous prompt
            prev_ts = next((p.ts for p in reversed(points) if p.index < index and p.ts), 0.0)
            traces[:] = [t for t in traces if float(getattr(t, "started_at", 0.0) or 0.0) <= prev_ts]
        if mode != "summarize_from_here":
            try:
                agent.pending_input = pt.prompt  # the UI may prefill the prompt for editing
            except Exception:
                pass
    return out


def _print_rewind_result(res: dict[str, Any], mode: str, index: int) -> None:
    label = dict(REWIND_MODES)[mode]
    print(f"\n  {TEAL}✓ {label}{RST} {SLATE}(to before prompt #{index}){RST}")
    for r in res["reverted"]:
        print(f"    {ROSE if r.startswith(('Failed', 'Skipped')) else SLATE}• {r}{RST}")
    if res["removed"]:
        print(f"    {SLATE}• {res['removed']} message(s) removed from the conversation{RST}")
    if res.get("summary"):
        print(f"    {SLATE}• summarised as:{RST} {WHITE}{res['summary'][:300]}{RST}")
    if mode in ("conversation", "code_and_conversation") and res.get("prompt"):
        print(f"    {DARK_SLATE}Prompt was: {res['prompt'][:120]}{RST}")
    print()


def _print_points(points: list[RewindPoint]) -> None:
    header("Rewind points", f"{len(points)} prompt(s)")
    if not points:
        print(f"    {SLATE}No prompts yet.{RST}\n")
        return
    for p in points:
        when = datetime.fromtimestamp(p.ts).strftime("%H:%M:%S") if p.ts else "--:--:--"
        files = f"{p.files_changed} file(s)" if p.files_changed else "no edits"
        print(f"    {CYAN}{p.index:>3}{RST}  {SLATE}{when}{RST}  {WHITE}{' '.join(p.prompt.split())[:60]}{RST}  {DARK_SLATE}{files}{RST}")
    hint("    /rewind to <n> [code_and_conversation|conversation|code|summarize_from_here]")
    print()


def _rewind_menu(agent: Any, points: list[RewindPoint]) -> CommandResult:
    """Two-step menu (axon.ui.picker.run_rewind_menu): pick a prompt, then what to restore."""
    from axon.ui.picker import run_rewind_menu
    picked = run_rewind_menu(points)
    if not picked:
        info("Rewind cancelled.")
        return CommandResult(handled=True)
    pt, mode = picked
    _print_rewind_result(apply_rewind(agent, pt.index, mode), mode, pt.index)
    return CommandResult(handled=True)


REWIND_USAGE = """/rewind                     pick a prompt, then restore code and/or conversation (menu)
/rewind to <n> [mode]       modes: code_and_conversation (default), conversation, code, summarize_from_here
/rewind points              list prompts you can rewind to
/rewind <n>                 undo the file edits of the last n turns
/rewind list                show file checkpoints"""


@command("rewind", category=CAT, description="Restore code and/or conversation to an earlier prompt",
         aliases=("undo", "checkpoint"), usage=REWIND_USAGE)
def handle_rewind(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    cp = getattr(agent, "checkpoints", None)
    if not a:
        points = rewind_points(agent)
        if is_tty() and points:
            return _rewind_menu(agent, points)
        _print_points(points)
        return CommandResult(handled=True)
    if a in ("points", "prompts"):
        _print_points(rewind_points(agent))
        return CommandResult(handled=True)
    if a.startswith("to "):
        parts = a.split()
        try:
            idx = int(parts[1].lstrip("#"))
        except (IndexError, ValueError):
            usage(REWIND_USAGE)
            return CommandResult(handled=True)
        mode = parts[2] if len(parts) > 2 else "code_and_conversation"
        try:
            res = apply_rewind(agent, idx, mode)
        except ValueError as e:
            err(str(e))
            return CommandResult(handled=True)
        _print_rewind_result(res, mode, idx)
        return CommandResult(handled=True)
    if cp is None:
        info("Checkpoints are not enabled for this session.")
        return CommandResult(handled=True)
    if a in ("list", "ls", "show"):
        snaps = cp.list_snapshots() if hasattr(cp, "list_snapshots") else list(getattr(cp, "history", []))
        header("Checkpoints", f"{len(snaps)} snapshot(s), newest last")
        if not snaps:
            print(f"    {SLATE}No file edits recorded yet.{RST}\n")
            return CommandResult(handled=True)
        for i, s in enumerate(snaps, 1):
            when = datetime.fromtimestamp(s.timestamp).strftime("%H:%M:%S")
            names = ", ".join(sorted(p.name for p in s.files))[:70]
            label = f" {DARK_SLATE}{s.label}{RST}" if getattr(s, "label", "") else ""
            print(f"    {CYAN}{i:>2}{RST}  {SLATE}{when}{RST}  {WHITE}{len(s.files)} file(s){RST}  {SLATE}{names}{RST}{label}")
        hint("    /rewind <n> undoes the newest n snapshots")
        print()
        return CommandResult(handled=True)
    try:
        steps = max(1, int(a))
    except ValueError:
        usage(REWIND_USAGE)
        return CommandResult(handled=True)
    reverted = cp.rewind(steps) if hasattr(cp, "rewind") else cp.rewind_last()
    if not reverted:
        info("No file modifications to rewind.")
        return CommandResult(handled=True)
    print(f"\n  {TEAL}✓ Rewound {steps} turn(s):{RST}")
    for r in reverted:
        color = ROSE if r.startswith("Failed") else SLATE
        print(f"    {color}• {r}{RST}")
    print(f"  {DARK_SLATE}Conversation history is unchanged; /rewind to <n> also restores the conversation.{RST}\n")
    return CommandResult(handled=True)


def _export_markdown(agent: Any) -> str:
    lines = [
        f"# Axon session {agent.session.active_session_id}",
        "",
        f"- Workspace: `{agent.settings.workspace}`",
        f"- Model: `{agent.settings.model}`",
        f"- Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Cost: ${float(agent.ledger.total()):.4f}",
        "",
    ]
    for m in agent.conversation.messages:
        role = str(m.get("role", "unknown"))
        content = m.get("content", "")
        if role == "user" and isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            for b in content:
                lines += [f"<details><summary>Tool result {b.get('tool_use_id')}</summary>", "", "```", str(b.get("content", "")).strip(), "```", "</details>", ""]
            continue
        lines.append(f"## {role.capitalize()}")
        lines.append("")
        if isinstance(content, str):
            lines.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    lines.append(str(b.get("text", "")))
                elif t == "tool_use":
                    lines += [f"**Tool call:** `{b.get('name')}`", "", "```json", json.dumps(b.get("input"), indent=2, ensure_ascii=False), "```"]
                elif t == "tool_result":
                    lines += ["```", str(b.get("content", "")).strip(), "```"]
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            lines += [f"**Tool call:** `{fn.get('name')}`", "", "```json", str(fn.get("arguments", "")), "```"]
        lines.append("")
    return "\n".join(lines)


def _export_text(agent: Any) -> str:
    """Plain-text transcript: user prompts and answers, tool calls as one-liners."""
    lines = [f"Axon session {agent.session.active_session_id} · {agent.settings.model} · {agent.settings.workspace}", ""]
    for m in agent.conversation.messages:
        role = str(m.get("role", "?"))
        content = m.get("content", "")
        if isinstance(content, str):
            if content.strip():
                lines += [f"{'>' if role == 'user' else '<'} {content.strip()}", ""]
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text", "")).strip():
                lines += [f"{'>' if role == 'user' else '<'} {str(b['text']).strip()}", ""]
            elif b.get("type") == "tool_use":
                lines.append(f"  [tool] {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:200]}")
            elif b.get("type") == "tool_result":
                first = (str(b.get("content", "")).strip().splitlines() or [""])[0]
                lines.append(f"  [result] {first[:200]}")
    return "\n".join(lines).rstrip() + "\n"


def export_payload(agent: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps({
            "session_id": agent.session.active_session_id,
            "model": agent.settings.model,
            "workspace": str(agent.settings.workspace),
            "cost_usd": float(agent.ledger.total()),
            "messages": agent.conversation.messages,
        }, indent=2, ensure_ascii=False, default=str)
    if fmt == "txt":
        return _export_text(agent)
    return _export_markdown(agent)


_FMT_ALIASES = {"md": "md", "markdown": "md", "json": "json", "txt": "txt", "text": "txt", "plain": "txt"}


@command("export", category=CAT, description="Export the conversation (Markdown, JSON, text) to a file or the clipboard",
         usage="/export [md|json|txt] [path]      default path: ~/.axon/exports/<session>.<ext>\n/export [md|json|txt] clipboard   copy instead of saving\n/export                          on a terminal: choose clipboard or file")
def handle_export(agent: Any, arg: str) -> CommandResult:
    parts = arg.strip().split(maxsplit=1)
    fmt, fname = "md", ""
    if parts:
        if parts[0].lower() in _FMT_ALIASES:
            fmt = _FMT_ALIASES[parts[0].lower()]
            fname = parts[1] if len(parts) > 1 else ""
        else:
            fname = arg.strip()
            low = fname.lower()
            fmt = "json" if low.endswith(".json") else "txt" if low.endswith(".txt") else "md"
    to_clipboard = fname.lower() in ("clipboard", "clip", "copy", "--clipboard")
    if not arg.strip() and is_tty():
        from axon.ui.picker import SelectItem, Selector
        sel = Selector([SelectItem(label="Copy to clipboard (Markdown)", value="clip"),
                        SelectItem(label="Save to a file (Markdown)", value="file", hint=f"~/.axon/exports/{agent.session.active_session_id}.md"),
                        SelectItem(label="Save as JSON", value="json"), SelectItem(label="Save as plain text", value="txt")],
                       title="Export conversation").run()
        choice = getattr(sel, "value", None)
        if choice is None:
            info("Export cancelled.")
            return CommandResult(handled=True)
        to_clipboard = choice == "clip"
        fmt = choice if choice in ("json", "txt") else "md"
    payload = export_payload(agent, fmt)
    if to_clipboard:
        from axon.ui.clipboard import copy_to_clipboard
        if copy_to_clipboard(payload):
            ok(f"Copied {len(agent.conversation.messages)} messages ({len(payload):,} chars, {fmt}) to the clipboard")
        else:
            err("No clipboard utility available (pbcopy, clip, xclip, wl-copy). Use /export <path>.")
        return CommandResult(handled=True)
    if fname:
        out_path = Path(fname).expanduser()
        if not out_path.is_absolute():
            out_path = ws_of(agent) / out_path
    else:
        out_path = _axon_home() / "exports" / f"{agent.session.active_session_id}.{fmt}"
    if out_path.exists() and not confirm(f"{out_path} exists. Overwrite?", default=False):
        info("Export cancelled.")
        return CommandResult(handled=True)
    from axon.session.store import atomic_write_text
    atomic_write_text(out_path, payload)
    ok(f"Exported {len(agent.conversation.messages)} messages to {WHITE}{out_path}{RST}")
    return CommandResult(handled=True)


def _history(agent: Any, window_only: bool) -> CommandResult:
    msgs = list(agent.conversation.messages)
    turns = getattr(agent.settings, "max_history_turns", 0) or 0
    if window_only and turns > 0 and len(msgs) > turns * 2:
        msgs = msgs[-turns * 2:]
    header("Active window" if window_only else "Conversation history", f"{len(msgs)} messages")
    if not msgs:
        print(f"    {SLATE}(empty){RST}\n")
        return CommandResult(handled=True)
    for i, m in enumerate(msgs, 1):
        role = str(m.get("role", "?"))
        content = m.get("content", "")
        clr = TEAL if role == "user" else (GOLD if role == "assistant" else LBLUE)
        if isinstance(content, list):
            kinds = [str(b.get("type", "?")) for b in content if isinstance(b, dict)]
            text = next((str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"), "")
            preview = (text.replace("\n", " ")[:60] + " " if text else "") + f"[{', '.join(kinds)}]"
        else:
            preview = str(content).replace("\n", " ")
        if len(preview) > 84:
            preview = preview[:83] + "…"
        print(f"    {SLATE}{i:>3}{RST} {clr}{role:<9}{RST} {preview} {DARK_SLATE}~{estimate_tokens(content):,} tok{RST}")
    print()
    return CommandResult(handled=True)


@command("history", category=CAT, description="List the messages in the conversation", aliases=("timeline",),
         usage="/history [window]")
def handle_history(agent: Any, arg: str, window_only: bool = False) -> CommandResult:
    return _history(agent, window_only or arg.strip().lower() == "window")


@command("whistory", category=CAT, description="List only the messages inside the sliding window", hidden=True)
def handle_whistory(agent: Any, arg: str) -> CommandResult:
    return _history(agent, True)
