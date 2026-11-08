"""
Context map: what fills the window right now (system prompt, tool schemas, memory, skills, messages,
tool results) with tokens and share, which items could be dropped, and dropping a tool result from
context while keeping its full text on disk. Plain functions returning data; /ctxmap in
axon.commands.backend_cmds renders them. Pins (axon.features.insights) are always respected.
"""
from __future__ import annotations

import json
import re
from typing import Any

from axon.agent.context import result_id, result_text
from axon.agent.state import estimate_content_tokens

DROPPED_MARK = "[Result dropped from context"
KEEP_RECENT = 4          # messages this close to the end are not offered for compaction


def _tok(obj: Any) -> int:
    try:
        return int(estimate_content_tokens(obj))
    except Exception:
        return max(1, len(json.dumps(obj, default=str)) // 4)


def _call_labels(messages: list[dict[str, Any]]) -> dict[str, str]:
    from axon.features.insights import tool_calls

    def hint(inp: Any) -> str:
        for k in ("file_path", "path", "pattern", "command", "url", "query"):
            if isinstance(inp, dict) and inp.get(k):
                v = " ".join(str(inp[k]).split())
                return v if len(v) <= 48 else "…" + v[-47:]      # the end of a path is the file name
        return ""
    return {cid: f"{name} {hint(inp)}".strip() for cid, name, inp in tool_calls(messages)}


def _is_dropped(content: Any) -> bool:
    return DROPPED_MARK in result_text(content)[:80]


def ctxmap_data(agent: Any) -> dict[str, Any]:
    from axon.commands.context_cmds import _block_kind, _budget, _system_blocks, _tool_schemas
    from axon.features.insights import sync_pins
    sync_pins(agent)
    pins = getattr(agent.context, "pins", {}) or {}
    items: list[dict[str, Any]] = []
    for i, b in enumerate(_system_blocks(agent), 1):
        text = str(b.get("text", ""))
        kind = _block_kind(text)
        items.append({"id": f"s{i}", "kind": "agents" if kind == "agents" else kind, "label": text.strip().splitlines()[0][:60] if text.strip() else "system block",
                      "tokens": _tok(text), "droppable": False, "compactable": False, "pinned": False, "why": "part of the prompt, not removable here"})
    schemas = _tool_schemas(agent)
    if schemas:
        items.append({"id": "tools", "kind": "tools", "label": f"{len(schemas)} tool schemas", "tokens": _tok(json.dumps(schemas)),
                      "droppable": False, "compactable": False, "pinned": False, "why": "needed to call tools"})
    msgs = list(agent.conversation.messages)
    labels = _call_labels(msgs)
    n = len(msgs)
    for idx, m in enumerate(msgs):
        content = m.get("content")
        old = idx < n - KEEP_RECENT
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if m.get("role") == "tool":
            results, rest_tokens = [m], 0
            rest = []
        else:
            rest = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_result")]
            rest_tokens = _tok(rest) if blocks else _tok(content)
        if m.get("role") != "tool" and (rest or not blocks) and rest_tokens:
            items.append({"id": f"m{idx}", "kind": "message", "label": f"{m.get('role', '?')} message {idx}", "tokens": rest_tokens,
                          "droppable": False, "compactable": old, "pinned": False,
                          "why": "older turn: /compact can summarise it" if old else "recent turn"})
        for r in results:
            rid = result_id(m, r if m.get("role") != "tool" else None)
            body = m.get("content") if m.get("role") == "tool" else r.get("content")
            dropped = _is_dropped(body)
            pinned = rid in pins
            items.append({"id": rid or f"r{idx}", "kind": "tool_result", "label": labels.get(rid, "tool result"), "tokens": _tok(body),
                          "droppable": bool(rid) and not pinned and not dropped, "compactable": old and not pinned and not dropped,
                          "pinned": pinned, "dropped": dropped,
                          "why": "pinned: kept in full" if pinned else "already dropped" if dropped else "can be dropped: /ctxmap drop " + (rid or "")})
    total = sum(i["tokens"] for i in items)
    for i in items:
        i["share"] = i["tokens"] / total if total else 0.0
    groups: dict[str, int] = {}
    for i in items:
        groups[i["kind"]] = groups.get(i["kind"], 0) + i["tokens"]
    window, threshold, _reserve = _budget(agent)
    return {"window": window, "threshold": threshold, "total": total, "items": items, "groups": groups,
            "droppable_tokens": sum(i["tokens"] for i in items if i["droppable"])}


def _safe_name(rid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", rid)[:80] or "result"


def drop_result(agent: Any, result_id_: str) -> dict[str, Any]:
    """Replace a tool result in the live conversation with a short stub; the full text is saved to disk.
    Returns {"ok", "message", "path", "tokens_freed"}."""
    from axon import storage
    from axon.features.insights import sync_pins
    sync_pins(agent)
    rid = result_id_.strip()
    if rid in (getattr(agent.context, "pins", {}) or {}):
        return {"ok": False, "message": f"{rid} is pinned and stays in context. /unpin it first."}
    target: tuple[dict[str, Any], str | None] | None = None
    for m in agent.conversation.messages:
        if m.get("role") == "tool" and result_id(m) == rid:
            target = (m, None)
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and result_id(m, b) == rid:
                target = (b, "content")
    if rid == "" or target is None:
        return {"ok": False, "message": f"No tool result {rid!r} in this conversation. /ctxmap lists them."}
    holder, field = target
    key = field or "content"
    old = holder.get(key)
    if _is_dropped(old):
        return {"ok": False, "message": f"{rid} was already dropped."}
    text = result_text(old)
    ws = agent.settings.workspace
    sid = str(getattr(agent.session, "active_session_id", "") or "")
    try:
        d = storage.session_dir(ws, sid) / "dropped"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe_name(rid)}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "message": f"Could not save the result to disk, so it was not dropped: {e}"}
    freed = _tok(old)
    stub = f"{DROPPED_MARK} (about {freed:,} tokens). The full text is saved at {path}; Read that file if it is needed again.]"
    if _tok(stub) >= freed:
        path.unlink(missing_ok=True)
        return {"ok": False, "message": f"{rid} is only about {freed:,} tokens, less than the note that would replace it, so it was kept."}
    holder[key] = stub
    return {"ok": True, "path": str(path), "tokens_freed": max(0, freed - _tok(stub)),
            "message": f"Dropped {rid} from context (about {freed:,} tokens). Full text kept at {path}."}
