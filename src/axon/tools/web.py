"""
WebFetch tool: fetch web URL and summarize text content.
"""
from __future__ import annotations
import re
import urllib.request
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class WebFetchTool(Tool):
    name: ClassVar[str] = "WebFetch"
    description: ClassVar[str] = (
        "Fetch and extract readable text from a URL. "
        "Useful for documentation or web resources."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The HTTP or HTTPS URL to fetch"},
            "prompt": {"type": "string", "description": "Specific question or focus for the web content"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        url = args.get("url", "")
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"Invalid URL protocol for '{url}'. Must start with http:// or https://.")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AxonBot/1.0)"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw_html = response.read().decode("utf-8", errors="ignore")
            # Convert block tags to linebreaks to avoid giant unbroken lines
            text = re.sub(r"<style.*?>.*?</style>", "", raw_html, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<script.*?>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<(?:br|p|div|h[1-6]|li|tr|blockquote)[^>]*>", "\n", text, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            # Normalize whitespace and clean empty lines
            clean_lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines()]
            text = "\n".join(l for l in clean_lines if l)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return f"[WebFetch Result from {url}]:\n\n{text[:15000]}"
        except Exception as e:
            raise ToolError(f"Failed to fetch {url}: {e}") from e
