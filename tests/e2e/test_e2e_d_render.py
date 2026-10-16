"""
Agent-D e2e: rendering robustness of real ``axon -p`` runs (colour control, terminal width,
markdown layout, wide characters and long tool output).
"""
from __future__ import annotations

import re

import pytest

from harness import AxonEnv, strip_ansi
from test_e2e_d_helpers import SGR_RE, TRACEBACK, tool_turn, visible_lines

pytestmark = pytest.mark.e2e

LEVELS = ("compact", "normal", "detailed", "debug")


def _width(line: str) -> int:
    from axon.ui.theme import display_width
    return display_width(line)


def _run(axon_env: AxonEnv, scripts, *args: str, env: dict[str, str] | None = None, prompt: str = "hi"):
    axon_env.llm.enqueue(*scripts)
    r = axon_env.run(["-p", "--mode", "bypass", *args, prompt], env=env)
    assert TRACEBACK not in r.stdout + r.stderr, str(r)
    return r


def _answer_block(stdout: str) -> list[str]:
    """Lines of the final answer (compact trace: everything before the ✓ Done footer)."""
    return strip_ansi(stdout).split("✓ Done")[0].splitlines()


# ─── colour control ────────────────────────────────────────────────────────

@pytest.mark.parametrize("level", LEVELS)
def test_no_color_output_has_zero_escape_chars(axon_env: AxonEnv, level: str) -> None:
    """With NO_COLOR every trace level of a tool turn emits no ESC byte at all."""
    r = _run(axon_env, tool_turn(), "--trace", level)
    assert r.code == 0, str(r)
    assert "\x1b" not in r.stdout and "\x1b" not in r.stderr, repr(r.stdout[:400])


def test_no_color_markdown_answer_has_no_escapes(axon_env: AxonEnv) -> None:
    """Markdown (headings, bold, code, tables) under NO_COLOR is rendered without escapes."""
    md = "# Head\n\n**bold** and `code`\n\n```py\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    r = _run(axon_env, [[{"text": md}]])
    assert "\x1b" not in r.stdout and "Head" in r.stdout and "bold" in r.stdout


def test_no_color_error_turn_has_no_escapes(axon_env: AxonEnv) -> None:
    """A failing provider call (401) is reported without ESC bytes under NO_COLOR."""
    axon_env.llm.enqueue([{"error": 401}])
    r = axon_env.run(["-p", "hi"])
    assert r.code != 0 and "\x1b" not in r.stdout + r.stderr, str(r)


def test_no_color_doctor_has_no_escapes(axon_env: AxonEnv) -> None:
    """`axon doctor --no-agent` honours NO_COLOR."""
    r = axon_env.run(["doctor", "--no-agent"])
    assert "Axon doctor" in r.output and "\x1b" not in r.stdout + r.stderr


def test_term_dumb_disables_colour(axon_env: AxonEnv) -> None:
    """TERM=dumb without NO_COLOR still produces plain output."""
    env = dict(axon_env.env)
    env.pop("NO_COLOR")
    axon_env.env = env
    r = _run(axon_env, tool_turn(), env={"TERM": "dumb"})
    assert "\x1b" not in r.stdout


def test_piped_stdout_disables_colour(axon_env: AxonEnv) -> None:
    """A non-tty stdout with TERM=xterm-256color and no NO_COLOR is still plain."""
    axon_env.env.pop("NO_COLOR")
    r = _run(axon_env, tool_turn(), env={"TERM": "xterm-256color"})
    assert "\x1b" not in r.stdout


def test_model_text_escape_sequences_are_sanitized_in_answer(axon_env: AxonEnv) -> None:
    """Raw ESC / OSC sequences inside the model's answer never reach the terminal."""
    r = _run(axon_env, [[{"text": "evil \x1b[31mRED\x1b[0m \x1b]0;pwned\x07 end"}]], "--trace", "compact")
    assert "\x1b" not in r.stdout and "\x07" not in r.stdout, repr(r.stdout[:300])


def test_tool_output_escape_sequences_are_sanitized(axon_env: AxonEnv) -> None:
    """ANSI / OSC sequences printed by a tool are stripped from the rendered result."""
    cmd = "printf '\\033[31mredout\\033[0m\\n\\033]0;title\\007done\\n'"
    r = _run(axon_env, [[{"tool": "Bash", "args": {"command": cmd, "description": "x"}}], [{"text": "ok"}]],
             "--trace", "detailed")
    rows = [ln for ln in r.stdout.splitlines() if "redout" in ln and "printf" not in ln]
    assert rows and all("\x1b" not in ln and "\x07" not in ln for ln in rows), rows


