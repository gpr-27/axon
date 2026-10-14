"""
WebFetch: fetch a URL and return readable markdown-ish text (HTML), pretty JSON, or plain text.
"""
from __future__ import annotations

import json
from typing import Any, ClassVar
from urllib.parse import urlparse

from axon.errors import ToolError
from axon.tools import _http
from axon.tools.base import Tool, ToolContext, cap_text, default_summary, int_arg, output_cap

DEFAULT_MAX_CHARS = 25_000


class WebFetchTool(Tool):
    name: ClassVar[str] = "WebFetch"
    description: ClassVar[str] = (
        "Fetch a URL (http/https) and return its content as readable text: HTML is converted to markdown-like text "
        "(headings, lists, code blocks, links), JSON is pretty-printed. Use for documentation pages, API references, "
        "READMEs or raw files. `prompt` states what you are looking for and is echoed with the result to focus your "
        "reading. Large pages are truncated (max_chars, default 25000); use start_char to read further. "
        "No JavaScript is executed. For finding pages, use WebSearch first."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Fully-qualified http(s) URL"},
            "prompt": {"type": "string", "description": "What information you want from the page"},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000, "description": "Max characters returned (default 25000)"},
            "start_char": {"type": "integer", "minimum": 0, "description": "Character offset to start from, for paging long pages"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    # Network access with user-controlled URLs (possible data exfiltration): not read-only.
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        url = str(args.get("url") or "").strip()
        if not url:
            raise ToolError("WebFetch requires 'url'.")

        if url.startswith("www."):
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError(f"Invalid URL '{url}'. Must start with http:// or https://.")
        max_chars = int_arg(args, "max_chars", DEFAULT_MAX_CHARS, lo=1000, hi=100_000)
        start = int_arg(args, "start_char", 0, lo=0)
        prompt = str(args.get("prompt") or "").strip()
        _http.ensure_online("WebFetch", url)

        res = _http.fetch(url)
        ctype = res.content_type.lower()
        if res.status >= 400:
            snippet = " ".join(res.text[:400].split())
            raise ToolError(f"HTTP {res.status} fetching {res.url}. {snippet}")

        title = ""
        if "html" in ctype or (not ctype and res.text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))):
            title, body = _http.html_to_text(res.text, res.url)
        elif "json" in ctype:
            try:
                body = json.dumps(json.loads(res.text), indent=2, ensure_ascii=False)
            except ValueError:
                body = res.text
        elif ctype.startswith(("text/", "application/xml", "application/javascript")) or not ctype:
            body = res.text
        else:
            return (f"[WebFetch {res.url}: HTTP {res.status}, content-type {res.content_type or 'unknown'} is binary "
                    f"and cannot be shown as text.]")

        total = len(body)
        window = body[start : start + max_chars]
        header = [f"URL: {res.url}", f"HTTP {res.status} · {res.content_type or 'unknown type'} · {total:,} chars"]
        if title:
            header.insert(1, f"Title: {title}")
        if prompt:
            header.append(f"Looking for: {prompt}")
        footer = ""
        if start + max_chars < total:
            footer = f"\n\n[... {total - start - max_chars:,} more chars. Continue with start_char={start + max_chars} ...]"
        if res.truncated:
            footer += "\n[Download stopped at the 5 MB size cap.]"
        if not window.strip():
            window = "(no readable text content)"
        out = "\n".join(header) + "\n\n" + window + footer
        return cap_text(out, output_cap(ctx, max_chars + 2000))

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        import re
        m = re.search(r"^HTTP (\d+) · (.*?) · ([\d,]+) chars", output, re.M)
        if not m:
            return default_summary(output, False)
        ctype = m.group(2).split(";")[0]
        more = " (truncated)" if "more chars" in output else ""
        return f"HTTP {m.group(1)} · {ctype} · {m.group(3)} chars{more}"
