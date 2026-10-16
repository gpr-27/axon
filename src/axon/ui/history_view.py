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

from axon.ui.markdown import format_markdown
from axon.ui.theme import (
    ACCENT, BOLD, BORDER, DIM, ERROR, INFO, ITALIC, MUTED, PRIMARY, RST, SUBTLE, SUCCESS, TEXT, WARNING,
    glyph, layout_width, sanitize, truncate, wrap_ansi,
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
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return out


def _full_output(result: dict[str, Any]) -> str:
    """The complete tool output: the saved full-output file when the transcript copy was cut."""
    full = str(result.get("full_output") or "")
    if full:
        try:
            return Path(full).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    content = result.get("content", "")
    return content if isinstance(content, str) else _text_of(content) or json.dumps(content, default=str)


def format_session_history(path: Path, registry: Any = None, width: int | None = None,
                           row_tags: list[Any] | None = None, tool_tags: dict[str, Any] | None = None,
                           show_thinking: bool = True, tag: str = "") -> list[str]:
    """Rows for the whole session in ``path`` (``row_tags`` receives the transcript-entry tag of each
    row for click-to-expand; ``tool_tags`` maps tool_use_id -> tag)."""
    from axon.ui.input import format_user_prompt_banner
    from axon.ui.render import Renderer, _one_line, _plural, _usage_line, tool_purpose, tool_result_rows, tool_target
    _SHELL_CTX = Renderer._SHELL_CTX_RE

    width = width or max(40, layout_width(4))
    records = _records(path)
    results: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("type") == "tool_results" and isinstance(r.get("data"), list):
            for res in r["data"]:
                if isinstance(res, dict) and res.get("tool_use_id"):
                    results[str(res["tool_use_id"])] = res

    rail = f"{BORDER}{glyph('v')}{RST} "
    says = f"{ACCENT}{glyph('quote')}{RST} "
    dot = glyph("dot")
    out: list[str] = []
    tags: list[Any] = []
    state = {"open": False, "turn": 0, "step": 0, "cost": 0.0, "calls": 0, "tools": 0, "time": 0.0}

    def add(row: str, tag: Any = None) -> None:
        out.append(row)
        tags.append(tag)

    def add_many(rows: Iterable[str], tag: Any = None) -> None:
        for r in rows:
            add(r, tag)

    def wrap_rows(text: str, lead: str, cont: str) -> list[str]:
        avail = max(20, width - 8)
        rows = wrap_ansi(text, avail) or [""]
        return [(lead if i == 0 else cont) + r for i, r in enumerate(rows)]

    def close_turn() -> None:
        if not state["open"]:
            return
        facts = [_plural(state["calls"], "LLM call"), _plural(state["tools"], "tool call")]
        if state["time"]:
            facts.insert(0, f"{state['time']:.1f}s")
        if state["cost"]:
            facts.append(f"${state['cost']:.4f}")
        add(f"{BORDER}{glyph('sq_bl')}{RST} {SUCCESS}{glyph('ok')}{RST} {MUTED}{f' {dot} '.join(facts)}{RST}")
        add("")
        state.update(open=False, step=0, cost=0.0, calls=0, tools=0, time=0.0)

    def open_turn(prompt: str) -> None:
        close_turn()
        state["turn"] += 1
        if prompt:
            add_many(format_user_prompt_banner(prompt, max_w=width + 4).splitlines())
            add("")
        head = f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {state['turn']}{RST}" + (f" {MUTED}{tag}{RST}" if tag else "")
        if prompt:
            head += f" {MUTED}{dot}{RST} {TEXT}\"{_one_line(prompt, 60)}\"{RST}"
        add(truncate(head, width + 2))
        state["open"] = True

    def tool_block(tu: dict[str, Any]) -> None:
        name = str(tu.get("name", "tool"))
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        call_id = str(tu.get("id", ""))
        tag = (tool_tags or {}).get(call_id)
        state["tools"] += 1
        add_many(wrap_rows(f"{PRIMARY}{glyph('tool')}{RST} {BOLD}{name}{RST}  {TEXT}{tool_target(name, inp)}{RST}",
                           f"{rail}  ", f"{rail}      "), tag)
        purpose = tool_purpose(name, registry)
        if purpose:
            add(f"{rail}      {SUBTLE}{purpose}{RST}", tag)
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
            rows = tool_result_rows(name, inp, content, is_err, width - 8, registry)
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
            if state["step"] > 1:
                add(rail.rstrip())
            add(truncate(f"{rail}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {state['step']}{RST}  {TEXT}LLM call{RST}"
                         + (f" {MUTED}{dot}{RST} {TEXT}{model}{RST}" if model else ""), width + 2))
            thinking = str(data.get("thinking") or "").strip()
            if thinking and show_thinking:
                add(f"{rail}  {SUBTLE}{glyph('star')}{RST} {MUTED}{ITALIC}Thinking{RST}")
                for para in thinking.splitlines():
                    add_many(wrap_rows(f"{DIM}{ITALIC}{para}{RST}", f"{rail}  {SUBTLE}{glyph('thinking_bar')}{RST} ",
                                       f"{rail}  {SUBTLE}{glyph('thinking_bar')}{RST} "))
            text = str(data.get("text") or "").strip()
            if text:
                for r in format_markdown(text, max_width=width - 2).splitlines():
                    add(f"{rail}{says}{r[2:] if r.startswith('  ') else r}")
            usage = data.get("usage")
            if usage:
                dur = data.get("duration_s")
                try:
                    state["cost"] += float(data.get("cost") or 0)
                    state["time"] += float(dur or 0)
                except (TypeError, ValueError):
                    pass
                stats = (f"replied in {float(dur):.1f}s {dot} " if dur else "") + _usage_line(usage, cost=data.get("cost"))
                add_many(wrap_rows(f"{SUBTLE}{glyph('wrap')}{RST} {MUTED}{stats}{RST}", f"{rail}  ", f"{rail}    "))
            uses = [u for u in (data.get("tool_uses") or []) if isinstance(u, dict)]
            if len(uses) > 1:
                add(f"{rail}  {MUTED}{glyph('arrow')} runs {_plural(len(uses), 'tool')}{RST}")
            for tu in uses:
                tool_block(tu)
        elif t == "permission" and isinstance(data, dict):
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
        elif t == "turn_failed" and isinstance(data, dict):
            if not state["open"]:
                open_turn(str(data.get("prompt") or ""))
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
