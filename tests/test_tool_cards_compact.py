"""The normal trace level draws each tool call as a short, phased card (choose / input / run / output),
grouped in a 'TOOLS' section between 'THINKING' and 'ANSWER'; detailed keeps the full explainer."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from axon.agent.trace import PermissionDecisionEvent, ToolRationale
from axon.providers.base import (
    LLMCallStart,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseBlock,
    ToolUseStart,
    TurnComplete,
    Usage,
)
from axon.session.store import SessionStore
from axon.ui import render, theme, tool_cards
from axon.ui.history_view import format_session_history
from axon.ui.render import Renderer, format_conversation_replay, get_tool_output, tool_output_details
from axon.ui.theme import display_width, strip_ansi

MODEL = "claude-haiku-5-5"
LS_OUT = "\n".join([f"dir{i}/" for i in range(15)] + [f"file{i}.py (1.2K)" for i in range(11)])
ECHO = 'echo "bash works: $(date)"'


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    _columns(monkeypatch, 100)


def _columns(monkeypatch, n: int) -> None:
    monkeypatch.setattr(render, "term_width", lambda: n)
    monkeypatch.setattr(theme, "term_width", lambda: n)


def _step(r: Renderer, n: int, stop: str = "tool_use", prompt: str = "test bash and ls") -> None:
    r.on_event(LLMCallStart(iteration=n, max_iterations=9, model=MODEL, message_count=n, tokens_in=3300,
                            user_prompt=prompt if n == 1 else "", mode="bypass"))
    r.on_event(TurnComplete(stop_reason=stop, usage=Usage(input=3300, output=80)))


def _calls(r: Renderer, calls: list[tuple], why: dict[str, tuple[str, str]] | None = None, finish: bool = True,
           order: list[str] | None = None, permission: str = "allow") -> None:
    """One batch: (id, name, input, output, is_error) for each call; results arrive in *order* (default: call order).
    *why* maps a call id to (sentence, source)."""
    why = why or {}
    r.on_event(ToolBatchStart(total_count=len(calls),
                              tools=[ToolUseBlock(id=c[0], name=c[1], input=c[2]) for c in calls]))
    for cid, name, inp, _out, _err in calls:
        if cid in why:
            r.on_event(ToolRationale(id=cid, name=name, text=why[cid][0], source=why[cid][1]))
        r.on_event(ToolExecutionStart(id=cid, name=name, input=inp))
        r.on_event(PermissionDecisionEvent(id=cid, tool=name, outcome=permission, reason="Bypass mode active.",
                                           source="mode"))
    if finish:
        by_id = {c[0]: c for c in calls}
        for cid in order or [c[0] for c in calls]:
            _id, name, inp, out, err = by_id[cid]
            r.on_event(ToolExecutionResult(id=cid, name=name, input={}, content=out, is_error=err))


def _bash_and_ls(level: str = "normal", **kw) -> Renderer:
    r = Renderer(trace_level=level, **kw)
    _step(r, 1)
    _calls(r, [("t1", "Bash", {"command": ECHO}, "bash works: Thu Nov 5 19:59:13 IST 2026", False),
               ("t2", "Ls", {"path": "."}, LS_OUT, False)],
           why={"t1": ("Run a harmless echo to test Bash", "model-text"),
                "t2": ("Second independent call in the same block", "model-text")})
    return r


def _out(capsys) -> str:
    return strip_ansi(capsys.readouterr().out)


def _section(out: str, start: str, stop: str) -> list[str]:
    rows = out.splitlines()
    first = next(i for i, row in enumerate(rows) if start in row)
    last = next(i for i, row in enumerate(rows) if i > first and stop in row)
    return rows[first:last + 1]


def _card_text(name: str, content: str, inp: dict | None = None, *, error: bool = False, width: int = 80,
               why: str = "", why_source: str = "model-text", allowed: str = "allowed: bypass mode is on",
               duration: float = 0.27, **extra) -> list[str]:
    call = tool_cards.CardCall(number=1, name=name, inp=inp or {}, why=why, why_source=why_source, allowed=allowed,
                               content=content, is_error=error, duration=duration, hint="/output 7", **extra)
    rows = tool_cards.card_rows(call, width)
    assert all(display_width(r) <= width for r in rows)
    return [strip_ansi(r) for r in rows]


def _fields(rows: list[str]) -> dict[str, list[str]]:
    """A card's rows by label: {'input': [...], 'run': [...], 'output': [row, row, ...]} (texts only)."""
    out: dict[str, list[str]] = {}
    current, column = "", -1
    for row in rows:
        m = re.search(r"(?<!\S)(choose|input|run|output|note) {2,}(?=\S)", row)
        if m and (column < 0 or m.start() == column):
            current, column = m.group(1), m.start()
            out.setdefault(current, []).append(row[m.end():].strip())
        elif current:
            out[current].append(row[column + 8:].strip())
    return out


