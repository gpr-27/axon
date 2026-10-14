"""
Agent-D e2e: the interactive REPL driven through a real pseudo-terminal (POSIX only).

Every test boots the real ``python -m axon`` against the scripted fake LLM server, types keys
into a pty exactly as a terminal would, and asserts on what the user sees.
"""
from __future__ import annotations

import contextlib
import os
import re
import signal
import struct
import time

import pytest

from harness import AxonEnv
from test_e2e_d_helpers import PROMPT_READY, SGR_RE, TRACEBACK, open_repl, posix_only, tool_turn

pytestmark = [pytest.mark.e2e, posix_only]

UP, DOWN, SHIFT_TAB, ESC = "\x1b[A", "\x1b[B", "\x1b[Z", "\x1b"
CTRL_C, CTRL_D, CTRL_O, CTRL_R, CTRL_U, CTRL_J, CTRL_L = "\x03", "\x04", "\x0f", "\x12", "\x15", "\x0a", "\x0c"
ALT_ENTER = "\x1b\r"


@pytest.fixture(scope="module")
def idle(tmp_path_factory: pytest.TempPathFactory):
    """One long-lived REPL (bypass mode, no model turns) shared by the editor-only tests below.

    Each test runs inside ``_shared`` which clears the draft before and after, so tests stay
    independent; tests that change a mode cycle it back to where it started."""
    from fake_llm import FakeLLMServer
    from harness import create_axon_env
    server = FakeLLMServer(model_ids=("gpt-4o",)).start()
    env = create_axon_env(tmp_path_factory.mktemp("idle"), server)
    (env.workspace / "alpha.txt").write_text("a", encoding="utf-8")
    (env.workspace / "zeta.txt").write_text("z", encoding="utf-8")
    (env.workspace / "srcdir_xyz").mkdir()
    (env.workspace / "srcdir_xyz" / "mod.py").write_text("x=1\n", encoding="utf-8")
    s = open_repl(env)
    s.env = env
    try:
        yield s
    finally:
        s.close()
        server.stop()


@contextlib.contextmanager
def _shared(s):
    """Reset the shared REPL's editor to an empty idle prompt around a test."""
    def reset() -> None:
        if s.proc.poll() is not None:
            pytest.fail("shared REPL exited:\n" + s.screen[-1500:])
        m = s.mark()
        s.send("x")  # guarantees a redraw, so the placeholder reappears after Ctrl+U
        time.sleep(0.05)
        s.send(CTRL_U)
        try:
            s.expect(PROMPT_READY, timeout=1.5, since=m)
        except TimeoutError:
            s.send(ESC)
            time.sleep(0.15)
            s.send(CTRL_U)
            s.expect(PROMPT_READY, timeout=3, since=m)
        s.drain(0.2)
    reset()
    yield s
    reset()


# ─── banner / status line ──────────────────────────────────────────────────

def test_banner_shows_version_model_workspace_and_hints(idle) -> None:
    """The startup banner shows the Axon version, active model, workspace path and the key hints."""
    import axon
    with _shared(idle) as s:
        scr = s.screen
        assert f"Axon v{axon.__version__}" in scr
        assert "gpt-4o" in scr
        assert idle.env.workspace.name in scr
        assert "/help commands" in scr and "! shell" in scr and "@ files" in scr
        assert TRACEBACK not in scr


def test_banner_shows_permission_mode_and_trace_level(axon_env: AxonEnv) -> None:
    """The banner reports the permission mode and the Ctrl+O trace hint."""
    with open_repl(axon_env, ("--mode", "plan")) as s:
        assert "plan mode" in s.screen
        assert "trace normal (ctrl+o)" in s.screen


def test_status_line_initial_model_ctx_cost(idle) -> None:
    """Before any turn the status line reads `<model> · ctx 0% · $0.0000 · trace normal`."""
    with _shared(idle) as s:
        assert re.search(r"gpt-4o · ctx 0% · \$0\.0000 · trace normal", s.screen), s.screen[-800:]


def test_status_line_updates_ctx_and_cost_after_turn(axon_env: AxonEnv) -> None:
    """After a turn the status line shows a non-zero context percentage and the session cost."""
    axon_env.llm.enqueue([{"text": "hello there"}, {"usage": {"input": 20000, "output": 50}}])
    with open_repl(axon_env) as s:
        out = s.turn("hi")
        m = re.findall(r"gpt-4o · ctx (\d+)% · \$(\d+\.\d+)", out)
        assert m, out[-1500:]
        ctx, cost = m[-1]
        assert int(ctx) > 0 and float(cost) > 0, m


# ─── the glass-box timeline ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def timeline_out(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One real REPL turn (text + Bash tool + markdown answer) shared by the timeline assertions."""
    from fake_llm import FakeLLMServer
    from harness import create_axon_env
    server = FakeLLMServer(model_ids=("gpt-4o",)).start()
    try:
        env = create_axon_env(tmp_path_factory.mktemp("timeline"), server)
        server.enqueue(*tool_turn("echo hi-from-tool", "FINAL **bold** ANSWER"))
        with open_repl(env, ("--mode", "bypass")) as s:
            return s.turn("run the tool")
    finally:
        server.stop()


def test_timeline_turn_header(timeline_out: str) -> None:
    """The turn opens with `┌ Turn 1 · "<prompt>"` and the model · mode on the right."""
    out = timeline_out
    assert re.search(r'┌ Turn 1 · "run the tool"\s+gpt-4o · bypass', out), out[-2500:]


def test_timeline_step_rows(timeline_out: str) -> None:
    """Each LLM call is a numbered `◆ Step N  LLM call` row with model and message count."""
    out = timeline_out
    assert "◆ Step 1  LLM call · gpt-4o · 1 message" in out
    assert "◆ Step 2  LLM call · gpt-4o · 3 messages" in out


