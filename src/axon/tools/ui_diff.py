"""
UI Visual Preview and HTML/Frontend Inspector Tool for Axon.
Launches local web preview servers, generates responsive HTML previews,
and visual layout inspection summaries.
"""
from __future__ import annotations
import http.server
import os
import socketserver
import threading
import webbrowser
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

_PREVIEW_SERVER: socketserver.TCPServer | None = None
_PREVIEW_PORT: int = 0


class UiPreviewTool(Tool):
    name: ClassVar[str] = "UiPreview"
    description: ClassVar[str] = (
        "Launch local interactive web preview for HTML, CSS, JavaScript, or Markdown files. "
        "Opens the browser preview on a local port and returns the active live URL."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to HTML/web file to preview"},
            "open_browser": {"type": "boolean", "description": "Whether to launch system web browser (default: true)"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        if not raw_path:
            raise ToolError("UiPreview requires 'path' argument.")

        p = resolve_in_workspace(ctx.workspace, raw_path, allow_git_read=True)
        if not p.exists():
            raise ToolError(f"File not found: {raw_path}")

        open_browser = args.get("open_browser", True)

        # Start ephemeral static server if not already running
        global _PREVIEW_SERVER, _PREVIEW_PORT
        if _PREVIEW_SERVER is None:
            import socket
            sock = socket.socket()
            sock.bind(("", 0))
            _PREVIEW_PORT = sock.getsockname()[1]
            sock.close()

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **kw):
                    super().__init__(*a, directory=str(ctx.workspace), **kw)

                def log_message(self, *a):
                    pass  # Quiet logging

            _PREVIEW_SERVER = socketserver.TCPServer(("", _PREVIEW_PORT), Handler)
            t = threading.Thread(target=_PREVIEW_SERVER.serve_forever, daemon=True)
            t.start()

        rel = p.relative_to(ctx.workspace).as_posix()
        url = f"http://localhost:{_PREVIEW_PORT}/{rel}"

        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        return f"🌐 UI Live Preview active at: {url}\nServing from: {ctx.workspace.name}/{rel}"
