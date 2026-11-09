"""Appearance: the preference store and its CSS, shared by the Settings page.

``APPEARANCE_JS`` is served on its own as /static/appearance.js and loaded in <head>, so the saved theme,
fonts and sizes are on the root element before the first paint. It exposes ``window.AxonAppearance``;
the Settings tab (dash_js_settings.py) only draws controls for it.

Root custom properties it sets: --font-ui, --font-response, --font-code, --size-response, --size-code,
--measure-chat. Fonts are never bundled or fetched: only fonts already installed are used.
"""
from __future__ import annotations

APPEARANCE_JS = r"""
(function () {
"use strict";
var KEY = 'axon-appearance', LEGACY = 'axon-theme';
var UI_STACK = '"Avenir Next",Avenir,"Segoe UI Variable Text","Segoe UI",Cantarell,"Noto Sans",ui-sans-serif,-apple-system,system-ui,sans-serif';
var SYS_STACK = 'ui-sans-serif,-apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif';
var SERIFS = ['Iowan Old Style', 'Charter', 'Source Serif 4', 'Georgia'];
var CODE_TAIL = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace';
var UI_FONTS = { system: { label: 'Axon default', family: '' }, plain: { label: 'System font', family: '', stack: SYS_STACK }, inter: { label: 'Inter', family: 'Inter' }, sfpro: { label: 'SF Pro', family: 'SF Pro Text' } };
var SIZES = [[14, 'Small'], [16, 'Default'], [18, 'Large'], [20, 'Extra large']];
var WIDTHS = [[620, 'Narrow'], [720, 'Default'], [960, 'Wide'], ['full', 'Full']];
var CODE_SIZES = [12, 13, 14, 15];
var TOKENS = ['c', 's', 'n', 'k', 'f'];
// Syntax palettes. Axon Light / Axon Dark mirror the app's own colours; the others are optional pairs.
var CODE_THEMES = {
  'axon-light': { mode: 'light', label: 'Axon Light', bg: '#F4F4F4', c: '#595959', s: '#1B7340', n: '#8A5600', k: '#00705E', f: '#2B58BC', add: '#DCF3E4', del: '#FBE4E4' },
  'paper-light': { mode: 'light', label: 'Paper', bg: '#FDF6E3', c: '#5F6F70', s: '#1F7F78', n: '#B03E0C', k: '#667A00', f: '#1B6FB0', add: '#E6EBC8', del: '#F8DAD0' },
  'axon-dark': { mode: 'dark', label: 'Axon Dark', bg: '#101010', c: '#A8A8A8', s: '#74E0A0', n: '#F4C25E', k: '#4FE3C4', f: '#93BBFF', add: '#14332C', del: '#3F1F24' },
  'ink-dark': { mode: 'dark', label: 'Deep Ink', bg: '#002B36', c: '#8FA3A8', s: '#3CC0B6', n: '#E8793C', k: '#B4C72B', f: '#58AEEA', add: '#0F3D2E', del: '#4A2428' }
};
var DEFAULTS = { theme: 'system', contrast: false, ui: 'system', response: 'match', rsize: 16, width: 720, motion: 'system', codeLight: 'axon-light', codeDark: 'axon-dark', cfont: '', csize: 13 };

function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* blocked storage: preferences last for this tab only */ } }
function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }

// Letters, digits, spaces and hyphens only: nothing that could close a string or add a rule in CSS.
function sanitizeFont(s) { return String(s == null ? '' : s).replace(/[^A-Za-z0-9 \-]/g, '').replace(/\s+/g, ' ').trim().slice(0, 40); }
function oneOf(v, list, d) { return list.indexOf(v) >= 0 ? v : d; }
function normalize(o) {
  o = (o && typeof o === 'object') ? o : {};
  var codeIds = Object.keys(CODE_THEMES);
  var lightIds = codeIds.filter(function (k) { return CODE_THEMES[k].mode === 'light'; }), darkIds = codeIds.filter(function (k) { return CODE_THEMES[k].mode === 'dark'; });
  return {
    theme: oneOf(o.theme, ['system', 'light', 'dark'], DEFAULTS.theme), contrast: o.contrast === true,
    ui: oneOf(o.ui, Object.keys(UI_FONTS), DEFAULTS.ui), response: oneOf(o.response, ['serif', 'match'], DEFAULTS.response),
    rsize: oneOf(Number(o.rsize), SIZES.map(function (x) { return x[0]; }), DEFAULTS.rsize),
    width: o.width === 'full' ? 'full' : oneOf(Number(o.width), [620, 720, 960], DEFAULTS.width),
    motion: oneOf(o.motion, ['system', 'reduced', 'on'], DEFAULTS.motion),
    codeLight: oneOf(o.codeLight, lightIds, DEFAULTS.codeLight), codeDark: oneOf(o.codeDark, darkIds, DEFAULTS.codeDark),
    cfont: sanitizeFont(o.cfont), csize: oneOf(Number(o.csize), CODE_SIZES, DEFAULTS.csize)
  };
}
function load() {
  var raw = lsGet(KEY), o = null;
  if (raw) { try { o = JSON.parse(raw); } catch (e) { o = null; } }
  if (!o) { var legacy = lsGet(LEGACY); o = (legacy === 'dark' || legacy === 'light') ? { theme: legacy } : {}; }
  // Saved before version 2, 'serif' and the old widths were only the defaults of the time, not choices: they follow the new default.
  else if (o.v !== 2) { delete o.response; if (o.width === 780 || o.width === 680) delete o.width; }
  return normalize(o);
}

var root = document.documentElement, prefs = load(), subs = [], expectTheme = null, cache = {};

// A font counts as installed when naming it changes the width of a test string against each generic family.
function installed(name) {
  if (!name) return true;
  if (name in cache) return cache[name];
  var ok = false;
  try {
    var ctx = document.createElement('canvas').getContext('2d'), probe = 'mmmmmmmmmmlliWw@#0Oo', bases = ['monospace', 'serif', 'sans-serif'];
    if (ctx) {
      ok = bases.some(function (b) {
        ctx.font = '72px ' + b; var w0 = ctx.measureText(probe).width;
        ctx.font = '72px "' + name + '",' + b; return ctx.measureText(probe).width !== w0;
      });
    }
  } catch (e) { ok = false; }
  cache[name] = ok; return ok;
}
function serifInUse() { for (var i = 0; i < SERIFS.length; i++) if (installed(SERIFS[i])) return SERIFS[i]; return 'the browser serif'; }
function uiStack(p) { var u = UI_FONTS[p.ui], f = u.family; if (u.stack) return u.stack; return f && installed(f) ? '"' + f + '",' + UI_STACK : UI_STACK; }
function serifStack() { return SERIFS.map(function (n) { return n.indexOf(' ') >= 0 ? '"' + n + '"' : n; }).join(',') + ',serif'; }
function codeStack(p) { return (p.cfont ? '"' + p.cfont + '",' : '') + CODE_TAIL; }
function scheme(p) { return (p || prefs).theme !== 'system' ? (p || prefs).theme : (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
function paletteVars(id) {
  var t = CODE_THEMES[id], v = { '--code-bg': t.bg, '--add-bg': t.add, '--del-bg': t.del };
  TOKENS.forEach(function (k) { v['--tk-' + k] = t[k]; }); return v;
}

function apply() {
  var p = prefs, st = root.style, a = function (n, on) { if (on) root.setAttribute(n, on === true ? '' : on); else root.removeAttribute(n); };
  expectTheme = p.theme === 'system' ? null : p.theme;
  if (expectTheme) root.setAttribute('data-theme', expectTheme); else root.removeAttribute('data-theme');
  a('data-contrast', p.contrast ? 'high' : '');
  a('data-motion', p.motion === 'system' ? '' : p.motion);
  st.setProperty('--font-ui', uiStack(p));
  st.setProperty('--font-response', p.response === 'match' ? 'var(--font-ui)' : serifStack());
  st.setProperty('--font-code', codeStack(p));
  st.setProperty('--size-response', p.rsize + 'px');
  st.setProperty('--size-code', p.csize + 'px');
  st.setProperty('--measure-chat', p.width === 'full' ? '100%' : p.width + 'px');
  a('data-ap-ui', p.ui !== 'system' && (!!UI_FONTS[p.ui].stack || installed(UI_FONTS[p.ui].family)));
  a('data-ap-resp', p.response);
  a('data-ap-size', p.rsize !== 16);
  a('data-ap-width', p.width !== 720);
  a('data-ap-code', p.cfont !== '' || p.csize !== 13);
  var id = scheme(p) === 'dark' ? p.codeDark : p.codeLight, plain = id === 'axon-light' || id === 'axon-dark';
  ['--code-bg', '--add-bg', '--del-bg'].concat(TOKENS.map(function (k) { return '--tk-' + k; })).forEach(function (n) { st.removeProperty(n); });
  if (!plain) { var v = paletteVars(id); Object.keys(v).forEach(function (n) { st.setProperty(n, v[n]); }); }
  a('data-code-theme', plain ? '' : id);
  // keep the older Light/Dark toggle's own memory in step with this one
  if (p.theme === 'system') lsDel(LEGACY); else lsSet(LEGACY, p.theme);
}
function save() { lsSet(KEY, JSON.stringify(Object.assign({ v: 2 }, prefs))); }
function emit() { subs.slice().forEach(function (f) { try { f(prefs); } catch (e) { /* a broken listener must not stop the rest */ } }); }
function set(patch) { prefs = normalize(Object.assign({}, prefs, patch)); apply(); save(); emit(); return prefs; }
function reset() { prefs = normalize({}); apply(); lsDel(KEY); lsDel(LEGACY); emit(); return prefs; }

// The top-bar Light/Dark button changes data-theme directly; follow it so the two never disagree.
try {
  new MutationObserver(function () {
    var t = root.getAttribute('data-theme');
    if (t === expectTheme || (t !== 'dark' && t !== 'light')) return;
    prefs = normalize(Object.assign({}, prefs, { theme: t })); expectTheme = t; save(); lsSet(LEGACY, t); apply(); emit();
  }).observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  if (window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { if (prefs.theme === 'system') { apply(); emit(); } });
} catch (e) { /* older browsers: the toggle and this panel may drift until reload */ }

apply();
window.AxonAppearance = {
  DEFAULTS: DEFAULTS, UI_FONTS: UI_FONTS, SIZES: SIZES, WIDTHS: WIDTHS, CODE_SIZES: CODE_SIZES, CODE_THEMES: CODE_THEMES, SERIFS: SERIFS,
  get: function () { return Object.assign({}, prefs); }, set: set, reset: reset, apply: apply, installed: installed, serifInUse: serifInUse,
  sanitizeFont: sanitizeFont, normalize: normalize, serifStack: serifStack, codeStack: function (p) { return codeStack(p || prefs); },
  paletteVars: paletteVars, scheme: function () { return scheme(); }, subscribe: function (f) { subs.push(f); return function () { subs = subs.filter(function (x) { return x !== f; }); }; }
};
})();
"""

