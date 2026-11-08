"""Regression tests from the line-by-line review of the markdown / cards / charts / history renderers and
the repo-map, analytics, tips and chart-helper features.  One test per defect; timing tests use bounds that are
orders of magnitude above the fixed cost and below the old (quadratic or cubic) cost."""
from __future__ import annotations

import os
import json
import time
from pathlib import Path

import pytest

from axon.features import _charts as fcharts
from axon.features import analytics, repo_map, tips
from axon.ui import cards, charts, history_view, markdown
from axon.ui.theme import strip_ansi


def _under(seconds: float, fn) -> object:
    start = time.perf_counter()
    result = fn()
    assert time.perf_counter() - start < seconds
    return result


# ─── markdown ───────────────────────────────────────────────────────────────

def test_fence_regex_is_linear_on_a_long_line_with_a_late_backtick() -> None:
    # "```aaaa...a`" used to backtrack quadratically (1.4 s for 20k characters)
    assert _under(0.3, lambda: markdown._FENCE_RE.match("```" + "a" * 20000 + "`")) is None
    m = markdown._FENCE_RE.match("  ```python title=x")
    assert m and m.group(2) == "```" and m.group(3) == "python"
    assert markdown._FENCE_RE.match("~~~") and markdown._FENCE_RE.match("```")
    assert markdown._FENCE_RE.match("``` has `tick") is None


def test_heading_regex_is_linear_on_a_long_whitespace_run() -> None:
    # "# x" + 1500 spaces + "y" took 4 s (cubic)
    assert _under(0.3, lambda: markdown.format_markdown("# x" + " " * 1500 + "y"))


def test_heading_closing_hashes_are_still_dropped() -> None:
    plain = strip_ansi(markdown.format_markdown("## Title ##"))
    assert "Title" in plain and "#" not in plain
    assert "Plain" in strip_ansi(markdown.format_markdown("### Plain"))


def test_css_number_regex_is_not_cubic_on_digit_runs() -> None:
    assert _under(0.5, lambda: markdown.highlight("a:" + "1" * 600 + "z", "css"))
    assert "12.5px" in strip_ansi(markdown.highlight("width: 12.5px;", "css"))
    assert ".5em" in strip_ansi(markdown.highlight("margin: .5em;", "css"))


def test_inline_code_with_unknown_user_home_does_not_raise() -> None:
    # Path("~nosuchuser/x").expanduser() raises RuntimeError on POSIX; Windows resolves it beside the profile folder.
    if os.name != "nt":
        assert markdown.file_uri("~nosuchuser-axon-review/foo") == ""
    assert "foo.py" in strip_ansi(markdown._style_inline("see `~nosuchuser-axon-review/foo.py` now"))


def test_deeply_nested_latex_does_not_blow_the_stack() -> None:
    deep = "\\sqrt{" * 2000 + "x" + "}" * 2000
    assert markdown.latex_to_unicode(deep).startswith("√(")
    markdown.format_markdown("$$\n" + deep + "\n$$")
    markdown.format_markdown("inline $" + deep + "$ math")


def test_latex_command_as_bare_superscript_argument() -> None:
    assert markdown.latex_to_unicode("x^\\alpha") == "xα"
    assert markdown.latex_to_unicode("10^\\circ") == "10∘"
    assert markdown.latex_to_unicode("e^{-x}") == "e⁻ˣ"
    assert markdown.latex_to_unicode("a_\\beta") == "aβ"


def test_inline_regexes_do_not_go_quadratic_on_unclosed_markers() -> None:
    _under(0.5, lambda: markdown._style_inline("`" * 5000 + "a"))
    _under(0.5, lambda: markdown._style_inline("[" * 20000))
    _under(0.5, lambda: markdown._style_inline("**a " * 5000))
    _under(0.5, lambda: markdown._style_inline("__a " * 5000))


