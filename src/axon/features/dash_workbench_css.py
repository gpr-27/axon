"""CSS of the workbench: toolbar, side panels, Progress panel sections, menus, composer footer. Appended after DESIGN_CSS."""

WORKBENCH_CSS = r"""
/* ---- workbench ---- */
:root{--c-msg:#4C8DF6;--c-sys:#E0703A;--c-mcp:#33A584;--c-skill:#D49A12;--c-prompt:#8E96A3;--c-minst:#A774D6;--c-agent:#D4588C;--c-mem:#4DAFC4;--c-buf:#5E6572}
:root[data-theme=light]{--c-msg:#2F6FDC;--c-sys:#C85A24;--c-mcp:#1F8A6C;--c-skill:#A87500;--c-prompt:#6A7280;--c-minst:#8650BD;--c-agent:#B83A70;--c-mem:#237F93;--c-buf:#8A919D}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]){--c-msg:#2F6FDC;--c-sys:#C85A24;--c-mcp:#1F8A6C;--c-skill:#A87500;--c-prompt:#6A7280;--c-minst:#8650BD;--c-agent:#B83A70;--c-mem:#237F93;--c-buf:#8A919D}}
/* top bar */
.top .tbicons{display:flex;align-items:center;gap:2px;margin-left:auto;overflow:visible}
.tbi{width:34px;height:34px;border:0;background:none;border-radius:8px;color:var(--ink2);display:grid;place-items:center;cursor:pointer;padding:0;flex:none}
.tbi:hover{background:var(--raised);color:var(--ink)}
.tbi[aria-pressed=true]{background:var(--raised2);color:var(--ink)}
.tbi .ico{width:18px;height:18px}
.top .ctitle{display:inline-flex;align-items:center;gap:6px;max-width:min(38vw,420px);min-width:0;background:none;border:0;border-radius:8px;padding:5px 9px;color:var(--ink);font-weight:550;font-size:14px;cursor:pointer;flex:0 1 auto}
.top .ctitle:hover:not(:disabled){background:var(--raised)}
.top .ctitle:disabled{cursor:default}
.top .ctitle .ct-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top .ctitle .ico{width:14px;height:14px;color:var(--ink2);flex:none}
.top .sidebtn{flex:none}
/* right area: Progress, then the side panels */
.ide-body>.rwrap{grid-column:6;grid-row:1;display:flex;min-width:0;min-height:0;background:var(--page)}
.rwrap>.rpanel{flex:1 1 0;min-width:0;border-radius:0;border:0;border-left:1px solid var(--line);background:var(--page)}
.rwrap.no-prog>.rpanel{display:none}
.wpanel{display:none;flex:0 0 auto;width:var(--wbw,0);min-width:0;min-height:0}
.wpanel.has{display:flex;border-left:1px solid var(--line)}
.wcol{display:flex;flex-direction:column;min-width:0;min-height:0}
.wcol[hidden]{display:none}
.wcol-narrow{width:var(--wn,380px)}.wcol-wide{width:var(--ww,600px)}
.wpanel .rz-w{flex:0 0 5px;cursor:col-resize}
.rz-h{flex:0 0 5px;height:5px;cursor:row-resize;background:var(--line);margin:-2px 0;position:relative;z-index:2}
.rz-h:hover{background:var(--primary)}
.wp{display:flex;flex-direction:column;min-height:0;min-width:0;background:var(--page);overflow:hidden}
.wp[hidden]{display:none}
.wp+.rz-h+.wp,.wp+.wp{border-top:1px solid var(--line)}
.wp-head{display:flex;align-items:center;gap:2px;padding:0 6px 0 14px;height:44px;flex:none;border-bottom:1px solid var(--line)}
.wp-head>.wp-title{flex:0 1 auto}.wp-tools,.wpb{flex:none}.wp-title{font-weight:550;font-size:13.5px;min-width:0;display:flex;align-items:center;gap:6px;overflow:hidden}
.wp-tools{display:flex;align-items:center;gap:2px}
.wp-body{flex:1;min-height:0;display:flex;flex-direction:column;position:relative;overflow:hidden}
.wp-pad{padding:12px 16px;overflow:auto}
.wpb{width:28px;height:28px;display:grid;place-items:center;background:none;border:0;border-radius:7px;color:var(--ink2);cursor:pointer;padding:0;flex:none}
.wpb:hover{background:var(--raised);color:var(--ink)}
.wpb .ico{width:15px;height:15px}.wpb.sm{width:24px;height:24px}.wpb.sm .ico{width:13px;height:13px}
.wpb.sm{opacity:0}.ch-file:hover .wpb.sm,.ch-file:focus-within .wpb.sm{opacity:1}
@media (hover:none){.wpb.sm{opacity:1}}
.ide-body.wbmax{grid-template-columns:var(--rail,64px) 0 0 0 0 minmax(0,1fr)!important}
.ide-body.wbmax>.lpanel,.ide-body.wbmax>.rz,.ide-body.wbmax>.center{display:none!important}
.ide-body.wbmax .rwrap>.rpanel{display:none}.ide-body.wbmax .wpanel{width:100%;flex:1}.ide-body.wbmax .wcol{flex:1;width:auto}
@media (prefers-reduced-motion:no-preference){.wpanel.has{animation:wbin .12s ease-out}@keyframes wbin{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:none}}}
/* menus */
.wbmenu{position:fixed;z-index:60;width:max-content;min-width:210px;max-width:min(92vw,380px);max-height:72vh;overflow:auto;background:var(--raised);border:1px solid var(--line2);border-radius:10px;padding:5px;box-shadow:0 10px 30px rgb(0 0 0/.35)}
.wbmenu button{display:flex;width:100%;align-items:center;gap:14px;justify-content:space-between;text-align:left;background:none;border:0;border-radius:6px;padding:6px 10px;color:var(--ink);cursor:pointer;font-size:13px}
.wbmenu button:hover:not(:disabled),.wbmenu button:focus-visible{background:var(--raised2)}
.wbmenu button:disabled{color:var(--muted);cursor:default}
.wbmenu button.on .mt::before{content:"\2713\00a0"}
.wbmenu .mhint{color:var(--muted);font-size:12px;white-space:nowrap}
.wbmenu .mh{padding:6px 10px 3px;font-size:11.5px;color:var(--muted);letter-spacing:.02em}
.wbmenu hr{border:0;border-top:1px solid var(--line);margin:5px 4px}
.wbmenu button.danger{color:var(--bad)}
.wbmenu button.mode-default .mt{color:var(--warn)}.wbmenu button.mode-bypass .mt{color:var(--bad)}
.wbconfirm{width:min(480px,100%)}.wbconfirm .search{width:100%}
.pushrow{display:flex;gap:8px;align-items:flex-start;font-size:13px;margin:8px 0}
/* Progress panel */
#rpbody{padding:4px 0 12px;overflow:auto}
.rs{padding:14px 18px 12px;border:0}
.rs+.rs{border-top:1px solid var(--line)}
.rh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:0 0 8px;font-size:12px;font-weight:500;color:var(--muted);letter-spacing:.02em}
.rh .plink,.rh-note{font-size:12px;font-weight:400}.rh-note.warn{color:var(--warn)}
.rrow{display:flex;align-items:center;gap:10px;width:100%;min-width:0;padding:5px 0;background:none;border:0;color:var(--ink);text-align:left;text-decoration:none;font-size:13px;cursor:default}
button.rrow,a.rrow{cursor:pointer;border-radius:6px}
button.rrow:hover,a.rrow:hover{color:var(--ink);background:var(--raised);box-shadow:0 0 0 4px var(--raised)}
.rrow .ico{width:16px;height:16px;color:var(--ink2);flex:none}
.rl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rr{font-size:12.5px;white-space:nowrap;flex:none}.rr.ok{color:var(--ok)}
.rp{font-size:12.5px}.rnote{font-size:12.5px;color:var(--muted);padding:3px 0}
.rrow.static{cursor:default}
.cx-head{display:flex;justify-content:space-between;align-items:baseline;font-size:13.5px;margin-bottom:6px}
.num{font-variant-numeric:tabular-nums;font-family:inherit}
.cx-bar{display:flex;gap:1px;height:4px;border-radius:2px;background:var(--raised2);overflow:hidden;margin-bottom:10px}
.cx-bar i{display:block;height:100%;flex:none;min-width:2px}.cx-bar i.buf{opacity:.55}
.cx-row{display:grid;grid-template-columns:12px minmax(0,1fr) auto 54px;gap:8px;align-items:center;width:100%;padding:3px 0;font-size:13px;color:var(--ink2);background:none;border:0;text-align:left;cursor:help}
.cx-row.can{cursor:pointer}.cx-row.can:hover{color:var(--ink)}
.cx-row .sq{width:10px;height:10px;border-radius:3px;display:block}.cx-row .sq.free{border:1px solid var(--line2)}.cx-row .sq.dim{background:var(--c-buf);opacity:.6}
.cx-row .cl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cx-row.can .cl{text-decoration:underline dotted;text-underline-offset:3px;text-decoration-color:var(--muted)}
.cx-n{color:var(--muted);font-size:12px}
.cx-row .ct,.cx-row .cp{text-align:right}
.cx-row.def{margin-top:6px}
.cx-list{margin:4px 0 6px 20px;border-left:1px solid var(--line);padding-left:10px}
.cx-item{display:flex;gap:8px;align-items:center;padding:2px 0;font-size:12.5px;color:var(--ink2)}
.cx-item .ci-n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.cx-item .ci-t{flex:none;min-width:44px;text-align:right}
.cx-sub{font-size:11.5px;color:var(--muted);padding:2px 0}
.kvr{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:2px 0}
.kvr .num{color:var(--ink)}
.steps{display:flex;align-items:flex-end;gap:2px;height:26px;margin:8px 0 4px}
.steps i{flex:1;max-width:10px;min-width:2px;background:var(--primary);opacity:.8;border-radius:1px}
.bar.thin{height:3px;margin:3px 0}
.lim{margin-top:8px}.lr{font-size:11.5px}
.cn-row{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:13px;cursor:pointer;border-radius:6px}
.cn-row:hover .rl{color:var(--ink)}
.badge-i{width:18px;height:18px;border-radius:5px;background:var(--raised2);color:var(--ink2);font-size:11px;display:grid;place-items:center;flex:none;font-weight:600}
.cn-act{background:none;border:0;color:var(--warn);cursor:pointer;font-size:12.5px;padding:0}.cn-act:hover{text-decoration:underline}
.cn-more{background:none;border:0;color:var(--ink2);cursor:pointer;padding:6px 0 0;font-size:13px}.cn-more:hover{color:var(--ink)}
.cn-body{margin:2px 0 8px 28px}.cn-tools{max-height:180px;overflow:auto;font-size:12.5px;color:var(--ink2)}.cn-tool{padding:1px 0;font-family:ui-monospace,Menlo,monospace;font-size:12px}
.cn-q{width:100%;margin:2px 0 6px;background:var(--raised);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12.5px}
.cn-err{font-size:12px;margin-bottom:4px}
.chrow{justify-content:space-between;padding:0}
.chrow .rrow.in{flex:1;padding:5px 0}
.cadd{color:var(--add)}.cdel{color:var(--del)}
.pr-btn{padding:4px 12px;font-size:13px;flex:none}
.rfoot{display:flex;justify-content:space-between;gap:10px;padding:10px 18px 4px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);margin-top:6px}
.rfoot>span{flex:none;white-space:nowrap}.rfoot .rfid{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rfid{background:none;border:0;padding:0;color:inherit;font:11.5px ui-monospace,Menlo,monospace;cursor:copy;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;text-align:left}
.rfid:hover{color:var(--ink2)}
/* composer footer */
.composer-wrap{display:flex;flex-direction:column;align-items:center}
.composer-wrap>.composer{margin-bottom:2px}
.cfoot{display:flex;align-items:center;gap:14px;width:calc(100% - 48px);max-width:var(--measure-chat,820px);padding:0 6px 14px;font-size:12.5px}
.fw{background:none;border:0;padding:2px 0;color:var(--ink2);cursor:pointer;font-size:12.5px;border-radius:4px;max-width:40vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fw:hover{color:var(--ink);text-decoration:underline;text-underline-offset:3px}
.fw-model{color:var(--ink)}
.fw-mode.mode-default{color:var(--warn)}.fw-mode.mode-bypass{color:var(--bad)}.fw-mode.mode-plan{color:var(--info)}.fw-mode.mode-acceptEdits{color:var(--ink)}
.ring{display:inline-grid;place-items:center;width:18px;height:18px;cursor:help}.ring svg{display:block}
.cplus,.cmic{width:30px;height:30px;display:grid;place-items:center;background:none;border:0;border-radius:50%;color:var(--ink2);cursor:pointer;padding:0;flex:none}
.cplus:hover,.cmic:hover{background:var(--raised);color:var(--ink)}.cmic[aria-pressed=true]{color:var(--bad);background:var(--raised)}
.cplus .ico,.cmic .ico{width:17px;height:17px}.cplus-wrap{display:inline-flex}
.chat-head.idle{display:none}
.chat-head.slim{min-height:0;padding:6px 16px;border-bottom:0;justify-content:flex-end}
/* terminal panel */
.wtabs{display:flex;align-items:center;gap:2px;min-width:0;overflow-x:auto;scrollbar-width:none}
.wtab{display:flex;align-items:center;border-radius:7px;flex:none}
.wtab.on{background:var(--raised)}
.wtl{background:none;border:0;color:var(--ink2);padding:5px 4px 5px 10px;cursor:pointer;font-size:13px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wtab.on .wtl{color:var(--ink)}
.wtx{background:none;border:0;color:var(--muted);cursor:pointer;padding:2px 7px 2px 3px;font-size:14px}.wtx:hover{color:var(--ink)}
.wt-none{font-weight:550;font-size:13.5px}
.wterms{flex:1;min-height:0;position:relative}
.wterm{position:absolute;inset:0;display:flex;flex-direction:column}
.wterm[hidden]{display:none}
.wterm .tscreen{flex:1;border:0;border-radius:0}
.wt-empty{padding:26px 22px;max-width:60ch;font-size:13.5px}.wt-empty strong{display:block;font-size:15px;margin-bottom:6px}
/* files panel */
.fp-bar{display:flex;align-items:center;gap:2px;padding:6px 10px 0}.fp-sp{flex:1}
.fp-find{display:flex;align-items:center;gap:8px;margin:6px 12px 8px;padding:0 10px;height:34px;background:var(--raised);border:1px solid var(--line);border-radius:9px;color:var(--ink2)}
.fp-find:focus-within{border-color:var(--primary)}.fp-find .ico{width:15px;height:15px;flex:none}
.fp-q{flex:1;min-width:0;background:none;border:0;outline:0;font-size:13.5px;color:var(--ink)}
.fp-list{flex:1;overflow:auto;padding:0 6px 10px}
.fp-row{display:flex;align-items:center;gap:4px;height:28px;border-radius:6px;cursor:pointer;font-size:13.5px;color:var(--ink);padding-right:10px;white-space:nowrap}
.fp-row:hover,.fp-row:focus-visible{background:var(--raised)}
.fp-chev{width:16px;flex:none;display:grid;place-items:center;color:var(--muted)}.fp-chev .ico{width:12px;height:12px;transition:none}.fp-chev.open .ico{transform:rotate(90deg)}
.fp-name{overflow:hidden;text-overflow:ellipsis}.fp-dir{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;margin-left:6px;flex:1;direction:rtl;text-align:left}
.fp-row.g-new .fp-name{color:var(--ok)}.fp-row.g-mod .fp-name{color:var(--warn)}.fp-row.g-del .fp-name{color:var(--bad);text-decoration:line-through}
.gdot{width:7px;height:7px;border-radius:50%;margin-left:auto;flex:none}.gdot.g-new{background:var(--ok)}.gdot.g-mod{background:var(--warn)}.gdot.g-del{background:var(--bad)}
.fp-note{padding:10px 12px;font-size:12.5px}
.fp-hit{display:flex;flex-direction:column;padding:5px 12px;text-decoration:none;color:var(--ink);font-size:12.5px;border-radius:6px}.fp-hit:hover{background:var(--raised)}
.fp-hit .muted{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
/* changes panel */
.ch-cmp{white-space:nowrap;display:inline-flex;align-items:center;gap:7px;background:none;border:0;color:var(--ink);font-size:13.5px;font-weight:550;cursor:pointer;padding:4px 6px;border-radius:7px;min-width:0}
.ch-cmp:hover{background:var(--raised)}.ch-cmp .ico{width:13px;height:13px;color:var(--ink2)}.ch-arrow{color:var(--muted)}
.ch-chip{font-size:11.5px;font-weight:400;border:1px solid var(--line2);border-radius:999px;padding:1px 9px;color:var(--ink2);white-space:nowrap}
.ch-q{margin:8px 12px 0;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px}
.ch-note{padding:14px 18px;font-size:13px;color:var(--ink2)}
.ch-wrap{position:relative;flex:1;min-height:0;display:flex;flex-direction:column}
.ch-scroll{flex:1;min-height:0;overflow:auto;position:relative}
.ch-inner{position:relative;width:100%}
.ch-file{position:absolute;left:0;right:0;display:flex;align-items:center;gap:7px;padding:0 6px 0 10px;font-size:13.5px;cursor:pointer;background:var(--page);border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden}
.ch-file:hover{background:var(--raised)}.ch-file.stuck{position:relative;box-shadow:0 1px 0 var(--line2);background:var(--raised)}
.ch-sticky{position:absolute;top:0;left:0;right:0;z-index:3;height:30px}
.ch-file .ftm{font:600 9.5px ui-monospace,Menlo,monospace;text-transform:uppercase;color:var(--ink2);border:1px solid var(--line2);border-radius:4px;padding:0 3px;min-width:22px;text-align:center;flex:none}
.ch-name{overflow:hidden;text-overflow:ellipsis;flex:0 1 auto}.ch-dir{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;flex:0 100 auto;min-width:0}
.ch-file.new .ch-name{color:var(--ok)}.ch-file.mod .ch-name{color:var(--warn)}.ch-file.del .ch-name{color:var(--bad);text-decoration:line-through}
.ch-st{font-size:11px;color:var(--info);border:1px solid var(--info);border-radius:999px;padding:0 6px}
.ch-file .cadd,.ch-file .cdel{font-variant-numeric:tabular-nums;font-size:12.5px;flex:none}
.ch-line{position:absolute;left:0;min-width:100%;width:max-content;display:flex;font:12px/19px ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre;color:var(--ink)}
.ch-line .ln{flex:none;width:56px;text-align:right;padding-right:10px;color:var(--muted);user-select:none}
.ch-line .lm{flex:none;width:14px;color:var(--muted);user-select:none}
.ch-line.add{background:var(--add-bg);box-shadow:inset 3px 0 0 var(--add)}.ch-line.del{background:var(--del-bg);box-shadow:inset 3px 0 0 var(--del)}
.ch-line.add .lm{color:var(--add)}.ch-line.del .lm{color:var(--del)}
.ch-line.hunk{color:var(--info);background:color-mix(in srgb,var(--info) 9%,transparent)}
.ch-line.note{color:var(--muted);font-style:italic;font-family:inherit}
/* browser panel */
.br-bar{display:flex;align-items:center;gap:4px;padding:8px 10px;border-bottom:1px solid var(--line)}
.br-form{flex:1;min-width:0}.br-url{width:100%;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:13px;font-family:ui-monospace,Menlo,monospace;color:var(--ink)}
.br-url:focus{border-color:var(--primary);outline:0}
.br-ports{display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px 0}
.br-chip{background:var(--raised);border:1px solid var(--line);border-radius:999px;padding:2px 10px;font:12.5px ui-monospace,Menlo,monospace;color:var(--ink);cursor:pointer;display:inline-flex;gap:6px}.br-chip:hover{border-color:var(--primary)}
.br-msg{padding:6px 12px;font-size:12px;color:var(--ink2);min-height:0}.br-msg:empty{display:none}
.br-stage{flex:1;min-height:0;position:relative;background:var(--page)}
.br-frame{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff}
.hk{display:flex;flex-direction:column;gap:2px;padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}.hk code{font-size:12px;overflow-wrap:anywhere}
.wp-pad .row{display:flex;gap:10px;align-items:center;padding:4px 0;font-size:13px}.wp-pad .row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* phone: panels are sheets */
@media (max-width:900px){
 .ide-body>.rwrap{display:contents}
 .wpanel,.wpanel.has{position:fixed;inset:0;width:100vw;z-index:31;flex-direction:column;background:var(--page);border:0;animation:none}
 .wpanel{display:none}body.drawer-wb .wpanel.has{display:flex}
 .wcol,.wcol-narrow,.wcol-wide{width:100%;flex:1 1 0}.wpanel .rz-w{display:none}
 .cfoot{width:calc(100% - 20px);gap:10px;padding-bottom:8px}.fw{max-width:34vw}
 .top .ctitle{max-width:34vw}.wbmenu{max-width:94vw}
}
@media (max-width:420px){.tbi{width:30px;height:30px}.top .brand span{display:none}}
/* Files panel: project controls and the shared virtual tree */
.fp-ctl{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:8px 12px 0}.fp-ctl .btn{padding:4px 10px;font-size:12.5px}.fp-ign{font-size:12px;display:flex;gap:4px;align-items:center}
.wp-files .wp-body{display:flex;flex-direction:column;min-height:0}.wp-files .vlist{flex:1;min-height:0}.wp-files .fp-res{flex:1;min-height:0;overflow:auto}
.node .fp-dir{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;max-width:45%}
/* the line Shell inside the Terminal panel (where there is no PTY) */
.wp-terminal .sh-side{height:100%;grid-template-columns:minmax(0,1fr)}.wp-terminal .sh-hist{display:none}.wp-terminal .wterms{display:flex;flex-direction:column}.wp-terminal .wterms>.sh-side{flex:1;min-height:0}
/* the rail labels must fit their column */
.side-nav a .lbl{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;letter-spacing:-.01em}
@media (max-width:720px){.top .ctitle .ct-t{display:none}}
"""
