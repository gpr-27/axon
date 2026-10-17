"""Result cards: created-file / diff / grep / list / JSON / test-summary cards, markdown code cards and
charts, ASCII / NO_COLOR fallbacks, width compliance, clickable result rows and provider labels."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from axon.providers.base import (LLMCallStart, ToolExecutionResult, ToolExecutionStart, TurnComplete, Usage)
from axon.ui import cards, charts, render, theme
from axon.ui.markdown import format_markdown, render_table
from axon.ui.render import Renderer, tool_result_rows
from axon.ui.theme import display_width, strip_ansi

PY = "\n".join([f"def f{i}(x):" if i % 3 == 0 else f"    return x + {i}  # step {i}" for i in range(45)])


def _plain(rows: list[str]) -> list[str]:
    return [strip_ansi(r) for r in rows]


@pytest.fixture
def colors(monkeypatch):
    """A truecolor terminal for the duration of the test (the colourless palette is restored after)."""
    monkeypatch.setattr(theme, "COLOR_ENABLED", True)
    monkeypatch.setattr(theme, "COLOR_LEVEL", 2)
    theme.set_theme("dark")
    yield
    monkeypatch.setattr(theme, "COLOR_ENABLED", False)
    theme.set_theme("dark")


# ─── code card (Write) ─────────────────────────────────────────────────────

def test_created_file_card_header_gutter_and_more_hint(monkeypatch):
    monkeypatch.setattr(render, "_MORE_HINT", "/output 7")
    rows = _plain(tool_result_rows("Write", {"path": "src/shop/cart.py", "content": PY},
                                   "Successfully wrote 1 characters (45 lines) to src/shop/cart.py. Created file.", width=80))
    assert rows[0].startswith("✚ Created src/shop/cart.py · +45 · ■ Python · 45 lines · ")
    assert rows[0].endswith("KB") or rows[0].endswith(" B")
    assert rows[1].split() == ["1", "+", "def", "f0(x):"] and rows[2].split()[:4] == ["2", "+", "return", "x"]
    assert rows[30].split()[:2] == ["30", "+"]
    assert rows[31].strip() == "… +15 more rows · /output 7 to view all"
    assert len(rows) == 32
    detailed = tool_result_rows("Write", {"path": "a.py", "content": PY}, "Created file.", width=80, detail=2)
    assert len(detailed) == 46                     # detailed: the whole (short) file
    compact = tool_result_rows("Write", {"path": "a.py", "content": PY}, "Created file.", width=80, detail=0)
    assert len(compact) == 1 and "Created a.py" in strip_ansi(compact[0])


def test_overwrite_shows_a_diff_against_the_old_content(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    old = "a = 1\nb = 2\nc = 3\n"
    (tmp_path / "m.py").write_text(old, encoding="utf-8")
    r = Renderer(trace_level="normal")
    new = "a = 1\nb = 20\nc = 3\n"
    r.on_event(ToolExecutionStart(id="w", name="Write", input={"path": "m.py", "content": new}))
    (tmp_path / "m.py").write_text(new, encoding="utf-8")        # the tool runs between start and result
    r.on_event(ToolExecutionResult(id="w", name="Write", input={}, content="Successfully wrote ... Overwrote file."))
    out = strip_ansi(capsys.readouterr().out)
    assert "⟳ Overwrote m.py · +1 −1" in out
    assert "2 - b = 2" in out and "2 + b = 20" in out
    # no old content known (restored history): labelled, shown as a code card
    rows = _plain(tool_result_rows("Write", {"path": "m.py", "content": new}, "Overwrote file.", width=80))
    assert rows[0].startswith("⟳ Overwrote m.py · ■ Python · 3 lines")


# ─── diff card (Edit / MultiEdit / Patch) ──────────────────────────────────

def test_edit_card_header_and_line_numbers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\ny = 2\nreturn_value = compute(x, y)\n", encoding="utf-8")
    rows = _plain(tool_result_rows("Edit", {"path": "app.py", "old_string": "return_value = calc(x)",
                                            "new_string": "return_value = compute(x, y)"}, "ok", width=80))
    assert rows[0] == "✎ Edited app.py · +1 −1"
    assert rows[1].strip() == "3 - return_value = calc(x)"
    assert rows[2].strip() == "3 + return_value = compute(x, y)"


def test_diff_intra_line_word_highlights(colors):
    rows = cards.diff_rows("total = price * qty\n", "total = price * quantity\n", 10, 80, "python")
    del_bg, add_bg = theme.DIFF_DEL_WORD_BG, theme.DIFF_ADD_WORD_BG
    assert del_bg and add_bg
    minus = next(r for r in rows if " - " in strip_ansi(r))
    plus = next(r for r in rows if " + " in strip_ansi(r))
    # only the changed word sits on the strong tint
    assert re.search(re.escape(del_bg) + r"(?:\x1b\[[0-9;]*m)*qty", minus)
    assert re.search(re.escape(add_bg) + r"(?:\x1b\[[0-9;]*m)*quantity", plus)
    assert not re.search(re.escape(add_bg) + r"(?:\x1b\[[0-9;]*m)*total", plus)
    assert strip_ansi(minus).startswith(" 10 - total") and strip_ansi(plus).startswith(" 10 + total")


def test_diff_hunks_and_wrapping_keep_the_gutter():
    old = "\n".join(f"line {i}" for i in range(1, 40))
    new = old.replace("line 3\n", "line three\n").replace("line 30", "line thirty " + "x" * 80)
    rows = _plain(cards.diff_rows(old, new, 1, 60, ""))
    assert any("┈┈┈ line" in r for r in rows)                       # hunk separator with its line number
    assert all(display_width(r) <= 60 for r in rows)
    wrapped = [r for r in rows if r.lstrip().startswith("↪")]
    assert wrapped, rows                                             # long + line wrapped under the gutter


def test_patch_card_uses_hunk_line_numbers():
    patch = ("--- a/src/x.py\n+++ b/src/x.py\n@@ -10,3 +10,3 @@\n keep\n-old value\n+new value\n keep2\n")
    rows = _plain(tool_result_rows("Patch", {"patch": patch}, "Successfully applied patch (1 hunk): src/x.py.", width=80))
    assert rows[0] == "✎ Patched src/x.py · +1 −1 · 1 hunk"
    assert any(r.strip() == "11 - old value" for r in rows) and any(r.strip() == "11 + new value" for r in rows)


# ─── grep / lists / json ───────────────────────────────────────────────────

def test_grep_grouped_by_file_with_line_numbers(colors):
    out = "src/a.py:3:def handler(x):\nsrc/a.py:9:    handler(1)\nsrc/b.py:12:handler = None"
    hits, files, rows = cards.grep_rows(out, "handler", 80)
    assert (hits, files) == (3, 2)
    plain = _plain(rows)
    assert plain[0] == "src/a.py · 2 matches"
    assert plain[1].strip() == "3: def handler(x):" and plain[2].strip() == "9:     handler(1)"
    assert plain[3] == "src/b.py · 1 match" and plain[4].strip() == "12: handler = None"
    assert theme.MATCH_BG + theme.WARNING + theme.BOLD + "handler" in rows[1]   # the match itself is marked


def test_grep_summary_row_and_more_hint(monkeypatch):
    monkeypatch.setattr(render, "_MORE_HINT", "/output 3")
    out = "\n".join(f"f{i % 4}.py:{i}:hit {i}" for i in range(1, 40))
    rows = _plain(tool_result_rows("Grep", {"pattern": "hit"}, out, width=80))
    assert rows[0].startswith("39 matches in 4 files")
    assert rows[-1].startswith("… +") and rows[-1].endswith("/output 3 to view all")


def test_glob_compact_columns_grouped_by_directory():
    paths = "\n".join([f"src/pkg/m{i}.py" for i in range(9)] + ["setup.py"])
    rows = _plain(tool_result_rows("Glob", {"pattern": "**/*.py"}, paths, width=80))
    assert rows[0].startswith("10 files") or rows[0].startswith("Found 10 files")
    assert any(r.startswith("▸ src/pkg/") for r in rows) and any("m0.py" in r and "m1.py" in r for r in rows)
    assert all(display_width(r) <= 80 for r in rows)


def test_json_output_pretty_printed():
    body = 'URL: https://x\nHTTP 200 · application/json · 20 chars\n\n{"a": 1, "b": [true, null]}'
    rows = _plain(tool_result_rows("WebFetch", {"url": "https://x"}, body, width=80))
    assert '  "a": 1,' in rows and '  "b": [' in rows


# ─── test-runner summaries ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,runner,passed,failed", [
    ("..F\nFAILED t.py::x\n==== 1 failed, 2 passed, 1 skipped in 0.52s ====", "pytest", 2, 1),
    ("14 passed in 0.1s", "pytest", 14, 0),
    ("Tests:       2 failed, 10 passed, 12 total", "jest", 10, 2),
    (" Tests  1 failed | 7 passed (8)", "vitest", 7, 1),
    ("  10 passing (2s)\n  2 failing", "mocha", 10, 2),
    ("test result: FAILED. 8 passed; 2 failed; 0 ignored; 0 measured", "cargo test", 8, 2),
    ("--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\nFAIL\tpkg\t0.01s", "go test", 1, 1),
    ("Ran 12 tests in 0.01s\n\nFAILED (failures=2, errors=1)", "unittest", 9, 2),
])
def test_test_summary_detection(text, runner, passed, failed):
    s = cards.test_summary(text)
    assert s and s["runner"] == runner and s["passed"] == passed and s["failed"] == failed


def test_no_false_test_summary():
    assert cards.test_summary("compiled 3 files\nall good") is None
    assert cards.test_summary("") is None


def test_bash_summary_row_has_pass_fail_bar():
    content = "Command exited with non-zero code 1.\nOutput:\n..F.F...\nFAILED t.py::a\nFAILED t.py::b\n2 failed, 6 passed in 0.21s"
    rows = _plain(tool_result_rows("Bash", {"command": "pytest"}, content, width=80))
    assert rows[0].startswith("█████████▒▒▒ 6 passed · 2 failed · exit code 1")
    one = _plain(tool_result_rows("Bash", {"command": "pytest"}, "5 passed in 0.19s", width=80))
    assert one == ["████████████ 5 passed · 0.19s"]


def test_long_bash_output_head_tail_and_hint(monkeypatch):
    monkeypatch.setattr(render, "_MORE_HINT", "/output 9")
    rows = _plain(tool_result_rows("Bash", {"command": "seq 1 300"}, "\n".join(map(str, range(1, 301))), width=80))
    assert rows[1:5] == ["1", "2", "3", "4"] and rows[-2:] == ["299", "300"]
    assert rows[5] == "… +294 more lines · /output 9 to view all"


# ─── charts ────────────────────────────────────────────────────────────────

def test_bar_chart_bars_proportional_with_labels_and_values():
    rows = _plain(charts.render_chart("chart", "title: Latency\napi: 100\nweb: 50\ndb: 25", 60) or [])
    assert rows[0].endswith("Latency")
    bars = {r.split()[1]: r for r in rows if "│" in r[3:]}
    lengths = {k: r.count("█") for k, r in bars.items()}
    assert lengths["api"] == 2 * lengths["web"] and abs(lengths["web"] - 2 * lengths["db"]) <= 1
    assert bars["api"].rstrip().endswith("100") and bars["db"].rstrip().endswith("25")
    assert "3 items · max 100 · total 175" in rows[-1]
    assert all(display_width(r) <= 60 for r in rows)


@pytest.mark.parametrize("body", [
    "a,1\nb,2\nc,3", '{"labels": ["a", "b", "c"], "values": [1, 2, 3]}', "| name | n |\n|---|---|\n| a | 1 |\n| b | 2 |\n| c | 3 |",
    '{"a": 1, "b": 2, "c": 3}', "a: 1\nb: 2\nc: 3",
])
def test_chart_input_formats(body):
    title, labels, values = charts.parse_chart(body)
    assert labels == ["a", "b", "c"] and values == [1, 2, 3]


def test_sparkline_and_non_chart_fallback():
    out = strip_ansi(format_markdown("```sparkline\n1, 5, 2, 8\n```", max_width=60))
    assert "▁▅▂█" in out and "min 1 · max 8 · last 8" in out
    code = strip_ansi(format_markdown("```chart\nnot numbers here\n```", max_width=60))
    assert "not numbers here" in code                    # unparseable: shown as code, nothing lost


def test_markdown_chart_fence_streams_like_whole_document():
    from axon.ui.markdown import MarkdownStream
    md = "Before\n\n```chart\nx: 1\ny: 3\n```\nAfter\n"
    whole = format_markdown(md, max_width=50)
    st = MarkdownStream(width=50)
    out = "".join(st.feed(ch) for ch in md) + st.flush()
    assert out.rstrip("\n") == whole and "█" in whole


# ─── markdown code cards / tables / callouts ───────────────────────────────

def test_code_fence_card_frame_only_in_gutter_and_numbers_for_long_blocks():
    long = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(8)) + "\n```"
    rows = _plain(format_markdown(long, max_width=60).splitlines())
    assert rows[0].strip() == "╭── python" and rows[-1].strip() == "╰──"
    body = rows[1:-1]
    assert [r.strip().split("│", 1)[1].split() for r in body][:2] == [["1", "x0", "=", "0"], ["2", "x1", "=", "1"]]
    box = set("│╭╮╰╯─┌┐└┘├┤┬┴┼")
    for r in body:     # the code itself (after the gutter) never contains frame characters
        code = r.split("│", 1)[1]
        assert not (set(code) & box), r
    short = _plain(format_markdown("```js\nlet a = 1\n```", max_width=60).splitlines())
    assert short[1].strip() == "│ let a = 1"           # <= 5 lines: no numbers


def test_table_numbers_right_aligned_and_callout_continuation():
    rows = _plain(render_table(["| name | count |", "|---|---|", "| a | 5 |", "| bb | 1,250 |"], 60))
    assert "  │ a    │     5 │" in rows and "  │ bb   │ 1,250 │" in rows
    out = _plain(format_markdown("> [!WARNING] careful\n> second line", max_width=60).splitlines())
    assert out[0].strip() == "▎ ⚠ Warning: careful" and out[1].strip() == "▎ second line"


# ─── ASCII / NO_COLOR fallbacks ────────────────────────────────────────────

def test_ascii_fallbacks(monkeypatch):
    monkeypatch.setenv("AXON_ASCII", "1")
    rows = tool_result_rows("Write", {"path": "a.py", "content": "x = 1\n"}, "Created file.", width=60)
    assert rows[0].startswith("+ Created a.py - +1 - Python - 1 line")
    diff = tool_result_rows("Edit", {"path": "zz.py", "old_string": "a", "new_string": "b"}, "ok", width=60)
    assert diff[0] == "~ Edited zz.py - +1 -1"
    chart = format_markdown("```chart\na: 4\nb: 2\n```", max_width=40)
    assert "####" in chart and "█" not in chart
    bar = cards.test_bar({"passed": 3, "failed": 1})
    assert set(strip_ansi(bar).split()[0]) <= {"#", "x"}
    code = format_markdown("```py\n" + "\n".join("x" for _ in range(7)) + "\n```", max_width=40)
    assert code.splitlines()[0].strip() == "+-- py" and code.splitlines()[1].strip().startswith("|   1")
    assert all(ord(c) < 128 for c in chart + code + bar + rows[0] + diff[0])


def test_no_color_output_has_no_escape_sequences():
    assert not theme.COLOR_ENABLED                    # tests run without a TTY: NO_COLOR behaviour
    rows = tool_result_rows("Edit", {"path": "zz.py", "old_string": "a b", "new_string": "a c"}, "ok", width=60)
    rows += cards.code_card("a.py", "def f():\n    return 1\n", 60)
    assert all("\x1b[" not in r for r in rows)


# ─── width compliance ──────────────────────────────────────────────────────

@pytest.mark.parametrize("cols", [40, 60, 100, 200])
def test_renderer_rows_never_exceed_terminal_width(cols, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(theme, "term_width", lambda: cols)
    monkeypatch.setattr(render, "term_width", lambda: cols)
    r = Renderer(trace_level="normal")
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1, user_prompt="go"))
    calls = [
        ("Write", {"path": "a_long_directory_name/file.py", "content": PY + "\n" + "y = '" + "z" * 300 + "'"}, "Created file."),
        ("Edit", {"path": "a.py", "old_string": "v = " + "q" * 150, "new_string": "v = " + "w" * 150}, "ok"),
        ("Grep", {"pattern": "x"}, "\n".join(f"some/deep/path/f{i}.py:{i}:" + "x " * 80 for i in range(5))),
        ("Bash", {"command": "pytest"}, "F" * 300 + "\n1 failed, 2 passed in 0.1s"),
        ("Glob", {"pattern": "*"}, "\n".join(f"dir{i % 3}/very_long_file_name_number_{i}.py" for i in range(30))),
        ("WebFetch", {"url": "https://e.x"}, "URL: x\n\n" + '{"k": "' + "v" * 300 + '"}'),
    ]
    for i, (name, inp, out) in enumerate(calls):
        r.on_event(ToolExecutionStart(id=f"t{i}", name=name, input=inp))
        r.on_event(ToolExecutionResult(id=f"t{i}", name=name, input={}, content=out))
    from axon.providers.base import TextDelta
    r.on_event(TextDelta("| a | b |\n|---|---|\n| 1 | 2 |\n\n```chart\nalpha: 3\nbeta: 12345\n```\n```python\n"
                         + "\n".join("x = '" + "y" * 200 + "'" for _ in range(7)) + "\n```\n"))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage(input=1, output=1)))
    out = capsys.readouterr().out
    wide = [strip_ansi(l) for l in out.splitlines() if display_width(l) > cols - 1]
    assert not wide, wide[:3]


# ─── clickable result rows ─────────────────────────────────────────────────

def test_result_rows_link_to_full_output_and_card_paths_to_the_file(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXON_HYPERLINKS", "1")
    r = Renderer(trace_level="normal")
    r.on_event(ToolExecutionStart(id="b", name="Bash", input={"command": "ls"}))
    r.on_event(ToolExecutionResult(id="b", name="Bash", input={}, content="a\nb"))
    out = capsys.readouterr().out
    summary = next(l for l in out.splitlines() if "⎿" in l)
    m = re.search(r"\x1b\]8;;(file://[^\x1b]+)\x1b\\", summary)
    assert m and "axon-outputs-" in m.group(1)                    # the summary row opens the full output
    assert strip_ansi(summary).strip().startswith("⎿  2 lines of output")
    r.on_event(ToolExecutionStart(id="w", name="Write", input={"path": "new.py", "content": "x = 1\n"}))
    r.on_event(ToolExecutionResult(id="w", name="Write", input={}, content="Created file."))
    out = capsys.readouterr().out
    header = next(l for l in out.splitlines() if "Created" in l)
    assert (tmp_path / "new.py").resolve().as_uri() in header      # the card header opens the file itself
    assert "axon-outputs-" not in header


# ─── footer after a failed turn ────────────────────────────────────────────

def test_failed_turn_footer_says_continue_or_retry(capsys):
    r = Renderer()
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1, user_prompt="hi"))
    r.turn_footer(0, Usage(), 0.0, 1.0, 1, stop_reason="error", error="boom")
    out = strip_ansi(capsys.readouterr().out)
    assert "Nothing was done yet · /retry sends your message again" in out and "continue" not in out
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1, user_prompt="hi"))
    r.on_event(ToolExecutionStart(id="b", name="Bash", input={"command": "ls"}))
    r.on_event(ToolExecutionResult(id="b", name="Bash", input={}, content="a"))
    r.turn_footer(1, Usage(), 0.0, 1.0, 1, stop_reason="interrupted")
    out = strip_ansi(capsys.readouterr().out)
    assert "Work done so far is kept · type continue to resume" in out and "/retry" not in out


# ─── provider next to the model ────────────────────────────────────────────

def test_turn_and_step_headers_name_the_provider(capsys, monkeypatch):
    monkeypatch.setattr(theme, "term_width", lambda: 120)
    monkeypatch.setattr(render, "term_width", lambda: 120)
    r = Renderer()
    r._agent = SimpleNamespace(settings=SimpleNamespace(base_url="https://agentrouter.org/v1", provider=None))
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="claude-opus-5-5", message_count=1,
                            user_prompt="hi", mode="bypass"))
    out = strip_ansi(capsys.readouterr().out)
    assert "claude-opus-5-5 via AgentRouter · bypass mode" in out
    assert "◆ Step 1  LLM call · claude-opus-5-5 (AgentRouter) · reads 1 message" in out
    r2 = Renderer()
    r2._agent = SimpleNamespace(settings=SimpleNamespace(base_url="", provider="anthropic"))
    r2.on_event(LLMCallStart(iteration=1, max_iterations=5, model="claude-opus-5-5", message_count=1, user_prompt="x"))
    assert "claude-opus-5-5 via Anthropic" in strip_ansi(capsys.readouterr().out)
    r3 = Renderer()                                    # no agent attached: no provider label
    r3.on_event(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=1, user_prompt="x"))
    assert " via " not in strip_ansi(capsys.readouterr().out)


def test_step_header_drops_provider_when_narrow(capsys, monkeypatch):
    monkeypatch.setattr(theme, "term_width", lambda: 70)
    monkeypatch.setattr(render, "term_width", lambda: 70)
    r = Renderer()
    r._agent = SimpleNamespace(settings=SimpleNamespace(base_url="https://agentrouter.org/v1", provider=None))
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="claude-opus-5-5", message_count=12,
                            tokens_in=12_000, user_prompt="hi"))
    out = strip_ansi(capsys.readouterr().out)
    assert "(AgentRouter)" not in out and "LLM call · claude-opus-5-5 · reads 12 messages" in out


# ─── history looks like live ───────────────────────────────────────────────

def test_replay_and_transcript_use_the_same_cards():
    msgs = [
        {"role": "user", "content": "make it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "w1", "name": "Write",
                                           "input": {"path": "k.py", "content": "a = 1\nb = 2\n"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w1", "content": "Created file."}]},
    ]
    rows = [strip_ansi(r) for r in render.format_conversation_replay(msgs, width=80)]
    assert any("✚ Created k.py · +2 · ■ Python · 2 lines" in r for r in rows)
    assert any(r.rstrip().endswith("1 + a = 1") for r in rows)
    card = render.transcript_card_rows("Write", '{"path": "k.py", "content": "a = 1\\nb = 2\\n"}', "Created file.", 70)
    assert card and strip_ansi(card[0]).startswith("✚ Created k.py") and strip_ansi(card[2]).strip() == "2 + b = 2"
    assert render.transcript_card_rows("Bash", '{"command": "ls"}', "a\nb", 70) is None


def test_ten_thousand_line_outputs_render_fast():
    import time
    big = "\n".join(f"x{i} = f({i})" for i in range(10_000))
    t = time.time()
    tool_result_rows("Write", {"path": "a.py", "content": big}, "Created file.", width=100)
    tool_result_rows("Bash", {"command": "x"}, big, width=100)
    tool_result_rows("Grep", {"pattern": "f"}, "\n".join(f"a{i % 9}.py:{i}:{l}" for i, l in enumerate(big.splitlines())), width=100)
    tool_result_rows("Edit", {"path": "a.py", "old_string": big, "new_string": big.replace("f(", "g(")}, "ok", width=100)
    assert time.time() - t < 2.0


def test_edit_of_unlocatable_file_leaves_line_numbers_blank(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = _plain(tool_result_rows("Edit", {"path": "gone.py", "old_string": "a = 1", "new_string": "a = 2"}, "ok", width=60))
    assert rows[1] == "    - a = 1" and rows[2] == "    + a = 2"