# ─── the layout of a step ──────────────────────────────────────────────────

def test_a_bash_and_ls_step_is_short_and_has_the_phases_in_order(capsys):
    _bash_and_ls()
    out = _out(capsys)
    step = _section(out, "◆ Step 1", "→ 2 results")
    assert len(step) <= 16, "\n".join(step)
    first = _section(out, " 1 Bash", " 2 Ls")[:-1]
    second = _section(out, " 2 Ls", "→ 2 results")[:-1]
    for card in (first, second):
        assert len(card) == 4
        assert [re.search(r"(choose|input|run|output) {2,}", row).group(1) for row in card] == \
            ["choose", "input", "run", "output"]
    f1, f2 = _fields(first), _fields(second)
    assert f1["choose"] == ["Run a harmless echo to test Bash"] and f1["input"] == [ECHO]
    assert re.fullmatch(r"✓ \d\.\d\ds", f1["run"][0]) and f1["output"] == ["bash works: Thu Nov 5 19:59:13 IST 2026"]
    assert re.fullmatch(r"✓ [\d.]+s · 15 dirs, 11 files", f2["run"][0])
    assert re.search(r"dir0/  dir1/ .*… \+\d+ more · /output \d+$", f2["output"][0])      # Ls: one row of names
    assert "▣ TOOLS · 2 calls · one after another · allowed: bypass mode" in out          # said once, here
    assert "allowed (bypass mode)" not in out and out.count("bypass mode") == 2           # (the turn header, the section)
    assert re.search(r"→ 2 results \(\d+ B, ~\d+ tokens\) go to step 2", out)
    assert out.count("go to step 2") == 1 and "goes to the model" not in out


def test_the_old_explainer_rows_are_gone_at_normal_level(capsys):
    _bash_and_ls()
    out = _out(capsys)
    for gone in ("chosen by", "from 25 tools", "call 1 of", "model thought", "model said", "exact call",
                 "exact result sent to the model", "runs 2 tools", "allowed: bypass mode is on",
                 "goes to the model in step", "LLM call", "reads 1 message"):
        assert gone not in out, gone


def test_step_header_is_short_and_the_model_is_named_once_per_step(capsys):
    r = _bash_and_ls()
    _step(r, 2)
    _calls(r, [("t3", "Grep", {"pattern": "def main", "path": "src"}, "src/a.py:1:def main():", False)])
    out = _out(capsys)
    assert "◆ Step 1 · claude-haiku-5-5 · 1 message · ~3.3k tokens" in out
    named = [row for row in out.splitlines() if MODEL in row]
    assert [row[:8] for row in named] == ["┌ Turn 1", "│ ◆ Step", "│ ◆ Step"]
    assert not [row for row in out.splitlines() if row.startswith("│  ") and MODEL in row]


def test_thinking_tools_and_answer_sections_come_in_that_order_with_air_between(capsys):
    r = Renderer()
    _step(r, 1)
    r.on_event(ThinkingDelta("The user wants Bash tested. I will run an echo."))
    _calls(r, [("t1", "Bash", {"command": "echo hi"}, "hi", False)])
    r.on_event(LLMCallStart(iteration=2, max_iterations=9, model=MODEL, message_count=3, tokens_in=3900))
    r.on_event(TextDelta("Bash works.\n"))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage(input=3900, output=20)))
    out = _out(capsys)
    assert out.index("✻ THINKING") < out.index("▣ TOOLS · 1 call") < out.index("▍ ANSWER") < out.index("▎ Bash works.")
    assert "THINKING  0.1s · The user wants Bash tested. I will run an echo." in out
    assert "I will run an echo.\n│ \n│ ▣ TOOLS" in out                       # one blank row before the TOOLS block
    assert out.count("\n│ \n") == 2                                          # ... and one between the steps
    assert "▣ TOOLS · 1 call · allowed: bypass mode\n│" in out
    assert "→ 1 result (" in out