def test_timeline_tool_row_and_result_summary(timeline_out: str) -> None:
    """A tool call renders as `● Bash  <command>` followed by a `⎿` summary with the output."""
    out = timeline_out
    assert "● Bash  echo hi-from-tool" in out
    assert re.search(r"⎿\s+hi-from-tool · sent \d+ B", out), out[-2500:]
    assert "auto-allowed: Bypass mode active." in out


def test_timeline_response_text_rendered_as_markdown(timeline_out: str) -> None:
    """The streamed answer is shown under a Response row with markdown emphasis applied."""
    out = timeline_out
    assert "├─ Response · step 2" in out
    assert "FINAL bold ANSWER" in out and "**bold**" not in out


def test_timeline_done_footer_has_tokens_and_cost(timeline_out: str) -> None:
    """The footer `└ ✓ Done` lists LLM calls, tool calls, tokens in/out and a dollar cost."""
    out = timeline_out
    assert re.search(r"└ ✓ Done · [\d.]+s · 2 LLM calls · 1 tool call · [\d.]+k in · \d+ out · \$\d+\.\d{4}", out), out[-1500:]


def test_timeline_feedback_row_between_steps(timeline_out: str) -> None:
    """Step 2 explains what was fed back to the model from step 1."""
    out = timeline_out
    assert "→ fed back 1 tool result from step 1" in out


# ─── trace levels via /trace ───────────────────────────────────────────────

TRACE_MARKERS = {
    "compact": (["● Bash(echo hi-from-tool)", "✓ Done"], ["┌ Turn", "◆ Step", "request: system", "LLMCallStart"]),
    "normal": (["┌ Turn", "◆ Step 1", "● Bash  echo hi-from-tool", "└ ✓ Done"],
               ["request: system", '"command": "echo hi-from-tool"', "LLMCallStart", "usage: input="]),
    "detailed": (["┌ Turn", "request: system", '"command": "echo hi-from-tool"', "observation #1"],
                 ["LLMCallStart", "usage: input=", "timing: id="]),
    "debug": (["┌ Turn", "request: system", "LLMCallStart", "usage: input=", "timing: id="], []),
}


@pytest.mark.parametrize("level", list(TRACE_MARKERS))
def test_trace_command_sets_level_and_filters_elements(axon_env: AxonEnv, level: str) -> None:
    """`/trace <level>` changes the status line and the next turn shows exactly that level's elements."""
    axon_env.llm.enqueue(*tool_turn())
    with open_repl(axon_env, ("--mode", "bypass")) as s:
        s.command(f"/trace {level}", f"Trace level: {level}")
        s.ready()
        assert f"trace {level}" in s.screen[-400:]
        out = s.turn("go")
        want, avoid = TRACE_MARKERS[level]
        for w in want:
            assert w in out, f"{level}: missing {w!r}\n{out[-3000:]}"
        for a in avoid:
            assert a not in out, f"{level}: unexpected {a!r}\n{out[-3000:]}"


def test_trace_without_argument_lists_levels(idle) -> None:
    """`/trace` alone lists all four levels and marks the current one."""
    with _shared(idle) as s:
        out = s.command("/trace", "debug")
        for lv in ("compact", "normal", "detailed", "debug"):
            assert lv in out
        assert re.search(r"●\s+normal", out), out[-1000:]


def test_trace_invalid_level_shows_usage(idle) -> None:
    """`/trace bogus` prints usage instead of changing the level."""
    with _shared(idle) as s:
        out = s.command("/trace bogus", r"/trace \[compact")
        assert "Trace level: bogus" not in out
        s.ready()
        assert "trace normal" in s.screen[-400:]


def test_ctrl_o_cycles_through_all_trace_levels(idle) -> None:
    """Ctrl+O cycles normal → detailed → debug → compact → normal, announced each time."""
    with _shared(idle) as s:
        for lv in ("detailed", "debug", "compact", "normal"):
            m = s.mark()
            s.send(CTRL_O)
            s.expect(f"trace level: {lv}", timeout=8, since=m)
            s.expect(f"trace {lv}", timeout=8, since=m)
            s.drain(0.3)


def test_ctrl_o_detailed_applies_to_next_turn(axon_env: AxonEnv) -> None:
    """After one Ctrl+O the next turn renders the detailed tool-input JSON but no debug fields."""
    axon_env.llm.enqueue(*tool_turn())
    with open_repl(axon_env, ("--mode", "bypass")) as s:
        m = s.mark()
        s.send(CTRL_O)
        s.expect("trace level: detailed", since=m)
        s.ready(since=m)
        out = s.turn("go")
        assert '"command": "echo hi-from-tool"' in out
        assert "LLMCallStart" not in out


# ─── /inspect ──────────────────────────────────────────────────────────────

def test_inspect_replays_last_turn_with_answer(axon_env: AxonEnv) -> None:
    """`/inspect` replays the last turn: header, steps, full tool input, `├─ Answer` and footer."""
    axon_env.llm.enqueue(*tool_turn("echo inspected-out", "FIN-INSPECT"))
    with open_repl(axon_env, ("--mode", "bypass")) as s:
        s.turn("do it")
        out = s.command("/inspect", r"└ ✓ Done")
        assert '┌ Turn 1 · "do it"' in out
        assert "◆ Step 1" in out and "◆ Step 2" in out
        assert '"command": "echo inspected-out"' in out
        assert "├─ Answer" in out and "FIN-INSPECT" in out
        assert "permission: allow by mode" in out


def test_inspect_before_any_turn_is_graceful(idle) -> None:
    """`/inspect` on a fresh session prints a message instead of crashing."""
    with _shared(idle) as s:
        m = s.submit("/inspect")
        s.ready(since=m)
        out = s.screen[m:]
        assert TRACEBACK not in out
        assert s.proc.poll() is None


# ─── permission modes (Shift+Tab) ──────────────────────────────────────────

