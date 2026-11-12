"""Dashboard API for memory history, restore, redaction and dreams (the same modules the terminal's /memory and /dream use)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:
    from axon.features.dashboard import DashServer


def _s(body: dict[str, Any], key: str, limit: int = 400) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


def _v(body: dict[str, Any]) -> int:
    try:
        return int(body.get("v"))
    except (TypeError, ValueError):
        raise DashError("'v' must be a version number") from None


def _resolve(srv: "DashServer", ref: str) -> tuple[str, str]:
    from axon.features import memory_history as mh
    try:
        return mh.resolve(srv.workspace, ref)
    except ValueError as e:
        raise DashError(str(e), 404) from None


def history(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    """Versions of one memory (``ref``: global|project|local or scope:id). With ``v``: that version's text."""
    from axon.features import memory_history as mh
    from axon.features.dashboard import redact_text
    key, label = _resolve(srv, qs.get("ref", ""))
    if qs.get("v"):
        e = mh.get_version(srv.workspace, key, int(qs["v"]) if qs["v"].isdigit() else -1)
        if e is None:
            raise DashError("No such version.", 404)
        return {"key": key, "label": label, **e.brief(), "text": redact_text(e.text)}
    return {"key": key, "label": label, "versions": [e.brief() for e in reversed(mh.versions(srv.workspace, key))]}


def _guard(fn: Any) -> Any:
    from axon.features import memory_history as mh
    try:
        return fn()
    except mh.Conflict as e:
        raise DashError(str(e), 409) from None
    except ValueError as e:
        raise DashError(str(e)) from None
    except OSError as e:
        raise DashError(f"Could not write it ({type(e).__name__}).", 500) from None


def restore(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_history as mh
    key, _ = _resolve(srv, _s(body, "ref", 200))
    exp = body.get("expected_sha")
    res = _guard(lambda: mh.restore_version(srv.workspace, key, _v(body), expected_sha=exp if isinstance(exp, str) else None))
    _reload(srv)
    return res


def redact(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_history as mh
    key, _ = _resolve(srv, _s(body, "ref", 200))
    res = _guard(lambda: mh.redact_version(srv.workspace, key, _v(body), _s(body, "secret", 2000)))
    _reload(srv)
    return res


def _reload(srv: "DashServer") -> None:
    for rt in srv.all_runtimes():
        try:
            rt.agent.reload_memory()
        except Exception:
            pass


# ─── dreams ─────────────────────────────────────────────────────────────────

def _proposal(srv: "DashServer", p: Any) -> dict[str, Any]:
    from axon.features import memory_dream as md
    d = md.diff(srv.workspace, p)
    return {"id": p.id, "status": p.status, "model": p.model, "cost": p.cost, "usage": p.usage, "sessions": p.sessions,
            "created": p.created, "insights": p.insights, "summary": md.summary_line(d), "diff": d}


def dream_state(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import memory_dream as md
    j = md.current()
    props = md.list_proposals(srv.workspace)
    want = md.load(srv.workspace, qs.get("id", "")) if qs.get("id") else (props[0] if props else None)
    return {"job": j.info() if j else None, "proposals": [{"id": p.id, "status": p.status, "notes": len(p.notes), "cost": p.cost} for p in props[:10]],
            "proposal": _proposal(srv, want) if want else None}


def dream_start(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_dream as md
    rts = srv.all_runtimes()
    if not rts:
        raise DashError("Open a chat first: a dream uses that chat's model and key.", 409)
    agent = rts[0].agent
    try:
        provider, model = md.resolve_provider(agent)
        job = md.start(srv.workspace, provider, model, getattr(agent, "ledger", None),
                       str(getattr(getattr(agent, "session", None), "active_session_id", "") or ""))
    except RuntimeError as e:
        raise DashError(str(e), 409) from None
    return {"ok": True, "job": job.info()}


def dream_cancel(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_dream as md
    return {"ok": True, "cancelled": md.cancel()}


def dream_apply(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_dream as md
    p = md.load(srv.workspace, _s(body, "id", 60))
    if p is None:
        raise DashError("No such dream.", 404)
    res = _guard(lambda: md.undo(srv.workspace, p) if body.get("undo") else md.apply(srv.workspace, p))
    _reload(srv)
    return res


def get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any | None:
    if route == "/api/manage/memory/history":
        return history(srv, qs)
    if route == "/api/manage/memory/dream":
        return dream_state(srv, qs)
    return None


_POST = {"/api/manage/memory/restore": restore, "/api/manage/memory/redact": redact,
         "/api/manage/memory/dream/start": dream_start, "/api/manage/memory/dream/cancel": dream_cancel,
         "/api/manage/memory/dream/apply": dream_apply}


def post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any | None:
    fn = _POST.get(route)
    return fn(srv, body) if fn else None
