"""A submitted prompt with a big paste is echoed as the chip plus its first and last lines."""
import re

from axon.ui.input import echo_text, format_user_prompt_banner, paste_preview

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _lines(n: int) -> str:
    return "\n".join(f"line {i}" for i in range(1, n + 1))


def test_preview_keeps_three_lines_at_each_end_and_counts_the_hidden_ones():
    preview = paste_preview(_lines(915))
    assert preview == ["line 1", "line 2", "line 3", "… 909 lines hidden …", "line 913", "line 914", "line 915"]


def test_short_pastes_are_shown_whole():
    assert paste_preview(_lines(7)) == _lines(7).split("\n")      # hiding 1 line would not save any room
    assert len(paste_preview(_lines(8))) == 7                      # 2 hidden: shortened


def test_echo_adds_the_preview_after_the_chip_and_keeps_the_rest_of_the_text():
    shown = "[Pasted text #2 +915 lines] why is this slow?"
    echoed = echo_text(shown, {2: _lines(915)})
    assert echoed.startswith("[Pasted text #2 +915 lines]\n  line 1\n")
    assert "909 lines hidden" in echoed and echoed.rstrip().endswith("why is this slow?")
    assert "line 500" not in echoed


def test_unknown_chip_and_plain_text_are_left_alone():
    assert echo_text("[Pasted text #9 +40 lines]", {}) == "[Pasted text #9 +40 lines]"
    assert echo_text("just a question", {1: "x"}) == "just a question"


def test_banner_for_a_pasted_file_is_short():
    banner = ANSI.sub("", format_user_prompt_banner(echo_text("[Pasted text #2 +915 lines]", {2: _lines(915)})))
    assert len(banner.splitlines()) == 8
    assert "909 lines hidden" in banner
