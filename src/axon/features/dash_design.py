"""The look of the Axon web app: restyles every screen without changing how any of them work.

Art direction "Conduction": a turn is a signal travelling along a fibre, one hairline with a node per tool call, and
everything else recedes. Neutrals are true grays, actions are the brand teal, and a light gray marks a signal in flight. ``DESIGN_CSS`` holds the tokens and base rules; ``FINISH_CSS`` is served after every other sheet and has the last word.
"""

_DARK = (
    "--canvas:#0E0E0E;--page:#161616;--surface:#161616;--raised:#212121;--raised2:#2C2C2C;--float:#242424;--field:#1C1C1C;--you:#2A2A2A;--grid:#262626;"
    "--line:rgba(255,255,255,.09);--line2:rgba(255,255,255,.18);--ink:#EDEDED;--ink2:#A8A8A8;--muted:#A8A8A8;--link:#6FE6CE;"
    "--code-bg:#101010;--primary:#4FE3C4;--on-primary:#04221B;--accent:#CFCFCF;--info:#93BBFF;--ok:#74E0A0;--on-bad:#2B060E;"
    "--warn:#F4C25E;--bad:#FF8498;--add:#74E0A0;--del:#FF8498;--add-bg:#14332C;--del-bg:#3F1F24;"
    "--shadow:0 14px 44px rgba(0,0,0,.6);--tint-shadow:rgba(0,0,0,.32);--scrim:rgba(0,0,0,.6);--soft:rgba(79,227,196,.15);"
    "--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#a3a3a3;--s6:#008300;--s7:#4cc0d4;--s8:#e66767;"
    "--grid-line:rgba(255,255,255,.09)"
)
_LIGHT = (
    "--canvas:#ECECEC;--page:#FAFAFA;--surface:#FAFAFA;--raised:#F0F0F0;--raised2:#E4E4E4;--float:#FFFFFF;--field:#FFFFFF;--you:#EAEAEA;--grid:#E3E3E3;"
    "--line:rgba(0,0,0,.09);--line2:rgba(0,0,0,.18);--ink:#1A1A1A;--ink2:#595959;--muted:#595959;--link:#006E5C;"
    "--code-bg:#F4F4F4;--primary:#00705E;--on-primary:#FFFFFF;--accent:#4A4A4A;--info:#2B58BC;--ok:#1B7340;--on-bad:#FFFFFF;"
    "--warn:#8A5600;--bad:#BC1F3F;--add:#1B7340;--del:#BC1F3F;--add-bg:#DCF3E4;--del-bg:#FBE4E4;"
    "--shadow:0 14px 44px rgba(0,0,0,.16);--tint-shadow:rgba(0,0,0,.07);--scrim:rgba(0,0,0,.32);--soft:rgba(0,112,94,.11);"
    "--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#7a7a7a;--s6:#008300;--s7:#0c8aa0;--s8:#e34948;"
    "--grid-line:rgba(0,0,0,.09)"
)

_UI_STACK = '"Avenir Next",Avenir,"Segoe UI Variable Text","Segoe UI",Cantarell,"Noto Sans",ui-sans-serif,-apple-system,system-ui,sans-serif'
_VARS = (
    f"--font-ui:{_UI_STACK};"
    "--font-response:var(--font-ui);"
    "--font-code:ui-monospace,SFMono-Regular,\"SF Mono\",Menlo,Consolas,monospace;"
    "--size-response:16px;--size-code:13px;--measure-chat:720px"
)


# ── type pass ──
# One interface face (--font-ui), one code face (--font-code, code only), sizes 12 13 14 15 17 22 (16 stays for form
# fields, which iOS would zoom otherwise), weights 400 500 600. The app's style sheets were written over time with
# half sizes and heavier weights, so the served sheet passes through ``snap_type`` instead of editing every rule.
import re as _re  # noqa: E402

_SIZE_STEPS = ((12.4, 12), (13.4, 13), (14.4, 14), (15.4, 15), (16.4, 16), (18.4, 17), (999, 22))


def _snap_size(px: float) -> int:
    for top, size in _SIZE_STEPS:
        if px <= top:
            return size
    return 22


def _snap_weight(w: int) -> int:
    return 400 if w <= 450 else 500 if w <= 550 else 600


TYPE_TAIL = r"""
/* ── type pass: labels and figures use the interface face; only code keeps the code face ── */
.num,.kpi .v,.turn,.who,.tn,.ts,td.r,th.r,.chips b,.kv dd,.ws-path,.ws-git,.kbd,.steprow,.step-line,.rawev summary,.pop .it b,.fhead .full,.node .g,.ftoolbar .p,.gbadge,.cmd-list .c b,.palette .it b,.rfid,.fp-hit .muted,.br-chip,.cc-table .r,.cc-on,.cc-said span,.pop .it .arg,.cn-tool,.chart svg text,.chip.rich .cicon,.ch-file .ftm,.flist,details>summary::before,.mg-th,.sh-cmd{font-family:var(--font-ui)}
.mono,code,pre,.code,.editor,.sh-out,.sh-in input,.cmd-out,.tscreen,.csv,.ch-line,.ghit,.br-url,.diff,pre.out{font-family:var(--font-code)}
pre,code,.code,.editor,.sh-out,.cmd-out,.csv,.diff,pre.out{font-size:var(--size-code)}
code{font-size:.92em}pre code{font-size:inherit}
b,strong,th,h1,h2,h3,h4,h5,h6{font-weight:600}
h1,.pane>h1,.head h1{font-size:22px}
body,input,select,textarea,button{font-family:var(--font-ui)}
body{font-size:14px;line-height:1.55;font-variant-numeric:tabular-nums}
.kv dt,.lbl,.cat,.sub,.note,.muted,.tag,.kbd{font-size:12px}
em,i,cite,address{font-style:normal}
.ap-lab span,.ap-ctl select,.ap-ctl input[type=text]{font-size:13px}.ap-code .cap{font-size:12px}
"""


def _snap_decls(decls: str) -> str:
    """The declarations of one rule that break the scale, rewritten on it (empty when the rule is already fine)."""
    out: list[str] = []
    for prop, val in _re.findall(r"(?:^|;)\s*(font(?:-size|-weight|-style|-family)?)\s*:\s*([^;]+)", decls):
        val = val.strip()
        if prop == "font-size":
            m = _re.fullmatch(r"(\d+(?:\.\d+)?)px", val)
            if m and _snap_size(float(m.group(1))) != float(m.group(1)):
                out.append(f"font-size:{_snap_size(float(m.group(1)))}px")
        elif prop == "font-weight":
            if val in ("bold", "bolder"):
                out.append("font-weight:600")
            elif val.isdigit() and _snap_weight(int(val)) != int(val):
                out.append(f"font-weight:{_snap_weight(int(val))}")
        elif prop == "font-style":
            if val == "italic":
                out.append("font-style:normal")
        elif prop == "font-family":
            if "monospace" in val and "var(" not in val:
                out.append("font-family:var(--font-code)")
        else:   # the font shorthand: [italic] [weight] size[/line-height] family
            m = _re.match(r"(italic\s+)?(?:(\d{3})\s+)?(\d+(?:\.\d+)?)px", val)
            if m:
                if m.group(1):
                    out.append("font-style:normal")
                if m.group(2) and _snap_weight(int(m.group(2))) != int(m.group(2)):
                    out.append(f"font-weight:{_snap_weight(int(m.group(2)))}")
                if _snap_size(float(m.group(3))) != float(m.group(3)):
                    out.append(f"font-size:{_snap_size(float(m.group(3)))}px")
            if "monospace" in val and "var(" not in val:
                out.append("font-family:var(--font-code)")
    return ";".join(out)


def type_overrides(css: str) -> str:
    """Rules, in the order they appear (inside their @media), that put every off-scale size, weight, italic and
    monospace label of ``css`` on the scale. The sheets themselves stay as they are; this goes after them."""
    out: list[str] = []
    stack: list[tuple[str, list[str]]] = []
    pos, n = 0, len(css)
    while pos < n:
        o, c = css.find("{", pos), css.find("}", pos)
        if o == -1 and c == -1:
            break
        if o != -1 and (c == -1 or o < c):
            head = css[pos:o].strip()
            # a rule block ends at the next "}" with no "{" between
            nxt_close = css.find("}", o + 1)
            nxt_open = css.find("{", o + 1)
            if head.startswith("@") or (nxt_open != -1 and nxt_open < nxt_close):
                stack.append((head, []))
                pos = o + 1
                continue
            body = css[o + 1:nxt_close]
            fix = _snap_decls(body)
            if fix and not any(s[0].startswith(("@keyframes", "@font-face")) for s in stack):
                rule = f"{head}{{{fix}}}"
                (stack[-1][1] if stack else out).append(rule)
            pos = nxt_close + 1
        else:
            if stack:
                head, rules = stack.pop()
                if rules and not head.startswith(("@keyframes", "@font-face")):
                    block = f"{head}{{{''.join(rules)}}}"
                    (stack[-1][1] if stack else out).append(block)
            pos = c + 1
    return "\n".join(out) + "\n"


