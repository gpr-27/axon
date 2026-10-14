"""
Shared, size-capped HTTP fetching for WebFetch/WebSearch, plus HTML → markdown-ish text.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from axon.errors import ToolError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 Axon/1.0"
)
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# Test hook: an httpx transport (e.g. httpx.MockTransport) used instead of the network.
_TRANSPORT: httpx.BaseTransport | None = None


def is_offline() -> bool:
    """AXON_OFFLINE=1 (true/yes/on) disables all web access from tools."""
    return os.environ.get("AXON_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def ensure_online(tool: str, url: str = "") -> None:
    """Fail fast, without touching the network, when offline mode is on. Loopback URLs (local docs
    servers) and the test transport stay reachable."""
    if _TRANSPORT is None and is_offline() and not (url and _is_loopback(url)):
        hint = " Use WebFetch on a localhost URL, or unset AXON_OFFLINE." if tool == "WebSearch" else " Unset AXON_OFFLINE to allow it."
        raise ToolError(f"{tool} failed: offline mode (AXON_OFFLINE is set); no network request was made.{hint}")


@dataclass
class FetchResult:
    url: str            # final URL after redirects
    status: int
    content_type: str
    text: str
    truncated: bool


def fetch(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Fetch a URL with redirects, a timeout and a hard byte cap. Raises ToolError."""
    ensure_online("Web access", url)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7"}
    hdrs.update(headers or {})
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True, max_redirects=8,
                          transport=_TRANSPORT) as client:
            with client.stream(method, url, data=data, params=params, headers=hdrs) as resp:
                buf = bytearray()
                truncated = False
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        del buf[max_bytes:]
                        break
                ctype = resp.headers.get("content-type", "")
                encoding = resp.encoding or "utf-8"
                m = re.search(r"charset=([\w\-]+)", ctype, re.I)
                if m:
                    encoding = m.group(1)
                try:
                    text = bytes(buf).decode(encoding, errors="replace")
                except LookupError:
                    text = bytes(buf).decode("utf-8", errors="replace")
                return FetchResult(str(resp.url), resp.status_code, ctype, text, truncated)
    except httpx.TimeoutException as e:
        raise ToolError(f"Request to {url} timed out after {timeout:.0f}s.") from e
    except httpx.TooManyRedirects as e:
        raise ToolError(f"Too many redirects fetching {url}.") from e
    except httpx.HTTPError as e:
        raise ToolError(f"Network error fetching {url}: {type(e).__name__}: {e}") from e


_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "form", "button", "nav", "footer", "aside")


def html_to_text(html: str, base_url: str = "") -> tuple[str, str]:
    """Convert HTML into compact markdown-like text. Returns (title, text)."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup

    out: list[str] = []

    def inline(node: Any) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name == "br":
            return "\n"
        inner = "".join(inline(c) for c in node.children)
        if name == "a":
            href = node.get("href") or ""
            txt = " ".join(inner.split())
            if href and not href.startswith(("#", "javascript:")) and txt:
                return f"[{txt}]({urljoin(base_url, href)})"
            return txt
        if name == "code":
            t = inner.strip()
            return f"`{t}`" if t and "\n" not in t else inner
        if name in ("strong", "b"):
            t = inner.strip()
            return f"**{t}**" if t else ""
        if name in ("em", "i"):
            t = inner.strip()
            return f"*{t}*" if t else ""
        if name == "img":
            alt = node.get("alt") or ""
            return f"[image: {alt}]" if alt else ""
        return inner

    def block(node: Any, depth: int = 0) -> None:
        if isinstance(node, NavigableString):
            t = " ".join(str(node).split())
            if t:
                out.append(t)
            return
        if not isinstance(node, Tag):
            return
        name = node.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = " ".join(inline(node).split())
            if t:
                out.append("\n" + "#" * int(name[1]) + " " + t + "\n")
        elif name == "pre":
            code = node.get_text()
            out.append("\n```\n" + code.strip("\n") + "\n```\n")
        elif name in ("ul", "ol"):
            for i, li in enumerate(node.find_all("li", recursive=False), 1):
                bullet = f"{i}." if name == "ol" else "-"
                t = " ".join(inline(li).split())
                if t:
                    out.append("  " * depth + f"{bullet} {t}")
            out.append("")
        elif name == "table":
            for tr in node.find_all("tr"):
                cells = [" ".join(inline(td).split()) for td in tr.find_all(["td", "th"])]
                if any(cells):
                    out.append("| " + " | ".join(cells) + " |")
            out.append("")
        elif name in ("p", "blockquote", "dd", "dt", "figcaption", "summary"):
            t = " ".join(inline(node).split())
            if t:
                out.append(("> " if name == "blockquote" else "") + t + "\n")
        elif any(isinstance(c, Tag) and c.name in _BLOCKS for c in node.children):
            for c in node.children:
                block(c, depth)
        else:
            t = " ".join(inline(node).split())
            if t:
                out.append(t)

    block(root)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


_BLOCKS = {"div", "section", "article", "main", "header", "p", "ul", "ol", "table", "pre", "blockquote",
           "h1", "h2", "h3", "h4", "h5", "h6", "dl", "figure", "details", "body", "li", "nav"}
