"""Plain data for exporting, sharing, searching and bookmarking one chat. No web and no terminal imports.

The app (``dash_js_chatx``) and the terminal (``/export``, ``/bookmark``, ``/bookmarks``) both read this module,
so a chat comes out the same wherever it is exported.

* A chat is a list of *items*: ``{"kind": "user" | "assistant" | "note", "ts", "text", "thinking", "model",
  "tools": [{"name", "input", "result", "is_error"}]}``. ``items_from_entries`` takes the saved transcript the
  app shows; ``items_from_messages`` takes the live conversation the terminal holds.
* ``redact_items`` removes secrets with Axon's own redactor and says how many it removed.
* ``to_markdown`` and ``to_html`` write the formats. The HTML is one file: inline CSS (with a print style sheet,
  so the browser's Print > Save as PDF gives a tidy document), no scripts, every piece of text escaped.
* Bookmarks live in ``bookmarks.json`` beside the session, written atomically, and a damaged file reads as empty.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REDACTED = "[REDACTED]"
TOOL_CLIP = 6000                  # characters of one tool input or result kept in an export
BOOKMARK_FILE = "bookmarks.json"
MAX_BOOKMARKS = 500
FORMATS = ("md", "html", "pdf", "json")

# ── items ────────────────────────────────────────────────────────────────────


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    out.append("[image]")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    return "" if content is None else str(content)


def _item(kind: str, text: str = "", ts: Any = None, **extra: Any) -> dict[str, Any]:
    it: dict[str, Any] = {"kind": kind, "ts": ts if isinstance(ts, (int, float)) else None, "text": text,
                          "thinking": "", "model": "", "tools": []}
    it.update(extra)
    return it


def merge_replies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One assistant item per reply: the model calls of a turn (text, tool calls) become a single block."""
    out: list[dict[str, Any]] = []
    for it in items:
        if it["kind"] == "assistant" and out and out[-1]["kind"] == "assistant":
            prev = out[-1]
            prev["text"] = "\n\n".join(x for x in (prev["text"].strip(), it["text"].strip()) if x)
            prev["thinking"] = "\n\n".join(x for x in (prev["thinking"].strip(), it["thinking"].strip()) if x)
            prev["tools"] = list(prev["tools"]) + list(it["tools"])
            prev["model"] = it["model"] or prev["model"]
        else:
            out.append(dict(it, tools=list(it["tools"])))
    return out


def items_from_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items from the saved transcript entries (the app's ``Transcript.entries``)."""
    items = []
    for e in entries:
        kind = e.get("kind")
        if kind not in ("user", "assistant", "note"):
            continue
        tools = [{"name": str(t.get("name") or "tool"), "input": t.get("input") if isinstance(t.get("input"), dict) else {},
                  "result": t.get("result"), "is_error": bool(t.get("is_error"))} for t in (e.get("tools") or []) if isinstance(t, dict)]
        items.append(_item(kind, str(e.get("text") or ""), e.get("ts"), thinking=str(e.get("thinking") or ""),
                           model=str(e.get("model") or ""), tools=tools, i=e.get("i")))
    return merge_replies(items)


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"arguments": str(raw)}