DESIGN_CSS = (
    f":root{{{_VARS}}}\n"
    f":root{{{_DARK}}}\n"
    f"@media (prefers-color-scheme:light){{:root:not([data-theme=\"dark\"]){{{_LIGHT}}}}}\n"
    f":root[data-theme=\"dark\"]{{{_DARK}}}\n"
    f":root[data-theme=\"light\"]{{{_LIGHT}}}\n"
    r"""
/* ── foundations ── */
body{background:var(--canvas);font:14px/1.55 var(--font-ui);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a,button,summary,.node,.pill,.tab,input,select,textarea,.composer{transition:background-color .12s ease-out,color .12s ease-out,border-color .12s ease-out,box-shadow .12s ease-out,opacity .12s ease-out}
*{scrollbar-width:thin;scrollbar-color:var(--raised2) transparent}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--raised2);border-radius:10px;border:3px solid transparent;background-clip:content-box}
::-webkit-scrollbar-track{background:transparent}
:focus-visible{outline:2px solid var(--primary);outline-offset:1px;border-radius:7px}
::selection{background:var(--soft)}
h1,h2,h3{letter-spacing:-.01em}
.pane.on,.scrim,.pop,.tab-pop{animation:ax-in .14s ease-out}
@keyframes ax-in{from{opacity:0;transform:translateY(3px)}}
details[open]>*:not(summary){animation:ax-fade .14s ease-out}
@keyframes ax-fade{from{opacity:0}}
@media (prefers-reduced-motion:reduce){:root:not([data-motion=on]) *{animation-duration:.001s!important;transition-duration:.001s!important}}

/* ── controls ── */
.btn{background:var(--raised);border:1px solid transparent;border-radius:8px;padding:5px 12px;font-weight:500;white-space:nowrap}
.btn:hover{background:var(--raised2);border-color:transparent}
.btn.primary{background:var(--primary);border-color:transparent;color:var(--on-primary);font-weight:600}
.btn.primary:hover{background:var(--primary);filter:brightness(1.07)}
.btn.danger{background:transparent;border-color:color-mix(in srgb,var(--bad) 45%,transparent);color:var(--bad)}
.btn.danger:hover{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.mini,.turnbar button{background:none;border:0;border-radius:6px;padding:2px 8px;color:var(--ink2)}
.mini:hover,.turnbar button:hover{background:var(--raised);color:var(--ink);border-color:transparent}
.kbd{border:0;background:var(--raised2);border-radius:4px;padding:0 5px;font:11px var(--font-ui);color:var(--ink2)}
.pill{border:0;background:var(--raised);border-radius:8px;padding:3px 9px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill select{cursor:pointer;max-width:82px;outline:0;text-overflow:ellipsis}
.pill select option{background:var(--raised);color:var(--ink)}
.pill .gauge{width:44px;height:4px;background:var(--raised2)}
.seg{border:0;background:var(--raised);border-radius:8px;padding:2px}
.seg button{border:0;border-radius:6px;padding:3px 10px}
.seg button[aria-pressed=true]{background:var(--page);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.18)}
input[type=text],input[type=search],input[type=password],.formrow input[type=text],.formrow input[type=search],textarea,select{border-radius:8px}
.formrow input[type=text],.formrow input[type=search],.sh-in input,.cmd-search,input[type=password]{background:var(--raised);border:1px solid transparent;padding:8px 11px}
.formrow input:focus,.sh-in input:focus,.cmd-search:focus,input[type=password]:focus{outline:0;border-color:var(--primary);box-shadow:0 0 0 3px var(--soft)}
.plink,a.plink{color:var(--info);text-decoration:none}a.plink:hover{text-decoration:underline;text-underline-offset:2px}
.foldbtn{border:0;background:var(--raised);border-radius:6px;padding:2px 10px}
.bar,.gauge{background:var(--raised2);border-radius:4px}

/* ── frame: sheets on a canvas ── */
.ide{background:var(--canvas)}
.top{height:46px;background:transparent;border:0;padding:0 12px;gap:10px;backdrop-filter:none}
.top .brand{font-weight:600}
.ws-chip{border:0;background:none;padding:4px 8px;border-radius:8px;flex:0 1 auto}
.ws-chip:hover{background:var(--raised);border-color:transparent}
.ws-path{font:12px var(--font-ui);max-width:300px;direction:rtl;text-align:left}
.ws-git{font:12px var(--font-ui)}
.tools{gap:2px}
.top .btn{background:none;color:var(--ink2);padding:5px 10px;display:inline-flex;align-items:center;gap:6px}
.top .btn:hover{background:var(--raised);color:var(--ink)}
.top .btn[aria-pressed=true]{color:var(--ink)}
.ide-body{padding:0 8px 0 0;column-gap:0}
.side-nav{background:transparent;border:0;padding:2px 6px;gap:2px}
@media (min-width:901px){:root{--rail:82px}}   /* "Commands" and "Extensions" are wider than the old 64px rail */
.side-nav a{padding:7px 2px 6px;border-radius:10px;color:var(--ink2)}
.side-nav a:hover{background:var(--raised);color:var(--ink)}
.side-nav a[aria-current=page]{background:var(--raised);color:var(--ink);box-shadow:none}
.side-nav a[aria-current=page] .ico{color:var(--primary)}
.lpanel,.rpanel,.center{background:var(--page);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.rz{background:transparent;border-radius:4px}
.rz:hover,.resizing .rz{background:color-mix(in srgb,var(--primary) 60%,transparent)}
.statusbar{background:transparent;border:0;height:28px;padding:0 14px;font-size:12px;color:var(--ink2);gap:14px}
.statusbar b{font-weight:500;color:var(--ink2)}.statusbar .muted{opacity:1}
.tabstrip{background:transparent;border-bottom:1px solid var(--line);padding:6px 8px;gap:4px}
.tabscroll{gap:4px}
/* a tab cut by the strip's edge fades out instead of showing half a title with its menu and close buttons */
.tabscroll.fade-l{-webkit-mask-image:linear-gradient(to right,transparent 0,#000 26px);mask-image:linear-gradient(to right,transparent 0,#000 26px)}
.tabscroll.fade-r{-webkit-mask-image:linear-gradient(to left,transparent 0,#000 26px);mask-image:linear-gradient(to left,transparent 0,#000 26px)}
.tabscroll.fade-l.fade-r{-webkit-mask-image:linear-gradient(to right,transparent 0,#000 26px,#000 calc(100% - 26px),transparent 100%);mask-image:linear-gradient(to right,transparent 0,#000 26px,#000 calc(100% - 26px),transparent 100%)}
.tab{border-radius:8px}
.tab.on{background:var(--raised);box-shadow:none}.tab.split:not(.on){box-shadow:inset 0 0 0 1px var(--line2)}
.tab:not(.on):hover{background:color-mix(in srgb,var(--raised) 60%,transparent)}
.tab .tl{padding:4px 2px 4px 10px;font-size:13px}.tab .tx{padding:2px 8px}.tab .tx:hover{color:var(--ink)}
.tabstrip>.mini{margin:0 0 0 4px}

/* ── side panels ── */
.ptabs,.phead{border-bottom:1px solid var(--line);padding:8px 10px;min-height:44px}
.phead b{font-weight:600}
.ptabs button{border-radius:7px;padding:4px 10px;font-size:13px}
.ptabs button:hover{background:var(--raised);color:var(--ink)}
.fhead{border:0;padding:12px 14px 8px}
.fhead .cur{font-size:15px}
.fhead .full{font:12px var(--font-ui);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left;margin:1px 0 10px}
.fhead .btn{padding:3px 10px;font-size:12.5px}
.node{height:28px;padding-block:0;border-radius:7px;margin:0 6px;width:calc(100% - 12px);color:var(--ink2)}
.node:hover{background:var(--raised);color:var(--ink)}
.node.sel{background:var(--raised);color:var(--ink);box-shadow:none}
.node.dir .nm{color:inherit}
.node .ar{width:12px;font-size:12px;opacity:1}
.ftree{background:transparent;border-right:1px solid var(--line)}
.psec{padding:14px 16px;border:0;border-bottom:1px solid var(--line)}
.psec:last-child{border-bottom:0}
.psec h3{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink)}
.psec h3 a.plink{font-size:12px;font-weight:400}
.psec p{margin:0;font-size:13px}
.psec .row{display:flex;align-items:center;gap:10px;padding:3px 0;font-size:13px;color:inherit;text-decoration:none}
.psec .row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.psec a.row:hover .t{color:var(--ink)}
.psec .row b{font-weight:550}
.psec .bar{height:4px;margin:4px 0 6px}
.psec .lbl{font-size:12px;color:var(--ink2);margin-bottom:4px}
.step-line{font:12px var(--font-ui);grid-template-columns:22px minmax(0,1fr) 54px 36px 30px;color:var(--ink2)}
.step-line .bar{height:4px;margin:0}
.gbadge{font:11px var(--font-ui);border:0;background:var(--raised);border-radius:4px;padding:0 5px;color:var(--ok)}
.psec .quiet,p.quiet{border:0;padding:12px 16px;font-size:12.5px;color:var(--ink2);opacity:.85}

/* ── chat ── */
.chat-head{border-bottom:1px solid var(--line);padding:8px 16px;gap:6px 6px;min-height:48px}
.chat-head h1{font-size:14px;font-weight:600;margin-right:6px;max-width:15ch}
.chat-head>.btn{background:none;color:var(--ink2);padding:3px 9px;font-size:12.5px}
.chat-head>.btn:hover{background:var(--raised);color:var(--ink)}
.chat-head .btn.danger{color:var(--bad)}
.feed{padding:28px 24px 12px}
.feed>*{max-width:var(--measure-chat);margin-left:auto;margin-right:auto}
.entry{display:block;padding:0;border:0;margin-bottom:24px}
.entry>.who{display:none}
.entry.n{margin-bottom:12px}
.entry .md{font:15px/1.65 var(--font-ui);color:var(--ink);letter-spacing:-.003em}
.entry .md p{margin:0 0 .75em}.entry .md p:last-child{margin-bottom:0}
.entry .md h1,.entry .md h2,.entry .md h3{color:var(--ink);margin-top:1.1em}.entry .md>:first-child{margin-top:0}
.entry .md code{background:var(--raised);padding:1px 5px;border-radius:5px;font-size:.88em}
.entry .md pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px}
.entry .md pre code{background:none;padding:0}
.entry .md ul,.entry .md ol{margin:.2em 0 .75em;padding-left:1.4em}.entry .md li{margin:.15em 0}
.entry .md blockquote{margin:.5em 0;padding-left:12px;border-left:2px solid var(--line2);color:var(--ink2)}
.entry .md table{margin:.5em 0;font-size:14px}.entry .md th,.entry .md td{padding:5px 10px;border-bottom:1px solid var(--line)}
.entry .md hr{border:0;border-top:1px solid var(--line);margin:1em 0}
.entry.u .md pre{background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;white-space:pre-wrap;max-height:none}
.entry.u{display:flex;flex-direction:column;align-items:flex-end}
.entry.u .md{background:var(--raised);padding:9px 15px;border-radius:18px 18px 5px 18px;max-width:85%;overflow-wrap:anywhere}
.turnbar{margin:4px 2px 0;font-size:12px}
.turn,.steprow{font:12px var(--font-ui);color:var(--ink2);opacity:.8;margin:6px 0 0 2px}
/* tool steps: quiet rows that open in place */
details.tool,details.think{margin:2px 0;border:0;border-radius:10px;background:transparent}
details>summary{padding:6px 10px;gap:9px;border-radius:10px;align-items:center;font-size:13.5px;color:var(--ink2)}
details>summary::before{content:"";flex:none;width:6px;height:6px;margin:0 2px 0 1px;border-right:1.6px solid var(--ink2);border-bottom:1.6px solid var(--ink2);transform:rotate(-45deg);transition:transform .14s ease-out;opacity:.7}
details[open]>summary::before{transform:rotate(45deg) translate(-1px,-1px)}
details>summary:hover{background:var(--raised);color:var(--ink)}
details[open].tool,details[open].think{background:var(--raised);padding-bottom:10px;margin:6px 0}
details[open]>summary:hover{background:transparent}
details>*:not(summary){margin-left:12px;margin-right:12px}
.tn{font:600 13.5px var(--font-ui);color:var(--ink)}
.ta{font:13px var(--font-ui);color:var(--ink2);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:0 1 auto}
.ts{font:12px var(--font-ui);margin-left:auto;color:var(--ink2);white-space:nowrap}
.tool.err .ts,.tool.denied .ts{color:var(--bad)}
.tool.err .tn::before,.tool.denied .tn::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--bad);margin-right:7px;vertical-align:2px}
.tool.err,.tool.denied{border:0}
pre.out,.tool pre{background:var(--code-bg);border:0;border-radius:8px;padding:10px 12px;font-size:12.5px;line-height:1.55}
.tool .lbl{font-size:12px;color:var(--ink2);margin-top:8px;margin-bottom:4px}
.trail{border-top:1px solid var(--line);font-size:12.5px}
.diff .add{background:var(--add-bg)}.diff .del{background:var(--del-bg)}
.notice{border:0;background:var(--raised);border-radius:10px;padding:9px 13px;font-size:13px;box-shadow:inset 3px 0 0 var(--info)}
.notice.warn{box-shadow:inset 3px 0 0 var(--warn)}.notice.bad{box-shadow:inset 3px 0 0 var(--bad)}.notice.ok{box-shadow:inset 3px 0 0 var(--ok)}
.entry .notice{background:none;box-shadow:none;padding:2px 10px;margin:0;font-size:12.5px;color:var(--ink2)}
.entry .notice details>summary{padding:2px 0;font-size:12.5px}
.entry .notice.bad{color:var(--bad)}
.approve{border:1px solid color-mix(in srgb,var(--warn) 55%,transparent);border-radius:14px;padding:14px 16px;background:var(--raised)}
.approve h4{font-size:14.5px;font-weight:600}
.approve.question{border-color:color-mix(in srgb,var(--info) 55%,transparent)}
.lock-note,.heldbox{border:0;border-radius:10px;background:var(--raised)}
.empty-chat{max-width:560px;margin:14vh auto 0;text-align:center;overflow-wrap:anywhere}
.empty-chat h2{font-size:26px;line-height:1.25;font-weight:600;letter-spacing:-.02em;margin-bottom:10px}
.empty-chat .hints{justify-items:center;margin-top:16px;color:var(--ink2)}
/* the message box floats over the conversation */
.composer{align-self:center;width:calc(100% - 48px);max-width:var(--measure-chat);margin:4px auto 16px;padding:12px 14px 10px;border:1px solid var(--line2);border-radius:18px;background:var(--raised);box-shadow:var(--shadow)}
.composer:focus-within{border-color:color-mix(in srgb,var(--primary) 65%,transparent);box-shadow:var(--shadow),0 0 0 3px var(--soft)}
.composer textarea,.composer textarea:focus{background:transparent;border:0;outline:0;box-shadow:none;padding:2px 2px 0;min-height:30px;font-size:15px;line-height:1.5;border-radius:0}
.composer textarea.shellmode{border:0;color:var(--warn)}
.composer .ghost{padding:2px 2px 0;border:0;font-size:15px;line-height:1.5}
.composer.drag{border-color:var(--primary);box-shadow:0 0 0 3px var(--soft)}
.composer.drag textarea{box-shadow:none}
.composer .notice,.composer .heldbox{background:var(--raised2);margin:0 0 8px}
.cbar{margin-top:6px;font-size:12px}
.cbar .btn{padding:4px 12px;border-radius:9px}
.chip{border:0;background:var(--raised2);border-radius:8px;padding:2px 4px 2px 10px}
.chip.img,.chip.txt{border:0}
.pop{border:1px solid var(--line2);border-radius:12px;background:var(--raised);box-shadow:var(--shadow);padding:4px}
.pop .it{border-radius:8px}.pop .it[aria-selected=true]{background:var(--raised2)}
.pop .it b{font:600 13px var(--font-ui)}

/* ── other pages ── */
.pane{padding:26px 32px 60px}
.pane>h1,.head h1{font-size:22px;font-weight:600}
h2{letter-spacing:0}
.card{border:0;background:var(--raised);border-radius:12px;padding:14px 16px}
.card h3{font-weight:600}
details.adv{border:0;background:var(--raised);border-radius:12px;padding:8px 14px}
details.adv .card{background:var(--page)}
/* a provider's open card is raised, so its fields and buttons sit one step off it instead of vanishing into it */
details.provider>summary .ta{font-family:var(--font-ui)}
details.provider>summary .tn,details.provider>summary .gbadge,details.provider>summary .ts{white-space:nowrap;flex:none}
details.provider input[type=text],details.provider input[type=password],details.adv .card input[type=text],details.adv .card input[type=password]{background:var(--page);border-color:var(--line2)}
details.provider .btn:not(.primary){background:var(--raised2);color:var(--ink)}
.strip{border:0;gap:8px;display:grid}
.kpi,.kpi:first-child{border:0;background:var(--raised);border-radius:12px;padding:12px 14px}
.kpi .v{font-family:inherit;font-size:21px}
table{border-collapse:collapse}
th{font-weight:500;color:var(--ink2)}
th,td{border-color:var(--line)!important}
tbody tr:hover{background:color-mix(in srgb,var(--raised) 60%,transparent)}
.page-hint{background:transparent;padding:10px 32px;border-bottom:1px solid var(--line)}
.ftoolbar{border-bottom:1px solid var(--line);padding:8px 14px;min-height:44px}
.ftoolbar .p{font:13px var(--font-ui)}
.code{background:var(--page);font-size:13px}
.code .ln:hover{background:var(--raised)}
.editor{background:var(--page)}
.sh-out{background:var(--page)}
.sh-in{border-top:1px solid var(--line);padding:10px 14px}
.sh-hist{background:transparent;border-left:1px solid var(--line)}
.sh-hist button.h{border-radius:6px;padding:4px 6px}.sh-hist button.h:hover{background:var(--raised)}
.cmd-out{border-radius:10px}
.cmd-list,.cmd-panel{border:1px solid var(--line);border-radius:12px;background:transparent}
.cmd-list .cat{background:var(--page);border-bottom:1px solid var(--line)}
.cmd-list button.c{border-bottom:0;border-radius:7px;margin:1px 4px;width:calc(100% - 8px)}
.cmd-list .c b{font:600 13px var(--font-ui)}
.palette{border:1px solid var(--line2);border-radius:16px;background:var(--raised);box-shadow:var(--shadow);padding:0}
.palette input{background:transparent;padding:16px 18px}
.palette .list{padding:6px}
.palette .it{border-radius:9px;padding:8px 12px}.palette .it[aria-selected=true]{background:var(--raised2)}
.palette .it b{font:500 14px var(--font-ui)}
.scrim{background:rgba(0,0,0,.5)}
.tab-pop{border:1px solid var(--line2);border-radius:12px;background:var(--raised);box-shadow:var(--shadow)}

/* ── fixes for pages that were wider than their sheet ── */
.center{grid-template-columns:minmax(0,1fr)}
.panes,.pane{min-width:0}
.strip{grid-template-columns:repeat(auto-fit,minmax(132px,1fr));margin-bottom:24px}
.kpi .h{white-space:normal;overflow:visible;text-overflow:clip}
@media (min-width:861px){.strip:has(>:nth-child(6):last-child){grid-template-columns:repeat(3,minmax(0,1fr))}}
.pane svg{max-width:100%}
.md{font:15px/1.65 var(--font-ui)}
.md code{background:var(--raised);padding:1px 5px;border-radius:5px;font-size:.88em;font-family:var(--font-code)}
.cmd-list .cmd-cat>summary.cat::before,.cmd-list .cmd-cat[open]>summary.cat::before{content:""}
.cmd-list .cmd-cat>summary.cat{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--ink2);border:0;border-radius:7px;padding:7px 10px}
.cmd-list .cmd-cat>summary.cat:hover{background:var(--raised);color:var(--ink)}
.cmd-list{padding:4px}
.page-hint{padding:0 0 12px;margin-bottom:14px;background:none}
.ftoolbar .btn,.ftoolbar a.btn{background:none;color:var(--ink2);padding:4px 9px;text-decoration:none;font-size:13px}
.ftoolbar .btn:hover,.ftoolbar a.btn:hover{background:var(--raised);color:var(--ink)}
.ftoolbar .btn.primary{background:var(--primary);color:var(--on-primary)}
a.btn{text-decoration:none}
.chat-head{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}
.chat-head::-webkit-scrollbar{display:none}
.chat-head h1{flex:0 1 auto;min-width:48px;max-width:none}
.chat-head>.sp{flex:1 1 0!important;min-width:0}
.chat-head .pill,.chat-head>.btn{flex:none}

.bpanel{background:var(--page);border-top:1px solid var(--line)}
.bpanel .shell>.ftoolbar>b,.bpanel .shell>.ftoolbar>.muted[title]{display:none}
.bpanel .shell>.ftoolbar{min-height:0;padding:6px 14px;font-size:12px}

/* ── small windows ── */
@media (max-width:900px){.ide-body{padding:0}.lpanel,.rpanel,.center{border-radius:0;border-width:0}}
@media (max-width:720px){.top .tools{overflow-x:auto;scrollbar-width:none;min-width:0;flex:1 1 0}.chat-head{flex-wrap:wrap;overflow:visible}.chat-head h1{flex-basis:100%}.feed{padding:16px 14px 8px}.composer{width:calc(100% - 20px);margin:4px auto 10px}.pane{padding:16px 14px 40px}}

/* ── round 3 fixes: header fit, tables, markdown, toast, phone sheets ── */
/* A hidden side panel must keep its grid column, or the centre sheet slides into the 0-wide one and the page goes blank. */
@media (min-width:901px){.ide-body>.side-nav{grid-column:1}.ide-body>.lpanel{grid-column:2}.ide-body>.rz-l{grid-column:3}.ide-body>.center{grid-column:4}.ide-body>.rz-r{grid-column:5}.ide-body>.rpanel{grid-column:6}.ide-body>*{grid-row:1}}
.chat-head{flex-wrap:wrap;overflow:visible}
.chat-head h1{flex:1 1 auto;min-width:min(160px,100%);max-width:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-head>.sp{display:none}
.chat-head .pill select{field-sizing:content;max-width:min(280px,100%);text-overflow:ellipsis}
.tw{max-width:100%;overflow-x:auto}
.tw table{min-width:100%}
.tw td{white-space:normal;max-width:420px;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}.tw td.r,.tw th.r{white-space:nowrap;overflow-wrap:normal}
.tw th,.tw td:first-child{white-space:nowrap}
.cmd-right .btn{white-space:normal;max-width:100%;text-align:left;height:auto}
.cmd-right .formrow{flex-wrap:wrap;min-width:0}
.cmd-right .formrow>input,.cmd-right input[type=text]{flex:1 1 100%;width:100%;min-width:0;box-sizing:border-box}
.cmd-right .cmd-out,.cmd-right .notice{max-width:100%;box-sizing:border-box}
.pane.wide>.page-hint{padding:12px 18px 10px;margin-bottom:0}
.tscreen{border-radius:0 0 14px 14px}
.toast{bottom:44px;max-width:min(560px,calc(100vw - 32px));box-shadow:var(--shadow);border-radius:10px;border-color:var(--line2);pointer-events:none}
/* the Markdown renderer shifts levels (# becomes h2, ## becomes h3), so h2 is the document title */
.md h1,.md h2{font-family:var(--font-ui);font-size:1.65em;line-height:1.25;font-weight:700;letter-spacing:-.02em;color:var(--ink);margin:1.2em 0 .5em;padding-bottom:.3em;border-bottom:1px solid var(--line2)}
.md h1:first-child,.md h2:first-child,.md h3:first-child{margin-top:0}
.md h3{font-size:1.28em;line-height:1.3;font-weight:650;letter-spacing:-.015em;margin:1.2em 0 .4em;color:var(--ink)}
.md h4{font-size:1.1em;font-weight:600;margin:1em 0 .3em}
.md table{display:block;max-width:100%;overflow-x:auto;border-collapse:separate;border-spacing:0;border:1px solid var(--line2);border-radius:10px;font-size:14px;min-width:0;width:max-content}
.md th,.md td{padding:7px 14px;border-bottom:1px solid var(--line)!important;border-right:1px solid var(--line)!important;text-align:left}
.md th:last-child,.md td:last-child{border-right:0!important}
.md tr:last-child td{border-bottom:0!important}
.md th{background:var(--raised);color:var(--ink);font-weight:600}
.md tbody tr:nth-child(even){background:color-mix(in srgb,var(--raised) 45%,transparent)}
@media (max-width:900px){
.top .btn.hide-small,.top .hide-small{display:none}
.top{padding:0 8px;gap:6px}.top .brand span:last-child{display:none}
.ws-chip{min-width:0;flex:1 1 auto;overflow:hidden}.ws-chip b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top .tools{flex:0 0 auto;overflow:visible}
.statusbar{padding:0 10px;gap:10px}
.side-nav{padding:2px 2px calc(2px + env(safe-area-inset-bottom));background:var(--page)}
.side-nav a{min-width:0;flex:1 1 0;padding:8px 0 6px}
.drawer-scrim{z-index:24}
.lpanel,.rpanel{box-shadow:none}body.drawer-left .lpanel,body.drawer-right .rpanel{box-shadow:0 0 40px rgb(0 0 0/.4)}
body.dock-open .drawer-scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.45)}
.bpanel{border-radius:16px 16px 0 0;height:min(60vh,520px);border:1px solid var(--line2);border-bottom:0}
.lpanel,.rpanel{border-radius:0 16px 16px 0}.rpanel{border-radius:16px 0 0 16px}
.ptabs .mini,.phead .mini{border:1px solid var(--line2);color:var(--ink);padding:6px 14px;min-height:34px;font-weight:500}
.ptabs .mini::before,.phead .mini::before{content:"\2715\00a0"}
.toast{bottom:112px}
}
@media (max-width:620px){
.side-nav a .lbl{display:none}.side-nav a{padding:12px 0}
}
@media (max-width:400px){.top .tools .btn{padding:5px 7px}}

/* ── the turn view: header, steps, rows, answer ── */
.entry.a{margin-bottom:28px}
.thead{position:sticky;top:0;z-index:4;display:flex;align-items:center;gap:8px;width:100%;min-height:36px;padding:4px 10px 4px 4px;margin:0;background:var(--page);border:0;border-radius:10px;color:var(--ink);font:500 14px/1.4 var(--font-ui);text-align:left;cursor:pointer}
.thead:hover{background:var(--raised);color:var(--ink)}
.thead:focus-visible{outline-offset:-2px}
.thead .mk{background:none}
.entry.a{position:relative}
.thead .tl{font-weight:600;white-space:nowrap}
.thead .tel{color:var(--ink2);font-weight:400;white-space:nowrap}
.thead .tmeta{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 7px;min-width:0;flex:1 1 auto;color:var(--ink2);font-size:13px;font-weight:400}
.thead .mi{white-space:nowrap}
.thead .mi+.mi::before{content:"\00b7";margin-right:7px}
.thead .mi.bad{color:var(--bad);font-weight:500}
.thead .chev{flex:none;width:7px;height:7px;margin:0 4px 0 auto;border-right:1.6px solid var(--ink2);border-bottom:1.6px solid var(--ink2);transform:rotate(45deg) translate(-1px,-1px);transition:transform .12s ease-out}
.entry.a[data-steps=closed] .thead .chev,.t-compact .entry.a:not([data-steps=open]) .thead .chev{transform:rotate(-45deg)}
.entry.a[data-steps=closed] .tsteps,.t-compact .entry.a:not([data-steps=open]) .tsteps{display:none}
.entry.a[data-steps=closed] .tsteps:has(.hp),.t-compact .entry.a:not([data-steps=open]) .tsteps:has(.hp){display:block}
:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .tsteps::before{display:none}
:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .step:not(.hp){display:none}
:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .step.hp>:not(.sbody),:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .step.hp .tool:not(.hp),:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .step.hp .narr,:is(.entry.a[data-steps=closed],.t-compact .entry.a:not([data-steps=open])) .step.hp .phead{display:none}
.tsteps{position:relative;margin:2px 0 4px;padding:0}
.tsteps::before{content:"";position:absolute;left:13px;top:16px;bottom:16px;width:1px;background:var(--line2)}
.mk{position:relative;flex:none;display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;background:var(--page);vertical-align:middle}
.mk .spin{width:11px;height:11px;border-width:1.7px;vertical-align:baseline}
.mk.ok::after{content:"";width:5px;height:9px;border:solid var(--ok);border-width:0 1.8px 1.8px 0;transform:translateY(-1px) rotate(45deg)}
.mk.bad::before,.mk.bad::after{content:"";position:absolute;left:50%;top:50%;width:10px;height:1.8px;margin:-.9px 0 0 -5px;background:var(--bad);border-radius:1px}
.mk.bad::before{transform:rotate(45deg)}.mk.bad::after{transform:rotate(-45deg)}
.mk.wait::after{content:"";width:9px;height:9px;border:1.8px solid var(--warn);border-radius:50%}
.mk.stop::after{content:"";width:8px;height:1.8px;background:var(--ink2);opacity:.7}
.phead>.mk::after{content:"";width:5px;height:5px;border-radius:50%;background:var(--line2)}
/* step: one model call */
.step{margin:0}
.shead{display:grid;grid-template-columns:18px auto minmax(0,1fr) auto 56px;column-gap:8px;align-items:center;min-height:30px;margin:6px 0 0;padding:0 8px 0 4px;font:600 13px/1.4 var(--font-ui);color:var(--ink)}
.step:first-child>.shead{margin-top:0}
.ssub{font-weight:400;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ssub.bad{color:var(--bad)}
.stime,.scost,.td,.ptime{font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
.stime{font-weight:500;color:var(--ink)}
.scost{font-weight:400;color:var(--ink2);margin-right:6px}
.sdetail{display:flex;flex-wrap:wrap;gap:0 14px;margin:0 8px 2px 34px;font:12px/1.5 var(--font-ui);color:var(--ink2)}
.sdetail .miss{color:var(--warn)}
.sbody{margin:0}
/* rows */
.tsteps details.tool,.tsteps details.tool[open]{margin:0;padding:0;border:0;border-radius:0;background:none}
.tsteps details>summary::before{display:none}
.tsteps details.tool>summary.xrow{display:flex;flex-wrap:wrap;align-items:center;column-gap:8px;min-height:28px;padding:1px 8px 1px 4px;border-radius:0;background:none;font:400 13px/1.5 var(--font-ui);color:var(--ink2);cursor:pointer;list-style:none}
.tsteps details.tool>summary.xrow::-webkit-details-marker{display:none}
.tsteps details.tool>summary.xrow:hover{background:none}
.tsteps details.tool>summary.xrow:hover .tn{color:var(--info)}
.xrow>.mk{flex:none}
.xrow .tn{flex:0 0 96px;width:96px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:500 13px/1.5 var(--font-ui);color:var(--ink)}
.xrow .ta{flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:400 13px/1.5 var(--font-ui);color:var(--ink2)}
.xrow .ta a{color:var(--info)}
.xrow .chips{flex:none;display:flex;gap:4px;justify-content:flex-end}
.xrow .chips:empty{display:none}
.chip2{font:500 12px/18px var(--font-ui);padding:0 6px;border-radius:5px;background:var(--raised);color:var(--ink2);white-space:nowrap;font-variant-numeric:tabular-nums}
.chip2.add{background:var(--add-bg);color:var(--add)}.chip2.del{background:var(--del-bg);color:var(--del)}
.xrow .td{flex:0 0 56px;font:400 12px/1.5 var(--font-ui);color:var(--ink2)}
.xrow .pnote{order:9;flex:1 0 100%;padding:0 0 3px 26px;font:400 12px/1.5 var(--font-ui);color:var(--ink2)}
.xrow .pnote.bad{color:var(--bad)}
.tool[data-state=bad]>summary .tn,.tool[data-state=bad]>summary .ta,.tool[data-state=bad]>summary .td{color:var(--bad)}
.tool[data-state=wait]>summary .ta{color:var(--warn)}
.tdet{margin:2px 8px 8px 30px;padding:8px 10px 10px;border-radius:10px;background:var(--raised)}
.tsteps details.tool>.tdet{margin-left:30px;margin-right:8px}
.tool.think>.tdet{background:none;padding:0 0 4px}
.tool.think pre{background:none;padding:0 0 0 2px;color:var(--ink2);font:13px/1.55 var(--font-ui);white-space:pre-wrap;max-height:240px;overflow:auto}
.tabs2{display:flex;align-items:center;gap:2px;margin:0 0 8px}
.tab2,.copy{background:none;border:0;border-radius:6px;padding:2px 10px;font:500 12px/1.6 var(--font-ui);color:var(--ink2);cursor:pointer}
.tab2:hover,.copy:hover{background:var(--raised2);color:var(--ink)}
.tab2[aria-selected=true]{background:var(--page);color:var(--ink)}
.copy{margin-left:auto}
.tpane .why,.tpane .perm{margin:0 0 6px;font-size:13px;color:var(--ink)}
.tpane .perm{color:var(--ink2)}
.tpane pre.out{background:var(--code-bg)}
/* parallel group */
.pgroup{margin:2px 0}
.phead{display:grid;grid-template-columns:18px minmax(0,1fr) 56px;column-gap:8px;align-items:center;min-height:24px;padding:0 8px 0 4px;font:500 12px/1.4 var(--font-ui);color:var(--ink2)}
/* the Shell dock's own header shares the class with a step group's header above: keep it a plain row */
.bpanel>.phead{display:flex;grid-template-columns:none;gap:2px 12px;align-items:center;padding:8px 14px;min-height:44px;font:inherit;color:var(--ink)}
.bpanel>.phead .hint{font-size:12px;color:var(--ink2)}
.prows{position:relative}
.prows::after{content:"";position:absolute;left:35px;top:1px;bottom:1px;width:2px;border-radius:2px;background:var(--line2);pointer-events:none}
.prows .xrow .tn{flex-basis:108px;width:108px;padding-left:12px}
.prows>.approve{margin-left:30px}
/* narration, answer */
.tool.err .tn::before,.tool.denied .tn::before{display:none}
.narr.md{margin:2px 8px 6px 30px;font-family:var(--font-response);font-size:calc(var(--size-response) - 1px);line-height:1.6;color:var(--ink2)}
.tanswer{margin:0}
.tanswer:empty{display:none}
.tsteps:not(:empty)+.tanswer:not(:empty){margin-top:12px;padding-top:14px;border-top:1px solid var(--line)}
.t-compact .tsteps+.tanswer{margin-top:4px;padding-top:0;border-top:0}
.entry.a .tanswer .md,.entry.a>.md{font-family:var(--font-response);font-size:var(--size-response);line-height:1.65}
.entry.a .md code,.entry.a .md pre,.entry.a .md pre code{font-family:var(--font-code);font-size:calc(var(--size-code) - .5px)}
.entry.a .md th,.entry.a .md td{font-family:var(--font-ui);font-size:14px}
.tsteps .notice{margin:2px 8px 4px 30px}
.tsteps .steprow{margin:0 8px 2px 30px}
.tsteps>.approve,.sbody>.approve{margin:6px 8px 10px 30px}
.sbody>.approve,.prows>.approve{margin:4px 8px 10px 30px}
.t-compact .tsteps .rawev{display:none}
@media (max-width:560px){
.thead{flex-wrap:wrap;row-gap:0}
.thead .tmeta{flex-basis:100%;order:5;padding-left:26px;margin-top:-4px}
.thead .chev{margin-left:auto}
.shead{grid-template-columns:18px auto minmax(0,1fr) auto 44px}
.xrow .tn{flex-basis:64px;width:64px}
.prows .xrow .tn{flex-basis:76px;width:76px}
.xrow .td{flex-basis:46px}
.xrow .chips{order:8;flex:1 0 100%;justify-content:flex-start;padding:0 0 2px 26px}
.tdet{margin-left:4px}.tsteps details.tool>.tdet{margin-left:4px}
.narr.md,.tsteps .notice,.tsteps .steprow{margin-left:26px}
.sdetail{margin-left:26px}
.phead{grid-template-columns:18px minmax(0,1fr) 46px}
}

"""
)


