"""The chat in the app, round two: actions on messages (copy, edit and resend, regenerate, bookmark, branch), search
inside a chat, bookmarks, and the one dialog that exports or shares a chat.

* ``handle_get`` / ``handle_post``: the ``/api/chatx/*`` routes (called by ``dash_api`` after its Host, cookie, Origin
  and CSRF checks). They reuse ``/rewind`` (``session_cmds.apply_rewind``), ``/branch`` (``chat_fork``) and the
  export/share code in ``chat_export``; nothing here keeps its own copy of those.
* ``share_preview``: the Share dialog's preview (``dashboard._share_preview`` calls it for a chat).
* ``apply_rewinds``: drops from a replayed event log the turns a rewind removed.
* ``JS_CHATX`` / ``CHATX_CSS``: the page script and style, appended to the app's.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features import chat_export as cx
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer

REWIND_MODES = ("conversation", "code_and_conversation")
MAX_QUERY = 200


def _s(body: dict[str, Any], key: str, limit: int = 300) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


def _num(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise DashError(f"'{name}' must be a number")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise DashError(f"'{name}' must be a number") from None


def _redact(text: str) -> str:
    from axon.features.dashboard import redact_text
    return redact_text(text)


# ── the saved chat ───────────────────────────────────────────────────────────

def _load(srv: "DashServer", sid: str) -> tuple[Any, Any]:
    from axon.features.dashboard import load_transcript
    ref = srv.index.lookup(sid)
    try:
        return ref, load_transcript(ref.path)
    except OSError:
        raise DashError("session not found", 404) from None


def _prompt_times(srv: "DashServer", ref: Any) -> dict[int, int]:
    """round(ts, 5) -> prompt number, for the prompts still in the conversation (a rewind or a rolled-back turn drops some)."""
    from axon.session.store import SessionStore
    ws = Path(ref.workspace) if ref.workspace and Path(ref.workspace).is_dir() else srv.workspace
    try:
        times = SessionStore(ws, session_dir=ref.path.parent.parent).user_message_times(ref.id)
    except Exception:
        times = []
    return {round(t, 5): n + 1 for n, t in enumerate(times) if t}


def message_map(srv: "DashServer", sid: str) -> dict[str, Any]:
    ref, tr = _load(srv, sid)
    turns = cx.turns_of(tr.entries, _prompt_times(srv, ref))
    rows = [{"id": b["id"], "kind": b["kind"], "n": b["n"], "ts": b["ts"], "snippet": b.get("snippet", ""),
             "note": b.get("note", ""), "created": b.get("created")} for b in cx.live_bookmarks(ref.path.parent, turns)]
    live = {(b["kind"], b["n"]): b["id"] for b in rows}
    out = []
    for t in turns:
        out.append({"n": t["n"], "prompt": t["prompt"], "ts": t["ts"], "removes": t["removes"],
                    "text": _redact(" ".join(t["user"].split())[:160]), "has_reply": t["reply_ts"] is not None,
                    "reply_ts": t["reply_ts"], "reply": _redact(" ".join(" ".join(t["reply"]).split())[:160]),
                    "bookmark": live.get(("assistant", t["n"]))})
    return {"session": sid, "turns": out, "bookmarks": rows}


def message_text(srv: "DashServer", sid: str, n: int, which: str) -> dict[str, Any]:
    ref, tr = _load(srv, sid)
    turns = cx.turns_of(tr.entries, {})
    if not 0 <= n < len(turns):
        raise DashError("no such message", 404)
    t = turns[n]
    text = t["user"] if which == "user" else "\n\n".join(t["reply"])
    return {"text": _redact(text[:2_000_000]), "n": n, "which": which}


def search_chat(srv: "DashServer", sid: str, q: str) -> dict[str, Any]:
    _ref, tr = _load(srv, sid)
    return cx.search_items(cx.items_from_entries(tr.entries), q[:MAX_QUERY])


# ── bookmarks ────────────────────────────────────────────────────────────────

def add_bookmark(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    sid = _s(body, "session")
    ref, tr = _load(srv, sid)
    n, kind = _num(body.get("n"), "n"), "user" if body.get("kind") == "user" else "assistant"
    turns = cx.turns_of(tr.entries, {})
    if not 0 <= n < len(turns):
        raise DashError("no such message", 404)
    t = turns[n]
    ts = t["ts"] if kind == "user" else t["reply_ts"]
    if not isinstance(ts, (int, float)):
        raise DashError("That reply is not saved yet. Bookmark it when the turn has finished.", 409)
    snippet = t["user"] if kind == "user" else " ".join(t["reply"])
    try:
        row, created = cx.add_bookmark(ref.path.parent, ts, kind, snippet, _s(body, "note", 200) if body.get("note") else "", _redact)
    except ValueError as e:
        raise DashError(str(e), 409) from None
    return {"bookmark": row, "created": created}


def remove_bookmark(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    ref = srv.index.lookup(_s(body, "session"))
    return {"ok": cx.remove_bookmark(ref.path.parent, _s(body, "id", 64))}


# ── rewind ───────────────────────────────────────────────────────────────────

def _rewindable(srv: "DashServer", sid: str) -> Any:
    rt = srv.runtime(sid, create=True)
    if rt is None:
        raise DashError("no such session", 404)
    if rt.external:
        raise DashError("This chat is open in a terminal, so it is rewound there with /rewind.", 409)
    bind = getattr(rt.agent, "_bind_checkpoints", None)       # a chat resumed from disk has not loaded its checkpoints until a turn runs
    if callable(bind):
        bind()
    return rt


def _point(rt: Any, prompt: int) -> tuple[Any, list[str]]:
    """(the rewind point, every file Axon edited from that prompt on: a code restore puts all of them back)."""
    from axon.commands.session_cmds import rewind_points
    pts = rewind_points(rt.agent)
    pt = next((p for p in pts if p.index == prompt), None)
    if pt is None:
        raise DashError("That message is no longer in the conversation. Reload the chat.", 404)
    return pt, sorted({f for p in pts if p.index >= prompt for f in p.files})


def rewind_preview(srv: "DashServer", sid: str, prompt: int) -> dict[str, Any]:
    rt = _rewindable(srv, sid)
    pt, touched = _point(rt, prompt)
    msg = rt.agent.conversation.messages[pt.message_index]
    content = msg.get("content") if isinstance(msg, dict) else None
    attach = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") not in ("text", None) for b in content)
    ws = str(rt.workspace)
    names = []
    for f in touched:
        try:
            names.append(os.path.relpath(f, ws) if f.startswith(ws) else f)
        except ValueError:
            names.append(f)
    clash: list[str] = []
    cp = getattr(rt.agent, "checkpoints", None)
    if cp is not None and pt.ts:
        from axon.commands.session_cmds import _code_conflicts
        try:
            clash = _code_conflicts(cp, pt.ts)
        except Exception:
            clash = []
    return {"prompt": pt.index, "text": _redact(pt.prompt[:300]), "files": names[:200], "files_changed": len(touched),
            "conflicts": clash[:50],
            "messages_after": len(rt.agent.conversation.messages) - pt.message_index, "busy": rt.busy, "attachment": attach,
            "can_restore_files": bool(getattr(rt.agent, "checkpoints", None)) and bool(pt.ts)}


def rewind(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.commands.session_cmds import apply_rewind
    sid, prompt = _s(body, "session"), _num(body.get("prompt"), "prompt")
    mode = _s(body, "mode", 40)
    if mode not in REWIND_MODES:
        raise DashError("mode must be conversation or code_and_conversation")
    rt = _rewindable(srv, sid)
    if rt.busy:
        raise DashError("Axon is working. Stop the turn first, then rewind.", 409)
    pt, _touched = _point(rt, prompt)
    cut = None
    log = rt.event_log()
    if log is not None and pt.ts:
        ev = [r for r in log.read(0, 1_000_000) if r.get("type") == "user" and float(r.get("ts") or 0) <= pt.ts + 0.001]
        cut = ev[-1]["seq"] if ev else None
    from axon.commands.session_cmds import RewindConflict
    try:
        res = apply_rewind(rt.agent, prompt, mode, force=body.get("overwrite") is True)
    except RewindConflict as e:                # files changed outside Axon: nothing was touched, and the page says which
        raise DashError(str(e), 409) from None
    except ValueError as e:
        raise DashError(str(e), 404) from None
    rt.hub.emit("rewound", {"prompt": prompt, "mode": mode, "cut_seq": cut, "client": _s(body, "client", 40) if body.get("client") else ""})
    return {"ok": True, "removed": res["removed"], "reverted": [_redact(str(x)) for x in res["reverted"]], "session": rt.session_id}


def apply_rewinds(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The replayed events without the turns a later ``rewound`` event removed."""
    cuts = [(int(r["seq"]), int(r["data"]["cut_seq"])) for r in events
            if r.get("type") == "rewound" and isinstance(r.get("data"), dict) and isinstance(r["data"].get("cut_seq"), int)]
    if not cuts:
        return events
    drop = [(c, s) for s, c in cuts]
    return [r for r in events if not any(a <= int(r.get("seq", 0)) < b for a, b in drop)]