def items_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items from a live conversation (Anthropic-style blocks or OpenAI-style tool calls)."""
    items: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "user":
            if isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    t = by_id.get(str(b.get("tool_use_id")))
                    if t is not None:
                        t["result"] = _blocks_text(b.get("content"))
                        t["is_error"] = bool(b.get("is_error"))
                continue
            text = _blocks_text(content)
            if text.strip():
                items.append(_item("user", text))
        elif role == "tool":
            t = by_id.get(str(m.get("tool_call_id")))
            if t is not None:
                t["result"] = _blocks_text(content)
        elif role == "assistant":
            it = _item("assistant", "")
            texts = []
            if isinstance(content, str):
                texts.append(content)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                elif b.get("type") == "thinking":
                    it["thinking"] += str(b.get("thinking", ""))
                elif b.get("type") == "tool_use":
                    t = {"name": str(b.get("name") or "tool"), "input": _args(b.get("input")), "result": None, "is_error": False}
                    it["tools"].append(t)
                    by_id[str(b.get("id"))] = t
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                t = {"name": str(fn.get("name") or "tool"), "input": _args(fn.get("arguments")), "result": None, "is_error": False}
                it["tools"].append(t)
                by_id[str(tc.get("id"))] = t
            it["text"] = "\n".join(x for x in texts if x.strip())
            if it["text"] or it["tools"]:
                items.append(it)
    return merge_replies(items)


# ── redaction ────────────────────────────────────────────────────────────────

def default_redactor() -> Callable[[str], str]:
    """The strongest redactor available: the app's (header lines, auth-like assignments, .env blocks) on top of
    the journal's. Falls back to the journal's alone."""
    try:
        from axon.features.dashboard import redact_text as strong
        return lambda s: strong(s)
    except Exception:  # pragma: no cover - the app module is part of the package
        from axon.session.activity import redact_text as base
        return lambda s: base(s)


def _walk(v: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, dict):
        return {k: _walk(x, fn) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_walk(x, fn) for x in v]
    return v


def _count(items: list[dict[str, Any]]) -> int:
    return json.dumps(items, ensure_ascii=False, default=str).count(REDACTED)


def redact_items(items: list[dict[str, Any]], redactor: Callable[[str], str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """(copy of ``items`` with secrets replaced, how many were replaced)."""
    fn = redactor or default_redactor()
    try:
        from axon.session.activity import redact as key_aware     # also masks values under secret-looking keys
    except Exception:  # pragma: no cover
        key_aware = lambda x: x  # noqa: E731
    out = []
    for it in items:
        c = dict(it)
        c["text"] = fn(str(it.get("text") or ""))
        c["thinking"] = fn(str(it.get("thinking") or ""))
        c["tools"] = [{"name": t["name"], "input": _walk(key_aware(t.get("input") or {}), fn),
                       "result": None if t.get("result") is None else fn(str(t["result"])), "is_error": t.get("is_error", False)}
                      for t in it.get("tools") or []]
        out.append(c)
    return out, max(0, _count(out) - _count(items))


def drop_tools(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same items without tool calls, thinking, and replies that were only tool calls."""
    out = []
    for it in items:
        if it["kind"] == "assistant":
            c = dict(it, tools=[], thinking="")
            if not c["text"].strip():
                continue
            out.append(c)
        else:
            out.append(it)
    return out


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else ""
    except (OverflowError, OSError, ValueError):
        return ""


def _tool_input_text(name: str, inp: dict[str, Any]) -> str:
    if isinstance(inp.get("command"), str) and name.lower() == "bash":
        return inp["command"]
    if not inp:
        return ""
    return "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)}" for k, v in inp.items())


def _tool_summary(inp: dict[str, Any]) -> str:
    for k in ("file_path", "path", "command", "pattern", "query", "url", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:90]
    return ""


def _clip(text: str, limit: int = TOOL_CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit:,} more characters not included)"


# ── Markdown ─────────────────────────────────────────────────────────────────

