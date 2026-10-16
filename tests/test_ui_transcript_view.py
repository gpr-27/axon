"""Full-screen transcript viewer: hierarchy, fold/expand state, row layout, search, mouse parsing,
the conversation fallback and live elapsed time of running tool calls."""
from __future__ import annotations

import pytest

from axon import platform as plat
from axon.ui.render import TranscriptEntry as E
from axon.ui.theme import display_width, strip_ansi
from axon.ui.transcript_view import (
    TranscriptViewer, build_nodes, elapsed_text, entries_from_messages, fmt_secs, footer_help, plain_dump,
)

NOW = 1_000.0


def _src(seq: str):
    it = iter(seq)
    return lambda _t: next(it, None)


def _key(seq: str) -> str:
    return plat._decode_sequence(seq[0], _src(seq[1:]))


def _entries() -> list[E]:
    long_out = "\n".join(f"out line {i}" for i in range(1, 41)) + "\nNEEDLE-in-output"
    return [
        E("turn", 1, title='Turn 1 · "fix the test"', summary="Done in 6.4s · 2 LLM calls", status="ok", duration=6.4),
        E("step", 1, 1, title="Step 1 · gpt-4o", summary="replied in 2.5s · 1.2k tokens · $0.01", status="ok", duration=2.5),
        E("thinking", 1, 1, title="Thought for 1.2s", summary="The test fails because", body="The test fails because\nREASONING-TAIL",
          duration=1.2),
        E("tool", 1, 1, title="Bash  pytest -q", summary="3 passed", body=long_out, detail='{\n  "command": "pytest -q"\n}',
          status="ok", duration=0.42, output_no=3),
        E("tool", 1, 1, title="Bash  sleep 10", detail='{"command": "sleep 10"}', status="running", started_at=NOW - 3.3),
        E("step", 1, 2, title="Step 2 · gpt-4o", summary="replied in 1.0s", status="ok", duration=1.0),
        E("text", 1, 2, title="Response", body="Line one\n\nLine two\n\nLine three\n\nLine four FINAL", summary="Line one"),
        E("note", 1, 2, title="Retry", status="error", summary="rate limited · retry 1 of 3"),
        E("turn", 2, title='Turn 2 · "again"', summary="Done in 1.0s", status="ok", duration=1.0),
        E("text", 2, 0, title="Response", body="second answer", summary="second answer"),
    ]


def _view(entries=None, width=100, height=60, **kw) -> TranscriptViewer:
    return TranscriptViewer(entries if entries is not None else _entries(), width=width, height=height,
                            clock=lambda: NOW, **kw)


def _screen(v: TranscriptViewer) -> str:
    return "\n".join(strip_ansi(ln) for ln in v.frame())


# ─── hierarchy / state ─────────────────────────────────────────────────────

def test_build_nodes_nests_steps_under_turns_and_children_under_steps() -> None:
    es = _entries()
    nodes = build_nodes(es)
    depth = {n.entry.title: n.depth for n in nodes}
    assert depth['Turn 1 · "fix the test"'] == 0
    assert depth["Step 1 · gpt-4o"] == 1
    assert depth["Bash  pytest -q"] == 2
    assert nodes[3].parents == (id(es[0]), id(es[1]))
    # a response without a step (turn 2) hangs directly under its turn
    assert nodes[-1].depth == 1 and nodes[-1].parents == (id(es[8]),)


def test_toggle_expands_and_collapses_a_tool_row() -> None:
    es = _entries()
    v = _view(es)
    assert "out line 7" not in _screen(v)
    v.sel = id(es[3])
    v.toggle()
    scr = _screen(v)
    assert '"command": "pytest -q"' in scr and "out line 7" in scr and "Output" in scr
    v.toggle()
    assert "out line 7" not in _screen(v)


def test_enter_and_space_toggle_the_selected_row() -> None:
    es = _entries()
    v = _view(es)
    v.sel = id(es[2])
    v.handle_key("enter")
    assert "REASONING-TAIL" in _screen(v)
    v.handle_key(" ")
    assert "REASONING-TAIL" not in _screen(v)


