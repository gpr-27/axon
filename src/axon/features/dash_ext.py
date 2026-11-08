"""Extensions page: folder trust for project hooks, plugins and MCP servers (the page's /trust)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer


def trust_view(workspace: Path) -> dict[str, Any]:
    """What this folder would run, and whether it is trusted. ``sha`` identifies exactly this list."""
    from axon.hooks import trust
    from axon.features.dashboard import redact_text
    items = trust.collect(workspace)
    status = trust.status(workspace, items)
    return {"status": status, "allowed": trust.allowed(workspace), "gated": trust.is_interactive(),
            "sha": trust.fingerprint(items) if items else "",
            "items": [{"kind": i.kind, "source": i.source, "detail": redact_text(i.detail)[:400]} for i in items[:200]]}


def trust_post(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.hooks import trust
    ws = srv.workspace
    action = body.get("action")
    if action == "trust":
        items = trust.collect(ws)
        if not items:
            return {"ok": True, "message": "Nothing in this folder runs code, so there is nothing to trust."}
        if body.get("sha") != trust.fingerprint(items):
            raise DashError("The list changed since you looked at it. Reload the page and read it again.", 409)
        trust.trust(ws, items)
        msg = f"Trusted {ws}. Its project hooks, plugins and MCP servers are loaded now."
    elif action == "revoke":
        msg = (f"Stopped trusting {ws}. Project hooks stop now; project MCP servers stay connected until you restart."
               if trust.revoke(ws) else "This folder was not trusted.")
    else:
        raise DashError("action must be trust or revoke")
    for rt in srv.all_runtimes():
        hooks = getattr(rt.agent, "hooks", None)
        if hooks is not None and hasattr(hooks, "reload"):
            try:
                hooks.reload()
            except Exception:
                pass
    try:
        from axon.mcp import bridge as bridge_mod
        from axon.mcp.config import MCPConfig
        if action == "trust" and bridge_mod._bridge is not None:
            bridge_mod._bridge.connect_all(MCPConfig(ws).load_specs())
    except Exception:
        pass
    return {"ok": True, "message": msg}
