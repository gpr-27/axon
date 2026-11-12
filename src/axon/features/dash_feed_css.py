"""The look of the chat feed's parts: one timeline rail per turn, step nodes with a state, tool rows and the lane of calls
running in parallel, streaming text, the turn's usage summary, and the subagent fan-out board with its concurrency timeline.

Neutral gray with one teal accent (``--primary``/``--accent``), both themes through the tokens, motion that stops under
``prefers-reduced-motion``. It is appended to ``CHATX_CSS``; ``FINISH_CSS`` is served later, so these rules take a class or
two more of specificity instead of ``!important`` where they can.
"""

FEED_CSS = r"""
/* ===== chat feed: rail, nodes, rows ===== */
.entry.a{--rail:var(--line2);--node:16px}
.entry.a .tsteps{position:relative;padding-left:0}
.entry.a .tsteps::before{content:"";position:absolute;left:7.5px;top:12px;bottom:12px;width:1px;background:linear-gradient(var(--rail),var(--rail));pointer-events:none}
.entry.a .tsteps .shead,.entry.a .tsteps .xrow>.mk,.entry.a .tsteps .phead>.mk{position:relative;z-index:1}
.entry.a .tsteps .mk{background:var(--page)}
.entry.a .mk.run .spin{animation:ax-node 1.1s ease-in-out infinite;box-shadow:0 0 0 0 color-mix(in srgb,var(--primary) 45%,transparent);animation-name:ax-node,ax-ring;animation-duration:1.1s,1.6s;animation-iteration-count:infinite}
@keyframes ax-ring{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--primary) 45%,transparent)}70%,100%{box-shadow:0 0 0 7px transparent}}
.entry.a .mk.stop::after{background:var(--ink2);opacity:.55}
.entry.a .tsteps .step{margin-top:4px}
.entry.a .tsteps .shead{min-height:30px;font:600 13px/1.4 var(--font-ui);color:var(--ink);letter-spacing:-.005em}
.entry.a .shead .ssub{font-weight:400;color:var(--ink2)}
.entry.a .shead .stime{color:var(--ink2);font-weight:500}

/* a tool call is a calm row; it opens into its input and output */
.entry.a .tsteps details.tool.kd-tool{margin:2px 0 2px 24px;padding:0;border:1px solid var(--line);border-left:1px solid var(--line);border-radius:10px;background:var(--surface);transition:border-color .2s ease,background-color .2s ease}
.entry.a .tsteps details.tool.kd-tool[open]{background:var(--surface)}
.entry.a .tsteps details.tool.kd-tool[data-state=run]{border-color:color-mix(in srgb,var(--primary) 38%,var(--line))}
.entry.a .tsteps details.tool.kd-tool[data-state=bad]{border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}
.entry.a .tsteps details.tool.kd-tool>summary.xrow{min-height:36px;padding:3px 10px 3px 8px;border-radius:10px;position:relative;overflow:hidden}
.entry.a .tsteps details.tool.kd-tool>summary.xrow:hover{background:var(--raised)}
.entry.a .xrow .tn{flex:0 0 auto;width:auto;max-width:34%;padding:0;font:600 13px/1.45 var(--font-ui)}
.entry.a .xrow .ta{font:400 13px/1.45 var(--font-ui)}
.entry.a .xrow .td{min-width:44px;margin-left:auto}
.entry.a .xrow .kind .kt{display:none}
.entry.a .xrow .kind,.entry.a .tool.kd-tool>summary .kind{flex:none;min-width:0}
.entry.a .tool.kd-tool>summary .kind svg{margin-right:2px}
.entry.a .tool.think>summary .kind .kt{display:inline}
.entry.a .xrow .route{order:7}
.tool.both>.tdet .pstat{display:none}
.tool.both>.tdet .tpane.p-why{display:none!important}
.tool.both>.tdet .tpane.p-why:has(a){display:block!important;margin-top:6px}
.entry.a .tool[data-state=run]>summary .td{color:var(--primary);font-weight:600}
/* running: a thin indeterminate line travels under the row; it stops when the call settles */
.entry.a .tool[data-state=run]>summary.xrow::after{content:"";position:absolute;left:0;bottom:0;height:2px;width:34%;background:linear-gradient(90deg,transparent,var(--primary),transparent);animation:ax-run 1.4s cubic-bezier(.4,0,.6,1) infinite}
@keyframes ax-run{from{transform:translateX(-100%)}to{transform:translateX(300%)}}
/* while a call runs there is nothing to read below its row yet; the body appears, softly, when the result is in */
.entry.a .tool.both[data-state=run]>.tdet{display:none}
.entry.a .tool.both:not([data-state=run])>.tdet{animation:ax-in .22s ease-out}
@keyframes ax-in{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
.entry.a .tsteps details.tool>.tdet{margin:0;padding:2px 12px 12px;border-radius:0 0 10px 10px;background:none;border-top:1px solid var(--line)}
.tool.both>.tdet .psec{margin:10px 0 4px;font:600 12px/1.6 var(--font-ui);color:var(--ink2);text-transform:none}
.tool.both>.tdet .psec .copy{border:0;background:none;padding:0 6px;color:var(--ink2)}
.tool.both>.tdet .psec .copy:hover{color:var(--ink);background:var(--raised)}
.tool.both>.tdet pre.out{background:var(--code-bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:12.5px/1.55 var(--font-code);max-height:none}
.tool.both>.tdet pre.out.cmd{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink)}
.axmain .kv1{display:grid;grid-template-columns:minmax(64px,auto) minmax(0,1fr);gap:0 12px;font:13px/1.55 var(--font-ui);color:var(--ink);overflow-wrap:anywhere}
.axmain .kv1 .k{color:var(--ink2)}
.rawin,.rawout{margin:6px 0 0}
.rawin>summary,.rawout>summary{cursor:pointer;font:500 12px/1.6 var(--font-ui);color:var(--ink2);min-height:24px}
.rawin>summary:hover,.rawout>summary:hover{color:var(--ink)}
.axpg .clipped{margin-top:6px}
.axpg .clipped .more{background:none;border:1px solid var(--line2);border-radius:7px;padding:2px 10px;min-height:28px;font:500 12px/1.5 var(--font-ui);color:var(--ink);cursor:pointer}
.axpg .clipped .more:hover{background:var(--raised)}
.entry.a .handoff{margin:2px 0 6px 24px;padding:0;background:none;font:400 12px/1.5 var(--font-ui);color:var(--ink2)}
.entry.a .handoff::before{content:"\2193\00a0"}
.entry.a .tsteps .tool.think>.tdet{border-top:0}
.entry.a .tsteps details.tool.kd-think{margin:2px 0 2px 24px;border:0;border-left:2px solid var(--line2);border-radius:0;padding:0 0 0 10px;background:none}
.entry.a .tsteps details.tool.kd-think>summary.xrow{padding-left:0}
.entry.a .tsteps details.tool.kd-think>summary.xrow>.mk{margin-left:-35px;margin-right:19px}

/* ===== calls running together: one lane, one header, each call with its own status and time ===== */
.entry.a .pgroup{margin:6px 0 6px 24px;padding:8px;border:1px solid var(--line);border-radius:12px;background:var(--raised);box-shadow:0 1px 0 rgba(0,0,0,.03)}
.entry.a .pgroup>.phead{display:flex;align-items:center;gap:10px;min-height:28px;padding:0 4px;font:600 12.5px/1.4 var(--font-ui);color:var(--ink)}
.entry.a .pgroup>.phead>.mk{margin-left:-34px;margin-right:18px;flex:none}
.entry.a .pgroup>.phead .ptitle{flex:0 1 auto;min-width:0}
.entry.a .pgroup>.phead .ptime{margin-left:auto;font-weight:500;color:var(--ink2)}
.pbar{flex:1 1 60px;max-width:160px;height:3px;border-radius:2px;background:var(--line2);overflow:hidden}
.pbar b{display:block;height:100%;width:0;background:var(--primary);border-radius:2px;transition:width .3s ease}
.entry.a .pgroup[data-state=bad] .pbar b{background:var(--bad)}
.entry.a .pgroup .prows{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:6px;margin-top:6px}
.entry.a .pgroup .prows::after{content:none}
.entry.a .pgroup .prows>details.tool.kd-tool{margin:0;min-width:0}
.entry.a .pgroup .prows>details.tool[open]:not([data-state=run]){grid-column:1/-1}
.entry.a .pgroup .xrow .tn{flex:1 1 auto;max-width:none}
.entry.a .pgroup .xrow .ta{flex:1 0 100%;order:5;padding-left:2px}
.entry.a .pgroup .xrow .route{flex:1 0 100%;order:6;padding:0 0 2px 2px}
.entry.a .pgroup .prows>details.tool.kd-tool>summary.xrow{padding-left:10px}
.entry.a .pgroup .xrow .chips{order:6;flex:1 0 100%;padding:0 0 2px 2px}

/* ===== streaming text, the answer ===== */
.entry.a.live .md.stream>:last-child::after{content:"";display:inline-block;width:7px;height:1em;margin-left:3px;vertical-align:-2px;background:var(--primary);border-radius:1px;animation:ax-caret 1s steps(2,start) infinite}
@keyframes ax-caret{to{visibility:hidden}}
.feed{overflow-anchor:auto}
.feed .stream,.feed .tanswer{overflow-anchor:none}
.entry.a .narr.md{margin:2px 0 4px 24px;border-left:0;padding-left:0;color:var(--ink2)}
.entry.a .tanswer .kind{margin-bottom:6px}
.jump-latest{position:absolute;left:50%;transform:translateX(-50%);z-index:8;min-height:36px;padding:0 16px;border:1px solid var(--line2);border-radius:999px;background:var(--float);color:var(--ink);font:500 13px/1 var(--font-ui);box-shadow:var(--shadow);cursor:pointer}
.jump-latest:hover{border-color:var(--primary)}

/* ===== the one usage summary at the end of a turn ===== */
.tusage{margin:14px 0 0;padding:8px 12px;border:1px solid var(--line);border-radius:10px;background:var(--raised);font:12px/1.5 var(--font-ui);color:var(--ink2);overflow-x:auto;max-width:100%}
.tusage-h{font:600 12px/1.6 var(--font-ui);color:var(--ink2);margin-bottom:2px}
.tusage-t{border-collapse:collapse;width:100%;min-width:360px;font-variant-numeric:tabular-nums}
.tusage-t th{font-weight:500;text-align:left;color:var(--muted);padding:2px 10px 2px 0}
.tusage-t th.r,.tusage-t td.r{text-align:right}
.tusage-t td{padding:2px 10px 2px 0;color:var(--ink);border:0}
.tusage-t td.mdl{color:var(--ink2);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tusage-t tr.tot td{border-top:1px solid var(--line);font-weight:600}

/* ===== subagent fan-out board ===== */
.fanout{position:relative;margin:8px 0 8px 24px;padding:12px;border:1px solid var(--line);border-radius:14px;background:var(--raised);box-shadow:0 1px 0 rgba(0,0,0,.03);min-width:0;max-width:100%}
.fanout.page{margin:12px 0}
.fanout h5{margin:0 0 10px;font:600 13px/1.4 var(--font-ui);letter-spacing:0;color:var(--ink)}
.fanout[data-state=run] h5::before{content:"";display:inline-block;width:7px;height:7px;margin-right:8px;border-radius:50%;background:var(--primary);vertical-align:1px;animation:ax-node 1.1s ease-in-out infinite}
.fanout .fo-tl{overflow-x:auto;margin:0 0 12px;padding:8px 10px 6px;border:1px solid var(--line);border-radius:10px;background:var(--surface);-webkit-overflow-scrolling:touch}
.fanout .fo-tl[hidden]{display:none}
.fo-tl-in{min-width:360px}
.fo-trow{display:grid;grid-template-columns:30px minmax(0,1fr);align-items:center;gap:8px;height:12px;margin:3px 0}
.fo-tn{font:500 11px/1 var(--font-ui);color:var(--muted);font-variant-numeric:tabular-nums}
.fo-track{position:relative;height:6px;border-radius:3px;background:repeating-linear-gradient(90deg,transparent 0 24px,var(--line) 24px 25px)}
.fo-bar{position:absolute;top:0;bottom:0;border-radius:3px;background:var(--ink2);opacity:.8;transition:width .5s linear}
.fo-bar.run{background:var(--primary);opacity:1}
.fo-bar.bad{background:var(--bad);opacity:1}
.fo-bar.ok{background:var(--ink2)}
.fo-bar.stop{opacity:.4}
.fo-axis{display:flex;justify-content:space-between;margin:4px 0 0 38px;font:11px/1.2 var(--font-ui);color:var(--muted);font-variant-numeric:tabular-nums}
.fanout .fo-grid{display:grid;gap:10px;border:0;border-radius:0;overflow:visible;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr))}
.fanout[data-n="1"] .fo-grid{grid-template-columns:minmax(0,1fr)}
.fanout .fo-lane{display:flex;flex-direction:column;min-width:0;border:1px solid var(--line);border-radius:12px;background:var(--surface);outline:0;overflow:hidden;transition:border-color .2s ease}
.fanout .fo-lane[data-state=run]{border-color:color-mix(in srgb,var(--primary) 40%,var(--line))}
.fanout .fo-lane[data-state=bad]{border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}
.fanout .fo-lh{padding:10px 12px 8px;border:0;display:flex;flex-direction:column;gap:3px}
.fanout .fo-t{display:flex;align-items:center;gap:8px;min-width:0}
.fanout .fo-t .mk{flex:none}
.fanout .fo-t a{flex:1 1 auto;min-width:0;font:600 13px/1.4 var(--font-ui);color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;min-height:24px;line-height:24px}
.fanout .fo-stat{flex:none;font:500 12px/1.4 var(--font-ui);color:var(--ink2)}
.fanout .fo-lane[data-state=run] .fo-stat{color:var(--primary)}
.fanout .fo-lane[data-state=bad] .fo-stat{color:var(--bad)}
.fanout .fo-time{flex:none;min-width:36px;text-align:right;font:500 12px/1.4 var(--font-ui);color:var(--ink);font-variant-numeric:tabular-nums}
.fanout .fo-meta{display:flex;flex-wrap:wrap;gap:0 8px;padding-left:24px;font:12px/1.5 var(--font-ui);color:var(--ink2)}
.fanout .fo-meta span+span::before{content:"\00B7";margin-right:8px;color:var(--muted)}
.fanout .fo-task{padding-left:24px;font:12.5px/1.5 var(--font-ui);color:var(--ink2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.fanout .fo-last{padding-left:24px;font:12.5px/1.5 var(--font-code);color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fanout .fo-last[hidden]{display:none}
.fanout:not(.page) .fo-last{display:none}
.fanout .fo-lane[data-state=run] .fo-last::before{content:"\25B8\00a0";color:var(--primary)}
.fanout .fo-steps{max-height:132px;flex:none;border-top:1px solid var(--line);padding:4px 0;background:var(--code-bg)}
.fanout .sa-row{font:12px/1.5 var(--font-code);grid-template-columns:14px minmax(0,auto) minmax(0,1fr) auto;padding:1px 10px}
.fanout .sa-row .ss{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;-webkit-line-clamp:unset}
.fanout .fo-acts{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:8px 12px 10px;border-top:1px solid var(--line)}
.fanout .fo-acts .formrow{gap:6px}
.fanout .fo-acts .btn{min-height:36px}
.fanout .sa-ctl{border-top:1px solid var(--line);padding:8px 12px}
.fanout .sa-ctl .sa-say{font-size:16px;min-height:36px;border-radius:8px}
.fanout .sa-ctl .btn{min-height:36px}

@media (max-width:560px){
.entry.a .tsteps details.tool.kd-tool,.entry.a .tsteps details.tool.kd-think,.entry.a .pgroup,.fanout,.entry.a .handoff,.entry.a .narr.md{margin-left:12px}
.entry.a .pgroup{padding:6px}
.entry.a .xrow .tn{max-width:46%}
.entry.a .tsteps::before{left:7.5px}
.fanout{padding:8px}
.fanout .fo-grid,.entry.a .pgroup .prows{grid-template-columns:minmax(0,1fr)}
.fanout .fo-acts .btn,.axpg .clipped .more,.rawin>summary,.rawout>summary{min-height:44px;display:inline-flex;align-items:center}
.fanout .fo-t a{min-height:44px;line-height:44px}
.tool.both>.tdet{padding-left:8px;padding-right:8px}
.tusage{padding:8px}
}
@media (prefers-reduced-motion:reduce){
.entry.a .mk.run .spin,.entry.a .tool[data-state=run]>summary.xrow::after,.entry.a.live .md.stream>:last-child::after,.fanout[data-state=run] h5::before{animation:none}
.entry.a .tool.both:not([data-state=run])>.tdet{animation:none}
.pbar b,.fo-bar,.entry.a .tsteps details.tool.kd-tool{transition:none}
}
"""
