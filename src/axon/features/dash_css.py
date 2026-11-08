"""App-layout styles of the dashboard (sidebar frame, chat, files, shell, palette). Appended after the base styles."""

APP_LAYOUT_CSS = r"""
html,body{height:100%}
body{overflow:hidden}
#app{height:100%}
.frame{display:grid;grid-template-rows:auto minmax(0,1fr);height:100%}
.top{position:static;display:flex;align-items:center;gap:14px;padding:0 14px;height:48px;border-bottom:1px solid var(--line);background:var(--surface)}
.top .brand{gap:8px}
.tools{display:flex;gap:6px;margin-left:auto}
.ws-chip{display:flex;align-items:center;gap:8px;min-width:0;padding:4px 10px;border:1px solid var(--line);border-radius:6px;text-decoration:none;color:var(--ink);background:var(--page);overflow:hidden}
.ws-chip:hover{border-color:var(--primary)}
.ws-chip b{white-space:nowrap}.ws-path{color:var(--muted);font:12px ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws-git{color:var(--accent);font:12px ui-monospace,Menlo,monospace;white-space:nowrap}
.ws-dot{width:8px;height:8px;border-radius:50%;background:var(--primary);flex:none}
.body{display:grid;grid-template-columns:176px minmax(0,1fr);min-height:0}
.side-nav{display:flex;flex-direction:column;gap:2px;padding:10px 8px;border-right:1px solid var(--line);background:var(--surface);overflow-y:auto}
.side-nav a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;color:var(--ink2);text-decoration:none;border:0}
.side-nav a:hover{background:var(--raised);color:var(--ink)}
.side-nav a[aria-current=page]{background:var(--raised);color:var(--ink);box-shadow:inset 2px 0 0 var(--primary)}
.ico{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:none}
main{max-width:none;margin:0;padding:22px 24px 60px;overflow-y:auto;min-width:0}
main.wide{padding:0;overflow:hidden;display:flex;flex-direction:column}
nav{display:flex}
.kbd{font-family:ui-monospace,Menlo,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:3px;padding:0 5px;font-size:11.5px;color:var(--ink2)}
a.plink{color:var(--info);text-decoration:underline dotted;text-underline-offset:2px}
.foldbtn{display:block;margin:2px 0 6px;background:none;border:1px dashed var(--line);border-radius:4px;color:var(--ink2);padding:1px 10px;cursor:pointer;font-size:12px}
.clamped{max-height:300px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}
/* chat */
.chat{display:grid;grid-template-rows:auto minmax(0,1fr);height:100%;min-height:0}
.chat-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;padding:10px 18px;border-bottom:1px solid var(--line)}
.chat-head h1{font-size:16px;max-width:36ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--ink2);background:var(--page)}
.pill b{color:var(--ink);font-weight:600}
.pill select{border:0;background:transparent;padding:0;color:var(--ink);max-width:150px}
.gauge{width:84px;height:6px;border-radius:3px;background:var(--grid);overflow:hidden;display:inline-block}
.gauge>i{display:block;height:100%;background:var(--primary)}.gauge.warn>i{background:var(--warn)}.gauge.bad>i{background:var(--bad)}
.chat-cols{display:grid;grid-template-columns:minmax(0,1fr) 288px;min-height:0}
.chat-main{display:flex;flex-direction:column;min-height:0;min-width:0}
.feed{flex:1;overflow-y:auto;padding:6px 18px 16px;min-height:0}
.rail{border-left:1px solid var(--line);overflow-y:auto;padding:12px 14px;background:var(--surface)}
.rail h3{font-size:12px;margin:16px 0 6px;color:var(--ink2);font-weight:600;display:flex;justify-content:space-between;align-items:center}
.rail h3:first-child{margin-top:0}
.rail .row{display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:12.5px}
.rail .row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rail a.row{color:inherit;text-decoration:none}.rail a.row:hover .t{text-decoration:underline}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none;align-self:center}
.dot.running{background:var(--primary);animation:pulse 1.2s infinite}.dot.completed,.dot.finished{background:var(--ok)}.dot.error,.dot.cancelled{background:var(--bad)}
@keyframes pulse{50%{opacity:.3}}
.bar{height:5px;background:var(--grid);border-radius:3px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--primary)}
.todo{display:flex;gap:8px;font-size:12.5px;padding:2px 0}.todo.done{color:var(--muted);text-decoration:line-through}.todo.now{color:var(--ink);font-weight:600}
.steprow{font:11.5px ui-monospace,Menlo,monospace;color:var(--muted);padding:2px 0;display:flex;gap:10px;flex-wrap:wrap}
.step-line{display:grid;grid-template-columns:22px 1fr 54px 60px 42px;gap:6px;font:11.5px ui-monospace,Menlo,monospace;align-items:center;padding:2px 0}
.step-line .bar{height:7px}
.miss{color:var(--warn)}
.notice{border:1px solid var(--line);border-left:3px solid var(--info);border-radius:4px;padding:6px 10px;margin:8px 0;font-size:12.5px;color:var(--ink2);background:var(--surface)}
.notice.warn{border-left-color:var(--warn)}.notice.bad{border-left-color:var(--bad)}.notice.ok{border-left-color:var(--ok)}
.notice b{color:var(--ink)}
.turnbar{display:flex;gap:6px;margin:4px 0 6px;align-items:center;color:var(--muted);font-size:12px}
.turnbar button,.mini{background:none;border:1px solid var(--line);border-radius:4px;padding:0 8px;color:var(--ink2);cursor:pointer;font-size:12px}
.mini:hover,.turnbar button:hover{border-color:var(--primary);color:var(--ink)}
.t-compact .steprow,.t-compact .dtl,.t-compact .rawev{display:none}
.t-normal .rawev{display:none}
.t-debug .rawev{display:block}
.rawev{font:11px ui-monospace,Menlo,monospace;color:var(--muted);margin:2px 0}
.rawev summary{font-size:11px}
.trail{border-top:1px dashed var(--line);margin-top:8px;padding-top:6px;display:grid;grid-template-columns:78px 1fr;gap:3px 10px;font-size:12.5px}
.trail dt{color:var(--muted)}.trail dd{margin:0;min-width:0;overflow-wrap:anywhere}
.spin{display:inline-block;width:10px;height:10px;border:2px solid var(--line);border-top-color:var(--primary);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}
@keyframes spin{to{transform:rotate(360deg)}}
.approve{border:1px solid var(--warn);border-radius:6px;padding:12px 14px;margin:10px 0;background:var(--surface)}
.approve h4{margin:0 0 4px;font-size:14px}.approve .why{color:var(--ink2);font-size:12.5px;margin-bottom:6px}
.approve .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.approve.question{border-color:var(--info)}.approve.question label{display:flex;gap:8px;padding:3px 0;cursor:pointer}
/* composer */
.composer{position:static;border-top:1px solid var(--line);padding:10px 18px 12px;background:var(--page);margin:0}
.composer .box{position:relative}
.composer textarea{width:100%;min-height:52px;max-height:260px;resize:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px 12px;line-height:1.45}
.composer textarea.shellmode{border-color:var(--warn);font-family:ui-monospace,Menlo,monospace}
.composer.drag textarea{border-color:var(--primary);box-shadow:0 0 0 3px color-mix(in srgb,var(--primary) 25%,transparent)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.chip{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--line);border-radius:999px;padding:1px 4px 1px 10px;font-size:12px;background:var(--surface)}
.chip.img{border-color:var(--accent)}.chip.txt{border-color:var(--warn)}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;padding:0 6px}
.cbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px;color:var(--ink2);font-size:12.5px}
.cbar .sp{flex:1}
.pop{position:absolute;left:0;right:0;bottom:calc(100% + 4px);background:var(--surface);border:1px solid var(--line);border-radius:8px;max-height:260px;overflow-y:auto;z-index:8;box-shadow:0 8px 24px rgb(0 0 0/.25)}
.pop .it{display:flex;gap:12px;padding:6px 12px;cursor:pointer;font-size:13px;align-items:baseline}
.pop .it[aria-selected=true]{background:var(--raised)}
.pop .it b{font-family:ui-monospace,Menlo,monospace;font-weight:600;min-width:130px}.pop .it span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty-chat{max-width:56ch;margin:8vh auto;color:var(--ink2);text-align:left}
.empty-chat h2{font-size:20px;color:var(--ink);margin-bottom:6px;letter-spacing:0}
.empty-chat .hints{display:grid;gap:4px;margin-top:14px;font-size:13px}
.lock-note{border:1px solid var(--warn);border-radius:6px;padding:10px 14px;margin:10px 18px;background:var(--surface)}
/* files */
.files{display:grid;grid-template-columns:300px minmax(0,1fr);height:100%;min-height:0}
.ftree{border-right:1px solid var(--line);overflow:auto;padding:8px 0;background:var(--surface)}
.fhead{padding:8px 12px 10px;border-bottom:1px solid var(--line)}
.fhead .cur{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fhead .full{color:var(--muted);font:11.5px ui-monospace,Menlo,monospace;overflow-wrap:anywhere;margin:2px 0 8px}
.node{display:flex;align-items:center;gap:6px;padding:2px 10px 2px calc(10px + min(var(--d,0),8)*14px + max(var(--d,0) - 8,0)*3px);font-size:13px;cursor:pointer;white-space:nowrap;border:0;background:none;width:100%;text-align:left}
.node:hover{background:var(--raised)}.node.sel{background:var(--raised);box-shadow:inset 2px 0 0 var(--primary)}
.node .nm{overflow:hidden;text-overflow:ellipsis;flex:1}.node .g{font:11px ui-monospace,monospace;color:var(--accent);width:12px;text-align:center}
.dirpick .node .nm.rn{flex:0 0 auto;margin-right:10px;font-weight:600}.dirpick .node .rpath{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .ar{width:10px;color:var(--muted);font-size:10px}.node.ign{opacity:.5}.node.dir .nm{color:var(--ink)}
.fview{display:flex;flex-direction:column;min-width:0;min-height:0}
.ftoolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--line)}
.ftoolbar .p{font:12.5px ui-monospace,Menlo,monospace;flex:1 1 240px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.code{flex:1;overflow:auto;font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code-bg)}
.code .ln{display:flex}.code .ln:hover{background:color-mix(in srgb,var(--raised) 60%,transparent)}
.code .no{flex:none;width:56px;text-align:right;padding-right:12px;color:var(--muted);user-select:none}
.code .tx{white-space:pre;padding-right:20px}
.code .ln.hl{background:color-mix(in srgb,var(--warn) 20%,transparent)}
.tk-c{color:var(--muted);font-style:italic}.tk-s{color:var(--ok)}.tk-n{color:var(--warn)}.tk-k{color:var(--accent)}.tk-f{color:var(--info)}
.editor{flex:1;width:100%;border:0;resize:none;background:var(--code-bg);color:var(--ink);font:12.5px/1.55 ui-monospace,Menlo,monospace;padding:10px 14px;tab-size:4;white-space:pre}
.editor:focus{outline:none}
.gbadge{font:11px ui-monospace,monospace;border:1px solid var(--accent);color:var(--accent);border-radius:3px;padding:0 5px}
.dirpick{max-height:46vh;overflow:auto;border:1px solid var(--line);border-radius:6px;margin:8px 0}
.dirpick .node{padding-left:12px}
/* shell */
.shell{display:grid;grid-template-rows:auto minmax(0,1fr) auto;height:100%;min-height:0}
.sh-out{overflow:auto;padding:10px 18px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code-bg)}
.sh-job{margin:0 0 14px}.sh-cmd{color:var(--primary);font-weight:600}
.sh-job .meta{color:var(--muted);font-size:11.5px}
.sh-job pre{margin:2px 0;background:none;padding:0;max-height:none;white-space:pre-wrap;word-break:break-word}
.sh-job .err{color:var(--bad)}.refused{color:var(--bad);border:1px solid var(--bad);padding:6px 10px;border-radius:4px;margin:4px 0}
.sh-in{display:flex;gap:8px;padding:10px 18px;border-top:1px solid var(--line)}
.sh-in input{flex:1;font:13px ui-monospace,Menlo,monospace;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.sh-in input:focus{border-color:var(--primary);outline:0}
.sh-side{display:grid;grid-template-columns:minmax(0,1fr) 300px;min-height:0;height:100%}
.sh-hist{border-left:1px solid var(--line);overflow:auto;padding:10px 12px;background:var(--surface);font-size:12.5px}
.sh-hist button.h{display:block;width:100%;text-align:left;background:none;border:0;padding:3px 0;color:var(--ink2);font:12px ui-monospace,monospace;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sh-hist button.h:hover{color:var(--ink)}
/* ansi */
.af0{color:var(--muted)}.af1{color:var(--bad)}.af2{color:var(--ok)}.af3{color:var(--warn)}.af4{color:var(--info)}.af5{color:var(--accent)}.af6{color:var(--primary)}.af7{color:var(--ink)}
.af8{color:var(--muted)}.af9{color:var(--bad)}.af10{color:var(--ok)}.af11{color:var(--warn)}.af12{color:var(--info)}.af13{color:var(--accent)}.af14{color:var(--primary)}.af15{color:var(--ink)}
.ab{font-weight:700}.ad{opacity:.65}.ai{font-style:italic}.au{text-decoration:underline}.as{text-decoration:line-through}.ar{filter:invert(1)}
.ab0{background:color-mix(in srgb,var(--muted) 35%,transparent)}.ab1{background:color-mix(in srgb,var(--bad) 35%,transparent)}.ab2{background:color-mix(in srgb,var(--ok) 35%,transparent)}.ab3{background:color-mix(in srgb,var(--warn) 35%,transparent)}.ab4{background:color-mix(in srgb,var(--info) 35%,transparent)}.ab5{background:color-mix(in srgb,var(--accent) 35%,transparent)}.ab6{background:color-mix(in srgb,var(--primary) 35%,transparent)}.ab7{background:color-mix(in srgb,var(--ink) 35%,transparent)}.ab8{background:color-mix(in srgb,var(--muted) 35%,transparent)}.ab9{background:color-mix(in srgb,var(--bad) 35%,transparent)}.ab10{background:color-mix(in srgb,var(--ok) 35%,transparent)}.ab11{background:color-mix(in srgb,var(--warn) 35%,transparent)}.ab12{background:color-mix(in srgb,var(--info) 35%,transparent)}.ab13{background:color-mix(in srgb,var(--accent) 35%,transparent)}.ab14{background:color-mix(in srgb,var(--primary) 35%,transparent)}.ab15{background:color-mix(in srgb,var(--ink) 35%,transparent)}
/* commands page */
.cmd-grid{display:grid;grid-template-columns:minmax(260px,340px) minmax(0,1fr);gap:20px;align-items:start}
.cmd-list{max-height:calc(100vh - 220px);overflow:auto;border:1px solid var(--line);border-radius:8px}
.cmd-list .cat{position:sticky;top:0;background:var(--surface);padding:6px 12px;font-size:11.5px;color:var(--muted);border-bottom:1px solid var(--line)}
.cmd-list button.c{display:block;width:100%;text-align:left;background:none;border:0;border-bottom:1px solid color-mix(in srgb,var(--line) 40%,transparent);padding:7px 12px;cursor:pointer}
.cmd-list button.c:hover,.cmd-list button.c.sel{background:var(--raised)}
.cmd-list .c b{font-family:ui-monospace,Menlo,monospace;font-size:13px}.cmd-list .c span{display:block;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmd-out{background:var(--code-bg);border-radius:8px;padding:12px 14px;font:12.5px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:60vh;margin:10px 0}
.formrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.formrow input[type=text],.formrow input[type=search]{flex:1;min-width:180px;background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:7px 10px}
.card{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--surface);margin-bottom:12px}
.card h3{margin:0 0 6px;font-size:14px}
.grid3{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.scrim.top2{z-index:40}
.palette{width:min(640px,100%);background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.palette input{width:100%;border:0;border-bottom:1px solid var(--line);background:var(--surface);padding:14px 16px;font-size:15px}
.palette input:focus{outline:0}
.palette .list{max-height:50vh;overflow:auto}
.palette .it{display:flex;gap:12px;padding:8px 16px;cursor:pointer;align-items:baseline}
.palette .it[aria-selected=true]{background:var(--raised)}
.palette .it b{font-family:ui-monospace,Menlo,monospace;min-width:150px}.palette .it span{color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:1000px){
 .chat-cols{grid-template-columns:minmax(0,1fr)}
 .rail{display:none}.rail.open{display:block;position:fixed;inset:48px 0 56px 0;z-index:12;border:0}
 .files{grid-template-columns:minmax(0,1fr)}.files .ftree{max-height:34vh;border-right:0;border-bottom:1px solid var(--line)}
 .sh-side{grid-template-columns:minmax(0,1fr)}.sh-hist{display:none}
 .cmd-grid{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:720px){
 .body{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}
 .side-nav{order:2;flex-direction:row;overflow-x:auto;border-right:0;border-top:1px solid var(--line);padding:4px 6px;gap:0}
 .side-nav a{flex-direction:column;gap:2px;padding:6px 10px;font-size:10.5px}.side-nav a[aria-current=page]{box-shadow:inset 0 2px 0 var(--primary)}
 .ws-path,.ws-git{display:none}.top{gap:8px;padding:0 10px}.tools .btn:nth-child(2){display:none}
 main{padding:16px 14px 40px}.feed{padding:4px 12px 12px}.composer{padding:8px 12px 10px}.chat-head{padding:8px 12px}
 .entry{grid-template-columns:minmax(0,1fr)!important}.entry>.who,.entry>*:not(.who){grid-column:1!important}
}

/* ---- IDE frame: rail, side panels, tabs, bottom panel, status bar ---- */
.ide{display:grid;grid-template-rows:auto minmax(0,1fr) auto;height:100%}
.ide-body{display:grid;grid-template-columns:var(--rail,64px) var(--lw,320px) 5px minmax(0,1fr) 5px var(--rw,340px);min-height:0}
.ide-body.no-left{grid-template-columns:var(--rail,64px) 0 0 minmax(0,1fr) 5px var(--rw,340px)}
.ide-body.no-right{grid-template-columns:var(--rail,64px) var(--lw,320px) 5px minmax(0,1fr) 0 0}
.ide-body.no-left.no-right{grid-template-columns:var(--rail,64px) 0 0 minmax(0,1fr) 0 0}
.ide-body.no-left .lpanel,.ide-body.no-left .rz-l,.ide-body.no-right .rpanel,.ide-body.no-right .rz-r{display:none}
.side-nav a .lbl{font-size:10.5px}
.side-nav{padding:8px 4px;align-items:stretch}
.side-nav a{flex-direction:column;gap:3px;padding:8px 2px;text-align:center;font-size:10.5px}
.lpanel,.rpanel{display:flex;flex-direction:column;min-height:0;min-width:0;background:var(--surface);overflow:hidden}
.lpanel{border-right:1px solid var(--line)}.rpanel{border-left:1px solid var(--line)}
.ptabs,.phead{display:flex;gap:4px;align-items:center;padding:6px 8px;border-bottom:1px solid var(--line)}
.ptabs button{background:none;border:0;padding:4px 10px;border-radius:5px;color:var(--ink2);cursor:pointer}
.ptabs button[aria-selected=true]{background:var(--raised);color:var(--ink)}
.pbody{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column}
.rz{background:transparent;cursor:col-resize;touch-action:none;position:relative}
.rz:hover,.resizing .rz{background:var(--primary)}
.rz-b{cursor:row-resize;height:5px;display:block}
.resizing{user-select:none;cursor:col-resize}
.center{display:grid;grid-template-rows:auto minmax(0,1fr) 5px var(--bh,0px);min-width:0;min-height:0}
.center.no-bottom{grid-template-rows:auto minmax(0,1fr) 0 0}.center.no-bottom .rz-b,.center.no-bottom .bpanel{display:none}
.tabstrip{display:flex;align-items:center;gap:2px;padding:4px 6px 0;border-bottom:1px solid var(--line);overflow-x:auto;background:var(--surface);scrollbar-width:none}
.tab{display:flex;align-items:center;border-radius:6px 6px 0 0;max-width:220px}
.tab.on{background:var(--page);box-shadow:inset 0 2px 0 var(--primary)}.tab.split:not(.on){box-shadow:inset 0 2px 0 var(--accent)}
.tab .tl{display:flex;align-items:center;gap:6px;background:none;border:0;padding:7px 4px 7px 10px;color:var(--ink2);cursor:pointer;min-width:0}
.tab.on .tl{color:var(--ink)}.tab .tl span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tab .tl .ico{width:14px;height:14px}
.tab .tx{background:none;border:0;color:var(--muted);cursor:pointer;padding:4px 8px;font-size:15px}
.panes{display:flex;min-height:0;min-width:0}
.pane{display:none;flex:1;min-width:0;min-height:0;overflow:auto;padding:22px 28px 60px}
.pane.wide{padding:0;overflow:hidden}
.pane.on{display:block}.pane.wide.on{display:flex;flex-direction:column}
.panes.split .pane.on{flex:1 1 50%;border-left:1px solid var(--line)}.panes.split .pane.on:first-of-type{border-left:0}
.bpanel{display:flex;flex-direction:column;min-height:0;background:var(--surface);border-top:1px solid var(--line)}
.bpanel .pbody{display:block}
.bpanel .sh-side{height:100%;grid-template-columns:minmax(0,1fr)}
.bpanel .phead{flex-wrap:wrap;gap:2px 10px}.bpanel .phead .hint{flex:1 1 200px;min-width:0}
.bpanel .sh-hist{display:none}                                  /* the dock is narrow: history stays one ArrowUp away */
.bpanel .shell .ftoolbar{flex-wrap:nowrap;overflow:hidden}.bpanel .shell .ftoolbar>.muted:not(#shcwd){display:none}
.bpanel .shell #shcwd{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto}
.statusbar{display:flex;flex-wrap:wrap;gap:4px 18px;align-items:center;padding:4px 14px;border-top:1px solid var(--line);background:var(--surface);font-size:12px;color:var(--ink2)}
.statusbar .sb b{color:var(--ink);font-weight:600}.statusbar .live b{color:var(--primary)}.statusbar .warn b{color:var(--warn)}.statusbar .bad b{color:var(--bad)}
.psec{padding:12px 14px;border-bottom:1px solid var(--line)}.psec h3{margin:0 0 8px}
.now{display:flex;gap:8px;align-items:center;font-weight:600}
.bar.warn>i{background:var(--warn)}.bar.bad>i{background:var(--bad)}
.vlist{flex:1;overflow:auto;position:relative}.vinner{position:relative}.vwin{position:absolute;left:0;right:0;top:0}
.vwin .node{height:28px}.node.note{color:var(--muted);cursor:default;font-size:12.5px}
.sres .hit{display:flex;flex-direction:column;padding:6px 14px;text-decoration:none;color:var(--ink);border-bottom:1px solid color-mix(in srgb,var(--line) 40%,transparent)}
.sres .hit:hover{background:var(--raised)}.sres .hit span{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.preview{padding:18px 24px;overflow:auto;flex:1}.preview img{max-width:100%;height:auto;background:var(--raised)}
.vbody{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column}.vbody>.code{flex:1}
.fview{display:flex;flex-direction:column;min-width:0;min-height:0;flex:1}
.iconbtn{padding:4px 8px}
.only-small{display:none}
.drawer-scrim{display:none}
.chat{display:flex;flex-direction:column;flex:1;min-height:0;grid-template-rows:none}
.chat-main{flex:1}
.tab .tl .ico{stroke-width:1.7}
@media (max-width:900px){
 .only-small{display:inline-flex}.hide-small{display:none}
 .ide-body,.ide-body.no-left,.ide-body.no-right,.ide-body.no-left.no-right{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}
 .center{grid-row:1}.side-nav{grid-row:2;grid-column:1;flex-direction:row;overflow-x:auto;border-right:0;border-top:1px solid var(--line);padding:2px 4px}
 .side-nav a{flex:1;min-width:58px}
 .rz{display:none}
 .lpanel,.rpanel{display:flex!important;position:fixed;top:0;bottom:0;width:min(88vw,380px);z-index:30;transform:translateX(-105%);transition:transform .16s ease-out;box-shadow:0 0 40px rgb(0 0 0/.4)}
 .rpanel{right:0;left:auto;transform:translateX(105%)}
 .ide-body.no-left .lpanel,.ide-body.no-right .rpanel{display:flex!important}
 body.drawer-left .lpanel,body.drawer-right .rpanel{transform:none}
 body.drawer-left .drawer-scrim,body.drawer-right .drawer-scrim{display:block;position:fixed;inset:0;background:color-mix(in srgb,var(--page) 60%,transparent);z-index:20}
 .center{grid-template-rows:auto minmax(0,1fr) 0 0}.center:not(.no-bottom){grid-template-rows:auto minmax(0,1fr) 0 var(--bh)}
 .bpanel{position:fixed;left:0;right:0;bottom:0;height:60vh;z-index:25;box-shadow:0 -10px 40px rgb(0 0 0/.4)}.center.no-bottom .bpanel{display:none}
 .pane{padding:16px 14px 40px}.pane.wide{padding:0}
 .ws-path,.ws-git,.statusbar .sp{display:none}
 .statusbar{overflow-x:auto;flex-wrap:nowrap;white-space:nowrap}
 .tools .btn{padding:4px 8px}
}

.tscreen{flex:1;min-height:0;overflow:hidden;background:var(--code-bg);padding:4px 8px;font:13px/1.25 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink);outline:none;position:relative;white-space:pre}
.tscreen:focus{box-shadow:inset 0 0 0 1px var(--primary)}
.trow{height:1.25em;white-space:pre;overflow:hidden}
.tprobe{position:absolute;visibility:hidden;font:inherit;white-space:pre;line-height:1.25}
.tb{font-weight:700}.td{opacity:.65}.ti{font-style:italic}.tu{text-decoration:underline}
.tcur{background:var(--ink)!important;color:var(--code-bg)!important}
.warnchip{border:1px solid var(--warn);color:var(--warn);border-radius:999px;padding:1px 10px;font-size:12px}

/* readable on both themes */
.muted,.steprow,.turn,.lbl,.note,.ws-path,.fhead .full,.tab .tx,.code .no,.statusbar .muted,.kv dt,.psec h3,.cat,.sub{color:var(--ink2)}
.tabstrip{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.tab{flex:0 0 auto;max-width:200px}.tab .tl span{max-width:140px}
.tabstrip::-webkit-scrollbar{height:6px}.tabstrip::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}
.authlost{max-width:56ch;margin:14vh auto;padding:0 24px}.authlost h1{margin-bottom:10px}.authlost code{background:var(--code-bg);padding:1px 6px;border-radius:3px}
.provider summary .gbadge{margin-left:8px}
.provider-banner{margin:0 0 8px}
.provider-banner .btn{margin-left:8px}

/* file viewer (dash_js_files.py) */
[hidden]{display:none!important}
.pview{flex:1;min-height:0;display:flex;flex-direction:column;min-width:0}
.pview .pagebar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:6px 14px;border-bottom:1px solid var(--line)}
.pview .pagebar.busy{opacity:.6}.pview .pagebar.single .btn:not([aria-pressed]){display:none}
.pview .pbody{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column}.pview .pbody>.code{min-height:0}
.pview .goto{width:84px}
.pview.wrap .code .tx{white-space:pre-wrap;overflow-wrap:anywhere;min-width:0;flex:1}
.vbody>.pview,.vbody>.imgstage{flex:1}
.imgstage{flex:1;min-height:0;overflow:auto;padding:16px;background-color:var(--raised);text-align:center}
.imgstage img{background:repeating-conic-gradient(color-mix(in srgb,var(--line) 55%,transparent) 0 25%,transparent 0 50%) 0 0/16px 16px}
.imgstage.fitmode{display:flex;align-items:center;justify-content:center}.imgstage.fitmode img{max-width:100%;max-height:100%;object-fit:contain}
.imgstage:not(.fitmode) img{max-width:none;image-rendering:auto}
.zoombar .zlab{min-width:44px;text-align:center}
.fmedia{display:block;max-width:100%;margin:0 auto;background:#000;border-radius:6px}audio.fmedia{width:min(560px,100%);background:none}
.mediabox .notice{margin-bottom:12px}
.pdfframe{flex:1;width:100%;min-height:70vh;border:0;background:var(--raised)}
.fcard{max-width:560px;margin:8vh auto;padding:18px 22px;border:1px solid var(--line);border-radius:8px;background:var(--raised);text-align:left}
.fcard h3{margin:0 0 10px;overflow-wrap:anywhere}.fcard .kv dd{overflow-wrap:anywhere;margin:0 0 6px}.fcard .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.csvwrap{padding:0}.csv{border-collapse:collapse;font:12.5px/1.4 ui-monospace,Menlo,monospace}
.csv th,.csv td{border:1px solid var(--line);padding:3px 8px;text-align:left;white-space:pre;max-width:420px;overflow:hidden;text-overflow:ellipsis}
.csv th{position:sticky;top:0;background:var(--raised);color:var(--ink)}.csv .rn{color:var(--ink2);text-align:right;user-select:none}
.fthumb{display:inline-flex;flex-direction:column;align-items:center;gap:4px;width:104px;padding:6px;border:1px solid var(--line);border-radius:8px;background:var(--raised);color:var(--ink);cursor:pointer;font:inherit}
.fthumb:hover{border-color:var(--primary)}.fthumb img,.fthumb .fext{width:90px;height:68px;object-fit:cover;border-radius:4px;display:flex;align-items:center;justify-content:center;background:var(--code-bg);font-weight:700;color:var(--ink2)}
.fthumb .fname{max-width:92px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
/* cards, ghost suggestion, trace levels (dash_js_cards.py) */
.cview{margin:4px 0 6px;font-size:12.5px}.cview mark{background:color-mix(in srgb,var(--warn) 35%,transparent);color:inherit;border-radius:2px}
.tbar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line);margin:2px 0 4px}.tbar i{display:block;height:100%}
.tbar .p{background:var(--ok)}.tbar .f{background:var(--bad)}.tbar .s{background:var(--muted)}
.ghit{font-family:ui-monospace,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}.ghit .gl{color:var(--info)}
.flist{list-style:none;padding:0;font-family:ui-monospace,Menlo,monospace;font-size:12px}
details.rawout{margin-top:6px}details.rawout>summary{color:var(--muted);font-size:12px;cursor:pointer}
.composer .ghost{position:absolute;left:0;right:0;top:0;padding:10px 12px;border:1px solid transparent;line-height:1.45;color:var(--muted);pointer-events:none;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.composer .ghost .ghint{border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:11px}
.t-compact .trail dt:nth-of-type(-n+2),.t-compact .trail dd:nth-of-type(-n+2),.t-normal .trail dt:nth-of-type(-n+2),.t-normal .trail dd:nth-of-type(-n+2),.t-compact .trail dt:nth-of-type(5),.t-compact .trail dd:nth-of-type(5),.t-normal .trail dt:nth-of-type(5),.t-normal .trail dd:nth-of-type(5){display:none}
.t-compact details.denied .trail dt:nth-of-type(2),.t-compact details.denied .trail dd:nth-of-type(2),.t-normal details.denied .trail dt:nth-of-type(2),.t-normal details.denied .trail dd:nth-of-type(2){display:block}
.t-normal .dtl,.t-normal .steprow.dtl{display:none}
/* commands page: grouped list, roomy output (commands agent) */
.cmd-grid{grid-template-columns:minmax(280px,380px) minmax(0,1fr)}
.cmd-search{width:100%;max-width:none}
.cmd-list .cmd-cat>summary.cat{cursor:pointer;list-style:none}.cmd-list .cmd-cat>summary.cat::before{content:"\25B8  "}.cmd-list .cmd-cat[open]>summary.cat::before{content:"\25BE  "}
.cmd-right{position:sticky;top:8px;min-width:0}
.cmd-panel{border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-height:50vh;background:var(--surface)}
.cmd-panel .cmd-out{max-height:68vh;margin-top:6px}
.cmd-running .btn{margin-left:8px}
details.cmd-quick{margin-top:22px}details.cmd-quick>summary{cursor:pointer;color:var(--ink2);font-size:13px;margin-bottom:10px}
@media (max-width:760px){.cmd-right{position:static}.cmd-grid{grid-template-columns:minmax(0,1fr)}}
/* ---- clarity pass: contrast, tabs, phone width, quiet panels ---- */
/* Secondary text must reach 4.5:1 on every surface in both themes: --muted becomes the same as --ink2. */
:root{--muted:var(--ink2)}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){--muted:var(--ink2);--primary:#00725F}}
:root[data-theme="dark"]{--muted:var(--ink2)}
:root[data-theme="light"]{--muted:var(--ink2);--primary:#00725F}
::placeholder{color:var(--ink2);opacity:1}
.td,.ad{opacity:.92}
.tabstrip{position:relative;overflow:visible;scrollbar-width:auto}
.tabscroll{display:flex;align-items:center;gap:2px;flex:1 1 auto;min-width:0;overflow-x:auto;scrollbar-width:none}
.tabscroll::-webkit-scrollbar{display:none}
.tabstrip>.mini{flex:none;margin:0 2px 4px;padding:2px 8px;line-height:1.5}
.tabstrip>.tsl,.tabstrip>.tsr{font-size:16px;line-height:1.2}
.tab{flex:0 0 auto}
.tab-pop{position:fixed;z-index:60;min-width:230px;max-width:min(92vw,360px);max-height:60vh;overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 10px 30px rgb(0 0 0/.35)}
.tab-pop hr{border:0;border-top:1px solid var(--line);margin:4px 0}
.tab-pop .tirow{display:flex;align-items:center;border-radius:5px}.tab-pop .tirow.on{background:var(--raised)}
.tab-pop .tmi{display:flex;align-items:center;gap:8px;flex:1;min-width:0;text-align:left;background:none;border:0;border-radius:5px;padding:6px 10px;color:var(--ink);cursor:pointer;font:inherit}
.tab-pop .tmi span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tab-pop .tmi .ico{width:14px;height:14px}
.tab-pop .tmi:hover,.tab-pop .tx:hover{background:var(--raised)}
.tab-pop .tx{background:none;border:0;color:var(--ink2);cursor:pointer;padding:4px 10px;font-size:15px;border-radius:5px}
.turnbar .tgl{opacity:0;transition:none}
.entry:hover .turnbar .tgl,.turnbar .tgl:focus-visible,.turnbar:hover .tgl{opacity:1}
@media (hover:none){.turnbar .tgl{opacity:1}}
.heldbox{border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:6px;background:var(--surface);padding:6px 10px;margin:0 0 8px}
.heldbox .held{display:flex;gap:8px;align-items:center;padding:3px 0}.heldbox .held .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.psec .quiet,p.quiet{margin:0;padding:10px 14px;color:var(--ink2);font-size:12px;border-bottom:1px solid var(--line)}
.page-hint{flex:none;padding:6px 14px;font-size:12.5px;color:var(--ink2);border-bottom:1px solid var(--line);background:var(--surface)}.page-hint b{color:var(--ink)}
.phead .hint{font-size:12px}
details.adv{margin:12px 0;border:1px solid var(--line);border-radius:8px;padding:6px 12px;background:var(--surface)}
details.adv>summary{cursor:pointer;color:var(--ink2);font-size:13px}
details.adv[open]>summary{margin-bottom:8px}
details.adv .card{margin-top:8px}details.adv code{background:var(--code-bg);padding:1px 6px;border-radius:3px}
.psec{padding:10px 14px}
.stopping,.stop-btn:disabled{opacity:.75}
@media (max-width:900px){
 .ide{height:100vh;height:100dvh}
 .ide-body{min-height:0}
 .center{grid-column:1;grid-row:1}.side-nav{grid-column:1;grid-row:2;display:flex}
 .side-nav a{flex:0 0 auto;min-width:64px}
 /* the Shell dock is an ordinary row now: it shares the screen with the page instead of covering the message box */
 .center:not(.no-bottom){grid-template-rows:auto minmax(0,1fr) min(var(--bh,300px),42vh);grid-template-rows:auto minmax(0,1fr) min(var(--bh,300px),42dvh)}
 .center.no-bottom{grid-template-rows:auto minmax(0,1fr)}
 .bpanel{position:static;height:auto;box-shadow:none;border-top:2px solid var(--line)}
 .tabstrip>.mini#tab-split{display:none}
 .tab-pop{left:8px;right:8px!important;max-width:none}
}
@media (max-width:720px){
 .top{gap:6px;padding:0 8px;overflow:hidden}
 .top .brand span:last-child{display:none}
 .ws-chip{flex:1 1 0;min-width:0;padding:4px 8px}.ws-chip b{overflow:hidden;text-overflow:ellipsis}
 .tools{flex:none;margin-left:0;gap:4px}.tools .btn{padding:4px 8px}
 .chat-head{gap:6px 8px}.chat-head h1{max-width:100%;flex-basis:100%}
 .pill select{max-width:110px}
 .cbar .btn{padding:6px 10px}
}
/* ---- drag and drop (attach files, folders and pictures) ---- */
.chat{position:relative}
.dropveil{position:absolute;inset:0;z-index:30;display:none;align-items:center;justify-content:center;pointer-events:none;background:color-mix(in srgb,var(--bg,#fff) 82%,transparent);border:2px dashed var(--primary)}
.chat.dropping .dropveil{display:flex}
.dropveil b{font-size:18px;padding:10px 18px;border-radius:8px;background:var(--surface);border:1px solid var(--primary)}
.chips .chip.rich{border-radius:8px;padding:3px 4px 3px 4px;gap:8px;max-width:100%}
.chip.rich .cthumb{width:32px;height:32px;object-fit:cover;border-radius:4px;flex:none;background:var(--raised)}
.chip.rich .cicon{width:32px;height:32px;flex:none;display:inline-flex;align-items:center;justify-content:center;border-radius:4px;background:var(--raised);font:600 10px ui-monospace,monospace;color:var(--muted)}
.chip.rich .cname{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.chip.rich .cwarn{color:var(--warn);font-size:12px;white-space:normal;max-width:260px}
.chip.rich .cname b{font-weight:600;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip.rich .cname span{font-size:11px;color:var(--muted)}
.chip.rich .copen{border:0;background:none;color:var(--primary);cursor:pointer;padding:0 4px;font-size:12px}
.node.msel{background:var(--raised);box-shadow:inset 2px 0 0 var(--accent)}
.node[draggable=true]{-webkit-user-drag:element}
.ctxmenu{position:fixed;z-index:60;min-width:190px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.ctxmenu button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink);padding:6px 10px;border-radius:5px;font-size:13px;cursor:pointer}
.ctxmenu button:hover,.ctxmenu button:focus{background:var(--raised);outline:none}
.ctxmenu hr{border:0;border-top:1px solid var(--line);margin:4px 0}
.sres .hit[draggable=true]{-webkit-user-drag:element}
/* >>> sessions-usage controls (dash_js_manage.py) */
.mg-opts{margin-top:-6px;gap:4px 18px}.mg-check{display:inline-flex;align-items:center;gap:6px;color:var(--ink2);font-size:13px;cursor:pointer}
.mg-selbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;margin-bottom:8px;background:var(--raised);border:1px solid var(--line);border-radius:8px}
.mg-head{display:flex;align-items:center;gap:10px;padding:4px 4px;font-size:12px;border-bottom:1px solid var(--line)}
.mg-rows{border-top:0}
.mg-group{padding:14px 4px 4px;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.mg-row{display:grid;grid-template-columns:22px 22px minmax(0,1fr) 150px 64px 78px 70px 30px;gap:10px;align-items:baseline;padding:9px 4px;border-bottom:1px solid var(--line)}
.mg-row:hover,.mg-row.sel{background:var(--surface)}
.mg-main{min-width:0;text-decoration:none;color:inherit}
.mg-main .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mg-main .meta{color:var(--muted);font-size:12px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mg-main .snip{color:var(--ink2)}
.mg-row .num{text-align:right;color:var(--ink2);font-size:12.5px}
.mg-model{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mg-star{background:none;border:0;padding:0;cursor:pointer;color:var(--accent);font-size:15px;line-height:1}.mg-star.off{color:var(--muted)}
.mg-more-btn{font-size:16px;line-height:1;padding:0 6px}
.mg-more{padding:14px 0;display:flex;gap:10px;align-items:center}
.mg-trash>summary{cursor:pointer;font-weight:600;padding:6px 0}
.mg-trow{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line)}.mg-ttl{flex:1;min-width:0}
.mg-ttl .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mg-trashbody .acts{display:flex;gap:8px;margin:6px 0}
.mg-toasts{position:fixed;left:16px;bottom:16px;z-index:80;display:flex;flex-direction:column;gap:8px;max-width:min(520px,calc(100vw - 32px))}
.mg-toast{display:flex;align-items:center;gap:10px;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:8px 12px;box-shadow:0 6px 20px rgb(0 0 0/.25)}
.mg-toast.bad{border-color:var(--bad)}.mg-toast span{flex:1;min-width:0}
.mg-pop{position:fixed;z-index:90;min-width:190px;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 10px 30px rgb(0 0 0/.3);animation:none}
.mg-pop button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink);padding:6px 10px;border-radius:5px;cursor:pointer}
.mg-pop button:hover,.mg-pop button:focus{background:var(--surface);outline:none}.mg-pop button.danger{color:var(--bad)}
.mg-pop hr{border:0;border-top:1px solid var(--line);margin:4px 0}
.mg-scrim,.mg-scrim .modal{animation:none}.mg-modal{width:min(520px,100%)}.mg-modal .formrow label{display:block;margin-bottom:4px;color:var(--ink2)}
.mg-tabmenu{font-weight:700}
.mg-custom{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:-8px 0 16px}
.mg-chartbar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:10px}
.chart h2 .mg-chartbtn{margin-left:10px;font-size:12px;font-weight:400}
.mg-sech{display:flex;align-items:baseline;gap:10px}
.mg-memadd textarea,.mg-mem textarea{font:inherit;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px;resize:vertical}.mg-memadd .formrow{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}.mg-mem .acts{display:flex;gap:8px;margin:8px 0}.mg-mem .out{white-space:pre-wrap}
.mg-th{background:none;border:0;padding:0;color:inherit;font:inherit;cursor:pointer;white-space:nowrap}.mg-th:hover{color:var(--ink)}
.mg-sortable>.btn{margin-top:8px}
.mg-bt{grid-column:1/-1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mg-brow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 18px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.mg-bn{display:flex;align-items:center;color:var(--ink2)}.mg-bc{display:flex;gap:6px;align-items:center}.mg-bc input{width:84px}
.mg-clear .acts{display:flex;flex-wrap:wrap;gap:8px}
@media (max-width:860px){.mg-row{grid-template-columns:22px 22px minmax(0,1fr) 70px 30px}.mg-row .hide-s{display:none}.mg-brow{grid-template-columns:1fr}}
/* <<< sessions-usage controls */
/* Jump to latest (a style element added by script is blocked by the page's content policy) */
.jump-latest{position:absolute;left:50%;transform:translateX(-50%);z-index:5;border:1px solid var(--line);background:var(--raised);color:var(--ink);border-radius:999px;padding:5px 14px;font:inherit;font-size:12.5px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.25)}
.jump-latest[hidden]{display:none}
"""
