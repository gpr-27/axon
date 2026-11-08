"""Client script: a compact VT100/xterm screen-grid emulator and the Terminal tab (real PTY on this computer)."""

JS_TERM = r"""
function curTheme() { const t = document.documentElement.getAttribute('data-theme'); return t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
// ---------- terminal emulator ----------
const ANSI16 = ['var(--muted)', 'var(--bad)', 'var(--ok)', 'var(--warn)', 'var(--info)', 'var(--accent)', 'var(--primary)', 'var(--ink2)',
  'var(--ink2)', 'var(--bad)', 'var(--ok)', 'var(--warn)', 'var(--info)', 'var(--accent)', 'var(--primary)', 'var(--ink)'];
function xcolor(n) {
  if (n < 16) return ANSI16[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return 'rgb(' + v + ',' + v + ',' + v + ')'; }
  n -= 16; const s = [0, 95, 135, 175, 215, 255]; return 'rgb(' + s[Math.floor(n / 36)] + ',' + s[Math.floor(n / 6) % 6] + ',' + s[n % 6] + ')';
}
class VT {
  constructor(cols, rows, onOut) { this.onOut = onOut; this.sb = []; this.dec = new TextDecoder('utf-8'); this.title = ''; this.reset(cols, rows); }
  reset(cols, rows) {
    this.cols = cols; this.rows = rows; this.main = this.blank(); this.alt = null; this.g = this.main; this.x = 0; this.y = 0; this.pend = false;
    this.top = 0; this.bot = rows - 1; this.a = this.fresh(); this.saved = null; this.mode = { wrap: true, cur: true, appCur: false, paste: false, alt: false, origin: false };
    this.st = 0; this.buf = ''; this.dirty = new Set(); for (let i = 0; i < rows; i++) this.dirty.add(i);
  }
  fresh() { return { fg: null, bg: null, b: false, d: false, i: false, u: false, r: false }; }
  cell(ch) { return { c: ch || ' ', fg: this.a.fg, bg: this.a.bg, b: this.a.b, d: this.a.d, i: this.a.i, u: this.a.u, r: this.a.r }; }
  blankRow() { const r = []; for (let i = 0; i < this.cols; i++) r.push({ c: ' ', fg: null, bg: null }); return r; }
  blank() { const g = []; for (let i = 0; i < this.rows; i++) g.push(this.blankRow()); return g; }
  resize(cols, rows) {
    if (cols === this.cols && rows === this.rows) return;
    const fix = (g) => { while (g.length < rows) g.push(this.blankRow()); while (g.length > rows) { const r = g.shift(); if (g === this.main) this.sb.push(r); } for (const r of g) { while (r.length < cols) r.push({ c: ' ', fg: null, bg: null }); r.length = cols; } return g; };
    this.cols = cols; this.rows = rows; this.main = fix(this.main); if (this.alt) this.alt = fix(this.alt); this.g = this.mode.alt ? this.alt : this.main;
    this.top = 0; this.bot = rows - 1; this.x = Math.min(this.x, cols - 1); this.y = Math.min(this.y, rows - 1); this.markAll();
  }
  markAll() { for (let i = 0; i < this.rows; i++) this.dirty.add(i); }
  scrollUp(n) { for (let k = 0; k < n; k++) { const r = this.g.splice(this.top, 1)[0]; if (this.g === this.main && this.top === 0) { this.sb.push(r); if (this.sb.length > 3000) this.sb.shift(); } this.g.splice(this.bot, 0, this.blankRow()); } for (let i = this.top; i <= this.bot; i++) this.dirty.add(i); }
  scrollDown(n) { for (let k = 0; k < n; k++) { this.g.splice(this.bot, 1); this.g.splice(this.top, 0, this.blankRow()); } for (let i = this.top; i <= this.bot; i++) this.dirty.add(i); }
  lf() { if (this.y === this.bot) this.scrollUp(1); else if (this.y < this.rows - 1) this.y++; }
  put(ch) {
    if (this.pend && this.mode.wrap) { this.x = 0; this.lf(); }
    this.pend = false; this.g[this.y][this.x] = this.cell(ch); this.dirty.add(this.y);
    if (this.x >= this.cols - 1) this.pend = true; else this.x++;
  }
  write(bytes) {
    const text = this.dec.decode(bytes, { stream: true });
    for (const ch of text) this.feed(ch);
  }
  feed(ch) {
    const code = ch.codePointAt(0);
    if (this.st === 0) {
      if (ch === '\x1b') { this.st = 1; this.buf = ''; return; }
      if (code < 32 || code === 127) { this.ctl(ch); return; }
      this.put(ch); return;
    }
    if (this.st === 1) {                                  // after ESC
      if (ch === '[') { this.st = 2; this.buf = ''; return; }
      if (ch === ']') { this.st = 3; this.buf = ''; return; }
      if (ch === '(' || ch === ')' || ch === '#' || ch === '%') { this.st = 4; return; }
      this.st = 0;
      if (ch === '7') this.save(); else if (ch === '8') this.restore(); else if (ch === 'M') { if (this.y === this.top) this.scrollDown(1); else if (this.y > 0) this.y--; }
      else if (ch === 'D') this.lf(); else if (ch === 'E') { this.x = 0; this.lf(); } else if (ch === 'c') this.reset(this.cols, this.rows);
      return;
    }
    if (this.st === 4) { this.st = 0; return; }
    if (this.st === 3) {                                  // OSC
      if (ch === '\x07' || (ch === '\\' && this.buf.endsWith('\x1b'))) { this.st = 0; const m = this.buf.match(/^[02];(.*?)\x1b?$/); if (m) this.title = m[1]; return; }
      this.buf += ch; if (this.buf.length > 2000) this.st = 0; return;
    }
    // CSI
    if ((code >= 0x30 && code <= 0x3f) || (code >= 0x20 && code <= 0x2f)) { this.buf += ch; return; }
    this.st = 0; this.csi(this.buf, ch);
  }
  ctl(ch) {
    if (ch === '\n' || ch === '\x0b' || ch === '\x0c') { this.lf(); this.pend = false; }
    else if (ch === '\r') { this.x = 0; this.pend = false; }
    else if (ch === '\b') { if (this.x > 0) this.x--; this.pend = false; }
    else if (ch === '\t') { this.x = Math.min(this.cols - 1, (this.x + 8) & ~7); this.pend = false; }
  }
  save() { this.saved = { x: this.x, y: this.y, a: Object.assign({}, this.a) }; }
  restore() { if (this.saved) { this.x = this.saved.x; this.y = this.saved.y; this.a = Object.assign({}, this.saved.a); this.pend = false; } }
  clearRow(y, from, to) { const r = this.g[y]; for (let i = from; i <= to && i < this.cols; i++) r[i] = { c: ' ', fg: null, bg: this.a.bg }; this.dirty.add(y); }
  csi(buf, fin) {
    const priv = /^[?<>=]/.test(buf) ? buf[0] : ''; const nums = (priv ? buf.slice(1) : buf).split(';').map(s => (s === '' ? null : parseInt(s, 10)));
    const n = (i, d) => (nums[i] == null || isNaN(nums[i]) ? d : nums[i]); const clampX = () => { this.x = Math.max(0, Math.min(this.cols - 1, this.x)); this.pend = false; }; const clampY = () => { this.y = Math.max(0, Math.min(this.rows - 1, this.y)); };
    switch (fin) {
      case 'A': this.y -= n(0, 1) || 1; this.y = Math.max(this.top, this.y); this.pend = false; break;
      case 'B': case 'e': this.y += n(0, 1) || 1; this.y = Math.min(this.bot, this.y); this.pend = false; break;
      case 'C': case 'a': this.x += n(0, 1) || 1; clampX(); break;
      case 'D': this.x -= n(0, 1) || 1; clampX(); break;
      case 'E': this.y += n(0, 1) || 1; this.x = 0; clampY(); this.pend = false; break;
      case 'F': this.y -= n(0, 1) || 1; this.x = 0; clampY(); this.pend = false; break;
      case 'G': case '`': this.x = n(0, 1) - 1; clampX(); break;
      case 'd': this.y = n(0, 1) - 1; clampY(); break;
      case 'H': case 'f': this.y = n(0, 1) - 1; this.x = n(1, 1) - 1; clampX(); clampY(); break;
      case 'J': { const m = n(0, 0); if (m === 0) { this.clearRow(this.y, this.x, this.cols); for (let i = this.y + 1; i < this.rows; i++) this.clearRow(i, 0, this.cols); } else if (m === 1) { for (let i = 0; i < this.y; i++) this.clearRow(i, 0, this.cols); this.clearRow(this.y, 0, this.x); } else { for (let i = 0; i < this.rows; i++) this.clearRow(i, 0, this.cols); if (m === 3) this.sb = []; } break; }
      case 'K': { const m = n(0, 0); if (m === 0) this.clearRow(this.y, this.x, this.cols); else if (m === 1) this.clearRow(this.y, 0, this.x); else this.clearRow(this.y, 0, this.cols); break; }
      case 'L': { const k = Math.min(n(0, 1) || 1, this.bot - this.y + 1); for (let i = 0; i < k; i++) { this.g.splice(this.bot, 1); this.g.splice(this.y, 0, this.blankRow()); } for (let i = this.y; i <= this.bot; i++) this.dirty.add(i); break; }
      case 'M': { const k = Math.min(n(0, 1) || 1, this.bot - this.y + 1); for (let i = 0; i < k; i++) { this.g.splice(this.y, 1); this.g.splice(this.bot, 0, this.blankRow()); } for (let i = this.y; i <= this.bot; i++) this.dirty.add(i); break; }
      case 'P': { const r = this.g[this.y]; const k = n(0, 1) || 1; r.splice(this.x, k); while (r.length < this.cols) r.push({ c: ' ', fg: null, bg: null }); this.dirty.add(this.y); break; }
      case '@': { const r = this.g[this.y]; const k = n(0, 1) || 1; for (let i = 0; i < k; i++) r.splice(this.x, 0, { c: ' ', fg: null, bg: null }); r.length = this.cols; this.dirty.add(this.y); break; }
      case 'X': this.clearRow(this.y, this.x, this.x + (n(0, 1) || 1) - 1); break;
      case 'S': this.scrollUp(n(0, 1) || 1); break;
      case 'T': this.scrollDown(n(0, 1) || 1); break;
      case 'r': this.top = Math.max(0, n(0, 1) - 1); this.bot = Math.min(this.rows - 1, n(1, this.rows) - 1); if (this.top >= this.bot) { this.top = 0; this.bot = this.rows - 1; } this.x = 0; this.y = 0; this.pend = false; break;
      case 's': this.save(); break;
      case 'u': this.restore(); break;
      case 'm': this.sgr(nums); break;
      case 'n': if (n(0, 0) === 6) this.onOut('\x1b[' + (this.y + 1) + ';' + (this.x + 1) + 'R'); else if (n(0, 0) === 5) this.onOut('\x1b[0n'); break;
      case 'c': if (!priv) this.onOut('\x1b[?1;2c'); break;
      case 'h': case 'l': {
        const on = fin === 'h';
        for (const v of nums) {
          if (priv !== '?') continue;
          if (v === 1) this.mode.appCur = on; else if (v === 7) this.mode.wrap = on; else if (v === 25) this.mode.cur = on; else if (v === 2004) this.mode.paste = on;
          else if (v === 1049 || v === 1047 || v === 47) this.setAlt(on, v === 1049);
        }
        break;
      }
      default: break;
    }
  }
  setAlt(on, saveCur) {
    if (on === this.mode.alt) return;
    if (on) { if (saveCur) this.save(); this.alt = this.blank(); this.g = this.alt; this.mode.alt = true; }
    else { this.g = this.main; this.alt = null; this.mode.alt = false; if (saveCur) this.restore(); }
    this.top = 0; this.bot = this.rows - 1; this.markAll();
  }
  sgr(nums) {
    if (!nums.length) nums = [0];
    for (let i = 0; i < nums.length; i++) {
      const v = nums[i] == null ? 0 : nums[i]; const a = this.a;
      if (v === 0) this.a = this.fresh(); else if (v === 1) a.b = true; else if (v === 2) a.d = true; else if (v === 3) a.i = true; else if (v === 4) a.u = true; else if (v === 7) a.r = true;
      else if (v === 22) { a.b = false; a.d = false; } else if (v === 23) a.i = false; else if (v === 24) a.u = false; else if (v === 27) a.r = false;
      else if (v >= 30 && v <= 37) a.fg = xcolor(v - 30); else if (v === 39) a.fg = null; else if (v >= 40 && v <= 47) a.bg = xcolor(v - 40); else if (v === 49) a.bg = null;
      else if (v >= 90 && v <= 97) a.fg = xcolor(v - 90 + 8); else if (v >= 100 && v <= 107) a.bg = xcolor(v - 100 + 8);
      else if (v === 38 || v === 48) {
        const key = v === 38 ? 'fg' : 'bg';
        if (nums[i + 1] === 5) { a[key] = xcolor(nums[i + 2] || 0); i += 2; } else if (nums[i + 1] === 2) { a[key] = 'rgb(' + (nums[i + 2] || 0) + ',' + (nums[i + 3] || 0) + ',' + (nums[i + 4] || 0) + ')'; i += 4; }
      }
    }
  }
}

// ----- minimum contrast: every cell's text is checked against that cell's own background -----
const _rgbCache = new Map(), _fixCache = new Map(); let _cacheTheme = '', _probe = null;
function toRGB(str) {
  let m = /^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/.exec(str); if (m) return [+m[1], +m[2], +m[3]];
  const key = str; if (_rgbCache.has(key)) return _rgbCache.get(key);
  if (!_probe) { _probe = document.createElement('span'); _probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none'; }
  const scr = document.querySelector('.tscreen') || document.body; scr.append(_probe); _probe.style.color = ''; _probe.style.color = str;
  m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(_probe).color); _probe.remove();
  const v = m ? [+m[1], +m[2], +m[3]] : null; _rgbCache.set(key, v); return v;
}
function lum(c) { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); }
function ratio(a, b) { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
function mixTo(c, t, k) { return [Math.round(c[0] + (t[0] - c[0]) * k), Math.round(c[1] + (t[1] - c[1]) * k), Math.round(c[2] + (t[2] - c[2]) * k)]; }
// Returns a foreground the reader can see on bg: unchanged when it already has 4.5:1, else nudged toward white or black.
function readable(fg, bg) {
  const th = curTheme(); if (th !== _cacheTheme) { _cacheTheme = th; _rgbCache.clear(); _fixCache.clear(); }
  const key = fg + '|' + bg; if (_fixCache.has(key)) return _fixCache.get(key);
  const f = toRGB(fg), b = toRGB(bg); let out = fg;
  if (f && b && ratio(f, b) < 4.5) {
    const white = [255, 255, 255], black = [0, 0, 0]; const first = lum(b) < 0.4 ? white : black, second = first === white ? black : white;
    for (const t of [first, second]) { let hit = null; for (let k = 0.1; k <= 1.001; k += 0.05) { const c = mixTo(f, t, k); if (ratio(c, b) >= 4.5) { hit = c; break; } } if (hit) { out = 'rgb(' + hit.join(',') + ')'; break; } }
    if (out === fg) out = ratio(white, b) >= ratio(black, b) ? 'rgb(255,255,255)' : 'rgb(0,0,0)';
  }
  _fixCache.set(key, out); return out;
}
function vtRow(row, cursorX, showCursor) {
  const el = document.createElement('div'); el.className = 'trow';
  let run = null, text = '';
  const flush = () => { if (!run) return; const s = document.createElement('span'); s.textContent = text; if (run.css) s.setAttribute('style', run.css); if (run.cls) s.className = run.cls; el.append(s); run = null; text = ''; };
  for (let x = 0; x < row.length; x++) {
    const c = row[x]; const cur = showCursor && x === cursorX;
    let fg = c.fg, bg = c.bg; if (c.r) { const t = fg; fg = bg || 'var(--page)'; bg = t || 'var(--ink)'; }
    if (bg && !cur) fg = readable(fg || 'var(--ink)', bg); else if (fg && !cur) fg = readable(fg, 'var(--code-bg)');
    const css = (fg ? 'color:' + fg + ';' : '') + (bg ? 'background:' + bg + ';' : ''); const cls = (c.b ? 'tb ' : '') + (c.d ? 'td ' : '') + (c.i ? 'ti ' : '') + (c.u ? 'tu ' : '') + (cur ? 'tcur' : '');
    if (!run || run.css !== css || run.cls !== cls) { flush(); run = { css, cls }; }
    text += c.c;
  }
  flush(); return el;
}
function keySeq(e, vt) {
  const k = e.key; const app = vt.mode.appCur;
  const map = { Enter: '\r', Backspace: '\x7f', Tab: e.shiftKey ? '\x1b[Z' : '\t', Escape: '\x1b', ArrowUp: app ? '\x1bOA' : '\x1b[A', ArrowDown: app ? '\x1bOB' : '\x1b[B', ArrowRight: app ? '\x1bOC' : '\x1b[C', ArrowLeft: app ? '\x1bOD' : '\x1b[D',
    Home: app ? '\x1bOH' : '\x1b[H', End: app ? '\x1bOF' : '\x1b[F', PageUp: '\x1b[5~', PageDown: '\x1b[6~', Delete: '\x1b[3~', Insert: '\x1b[2~',
    F1: '\x1bOP', F2: '\x1bOQ', F3: '\x1bOR', F4: '\x1bOS', F5: '\x1b[15~', F6: '\x1b[17~', F7: '\x1b[18~', F8: '\x1b[19~', F9: '\x1b[20~', F10: '\x1b[21~', F11: '\x1b[23~', F12: '\x1b[24~' };
  if (map[k]) return (e.altKey ? '\x1b' : '') + map[k];
  if (e.ctrlKey && !e.metaKey && k.length === 1) { const c = k.toLowerCase().charCodeAt(0); if (c >= 97 && c <= 122) return String.fromCharCode(c - 96); if (k === '[') return '\x1b'; if (k === '\\') return '\x1c'; if (k === ']') return '\x1d'; if (k === ' ') return '\x00'; }
  if (e.altKey && !e.metaKey && !e.ctrlKey && k.length === 1) return '\x1b' + k;
  if (!e.ctrlKey && !e.metaKey && k.length >= 1 && [...k].length === 1) return k;
  return null;
}

// A terminal screen on a PTY, mounted into host: the Terminal tab and the side-panel tabs both use it.
function termView(host, tid, opts) {
  opts = opts || {};
  const T = { closed: false, vt: null, es: null, off: 0, id: tid };
  const screen = h('div', { class: 'tscreen', tabindex: '0', 'aria-label': 'Terminal. Everything you type goes to a shell on this computer.' });
  const bar = h('div', { class: 'ftoolbar' }, h('span', { class: 'warnchip' }, 'Full shell on this computer'), h('span', { class: 'muted tt' }, ''), h('span', { style: 'flex:1' }),
    h('button', { class: 'btn', onclick: async () => { await api('/api/term/close', { id: tid }).catch(() => { }); if (opts.onClose) opts.onClose(); } }, 'Close terminal'));
  if (opts.bar) host.append(bar, screen); else host.append(screen);
  const probe = h('span', { class: 'tprobe' }, 'M'.repeat(10)); screen.append(probe);
  const vt = new VT(100, 30, (s) => send(s)); T.vt = vt; let dirtyAll = true;
  const send = (data) => fetch('/api/term/input', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: tid, data }) }).catch(() => { });
  let rz; const fit = () => {
    const r = probe.getBoundingClientRect(); const cw = r.width / 10, ch = r.height || 18; const box = screen.getBoundingClientRect(); if (box.width < 60 || box.height < 30) return;
    const cols = Math.max(20, Math.floor((box.width - 16) / cw)), rows = Math.max(5, Math.floor((box.height - 8) / ch));
    if (cols !== vt.cols || rows !== vt.rows) { vt.resize(cols, rows); dirtyAll = true; clearTimeout(rz); rz = setTimeout(() => api('/api/term/resize', { id: tid, cols, rows }).catch(() => { }), 120); }
  };
  new ResizeObserver(fit).observe(screen); setTimeout(fit, 30);
  const paint = () => {
    if (T.closed) return;
    if (vt.dirty.size || dirtyAll) {
      const rows = [...screen.querySelectorAll('.trow')];
      if (dirtyAll || rows.length !== vt.rows) { screen.querySelectorAll('.trow').forEach(x => x.remove()); for (let y = 0; y < vt.rows; y++) screen.append(vtRow(vt.g[y], vt.x, vt.mode.cur && y === vt.y)); dirtyAll = false; }
      else { for (const y of vt.dirty) if (rows[y]) rows[y].replaceWith(vtRow(vt.g[y], vt.x, vt.mode.cur && y === vt.y)); }
      if (T.lastCur !== undefined && T.lastCur !== vt.y) { const rr = [...screen.querySelectorAll('.trow')]; if (rr[T.lastCur]) rr[T.lastCur].replaceWith(vtRow(vt.g[T.lastCur], vt.x, false)); if (rr[vt.y]) rr[vt.y].replaceWith(vtRow(vt.g[vt.y], vt.x, vt.mode.cur)); }
      T.lastCur = vt.y; vt.dirty.clear(); if (vt.title) { if (bar.querySelector('.tt')) bar.querySelector('.tt').textContent = vt.title; if (opts.onTitle) opts.onTitle(vt.title); }
    }
    requestAnimationFrame(paint);
  };
  requestAnimationFrame(paint);
  const connect = () => {
    if (T.closed) return;
    const es = new EventSource('/api/term/stream?id=' + encodeURIComponent(tid) + '&since=' + T.off); T.es = es;
    es.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.exit !== undefined) { es.close(); vt.write(new TextEncoder().encode('\r\n[the terminal ended with exit code ' + d.exit + ']\r\n')); return; }
      if (d.reset) { vt.reset(vt.cols, vt.rows); dirtyAll = true; }
      const bin = atob(d.b); const bytes = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      vt.write(bytes); T.off = Number(m.lastEventId) || T.off; vt.markAll();
    };
  };
  connect();
  screen.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') return;          // paste handled below
    if (e.metaKey) return;
    const s = keySeq(e, vt); if (s != null) { e.preventDefault(); send(s); }
  });
  screen.addEventListener('paste', (e) => { e.preventDefault(); const t = (e.clipboardData || window.clipboardData).getData('text'); if (t) send(vt.mode.paste ? '\x1b[200~' + t + '\x1b[201~' : t); });
  screen.addEventListener('click', () => screen.focus());
  setTimeout(() => screen.focus(), 60);
  return { T, screen, destroy() { T.closed = true; if (T.es) T.es.close(); screen.remove(); bar.remove(); }, focus() { screen.focus(); }, fit() { fit(); } };
}

// ---------- terminal tab ----------
async function viewTerminal(rest) {
  const tid = rest ? decodeURIComponent(rest) : '';
  return shell('terminal', async (pane, tab) => {
    let view = null; onLeave(() => { if (view) view.destroy(); });
    let avail; try { avail = await api('/api/term/list'); } catch (e) { pane.append(errorBox(e.message)); return; }
    if (!avail.available) { pane.append(h('div', { class: 'empty', style: 'padding:30px' }, h('strong', null, 'No real terminal on this system'), avail.reason)); return; }
    if (tid && !(avail.terminals || []).some(t => t.id === tid)) { pane.append(h('div', { class: 'empty', style: 'padding:30px' }, h('strong', null, 'This terminal has ended'), 'It was closed or the server restarted.', h('div', { class: 'formrow' }, h('button', { class: 'btn primary', onclick: () => { closeTab(tab); location.hash = '#/terminal'; } }, 'Open a new terminal')))); return; }
    if (!tid) {
      const start = async (kind, initial) => { try { const s = await api('/api/term/start', { kind, cols: 110, rows: 32, initial: initial || '', theme: curTheme() }); location.hash = '#/terminal/' + encodeURIComponent(s.id); tab.hash = location.hash; closeTab(tab); location.hash = '#/terminal/' + encodeURIComponent(s.id); } catch (e) { toast(e.message); } };
      const list = avail.terminals.filter(t => t.running);
      pane.append(h('div', { class: 'empty', style: 'padding:30px;max-width:70ch' }, h('strong', null, 'Open a terminal'),
        h('p', null, 'This is a full shell on this computer, in ', h('b', null, avail.cwd), '. Anything you type runs with your own permissions, with no shell guard and no approval step. Only open it if you want that.'),
        h('div', { class: 'formrow' }, h('button', { class: 'btn primary', onclick: () => start('shell') }, 'Open a shell'), h('button', { class: 'btn', onclick: () => start('axon') }, 'Axon in a terminal')),
        h('p', { class: 'muted' }, 'Axon in a terminal runs the real axon program, so every picker and terminal feature works.'),
        list.length ? h('div', null, h('div', { class: 'lbl' }, 'Running terminals'), ...list.map(t => h('a', { class: 'row', href: '#/terminal/' + encodeURIComponent(t.id) }, h('span', { class: 'dot running' }), h('span', { class: 't' }, t.label + '  ' + t.cwd)))) : null));
      return;
    }
    view = termView(pane, tid, { bar: true, onClose: () => closeTab(tab) });
  }, { wide: true, persist: true, key: 'term:' + tid, hash: '#/terminal' + (tid ? '/' + encodeURIComponent(tid) : ''), title: tid ? 'Terminal' : 'Terminal' });
}
function openInTerminal(line) {
  api('/api/term/start', { kind: 'axon', cols: 110, rows: 32, initial: line, theme: curTheme() }).then((s) => { location.hash = '#/terminal/' + encodeURIComponent(s.id); }).catch((e) => toast(e.message));
}
"""
