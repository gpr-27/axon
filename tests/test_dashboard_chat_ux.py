"""Chat and layout clarity of the web app: phone width, Axon's own change list, tabs, Stop, plain words, contrast.

The page script is a string, so these tests check the generated JS and CSS, run the pure helpers in node when it
is installed, and compute the contrast of the real theme colours."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from axon.features import dashboard as dash
from axon.features.dash_css import APP_LAYOUT_CSS
from axon.features.dashboard_web import APP_JS

NODE = shutil.which("node")


def _fn(name: str) -> str:
    """The source of one top-level JS function (or const) from the page script."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^}\n", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def _node(code: str) -> str:
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_page_script_parses():
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "app.js"
        f.write_text(APP_JS, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr


# 1. phone width
def test_phone_layout_keeps_rail_bar_and_composer_visible():
    css = APP_LAYOUT_CSS
    m = re.search(r"@media \(max-width:900px\)\{\s*\.ide\{height:100vh.*?\n\}", css[css.index("clarity pass"):], re.S)
    assert m, "phone block"
    block = m.group(0)
    assert "min(var(--bh,300px),42dvh)" in block
    assert "side-nav{grid-column:1;grid-row:2" in block
    small = css[css.index("@media (max-width:720px){\n .top{gap:6px"):]
    assert ".ws-chip{flex:1 1 0;min-width:0" in small and ".top .brand span:last-child{display:none}" in small
    assert "tbicons" in APP_JS and True  # the top bar is icon buttons (they fit a phone); the bottom dock is gone
    assert css.count("/* ---- IDE frame") == 1, "the IDE styles must not be repeated"


# 2. Axon's own change list
def test_changed_files_come_from_axon_tool_results_not_only_git():
    assert "function trackTouched(" in APP_JS and "C.touched" in APP_JS
    assert "case 'tool_result': trackTouched(C, ev)" in APP_JS
    assert "No uncommitted changes" not in APP_JS
    # the Progress panel was rebuilt (wbRail and its Changes section); the rule is unchanged
    rail = _fn("wbChangesSection")
    assert "C.touched" in rail and "d.files" in rail and "wbRail(C)" in _fn("drawRail")


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_track_touched_records_a_created_file_in_a_non_git_folder():
    out = _node("""
const state = { ws: { workspace: '/w/proj' } };
const EDIT_NAMES = ['Write', 'Edit', 'MultiEdit', 'Patch', 'NotebookEdit', 'Notebook'];
%s
%s
const C = {};
C.uses = { a: { name: 'Write', input: { file_path: '/w/proj/notes.txt' } }, b: { name: 'Edit', input: { file_path: '/w/proj/src/x.py' } }, c: { name: 'Write', input: { file_path: '/w/proj/bad.txt' } } };
trackTouched(C, { tool_use_id: 'a', is_error: false });
trackTouched(C, { tool_use_id: 'b', is_error: false });
trackTouched(C, { tool_use_id: 'c', is_error: true });
trackTouched(C, { tool_use_id: 'z', is_error: false, changes: [{ kind: 'create', path: '/w/proj/new.md' }] });
console.log(JSON.stringify(C.touched));
""" % (_fn("relPath"), _fn("trackTouched")))
    assert json.loads(out) == {"notes.txt": "A", "src/x.py": "M", "new.md": "A"}


# 3. tabs
def test_tabs_scroll_have_an_overflow_menu_and_close_others():
    for needle in ("id: 'tabscroll'", "function openTabMenu(", "function closeOthers(", "Close the other ", "contextmenu", "const SINGLE_TAB = ["):
        assert needle in APP_JS, needle
    assert "'settings'" in re.search(r"const SINGLE_TAB = \[(.*?)\]", APP_JS).group(1)
    assert ".tabscroll{" in APP_LAYOUT_CSS and "overflow-x:auto" in APP_LAYOUT_CSS


# 4. Stop
def test_stop_says_stopped_and_holds_queued_messages():
    assert "notice('Stopped', '')" in APP_JS and "notice('Stopped.', 'warn')" not in APP_JS
    assert "Stopping..." not in APP_JS
    assert "replace(/\\[Response interrupted by user\\]/g, 'Stopped')" in APP_JS
    stop = _fn("stopTurn")
    assert "/api/chat/queue/remove" in stop and "C.held" in stop
    held = _fn("drawHeld")
    assert "'Send now'" in held and "'Remove'" in held
    assert "stopTurn(C, { keep: C.lastQueued })" in APP_JS, "Send now must still run the message it just queued"


# 5. plain words
def test_effort_and_cache_words_are_plain_unless_detailed():
    assert "quantum: 'Highest'" in APP_JS and "synapse: 'High'" in APP_JS
    assert "item('effort', effortWord(s.effort || info.effort)" in APP_JS
    assert "'sdetail dtl'" in APP_JS and "tok(u.cache_read_tokens) + ' cached'" in APP_JS     # tokens and cache live in the Detailed line of a step
    assert "orange: cache missed" not in APP_JS
    if NODE:
        out = _node("""
const state = { trace: 'normal' };
%s
%s
const a = effortWord('quantum'); state.trace = 'detailed'; console.log(a + '|' + effortWord('quantum') + '|' + effortWord(''));
""" % ("const EFFORT_WORD = { reflex: 'Low', low: 'Low', balanced: 'Medium', medium: 'Medium', synapse: 'High', high: 'High', quantum: 'Highest', xhigh: 'Highest', max: 'Highest', hyper: 'Highest' };", _fn("effortWord")))
        assert out == "Highest|Highest (quantum)|—"


def test_model_picker_lists_current_provider_first_and_links_to_providers():
    pick = _fn("modelPicker")
    assert "/api/providers" in pick and "active.models" in pick
    assert "Set up another provider..." in pick and "#/settings/providers" in pick
    assert "for (const p of pv.providers)" not in pick, "only the provider in use is listed; the others sit behind the link"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_always_allow_names_the_exact_rule():
    out = _node("""
const state = { ws: { workspace: '/w/proj' } };
const EDIT_NAMES = ['Write', 'Edit', 'MultiEdit', 'Patch', 'NotebookEdit', 'Notebook'];
%s
%s
console.log(JSON.stringify([narrowRule('Bash', { command: 'git status --short' }), narrowRule('Bash', { command: 'ls -la' }), narrowRule('Bash', { command: 'a && b' }),
  narrowRule('Write', { file_path: '/w/proj/notes.txt' }), narrowRule('WebFetch', { url: 'https://example.com/x' }), narrowRule('Git', { subcommand: 'diff' }), narrowRule('Foo', {})]));
""" % (_fn("relPath"), _fn("narrowRule")))
    assert json.loads(out) == ["Bash(git status:*)", "Bash(ls:*)", "Bash(a && b)", "Edit(notes.txt)", "WebFetch(domain:example.com)", "Git(diff:*)", "Foo (every call)"]
    assert "'Always allow ' + narrowRule(ev.tool, a)" in APP_JS


def test_turn_controls_are_one_hover_button():
    from axon.features.dash_js_chat import JS_CHAT
    assert "'Expand all'" not in JS_CHAT and "'Collapse all'" not in JS_CHAT
    from axon.features.dash_design import DESIGN_CSS
    assert JS_CHAT.count("toggleSteps(") >= 2 and "turnToggle(" not in JS_CHAT      # the turn header is the one control
    assert ".thead{" in DESIGN_CSS and "[data-steps=closed] .tsteps" in DESIGN_CSS


def test_settings_hides_the_endpoint_under_advanced():
    from axon.features.dash_js_settings import JS_SETTINGS
    assert "' at ', cur.base_url" not in JS_SETTINGS
    assert JS_SETTINGS.count("Advanced") >= 2


def test_shell_terminal_commands_have_one_line_descriptions_and_nav_order():
    # Owner decision: Files and Terminal are right-hand panels, Shell is the fallback inside the Terminal panel, so the rail
    # no longer lists them; they stay one Cmd+K entry (NAV_PANELS) and one key away.
    nav = re.search(r"const NAV = \[(.*?)\];", APP_JS, re.S).group(1)
    ids = re.findall(r"\['(\w+)', '", nav)
    assert ids == ["chat", "commands", "sessions", "usage", "insights", "memory", "extensions", "settings"]
    panels = re.search(r"const NAV_PANELS = \[(.*?)\];", APP_JS, re.S).group(1)
    assert re.findall(r"\['(\w+)', '", panels) == ["files", "terminal", "shell"]
    assert "...NAV_PANELS.map(" in APP_JS            # Cmd+K reaches them
    for k in ("shell", "terminal", "commands"):
        assert re.search(k + r": ['\"].{20,}", APP_JS.split("const NAV_HELP")[1].split("};")[0]), k
        assert k in APP_JS.split("const PAGE_HINT")[1].split("};")[0] or k == "shell"


# 7. empty panels
def test_empty_progress_sections_collapse_to_one_line():
    rail = _fn("wbRail")
    for gone in ("No todo list yet", "No tool is running", "No subagents in this session", "No background shells", "Messages you send while a turn runs wait here"):
        assert gone not in rail, gone
    # sections with no content are left out instead of printing "Nothing yet", so the idle panel stays deliberate
    assert "Nothing yet" not in rail and "if (todos.length) kids.push(sec('Plan'" in rail and "if (running.length)" in rail and "if (bg.length)" in rail


# 6. contrast
def _lum(c: str) -> float:
    c = c.lstrip("#")
    ch = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _ratio(a: str, b: str) -> float:
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


@pytest.mark.parametrize("theme", [0, 1])
def test_text_colours_reach_4_5_to_1_on_every_surface(theme):
    t = dict(dash._theme_tokens()[theme])
    css = APP_LAYOUT_CSS
    assert ":root{--muted:var(--ink2)}" in css, "muted text must reuse the readable secondary colour"
    t["--muted"] = t["--ink2"]
    if theme == 1:                      # light theme: the primary is darkened where it is used as text
        t["--primary"] = re.search(r':root\[data-theme="light"\]\{[^}]*--primary:(#[0-9A-Fa-f]{6})', css).group(1)
    surfaces = [t[k] for k in ("--page", "--surface", "--raised", "--code-bg")]
    bad = [(fg, bg, round(_ratio(t[fg], bg), 2)) for fg in ("--ink", "--ink2", "--muted", "--primary", "--accent", "--ok", "--warn", "--bad", "--info", "--code-fg") for bg in surfaces if _ratio(t[fg], bg) < 4.5]
    assert not bad, bad


def test_terminal_colours_use_theme_tokens_and_dim_text_stays_readable():
    from axon.features.dash_js_term import JS_TERM
    ansi = re.search(r"const ANSI16 = \[(.*?)\];", JS_TERM, re.S).group(1)
    assert set(re.findall(r"var\((--[a-z0-9]+)\)", ansi)) <= {"--muted", "--bad", "--ok", "--warn", "--info", "--accent", "--primary", "--ink", "--ink2"}
    assert ".td,.ad{opacity:.92}" in APP_LAYOUT_CSS


# e2e round: Stop is not a failure, and idle hidden tabs must not hold a connection each
def test_a_stopped_turn_is_not_drawn_as_a_failure():
    fn = _fn("finishTurn")
    assert "ev.stop_reason === 'interrupted'" in fn
    assert "notice('Stopped', '')" in fn and "else if (ev && ev.is_error) cur.answer.append(turnError(" in fn


def test_hidden_idle_chat_tabs_release_their_event_stream():
    """A browser allows about six connections per site; each open stream takes one, so with six chat tabs every other
    request (even Send) hung. A hidden, idle tab closes its stream; the state poll reopens it when it matters."""
    fn = _fn("refreshState")
    assert "C.feed.offsetParent === null" in fn and "C.es.close()" in fn and "connectEvents(C, st)" in fn
    assert "if (s.none)" in _fn("connectEvents")        # a server that forgot the chat is not retried forever


def test_retry_notice_rounds_the_delay():
    assert "ev.delay_s + 's: '" not in APP_JS and "Math.round(Number(ev.delay_s || 0) * 10) / 10" in APP_JS


def test_scrolling_up_on_a_feed_that_cannot_scroll_does_not_stop_following():
    assert "e.deltaY < 0 && feed.scrollTop > 0" in APP_JS
    assert "C.atBottom = true;" in _fn("buildComposer")


def test_jump_to_latest_is_styled_by_the_stylesheet_not_a_script_made_style_element():
    """The page's content policy blocks style elements added by script, which left the button an unstyled bar."""
    assert "createElement('style')" not in APP_JS
    assert ".jump-latest{position:absolute" in APP_LAYOUT_CSS


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_markdown_lists_nest_by_indent():
    out = _node(_fn("mdEsc") + _fn("mdInline") + _fn("mdLite") + _fn("mdLiveTable") + """
const r = (s) => mdLite(s, false);
console.log(JSON.stringify([
  r('- a\\n- b\\n  - c\\n  - d\\n- e'),
  r('1. x\\n   - y\\n2. z'),
  r('- one\\n- two'),
]));""")
    a, b, c = json.loads(out)
    assert a == "<ul><li>a</li><li>b<ul><li>c</li><li>d</li></ul></li><li>e</li></ul>"
    assert b == "<ol><li>x<ul><li>y</li></ul></li><li>z</li></ol>"
    assert c == "<ul><li>one</li><li>two</li></ul>"