def test_expand_all_collapse_all_and_turn_folding() -> None:
    es = _entries()
    v = _view(es, height=200)
    v.handle_key("e")
    scr = _screen(v)
    assert "REASONING-TAIL" in scr and "out line 40" in scr and "Line four FINAL" in scr
    v.handle_key("c")
    scr = _screen(v)
    assert "REASONING-TAIL" not in scr and "out line 40" not in scr
    v.handle_key("t")
    scr = _screen(v)
    assert "Turn 1" in scr and "Turn 2" in scr and "Step 1" not in scr and "hidden" in scr
    v.handle_key("t")
    assert "Step 1" in _screen(v)


def test_folding_a_step_hides_its_children() -> None:
    es = _entries()
    v = _view(es)
    v.toggle(id(es[1]))
    scr = _screen(v)
    assert "pytest -q" not in scr and "Step 2" in scr


def test_response_is_collapsed_to_a_preview_with_more_hint() -> None:
    es = _entries()
    v = _view(es)
    scr = _screen(v)
    assert "Line one" in scr and "Line four FINAL" not in scr and "more line" in scr
    v.toggle(id(es[6]))
    assert "Line four FINAL" in _screen(v)


def test_navigation_moves_selection_and_q_closes() -> None:
    es = _entries()
    v = _view(es)
    assert v.sel == id(es[-1])  # opens at the end, like the live scrollback
    v.handle_key("up")
    assert v.sel == id(es[-2])
    v.handle_key("g")
    assert v.sel == id(es[0]) and v.top == 0
    v.handle_key("j")
    assert v.sel == id(es[1])
    v.handle_key("G")
    assert v.sel == id(es[-1])
    assert v.handle_key("q") is False
    assert _view(es).handle_key("esc") is False
    assert _view(es).handle_key("ctrl-c") is False


def test_selection_scrolls_into_view_on_a_short_screen() -> None:
    es = _entries()
    v = _view(es, height=8)
    v.handle_key("e")
    v.handle_key("g")
    for _ in range(4):
        v.handle_key("down")
    rows = v.rows()
    i = v.header_index(v.sel, rows)
    assert v.top <= i < v.top + v.body_height


def test_entries_updated_in_place_are_picked_up_on_refresh() -> None:
    es = _entries()
    live = list(es)
    v = TranscriptViewer(lambda: live, width=100, height=60, clock=lambda: NOW)
    running = es[4]
    running.status, running.body, running.summary, running.duration = "ok", "slept", "slept", 10.0
    live.append(E("text", 2, 0, title="Response", body="NEW ENTRY", summary="NEW ENTRY"))
    assert v.refresh() is True
    assert "NEW ENTRY" in _screen(v)
    assert v.refresh() is False  # nothing changed, nothing running


# ─── live elapsed time ─────────────────────────────────────────────────────

def test_running_entries_tick_their_elapsed_time() -> None:
    e = E("tool", 1, 1, title="Bash  sleep 10", status="running", started_at=NOW - 3.3)
    assert elapsed_text(e, NOW) == "3.3s"
    assert elapsed_text(e, NOW + 2) == "5.3s"
    clock = [NOW]
    v = TranscriptViewer([E("turn", 1, title="Turn 1", status="running", started_at=NOW - 1), e], width=90, height=20,
                         clock=lambda: clock[0])
    assert v.any_running()
    assert "3.3s" in _screen(v)
    clock[0] += 61
    assert "1m 04s" in _screen(v)
    assert v.refresh() is True  # running: redraw on every tick


def test_finished_entries_show_their_duration() -> None:
    assert elapsed_text(E("tool", 1, duration=0.42), NOW) == "0.42s"
    assert fmt_secs(None) == "" and fmt_secs(75) == "1m 15s" and fmt_secs(4000).startswith("1h")


# ─── layout ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", [24, 40, 80, 160])
def test_every_row_fits_the_width(width: int) -> None:
    v = _view(width=width, height=200)
    v.handle_key("e")
    for ln in v.frame():
        assert display_width(ln) <= width - 1, (width, strip_ansi(ln))


def test_ascii_mode_has_no_unicode_box_glyphs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_ASCII", "1")
    v = _view(height=200)
    v.handle_key("e")
    scr = _screen(v)
    for ch in "▸▾●⎿✓✗◆✻▎❯│┊":
        assert ch not in scr, ch
    assert "pytest -q" in scr and "out line 40" in scr


def test_tool_rows_show_purpose_status_and_line_count() -> None:
    scr = _screen(_view())
    line = next(ln for ln in scr.splitlines() if "pytest -q" in ln)
    assert "runs a shell command" in line and "0.42s" in line
    assert "3 passed" in scr and "41 lines" in scr