def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def to_markdown(items: list[dict[str, Any]], meta: dict[str, Any], include_tools: bool = True) -> str:
    title = str(meta.get("title") or "Chat")
    bits = [f"Session `{meta['id']}`" if meta.get("id") else "", f"model {meta['model']}" if meta.get("model") else "",
            f"{len(items)} messages", "tool details included" if include_tools else "tool details left out"]
    lines = [f"# {title}", "", " · ".join(b for b in bits if b), ""]
    for it in items if include_tools else drop_tools(items):
        when = _when(it.get("ts"))
        if it["kind"] == "user":
            lines += [f"## You{' · ' + when if when else ''}", "", it["text"].strip(), ""]
        elif it["kind"] == "assistant":
            lines += [f"## Axon{' · ' + when if when else ''}", ""]
            if it["text"].strip():
                lines += [it["text"].strip(), ""]
            if include_tools:
                for t in it["tools"]:
                    s = _tool_summary(t["input"])
                    lines += [f"**Tool:** `{t['name']}`" + (f" `{s}`" if s and "`" not in s else "") + (" (failed)" if t.get("is_error") else ""), ""]
                    body = _clip(_tool_input_text(t["name"], t["input"]))
                    if body:
                        f = _fence(body)
                        lines += [f"{f}text", body, f, ""]
                    if t.get("result"):
                        res = _clip(str(t["result"]))
                        f = _fence(res)
                        lines += ["Result:", "", f, res, f, ""]
        else:
            lines += [f"> {it['text'].strip()}", ""]
    return "\n".join(lines).rstrip() + "\n"


# ── HTML ─────────────────────────────────────────────────────────────────────

_CSS = """
:root{color-scheme:light dark;--bg:#fff;--ink:#1d2127;--ink2:#566070;--line:#d9dde3;--code:#f3f5f8;--you:#0b6b5d;--ax:#7a3fb0;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--ink:#e6e9ee;--ink2:#a2abb8;--line:#2e343d;--code:#1d222a;--you:#5ed2c8;--ax:#b4a0ff;--bad:#ff7878}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:22px;line-height:1.3;margin:0 0 4px}.sub{color:var(--ink2);font-size:13px;margin:0 0 24px}
.msg{border-top:1px solid var(--line);padding:14px 0}.who{font-size:12.5px;font-weight:600;color:var(--you);margin-bottom:4px}
.msg.a .who{color:var(--ax)}.when{color:var(--ink2);font-weight:400;margin-left:8px}
.body p{margin:.5em 0}.body h2,.body h3,.body h4{font-size:15px;margin:1em 0 .3em}.body ul,.body ol{margin:.4em 0;padding-left:1.4em}
.body blockquote{margin:.6em 0;padding-left:12px;border-left:3px solid var(--line);color:var(--ink2)}
.body{overflow-wrap:anywhere}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:1px 4px;border-radius:3px}
pre{background:var(--code);padding:10px 12px;border-radius:5px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:.5em 0}
pre code{background:none;padding:0}
.tool{margin:10px 0;border-left:3px solid var(--line);padding-left:10px;font-size:13px}.tool.err{border-color:var(--bad)}
.th{color:var(--ink2);font-weight:600}.th span{font-weight:400;font-family:ui-monospace,Menlo,monospace;margin-left:8px}
.tool pre{max-height:22em}.lbl{color:var(--ink2);font-size:12px;margin-top:6px}
.note{color:var(--ink2);font-style:italic;font-size:13px;padding:8px 0}
footer{margin-top:36px;border-top:1px solid var(--line);padding-top:12px;color:var(--ink2);font-size:12.5px}
a{color:inherit}
@media print{
 :root{--bg:#fff;--ink:#000;--ink2:#444;--line:#bbb;--code:#f2f2f2;--you:#000;--ax:#000}
 @page{margin:16mm 14mm}
 body{font-size:11pt;background:#fff;color:#000}main{max-width:none;padding:0}
 .msg{break-inside:auto;padding:10px 0}.who{break-after:avoid}.tool{break-inside:avoid}
 pre,.tool pre{max-height:none;overflow:visible;border:1px solid var(--line);background:#fff}
 a{text-decoration:none}
}
"""

