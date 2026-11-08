"""Interface languages for the web app: the translation layer, its Settings row, its routes and the coverage count.

How it works (so it is easy to add to):
  * ``features/i18n/en.json`` holds every translatable string as ``"key": "English text"``; ``te.json`` and ``hi.json`` hold the
    same keys in Telugu and Hindi. A key missing from a language falls back to English. ``{name}`` marks a value filled in at run time.
  * New code can call ``t('key')`` or ``t('key', {name: value})``.
  * Code that already writes English literals needs no change: the page script swaps any text, ``title``, ``aria-label`` or
    ``placeholder`` that is exactly an English catalog string (or matches one with ``{name}`` slots) for the chosen language.
    Code, file paths, tool output and model replies are never touched (``pre``, ``code``, ``.md``, terminal, diffs, ``translate="no"``).
  * To translate a new string: add the key to the three JSON files. ``python -m axon.features.dash_js_i18n`` prints the coverage.

The engine is the first thing /static/app.js runs (and is also served on its own as /static/i18n.js); a chosen language's
catalog is fetched from /static/i18n/<code>.json. The choice is kept in this browser (localStorage "axon-lang"), like Appearance.
"Reply in this language" is the one server-side part: ``replyLanguage`` in settings.json, one line in the system prompt.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from axon.features.i18n_data import CODES, LANGS, catalog, placeholders, reply_line

I18N_ENGINE_JS = r"""
(function () {
"use strict";
var KEY = 'axon-lang';
var LANGS = __LANGS__;
var EN = __EN__;
var cats = { en: EN }, loading = {}, lang = 'en', subs = [], rev = {}, pats = [], mo = null;
var ATTRS = ['title', 'aria-label', 'placeholder', 'alt', 'aria-description'];
var SKIP = 'script,style,pre,code,textarea,.md,.tscreen,.xterm,.cmd-out,.sh-out,.diff,.code,.editor,[translate=no],[data-notranslate]';
var SKIP_ATTR = SKIP.replace('textarea,', '');   // a textarea's own placeholder and label are ours; its text is the person's
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* blocked storage: the choice lasts for this tab only */ } }
function valid(id) { return LANGS.some(function (l) { return l.id === id; }); }
var saved = lsGet(KEY); if (valid(saved)) lang = saved;

