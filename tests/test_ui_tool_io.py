"""Tool call presentation: every tool (built-in, MCP, unknown) gets a header + 'key  value' input rows and
a result section whose first row summarises it (exit code, counts, JSON shape, error message)."""
from __future__ import annotations

import json

import pytest

from axon.providers.base import (LLMCallStart, ToolBatchStart, ToolExecutionResult, ToolExecutionStart,
                                 ToolUseBlock, TurnComplete, Usage)
from axon.ui import render, theme
from axon.ui.render import (Renderer, format_conversation_replay, format_trace, tool_call_rows, tool_header_rows,
                            tool_input_rows, tool_label, tool_result_rows, tool_target)
from axon.ui.theme import display_width, strip_ansi

MCP = "mcp__github__search_issues"
MCP_ARGS = {"owner": "acme", "repo": "widgets", "query": "label:bug state:open", "per_page": 5}
MCP_OUT = json.dumps({"total_count": 2, "items": [{"number": 41}, {"number": 37}]})


def _plain(rows) -> list[str]:
    return [strip_ansi(r if isinstance(r, str) else r[1]) for r in rows]


# ─── input ─────────────────────────────────────────────────────────────────

def test_mcp_tool_header_names_server_and_shows_its_best_target():
    assert tool_label(MCP) == "github › search_issues"
    assert tool_label("Bash") == "Bash"
    assert tool_target(MCP, MCP_ARGS) == "label:bug state:open"          # 'query' beats the first argument
    assert _plain(tool_header_rows(MCP, MCP_ARGS)) == ["● github › search_issues  label:bug state:open"]
    assert _plain(tool_header_rows(MCP, MCP_ARGS, detail=0)) == ["● github › search_issues(label:bug state:open)"]


def test_remaining_arguments_are_aligned_key_value_rows():
    rows = _plain(tool_input_rows(MCP, MCP_ARGS, width=80))
    assert rows == ["owner     acme", "repo      widgets", "per_page  5"]  # the header's query is not repeated
    assert tool_input_rows(MCP, MCP_ARGS, width=80, detail=0) == []       # compact: the header only
    odd = _plain(tool_input_rows("plugin_tool", {"name": "x", "on": True, "tags": ["a", "b"], "none": None,
                                                  "_internal": 1}, width=80))
    assert odd == ["on    true", 'tags  ["a", "b"]']                      # bools / JSON; empty + private hidden


def test_long_and_multiline_values_are_elided_per_trace_level():
    inp = {"description": "Inspect", "prompt": "line one\nline two\nline three"}
    normal = _plain(tool_input_rows("Task", inp, width=80))
    assert normal == ["prompt  line one ↪ +2 lines"]
    detailed = _plain(tool_input_rows("Task", inp, width=80, detail=2))
    assert detailed == ["prompt  line one", "        line two", "        line three"]
    long = _plain(tool_input_rows("t", {"q": "x", "note": "word " * 200}, width=60))
    assert len(long) == 2 and long[-1].endswith("…") and all(display_width(r) <= 60 for r in long)


def test_bodies_the_card_draws_are_hidden_at_normal_and_sized_at_detailed():
    inp = {"path": "a.py", "old_string": "a\nb", "new_string": "c", "replace_all": True}
    assert _plain(tool_input_rows("Edit", inp, width=80)) == ["replace_all  true"]
    detailed = _plain(tool_input_rows("Edit", inp, width=80, detail=2))
    assert detailed == ["old_string   2 lines · 3 B", "new_string   1 line · 1 B", "replace_all  true"]
    debug = _plain(tool_input_rows("Edit", inp, width=80, detail=3))
    assert debug[:2] == ["old_string   a", "             b"]


def test_bash_command_block_keeps_multiline_commands_readable():
    inp = {"command": "make build \\\n  --jobs 4\nmake test", "description": "Build and test", "timeout": 60}
    rows = tool_call_rows("Bash", inp, detail=1, width=80)
    plain = [(i, strip_ansi(t)) for i, t in rows]
    assert plain[0] == (0, "● Bash  $ make build \\")
    lead = len("● Bash  $ ")
    assert plain[1] == (lead, "  --jobs 4") and plain[2] == (lead, "make test")   # under the first line
    assert plain[3] == (4, "Build and test")                  # its own description replaces 'runs a shell command'
    assert plain[4] == (4, "timeout  60")
    assert tool_target("Bash", inp) == "make build --jobs 4 make test"
    assert _plain(tool_header_rows("Bash", inp, detail=0)) == ["● Bash(make build --jobs 4 make test)"]


# ─── output ────────────────────────────────────────────────────────────────

def test_bash_exit_code_is_the_first_fact_and_empty_output_says_so():
    failed = _plain(tool_result_rows("Bash", {"command": "make"},
                                     "Command exited with non-zero code 2.\nOutput:\nmake: *** No rule\nstop", width=80))
    assert failed[0] == "✗ exit 2 · 2 lines of output"
    assert failed[1:] == ["make: *** No rule", "stop"]
    quiet = _plain(tool_result_rows("Bash", {"command": "true"},
                                    "(Command completed with exit code 0 and no output in 0.3s)", width=80))
    assert quiet == ["✓ exit 0 · (no output)"]
    moved = _plain(tool_result_rows("Bash", {"command": "cd sub"},
                                    "(Command completed with exit code 0 and no output in 0.1s)\n(cwd: /tmp/sub)", width=80))
    assert moved == ["✓ exit 0 · (no output) · in /tmp/sub"]
    many = _plain(tool_result_rows("Bash", {"command": "ls"}, "a\nb\nc", width=80))
    assert many[0] == "✓ exit 0 · 3 lines of output"


