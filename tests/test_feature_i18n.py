"""Interface languages and accessibility of the web app: the catalogs, the engine, the reply-language line, the routes, the
coverage count, and a check that fails on a control in the page scripts that has no accessible name."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_js_a11y, dash_js_i18n, dashboard_web  # noqa: E402
from axon.features import i18n_data  # noqa: E402
from axon.features.dash_js_a11y import find_unlabelled, scan_scripts  # noqa: E402
from test_dashboard import Client, c, env, server  # noqa: E402,F401

NODE = shutil.which("node")
SCRIPT_RANGES = {"te": (0x0C00, 0x0C7F), "hi": (0x0900, 0x097F)}


def _load(code):
    return json.loads((i18n_data.I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))


# ─── catalogs ────────────────────────────────────────────────────────────────

def test_every_language_has_every_key_with_the_same_slots():
    en = _load("en")
    assert len(en) > 300
    for code in ("te", "hi"):
        cat = _load(code)
        assert set(cat) == set(en), (code, sorted(set(en) ^ set(cat))[:5])
        for k, v in cat.items():
            assert isinstance(v, str) and v.strip(), (code, k)
            assert i18n_data.placeholders(v) == i18n_data.placeholders(en[k]), (code, k)


def test_english_strings_are_unique_so_a_string_has_one_key():
    en = _load("en")
    seen: dict[str, str] = {}
    for k, v in en.items():
        assert v not in seen, f"{k} and {seen[v]} both say {v!r}"
        seen[v] = k


@pytest.mark.parametrize("code", ["te", "hi"])
def test_translations_are_written_in_their_script_unless_kept_on_purpose(code):
    en, cat = _load("en"), _load(code)
    lo, hi = SCRIPT_RANGES[code]
    plain = [k for k, v in cat.items() if not any(lo <= ord(ch) <= hi for ch in v)]
    # a value with no letters of the language is allowed only when it is the same as English (a name that stays as it is)
    assert all(cat[k] == en[k] for k in plain), [k for k in plain if cat[k] != en[k]][:5]
    assert len(plain) <= 5


def test_catalog_values_carry_no_markup():
    for code in i18n_data.CODES:
        for k, v in _load(code).items():
            assert not re.search(r"<[a-z/!]", v, re.I), (code, k)


def test_a_corrupt_or_missing_catalog_is_empty_not_an_error(tmp_path, monkeypatch):
    (tmp_path / "te.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(i18n_data, "I18N_DIR", tmp_path)
    monkeypatch.setattr(i18n_data, "_cache", {})
    assert i18n_data.catalog("te") == {} and i18n_data.catalog("hi") == {} and i18n_data.catalog("xx") == {}


# ─── the reply-language line ─────────────────────────────────────────────────

def test_reply_line_is_off_by_default_and_names_the_language():
    assert i18n_data.reply_line("") == "" and i18n_data.reply_line(None) == "" and i18n_data.reply_line("fr") == ""
    assert "Telugu" in i18n_data.reply_line("te") and "Hindi" in i18n_data.reply_line("hi")
    assert "\n" not in i18n_data.reply_line("te")           # one line
    assert "code" in i18n_data.reply_line("te")              # says what stays as it is


def test_system_prompt_gets_the_line_only_when_the_preference_is_on(tmp_path):
    from axon.agent.prompt import build_system
    from axon.tools.registry import create_default_registry
    from tests.test_core_helpers import make_settings
    reg = create_default_registry()
    off = "\n".join(b["text"] for b in build_system(make_settings(tmp_path), reg, git_status=""))
    on = "\n".join(b["text"] for b in build_system(make_settings(tmp_path, reply_language="hi"), reg, git_status=""))
    assert "Write your replies to the user in" not in off
    assert "Write your replies to the user in Hindi" in on and on.count("Write your replies to the user in") == 1


def test_reply_language_is_read_from_settings_json(tmp_path, monkeypatch):
    from axon.config import _JSON_KEY_MAP
    assert _JSON_KEY_MAP["replyLanguage"] == "reply_language"


# ─── served assets and routes ────────────────────────────────────────────────

def test_assets_serve_the_engine_and_each_catalog():
    js, ctype = dash_js_i18n.asset("i18n.js")
    assert "window.AxonI18n" in js and "javascript" in ctype and "__EN__" not in js and "__LANGS__" not in js
    te, ctype = dash_js_i18n.asset("i18n/te.json")
    assert "json" in ctype and json.loads(te) == _load("te")
    assert dash_js_i18n.asset("i18n/xx.json") is None and dash_js_i18n.asset("i18n/../x.json") is None
    assert dashboard_web.asset("i18n.js")[0] == js and dashboard_web.asset("i18n/hi.json") is not None


def test_page_script_starts_with_the_engine_and_the_page_has_a_skip_link():
    # the page keeps its two scripts (a test pins that): the engine is the first thing app.js runs, before any page code
    app = dashboard_web.asset("app.js")[0]
    assert "window.AxonI18n = {" in app and app.index("window.AxonI18n = {") < app.index("const app = document.getElementById")
    assert dashboard_web.INDEX_HTML.count("<script") == 2
    assert 'class="skip-link" href="#main"' in dashboard_web.INDEX_HTML
    assert dashboard_web.INDEX_HTML.index("skip-link") < dashboard_web.INDEX_HTML.index('id="app"')


def test_reply_route_saves_and_validates(c, env):
    assert c.json("/api/i18n/state")["reply"] == ""
    st, _, b = c.post("/api/i18n/reply", {"lang": "te"})
    assert st == 200 and json.loads(b)["reply"] == "te"
    assert c.json("/api/i18n/state")["reply"] == "te"
    saved = json.loads((env.home / "settings.json").read_text(encoding="utf-8"))
    assert saved["replyLanguage"] == "te"
    assert c.post("/api/i18n/reply", {"lang": "klingon"})[0] == 400
    assert c.json("/api/i18n/state")["reply"] == "te"
    st, _, _ = c.post("/api/i18n/reply", {"lang": ""})
    assert st == 200 and c.json("/api/i18n/state")["reply"] == ""
    assert "replyLanguage" not in json.loads((env.home / "settings.json").read_text(encoding="utf-8"))


def test_reply_route_keeps_the_dashboard_guards(c, env):
    assert c.post("/api/i18n/reply", {"lang": "te"}, origin="http://evil.example")[0] in (403, 400)
    assert c.post("/api/i18n/reply", {"lang": "te"}, cookie=False)[0] == 401
    assert c.get("/api/i18n/state", cookie=False)[0] == 401
    assert c.json("/api/i18n/state")["reply"] == ""


def test_catalog_is_served_to_a_signed_in_page_only(c):
    st, _, b = c.get("/static/i18n/te.json")
    assert st == 200 and json.loads(b)
    assert c.get("/static/i18n/te.json", cookie=False)[0] == 401
    assert c.get("/static/i18n/xx.json")[0] == 404


# ─── coverage ────────────────────────────────────────────────────────────────

def test_coverage_counts_translated_keys_against_script_strings():
    cov = dash_js_i18n.coverage()
    assert cov["strings_in_scripts"] > 500 and cov["keys"] > 300
    for code in ("te", "hi"):
        d = cov["languages"][code]
        assert d["keys_translated"] == cov["keys"] and d["keys_missing"] == 0
        assert 0 < d["percent"] <= 100 and 0 < d["frame_percent"] <= 100
        assert d["frame_percent"] > d["percent"]             # the frame is translated first
    # a string a page script writes that is in the catalog counts as covered; an invented one does not
    assert "Send after this turn" in dash_js_i18n.source_strings()


# ─── the engine, run for real under node ─────────────────────────────────────

ENGINE_HARNESS = r"""
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
const el = { lang: '' };
global.document = { documentElement: el, createTreeWalker() { return { nextNode() { return null; } }; } };
global.window = global; global.NodeFilter = { SHOW_ELEMENT: 1, SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
const cats = JSON.parse(require('fs').readFileSync(process.env.CATS_FILE, 'utf8'));
global.fetch = (url) => Promise.resolve({ ok: true, json: () => Promise.resolve(cats[url.split('/').pop().split('.')[0]]) });
store['axon-lang'] = process.env.LANG_ID;
require(process.env.ENGINE);
const I = window.AxonI18n;
(async () => {
  await new Promise(r => setTimeout(r, 30));
  const out = {};
  out.lang = I.lang; out.htmlLang = el.lang;
  out.exact = I.tr('Send after this turn');
  out.padded = I.tr('  Cancel  ');
  out.pattern = I.tr('Tabs (3)');
  out.nested = I.tr('Expand Files');
  out.unknown = I.tr('some model output that is not in the catalog');
  out.path = I.tr('src/app/main.py');
  out.t = I.t('common.cancel');
  out.tvars = I.t('tabs.tabs_n', { n: 7 });
  out.missing = I.t('no.such.key');
  out.long = I.tr('x'.repeat(500));
  await I.setLang('en'); out.back = I.tr(process.env.CANCEL); out.afterEn = I.t('common.cancel'); out.stored = store['axon-lang'];
  console.log(JSON.stringify(out));
})();
"""


def _engine_run(tmp_path, lang):
    engine = tmp_path / "engine.js"
    engine.write_text(dash_js_i18n.asset("i18n.js")[0], encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(ENGINE_HARNESS, encoding="utf-8")
    cats = {c: _load(c) for c in i18n_data.CODES}
    (tmp_path / "cats.json").write_text(json.dumps(cats, ensure_ascii=False), encoding="utf-8")
    env = {"CATS_FILE": str(tmp_path / "cats.json"), "ENGINE": str(engine), "LANG_ID": lang, "CANCEL": cats[lang]["common.cancel"], "PATH": str(Path(NODE).parent)}
    r = subprocess.run([NODE, str(harness)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="node is not installed")
@pytest.mark.parametrize("code", ["te", "hi"])
def test_engine_translates_strings_slots_and_falls_back_to_english(tmp_path, code):
    en, cat = _load("en"), _load(code)
    o = _engine_run(tmp_path, code)
    assert o["lang"] == code and o["htmlLang"] == code
    assert o["exact"] == cat["composer.send_after_this_turn"]
    assert o["padded"] == "  " + cat["common.cancel"] + "  "                      # whitespace around a string is kept
    assert o["pattern"] == cat["tabs.tabs_n"].replace("{n}", "3")
    assert o["nested"] == cat["tabs.expand_name"].replace("{name}", cat["top.files"])       # a slot's value is translated too
    assert o["unknown"] == "some model output that is not in the catalog" and o["path"] == "src/app/main.py"
    assert o["t"] == cat["common.cancel"] and o["tvars"] == cat["tabs.tabs_n"].replace("{n}", "7")
    assert o["missing"] == "no.such.key" and o["long"] == "x" * 500
    assert o["back"] == en["common.cancel"] and o["afterEn"] == en["common.cancel"] and o["stored"] == "en"   # any language back to English


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_a_missing_translation_falls_back_to_english(tmp_path):
    cats = {"en": {"a.b": "Hello {n}"}, "te": {}, "hi": {}}
    engine = tmp_path / "engine.js"
    js = dash_js_i18n.I18N_ENGINE_JS.replace("__LANGS__", json.dumps([{"id": "en"}, {"id": "te"}])).replace("__EN__", json.dumps(cats["en"]))
    engine.write_text(js, encoding="utf-8")
    harness = tmp_path / "h.js"
    harness.write_text(ENGINE_HARNESS.split("(async")[0] + "(async () => { await new Promise(r => setTimeout(r, 30)); console.log(JSON.stringify({ v: I.t('a.b', {n: 2}), x: I.tr('Hello 5') })); })();", encoding="utf-8")
    cats_file = tmp_path / "cats.json"; cats_file.write_text(json.dumps({"te": {}}), encoding="utf-8")
    r = subprocess.run([NODE, str(harness)], capture_output=True, text=True, env={"CATS_FILE": str(cats_file), "ENGINE": str(engine), "LANG_ID": "te", "PATH": str(Path(NODE).parent)}, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"v": "Hello 2", "x": "Hello 5"}


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_whole_page_script_parses(tmp_path):
    f = tmp_path / "app.js"
    f.write_text(dashboard_web.asset("app.js")[0] + "\n", encoding="utf-8")
    g = tmp_path / "i18n.js"
    g.write_text(dash_js_i18n.asset("i18n.js")[0], encoding="utf-8")
    for p in (f, g):
        r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr


def test_app_frame_text_is_in_the_catalog():
    en = set(_load("en").values())
    for word in ("Chat", "Commands", "Sessions", "Usage", "Insights", "Memory", "Extensions", "Settings", "Progress panel", "Terminal",
                 "Changes", "Browser", "Files", "More", "Send", "Stop", "Cancel", "Delete", "Message", "Reply in this language",
                 "Interface language", "Skip to the main content", "Highest", "Asks first", "Thinking", "Done", "Failed"):
        assert word in en, word


# ─── accessibility ───────────────────────────────────────────────────────────

def test_no_control_in_the_page_scripts_lacks_an_accessible_name():
    """A button with only an icon or symbol, or a field with no label, fails here. A label comes from aria-label, aria-labelledby,
    text, or (fields) an id a <label for> can point at."""
    assert scan_scripts() == {}, scan_scripts()


def test_the_unlabelled_check_flags_what_it_should_and_passes_what_it_should():
    bad = "const b = h('button', { class: 'x', type: 'button', onclick: go }, svgIcon(ICONS.x));\nconst c = h('button', { type: 'button' }, '×');\nconst f = h('input', { type: 'text', placeholder: 'Name' });"
    assert len(find_unlabelled(bad)) == 3
    good = ("const b = h('button', { 'aria-label': 'Close', type: 'button' }, svgIcon(ICONS.x));\nconst c = h('button', { type: 'button' }, 'Save');\n"
            "const d = h('button', { type: 'button' }, svgIcon(ICONS.x), 'Open');\nconst f = h('input', { type: 'text', 'aria-label': 'Name' });\n"
            "const g = h('select', { id: 'pick' });\nconst l = h('label', null, 'Hide ', h('input', { type: 'checkbox' }));")
    assert find_unlabelled(good) == []


def test_the_layer_covers_landmarks_live_status_dialogs_menus_and_tree():
    js = dash_js_a11y.JS_A11Y
    for needle in ("role', 'main'", "aria-live", "aria-modal=true", "role=menu", "treeitem", "role=tablist", "inert", "aria-busy", "sr-live"):
        assert needle in js, needle
    assert "characterData" not in js        # token streaming must not wake the layer or the live region


def test_css_has_focus_hit_targets_and_reduced_motion():
    css = dashboard_web.asset("app.css")[0]
    assert ":focus-visible" in css and "min-height:24px" in css and "prefers-reduced-motion:reduce" in css and ".sr-only" in css
    assert "html:lang(te)" in css and "html:lang(hi)" in css