def test_inline_code_spans_still_pair_by_run_length() -> None:
    assert strip_ansi(markdown._style_inline("use ``a`b`` here")).count("a`b") == 1
    assert "ls -la" in strip_ansi(markdown._style_inline("run `ls -la` now"))
    assert strip_ansi(markdown._style_inline("```not closed``")) == "```not closed``"


# ─── cards ──────────────────────────────────────────────────────────────────

def test_word_highlights_of_a_changed_long_line_do_not_freeze() -> None:
    old = "q" * 20000
    new = "q" * 10000 + "Z" + "q" * 9999
    _under(1.0, lambda: cards._pair_lines([old], [new]))            # 48 s before the cap
    long_words = "x = " + " ".join(f"w{i}" for i in range(3000))
    _under(1.0, lambda: cards.diff_rows(long_words + "\n", long_words.replace("w10 ", "wXX ") + "\n", width=80))


def test_word_highlight_ranges_follow_the_tab_expanded_text() -> None:
    old_ch, new_ch = cards._pair_lines(["\tfoo bar baz"], ["\tfoo bar BAZ"])
    expanded = "    foo bar BAZ"
    start, end = new_ch[0][0]
    assert expanded[start:end] == "BAZ"
    start, end = old_ch[0][0]
    assert "    foo bar baz"[start:end] == "baz"


def test_unified_diff_keeps_a_removed_comment_line_that_looks_like_a_file_header() -> None:
    patch = ("--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n ctx\n--- comment removed\n-keep\n+new\n")
    (f,) = cards.parse_unified_diff(patch)
    assert f["path"] == "q.sql" and f["added"] == 1 and f["removed"] == 2
    assert f["hunks"][0][2] == [" ctx", "--- comment removed", "-keep", "+new"]
    two = ("--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n")
    assert [x["path"] for x in cards.parse_unified_diff(two)] == ["one.py", "two.py"]


def test_find_json_gives_up_quietly_on_recursion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_s: str):
        raise RecursionError("too deep")
    monkeypatch.setattr(cards.json, "loads", boom)
    assert cards.find_json("[1, 2, 3]") is None


# ─── ui charts ──────────────────────────────────────────────────────────────

def test_chart_json_values_object_accepts_formatted_numbers() -> None:
    title, labels, values = charts.parse_chart('{"values": {"a": "$5", "b": "1,200", "c": "n/a"}}')
    assert (labels, values) == (["a", "b"], [5.0, 1200.0])


def test_chart_json_labels_are_padded_to_the_values() -> None:
    _, labels, values = charts.parse_chart('{"labels": ["a"], "values": [1, 2, 3]}')
    assert len(labels) == len(values) == 3 and labels[0] == "a"
    rows = charts.render_chart("bar", '{"labels": ["a"], "values": [1, 2, 3]}', 60)
    assert rows is not None and sum("│" in r for r in rows) == 3


def test_chart_table_plots_the_column_after_a_numeric_label_column() -> None:
    title, labels, values = charts.parse_chart("year,sales\n2021,100\n2022,150")
    assert (labels, values) == (["2021", "2022"], [100.0, 150.0]) and title == "sales by year"
    # a numeric first column is still used when nothing else is numeric
    assert charts.parse_chart("5,a\n7,b")[2] == [5.0, 7.0]


def test_chart_numbers_must_be_finite() -> None:
    assert charts.parse_number("9" * 400) is None                       # float('9' * 400) is inf
    assert charts.parse_chart("a: " + "9" * 400 + "\nb: 1") is None
    assert charts.render_chart("sparkline", "[1, Infinity]", 60) is None


def test_sparkline_rows_label_row_matches_the_line_that_is_drawn() -> None:
    rows = charts.sparkline_rows(list("abcdef"), [1, 2, 3, 4, 5, 6], 12)
    spark, labels = strip_ansi(rows[0]), strip_ansi(rows[-1])
    assert len(labels) == len(spark) and labels.rstrip().endswith("f")


# ─── features/_charts ───────────────────────────────────────────────────────