def test_generic_json_output_is_pretty_printed_under_a_shape_summary():
    rows = _plain(tool_result_rows(MCP, MCP_ARGS, MCP_OUT, width=80))
    assert rows[0] == f"JSON object · 2 keys · {len(MCP_OUT)} B"
    assert rows[1] == "{" and rows[2] == '  "total_count": 2,'
    arr = _plain(tool_result_rows("x", {}, "[1, 2, 3]", width=80))
    assert arr[0].startswith("JSON array · 3 items")
    assert _plain(tool_result_rows("x", {}, "   ", width=80)) == ["(no output)"]


def test_generic_text_output_has_head_tail_and_the_output_hint():
    out = "\n".join(f"row {i}" for i in range(1, 31))
    rows = _plain(tool_result_rows("plugin", {"q": "x"}, out, width=80))
    assert rows[0].startswith("30 lines of output · ")
    assert rows[1:5] == ["row 1", "row 2", "row 3", "row 4"]
    assert rows[5].startswith("… +24 more lines · /output") and rows[-2:] == ["row 29", "row 30"]
    compact = _plain(tool_result_rows("plugin", {"q": "x"}, out, width=80, detail=0))
    assert len(compact) == 2 and compact[1].startswith("… +")


def test_error_block_leads_with_the_message_then_a_red_accented_body():
    rows = _plain(tool_result_rows("Read", {"path": "nope.py"}, "File not found: nope.py\nDid you mean a.py?\n",
                                   is_error=True, width=80))
    assert rows == ["error: File not found: nope.py", "▎ Did you mean a.py?"]
    long = "\n".join(["Error: boom"] + [f"frame {i}" for i in range(20)])
    rows = _plain(tool_result_rows("mcp__x__y", {}, long, is_error=True, width=80))
    assert rows[0] == "error: boom" and all(r.startswith("▎ ") for r in rows[1:])
    assert any("more lines" in r for r in rows) and rows[-1] == "▎ frame 19"      # head + tail


# ─── the live timeline, /inspect and restored sessions agree ───────────────

def _live(capsys, level: str, calls) -> str:
    r = Renderer(trace_level=level)
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1, user_prompt="go"))
    for i, (name, inp, out, err) in enumerate(calls):
        r.on_event(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id=f"c{i}", name=name, input=inp)]))
        r.on_event(ToolExecutionStart(id=f"c{i}", name=name, input=inp))
        r.on_event(ToolExecutionResult(id=f"c{i}", name=name, input=inp, content=out, is_error=err))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage(input=1, output=1)))
    return capsys.readouterr().out


CALLS = [
    ("Read", {"file_path": "src/app.py", "offset": 1, "limit": 3}, "     1→import os\n     2→\n     3→x = 1", False),
    ("Edit", {"file_path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"}, "ok", False),
    ("Bash", {"command": "pytest -q \\\n  --maxfail 1", "timeout": 120},
     "Command exited with non-zero code 1.\nOutput:\nE   assert 1 == 2\n1 failed in 0.1s", False),
    ("Grep", {"pattern": "def", "path": "src", "glob": "*.py"}, "src/a.py:1:def a():\nsrc/b.py:2:def b():", False),
    (MCP, MCP_ARGS, MCP_OUT, False),
    ("Read", {"file_path": "nope.py"}, "File not found: nope.py", True),
]


def test_live_timeline_shows_input_and_output_sections(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = strip_ansi(_live(capsys, "normal", CALLS))
    assert "● Bash  $ pytest -q \\" in out and "--maxfail 1" in out and "timeout  120" in out
    assert "⎿  ✗ exit 1 · " in out
    assert "● github › search_issues  label:bug state:open" in out
    assert "owner     acme" in out and "per_page  5" in out
    assert "⎿  JSON object · 2 keys" in out and '"total_count": 2,' in out
    assert "glob  *.py" in out
    assert "✗ error: File not found: nope.py" in out


@pytest.mark.parametrize("cols", [60, 88, 120, 160])
@pytest.mark.parametrize("level", ["compact", "normal", "detailed", "debug"])
def test_every_row_fits_the_terminal(cols, level, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(theme, "term_width", lambda: cols)
    monkeypatch.setattr(render, "term_width", lambda: cols)
    calls = CALLS + [("plugin", {"q": "y" * 300, "blob": {"k": "v" * 400}}, "z" * 500, False)]
    out = _live(capsys, level, calls)
    wide = [strip_ansi(l) for l in out.splitlines() if display_width(l) > cols - 1]
    assert not wide, wide[:3]


def test_inspect_and_restored_views_draw_calls_like_the_live_view():
    trace = pytest.importorskip("axon.agent.trace")
    t = trace.TurnTrace(turn=1, prompt="go", model="m", mode="bypass")
    t.record(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1))
    for i, (name, inp, out, err) in enumerate(CALLS[2:]):
        t.record(ToolExecutionStart(id=f"c{i}", name=name, input=inp))
        t.record(ToolExecutionResult(id=f"c{i}", name=name, input=inp, content=out, is_error=err))
    t.finish("done", "end_turn")
    text = strip_ansi(format_trace(t, "normal"))
    assert "│   ● Bash  $ pytest -q \\" in text and "│       timeout  120" in text
    assert "│     ⎿  ✗ exit 1 · " in text
    assert "│       owner     acme" in text and "│     ⎿  JSON object · 2 keys" in text
    assert "✗ error: File not found: nope.py" in text
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "m1", "name": MCP, "input": MCP_ARGS}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "m1", "content": MCP_OUT}]}]
    replay = [strip_ansi(r) for r in format_conversation_replay(msgs, width=96)]
    assert "│   ● github › search_issues  label:bug state:open" in replay
    assert "│       owner     acme" in replay and "│       per_page  5" in replay
    assert any(r.startswith("│     ⎿  JSON object · 2 keys") for r in replay)
