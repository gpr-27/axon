"""
Tests for the prompt editor, shared selector, in-flight listener, approval prompt and pickers.
Keyboard input is scripted through axon.platform.read_key, so these exercise exactly the code
paths used on Windows consoles as well as POSIX terminals.
"""
from __future__ import annotations

import contextlib
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.ui import input as ui_input
from axon.ui.input import EditorState, layout_rows, read_input
from axon.ui.picker import SelectItem, Selector, pick


@pytest.fixture
def keys(monkeypatch):
    """Script keyboard input: keys(["a", "enter"])."""
    def install(seq: list[str]) -> None:
        q = deque(seq)

        def fake_read_key(timeout=None, report_resize=True):
            if not q:
                raise AssertionError("ran out of scripted keys")
            return q.popleft()
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    return install


@pytest.fixture(autouse=True)
def _isolate_history(monkeypatch):
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])


# ── read_input end to end ──────────────────────────────────────────────────

def test_type_and_submit(keys) -> None:
    keys(list("hello") + ["enter"])
    assert read_input() == ("hello", None, 0)


def test_multiline_shift_enter_backslash_and_paste(keys) -> None:
    keys(["a", "shift-enter", "b", "\\", "enter", "c", plat.PASTE_PREFIX + "\nd", "enter"])
    assert read_input()[0] == "a\nb\nc\nd"


def test_shift_tab_cycles_mode(keys) -> None:
    keys(["shift-tab", "shift-tab", "x", "enter"])
    assert read_input(mode="default") == ("x", "plan", 0)


def test_ctrl_c_clears_then_interrupts(keys) -> None:
    keys(list("draft") + ["ctrl-c", "ctrl-c"])
    with pytest.raises(KeyboardInterrupt):
        read_input()


def test_up_restores_cleared_draft(keys) -> None:
    keys(list("draft") + ["ctrl-c", "up", "enter"])
    assert read_input()[0] == "draft"


def test_ctrl_d_twice_exits(keys) -> None:
    keys(["ctrl-d", "ctrl-d"])
    with pytest.raises(EOFError):
        read_input()


def test_esc_esc_on_empty_prompt_requests_rewind(keys) -> None:
    keys(["esc", "esc"])
    assert read_input()[0] == ui_input.REWIND


def test_left_on_empty_opens_switcher_and_ctrl_o_cycles_trace(keys) -> None:
    keys(["left"])
    assert read_input()[0] == ui_input.SWITCH_SESSION
    keys(["ctrl-o"])
    assert read_input()[0] == ui_input.TRACE_CYCLE


def test_slash_menu_enter_runs_selected_command(keys) -> None:
    keys(list("/cos") + ["enter"])
    assert read_input()[0] == "/cost"


def test_slash_menu_tab_fills_and_keeps_editing(keys) -> None:
    keys(list("/mod") + ["tab"] + list("gpt") + ["enter"])
    assert read_input()[0] == "/model gpt"


