"""Static page assets of the Axon dashboard: HTML shell, CSS (colours generated from the brand
palette at serve time) and the client script. No external loads; the CSP allows only same-origin."""
from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Axon</title>
<link rel="icon" type="image/svg+xml" href="/static/icon.svg">
<link rel="stylesheet" href="/static/app.css">
<script src="/static/appearance.js"></script>
</head><body>
<a class="skip-link" href="#main">Skip to the main content</a>
<div id="app"><p class="boot">Loading the dashboard...</p></div>
<script src="/static/app.js"></script>
</body></html>
"""

APP_CSS = r"""
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif}
.mono,code,pre,.num,.kpi .v,.turn,.who,.tn,.ts{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
a{color:var(--info)}
button,input,select,textarea{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:3px}
.boot{padding:48px 16px;color:var(--ink2)}
/* top bar */
.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--page) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:0 16px;display:flex;align-items:center;gap:20px;height:52px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--ink);font-weight:650;letter-spacing:.01em}
.brand svg{height:22px;width:auto;display:block}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;flex:1}
nav a{padding:15px 12px 13px;color:var(--ink2);text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
nav a:hover{color:var(--ink)}
nav a[aria-current=page]{color:var(--ink);border-bottom-color:var(--primary)}
.tools{display:flex;gap:6px}
.btn{background:transparent;border:1px solid var(--line);border-radius:5px;padding:5px 11px;cursor:pointer;color:var(--ink)}
.btn:hover{border-color:var(--primary)}
.btn.primary{background:var(--primary);border-color:var(--primary);color:var(--page);font-weight:600}
.btn.danger{border-color:var(--bad);color:var(--bad)}
.btn[disabled]{opacity:.5;cursor:default}
main{max-width:1180px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:22px;line-height:1.25;margin:0;font-weight:650}
h2{font-size:13px;font-weight:600;margin:0 0 10px;color:var(--ink2);letter-spacing:.02em}
.head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px;margin-bottom:22px}
.head .sub{color:var(--ink2);margin-top:4px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:5px;overflow:hidden}
.seg button{background:none;border:0;padding:5px 11px;cursor:pointer;color:var(--ink2);border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button[aria-pressed=true]{background:var(--raised);color:var(--ink)}
.controls{display:flex;flex-wrap:wrap;gap:8px}
/* ledger strip */
.strip{display:grid;grid-template-columns:repeat(6,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:28px}
.kpi{padding:14px 16px;border-left:1px solid var(--line);min-width:0}
.kpi:first-child{border-left:0;padding-left:0}
.kpi .l{color:var(--ink2);font-size:12px}
.kpi .v{font-size:23px;font-weight:600;margin:2px 0;font-variant-numeric:tabular-nums;white-space:nowrap}
.kpi .v.sig{color:var(--primary)}.kpi .v.syn{color:var(--accent)}
.kpi .h{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel{margin-bottom:28px;min-width:0}
.cols{display:grid;grid-template-columns:2fr 1fr;gap:32px;margin-bottom:28px}
.cols.eq{grid-template-columns:1fr 1fr}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.panel.chart svg{max-width:880px}
.chart svg text{fill:var(--muted);font-size:11px;font-family:ui-monospace,Menlo,monospace}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;list-style:none;margin:0 0 8px;padding:0;color:var(--ink2);font-size:12px}
.legend li{display:flex;align-items:center;gap:6px}.sw{width:10px;height:10px;border-radius:2px;display:inline-block}
.dl{list-style:none;margin:10px 0 0;padding:0}.dl li{display:flex;gap:8px;padding:3px 0;align-items:center}.dl .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--ink2);padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px 7px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 45%,transparent);vertical-align:top}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;font-family:ui-monospace,Menlo,monospace}
td.t{max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.muted{color:var(--muted)}.good{color:var(--ok)}.bad{color:var(--bad)}
.empty{padding:56px 0;color:var(--ink2);max-width:52ch}
.empty strong{display:block;color:var(--ink);font-size:16px;margin-bottom:6px}
.err-box{border:1px solid var(--bad);color:var(--bad);padding:10px 12px;border-radius:5px;margin:12px 0}
/* sessions */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center}
.search{flex:1 1 260px;min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:8px 12px}
.search:focus{border-color:var(--primary);outline:0}
select{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:7px 8px}
.rows{border-top:1px solid var(--line)}
.row{display:grid;grid-template-columns:22px minmax(0,1fr) 150px 64px 78px 70px;gap:12px;align-items:baseline;padding:11px 0;border-bottom:1px solid var(--line);text-decoration:none;color:inherit}
.row:hover{background:var(--surface)}
.row .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .meta{color:var(--muted);font-size:12px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .num{text-align:right;color:var(--ink2);font-size:12.5px}
.star{color:var(--accent)}.star.off{color:var(--line)}
.tag{display:inline-block;border:1px solid var(--line);border-radius:3px;padding:0 6px;margin-right:4px;font-size:11.5px;color:var(--ink2)}
.live{color:var(--primary);font-size:11.5px;border:1px solid var(--primary);border-radius:3px;padding:0 6px;margin-left:6px}
/* transcript */
.sess-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;margin-bottom:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink2);font-size:12.5px;margin:6px 0 22px}
.chips b{color:var(--ink);font-weight:600;font-family:ui-monospace,Menlo,monospace}
.entry{display:grid;grid-template-columns:56px minmax(0,1fr);gap:0 14px;padding:16px 0;border-top:1px solid color-mix(in srgb,var(--line) 55%,transparent)}
.entry>.who{grid-column:1;font-size:12px;color:var(--primary);padding-top:3px}
.entry.a>.who{color:var(--accent)}
.entry>*:not(.who){grid-column:2;min-width:0}
.entry.n{display:block;padding:8px 0}.note{color:var(--muted);font-size:12.5px;font-style:italic}
.md{font-family:Charter,"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:15.5px;line-height:1.65;max-width:78ch;overflow-wrap:anywhere}
.entry.u .md{font-family:ui-sans-serif,system-ui,sans-serif;font-size:14.5px;color:var(--ink)}
.md p{margin:.45em 0}.md h2,.md h3,.md h4,.md h5,.md h6,.md h7{font-family:ui-sans-serif,system-ui,sans-serif;color:var(--ink);letter-spacing:0;font-size:15px;margin:1em 0 .3em}
.md ul,.md ol{padding-left:1.4em;margin:.4em 0}.md blockquote{margin:.6em 0;padding-left:12px;border-left:2px solid var(--line);color:var(--ink2)}
.md code{background:var(--code-bg);color:var(--code-fg);padding:1px 5px;border-radius:3px;font-size:.88em}
pre{margin:.5em 0;background:var(--code-bg);border-radius:5px;padding:10px 12px;overflow:auto;font-size:12.5px;line-height:1.5;max-height:520px;white-space:pre-wrap;word-break:break-word;color:var(--ink)}
pre code{background:none;padding:0;color:inherit}
.diff span{display:block;min-height:1.5em;white-space:pre-wrap}
.md table{width:auto;min-width:40%}.md td,.md th{padding-right:28px}
.diff .add{color:var(--add);background:var(--add-bg);display:block}.diff .del{color:var(--del);background:var(--del-bg);display:block}
.diff .hunk{color:var(--info);display:block}.diff .hdr{color:var(--ink2);display:block}.diff .ctx{display:block}
details.tool,details.think{margin:8px 0;border:1px solid var(--line);border-radius:5px;background:var(--surface)}
details>summary{cursor:pointer;padding:6px 10px;color:var(--ink2);font-size:12.5px;list-style:none;display:flex;gap:10px;align-items:baseline}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"+";color:var(--muted);font-family:ui-monospace,monospace;width:10px}
details[open]>summary::before{content:"-"}
details>*:not(summary){margin-left:10px;margin-right:10px}details[open]{padding-bottom:8px}
.tn{color:var(--ink);font-weight:600;font-size:12.5px}.ta{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,monospace;font-size:12px;color:var(--ink2)}
.ts{font-size:11.5px;color:var(--ok)}
details.err .ts,details.denied .ts{color:var(--bad)}details.denied{border-color:var(--bad)}
.reason{color:var(--bad);font-size:12.5px;margin:4px 10px}
.lbl{font-size:11.5px;color:var(--muted);margin-top:6px}
.turn{font-size:12px;color:var(--muted);margin-top:8px}
.clipped{font-size:12.5px;color:var(--warn);display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0}
.more{background:none;border:1px solid var(--warn);color:var(--warn);border-radius:4px;padding:2px 9px;cursor:pointer}
.earlier{display:block;margin:0 auto 12px}
.entry,.approve{scroll-margin-bottom:190px}
/* composer */
.composer{position:sticky;bottom:0;background:var(--page);border-top:1px solid var(--line);padding:12px 0;margin-top:20px}
.composer textarea{width:100%;min-height:54px;max-height:240px;resize:vertical;background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:9px 12px}
.composer textarea:focus{border-color:var(--primary);outline:0}
.composer .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px;color:var(--ink2);font-size:12.5px}
.composer .bar .sp{flex:1}
.live-note{border:1px solid var(--primary);border-radius:5px;padding:8px 12px;margin-bottom:10px;color:var(--ink2)}
.stream .md{white-space:pre-wrap}
.approve{border:1px solid var(--warn);border-radius:5px;padding:10px 12px;margin:8px 0;background:var(--surface)}
.approve .acts{display:flex;gap:8px;margin-top:8px}
.badge-denied{color:var(--bad);font-size:12.5px;margin:6px 0}
/* extensions */
.tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:16px}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;color:var(--ink2);cursor:pointer}
.tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--primary)}
.kv{display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:4px 18px;font-size:13px}
.kv dt{color:var(--ink2)}.kv dd{margin:0;font-family:ui-monospace,Menlo,monospace;overflow-wrap:anywhere}
/* modal */
.scrim{position:fixed;inset:0;background:color-mix(in srgb,var(--page) 70%,transparent);backdrop-filter:blur(3px);display:flex;align-items:flex-start;justify-content:center;padding:6vh 12px;z-index:20;overflow:auto}
.modal{background:var(--surface);border:1px solid var(--line);border-radius:8px;width:min(860px,100%);padding:18px}
.modal h3{margin:0 0 4px;font-size:16px}
.modal pre{max-height:45vh;font-size:11.5px}
.modal .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;align-items:center}
.kbd{font-family:ui-monospace,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:3px;padding:0 5px;font-size:11.5px;color:var(--ink2)}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--raised);border:1px solid var(--line);padding:8px 14px;border-radius:5px;z-index:30}
@media (max-width:860px){
 .strip{grid-template-columns:repeat(2,1fr)}
 .kpi,.kpi:first-child{border-left:0;padding-left:0;border-top:1px solid var(--line)}
 .kpi:nth-child(-n+2){border-top:0}
 .cols,.cols.eq{grid-template-columns:1fr}
 .row{grid-template-columns:20px minmax(0,1fr) 70px;row-gap:2px}
 .row .hide-s{display:none}
}
@media (max-width:560px){
 .top-in{gap:8px}.brand span{display:none}
 .entry{grid-template-columns:1fr}.entry>.who,.entry>*:not(.who){grid-column:1}
 .entry>.who{padding-bottom:4px}
 h1{font-size:19px}
}
@media (prefers-reduced-motion:no-preference){.entry.fresh{animation:fresh .6s ease-out}}
@keyframes fresh{from{background:color-mix(in srgb,var(--primary) 14%,transparent)}to{background:transparent}}
"""

from axon.features.dash_appearance import APPEARANCE_CSS, APPEARANCE_JS  # noqa: E402
from axon.features.dash_css import APP_LAYOUT_CSS  # noqa: E402
from axon.features.dash_design import DESIGN_CSS, FINISH_CSS, TYPE_TAIL, type_overrides  # noqa: E402
from axon.features.dash_js_cards import JS_CARDS  # noqa: E402
from axon.features.dash_js_context import CONTEXT_CSS, JS_CONTEXT  # noqa: E402
from axon.features.dash_js_chatx import CHATX_CSS, JS_CHATX  # noqa: E402
from axon.features.dash_js_compare import COMPARE_CSS, JS_COMPARE  # noqa: E402
from axon.features.dash_js_cmdcards import CMDCARDS_CSS, JS_CMDCARDS  # noqa: E402
from axon.features.dash_js_chat import JS_CHAT  # noqa: E402
from axon.features.dash_js_core import JS_CORE  # noqa: E402
from axon.features.dash_js_files import JS_FILES  # noqa: E402
from axon.features.dash_js_frame import JS_FRAME  # noqa: E402
from axon.features.dash_js_insights import JS_INSIGHTS  # noqa: E402
from axon.features.dash_js_manage import JS_MANAGE  # noqa: E402
from axon.features.dash_js_md import JS_MD  # noqa: E402
from axon.features.dash_js_settings import JS_SETTINGS  # noqa: E402
from axon.features.dash_js_subagents import JS_SUBAGENTS  # noqa: E402
from axon.features.dash_js_term import JS_TERM  # noqa: E402
from axon.features.dash_js_workbench import JS_WORKBENCH, JS_WORKBENCH_PANELS, JS_WORKBENCH_RAIL  # noqa: E402
from axon.features.dash_workbench_css import WORKBENCH_CSS  # noqa: E402
from axon.features.dash_js_tools import JS_TOOLS  # noqa: E402
from axon.features.dash_js_welcome import JS_WELCOME, WELCOME_CSS  # noqa: E402
from axon.features.dash_js_i18n import I18N_CSS, JS_I18N, asset as i18n_asset, lang_type_scale  # noqa: E402
from axon.features.dash_js_a11y import A11Y_CSS, JS_A11Y  # noqa: E402
from axon.features.dash_js_tasks import JS_TASKS, TASKS_CSS  # noqa: E402

from axon.features.dash_js_library import JS_LIBRARY, LIBRARY_CSS  # noqa: E402

APP_JS = '(() => {\n"use strict";\n' + i18n_asset("i18n.js")[0] + JS_CORE + JS_MD + JS_FRAME + JS_CHAT + JS_CARDS + JS_FILES + JS_TERM + JS_WORKBENCH + JS_WORKBENCH_PANELS + JS_WORKBENCH_RAIL + JS_CONTEXT + JS_INSIGHTS + JS_SETTINGS + JS_TOOLS + JS_SUBAGENTS + JS_MANAGE + JS_CMDCARDS + JS_WELCOME + JS_I18N + JS_TASKS + JS_COMPARE + JS_CHATX + JS_LIBRARY + JS_A11Y + "\n})();\n"


def asset(name: str) -> tuple[str, str] | None:
    from axon.features import dashboard as d
    if name == "app.js":
        return APP_JS, "text/javascript; charset=utf-8"
    if name == "i18n.js" or name.startswith("i18n/"):
        return i18n_asset(name)
    if name == "appearance.js":
        return APPEARANCE_JS, "text/javascript; charset=utf-8"
    if name == "app.css":
        sheet = d.theme_css() + APP_CSS + APP_LAYOUT_CSS + APPEARANCE_CSS + DESIGN_CSS + WORKBENCH_CSS + CMDCARDS_CSS + COMPARE_CSS + WELCOME_CSS + I18N_CSS + TASKS_CSS + CHATX_CSS + LIBRARY_CSS + CONTEXT_CSS + A11Y_CSS
        return sheet + type_overrides(sheet) + TYPE_TAIL + FINISH_CSS + lang_type_scale(sheet + FINISH_CSS), "text/css; charset=utf-8"
    if name in ("icon.svg", "logo.svg"):
        svg = d.logo_svg()
        if name == "icon.svg":
            t, _ = d._theme_tokens()
            svg = svg.replace("var(--axon-primary, ", "var(--axon-primary, ")  # defaults already carry the brand colours
        return svg, "image/svg+xml"
    return None