_INLINE = [(re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
           (re.compile(r"\*\*([^*\n]+)\*\*"), r"<strong>\1</strong>"),
           (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])"), r"<em>\1</em>")]
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")


def _inline(text: str) -> str:
    s = html.escape(text, quote=True)
    # inline code first so its content is not touched by bold or italic
    parts = re.split(r"(`[^`\n]+`)", s)
    for i, p in enumerate(parts):
        if i % 2:
            parts[i] = f"<code>{p[1:-1]}</code>"
            continue
        p = _LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer">{m.group(1)}</a>', p)
        for rx, rep in _INLINE[1:]:
            p = rx.sub(rep, p)
        parts[i] = p
    return "".join(parts)


def markdown_to_html(text: str) -> str:
    """A small, safe Markdown renderer: headings, lists, quotes, code fences, rules, emphasis, links. All text is
    escaped before any markup is added, and only http, https and mailto links are kept."""
    lines = text.replace("\r\n", "\n").replace("\x00", "").split("\n")
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        fence = re.match(r"^\s*(`{3,}|~{3,})\s*([\w+.-]*)", ln)
        if fence:
            flush()
            mark, buf = fence.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(buf))}</code></pre>")
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if h:
            flush()
            lvl = min(4, max(2, len(h.group(1)) + 1))
            out.append(f"<h{lvl}>{_inline(h.group(2))}</h{lvl}>")
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            flush()
            out.append("<hr>")
        elif re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", ln):
            flush()
            ordered = bool(re.match(r"^\s*\d", ln))
            tag, buf = ("ol" if ordered else "ul"), []
            while i < len(lines) and re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", lines[i]):
                buf.append("<li>" + _inline(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", lines[i])) + "</li>")
                i += 1
            out.append(f"<{tag}>{''.join(buf)}</{tag}>")
            continue
        elif ln.lstrip().startswith(">"):
            flush()
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in buf) + "</blockquote>")
            continue
        elif not ln.strip():
            flush()
        else:
            para.append(ln)
        i += 1
    flush()
    return "\n".join(out)


def to_html(items: list[dict[str, Any]], meta: dict[str, Any], include_tools: bool = True, removed: int | None = None) -> str:
    """One self-contained HTML file. Nothing in it can run: there is no script, and every piece of text is escaped."""
    title = str(meta.get("title") or "Chat")
    e = html.escape
    body: list[str] = []
    for it in items if include_tools else drop_tools(items):
        when = _when(it.get("ts"))
        w = f'<span class="when">{e(when)}</span>' if when else ""
        if it["kind"] == "user":
            body.append(f'<section class="msg u"><div class="who">You{w}</div><div class="body">{markdown_to_html(it["text"])}</div></section>')
        elif it["kind"] == "assistant":
            parts = [f'<section class="msg a"><div class="who">Axon{w}</div>']
            if it["text"].strip():
                parts.append(f'<div class="body">{markdown_to_html(it["text"])}</div>')
            if include_tools:
                for t in it["tools"]:
                    s = _tool_summary(t["input"])
                    parts.append(f'<div class="tool{" err" if t.get("is_error") else ""}"><div class="th">{e(t["name"])}'
                                 + (f"<span>{e(s)}</span>" if s else "") + ("<span>failed</span>" if t.get("is_error") else "") + "</div>")
                    inp = _clip(_tool_input_text(t["name"], t["input"]))
                    if inp:
                        parts.append(f'<div class="lbl">Input</div><pre>{e(inp)}</pre>')
                    if t.get("result"):
                        parts.append(f'<div class="lbl">Result</div><pre>{e(_clip(str(t["result"])))}</pre>')
                    parts.append("</div>")
            parts.append("</section>")
            body.append("".join(parts))
        else:
            body.append(f'<div class="note">{e(it["text"].strip())}</div>')
    bits = [str(meta["model"]) if meta.get("model") else "", f"{len(items)} messages",
            "tool details included" if include_tools else "tool details left out",
            "exported " + datetime.now().strftime("%Y-%m-%d %H:%M")]
    foot = "Read-only copy exported from Axon. "
    foot += (f"{removed} secret{'s' if removed != 1 else ''} (keys, tokens, passwords) removed. " if removed is not None else "")
    foot += "Read it through before you share it."
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f"<title>{e(title[:120])}</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{e(title[:200])}</h1><p class=\"sub\">{e(' · '.join(b for b in bits if b))}</p>"
            + "".join(body) + f"<footer>{e(foot)}</footer></main></body></html>\n")