def test_shift_tab_cycles_permission_modes(idle) -> None:
    """Shift+Tab cycles bypass → manual → auto-accept edits → plan → bypass on the mode line."""
    with _shared(idle) as s:
        assert "bypass permissions on" in s.screen
        for label in ("manual mode on", "auto-accept edits on", "plan mode on", "bypass permissions on"):
            m = s.mark()
            s.send(SHIFT_TAB)
            s.expect(re.escape(label), timeout=8, since=m)
            s.drain(0.2)


def test_shift_tab_mode_applies_to_next_turn(axon_env: AxonEnv) -> None:
    """After cycling to plan mode the next turn header is tagged `plan`."""
    axon_env.llm.enqueue([{"text": "planning only"}])
    with open_repl(axon_env) as s:
        for label in ("manual mode on", "auto-accept edits on", "plan mode on"):
            m = s.mark()
            s.send(SHIFT_TAB)
            s.expect(re.escape(label), since=m)
        s.drain(0.3)
        out = s.turn("make a plan")
        assert re.search(r'Turn 1 · "make a plan"\s+gpt-4o · plan', out), out[-1500:]


def test_plan_mode_blocks_write_interactively(axon_env: AxonEnv) -> None:
    """In plan mode a Write tool call is refused and the file is not created."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "nope.txt", "content": "x"}}], [{"text": "fine"}])
    with open_repl(axon_env, ("--mode", "plan")) as s:
        s.turn("write it")
    assert not (axon_env.workspace / "nope.txt").exists()


# ─── Esc Esc / rewind ──────────────────────────────────────────────────────

def test_esc_esc_opens_rewind_menu_and_esc_cancels(axon_env: AxonEnv) -> None:
    """On an empty prompt after a turn, Esc Esc opens the Rewind menu listing the prompt; Esc cancels."""
    axon_env.llm.enqueue([{"text": "first"}])
    with open_repl(axon_env) as s:
        s.turn("rewindable prompt")
        m = s.mark()
        s.send(ESC + ESC)
        s.expect(r"◆ Rewind", timeout=8, since=m)
        s.expect(r"#1 rewindable prompt", timeout=5, since=m)
        s.drain(0.3)
        m = s.mark()
        s.send(ESC)
        s.expect("Rewind cancelled", timeout=8, since=m)
        assert s.proc.poll() is None


def test_rewind_restores_conversation(axon_env: AxonEnv) -> None:
    """Choosing a rewind point drops that exchange from the history and puts the prompt back in the editor."""
    axon_env.llm.enqueue([{"text": "A1"}], [{"text": "A2-DROPPED"}])
    with open_repl(axon_env) as s:
        s.turn("keep me")
        s.turn("forget me")
        m = s.mark()
        s.send(ESC + ESC)
        s.expect(r"◆ Rewind", since=m)
        s.expect(r"forget me", since=m)
        s.drain(0.3)
        s.send("\r")
        s.expect(r"Restore conversation", timeout=8, since=m)
        s.drain(0.3)
        s.send("\r")
        s.expect(r"❯ forget me", timeout=8, since=m)  # the rewound prompt is restored for editing
        s.drain(0.3)
        s.send(CTRL_U)
        time.sleep(0.3)
        axon_env.llm.enqueue([{"text": "A3"}])
        s.turn("after rewind")
    last = axon_env.llm.completion_requests()[-1]
    joined = "\n".join(str(x.get("content")) for x in last.messages)
    assert "keep me" in joined and "A1" in joined and "after rewind" in joined
    assert "forget me" not in joined and "A2-DROPPED" not in joined, joined[-800:]


def test_esc_esc_clears_draft(idle) -> None:
    """With text in the prompt, a quick Esc Esc clears the draft."""
    with _shared(idle) as s:
        s.type("throwaway draft")
        m = s.mark()
        s.send(ESC)
        time.sleep(0.15)
        s.send(ESC)
        s.expect(PROMPT_READY, timeout=8, since=m)


def test_single_esc_on_draft_only_hints(idle) -> None:
    """A single Esc on a draft keeps the text and hints that a second Esc clears it."""
    with _shared(idle) as s:
        s.type("keep this draft")
        m = s.mark()
        s.send(ESC)
        s.expect("esc again to clear the draft", timeout=8, since=m)
        assert "keep this draft" in s.screen[m:]


# ─── Ctrl+C / Ctrl+D / exit ────────────────────────────────────────────────

def test_ctrl_c_clears_the_line(idle) -> None:
    """Ctrl+C with a draft clears it and explains how to restore / exit."""
    with _shared(idle) as s:
        s.type("some draft")
        m = s.mark()
        s.send(CTRL_C)
        s.expect("draft cleared", timeout=8, since=m)
        assert s.proc.poll() is None


def test_ctrl_c_cleared_draft_restored_with_up(idle) -> None:
    """After Ctrl+C clears a draft, Up brings it back."""
    with _shared(idle) as s:
        s.type("restore me please")
        s.send(CTRL_C)
        time.sleep(0.4)
        m = s.mark()
        s.send(UP)
        s.expect("❯ restore me please", timeout=8, since=m)


def test_double_ctrl_c_on_empty_prompt_exits(axon_env: AxonEnv) -> None:
    """Ctrl+C on an empty prompt warns once; pressing it again (after the prompt is back) exits with code 0."""
    with open_repl(axon_env) as s:
        m = s.mark()
        s.send(CTRL_C)
        s.expect(r"press Ctrl\+C again|ctrl\+c again", timeout=8, since=m)
        s.ready(since=m)
        time.sleep(0.5)
        s.send(CTRL_C)
        code = s.wait(10)
        assert code == 0, s.screen[-1000:]
        assert "Session closed" in s.screen or "Goodbye" in s.screen


def test_double_ctrl_d_on_empty_prompt_exits(axon_env: AxonEnv) -> None:
    """Ctrl+D on an empty prompt asks for confirmation; a second Ctrl+D exits cleanly."""
    with open_repl(axon_env) as s:
        m = s.mark()
        s.send(CTRL_D)
        s.expect("press ctrl\\+d again to exit", timeout=8, since=m)
        time.sleep(0.2)
        s.send(CTRL_D)
        assert s.wait(10) == 0, s.screen[-1000:]


def test_ctrl_d_with_draft_does_not_exit(idle) -> None:
    """Ctrl+D on a non-empty line is delete-forward, never exit."""
    with _shared(idle) as s:
        s.type("abc")
        s.send(CTRL_D)
        time.sleep(0.3)
        s.send(CTRL_D)
        time.sleep(0.6)
        assert s.proc.poll() is None


def test_exit_command_exits_zero_with_goodbye(axon_env: AxonEnv) -> None:
    """`/exit` ends the session with code 0 and prints the session total."""
    with open_repl(axon_env) as s:
        s.submit("/exit")
        assert s.wait(10) == 0
        assert re.search(r"(Goodbye|Session closed).*Total cost: \$", s.screen), s.screen[-600:]


def test_type_ahead_enter_right_after_turn_submits(axon_env: AxonEnv) -> None:
    """Typing a command + Enter immediately after the Done footer (before the prompt redraw) still submits it."""
    axon_env.llm.enqueue([{"text": "quick"}])
    with open_repl(axon_env) as s:
        m = s.submit("hi")
        s.expect("✓ Done", timeout=15, since=m)
        s.send("/exit")
        time.sleep(0.15)
        s.send("\r")
        assert s.wait(2) == 0, s.screen[-1200:]


def test_type_ahead_typed_slowly_at_turn_end_is_not_lost(axon_env: AxonEnv) -> None:
    """Characters typed one by one as the answer finishes are all kept and Enter submits them."""
    axon_env.llm.enqueue([{"text": "slow start "}, {"delay": 0.8}, {"text": "slow end"}], [{"text": "NEXT-ANS"}])
    with open_repl(axon_env) as s:
        m = s.submit("slow one")
        s.expect("slow start", timeout=10, since=m)  # markdown is flushed when the stream ends
        for ch in "follow up":
            s.send(ch)
            time.sleep(0.03)
        time.sleep(0.3)
        s.send("\r")
        s.expect("NEXT-ANS", timeout=2.5, since=m)
    assert axon_env.llm.completion_requests()[-1].last_user_text() == "follow up"


# ─── history / search / multiline ──────────────────────────────────────────

def test_history_up_down_navigation(axon_env: AxonEnv) -> None:
    """Up recalls older prompts in order, Down moves back to newer ones."""
    axon_env.llm.enqueue([{"text": "1"}], [{"text": "2"}])
    with open_repl(axon_env) as s:
        s.turn("first prompt")
        s.turn("second prompt")
        m = s.mark()
        s.send(UP)
        s.expect("❯ second prompt", since=m)
        m = s.mark()
        s.send(UP)
        s.expect("❯ first prompt", since=m)
        m = s.mark()
        s.send(DOWN)
        s.expect("❯ second prompt", since=m)


def test_ctrl_r_reverse_search_runs_match(axon_env: AxonEnv) -> None:
    """Ctrl+R opens reverse-i-search, typing finds the matching prompt and Enter runs it."""
    axon_env.llm.enqueue([{"text": "1"}], [{"text": "2"}], [{"text": "3"}])
    with open_repl(axon_env) as s:
        s.turn("alpha query")
        s.turn("beta query")
        m = s.mark()
        s.send(CTRL_R)
        s.expect("reverse-i-search", since=m)
        s.send("alp")
        s.expect("❯ alpha query", since=m)
        time.sleep(0.3)
        s.send("\r")
        s.expect("Turn 3", timeout=15, since=m)
        s.expect("✓ Done", timeout=15, since=m)
    assert axon_env.llm.completion_requests()[-1].last_user_text() == "alpha query"


@pytest.mark.parametrize("newline_key,label", [(ALT_ENTER, "alt-enter"), (CTRL_J, "ctrl-j")])
def test_multiline_newline_keys(axon_env: AxonEnv, newline_key: str, label: str) -> None:
    """Alt+Enter / Ctrl+J insert a newline; Enter then submits both lines as a single prompt."""
    axon_env.llm.enqueue([{"text": "multi ok"}])
    with open_repl(axon_env) as s:
        s.type("line one")
        s.send(newline_key)
        time.sleep(0.3)
        s.type("line two")
        m = s.mark()
        s.send("\r")
        s.expect("✓ Done", timeout=15, since=m)
    assert axon_env.llm.completion_requests()[0].last_user_text() == "line one\nline two", label


def test_multiline_trailing_backslash_continuation(axon_env: AxonEnv) -> None:
    """A line ending in a backslash continues on the next line instead of submitting."""
    axon_env.llm.enqueue([{"text": "bs ok"}])
    with open_repl(axon_env) as s:
        s.type("first part \\")
        s.send("\r")
        time.sleep(0.4)
        assert axon_env.llm.completion_requests() == []
        s.type("second part")
        m = s.mark()
        s.send("\r")
        s.expect("✓ Done", timeout=15, since=m)
    txt = axon_env.llm.completion_requests()[0].last_user_text()
    assert "first part" in txt and "second part" in txt and "\n" in txt and "\\" not in txt, repr(txt)


# ─── slash menu / @ completion ─────────────────────────────────────────────

def test_slash_opens_command_menu(idle) -> None:
    """Typing `/` opens the command menu with descriptions and key hints."""
    with _shared(idle) as s:
        m = s.mark()
        s.send("/")
        s.expect(r"/model\s+Switch active LLM", since=m)
        s.expect("tab complete · enter run · esc dismiss", since=m)


def test_slash_menu_filters_as_you_type(idle) -> None:
    """`/tra` narrows the menu to matching commands (e.g. /trace) and drops unrelated ones."""
    with _shared(idle) as s:
        s.send("/")
        time.sleep(0.4)
        m = s.mark()
        s.send("tra")
        s.expect(r"/trace", since=m)
        s.drain(0.4)
        last = s.screen[m:].rsplit("❯ /tra", 1)[-1]
        assert "/trace" in last
        assert "/provider" not in last and "/keys" not in last, last[-1200:]


def test_slash_menu_tab_completes(idle) -> None:
    """Tab in the slash menu completes the highlighted command into the prompt."""
    with _shared(idle) as s:
        s.type("/inspec")
        m = s.mark()
        s.send("\t")
        s.expect(r"❯ /inspect", since=m)


def test_slash_menu_esc_dismisses_and_keeps_text(idle) -> None:
    """Esc dismisses the slash menu without exiting or submitting."""
    with _shared(idle) as s:
        s.type("/mod")
        s.send(ESC)
        time.sleep(0.5)
        s.drain(0.3)
        assert s.proc.poll() is None
    assert idle.env.llm.completion_requests() == []


def test_unknown_slash_command_is_reported(idle) -> None:
    """An unknown `/command` gets a friendly message and is not sent to the model."""
    with _shared(idle) as s:
        m = s.submit("/definitely-not-a-command")
        s.ready(since=m)
        out = s.screen[m:].lower()
        assert "unknown" in out or "not found" in out or "did you mean" in out, out[-800:]
    assert idle.env.llm.completion_requests() == []


def test_at_file_completion_menu(idle) -> None:
    """`@al` offers workspace files matching the prefix."""
    (idle.env.workspace / "alpha.txt").write_text("a", encoding="utf-8")
    (idle.env.workspace / "zeta.txt").write_text("z", encoding="utf-8")
    with _shared(idle) as s:
        m = s.mark()
        s.send("@al")
        s.expect(r"alpha\.txt", since=m)
        s.expect("tab complete · enter insert", since=m)
        time.sleep(0.5)
        s.drain(0.3)
        final = s.screen[m:].rsplit("❯ @al", 1)[-1]
        assert "alpha.txt" in final and "zeta.txt" not in final, final


def test_at_file_completion_tab_inserts_and_attaches(axon_env: AxonEnv) -> None:
    """Tab inserts `@alpha.txt`; submitting attaches the file contents to the request."""
    (axon_env.workspace / "alpha.txt").write_text("ALPHA-FILE-CONTENT-42", encoding="utf-8")
    axon_env.llm.enqueue([{"text": "read it"}])
    with open_repl(axon_env) as s:
        s.send("@al")
        time.sleep(0.5)
        m = s.mark()
        s.send("\t")
        s.expect(r"@alpha\.txt", since=m)
        time.sleep(0.3)
        s.type(" summarize")
        m = s.mark()
        s.send("\r")
        s.expect("✓ Done", timeout=15, since=m)
    assert "ALPHA-FILE-CONTENT-42" in axon_env.llm.completion_requests()[0].all_text()


def test_at_completion_lists_directories(idle) -> None:
    """`@` completion also offers directories."""
    with _shared(idle) as s:
        m = s.mark()
        s.send("@srcd")
        s.expect(r"srcdir_xyz", since=m)


# ─── ! shell mode / # memory ───────────────────────────────────────────────

def test_bang_shell_mode_runs_command(idle) -> None:
    """`!echo hi` runs in the shell, prints the output and does not call the model."""
    with _shared(idle) as s:
        out = s.command("!echo hi-shell-mode", r"output added to context")
        assert "Shell: echo hi-shell-mode" in out
        assert re.search(r"^hi-shell-mode$", out, re.M), out[-800:]
    assert idle.env.llm.completion_requests() == []


def test_bang_shell_output_reaches_next_prompt(axon_env: AxonEnv) -> None:
    """Output of a `!` command is added to the context of the next model request."""
    axon_env.llm.enqueue([{"text": "seen"}])
    with open_repl(axon_env) as s:
        s.command("!echo SHELL-CTX-777", r"output added to context")
        s.ready()
        s.turn("what did it print?")
    assert "SHELL-CTX-777" in axon_env.llm.completion_requests()[0].all_text()


def test_bang_shell_mode_nonzero_exit_is_shown(idle) -> None:
    """A failing `!` command is reported without crashing the REPL."""
    with _shared(idle) as s:
        m = s.submit("!exit 3")
        s.ready(since=m)
        out = s.screen[m:]
        assert TRACEBACK not in out and s.proc.poll() is None
        assert "3" in out


def test_hash_quick_memory_saves_note(axon_env: AxonEnv) -> None:
    """`#remember use tabs` saves a project memory file and confirms it."""
    with open_repl(axon_env) as s:
        out = s.command("#remember use tabs", r"Saved .*memory")
        assert "remember use tabs" in out
    files = list((axon_env.workspace / ".axon" / "memory").glob("*.md")) + \
        list(axon_env.axon_home.rglob("memory/*.md"))
    assert any("use tabs" in p.read_text(encoding="utf-8") for p in files), files


def test_hash_memory_reaches_system_prompt(axon_env: AxonEnv) -> None:
    """A saved `#` memory is included in the next request to the model."""
    axon_env.llm.enqueue([{"text": "ok"}])
    with open_repl(axon_env) as s:
        s.command("#always answer in haiku ZQX", r"Saved .*memory")
        s.ready()
        s.turn("hello")
    assert "ZQX" in axon_env.llm.completion_requests()[0].all_text()


# ─── vim mode ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def vim_repl(tmp_path_factory: pytest.TempPathFactory):
    """One REPL switched to vim mode with `/vim`, shared by the vim-key tests (draft reset between tests)."""
    from fake_llm import FakeLLMServer
    from harness import create_axon_env
    server = FakeLLMServer(model_ids=("gpt-4o",)).start()
    env = create_axon_env(tmp_path_factory.mktemp("vim"), server)
    s = open_repl(env)
    s.env = env
    try:
        s.command("/vim", "Editor mode vim")
        s.ready()
        yield s
    finally:
        s.close()
        server.stop()


@contextlib.contextmanager
def _vim(s):
    """Return the shared vim REPL to an empty prompt in insert mode around a test."""
    def reset() -> None:
        if s.proc.poll() is not None:
            pytest.fail("vim REPL exited:\n" + s.screen[-1500:])
        m = s.mark()
        s.send(ESC)
        s.expect("-- NORMAL --", timeout=4, since=m)
        s.send("A")  # append at end of line -> insert mode with the cursor at the end
        s.expect("-- INSERT --", timeout=4, since=m)
        s.send("x")
        time.sleep(0.05)
        s.send(CTRL_U)  # kill to start of line = whole draft
        s.expect(PROMPT_READY, timeout=4, since=m)
        s.drain(0.2)
    reset()
    yield s
    reset()


def _normal(s) -> None:
    m = s.mark()
    s.send(ESC)
    s.expect("-- NORMAL --", timeout=6, since=m)


def test_vim_mode_shows_insert_then_normal(vim_repl) -> None:
    """/vim enables the vim editor: `-- INSERT --` is shown, Esc switches to `-- NORMAL --`."""
    with _vim(vim_repl) as s:
        assert "-- INSERT --" in s.screen[-600:]
        s.type("abc")
        m = s.mark()
        s.send(ESC)
        s.expect("-- NORMAL --", timeout=8, since=m)


def test_vim_dw_deletes_word(vim_repl) -> None:
    """In normal mode `0dw` deletes the first word."""
    with _vim(vim_repl) as s:
        s.type("hello world foo")
        _normal(s)
        m = s.mark()
        s.send("0")
        time.sleep(0.1)
        s.send("dw")
        s.expect("❯ world foo", timeout=8, since=m)


def test_vim_u_undoes(vim_repl) -> None:
    """`u` undoes the previous `dw`."""
    with _vim(vim_repl) as s:
        s.type("hello world foo")
        _normal(s)
        s.send("0")
        time.sleep(0.1)
        m = s.mark()
        s.send("dw")
        s.expect("❯ world foo", timeout=8, since=m)
        m = s.mark()
        s.send("u")
        s.expect("❯ hello world foo", timeout=8, since=m)


def test_vim_dd_clears_line(vim_repl) -> None:
    """`dd` deletes the whole line (placeholder shown again)."""
    with _vim(vim_repl) as s:
        s.type("delete me entirely")
        _normal(s)
        m = s.mark()
        s.send("dd")
        s.expect(PROMPT_READY, timeout=8, since=m)


def test_vim_i_returns_to_insert_and_submit(vim_repl) -> None:
    """After Esc, `i` returns to insert mode (before the cursor); Enter submits the edited text."""
    vim_repl.env.llm.enqueue([{"text": "vim ok"}])
    with _vim(vim_repl) as s:
        s.type("abc")
        _normal(s)
        m = s.mark()
        s.send("i")
        s.expect("-- INSERT --", since=m)
        s.type("X")
        s.send("\r")
        s.expect("✓ Done", timeout=15, since=m)
        s.ready(since=m)
    assert vim_repl.env.llm.completion_requests()[-1].last_user_text() == "abXc"


def test_vim_normal_mode_keys_not_inserted(vim_repl) -> None:
    """Motion keys typed in normal mode (h, l, w) never end up in the submitted text."""
    vim_repl.env.llm.enqueue([{"text": "ok"}])
    with _vim(vim_repl) as s:
        s.type("one two")
        _normal(s)
        s.send("hhlw")
        time.sleep(0.3)
        m = s.mark()
        s.send("\r")
        s.expect("✓ Done", timeout=15, since=m)
        s.ready(since=m)
    assert vim_repl.env.llm.completion_requests()[-1].last_user_text() == "one two"


# ─── type-ahead while a turn runs ──────────────────────────────────────────

def test_typing_during_text_turn_is_queued_as_next_prompt(axon_env: AxonEnv) -> None:
    """A prompt typed while a slow answer streams runs as the next turn afterwards."""
    axon_env.llm.enqueue([{"text": "slow start "}, {"delay": 2.0}, {"text": "slow end"}], [{"text": "QUEUED-ANS"}])
    # (typing starts while the spinner is up; see the xfail below for typing during the turn->prompt handover)
    with open_repl(axon_env) as s:
        m = s.submit("slow one")
        s.expect("responding", timeout=10, since=m)
        for ch in "follow up":
            s.send(ch)
            time.sleep(0.03)
        time.sleep(0.3)
        s.send("\r")
        s.expect("QUEUED-ANS", timeout=20, since=m)
    assert [q.last_user_text() for q in axon_env.llm.completion_requests()] == ["slow one", "follow up"]


def test_typing_during_tool_round_steers_model(axon_env: AxonEnv) -> None:
    """A message typed during a tool round is delivered to the model at the next step."""
    axon_env.llm.enqueue([{"text": "working"}, {"delay": 2.0},
                          {"tool": "Bash", "args": {"command": "echo tick", "description": "t"}}],
                         [{"text": "AFTER-STEER"}])
    with open_repl(axon_env) as s:
        m = s.submit("go")
        s.expect("responding|working", timeout=10, since=m)
        for ch in "steer msg":
            s.send(ch)
            time.sleep(0.03)
        time.sleep(0.3)
        s.send("\r")
        s.expect("Queued #1", timeout=10, since=m)
        s.expect("AFTER-STEER", timeout=20, since=m)
        s.expect("✓ Done", timeout=10, since=m)
        assert "message you typed during the turn sent to the model" in s.screen[m:]
    reqs = axon_env.llm.completion_requests()
    assert "[User added while you were working]: steer msg" in reqs[1].all_text()
    time.sleep(0.2)
    assert len(reqs) == 2, "steered message must not also run as a separate turn"


def _start_slow(axon_env: AxonEnv, s, kind: str) -> int:
    if kind == "stream":
        axon_env.llm.enqueue([{"text": "long "}, {"delay": 3.0}, {"text": "never shown"}])
        m = s.submit("long task")
        s.expect("responding", timeout=10, since=m)
    else:
        axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": "sleep 3; echo SLEPT-$((40+2))", "description": "s"}}],
                             [{"text": "after tool"}])
        m = s.submit("long tool")
        s.expect("Bash  sleep 3", timeout=10, since=m)
    time.sleep(0.5)
    return m


def test_esc_interrupts_running_tool(axon_env: AxonEnv) -> None:
    """Esc on an empty in-flight draft interrupts a running Bash tool promptly; the REPL stays alive."""
    with open_repl(axon_env) as s:
        m = _start_slow(axon_env, s, "tool")
        t0 = time.monotonic()
        s.send(ESC)
        s.expect("Interrupting", timeout=5, since=m)
        s.ready(since=m, timeout=12)
        assert time.monotonic() - t0 < 4.0
        assert "SLEPT-42" not in s.screen[m:] and s.proc.poll() is None


def test_esc_interrupts_stalled_stream_promptly(axon_env: AxonEnv) -> None:
    """Esc while the model stream is stalled returns to the prompt within a few seconds."""
    with open_repl(axon_env) as s:
        m = _start_slow(axon_env, s, "stream")
        s.send(ESC)
        s.expect("Interrupted", timeout=2, since=m)


def test_ctrl_c_interrupts_running_tool(axon_env: AxonEnv) -> None:
    """Ctrl+C during a running Bash tool interrupts the turn."""
    with open_repl(axon_env) as s:
        m = _start_slow(axon_env, s, "tool")
        s.send(CTRL_C)
        s.expect("Interrupting|Interrupted", timeout=2, since=m)


def test_ctrl_c_interrupts_streaming_turn(axon_env: AxonEnv) -> None:
    """Ctrl+C during a streaming answer interrupts the turn."""
    with open_repl(axon_env) as s:
        m = _start_slow(axon_env, s, "stream")
        s.send(CTRL_C)
        s.expect(r"Interrupting|Interrupted|never shown", timeout=6, since=m)
        assert "never shown" not in s.screen[m:]


# ─── approval prompts ──────────────────────────────────────────────────────

def _approval(axon_env: AxonEnv, target: str = "approved.txt"):
    axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": f"echo approved-run > {target}", "description": "write"}}],
                         [{"text": "DONE-APPROVAL"}])
    s = open_repl(axon_env, ("--mode", "default"))
    m = s.submit("run bash")
    s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
    s.expect(r"1/2/3 quick pick", timeout=5, since=m)
    s.drain(0.3)
    return s, m


def test_approval_prompt_shows_command_and_shell(axon_env: AxonEnv) -> None:
    """In default mode a Bash call shows `⚠ Allow Bash?` with the command, description and shell."""
    s, m = _approval(axon_env)
    with s:
        out = s.screen[m:]
        assert "$ echo approved-run > approved.txt" in out
        assert "runs in" in out
        assert "Yes, don't ask again this session" in out


def test_approval_quick_pick_1_allows_once(axon_env: AxonEnv) -> None:
    """Pressing `1` allows the call: the command runs and the turn completes."""
    s, m = _approval(axon_env)
    with s:
        s.send("1")
        s.expect("Allowed once", timeout=8, since=m)
        s.expect("DONE-APPROVAL", timeout=15, since=m)
    assert (axon_env.workspace / "approved.txt").read_text().strip() == "approved-run"


def test_approval_quick_pick_3_denies(axon_env: AxonEnv) -> None:
    """Pressing `3` denies: the file is not written and the refusal is reported to the model."""
    s, m = _approval(axon_env)
    with s:
        s.send("3")
        s.expect("Denied", timeout=8, since=m)
        s.expect("User declined this action", timeout=8, since=m)
        s.expect(r"└ ✓ Done", timeout=15, since=m)
    assert not (axon_env.workspace / "approved.txt").exists()
    res = axon_env.llm.completion_requests()[1].tool_results()
    assert res and "declined" in res[0].lower(), res


def test_approval_esc_denies(axon_env: AxonEnv) -> None:
    """Esc in the approval picker denies the call."""
    s, m = _approval(axon_env)
    with s:
        s.send(ESC)
        s.expect("Denied", timeout=8, since=m)
    assert not (axon_env.workspace / "approved.txt").exists()


def test_approval_always_skips_prompt_for_same_command(axon_env: AxonEnv) -> None:
    """After `2` (don't ask again), re-running the very same command in the session is not prompted."""
    s, m = _approval(axon_env)
    with s:
        s.send("2")
        s.expect("Allowed for this session", timeout=8, since=m)
        s.ready(since=m)
        (axon_env.workspace / "approved.txt").unlink()
        axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": "echo approved-run > approved.txt",
                                                        "description": "write"}}], [{"text": "SAME-DONE"}])
        m2 = s.submit("same again")
        s.expect(r"SAME-DONE|Allow Bash\?", timeout=15, since=m2)
        assert "Allow Bash?" not in s.screen[m2:]
    assert (axon_env.workspace / "approved.txt").exists()


