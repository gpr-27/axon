"""The Appearance tab: preferences, safe CSS values, font detection, themes and the served assets."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from axon.features.dash_appearance import APPEARANCE_CSS, APPEARANCE_JS
from axon.features.dash_design import DESIGN_CSS
from axon.features.dash_js_settings import JS_SETTINGS
from axon.features.dashboard_web import INDEX_HTML, asset

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page script")

# A small stand-in for the browser: enough DOM, storage, canvas text measuring and matchMedia to run the scripts.
FAKE = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.className = ''; this.value = ''; this.checked = false; this.parent = null; this.textContent = '';
    const props = {}; this.style = { props, setProperty: (k, v) => { props[k] = v; }, removeProperty: (k) => { delete props[k]; }, getPropertyValue: (k) => props[k] }; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5)] = String(v); if (this === root) fire(); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; if (this === root) fire(); }
  append(...k) { k.forEach((c) => { if (c && c.tag) c.parent = this; this.children.push(c); }); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatch(t, ev) { (this.listeners[t] || []).forEach((f) => f(Object.assign({ target: this, key: '' }, ev))); }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === document.body; }
  get text() { return this.children.map((c) => (c && c.tag ? c.text : String(c))).join('') + this.textContent; }
  all(pred, out = []) { this.children.forEach((c) => { if (c && c.tag) { if (pred(c)) out.push(c); c.all(pred, out); } }); return out; }
}
const root = new El('html'); const fire = () => { if (root._mo) root._mo(); };
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
const INSTALLED = (typeof INSTALLED_FONTS !== 'undefined' ? INSTALLED_FONTS : []);
global.document = { documentElement: root, body: new El('body'), activeElement: null,
  createElement: (t) => { const e = new El(t); if (t === 'canvas') e.getContext = () => { const ctx = { font: '', measureText: (s) => { const m = /"([^"]+)"/.exec(ctx.font); const base = /(monospace|sans-serif|serif)$/.exec(ctx.font)[1]; const w = { monospace: 10, serif: 11, 'sans-serif': 12 }[base]; return { width: s.length * w + (m && INSTALLED.includes(m[1]) ? 3 : 0) }; } }; return ctx; }; return e; },
  createElementNS: (n, t) => new El(t), createTextNode: (s) => String(s) };
global.MutationObserver = class { constructor(f) { root._mo = f; } observe() {} };
let dark = false; global.matchMedia = () => ({ get matches() { return dark; }, addEventListener() {} });
global.window = global;
global.toast = () => {}; global.errorBox = (m) => new El('err');
global.svgIcon = (d) => { const e = new El('svg'); e.attrs.d = d; return e; };
global.h = function (tag, props, ...kids) { const el = new El(tag); for (const [k, v] of Object.entries(props || {})) { if (v == null || v === false) continue; if (k === 'class') el.className = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? '' : v); } el.append(...kids.flat(3).filter((x) => x != null && x !== false)); return el; };
"""


def _run(body: str, fonts: list[str] | None = None, with_settings: bool = False) -> dict:
    src = f"const INSTALLED_FONTS = {json.dumps(fonts or [])};\n" + FAKE + APPEARANCE_JS + (JS_SETTINGS if with_settings else "") + "\n" + body
    p = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


STATE = "const AP = window.AxonAppearance; const P = () => root.style.props; const A = (k) => root.getAttribute(k);\n"


def test_assets_are_served_and_registered():
    js, ctype = asset("appearance.js")
    assert js == APPEARANCE_JS and ctype.startswith("text/javascript")
    assert '<script src="/static/appearance.js"></script>' in INDEX_HTML       # in <head>, before first paint
    assert INDEX_HTML.index("appearance.js") < INDEX_HTML.index("<body>")
    css, _ = asset("app.css")
    assert APPEARANCE_CSS in css and DESIGN_CSS in css and css.index(DESIGN_CSS) > css.index(APPEARANCE_CSS)


def test_no_outside_loads_or_proprietary_font_names():
    for text in (APPEARANCE_JS, APPEARANCE_CSS, JS_SETTINGS.split("// ---------- appearance")[1]):
        assert "http://" not in text and "https://" not in text and "@import" not in text and "url(" not in text
        assert "Anthropic" not in text and "Tiempos" not in text and "Styrene" not in text
    assert "@font-face" not in APPEARANCE_CSS


@needs_node
def test_defaults_set_every_variable():
    r = _run(STATE + "console.log(JSON.stringify({p: P(), theme: A('data-theme'), motion: A('data-motion')}))")
    p = r["p"]
    assert p["--size-response"] == "16px" and p["--size-code"] == "13px" and p["--measure-chat"] == "780px"
    assert p["--font-response"] == '"Iowan Old Style",Charter,"Source Serif 4",Georgia,serif'
    assert p["--font-code"].startswith("ui-monospace") and p["--font-ui"].startswith("ui-sans-serif")
    assert r["theme"] is None and r["motion"] is None          # follow the system, no overrides


