"""The app's look layer and the live Markdown formatter."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from axon.features.dash_design import DESIGN_CSS
from axon.features.dash_js_md import JS_MD
from axon.features.dashboard_web import asset


def _md(text: str) -> str:
    src = JS_MD + f"\nconsole.log(mdLite({json.dumps(text)}))"
    p = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page script")


def test_look_layer_is_served_last():
    from axon.features.dash_workbench_css import WORKBENCH_CSS
    css, _ = asset("app.css")
    assert DESIGN_CSS in css and css.index(WORKBENCH_CSS) == css.index(DESIGN_CSS) + len(DESIGN_CSS)   # the workbench layer follows it directly


def test_look_layer_loads_nothing_from_outside():
    assert "http" not in DESIGN_CSS and "@import" not in DESIGN_CSS and "url(" not in DESIGN_CSS


def test_both_themes_define_the_same_colours():
    from axon.features import dash_design
    names = lambda block: {p.split(":")[0] for p in block.split(";")}  # noqa: E731
    assert names(dash_design._DARK) == names(dash_design._LIGHT)


def test_motion_stops_when_the_user_asks():
    assert "prefers-reduced-motion:reduce" in DESIGN_CSS


def _ratio(a: str, b: str) -> float:
    def lum(h: str) -> float:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, bl = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("block", ["_DARK", "_LIGHT"])
def test_text_tokens_reach_aa_on_every_surface(block):
    from axon.features import dash_design
    t = dict(p.split(":", 1) for p in getattr(dash_design, block).split(";"))
    surfaces = ("--canvas", "--page", "--raised", "--raised2", "--float", "--field", "--you", "--code-bg")
    inks = ("--ink", "--ink2", "--link", "--primary", "--accent", "--info", "--ok", "--warn", "--bad")
    bad = [(fg, bg, round(_ratio(t[fg], t[bg]), 2)) for fg in inks for bg in surfaces if _ratio(t[fg], t[bg]) < 4.5]
    assert not bad, bad
    assert _ratio(t["--on-primary"], t["--primary"]) >= 4.5


def test_tab_bar_waits_for_a_second_tab_and_turn_state_updates_live():
    from axon.features.dashboard_web import APP_JS
    assert "strip.hidden = UI.tabs.length < 2" in APP_JS
    # the tab is named by the first message, and files changed by a tool reach the panels, while the turn runs
    assert "liveTitle(C, ev.text)" in APP_JS and "liveFiles(C, ev)" in APP_JS
    live = APP_JS[APP_JS.index("function liveFiles"):]
    assert "treeReload()" in live[:live.index("// What Axon itself changed")] and "wbRefreshChanges(true)" in live[:live.index("// What Axon itself changed")]


def test_finish_layer_loads_nothing_and_is_served_after_the_type_pass():
    from axon.features.dash_design import FINISH_CSS, TYPE_TAIL
    css, _ = asset("app.css")
    assert "http" not in FINISH_CSS and "@import" not in FINISH_CSS and "url(" not in FINISH_CSS
    assert css.index(FINISH_CSS) > css.rindex(TYPE_TAIL)


@needs_node
@pytest.mark.parametrize("text,expected", [
    ("Created **notes.txt** for you.", "<strong>notes.txt</strong>"),
    ("use `pytest -q` now", "<code>pytest -q</code>"),
    ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
    ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
    ("## Title", "<h3>Title</h3>"),
    ("```py\nx = 1\n```", "<pre><code>x = 1</code></pre>"),
    ("| a | b |\n| - | - |\n| 1 | 2 |", "<td>1</td>"),
    ("[docs](https://example.com/x)", 'href="https://example.com/x"'),
])
def test_markdown_formats(text: str, expected: str):
    assert expected in _md(text)


@needs_node
@pytest.mark.parametrize("text", [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "```\n<script>x</script>\n```",
    "`<b onclick=x>`", "| <i onclick=x> | b |\n| - | - |\n| <script> | y |",
])
def test_markdown_escapes_html(text: str):
    html = _md(text)
    assert "<script" not in html and "<img" not in html and "<b " not in html and "<i " not in html


@needs_node
@pytest.mark.parametrize("text", ["[x](javascript:alert(1))", "[x](data:text/html,hi)"])
def test_only_web_links_become_links(text: str):
    assert "<a " not in _md(text)


@needs_node
def test_a_half_written_reply_does_not_break():
    assert "<pre><code>x = 1</code></pre>" in _md("Here:\n```py\nx = 1")
    assert "**bo" in _md("some **bo")
