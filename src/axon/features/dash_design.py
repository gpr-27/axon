"""The look of the Axon web app. Loaded last, so it restyles every screen without changing how any of them work.

Rounded sheets sit on a slightly darker canvas. Borders give way to spacing and soft surfaces, and motion only
ever answers something the user did.
"""

_DARK = (
    "--canvas:#0A0C0F;--page:#121519;--surface:#121519;--raised:#1C2026;--raised2:#262B33;--grid:#2A3038;"
    "--line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.14);--ink:#ECEFF3;--ink2:#A3ABB7;--muted:#A3ABB7;"
    "--code-bg:#0D0F13;--primary:#3CDDBD;--on-primary:#04211B;--accent:#F27DB5;--info:#7DBBFF;--ok:#8BDD72;"
    "--warn:#F2BE5C;--bad:#FF7B88;--add:#8BDD72;--del:#FF7B88;--add-bg:#12352A;--del-bg:#40202B;"
    "--shadow:0 12px 40px rgba(0,0,0,.45);--soft:rgba(60,221,189,.14)"
)
_LIGHT = (
    "--canvas:#ECEEF1;--page:#FFFFFF;--surface:#FFFFFF;--raised:#F1F3F5;--raised2:#E6E9ED;--grid:#DDE1E6;"
    "--line:rgba(16,20,28,.08);--line2:rgba(16,20,28,.16);--ink:#14171C;--ink2:#555D69;--muted:#555D69;"
    "--code-bg:#F6F7F9;--primary:#00725F;--on-primary:#FFFFFF;--accent:#B5186A;--info:#1259C3;--ok:#26741C;"
    "--warn:#8F5800;--bad:#BE1E36;--add:#26741C;--del:#BE1E36;--add-bg:#DDF4E2;--del-bg:#FBE4E4;"
    "--shadow:0 12px 40px rgba(16,20,28,.12);--soft:rgba(0,114,95,.10)"
)

_VARS = (
    "--font-ui:ui-sans-serif,-apple-system,BlinkMacSystemFont,\"SF Pro Text\",\"Segoe UI Variable\",\"Segoe UI\",system-ui,sans-serif;"
    "--font-response:\"Iowan Old Style\",\"Charter\",\"Source Serif 4\",Georgia,serif;"
    "--font-code:ui-monospace,SFMono-Regular,\"SF Mono\",Menlo,Consolas,monospace;"
    "--size-response:16px;--size-code:13px;--measure-chat:780px"
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
.scrim{background:rgba(5,7,10,.5)}
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
body.dock-open .drawer-scrim{display:block;position:fixed;inset:0;background:rgba(5,7,10,.45)}
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