@needs_node
def test_theme_applies_instantly_and_stays_in_step_with_the_toggle():
    r = _run(STATE + """
    AP.set({theme: 'dark'}); const a = A('data-theme'), legacy = localStorage.getItem('axon-theme');
    root.setAttribute('data-theme', 'light');                 // what the top-bar toggle does
    const b = AP.get().theme, saved = JSON.parse(localStorage.getItem('axon-appearance')).theme;
    AP.set({theme: 'system'}); const c = A('data-theme'), gone = localStorage.getItem('axon-theme');
    AP.set({contrast: true}); const hc = A('data-contrast');
    console.log(JSON.stringify({a, legacy, b, saved, c, gone, hc}))""")
    assert r == {"a": "dark", "legacy": "dark", "b": "light", "saved": "light", "c": None, "gone": None, "hc": "high"}


@needs_node
def test_sizes_widths_and_motion():
    r = _run(STATE + """
    AP.set({rsize: 20, csize: 15, width: 'full', motion: 'reduced'});
    const full = {r: P()['--size-response'], c: P()['--size-code'], w: P()['--measure-chat'], m: A('data-motion'), ds: A('data-ap-size'), dc: A('data-ap-code')};
    AP.set({width: 960, rsize: 17, csize: 99}); console.log(JSON.stringify({full, w: P()['--measure-chat'], r: AP.get().rsize, c: AP.get().csize}))""")
    assert r["full"] == {"r": "20px", "c": "15px", "w": "100%", "m": "reduced", "ds": "", "dc": ""}
    assert r["w"] == "960px" and r["r"] == 16 and r["c"] == 13            # values outside the offered list fall back


@needs_node
def test_code_font_is_sanitized_before_css():
    r = _run(STATE + """
    AP.set({cfont: 'Fira Code"};}body{background:url(//x)/*'});
    const cleaned = AP.get().cfont, stack = P()['--font-code'];
    AP.set({cfont: '  JetBrains   Mono-NL '}); const spaced = P()['--font-code'];
    console.log(JSON.stringify({cleaned, stack, spaced, direct: AP.sanitizeFont('a;b{c}d:e\\\\f<g>'), long: AP.sanitizeFont('x'.repeat(99)).length}))""")
    assert r["cleaned"] == "Fira Codebodybackgroundurlx"
    assert all(ch not in r["stack"] for ch in ";{}()/*<>\\") or r["stack"].count('"') == 2
    assert r["spaced"].startswith('"JetBrains Mono-NL",ui-monospace')
    assert r["direct"] == "abcdefg" and r["long"] == 40


@needs_node
def test_tampered_storage_is_ignored():
    r = _run("localStorage.setItem('axon-appearance', JSON.stringify({theme: 'x', cfont: '};evil{', rsize: 5000, ui: '__proto__', codeDark: 'nope'}));\n"
             + STATE.replace("const AP = window.AxonAppearance;", "AP0 = window.AxonAppearance;").replace("const P", "const P")
             + "AP0.apply(); const n = AP0.normalize(JSON.parse(localStorage.getItem('axon-appearance'))); console.log(JSON.stringify(n))")
    assert r == {"theme": "system", "contrast": False, "ui": "system", "response": "serif", "rsize": 16, "width": 780, "motion": "system",
                 "codeLight": "axon-light", "codeDark": "axon-dark", "cfont": "evil", "csize": 13}


@needs_node
def test_fonts_are_detected_by_measuring_and_hidden_when_missing():
    base = STATE + "console.log(JSON.stringify({inter: AP.installed('Inter'), sf: AP.installed('SF Pro Text'), serif: AP.serifInUse(), ui: P()['--font-ui']}))"
    none = _run(base)
    assert none["inter"] is False and none["sf"] is False and none["serif"] == "the browser serif"
    some = _run(STATE + "AP.set({ui: 'inter'}); console.log(JSON.stringify({inter: AP.installed('Inter'), serif: AP.serifInUse(), ui: P()['--font-ui']}))", fonts=["Inter", "Charter", "Georgia"])
    assert some["inter"] is True and some["serif"] == "Charter" and some["ui"].startswith('"Inter",ui-sans-serif')
    missing = _run(STATE + "AP.set({ui: 'sfpro'}); console.log(JSON.stringify({ui: P()['--font-ui'], attr: A('data-ap-ui')}))")
    assert missing["ui"].startswith("ui-sans-serif") and missing["attr"] is None      # an uninstalled choice does nothing


