"""
Decision trail of a tool call: why it was chosen, what allowed it, what it returned and what came next.
Plain functions over the saved turn traces (axon.agent.trace); the terminal view is /trail in
axon.commands.backend_cmds. A piece the trace did not record is None, never inferred.
"""
from __future__ import annotations

from typing import Any


def list_calls(traces: list[Any]) -> list[dict[str, Any]]:
    """Every tool call of the session in order: n (1-based), turn, step index, tool, id."""
    out: list[dict[str, Any]] = []
    for tr in traces:
        for st in tr.steps:
            if st.kind == "tool":
                out.append({"n": len(out) + 1, "turn": tr.turn, "step": st.index, "name": st.data.get("name", "?"),
                            "id": st.data.get("id", "")})
    return out


def _target(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in ("path", "file_path", "notebook_path", "pattern", "query", "url", "command", "description"):
        if inp.get(k):
            return str(inp[k])[:80]
    return ""


def _describe_next(trace: Any, pos: int) -> dict[str, Any]:
    for st in trace.steps[pos + 1:]:
        if st.kind == "tool":
            return {"kind": "tool", "step": st.index, "name": st.data.get("name", "?"), "target": _target(st.data.get("input")),
                    "summary": f"ran {st.data.get('name', '?')} {_target(st.data.get('input'))}".strip()}
        if st.kind == "llm":
            bits = [f"model call {st.data.get('iteration', '')}".strip(), f"stop {st.data.get('stop_reason', '?')}"]
            if st.data.get("tool_use_count"):
                bits.append(f"{st.data['tool_use_count']} tool call(s)")
            return {"kind": "llm", "step": st.index, "stop_reason": st.data.get("stop_reason", ""), "summary": ", ".join(bits)}
        if st.kind in ("retry", "compaction", "budget", "steer", "goal_check"):
            return {"kind": st.kind, "step": st.index, "summary": st.kind}
    return {"kind": "end", "summary": f"turn ended ({trace.stop_reason or 'no stop reason recorded'})"}


def trail_for(traces: list[Any], n: int | None = None) -> dict[str, Any] | None:
    """The trail for tool call number *n* of the session (default: the last one); None when absent."""
    calls = list_calls(traces)
    if not calls:
        return None
    want = calls[-1]["n"] if n is None else n
    if not 1 <= want <= len(calls):
        return None
    meta = calls[want - 1]
    for tr in traces:
        if tr.turn != meta["turn"]:
            continue
        for pos, st in enumerate(tr.steps):
            if st.index != meta["step"] or st.kind != "tool":
                continue
            d = st.data
            why = d.get("why") if isinstance(d.get("why"), dict) else None
            perm = d.get("permission") if isinstance(d.get("permission"), dict) else None
            result = None
            if "result_preview" in d or "is_error" in d:
                result = {"is_error": bool(d.get("is_error")), "preview": d.get("result_preview", ""),
                          "lines": d.get("lines"), "bytes": d.get("bytes"), "est_tokens": d.get("est_tokens"),
                          "duration_s": d.get("duration_s")}
            return {"n": want, "total": len(calls), "turn": tr.turn, "step": st.index, "id": d.get("id", ""),
                    "name": d.get("name", "?"), "input": d.get("input"), "rationale": dict(why) if why else None,
                    "permission": dict(perm) if perm else None, "hooks": list(d.get("hooks") or []),
                    "result": result, "next": _describe_next(tr, pos)}
    return None