def to_json(items: list[dict[str, Any]], meta: dict[str, Any], include_tools: bool = True) -> str:
    keep = items if include_tools else drop_tools(items)
    return json.dumps({"id": meta.get("id", ""), "title": meta.get("title", ""), "model": meta.get("model", ""), "entries": keep},
                      ensure_ascii=False, indent=2, default=str)


def build(items: list[dict[str, Any]], meta: dict[str, Any], fmt: str, include_tools: bool = True,
          redact: bool = True, redactor: Callable[[str], str] | None = None) -> dict[str, Any]:
    """{"filename", "mime", "content", "redacted", "messages", "format"} for ``fmt`` in md, html, pdf, json.
    ``pdf`` is the HTML file: the browser prints it to PDF with its print style sheet."""
    if fmt not in FORMATS:
        raise ValueError("format must be md, html, pdf or json")
    removed = 0
    if redact:
        items, removed = redact_items(items, redactor)
        meta = dict(meta, title=(redactor or default_redactor())(str(meta.get("title") or "")))
    if fmt == "md":
        content, ext, mime = to_markdown(items, meta, include_tools), "md", "text/markdown; charset=utf-8"
    elif fmt == "json":
        content, ext, mime = to_json(items, meta, include_tools), "json", "application/json"
    else:
        content, ext, mime = to_html(items, meta, include_tools, removed if redact else None), "html", "text/html; charset=utf-8"
    return {"filename": export_filename(str(meta.get("title") or ""), str(meta.get("id") or ""), ext), "mime": mime,
            "content": content, "redacted": removed, "messages": len(items), "format": fmt}


def export_filename(title: str, sid: str, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:40] or "chat"
    tail = re.sub(r"[^A-Za-z0-9]", "", sid)[-8:]
    return f"{slug}-{tail}.{ext}" if tail else f"{slug}.{ext}"


# ── search ───────────────────────────────────────────────────────────────────

def _fields(it: dict[str, Any]) -> list[tuple[str, str]]:
    out = [("message", it.get("text") or "")]       # thinking is not searched: the page does not show it as text
    for t in it.get("tools") or []:
        out.append((f"tool {t['name']}", _tool_input_text(t["name"], t.get("input") or {})))
        out.append((f"tool {t['name']} result", str(t.get("result") or "")))
    return [(w, s) for w, s in out if s]


def search_items(items: list[dict[str, Any]], query: str, limit: int = 200) -> dict[str, Any]:
    """Where ``query`` occurs (case-insensitive) in messages, thinking, and tool inputs and results, collapsed or not."""
    q = (query or "").strip()[:200].lower()
    if not q:
        return {"query": "", "total": 0, "messages": []}
    rows, total = [], 0
    for idx, it in enumerate(items):
        n, where = 0, ""
        for w, s in _fields(it):
            c = s.lower().count(q)
            if c:
                n += c
                where = where or w
        if n:
            total += n
            if len(rows) < limit:
                rows.append({"index": idx, "i": it.get("i"), "kind": it["kind"], "ts": it.get("ts"), "count": n, "where": where})
    return {"query": q, "total": total, "messages": rows}


# ── bookmarks ────────────────────────────────────────────────────────────────

_LOCK = threading.Lock()


def _bm_path(folder: Path | str) -> Path:
    return Path(folder) / BOOKMARK_FILE


def load_bookmarks(folder: Path | str) -> list[dict[str, Any]]:
    try:
        data = json.loads(_bm_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("bookmarks") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and r.get("kind") in ("user", "assistant")] if isinstance(rows, list) else []