def test_model_text_escapes_sanitized_in_tool_why_line(axon_env: AxonEnv) -> None:
    """The `why:` reasoning line shown under a tool call is sanitized like the answer text."""
    scripts = [[{"text": "ok \x1b[31mRED\x1b[0m \x1b]0;pwned\x07"},
                {"tool": "Bash", "args": {"command": "echo hi", "description": "x"}}], [{"text": "fin"}]]
    r = _run(axon_env, scripts)
    assert "\x1b" not in r.stdout and "\x07" not in r.stdout


def test_force_color_truecolor(axon_env: AxonEnv) -> None:
    """FORCE_COLOR=truecolor emits 24-bit SGR codes and no 256-colour codes."""
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "truecolor"})
    codes = set(SGR_RE.findall(r.stdout))
    assert any(c.startswith("\x1b[38;2;") for c in codes), codes
    assert not any(c.startswith("\x1b[38;5;") for c in codes), codes


def test_axon_force_color_truecolor(axon_env: AxonEnv) -> None:
    """AXON_FORCE_COLOR=truecolor also forces 24-bit colour on a pipe."""
    r = _run(axon_env, tool_turn(), env={"AXON_FORCE_COLOR": "truecolor"})
    assert "\x1b[38;2;" in r.stdout


def test_force_color_1_uses_256_colours(axon_env: AxonEnv) -> None:
    """FORCE_COLOR=1 without COLORTERM uses the 256-colour palette."""
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "1"})
    assert "\x1b[38;5;" in r.stdout and "\x1b[38;2;" not in r.stdout


def test_force_color_with_colorterm_truecolor(axon_env: AxonEnv) -> None:
    """FORCE_COLOR=1 plus COLORTERM=truecolor upgrades to 24-bit colour."""
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "1", "COLORTERM": "truecolor"})
    assert "\x1b[38;2;" in r.stdout


def test_force_color_overrides_no_color(axon_env: AxonEnv) -> None:
    """FORCE_COLOR wins over NO_COLOR (documented precedence)."""
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "1", "NO_COLOR": "1"})
    assert "\x1b[38;5;" in r.stdout


def test_force_color_zero_is_plain(axon_env: AxonEnv) -> None:
    """FORCE_COLOR=0 does not force colour on a pipe."""
    axon_env.env.pop("NO_COLOR")
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "0", "TERM": "xterm-256color"})
    assert "\x1b" not in r.stdout


def test_theme_mono_env_only_bold_and_reset(axon_env: AxonEnv) -> None:
    """AXON_THEME=mono with forced colour emits only attribute codes, no colours."""
    r = _run(axon_env, tool_turn(), env={"FORCE_COLOR": "1", "AXON_THEME": "mono"})
    codes = set(SGR_RE.findall(r.stdout))
    assert codes and not any("38;" in c or "48;" in c for c in codes), codes


def test_theme_light_env_differs_from_dark(axon_env: AxonEnv) -> None:
    """AXON_THEME=light uses a different palette than the default dark theme."""
    dark = set(SGR_RE.findall(_run(axon_env, tool_turn(), env={"FORCE_COLOR": "1"}).stdout))
    light = set(SGR_RE.findall(_run(axon_env, tool_turn(), env={"FORCE_COLOR": "1", "AXON_THEME": "light"}).stdout))
    assert dark and light and dark != light
    assert "\x1b[38;5;28m" in light and "\x1b[38;5;28m" not in dark


def test_forced_colour_visible_text_matches_plain(axon_env: AxonEnv) -> None:
    """Stripping ANSI from coloured output yields the same answer text as NO_COLOR output."""
    plain = _run(axon_env, [[{"text": "Same **words** here"}]], "--trace", "compact").stdout
    col = _run(axon_env, [[{"text": "Same **words** here"}]], "--trace", "compact", env={"FORCE_COLOR": "1"}).stdout
    assert "Same words here" in plain and "Same words here" in strip_ansi(col)


# ─── terminal width ────────────────────────────────────────────────────────

WIDE_MD = ("# Title heading that is rather long indeed\n\nSome paragraph text that goes on and on well past forty "
           "columns of width for sure.\n\n- item one is long enough to wrap around the narrow terminal width\n"
           "- 二 中文字符测试中文字符测试中文字符测试中文字符测试\n\n| col a | col b long header | c |\n|---|---|---|\n"
           "| 1 | 二中文 | 🎉 emoji |\n| longer cell value here | x | y |\n\n1. first\n2. second 🚀 rocket\n")


