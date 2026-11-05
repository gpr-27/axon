"""Every tool call explains its decision from data Axon already has: who chose it, why (the model's own
words), what it does in plain words, why it was allowed to run, and where its result goes."""
from __future__ import annotations

import json

import pytest

from axon.agent.decision import (
    allowed_because, chosen_by_text, describe_arguments, pick_rationale, pick_rationales, rationale_line,
    split_sentences, then_text,
)
from axon.agent.loop import tool_rationales
from axon.agent.trace import LLMCallEnd, LLMRequestInfo, PermissionDecisionEvent, ToolRationale
from axon.providers.base import (
    AssistantTurn, LLMCallStart, TextBlock, ThinkingBlock, ToolExecutionResult, ToolExecutionStart, ToolUseBlock,
    Usage,
)
from axon.ui.render import Renderer, tool_call_rows
from axon.ui.theme import strip_ansi


# ─── why ──────────────────────────────────────────────────────────────────

def test_colon_does_not_split_a_sentence():
    assert split_sentences("Test 1 (Bash) passed: it returned main.\nTest 2 next: Glob.") == [
        "Test 1 (Bash) passed: it returned main.", "Test 2 next: Glob."]


def test_glob_case_never_gives_the_bare_tool_name():
    turn = AssistantTurn(blocks=[
        TextBlock(text="Test 1 (Bash) passed: it returned the working directory and branch main.\nTest 2 next: Glob."),
        ToolUseBlock(id="g", name="Glob", input={"pattern": "pyproject.toml", "head_limit": 5}),
    ])
    why, source = tool_rationales(turn)["g"]
    assert why != "Glob." and why == "Test 2 next: Glob."
    assert source == "model-text"


def test_a_sentence_naming_the_path_wins_over_the_last_sentence():
    text = "I will look at src/axon/agent/loop.py to find the rationale code. Let me get started now."
    turn = AssistantTurn(blocks=[TextBlock(text=text),
                                 ToolUseBlock(id="r", name="Read", input={"path": "src/axon/agent/loop.py"})])
    assert tool_rationales(turn)["r"][0] == "I will look at src/axon/agent/loop.py to find the rationale code."


def test_short_or_name_only_sentences_never_count():
    assert pick_rationale("Glob", {"pattern": "*.py"}, "Glob.") is None
    assert pick_rationale("Glob", {"pattern": "*.py"}, "Now Glob it.") is None       # under four words


def test_each_call_in_one_reply_gets_its_own_sentence():
    blocks = [
        ("text", "First I read the config in settings.toml to see the port. Then I search for the handler."),
        ("tool", ("a", "Read", {"path": "settings.toml"})),
        ("tool", ("b", "Grep", {"pattern": "def handler", "path": "src"})),
    ]
    out = pick_rationales(blocks)
    assert out["a"][0] == "First I read the config in settings.toml to see the port."
    assert "b" not in out or out["b"][0] != out["a"][0]      # one intro sentence is not reused for every call


def test_a_later_call_uses_its_own_preceding_text():
    turn = AssistantTurn(blocks=[
        TextBlock(text="Let me read the main module first."),
        ToolUseBlock(id="a", name="Read", input={"path": "main.py"}),
        TextBlock(text="And I will run the unit tests after that."),
        ToolUseBlock(id="b", name="Bash", input={"command": "pytest -q"}),
    ])
    out = tool_rationales(turn)
    assert out["a"][0] == "Let me read the main module first."
    assert out["b"][0] == "And I will run the unit tests after that."


def test_thinking_is_labelled_as_thought_and_heuristic_as_axons_note():
    turn = AssistantTurn(blocks=[ThinkingBlock(text="The failing test lives in tests/test_api.py so read it."),
                                 ToolUseBlock(id="t", name="Read", input={"path": "tests/test_api.py"})])
    why, source = tool_rationales(turn)["t"]
    assert source == "model-thinking"
    assert rationale_line(why, source).startswith('model thought: "The failing test')
    assert rationale_line("read-only lookup", "heuristic") == "Axon's note: read-only lookup"


def test_rationale_is_one_line_of_at_most_160_chars():
    long = "I need to inspect " + "the very long module name " * 20 + "in app.py now."
    why, _ = pick_rationale("Read", {"path": "app.py"}, long)
    assert len(why) <= 160 and "\n" not in why


# ─── chosen by / what / allowed / then ───────────────────────────────────

def test_chosen_by_names_model_provider_step_call_and_tool_count():
    text = chosen_by_text("claude-haiku-5-5", "Anthropic", 2, 1, 1, 22)
    assert text == "chosen by claude-haiku-5-5 (Anthropic) in step 2 · call 1 of 1 · from 22 tools"
    assert "via prompt tools" in chosen_by_text("qwen-3", "Gateway", 1, 1, 2, 9, prompt_tools=True)


