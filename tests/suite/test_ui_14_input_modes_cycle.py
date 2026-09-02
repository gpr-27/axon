"""Unit test for Cycling modes default -> auto -> plan -> bypass (input_modes_cycle)."""
import pytest
from pathlib import Path
from axon.ui.theme import strip_ansi, BOLD, TEAL, RST
from axon.ui.render import render_shortcuts_footer, render_side_question_box
from axon.ui.switcher import format_time_ago, load_dashboard_sessions

def test_ui_input_modes_cycle(workspace: Path):
    footer = render_shortcuts_footer()
    assert len(footer) > 0
    clean_f = strip_ansi(footer)
    assert "shell mode" in clean_f

    box = render_side_question_box("question", "answer")
    assert "Side Question" in box

    assert format_time_ago(100.0) is not None

    from unittest.mock import MagicMock
    from axon.ui.live_turn import run_interactive_turn
    from axon.ui.input import _get_mode_info
    from axon.ui.theme import SLATE, TEAL, PURPLE, GOLD, AMBER
    mock_agent = MagicMock()
    mock_renderer = MagicMock()
    mock_agent.run_turn.return_value = "turn_ok"
    res = run_interactive_turn(mock_agent, mock_renderer, "hello")
    assert res == "turn_ok"

    # Test mode info mapping
    assert _get_mode_info("default")[1] == SLATE
    assert _get_mode_info("acceptEdits")[1] == TEAL
    assert _get_mode_info("plan")[1] == PURPLE
    assert _get_mode_info("bypass")[1] == GOLD

    # Test ALL_SLASH_COMMANDS coverage
    from axon.ui.input import ALL_SLASH_COMMANDS, highlight_prompt_syntax
    cmd_names = [c[0] for c in ALL_SLASH_COMMANDS]
    assert "/subagents" in cmd_names
    assert "/main" in cmd_names
    assert "/model" in cmd_names
    assert "/effort" in cmd_names

    # Test highlight_prompt_syntax
    styled = highlight_prompt_syntax("@axon.md read this /cost !ls")
    assert "@axon.md" in styled
    assert "/cost" in styled
    assert "!ls" in styled
