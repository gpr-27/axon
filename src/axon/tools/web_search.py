"""
WebSearch: web search with no API key required (DuckDuckGo HTML, then DuckDuckGo Lite).
If BRAVE_API_KEY (or BRAVE_SEARCH_API_KEY) is set, the Brave Search API is tried first.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, ClassVar
from urllib.parse import parse_qs, unquote, urlparse

from axon.errors import ToolError
from axon.tools import _http
from axon.tools.base import Tool, ToolContext, default_summary, int_arg, plural


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        q = parse_qs(parsed.query)
        if "uddg" in q:
            return unquote(q["uddg"][0])
    return href


def _is_ad(url: str) -> bool:
    p = urlparse(url)
    return ("duckduckgo.com" in p.netloc and p.path.startswith("/y.js")) or "ad_domain=" in url


def parse_ddg_html(html: str) -> list[SearchResult]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchResult] = []
    for res in soup.select("div.result, div.web-result"):
        classes = res.get("class") or []
        if any("result--ad" in c for c in classes):
            continue
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        url = _ddg_target(a["href"])
        if not url.startswith("http") or _is_ad(url):
            continue
        snip = res.select_one(".result__snippet")
        out.append(SearchResult(a.get_text(" ", strip=True), url, snip.get_text(" ", strip=True) if snip else ""))
    return out


def parse_ddg_lite(html: str) -> list[SearchResult]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchResult] = []
    links = soup.select("a.result-link")
    snippets = soup.select("td.result-snippet")
    for i, a in enumerate(links):
        url = _ddg_target(a.get("href") or "")
        if not url.startswith("http") or _is_ad(url):
            continue
        snip = snippets[i].get_text(" ", strip=True) if i < len(snippets) else ""
        out.append(SearchResult(a.get_text(" ", strip=True), url, snip))
    return out


def _looks_blocked(html: str) -> bool:
    low = html.lower()
    return "anomaly" in low and "duckduckgo" in low or "captcha" in low


def search_ddg_html(query: str) -> list[SearchResult]:
    res = _http.fetch("https://html.duckduckgo.com/html/", method="POST", data={"q": query, "kl": "wt-wt"},
                      timeout=15, max_bytes=2_000_000)
    if res.status >= 400:
        raise ToolError(f"DuckDuckGo returned HTTP {res.status}")
    results = parse_ddg_html(res.text)
    if not results and _looks_blocked(res.text):
        raise ToolError("DuckDuckGo rate-limited this client (bot challenge)")
    return results


def search_ddg_lite(query: str) -> list[SearchResult]:
    res = _http.fetch("https://lite.duckduckgo.com/lite/", method="POST", data={"q": query},
                      timeout=15, max_bytes=2_000_000)
    if res.status >= 400:
        raise ToolError(f"DuckDuckGo Lite returned HTTP {res.status}")
    results = parse_ddg_lite(res.text)
    if not results and _looks_blocked(res.text):
        raise ToolError("DuckDuckGo Lite rate-limited this client (bot challenge)")
    return results


def search_brave(query: str, key: str, count: int) -> list[SearchResult]:
    res = _http.fetch(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": min(20, count)},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
        timeout=15, max_bytes=2_000_000,
    )
    if res.status in (401, 403):
        raise ToolError("Brave Search rejected the API key")
    if res.status >= 400:
        raise ToolError(f"Brave Search returned HTTP {res.status}")
    try:
        data = json.loads(res.text)
    except ValueError as e:
        raise ToolError("Brave Search returned invalid JSON") from e
    items = ((data.get("web") or {}).get("results")) or []
    return [SearchResult(it.get("title", ""), it.get("url", ""), it.get("description", "")) for it in items if it.get("url")]


def _domain_ok(url: str, allowed: list[str], blocked: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()

    def match(d: str) -> bool:
        d = d.lower().lstrip(".")
        return host == d or host.endswith("." + d)

    if allowed and not any(match(d) for d in allowed):
        return False
    return not any(match(d) for d in blocked)


def _strip_tags(s: str) -> str:
    import re
    import html
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", s)).split())


class WebSearchTool(Tool):
    name: ClassVar[str] = "WebSearch"
    description: ClassVar[str] = (
        "Search the web for current information: documentation, error messages, release notes, library versions. "
        "Returns ranked results (title, URL, snippet). Then use WebFetch on the most relevant URLs to read them. "
        "Use allowed_domains / blocked_domains to restrict sources (e.g. [\"docs.python.org\"])."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "description": "Search query"},
            "num_results": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Number of results (default 8)"},
            "allowed_domains": {"type": "array", "items": {"type": "string"}, "description": "Only include these domains"},
            "blocked_domains": {"type": "array", "items": {"type": "string"}, "description": "Exclude these domains"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    # Sends the query to a third-party service: not read-only.
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if len(query) < 2:
            raise ToolError("WebSearch requires a non-empty 'query'.")
        _http.ensure_online("WebSearch")
        n = int_arg(args, "num_results", 8, lo=1, hi=20)
        allowed = [str(d) for d in (args.get("allowed_domains") or [])]
        blocked = [str(d) for d in (args.get("blocked_domains") or [])]
        q = query
        if len(allowed) == 1:
            q = f"{query} site:{allowed[0]}"

        backends: list[tuple[str, Callable[[], list[SearchResult]]]] = []
        brave_key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY")
        if brave_key:
            backends.append(("Brave", lambda: search_brave(q, brave_key, n * 2)))
        backends.append(("DuckDuckGo", lambda: search_ddg_html(q)))
        backends.append(("DuckDuckGo Lite", lambda: search_ddg_lite(q)))

        errors: list[str] = []
        for name, fn in backends:
            try:
                results = fn()
            except ToolError as e:
                errors.append(f"{name}: {e}")
                continue
            except Exception as e:  # parsing failures shouldn't kill the fallback chain
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            seen: set[str] = set()
            filtered = []
            for r in results:
                if r.url in seen or not _domain_ok(r.url, allowed, blocked):
                    continue
                seen.add(r.url)
                filtered.append(r)
            if filtered:
                lines = [f"Web search results for \"{query}\" (via {name}):", ""]
                for i, r in enumerate(filtered[:n], 1):
                    lines.append(f"{i}. {_strip_tags(r.title) or r.url}")
                    lines.append(f"   {r.url}")
                    if r.snippet:
                        lines.append(f"   {_strip_tags(r.snippet)[:300]}")
                return "\n".join(lines)
            errors.append(f"{name}: no results")

        detail = "; ".join(errors)
        if all("no results" in e for e in errors):
            return f"No web results found for \"{query}\". Try broader or different keywords."
        raise ToolError(
            f"WebSearch failed for \"{query}\" ({detail}). Check your network/proxy settings "
            f"(HTTPS_PROXY), or set BRAVE_API_KEY for a keyed backend. If you know a relevant URL, use WebFetch."
        )

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        import re
        n = len(re.findall(r"^\d+\. ", output, re.M))
        m = re.search(r"\(via (.*?)\)", output)
        return plural(n, "result") + (f" via {m.group(1)}" if m else "")
