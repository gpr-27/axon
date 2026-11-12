"""One bounded, tool-less model call in a fresh context, on any provider (shared by the advisor and auto mode).

The model may belong to another provider than the main one: it is resolved through the normal provider registry
(``make_provider``: model id or ``provider/model`` prefix, with that provider's key and endpoint). The request is plain
chat (a system text and one user message): no tools, no JSON mode, no server-side features. Cost and tokens are recorded
in the ledger and the usage log under ``source`` so the usage table has a row of their own.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class SideResult:
    text: str = ""
    model: str = ""
    usage: Any = None
    cost: float = 0.0
    duration_s: float = 0.0
    error: str = ""             # "" when the call completed; else "timeout" or the exception name


def redact(text: str) -> str:
    try:
        from axon.features.memory_files import redact as _r
        return _r(str(text or ""))
    except Exception:
        return str(text or "")


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + " [...]"


def provider_for_model(agent: Any, model: str, role: str) -> Any:
    override = getattr(agent, f"{role}_provider", None)       # tests inject a fake here
    if override is not None:
        return override
    if model and model != agent.settings.model:
        from axon.agent.loop import make_provider
        return make_provider(model, agent.settings, role=role)
    return agent.provider


def _call(agent: Any, prov: Any, model: str, system: str, user: str, max_tokens: int, source: str) -> tuple[str, Any, float]:
    from axon.agent.loop import _as_float, _provider_lock
    from axon.agent.state import record_usage
    with _provider_lock(prov):
        for _ in prov.stream(model=model, system=[{"type": "text", "text": system}],
                             messages=[{"role": "user", "content": user}], tools=[], max_tokens=max_tokens,
                             effort="reflex", thinking=False):
            pass
        turn = prov.finalize()
    usage = getattr(turn, "usage", None)
    cost = 0.0
    if usage is not None:
        cost = _as_float(record_usage(agent.ledger, model, usage, source=source))
        agent._log_usage(model, usage, cost, source)
    return str(getattr(turn, "text", "") or ""), usage, cost


def ask(agent: Any, model: str, system: str, user: str, *, source: str, role: str,
        max_tokens: int = 600, timeout_s: float = 30.0) -> SideResult:
    """Never raises. A call that outlives ``timeout_s`` is abandoned (``error == "timeout"``)."""
    t0 = time.time()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            prov = provider_for_model(agent, model, role)
            box["r"] = _call(agent, prov, model, system, user, max_tokens, source)
        except BaseException as e:      # noqa: BLE001 - reported as a failed call
            box["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout_s if timeout_s and timeout_s > 0 else None)
    dur = time.time() - t0
    if th.is_alive():
        return SideResult(model=model, duration_s=dur, error="timeout")
    if "e" in box:
        return SideResult(model=model, duration_s=dur, error=type(box["e"]).__name__)
    text, usage, cost = box["r"]
    return SideResult(text, model, usage, cost, dur)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply (plain, fenced or surrounded by prose); None when there is none."""
    text = str(text or "").strip()
    cands = [text] + [m.group(1).strip() for m in _FENCE.finditer(text)]
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                cands.append(text[start:i + 1])
                break
        start = text.find("{", start + 1)
    for c in cands:
        try:
            v = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict):
            return v
    return None


def usage_row(label: str, res: Any) -> dict[str, Any] | None:
    """A usage-table row for a side call (None when it reported no usage)."""
    if getattr(res, "usage", None) is None:
        return None
    from axon.ui.render import step_usage
    row = step_usage(0, str(getattr(res, "model", "") or ""), getattr(res, "duration_s", None), res.usage, getattr(res, "cost", None))
    row["step"] = label
    return row


def merge_row(rows: list[dict[str, Any]], row: dict[str, Any]) -> None:
    """Add ``row`` to ``rows``; a row with the same label (many permission checks in one turn) is summed into one."""
    for r in rows:
        if r.get("step") == row.get("step") and isinstance(r.get("step"), str):
            for k in ("input", "cache_read", "cache_write", "output"):
                r[k] = int(r.get(k) or 0) + int(row.get(k) or 0)
            if row.get("cost") is not None:
                r["cost"] = float(r.get("cost") or 0.0) + float(row["cost"])
            if row.get("duration") is not None:
                r["duration"] = float(r.get("duration") or 0.0) + float(row["duration"])
            return
    rows.append(dict(row))
