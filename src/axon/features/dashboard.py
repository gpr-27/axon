"""
`axon dash` / `/dash`: a local browser dashboard (usage charts, sessions, replies, extensions, sharing).

    axon dash [--port N] [--no-open] [--workspace DIR]

Security model (all of it is tested in tests/test_dashboard.py)
  * binds 127.0.0.1 only; there is no option to listen anywhere else
  * the URL carries a random one-time-style token; ``/?t=<token>`` is exchanged for an
    ``HttpOnly; SameSite=Strict`` cookie and redirects to ``/`` (no token left in the address bar)
  * every request needs a loopback ``Host`` header (DNS rebinding) and the cookie; every POST
    additionally needs a same-origin ``Origin`` header and a JSON body (CSRF)
  * strict Content-Security-Policy (no inline script, no remote loads), nosniff, no-store
  * transcript text is untrusted: it is escaped server-side before any HTML is built, secrets are
    redacted from everything that leaves the process, and session ids are looked up in an index
    (never joined into a path)
  * replying never auto-runs tools needing approval: the session runs in ``default`` mode and each
    approval is surfaced in the page; the nothing-leaves-the-machine rule holds except for the
    explicit "secret gist" action, which uploads only the exact file previewed, after a confirm click
"""
from __future__ import annotations

import argparse
import difflib
import errno
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from axon.features.dash_common import DashError
from axon.features.server import is_loopback

DEFAULT_PORT = 8787
LOOPBACK_HOST = "127.0.0.1"
COOKIE_NAME = "axon_dash"
MAX_BODY = 1024 * 1024                 # JSON bodies; file saves and pasted-text uploads have their own limits below
MAX_SAVE_BODY = 4 * 1024 * 1024
MAX_TEXT_BODY = 96 * 1024 * 1024
MAX_UPLOAD = 64 * 1024 * 1024
INLINE_CHARS = 50_000          # a message longer than this is collapsed behind "show all"
INLINE_TOOL_CHARS = 20_000
TEXT_PAGE_MAX = 1_000_000
PAGE_BUDGET_CHARS = 1_500_000  # soft cap on the HTML of one transcript page
GIST_MAX_BYTES = 1_000_000
APPROVAL_TIMEOUT_S = 300
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,200}")
REPLY_MODES = ("default", "plan", "acceptEdits", "auto")   # bypass is never offered

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
       "img-src 'self' data:; media-src 'self'; frame-src 'self' http://localhost:* http://127.0.0.1:*; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'; object-src 'none'")


# ─── redaction & escaping ────────────────────────────────────────────────────

REDACTED = "[REDACTED]"
_EXTRA_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?im)^(\s*(?:x-api-key|api-key|authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).+$"),
     r"\1" + REDACTED),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:PASS|CREDENTIAL|PRIVATE|AUTH|DSN|SALT|SIGNING|COOKIE|CONNECTION_STRING|DATABASE_URL)"
                r"[A-Z0-9_]*)(\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\3"), r"\1\2\3" + REDACTED + r"\3"),
]
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,63})\s*=\s*(.*?)\s*$")
_BENIGN = re.compile(r"^(true|false|yes|no|on|off|none|null|\d{1,6}|[a-z]{1,5})$", re.I)


def _mask_env_blocks(text: str) -> str:
    """Two or more consecutive ``NAME=value`` lines look like a pasted .env: mask their values."""
    if "=" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _ENV_LINE.match(lines[j]) and len(lines[j]) < 2000:
            j += 1
        if j - i >= 2:
            for k in range(i, j):
                m = _ENV_LINE.match(lines[k])
                val = m.group(2).strip("'\"")  # type: ignore[union-attr]
                if val and not _BENIGN.match(val) and val != REDACTED:
                    lines[k] = f"{m.group(1)}={REDACTED}"  # type: ignore[union-attr]
            out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def redact_text(text: Any, home: bool = False) -> str:
    """Secrets masked: Axon's journal redactor plus header lines, auth-ish assignments and .env blocks."""
    s = str(text if text is not None else "")
    if not s:
        return s
    try:
        from axon.session.activity import redact_text as base
        s = base(s)
    except Exception:  # pragma: no cover - the redactor is part of the package
        pass
    for rx, repl in _EXTRA_PATTERNS:
        s = rx.sub(repl, s)
    s = _mask_env_blocks(s)
    if home:
        try:
            h = str(Path.home())
            if len(h) > 3:
                s = s.replace(h, "~")
        except Exception:
            pass
    return s


def scrub_wire(text: str) -> str:
    """Journal redactor for one plain string (never for serialized JSON: its patterns would run on past
    the end of a JSON string and eat structure)."""
    try:
        from axon.session.activity import redact_text as base
        return base(text)
    except Exception:  # pragma: no cover
        return text


def scrub_obj(obj: Any, key: str = "") -> Any:
    """Safety net before serialization: redact every string value. Server-rendered HTML ('html' keys) was
    built from already redacted text and is left alone so the scan cannot damage markup."""
    if isinstance(obj, str):
        return obj if key in ("html", "content", "cost", "tokens", "donut") else scrub_wire(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v, key) for v in obj]
    return obj


_TAG_SPLIT = re.compile(r"(<[^>]*>)")


def redact_html_text(doc: str, home: bool = True) -> str:
    """Redact the text nodes of an HTML document (tags are skipped, so a pattern can never run into markup)."""
    parts = _TAG_SPLIT.split(doc)
    for i in range(0, len(parts), 2):
        if parts[i].strip():
            parts[i] = redact_text(parts[i], home=home)
    return "".join(parts)


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# ─── formatting ──────────────────────────────────────────────────────────────

def _usd(v: float) -> str:
    return f"${v:,.2f}" if abs(v) >= 1 else (f"${v:.4f}" if v else "$0.00")


def _tok(n: float) -> str:
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


