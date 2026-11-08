"""Words appear as they arrive: the unfinished line shows in the live area, and scrollback only gets finished rows."""
from __future__ import annotations

from axon.ui import render
from axon.ui.markdown import MarkdownStream
from axon.ui.theme import display_width, strip_ansi


def _md(text: str, width: int = 80) -> MarkdownStream:
    md = MarkdownStream(width=width)
    md.feed(text)
    return md


def test_unfinished_line_is_previewed():
    assert "The quick brown" in strip_ansi(_md("The quick brown").preview())


def test_nothing_pending_means_no_preview():
    assert _md("Done.\n").preview() == ""
    assert MarkdownStream(width=80).preview() == ""


def test_preview_never_wraps():
    preview = _md("word " * 5).preview()
    assert display_width(preview) <= 80
    long_token = _md("x" * 500, width=60).preview()
    assert display_width(long_token) <= 60


def test_preview_keeps_the_newest_words_when_the_line_is_too_long():
    assert "END" in strip_ansi(_md("y" * 500 + "END", width=60).preview())


def test_preview_covers_only_the_part_not_yet_printed():
    md = MarkdownStream(width=40)
    printed = md.feed("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo")
    preview = strip_ansi(md.preview())
    assert "kilo" in preview
    assert "alpha" not in preview and printed


def test_preview_and_final_rows_contain_the_same_words():
    text = "The quick brown fox jumps over the lazy dog and keeps running across the field"
    md = MarkdownStream(width=40)
    printed = strip_ansi(md.feed(text))
    preview = strip_ansi(md.preview())
    assert text.startswith(" ".join(printed.split()))
    assert text.endswith(preview.strip().lstrip("…"))


def test_open_inline_markup_is_shown_plain():
    assert "**bold st" in strip_ansi(_md("some **bold st").preview())


def test_closed_markup_is_styled():
    assert "**" not in strip_ansi(_md("some **bold** text").preview())


def test_code_block_line_is_previewed():
    assert "print(" in strip_ansi(_md("```python\nprint(").preview())


def test_fence_and_table_syntax_is_not_shown():
    assert _md("```").preview() == ""
    assert _md("| a | b").preview() == ""
    assert _md("| a | b |\n| - | - |\n| 1 | 2 |\n").preview() == ""


def test_control_characters_never_reach_the_preview():
    assert "\x1b[2J" not in _md("hello \x1b[2J world").preview()


def test_spinner_draws_the_preview_above_its_line():
    spinner = render.Spinner()
    spinner.preview_provider = lambda: "partial words"
    text, rows = spinner.frame_text("x")
    assert strip_ansi(text).index("partial words") < strip_ansi(text).index("\n")
    assert rows == text.count("\n") + 1


def test_spinner_without_a_preview_is_unchanged():
    spinner = render.Spinner()
    text, rows = spinner.frame_text("x")
    assert rows == text.count("\n") + 1 and "partial" not in text


def test_a_failing_preview_never_breaks_the_spinner():
    spinner = render.Spinner()

    def boom() -> str:
        raise ValueError
    spinner.preview_provider = boom
    assert spinner.frame_text("x")[1] >= 1


def test_renderer_preview_uses_the_same_rail_as_final_rows():
    renderer = render.Renderer.__new__(render.Renderer)
    renderer._md = _md("partial words")
    renderer._md_railed = True
    assert "partial words" in strip_ansi(renderer._stream_preview())
    assert strip_ansi(renderer._stream_preview()).startswith(strip_ansi(render.glyph("v")))


def test_renderer_has_no_preview_outside_a_response():
    renderer = render.Renderer.__new__(render.Renderer)
    renderer._md = None
    assert renderer._stream_preview() == ""