def _overflow(stdout: str, cols: int) -> list[str]:
    return [ln for ln in visible_lines(stdout) if _width(ln) > cols]


def _wide_turn(axon_env: AxonEnv, cols: int, level: str) -> str:
    scripts = [[{"text": "ok"}, {"tool": "Bash", "args": {"command": "seq 1 300",
                                                          "description": "long output with a long description"}},
                {"usage": {"input": 8600, "output": 20}}],
               [{"text": WIDE_MD}, {"usage": {"input": 9000, "output": 120}}]]
    r = _run(axon_env, scripts, "--trace", level, env={"COLUMNS": str(cols)},
             prompt="please do the thing with a long prompt text here")
    assert r.code == 0, str(r)
    return r.stdout


@pytest.mark.parametrize("cols", [60, 80, 100, 120])
def test_normal_trace_never_exceeds_columns(axon_env: AxonEnv, cols: int) -> None:
    """Every rendered line of a normal-level turn (tool + markdown answer) fits within COLUMNS."""
    out = _overflow(_wide_turn(axon_env, cols, "normal"), cols)
    assert not out, out


def test_rail_wrapping_narrow_widths_sweep(axon_env: AxonEnv) -> None:
    """For narrow widths (40, 44, 46, 50) no line of a normal trace exceeds COLUMNS."""
    bad = {}
    for cols in (40, 44, 46, 50):
        axon_env.llm.enqueue([{"text": "ok"}, {"tool": "Bash", "args": {"command": "seq 1 300", "description": "x"}},
                              {"usage": {"input": 8600, "output": 20}}],
                             [{"text": "fin"}, {"usage": {"input": 8700, "output": 3}}])
        r = axon_env.run(["-p", "--mode", "bypass", "hi"], env={"COLUMNS": str(cols)})
        over = _overflow(r.stdout, cols)
        if over:
            bad[cols] = over
    assert not bad, bad


@pytest.mark.parametrize("level", ["compact", "detailed", "debug"])
def test_other_trace_levels_fit_60_columns(axon_env: AxonEnv, level: str) -> None:
    """compact / detailed / debug turns stay within a 60-column terminal."""
    out = _overflow(_wide_turn(axon_env, 60, level), 60)
    assert not out, out


def test_compact_trace_fits_40_columns(axon_env: AxonEnv) -> None:
    """The compact trace fits a 40-column terminal."""
    out = _overflow(_wide_turn(axon_env, 40, "compact"), 40)
    assert not out, out


def test_turn_header_truncates_long_prompt(axon_env: AxonEnv) -> None:
    """A long prompt in the turn header is cut with an ellipsis at narrow widths."""
    out = _wide_turn(axon_env, 40, "normal")
    head = next(ln for ln in visible_lines(out) if ln.startswith("┌ Turn 1"))
    assert head.endswith("…") and _width(head) <= 40, head


def test_code_fence_fits_narrow_width(axon_env: AxonEnv) -> None:
    """Long lines inside a fenced code block are wrapped or truncated to COLUMNS."""
    md = "```python\ndef f(x):\n    return x * 2  # a long comment that exceeds the narrow width of forty cols\n```\n"
    r = _run(axon_env, [[{"text": md}]], "--trace", "compact", env={"COLUMNS": "40"})
    assert not _overflow(r.stdout, 40), _overflow(r.stdout, 40)


def test_long_single_line_tool_output_fits_width(axon_env: AxonEnv) -> None:
    """A 5000-character single-line tool output is elided so no rendered row exceeds COLUMNS."""
    cmd = "python3 -c \"print('A'*5000)\""
    r = _run(axon_env, [[{"tool": "Bash", "args": {"command": cmd, "description": "x"}}], [{"text": "k"}]],
             env={"COLUMNS": "80"})
    assert "A" * 200 not in r.stdout
    assert not _overflow(r.stdout, 80), _overflow(r.stdout, 80)


# ─── markdown layout ───────────────────────────────────────────────────────

def _md(axon_env: AxonEnv, md: str, cols: int = 100) -> list[str]:
    r = _run(axon_env, [[{"text": md}]], "--trace", "compact", env={"COLUMNS": str(cols)})
    assert r.code == 0, str(r)
    return _answer_block(r.stdout)


