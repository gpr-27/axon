"""
Http tool: send structured HTTP requests (GET, POST, PUT, DELETE, PATCH) with JSON payloads and headers.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class HttpTool(Tool):
    name: ClassVar[str] = "Http"
    description: ClassVar[str] = (
        "Send HTTP requests (GET, POST, PUT, DELETE, PATCH) to test local or remote endpoints, "
        "inspect APIs, send JSON bodies, and review HTTP status and response payloads."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "description": "HTTP method"},
            "url": {"type": "string", "description": "Full URL to request"},
            "headers": {"type": "object", "description": "Optional HTTP headers key-value map"},
            "body": {"type": "string", "description": "Optional request body (JSON or text)"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds (default: 15)"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        url = args.get("url", "").strip()
        method = (args.get("method") or "GET").upper()
        headers = args.get("headers") or {}
        body = args.get("body") or ""
        timeout = int(args.get("timeout_s") or 15)

        if not url.startswith(("http://", "https://")):
            raise ToolError(f"Invalid URL '{url}'. Must start with http:// or https://")

        req_headers = {"User-Agent": "Axon-Agent/1.0", **headers}
        data_bytes = None
        if body:
            data_bytes = body.encode("utf-8")
            if "Content-Type" not in req_headers and "content-type" not in req_headers:
                req_headers["Content-Type"] = "application/json" if body.strip().startswith(("{", "[")) else "text/plain"

        req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                raw_body = resp.read().decode("utf-8", errors="replace")

                # Try formatting JSON
                try:
                    parsed = json.loads(raw_body)
                    formatted_body = json.dumps(parsed, indent=2)
                except Exception:
                    formatted_body = raw_body

                if len(formatted_body) > 15000:
                    formatted_body = formatted_body[:15000] + f"\n\n[... {len(formatted_body)-15000} chars truncated ...]"

                return (
                    f"HTTP {status} {resp.reason}\n"
                    f"Content-Type: {resp_headers.get('Content-Type', 'unknown')}\n\n"
                    f"{formatted_body}"
                )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            return f"HTTP {e.code} Error:\n{err_body[:5000]}"
        except Exception as e:
            raise ToolError(f"HTTP request to {url} failed: {e}")