FINISH_CSS = r"""
/* ═══ Conduction ═══
   A turn is a signal travelling along a fibre: one hairline with a node per tool call, everything else recedes.
   Neutrals are true grays, actions are the brand teal, and a light gray is kept for one thing: a signal in flight.
   This layer is served after every other sheet. Sizes stay on the app's scale (12 13 14 15 16 17 22), plus 28 and 34 for
   page titles and the Usage hero figure. */

/* ── foundations ── */
html{background:var(--canvas)}
body{background:var(--canvas);color:var(--ink);letter-spacing:0;text-rendering:optimizeLegibility}
::selection{background:color-mix(in srgb,var(--primary) 28%,transparent)}
a{color:var(--link)}
.pane a:not(.btn):not(.plink):not(.hit):not(.mg-main):not(.rrow){text-underline-offset:2px;text-decoration-thickness:1px}
a.plink{color:var(--link);text-underline-offset:2px}
.muted,.sub,.note{font-size:inherit}
p.muted,.sub{font-size:14px;line-height:1.55}
.muted{color:var(--ink2)}
.kv dt{font-size:13px}.kv dd{font-family:var(--font-ui);font-size:13px}
input[type=checkbox],input[type=radio]{width:16px;height:16px;margin:4px;accent-color:var(--primary);flex:none}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;box-shadow:none;border-radius:6px}
.tl:focus-visible,.tx:focus-visible,.node:focus-visible,.fp-row:focus-visible,.thead:focus-visible,summary:focus-visible,.vz:focus-visible{outline-offset:-2px}

/* controls */
.btn{background:var(--raised);border:1px solid transparent;border-radius:9px;padding:5px 12px;font-size:13px;font-weight:500;color:var(--ink);line-height:1.5}
.btn:hover{background:var(--raised2);border-color:transparent}
.btn.primary{background:var(--primary);color:var(--on-primary);font-weight:600}
.btn.primary:hover{background:var(--primary);filter:brightness(1.08)}
.btn.danger{background:transparent;border-color:color-mix(in srgb,var(--bad) 40%,transparent);color:var(--bad)}
.btn.danger:hover{background:color-mix(in srgb,var(--bad) 10%,transparent);border-color:color-mix(in srgb,var(--bad) 60%,transparent)}
.btn[disabled]{opacity:.5}
.mini{border:0!important;background:none;border-radius:7px;padding:2px 8px;font-size:12px;color:var(--ink2)}
.mini:hover{background:var(--raised2);color:var(--ink)}
.mini.primary{color:var(--primary);font-weight:600}.mini.danger{color:var(--bad)}
.seg{background:var(--raised);border:0;border-radius:10px;padding:2px;gap:0}
.seg button{border:0;border-radius:8px;padding:4px 11px;font-size:13px;color:var(--ink2)}
.seg button:hover{color:var(--ink)}
.seg button[aria-pressed=true]{background:var(--page);color:var(--ink);box-shadow:0 0 0 1px var(--line),0 1px 2px var(--tint-shadow)}
input[type=text],input[type=search],input[type=password],input[type=number],input[type=date],input[type=time],input[type=datetime-local],select,textarea,.search,.formrow input[type=text],.formrow input[type=search],.cmd-search,.cn-q,.ch-q,.pl-q,.fp-find,.br-url,.ap-ctl select,.ap-ctl input[type=text],.pl-row input,.pl-row textarea,.pl-row select,.tkform input[type=text],.tkform input[type=number],.tkform input[type=time],.tkform input[type=datetime-local],.tkform select,.tkform textarea,.mg-memadd textarea,.mg-mem textarea,.cf-dlg textarea,.cx-edit,.cx-find input,.sh-in input{background:var(--field);border:1px solid var(--line2);border-radius:9px;padding:6px 10px;font-size:14px;color:var(--ink)}
input[type=text]:focus,input[type=search]:focus,input[type=password]:focus,input[type=number]:focus,input[type=date]:focus,input[type=time]:focus,select:focus,textarea:focus,.search:focus,.cmd-search:focus,.fp-find:focus-within,.br-url:focus,.sh-in input:focus{outline:0;border-color:var(--primary);box-shadow:0 0 0 3px var(--soft)}
::placeholder{color:var(--ink2);opacity:.85}
.fp-find input.fp-q,.fp-find input.fp-q:focus,.composer textarea,.composer textarea:focus{background:none;border:0;box-shadow:none;padding:0;border-radius:0;min-height:0}
.kbd{background:var(--raised2);border:0;border-radius:5px;padding:0 5px;font-size:12px;color:var(--ink2)}
.helptip{width:16px;height:16px;min-width:16px;min-height:16px;border-color:var(--line2);font-size:12px;opacity:.75}
.rs .helptip{opacity:0}.rs:hover .helptip,.rs:focus-within .helptip,.helptip[aria-expanded=true]{opacity:1}
@media (hover:none){.rs .helptip{opacity:.75}}
.dot.running{background:var(--accent)}
.good{color:var(--ok)}.bad{color:var(--bad)}

/* ── the frame: sheets on a canvas ── */
:root{--rail:74px}
.ide{background:var(--canvas)}
.top{height:48px;background:transparent;border:0;padding:0 10px 0 22px;gap:2px;backdrop-filter:none}
.top .brand{gap:8px;font-size:15px;font-weight:600;letter-spacing:-.01em;margin-right:14px}
.top .brand svg{height:20px}
.ws-chip,.top .ctitle{position:relative;padding:4px 8px;border:0;border-radius:8px;background:none;font-size:14px;font-weight:500;color:var(--ink);min-height:28px;flex:0 1 auto}
.ws-chip{color:var(--ink2);gap:0}
.ws-chip b{font-weight:500}
.ws-dot{display:none}
.top .ctitle{margin-left:12px;max-width:min(40vw,460px)}
.top .ctitle::before{content:"/";position:absolute;left:-9px;color:var(--ink2);opacity:.55;font-weight:400}
.ws-chip:hover,.top .ctitle:hover:not(:disabled){background:var(--raised);color:var(--ink)}
.tbi{width:32px;height:32px;border-radius:9px}
.tbi:hover{background:color-mix(in srgb,var(--ink) 7%,transparent)}
.tbi[aria-pressed=true]{background:color-mix(in srgb,var(--ink) 9%,transparent);color:var(--ink)}
.tbi .ico{width:17px;height:17px}
.ico{stroke-width:1.6}
.ide-body{padding:0 8px 8px 0;column-gap:0}
.side-nav{background:transparent;border:0;padding:4px 0;gap:2px;overflow:visible}
.side-nav a{position:relative;padding:8px 2px 6px;gap:3px;border-radius:0;color:var(--ink2);min-height:50px}
.side-nav a .ico{width:19px;height:19px}
.side-nav a .lbl{font-size:12px;letter-spacing:-.01em;max-width:100%}
.side-nav a:hover{background:none;color:var(--ink)}
.side-nav a[aria-current=page]{background:none;color:var(--ink);box-shadow:none}
.side-nav a[aria-current=page] .ico{color:var(--primary)}
.side-nav a[aria-current=page] .lbl{font-weight:600}
.side-nav a[aria-current=page]::after{content:"";position:absolute;right:-4px;top:50%;width:8px;height:8px;margin-top:-4px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 2px var(--canvas);z-index:3}
.side-nav a:focus-visible{outline-offset:-3px;border-radius:10px}
.center,.lpanel,.rpanel{background:var(--page);border:0;border-radius:16px;box-shadow:0 0 0 1px var(--line);overflow:hidden}
.rz{background:transparent}
.rz:hover,.resizing .rz{background:color-mix(in srgb,var(--primary) 50%,transparent)}
.ide-body>.rwrap{background:transparent;gap:8px}
.ide-body>.rz-r{width:8px}
.rwrap>.rpanel{border:0;border-radius:16px;background:var(--page)}
.wpanel.has{border:0;gap:0;min-width:0}
.wcol{gap:8px}
.wp{border-radius:16px;box-shadow:0 0 0 1px var(--line);background:var(--page)}
.wp+.rz-h+.wp,.wp+.wp{border-top:0}
.rz-h{background:transparent;height:8px;flex:0 0 8px;margin:-8px 0 -8px;z-index:2}
.wpanel .rz-w{flex:0 0 8px;background:transparent}
.ide-body.wbmax .rwrap{margin-left:8px}
@media (prefers-reduced-motion:no-preference){.wpanel.has{animation:ax-in .16s ease-out}}
.statusbar{height:28px;padding:0 14px 0 calc(var(--rail) + 2px);margin-top:-6px;gap:14px;background:transparent;border:0;font-size:12px;color:var(--ink2)}
.statusbar.in-chat{display:none}
.statusbar .sb b{font-weight:500;color:var(--ink2)}
.statusbar .live b{color:var(--ink)}
.center>.tabstrip{grid-row:1}.center>.panes{grid-row:2}
/* tab strip: only there with two or more tabs */
.tabstrip{background:transparent;border-bottom:1px solid var(--line);padding:6px 8px;gap:2px;overflow:visible}
.tab{border-radius:8px;max-width:200px;box-shadow:none}
.tab.on{background:var(--raised);box-shadow:none}
.tab.split:not(.on){box-shadow:inset 0 0 0 1px var(--line2)}
.tab .tl{padding:3px 2px 3px 9px;font-size:13px;color:var(--ink2)}.tab.on .tl{color:var(--ink);font-weight:500}
.tab .tl .ico{display:none}
.tab .tl span{max-width:150px}
.tab .tx{padding:0 7px;font-size:15px;opacity:.65}.tab .tx:hover{opacity:1}
.tabstrip>.mini{margin:0;padding:2px 8px;white-space:nowrap}
.tab-pop{background:var(--float);border:1px solid var(--line2);border-radius:12px;box-shadow:var(--shadow)}

/* ── chat: the answer is the hero ── */
.chat{display:flex;flex-direction:column}
.feed{padding:28px 28px 24px;scroll-padding-bottom:20px;overflow-anchor:none;-webkit-mask-image:linear-gradient(to bottom,#000 calc(100% - 22px),transparent);mask-image:linear-gradient(to bottom,#000 calc(100% - 22px),transparent)}
.feed>*{max-width:var(--measure-chat);margin-left:auto;margin-right:auto}
.feed>.entry{padding:0;border:0;margin-bottom:26px}
.feed>.entry.n{margin-bottom:12px}
.entry>.who{display:none}
.entry.fresh{animation:none}
.entry.u.fresh{animation:ax-rise .22s cubic-bezier(.2,.7,.2,1)}
@keyframes ax-rise{from{opacity:0;transform:translateY(6px)}}
/* your words */
.entry.u{display:flex;flex-direction:column;align-items:flex-end;margin-top:6px}
.entry.u .md{background:var(--you);color:var(--ink);padding:9px 14px;border-radius:16px 16px 4px 16px;max-width:82%;font:15px/1.5 var(--font-ui);overflow-wrap:anywhere}
.entry.u .md pre,.entry.u .md pre.out{background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;white-space:pre-wrap;max-height:none}
/* the turn head: mark, words, fibre */
.entry.a{position:relative}
.thead{position:sticky;top:-1px;z-index:4;display:flex;align-items:center;flex-wrap:nowrap;gap:8px;width:calc(100% + 8px);min-height:30px;margin:0 -8px 0 -6px;padding:3px 8px 3px 6px;background:var(--page);border:0;border-radius:9px;color:var(--ink2);font:400 13px/1.4 var(--font-ui);text-align:left;cursor:pointer}
.thead:hover{background:var(--raised);color:var(--ink)}
.thead .mk{background:none}
.thead .tl{font-weight:500;color:inherit;white-space:nowrap}
.entry.a.live .thead .tl{color:var(--ink)}
.thead .tel{font-weight:400;color:var(--ink2);white-space:nowrap;font-variant-numeric:tabular-nums;min-width:3ch}
.thead .tel:empty{display:none}
.thead .tmeta{flex:0 1 auto;min-width:0;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2);font-size:13px;font-weight:400}
.thead .mi{white-space:nowrap}
.thead .mi+.mi{margin-left:10px}.thead .mi+.mi::before{content:none}
.thead .mi.bad{color:var(--bad)}
.thead .chev{width:6px;height:6px;margin:0 3px 0 auto;flex:none;border-color:var(--ink2);opacity:.75}
.fib{position:relative;flex:0 1 auto;display:inline-flex;align-items:center;gap:9px;min-width:0;max-width:44%;height:12px;padding:0 9px;overflow:hidden}
.fib:empty{display:none}
.fib::before{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--line2)}
.fib i{position:relative;flex:none;width:5px;height:5px;border-radius:50%;background:var(--ink2);opacity:.7;transition:background-color .2s ease-out,opacity .2s ease-out}
.fib i:nth-last-child(n+24){display:none}
.fib i.ok{background:var(--ink2);opacity:.75}
.fib i.bad{background:var(--bad);opacity:1;width:7px;height:7px}
.fib i.wait{background:var(--warn);opacity:1;width:7px;height:7px}
.fib i.run{background:var(--accent);opacity:1}
.fib i.stop{opacity:.35}
.entry.a.live .fib::after{content:"";position:absolute;top:50%;left:0;width:34px;height:1px;background:linear-gradient(90deg,transparent,var(--accent));animation:ax-conduct 1.5s cubic-bezier(.4,0,.6,1) infinite}
@keyframes ax-conduct{from{transform:translateX(-40px)}to{transform:translateX(420px)}}
@keyframes ax-node{50%{transform:scale(1.5)}}
/* marks: quiet when fine, loud only when it matters */
.mk{width:16px;height:16px;background:none}
.mk.ok::after{width:4px;height:8px;border-color:var(--ink2);border-width:0 1.5px 1.5px 0}
.mk.run .spin,.mk .spin{width:7px;height:7px;border:0;border-radius:50%;background:var(--accent);animation:ax-node 1.1s ease-in-out infinite}
.mk.stop::after{background:var(--ink2)}
.thead .mk.run .spin{animation:none}
.thead:has(.fib:empty) .mk.run .spin{animation:ax-node 1.1s ease-in-out infinite}
/* steps: each model call has a head (number, tools, time, cost) and two lines saying what it was sent and what came back.
   Only the compact trace level, which is opt-in, leaves them out. */
.t-normal .entry.a.live .tmeta{display:none}
.tsteps{margin:2px 0 0;padding:0}
.tsteps::before{content:none}
.t-compact .shead,.t-compact .pgroup>.phead,.t-compact .sdetail,.t-compact .xrow .route{display:none}
.sdetail{font:400 12px/1.5 var(--font-ui);color:var(--ink2)}
.sline{position:relative;display:flex;flex-wrap:wrap;column-gap:12px;padding-left:40px}
.sline:empty{display:none}
.sline>b{position:absolute;left:0;top:0;font-weight:500;color:var(--ink2)}
.steprow>b{flex:0 0 auto;font-weight:500;color:var(--ink2)}
.sline>span{font:inherit;margin:0;padding:0}
.sline .bad,.steprow .bad{color:var(--bad)}
.tsteps .steprow{font:400 12px/1.5 var(--font-ui);column-gap:12px;row-gap:0;padding:0}
.xrow .route{flex:none;display:flex;gap:4px}
.xrow .route:empty{display:none}
.chip2.rt{background:none;box-shadow:inset 0 0 0 1px var(--line);color:var(--ink2);white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}
.chip2.rt.bad{color:var(--bad)}
@media (max-width:560px){.xrow .route{order:8;flex:1 0 100%;flex-wrap:wrap;padding:0 0 2px 26px}}
.shead{grid-template-columns:16px auto minmax(0,1fr) auto 56px;min-height:26px;margin:6px 0 0;padding:0 8px 0 0;font:500 13px/1.4 var(--font-ui);color:var(--ink2)}
.stime{font-weight:400;color:var(--ink2)}
.sdetail{margin:0 8px 2px 24px}
.pgroup{margin:0}
.prows::after{content:none}
.prows .xrow .tn{flex-basis:72px;width:72px;padding-left:0}
.pgroup>.phead{grid-template-columns:16px minmax(0,1fr) 56px;padding:0 8px 0 0;min-height:24px;font-size:12px}
.tsteps details.tool>summary.xrow{min-height:26px;padding:0 6px 0 0;border-radius:7px;font:400 13px/1.45 var(--font-ui);column-gap:8px}
.tsteps details.tool>summary.xrow:hover{background:var(--raised)}
.tsteps details.tool>summary.xrow:hover .tn{color:var(--ink)}
.xrow .tn{flex:0 0 72px;width:72px;font:500 13px/1.45 var(--font-ui);color:var(--ink)}
.xrow .ta{font:400 13px/1.45 var(--font-ui);color:var(--ink2)}
.xrow .td{flex:0 0 auto;min-width:40px;font:400 12px/1.45 var(--font-ui);color:var(--ink2);text-align:right;font-variant-numeric:tabular-nums}
.xrow .pnote{padding-left:24px}
.chip2{background:var(--raised);border-radius:5px;font-size:12px;padding:0 6px;font-weight:400}
.chip2.add{background:none;color:var(--add);padding:0 2px}.chip2.del{background:none;color:var(--del);padding:0 2px}
.tsteps details.tool>.tdet{margin:2px 0 8px 24px;padding:8px;border-radius:10px;background:var(--raised)}
.tool.think>.tdet,.tsteps details.tool.think>.tdet{background:none;padding:0 0 4px}
.tool.think pre{font:13px/1.55 var(--font-ui);max-height:160px;color:var(--ink2);background:none;padding:0}
.xrow .think-t{flex:1 1 auto}
.tabs2{gap:0;margin:0 0 6px}
.tab2,.copy{border-radius:7px;padding:1px 9px;font:500 12px/1.7 var(--font-ui);min-height:24px}
.tab2:hover,.copy:hover{background:var(--raised2)}
.tab2[aria-selected=true]{background:var(--page);box-shadow:0 0 0 1px var(--line)}
pre.out,.tool pre,.tpane pre.out{background:var(--code-bg);border:0;border-radius:8px;padding:9px 11px;font-size:var(--size-code);line-height:1.5;color:var(--ink)}
.tpane .why,.tpane .perm{font-size:13px;line-height:1.5}
.narr.md{margin:4px 8px 6px 24px;font:14px/1.55 var(--font-ui);color:var(--ink2)}
.narr.md p{margin:0 0 .4em}
.tsteps .notice,.tsteps .steprow{margin:2px 8px 4px 24px}
.tsteps>.approve,.sbody>.approve,.prows>.approve{margin:6px 0 10px 24px}
.tanswer:empty{display:none}
.tanswer{margin:0;padding:0}
.tsteps:not(:empty)+.tanswer:not(:empty),.t-compact .tsteps+.tanswer{margin-top:8px;padding-top:0;border-top:0}
/* every step stays on screen under a finished turn. Only the viewer's "Collapse steps" choice folds them (the head's chevron opens one turn again) */
html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .thead .chev{transform:rotate(-45deg)}
html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .tsteps{display:none}
html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .tsteps:has(.hp){display:block}
html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .step:not(.hp){display:none}
html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .step.hp>:not(.sbody),html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .step.hp .tool:not(.hp),html[data-steps-view=hidden] .entry.a:not(.live):not([data-steps=open]) .step.hp .narr{display:none}
/* the kinds of step: a label and icon each, in grays, with teal only for a tool call (the thing that acts) */
.kind{display:inline-flex;align-items:center;gap:5px;flex:none;font:600 12px/1.4 var(--font-ui);color:var(--ink2);letter-spacing:.01em;white-space:nowrap}
.kind .ico{width:13px;height:13px;stroke-width:1.7}
.kind.k-tool{color:var(--primary)}
.kind.k-answer{color:var(--ink)}
.kind.k-think{color:var(--ink2)}
.kind.k-note{color:var(--ink2);margin:0 0 2px;display:flex}
.tool.kd-tool>summary .kind{min-width:84px}
.tool.kd-think>summary .kind{min-width:84px}
.tsteps details.tool.kd-tool,.tsteps details.tool.kd-think{border-left:2px solid var(--line2);border-radius:0 10px 10px 0;padding-left:6px;margin:6px 0}
.tsteps details.tool.kd-tool{border-left-color:color-mix(in srgb,var(--primary) 60%,transparent)}
.tsteps details.tool.kd-tool[data-state=bad]{border-left-color:var(--bad)}
.tsteps details.tool.kd-tool[data-state=run]{border-left-color:var(--ink2)}
.tsteps .narr.md{border-left:2px solid var(--line);padding-left:8px;margin-left:8px}
.tanswer .kind.k-answer{display:flex;margin:0 0 6px;padding-top:2px}
.tanswer:not(:empty){border-top:1px solid var(--line2);margin-top:14px;padding-top:12px}
.tsteps:empty+.tanswer:not(:empty){border-top:0;margin-top:0;padding-top:0}
/* an open tool call shows its input and its result together, not behind tabs */
.tool.both>.tdet .tabs2 .tab2{display:none}.tool.both>.tdet .tabs2{margin:0;justify-content:flex-end;position:absolute;right:14px;margin-top:2px}.tool.both>.tdet{position:relative}
.tool.both>.tdet .tpane.p-in,.tool.both>.tdet .tpane.p-out{display:block!important}
.tool.both>.tdet .tpane.p-in::before,.tool.both>.tdet .tpane.p-out::before{display:block;font:600 12px/1.6 var(--font-ui);color:var(--ink2);margin:2px 0 2px}
.tool.both>.tdet .tpane.p-in::before{content:"Input"}
.tool.both>.tdet .tpane.p-out::before{content:"Result"}
.tool.both>.tdet .tpane.p-in{margin-bottom:6px}
.tool.both>.tdet pre.out{max-height:260px;overflow:auto}
.tool.think pre{max-height:320px}
.steps-all{font-size:12px;padding:3px 10px}
.entry.a[data-steps=open] .tsteps{animation:ax-fade .16s ease-out}
.entry.a[data-steps=open] .thead .chev{transform:rotate(45deg) translate(-1px,-1px)}
/* the answer */
.entry.a .tanswer .md,.entry.a>.md{font-family:var(--font-response);font-size:var(--size-response);line-height:1.62;color:var(--ink);letter-spacing:0;max-width:none}
.md p{margin:0 0 .8em}.md p:last-child{margin-bottom:0}
.md h1,.md h2{font:600 22px/1.3 var(--font-ui);letter-spacing:-.012em;margin:1.3em 0 .45em;padding:0;border:0;color:var(--ink)}
.md h3{font:600 17px/1.35 var(--font-ui);letter-spacing:-.005em;margin:1.3em 0 .35em;color:var(--ink)}
.md h4,.md h5,.md h6{font:600 15px/1.4 var(--font-ui);margin:1.1em 0 .3em;color:var(--ink)}
.md>:first-child{margin-top:0}
.md ul,.md ol{margin:.2em 0 .8em;padding-left:1.35em}.md li{margin:.2em 0}.md li::marker{color:var(--ink2)}
.md blockquote{margin:.6em 0;padding-left:14px;border-left:2px solid var(--line2);color:var(--ink2)}
.md hr{border:0;border-top:1px solid var(--line);margin:1.4em 0}
.md a{color:var(--link)}
.md code,.entry .md code,.entry.a .md code{background:var(--raised);color:var(--ink);padding:1px 5px;border-radius:5px;font-family:var(--font-code);font-size:.875em}
.md pre,.entry .md pre,.entry.a .md pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin:.8em 0;font-family:var(--font-code);font-size:var(--size-code);line-height:1.5}
.md pre code,.entry .md pre code,.entry.a .md pre code{background:none;padding:0;font-size:inherit}
.md table{border:1px solid var(--line);border-radius:10px;margin:.8em 0}
.md th{background:var(--raised);font:600 13px/1.4 var(--font-ui)}.md td{font:400 14px/1.45 var(--font-ui)}
/* message actions */
.mxa{background:var(--float);border:0;box-shadow:0 0 0 1px var(--line),0 4px 14px var(--tint-shadow);border-radius:9px;padding:2px;transition:opacity .12s ease-out}
.mxa button{border-radius:7px;font-size:12px}
.entry.a>.mxa{bottom:-26px;right:0}
.jump-latest{background:var(--float);border:0;box-shadow:0 0 0 1px var(--line2),var(--shadow);font-size:13px;padding:5px 14px}
/* notices, approvals, questions */
.notice{background:var(--raised);border:0;border-radius:10px;padding:8px 12px;font-size:13px;color:var(--ink);box-shadow:none}
.notice.warn,.notice.bad,.notice.ok{box-shadow:none}
.notice.warn{background:color-mix(in srgb,var(--warn) 11%,var(--page))}
.notice.bad{background:color-mix(in srgb,var(--bad) 10%,var(--page))}
.notice.ok{background:color-mix(in srgb,var(--ok) 10%,var(--page))}
.entry .notice,.feed>.notice{background:none;box-shadow:none;padding:2px 0;font-size:13px;color:var(--ink2)}
.feed>.notice.warn{color:var(--warn)}
.err-box{border:0;background:color-mix(in srgb,var(--bad) 10%,var(--page));color:var(--ink);border-radius:10px;padding:10px 14px;font-size:14px;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--bad) 28%,transparent)}
.approve{border:0;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--warn) 45%,transparent);background:color-mix(in srgb,var(--warn) 6%,var(--page));border-radius:14px;padding:14px 16px}
.approve.question{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--info) 40%,transparent);background:color-mix(in srgb,var(--info) 5%,var(--page))}
.approve h4{font-size:15px;font-weight:600}.approve .why{font-size:13px;line-height:1.5}
.approve .acts .btn{max-width:100%;white-space:normal;text-align:left}
.lock-note,.heldbox{border:0;border-radius:10px;background:var(--raised);box-shadow:none}
/* the empty chat: the start of a fibre */
.feed>.empty-chat{max-width:min(var(--measure-chat),620px);margin:max(8vh,32px) auto 0;text-align:left}
.empty-chat h2{font:500 34px/1.2 var(--font-ui);letter-spacing:-.022em;color:var(--ink);margin:0 0 8px}
.empty-chat>p{margin:0;font-size:15px;color:var(--ink2)}
.starters{position:relative;display:flex;flex-direction:column;align-items:stretch;gap:0;margin:22px 0 0 -4px}
.starters:empty{display:none}
.starters::before{content:"";position:absolute;left:12px;top:18px;bottom:18px;width:1px;background:var(--line2)}
.starters button{position:relative;display:block;width:100%;background:none;border:0;border-radius:10px;padding:8px 12px 8px 30px;font-size:15px;color:var(--ink);min-height:36px}
.starters button::before{content:"";position:absolute;left:9px;top:50%;width:7px;height:7px;margin-top:-3.5px;border-radius:50%;background:var(--ink2);box-shadow:0 0 0 3px var(--page);transition:background-color .12s ease-out,transform .12s ease-out}
.starters button:hover,.starters button:focus-visible{background:var(--raised)}
.starters button:hover::before,.starters button:focus-visible::before{background:var(--accent);box-shadow:0 0 0 3px var(--raised);transform:scale(1.25)}
@media (prefers-reduced-motion:no-preference){.starters button{animation:ax-rise .3s cubic-bezier(.2,.7,.2,1) backwards}.starters button:nth-child(2){animation-delay:.05s}.starters button:nth-child(3){animation-delay:.1s}.starters button:nth-child(4){animation-delay:.15s}.starters button:nth-child(5){animation-delay:.2s}}
/* the composer: one box, one row of controls. It measures itself, so a chat squeezed by side panels fits the way a phone does */
.composer{container-type:inline-size}
.composer-wrap{flex:none;padding:0 28px 14px}
.composer-wrap>.composer,.composer{position:relative;align-self:center;width:100%;max-width:var(--measure-chat);margin:0 auto;padding:11px 10px 7px 14px;background:var(--field);border:1px solid var(--line2);border-radius:18px;box-shadow:0 1px 2px var(--tint-shadow),0 8px 28px var(--tint-shadow)}
.composer:focus-within{border-color:color-mix(in srgb,var(--primary) 60%,var(--line2));box-shadow:0 0 0 3px var(--soft),0 8px 28px var(--tint-shadow)}
.composer textarea,.composer textarea:focus{width:100%;min-height:24px;max-height:260px;padding:0;background:none;border:0;border-radius:0;outline:0;box-shadow:none;font-size:15px;line-height:1.5;color:var(--ink)}
.composer textarea.shellmode{font-family:var(--font-code);font-size:14px;color:var(--ink)}
.composer .ghost{padding:0;border:0;font-size:15px;line-height:1.5;color:var(--ink2)}
.composer .box{position:relative}
.composer #cstatus{display:none;margin:0 0 4px;font-size:12px;color:var(--warn)}
.composer #cstatus.on{display:block}
.composer>.cstatus-line{display:none}
.cbar{display:flex;flex-wrap:wrap;align-items:center;gap:2px;margin:6px 0 0 -6px;font-size:13px;color:var(--ink2)}
.cbar .sp{flex:1 1 8px}
.cbar .btn{padding:4px 12px;border-radius:10px;min-height:30px}
.cbar #send-btn{border-radius:10px;margin-left:4px}
.cplus,.cmic{width:30px;height:30px;border-radius:9px}
.cplus:hover,.cmic:hover{background:var(--raised)}
.cfoot{display:flex;align-items:center;gap:0;width:auto;max-width:none;padding:0;font-size:13px;min-width:0}
.cfoot>span[style]{display:none}
.fw{padding:3px 7px;border-radius:8px;font-size:13px;color:var(--ink2);max-width:170px;min-height:28px}
.fw:hover{background:var(--raised);color:var(--ink);text-decoration:none}
.fw-model{color:var(--ink2)}
.fw-mode.mode-default{color:var(--ink2)}.fw-mode.mode-acceptEdits{color:var(--ink2)}
.ring{width:auto;height:auto;min-width:0;padding:0 5px;font-size:13px;color:var(--ink2);font-variant-numeric:tabular-nums;cursor:help}.ring.warn{color:var(--warn)}.ring.bad{color:var(--bad)}
.cfoot .helptip{margin:0 2px;opacity:0;width:16px}
.composer:hover .cfoot .helptip,.cfoot .helptip:focus-visible{opacity:.75}
.chat-head,.chat-head.slim{display:inline-flex;align-items:center;gap:6px;min-height:0;padding:0;margin:0 2px 0 0;border:0;flex:none;flex-wrap:nowrap;overflow:visible;justify-content:flex-end;background:none}
.chat-head.idle{display:none}
.chat-head>.sp{display:none}
.chat-head .pill{background:none;padding:0 4px;font-size:12px;color:var(--ink2)}
.chat-head .btn{padding:4px 12px;min-height:30px;background:none;font-size:13px}
.chat-head .stop-btn{border:1px solid var(--line2);color:var(--ink)}
.chat-head .stop-btn:hover{border-color:color-mix(in srgb,var(--bad) 60%,transparent);color:var(--bad);background:color-mix(in srgb,var(--bad) 8%,transparent)}
.term-note{font-size:12px;color:var(--ink2);padding:0 4px;white-space:nowrap}
.cf-fc{width:100%;margin:6px auto 0;padding:0 14px;font-size:12px;max-width:var(--measure-chat)}
.chip{background:var(--raised);border:0;border-radius:9px;font-size:13px}
.chips{margin-bottom:6px}.chips:empty{display:none}
.pop{left:-14px;right:-10px;background:var(--float);border:1px solid var(--line2);border-radius:14px;box-shadow:var(--shadow);padding:4px;bottom:calc(100% + 12px)}
.pop .it{border-radius:9px}.pop .it[aria-selected=true]{background:var(--raised)}
.pop .it b{font:500 13px var(--font-code)}
.pop .grp{background:var(--float);font-size:12px}
.dropveil{background:color-mix(in srgb,var(--page) 82%,transparent);border:0;box-shadow:inset 0 0 0 2px var(--primary);border-radius:16px}
.dropveil b{font-size:17px;font-weight:500;background:var(--float);border:0;box-shadow:var(--shadow);border-radius:12px}

/* ── every other page: one column, plain hierarchy ── */
.pane{padding:34px 40px 64px}
.pane:not(.wide)>*{max-width:1060px;margin-left:auto;margin-right:auto}
.pane.on{animation:ax-fade .14s ease-out}
.head{align-items:flex-end;margin-bottom:24px;gap:12px 28px}
.head h1,.pane>h1{font:600 28px/1.2 var(--font-ui);letter-spacing:-.018em;color:var(--ink);display:flex;align-items:center}
.head .sub{margin-top:6px;max-width:64ch;color:var(--ink2)}
h2{font:600 15px/1.35 var(--font-ui);letter-spacing:0;color:var(--ink);margin:0 0 10px}
h3{font-weight:600}
.page-hint{padding:0 0 12px;margin-bottom:0;background:none;border:0;font-size:13px;color:var(--ink2)}
.pane.wide>.page-hint{padding:10px 16px;border-bottom:1px solid var(--line)}
.tabs{position:relative;flex-wrap:nowrap;gap:18px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--line);margin-bottom:20px}
.tabs::-webkit-scrollbar{display:none}
.tabs button{flex:none;padding:8px 0;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;font-size:14px;color:var(--ink2);background:none;border-radius:0;white-space:nowrap}
.tabs button:hover{color:var(--ink)}
.tabs button[aria-selected=true]{color:var(--ink);font-weight:500;border-bottom-color:var(--primary)}
.tabs button:focus-visible{outline-offset:-2px}
.filters{gap:8px;margin-bottom:14px}
.card{background:var(--raised);border:0;border-radius:12px;padding:14px 16px;box-shadow:none;margin-bottom:10px}
.card h3{font-size:14px;margin:0 0 6px}
.card input[type=text],.card input[type=search],.card select,.card textarea{background:var(--page)}
.card>summary,details.card>summary{padding:0;font-size:14px;font-weight:500;color:var(--ink)}
details.card[open]>summary{margin-bottom:8px}
details.card>*:not(summary){margin-left:0;margin-right:0}
details.adv{background:none;border:0;padding:0;border-radius:0;margin:14px 0}
details.adv>summary{padding:4px 0;color:var(--ink2);font-size:13px}
details.adv .card{background:var(--raised)}
details>summary::before{opacity:.7}
.grid3{gap:10px}
table{font-size:13px}
th{font-weight:500;font-size:12px;color:var(--ink2);padding:6px 12px 6px 0}
td{padding:7px 12px 7px 0;border-bottom:1px solid var(--line)}
th,td{border-color:var(--line)!important}
td.r,th.r,.cc-table .r{font-family:var(--font-ui);font-variant-numeric:tabular-nums}
tbody tr:hover{background:none}
.tw{border-radius:0}
.mg-th{font-size:12px;font-weight:500}
.empty{padding:40px 0;color:var(--ink2);font-size:14px;max-width:56ch}
.empty strong{font-size:17px;font-weight:600;letter-spacing:-.005em;margin-bottom:6px}
.hit{display:flex;flex-direction:column;padding:8px 10px;margin:0 -10px;border-radius:9px;text-decoration:none;color:var(--ink)}
.hit:hover{background:var(--raised)}.hit span{font-size:13px}
/* commands */
.cmd-search{width:100%}
.cmd-grid{grid-template-columns:minmax(260px,340px) minmax(0,1fr);gap:20px}
.cmd-list{border:0;background:none;border-radius:0;padding:0;max-height:calc(100vh - 250px)}
.cmd-list .cmd-cat>summary.cat{padding:7px 8px;font-size:13px;color:var(--ink);font-weight:500;background:none;border-radius:8px;position:static}
.cmd-list .cmd-cat>summary.cat:hover{background:var(--raised)}
.cmd-list button.c{margin:0;width:100%;border-radius:8px;padding:6px 8px}
.cmd-list button.c:hover,.cmd-list button.c.sel{background:var(--raised)}
.cmd-list .c b{font:500 13px var(--font-code)}
.cmd-panel{border:0;background:var(--raised);border-radius:14px;padding:16px 18px;min-height:220px}
.cmd-panel>p.muted:only-child{margin:0}
.cmd-out{background:var(--code-bg);border-radius:10px;font-family:var(--font-code);font-size:var(--size-code)}
.cmd-panel .cmd-out{background:var(--page)}
.cc .cmd-out{background:none;padding:6px 0;margin:2px 0}
/* command output arrives with the terminal's colours inline, chosen for a dark ground: on a light sheet they are darkened to stay readable */
:root[data-theme=light] .cmd-out span[style*="color:"]{filter:brightness(.52) saturate(1.5)}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]) .cmd-out span[style*="color:"]{filter:brightness(.52) saturate(1.5)}}
.cc{background:var(--page);border:0;box-shadow:0 0 0 1px var(--line);border-radius:14px}
.cc-top{border-color:var(--line)}
.cc-tt h3{font-size:15px;font-weight:600}
.cmd-panel>.cc{background:none;box-shadow:none}
.cc-said span{background:var(--you);font-family:var(--font-code);font-size:13px}
.cc-bt{height:8px;background:var(--raised2)}
.cc-tip{background:var(--raised);border-color:var(--line)}
details.cmd-quick>summary{padding:6px 0;font-size:14px;color:var(--ink)}
/* sessions */
.mg-row{align-items:center;padding:8px 8px;margin:0 -8px;border:0;border-radius:10px}
.mg-row:hover,.mg-row.sel{background:var(--raised)}
.mg-head{border-color:var(--line);padding:4px 0 6px;color:var(--ink2)}
.mg-group{letter-spacing:0;text-transform:none;font-size:13px;font-weight:600;color:var(--ink);padding:18px 0 4px}
.mg-main .ti{font-size:14px;font-weight:500}.mg-main .meta{font-size:12px}
.mg-star{color:var(--accent)}
.mg-selbar{background:var(--float);border:0;box-shadow:0 0 0 1px var(--line2)}
.mg-trow{border-color:var(--line)}
.mg-toasts{left:50%;top:56px;bottom:auto;transform:translateX(-50%);align-items:center}
.mg-toast{background:var(--ink);color:var(--page);border:0;border-radius:12px;box-shadow:var(--shadow);padding:8px 14px;font-size:14px;animation:ax-rise .2s cubic-bezier(.2,.7,.2,1)}
.mg-toast .btn,.mg-toast .mini{color:var(--page);background:color-mix(in srgb,var(--page) 16%,transparent)}
.mg-toast.bad{background:var(--bad);color:var(--on-bad)}
.mg-pop{background:var(--float);border-color:var(--line2);box-shadow:var(--shadow);border-radius:12px}
.mg-pop button:hover,.mg-pop button:focus{background:var(--raised)}
/* memory */
details.tool.mg-mem,details.mg-mem{margin:0 -8px;border-radius:10px;padding:0 8px;background:none}
details.mg-mem>summary{padding:8px 0;font-size:14px}
details.mg-mem>summary .tn{font:500 14px var(--font-ui);flex:0 1 auto}
details.mg-mem>summary .ts{font-size:12px}
details.mg-mem[open]{background:var(--raised);padding-bottom:12px}
.mg-memadd{margin-bottom:6px}
.mg-memadd textarea{width:100%}
/* extensions: one row per item, its actions at the end */
.xrows{margin-top:6px;border-top:1px solid var(--line)}
.xb{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:18px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line)}
.xb-t{grid-column:1;font-size:14px;overflow-wrap:anywhere}.xb-t b{font-weight:500}
.xb-d{grid-column:1;font-size:13px;line-height:1.45;word-break:break-word;white-space:pre-line}
.xb>.formrow{grid-column:2;grid-row:1/span 2;justify-content:flex-end}
.xb .mini{padding:2px 8px}
@media (max-width:720px){.xb{grid-template-columns:minmax(0,1fr)}.xb>.formrow{grid-column:1;grid-row:auto;justify-content:flex-start;margin-top:4px!important}}
/* extensions: the marketplace rows */
.pane .row{border-color:var(--line)!important}
.pane .row{grid-template-columns:none}
/* settings */
.ap-group>h2{margin:0 0 8px}
.ap-card{border:0;background:var(--raised);border-radius:14px;padding:0 16px}
.ap-row{border-color:var(--line)}
.ap-lab b{font-weight:500;font-size:14px}.ap-lab span{font-size:13px}
.ap-preview,.ap-code{border-color:var(--line)}
.ap-preview{background:var(--page)}
.ap-ctl .seg{background:var(--raised2)}
details.provider{background:var(--raised);border:0;border-radius:12px;margin:0 0 8px;padding:0}
details.provider>summary{padding:10px 14px;border-radius:12px}
details.provider[open]{padding-bottom:14px}
details.provider>*:not(summary){margin-left:14px;margin-right:14px}
details.provider input[type=text],details.provider input[type=password]{background:var(--page);border-color:var(--line2)}
.gbadge{background:var(--raised2);border:0;border-radius:5px;padding:0 6px;font:500 12px/1.6 var(--font-ui);color:var(--ok)}
/* files and the viewer */
.ftoolbar{min-height:46px;padding:7px 14px;border-color:var(--line);gap:6px}
.ftoolbar .p{font:500 13px var(--font-code);color:var(--ink)}
.ftoolbar .btn,.ftoolbar a.btn{background:none;font-size:13px;padding:3px 9px;font-weight:400;color:var(--ink2)}
.ftoolbar .btn:hover,.ftoolbar a.btn:hover{background:var(--raised);color:var(--ink)}
.ftoolbar .btn.primary{background:var(--primary);color:var(--on-primary);font-weight:600}
.code,.editor{background:var(--page);font-family:var(--font-code);font-size:var(--size-code);line-height:1.55}
.code .no{color:var(--ink2);opacity:.65}
.pview .pagebar{border-color:var(--line);font-size:13px}
.fcard{background:var(--raised);border:0;border-radius:14px}
.csv{font-family:var(--font-code)}.csv th,.csv td{border-color:var(--line)!important}
.csv th{background:var(--raised)}
/* side panels */
.wp-head{height:44px;padding:0 6px 0 14px;border-color:var(--line)}
.wp-title{font-weight:600;font-size:14px;letter-spacing:-.003em}
.wpb{width:28px;height:28px;border-radius:8px}.wpb:hover{background:var(--raised)}
.fp-ctl{padding:8px 10px 0;gap:2px 2px}
.fp-ctl .btn{background:none;padding:2px 8px;font-size:12px;font-weight:400;color:var(--ink2);min-height:24px}
.fp-ctl .btn:hover{background:var(--raised);color:var(--ink)}
.fp-ign{margin-left:auto;gap:2px;font-size:12px;color:var(--ink2);cursor:pointer}
.fp-ign input{width:14px;height:14px;margin:0 3px 0 0}
.fp-find{margin:6px 10px 8px;height:32px;padding:0 10px;border-radius:9px}
.fp-q{font-size:13px}
.vlist{padding:0 2px}
.node{height:26px;margin:0 6px;width:calc(100% - 12px);padding-block:0;border-radius:7px;font-size:13px;color:var(--ink)}
.vwin .node{height:26px}
.node:hover{background:var(--raised)}
.node.sel{background:var(--raised);box-shadow:none;font-weight:500}.node.msel{box-shadow:inset 0 0 0 1px var(--primary)}
.node .ar{color:var(--ink2)}
.node .g,.gdot{font-size:12px}
.wt-empty b{overflow-wrap:anywhere}
.wt-empty{padding:22px 18px;font-size:14px}.wt-empty strong{font-size:15px;font-weight:600}
.wtab{border-radius:8px}.wtl{font-size:13px}
.tscreen{background:var(--code-bg);border-radius:0;padding:8px 10px;font-family:var(--font-code)}
.tscreen:focus{box-shadow:none}
.ch-file{background:var(--page);border-color:var(--line);font-size:13px}
.ch-file:hover{background:var(--raised)}.ch-file.stuck{background:var(--raised)}
.ch-file .ftm{display:none}
.ch-file.new .ch-name,.ch-file.mod .ch-name{color:var(--ink)}
.ch-file.new::before,.ch-file.mod::before,.ch-file.del::before{content:"";flex:none;width:6px;height:6px;border-radius:50%;order:-1;background:var(--warn)}
.ch-file.new::before{background:var(--ok)}.ch-file.del::before{background:var(--bad)}
.ch-st{border:0;background:var(--raised2);color:var(--ink2);border-radius:5px;font-size:12px}
.ch-chip{border:0;background:var(--raised);font-size:12px;border-radius:5px}
.ch-line{font-family:var(--font-code)}
.ch-line.add{box-shadow:none}.ch-line.del{box-shadow:none}
.ch-note{font-size:14px;padding:18px}
.ch-q{margin:8px 10px 0}
.cp-sec{border-color:var(--line)}
.cp-sec>summary{font-size:13px;font-weight:500;padding:9px 14px;gap:6px}
.cp-sec>summary::before,.cp-sec[open]>summary::before{content:"";font-family:var(--font-ui)}
.cp-line{font-family:var(--font-code)}
.cp-line.add,.cp-line.del{box-shadow:none}
.br-bar{padding:8px;border-color:var(--line);gap:2px}
.br-url{font-family:var(--font-code);font-size:13px;padding:5px 12px;border-radius:9px}
.br-chip{background:var(--raised);border:0;border-radius:8px;font:13px var(--font-code)}
.br-stage{background:var(--page)}
.sh-out{background:var(--page);font-family:var(--font-code)}
.sh-in{border-color:var(--line);padding:9px 14px}
.sh-cmd{color:var(--ink);font-family:var(--font-code)}
.sh-job pre{font-family:var(--font-code)}
.refused{border:0;background:color-mix(in srgb,var(--bad) 10%,transparent);border-radius:8px}
/* the Progress panel */
#rpbody{padding:2px 0 8px}
.rs{padding:14px 16px 12px}
.rs+.rs{border-top:1px solid var(--line)}
.rh{margin:0 0 8px;font:600 13px/1.4 var(--font-ui);color:var(--ink);letter-spacing:0}
.rh .plink,.rh-note{font-size:12px;font-weight:400;color:var(--ink2)}
.rh .plink:hover{color:var(--ink)}
.rrow{font-size:13px;padding:4px 0;gap:8px;min-height:26px}
.rrow .ico{width:15px;height:15px}
button.rrow:hover,a.rrow:hover{background:var(--raised);box-shadow:0 0 0 4px var(--raised)}
.rr{font-size:12px}.rp{font-size:12px}.rnote{font-size:13px;line-height:1.5}
.cx-head{font-size:13px;margin-bottom:6px}.cx-head b{font-weight:500}
.cx-bar{height:8px;gap:2px;background:var(--raised2);border-radius:4px;margin-bottom:10px}
.cx-bar i{min-width:3px}.cx-bar i:first-child{border-radius:4px 0 0 4px}
.cx-row{grid-template-columns:10px minmax(0,1fr) auto 46px;font-size:13px;padding:2px 0;min-height:24px}
.cx-row .sq{width:8px;height:8px;border-radius:2px}
.cx-row .cp{color:var(--ink2);font-size:12px}
.cx-row.can .cl{text-decoration:none}
.cx-n{font-size:12px}
.cx-list{border-color:var(--line);margin-left:3px}
.kvr{font-size:13px;padding:2px 0}
.steps{height:24px;gap:2px}.steps i{background:var(--primary);opacity:1;border-radius:2px 2px 0 0;max-width:8px}
.bar,.gauge,.bar.thin{background:var(--raised2);border-radius:3px}
.bar>i{background:var(--primary);border-radius:3px}
.bar.thin{height:4px}
.lr{font-size:12px}
.cn-row{font-size:13px;padding:4px 0}
.badge-i{background:var(--raised);font-size:12px;border-radius:6px}
.cn-tool{font-family:var(--font-code);font-size:12px}
.pr-btn{padding:3px 10px;font-size:12px}
.rfoot{border-top:1px solid var(--line);padding:10px 16px 2px;font-size:12px;margin-top:0}
.rfid{font:12px var(--font-ui)}
.todo{font-size:13px}.todo.now{font-weight:500}
/* floating: menus, dialogs, palette, toasts */
.wbmenu,.ctxmenu,.helppop{background:var(--float);border:0;box-shadow:0 0 0 1px var(--line2),var(--shadow);border-radius:13px;padding:5px}
.wbmenu{animation:ax-in .12s ease-out}
.wbmenu button{border-radius:8px;padding:5px 9px;font-size:13px;min-height:28px}
.wbmenu button:hover:not(:disabled),.wbmenu button:focus-visible{background:var(--raised);outline:0}
.wbmenu .mh{font-size:12px;letter-spacing:0;padding:6px 9px 3px}
.wbmenu .mhint{font-size:12px}
.wbmenu hr,.ctxmenu hr,.tab-pop hr,.mg-pop hr{border-color:var(--line)}
.wbmenu button.on .mt::before{color:var(--primary)}
.wbmenu button:disabled{opacity:.6}
.ctxmenu button{border-radius:8px}.ctxmenu button:hover,.ctxmenu button:focus{background:var(--raised)}
.helppop{padding:12px 14px;font-size:13px;line-height:1.5}
.scrim{background:var(--scrim);backdrop-filter:none;animation:ax-fade .14s ease-out}
.modal{background:var(--float);border:0;border-radius:20px;box-shadow:0 0 0 1px var(--line),var(--shadow);padding:22px 24px;animation:ax-rise .2s cubic-bezier(.2,.7,.2,1)}
.modal h3{font:600 17px/1.3 var(--font-ui);letter-spacing:-.005em;margin:0 0 8px}
.modal .acts{margin-top:16px}
.modal input[type=text],.modal input[type=search],.modal select,.modal textarea{background:var(--field)}
.modal pre{background:var(--code-bg);font-size:12px}
.palette{background:var(--float);border:0;border-radius:18px;box-shadow:0 0 0 1px var(--line),var(--shadow);padding:0;animation:ax-rise .16s cubic-bezier(.2,.7,.2,1)}
.palette input[type=text],.palette input[type=text]:focus{padding:15px 18px;font-size:16px;background:none;border:0;border-bottom:1px solid var(--line);border-radius:0;box-shadow:none}
.palette .list{padding:6px}
.palette .it{border-radius:10px;padding:7px 12px}
.palette .it[aria-selected=true]{background:var(--raised)}
.palette .it b{font:500 14px var(--font-ui);min-width:190px}
.palette .it span{font-size:13px;color:var(--ink2)}
.toast{top:56px;bottom:auto!important;background:var(--ink);color:var(--page);border:0;border-radius:12px;box-shadow:var(--shadow);padding:8px 14px;font-size:14px;animation:ax-toast .2s cubic-bezier(.2,.7,.2,1)}
@keyframes ax-toast{from{opacity:0;transform:translate(-50%,8px)}}
.welcome{width:min(620px,100%)}
.welcome h2{font:500 28px/1.2 var(--font-ui);letter-spacing:-.018em;margin:0 0 8px}
.welcome .wl-top{font-size:13px;margin-bottom:14px}
.welcome .wl-group{font-size:14px;margin:18px 0 2px}
.welcome .wl-picks{gap:6px;margin-top:8px}
.welcome .wl-picks button{background:var(--raised);border:0;border-radius:11px;padding:9px 12px;font-size:14px;font-weight:500}
.welcome .wl-picks button span{font-weight:400;font-size:12px;color:var(--ink2)}
.welcome .wl-picks button:hover,.welcome .wl-picks button:focus-visible{background:var(--raised2)}
.welcome .wl-path{background:var(--raised);border-radius:9px;font-family:var(--font-code);font-size:13px}
.authlost h1{font:600 28px/1.2 var(--font-ui);letter-spacing:-.018em}
.boot{color:var(--ink2);text-align:center}
.tk{background:var(--raised);border:0;border-radius:12px}
.tk .st{border:0;background:var(--raised2);border-radius:5px;font-size:12px}
.tkform input[type=text],.tkform select,.tkform textarea{background:var(--field)}
.cf-col{background:var(--raised);border:0;border-radius:14px}.cf-col header,.cf-col footer{border-color:var(--line)}
.cf-dlg .cf-ro{border:0;background:color-mix(in srgb,var(--ok) 10%,transparent);border-radius:9px;font-size:13px}
.pl-list{border-color:var(--line)}.pl-item{border-color:var(--line)}
.pl-main{border-radius:9px}
.cx-find{background:var(--float);border:0;box-shadow:0 0 0 1px var(--line2),var(--shadow);border-radius:12px}
.cx-flash{outline-color:var(--accent)}
.clamped{-webkit-mask-image:linear-gradient(#000 76%,transparent);mask-image:linear-gradient(#000 76%,transparent)}
.foldbtn{background:none;border:0;padding:2px 0;color:var(--link);font-size:13px}
.earlier{margin:0 auto 20px}

/* ── data: the Usage page, Insights and every chart ── */
.u-hero{display:grid;grid-template-columns:minmax(190px,auto) minmax(0,1fr);gap:18px 36px;align-items:end;margin:0 0 26px}
.u-num{font:600 52px/1.05 var(--font-ui);letter-spacing:-.03em;color:var(--ink);white-space:nowrap}
.u-cap{margin-top:8px;font-size:14px;color:var(--ink)}.u-cap.dim{margin-top:0;color:var(--ink2);font-size:13px}
.u-spark{min-width:0}
.strip,.vz-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:0 0 30px}
.vz-stats{border-top:0;margin:-8px 0 22px}
.strip:has(>:nth-child(6):last-child){grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.kpi,.kpi:first-child,.vz-stat{background:none;border:0;border-radius:0;padding:13px 16px 13px 0;min-width:0}
.kpi .l,.vz-stat .l{font-size:12px;color:var(--ink2)}
.kpi .v,.vz-stat .v{font:600 22px/1.25 var(--font-ui);letter-spacing:-.012em;margin:2px 0;color:var(--ink);font-variant-numeric:normal;white-space:normal}
.kpi .h,.vz-stat .h{font-size:12px;color:var(--ink2);white-space:normal}
.kpi .vz-meter{margin:4px 0 5px;max-width:120px}
.vz-stat.wide{display:flex;align-items:center;grid-column:span 2}
.vz-stat.wide .vz-meter{width:100%;max-width:280px}
.vz{position:relative;padding-bottom:0;border-radius:6px}
.vz-plot{position:relative;height:160px;margin-left:44px}
.u-spark .vz-plot{height:120px}
.vz-grid{position:absolute;inset:0;pointer-events:none}
.vz-gl{position:absolute;left:0;right:0;height:1px;background:var(--grid)}
.vz-gl.base{background:var(--ink2);opacity:.45}
.vz-gl span{position:absolute;right:100%;margin-right:8px;top:-8px;font-size:12px;line-height:16px;color:var(--ink2);white-space:nowrap;font-variant-numeric:tabular-nums}
.vz-bars{position:absolute;inset:0;display:flex;align-items:flex-end;gap:0}
.vz-slot{flex:1 1 0;min-width:0;height:100%;display:flex;align-items:flex-end;justify-content:center;padding:0 1px;cursor:default}
.vz-slot.on{background:color-mix(in srgb,var(--ink) 6%,transparent);border-radius:4px 4px 0 0}
.vz-stack{display:flex;flex-direction:column;gap:2px;width:100%;max-width:24px;min-height:2px;border-radius:4px 4px 0 0;overflow:hidden;transform-origin:bottom;animation:vz-grow .32s cubic-bezier(.2,.7,.2,1) backwards}
@keyframes vz-grow{from{transform:scaleY(0)}}
.vz-stack i{display:block;min-height:1px}
.vz-x{display:flex;justify-content:space-between;margin:5px 0 0 44px;font-size:12px;color:var(--ink2)}
.vz-tip{position:absolute;bottom:calc(100% - 26px);z-index:7;min-width:120px;max-width:240px;padding:7px 11px;background:var(--float);border-radius:10px;box-shadow:0 0 0 1px var(--line2),var(--shadow);font-size:12px;pointer-events:none;white-space:nowrap}
.vz-tip b{display:block;font-size:14px;font-weight:600;color:var(--ink)}
.vz-tipd{display:block;color:var(--ink2)}
.vz-tipr{display:flex;align-items:center;gap:6px;margin-top:2px;color:var(--ink2)}
.vz-tipr i{width:10px;height:2px;border-radius:1px;flex:none}
.vz-tipr em{margin-left:auto;padding-left:12px;font-style:normal;color:var(--ink);font-variant-numeric:tabular-nums}
.vz-legend{display:flex;flex-wrap:wrap;gap:3px 14px;list-style:none;margin:8px 0 10px;padding:0;font-size:12px;color:var(--ink2)}
.vz-legend li{display:inline-flex;align-items:center;gap:6px;min-width:0}
.vz-legend i{width:9px;height:9px;border-radius:2px;flex:none}
.vz-legend em{font-style:normal;color:var(--ink);font-variant-numeric:tabular-nums}
.vz-rank{display:grid;gap:9px}
.vz-r{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;font-size:13px;align-items:baseline}
.vz-rn{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.vz-rv{color:var(--ink2);font-variant-numeric:tabular-nums;white-space:pre}
.vz-rb{grid-column:1/-1;height:8px;display:block}
.vz-rb i{display:block;height:100%;background:var(--primary);border-radius:0 4px 4px 0}
.vz-r.dim .vz-rb i{background:var(--ink2);opacity:.45}
.vz-r.dim .vz-rn{color:var(--ink2)}
.vz-rank{margin:6px 0 20px}.chart .vz-rank{margin-top:2px}
.vz-seg{display:flex;gap:2px;height:8px;border-radius:4px;overflow:hidden;margin:8px 0 2px}
.vz-seg i{display:block;min-width:2px}
.vz-seg i.rest{background:var(--raised2)}
.vz-seg.tall{height:28px;border-radius:5px;margin-bottom:4px}
.vz-seg.tall i{display:flex;align-items:center;padding:0 7px;font-size:12px;font-weight:500;font-style:normal;color:#fff;white-space:nowrap;overflow:hidden;border-radius:0}
.vz-whole{margin-bottom:18px}
.vz-whole .vz-legend{margin:6px 0 0}
.vz-meter{height:6px;background:color-mix(in srgb,var(--primary) 18%,var(--raised));border-radius:3px;overflow:hidden}
.vz-meter i{display:block;height:100%;background:var(--primary);border-radius:3px}
.vz-meter.warn i{background:var(--warn)}
td.vz-cell{width:24%;min-width:90px;vertical-align:middle}
.vz-track{height:6px;display:block;width:100%}
.vz-track i{display:block;height:100%;background:var(--primary);border-radius:0 3px 3px 0}
.vz-track i.tool{background:var(--ink2);opacity:.6}
.cols,.cols.eq{gap:36px;margin-bottom:30px;grid-template-columns:minmax(0,3fr) minmax(0,2fr)}
.chart h2{display:flex;align-items:center;justify-content:space-between}
.chart h2 .mg-chartbtn{opacity:0;margin-left:10px}
.chart:hover h2 .mg-chartbtn,.chart h2 .mg-chartbtn:focus-visible{opacity:1}
@media (hover:none){.chart h2 .mg-chartbtn{opacity:1}}
.mg-chartbar{font-size:13px;justify-content:flex-start;gap:6px 10px;padding-top:0;margin-bottom:16px}
.mg-chartbar>.muted{flex:1 1 auto;font-size:13px}
.mg-chartbar .btn{background:none;padding:2px 8px;font-size:12px;font-weight:400;color:var(--ink2);min-height:24px}
.mg-chartbar .btn:hover{background:var(--raised);color:var(--ink)}
.panel{margin-bottom:30px}
.mg-sech{justify-content:space-between}
.mg-sech .mini{opacity:.75}
.mg-brow{border-color:var(--line)}.mg-bn{font-size:13px}
.mg-custom{margin:-14px 0 18px;font-size:13px}
/* the project overview: facts in rows */
details.ov{background:none;border:0;border-radius:0;margin:0 0 28px}
details.ov>summary{padding:4px 0 10px;font:600 15px/1.35 var(--font-ui);color:var(--ink);gap:8px}
details.ov>*:not(summary){margin:0}
.ov-body{padding:0}
.ov-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));gap:16px 32px;font-size:14px;padding:14px 0 18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.ov-f{min-width:0}
.ov-grid dt{font-size:12px;color:var(--ink2);margin:0 0 2px}
.ov-grid dd{min-width:0;margin:0;line-height:1.45}
.ov-grid ul{margin:2px 0 0}
.ov-grid li{font-size:13px;padding:2px 0}
.ov-grid li code{background:none;padding:0;color:var(--ink2);font:12px var(--font-code)}
.ov-grid .vz-whole{margin:2px 0 0}
.linkbtn{color:var(--link);text-underline-offset:2px}
.linkbtn:hover{text-decoration:underline}

/* ── small windows ── */
@container (max-width:620px){.term-note{display:none}.fw{max-width:110px}}
@container (max-width:440px){.fw{max-width:84px;padding:3px 4px;font-size:12px}.ring{padding:0 3px;font-size:12px}.cplus,.cmic{width:26px}.cbar #send-btn{padding:4px 10px;margin-left:0}.cfoot .helptip{display:none}}
@media (max-width:560px) and (hover:hover){.feed>.entry>.mxa{opacity:0}.feed>.entry:hover>.mxa,.feed>.entry:focus-within>.mxa{opacity:1}}
@media (max-width:900px){
 .top{padding:0 6px 0 12px;gap:0}
 .top .brand{margin-right:4px}
 .top .ctitle{max-width:40vw}
 .ide-body{padding:0}
 .center{border-radius:0;box-shadow:none}
 .side-nav{padding:0 0 env(safe-area-inset-bottom);background:var(--page);border-top:1px solid var(--line);overflow:visible}
 .side-nav a{min-height:48px;padding:8px 0 6px;flex:1 1 0;min-width:0}
 .side-nav a[aria-current=page]::after{right:auto;left:50%;top:-4px;margin:0 0 0 -4px;box-shadow:0 0 0 2px var(--page)}
 .statusbar{display:none}
 .wpanel,.wpanel.has{background:var(--page);gap:0}
 .wp{border-radius:0;box-shadow:none}
 .rpanel,.rwrap>.rpanel{border-radius:16px 0 0 16px}
 .pane{padding:22px 16px 48px}
 .head h1,.pane>h1{font-size:22px}
 .cols,.cols.eq{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:720px){
 .feed{padding:20px 16px 12px}
 .composer-wrap{padding:0 10px 8px}
 .thead{width:calc(100% + 6px);margin-right:-6px}
 .fib{max-width:30%}
 .entry.u .md{max-width:90%}
 .empty-chat h2{font-size:28px}
 .feed>.empty-chat{margin-top:20px}
 .u-hero{grid-template-columns:minmax(0,1fr)}
 .u-num{font-size:48px}
 .strip,.vz-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
 .vz-stat.wide{grid-column:1/-1}
 .cmd-grid{grid-template-columns:minmax(0,1fr)}
 .cmd-list{max-height:none}
 .mg-row{grid-template-columns:22px 22px minmax(0,1fr) 30px}
 .mg-row .num,.mg-row .mg-model{display:none}
 .thead .tmeta{flex-basis:auto;order:0;padding-left:0;margin-top:0}
 .thead{flex-wrap:nowrap}
 .feed>.entry>.mxa{background:none;box-shadow:none;padding:0;margin:4px 0 0 -6px;flex-wrap:nowrap;max-width:100%;overflow-x:auto;scrollbar-width:none}
 .entry.u>.mxa{align-self:flex-end;margin-right:-6px}
 .ap-ctl{justify-content:flex-start}
 .modal{padding:18px 16px;border-radius:16px}
 .tabs{gap:14px}
 .palette .it{flex-wrap:wrap;gap:2px 10px}.palette .it b{min-width:0}
}
@media (max-width:480px){.xrow .tn{flex-basis:56px;width:56px}.xrow .td{min-width:0}.thead .tmeta .mi:nth-child(n+3){display:none}.top .brand>span:first-child{display:inline-flex}.top .brand>span:last-child{display:none}.top .brand{margin-right:0}.side-nav a .lbl{display:none}.side-nav a{min-height:44px;padding:12px 0}.tbi{width:30px;height:30px}.vz-plot{margin-left:38px}.vz-x{margin-left:38px}}
@media (forced-colors:active){.vz-stack i,.vz-seg i,.vz-rb i,.vz-track i,.vz-meter i{background:Highlight!important}.fib i{background:CanvasText}}
@media print{.fib{display:none}}
"""