def test_file_completion(keys, tmp_path, monkeypatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main_module.py").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui_input, "match_files", lambda q, cwd=None, limit=50: ["src/main_module.py"])
    keys(list("see @mai") + ["tab"] + list("now") + ["enter"])
    assert read_input()[0] == "see @src/main_module.py now"


def test_history_is_persisted_and_recalled(keys) -> None:
    keys(list("first prompt") + ["enter"])
    read_input()
    ui_input._SESSION_HISTORY.clear()
    assert "first prompt" in ui_input.load_history()
    keys(["up", "enter"])
    assert read_input()[0] == "first prompt"


def test_ctrl_r_reverse_search(keys) -> None:
    for text in ("deploy staging", "run tests", "deploy prod"):
        ui_input.add_history(text)
    keys(["ctrl-r"] + list("dep") + ["ctrl-r", "enter"])
    assert read_input()[0] == "deploy staging"


def test_large_paste_collapses_to_chip_but_submits_full_text(keys) -> None:
    big = "\n".join(f"line {i}" for i in range(40))
    keys([plat.PASTE_PREFIX + big, "enter"])
    assert read_input()[0] == big


def test_question_mark_on_empty_shows_shortcuts(keys) -> None:
    keys(["?"])
    assert read_input()[0] == "/kb"


def test_plain_fallback_when_not_a_tty(monkeypatch) -> None:
    monkeypatch.setattr(plat, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "  piped text  ")
    assert read_input() == ("piped text", None, 0)


# ── editor state units ─────────────────────────────────────────────────────

def test_layout_rows_wraps_wide_chars() -> None:
    rows, r, c = layout_rows("世界世界世", 6, 5)
    assert rows == ["世界世", "界世"]
    assert (r, c) == (1, 4)


def test_kill_and_yank_ring() -> None:
    st = EditorState(buf="one two three", pos=13)
    st.kill(st.word_left(), st.pos)
    assert st.buf == "one two "
    st.yank()
    assert st.buf == "one two three"


def test_vertical_movement_in_multiline_buffer() -> None:
    st = EditorState(buf="abc\ndefgh", pos=8)
    assert st.move_vertical(-1) and st.pos == 3
    assert not st.move_vertical(-1)


def test_history_prefix_filter() -> None:
    st = EditorState(history=["git status", "ls", "git push"], buf="git", pos=3)
    assert st.history_step(-1) and st.buf == "git push"
    assert st.history_step(-1) and st.buf == "git status"
    assert st.history_step(1) and st.buf == "git push"


# ── selector / pickers ─────────────────────────────────────────────────────

def test_selector_navigation_and_filter(keys) -> None:
    items = [SelectItem(n, value=n) for n in ("alpha", "beta", "gamma")]
    keys(["down", "enter"])
    assert Selector(items).run().value == "beta"
    keys(list("gam") + ["enter"])
    assert Selector(items, filterable=True).run().value == "gamma"
    keys(["esc"])
    assert Selector(items).run() is None


def test_selector_tabs(keys) -> None:
    data = {0: ["a1", "a2"], 1: ["b1"]}
    keys(["tab", "enter"])
    sel = Selector(tabs=["A", "B"], items_for_tab=lambda i: [SelectItem(x, value=x) for x in data[i]])
    assert sel.run().value == "b1"


def test_pick_interactive_and_digit_shortcut(keys) -> None:
    keys(["2"])
    assert pick(["x", "y", "z"]) == "y"


def test_switcher_on_key_new_chat_with_prompt(keys, monkeypatch) -> None:
    import axon.ui.switcher as sw
    session = SimpleNamespace(active_session_id="s1", active_file=Path("s1.jsonl"))
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=Path(".")), session=session,
                            conversation=SimpleNamespace(messages=[]), ledger=SimpleNamespace(total=lambda: 0.0))
    monkeypatch.setattr(sw, "load_dashboard_sessions", lambda ws, active: [])
    keys(["n"] + list("fix bug") + ["enter"])
    assert sw.run_session_dashboard(agent) == "__NEW_SESSION__:fix bug"


# ── approvals and in-flight listener ───────────────────────────────────────

def test_approval_denied_without_tty(monkeypatch) -> None:
    from axon.ui.approve import ask_approval
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert ask_approval("Bash", {"command": "rm -rf build"}) == "deny"


def test_approval_menu(keys, monkeypatch) -> None:
    from axon.ui.approve import ask_approval
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    keys(["down", "enter"])
    assert ask_approval("Bash", {"command": "ls", "description": "list"}) == "always"


def test_in_flight_keys_queue_prompt() -> None:
    from axon.agent.state import MessageQueue
    from axon.ui.in_flight import InFlightInputListener
    agent = SimpleNamespace(message_queue=MessageQueue())
    listener = InFlightInputListener(agent)
    for k in list("next tsk") + ["left", "a", "backspace", "end", "enter"]:
        listener.handle_key(k)
    assert [it.text for it in agent.message_queue.items] == ["next tsk"]


def test_in_flight_esc_interrupts_turn() -> None:
    from axon.ui.in_flight import InFlightInputListener
    called = []
    agent = SimpleNamespace(cancel_current_turn=lambda: called.append(True))
    listener = InFlightInputListener(agent)
    listener.handle_key("x")
    listener.handle_key("esc")       # clears the draft only
    assert called == []
    listener.handle_key("esc")       # empty draft: interrupt
    assert called == [True]


def test_paused_stops_and_resumes_listener(monkeypatch) -> None:
    import threading
    from axon.ui import in_flight
    states = []

    @contextlib.contextmanager
    def fake_raw(*a, **k):
        states.append("raw")
        try:
            yield True
        finally:
            states.append("cooked")
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "raw_terminal", fake_raw)
    monkeypatch.setattr(plat, "read_key", lambda timeout=None, report_resize=True: (threading.Event().wait(0.01), None)[1])
    listener = in_flight.InFlightInputListener(SimpleNamespace())
    with listener:
        for _ in range(100):
            if states:
                break
            threading.Event().wait(0.01)
        with in_flight.paused():
            assert states[-1] == "cooked"
            assert listener.is_paused
        for _ in range(100):
            if states.count("raw") >= 2:
                break
            threading.Event().wait(0.01)
        assert states.count("raw") >= 2
    assert not listener._thread