APPEARANCE_CSS = r"""
/* ── Appearance: fallbacks for surfaces not yet reading the --font-*, --size-* and --measure-chat variables.
   Every rule is gated on a data attribute that is only set for a non-default choice, so defaults change nothing. ── */
html[data-ap-ui] body,html[data-ap-ui] button,html[data-ap-ui] input,html[data-ap-ui] select,html[data-ap-ui] textarea{font-family:var(--font-ui)}
html[data-ap-resp] .entry:not(.u) .md{font-family:var(--font-response)}
html[data-ap-size] .entry:not(.u) .md{font-size:var(--size-response)}
html[data-ap-width] .feed>*,html[data-ap-width] .composer{max-width:var(--measure-chat)}
html[data-ap-code] .tscreen,html[data-ap-code] .code,html[data-ap-code] .editor,html[data-ap-code] .sh-out,html[data-ap-code] pre,html[data-ap-code] pre code,html[data-ap-code] .md code{font-family:var(--font-code)}
html[data-ap-code] .tscreen,html[data-ap-code] .code,html[data-ap-code] .editor,html[data-ap-code] .sh-out,html[data-ap-code] pre{font-size:var(--size-code)}
html[data-ap-code] .md pre code{font-size:inherit}
/* syntax colours of a chosen code theme (the panels in Settings carry the attribute themselves) */
[data-code-theme] .tk-c{color:var(--tk-c)}[data-code-theme] .tk-s{color:var(--tk-s)}[data-code-theme] .tk-n{color:var(--tk-n)}
[data-code-theme] .tk-k{color:var(--tk-k)}[data-code-theme] .tk-f{color:var(--tk-f)}
/* a chosen code theme also paints the viewer and editor ground, so its colours sit on the background they were made for */
[data-code-theme] .code,[data-code-theme] .editor{background:var(--code-bg)}
html[data-motion=reduced] *,html[data-motion=reduced] *::before,html[data-motion=reduced] *::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}
/* high contrast: stronger text and borders in whichever scheme is showing */
html[data-contrast=high]:not([data-theme=dark]){--page:#fff;--surface:#fff;--canvas:#fff;--float:#fff;--field:#fff;--you:#EDEFF2;--link:#004D3F;--raised:#EDEFF2;--raised2:#DDE1E6;--ink:#000;--ink2:#14171C;--muted:#14171C;--line:rgba(0,0,0,.55);--line2:#000;--primary:#004D3F;--code-bg:#fff;--info:#0A3D91;--accent:#333333;--ok:#1B5C13;--warn:#6B4000;--bad:#9C1229}
@media (prefers-color-scheme:dark){html[data-contrast=high]:not([data-theme=light]){--page:#000;--surface:#000;--canvas:#000;--float:#15181C;--field:#000;--you:#22262C;--link:#5CF0D2;--raised:#15181C;--raised2:#22262C;--ink:#fff;--ink2:#F2F4F7;--muted:#F2F4F7;--line:rgba(255,255,255,.5);--line2:rgba(255,255,255,.85);--primary:#5CF0D2;--code-bg:#000;--info:#9CD0FF;--accent:#E0E0E0;--ok:#A6F08E;--warn:#FFD27A;--bad:#FF9AA4}}
html[data-contrast=high][data-theme=dark]{--page:#000;--surface:#000;--canvas:#000;--float:#15181C;--field:#000;--you:#22262C;--link:#5CF0D2;--raised:#15181C;--raised2:#22262C;--ink:#fff;--ink2:#F2F4F7;--muted:#F2F4F7;--line:rgba(255,255,255,.5);--line2:rgba(255,255,255,.85);--primary:#5CF0D2;--code-bg:#000;--info:#9CD0FF;--accent:#E0E0E0;--ok:#A6F08E;--warn:#FFD27A;--bad:#FF9AA4}
html[data-contrast=high][data-theme=light]{--page:#fff;--surface:#fff;--canvas:#fff;--float:#fff;--field:#fff;--you:#EDEFF2;--link:#004D3F;--raised:#EDEFF2;--raised2:#DDE1E6;--ink:#000;--ink2:#14171C;--muted:#14171C;--line:rgba(0,0,0,.55);--line2:#000;--primary:#004D3F;--code-bg:#fff;--info:#0A3D91;--accent:#333333;--ok:#1B5C13;--warn:#6B4000;--bad:#9C1229}
/* the Appearance tab */
.ap-group{margin:0 0 22px}
.ap-group>h2{margin:0 0 6px}
.ap-card{border:1px solid var(--line);border-radius:12px;padding:0 16px}
.ap-row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 24px;padding:14px 0;border-bottom:1px solid var(--line)}
.ap-row:last-child{border-bottom:0}
.ap-lab{min-width:0;flex:1 1 220px}
.ap-lab b{display:block;font-weight:600}
.ap-lab span{display:block;color:var(--ink2);font-size:13px;margin-top:2px}
.ap-ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:flex-end}
.ap-ctl .seg button{display:inline-flex;align-items:center;gap:6px}
.ap-ctl .seg .ico{width:15px;height:15px}
.ap-ctl select,.ap-ctl input[type=text]{background:var(--raised);border:1px solid var(--line);border-radius:7px;padding:5px 9px;min-width:150px}
.ap-ctl input[type=text]{width:220px}
.ap-ctl input[aria-invalid=true]{border-color:var(--bad)}
.ap-preview{flex-basis:100%;border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--raised);line-height:1.6}
.ap-previews{flex-basis:100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
.ap-code{margin:0;border-radius:10px;padding:8px 0;background:var(--code-bg);color:var(--ink);font-family:var(--font-code);font-size:var(--size-code);line-height:1.5;overflow:auto;border:1px solid var(--line)}
.ap-code .ln{display:block;padding:0 12px;white-space:pre}
.ap-code .ln.add{background:var(--add-bg)}.ap-code .ln.del{background:var(--del-bg)}
.ap-code .cap{display:block;padding:0 12px 4px;color:inherit;opacity:.8;font:12px var(--font-ui)}
.ap-foot{display:flex;gap:10px;align-items:center;margin-top:6px}
"""