def test_footer_help_and_title_bar() -> None:
    scr = _screen(_view()).splitlines()
    assert "Transcript" in scr[0] and "2 turns" in scr[0] and "2 tool calls" in scr[0]
    assert scr[-1].strip() == strip_ansi(footer_help())
    assert "click/enter expand" in footer_help() and "q close" in footer_help()


def test_plain_dump_lists_collapsed_rows() -> None:
    rows = [strip_ansi(r) for r in plain_dump(_entries(), width=100)]
    text = "\n".join(rows)
    assert "Turn 1" in text and "pytest -q" in text and "out line 7" not in text


def test_empty_transcript_frame() -> None:
    scr = _screen(TranscriptViewer([], width=80, height=10))
    assert "Nothing in the transcript yet" in scr


# ─── search / copy ─────────────────────────────────────────────────────────

def test_incremental_search_jumps_and_auto_expands_body_matches() -> None:
    es = _entries()
    v = _view(es, height=12)
    v.handle_key("/")
    for ch in "needle":
        v.handle_key(ch)
    assert v.sel == id(es[3]) and id(es[3]) in v.expanded
    assert "NEEDLE-in-output" in _screen(v)  # scrolled to the matching output line
    v.handle_key("enter")
    assert not v.searching
    v.handle_key("/")
    for ch in "second":
        v.handle_key(ch)
    v.handle_key("enter")
    assert v.sel == id(es[9]) and id(es[3]) not in v.expanded  # auto-expansion undone
    v.query = "Line"
    v.handle_key("n")
    first = v.sel
    v.handle_key("N")
    assert v.sel != first or len(v.matches()) == 1


def test_search_without_match_reports_it() -> None:
    v = _view()
    v.handle_key("/")
    for ch in "zzzz":
        v.handle_key(ch)
    assert "no match" in _screen(v)
    v.handle_key("esc")
    assert not v.searching and v.handle_key("q") is False


def test_search_unfolds_the_matching_entry() -> None:
    es = _entries()
    v = _view(es)
    v.handle_key("t")
    v.handle_key("/")
    for ch in "sleep":
        v.handle_key(ch)
    assert v.sel == id(es[4]) and "sleep 10" in _screen(v)


def test_y_copies_the_selected_body() -> None:
    copied: list[str] = []
    es = _entries()
    v = _view(es, copier=lambda t: copied.append(t) or True)
    v.sel = id(es[3])
    v.handle_key("y")
    assert copied == [es[3].body] and "Copied" in _screen(v)
    v2 = _view(es, copier=lambda t: False)
    v2.sel = id(es[3])
    v2.handle_key("y")
    assert "No clipboard" in _screen(v2)


# ─── mouse ─────────────────────────────────────────────────────────────────

def test_sgr_mouse_sequences_decode_to_events() -> None:
    assert _key("\x1b[<0;10;5M") == "mouse:0:10:5:M"
    ev = plat.parse_mouse(_key("\x1b[<0;10;5M"))
    assert ev is not None and (ev.kind, ev.button, ev.x, ev.y) == ("press", 0, 10, 5)
    assert plat.parse_mouse(_key("\x1b[<0;10;5m")).kind == "release"
    assert plat.parse_mouse(_key("\x1b[<64;1;1M")).kind == "wheel-up"
    assert plat.parse_mouse(_key("\x1b[<65;1;1M")).kind == "wheel-down"
    assert plat.parse_mouse(_key("\x1b[<32;3;3M")).kind == "drag"
    ctrl_click = plat.parse_mouse(_key("\x1b[<16;3;3M"))
    assert ctrl_click.ctrl and ctrl_click.kind == "press"
    assert plat.parse_mouse("up") is None and plat.parse_mouse("mouse:x") is None


def test_mouse_parsing_does_not_break_other_keys() -> None:
    assert _key("\x1b[A") == "up" and _key("\x1b[5~") == "pageup" and _key("\x1b[1;5C") == "ctrl-right"
    assert _key("\x1b[<1;2M") == ""  # malformed report is swallowed, not typed
    assert _key("\x1b[200~hi\x1b[201~") == "paste:hi"


