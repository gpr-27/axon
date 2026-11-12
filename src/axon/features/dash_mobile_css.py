"""Phone and tablet layer of the dashboard shell, loaded last so it wins over the desktop rules.

Root causes this fixes (real device, opened over the LAN):
  * iOS Safari zooms into any focused field whose font is under 16px, then leaves the page zoomed;
    every field is now 16px on touch and narrow screens, so focus never zooms.
  * 100vh and a bare viewport meta made the bars jump with the URL bar and the keyboard; the shell is
    now exactly one dynamic viewport tall and follows `visualViewport` while the keyboard is open.
  * menus, modals and the progress panel were desktop popovers; they are bottom sheets and slide-overs.
Colours come only from the existing theme tokens (neutral gray, one teal accent)."""
from __future__ import annotations

MOBILE_CSS = r"""
/* ---- phone and tablet shell ---- */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%;-webkit-tap-highlight-color:transparent}
a,button,input,select,textarea,summary,label,[role=button],[role=tab]{touch-action:manipulation}
img,svg,video,canvas{max-width:100%}
@media (max-width:900px),(pointer:coarse){
 /* one fixed screen: the document never scrolls, inner areas do (no URL-bar jitter, no rubber band) */
 html,body{height:100%;overflow:hidden;overscroll-behavior:none;max-width:100%}
 #app{height:100%;max-width:100%}
 .ide{height:100vh;height:100dvh;max-width:100%;padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right)}
 html.kb-open .ide{height:var(--vvh,100dvh)}
 html.kb-open .side-nav{display:none}
 .pane,.feed,.pbody,.wp-body,.modal,.tabscroll,.wbmenu,.tab-pop,.ctxmenu,.mg-pop,.pop,.vlist,pre,.lpanel,.rpanel,.wpanel{overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
 pre,.md pre,.code{max-width:100%;overflow-x:auto}
 .md,.entry,.pane,.modal,.pbody{overflow-wrap:anywhere}
 .md table{max-width:100%}
 body:has(.scrim),body.drawer-right,body.drawer-left,body.drawer-wb{overflow:hidden}
 /* every form control is 16px: iOS never zooms on focus (checkboxes and ranges are not text fields) */
 input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=file]),select,textarea,
 .composer textarea,.composer .ghost,.composer textarea:focus,.search,.fp-q,.sh-in,.xterm-helper-textarea{font-size:16px!important}
 input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=file]),select{min-height:44px}
 input[type=checkbox],input[type=radio]{width:22px;height:22px}
 /* tap targets */
 .btn,button.mini,.seg button,.tabs button,.tabs a,.pill{min-height:44px}
 .btn.sm,.mini{min-width:44px}
 summary{min-height:44px;display:flex;align-items:center}
 .cmd-search,.search{min-height:44px}
 .node,.row,.mg-row,.xrow .tn{min-height:44px}
 .helptip{min-width:44px;min-height:44px;display:inline-grid;place-items:center}
 /* top bar: respects the notch, project chip takes the spare room, icons are real buttons */
 .top{height:calc(48px + env(safe-area-inset-top));padding-top:env(safe-area-inset-top);padding-left:max(8px,env(safe-area-inset-left));padding-right:max(6px,env(safe-area-inset-right));gap:4px}
 .top .tbicons{gap:0;flex:0 0 auto;margin-left:auto}
 .tbi{width:44px;height:44px;border-radius:12px}
 .tbi .ico{width:20px;height:20px}
 .top .ws-chip{height:44px;min-height:44px;display:flex;align-items:center}
 .top .ctitle{min-height:44px;min-width:44px}
 .linkbtn,.ci-btn,.cc-runb,.cn-act,.cn-more,.plink,.mg-star,.rfid,.tabs button,.tabs a,.pane .btn{min-height:44px!important;min-width:44px!important;display:inline-flex;align-items:center;justify-content:center}
 .mg-main{min-height:44px;display:flex!important;align-items:center}
 .top .brand{display:flex;align-items:center;min-height:44px}
 input[type=checkbox],input[type=radio]{width:24px!important;height:24px!important}
 /* tab strip becomes one compact switcher: the open tab and a Tabs menu (the bottom nav does the rest) */
 .tabstrip{padding:0 4px 0 6px;min-height:44px}
 .tabstrip>.tsl,.tabstrip>.tsr,.tabstrip>#tab-split{display:none!important}
 .tabscroll .tab:not(.on){display:none}
 .tabscroll .tab{max-width:none;flex:1 1 auto;min-width:0}
 .tabscroll .tab .tl{min-height:44px;flex:1 1 auto;min-width:0}
 .tabscroll .tab .tl span{max-width:none}
 .tabscroll .tab .tx{min-width:44px!important;min-height:44px!important}
 .tabstrip>#tab-menu{margin:0;min-height:44px;padding:0 12px}
 /* bottom nav */
 .side-nav{padding-bottom:env(safe-area-inset-bottom)}
 .side-nav a{min-height:52px}
 /* composer: textarea, then one tidy row (+, model/effort/mode, mic, send) that scrolls sideways if it must */
 .composer-wrap{padding:0 10px calc(6px + env(safe-area-inset-bottom))}
 html.kb-open .composer-wrap{padding-bottom:6px}
 .composer{width:100%;margin:4px 0 0;padding:10px 12px 8px;border-radius:16px}
 .cbar{flex-wrap:wrap;gap:0;margin-left:0}
 .cbar .sp{display:none}
 .cbar .cfoot{flex:1 1 0;min-width:0;overflow-x:auto;scrollbar-width:none;padding:0;gap:0;width:auto}
 .cbar .cfoot::-webkit-scrollbar{display:none}
 .cbar .cfoot .fw{flex:none;max-width:none;min-height:44px;padding:0 8px;font-size:13px}
 .cbar .cfoot .fw-model{max-width:150px}
 .cbar .cfoot .helptip{display:none}
 .cbar .cplus,.cbar .cmic{width:44px;height:44px;flex:none}
 .cbar #send-btn{min-height:44px;min-width:64px;margin-left:4px;border-radius:12px;flex:none}
 .cbar>.btn:not(#send-btn){flex:1 1 auto;margin-top:4px}
 .composer .chips{max-height:96px;overflow:auto}
 /* menus are bottom sheets */
 .wbmenu,.tab-pop,.ctxmenu,.mg-pop{position:fixed!important;left:0!important;right:0!important;top:auto!important;bottom:0!important;width:auto!important;min-width:0!important;max-width:none!important;max-height:72vh;max-height:72dvh;overflow:auto;border-radius:18px 18px 0 0!important;padding:8px 8px calc(10px + env(safe-area-inset-bottom))!important;box-shadow:0 -12px 40px rgb(0 0 0/.35)!important;z-index:70}
 .wbmenu button,.tab-pop button,.ctxmenu button,.mg-pop button,.tab-pop .it,.ctxmenu .it{min-height:46px;font-size:16px!important;padding:8px 12px!important;border-radius:10px}
 .wbmenu::before,.tab-pop::before,.ctxmenu::before,.mg-pop::before{content:"";display:block;width:36px;height:4px;border-radius:2px;background:var(--line2);margin:0 auto 6px}
 /* modals are sheets too (the command palette stays at the top, next to its keyboard) */
 .scrim:not(.top2){align-items:flex-end;padding:0;z-index:80}
 .scrim:not(.top2) .modal{width:100%;max-width:none;max-height:92vh;max-height:92dvh;overflow:auto;border-radius:20px 20px 0 0;padding:18px 16px calc(16px + env(safe-area-inset-bottom));animation:none}
 .scrim.top2{padding:calc(8px + env(safe-area-inset-top)) 8px 8px}
 .modal .acts{gap:8px}
 .modal .acts .btn{flex:1 1 auto}
 .toast{max-width:calc(100vw - 24px);bottom:calc(120px + env(safe-area-inset-bottom))}
 html.kb-open .toast{bottom:16px}
 /* pages */
 .pane{padding:16px 14px calc(28px + env(safe-area-inset-bottom))}
 .pane .head,.pane .formrow,.pane .filters{flex-wrap:wrap}
 .pane .search,.pane select{max-width:100%}
 .seg{max-width:100%;overflow-x:auto;scrollbar-width:none;flex-wrap:nowrap}.seg::-webkit-scrollbar{display:none}
 .seg button{white-space:nowrap;flex:none}
 .tabs{overflow-x:auto;scrollbar-width:none;flex-wrap:nowrap}.tabs::-webkit-scrollbar{display:none}
 .tabs>*{flex:none;white-space:nowrap}
 .strip{grid-template-columns:repeat(2,minmax(0,1fr))}
 .cols,.cols.eq,.grid2,.grid2.eq{grid-template-columns:minmax(0,1fr)}
 /* slide-over and full-screen panels: drawer from the right with a scrim, full-height sheets for tools */
 .rpanel{width:min(92vw,400px);padding-bottom:env(safe-area-inset-bottom);padding-top:env(safe-area-inset-top)}
 .wpanel,.wpanel.has{height:100vh;height:100dvh;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
 .phead .mini,.ptabs .mini{min-height:44px}
 .mclose{position:absolute;top:calc(6px + env(safe-area-inset-top));right:6px;z-index:5;width:44px;height:44px;border:0;border-radius:12px;background:var(--raised);color:var(--ink);font-size:20px;line-height:1;display:grid;place-items:center;cursor:pointer;padding:0}
 #rpbody{padding-top:4px}
 body.drawer-right #rpbody>:first-child{padding-right:52px}
 .feed{padding-left:14px;padding-right:14px}
}
.mclose{display:none}
@media (max-width:900px){body.drawer-right .mclose{display:grid}}
@media (max-width:480px){
 .top .brand{display:none}
 .top .tbi{width:40px}
 html.is-phone .tbi[data-wb=terminal],html.is-phone .tbi[data-wb=browser]{display:none}
 .strip,.vz-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:900px) and (orientation:landscape) and (max-height:500px){
 .side-nav a .lbl{display:none}.side-nav a{min-height:40px}
 .top{height:calc(44px + env(safe-area-inset-top))}
}
"""

