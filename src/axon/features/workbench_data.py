"""Numbers for the workbench views (context, connectors, limits, plan), as plain functions.

No terminal and no web imports: the terminal screens (``axon.commands.workbench_cmds``) and the web
workbench read these, so both show the same figures. ``dash_workbench`` can call them instead of its
own copies; the shapes are the same.

* ``context_breakdown(agent, real_used)``: window, used, one row per category, deferred tools, lists.
  Counts are estimates (about 3.7 characters per token) unless the provider reported the total
  (``real_used``), in which case ``calibrated`` is true and Messages absorbs the difference.
* ``connectors(ws)``: MCP servers with state, tool count and tool names.
* ``usage_limits(agent)``: real provider rate-limit rows only.
* ``plan_and_todos(agent)``, ``background(agent)``, ``environment(agent)``.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

CATEGORIES = (
    ("messages", "Messages"), ("system_tools", "System tools"), ("mcp_tools", "MCP tools"), ("skills", "Skills"),
    ("system_prompt", "System prompt"), ("mcp_instructions", "MCP server instructions"), ("custom_agents", "Custom agents"),
    ("memory_files", "Memory files"), ("autocompact", "Autocompact buffer"), ("free", "Free space"),
)
NEEDS_ACTION = ("failed", "needs-auth", "needs-config", "disconnected", "not connected", "blocked", "rejected", "pending-approval")


def _tok(text: Any) -> int:
    from axon.agent.state import estimate_content_tokens
    try:
        return int(estimate_content_tokens(text))
    except Exception:
        return max(1, len(str(text)) // 4)


def _schema_name(s: dict[str, Any]) -> str:
    return str(s.get("name") or s.get("function", {}).get("name", "?"))


def _server_of(tool: str) -> str:
    parts = tool.split("__")
    return parts[1] if len(parts) > 2 else "mcp"


def _block_class(text: str) -> str:
    head = text.lstrip()[:60].lower()
    if head.startswith("## memory"):
        return "memory_files"
    if head.startswith(("## skills", "## available skills")):
        return "skills"
    if head.startswith("## subagents"):
        return "custom_agents"
    if head.startswith("## mcp server instructions"):
        return "mcp_instructions"
    return "system_prompt"


def _lines_by_name(text: str, pattern: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        m = re.match(pattern, line)
        if m:
            out.append({"name": m.group(1), "tokens": _tok(line)})
    return out


def context_breakdown(agent: Any, real_used: int | None = None) -> dict[str, Any]:
    """Categories, per-category lists and the window figures (see the module docstring)."""
    from axon.commands.context_cmds import _budget, _system_blocks, _tool_schemas
    from axon.features.ctxmap import ctxmap_data
    window, threshold, _reserve = _budget(agent)
    cats = {k: 0 for k, _ in CATEGORIES}
    lists: dict[str, list[dict[str, Any]]] = {"mcp_tools": [], "memory_files": [], "custom_agents": [], "skills": [], "messages": []}
    for b in _system_blocks(agent):
        text = str(b.get("text", ""))
        k = _block_class(text)
        cats[k] += _tok(text)
        if k == "skills":
            lists["skills"] = _lines_by_name(text, r"- \*\*/?([^*]+)\*\*")
        elif k == "custom_agents":
            lists["custom_agents"] = [r for r in _lines_by_name(text, r"- \*\*([^*]+)\*\*") if r["name"] != "fork"]
    try:
        from axon.agent.memory import MemoryLoader
        files = MemoryLoader(Path(agent.settings.workspace)).load_startup()
        home = str(Path.home())
        lists["memory_files"] = [{"name": str(f.path).replace(home, "~", 1), "scope": f.scope, "tokens": f.tokens} for f in files]
    except Exception:
        pass
    by_server: dict[str, dict[str, Any]] = {}
    for s in _tool_schemas(agent):
        n = _schema_name(s)
        t = _tok(json.dumps(s))
        if n.startswith("mcp__"):
            cats["mcp_tools"] += t
            g = by_server.setdefault(_server_of(n), {"name": _server_of(n), "tokens": 0, "count": 0, "tools": []})
            g["tokens"] += t
            g["count"] += 1
            g["tools"].append({"name": n.split("__", 2)[-1], "tokens": t})
        else:
            cats["system_tools"] += t
    deferred = {"mcp_tools": 0, "mcp_count": 0}
    try:
        ts = agent.registry.tools.get("ToolSearch")
        dtools = list(getattr(ts, "deferred", {}).values()) if ts is not None else []
        if dtools:
            from axon.tools.interactive import schema_tokens
            deferred = {"mcp_tools": schema_tokens(dtools), "mcp_count": len(dtools)}
            for t in dtools:
                nm = str(t.name)
                g = by_server.setdefault(_server_of(nm), {"name": _server_of(nm), "tokens": 0, "count": 0, "tools": [], "deferred": 0})
                g["deferred"] = g.get("deferred", 0) + 1
                g["count"] += 1
                g["tools"].append({"name": nm.split("__", 2)[-1], "tokens": 0, "deferred": True})
    except Exception:
        pass
    lists["mcp_tools"] = sorted(by_server.values(), key=lambda g: -g["tokens"])
    cm = ctxmap_data(agent)
    msg_items = [i for i in cm["items"] if i["kind"] in ("message", "tool_result")]
    cats["messages"] = sum(i["tokens"] for i in msg_items)
    lists["messages"] = [{"id": i["id"], "label": i["label"], "tokens": i["tokens"], "droppable": i["droppable"], "pinned": i["pinned"],
                          "dropped": bool(i.get("dropped")), "why": i["why"]}
                         for i in sorted(msg_items, key=lambda i: -i["tokens"])[:12]]
    fixed = sum(v for k, v in cats.items() if k not in ("messages", "autocompact", "free"))
    calibrated = False
    if real_used and real_used > fixed + cats["messages"]:
        cats["messages"] = real_used - fixed
        calibrated = True
    used = fixed + cats["messages"]
    buffer = max(0, window - threshold)
    cats["autocompact"] = buffer
    cats["free"] = max(0, window - used - buffer)
    rows = [{"key": k, "label": label, "tokens": cats[k], "pct": round(100 * cats[k] / window, 1) if window else 0.0} for k, label in CATEGORIES]
    return {"window": window, "used": used, "pct": round(100 * used / window) if window else 0, "threshold": threshold,
            "rows": rows, "deferred": deferred, "lists": lists, "calibrated": calibrated,
            "estimate": "Token counts are estimates (about 3.7 characters per token) unless the provider reported the total."}


def connectors(ws: Path | str) -> dict[str, Any]:
    """MCP servers: ``{"servers": [{"name", "status", "tools", "tool_names", "scope", "error", "needs_reconnect",
    "needs_auth"}], "reconnect", "connected"}``. Never connects anything itself."""
    from axon.mcp.config import MCPConfig
    from axon.session.activity import redact_text
    ws = Path(ws)
    specs = MCPConfig(ws).load_specs()
    live: dict[str, dict[str, Any]] = {}
    names: dict[str, list[str]] = {}
    try:
        mod = sys.modules.get("axon.mcp.bridge")
        bridge = getattr(mod, "_bridge", None) if mod is not None else None
        if bridge is not None:
            live = {r["name"]: r for r in bridge.status_rows()}
            for t in bridge.active_tools:
                names.setdefault(str(getattr(t, "server_name", "")), []).append(str(t.name).split("__", 2)[-1])
    except Exception:
        pass
    rows = []
    for name, s in sorted(specs.items()):
        lv = live.get(name, {})
        status = "disabled" if s.disabled else (lv.get("status") or "not connected")
        if not s.disabled and getattr(s, "approval", "") in ("pending", "rejected"):
            status = "pending-approval" if s.approval == "pending" else "rejected"
        rows.append({"name": name, "status": status, "tools": lv.get("tools"), "tool_names": sorted(names.get(name, [])),
                     "scope": s.scope, "error": redact_text(str(lv.get("error") or ""))[:300],
                     "needs_reconnect": status in NEEDS_ACTION, "needs_auth": status == "needs-auth"})
    return {"servers": rows, "reconnect": sum(1 for r in rows if r["needs_reconnect"]),
            "connected": sum(1 for r in rows if r["status"] == "connected")}


def usage_limits(agent: Any | None) -> dict[str, Any]:
    from axon.features import quota
    rows = [{"name": r.name, "label": r.label, "used": r.used_fraction, "reset": quota.local_reset_text(r.reset_at),
             "limit": r.limit, "remaining": r.remaining}
            for r in quota.snapshot() if r.used_fraction is not None]
    s = getattr(agent, "settings", None)
    cap, turn_cap = getattr(s, "budget_session_usd", None), getattr(s, "budget_turn_usd", None)
    return {"limits": rows, "budget": {"session_usd": cap, "turn_usd": turn_cap} if (cap or turn_cap) else None}


def plan_and_todos(agent: Any) -> dict[str, Any]:
    """``{"plan_mode", "items": [{"content", "status"}], "done", "total"}`` from the live session."""
    items: list[dict[str, str]] = []
    try:
        for t in list(getattr(getattr(agent, "todos", None), "items", None) or []):
            items.append({"content": str(t.active_form or t.content) if t.status == "in_progress" else str(t.content), "status": str(t.status)})
    except Exception:
        pass
    return {"plan_mode": str(getattr(getattr(agent, "settings", None), "mode", "")) == "plan", "items": items,
            "done": sum(1 for i in items if i["status"] == "completed"), "total": len(items)}


def background(agent: Any) -> dict[str, Any]:
    """Running background shells, background turns and subagent tasks: ``{"rows": [{"kind", "id", "label",
    "status", "age_s"}], "running": n}``."""
    rows: list[dict[str, Any]] = []
    now = time.time()
    try:
        from axon.tools.shell import BACKGROUND
        for sh in BACKGROUND.all():
            rows.append({"kind": "shell", "id": sh.id, "label": str(sh.command), "status": str(sh.status),
                         "age_s": max(0.0, now - float(getattr(sh, "started", now)))})
    except Exception:
        pass
    turns = getattr(agent, "background_tasks", None)
    for tid, rec in (turns.items() if isinstance(turns, dict) else []):
        rows.append({"kind": "turn", "id": str(tid), "label": str(getattr(rec, "prompt", "")), "status": str(getattr(rec, "status", "?")),
                     "age_s": float(getattr(rec, "elapsed_s", 0.0) or 0.0)})
    try:
        mgr = getattr(agent, "subagents", None)
        for t in (mgr.all_tasks() if mgr is not None else []):
            rows.append({"kind": "subagent", "id": str(getattr(t, "id", "")), "label": str(getattr(t, "description", "") or getattr(t, "name", "")),
                         "status": str(t.status), "age_s": 0.0})
    except Exception:
        pass
    return {"rows": rows, "running": sum(1 for r in rows if str(r["status"]).startswith("running"))}


def environment(agent: Any) -> dict[str, Any]:
    from axon.features import worktree_view
    s = agent.settings
    ws = Path(getattr(s, "workspace", Path.cwd()))
    info = worktree_view.repo_info(ws)
    return {"workspace": str(ws), "name": ws.name, "branch": info["branch"] if info["repo"] else "", "repo": info["repo"],
            "model": str(getattr(s, "model", "")), "mode": str(getattr(s, "mode", "")),
            "session": str(getattr(getattr(agent, "session", None), "active_session_id", "") or "")}
