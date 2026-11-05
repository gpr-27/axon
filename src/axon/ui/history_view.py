"""
Full session history, drawn like the live timeline, from the session transcript on disk.

The model's working context is deliberately lossy (old tool outputs are evicted, long sessions are
summarised, thinking is not kept), so redrawing the screen from it made earlier turns look empty.
The transcript file keeps every record, so this view shows each turn completely: the prompt, every
LLM call with its thinking, answer, model, time and cost, every tool call with what it does and its
result, permission decisions, shell commands, side questions, compactions and failures.

Used after a window resize (cli._redraw_for_new_size) and when a session is resumed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from axon.agent import decision as _decision
from axon.ui.markdown import format_markdown
from axon.ui.theme import (
    ACCENT, BOLD, BORDER, DIM, ERROR, INFO, ITALIC, MUTED, RST, SUBTLE, SUCCESS, TEXT, WARNING,
    display_width, glyph, layout_width, sanitize, truncate, wrap_ansi,
)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        from axon.session.store import iter_records
        return list(iter_records(Path(path)))
    except Exception:
        out = []
        try:
            for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):       # a stray `123` / `"x"` line is not a record
                    out.append(rec)
        except OSError:
            pass
        return out


def _full_output(result: dict[str, Any]) -> str:
    """The complete tool output: the saved full-output file when the transcript copy was cut."""
    full = str(result.get("full_output") or "")
    if full:
        try:
            from axon.storage import relocate_recorded_path  # the file may have moved into its session folder
            return relocate_recorded_path(full).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    content = result.get("content", "")
    return content if isinstance(content, str) else _text_of(content) or json.dumps(content, default=str)


def _then_text(result: dict[str, Any], next_step: int) -> str:
    """Where a stored result went: the content the model saw, sized like the live timeline sizes it."""
    content = result.get("content", "")
    text = content if isinstance(content, str) else _text_of(content) or json.dumps(content, default=str)
    est = max(1, int(len(text) / 3.7)) if text else 0
    return _decision.then_text(len(text.encode("utf-8", errors="replace")), est, next_step)


def _model_text(result: dict[str, Any]) -> str:
    """The tool result exactly as the model received it (the transcript keeps that text)."""
    content = result.get("content", "")
    return content if isinstance(content, str) else _text_of(content) or json.dumps(content, default=str)


def format_session_history(path: Path, registry: Any = None, width: int | None = None,
                           row_tags: list[Any] | None = None, tool_tags: dict[str, Any] | None = None,
                           show_thinking: bool = True, tag: str = "", trace_level: str = "normal") -> list[str]:
    """Rows for the whole session in ``path`` (``row_tags`` receives the transcript-entry tag of each
    row for click-to-expand; ``tool_tags`` maps tool_use_id -> tag). *trace_level* picks the layout like
    the live view: compact and normal draw the phased tool cards, detailed and debug the full explainer."""
    from axon.ui import tool_cards
    from axon.ui.theme import strip_ansi
    from axon.ui.input import format_user_prompt_banner
    from axon.ui.render import (
        TRACE_LEVELS, Renderer, _clean, _fmt_bytes, _fmt_tokens, _one_line, _plural, context_gauge, exact_call_rows,
        normalize_trace_level, step_usage, tool_result_rows_exact, turn_summary_rows,
    )
    phased = TRACE_LEVELS.index(normalize_trace_level(trace_level)) <= 1
    _SHELL_CTX = Renderer._SHELL_CTX_RE

    width = width or max(40, layout_width(4))
    records = _records(path)
    results: dict[str, dict[str, Any]] = {}
    permissions: dict[str, dict[str, Any]] = {}     # tool_use_id -> its permission decision
    for r in records:
        if r.get("type") == "tool_results" and isinstance(r.get("data"), list):
            for res in r["data"]:
                if isinstance(res, dict) and res.get("tool_use_id"):
                    results[str(res["tool_use_id"])] = res
        elif r.get("type") == "permission" and isinstance(r.get("data"), dict) and r["data"].get("tool_use_id"):
            permissions[str(r["data"]["tool_use_id"])] = r["data"]

    rail = f"{BORDER}{glyph('v')}{RST} "
    says = f"{ACCENT}{glyph('quote')}{RST} "
    dot = glyph("dot")
    out: list[str] = []
    tags: list[Any] = []
    state = {"open": False, "turn": 0, "step": 0, "cost": 0.0, "calls": 0, "tools": 0, "time": 0.0,
             "answer": (0, ""), "steps": [], "context": (0, 0), "stopped": False, "after_header": False, "head": -1}
    reply: dict[str, Any] = {}     # the model reply whose tool calls are being drawn (for 'chosen by' / 'why')

    def add(row: str, tag: Any = None) -> None:
        out.append(row)
        tags.append(tag)
        state["after_header"] = False

    def gap() -> None:
        """A blank rail row, unless the step header or a blank row is right above."""
        if out and not state["after_header"] and strip_ansi(out[-1]).strip(" │|"):
            add(rail.rstrip())

    def add_many(rows: Iterable[str], tag: Any = None) -> None:
        for r in rows:
            add(r, tag)

    def wrap_rows(text: str, lead: str, cont: str) -> list[str]:
        avail = max(20, width - 8)
        rows = wrap_ansi(text, avail) or [""]
        return [(lead if i == 0 else cont) + r for i, r in enumerate(rows)]

    def done_row() -> str:
        """'└ ✓ Done · 5.2s · 2 LLM calls · 1 tool call · 40k in · 1.2k out · $0.0012'
        (the live footer's line; '✗ Stopped' for a turn that failed)."""
        steps = state["steps"]
        facts = [_plural(state["calls"], "LLM call"), _plural(state["tools"], "tool call"),
                 f"{_fmt_tokens(sum(s['input'] for s in steps))} in",
                 f"{_fmt_tokens(sum(s['output'] for s in steps))} out"]
        if state["time"]:
            facts.insert(0, f"{state['time']:.1f}s")      # time spent in the LLM calls
        if state["cost"]:
            facts.append(f"${state['cost']:.4f}")
        if state["stopped"]:
            head = f"{ERROR}{glyph('fail')} Stopped{RST}"
        else:
            head = f"{SUCCESS}{glyph('ok')} Done{RST}"
        return f"{BORDER}{glyph('sq_bl')}{RST} {head} {MUTED}{dot} {f' {dot} '.join(facts)}{RST}"

    def close_turn() -> None:
        if not state["open"]:
            return
        answer_step, answer_model = state["answer"]
        if state["calls"] > 1 and answer_step and not state["stopped"]:
            add(f"{rail}  {MUTED}{glyph('arrow')} {_decision.final_answer_text(answer_step, answer_model)}{RST}")
        for row in turn_summary_rows(state["steps"], width - 4, stopped=state["stopped"]):
            add(f"{rail}  {row}")
        add_many(wrap_rows(done_row(), "", "    "))
        if all(state["context"]):
            add(f"    {context_gauge(*state['context'])}")      # the context gauge, once per turn
        add("")
        state.update(open=False, step=0, cost=0.0, calls=0, tools=0, time=0.0, answer=(0, ""), steps=[],
                     context=(0, 0), stopped=False)

    def open_turn(prompt: str) -> None:
        close_turn()
        state["turn"] += 1
        if prompt:
            add_many(format_user_prompt_banner(prompt, max_w=width + 2).splitlines())
            add("")
        head = f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {state['turn']}{RST}" + (f" {MUTED}{tag}{RST}" if tag else "")
        if prompt:
            head += f" {MUTED}{dot}{RST} {TEXT}\"{_one_line(prompt, 60)}\"{RST}"
        add(truncate(head, width + 2))
        state["head"] = len(out) - 1
        state["open"] = True

    def tool_block(tu: dict[str, Any]) -> None:
        name = str(tu.get("name", "tool"))
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        call_id = str(tu.get("id", ""))
        tag = (tool_tags or {}).get(call_id)
        state["tools"] += 1
        reply["call_no"] = reply.get("call_no", 0) + 1
        chosen = _decision.chosen_by_text(reply.get("model", ""), reply.get("provider", ""), state["step"],
                                          reply["call_no"], reply.get("uses", 0), reply.get("tool_count", 0),
                                          reply.get("prompt_tools", False))
        why, why_source = reply.get("why", {}).get(call_id, ("", ""))
        # The same header and explanation rows the live timeline prints for this call.
        from axon.ui.render import tool_allowed_row, tool_call_rows, tool_then_row
        for indent, row in tool_call_rows(name, inp, 1, width - 8, registry, why, why_source, chosen):
            add_many(wrap_rows(row, f"{rail}  {' ' * indent}", f"{rail}  {' ' * (indent + 4)}"), tag)
        perm = permissions.get(call_id)
        if perm:
            row = tool_allowed_row(str(perm.get("outcome", "")), str(perm.get("source", "")),
                                   str(perm.get("reason", "")), perm.get("approval"))
            add_many(wrap_rows(row, f"{rail}      ", f"{rail}        "), tag)
        # The arguments exactly as the model sent them (the raw text when they were not valid JSON).
        raw = str(inp.get("raw") or "") if tu.get("parse_error") else ""
        add_many((f"{rail}      {r}" for r in exact_call_rows(name, inp, width - 8, 1, raw)), tag)
        res = results.get(call_id)
        if res is None:
            add(f"{rail}    {SUBTLE}{glyph('result')}  (no result recorded){RST}", tag)
            return
        content = sanitize(_full_output(res))
        is_err = bool(res.get("is_error"))
        import axon.ui.render as _render
        hint = getattr(_render, "_MORE_HINT", "/output")
        _render._MORE_HINT = "/transcript"          # history outputs are browsed in the transcript (Ctrl+F)
        try:
            rows = tool_result_rows_exact(name, inp, content, is_err, width - 8, registry,
                                          hint="/transcript", exact_text=_model_text(res))
        except Exception:
            rows = [content.splitlines()[0] if content else "(no output)"]
        finally:
            _render._MORE_HINT = hint
        if not rows:
            return
        dur = res.get("duration_s")
        first = (f"{ERROR}{glyph('fail')}{RST} " if is_err else "") + rows[0]
        if dur:
            first += f"{SUBTLE} {dot} {float(dur):.2f}s{RST}"
        add_many(wrap_rows(first, f"{rail}    {SUBTLE}{glyph('result')}{RST}  ", f"{rail}       "), tag)
        for r in rows[1:]:
            add_many(wrap_rows(r, f"{rail}       ", f"{rail}         "), tag)
        add_many(wrap_rows(tool_then_row(_then_text(res, state["step"] + 1)), f"{rail}       ", f"{rail}         "), tag)

    def card_block(uses: list[dict[str, Any]]) -> None:
        """The step's tool calls as one 'TOOLS' section of phased cards, then the '→ N results' line."""
        names = [str(u.get("name", "tool")) for u in uses]
        allowed_texts: list[str] = []
        for tu in uses:
            perm = permissions.get(str(tu.get("id", "")))
            allowed_texts.append(_decision.allowed_because(str(perm.get("outcome", "")), str(perm.get("source", "")),
                                                           str(perm.get("reason", "")), perm.get("approval")) if perm else "")
        common = tool_cards.common_permission(allowed_texts)
        gap()
        add(f"{rail}{tool_cards.tools_label(len(uses), tool_cards.batch_mode(names, registry), common)}")
        name_w = tool_cards.name_column_width(names, width - 6)
        n_bytes = n_tokens = 0
        for number, tu in enumerate(uses, 1):
            call_id = str(tu.get("id", ""))
            inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
            state["tools"] += 1
            why, why_source = reply.get("why", {}).get(call_id, ("", ""))
            res = results.get(call_id)
            content = sanitize(_full_output(res)) if res is not None else ""
            if res is not None:
                sent = _model_text(res)
                n_bytes += len(sent.encode("utf-8", errors="replace"))
                n_tokens += max(1, int(len(sent) / 3.7)) if sent else 0
            dur = res.get("duration_s") if res is not None else None
            call = tool_cards.CardCall(
                number=number, name=names[number - 1], inp=inp, why=why, why_source=why_source,
                allowed=allowed_texts[number - 1], content=content,
                is_error=bool(res.get("is_error")) if res is not None else False,
                duration=float(dur) if dur else None, state="done" if res is not None else "stopped",
                hint="/transcript", registry=registry, name_w=name_w, total=len(uses), common_allowed=common)
            add_many((f"{rail}  {r}" for r in tool_cards.card_rows(call, width - 6)), (tool_tags or {}).get(call_id))
        add(f"{rail}  {tool_cards.step_results_text(len(uses), n_bytes, n_tokens, state['step'] + 1, width - 6)}")

    for rec in records:
        t, data = rec.get("type"), rec.get("data")
        if t == "user_message" and isinstance(data, dict):
            text = _SHELL_CTX.sub("", _text_of(data.get("content"))).strip()
            if text:
                open_turn(text)
        elif t == "assistant_turn" and isinstance(data, dict):
            if not state["open"]:
                open_turn("")
            state["step"] += 1
            state["calls"] += 1
            model = str(data.get("model") or "")
            call = data.get("call") if isinstance(data.get("call"), dict) else {}
            if state["step"] > 1:
                add(rail.rstrip())
            # Same header as the live step row: model (provider) · reads N messages (~tokens).
            prov = str(call.get("provider") or "")
            reads = int(call.get("message_count") or 0)
            size = f" (~{_fmt_tokens(int(call.get('tokens_in') or 0))} tokens)" if call.get("tokens_in") else ""
            if phased:       # normal: 'Step 4 · model · 7 messages · ~17.2k tokens' (the turn header names the provider)
                tokens = f" {dot} ~{_fmt_tokens(int(call.get('tokens_in') or 0))} tokens" if call.get("tokens_in") else ""
                add(truncate(f"{rail}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {state['step']}{RST} {MUTED}{dot}{RST} {TEXT}{model}{RST}"
                             + (f" {MUTED}{dot} {_plural(reads, 'message')}{tokens}{RST}" if reads else ""), width + 2))
                state["after_header"] = True
                if state["step"] == 1 and state["head"] >= 0 and model:     # put model + provider in the turn header
                    right = f"{MUTED}{model}" + (f" via {prov}" if prov else "") + RST
                    head = out[state["head"]]
                    room = width + 2 - display_width(head) - display_width(right)
                    if room >= 2:
                        out[state["head"]] = head + " " * room + right
            else:
                add(truncate(f"{rail}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {state['step']}{RST}  {TEXT}LLM call{RST}"
                             + (f" {MUTED}{dot}{RST} {TEXT}{model}{RST}" if model else "")
                             + (f" {MUTED}({prov}){RST}" if prov else "")
                             + (f" {MUTED}{dot} reads {_plural(reads, 'message')}{size}{RST}" if reads else ""), width + 2))
            # Same "includes" line the live view draws from the results it fed back into this call.
            fed = [f for f in (call.get("fed_back") or []) if isinstance(f, dict)]
            if state["step"] > 1 and fed and not phased:
                total_bytes = sum(int(f.get("bytes") or 0) for f in fed)
                total_tok = sum(int(f.get("tokens") or 0) for f in fed)
                errors = sum(1 for f in fed if f.get("is_error"))
                line = (f"{glyph('arrow')} includes {_plural(len(fed), 'tool result')} from step {state['step'] - 1} "
                        f"({_fmt_bytes(total_bytes)} {dot} ~{_fmt_tokens(total_tok)} tokens)")
                if errors:
                    line += f" {dot} {ERROR}{_plural(errors, 'error')}{RST}{MUTED}"
                add(f"{rail}  {MUTED}{line}{RST}")
                # Each tool call fed into this step, in the same format as the live verbose rows.
                for f in fed:
                    status = f"{ERROR}{glyph('fail')}{RST}" if f.get("is_error") else f"{SUCCESS}{glyph('ok')}{RST}"
                    add(f"{rail}    {status} {BOLD}{_clean(f.get('name', 'Tool'))}{RST} {MUTED}{_clean(f.get('target') or '')}{RST} "
                        f"{SUBTLE}{dot} {int(f.get('lines') or 0)} lines {dot} {int(f.get('bytes') or 0):,} bytes {dot} "
                        f"~{_fmt_tokens(int(f.get('tokens') or 0))} tok{RST}")
            thinking = sanitize(str(data.get("thinking") or "")).strip()        # model text: no raw escapes
            if thinking and show_thinking and phased:
                secs = sum(float(x) for x in (data.get("thinking_s") or []) if isinstance(x, (int, float)))
                add(f"{rail}{tool_cards.thinking_line(secs or None, thinking, width - 2)}")
            elif thinking and show_thinking:
                add(f"{rail}  {SUBTLE}{glyph('star')}{RST} {MUTED}{ITALIC}Thinking{RST}")
                for para in thinking.splitlines():
                    add_many(wrap_rows(f"{DIM}{ITALIC}{para}{RST}", f"{rail}  {SUBTLE}{glyph('thinking_bar')}{RST} ",
                                       f"{rail}  {SUBTLE}{glyph('thinking_bar')}{RST} "))
                for secs in data.get("thinking_s") or []:
                    if isinstance(secs, (int, float)):
                        add(f"{rail}  {SUBTLE}{glyph('star')} thought for {secs:.1f}s{RST}")
            text = str(data.get("text") or "").strip()
            if text and phased:
                gap()
                add(f"{rail}{tool_cards.answer_label()}")
            if text:
                for r in format_markdown(text, max_width=width - 2).splitlines():
                    add(f"{rail}{says}{r[2:] if r.startswith('  ') else r}")
            usage = data.get("usage")
            if usage:
                # Recorded for the end-of-turn table (the live view shows no cost line mid-turn either).
                dur = data.get("duration_s")
                try:
                    state["cost"] += float(data.get("cost") or 0)
                    state["time"] += float(dur or 0)
                except (TypeError, ValueError):
                    pass
                state["steps"].append(step_usage(state["step"], model, dur, usage, data.get("cost")))
                if call.get("context_used") and call.get("context_window"):
                    state["context"] = (int(call["context_used"]), int(call["context_window"]))
            uses = [u for u in (data.get("tool_uses") or []) if isinstance(u, dict)]
            if not uses and data.get("stop_reason") not in (None, "tool_use"):
                state["answer"] = (state["step"], model)
            try:
                why = _decision.pick_rationales(_decision.reply_blocks(data))
            except Exception:
                why = {}
            reply.clear()
            reply.update(model=model, provider=prov, uses=len(uses), call_no=0, why=why,
                         tool_count=int(call.get("tool_count") or 0), prompt_tools=bool(call.get("prompt_tools")))
            if phased and uses:
                card_block(uses)
            else:
                if len(uses) > 1:
                    add(f"{rail}  {MUTED}{glyph('arrow')} runs {_plural(len(uses), 'tool')}{RST}")
                for tu in uses:
                    tool_block(tu)
        elif t == "permission" and isinstance(data, dict) and not data.get("tool_use_id"):
            outcome, source = str(data.get("outcome", "")), str(data.get("source", ""))
            if outcome == "deny":
                add(f"{rail}      {ERROR}{glyph('fail')} denied{(' by ' + source) if source else ''}"
                    f"{(': ' + _one_line(data.get('reason', ''), 120)) if data.get('reason') else ''}{RST}")
            elif source == "user" or data.get("approval"):
                add(f"{rail}      {SUCCESS}{glyph('ok')}{RST} {MUTED}approved by you"
                    f"{(' (' + str(data.get('approval')) + ')') if data.get('approval') else ''}{RST}")
        elif t == "shell_command" and isinstance(data, dict):
            close_turn()
            code = data.get("exit_code")
            add(f"  {WARNING}{BOLD}!{RST} {TEXT}{_one_line(data.get('command', ''), 200)}{RST}"
                + (f" {ERROR}exit {code}{RST}" if code else ""))
            output = sanitize(_full_output({"content": data.get("output", ""), "full_output": data.get("full_output")}))
            for r in output.rstrip().splitlines()[:12]:
                add(f"    {DIM}{r}{RST}")
            n = len(output.rstrip().splitlines())
            if n > 12:
                add(f"    {SUBTLE}{glyph('ellipsis')} +{n - 12} more lines{RST}")
            add("")
        elif t == "side_question" and isinstance(data, dict):
            add(f"  {INFO}/btw{RST} {TEXT}{_one_line(data.get('question', ''), 200)}{RST}")
            ans = str(data.get("answer") or data.get("error") or "").strip()
            for r in format_markdown(ans, max_width=width - 6).splitlines() if ans else []:
                add(f"    {r}")
            add("")
        elif t == "steer_message" and isinstance(data, dict):
            add(f"{rail}  {INFO}{glyph('arrow')} you added while it worked:{RST} {TEXT}{_one_line(data.get('text', ''), 200)}{RST}")
        elif t == "compaction":
            add(f"{rail}  {INFO}{glyph('info')} context compacted{RST} {MUTED}(older turns were summarised for the "
                f"model; everything is still shown here){RST}")
        elif t == "rewind" and isinstance(data, dict):
            close_turn()
            add(f"  {INFO}{glyph('info')} rewound{RST} {MUTED}to an earlier point ({data.get('mode', '')}); the turns "
                f"above stay in the record{RST}")
            add("")
        elif t in ("skill_run", "subagent_result", "hook_run", "command_run", "plan", "saved") and isinstance(data, dict):
            lead = f"{rail}  " if state["open"] else "  "
            if t == "subagent_result":
                status = str(data.get("status", ""))
                color = SUCCESS if status == "completed" else (ERROR if status == "error" else WARNING)
                line = (f"{color}{glyph('arrow')}{RST} {BOLD}subagent {data.get('agent_type', '')}{RST} "
                        f"{TEXT}{_one_line(data.get('title') or data.get('prompt', ''), 80)}{RST} {MUTED}{status} {dot} "
                        f"{float(data.get('elapsed_s') or 0):.1f}s {dot} ${float(data.get('cost') or 0):.4f} {dot} "
                        f"{int(data.get('input_tokens') or 0) + int(data.get('output_tokens') or 0):,} tokens{RST}")
            elif t == "skill_run":
                line = (f"{INFO}{glyph('arrow')}{RST} {BOLD}skill /{data.get('name', '')}{RST} "
                        f"{TEXT}{_one_line(data.get('args', ''), 80)}{RST} {MUTED}{data.get('source') or 'user'}"
                        f"{(' ' + dot + ' ' + str(data.get('scope'))) if data.get('scope') else ''}{RST}")
            elif t == "hook_run":
                line = (f"{(ERROR if data.get('blocked') else MUTED)}{glyph('info')} hook {data.get('event', '')}"
                        f"{(' ' + str(data.get('tool_name'))) if data.get('tool_name') else ''}"
                        f"{' blocked' if data.get('blocked') else ''}{RST} {MUTED}{_one_line(data.get('message', ''), 100)}{RST}")
            elif t == "command_run":
                line = (f"{INFO}{glyph('arrow')}{RST} {BOLD}/{data.get('name', '')}{RST} "
                        f"{TEXT}{_one_line(data.get('args', ''), 80)}{RST} {MUTED}{data.get('kind', '')}{RST}")
            elif t == "plan":
                line = f"{INFO}{glyph('info')} plan {data.get('action', '')}{RST} {MUTED}{data.get('path', '')}{RST}"
            else:
                line = (f"{SUCCESS}{glyph('ok')}{RST} {MUTED}{data.get('action', 'saved')} {data.get('type', '')} "
                        f"{RST}{TEXT}{data.get('name', '')}{RST} {MUTED}({data.get('scope', '')}) "
                        f"{dot} copy kept: {data.get('copy') or data.get('path', '')}{RST}")
            add_many(wrap_rows(line, lead, lead + "    "))
        elif t == "turn_failed" and isinstance(data, dict):
            if not state["open"]:
                open_turn(str(data.get("prompt") or ""))
            state["stopped"] = True
            add_many(wrap_rows(f"{ERROR}{glyph('fail')} model call failed: {_one_line(data.get('error', ''), 300)}{RST}",
                               f"{rail}  ", f"{rail}    "))
    close_turn()
    if row_tags is not None:
        row_tags[:] = tags
    return out


def session_file(agent: Any, session_id: str | None = None) -> Path | None:
    """The transcript file of ``session_id`` (default: the agent's active session), if it exists."""
    store = getattr(agent, "session", None)
    sid = session_id or getattr(store, "active_session_id", "")
    if store is None or not sid:
        return None
    try:
        path = Path(store._file(sid))
    except Exception:
        return None
    return path if path.is_file() else None
