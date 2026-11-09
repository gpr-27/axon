"""The Context Inspector page: its script and styles are served, the route and class names exist, and every row of the
right-hand Context panel links to it."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401
from axon.features.dash_js_context import CONTEXT_CSS, JS_CONTEXT  # noqa: E402
from axon.features.dash_js_workbench import JS_WORKBENCH_RAIL, JS_WORKBENCH, JS_WORKBENCH_PANELS  # noqa: E402
from axon.features.dashboard_web import APP_JS, asset  # noqa: E402

SECTIONS = ("overview", "messages", "system_prompt", "system_tools", "mcp_tools", "mcp_instructions", "skills",
            "custom_agents", "memory_files", "autocompact", "free", "permissions", "hooks")


def test_module_is_in_the_served_script_and_styles():
    assert JS_CONTEXT in APP_JS
    css, _ = asset("app.css")
    assert CONTEXT_CSS in css
    text, ctype = asset("app.js")
    assert "viewContext" in text and "javascript" in ctype


def test_served_over_http(c):
    st, _, body = c.get("/static/app.js")
    assert st == 200 and b"/api/insights/ctxinspect" in body
    st, _, css = c.get("/static/app.css")
    assert st == 200 and b".ci-card" in css


def test_all_sections_routes_and_classes():
    for key in SECTIONS:
        assert f"{key}:" in JS_CONTEXT or f"'{key}'" in JS_CONTEXT, key
    for cls in ("ci-side", "ci-main", "ci-card", "ci-gh", "ci-heat", "ci-code", "ci-sel", "ci-more", "ci-seg", "ci-state", "ci-skel"):
        assert cls in JS_CONTEXT and "." + cls in CONTEXT_CSS, cls
    assert "position:sticky" in CONTEXT_CSS and "@media (max-width:760px)" in CONTEXT_CSS
    assert "'#/context/'" in JS_CONTEXT and "hlLine(" in JS_CONTEXT and "aria-expanded" in JS_CONTEXT


def test_router_opens_the_inspector():
    from axon.features import dash_js_tools
    assert "name === 'context'" in dash_js_tools.JS_TOOLS and "viewContext(rest)" in dash_js_tools.JS_TOOLS


def test_rail_rows_link_to_the_inspector():
    rail = JS_WORKBENCH + JS_WORKBENCH_PANELS + JS_WORKBENCH_RAIL
    assert "'#/context/' + key" in rail and "cx-row lnk" in rail
    assert "'open inspector'" in rail
    for key in ("permissions", "hooks", "overview"):
        assert f"ins('{key}')" in rail
    assert "Open ' + x.label + ' in the context inspector" in rail     # accessible name of every row
