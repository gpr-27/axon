"""Regressions from the UI / REPL review: subagent monitor keys, unicode digits, resize draft, print-mode
JSON errors, queued-prompt wake-up, shell interrupt output, paste chips."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from collections import deque
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.agent.state import MessageQueue
from axon.ui import input as ui_input
from axon.ui import subagent_monitor as mon
from axon.ui.input import EditorState


def _task(i: int, status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(index=i, title=f"Task {i}", status=status, steps=1, max_steps=10, start_time=0.0,
                           elapsed_s=1.0, prompt="p", timeline=[], live_logs=[], activity="", agent_type="",
                           model="", tokens_consumed=0, tool_calls=0, retries=0, resumes=0)


# ── subagent monitor ────────────────────────────────────────────────────────

def test_monitor_enter_on_empty_input_does_not_crash() -> None:
    keys = mon.MonitorKeys(SimpleNamespace(message_queue=MessageQueue()))
    assert keys.handle("enter", [_task(1)]) is None          # was IndexError on "".split()[0]
    keys.buf = "   "
    assert keys.handle("enter", [_task(1)]) is None and keys.buf == ""


def test_monitor_superscript_digit_is_typed_not_parsed() -> None:
    keys = mon.MonitorKeys(None)
    keys.handle("²", [_task(1)])                              # "²".isdigit() is True; int("²") raised
    assert keys.view == 0 and keys.buf == "²"
    keys.buf = ""
    keys.handle("1", [_task(1)])
    assert keys.view == 1


def test_monitor_note_queued_notice_clears_on_next_edit_and_commands_are_returned() -> None:
    agent = SimpleNamespace(message_queue=MessageQueue())
    keys = mon.MonitorKeys(agent)
    for k in "hello":
        keys.handle(k, [])
    keys.handle("enter", [])
    assert len(agent.message_queue) == 1 and keys.queued and keys.notice.startswith("✓ Note")
    keys.handle("x", [])
    assert keys.notice == "" and keys.buf == "x"
    keys.buf = "/btw why"
    assert keys.handle("enter", []) == "command" and keys.command == "/btw why"


def test_monitor_paste_flattens_cr_and_tabs() -> None:
    keys = mon.MonitorKeys(None)
    keys.handle(plat.PASTE_PREFIX + "a\r\nb\tc\rd", [])
    assert keys.buf == "a b c d"


def test_monitor_long_wide_input_keeps_the_typing_end_visible() -> None:
    buf = "start " + "界" * 60 + " END"
    lines = mon.build_monitor_frame(0, [_task(1)], None, buf, 40)
    last = plat.strip_ansi(lines[-1])
    assert last.rstrip().endswith("END") and plat.str_width(lines[-1]) <= 40
    assert mon.input_cursor_col("", 40) == 4
    assert mon.input_cursor_col(buf, 40) == plat.str_width(last.rstrip())


def test_monitor_detail_error_without_error_msg_attribute() -> None:
    t = _task(1, "error")
    text = "\n".join(plat.strip_ansi(ln) for ln in mon.build_monitor_frame(1, [t], None, "", 80))
    assert "failed (no reason reported)" in text


def test_monitor_prints_queued_notes_even_after_ctrl_c(monkeypatch, capsys) -> None:
    agent = SimpleNamespace(message_queue=MessageQueue())
    script = deque(["n", "o", "enter"])

    def read_key(timeout=None, report_resize=True):
        if script:
            return script.popleft()
        raise KeyboardInterrupt

    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(plat, "read_key", read_key)
    monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
    class _Fut:
        def done(self):
            return False
    fut = _Fut()
    mgr = SimpleNamespace(all_tasks=lambda: [_task(1)])
    with pytest.raises(KeyboardInterrupt):
        mon.run_live_subagent_monitor({fut: 0}, mgr, agent)
    assert "1 note for the main agent" in plat.strip_ansi(capsys.readouterr().out)


# ── pickers / vim: unicode digits ───────────────────────────────────────────

def test_selector_and_vim_ignore_non_ascii_digits() -> None:
    from axon.ui.picker import SelectItem, Selector
    from axon.ui.vim import VimState
    sel = Selector([SelectItem("a", 1), SelectItem("b", 2)])
    sel._default_key("³")                                     # was ValueError from int("³")
    assert not sel._done
    sel._default_key("2")
    assert sel._done and sel._result.value == 2
    st = EditorState()
    st.set_text("abc", 0)
    vim = VimState(mode="normal")
    assert vim.handle(st, "²") is True and vim.count == ""


# ── prompt editor ───────────────────────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "_RESIZE_DRAFT", None)

    def install(seq):
        q = deque(seq)
        monkeypatch.setattr(plat, "read_key", lambda timeout=None, report_resize=True: q.popleft() if q else None)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
    return install


def test_resize_redraw_keeps_paste_chip_content_and_cursor(keys, monkeypatch, tmp_path) -> None:
    big = "\n".join(f"line {i}" for i in range(40))
    widths = iter([100, 80, 80, 80])
    monkeypatch.setattr(ui_input, "term_width", lambda: next(widths, 80))
    keys([plat.PASTE_PREFIX + big, "left", "resize", None])
    assert ui_input.read_input(cwd=str(tmp_path))[0] == ui_input.RESIZE_REDRAW
    keys(["enter"])
    text = ui_input.read_input(cwd=str(tmp_path))[0]
    assert text == big                                        # the chip still expands after the redraw


def test_delete_removes_a_whole_paste_chip() -> None:
    st = EditorState()
    st.insert_paste("x" * 2000)
    st.insert(" tail")
    st.pos = 0
    st.delete()
    assert st.buf == " tail"


# ── cli ─────────────────────────────────────────────────────────────────────

def test_print_mode_empty_prompt_and_interrupt_emit_json(monkeypatch, capsys) -> None:
    from axon import cli
    assert cli.run_print_mode(SimpleNamespace(), "  ", "json") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["is_error"] and out["subtype"] == "no_prompt"

    def boom(prompt):
        raise KeyboardInterrupt
    agent = SimpleNamespace(run_turn=boom, settings=SimpleNamespace(workspace="."))
    monkeypatch.setattr(cli, "_journal", lambda *a, **k: None)
    assert cli.run_print_mode(agent, "hi", "stream-json") == 130
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["subtype"] == "interrupted"


def test_resume_without_replay_in_print_mode(monkeypatch) -> None:
    from axon import cli
    called = []
    monkeypatch.setattr("axon.session.interactive.render_restored_conversation",
                        lambda *a, **k: called.append(1), raising=False)
    monkeypatch.setattr(cli, "session_state", lambda agent, sid: {"turns": 2, "last_prompt": "p", "last_answer": "a"})
    monkeypatch.setattr("axon.session.store.restore_retry_state", lambda agent, sid: None)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    conv = SimpleNamespace(messages=[{"role": "user", "content": "p"}], repair=lambda: None)
    ledger = SimpleNamespace(total_input_tokens=0, total_output_tokens=0, total=lambda: 0.0)
    session = SimpleNamespace(open=lambda sid: sid, load=lambda sid: conv, load_ledger=lambda sid, m: ledger)
    agent = SimpleNamespace(session=session, settings=SimpleNamespace(model="m", mode="default"), conversation=None)
    said: list[str] = []
    assert cli._resume(agent, "s1", said.append, replay=False)
    assert not called and any("Recap" in s for s in said)


def test_repl_wake_runs_queue_with_banner(monkeypatch, capsys) -> None:
    from axon import cli
    q = MessageQueue()
    q.push("queued question")
    agent = SimpleNamespace(message_queue=q, busy=False)
    ran: list[str] = []
    monkeypatch.setattr(cli, "process_line", lambda a, r, line, from_queue=False: ran.append(line) or True)
    assert cli.drain_queue(agent, None)
    assert ran == ["queued question"] and "[Queued #" in plat.strip_ansi(capsys.readouterr().out)


def test_shell_interrupt_keeps_partial_output(monkeypatch) -> None:
    from axon import cli

    class _Out:
        def __init__(self):
            self.n = 0

        def read1(self, _n):
            self.n += 1
            if self.n == 1:
                return b"partial output"
            raise KeyboardInterrupt

    class _Proc:
        stdout = _Out()

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr("axon.platform.kill_process_tree", lambda p, grace=1.0: None)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    sink: list[str] = []
    assert cli.run_shell_shortcut("echo hi", sink=sink) == 130
    assert sink == ["partial output\n[interrupted by user]"]