# ─── markdown (escape first, then format) ────────────────────────────────────

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+#.\-]{0,20})[^`]*$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def md_inline(raw: str) -> str:
    raw = raw.replace("\x00", "")
    spans: list[str] = []

    def code(m: re.Match[str]) -> str:
        spans.append(f"<code>{esc(m.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    s = re.sub(r"`([^`\n]{1,2000})`", code, raw)
    s = esc(s)
    s = re.sub(r"\*\*([^*\n]{1,2000}?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]{1,2000}?)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"~~([^~\n]{1,2000}?)~~", r"<del>\1</del>", s)
    s = re.sub(r"\[([^\]\n]{1,300})\]\((https?://[^\s()<>\"']{1,1000})\)",
               r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], s)


def render_diff(text: str, max_lines: int = 3000) -> str:
    out = []
    for n, line in enumerate(text.split("\n")):
        if n >= max_lines:
            out.append('<span class="ctx">... diff continues ...</span>')
            break
        if line.startswith(("+++", "---", "diff ", "index ")):
            cls = "hdr"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append(f'<span class="{cls}">{esc(line)}</span>')
    return '<pre class="diff">' + "".join(out) + "</pre>"


def looks_like_diff(text: str) -> bool:
    head = text[:4000]
    return bool(re.search(r"(?m)^(@@ -\d+|diff --git |--- \S.*\n\+\+\+ )", head))


def render_markdown(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\x00", "").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    def starts_block(idx: int) -> bool:
        ln = lines[idx]
        return bool(_FENCE.match(ln) or _HEAD.match(ln) or _HR.match(ln) or _ITEM.match(ln) or ln.lstrip().startswith(">"))

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fm = _FENCE.match(line)
        if fm:
            fence, lang = fm.group(1), fm.group(2).lower()
            body: list[str] = []
            i += 1
            while i < n and not (lines[i].strip().startswith(fence[0] * len(fence)) and not lines[i].strip().strip(fence[0])):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body)
            if lang in ("diff", "patch") or (not lang and looks_like_diff(code)):
                out.append(render_diff(code))
            else:
                cls = f' class="lang-{esc(lang)}"' if lang else ""
                out.append(f'<pre class="code"><code{cls}>{esc(code)}</code></pre>')
            continue
        hm = _HEAD.match(line)
        if hm:
            lvl = min(6, len(hm.group(1)) + 1)
            out.append(f"<h{lvl}>{md_inline(hm.group(2))}</h{lvl}>")
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            q: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                q.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(q))}</blockquote>")
            continue
        im = _ITEM.match(line)
        if im:
            ordered = im.group(2)[0].isdigit()
            items: list[str] = []
            while i < n:
                m2 = _ITEM.match(lines[i])
                if not m2:
                    if lines[i].startswith("  ") and lines[i].strip() and items:
                        items[-1] += " " + lines[i].strip()
                        i += 1
                        continue
                    break
                depth = min(3, len(m2.group(1).replace("\t", "  ")) // 2)
                items.append(f'<li class="d{depth}">{md_inline(m2.group(3))}</li>')
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            def cells(ln: str) -> list[str]:
                return [c.strip() for c in ln.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i]))
                i += 1
            t = "<div class=\"tw\"><table><thead><tr>" + "".join(f"<th>{md_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
            t += "".join("<tr>" + "".join(f"<td>{md_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(t + "</tbody></table></div>")
            continue
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not starts_block(i):
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(md_inline(p) for p in para) + "</p>")
    return "\n".join(out)


# ─── transcripts ─────────────────────────────────────────────────────────────

def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(str(b.get("text", "")))
                elif t == "image":
                    parts.append("[image]")
                elif t == "tool_result":
                    parts.append(_blocks_text(b.get("content")))
                elif "text" in b:
                    parts.append(str(b["text"]))
    return "\n".join(p for p in parts if p)


@dataclass
class Transcript:
    entries: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    model: str = ""


def parse_transcript(path: Path) -> Transcript:
    from axon.session.store import is_user_prompt, iter_records
    tr = Transcript()
    tools: dict[str, dict[str, Any]] = {}
    prompts: list[int | None] = []      # entry index of each prompt still in the conversation (mirrors SessionStore.user_message_times)
    for rec in iter_records(path):
        t, data, ts = rec.get("type"), rec.get("data"), rec.get("timestamp")
        if t == "user_message" and isinstance(data, dict):
            text = _blocks_text(data.get("content"))
            if text.strip():
                tr.entries.append({"kind": "user", "ts": ts, "text": text})
                prompts.append(len(tr.entries) - 1)
        elif t == "rewind" and isinstance(data, dict) and isinstance(data.get("messages"), list):
            # a rewound conversation no longer shows the prompts it dropped (the file keeps them: /history all)
            n = sum(1 for m in data["messages"] if isinstance(m, dict) and is_user_prompt(m))
            if data.get("mode") in ("conversation", "code_and_conversation") and n < len(prompts) and prompts[n] is not None:
                del tr.entries[prompts[n]:]
                del prompts[n:]
        elif t == "compaction" and isinstance(data, dict) and isinstance(data.get("messages"), list):
            n = sum(1 for m in data["messages"] if isinstance(m, dict) and is_user_prompt(m))
            prompts = [None] * max(0, n - len(prompts)) + (prompts[-n:] if n else [])
        elif t == "turn_failed" and isinstance(data, dict) and data.get("rolled_back") and prompts:
            prompts.pop()
            tr.entries.append({"kind": "note", "ts": ts, "text": "This turn failed and was rolled back."})
        elif t == "assistant_turn" and isinstance(data, dict):
            ent: dict[str, Any] = {
                "kind": "assistant", "ts": ts, "text": str(data.get("text") or ""),
                "thinking": str(data.get("thinking") or "") if isinstance(data.get("thinking"), (str, type(None))) else "",
                "model": data.get("model") or "", "cost": data.get("cost"), "usage": data.get("usage") or {},
                "duration": data.get("duration_s"), "stop": data.get("stop_reason") or "", "tools": [],
            }
            tr.model = ent["model"] or tr.model
            for tu in data.get("tool_uses") or []:
                if not isinstance(tu, dict):
                    continue
                tool = {"id": str(tu.get("id") or ""), "name": str(tu.get("name") or "tool"),
                        "input": tu.get("input") if isinstance(tu.get("input"), dict) else {"value": tu.get("input")},
                        "result": None, "is_error": False, "permission": None, "duration": None}
                ent["tools"].append(tool)
                if tool["id"]:
                    tools[tool["id"]] = tool
            if ent["text"].strip() or ent["thinking"].strip() or ent["tools"] or ent["usage"]:
                tr.entries.append(ent)
        elif t == "tool_results" and isinstance(data, list):
            for r in data:
                if isinstance(r, dict):
                    tool = tools.get(str(r.get("tool_use_id")))
                    if tool is not None:
                        tool["result"] = _blocks_text(r.get("content"))
                        tool["is_error"] = bool(r.get("is_error"))
                        tool["duration"] = r.get("duration_s")
        elif t == "permission" and isinstance(data, dict):
            tool = tools.get(str(data.get("tool_use_id")))
            if tool is not None:
                tool["permission"] = {"outcome": str(data.get("outcome") or ""), "reason": str(data.get("reason") or ""),
                                      "source": str(data.get("source") or ""), "approval": data.get("approval")}
        elif t == "session_rename" and isinstance(data, dict):
            tr.title = str(data.get("title") or tr.title)
        elif t == "steer_message" and isinstance(data, dict):
            tr.entries.append({"kind": "note", "ts": ts, "text": "Sent mid-turn: " + str(data.get("text") or "")})
        elif t == "turn_failed":
            tr.entries.append({"kind": "note", "ts": ts, "text": "This turn failed and was rolled back."})
    for i, e in enumerate(tr.entries):
        e["i"] = i
    return tr


_CACHE: "OrderedDict[str, tuple[int, int, Transcript]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_RED_CACHE: dict[tuple[int, int, str], str] = {}


def load_transcript(path: Path) -> Transcript:
    st = path.stat()
    key = str(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            _CACHE.move_to_end(key)
            return hit[2]
    tr = parse_transcript(path)
    with _CACHE_LOCK:
        _CACHE[key] = (st.st_mtime_ns, st.st_size, tr)
        while len(_CACHE) > 6:
            _CACHE.popitem(last=False)
    return tr


def tool_input_text(name: str, inp: dict[str, Any]) -> tuple[str, str]:
    """(kind, text) where kind is 'diff' or 'text'."""
    if isinstance(inp.get("old_string"), str) and isinstance(inp.get("new_string"), str):
        a, b = inp["old_string"].splitlines(), inp["new_string"].splitlines()
        p = str(inp.get("file_path") or inp.get("path") or "file")
        diff = list(difflib.unified_diff(a, b, f"a/{p}", f"b/{p}", lineterm="", n=2))
        return "diff", "\n".join(diff)
    if name.lower() == "bash" and isinstance(inp.get("command"), str):
        return "text", inp["command"]
    parts = []
    for k, v in inp.items():
        if isinstance(v, str) and ("\n" in v or len(v) > 100):
            parts.append(f"{k}:\n{v}")
        elif isinstance(v, str):
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    return "text", "\n".join(parts)


def tool_summary(name: str, inp: dict[str, Any]) -> str:
    for k in ("file_path", "path", "command", "pattern", "query", "url", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:90]
    return ""


def _field_value(entry: dict[str, Any], fld: str) -> tuple[str, str]:
    """(kind, text) of one collapsible field of an entry."""
    if fld in ("text", "thinking"):
        return "md" if fld == "text" else "text", str(entry.get(fld) or "")
    m = re.fullmatch(r"t(\d{1,4})\.(in|out)", fld)
    if m and entry.get("kind") == "assistant":
        idx = int(m.group(1))
        if idx < len(entry["tools"]):
            tool = entry["tools"][idx]
            if m.group(2) == "in":
                return tool_input_text(tool["name"], tool["input"])
            res = tool.get("result") or ""
            return ("diff" if looks_like_diff(res) else "text"), res
    raise DashError("no such field", 404)


def _clip(sid: str, entry: dict[str, Any], fld: str, limit: int, redact: bool = True) -> str:
    kind, raw = _field_value(entry, fld)
    total = len(raw)
    lines = raw.count("\n") + 1 if total > limit else 0
    shown = raw[:limit + 512]          # redact only what is shown (a 4 MB message must not cost a 4 MB scan)
    if redact:
        shown = redact_text(shown)
    shown = shown[:limit]
    if kind == "md":
        body = render_markdown(shown)
    elif kind == "diff":
        body = render_diff(shown)
    else:
        body = f'<pre class="out">{esc(shown)}</pre>'
    if total > limit:
        return (f'<div class="clip"><div class="cbody">{body}</div><div class="clipped"><span>Showing the first {limit:,} of '
                f'{total:,} characters ({lines:,} lines).</span> <button type="button" class="more" data-entry="{entry["i"]}" '
                f'data-field="{esc(fld)}" data-offset="0" data-total="{total}">Show all</button></div></div>')
    return body


def _usage_line(e: dict[str, Any]) -> str:
    u = e.get("usage") or {}
    bits = []
    if u:
        bits.append(f"in {_tok(u.get('input', 0))}")
        bits.append(f"out {_tok(u.get('output', 0))}")
        if u.get("cache_read"):
            bits.append(f"cached {_tok(u['cache_read'])}")
    if isinstance(e.get("cost"), (int, float)):
        bits.append(_usd(float(e["cost"])))
    if isinstance(e.get("duration"), (int, float)):
        bits.append(f"{float(e['duration']):.1f}s")
    if e.get("model"):
        bits.append(str(e["model"]))
    return " · ".join(bits)


def render_entry(sid: str, e: dict[str, Any], limit: int = INLINE_CHARS, tool_limit: int = INLINE_TOOL_CHARS) -> str:
    kind = e["kind"]
    if kind == "note":
        return f'<div class="note">{esc(redact_text(e["text"])[:500])}</div>'
    if kind == "user":
        return f'<div class="who">you</div><div class="md">{_clip(sid, e, "text", limit)}</div>'
    parts = ['<div class="who">axon</div>']
    if e.get("thinking", "").strip():
        parts.append(f'<details class="think"><summary>Thinking</summary>{_clip(sid, e, "thinking", tool_limit)}</details>')
    if e.get("text", "").strip():
        parts.append(f'<div class="md">{_clip(sid, e, "text", limit)}</div>')
    for n, tool in enumerate(e["tools"]):
        perm = tool.get("permission") or {}
        outcome = perm.get("outcome", "")
        denied = outcome.lower().startswith(("den", "block", "reject"))
        cls = "tool" + (" err" if tool.get("is_error") else "") + (" denied" if denied else "")
        status = "denied" if denied else ("error" if tool.get("is_error") else ("ok" if tool.get("result") is not None else "no result"))
        summ = tool_summary(tool["name"], tool["input"])
        parts.append(
            f'<details class="{cls}"><summary><span class="tn">{esc(tool["name"])}</span>'
            f'<span class="ta">{esc(redact_text(summ))}</span><span class="ts">{esc(status)}</span></summary>'
            + (f'<div class="reason">{esc(redact_text(perm.get("reason", "")))}</div>' if denied and perm.get("reason") else "")
            + f'<div class="lbl">input</div>{_clip(sid, e, f"t{n}.in", tool_limit)}'
            + (f'<div class="lbl">result</div>{_clip(sid, e, f"t{n}.out", tool_limit)}' if tool.get("result") is not None else "")
            + "</details>")
    foot = _usage_line(e)
    if foot:
        parts.append(f'<div class="turn">{esc(foot)}</div>')
    return "".join(parts)


def _clip_val(v: Any, limit: int) -> tuple[str, int]:
    """(redacted text cut to ``limit``, full length) of one field the page script draws itself."""
    raw = str(v or "")
    return redact_text(raw[:limit + 512])[:limit], len(raw)


def entry_data(e: dict[str, Any], limit: int = INLINE_CHARS, tool_limit: int = INLINE_TOOL_CHARS) -> dict[str, Any] | None:
    """One assistant entry (a model call and its tool calls) as plain data, so the page draws a stored turn with
    the same code as a live one. Redacted and cut like ``render_entry``; ``*_total`` says how much was cut."""
    if e.get("kind") != "assistant":
        return None
    text, text_total = _clip_val(e.get("text"), limit)
    think, _ = _clip_val(e.get("thinking"), tool_limit)
    tools: list[dict[str, Any]] = []
    for tool in e.get("tools") or []:
        inp = tool.get("input") if isinstance(tool.get("input"), dict) else {}
        clean: dict[str, Any] = {}
        for k, v in inp.items():
            clean[str(k)] = _clip_val(v, tool_limit)[0] if isinstance(v, str) else v
        if len(json.dumps(clean, default=str)) > 4 * tool_limit:
            clean = {"input": _clip_val(json.dumps(inp, default=str), tool_limit)[0]}
        res = tool.get("result")
        out, out_total = _clip_val(res, tool_limit) if res is not None else (None, 0)
        perm = tool.get("permission") or {}
        tools.append({"id": tool.get("id") or "", "name": tool.get("name") or "tool", "input": redact_obj(clean),
                      "result": out, "result_total": out_total, "is_error": bool(tool.get("is_error")),
                      "permission": ({"outcome": perm.get("outcome", ""), "reason": redact_text(perm.get("reason", "")),
                                      "source": perm.get("source", ""), "approval": perm.get("approval")} if perm else None),
                      "duration": tool.get("duration") if isinstance(tool.get("duration"), (int, float)) else None})
    return {"ts": e.get("ts"), "model": e.get("model") or "", "cost": e.get("cost") if isinstance(e.get("cost"), (int, float)) else None,
            "usage": e.get("usage") or {}, "duration": e.get("duration") if isinstance(e.get("duration"), (int, float)) else None,
            "stop": e.get("stop") or "", "thinking": think, "text": text, "text_total": text_total, "tools": tools}


def redact_obj(v: Any) -> Any:
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        return {k: redact_obj(x) for k, x in v.items()}
    if isinstance(v, list):
        return [redact_obj(x) for x in v]
    return v


def transcript_page(sid: str, tr: Transcript, before: int | None, after: int | None, limit: int) -> dict[str, Any]:
    total = len(tr.entries)
    limit = max(1, min(limit, 200))
    if after is not None:
        rng = list(range(max(0, after + 1), total))[:limit]
    else:
        end = total if before is None else max(0, min(before, total))
        rng = list(range(max(0, end - limit), end))
    out: list[dict[str, Any]] = []
    budget = 0
    ordered = rng if after is not None else list(reversed(rng))
    for i in ordered:
        h = render_entry(sid, tr.entries[i])
        if out and budget + len(h) > PAGE_BUDGET_CHARS:
            break
        budget += len(h)
        out.append({"i": i, "kind": tr.entries[i]["kind"], "html": h, "data": entry_data(tr.entries[i])})
    out.sort(key=lambda x: x["i"])
    first = out[0]["i"] if out else (rng[0] if rng else 0)
    return {"entries": out, "total": total, "first": first, "has_before": first > 0}


def field_text(tr: Transcript, entry: int, fld: str, offset: int, limit: int) -> dict[str, Any]:
    if not 0 <= entry < len(tr.entries):
        raise DashError("no such entry", 404)
    key = (id(tr), entry, fld)
    with _CACHE_LOCK:
        text = _RED_CACHE.get(key)
    if text is None:
        text = redact_text(_field_value(tr.entries[entry], fld)[1])
        with _CACHE_LOCK:
            _RED_CACHE[key] = text
            while len(_RED_CACHE) > 3:
                _RED_CACHE.pop(next(iter(_RED_CACHE)))
    offset = max(0, offset)
    limit = max(1, min(limit, TEXT_PAGE_MAX))
    chunk = text[offset:offset + limit]
    return {"text": chunk, "offset": offset, "next": offset + len(chunk), "total": len(text),
            "done": offset + len(chunk) >= len(text)}


# ─── session index ───────────────────────────────────────────────────────────

@dataclass
class SessionRef:
    id: str
    path: Path
    project_key: str
    project: str
    workspace: str
    meta: Any = None
    sub: int | None = None        # subagent number when this ref is a subagent transcript


def held_by(session_dir: Path) -> dict[str, Any] | None:
    """The other live process writing this session (never this process), from the writer lease."""
    try:
        from axon.session import lease
        info = lease.holder(session_dir)
    except Exception:
        return None
    if info is None or info.get("pid") == os.getpid():
        return None
    return {k: info.get(k) for k in ("pid", "owner", "host", "started")}


def _project_key(ws: Path) -> str:
    from axon import storage
    return storage.project_key(ws)


class SessionIndex:
    LIMIT_PER_PROJECT = 5000

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self._lock = threading.Lock()
        self._refs: dict[str, dict[str, SessionRef]] = {}
        self._at: dict[str, float] = {}
        self.cut: dict[str, list[str]] = {}      # scope -> names of projects that have more than LIMIT_PER_PROJECT chats (older ones are not listed)

    def _build(self, scope: str) -> dict[str, SessionRef]:
        from axon import storage
        from axon.features.analytics import _project_name
        from axon.session.store import SessionStore
        refs: dict[str, SessionRef] = {}
        cur_key = _project_key(self.workspace)
        cut: list[str] = []

        def add(store: Any, key: str, ws: str) -> None:
            metas = store.list_recent(limit=self.LIMIT_PER_PROJECT + 1, all_workspaces=scope == "all")
            if len(metas) > self.LIMIT_PER_PROJECT:       # one extra is read only to know that older chats exist
                metas = metas[:self.LIMIT_PER_PROJECT]
                cut.append(_project_name(key, ws))
            for m in metas:
                if SESSION_ID_RE.fullmatch(m.session_id) and m.session_id not in refs:
                    refs[m.session_id] = SessionRef(m.session_id, Path(m.path), key, _project_name(key, ws), ws, m)

        try:
            add(SessionStore(self.workspace), cur_key, str(self.workspace))
        except OSError:
            pass            # a full or read-only disk: the list is empty rather than the whole page failing
        if scope == "all":
            root = storage.axon_home() / "projects"
            try:
                dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name != cur_key)
            except OSError:
                dirs = []
            for d in dirs:
                sdir = d / "sessions"
                if not sdir.is_dir():
                    continue
                ws = ""
                try:
                    ws = str(json.loads((d / "project.json").read_text(encoding="utf-8")).get("workspace") or "")
                except (OSError, ValueError, AttributeError):
                    pass
                try:
                    add(SessionStore(Path(ws) if ws else self.workspace, session_dir=sdir), d.name, ws)
                except Exception:
                    continue
        self.cut[scope] = cut
        return refs

    def refs(self, scope: str = "project", fresh: bool = False) -> dict[str, SessionRef]:
        scope = "all" if scope == "all" else "project"
        with self._lock:
            if fresh or scope not in self._refs or time.time() - self._at.get(scope, 0) > 15:
                self._refs[scope] = self._build(scope)
                self._at[scope] = time.time()
            return self._refs[scope]

    def lookup(self, sid: str) -> SessionRef:
        if not SESSION_ID_RE.fullmatch(sid or "") or sid in (".", ".."):
            raise DashError("invalid session id", 400)
        m = re.fullmatch(r"(.+)_sub_(\d{1,4})", sid)
        if m:    # a subagent transcript: <parent>/subagents/<n>.jsonl, found through its parent
            parent = self.lookup(m.group(1))
            path = parent.path.parent / "subagents" / f"{int(m.group(2))}.jsonl"
            if not path.is_file():
                flat = parent.path.parent / f"{sid}.jsonl"
                path = flat if flat.is_file() else path
            if not path.is_file():
                raise DashError("subagent transcript not found", 404)
            return SessionRef(sid, path, parent.project_key, parent.project, parent.workspace, parent.meta, int(m.group(2)))
        for scope, fresh in (("all", False), ("all", True)):
            ref = self.refs(scope, fresh).get(sid)
            if ref is not None:
                return ref
        raise DashError("session not found", 404)


# ─── overview ────────────────────────────────────────────────────────────────

def overview_data(workspace: Path, period: str = "30d", scope: str = "project", stats: Any = None) -> dict[str, Any]:
    from axon.features import analytics as an
    per = an.parse_period(period) or an.parse_period("30d")
    key = _project_key(workspace) if scope != "all" else None
    if stats is None:
        stats = an.collect(per, project=key, scope="all projects" if scope == "all" else f"project {workspace.name}")
    t = stats.totals
    api_time = sum(stats.latencies)
    wall = sum(s.duration_s for s in stats.sessions)
    out: dict[str, Any] = {
        "period": per.key, "period_label": per.label, "scope": "all" if scope == "all" else "project",
        "scope_label": stats.scope, "empty": stats.empty,
        "kpis": {
            "cost": t.cost, "cost_text": an.fmt_usd(t.cost), "calls": t.calls, "sessions": len(stats.sessions),
            "tokens_in": t.input, "tokens_out": t.output, "cache_read": t.cache_read, "cache_write": t.cache_write,
            "hit_pct": t.hit_pct, "savings": t.savings, "savings_text": an.fmt_usd(t.savings),
            "api_s": api_time, "wall_s": wall, "avg_latency": stats.avg_latency, "p95_latency": stats.p95_latency,
            "has_latency": bool(stats.latencies), "per_day": t.cost / max(1, len(stats.days)),
        },
    }
    if stats.empty:
        return out
    pts = [(d.strftime("%m-%d"), a.cost, f"{d.strftime('%a %Y-%m-%d')}: {an.fmt_usd(a.cost)} · {a.calls} calls")
           for d, a in stats.days]
    series = ["Uncached input", "Output", "Cache read", "Cache write"]
    cols = [(d.strftime("%m-%d"), (a.uncached_input, a.output, a.cache_read, a.cache_write),
             f"{d.strftime('%a %Y-%m-%d')}: {an.fmt_tokens(a.uncached_input)} uncached in · {an.fmt_tokens(a.output)} out · "
             f"{an.fmt_tokens(a.cache_read)} cache read · {an.fmt_tokens(a.cache_write)} cache write")
            for d, a in stats.days]
    top = stats.by_model[:5]
    other = sum(a.cost for _, a in stats.by_model[5:])
    parts = [(m, a.cost) for m, a in top] + ([("Other", other)] if other > 0 else [])
    out["charts"] = {
        "cost": an.svg_line_chart(pts), "tokens": an.svg_stacked_bars(cols, series), "token_series": series,
        "donut": an.svg_donut(parts, an.fmt_usd(t.cost)),
        "donut_parts": [{"name": m, "cost": v, "pct": (v / t.cost * 100 if t.cost else 0)} for m, v in parts],
    }

    def row(name: str, a: Any, label: str | None = None) -> dict[str, Any]:
        return {"name": label or name, "calls": a.calls, "input": a.input, "output": a.output,
                "cache_read": a.cache_read, "cache_write": a.cache_write, "cost": a.cost, "hit_pct": a.hit_pct}

    out["by_model"] = [row(m, a) for m, a in stats.by_model]
    out["by_source"] = [row(s, a, an._source_label(s)) for s, a in stats.by_source]
    out["tools"] = [{"name": a.name, "calls": a.calls, "total_s": a.total_s, "errors": a.errors} for a in stats.tools[:30]]
    out["turns"] = [{"prompt": redact_text(an._clean(x.prompt, 160)), "model": x.model, "tokens": x.tokens,
                     "tools": x.tool_calls, "seconds": x.duration_s, "cost": x.cost} for x in stats.turns[:15]]
    out["stats_obj_ok"] = True
    return out


def overview_export_html(workspace: Path, period: str, scope: str) -> str:
    from axon.features import analytics as an
    per = an.parse_period(period) or an.parse_period("30d")
    key = _project_key(workspace) if scope != "all" else None
    stats = an.collect(per, project=key, scope="all projects" if scope == "all" else f"project {workspace.name}")
    return redact_html_text(an.render_html(stats))


# ─── extensions & settings ───────────────────────────────────────────────────

def _mask_settings(obj: Any, key: str = "") -> Any:
    secretish = re.compile(r"(?i)(key|token|secret|password|passwd|authorization|credential)")
    if isinstance(obj, dict):
        return {str(k): _mask_settings(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mask_settings(v, key) for v in obj]
    if isinstance(obj, str):
        if obj and secretish.search(key) and not key.endswith(("_tokens", "tokens")):
            return "•••• (hidden)"
        return redact_text(obj)
    return obj


def settings_view(settings: Any) -> dict[str, Any]:
    try:
        data = settings.model_dump(mode="json")
    except Exception as e:
        return {"error": f"{type(e).__name__}"}
    data["api_key"] = "•••• (hidden)"
    return _mask_settings(data)


def extensions_data(workspace: Path, agent: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def guard(name: str, fn: Callable[[], Any]) -> None:
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = []
            out.setdefault("problems", []).append(f"{name}: {type(e).__name__}")

    def mcp() -> list[dict[str, Any]]:
        from axon.mcp.config import MCPConfig
        specs = MCPConfig(workspace).load_specs()
        live: dict[str, dict[str, Any]] = {}
        try:
            from axon.mcp.bridge import get_bridge
            for r in get_bridge(workspace).status_rows():
                live[r["name"]] = r
        except Exception:
            pass
        rows = []
        for name, s in sorted(specs.items()):
            lv = live.get(name, {})
            status = lv.get("status") or ("disabled" if s.disabled else "not started")
            rows.append({"name": name, "type": s.type, "target": redact_text(s.describe_target()), "scope": s.scope,
                         "status": status, "tools": lv.get("tools"), "error": redact_text(lv.get("error") or ""),
                         "disabled": bool(s.disabled), "approval": getattr(s, "approval", "") or ""})
        return rows

    def skills() -> list[dict[str, Any]]:
        from axon.skills.manager import _manager_for
        return [{"name": k, "scope": s.scope, "description": s.description[:240], "plugin": s.plugin,
                 "invocable": s.user_invocable} for k, s in sorted(_manager_for(workspace).skills.items())]

    def plugins() -> list[dict[str, Any]]:
        from axon.commands.plugins import load_plugins
        return [{"name": p.id, "version": p.version, "scope": p.scope, "enabled": p.enabled,
                 "description": p.description[:240], "components": p.components()} for p in load_plugins(workspace)]

    def hooks() -> list[dict[str, Any]]:
        hm = getattr(agent, "hooks", None) if agent is not None else None
        if hm is None or not hasattr(hm, "describe"):
            from axon.hooks.manager import HookManager
            hm = HookManager(getattr(agent, "settings", None), workspace)
        return [{"event": h["event"], "matcher": h["matcher"], "type": h["type"], "source": redact_text(h["source"]),
                 "command": redact_text(h["command"])[:300], "timeout": h["timeout"]} for h in hm.describe()]

    def agents() -> list[dict[str, Any]]:
        from axon.agent.subagent import load_agent_definitions
        return [{"name": a.name, "description": (a.description or "")[:240], "model": a.model or "inherit",
                 "source": str(a.source), "readonly": a.readonly} for a in sorted(load_agent_definitions(workspace), key=lambda a: a.name)]

    def commands() -> list[dict[str, Any]]:
        from axon.commands.registry import list_command_names
        return [{"name": n, "description": d[:200]} for n, d in list_command_names(workspace, agent)]

    for name, fn in (("mcp", mcp), ("skills", skills), ("plugins", plugins), ("hooks", hooks),
                     ("agents", agents), ("commands", commands)):
        guard(name, fn)
    try:
        from axon.features.dash_ext import trust_view
        out["trust"] = trust_view(workspace)
    except Exception as e:
        out["trust"] = {"status": "unknown", "allowed": True, "items": [], "error": type(e).__name__}

    def settings() -> dict[str, Any]:
        s = getattr(agent, "settings", None) if agent is not None else None
        if s is None:
            from axon.config import Settings
            s = Settings.load({"workspace": workspace}, require_api_key=False)
        return settings_view(s)

    try:
        out["settings"] = settings()
    except Exception as e:
        out["settings"] = {"error": f"could not load settings ({type(e).__name__})"}
    return out


# ─── sharing ─────────────────────────────────────────────────────────────────

def _theme_tokens() -> tuple[dict[str, str], dict[str, str]]:
    """(dark, light) CSS custom properties. Prefers axon.ui.brand; falls back to the terminal theme."""
    try:
        from axon.ui import brand  # type: ignore[attr-defined]
        pal_d, pal_l = brand.palette_hex("axon"), brand.palette_hex("axon-light")
        bg_d, bg_l = brand.BROWSER_BG["axon"], brand.BROWSER_BG["axon-light"]
    except Exception:
        from axon.ui import theme as th

        def hexes(spec: dict[str, Any]) -> dict[str, str]:
            return {k: "#%02x%02x%02x" % v[0] for k, v in spec.items()}
        pal_d = hexes(th._DARK)
        pal_l = hexes({**th._DARK, **th._LIGHT})
        bg_d = {"page": "#0f1218", "surface": "#161a22", "raised": "#1d222c"}
        bg_l = {"page": "#f5f7f9", "surface": "#ffffff", "raised": "#eef1f4"}

    def build(p: dict[str, str], bg: dict[str, str]) -> dict[str, str]:
        g = p.get
        return {
            "--page": bg["page"], "--surface": bg["surface"], "--raised": bg["raised"],
            "--ink": g("TEXT", "#e4e6ec"), "--ink2": g("MUTED", "#8c92a0"), "--muted": g("SUBTLE", "#646a78"),
            "--line": g("BORDER", "#484e5c"), "--primary": g("PRIMARY", "#5ed2c8"), "--accent": "#b4b4b4" if p is pal_d else "#4a4a4a",
            "--ok": g("SUCCESS", "#86de8c"), "--warn": g("WARNING", "#f6c45c"), "--bad": g("ERROR", "#ff7878"),
            "--info": g("INFO", "#7ab6ff"), "--code-bg": g("CODE_BG", bg["raised"]), "--code-fg": g("CODE_FG", "#f0be8c"),
            "--add": g("DIFF_ADD", "#86de8c"), "--del": g("DIFF_DEL", "#ff7878"),
            "--add-bg": g("DIFF_ADD_BG", "#183a24"), "--del-bg": g("DIFF_DEL_BG", "#461c20"),
            "--s1": g("PRIMARY", "#5ed2c8"), "--s2": g("ORANGE", "#e8793c"), "--s3": g("WARNING", "#f6c45c"),
            "--s4": g("INFO", "#7ab6ff"), "--s5": g("SUCCESS", "#86de8c"), "--s6": g("ERROR", "#ff7878"),
            "--s7": g("CYAN", "#6edcf0"), "--s8": g("CYAN", "#6edcf0"),
            "--axon-primary": g("PRIMARY", "#38E8C6"), "--axon-accent": g("PRIMARY", "#38E8C6"),
            "--grid": g("BORDER", "#484e5c"), "--axis": g("SUBTLE", "#646a78"), "--ring": g("BORDER", "#484e5c"),
        }
    return build(pal_d, bg_d), build(pal_l, bg_l)


def logo_svg() -> str:
    try:
        from axon.ui import brand  # type: ignore[attr-defined]
        return str(brand.LOGO_SVG)
    except Exception:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Axon">'
                '<circle cx="16" cy="16" r="5" fill="currentColor"/><path d="M16 11V3M21 19l6 6M11 19l-6 6" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>')


def theme_css() -> str:
    d, l = _theme_tokens()

    def block(t: dict[str, str]) -> str:
        return ";".join(f"{k}:{v}" for k, v in t.items())
    return (f":root{{color-scheme:dark;{block(d)}}}\n"
            f"@media (prefers-color-scheme:light){{:root:not([data-theme=\"dark\"]){{color-scheme:light;{block(l)}}}}}\n"
            f":root[data-theme=\"dark\"]{{color-scheme:dark;{block(d)}}}\n"
            f":root[data-theme=\"light\"]{{color-scheme:light;{block(l)}}}\n")


def session_export_html(ref: SessionRef, tr: Transcript, title: str, include_tools: bool = True) -> str:
    """One chat as a standalone, script-free HTML file (the same writer the Share dialog and /export use)."""
    from axon.features import chat_export as cx
    return cx.build(cx.items_from_entries(tr.entries), {"id": ref.id, "title": title, "model": tr.model}, "html", include_tools,
                    True, redact_text)["content"]


def run_gh_gist(content: str, filename: str, description: str) -> str:
    gh = shutil.which("gh")
    if not gh:
        raise DashError("The GitHub CLI (gh) is not installed or not on PATH.", 501)
    try:
        p = subprocess.run([gh, "gist", "create", "--filename", filename, "--desc", description, "-"],
                           input=content, capture_output=True, text=True, encoding="utf-8", timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DashError(f"gh failed: {type(e).__name__}", 502) from e
    if p.returncode != 0:
        raise DashError("gh gist create failed: " + redact_text((p.stderr or p.stdout).strip())[:300], 502)
    urls = re.findall(r"https://\S+", p.stdout)
    return urls[-1] if urls else p.stdout.strip()


# ─── replying ────────────────────────────────────────────────────────────────

@dataclass
class _Approval:
    event: threading.Event = field(default_factory=threading.Event)
    allow: bool = False


class Run:
    def __init__(self, session_id: str) -> None:
        self.id = secrets.token_hex(8)
        self.session_id = session_id
        self.q: queue.Queue[Any] = queue.Queue()
        self.approvals: dict[str, _Approval] = {}
        self.session: Any = None
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        for a in list(self.approvals.values()):
            a.allow = False
            a.event.set()
        s = self.session
        if s is not None:
            try:
                s.cancel()
            except Exception:
                pass


_STREAM_TYPES = {"system", "text_delta", "thinking_delta", "tool_use", "tool_result", "permission",
                 "llm_call_end", "approval_request", "result", "error", "retry"}


_SLIM_LIMIT = 200_000     # the page shows a tool's whole input and output; only a truly huge one is cut (and says so)


def _slim(ev: dict[str, Any]) -> dict[str, Any] | None:
    t = ev.get("type")
    if t not in _STREAM_TYPES:
        return None
    ev = dict(ev)
    if t == "system":
        ev = {k: ev.get(k) for k in ("type", "subtype", "session_id", "model", "mode")}
    elif t == "tool_result" and isinstance(ev.get("content"), str):
        full = redact_text(ev["content"])
        ev["content"] = full[:_SLIM_LIMIT]
        if len(full) > _SLIM_LIMIT:
            ev["truncated_from"] = len(full)
    elif t == "tool_use":
        shown = redact_text(json.dumps(ev.get("input"), ensure_ascii=False, default=str))
        try:
            ev["input"] = json.loads(shown) if len(shown) <= _SLIM_LIMIT else {"value": shown[:_SLIM_LIMIT]}
        except ValueError:
            ev["input"] = {"value": shown[:_SLIM_LIMIT]}
    elif t == "permission":
        ev = {k: (redact_text(v) if isinstance(v, str) else v) for k, v in ev.items()}
    return ev


def load_secrets() -> tuple[str, str]:
    """The dashboard's (url token, cookie secret), kept in AXON_HOME/dashboard-secret.json (0600) so a restart does not sign
    out every open tab, and so two dashboards on this machine do not overwrite each other's cookie."""
    import tempfile
    from axon import storage
    path = storage.axon_home() / "dashboard-secret.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok, cook = str(data.get("token", "")), str(data.get("cookie", ""))
        if len(tok) >= 24 and len(cook) >= 32 and tok != cook:
            return tok, cook
    except (OSError, ValueError, AttributeError):
        pass
    tok, cook = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dash-secret.", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": tok, "cookie": cook}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass
    return tok, cook


class DashServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256            # the default of 5 reset most of 200 simultaneous connections (two tabs and a few streams are plenty, a burst is not)

    def __init__(self, port: int, workspace: Path, agent: Any = None, token: str | None = None) -> None:
        stored_token, self.cookie = load_secrets()
        self.token = token or stored_token
        self.live: Any = None
        self.agent = agent
        self._workspace = Path(workspace).expanduser().resolve()
        self.index = SessionIndex(self._workspace)
        self.runs: dict[str, Run] = {}
        self.busy: set[str] = set()
        self.exports: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self.lock = threading.RLock()
        self.started_at = time.time()
        from axon.features.dash_runtime import Attachments, CommandRunner, ShellRunner
        self.rts: dict[str, Any] = {}
        self.default_rt: Any = None
        self.commands = CommandRunner(self)
        self.shell = ShellRunner()
        self.attach = Attachments()
        from axon.features.dash_pty import PtyManager
        self.pty = PtyManager()
        from axon.features.dash_phone import PhoneAccess
        self.phone = PhoneAccess(self)
        self._settings_cache: dict[str, Any] = {}
        self.provider_choice: dict[str, Any] = {}      # what the person picked in this page; it beats environment defaults
        super().__init__((LOOPBACK_HOST, port), _Handler)
        if agent is not None:
            from axon.features.dash_runtime import Runtime
            self.live = Runtime("live", agent, self.workspace, external=True)
            self.live.attach()
        try:  # keep the active key out of anything we send, even if a transcript quotes it
            from axon.session.activity import add_secret
            s = getattr(agent, "settings", None)
            if s is not None:
                add_secret(s.api_key.get_secret_value())
        except Exception:
            pass

    @property
    def workspace(self) -> Path:
        """The project shown. When attached to a terminal that is wherever the terminal is now (it may have used /cd)."""
        if self.live is not None:
            now = getattr(getattr(self.agent, "settings", None), "workspace", None)
            if isinstance(now, (str, Path)) and Path(now).resolve() != self._workspace:
                self._workspace = Path(now).resolve()
                self.index = SessionIndex(self._workspace)
                self.live.workspace = self._workspace
        return self._workspace

    @workspace.setter
    def workspace(self, value: Path) -> None:
        self._workspace = Path(value)

    @property
    def can_switch(self) -> bool:
        """Whether the page can move to another folder or chat: on its own, or through the terminal's main loop."""
        from axon.ui.repl_inbox import INBOX
        return self.live is None or INBOX.active

    def on_terminal(self, fn: Any, timeout: float = 30.0) -> Any:
        """Run ``fn(ctx)`` on the terminal's main loop and wait for it."""
        from axon.ui.repl_inbox import INBOX
        if self.live is None or not INBOX.active:
            raise DashError("No terminal session can do this right now.", 409)
        req = INBOX.post(fn)
        if not req.done.wait(timeout):
            raise DashError("The terminal did not respond. Close any menu open there and try again.", 409)
        if req.error is not None:
            raise DashError(f"The terminal could not do that: {req.error}", 409)
        return req.result

    def switch_live(self, sid: str) -> dict[str, Any]:
        """Move the attached terminal to chat ``sid``, so the terminal and the page show the same conversation.
        A chat from another project moves the terminal to that folder too."""
        live = self.live
        if live is None:
            raise DashError("This page is not attached to a terminal session.", 409)
        if sid == live.session_id:
            return live.state()
        if live.busy:
            raise DashError("The terminal is working. Stop the turn before switching chats.", 409)
        ref = self.index.lookup(sid)
        other = held_by(ref.path.parent)
        if other:
            raise DashError(f"This session is open in another terminal (pid {other['pid']}).", 423)
        target = Path(ref.workspace).resolve() if ref.workspace and Path(ref.workspace).is_dir() else None

        def go(ctx: Any) -> None:
            from axon.commands.session_cmds import handle_resume_cmd
            if target is not None and target != self._workspace:
                from axon.commands.workspace_cmds import change_workspace
                change_workspace(ctx.agent, target)
            handle_resume_cmd(ctx.agent, sid)
            ctx.redraw()

        self.on_terminal(go)
        if live.session_id != sid:
            raise DashError("The terminal could not open that chat.", 409)
        return live.state()

    def new_live_chat(self) -> dict[str, Any]:
        """A new chat from the page starts a new session in the attached terminal."""
        def go(ctx: Any) -> None:
            from axon.commands.session_cmds import handle_clear
            handle_clear(ctx.agent, "")
        self.on_terminal(go)
        return self.live.state()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"

    @property
    def open_url(self) -> str:
        return f"{self.url}/?t={self.token}"

    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", f"http://[::1]:{self.port}"}

    def live_session_id(self) -> str:
        try:
            return str(self.agent.session.active_session_id or "") if self.agent is not None else ""
        except Exception:
            return ""

    # ── runtimes ──
    def all_runtimes(self) -> list[Any]:
        with self.lock:
            rts = list(self.rts.values())
            if self.live is not None:
                rts.append(self.live)
            return rts

    def settings_obj(self) -> Any:
        for rt in self.all_runtimes():
            return rt.agent.settings
        key = str(self.workspace)
        if key not in self._settings_cache:
            from axon.features.sdk import AxonOptions, SDKError, build_settings
            try:
                self._settings_cache[key] = build_settings(AxonOptions(workspace=self.workspace, **self.provider_choice))
            except SDKError as e:
                raise DashError(str(e), 409) from None
        return self._settings_cache[key]

    def effective_mode(self) -> str:
        """The permission mode chats use. A runtime knows its own; before one exists a new chat starts in ``default``,
        because the page never starts a chat in bypass, whatever settings.json says. One answer for every label."""
        for rt in self.all_runtimes():
            return str(getattr(rt.agent.settings, "mode", "") or "default")
        return self.startup_mode()

    def startup_mode(self) -> str:
        """The mode a new chat starts in: the saved Permission mode (Settings), except bypass, which the page never offers."""
        try:
            saved = str(getattr(self.settings_obj(), "mode", "") or "default")
        except DashError:
            return "default"
        return saved if saved in REPLY_MODES else "default"

    def _safe_settings(self) -> Any:
        try:
            return self.settings_obj()
        except Exception:
            return None

    def extra_dirs(self) -> list[Path]:
        try:
            return [Path(x) for x in (getattr(self.settings_obj(), "additional_dirs", None) or [])]
        except DashError:
            return []

    def elicit(self, server: str, params: dict[str, Any]) -> dict[str, Any]:
        """An MCP server asked for input: show it in the chat that is working (the bridge is shared by every chat)."""
        rts = [rt for rt in self.all_runtimes() if getattr(rt, "busy", False)] or self.all_runtimes()
        if not rts:
            return {"action": "decline"}
        return rts[-1].elicit(server, params)

    def _build_runtime(self, ws: Path, resume: str | None) -> Any:
        from axon import storage
        from axon.features.dash_runtime import Runtime
        from axon.features.sdk import AxonOptions, SDKError, build_agent
        if resume:
            try:
                other = held_by(self.index.lookup(resume).path.parent)
            except DashError:
                other = None
            if other:
                raise DashError(f"This session is open in another terminal (pid {other['pid']}). Fork it into a new "
                                "session to continue from here.", 423)
        rt = Runtime("", None, ws)
        try:
            from axon.mcp.bridge import get_bridge
            from axon.mcp.elicit import with_hooks
            bridge = get_bridge(ws)
            if bridge.elicitation_handler is None and self.live is None:   # inside a terminal session the terminal asks
                bridge.elicitation_handler = with_hooks(self.elicit)
        except Exception:
            pass
        pc = self.provider_choice or {}
        try:
            agent = build_agent(AxonOptions(workspace=ws, mode=self.startup_mode(), resume=resume, **{"connect_mcp": True, **pc}),
                                  on_event=rt._tee)
        except SDKError as e:
            raise DashError(str(e), 409) from None
        rt.agent = agent
        rt.attached = True
        rt.hook_turns()
        agent.on_approval = rt.approver
        agent.decline_ends_turn = True      # a person answers on the page: a plain No means stop and wait for them
        rt.install_web_tools()
        rt.key = rt.session_id
        try:
            storage.set_active_session(ws, rt.key)
        except Exception:
            pass
        return rt

    def runtime(self, sid: str = "", create: bool = True) -> Any:
        """The runtime showing session *sid*: the REPL's live agent, one this server built, or (create) a new
        one that resumes the stored session. An empty id means the live agent, else the current chat."""
        sid = (sid or "").strip()
        with self.lock:
            if self.live is not None and sid in ("", "live", self.live.session_id):
                return self.live
            if not sid:
                if self.default_rt is None and create:
                    self.default_rt = self._build_runtime(self.workspace, None)
                    self.rts[self.default_rt.key] = self.default_rt
                return self.default_rt
            if sid in self.rts:
                return self.rts[sid]
            for r in self.rts.values():      # a runtime that moved to another session (/clear, /branch) answers to its new id too
                if sid == r.session_id:
                    return r
        if not create:
            return None
        ref = self.index.lookup(sid)
        ws = Path(ref.workspace) if ref.workspace and Path(ref.workspace).is_dir() else self.workspace
        rt = self._build_runtime(ws, sid)
        with self.lock:
            return self.rts.setdefault(rt.key, rt)

    def new_runtime(self) -> Any:
        rt = self._build_runtime(self.workspace, None)
        with self.lock:
            self.rts[rt.key] = rt
            self.default_rt = rt
        return rt

    def set_workspace(self, raw: str) -> dict[str, Any]:
        from axon.permissions.paths import is_protected_path
        p = Path(raw).expanduser()
        try:
            p = p.resolve()
        except OSError:
            raise DashError("cannot resolve that folder") from None
        if not p.is_dir():
            raise DashError("not a folder", 404)
        why = is_protected_path(p)
        if why:
            raise DashError(f"refused: {why}", 403)
        from axon.permissions.paths import too_broad_for_workspace
        broad = too_broad_for_workspace(p)
        if broad:
            raise DashError(broad, 400)
        if self.live is not None:
            if self.live.busy or any(r.busy for r in self.rts.values()):
                raise DashError("A turn is still running. Stop it before changing folders.", 409)

            def go(ctx: Any) -> None:     # the terminal moves too, and starts a fresh chat in the new project
                from axon.commands.session_cmds import handle_clear
                from axon.commands.workspace_cmds import change_workspace
                change_workspace(ctx.agent, p)
                handle_clear(ctx.agent, "")

            self.on_terminal(go)
        with self.lock:
            busy = [r for r in self.rts.values() if r.busy]
            if busy:
                raise DashError("A turn is still running. Stop it before changing folders.", 409)
            for r in list(self.rts.values()):
                r.close()
            self.rts.clear()
            self.default_rt = None
            self.workspace = p
            self.index = SessionIndex(p)
            self.shell.cwds.clear()              # the dock starts in the new project's folder, not where an old `cd` ended
        return {"workspace": str(p), "name": p.name}

    def record_shell(self, job: Any) -> None:
        """A command run from the Shell panel belongs in the record of the chat it sits beside."""
        from axon import storage
        from axon.features.dash_runtime import EventLog
        data = {"command": job.command, "cwd": job.cwd, "code": job.code, "refused": job.refused, "killed": job.killed,
                "duration": round((job.finished or time.time()) - job.started, 2), "started": job.started,
                "output": "".join(job.text), "source": "shell panel"}
        rt = self.live or self.default_rt or next(iter(self.rts.values()), None)
        if rt is not None and rt.event_log() is not None:
            rt.record("shell_command", data)
            return
        log = EventLog(storage.sessions_dir(self.workspace) / storage.UNATTACHED)
        log.append("shell_command", data)
        log.flush()

    def store_export(self, filename: str, content: str) -> str:
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.lock:
            self.exports[sha] = (filename, content)
            self.exports.move_to_end(sha)
            while len(self.exports) > 8:
                self.exports.popitem(last=False)
        return sha

    # ── reply ──
    def reply(self, ref: SessionRef, text: str, mode: str) -> Run | dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise DashError("message is empty")
        if mode not in REPLY_MODES:
            raise DashError(f"mode must be one of {', '.join(REPLY_MODES)}")
        agent = self.agent
        if agent is not None and ref.id == self.live_session_id():
            q = getattr(agent, "message_queue", None)
            if q is None:
                raise DashError("the live session has no message queue", 409)
            busy = bool(getattr(agent, "busy", False))
            q.push(text, steer=busy)
            return {"queued": True, "live": True, "busy": busy,
                    "message": ("Delivered to the running turn." if busy else
                                "Queued on the live terminal session. It runs when that session next reads its queue.")}
        other = held_by(ref.path.parent)
        if other:
            raise DashError(f"This session is open in another terminal (pid {other['pid']}); it is read-only here. "
                            "Fork it into a new session to continue.", 423)
        ws = Path(ref.workspace) if ref.workspace else self.workspace
        if not ws.is_dir():
            raise DashError("this session's project folder no longer exists, so it can't be resumed here", 409)
        with self.lock:
            if ref.id in self.busy:
                raise DashError("a reply is already running for this session", 409)
            self.busy.add(ref.id)
        run = Run(ref.id)
        self.runs[run.id] = run
        threading.Thread(target=self._pump, args=(run, ws, text, mode), name="axon-dash-reply", daemon=True).start()
        return run

    def _pump(self, run: Run, ws: Path, text: str, mode: str) -> None:
        from axon.features.sdk import AxonOptions, AxonSession, SDKError

        def can_use_tool(tool: str, args: dict[str, Any]) -> bool:
            aid = secrets.token_hex(6)
            slot = _Approval()
            run.approvals[aid] = slot
            shown = redact_text(json.dumps(args, ensure_ascii=False, default=str))
            run.q.put({"type": "approval_request", "id": aid, "tool": tool,
                       "input": shown[:3000], "truncated": len(shown) > 3000})
            ok = slot.event.wait(APPROVAL_TIMEOUT_S) and slot.allow and not run.cancelled
            run.approvals.pop(aid, None)
            return bool(ok)

        session = None
        try:
            opts = AxonOptions(workspace=ws, resume=run.session_id, mode=mode, can_use_tool=can_use_tool)
            session = AxonSession(opts)
            run.session = session
            for ev in session.query(text):
                s = _slim(ev)
                if s is not None:
                    run.q.put(s)
        except SDKError as e:
            run.q.put({"type": "error", "message": str(e)})
        except Exception as e:
            run.q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
            with self.lock:
                self.busy.discard(run.session_id)
            run.q.put(None)

    def shutdown_runs(self) -> None:
        for r in list(self.runs.values()):
            r.cancel()
        for r in list(self.rts.values()):
            try:
                r.close()
            except Exception:
                pass
        if self.live is not None:
            self.live.detach()
        try:
            from axon.features import compare_models
            compare_models.drop_all()
        except Exception:
            pass
        try:
            self.shell.kill_all()
            self.pty.close_all()
        except Exception:
            pass


# ─── HTTP ────────────────────────────────────────────────────────────────────

def _disk_message(e: OSError) -> str:
    """What to tell the person when a file could not be read or written, without printing a path."""
    if e.errno == errno.ENOSPC:
        return "The disk is full, so this could not be saved. Free some space and try again."
    if e.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        return "A file Axon needs is read-only or not allowed, so this could not be saved."
    return f"A file could not be read or written ({e.strerror or type(e).__name__})."


def _split_host(value: str) -> tuple[str, int | None]:
    value = (value or "").strip()
    if value.startswith("["):
        end = value.find("]")
        host = value[1:end] if end > 0 else value
        rest = value[end + 1:] if end > 0 else ""
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
    else:
        host, port = value, ""
    try:
        return host, int(port) if port else None
    except ValueError:
        return host, -1


class _Handler(BaseHTTPRequestHandler):
    server: DashServer
    server_version = "axon-dash"
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True          # TCP_NODELAY: a small streamed frame leaves at once instead of waiting for an ACK
    timeout = 120                           # a client that stalls mid-request (half a header, half a body) is dropped instead of holding a thread forever

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # ── output ──
    def _headers(self, ctype: str, length: int | None, extra: dict[str, str] | None = None) -> None:
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        base = {"Cache-Control": "no-store", "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin"}
        base.update(extra or {})                      # a route may replace a header, never repeat one
        for k, v in base.items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self._headers(ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(scrub_obj(payload), ensure_ascii=False, default=str)
        self._send(status, raw.encode("utf-8"), "application/json; charset=utf-8", extra)

    def _err(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # ── checks ──
    @property
    def _lan(self) -> bool:
        """True when this request came in on the phone listener rather than loopback."""
        try:
            return self.server.phone.serves(int(self.request.getsockname()[1]))
        except Exception:
            return False

    def _origins(self) -> set[str]:
        return self.server.phone.origins() if self._lan else self.server.allowed_origins()

    def _host_ok(self) -> bool:
        host, port = _split_host(self.headers.get("Host", ""))
        if self._lan:
            if not self.server.phone.host_ok(host, port):
                self._err(403, "Host header must be this machine's address")
                return False
            return True
        if not is_loopback(host) or (port is not None and port != self.server.port):
            self._err(403, "Host header must be this dashboard's loopback address")
            return False
        return True

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v:
                if self._lan:
                    if self.server.phone.cookie_ok(v):
                        return True
                elif hmac.compare_digest(v.encode(), self.server.cookie.encode()):
                    return True
        return False

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin not in self._origins() or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            self._err(403, "cross-origin request refused")
            return False
        return True

    def _same_site_ok(self) -> bool:
        """The API answers this page only. A request that a browser marks cross-site or same-site (another port on the
        same host, such as a dev server shown in the Browser panel) carries the cookie but must not reach the API."""
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            self._err(403, "cross-origin request refused")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self._origins():
            self._err(403, "cross-origin request refused")
            return False
        return True

    def _lan_blocked(self, route: str) -> bool:
        """The phone listener serves chat, files and commands, not the raw terminal/shell or the phone controls themselves."""
        if not self._lan:
            return False
        from axon.features.dash_phone import BLOCKED_PREFIXES
        if route.startswith(BLOCKED_PREFIXES) or route.startswith("/api/phone"):
            self._err(403, "not available from a phone: use the terminal or the browser on this computer")
            return True
        return False

    def _phone_post(self, body: dict[str, Any]) -> dict[str, Any]:
        ph = self.server.phone
        action = str(body.get("action") or "pair")
        if action == "stop":
            ph.stop()
            return ph.status()
        try:
            ph.start(str(body.get("ip") or "") or None)
        except OSError as e:
            raise DashError(str(e)) from None
        link = ph.pairing_url()
        from axon.features.dash_phone import PAIR_TTL
        from axon.features.qr import qr_svg
        return {**ph.status(), "link": link, "svg": qr_svg(link), "expires_in": int(PAIR_TTL)}

    def _body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            raise DashError("Content-Type must be application/json", 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n > limit:
            left = min(n, 16 * 1024 * 1024)      # read what a browser is still sending, so it can see the 413 instead of a reset
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
        if n <= 0 or n > limit:
            raise DashError(f"body must be JSON of at most {limit // 1024} KB", 413)
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DashError("invalid JSON body") from None
        if not isinstance(data, dict):
            raise DashError("body must be a JSON object")
        return data

    @staticmethod
    def _int(v: Any, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(v))) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    # ── routes ──
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        u = urlsplit(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        route = u.path
        try:
            if route == "/":
                tok = qs.get("t")
                if tok is not None:
                    lan = self._lan
                    if self.server.phone.consume(tok) if lan else hmac.compare_digest(tok.encode(), self.server.token.encode()):
                        self.send_response(302)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"{COOKIE_NAME}={self.server.phone.cookie if lan else self.server.cookie}; Path=/; HttpOnly; SameSite=Strict")
                        self._headers("text/plain; charset=utf-8", 0)
                    else:
                        self._send(403, b"This link was already used or has expired. Run /phone in Axon for a new QR code." if lan else b"Invalid dashboard token. Use the URL printed by axon dash.", "text/plain; charset=utf-8")
                    return
                if not self._cookie_ok():
                    page = (b"<!doctype html><meta charset=utf-8><title>Axon: signed out</title><meta name=viewport content='width=device-width'>"
                            b"<meta name=color-scheme content='light dark'><style>body{font:15px/1.55 ui-sans-serif,-apple-system,system-ui,sans-serif;max-width:56ch;margin:14vh auto;padding:0 24px}"
                            b"h1{font-size:22px;font-weight:600;margin:0 0 10px}code{font:13px ui-monospace,Menlo,monospace}</style>"
                            b"<h1>This tab is signed out</h1><p>Open the link that <code>axon open</code> printed in your terminal (it ends "
                            b"in <code>?t=...</code>), or type <code>/open</code> inside Axon. That signs this browser in again.</p>")
                    self._send(401, page, "text/html; charset=utf-8")
                    return
                from axon.features import dashboard_web as web
                self._send(200, web.INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if route in ("/favicon.ico", "/static/icon.svg"):   # the mark only; browsers ask before any cookie exists
                from axon.features import dashboard_web as web
                self._send(200, web.asset("icon.svg")[0].encode("utf-8"), "image/svg+xml")  # type: ignore[index]
                return
            if not self._cookie_ok():
                self._err(401, "missing or invalid dashboard cookie")
                return
            if route.startswith("/api/") and not self._same_site_ok():
                return
            if self._lan_blocked(route):
                return
            if route == "/api/phone":
                self._json(200, self.server.phone.status())
                return
            if route.startswith("/static/"):
                from axon.features import dashboard_web as web
                asset = web.asset(route[len("/static/"):])
                if asset is None:
                    self._err(404, "not found")
                else:
                    self._send(200, asset[0].encode("utf-8"), asset[1])
                return
            self._api_get(route, qs)
        except DashError as e:
            self._err(e.status, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            self._err(507 if e.errno == errno.ENOSPC else 500, _disk_message(e))
        except Exception as e:
            self._err(500, f"internal error ({type(e).__name__})")

    def _api_get(self, route: str, qs: dict[str, str]) -> None:
        srv = self.server
        if route == "/api/info":
            import axon
            self._json(200, {"version": getattr(axon, "__version__", ""), "workspace": str(srv.workspace),
                             "project": srv.workspace.name, "live_session": srv.live_session_id(), "live": srv.live is not None,
                             "model": getattr(srv._safe_settings(), "model", ""), "mode": srv.effective_mode(),
                             "effort": getattr(srv._safe_settings(), "effort", ""),
                             "gh": bool(shutil.which("gh")), "modes": list(REPLY_MODES)})
        elif route == "/api/overview":
            self._json(200, overview_data(srv.workspace, qs.get("period", "30d"), qs.get("scope", "project")))
        elif route == "/api/sessions":
            scope = "all" if qs.get("scope") == "all" else "project"
            live = srv.live_session_id()
            rows = []
            for ref in srv.index.refs(scope, fresh=qs.get("fresh") == "1").values():
                m = ref.meta
                rows.append({"id": ref.id, "title": redact_text(m.first_prompt)[:200], "model": m.model,
                             "messages": m.message_count, "cost": m.total_cost, "tokens": m.total_tokens,
                             "updated": m.created_at, "tags": list(m.tags), "starred": bool(m.is_starred),
                             "project": ref.project, "live": ref.id == live})
            rows.sort(key=lambda r: r["updated"], reverse=True)
            self._json(200, {"sessions": rows, "scope": scope})
        elif route == "/api/extensions":
            self._json(200, extensions_data(srv.workspace, srv.agent))
        elif route in ("/api/agentver/list", "/api/agentver/diff"):    # read-only: fine on the phone listener too
            from axon.features import dash_agentver
            self._json(200, dash_agentver.get(srv, route, qs))
        elif route == "/api/share/file":
            item = srv.exports.get(qs.get("sha", ""))
            if item is None:
                raise DashError("preview expired; open Share again", 404)
            name = re.sub(r"[^A-Za-z0-9_.\-]", "_", item[0])
            from axon.features.dash_js_chatx import MIME_BY_EXT
            mime = MIME_BY_EXT.get(Path(name).suffix.lower(), "text/plain; charset=utf-8")
            if qs.get("inline") == "1" and mime.startswith("text/html"):     # preview and print: nothing in it can run
                self._send(200, item[1].encode("utf-8"), mime, {"Content-Disposition": "inline", "X-Frame-Options": "SAMEORIGIN",
                           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})
            else:
                self._send(200, item[1].encode("utf-8"), mime, {"Content-Disposition": f'attachment; filename="{name}"'})
        elif route == "/api/file/raw":
            self._serve_raw(qs)
        elif route == "/api/attachment/image":
            self._serve_attachment_image(qs)
        elif route in ("/api/events", "/api/shell/stream"):
            self._sse_hub(route, qs)
        elif route == "/api/term/stream":
            self._sse_pty(qs)
        else:
            from axon.features.dash_api import api_get
            payload = api_get(srv, route, qs)
            if payload is not None:
                self._json(200, payload)
                return
            m = re.fullmatch(r"/api/session/([^/]+)(?:/(text))?", route)
            if not m:
                raise DashError("not found", 404)
            try:
                ref = srv.index.lookup(unquote(m.group(1)))
            except DashError as e:
                if e.status == 404 and not m.group(2) and srv.runtime(unquote(m.group(1)), create=False) is not None:
                    # a chat that was just opened has no file yet: an empty page, not a 404 the browser logs as an error.
                    # That holds for the terminal's own chat too, which is not one of the runtimes this server built.
                    self._json(200, {"entries": [], "total": 0, "first": 0, "has_before": False})
                    return
                raise
            tr = load_transcript(ref.path)
            if m.group(2) == "text":
                self._json(200, field_text(tr, self._int(qs.get("entry"), -1, -1, 10**9), qs.get("field", ""),
                                           self._int(qs.get("offset"), 0, 0, 10**10), self._int(qs.get("limit"), 400_000, 1, TEXT_PAGE_MAX)))
                return
            if "before_ts" in qs:      # a session with an event log: the transcript only fills in what happened before the log began
                try:
                    cut = float(qs["before_ts"])
                except ValueError:
                    cut = 0.0
                tr = Transcript(entries=[e for e in tr.entries if isinstance(e.get("ts"), (int, float)) and e["ts"] < cut],
                                title=tr.title, model=tr.model)
            page = transcript_page(ref.id, tr, self._int(qs.get("before"), -1, -1, 10**9) if "before" in qs else None,
                                   self._int(qs.get("after"), -1, -1, 10**9) if "after" in qs else None,
                                   self._int(qs.get("limit"), 30, 1, 200))
            mt = ref.meta
            if ref.sub is not None:
                cost = sum(float(e.get("cost") or 0) for e in tr.entries if e["kind"] == "assistant")
                toks = sum(int((e.get("usage") or {}).get("input", 0)) + int((e.get("usage") or {}).get("output", 0))
                           for e in tr.entries if e["kind"] == "assistant")
                first = next((e["text"] for e in tr.entries if e["kind"] == "user"), "")
                mt = SimpleNamespace(first_prompt=" ".join(first.split())[:100] or f"Subagent {ref.sub}", model=tr.model,
                                     message_count=len(tr.entries), total_cost=round(cost, 5), total_tokens=toks, tags=[],
                                     is_starred=False, created_at=ref.path.stat().st_mtime)
            page["meta"] = {"id": ref.id, "title": redact_text(tr.title or mt.first_prompt)[:200], "model": mt.model,
                            "sub": ref.sub, "held_by": held_by(ref.path.parent if ref.sub is None else ref.path.parent.parent),
                            "messages": mt.message_count, "cost": mt.total_cost, "tokens": mt.total_tokens,
                            "tags": list(mt.tags), "starred": bool(mt.is_starred), "project": ref.project,
                            "workspace": redact_text(ref.workspace, home=True), "updated": mt.created_at,
                            "live": ref.id == srv.live_session_id(),
                            "running": ref.id in srv.busy}
            self._json(200, page)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        if not self._cookie_ok():
            self._err(401, "missing or invalid dashboard cookie")
            return
        if not self._origin_ok():
            return
        u = urlsplit(self.path)
        route = u.path
        srv = self.server
        if self._lan_blocked(route):
            return
        try:
            if route == "/api/phone":
                self._json(200, self._phone_post(self._body()))
                return
            if route == "/api/attach":
                self._json(200, self._attach({k: v[-1] for k, v in parse_qs(u.query).items()}))
                return
            limit = MAX_SAVE_BODY if route == "/api/file/save" else MAX_TEXT_BODY if route == "/api/attach/text" else MAX_BODY
            body = self._body(limit)
            m = re.fullmatch(r"/api/session/([^/]+)/reply", route)
            if m:
                ref = srv.index.lookup(unquote(m.group(1)))
                res = srv.reply(ref, str(body.get("text", "")), str(body.get("mode") or "default"))
                if isinstance(res, dict):
                    self._json(200, res)
                else:
                    self._stream(res)
            elif route == "/api/approval":
                run = srv.runs.get(str(body.get("run", "")))
                slot = run.approvals.get(str(body.get("id", ""))) if run else None
                if slot is None:
                    raise DashError("no pending approval with that id", 404)
                slot.allow = body.get("allow") is True
                slot.event.set()
                self._json(200, {"ok": True})
            elif route == "/api/cancel":
                run = srv.runs.get(str(body.get("run", "")))
                if run:
                    run.cancel()
                self._json(200, {"ok": True})
            elif route == "/api/agentver/act":
                if self._lan:        # rollback writes the agent file: this computer only
                    raise DashError("Rolling back or pinning an agent is not available from a phone; use this computer.", 403)
                from axon.features import dash_agentver
                self._json(200, dash_agentver.post(srv, body))
            elif route == "/api/share/preview":
                self._json(200, self._share_preview(body))
            elif route == "/api/share/gist":
                self._json(200, self._share_gist(body))
            else:
                from axon.features.dash_api import api_post
                payload = api_post(srv, route, body)
                if payload is None:
                    raise DashError("not found", 404)
                self._json(200, payload)
        except DashError as e:
            self._err(e.status, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            self._err(507 if e.errno == errno.ENOSPC else 500, _disk_message(e))
        except Exception as e:
            self._err(500, f"internal error ({type(e).__name__})")

    def do_PUT(self) -> None:  # noqa: N802
        self._err(405, "method not allowed")

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT  # no CORS preflight is ever approved

    def _serve_attachment_image(self, qs: dict[str, str]) -> None:
        """A thumbnail of an image the user attached in this process: only files Axon itself stored in its image cache."""
        from axon.agent.images import get_attachment_by_label, get_images_cache_dir
        from axon.permissions.paths import is_within
        try:
            n = int(qs.get("n", ""))
        except ValueError:
            raise DashError("n must be a number", 400) from None
        att = get_attachment_by_label(f"[Image #{n}]")
        if att is None or not Path(att.cached_path).is_file() or not is_within(get_images_cache_dir(), att.cached_path):
            raise DashError("no such attachment", 404)
        ctype = att.mime_type if str(att.mime_type).startswith("image/") and att.mime_type != "image/svg+xml" else "application/octet-stream"
        data = Path(att.cached_path).read_bytes()
        self.send_response(200)
        self._headers(ctype, len(data), {"Content-Security-Policy": "sandbox; default-src 'none'", "Content-Disposition": "inline"})
        self.wfile.write(data)

    def _serve_raw(self, qs: dict[str, str]) -> None:
        """Bytes of a previewable file with Range support (video and audio seek), or a download of any file."""
        from axon.features import dash_fs
        srv = self.server
        download = qs.get("download") == "1"
        p, ctype = dash_fs.raw_info(srv.workspace, srv.extra_dirs(), qs.get("path", ""), download)
        size = p.stat().st_size
        rng = dash_fs.parse_range(self.headers.get("Range"), size)
        if rng == "bad":
            self.send_response(416)
            self._headers("text/plain; charset=utf-8", 0, {"Content-Range": f"bytes */{size}"})
            return
        start, end = (0, size - 1) if rng is None else rng      # type: ignore[misc]
        length = max(0, end - start + 1)
        extra = {"Accept-Ranges": "bytes", "X-Frame-Options": "SAMEORIGIN",
                 "Content-Security-Policy": ("object-src 'self'; default-src 'none'" if ctype == "application/pdf"
                                             else "sandbox; default-src 'none'; style-src 'unsafe-inline'; media-src 'self'")}
        name = re.sub(r"[^A-Za-z0-9_.\-]", "_", p.name)
        extra["Content-Disposition"] = f'attachment; filename="{name}"' if download else "inline"
        if rng is None:
            self.send_response(200)
        else:
            self.send_response(206)
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(ctype, length, extra)
        if self.command == "HEAD" or length == 0:
            return
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _attach(self, qs: dict[str, str]) -> dict[str, Any]:
        """Raw upload of a dropped or pasted file/image (not JSON). Stored where the terminal stores attachments."""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/octet-stream" not in ctype:
            raise DashError("Content-Type must be application/octet-stream", 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0:
            raise DashError("an upload needs a Content-Length", 411)
        if n > MAX_UPLOAD:
            raise DashError(f"files over {MAX_UPLOAD // (1024 * 1024)} MB cannot be attached from the page; "
                            "reference them by path instead", 413)
        data = self.rfile.read(n)
        rt = self.server.runtime(qs.get("session", ""), create=True)
        try:
            from axon import storage
            storage.set_active_session(rt.workspace, rt.session_id)
        except Exception:
            pass
        return self.server.attach.add_file(qs.get("name", "file"), data, rt.workspace)

    def _sse_pty(self, qs: dict[str, str]) -> None:
        sess = self.server.pty.get(qs.get("id", ""))
        try:
            since = int(self.headers.get("Last-Event-ID") or qs.get("since") or 0)
        except ValueError:
            since = 0
        self.send_response(200)
        self._headers("text/event-stream; charset=utf-8", None, {"Connection": "close", "X-Accel-Buffering": "no"})
        self.close_connection = True
        from axon.features.dash_pty import encode_chunk
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while True:
                data, new, reset, finished = sess.read(since, timeout=10)
                if data or reset:
                    payload = json.dumps({"b": encode_chunk(data), "reset": reset})
                    self.wfile.write(f"id: {new}\ndata: {payload}\n\n".encode("ascii"))
                    since = new
                elif finished:
                    self.wfile.write(f"id: {new}\ndata: {json.dumps({'exit': sess.code})}\n\n".encode("ascii"))
                    self.wfile.flush()
                    break
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_hub(self, route: str, qs: dict[str, str]) -> None:
        srv = self.server
        if route == "/api/events":
            rt = srv.runtime(qs.get("session", ""), create=False)
            if rt is None:
                raise DashError("this session has no running agent in the page yet", 404)
            hub = rt.hub
        else:
            job = srv.shell.jobs.get(qs.get("id", ""))
            if job is None:
                raise DashError("no such shell job", 404)
            hub = job.hub
        try:
            since = int(self.headers.get("Last-Event-ID") or qs.get("since") or 0)
        except ValueError:
            since = 0
        self.send_response(200)
        self._headers("text/event-stream; charset=utf-8", None, {"Connection": "close", "X-Accel-Buffering": "no"})
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                events, gap = hub.read(since, timeout=10)
                frames: list[str] = ["event: gap\ndata: {}\n\n"] if gap else []
                for seq, ev in events:
                    since = seq
                    frames.append(f"id: {seq}\ndata: {json.dumps(scrub_obj(ev), ensure_ascii=False, default=str)}\n\n")
                if not events:
                    if hub.closed:
                        break
                    frames.append(": ping\n\n")
                self.wfile.write("".join(frames).encode("utf-8"))      # one write per wake-up: one packet, not one per delta
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _share_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        srv = self.server
        kind = body.get("kind")
        if kind == "session":
            from axon.features.dash_js_chatx import share_preview       # formats, tool details, secrets counted
            return share_preview(srv, body)
        elif kind == "overview":
            scope = "all" if body.get("scope") == "all" else "project"
            content = overview_export_html(srv.workspace, str(body.get("period") or "30d"), scope)
            filename = f"axon-usage-{datetime.now():%Y-%m-%d}.html"
        else:
            raise DashError("kind must be 'session' or 'overview'")
        filename = re.sub(r"[^A-Za-z0-9_.\-]", "_", filename)
        sha = srv.store_export(filename, content)
        size = len(content.encode("utf-8"))
        return {"filename": filename, "sha256": sha, "bytes": size, "content": content,
                "gh": bool(shutil.which("gh")), "gist_ok": size <= GIST_MAX_BYTES,
                "gist_note": "" if size <= GIST_MAX_BYTES else "Too large for a gist (1 MB limit). Download it instead."}

    def _share_gist(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("confirm") is not True:
            raise DashError("upload needs an explicit confirmation", 400)
        item = self.server.exports.get(str(body.get("sha256", "")))
        if item is None:
            raise DashError("preview expired; open Share again and review the file before uploading", 409)
        filename, content = item
        if len(content.encode("utf-8")) > GIST_MAX_BYTES:
            raise DashError("file is too large for a gist", 413)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != body.get("sha256"):
            raise DashError("content changed since preview", 409)
        url = run_gh_gist(content, filename, "Axon dashboard export (secret gist)")
        return {"url": url}

    def _stream(self, run: Run) -> None:
        self.send_response(200)
        self._headers("text/event-stream; charset=utf-8", None,
                      {"Connection": "close", "X-Accel-Buffering": "no", "X-Run-Id": run.id})
        self.close_connection = True
        i = 0
        try:
            self.wfile.write(f"event: run\ndata: {json.dumps({'run': run.id})}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = run.q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    break
                i += 1
                kind = str(ev.get("type"))
                frame = f"id: {i}\nevent: {kind}\ndata: {json.dumps(scrub_obj(ev), ensure_ascii=False, default=str)}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            run.cancel()
        finally:
            self.server.runs.pop(run.id, None)


# ─── launching ───────────────────────────────────────────────────────────────

def make_server(port: int = DEFAULT_PORT, workspace: Path | str | None = None, agent: Any = None,
                host: str = LOOPBACK_HOST, token: str | None = None) -> DashServer:
    if host not in ("127.0.0.1", "::1") or not is_loopback(host):
        raise ValueError(f"the dashboard only listens on loopback, not {host!r}")
    ws = Path(workspace) if workspace else (getattr(getattr(agent, "settings", None), "workspace", None) or Path.cwd())
    # The first page load asks for several things at once; if two request threads were the first to import the tools
    # package together, one failed with a KeyError (a 500 on /api/tree). Import it here, once, before any thread runs.
    try:
        import axon.tools._files  # noqa: F401
    except Exception:
        pass
    return DashServer(port, Path(ws), agent, token)


def _bind(port: int, workspace: Path, agent: Any) -> DashServer:
    try:
        return make_server(port, workspace, agent)
    except OSError:
        if port == 0:
            raise
        return make_server(0, workspace, agent)   # preferred port busy: any free one


def has_display(env: dict[str, str] | None = None, platform: str | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    plat = platform or sys.platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"):
        return False
    if env.get("CI") or env.get("AXON_NO_BROWSER") or env.get("PYTEST_CURRENT_TEST"):
        return False
    if plat.startswith("linux") or plat.startswith("freebsd"):
        return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    return True


def open_browser(url: str) -> bool:
    if not has_display():
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _announce(server: DashServer, opened: bool, out: Any) -> None:
    out.write(f"Axon dashboard: {server.open_url}\n")
    out.write("  " + ("Opened in your browser." if opened else "Open that URL in a browser on this machine.") + "\n")
    out.write(f"  workspace: {server.workspace}\n  Local only (127.0.0.1). The link carries a private token; don't share it.\n")
    out.flush()


def _configure_gates() -> Callable[[], None]:
    """The page is an interactive place: an untrusted folder's hooks, plugins and .axon/mcp.json servers stay off
    until the Extensions page trusts them, and .mcp.json servers wait for an Approve there (as in the terminal).
    Returns a function that puts the previous settings back."""
    try:
        from axon.hooks import trust
        from axon.mcp import state as mcp_state
        before = (trust._state["interactive"], trust._state["require"], mcp_state._session["interactive"],
                  mcp_state._session.get("page", False))
        trust.configure(True)
        mcp_state.set_interactive(True)
        mcp_state.set_page_mode(True)
    except Exception:
        return lambda: None

    def restore() -> None:
        trust.configure(before[0], before[1])
        mcp_state.set_interactive(before[2])
        mcp_state.set_page_mode(before[3])
    return restore


def dash_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="axon dash", description="Open the Axon dashboard in your browser.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT}, or a free one if busy; 0 = random)")
    p.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    p.add_argument("--workspace", help="Project to show (default: current directory)")
    args = p.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    if not ws.is_dir():
        sys.stderr.write(f"axon dash: workspace does not exist: {ws}\n")
        return 2
    try:
        server = _bind(max(0, args.port), ws, None)
    except OSError as e:
        sys.stderr.write(f"axon dash: cannot start the server: {e}\n")
        return 2
    restore_gates = _configure_gates()
    _start_scheduler()
    opened = False if args.no_open else open_browser(server.open_url)
    _announce(server, opened, sys.stdout)
    sys.stdout.write("  Press Ctrl+C to stop.\n")
    sys.stdout.flush()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        sys.stdout.write("\nStopping the dashboard.\n")
    finally:
        server.shutdown_runs()
        server.server_close()
        restore_gates()
    return 0


_LIVE: dict[str, Any] = {"server": None, "thread": None}


def _start_scheduler() -> None:
    """Scheduled tasks run while the app is open (see axon.features.scheduler)."""
    try:
        from axon.features.scheduler import ensure_started
        ensure_started()
    except Exception:
        pass


def _ensure_server(agent: Any, port: int = DEFAULT_PORT) -> DashServer | None:
    """The running in-terminal dashboard, started if needed. None (after printing why) when it cannot start."""
    srv: DashServer | None = _LIVE["server"]
    if srv is not None and _LIVE["thread"] is not None and _LIVE["thread"].is_alive():
        srv.agent = agent
        return srv
    ws = getattr(getattr(agent, "settings", None), "workspace", None) or Path.cwd()
    try:
        srv = _bind(port, Path(ws), agent)
    except OSError as e:
        print(f"Could not start the dashboard: {e}")
        return None
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3}, name="axon-dash", daemon=True)
    t.start()
    _LIVE.update(server=srv, thread=t)
    _start_scheduler()
    return srv


def _dash_command(agent: Any, arg: str) -> None:
    toks = (arg or "").split()
    srv: DashServer | None = _LIVE["server"]
    alive = srv is not None and _LIVE["thread"] is not None and _LIVE["thread"].is_alive()
    if "stop" in toks:
        if alive:
            srv.phone.stop()  # type: ignore[union-attr]
            srv.shutdown()  # type: ignore[union-attr]
            srv.shutdown_runs()  # type: ignore[union-attr]
            srv.server_close()  # type: ignore[union-attr]
            _LIVE.update(server=None, thread=None)
            print("Dashboard stopped.")
        else:
            print("The dashboard is not running.")
        return
    no_open = "--no-open" in toks
    port = next((int(t) for t in toks if t.isdigit()), DEFAULT_PORT)
    reused = alive
    srv = _ensure_server(agent, port)
    if srv is None:
        return
    opened = False if no_open else open_browser(srv.open_url)
    if reused:
        print("Dashboard already running; reusing it.")
    _announce(srv, opened, sys.stdout)
    print("  The page and this terminal share one session: messages, chats and folders changed in either show in both.")
    print("  Want it on your phone? Type /phone for a QR code.")


def _phone_command(agent: Any, arg: str) -> None:
    toks = (arg or "").split()
    srv: DashServer | None = _LIVE["server"]
    if "stop" in toks or "off" in toks:
        if srv is not None and srv.phone.stop():
            print("Phone access is off. Phones that were signed in are signed out.")
        else:
            print("Phone access was not on.")
        return
    srv = _ensure_server(agent)
    if srv is None:
        return
    ip = next((t for t in toks if t.count(".") == 3), None)
    try:
        srv.phone.start(ip)
    except OSError as e:
        print(f"Could not open phone access: {e}")
        return
    link = srv.phone.pairing_url()
    print("\nScan this with your phone's camera (same Wi-Fi as this computer):\n")
    try:
        from axon.features.qr import qr_terminal
        print(qr_terminal(link))
    except Exception:
        pass
    print(f"\n  {link}")
    print("  The link works once and expires in 10 minutes; type /phone again for a new one.")
    print("  Your phone then chats in this same session: messages show live in the terminal, the browser and the phone.")
    print("  Only devices on your own network can connect. The raw terminal is not offered on the phone. '/phone stop' turns it off.\n")


CLI_SUBCOMMANDS = {"open": dash_main, "dash": dash_main}

try:
    from axon.features import FeatureCommand
    COMMANDS = [FeatureCommand(name="/open", handler=_dash_command, category="Integrations", aliases=("/dash", "/web"),
                               description="Open the Axon app in your browser (chat, files, shell, commands, usage)",
                               usage="/open [port] [--no-open] | /open stop"),
                FeatureCommand(name="/phone", handler=_phone_command, category="Integrations", aliases=("/mobile", "/qr"),
                               description="Continue this chat from your phone: scan a QR code (same Wi-Fi)",
                               usage="/phone | /phone stop")]
except (ImportError, AttributeError):
    COMMANDS = []

__all__ = ["DashServer", "make_server", "dash_main", "render_markdown", "redact_text", "SessionIndex",
           "transcript_page", "overview_data", "extensions_data", "has_display", "CLI_SUBCOMMANDS", "COMMANDS"]
