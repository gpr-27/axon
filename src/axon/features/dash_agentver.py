"""Dashboard side of agent definition versions (Extensions > Agents > Versions).

Reading (list, diff) works on the phone listener too; writing (rollback, pin) is refused there, see dashboard.py.
All logic lives in axon.agent.agent_history. Server text reaches the page only through text nodes.
"""
from __future__ import annotations

from typing import Any

from axon.agent import agent_history as ah
from axon.features.dash_common import DashError


def _find(srv: Any, name: str) -> tuple[Any, str]:
    t = ah.find_definition(srv.workspace, name)
    if t is None:
        raise DashError(f"No file-based agent named '{name}'", 404)
    return t


def get(srv: Any, route: str, qs: dict[str, str]) -> dict[str, Any]:
    d, scope = _find(srv, qs.get("name", ""))
    if route.endswith("/diff"):
        vs = ah.versions(scope, d.name)
        nums = [int(x) for x in (qs.get("a", ""), qs.get("b", "")) if x.isdigit()]
        if not vs:
            raise DashError("No versions recorded yet", 404)
        a, b = (nums + [None, None])[:2] if nums else (vs[-2]["v"] if len(vs) > 1 else vs[-1]["v"], vs[-1]["v"])
        if b is None:
            b = vs[-1]["v"]
        ra, rb = ah.get_version(scope, d.name, a), ah.get_version(scope, d.name, b)
        if ra is None or rb is None:
            raise DashError("Version not found", 404)
        return {"a": a, "b": b, "lines": ah.diff_text(ra["text"], rb["text"], f"v{a}", f"v{b}")}
    pin = ah.load_pins(srv.workspace).get(d.name.lower())
    return {"name": d.name, "source": str(d.source), "plugin": scope.startswith("plugin-"),
            "pinned": pin["v"] if pin and pin.get("scope") == scope else None, "note": ah.pin_note(srv.workspace, d),
            "versions": [{"v": v["v"], "ts": v["ts"], "who": v["who"], "sha": v["sha256"][:8], "size": len(v["text"])}
                         for v in reversed(ah.versions(scope, d.name))]}


def post(srv: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Loopback only (the caller refuses the phone listener)."""
    d, scope = _find(srv, str(body.get("name") or ""))
    action = str(body.get("action") or "")
    raw = body.get("version")
    n = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else (int(raw) if isinstance(raw, str) and raw.isdigit() else None)
    if action == "rollback":
        if n is None:
            raise DashError("version is required")
        try:
            return {"ok": True, "version": ah.rollback(srv.workspace, d, scope, n)}
        except ValueError as e:
            raise DashError(str(e)) from None
    if action in ("pin", "unpin"):
        from axon.storage import active_session_id
        if not active_session_id():
            raise DashError("Pins belong to a session: pin from the terminal inside the session (/agents pin).")
        if action == "unpin":
            ah.set_pin(srv.workspace, d.name, scope, None)
        else:
            if n is None or ah.get_version(scope, d.name, n) is None:
                raise DashError("Unknown version")
            ah.set_pin(srv.workspace, d.name, scope, n)
        return {"ok": True}
    raise DashError("unknown action")