# ── sharing and exporting ────────────────────────────────────────────────────

def share_preview(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    """Build the file for the Share dialog: secrets removed and counted, tool details optional, kept in memory for the
    preview and the download. Nothing is written to disk and nothing is sent anywhere."""
    from axon.features.dashboard import GIST_MAX_BYTES, redact_text
    ref, tr = _load(srv, str(body.get("id", "")))
    fmt = str(body.get("format") or "html")
    if fmt not in cx.FORMATS:
        raise DashError("format must be md, html, pdf or json")
    tools = body.get("tools") is not False
    title = tr.title or (ref.meta.first_prompt if ref.meta else ref.id)
    out = cx.build(cx.items_from_entries(tr.entries), {"id": ref.id, "title": title, "model": tr.model}, fmt, tools, True, redact_text)
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", out["filename"])
    sha = srv.store_export(name, out["content"])
    size = len(out["content"].encode("utf-8"))
    return {"filename": name, "sha256": sha, "bytes": size, "content": out["content"], "format": fmt, "tools": tools,
            "redacted": out["redacted"], "messages": out["messages"], "mime": out["mime"],
            "gh": bool(shutil.which("gh")), "gist_ok": size <= GIST_MAX_BYTES,
            "gist_note": "" if size <= GIST_MAX_BYTES else "Too large for a gist (1 MB limit). Download it instead."}


MIME_BY_EXT = {".html": "text/html; charset=utf-8", ".md": "text/markdown; charset=utf-8", ".json": "application/json; charset=utf-8"}


# ── routes ───────────────────────────────────────────────────────────────────

def handle_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any:
    sid = qs.get("session", "")
    if route == "/api/chatx/map":
        return message_map(srv, sid)
    if route == "/api/chatx/message":
        return message_text(srv, sid, _num(qs.get("n"), "n"), "user" if qs.get("which") == "user" else "reply")
    if route == "/api/chatx/search":
        return search_chat(srv, sid, qs.get("q", ""))
    if route == "/api/chatx/rewind_preview":
        return rewind_preview(srv, sid, _num(qs.get("prompt"), "prompt"))
    raise DashError("not found", 404)


def handle_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any:
    if route == "/api/chatx/bookmark":
        return add_bookmark(srv, body)
    if route == "/api/chatx/bookmark/remove":
        return remove_bookmark(srv, body)
    if route == "/api/chatx/rewind":
        return rewind(srv, body)
    raise DashError("not found", 404)


# ── the page ─────────────────────────────────────────────────────────────────

CHATX_CSS = r"""
.chat-main{position:relative}
.feed>.entry{position:relative}
.mxa{position:absolute;right:6px;display:flex;flex-wrap:wrap;gap:2px;max-width:calc(100% - 12px);padding:2px;background:var(--surface);border:1px solid var(--line);border-radius:6px;opacity:0;z-index:6}      /* above the sticky turn header (z-index 4) */
.entry:hover>.mxa,.entry:focus-within>.mxa,.mxa:focus-within{opacity:1}
.entry.a.live>.mxa{display:none}
.entry.a>.mxa{bottom:-28px}      /* in the gap under the reply, so no text is covered */
.entry.u>.mxa{top:50%;transform:translateY(-50%)}      /* the script moves it to the left of the bubble */
@media (max-width:560px){.feed>.entry>.mxa{position:static;transform:none;margin:6px 0 0;opacity:1;width:max-content;max-width:100%}}
@media (hover:none){.mxa{opacity:1}}
.mxa button{background:none;border:0;border-radius:4px;padding:2px 8px;font-size:12px;color:var(--ink2);cursor:pointer;white-space:nowrap}
.mxa button:hover,.mxa button:focus-visible{background:var(--raised);color:var(--ink)}
.mxa button[aria-pressed=true]{color:var(--primary)}
.mxa button[disabled]{opacity:.45;cursor:default}
.cx-edit{width:100%;min-height:84px;max-height:50vh;resize:vertical;background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:8px 10px}
.cx-edit:focus{border-color:var(--primary);outline:0}
.cx-editbar{display:flex;gap:8px;margin-top:6px}
.cx-find{position:absolute;top:8px;right:16px;z-index:6;display:flex;align-items:center;gap:6px;flex-wrap:wrap;max-width:calc(100% - 32px);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:6px 8px}
.cx-find input{width:min(240px,50vw);background:var(--page);border:1px solid var(--line);border-radius:5px;padding:4px 8px}
.cx-find input:focus{border-color:var(--primary);outline:0}
.cx-find .cnt{font-size:12px;color:var(--ink2);min-width:64px}
.cx-find .more{flex-basis:100%;font-size:12px;color:var(--ink2)}
.cx-find .more button{margin-left:6px}
::highlight(cx-match){background:color-mix(in srgb,var(--warn) 38%,transparent);color:inherit}
::highlight(cx-cur){background:color-mix(in srgb,var(--primary) 60%,transparent);color:inherit}
.cx-flash{outline:2px solid var(--primary);outline-offset:2px;border-radius:4px}
.cx-dlg input[type=radio],.cx-share input[type=radio],.cx-share input[type=checkbox]{width:16px;height:16px;margin:2px 0 0;padding:0;flex:none;accent-color:var(--primary)}
.cx-dlg p{margin:.4em 0}
.cx-dlg label{display:flex;gap:8px;align-items:flex-start;margin:6px 0;cursor:pointer}
.cx-dlg ul{margin:.2em 0 .4em 1.2em;padding:0;color:var(--ink2);font-size:13px}
.cx-share .fmt{display:flex;flex-wrap:wrap;gap:6px 14px;margin:8px 0}
.cx-share .fmt label{display:flex;gap:6px;align-items:center;cursor:pointer}
.cx-share iframe{width:100%;height:min(46vh,420px);border:1px solid var(--line);border-radius:5px;background:#fff}
.cx-share .sum{margin:8px 0;font-size:13px}
.cx-share .sum b{font-weight:600}
"""

JS_CHATX = r"""
// ---------- chat extras: actions on messages, find, bookmarks, export ----------
function cxNorm(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
function cxEnc(C) { return encodeURIComponent(C.sid || ''); }
function cxClient(C) { return C.cxId || (C.cxId = Math.random().toString(36).slice(2, 12)); }
async function cxMap(C, force) {
  const x = C.cx || (C.cx = {});
  if (!force && x.map && Date.now() - x.at < 5000) return x.map;
  if (x.p) return x.p;
  x.p = api('/api/chatx/map?session=' + cxEnc(C)).then(m => { x.map = m; x.at = Date.now(); return m; }).finally(() => { x.p = null; });
  return x.p;
}
function cxInvalidate(C) { if (C.cx) { C.cx.at = 0; C.cx.map = null; } }
// The saved turn behind an article. Your messages are matched from the end of the feed (earlier ones may not be loaded);
// an answer belongs to the message of yours above it.
function cxTurnFor(C, art, map) {
  let u = art; while (u && !(u.classList && u.classList.contains('entry') && u.classList.contains('u'))) u = u.previousElementSibling;
  if (!u) return null;
  const users = [...C.feed.querySelectorAll(':scope > article.entry.u')]; const j = users.indexOf(u); if (j < 0) return null;
  const t = map.turns[map.turns.length - (users.length - j)]; if (!t) return null;
  const md = u.querySelector(':scope > .md'); const a = cxNorm(md && md.textContent).slice(0, 24).toLowerCase(), b = cxNorm(t.text).slice(0, 24).toLowerCase();
  if (a && b && !(a === b || a.startsWith(b) || b.startsWith(a))) return null;
  return t;
}
function cxBusy(C) { if (C.st && C.st.busy) { toast('Axon is working. Stop the turn first.'); return true; } if (C.st && C.st.external) { toast('This chat is open in a terminal; change it there.'); return true; } return false; }
function cxArtFor(C, n, map, kind) {
  const users = [...C.feed.querySelectorAll(':scope > article.entry.u')]; const u = users[users.length - (map.turns.length - n)]; if (!u) return null;
  if (kind === 'user') return u;
  let a = u.nextElementSibling; while (a && !a.classList.contains('u')) { if (a.classList.contains('a')) return a; a = a.nextElementSibling; }
  return null;
}
async function cxCopy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); return; } catch (e) { /* fall back below */ }
  const ta = h('textarea', { style: 'position:fixed;opacity:0', 'aria-hidden': 'true' }); ta.value = text; document.body.append(ta); ta.select();
  try { document.execCommand('copy'); toast('Copied'); } catch (e) { toast('Could not copy'); } ta.remove();
}
async function cxCopyMsg(C, art, isUser) {
  try {
    const map = await cxMap(C, true); const t = cxTurnFor(C, art, map);
    if (t) { const r = await api('/api/chatx/message?session=' + cxEnc(C) + '&n=' + t.n + '&which=' + (isUser ? 'user' : 'reply')); if (r.text) { await cxCopy(r.text); return; } }
  } catch (e) { /* use what the page shows */ }
  const el = isUser ? art.querySelector(':scope > .md') : (art.querySelector('.tanswer') || art);
  await cxCopy((el.innerText || el.textContent || '').trim());
}
// The question asked before anything is removed: what goes, what happens to files, and what is sent next.
async function cxRewind(C, t, o) {
  let pv; try { pv = await api('/api/chatx/rewind_preview?session=' + cxEnc(C) + '&prompt=' + t.prompt); } catch (e) { toast(e.message); return false; }
  if (pv.busy) { toast('Axon is working. Stop the turn first.'); return false; }
  if (pv.attachment) { toast('This message has an image or file attached, which cannot be sent again from here. Send it again by hand.'); return false; }
  const files = pv.files || []; const kids = [];
  kids.push(h('p', null, 'This goes back to just before "' + cxNorm(pv.text).slice(0, 80) + (cxNorm(pv.text).length > 80 ? '...' : '') + '". ',
    'Axon forgets that message and everything after it (' + t.removes + ' message' + (t.removes === 1 ? '' : 's') + ' in this chat). They stay in the saved session file, but Axon will not see them again.'));
  kids.push(h('p', null, o.text != null ? 'Your edited message is then sent as a new message.' : 'The same message is then sent again' + (o.model ? ' to ' + o.model + ' (this chat then uses that model)' : '') + '.'));
  let keep = null, restore = null, over = null; const clash = pv.conflicts || [];
  if (files.length && pv.can_restore_files) {
    keep = h('input', { type: 'radio', name: 'cxfiles', checked: true }); restore = h('input', { type: 'radio', name: 'cxfiles' });
    kids.push(h('div', { class: 'cx-dlg' }, h('label', null, keep, h('span', null, 'Keep my files as they are now.')),
      h('label', null, restore, h('span', null, 'Also put back the ' + files.length + ' file' + (files.length === 1 ? '' : 's') + ' Axon changed after this point, from its checkpoints. Each returns to how it was before this message.')),
      h('ul', null, files.slice(0, 8).map(f => h('li', null, f)), files.length > 8 ? h('li', null, '+ ' + (files.length - 8) + ' more') : null)));
    if (clash.length) {
      over = h('input', { type: 'checkbox' });
      kids.push(h('div', { class: 'cx-dlg' }, h('p', null, clash.length + ' of them changed outside Axon since it last wrote them (' + clash.slice(0, 4).join(', ') + (clash.length > 4 ? ', ...' : '') + '). Putting files back stops and changes nothing unless you also allow this:'),
        h('label', null, over, h('span', null, 'Overwrite those too. A copy of what is there now is kept.'))));
    }
  } else if (files.length) kids.push(h('p', { class: 'muted' }, 'Axon changed ' + files.length + ' file' + (files.length === 1 ? '' : 's') + ' after this point, but no checkpoint is available to put them back, so your files stay as they are.'));
  else kids.push(h('p', { class: 'muted' }, 'Axon changed no files after this point, so nothing on disk changes.'));
  if (!(await wbConfirm(o.title, kids, o.ok, true))) return false;
  const mode = restore && restore.checked ? 'code_and_conversation' : 'conversation';
  try {
    const text = o.text != null ? o.text : (await api('/api/chatx/message?session=' + cxEnc(C) + '&n=' + t.n + '&which=user')).text;
    C.cxBusy = true;
    const r = await api('/api/chatx/rewind', { session: C.sid, prompt: t.prompt, mode, client: cxClient(C), overwrite: !!(over && over.checked) });
    if (o.model) await runCmd(C, '/model ' + o.model);
    await cxReload(C);
    await api('/api/chat/send', { session: C.sid, text, steer: false });
    refreshState(C, true);
    const bad = (r.reverted || []).filter(x => /^(Failed|Skipped)/.test(x));
    toast((r.removed ? 'Went back ' + r.removed + ' message' + (r.removed === 1 ? '' : 's') + '. ' : '') + (mode === 'code_and_conversation' ? (r.reverted || []).length + ' file note' + ((r.reverted || []).length === 1 ? '' : 's') + '.' : '') + (bad.length ? ' ' + bad[0] : ''));
    return true;
  } catch (e) { toast(e.message); try { await cxReload(C); } catch (x) { /* ignore */ } return false; }
  finally { C.cxBusy = false; }
}
// Draw the chat again from the saved record (after a rewind, the removed turns no longer come back).
async function cxReload(C) {
  C.feed.replaceChildren(); C.first = 0; C.lastIdx = -1; C.cur = null; C.steps = []; C.replayTs = 0; C.uses = {}; C.aps = {}; C.chips = C.chips || {};
  cxInvalidate(C);
  await loadHistory(C);
  if (!C.feed.children.length) C.feed.append(emptyChat(C));
  C.atBottom = true; C.feed.scrollTop = C.feed.scrollHeight;
}
async function cxEdit(C, art) {
  if (cxBusy(C) || art.dataset.cxEditing) return;
  let map, t, full;
  try { map = await cxMap(C, true); t = cxTurnFor(C, art, map); } catch (e) { toast(e.message); return; }
  if (!t) { toast('Could not match this message to the saved chat. Reload the page and try again.'); return; }
  if (!t.prompt) { toast('Axon does not keep this message in the conversation (its turn failed and was rolled back), so there is nothing to rewind to.'); return; }
  try { full = (await api('/api/chatx/message?session=' + cxEnc(C) + '&n=' + t.n + '&which=user')).text; } catch (e) { toast(e.message); return; }
  const md = art.querySelector(':scope > .md'); if (!md) return;
  art.dataset.cxEditing = '1';
  const ta = h('textarea', { class: 'cx-edit', 'aria-label': 'Edit your message' }); ta.value = full;
  const close = () => { delete art.dataset.cxEditing; ed.remove(); md.hidden = false; };
  const go = h('button', { class: 'btn primary', type: 'button', onclick: async () => { const text = ta.value.trim(); if (!text) return; go.disabled = true; const done = await cxRewind(C, t, { text, title: 'Edit and resend', ok: 'Rewind and resend' }); if (!done && ta.isConnected) go.disabled = false; } }, 'Resend');
  const ed = h('div', { class: 'cx-editbox' }, ta, h('div', { class: 'cx-editbar' }, go, h('button', { class: 'btn', type: 'button', onclick: close }, 'Cancel')));
  ta.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go.click(); } });
  md.hidden = true; md.after(ed); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
}
async function cxRegenerate(C, art, model) {
  if (cxBusy(C)) return;
  let map, t;
  try { map = await cxMap(C, true); t = cxTurnFor(C, art, map); } catch (e) { toast(e.message); return; }
  if (!t) { toast('Could not match this reply to the saved chat. Reload the page and try again.'); return; }
  if (!t.prompt) { toast('Axon does not keep this turn in the conversation, so it cannot be regenerated.'); return; }
  await cxRewind(C, t, { title: model ? 'Regenerate with ' + model : 'Regenerate this reply', ok: 'Rewind and regenerate', model });
}
async function cxModelMenu(C, art, anchor) {
  let d; try { d = await api('/api/models?session=' + cxEnc(C)); } catch (e) { toast(e.message); return; }
  const cur = d.current || (C.st && C.st.model) || ''; let names = [];
  try { const pv = await api('/api/providers'); const act = (pv.providers || []).find(p => p.active); if (act && (act.models || []).length) names = act.models; } catch (e) { /* use the list below */ }
  if (!names.length) names = (d.models || d.available || []).map(m => (typeof m === 'string' ? m : m.id || m.name)).filter(Boolean);
  names = [...new Set(names.filter(m => m && m !== cur))].slice(0, 30);
  if (!names.length) { toast('No other model is set up. Add one in Settings > Providers.'); return; }
  wbMenu(anchor, [{ head: 'Regenerate with (now: ' + cur + ')' }, ...names.map(m => ({ label: m, run: () => cxRegenerate(C, art, m) }))]);
}
async function cxBookmark(C, art, btn) {
  try {
    const map = await cxMap(C, true); const t = cxTurnFor(C, art, map);
    if (!t) { toast('Could not match this reply to the saved chat. Reload the page and try again.'); return; }
    if (!t.has_reply) { toast('This reply is not saved yet. Bookmark it when the turn has finished.'); return; }
    if (t.bookmark) { await api('/api/chatx/bookmark/remove', { session: C.sid, id: t.bookmark }); toast('Bookmark removed'); }
    else { await api('/api/chatx/bookmark', { session: C.sid, n: t.n, kind: 'assistant' }); toast('Bookmarked. Find it in Chat options > Bookmarks.'); }
    await cxMap(C, true); cxPaintBar(C, art, btn);
  } catch (e) { toast(e.message); }
}
async function cxBranch(C, art) {
  if (C.st && C.st.external) { toast('This chat is open in a terminal; use /branch there.'); return; }
  try {
    const map = await cxMap(C, true); const t = cxTurnFor(C, art, map);
    if (!t || !t.prompt) { toast(t ? 'Axon does not keep this turn in the conversation, so it cannot be branched from.' : 'Could not match this reply to the saved chat. Reload the page and try again.'); return; }
    toast('Branching from here...');
    const st = await api('/api/chat/fork', { session: C.sid, upto: t.prompt });
    location.hash = '#/chat/' + encodeURIComponent(st.session);
    toast('A new chat now holds the conversation up to this reply. The original is unchanged.');
  } catch (e) { toast(e.message); }
}
function cxPaintBar(C, art, btn) {
  const x = C.cx && C.cx.map; if (!x || !btn) return;
  const t = cxTurnFor(C, art, x); const on = !!(t && t.bookmark);
  btn.setAttribute('aria-pressed', String(on)); btn.textContent = on ? 'Bookmarked' : 'Bookmark';
}
function cxDecorate(C, art) {
  if (art.querySelector(':scope > .mxa')) return;
  const user = art.classList.contains('u'); const b = (label, fn, extra) => h('button', Object.assign({ type: 'button', onclick: fn }, extra || {}), label);
  let bar;
  if (user) bar = h('div', { class: 'mxa', role: 'toolbar', 'aria-label': 'Message actions' }, b('Copy', () => cxCopyMsg(C, art, true)), b('Edit and resend', () => cxEdit(C, art), { title: 'Go back to this message, change it and send it again' }));
  else {
    const bm = b('Bookmark', () => cxBookmark(C, art, bm), { 'aria-pressed': 'false', title: 'Keep a link to this reply in Chat options > Bookmarks' });
    const more = b('with another model', (e) => cxModelMenu(C, art, e.currentTarget), { 'aria-haspopup': 'menu', title: 'Regenerate with a different model' });
    bar = h('div', { class: 'mxa', role: 'toolbar', 'aria-label': 'Reply actions' }, b('Copy', () => cxCopyMsg(C, art, false)),
      b('Regenerate', () => cxRegenerate(C, art, ''), { title: 'Go back to the message above and answer it again' }), more, bm,
      b('Branch from here', () => cxBranch(C, art), { title: 'Continue from this reply in a new chat; this one stays as it is' }));
    const paint = () => { if (C.sid) cxMap(C).then(() => cxPaintBar(C, art, bm)).catch(() => { }); };
    art.addEventListener('mouseenter', paint); art.addEventListener('focusin', paint);
  }
  if (user) { const place = () => { const md = art.querySelector(':scope > .md'); if (md && md.offsetLeft > 0) bar.style.right = Math.max(8, art.clientWidth - md.offsetLeft + 8) + 'px'; }; art.addEventListener('mouseenter', place); art.addEventListener('focusin', place); }
  art.append(bar);
}
function cxWire(C) {
  if (!C || !C.feed || C.cxWired) return; C.cxWired = true;
  const scan = () => C.feed.querySelectorAll(':scope > article.entry.u, :scope > article.entry.a').forEach(a => cxDecorate(C, a));
  scan(); new MutationObserver(scan).observe(C.feed, { childList: true });
}
if (typeof drawHead === 'function') { const baseDrawHead2 = drawHead; drawHead = function (C) { baseDrawHead2.apply(this, arguments); try { cxWire(C); } catch (e) { console.warn('chat actions', e); } }; }
if (typeof axOnEvent === 'function') {
  const baseAx = axOnEvent;
  axOnEvent = function (C, ev) {
    baseAx.apply(this, arguments);
    if (['turn_end', 'idle', 'interrupted', 'error', 'user'].includes(ev.type)) cxInvalidate(C);
    if (ev.type === 'rewound' && !C.replaying && ev.client !== cxClient(C) && !C.cxBusy) cxReload(C).catch(() => { });   // another page or the terminal rewound this chat
  };
}

// ----- find in the chat -----
// Matches are found in the page text (collapsed rows included) and painted with the browser's highlight layer, so nothing in
// the feed is changed. Messages that are not loaded yet are counted by the server and can be loaded from the bar.
function cxFind(C) {
  C = C || UI.chat; if (!C || !C.feed || !C.feed.isConnected) return;
  let f = C.cxFindBar;
  if (!f) {
    const input = h('input', { type: 'text', 'aria-label': 'Find in this chat', placeholder: 'Find in this chat', autocomplete: 'off', spellcheck: 'false' });
    const cnt = h('span', { class: 'cnt', 'aria-live': 'polite' }); const more = h('div', { class: 'more', hidden: true });
    const prev = h('button', { class: 'mini', type: 'button', 'aria-label': 'Previous match', title: 'Previous (Shift+Enter)', onclick: () => cxStep(C, -1) }, 'Prev');
    const next = h('button', { class: 'mini', type: 'button', 'aria-label': 'Next match', title: 'Next (Enter)', onclick: () => cxStep(C, 1) }, 'Next');
    const bar = h('div', { class: 'cx-find', role: 'search', hidden: true }, input, cnt, prev, next, h('button', { class: 'mini', type: 'button', 'aria-label': 'Close find', onclick: () => cxFindClose(C) }, 'Close'), more);
    f = C.cxFindBar = { bar, input, cnt, more, ranges: [], cur: -1, t: 0, ts: 0, server: null };
    input.addEventListener('input', () => { clearTimeout(f.t); f.t = setTimeout(() => { cxRun(C, false); cxServerCount(C); }, 120); });
    bar.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); cxFindClose(C); }
      else if (e.key === 'Enter') { e.preventDefault(); cxStep(C, e.shiftKey ? -1 : 1); }
    });
    C.feed.parentNode.append(bar);
    new MutationObserver(() => { if (!bar.hidden && input.value) { clearTimeout(f.ts); f.ts = setTimeout(() => cxRun(C, true), 600); } }).observe(C.feed, { childList: true, subtree: true, characterData: true });
  }
  f.bar.hidden = false; f.input.focus(); f.input.select(); if (f.input.value) { cxRun(C, false); cxServerCount(C); }
}
function cxFindClose(C) {
  const f = C.cxFindBar; if (!f) return; f.bar.hidden = true; f.ranges = []; f.cur = -1; cxPaintHL(f);
  const ta = document.getElementById('msg'); if (ta) ta.focus();
}
function cxPaintHL(f) {
  if (!(window.CSS && CSS.highlights && window.Highlight)) return;
  if (!f.ranges.length) { CSS.highlights.delete('cx-match'); CSS.highlights.delete('cx-cur'); return; }
  CSS.highlights.set('cx-match', new Highlight(...f.ranges));
  if (f.cur >= 0 && f.ranges[f.cur]) CSS.highlights.set('cx-cur', new Highlight(f.ranges[f.cur])); else CSS.highlights.delete('cx-cur');
}
function cxRun(C, keep) {
  const f = C.cxFindBar; if (!f) return; const q = f.input.value; const ranges = [];
  if (q) {
    const needle = q.toLowerCase();
    const walker = document.createTreeWalker(C.feed, NodeFilter.SHOW_TEXT, { acceptNode: (n) => { const p = n.parentElement; return !p || !n.nodeValue.trim() || p.closest('.mxa, script, style, textarea') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; } });
    for (let n; (n = walker.nextNode()) && ranges.length < 5000;) {
      const text = n.nodeValue.toLowerCase(); let i = text.indexOf(needle);
      while (i >= 0 && ranges.length < 5000) { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + needle.length); ranges.push(r); i = text.indexOf(needle, i + needle.length); }
    }
  }
  f.ranges = ranges; f.cur = ranges.length ? (keep && f.cur >= 0 ? Math.min(f.cur, ranges.length - 1) : (keep ? 0 : ranges.length - 1)) : -1;
  cxPaintHL(f); cxCount(C);
  if (!keep && ranges.length) cxGo(C, f.cur);
}
function cxCount(C) {
  const f = C.cxFindBar; const n = f.ranges.length;
  f.cnt.textContent = !f.input.value ? '' : n ? (f.cur + 1) + ' of ' + n + (n >= 5000 ? '+' : '') : 'No matches';
  const s = f.server; const early = s ? s.rows.filter(r => r.i != null && r.i < (C.first || 0)).reduce((a, r) => a + r.count, 0) : 0;
  f.more.replaceChildren(); f.more.hidden = !early || !f.input.value;
  if (early && f.input.value) f.more.append('Also in earlier messages that are not loaded: ' + early + '.', h('button', { class: 'btn', type: 'button', onclick: () => cxLoadEarlier(C) }, 'Load them'));
}
async function cxServerCount(C) {
  const f = C.cxFindBar; const q = f.input.value; if (!q || !C.sid) { f.server = null; cxCount(C); return; }
  try { const r = await api('/api/chatx/search?session=' + cxEnc(C) + '&q=' + encodeURIComponent(q)); if (f.input.value === q) { f.server = { total: r.total, rows: r.messages }; cxCount(C); } } catch (e) { f.server = null; }
}
async function cxLoadEarlier(C) {
  const f = C.cxFindBar;
  for (let k = 0; k < 200; k++) {
    const b = C.feed.querySelector(':scope > button.earlier'); if (!b || b.hidden) break;
    const before = C.first; b.click(); await new Promise(r => setTimeout(r, 120));
    for (let w = 0; w < 40 && C.first === before; w++) await new Promise(r => setTimeout(r, 100));
    if (C.first === before) break;
    if (f.server && !f.server.rows.some(r => r.i != null && r.i < C.first)) break;
  }
  cxRun(C, true);
}
function cxReveal(C, r) {
  const el = r.startContainer.parentElement; if (!el) return;
  for (let p = el; p && p !== C.feed; p = p.parentElement) if (p.tagName === 'DETAILS' && !p.open) p.open = true;
  if (!el.getClientRects().length) {
    const art = el.closest('article.entry.a');
    if (art && art.dataset.steps !== 'open') { art.dataset.steps = 'open'; const hb = art.querySelector('.thead'); if (hb) hb.setAttribute('aria-expanded', 'true'); }
  }
  el.scrollIntoView({ block: 'center' });
}
function cxGo(C, i) {
  const f = C.cxFindBar; if (!f.ranges.length) return; f.cur = (i + f.ranges.length) % f.ranges.length;
  cxReveal(C, f.ranges[f.cur]); cxPaintHL(f); cxCount(C);
  if (!(window.CSS && CSS.highlights && window.Highlight)) { const s = getSelection(); s.removeAllRanges(); s.addRange(f.ranges[f.cur]); }
}
function cxStep(C, d) { const f = C.cxFindBar; if (!f) return; if (!f.ranges.length) { cxRun(C, false); return; } cxGo(C, f.cur + d); }
document.addEventListener('keydown', (e) => {
  const C = UI.chat; if (!C || !C.feed || !C.feed.isConnected || !UI.active || UI.active.route !== 'chat') return;
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'f') {
    if (document.querySelector('.scrim')) return;
    const t = e.target; if (t !== document.body && !(t.closest && t.closest('.chat'))) return;       // in another panel the browser keeps its own find
    e.preventDefault(); cxFind(C);
  } else if (C.cxFindBar && !C.cxFindBar.bar.hidden && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'g') { e.preventDefault(); cxStep(C, e.shiftKey ? -1 : 1); }
}, true);

// ----- bookmarks -----
async function cxBookmarksMenu(C, anchor) {
  let map; try { map = await cxMap(C, true); } catch (e) { toast(e.message); return; }
  const rows = map.bookmarks || [];
  const items = rows.length ? [{ head: 'Bookmarks (' + rows.length + ')' }, ...rows.map(b => ({ label: (b.kind === 'user' ? 'You: ' : '') + (b.snippet || '(empty)').slice(0, 70), hint: b.created ? age(b.created) : '', title: b.note || b.snippet, run: () => cxJump(C, b) })),
      '-', { label: 'Remove a bookmark...', run: () => wbMenu(anchor, [{ head: 'Remove which?' }, ...rows.map(b => ({ label: (b.snippet || '(empty)').slice(0, 70), run: async () => { try { await api('/api/chatx/bookmark/remove', { session: C.sid, id: b.id }); cxInvalidate(C); toast('Bookmark removed'); } catch (e) { toast(e.message); } } }))]) }]
    : [{ label: 'No bookmarks yet. Hover over a reply and choose Bookmark.', disabled: true }];
  wbMenu(anchor, items);
}
async function cxJump(C, b) {
  let map; try { map = await cxMap(C, true); } catch (e) { toast(e.message); return; }
  const t = map.turns[b.n]; if (!t) { toast('That message is no longer in the chat.'); return; }
  let art = cxArtFor(C, b.n, map, b.kind);
  for (let k = 0; !art && k < 400; k++) {            // not loaded yet: load earlier messages until it is
    const btn = C.feed.querySelector(':scope > button.earlier'); if (!btn || btn.hidden) break;
    const before = C.first; btn.click();
    for (let w = 0; w < 40 && C.first === before; w++) await new Promise(r => setTimeout(r, 100));
    if (C.first === before) break;
    art = cxArtFor(C, b.n, map, b.kind);
  }
  if (!art) { toast('Could not find that message on the page.'); return; }
  C.atBottom = false; art.scrollIntoView({ block: 'center' }); art.classList.add('cx-flash'); setTimeout(() => art.classList.remove('cx-flash'), 2200);
}

// ----- export or share (the one dialog) -----
function shareModal(req) {
  const isChat = req.kind === 'session';
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': isChat ? 'Export or share this chat' : 'Share' });
  const close = () => { scrim.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey, true);
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); });
  const box = h('div', { class: 'modal cx-share' }, h('p', { class: 'muted' }, 'Preparing a copy with secrets removed...'));
  scrim.append(box); document.body.append(scrim);
  let fmt = lsGet('axon-export-fmt') || 'html'; if (!['md', 'html', 'pdf', 'json'].includes(fmt)) fmt = 'html';
  let tools = lsGet('axon-export-tools') !== '0'; let seq = 0;
  const FMTS = [['html', 'Web page (one file)'], ['md', 'Markdown'], ['pdf', 'PDF (print)'], ['json', 'JSON']];
  const printFile = (p) => {
    const fr = h('iframe', { src: '/api/share/file?inline=1&sha=' + p.sha256, 'aria-hidden': 'true', tabindex: '-1', style: 'position:fixed;width:0;height:0;border:0;opacity:0' });
    fr.addEventListener('load', () => { try { fr.contentWindow.focus(); fr.contentWindow.print(); } catch (e) { toast('The browser would not open the print dialog. Save the web page and print it from there.'); } setTimeout(() => fr.remove(), 120000); });
    document.body.append(fr);
  };
  const draw = (p) => {
    const kids = [h('h3', null, isChat ? 'Export or share this chat' : 'Share a copy')];
    if (isChat) {
      kids.push(h('div', { class: 'fmt', role: 'radiogroup', 'aria-label': 'Format' }, FMTS.map(([v, l]) => h('label', null, h('input', { type: 'radio', name: 'cxfmt', value: v, checked: v === fmt, onchange: () => { fmt = v; lsSet('axon-export-fmt', v); load(); } }), l))),
        h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer' }, h('input', { type: 'checkbox', checked: tools, onchange: (e) => { tools = e.target.checked; lsSet('axon-export-tools', tools ? '1' : '0'); load(); } }), 'Include tool details (commands, file contents and results)'));
    }
    const n = p.redacted;
    const gone = n == null ? 'Keys, tokens, bearer headers and .env values were removed.' : n ? n + ' secret' + (n === 1 ? '' : 's') + ' (keys, tokens, passwords) removed.' : 'No secrets were found.';
    kids.push(h('p', { class: 'sum' }, h('b', null, gone), ' ' + (isChat ? p.messages + ' messages. ' : '') + 'This is a file on this computer; nothing is uploaded unless you choose to. Look through the preview before you send it to anyone.'));
    if (isChat && (p.format === 'html' || p.format === 'pdf')) kids.push(h('iframe', { src: '/api/share/file?inline=1&sha=' + p.sha256, title: 'Preview of the file', sandbox: '' }));
    else { const pre = h('pre'); pre.textContent = p.content.length > 200000 ? p.content.slice(0, 200000) + '\n... (the preview shows the first 200,000 characters; the file has all of it)' : p.content; kids.push(pre); }
    const acts = h('div', { class: 'acts' });
    if (isChat && p.format === 'pdf') acts.append(h('button', { class: 'btn primary', type: 'button', onclick: () => printFile(p) }, 'Print or save as PDF...'), h('a', { class: 'btn', href: '/api/share/file?sha=' + p.sha256, download: p.filename }, 'Save as web page'));
    else acts.append(h('a', { class: 'btn primary', href: '/api/share/file?sha=' + p.sha256, download: p.filename }, 'Save ' + p.filename));
    const up = h('div');
    if (p.gh && p.gist_ok) {
      const gist = h('button', { class: 'btn', type: 'button', onclick: () => {
        gist.disabled = true;
        const yes = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
          yes.disabled = true;
          try { const r = await api('/api/share/gist', { sha256: p.sha256, confirm: true }); up.replaceChildren(h('p', null, 'Created: ', h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.url))); }
          catch (e) { up.replaceChildren(errorBox(e.message)); gist.disabled = false; }
        } }, 'Upload exactly this file');
        up.replaceChildren(h('p', { class: 'sum' }, 'This sends the file above (' + p.bytes.toLocaleString() + ' bytes) to GitHub through the gh command. Anyone with the link can read a secret gist.'), yes);
      } }, 'Create secret GitHub gist...');
      acts.append(gist);
    }
    acts.append(h('button', { class: 'btn', type: 'button', onclick: close }, 'Close'), h('span', { class: 'muted' }, p.bytes.toLocaleString() + ' bytes' + (isChat && p.format === 'pdf' ? ' · in the print dialog choose "Save as PDF"' : '')));
    kids.push(acts, up);
    box.replaceChildren(...kids);
    const first = box.querySelector('input[type=radio]:checked, .btn.primary'); if (first && !scrim.dataset.focused) { scrim.dataset.focused = '1'; first.focus(); }
  };
  const load = () => {
    const mine = ++seq; const keepH = box.offsetHeight;
    if (isChat && box.querySelector('iframe, pre')) box.style.minHeight = keepH + 'px';
    api('/api/share/preview', isChat ? { kind: 'session', id: req.id, format: fmt, tools } : req).then(p => { if (mine === seq) { draw(p); box.style.minHeight = ''; } })
      .catch(e => { if (mine === seq) box.replaceChildren(errorBox(e.message), h('button', { class: 'btn', onclick: close }, 'Close')); });
  };
  load();
}
"""