@needs_node
def test_code_theme_follows_the_scheme_and_uses_diff_variables():
    r = _run(STATE + """
    AP.set({theme: 'dark', codeDark: 'ink-dark'}); const d = {id: A('data-code-theme'), bg: P()['--code-bg'], add: P()['--add-bg'], del: P()['--del-bg'], k: P()['--tk-k']};
    AP.set({theme: 'light', codeLight: 'paper-light'}); const l = {id: A('data-code-theme'), bg: P()['--code-bg']};
    AP.set({codeLight: 'axon-light'}); const plain = {id: A('data-code-theme'), bg: P()['--code-bg'], k: P()['--tk-k']};
    console.log(JSON.stringify({d, l, plain}))""")
    assert r["d"] == {"id": "ink-dark", "bg": "#002B36", "add": "#0F3D2E", "del": "#4A2428", "k": "#B4C72B"}
    assert r["l"] == {"id": "paper-light", "bg": "#FDF6E3"}
    assert r["plain"] == {"id": None}      # no overrides left (undefined values are dropped by JSON)          # the app's own colours stay untouched
    assert "--add-bg" in APPEARANCE_JS and "--del-bg" in APPEARANCE_JS
    for t in ("c", "s", "n", "k", "f"):
        assert f"[data-code-theme] .tk-{t}" in APPEARANCE_CSS


@needs_node
def test_reset_restores_defaults_and_clears_storage():
    r = _run(STATE + """
    AP.set({theme: 'dark', rsize: 20, cfont: 'Menlo', motion: 'reduced', contrast: true}); AP.reset();
    console.log(JSON.stringify({p: AP.get(), theme: A('data-theme'), motion: A('data-motion'), hc: A('data-contrast'), size: P()['--size-response'], saved: localStorage.getItem('axon-appearance')}))""")
    assert r["p"] == {"theme": "system", "contrast": False, "ui": "system", "response": "serif", "rsize": 16, "width": 780, "motion": "system",
                      "codeLight": "axon-light", "codeDark": "axon-dark", "cfont": "", "csize": 13}
    assert (r["theme"], r["motion"], r["hc"], r["size"], r["saved"]) == (None, None, None, "16px", None)


@needs_node
def test_storage_failures_do_not_break_it():
    r = _run("global.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };\n"
             + STATE + "AP.set({theme: 'dark'}); console.log(JSON.stringify({t: A('data-theme')}))")
    assert r["t"] == "dark"


@needs_node
def test_settings_page_has_every_row_and_controls_work():
    r = _run(STATE + """
    const body = h('div'); document.body.append(body); appearanceTab(body);
    const labels = body.all((e) => e.tag === 'b').map((e) => e.text);
    const seg = (name) => body.all((e) => e.attrs['aria-label'] === name)[0];
    const pick = (name, v) => seg(name).all((e) => e.tag === 'button' && e.dataset.v === String(v))[0].dispatch('click');
    const pressed = (name) => seg(name).all((e) => e.attrs['aria-pressed'] === 'true').map((e) => e.dataset.v);
    pick('Colour theme', 'dark'); const t1 = [A('data-theme'), pressed('Colour theme')];
    pick('Response text size', 20); pick('Conversation width', 'full'); pick('Motion', 'reduced'); pick('Code font size', 14); pick('Response font', 'match');
    const font = body.all((e) => e.attrs['aria-label'] === 'Code font name')[0]; font.value = 'Fira Code;'; font.dispatch('change');
    const themeBtns = seg('Colour theme').all((e) => e.tag === 'button').map((e) => e.text);
    const uiOptions = body.all((e) => e.attrs['aria-label'] === 'Interface font')[0].all((e) => e.tag === 'option').map((e) => e.text);
    const state = AP.get(); const resp = P()['--font-response'];
    const reset = body.all((e) => e.tag === 'button' && e.text === 'Reset to defaults')[0]; reset.dispatch('click');
    console.log(JSON.stringify({labels, t1, themeBtns, uiOptions, state, resp, after: AP.get().rsize, pressedAfter: pressed('Response text size'), font: font.value}))""", with_settings=True)
    for label in ("Colour theme", "High contrast", "Interface font", "Response font", "Response text size", "Conversation width", "Animations",
                  "Light code theme", "Dark code theme", "Code font", "Code font size"):
        assert label in r["labels"], label
    assert r["t1"] == ["dark", ["dark"]] and r["themeBtns"] == ["System", "Light", "Dark"]
    assert r["uiOptions"] == ["System default"]                          # Inter and SF Pro are hidden when not installed
    s = r["state"]
    assert (s["rsize"], s["width"], s["motion"], s["csize"], s["response"], s["cfont"]) == (20, "full", "reduced", 14, "match", "Fira Code")
    assert r["resp"] == "var(--font-ui)" and r["after"] == 16 and r["pressedAfter"] == ["16"] and r["font"] == ""


@needs_node
def test_settings_tab_is_first_and_default():
    assert JS_SETTINGS.index("['appearance', 'Appearance']") < JS_SETTINGS.index("['providers', 'Providers']")
    assert "(rest || 'appearance')" in JS_SETTINGS
    r = _run("console.log(JSON.stringify({ok: typeof viewSettings === 'function'}))", with_settings=True)
    assert r["ok"]