def test_plain_words_for_read_glob_grep_bash():
    assert describe_arguments("Read", {"path": "a.py", "offset": 10, "limit": 20})[0] == "read a.py lines 10-29"
    glob, covered = describe_arguments("Glob", {"pattern": "*.toml", "path": "src", "head_limit": 5})
    assert glob == "find files matching *.toml in src, show at most 5" and "head_limit" in covered
    grep, _ = describe_arguments("Grep", {"pattern": "def x", "path": "src", "glob": "*.py",
                                          "output_mode": "content", "head_limit": 20})
    assert grep == 'search "def x" in src (*.py files), show matching lines, first 20'
    bash, _ = describe_arguments("Bash", {"command": "pytest -q", "description": "Run the tests", "timeout_s": 60})
    assert bash == "Run the tests, time limit 60s"
    assert describe_arguments("WebFetch", {"url": "https://x"}) == ("", set())   # generic tools keep key rows


def test_plain_words_replace_argument_rows_at_normal_level_only():
    inp = {"pattern": "*.toml", "head_limit": 5}
    normal = [strip_ansi(r) for _, r in tool_call_rows("Glob", inp, detail=1, width=100)]
    assert "finds files by name · find files matching *.toml in the workspace, show at most 5" in normal
    assert not any(r.startswith("head_limit") for r in normal)
    detailed = [strip_ansi(r) for _, r in tool_call_rows("Glob", inp, detail=2, width=100)]
    assert any(r.startswith("head_limit") for r in detailed)


def test_allowed_because_lines():
    assert allowed_because("allow", "mode", "Bypass mode active.") == "allowed: bypass mode is on"
    assert allowed_because("allow", "rule", "Allowed by rule: Bash(git *)") == "allowed by rule Bash(git *)"
    assert allowed_because("allow", "mode", "'Glob' is allowed by default.") == \
        "allowed: this tool is allowed by default (read-only)"
    assert allowed_because("allow", "user", "x", "once") == "approved by you (once)"
    assert allowed_because("allow", "hook", "Allowed by PreToolUse hook.") == "allowed by hook: allowed by PreToolUse hook"
    assert allowed_because("deny", "rule", "Blocked by deny rule: Bash(rm *)").startswith("denied by rule")


def test_then_line():
    assert then_text(54, 14, 3) == "result (54 B, ~14 tokens) goes to the model in step 3"
    assert then_text(54, 14, 3, 1, "call_01") == "result #1 [call_01] (54 B, ~14 tokens) goes to the model in step 3"


# ─── the live timeline ───────────────────────────────────────────────────

def _live_turn(capsys, level="normal") -> str:
    r = Renderer(trace_level=level)
    r.on_event(LLMCallStart(iteration=2, max_iterations=30, model="claude-haiku-5-5", message_count=13,
                            tokens_in=16200, user_prompt="test tools", mode="bypass"))
    r.on_event(LLMRequestInfo(iteration=2, model="claude-haiku-5-5", max_tokens=8000, effort="balanced", thinking=False,
                              message_count=13, system_tokens=1, tool_schema_tokens=1, conversation_tokens=1,
                              tool_count=22, cache_markers=0, provider="anthropic"))
    r.on_event(LLMCallEnd(iteration=2, model="claude-haiku-5-5", started_at=0, ended_at=1, duration_s=1.2,
                          usage=Usage(input=100, output=10), cost=0.0, stop_reason="tool_use", tool_use_count=1))
    r.on_event(ToolRationale(id="g", name="Glob", text="Test 2 next: Glob.", source="model-text"))
    r.on_event(ToolExecutionStart(id="g", name="Glob", input={"pattern": "pyproject.toml", "head_limit": 5}))
    r.on_event(PermissionDecisionEvent(id="g", tool="Glob", outcome="allow", reason="Bypass mode active.", source="mode"))
    r.on_event(ToolExecutionResult(id="g", name="Glob", input={"pattern": "pyproject.toml"}, content="pyproject.toml"))
    return strip_ansi(capsys.readouterr().out)


def test_live_tool_row_explains_the_decision(capsys, monkeypatch):
    import axon.ui.render as render_mod
    monkeypatch.setattr(render_mod, "term_width", lambda: 160)
    out = _live_turn(capsys, "detailed")
    assert "chosen by claude-haiku-5-5 in step 2 · call 1 of 1 · from 22 tools" in out
    assert 'model said: "Test 2 next: Glob."' in out and "why: Glob." not in out
    assert "finds files by name · find files matching pyproject.toml in the workspace, show at most 5" in out
    assert "allowed: bypass mode is on" in out
    assert "result #1 [g] (14 B, ~3 tokens) goes to the model in step 3" in out