def test_chart_table_ignores_cells_beyond_the_headers() -> None:
    lines = fcharts.table(["a", "b"], [["1", "2", "3"], ["4"]])
    assert [ln.split() for ln in lines[2:]] == [["1", "2"], ["4"]]


def test_chart_table_never_widens_the_first_column() -> None:
    lines = fcharts.table(["a", "b" * 30], [["1", "2"]], max_width=20)
    assert lines[0].index("b") == len("  ") + 1 + 2          # indent + width-1 column + gap, not 8 wide


# ─── history view ───────────────────────────────────────────────────────────

def _history(tmp_path: Path, records: list) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _turn(**extra) -> list:
    data = {"model": "m", "text": "done", "call": {"provider": "p"}, "stop_reason": "end_turn", **extra}
    return [{"type": "user_message", "data": {"content": "hello"}}, {"type": "assistant_turn", "data": data}]


@pytest.mark.parametrize("level", ["normal", "detailed"])
def test_thinking_text_cannot_carry_terminal_escapes(tmp_path: Path, level: str) -> None:
    path = _history(tmp_path, _turn(thinking="plan \x1b[2Jwipe \x1b]0;title\x07 end"))
    out = "\n".join(history_view.format_session_history(path, width=80, trace_level=level))
    assert "\x1b[2J" not in out and "\x1b]0" not in out and "plan" in out


def test_history_survives_a_non_numeric_thinking_time(tmp_path: Path) -> None:
    path = _history(tmp_path, _turn(thinking="hmm", thinking_s=["x", 1.5]))
    out = strip_ansi("\n".join(history_view.format_session_history(path, width=80, trace_level="detailed")))
    assert "thought for 1.5s" in out


def test_history_fallback_reader_skips_lines_that_are_not_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.session.store as store

    def broken(_path):
        raise RuntimeError("no store")
    monkeypatch.setattr(store, "iter_records", broken)
    path = _history(tmp_path, ["123", '"text"', "[1]", *_turn()])
    assert history_view.format_session_history(path, width=80)


# ─── repo map ───────────────────────────────────────────────────────────────

def test_repo_map_keeps_parameters_that_start_with_self_or_cls() -> None:
    sym = ["f", "function", 1, "def f(self, selfie, cls_a)", "C"]
    assert repo_map._sig(sym) == "def f(selfie, cls_a)"
    assert repo_map._sig(["f", "function", 1, "def f(selfie)", None]) == "def f(selfie)"
    assert repo_map._sig(["f", "function", 1, "def f(cls)", None]) == "def f()"


def test_repo_map_signatures_carry_no_terminal_escapes() -> None:
    symbols = repo_map._regex_symbols("def f(x):  # \x1b]0;pwned\x07 \x1b[2J\n", "python")
    assert symbols and "\x1b" not in repo_map._sig(symbols[0])
    ranked = repo_map.rank_files({"files": {"a.py": {"m": 1, "s": 10, "l": "python", "sym": symbols, "id": []}}})
    text = repo_map.render_map(ranked)
    assert "\x1b" not in text and "\x07" not in text and "def f(x)" in text


def test_rank_files_scales_linearly_with_the_number_of_files() -> None:
    files = {}
    for i in range(6000):                        # 13.7 s with the per-file scan of every reference
        files[f"d{i % 50}/f{i}.py"] = {
            "m": 1, "s": 1000, "l": "python",
            "sym": [[f"Name{i}_{k}", "function", k, f"def Name{i}_{k}()", None] for k in range(8)],
            "id": [f"Name{(i * 7 + j * 13) % 6000}_{j % 8}" for j in range(200)],
        }
    ranked = _under(6.0, lambda: repo_map.rank_files({"files": files}))
    assert len(ranked) == 6000 and any(r.symbol_refs for r in ranked)