def _save(folder: Path | str, rows: list[dict[str, Any]]) -> None:
    p = _bm_path(folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "bookmarks": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def add_bookmark(folder: Path | str, ts: float, kind: str, snippet: str, note: str = "",
                 redactor: Callable[[str], str] | None = None) -> tuple[dict[str, Any], bool]:
    """(bookmark, created). A message already bookmarked is returned as it is."""
    if kind not in ("user", "assistant"):
        raise ValueError("kind must be user or assistant")
    fn = redactor or default_redactor()
    with _LOCK:
        rows = load_bookmarks(folder)
        for r in rows:
            if abs(float(r["ts"]) - float(ts)) < 1e-6 and r["kind"] == kind:
                return r, False
        if len(rows) >= MAX_BOOKMARKS:
            raise ValueError(f"A chat can have up to {MAX_BOOKMARKS} bookmarks; remove some first.")
        row = {"id": f"b{int(time.time() * 1000):x}{len(rows)}", "ts": float(ts), "kind": kind,
               "snippet": fn(" ".join(str(snippet).split()))[:160], "note": fn(" ".join(str(note).split()))[:200], "created": time.time()}
        rows.append(row)
        _save(folder, rows)
        return row, True


def remove_bookmark(folder: Path | str, bid: str) -> bool:
    with _LOCK:
        rows = load_bookmarks(folder)
        keep = [r for r in rows if r.get("id") != bid]
        if len(keep) == len(rows):
            return False
        _save(folder, keep)
        return True


def turns_of(entries: list[dict[str, Any]], prompts: dict[int, int]) -> list[dict[str, Any]]:
    """One row per message of yours with the reply that follows it, in the order a chat is drawn. ``prompts`` maps
    round(ts, 5) of a prompt still in the conversation to its number (a rewound or rolled-back prompt gets None)."""
    turns: list[dict[str, Any]] = []
    for e in entries:
        if e["kind"] == "user":
            ts = e.get("ts")
            turns.append({"n": len(turns), "prompt": prompts.get(round(ts, 5)) if isinstance(ts, (int, float)) else None,
                          "ts": ts, "i": e.get("i"), "user": e.get("text") or "", "reply": [], "reply_ts": None})
        elif e["kind"] == "assistant" and turns:
            t = turns[-1]
            if t["reply_ts"] is None and isinstance(e.get("ts"), (int, float)):
                t["reply_ts"] = e["ts"]
            if (e.get("text") or "").strip():
                t["reply"].append(e["text"].strip())
    left = 0
    for t in reversed(turns):
        left += 1 + (1 if t["reply_ts"] is not None else 0)
        t["removes"] = left
    return turns


def live_bookmarks(folder: Path | str, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The bookmarks whose message is still in the chat (a rewind can remove it), each with the turn number ``n``."""
    by_ts: dict[tuple[str, float], dict[str, Any]] = {}
    for t in turns:
        if isinstance(t["ts"], (int, float)):
            by_ts[("user", round(t["ts"], 5))] = t
        if t["reply_ts"] is not None:
            by_ts[("assistant", round(t["reply_ts"], 5))] = t
    rows = []
    for b in load_bookmarks(folder):
        t = by_ts.get((b["kind"], round(float(b["ts"]), 5)))
        if t is not None:
            rows.append({**b, "n": t["n"]})
    rows.sort(key=lambda r: (r["n"], r["kind"] != "user"))
    return rows


def messages_through_prompt(messages: list[dict[str, Any]], upto: int) -> list[dict[str, Any]]:
    """The conversation up to and including the reply to prompt number ``upto`` (1-based): what a branch keeps."""
    from axon.session.store import is_user_prompt
    idxs = [i for i, m in enumerate(messages) if isinstance(m, dict) and is_user_prompt(m)]
    if upto < 1 or upto > len(idxs):
        raise ValueError(f"no prompt #{upto} (have {len(idxs)})")
    return list(messages[:idxs[upto]] if upto < len(idxs) else messages)


def last_message_ref(items_with_ts: list[dict[str, Any]], kind: str = "assistant") -> dict[str, Any] | None:
    """The newest item of ``kind`` that has a time: what ``/bookmark`` marks."""
    for it in reversed(items_with_ts):
        if it["kind"] == kind and it.get("ts"):
            return it
    return None