def test_thinking_is_one_short_line_at_normal_and_the_whole_text_at_detailed(capsys):
    thought = "word " * 60                                # 300 characters
    r = Renderer(show_thinking=True)
    _step(r, 1)
    r.on_event(ThinkingDelta(thought))
    r.on_event(TextDelta("done\n"))
    out = _out(capsys)
    line = next(row for row in out.splitlines() if "THINKING" in row)
    assert line.endswith("…") and len(line) < 150 and "thought for" not in out
    assert out.count("word") <= 21                      # about the first 100 characters
    r = Renderer(trace_level="detailed", show_thinking=True)
    _step(r, 1)
    r.on_event(ThinkingDelta(thought))
    r.on_event(TextDelta("done\n"))
    detailed = _out(capsys)
    assert "Thinking" in detailed and "thought for" in detailed and detailed.count("word") >= 60


def test_parallel_batches_say_so_in_the_section_header_and_print_in_call_order(capsys):
    r = Renderer()
    _step(r, 1)
    _calls(r, [("a", "Read", {"path": "a.py"}, "     1→x = 1", False),
               ("b", "Glob", {"pattern": "*.py"}, "a.py\nb.py", False)], order=["b", "a"])
    out = _out(capsys)
    assert "▣ TOOLS · 2 calls · in parallel" in out
    assert out.index(" 1 Read") < out.index(" 2 Glob")                 # the model's order, not the finishing order
    sequential = Renderer()
    _step(sequential, 1)
    _calls(sequential, [("a", "Bash", {"command": "ls"}, "a", False), ("b", "Bash", {"command": "pwd"}, "/", False)])
    assert "▣ TOOLS · 2 calls · one after another" in _out(capsys)


def test_the_call_number_is_shown_only_when_the_step_has_several_calls(capsys):
    r = Renderer()
    _step(r, 1)
    _calls(r, [("a", "Bash", {"command": "ls"}, "a", False)])
    one = _out(capsys)
    assert re.search(r"│ {3}Bash +input", one) and not re.search(r"│ +1 Bash", one)
    _step(r, 2)
    _calls(r, [("b", "Bash", {"command": "ls"}, "a", False), ("c", "Bash", {"command": "pwd"}, "/", False)])
    two = _out(capsys)
    assert re.search(r"│ +1 Bash", two) and re.search(r"│ +2 Bash", two) and "①" not in two


def test_no_blank_rows_between_cards_and_one_between_steps(capsys):
    r = _bash_and_ls()
    _step(r, 2)
    _calls(r, [("t3", "Bash", {"command": "pwd"}, "/tmp", False)])
    out = _out(capsys)
    rows = out.splitlines()
    first, last = next(i for i, x in enumerate(rows) if " 1 Bash" in x), next(i for i, x in enumerate(rows) if "→ 2 results" in x)
    assert all(row.strip("│ ") for row in rows[first:last])
    assert sum(1 for row in rows if row.strip() == "│") == 1


# ─── the input phase ───────────────────────────────────────────────────────

def test_a_shell_command_is_shown_exactly_with_all_its_lines():
    script = "set -e\ncd src && python - <<'EOF'\nprint('hi')\nEOF\nls -la"
    fields = _fields(_card_text("Bash", "hi", {"command": script, "description": "Run it", "timeout": 60}, width=90))
    assert fields["input"] == ["set -e", "cd src && python - <<'EOF'", "print('hi')", "EOF", "ls -la"]
    long = "echo " + "word " * 40
    rows = _fields(_card_text("Bash", "x", {"command": long}, width=70))["input"]
    assert 1 < len(rows) <= 6 and " ".join(rows) == long.strip()           # wrapped at spaces, nothing lost
    many = "\n".join(f"echo {i}" for i in range(12))
    rows = _fields(_card_text("Bash", "x", {"command": many}, width=90))["input"]
    assert rows[0] == "echo 0" and rows[-1] == "… +7 more lines · /output 7" and len(rows) == 6


def _slashes(rows):
    """Windows shows the path separator it uses; the cards are otherwise identical."""
    return [r.replace("\\", "/") for r in rows]