def test_approval_always_covers_other_bash_commands(axon_env: AxonEnv) -> None:
    """`Yes, don't ask again this session (for Bash)` also lets a different Bash command run unprompted."""
    s, m = _approval(axon_env)
    with s:
        s.send("2")
        s.expect("Allowed for this session", timeout=8, since=m)
        s.ready(since=m)
        axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": "echo again > second.txt", "description": "w"}}],
                             [{"text": "SECOND-DONE"}])
        m2 = s.submit("again")
        s.expect(r"SECOND-DONE|Allow Bash\?", timeout=15, since=m2)
        assert "Allow Bash?" not in s.screen[m2:]


def test_approval_keys_not_leaked_into_next_prompt(axon_env: AxonEnv) -> None:
    """The key used to answer the approval is consumed: the next submitted prompt is exactly what was typed."""
    s, m = _approval(axon_env)
    with s:
        s.send("1")
        s.ready(since=m)
        axon_env.llm.enqueue([{"text": "clean"}])
        s.turn("next prompt")
    assert axon_env.llm.completion_requests()[-1].last_user_text() == "next prompt"


def test_approval_y_key_allows(axon_env: AxonEnv) -> None:
    """Pressing `y` at the approval prompt allows the call."""
    s, m = _approval(axon_env)
    with s:
        s.send("y")
        s.expect("Allowed once", timeout=1.5, since=m)


