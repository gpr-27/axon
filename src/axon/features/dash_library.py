"""Server side of the prompt library and the checkpoint timeline (``/api/library/*``).

Thin glue over ``prompt_library`` and ``checkpoint_view``, which hold the logic and are shared with the
terminal. Every handler runs behind the HTTP layer's Host, cookie and Origin checks (see dash_api).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features import checkpoint_view as cv
from axon.features import prompt_library as pl
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer


def _str(body: dict[str, Any], key: str, limit: int = 100_000) -> str:
    v = body.get(key, "")
    if v is None:
        return ""
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


def _num(v: Any, name: str = "n") -> int:
    if isinstance(v, bool):
        raise DashError(f"'{name}' must be a number")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise DashError(f"'{name}' must be a number") from None


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (pl.PromptError, cv.RestoreError) as e:
        raise DashError(str(e), 400) from None


def _rt(srv: "DashServer", session: str) -> Any:
    return srv.runtime((session or "").strip(), create=False)


def _prompts_sent(agent: Any) -> list[tuple[float, str]]:
    try:
        from axon.commands.session_cmds import rewind_points
        return [(p.ts, p.prompt) for p in rewind_points(agent)]
    except Exception:
        return []


def handle_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any:
    ws = srv.workspace
    if route == "/api/library/prompts":
        rows = pl.search(ws, qs.get("q", "")[:200], qs.get("scope", ""))
        return {"prompts": [p.to_dict() for p in rows], "project": ws.name}
    if route == "/api/library/export":
        return pl.export_bundle(ws, scope=qs.get("scope", ""))
    rt = _rt(srv, qs.get("session", ""))
    cp = getattr(getattr(rt, "agent", None), "checkpoints", None)
    if route == "/api/library/checkpoints":
        if cp is None:
            return {"checkpoints": [], "busy": False}
        return {"checkpoints": cv.timeline(cp, _prompts_sent(rt.agent), ws), "busy": bool(getattr(rt, "busy", False))}
    if route == "/api/library/checkpoint_diff":
        if cp is None:
            raise DashError("There are no checkpoints in this chat.", 404)
        return {"files": _run(cv.checkpoint_diff, cp, _num(qs.get("n")), ws, qs.get("path", ""))}
    if route == "/api/library/restore_plan":
        if cp is None:
            raise DashError("There are no checkpoints in this chat.", 404)
        plan = _run(cv.plan_restore, cp, _num(qs.get("n")), ws)
        plan["lines"] = cv.summary_lines(plan)
        plan["busy"] = bool(getattr(rt, "busy", False))
        return plan
    return None


def handle_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any:
    ws = srv.workspace
    if route == "/api/library/prompt_save":
        scope = _str(body, "scope", 20) or "global"
        p = _run(pl.save, ws, _str(body, "name", 100), _str(body, "text", pl.MAX_TEXT + 100), _str(body, "description", 1000),
                 scope, _str(body, "replace", 100), _str(body, "replace_scope", 20))
        return {"ok": True, "prompt": p.to_dict()}
    if route == "/api/library/prompt_delete":
        return {"ok": True, "message": _run(pl.delete, ws, _str(body, "name", 100), _str(body, "scope", 20))}
    if route == "/api/library/prompt_fill":
        p = pl.get(ws, _str(body, "name", 100))
        values = body.get("values")
        if p is None:
            raise DashError("No prompt with that name.", 404)
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(v) < 5000 for k, v in values.items()):
            raise DashError("'values' must map each field to text")
        return {"text": _run(pl.fill, p.text, values)}
    if route == "/api/library/starters_restore":
        return {"ok": True, "restored": pl.restore_starters()}
    if route == "/api/library/prompts_import":
        data = body.get("data")
        if not isinstance(data, (dict, list, str)):
            raise DashError("'data' must be the contents of an exported prompt file")
        return {"ok": True, **_run(pl.import_bundle, ws, data, _str(body, "scope", 20) or "global", _str(body, "on_conflict", 20) or "skip")}
    if route == "/api/library/restore":
        rt = _rt(srv, _str(body, "session", 300))
        cp = getattr(getattr(rt, "agent", None), "checkpoints", None)
        if cp is None:
            raise DashError("There are no checkpoints in this chat.", 404)
        if getattr(rt, "busy", False):
            raise DashError("Axon is working on a turn. Wait for it to finish, then restore.", 409)
        res = _run(cv.restore, cp, _num(body.get("n")), ws, _str(body, "conflicts", 20) or "refuse")
        try:
            for r in list(res["restored"]) + list(res["deleted"]) + list(res["kept"]):
                rt.agent.file_state.invalidate(Path(ws) / r)
            rt.agent.file_state.clear()                 # the agent must read again what changed on disk
        except Exception:
            pass
        return res
    return None