def test_paths_are_shown_relative_to_the_workspace_and_home_as_tilde(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    home = os.path.expanduser("~")
    inp = {"file_path": f"{tmp_path}/src/axon/registry.py", "offset": 1, "limit": 40}
    assert _slashes(_fields(_card_text("Read", "     1→x", inp))["input"]) == ["src/axon/registry.py · lines 1-40"]
    grep = _fields(_card_text("Grep", f"{tmp_path}/src/a.py:3:x", {"pattern": "x", "path": f"{tmp_path}/src"}))
    assert _slashes(grep["input"]) == ['"x" in src'] and _slashes(grep["output"]) == ["src/a.py:3  x"]
    other = _fields(_card_text("Read", "     1→x", {"file_path": f"{home}/notes/todo.md"}))
    assert _slashes(other["input"]) == ["~/notes/todo.md"]
    assert tool_cards.tidy_paths(f"cat {tmp_path}/a.txt {home}/b.txt").replace("\\", "/") == "cat a.txt ~/b.txt"


def test_default_options_are_dropped_and_real_ones_are_dim_extras():
    inp = {"pattern": "def f", "path": "src", "output_mode": "content", "head_limit": 60, "-A": 3, "-n": True,
           "glob": "*.py", "-i": True, "type": "py"}
    assert tool_cards.input_options("Grep", inp) == ["glob=*.py", "-i"]            # at most two, no boilerplate
    card = _fields(_card_text("Grep", "src/a.py:1:def f", inp, width=100))
    assert card["input"] == ['"def f" in src  glob=*.py  -i']
    narrow = _fields(_card_text("Grep", "src/a.py:1:def f", inp, width=70))
    assert narrow["input"] == ['"def f" in src']                                    # too narrow: the options go first
    own_row = _fields(_card_text("Grep", "x", {"pattern": "p" * 40, "path": "a/" * 12, "glob": "*.py"}, width=90))
    assert own_row["input"][-1] == "glob=*.py" and len(own_row["input"]) >= 2          # no mid-path wrap: its own row


def test_choose_is_shown_only_for_a_real_reason():
    assert "choose" not in _fields(_card_text("Grep", "x", {"pattern": "x"}))
    assert "choose" not in _fields(_card_text("Grep", "x", {"pattern": "x"}, why="read-only lookup", why_source="heuristic"))
    same = _card_text("Bash", "x", {"command": "ls", "description": "List files"}, why="List files.")
    assert "choose" not in _fields(same)                                                # it only repeats the description
    real = _fields(_card_text("Grep", "x", {"pattern": "x"}, why="Find the handler.", why_source="model-text"))
    assert real["choose"] == ["Find the handler."]
    assert len(_card_text("Grep", "x", {"pattern": "x"})) == 3                          # input, run, output


# ─── the run phase ─────────────────────────────────────────────────────────

def test_permission_is_said_in_the_header_and_on_a_card_only_when_it_differs(capsys):
    r = Renderer()
    _step(r, 1)
    r.on_event(ToolBatchStart(total_count=3, tools=[ToolUseBlock(id=i, name="Bash", input={"command": c})
                                                    for i, c in (("a", "ls"), ("b", "pwd"), ("c", "rm x"))]))
    for cid, outcome, source, reason in (("a", "allow", "mode", "Bypass mode active."), ("b", "allow", "mode", "Bypass mode active."),
                                         ("c", "deny", "rule", "Bash(rm:*)")):
        r.on_event(ToolExecutionStart(id=cid, name="Bash", input={"command": "x"}))
        r.on_event(PermissionDecisionEvent(id=cid, tool="Bash", outcome=outcome, reason=reason, source=source))
    for cid, err in (("a", False), ("b", False), ("c", True)):
        r.on_event(ToolExecutionResult(id=cid, name="Bash", input={}, content="Denied" if err else "ok", is_error=err))
    out = _out(capsys)
    assert "▣ TOOLS · 3 calls · one after another · allowed: bypass mode" in out
    runs = [row for row in out.splitlines() if re.search(r"(?<!\S)run {2,}", row)]
    assert len(runs) == 3 and "allowed" not in runs[0] + runs[1]
    assert "✗ blocked" in runs[2] and "denied (rule)" in runs[2]


def test_a_failing_shell_call_shows_the_exit_code_and_its_first_error_line(monkeypatch):
    rows = _card_text("Bash", "Command exited with non-zero code 1.\nOutput:\ncat: nofile.txt: No such file or directory\n"
                              "second line", {"command": "cat nofile.txt"}, error=True, duration=0.01)
    f = _fields(rows)
    assert f["run"] == ["✗ failed · exit 1 · 0.01s · allowed (bypass mode)"]
    assert f["output"][0] == "cat: nofile.txt: No such file or directory" and "more lines · /output 7" in f["output"][1]
    tool_error = _fields(_card_text("Read", "File not found: nope.py\nDid you mean a.py?", {"path": "nope.py"}, error=True))
    assert tool_error["run"][0].startswith("✗ failed") and tool_error["output"][0] == "File not found: nope.py"
    monkeypatch.setattr(tool_cards, "ERROR", "<red>")
    monkeypatch.setattr(tool_cards, "RST", "</red>")
    colored = tool_cards.card_rows(tool_cards.CardCall(number=1, name="Bash", inp={"command": "x"}, is_error=True,
                                                       content="Command exited with non-zero code 2.\nboom"), 80)
    assert any("<red>boom" in row for row in colored)                       # the error line is coloured


def test_a_denied_call_says_it_was_blocked():
    f = _fields(_card_text("Bash", "Denied", {"command": "rm -rf x"}, error=True, allowed="denied by rule: Bash(rm:*)"))
    assert f["run"] == ["✗ blocked · 0.27s · denied (rule)"]


def test_an_empty_result_is_said_on_the_run_line_not_as_an_empty_output_row():
    for name, inp, content, note in (("Grep", {"pattern": "x"}, "No matches found for pattern 'x'.", "no matches"),
                                     ("Glob", {"pattern": "*.zz"}, "No files matched", "no files matched"),
                                     ("Ls", {"path": "x"}, "(Empty directory)", "empty directory"),
                                     ("Bash", {"command": "true"}, "(Command completed with exit code 0 and no output in 0.1s)",
                                      "no output")):
        f = _fields(_card_text(name, content, inp, allowed=""))
        assert "output" not in f and f["run"][0].endswith(note), (name, f)
        assert f["run"][0].count(note) == 1


# ─── the output phase, per tool ────────────────────────────────────────────

def test_output_is_at_most_three_rows_and_ends_with_the_output_pointer():
    out = _fields(_card_text("Bash", "\n".join(f"line {i}" for i in range(1, 21)), {"command": "seq 20"}))["output"]
    assert out == ["line 1", "line 2", "… +18 more lines · /output 7"]
    short = _fields(_card_text("Bash", "a\nb\nc", {"command": "ls"}))["output"]
    assert short == ["a", "b", "c"]                                                   # fits: no pointer


def test_ls_and_glob_are_one_line_of_names_with_a_count_of_the_rest():
    ls = _fields(_card_text("Ls", LS_OUT + "\n\n(15 directories, 11 files)", {"path": "."}))
    assert len(ls["output"]) == 1 and ls["output"][0].startswith("dir0/  dir1/")
    assert re.search(r"… \+\d+ more · /output 7$", ls["output"][0]) and "(15 directories" not in ls["output"][0]
    assert "(1.2K)" not in ls["output"][0] and ls["run"][0].startswith("✓ 0.27s · 15 dirs, 11 files")
    glob = _fields(_card_text("Glob", "\n".join(f"src/m{i}.py" for i in range(40)), {"pattern": "**/*.py"}))
    assert len(glob["output"]) == 1 and glob["output"][0].startswith("src/m0.py  src/m1.py")
    assert "more · /output 7" in glob["output"][0]


def test_grep_shows_matches_with_file_and_line():
    hits = "\n".join(f"src/f{i}.py:{i + 10}:def f{i}():" for i in range(8))
    out = _fields(_card_text("Grep", hits, {"pattern": "def f", "path": "src"}))["output"]
    assert out == ["src/f0.py:10  def f0():", "src/f1.py:11  def f1():", "… +6 more matches · /output 7"]
    assert _fields(_card_text("Grep", "src/a.py:3:x", {"pattern": "x"}))["output"] == ["src/a.py:3  x"]


def test_read_shows_a_couple_of_numbered_lines_and_the_pointer():
    body = "\n".join(f"{i:>6}→line {i}" for i in range(10, 60))
    f = _fields(_card_text("Read", body, {"path": "src/app.py", "offset": 10, "limit": 50}))
    assert f["input"] == ["src/app.py · lines 10-59"]                        # the path and the range are the input
    assert f["output"] == ["10  line 10", "11  line 11", "… +48 more lines · /output 7"]


def test_web_search_lists_result_titles_one_per_row():
    content = "\n".join(f"{i}. Title number {i}\n   https://example.com/{i}\n   snippet" for i in range(1, 7))
    out = _fields(_card_text("WebSearch", "Search results:\n\n" + content, {"query": "tool use"}))["output"]
    assert out == ["Title number 1", "Title number 2", "… +4 more results · /output 7"]


def test_edit_keeps_its_diff_but_collapses_a_long_one():
    old = "\n".join(f"old {i}" for i in range(30))
    new = "\n".join(f"new {i}" for i in range(30))
    out = _fields(_card_text("Edit", "ok", {"path": "f.py", "old_string": old, "new_string": new}, width=90))["output"]
    assert out[0] == "✎ Edited f.py · +30 −30"
    assert len(out) == 1 + tool_cards.DIFF_BODY_ROWS + 1
    assert sum(1 for row in out if re.match(r"^(\d+ )?- old", row)) == 4 and sum(1 for row in out if re.match(r"^(\d+ )?\+ new", row)) == 4
    assert out[-1].startswith("… +") and "more lines · /output 7" in out[-1]
    small = _fields(_card_text("Edit", "ok", {"path": "f.py", "old_string": "a", "new_string": "b"}, width=90))["output"]
    assert not any("more lines" in row for row in small)


# ─── widths ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cols", [50, 60, 80, 100, 120])
def test_every_row_fits_the_terminal_without_breaking_words(cols, capsys, monkeypatch):
    _columns(monkeypatch, cols)
    r = Renderer()
    _step(r, 1)
    r.on_event(ThinkingDelta("A long thought " * 20))
    _calls(r, [("t1", "Bash", {"command": "echo " + "very-long-argument " * 12}, "x" * 300, False),
               ("t2", "mcp__github__search_issues", {"query": "label:bug state:open", "owner": "acme"},
                json.dumps({"total_count": 2, "items": [{"n": 1}, {"n": 2}]}), False),
               ("t3", "Bash", {"command": "false"}, "Command exited with non-zero code 1.\nOutput:\n" + "e" * 200, True)],
           why={"t1": ("A very long sentence the model wrote to explain why it is calling this tool " * 3, "model-text")})
    _step(r, 2)
    r.on_event(TextDelta("Answer " * 30 + "\n"))
    out = _out(capsys)
    too_wide = [row for row in out.splitlines() if display_width(row) > cols - 1]
    assert not too_wide, too_wide[:3]
    assert " 1 Bash" in out and " 2 github" in out and " 3 Bash" in out
    assert len(_section(out, " 1 Bash", "→ 3 results")) <= 30
    assert "very-long-argu\n" not in out                     # a long word is cut with an ellipsis, never split in two


def test_a_narrow_window_drops_options_then_the_choose_line_then_shrinks_the_preview():
    inp = {"pattern": "def f", "path": "src", "glob": "*.py"}
    hits = "\n".join(f"src/f{i}.py:{i}:def f{i}():" for i in range(8))
    wide = _fields(_card_text("Grep", hits, inp, width=100, why="Find the definitions."))
    assert "choose" in wide and "glob=*.py" in wide["input"][0] and len(wide["output"]) == 3
    mid = _fields(_card_text("Grep", hits, inp, width=70, why="Find the definitions."))
    assert "choose" in mid and "glob" not in " ".join(mid["input"])                # options go first
    narrow = _fields(_card_text("Grep", hits, inp, width=56, why="Find the definitions."))
    assert "choose" not in narrow and len(narrow["output"]) == 3                      # then the choose line
    tiny = _fields(_card_text("Grep", hits, inp, width=44, why="Find the definitions."))
    assert len(tiny["output"]) == 1 and "/output 7" in tiny["output"][0]              # then the preview: one row


def test_labels_stay_aligned_inside_a_card():
    rows = _card_text("Bash", "ok", {"command": "echo " + "word " * 30}, width=60, why="why " * 5)
    column = next(r.index("input") for r in rows if "input" in r)
    for row in rows:
        for label in ("run", "output"):
            if re.search(rf"(?<!\S){label} {{2,}}", row):
                assert row.index(label) == column


# ─── levels ────────────────────────────────────────────────────────────────

def test_detailed_level_keeps_the_full_explainer(capsys):
    r = Renderer(trace_level="detailed")
    _step(r, 1)
    r.on_event(LLMCallStart(iteration=2, max_iterations=9, model=MODEL, message_count=2, tokens_in=100))
    r.on_event(TurnComplete(stop_reason="tool_use", usage=Usage(input=1, output=1)))
    _calls(r, [("t1", "Bash", {"command": ECHO}, "bash works", False),
               ("t2", "Ls", {"path": "."}, LS_OUT, False)],
           why={"t1": ("Run a harmless echo to test Bash", "model-text")})
    out = _out(capsys)
    assert "◆ Step 2  LLM call · claude-haiku-5-5 · reads 2 messages" in out
    assert "chosen by claude-haiku-5-5 in step 2 · call 1" in out
    assert 'model said: "Run a harmless echo to test Bash"' in out
    assert "runs a shell command" in out and "allowed: bypass mode is on" in out
    assert "exact call · Bash" in out and "exact result sent to the model (26 lines)" in out
    assert "result #1 [t1] (" in out and "goes to the model in step 3" in out
    assert "▣ TOOLS" not in out and "choose" not in out


def test_compact_level_is_still_one_line_per_call(capsys):
    _bash_and_ls("compact")
    out = _out(capsys)
    assert "● Bash(" in out and "● Ls(" in out
    for absent in ("▣ TOOLS", "choose", "THINKING", "chosen by", "exact call", "→ 2 results"):
        assert absent not in out


# ─── /output K and the live phase ──────────────────────────────────────────

def test_output_details_give_the_full_explainer_of_one_call(capsys):
    r = Renderer()
    _step(r, 1)
    r.on_event(LLMCallStart(iteration=2, max_iterations=9, model=MODEL, message_count=2, tokens_in=100))
    _calls(r, [("t1", "Bash", {"command": ECHO}, "bash works", False)],
           why={"t1": ("Run a harmless echo to test Bash", "model-text")})
    _out(capsys)
    last = max(render._TOOL_CALLS)
    details = "\n".join(strip_ansi(row) for row in tool_output_details(last, 100))
    assert "chosen by claude-haiku-5-5 in step 2" in details and 'model said: "Run a harmless echo to test Bash"' in details
    assert "allowed: bypass mode is on" in details
    assert "exact call · Bash" in details and 'echo \\"bash works' in details
    assert "goes to the model in step 3" in details and "took " in details
    assert get_tool_output(last)[2] == "bash works"
    assert tool_output_details(10_000_000) == []


def test_live_phase_follows_the_model_from_choosing_to_running():
    r = Renderer()
    _step(r, 1)
    assert r.tool_phase["phase"] == "idle"
    r.on_event(ToolUseStart(id="t1", name="Bash"))
    assert r.tool_phase["phase"] == "selected" and r.tool_phase["tool"] == "Bash"
    r.on_event(ToolArgsDelta(id="t1", fragment='{"command": "ec'))
    r.on_event(ToolArgsDelta(id="t1", fragment='ho hi"}'))
    assert r.tool_phase["phase"] == "input" and r.tool_phase["args"] == '{"command": "echo hi"}'
    r.on_event(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})]))
    r.on_event(ToolExecutionStart(id="t1", name="Bash", input={"command": "echo hi"}))
    assert r.tool_phase["phase"] == "running" and r.tool_phase["tool"] == "Bash"
    r.on_event(ToolExecutionResult(id="t1", name="Bash", input={}, content="hi"))
    assert r.tool_phase["phase"] == "idle"
    r.on_event(ToolUseStart(id="t2", name=""))
    assert r.tool_phase["phase"] == "choosing"