# ─── themes / colours ──────────────────────────────────────────────────────

def _turn_sgr(s, text: str) -> set[str]:
    off = len(s._raw)
    s.turn(text)
    raw = s.raw_since(off)
    i, j = raw.find("Turn"), raw.find("Done")
    return set(SGR_RE.findall(raw[i:j]))


def test_theme_command_light_then_dark_changes_escape_codes(axon_env: AxonEnv) -> None:
    """`/theme light` then `/theme dark` switch the palette used by each following turn's escape codes."""
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    with open_repl(axon_env) as s:
        s.command("/theme light", "Theme light")
        s.ready()
        light = _turn_sgr(s, "hello")
        s.command("/theme dark", "Theme dark")
        s.ready()
        dark = _turn_sgr(s, "again")
    assert "\x1b[38;5;28m" in light and "\x1b[38;5;114m" not in light, light
    assert "\x1b[38;5;114m" in dark and "\x1b[38;5;28m" not in dark, dark


def test_theme_mono_strips_colours(axon_env: AxonEnv) -> None:
    """`/theme mono` leaves only bold/reset SGR codes in the turn output."""
    axon_env.llm.enqueue([{"text": "mono"}])
    with open_repl(axon_env) as s:
        s.command("/theme mono", "Theme mono")
        s.ready()
        codes = _turn_sgr(s, "hello")
        assert codes <= {"\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[3m", "\x1b[4m", "\x1b[7m"}, codes