MOBILE_JS = r"""
// ---- phone shell: keep the screen exactly the visible area while the keyboard is open ----
(() => {
  const root = document.documentElement, vv = window.visualViewport;
  const loop = /^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?)$/.test(location.hostname);
  if (!loop) root.classList.add('is-phone');   // the Terminal and Browser tools run on the computer, not on a paired phone
  let raf = 0;
  const fit = () => {
    raf = 0;
    const h = vv ? vv.height : innerHeight;
    const kb = vv ? Math.max(0, innerHeight - vv.height - vv.offsetTop) : 0;
    const open = kb > 120 && matchMedia('(max-width:900px),(pointer:coarse)').matches;
    root.style.setProperty('--vvh', Math.round(h) + 'px');
    root.classList.toggle('kb-open', open);
    if (open && (window.scrollY || (vv && vv.offsetTop))) window.scrollTo(0, 0);   // the page itself never scrolls; the sheet follows the visible area
  };
  const sched = () => { if (!raf) raf = requestAnimationFrame(fit); };
  if (vv) { vv.addEventListener('resize', sched); vv.addEventListener('scroll', sched); }
  addEventListener('orientationchange', () => setTimeout(sched, 250));
  addEventListener('resize', sched);
  document.addEventListener('focusin', (e) => { if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) setTimeout(() => { sched(); try { e.target.scrollIntoView({ block: 'nearest' }); } catch (_) {} }, 300); });
  sched();
  // the progress slide-over gets one visible close button (it was only closable by tapping the dimmed strip)
  const addClose = () => {
    const rp = document.getElementById('rpanel');
    if (!rp || rp.querySelector('.mclose')) return !!rp;
    const b = document.createElement('button'); b.type = 'button'; b.className = 'mclose'; b.setAttribute('aria-label', 'Close panel'); b.textContent = '\u2715';
    b.addEventListener('click', () => { if (typeof togglePanel === 'function') togglePanel('right'); else document.body.classList.remove('drawer-right'); });
    rp.append(b); return true;
  };
  if (!addClose()) { const mo = new MutationObserver(() => { if (addClose()) mo.disconnect(); }); mo.observe(document.body, { childList: true, subtree: true }); }
})();
"""