def test_a_call_that_never_finished_still_gets_a_card(capsys):
    r = Renderer()
    _step(r, 1)
    _calls(r, [("t1", "Bash", {"command": "sleep 100"}, "", False)], finish=False)
    r.on_event(LLMCallStart(iteration=2, max_iterations=9, model=MODEL, message_count=3, tokens_in=10))
    out = _out(capsys)
    assert "Bash" in out and "stopped before it finished" in out


# ─── restored sessions draw the same cards ─────────────────────────────────

def _history(tmp_path: Path, trace_level: str = "normal") -> str:
    store = SessionStore(tmp_path, session_dir=tmp_path / "sessions")
    sid = store.open(None)
    store.append("user_message", {"role": "user", "content": "test bash and ls"})
    store.append("assistant_turn", {
        "stop_reason": "tool_use", "text": "Run a harmless echo to test Bash.", "thinking": "Let me test both tools.",
        "tool_uses": [{"id": "t1", "name": "Bash", "input": {"command": ECHO}},
                      {"id": "t2", "name": "Ls", "input": {"path": "."}}],
        "model": MODEL, "usage": {"input": 100, "output": 10}, "thinking_s": [0.1],
        "call": {"provider": "Anthropic", "message_count": 1, "tool_count": 25, "tokens_in": 3300}})
    for tid in ("t1", "t2"):
        store.append("permission", {"tool_use_id": tid, "tool": "x", "outcome": "allow", "reason": "Bypass mode active.",
                                    "source": "mode", "approval": None})
    store.append("tool_results", [{"tool_use_id": "t1", "content": "bash works: Thu Nov 5 19:59:13 IST 2026",
                                   "is_error": False, "duration_s": 0.27},
                                  {"tool_use_id": "t2", "content": LS_OUT, "is_error": False, "duration_s": 0.0}])
    store.append("assistant_turn", {"stop_reason": "end_turn", "text": "Both worked.", "thinking": "", "tool_uses": [],
                                    "model": MODEL, "usage": {"input": 200, "output": 5}, "call": {"message_count": 3}})
    return "\n".join(strip_ansi(r) for r in format_session_history(Path(store._file(sid)), width=100,
                                                                    trace_level=trace_level))


