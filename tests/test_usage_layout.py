"""Welcome box, path fitting, /status alignment, prompt colours and the report footer hint."""
from __future__ import annotations

import pytest
from fakes import FakeProvider
from test_core_helpers import make_agent
from test_keyboard_menus import axon_home  # noqa: F401  (fixture)

from axon.ui import theme
from axon.ui.theme import closed_box, display_width, kv_lines, middle_ellipsis, shorten_path, strip_ansi

CJK = "/Users/me/プロジェクト/日本語のとても長いディレクトリ名/😀emoji/src"


def test_shorten_path_uses_tilde_and_a_middle_ellipsis() -> None:
    p = shorten_path("/Users/gpr/Documents/gpr/Important projects /axon/src/axon/ui", 30, home="/Users/gpr")
    assert p.startswith("~/") and "…" in p and p.endswith("ui") and display_width(p) <= 30
    assert shorten_path("/Users/gpr/x", 30, home="/Users/gpr") == "~/x"
    assert shorten_path("/Users/gprX/x", 40, home="/Users/gpr") == "/Users/gprX/x"       # not a home prefix


@pytest.mark.parametrize("width", [8, 12, 20, 33])
def test_shorten_path_always_fits_even_with_cjk_and_emoji(width) -> None:
    for path in (CJK, "/" + "a" * 200, "~/" + "长" * 50):
        assert display_width(shorten_path(path, width, home="/nowhere")) <= width


def test_middle_ellipsis_counts_columns() -> None:
    out = middle_ellipsis("日本語日本語日本語日本語", 9)
    assert display_width(out) <= 9 and "…" in out


@pytest.mark.parametrize("width", [40, 41, 60, 84])
def test_closed_box_keeps_all_four_borders(width) -> None:
    rows = ["▲█▲  Axon", "中文字符😀" * 20, "", "plain " * 30, CJK]
    lines = closed_box(rows, width).split("\n")
    assert all(display_width(l) == width for l in lines), [display_width(l) for l in lines]
    plain = [strip_ansi(l) for l in lines]
    assert plain[0].lstrip().startswith("╭") and plain[0].endswith("╮")
    assert plain[-1].lstrip().startswith("╰") and plain[-1].endswith("╯")
    assert all(l.endswith("│") for l in plain[1:-1])


@pytest.mark.parametrize("cols", [40, 52, 60, 100, 200])
def test_welcome_box_fits_any_width(cols, monkeypatch, workspace) -> None:
    from axon.ui import render
    monkeypatch.setattr(render, "term_width", lambda: cols)
    r = render.Renderer()
    deep = workspace / ("very-long-directory-name-" * 4) / "名前" / "project"
    deep.mkdir(parents=True)
    text = r.banner_text("0.29.1", "claude-sonnet-4-6", "high", str(deep), "bypass", "")
    lines = [l for l in text.split("\n") if l.strip()]
    assert all(display_width(l) <= cols for l in lines), [display_width(l) for l in lines]
    plain = [strip_ansi(l) for l in lines]
    assert plain[0].lstrip().startswith("╭") and plain[0].rstrip().endswith("╮")
    assert plain[-1].lstrip().startswith("╰") and plain[-1].rstrip().endswith("╯")
    assert all(l.rstrip().endswith("│") for l in plain[1:-1])
    assert len({display_width(l) for l in lines}) == 1                 # one straight right edge


def test_kv_lines_wrap_aligned_and_never_mid_word_for_short_values() -> None:
    rows = kv_lines("Workspace", "/private/tmp/very/long/path/to/the/project/directory/here (git: main)", 60, label_w=18)
    assert len(rows) >= 2 and all(display_width(r) <= 60 for r in rows)
    col = len(rows[0]) - len(rows[0].split("Workspace", 1)[1].lstrip()) - 0
    assert all(r.startswith(" " * 22) for r in rows[1:]) and col > 0
    long_path = kv_lines("Storage", "/" + "/".join(["dir" * 5] * 30), 60, label_w=18)
    assert len(long_path) <= 3 and "…" in "".join(long_path)


def test_status_paths_wrap_and_align(workspace, axon_home, capsys, monkeypatch) -> None:  # noqa: F811
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.term_width", lambda: 60)
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/status", agent)
    out = strip_ansi(capsys.readouterr().out).split("\n")
    assert max(display_width(l) for l in out) <= 60
    assert any(l.strip().startswith("Workspace") for l in out)


# ─── prompt colours ─────────────────────────────────────────────────────────

def test_prompt_colours_are_darker_on_the_light_theme() -> None:
    assert theme.PROMPT_COLOR_256["yellow"] == (221, 136) and theme.PROMPT_COLOR_256["orange"] == (215, 166)
    for name, (dark, light) in theme.PROMPT_COLOR_256.items():
        assert dark != light, name
    prev = theme.current_theme()
    try:
        theme._current_theme = "dark"
        assert theme.prompt_color_256("yellow") == 221 and theme.prompt_color_256("grey") == 245
        theme._current_theme = "light"
        assert theme.prompt_color_256("yellow") == 136 and theme.prompt_color_256("orange") == 166
        assert theme.prompt_color_256("nonsense") is None
    finally:
        theme._current_theme = prev


def test_color_command_has_no_hard_coded_codes() -> None:
    import inspect
    from axon.commands import prefs_cmds
    src = inspect.getsource(prefs_cmds)
    assert "38;5;221" not in src and "38;5;215" not in src


# ─── the report footer hint replaces "What next?" ───────────────────────────

def test_report_ends_with_one_hint_not_a_menu(workspace, axon_home, monkeypatch, capsys) -> None:  # noqa: F811
    from axon.commands import usage_menu
    from axon.commands.registry import dispatch_command, resolve_builtin
    monkeypatch.delenv("AXON_NEXT_MENU", raising=False)
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown: list[str] = []
    monkeypatch.setattr(usage_menu, "run_next_menu", lambda a, c: shown.append(c.name))
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/status", agent)
    out = strip_ansi(capsys.readouterr().out)
    assert shown == [] and "related:" in out and "/" not in out.split("related:")[1]
    assert usage_menu.footer_hint(resolve_builtin("status")).startswith("related: usage")
    monkeypatch.setenv("AXON_NEXT_MENU", "1")
    dispatch_command("/status", agent)
    assert shown == ["status"]            # the old menu is still there behind the setting


def test_usage_command_prints_plain_text_without_a_terminal(workspace, axon_home, monkeypatch, capsys) -> None:  # noqa: F811
    from axon.commands.registry import dispatch_command, resolve_builtin
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: False)
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/usage", agent)
    out = capsys.readouterr().out
    assert "== Usage ==" in out and "== Context ==" in out and "\x1b" not in out
    assert resolve_builtin("usage").name == "usage" and resolve_builtin("cost").name == "cost"
    dispatch_command("/usage context", agent)
    assert "== Status ==" not in capsys.readouterr().out