def test_normal_level_card_has_the_four_phases_and_names_the_model_once(capsys, monkeypatch):
    import axon.ui.render as render_mod
    monkeypatch.setattr(render_mod, "term_width", lambda: 160)
    out = _live_turn(capsys)
    rows = out.splitlines()
    labels = [next(i for i, r in enumerate(rows) if f"  {name} " in r and "│ " in r)
              for name in ("choose", "input", "run", "output")]
    assert labels == sorted(labels)                                              # choose, input, run, output in order
    assert "choose  Test 2 next: Glob." in out and "input   pyproject.toml" in out
    assert "run     ✓ " in out and "▣ TOOLS · 1 call · allowed: bypass mode" in out and "output  pyproject.toml" in out
    assert "allowed (bypass mode)" not in out                                    # said once, in the section header
    assert "chosen by" not in out and "from 22 tools" not in out and "call 1 of 1" not in out
    assert "model said" not in out and "exact call" not in out
    with_model = [r for r in rows if "claude-haiku-5-5" in r]
    assert len(with_model) == 2 and with_model[0].startswith("┌ Turn") and with_model[1].startswith("│ ◆ Step 2")
    assert "→ 1 result (14 B, ~3 tokens) goes to step 3" in out


def test_compact_level_stays_one_line_per_call(capsys):
    out = _live_turn(capsys, "compact")
    assert "chosen by" not in out and "model said" not in out and "goes to the model" not in out


# ─── the restored-session view ───────────────────────────────────────────

def test_restored_history_shows_the_same_explanation(tmp_path):
    from axon.ui.history_view import format_session_history

    recs = [
        {"type": "user_message", "data": {"role": "user", "content": "test tools"}},
        {"type": "assistant_turn", "data": {
            "stop_reason": "tool_use", "text": "Test 1 passed: it printed main.\nTest 2 next: Glob.", "thinking": "",
            "tool_uses": [{"id": "g", "name": "Glob", "input": {"pattern": "pyproject.toml", "head_limit": 5}}],
            "model": "claude-haiku-5-5", "usage": {"input": 100, "output": 10, "cache_read": 0, "cache_write": 0},
            "call": {"provider": "Anthropic", "message_count": 13, "tool_count": 22}}},
        {"type": "permission", "data": {"tool_use_id": "g", "tool": "Glob", "outcome": "allow",
                                        "reason": "Bypass mode active.", "source": "mode", "approval": None}},
        {"type": "tool_results", "data": [{"tool_use_id": "g", "content": "pyproject.toml", "is_error": False}]},
        {"type": "assistant_turn", "data": {"stop_reason": "end_turn", "text": "Both passed.", "tool_uses": [],
                                            "model": "claude-haiku-5-5", "call": {}}},
        # an older session without the new fields still draws
        {"type": "assistant_turn", "data": {"stop_reason": "tool_use", "text": "",
                                            "tool_uses": [{"id": "x", "name": "Read", "input": {"path": "a.py"}}]}},
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    normal = "\n".join(strip_ansi(r) for r in format_session_history(path, width=120))
    assert "choose  Test 2 next: Glob." in normal and "allowed: bypass mode" in normal and "chosen by" not in normal
    assert "→ 1 result (14 B, ~3 tokens) goes to step 2" in normal
    out = "\n".join(strip_ansi(r) for r in format_session_history(path, width=120, trace_level="detailed"))
    assert "chosen by claude-haiku-5-5 (Anthropic) in step 1 · call 1 of 1 · from 22 tools" in out
    assert 'model said: "Test 2 next: Glob."' in out
    assert "allowed: bypass mode is on" in out
    assert "result (14 B, ~3 tokens) goes to the model in step 2" in out
    assert "final answer written in step 2 by claude-haiku-5-5" in out
    assert "Read  a.py" in out
    assert "Read" in normal and "input   a.py" in normal


def test_inspect_trace_reads_stored_why_dicts():
    from axon.ui.render import format_trace

    trace = {"turn": 1, "prompt": "p", "model": "m", "steps": [
        {"kind": "llm", "data": {"iteration": 1, "model": "m", "request": {"tool_count": 7, "provider": "anthropic"},
                                 "tool_use_count": 1, "stop_reason": "tool_use"}},
        {"kind": "tool", "data": {"id": "a", "name": "Read", "input": {"path": "a.py"},
                                  "why": {"text": "I need to read a.py first.", "source": "model-text"},
                                  "permission": {"outcome": "allow", "source": "mode", "reason": "Bypass mode active."},
                                  "bytes": 10, "est_tokens": 3, "result_preview": "x = 1"}},
    ]}
    card = strip_ansi(format_trace(trace, "normal"))
    assert "choose  I need to read a.py first." in card and "allowed: bypass mode" in card and "chosen by" not in card
    out = strip_ansi(format_trace(trace, "detailed"))
    assert "chosen by m (anthropic) in step 1 · call 1 of 1 · from 7 tools" in out
    assert 'model said: "I need to read a.py first."' in out and "{'text'" not in out
    assert "allowed: bypass mode is on" in out
    assert "result (10 B, ~3 tokens) goes to the model in step 2" in out
    assert "→ 1 result (10 B, ~3 tokens) goes to step 2" in card