def test_java_method_regex_is_not_cubic_on_trailing_whitespace() -> None:
    line = "public void foo(int a)" + " " * 800 + "x"
    assert _under(0.3, lambda: repo_map._regex_symbols(line, "java")) == []
    symbols = repo_map._regex_symbols("class A {\n    public void run(int a) throws IOException, Foo {\n}\n", "java")
    assert [s[0] for s in symbols] == ["A", "run"]


def test_map_arguments_report_a_dangling_tokens_flag() -> None:
    assert repo_map._parse_args("--tokens")[3] is True
    assert repo_map._parse_args("src --tokens 300") == ("src", 300, False, False)


def test_index_skips_files_whose_names_contain_control_characters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("def fine():\n    pass\n", encoding="utf-8")
    try:
        (ws / "bad\x1b[2J.py").write_text("def evil():\n    pass\n", encoding="utf-8")
    except OSError:
        pytest.skip("file system rejects control characters in names")
    index, _ = repo_map.build_index(ws, rebuild=True)
    assert list(index["files"]) == ["ok.py"]


# ─── analytics ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [1e20, float("nan"), float("inf"), -5, 0, True, "0001-01-01T00:00:00", "", None, "garbage"])
def test_unusable_timestamps_are_dropped(bad) -> None:
    assert analytics._parse_ts(bad) is None


def test_usable_timestamps_survive() -> None:
    assert analytics._parse_ts(1_700_000_000) == 1_700_000_000.0
    assert analytics._parse_ts("2024-05-01T12:00:00Z") is not None


def test_one_corrupt_usage_record_does_not_sink_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon import storage
    now = time.time()
    rows = [{"ts": 1e20, "model": "x", "usage": {"input": 1}}, {"ts": now, "model": "ok", "usage": {"input": 10, "output": 5}, "cost": 0.5}]
    path = storage.user_file("usage.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = analytics.collect(analytics.parse_period("all"))
    assert stats.totals.calls == 1
    assert "ok" in "\n".join(analytics.render_stats(stats, 100))
    assert "<script" not in analytics.render_html(stats)


def test_usage_summary_text_cleans_untrusted_names() -> None:
    stats = analytics.collect(analytics.parse_period("all"))
    stats.tools.append(analytics.ToolAgg("evil\ntool\x1b[2J", calls=1, total_s=1.0))
    stats.by_model.append(("model\nwith\x1b[31mcodes", analytics.Agg(calls=1, cost=1.0)))
    stats.totals.calls = 1
    for group in ("tool", "model"):
        text = analytics.usage_summary_text(stats, group)
        assert "\x1b" not in text and "evil\ntool" not in text and "model\nwith" not in text


def test_html_report_escapes_prompt_and_name_content() -> None:
    payload = '<script>alert(1)</script>"\'&'
    stats = analytics.collect(analytics.parse_period("all"))
    now = time.time()
    stats.totals.calls = 1
    stats.totals.cost = 1.0
    stats.by_model.append((payload, analytics.Agg(calls=1, cost=1.0)))
    stats.by_source.append((payload, analytics.Agg(calls=1, cost=1.0)))
    stats.tools.append(analytics.ToolAgg(payload, calls=1, total_s=1.0))
    stats.turns.append(analytics.TurnRow("s", "p", 1, payload, payload, 1.0, 1, 1, 10, 1.0, now))
    stats.sessions.append(analytics.SessionRow(id=payload, project="p", workspace=payload, title=payload,
                                               first_ts=now, last_ts=now, calls=1, tokens=5, cost=1.0))
    html_text = analytics.render_html(stats)
    assert "<script>alert(1)" not in html_text and "&lt;script&gt;" in html_text


# ─── tips ───────────────────────────────────────────────────────────────────

def test_tips_parser_survives_a_recursion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    def boom(self, s, idx=0):
        raise RecursionError("too deep")
    monkeypatch.setattr(_json.JSONDecoder, "raw_decode", boom)
    assert tips.parse_tips_json("[[[[ nothing to see") == []
