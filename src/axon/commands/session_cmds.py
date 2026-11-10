"""Session commands: /clear /resume /sessions /delete /rename /tag /star /branch /rewind /export /history /exit."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from axon.commands.common import (
    confirm, err, estimate_tokens, header, info, is_tty, ok, reprint_banner, usage, ws_of,
    typed_hint,
    typed_only,
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
    _clear_transcript()
    reprint_banner(agent)
    print(f"  {MINT}⚡ New session:{RST} {BOLD}{WHITE}{new_id}{RST}")
    if had_messages:
        print(f"  {SLATE}{typed_only('/resume ')}{old_id} (previous session{', ' + title if title else ''}){RST}")
    print()
    return CommandResult(handled=True)


@command("resume", category=CAT, description="Resume a previous session of this workspace",
         aliases=("continue",), usage="/resume [session id, part of an id, or title]")
def handle_resume_cmd(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_resume
    before = getattr(agent.session, "active_session_id", None)
    handle_resume(agent, arg)
    if getattr(agent.session, "active_session_id", None) != before:
        _clear_transcript()  # /transcript then rebuilds the resumed conversation from its messages
    return CommandResult(handled=True)


def _clear_transcript() -> None:
    """Forget the expandable transcript (/transcript) of the previous session."""
    try:
        from axon.ui.render import clear_transcript
        clear_transcript()
    except Exception:
        pass


RETRY_RESUME_PROMPT = ("continue (the previous request failed with a provider error; pick up where you left off "
                       "using the tool results above)")


@command("retry", category=CAT, description="Retry the last turn that failed with a provider error", usage="/retry")
def handle_retry(agent: Any, arg: str) -> CommandResult:
    """Re-run a turn that ended on a provider error. When tools had already run, the turn's progress
    is still in the history, so the model is asked to continue; otherwise the prompt is sent again."""
    failed = getattr(agent, "last_failed_turn", None)
    if not failed:
        info("Nothing to retry: the last turn did not fail with a provider error.")
        return CommandResult(handled=True)
    prompt = RETRY_RESUME_PROMPT if failed.get("resume") else str(failed.get("prompt") or "")
    if not prompt.strip():
        info("Nothing to retry.")
        return CommandResult(handled=True)
    label = "resuming the failed turn" if failed.get("resume") else "sending the failed prompt again"
    print(f"\n  {TEAL}▸ /retry{RST}  {SLATE}{label}{RST}\n")
    agent.run_turn(prompt)
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
    elif is_tty():
        from axon.commands.browse_menus import sessions_menu
        sessions_menu(agent)
    else:
        handle_sessions_list(agent.session)
    return CommandResult(handled=True)


@command("delete", category=CAT, description="Delete a saved session (picker, id, or --all)",
         usage="/delete [session-id | --all]")
def handle_delete(agent: Any, arg: str) -> CommandResult:
    from axon.session.interactive import handle_session_delete
    if not arg.strip() and not is_tty():
        # Without a terminal the picker would silently take its first entry (the latest session).
        usage("/delete <session-id> | /delete --all   (the picker needs an interactive terminal)")
        return CommandResult(handled=True)
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


def _same_title(title: str, first_prompt: str) -> bool:
    """True when *title* is just the first words of the prompt (what Axon used before AI titles)."""
    from axon.agent.loop import heuristic_title
    guess = heuristic_title(first_prompt).rstrip("…").strip().lower()
    return bool(guess) and title.strip().lower().startswith(guess[:40])


def auto_named_sessions(agent: Any) -> list[tuple[Any, Any]]:
    """(meta, summary) of past chats still named after their first words: not renamed by you, no AI title."""
    from axon.session.store import _scan
    found = []
    for meta in agent.session.list_recent():
        summary = _scan(meta.path, agent.session.model)
        if not summary.first_prompt:
            continue
        if summary.custom_title and not _same_title(summary.custom_title, summary.first_prompt):
            continue
        found.append((meta, summary))
    return found


def _ai_title_for(agent: Any, summary: Any) -> str:
    from axon.commands.common import one_shot
    text = f"User asked: {summary.first_prompt[:600]}"
    if summary.first_assistant:
        text += f"\n\nAssistant answered: {summary.first_assistant[:300]}"
    reply = one_shot(agent, "Name this coding chat in 3-6 words, Title Case, saying what it was about. "
                            "Reply with the title only: no quotes, no punctuation at the end.",
                     text, max_tokens=30, source="title")
    return " ".join(reply.strip().strip("\"'. ").split())[:64]


@command("retitle", category=CAT, description="Give past chats short AI-written titles",
         usage="/retitle [all]   (no argument: this chat; all: every past chat still named after its first words)")
def handle_retitle(agent: Any, arg: str) -> CommandResult:
    if arg.strip().lower() != "all":
        return handle_rename(agent, "")
    todo = auto_named_sessions(agent)
    if not todo:
        info("Every past chat already has a title you or the AI chose.")
        return CommandResult(handled=True)
    print(f"\n  {SLATE}Writing titles for {len(todo)} chats with {agent.settings.model}: one short call each (Esc stops).{RST}\n")
    done = 0
    try:
        for meta, summary in todo:
            try:
                title = _ai_title_for(agent, summary)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  {ROSE}✗{RST} {SLATE}{meta.session_id[-8:]}: {e}{RST}")
                continue
            if not title:
                continue
            agent.session.rename_session(title, session_id=meta.session_id)
            done += 1
            print(f"  {MINT}✓{RST} {SLATE}{meta.first_prompt[:28]:<28}{RST} {WHITE}→ {title}{RST}")
    except KeyboardInterrupt:
        print(f"\n  {GOLD}Stopped.{RST}")
    ok(f"Titled {done} of {len(todo)} chats")
    return CommandResult(handled=True)


@command("files", category=CAT, description="Files, images and documents you gave this session, and where each is saved",
         aliases=("attachments",), usage="/files")
def handle_files(agent: Any, arg: str) -> CommandResult:
    from axon.agent.attachments import list_session_attachments
    rows = list_session_attachments()
    if not rows:
        info("Nothing attached yet. Drag a file or image into the prompt; Axon saves a copy here at once.")
        return CommandResult(handled=True)
    print()
    for row in rows:
        size = f"{int(row.get('size') or 0) / 1024:.1f} KB"
        print(f"  {WHITE}{row.get('name', '')}{RST} {SLATE}{row.get('kind', '')} · {size}{RST}")
        print(f"    {SLATE}{row.get('stored', '')}{RST}")
    print()
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


def _bookmark_context(agent: Any) -> tuple[Path, list[dict[str, Any]]]:
    """(the session's folder, the chat's turns as the app lists them): both surfaces read the same bookmark file."""
    from axon.features import chat_export as cx
    from axon.features.dashboard import load_transcript
    path = Path(agent.session._file(agent.session.active_session_id))
    try:
        entries = load_transcript(path).entries
    except OSError:
        entries = []
    return path.parent, cx.turns_of(entries, {})


@command("bookmark", category=CAT, description="Bookmark the latest reply, so you can find it again (the app lists it too)",
         usage="/bookmark [note]   marks the latest reply; /bookmarks lists them")
def handle_bookmark(agent: Any, arg: str) -> CommandResult:
    from axon.features import chat_export as cx
    folder, turns = _bookmark_context(agent)
    t = next((x for x in reversed(turns) if x["reply_ts"] is not None), None)
    if t is None:
        info("Nothing to bookmark yet: Axon has not replied in this chat.")
        return CommandResult(handled=True)
    try:
        row, created = cx.add_bookmark(folder, t["reply_ts"], "assistant", " ".join(t["reply"]) or t["user"], arg.strip())
    except ValueError as e:
        err(str(e))
        return CommandResult(handled=True, success=False)
    if created:
        ok(f"Bookmarked: {row['snippet'][:70]}")
        print(f"  {SLATE}{typed_only('/bookmarks lists them. ')}The app shows them under Chat options > Bookmarks.{RST}\n")
    else:
        info("That reply is already bookmarked.")
    return CommandResult(handled=True)


@command("bookmarks", category=CAT, description="List this chat's bookmarks, show one in full, or remove one",
         usage="/bookmarks               list\n/bookmarks show <n>      print the message in full\n/bookmarks remove <n>    remove a bookmark")
def handle_bookmarks(agent: Any, arg: str) -> CommandResult:
    from axon.features import chat_export as cx
    folder, turns = _bookmark_context(agent)
    rows = cx.live_bookmarks(folder, turns)
    words = arg.split()
    if words and words[0].lower() in ("show", "remove", "rm", "delete"):
        try:
            row = rows[int(words[1]) - 1]
            if int(words[1]) < 1:
                raise IndexError
        except (IndexError, ValueError):
            err(f"Give a bookmark number from 1 to {len(rows)}." if rows else "There are no bookmarks.")
            return CommandResult(handled=True, success=False)
        if words[0].lower() == "show":
            t = turns[row["n"]]
            text = "\n\n".join(t["reply"]) if row["kind"] == "assistant" else t["user"]
            from axon.session.activity import redact_text
            print(f"\n{redact_text(text).strip()}\n")
        else:
            cx.remove_bookmark(folder, row["id"])
            ok("Bookmark removed")
        return CommandResult(handled=True)
    if words:
        usage("/bookmarks [show <n> | remove <n>]")
        return CommandResult(handled=True)
    header("Bookmarks", f"{len(rows)} in this chat")
    if not rows:
        print(f"    {SLATE}No bookmarks yet.{typed_only(' /bookmark marks the latest reply.')}{RST}\n")
        return CommandResult(handled=True)
    for i, b in enumerate(rows, 1):
        when = datetime.fromtimestamp(b["created"]).strftime("%m-%d %H:%M") if b.get("created") else ""
        note = f"  {DARK_SLATE}{b['note']}{RST}" if b.get("note") else ""
        print(f"    {CYAN}{i:>3}{RST}  {SLATE}{when}{RST}  {WHITE}{b['snippet'][:70]}{RST}{note}")
    print()
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
    files: list[str] = field(default_factory=list)   # the files those edits touched (paths)


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
        points.append(RewindPoint(index=k + 1, prompt=_prompt_text(msgs[mi]), ts=ts, files_changed=len(files), message_index=mi,
                                  files=sorted(files)))
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


class RewindNotSaved(ValueError):
    """The rewind record could not be written; nothing was changed."""


class RewindConflict(ValueError):
    """Files changed outside Axon since it last wrote them: a code rewind would overwrite the changes."""

    def __init__(self, message: str, files: list[str]) -> None:
        super().__init__(message)
        self.files = files


def _code_conflicts(cp: Any, ts: float) -> list[str]:
    """Files a rewind to time ``ts`` would overwrite although they changed outside Axon (see features/checkpoint_view)."""
    from axon.features import checkpoint_view as cv
    snaps = cp.list_snapshots() if hasattr(cp, "list_snapshots") else []
    n = next((i + 1 for i, s in enumerate(snaps) if s.timestamp >= ts or s.turn_id == -1), 0)
    if not n or cp.pending_count():
        return []
    try:
        return [f["rel"] for f in cv.plan_restore(cp, n)["conflicts"]]
    except cv.RestoreError:
        return []


def apply_rewind(agent: Any, index: int, mode: str, force: bool = False) -> dict[str, Any]:
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
    cp = getattr(agent, "checkpoints", None) if mode in ("code", "code_and_conversation") else None
    restore_code = cp is not None and bool(pt.ts) and hasattr(cp, "rewind_to")
    if restore_code:
        clash = [] if force else _code_conflicts(cp, pt.ts)
        if clash:
            raise RewindConflict(f"{len(clash)} file(s) changed outside Axon since it last wrote them: {', '.join(clash[:6])}"
                                 + (" ..." if len(clash) > 6 else "") + ". Nothing was changed.", clash)
    kept: list[dict[str, Any]] | None = None
    if mode in ("conversation", "code_and_conversation"):
        kept = msgs[:pt.message_index]
        out["removed"] = len(msgs) - len(kept)
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
    if mode != "code":
        # Save the rewind before anything changes: a rewind the transcript does not hold is undone by the next resume.
        sess = getattr(agent, "session", None)
        if sess is not None and hasattr(sess, "append_rewind"):
            try:
                sess.append_rewind(list(kept if kept is not None else msgs), mode, index)
            except Exception as e:
                raise RewindNotSaved(f"The rewind could not be saved to the session file ({e}). Nothing was changed.") from e
    if restore_code:
        try:
            out["reverted"] = cp.rewind_to(pt.ts)
        except Exception as e:
            if mode == "code":
                raise
            out["reverted"] = [f"Failed to restore files: {e}"]    # the conversation rewind is already saved; finish it
    elif cp is not None and not pt.ts:
        out["reverted"] = ["Skipped code restore: the prompt's time is unknown (resumed without a transcript)"]
    if kept is not None:
        agent.conversation.messages[:] = kept
    out["left"] = len(agent.conversation.messages)
    if mode != "code":
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
        left = f", {res['left']} left" if isinstance(res.get("left"), int) else ""
        print(f"    {SLATE}• {res['removed']} message(s) removed from the conversation{left} · saved, so a resume keeps it{RST}")
    elif mode != "code":
        print(f"    {SLATE}• the conversation had nothing after that prompt to remove{RST}")
    if mode in ("code", "code_and_conversation") and not res["reverted"]:
        print(f"    {SLATE}• no file edits to undo since that prompt{RST}")
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
    typed_hint("    /rewind to <n> [code_and_conversation|conversation|code|summarize_from_here]")
    print()


def _rewind_menu(agent: Any, points: list[RewindPoint]) -> CommandResult:
    """Two-step menu (axon.ui.picker.run_rewind_menu): pick a prompt, then what to restore."""
    from axon.ui.picker import run_rewind_menu
    picked = run_rewind_menu(points)
    if not picked:
        info("Rewind cancelled.")
        return CommandResult(handled=True)
    pt, mode = picked
    try:
        res = apply_rewind(agent, pt.index, mode)
    except RewindConflict as e:
        err(str(e))
        if not confirm("Overwrite those files anyway? A copy of what is there now is kept.", default=False):
            info("Nothing was changed.")
            return CommandResult(handled=True)
        res = apply_rewind(agent, pt.index, mode, force=True)
    except ValueError as e:
        err(str(e))
        return CommandResult(handled=True, success=False)
    _print_rewind_result(res, mode, pt.index)
    return CommandResult(handled=True)


REWIND_USAGE = """/rewind                     pick a prompt, then restore code and/or conversation (menu)
/rewind to <n> [mode] [--overwrite]   modes: code_and_conversation (default), conversation, code, summarize_from_here
/rewind points              list prompts you can rewind to
/rewind list                the file checkpoints: time, prompt, files, +/- lines
/rewind show <n>            what checkpoint n changed
/rewind restore <n> [--keep-mine|--overwrite] [--yes]   put the files back to before checkpoint n (undo it by restoring the new checkpoint)
/rewind <n> [--overwrite]   undo the file edits of the last n turns"""


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
        force = "--overwrite" in a.split()
        parts = [w for w in a.split() if w != "--overwrite"]
        try:
            idx = int(parts[1].lstrip("#"))
        except (IndexError, ValueError):
            usage(REWIND_USAGE)
            return CommandResult(handled=True)
        mode = parts[2] if len(parts) > 2 else "code_and_conversation"
        try:
            res = apply_rewind(agent, idx, mode, force=force)
        except RewindConflict as e:
            err(str(e) + typed_only(" Add --overwrite to replace them (a copy is kept)."))
            return CommandResult(handled=True)
        except ValueError as e:
            err(str(e))
            return CommandResult(handled=True)
        _print_rewind_result(res, mode, idx)
        return CommandResult(handled=True)
    if cp is None:
        info("Checkpoints are not enabled for this session.")
        return CommandResult(handled=True)
    if a in ("list", "ls") or a.startswith(("show", "restore")):
        return _rewind_checkpoints(agent, cp, a)
    words = a.split()
    force = "--overwrite" in words
    words = [w for w in words if w != "--overwrite"]
    try:
        steps = max(1, int(words[0]))
    except (ValueError, IndexError):
        usage(REWIND_USAGE)
        return CommandResult(handled=True)
    snaps = cp.list_snapshots() if hasattr(cp, "list_snapshots") else []
    if snaps and not force and not cp.pending_count():
        from axon.features import checkpoint_view as cv
        try:
            clash = cv.plan_restore(cp, max(1, len(snaps) - steps + 1))["conflicts"]
        except cv.RestoreError:
            clash = []
        if clash:
            err(f"{len(clash)} file(s) changed outside Axon since it last wrote them: " + ", ".join(f["rel"] for f in clash[:6])
                + ". Nothing was changed." + typed_only(" Add --overwrite to replace them (a copy is kept)."))
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


def _rewind_checkpoints(agent: Any, cp: Any, a: str) -> CommandResult:
    """/rewind list, show <n> and restore <n>: the checkpoint timeline (axon.features.checkpoint_view, same as the app)."""
    from axon.features import checkpoint_view as cv
    ws = ws_of(agent)
    words = a.split()
    if words[0] in ("list", "ls") or (words[0] == "show" and len(words) == 1):
        rows = cv.timeline(cp, [(p.ts, p.prompt) for p in rewind_points(agent) if p.ts], ws)
        header("Checkpoints", f"{len(rows)} checkpoint(s), newest last")
        if not rows:
            print(f"    {SLATE}No file edits recorded yet.{RST}\n")
            return CommandResult(handled=True)
        for r in rows:
            names = ", ".join(f["rel"] for f in r["files"][:3]) + (f" +{r['file_count'] - 3}" if r["file_count"] > 3 else "")
            what = r["label"] or (" ".join(r["prompt"].split())[:44] if r["prompt"] else "(no prompt recorded)")
            print(f"    {CYAN}{r['n']:>2}{RST}  {SLATE}{cv.when(r['time'])}{RST}  {WHITE}{what}{RST}")
            print(f"        {SLATE}{r['file_count']} file(s): {names}{RST}  {MINT}+{r['added']}{RST} {ROSE}-{r['removed']}{RST}")
        typed_hint("\n    /rewind show <n> · /rewind restore <n>")
        print()
        return CommandResult(handled=True)
    try:
        n = int(words[1].lstrip("T#t")) if len(words) > 1 else 0
    except ValueError:
        n = 0
    if not n:
        usage(REWIND_USAGE)
        return CommandResult(handled=True)
    try:
        if words[0] == "show":
            files = cv.checkpoint_diff(cp, n, ws)
            if not files:
                info(f"Checkpoint {n} changed no files.")
            from axon.commands.code_cmds import _print_diff
            for f in files:
                header(f["rel"], f["status"])
                if f["rows"]:
                    sign = {"add": "+", "del": "-", "ctx": " ", "meta": "\\"}
                    _print_diff("\n".join([f"--- a/{f['rel']}", f"+++ b/{f['rel']}"] + [r["text"] if r["kind"] == "hunk" else sign[r["kind"]] + r["text"] for r in f["rows"]]))
                else:
                    print(f"    {SLATE}{f['note']}{RST}\n")
            return CommandResult(handled=True)
        flags = [w for w in words[2:] if w.startswith("--")]
        plan = cv.plan_restore(cp, n, ws)
        if not plan["files"]:
            info("Nothing to restore: the files already match how they were before that checkpoint."
                 + "".join(f"\n    {s['rel']}: {s['why']}" for s in plan["skipped"]))
            return CommandResult(handled=True)
        header(f"Restore to before checkpoint {n}", f"{len(plan['files'])} file(s), undoes {plan['checkpoints']} checkpoint(s)")
        for line in cv.summary_lines(plan):
            print(f"    {ROSE if 'CONFLICT' in line else WHITE}{line}{RST}")
        for s in plan["skipped"]:
            print(f"    {SLATE}{s['rel']}: left alone ({s['why']}){RST}")
        mode = "overwrite" if "--overwrite" in flags else "keep" if "--keep-mine" in flags else "refuse"
        if plan["conflicts"] and mode == "refuse":
            if is_tty():
                from axon.ui.menus import choose_value
                mode = choose_value(f"{len(plan['conflicts'])} file(s) changed outside Axon", [
                    ("Keep my changes, restore the rest", "keep", "the conflicting files are left as they are"),
                    ("Overwrite my changes too", "overwrite", "a copy of what is there now is kept; restore the new checkpoint to get it back"),
                    ("Cancel", None, "nothing is changed")]) or ""
                if not mode:
                    info("Nothing was changed.")
                    return CommandResult(handled=True)
            else:
                err("Those files changed outside Axon since it last wrote them. Nothing was changed. Add --keep-mine or --overwrite.")
                return CommandResult(handled=True)
        if not ("--yes" in flags or (is_tty() and confirm("Restore these files?", default=False))):
            info("Nothing was changed." if is_tty() else "Nothing was changed. Add --yes to restore.")
            return CommandResult(handled=True)
        res = cv.restore(cp, n, ws, mode)
        try:
            agent.file_state.clear()
        except Exception:
            pass
    except cv.RestoreError as e:
        err(str(e))
        return CommandResult(handled=True)
    (ok if res["ok"] else err)(res["message"] + (f" Undo it by restoring checkpoint {res['checkpoint']}." if res["checkpoint"] else ""))
    for name in res["failed"]:
        print(f"    {ROSE}• {name}{RST}")
    return CommandResult(handled=True)


def _export_markdown(agent: Any, messages: list[dict[str, Any]] | None = None) -> str:
    lines = [
        f"# Axon session {agent.session.active_session_id}",
        "",
        f"- Workspace: `{agent.settings.workspace}`",
        f"- Model: `{agent.settings.model}`",
        f"- Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Cost: ${float(agent.ledger.total()):.4f}",
        "",
    ]
    for m in (agent.conversation.messages if messages is None else messages):
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


def _export_text(agent: Any, messages: list[dict[str, Any]] | None = None) -> str:
    """Plain-text transcript: user prompts and answers, tool calls as one-liners."""
    lines = [f"Axon session {agent.session.active_session_id} · {agent.settings.model} · {agent.settings.workspace}", ""]
    for m in (agent.conversation.messages if messages is None else messages):
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


def _without_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation as prompts and answers only: no tool calls, no tool results."""
    out: list[dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "tool" or (m.get("role") == "user" and isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)):
            continue
        keep = dict(m)
        keep.pop("tool_calls", None)
        if isinstance(c, list):
            keep["content"] = [b for b in c if not (isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result", "thinking"))]
        if m.get("role") == "assistant" and not (keep.get("content") if not isinstance(keep.get("content"), str) else keep["content"].strip()):
            continue
        out.append(keep)
    return out


def export_page(agent: Any, fmt: str, include_tools: bool = True) -> tuple[str, int]:
    """(standalone HTML, secrets removed) for ``html`` and ``pdf``: the file the app's Export dialog writes too."""
    from axon.features import chat_export as cx
    meta = {"id": agent.session.active_session_id, "title": f"Axon session {agent.session.active_session_id}",
            "model": agent.settings.model}
    out = cx.build(cx.items_from_messages(list(agent.conversation.messages)), meta, "html", include_tools, True)
    return out["content"], out["redacted"]


def export_payload(agent: Any, fmt: str, include_tools: bool = True) -> str:
    msgs = None if include_tools else _without_tools(list(agent.conversation.messages))
    if fmt in ("html", "pdf"):
        return export_page(agent, fmt, include_tools)[0]
    if fmt == "json":
        return json.dumps({
            "session_id": agent.session.active_session_id,
            "model": agent.settings.model,
            "workspace": str(agent.settings.workspace),
            "cost_usd": float(agent.ledger.total()),
            "messages": agent.conversation.messages if msgs is None else msgs,
        }, indent=2, ensure_ascii=False, default=str)
    if fmt == "txt":
        return _export_text(agent, msgs)
    return _export_markdown(agent, msgs)


_FMT_ALIASES = {"md": "md", "markdown": "md", "json": "json", "txt": "txt", "text": "txt", "plain": "txt", "html": "html", "pdf": "pdf"}


@command("export", category=CAT, description="Export the conversation (Markdown, web page, PDF, JSON, text) to a file or the clipboard",
         usage="/export [md|html|pdf|json|txt] [path]   default path: ~/.axon/projects/<project>/exports/<session>.<ext>\n/export [md|json|txt] clipboard   copy instead of saving\n/export html|pdf                 one file with secrets removed (pdf: open it in a browser and print)\n/export <format> --no-tools      leave out tool calls and results\n/export                          on a terminal: choose clipboard or file")
def handle_export(agent: Any, arg: str) -> CommandResult:
    words = arg.split()
    include_tools = "--no-tools" not in words
    arg = " ".join(w for w in words if w != "--no-tools") if not include_tools else arg
    parts = arg.strip().split(maxsplit=1)
    fmt, fname = "md", ""
    if parts:
        if parts[0].lower() in _FMT_ALIASES:
            fmt = _FMT_ALIASES[parts[0].lower()]
            fname = parts[1] if len(parts) > 1 else ""
        else:
            fname = arg.strip()
            if len(parts) == 1 and not any(ch in fname for ch in "./\\~") and fname.lower() not in ("clipboard", "clip", "copy", "--clipboard"):
                # one bare word that is not a format: a typo, not a file name (it used to be written as a Markdown file)
                err(f"Unknown export format '{fname}'. Formats: md, html, pdf, json, txt. "
                    f"To save under that name, add an extension or a folder: /export {fname}.md")
                return CommandResult(handled=True, success=False)
            low = fname.lower()
            fmt = "json" if low.endswith(".json") else "txt" if low.endswith(".txt") else "html" if low.endswith((".html", ".htm")) else "md"
    to_clipboard = fname.lower() in ("clipboard", "clip", "copy", "--clipboard")
    if not arg.strip() and is_tty():
        from axon.ui.picker import SelectItem, Selector
        sel = Selector([SelectItem(label="Copy to clipboard (Markdown)", value="clip"),
                        SelectItem(label="Save to a file (Markdown)", value="file", hint=f"~/.axon/projects/<project>/exports/{agent.session.active_session_id}.md"),
                        SelectItem(label="Save as a web page (HTML, secrets removed)", value="html"),
                        SelectItem(label="Save for PDF (HTML to print from a browser)", value="pdf"),
                        SelectItem(label="Save as JSON", value="json"), SelectItem(label="Save as plain text", value="txt")],
                       title="Export conversation").run()
        choice = getattr(sel, "value", None)
        if choice is None:
            info("Export cancelled.")
            return CommandResult(handled=True)
        to_clipboard = choice == "clip"
        fmt = choice if choice in ("json", "txt", "html", "pdf") else "md"
    removed = None
    if fmt in ("html", "pdf"):
        payload, removed = export_page(agent, fmt, include_tools)
    else:
        payload = export_payload(agent, fmt, include_tools)
    if to_clipboard:
        from axon.ui.clipboard import copy_to_clipboard
        if copy_to_clipboard(payload):
            ok(f"Copied {len(agent.conversation.messages)} messages ({len(payload):,} chars, {fmt}) to the clipboard")
        else:
            err("No clipboard utility available (pbcopy, clip, xclip, wl-copy)." + typed_only(" Use /export <path>."))
        return CommandResult(handled=True)
    if fname:
        out_path = Path(fname).expanduser()
        if not out_path.is_absolute():
            out_path = ws_of(agent) / out_path
    else:
        from axon import storage
        out_path = storage.session_subdir(ws_of(agent), agent.session.active_session_id, "exports") / f"{agent.session.active_session_id}.{'html' if fmt == 'pdf' else fmt}"
    if out_path.exists() and not is_tty():
        # No keyboard to ask "Overwrite?" (the web app): never overwrite; the default file gets a numbered name instead.
        if fname:
            err(f"{out_path} already exists. Give another file name, or remove that file first.")
            return CommandResult(handled=True, success=False)
        n = 2
        while out_path.with_name(f"{out_path.stem}-{n}{out_path.suffix}").exists():
            n += 1
        out_path = out_path.with_name(f"{out_path.stem}-{n}{out_path.suffix}")
    if out_path.exists() and not confirm(f"{out_path} exists. Overwrite?", default=False):
        info("Export cancelled.")
        return CommandResult(handled=True)
    from axon.session.store import atomic_write_text
    atomic_write_text(out_path, payload)
    ok(f"Exported {len(agent.conversation.messages)} messages to {WHITE}{out_path}{RST}")
    if removed is not None:
        print(f"  {SLATE}{removed} secret{'s' if removed != 1 else ''} (keys, tokens, passwords) removed. Read it through before you share it; nothing was uploaded.{RST}")
    if fmt == "pdf":
        print(f"  {SLATE}Open it in a browser and print it (Save as PDF): it has a print layout.{RST}")
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


def _history_all(agent: Any, session_id: str = "") -> CommandResult:
    """Every record of a transcript, including messages a /rewind or compaction removed."""
    from axon.session.store import iter_history
    sess = agent.session
    sid = session_id or sess.active_session_id
    path = sess._file(sid) if hasattr(sess, "_file") else getattr(sess, "active_file", None)
    rows = list(iter_history(path)) if path is not None else []
    header("Full transcript", f"{len(rows)} record(s) · {path}")
    if not rows:
        print(f"    {SLATE}(empty){RST}\n")
        return CommandResult(handled=True)
    for r in rows:
        clr = {"user": TEAL, "assistant": GOLD, "tool": LBLUE}.get(r["role"], DARK_SLATE)
        when = datetime.fromtimestamp(float(r["timestamp"])).strftime("%m-%d %H:%M:%S") if r.get("timestamp") else ""
        text = " ".join(str(r["text"]).split())
        print(f"    {SLATE}{r['index']:>4} {when}{RST} {clr}{r['role'] or r['type']:<9}{RST} {text[:96]}")
    print(f"\n  {DARK_SLATE}Rewound/compacted messages stay in the transcript file; /export or the file has them in full.{RST}\n")
    return CommandResult(handled=True)


@command("history", category=CAT, description="List the messages in the conversation",
         usage="/history [window]\n/history all [session-id]   every record of the transcript, rewound and compacted messages included")
def handle_history(agent: Any, arg: str, window_only: bool = False) -> CommandResult:
    parts = arg.strip().split()
    if parts and parts[0].lower() in ("all", "full", "raw"):
        return _history_all(agent, parts[1] if len(parts) > 1 else "")
    return _history(agent, window_only or arg.strip().lower() == "window")


@command("whistory", category=CAT, description="List only the messages inside the sliding window", hidden=True)
def handle_whistory(agent: Any, arg: str) -> CommandResult:
    return _history(agent, True)