function add(text, key) {
  if (!text) return;
  if (/\{\w+\}/.test(text)) {
    var names = (text.match(/\{(\w+)\}/g) || []).map(function (s) { return s.slice(1, -1); });
    var parts = text.split(/\{\w+\}/), lit = parts.join('').length;
    if (lit < 3) return;
    var src = parts.map(function (p) { return p.replace(/[.*+?^$()|[\]{}\\\/]/g, '\\$&'); }).join('([\\s\\S]+?)');
    pats.push({ re: new RegExp('^' + src + '$'), names: names, key: key, lit: lit });
  } else if (!(text in rev)) rev[text] = key;
}
function rebuild() {
  rev = {}; pats = [];
  Object.keys(cats).forEach(function (id) { var c = cats[id]; Object.keys(c).forEach(function (k) { add(c[k], k); }); });
  pats.sort(function (a, b) { return b.lit - a.lit; });
}
function lookup(key) { var c = cats[lang]; return (c && c[key]) || EN[key]; }
function fill(text, vars) { return text.replace(/\{(\w+)\}/g, function (m, n) { return vars && vars[n] != null ? String(vars[n]) : ''; }); }
function t(key, vars) { var s = lookup(key); return s == null ? String(key) : fill(s, vars); }
function has(key) { return key in EN; }
// Translate a whole string that equals a catalog string in any loaded language; anything else is returned unchanged.
function xl(s) {
  if (typeof s !== 'string' || s.length > 400) return s;
  var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(s), core = m[2];
  if (core.length < 2) return s;
  var key = rev[core], out;
  if (key !== undefined) { out = lookup(key); return out == null ? s : m[1] + out + m[3]; }
  if (core.length > 200 || core.indexOf('\n') >= 0) return s;
  for (var i = 0; i < pats.length; i++) {
    var r = pats[i].re.exec(core); if (!r) continue;
    var vars = {}; pats[i].names.forEach(function (n, j) { vars[n] = xl(r[j + 1]); });
    out = lookup(pats[i].key); if (out == null) continue;
    return m[1] + fill(out, vars) + m[3];
  }
  return s;
}
function skipped(el, sel) { return !!(el && el.closest && el.closest(sel || SKIP)); }
function doText(n) { if (skipped(n.parentElement)) return; var v = n.nodeValue, o = xl(v); if (o !== v) n.nodeValue = o; }
function doEl(el, only) {
  var list = only ? [only] : ATTRS;
  for (var i = 0; i < list.length; i++) { var v = el.getAttribute(list[i]); if (v) { var o = xl(v); if (o !== v) el.setAttribute(list[i], o); } }
}
function sweep(root) {
  if (!root) return;
  if (root.nodeType === 3) { doText(root); return; }
  if (root.nodeType !== 1 && root.nodeType !== 11) return;
  if (root.nodeType === 1) { if (skipped(root, SKIP_ATTR)) return; doEl(root); }
  var w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, { acceptNode: function (n) {
    return n.nodeType === 1 && n.matches(SKIP_ATTR) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; } });
  var n, list = []; while ((n = w.nextNode())) list.push(n);
  list.forEach(function (x) { if (x.nodeType === 3) doText(x); else doEl(x); });
}
function observe() {
  if (mo || !window.MutationObserver) return;
  mo = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (m.type === 'childList') m.addedNodes.forEach(sweep);
      else if (m.type === 'characterData') doText(m.target);
      else if (m.type === 'attributes' && !skipped(m.target, SKIP_ATTR)) doEl(m.target, m.attributeName);
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
}
function apply() {
  document.documentElement.lang = lang;
  rebuild();
  if (lang !== 'en') observe();
  sweep(document.documentElement);
  subs.slice().forEach(function (f) { try { f(lang); } catch (e) { /* one broken listener must not stop the rest */ } });
}
function load(id) {
  if (cats[id]) return Promise.resolve();
  if (!loading[id]) loading[id] = fetch('/static/i18n/' + id + '.json', { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; })
    .then(function (j) { cats[id] = (j && typeof j === 'object') ? j : {}; });
  return loading[id];
}
function setLang(id) {
  if (!valid(id)) return Promise.resolve(false);
  return load(id).then(function () { lang = id; lsSet(KEY, id); apply(); return true; });
}
window.AxonI18n = { t: t, tr: xl, has: has, setLang: setLang, get lang() { return lang; }, langs: LANGS, subscribe: function (f) { subs.push(f); return function () { subs = subs.filter(function (x) { return x !== f; }); }; }, sweep: sweep };
document.documentElement.lang = lang;
if (lang !== 'en') load(lang).then(apply); else rebuild();
})();
"""

# App-scope part: `t` for every module, and the Language group of Settings > Appearance.
JS_I18N = r"""
// ---------- interface language (see dash_js_i18n.py) ----------
// t('key') or t('key', { name: value }): the chosen language, English when a key is missing. Inside a function that has its own `t`, use AxonI18n.t.
const t = (key, vars) => window.AxonI18n.t(key, vars);
function i18nGroup() {
  const I = window.AxonI18n;
  const row = (label, desc, ...ctl) => h('div', { class: 'ap-row' }, h('div', { class: 'ap-lab' }, h('b', null, label), desc ? h('span', null, desc) : null), h('div', { class: 'ap-ctl' }, ...ctl));
  const btns = I.langs.map(l => h('button', { type: 'button', 'data-v': l.id, lang: l.id, translate: 'no', 'aria-pressed': String(I.lang === l.id), onclick: () => { chooseLang(l.id); } }, l.native));
  const reply = h('input', { type: 'checkbox', id: 'reply-lang', 'aria-label': t('i18n.reply_in_this_language') });
  const note = h('span', { class: 'muted', id: 'reply-note', role: 'status' });
  let on = ''; const sync = () => { reply.checked = on !== '' && on === I.lang; reply.disabled = I.lang === 'en'; };
  const post = async (code) => { try { const r = await api('/api/i18n/reply', { lang: code, session: UI.chat ? UI.chat.sid : '' }); on = r.reply || ''; note.textContent = ''; } catch (e) { note.textContent = e.message; } sync(); };
  api('/api/i18n/state').then(s => { on = s.reply || ''; sync(); }).catch(() => {});
  reply.addEventListener('change', () => post(reply.checked ? I.lang : ''));
  async function chooseLang(id) {
    await I.setLang(id); btns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === id)));
    if (on !== '') await post(id === 'en' ? '' : id); else sync();
  }
  sync();
  return h('section', { class: 'ap-group' }, h('h2', null, t('i18n.language')), h('div', { class: 'ap-card' },
    row(t('i18n.interface_language'), t('i18n.interface_language_note'), h('div', { class: 'seg', role: 'group', 'aria-label': t('i18n.interface_language') }, ...btns)),
    row(t('i18n.reply_in_this_language'), t('i18n.reply_note'), reply, note)));
}
"""

I18N_CSS = r"""
/* Telugu and Hindi: taller glyphs and stacked marks need more line height, and a font chain that names the system fonts for each script. */
html:lang(te){--lh-extra:1.75}
html:lang(hi){--lh-extra:1.65}
html:lang(te) body,html:lang(te) button,html:lang(te) input,html:lang(te) select,html:lang(te) textarea{font-family:ui-sans-serif,-apple-system,"Segoe UI Variable","Segoe UI",system-ui,"Noto Sans Telugu","Telugu MN","Telugu Sangam MN","Nirmala UI",Gautami,sans-serif}
html:lang(hi) body,html:lang(hi) button,html:lang(hi) input,html:lang(hi) select,html:lang(hi) textarea{font-family:ui-sans-serif,-apple-system,"Segoe UI Variable","Segoe UI",system-ui,"Noto Sans Devanagari","Devanagari MT","Devanagari Sangam MN","Nirmala UI","Mangal",sans-serif}
html:lang(te) body,html:lang(hi) body{line-height:var(--lh-extra)}
html:lang(te) button,html:lang(hi) button,html:lang(te) .btn,html:lang(hi) .btn,html:lang(te) .mini,html:lang(hi) .mini{line-height:var(--lh-extra)}
html:lang(te) .md,html:lang(hi) .md{line-height:1.8}
/* long words wrap instead of pushing buttons and rows wider than their box */
html:lang(te) button,html:lang(hi) button,html:lang(te) .tabs [role=tab],html:lang(hi) .tabs [role=tab]{overflow-wrap:anywhere}
"""


# Telugu glyphs sit small in their em box, Devanagari a little less so. The sheets give many labels 11 to 13 px; for those two languages
# this adds a copy of each small-size rule under html:lang(..) with a larger size, so the page's own sheets stay as they are.
_SCALE = {"te": (14.0, 1.5), "hi": (13.0, 1.0)}   # language: (largest size that gets bigger, extra px)


def lang_type_scale(css: str) -> str:
    out: list[str] = []
    stack: list[tuple[str, list[str]]] = []
    pos, n = 0, len(css)
    while pos < n:
        o, c = css.find("{", pos), css.find("}", pos)
        if o == -1 and c == -1:
            break
        if o != -1 and (c == -1 or o < c):
            head = css[pos:o].strip()
            nxt_close, nxt_open = css.find("}", o + 1), css.find("{", o + 1)
            if head.startswith("@") or (nxt_open != -1 and nxt_open < nxt_close):
                stack.append((head, []))
                pos = o + 1
                continue
            m = re.search(r"(?:^|;)\s*font-size:\s*([\d.]+)px", css[o + 1:nxt_close])
            if m and not any(h.startswith(("@keyframes", "@font-face")) for h, _r in stack):
                size = float(m.group(1))
                for code, (limit, extra) in _SCALE.items():
                    if 9 <= size <= limit:
                        sels = ",".join(f"html:lang({code}) {x.strip()}" for x in head.split(",") if x.strip() and "@" not in x)
                        if sels:
                            (stack[-1][1] if stack else out).append(f"{sels}{{font-size:{size + extra:g}px}}")
            pos = nxt_close + 1
        else:
            if stack:
                head, rules = stack.pop()
                if rules and not head.startswith(("@keyframes", "@font-face")):
                    (stack[-1][1] if stack else out).append(f"{head}{{{''.join(rules)}}}")
            pos = c + 1
    return "\n".join(out) + "\n"


def asset(name: str) -> tuple[str, str] | None:
    """Served under /static/: ``i18n.js`` (the engine) and ``i18n/<code>.json`` (one catalog)."""
    if name == "i18n.js":
        langs = [{"id": c, "en": e, "native": n} for c, e, n in LANGS]
        js = I18N_ENGINE_JS.replace("__LANGS__", json.dumps(langs, ensure_ascii=False)).replace("__EN__", json.dumps(catalog("en"), ensure_ascii=False))
        return js.replace("</", "<\\/"), "text/javascript; charset=utf-8"
    m = re.fullmatch(r"i18n/([a-z]{2})\.json", name)
    if m and m.group(1) in CODES:
        return json.dumps(catalog(m.group(1)), ensure_ascii=False), "application/json; charset=utf-8"
    return None


# ─── routes (called from dash_api.py) ──────────────────────────────────────

def _reply_now(srv: Any) -> str:
    try:
        return str(getattr(srv.settings_obj(), "reply_language", "") or "")
    except Exception:
        return ""


def get(srv: Any, route: str, qs: dict[str, str]) -> dict[str, Any] | None:
    if route == "/api/i18n/state":
        return {"reply": _reply_now(srv), "langs": [{"id": c, "name": e, "native": n} for c, e, n in LANGS]}
    return None


def post(srv: Any, route: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if route != "/api/i18n/reply":
        return None
    from axon.features.dash_common import DashError
    code = str(body.get("lang") or "").strip().lower()
    if code and code not in CODES:
        raise DashError("unknown language", 400)
    from axon.commands.common import update_settings, update_settings_file
    if update_settings_file({"replyLanguage": code or None}) is None:
        raise DashError("could not write settings.json", 500)
    srv._settings_cache.clear()
    for rt in srv.all_runtimes():
        update_settings(rt.agent, reply_language=code)
    return {"ok": True, "reply": code, "line": reply_line(code)}


# ─── coverage ──────────────────────────────────────────────────────────────

_LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
_SKIP_VALUE = re.compile(r"[_{}<>=$\\]|^https?:|/api/|\.js\b|#/")


def source_strings_by_file(root: Path | None = None) -> dict[str, list[str]]:
    """The user-visible English strings in each page script, found by a simple scan: quoted literals that start like a word or
    sentence. A scan cannot be exact (it also catches a few non-visible words and misses strings built from parts), so the
    coverage percentage is an estimate, stated as such."""
    root = root or Path(__file__).parent
    out: dict[str, list[str]] = {}
    for f in sorted(root.glob("dash_js_*.py")) + [root / "dashboard_web.py"]:
        if f.name in ("dash_js_i18n.py", "dash_js_a11y.py"):
            continue
        seen: dict[str, None] = {}
        text = re.sub(r'^\s*"""[\s\S]*?"""', "", f.read_text(encoding="utf-8"), count=1)
        for ln in text.splitlines():
            ln = re.sub(r"\s//\s.*$", "", ln)
            for m in _LITERAL.finditer(ln):
                s = m.group(1).replace("\\'", "'")
                if len(s) < 3 or not re.search(r"[A-Za-z]{3}", s) or not re.match(r"[A-Z][a-z]", s) or _SKIP_VALUE.search(s):
                    continue
                seen.setdefault(s.strip(), None)
        out[f.name] = list(seen)
    return out


def source_strings(root: Path | None = None) -> list[str]:
    seen: dict[str, None] = {}
    for strings in source_strings_by_file(root).values():
        for x in strings:
            seen.setdefault(x, None)
    return list(seen)


# The modules that draw the app frame: left rail, top bar, toolbar, composer, menus, Progress panel, turn header, dialogs, Settings.
FRAME_FILES = ("dash_js_frame.py", "dash_js_workbench.py", "dash_js_chat.py", "dash_js_settings.py", "dash_js_core.py", "dash_js_tools.py")


def _matches(strings: dict[str, str]) -> Any:
    exact = {v.strip() for v in strings.values()}
    pats = []
    for v in strings.values():
        if placeholders(v):
            pats.append(re.compile("^" + r"[\s\S]+?".join(re.escape(p) for p in re.split(r"\{\w+\}", v.strip())) + "$"))
    return lambda s: s in exact or any(p.match(s) for p in pats)


def coverage(root: Path | None = None) -> dict[str, Any]:
    """Counts: keys per language, and how many of the page-script strings the English catalog (and each language) covers."""
    en = catalog("en")
    strings = source_strings(root)
    in_en = _matches(en)
    covered_keys = {k for k, v in en.items() if v}
    by_file = source_strings_by_file(root)
    out: dict[str, Any] = {"strings_in_scripts": len(strings), "keys": len(en),
                           "strings_covered_by_catalog": sum(1 for s in strings if in_en(s)), "languages": {}}
    for code in CODES:
        if code == "en":
            continue
        cat = catalog(code)
        translated = {k for k in covered_keys if cat.get(k)}
        kept = {k for k in translated if cat[k] == en[k]}
        in_lang = _matches({k: en[k] for k in translated})
        covered = sum(1 for s in strings if in_lang(s))
        frame = [x for f in FRAME_FILES for x in by_file.get(f, [])]
        fcov = sum(1 for x in frame if in_lang(x))
        out["languages"][code] = {"keys_translated": len(translated), "kept_in_english": len(kept), "keys_missing": len(covered_keys - translated),
                                  "strings_covered": covered, "percent": round(100.0 * covered / max(1, len(strings)), 1),
                                  "frame_strings": len(frame), "frame_covered": fcov, "frame_percent": round(100.0 * fcov / max(1, len(frame)), 1),
                                  "uncovered_frame": [x for x in frame if not in_lang(x)]}
    return out


def main() -> None:
    c = coverage()
    print(f"English strings found in the page scripts (estimate): {c['strings_in_scripts']}")
    print(f"Catalog keys: {c['keys']}; scripts strings the catalog covers: {c['strings_covered_by_catalog']}")
    for code, d in c["languages"].items():
        name = next(e for k, e, _n in LANGS if k == code)
        print(f"{name}: {d['keys_translated']}/{c['keys']} keys translated ({d['kept_in_english']} deliberately kept in English), "
              f"{d['strings_covered']}/{c['strings_in_scripts']} script strings covered = {d['percent']}%")
        print(f"  app frame modules only: {d['frame_covered']}/{d['frame_strings']} = {d['frame_percent']}%")
        if "--missing" in sys.argv:
            print("  not yet translated (frame):")
            for x in d["uncovered_frame"]:
                print("   ", x)


if __name__ == "__main__":
    main()