def test_theme_persists_to_next_session(axon_env: AxonEnv) -> None:
    """The chosen theme is saved to settings.json and used by the next session."""
    axon_env.llm.enqueue([{"text": "x"}])
    with open_repl(axon_env) as s:
        s.command("/theme light", "Theme light")
    assert '"theme": "light"' in (axon_env.axon_home / "settings.json").read_text()
    with open_repl(axon_env) as s2:
        assert "\x1b[38;5;28m" in _turn_sgr(s2, "hi")


def test_no_color_pty_has_no_sgr_colour(axon_env: AxonEnv) -> None:
    """With NO_COLOR set on a real terminal, banner and turn output carry no colour SGR codes."""
    axon_env.llm.enqueue(*tool_turn())
    with open_repl(axon_env, ("--mode", "bypass"), no_color=True) as s:
        s.turn("go")
        raw = s.raw_since(0)
        assert not re.search(r"\x1b\[(?:3|4|9|10)[0-9](?:;[0-9]+)*m", raw), set(SGR_RE.findall(raw))


# ─── misc keys / robustness ────────────────────────────────────────────────

def test_question_mark_shows_shortcuts(idle) -> None:
    """`?` on an empty prompt lists the keyboard shortcuts with their actions."""
    with _shared(idle) as s:
        m = s.mark()
        s.send("?")
        s.expect(r"history:search", timeout=8, since=m)
        assert "ctrl-r" in s.screen[m:] and "chat:cycleMode" in s.screen[m:]