def test_click_on_a_row_toggles_it_and_wheel_scrolls() -> None:
    es = _entries()
    v = _view(es, height=12)
    v.handle_key("g")
    rows = v.rows()
    idx = next(i for i, r in enumerate(rows) if r.key == id(es[3]) and r.header)
    y = idx - v.top + 2  # row 1 is the title bar
    v.handle_key(f"mouse:0:5:{y}:M")
    assert id(es[3]) in v.expanded and v.sel == id(es[3])
    v.handle_key(f"mouse:0:5:{v.header_index(id(es[3])) - v.top + 2}:M")
    assert id(es[3]) not in v.expanded
    v.handle_key("e")
    top = v.top
    v.handle_key("mouse:65:5:5:M")
    assert v.top == top + 3
    v.handle_key("mouse:64:5:5:M")
    assert v.top == top
    v.handle_key("mouse:0:5:1:M")  # title bar: ignored
    v.handle_key("mouse:0:5:5:m")  # release: ignored
    assert v.handle_key("q") is False


# ─── conversation fallback ─────────────────────────────────────────────────

def test_entries_from_anthropic_messages() -> None:
    msgs = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "I should run ls"},
                                          {"type": "text", "text": "Listing."},
                                          {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt\nb.txt"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "nope"}],
                                      "is_error": True}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Two files."}]},
        {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
    ]
    es = entries_from_messages(msgs)
    kinds = [e.kind for e in es]
    assert kinds == ["turn", "thinking", "text", "tool", "tool", "text", "turn"]
    assert es[0].title == 'Turn 1 · "list files"' and es[0].status == "error"
    assert "2 tool calls" in es[0].summary
    bash = es[3]
    assert bash.title.startswith("Bash") and "ls" in bash.title and bash.body == "a.txt\nb.txt"
    assert bash.summary == "a.txt" and '"command": "ls"' in bash.detail
    assert es[4].status == "error" and es[4].body == "nope"
    assert es[-1].title == 'Turn 2 · "thanks"'
    v = TranscriptViewer(es, width=100, height=40, clock=lambda: NOW)
    v.toggle(id(bash))
    assert "b.txt" in _screen(v)


def test_entries_from_openai_messages() -> None:
    msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "echo hi"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    es = entries_from_messages(msgs)
    assert [e.kind for e in es] == ["turn", "tool", "text"]
    assert es[1].body == "hi" and '"command": "echo hi"' in es[1].detail and "echo hi" in es[1].title


def test_transcript_source_falls_back_to_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import axon.ui.render as render
    from axon.ui.transcript_view import transcript_source
    render.clear_transcript()
    agent = SimpleNamespace(conversation=SimpleNamespace(messages=[{"role": "user", "content": "hello"},
                                                                   {"role": "assistant", "content": "hi there"}]),
                            tools=None)
    source, live = transcript_source(agent)
    assert not live and [e.kind for e in source] == ["turn", "text"]
    render._record(E("turn", 1, title="Turn 1"))
    try:
        source, live = transcript_source(agent)
        assert live and callable(source) and source()[0].title == "Turn 1"
    finally:
        render.clear_transcript()


# ─── wiring ────────────────────────────────────────────────────────────────

def test_ctrl_f_is_bound_to_the_transcript() -> None:
    from axon.ui import input as inp
    from axon.ui.keybindings import ACTIONS
    assert ACTIONS["app:transcript"][1] == ("ctrl-f",)
    assert ACTIONS["app:traceCycle"][1] == ("ctrl-o",)  # unchanged
    assert inp._ACTION_COMMANDS["app:transcript"] == "/transcript"
    owners = [a for a, (_d, keys) in ACTIONS.items() if "ctrl-f" in keys]
    assert owners == ["app:transcript"]


def test_transcript_command_is_registered_and_prints_when_not_a_tty(capsys: pytest.CaptureFixture) -> None:
    import axon.ui.render as render
    from axon.commands.registry import resolve_builtin
    cmd = resolve_builtin("transcript")
    assert cmd is not None and resolve_builtin("tr") is cmd
    render.clear_transcript()
    try:
        for e in _entries()[:4]:
            render._record(e)
        cmd.handler(None, "")
        out = strip_ansi(capsys.readouterr().out)
        assert "Turn 1" in out and "pytest -q" in out
    finally:
        render.clear_transcript()


def test_clear_command_clears_the_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.commands.session_cmds as sc
    import axon.ui.render as render
    render._record(E("turn", 1, title="Turn 1"))
    sc._clear_transcript()
    assert render.get_transcript() == []