def _table_rows(lines: list[str]) -> list[str]:
    return [ln.strip() for ln in lines if ln.strip()[:1] in ("┌", "│", "├", "└", "+", "|")]


def test_table_rows_aligned_with_cjk_and_emoji(axon_env: AxonEnv) -> None:
    """Table rows containing CJK and emoji all have the same display width (box stays aligned)."""
    rows = _table_rows(_md(axon_env, "| 名前 | 値 |\n|:---|---:|\n| 東京 | 1 |\n| 🎉🎉 | 22 |\n| ascii | 333 |\n"))
    assert len(rows) >= 6, rows
    assert len({_width(r) for r in rows}) == 1, [(r, _width(r)) for r in rows]


def test_table_alignment_right_column(axon_env: AxonEnv) -> None:
    """A `---:` column is right aligned."""
    rows = _table_rows(_md(axon_env, "| k | num |\n|---|---:|\n| a | 1 |\n| b | 333 |\n"))
    one = next(r for r in rows if " a " in r)
    assert re.search(r"\s1 │$", one), one


def test_narrow_table_wraps_cells_and_stays_aligned(axon_env: AxonEnv) -> None:
    """At 40 columns a wide table wraps its cells but every row keeps the same width ≤ 40."""
    md = "| column one | column two header | three |\n|---|---|---|\n| a fairly long cell value | 二中文字 | 🎉 |\n"
    rows = _table_rows(_md(axon_env, md, cols=40))
    widths = {_width(r) for r in rows}
    assert len(widths) == 1 and max(widths) <= 38, [(r, _width(r)) for r in rows]


def test_bullet_lists_and_nesting(axon_env: AxonEnv) -> None:
    """`-` lists render as • bullets; nested items as ◦ and indented."""
    lines = _md(axon_env, "- outer\n  - inner nested\n- second\n")
    joined = "\n".join(lines)
    assert "• outer" in joined and "• second" in joined
    inner = next(ln for ln in lines if "inner nested" in ln)
    assert "◦" in inner and inner.index("◦") > lines[[i for i, ln in enumerate(lines) if "outer" in ln][0]].index("•")


def test_ordered_list_numbers(axon_env: AxonEnv) -> None:
    """Ordered lists keep their numbers."""
    joined = "\n".join(_md(axon_env, "1. first\n2. second\n3. third\n"))
    assert re.search(r"1\. first", joined) and re.search(r"3\. third", joined)


def test_task_list_checkboxes(axon_env: AxonEnv) -> None:
    """`- [ ]` / `- [x]` become ☐ / ☑."""
    joined = "\n".join(_md(axon_env, "- [ ] todo item\n- [x] done item\n"))
    assert "☐ todo item" in joined and "☑ done item" in joined


def test_heading_rendered_without_hashes(axon_env: AxonEnv) -> None:
    """`# Title` is rendered as a heading (with an underline rule) and the `#` marker is removed."""
    lines = _md(axon_env, "# Big Title\n\ntext\n")
    joined = "\n".join(lines)
    assert "Big Title" in joined and "# Big" not in joined
    assert any(set(ln.strip()) <= {"━", "═", "─", "="} and ln.strip() for ln in lines), lines


def test_emphasis_markers_removed(axon_env: AxonEnv) -> None:
    """Bold, italic and strike markers are consumed by the renderer."""
    joined = "\n".join(_md(axon_env, "A **bold** and *italic* and ~~gone~~ word\n"))
    assert "A bold and italic and gone word" in joined
    assert "**" not in joined and "~~" not in joined


def test_code_fence_verbatim_with_language_label(axon_env: AxonEnv) -> None:
    """A fenced block shows a `── python` label and keeps indentation verbatim."""
    joined = "\n".join(_md(axon_env, "```python\ndef f(x):\n    if x:\n        return 1\n```\n"))
    assert "── python" in joined
    assert "def f(x):" in joined and "    if x:" in joined and "        return 1" in joined
    assert "```" not in joined


def test_blockquote_and_rule(axon_env: AxonEnv) -> None:
    """Block quotes get a ▎ gutter and `---` becomes a horizontal rule."""
    lines = _md(axon_env, "> quoted text here\n\n---\n\nafter\n")
    assert any("▎ quoted text here" in ln for ln in lines)
    assert any(ln.strip() and set(ln.strip()) == {"─"} for ln in lines), lines