def test_the_restored_view_draws_the_same_cards_as_the_live_view(tmp_path, capsys):
    history = _history(tmp_path)
    _bash_and_ls()
    live = _out(capsys)

    def labels(text: str) -> list[tuple[str, str]]:
        """(label, text) of each row of the TOOLS block with times, sizes and pointers blanked out."""
        rows = _section(text, "▣ TOOLS", "→ 2 results")[1:-1]
        found = []
        for row in rows:
            m = re.search(r"(?<!\S)(choose|input|run|output) {2,}(.*)$", row)
            if m:
                found.append((m.group(1), re.sub(r"[\d.]+s|/output \d+|/transcript", "X", m.group(2))))
        return found

    def no_choose(text):     # (the stored reasons differ)
        return [label for label, _ in labels(text) if label != "choose"]
    assert no_choose(history) == no_choose(live)
    assert [t for k, t in labels(history) if k == "input"] == [t for k, t in labels(live) if k == "input"]
    assert [t for k, t in labels(history) if k == "output"] == [t for k, t in labels(live) if k == "output"]
    assert "▣ TOOLS · 2 calls · one after another · allowed: bypass mode" in history
    assert "◆ Step 1 · claude-haiku-5-5 · 1 message · ~3.3k tokens" in history
    assert "┌ Turn 1" in history and "claude-haiku-5-5 via Anthropic" in history      # model + provider, once
    assert history.index("✻ THINKING") < history.index("▍ ANSWER") < history.index("▣ TOOLS")
    assert "chosen by" not in history and "exact call" not in history and "LLM call ·" not in history
    cards = "\n".join(_section(history, "▣ TOOLS", "→ 2 results"))
    assert "claude-haiku-5-5" not in cards and "Anthropic" not in cards


def test_the_restored_view_at_detailed_level_keeps_the_explainer(tmp_path):
    history = _history(tmp_path, "detailed")
    assert "chosen by claude-haiku-5-5 (Anthropic) in step 1 · call 1 of 2 · from 25 tools" in history
    assert "exact call · Bash" in history and "exact result sent to the model" in history
    assert "▣ TOOLS" not in history and "LLM call" in history


def test_message_replay_uses_cards_at_normal_and_the_explainer_at_detailed():
    msgs = [{"role": "user", "content": "list"},
            {"role": "assistant", "content": [{"type": "text", "text": "I will list the folder."},
                                              {"type": "tool_use", "id": "t1", "name": "Ls", "input": {"path": "."}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": LS_OUT}]}]
    normal = "\n".join(strip_ansi(r) for r in format_conversation_replay(msgs, width=90))
    assert "▣ TOOLS · 1 call" in normal and "Ls  " in normal and "choose  I will list the folder." in normal
    assert "→ 1 result (" in normal and "①" not in normal
    detailed = "\n".join(strip_ansi(r) for r in format_conversation_replay(msgs, width=90, detail=2))
    assert "● Ls" in detailed and "exact call · Ls" in detailed and "▣ TOOLS" not in detailed
