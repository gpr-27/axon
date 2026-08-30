"""
WebSearch tool: search the public web using DuckDuckGo HTML parser with resilient fallback.
"""
from __future__ import annotations
import html
import re
import urllib.parse
import urllib.request
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class WebSearchTool(Tool):
    name: ClassVar[str] = "WebSearch"
    description: ClassVar[str] = (
        "Search the live web for technical documentation, library information, error solutions, or current information. "
        "Returns top search result cards with title, URL, and snippet."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query keywords"},
            "num_results": {"type": "integer", "description": "Number of results to retrieve (default: 5, max: 10)"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query", "").strip()
        if not query:
            raise ToolError("WebSearch requires non-empty 'query'.")

        num_results = min(10, max(1, int(args.get("num_results") or 5)))

        # 1. Query DuckDuckGo HTML endpoint
        try:
            encoded_q = urllib.parse.quote_plus(query)
            url = f"https://html.duckduckgo.com/html/?q={encoded_q}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )

            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="replace")

            # Parse results from HTML
            results = []
            # Extract links and snippets from DDG HTML structure
            matches = re.findall(
                r'<a[^>]+class="result__url"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                content,
                re.DOTALL,
            )

            if not matches:
                # Secondary regex format
                matches = re.findall(
                    r'<a[^>]+class="result__snippet"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    content,
                    re.DOTALL,
                )
                for href, snip in matches[:num_results]:
                    clean_snip = re.sub(r"<[^>]+>", "", html.unescape(snip)).strip()
                    actual_url = href
                    if "uddg=" in href:
                        try:
                            actual_url = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                        except Exception:
                            pass
                    results.append({"title": query, "url": actual_url, "snippet": clean_snip})
            else:
                for href, title, snippet in matches[:num_results]:
                    clean_title = re.sub(r"<[^>]+>", "", html.unescape(title)).strip()
                    clean_snippet = re.sub(r"<[^>]+>", "", html.unescape(snippet)).strip()
                    actual_url = href
                    if "uddg=" in href:
                        try:
                            actual_url = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                        except Exception:
                            pass
                    results.append({"title": clean_title, "url": actual_url, "snippet": clean_snippet})

            if not results:
                # Fallback: Extract any outbound links and snippets
                link_matches = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', content)
                for h, t in link_matches:
                    if h.startswith("http") and "duckduckgo.com" not in h and len(t.strip()) > 10:
                        results.append({"title": re.sub(r"<[^>]+>", "", t).strip(), "url": h, "snippet": ""})
                        if len(results) >= num_results:
                            break

            if results:
                out_lines = [f"Found {len(results)} web search results for '{query}':\n"]
                for idx, r in enumerate(results, 1):
                    out_lines.append(f"{idx}. {r['title']}")
                    out_lines.append(f"   URL: {r['url']}")
                    if r.get("snippet"):
                        out_lines.append(f"   Snippet: {r['snippet']}")
                    out_lines.append("")
                return "\n".join(out_lines).strip()

            return f"No search results found on web for query '{query}'."

        except Exception as e:
            return f"[WebSearch Note: Query '{query}' could not reach web endpoint: {e}. You may use WebFetch with specific URLs if available.]"