def test_link_text_kept(axon_env: AxonEnv) -> None:
    """A markdown link shows its text without the bracket syntax."""
    joined = "\n".join(_md(axon_env, "See [the docs](https://example.com) now.\n"))
    assert "See the docs now." in joined


def test_link_across_wrap_boundary_not_lost(axon_env: AxonEnv) -> None:
    """A paragraph with a link that straddles the wrap column is rendered completely."""
    md = "Use `inline code` and *italic* and ~~strike~~ and [a link](https://example.com).\n"
    joined = " ".join(ln.strip() for ln in _md(axon_env, md, cols=60))
    assert "a link." in joined and "[a" not in joined, joined


def test_cjk_paragraph_wraps_within_width(axon_env: AxonEnv) -> None:
    """A long CJK paragraph wraps by display width (each CJK char counts as 2 columns)."""
    lines = _md(axon_env, "中文字符测试" * 12 + "\n", cols=40)
    body = [ln for ln in lines if "中" in ln or "测" in ln]
    assert len(body) >= 3 and all(_width(ln) <= 40 for ln in body), [(ln, _width(ln)) for ln in body]
    assert "".join(ln.strip() for ln in body) == "中文字符测试" * 12


def test_emoji_text_wraps_within_width(axon_env: AxonEnv) -> None:
    """Emoji-heavy text wraps by display width without exceeding COLUMNS."""
    lines = _md(axon_env, "🚀 launch " * 15 + "\n", cols=40)
    body = [ln for ln in lines if "🚀" in ln]
    assert len(body) >= 3 and all(_width(ln) <= 40 for ln in body), [(ln, _width(ln)) for ln in body]


def test_mixed_cjk_in_list_items(axon_env: AxonEnv) -> None:
    """List items with CJK continue with a hanging indent under the bullet."""
    lines = _md(axon_env, "- 二 " + "中文字符测试" * 6 + "\n", cols=40)
    items = [ln for ln in lines if "中" in ln or "二" in ln]
    assert items[0].lstrip().startswith("•")
    assert all(_width(ln) <= 40 for ln in items)
    assert all(ln.startswith("    ") for ln in items[1:]), items


# ─── long tool output ──────────────────────────────────────────────────────

def _seq_turn(axon_env: AxonEnv, n: int, level: str) -> str:
    scripts = [[{"tool": "Bash", "args": {"command": f"seq 1 {n}", "description": "count"}}], [{"text": "k"}]]
    return _run(axon_env, scripts, "--trace", level).stdout


def test_long_tool_output_truncated_in_normal_trace(axon_env: AxonEnv) -> None:
    """300 output lines show the first rows and a `… +N lines` marker; middle lines are hidden."""
    out = _seq_turn(axon_env, 300, "normal")
    assert re.search(r"… \+\d+ more lines · /output \d+ to view all", out), out[-1500:]
    lines = [ln.strip("│ ") for ln in visible_lines(out)]
    assert "2" in lines and "150" not in lines


def test_long_tool_output_detailed_shows_more_but_bounded(axon_env: AxonEnv) -> None:
    """The detailed trace shows a longer preview but still elides most of 300 lines."""
    out = _seq_turn(axon_env, 300, "detailed")
    lines = [ln.strip("│ ") for ln in visible_lines(out)]
    assert "10" in lines and "200" not in lines
    assert re.search(r"\+\d+ more lines", out)


def test_long_tool_output_compact_hint(axon_env: AxonEnv) -> None:
    """The compact trace says how to see truncated output in full (/output <n>)."""
    out = _seq_turn(axon_env, 300, "compact")
    assert re.search(r"… \+\d+ more lines · /output \d+ to view all", out), out


def test_huge_tool_output_head_and_tail_sent_to_model(axon_env: AxonEnv) -> None:
    """A huge Bash output is cut in the middle: the model receives both head and tail plus an elision marker."""
    _seq_turn(axon_env, 200000, "compact")
    res = axon_env.llm.completion_requests()[-1].tool_results()[0]
    assert res.startswith("1\n2\n3\n") and res.rstrip().endswith("199999\n200000")
    assert "elided" in res and len(res) < 400_000


def test_short_tool_output_not_truncated(axon_env: AxonEnv) -> None:
    """A 3-line output is shown in full with no elision marker."""
    out = _seq_turn(axon_env, 3, "normal")
    assert "+" not in "".join(ln for ln in visible_lines(out) if "lines" in ln and "…" in ln)
    assert not re.search(r"… \+\d+ lines", out)