def test_help_lists_commands(idle) -> None:
    """/help lists core commands."""
    with _shared(idle) as s:
        out = s.command("/help", r"/exit")
        for c in ("/model", "/trace", "/inspect"):
            assert c in out


def test_sigwinch_resize_does_not_crash(axon_env: AxonEnv) -> None:
    """Shrinking the window (TIOCSWINSZ + SIGWINCH) keeps the REPL alive and responsive."""
    import fcntl
    import termios
    axon_env.llm.enqueue([{"text": "after resize"}])
    with open_repl(axon_env) as s:
        for rows, cols in ((20, 50), (50, 160), (24, 80)):
            fcntl.ioctl(s._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.killpg(s.proc.pid, signal.SIGWINCH)
            time.sleep(0.3)
        s.drain(0.4)
        assert s.proc.poll() is None
        out = s.turn("still there?")
        assert "after resize" in out and TRACEBACK not in s.screen


def test_resize_during_running_turn(axon_env: AxonEnv) -> None:
    """A SIGWINCH while a turn is streaming does not break the turn."""
    import fcntl
    import termios
    axon_env.llm.enqueue([{"text": "part one "}, {"delay": 1.0}, {"text": "part two"}])
    with open_repl(axon_env) as s:
        m = s.submit("stream")
        s.expect("part one", timeout=10, since=m)
        fcntl.ioctl(s._master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 70, 0, 0))
        os.killpg(s.proc.pid, signal.SIGWINCH)
        s.expect("✓ Done", timeout=15, since=m)
        assert "part two" in s.screen[m:] and TRACEBACK not in s.screen


def test_ctrl_l_redraws(idle) -> None:
    """Ctrl+L clears and redraws the prompt without losing the draft."""
    with _shared(idle) as s:
        s.type("kept draft")
        m = s.mark()
        s.send(CTRL_L)
        s.expect("❯ kept draft", timeout=8, since=m)


def test_narrow_pty_turn_fits_width(axon_env: AxonEnv) -> None:
    """In a 60-column terminal the turn timeline rail lines stay within 60 columns."""
    from axon.ui.theme import display_width
    axon_env.llm.enqueue(*tool_turn())
    with open_repl(axon_env, ("--mode", "bypass"), cols=60) as s:
        out = s.turn("go")
        i = out.find("┌ Turn")
        body = out[i:out.find("✓ Done", i) + 80]
        too_wide = [ln for ln in body.splitlines() if display_width(ln) > 60]
        assert not too_wide, too_wide
