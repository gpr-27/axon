"""Refusal handling that works with every provider: one retry on a configurable fallback model, never a loop.

Signals (all end up as ``stop_reason == "refusal"``, or as a content-filter error before any reply):
Anthropic ``stop_reason: refusal`` (with ``stop_details``), OpenAI ``finish_reason: content_filter`` / a ``refusal``
field / ``incomplete_details.reason: content_filter``, and other providers' content-filter HTTP errors
(:func:`is_filter_error`).

With ``refusal_fallback_model`` set the loop retries the SAME request once on that model (any provider: the model is
switched for the rest of the turn, then restored). The refused reply is not kept in the history and every thinking
block is dropped (a reasoning block of one model is never replayed to another). When both ends are the official
Anthropic API and the refusal carried a ``fallback_credit_token`` (docs: Fallback credit), the token is echoed on
the retry so the prompt cache is not paid twice. Without a fallback model the user gets one line naming the next step.
"""
from __future__ import annotations

from typing import Any

_MARKERS = ("content_filter", "content filter", "content_policy", "content policy", "content_management_policy",
            "responsibleaipolicyviolation", "prohibited_content", "blocked by", "safety settings", "finish_reason: safety")


_credit: list[str | None] = [None]
BETA_FALLBACK_CREDIT = "fallback-credit-2026-07-01"


def take_credit() -> str | None:
    """The credit token for the retry request (one use)."""
    tok, _credit[0] = _credit[0], None
    return tok


def apply_credit(kwargs: dict[str, Any], settings: Any, *, official: bool, token: str | None = None) -> bool:
    """Official Anthropic API only: ask refusals to carry a credit token (the beta header) and, on the retry, echo
    it as ``fallback_credit_token``. Returns True when the beta header was added (see :func:`strip_credit`)."""
    if not official or not fallback_model(settings):
        return False
    from axon.providers.anthropic_wire import add_beta
    add_beta(kwargs, BETA_FALLBACK_CREDIT)
    if token:
        kwargs["extra_body"] = {**dict(kwargs.get("extra_body") or {}), "fallback_credit_token": token}
    return True


def strip_credit(kwargs: dict[str, Any], error_text: str) -> bool:
    text = (error_text or "").lower()
    if "fallback" not in text and "anthropic-beta" not in text:
        return False
    from axon.providers.anthropic_wire import drop_beta
    had = BETA_FALLBACK_CREDIT in str((kwargs.get("extra_headers") or {}).get("anthropic-beta", ""))
    drop_beta(kwargs, BETA_FALLBACK_CREDIT)
    body = dict(kwargs.get("extra_body") or {})
    had = body.pop("fallback_credit_token", None) is not None or had
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    return had


def fallback_model(settings: Any) -> str:
    return str(getattr(settings, "refusal_fallback_model", "") or "").strip()


def is_filter_error(err: Any) -> bool:
    """A provider error that means "the content was filtered" (not a transient failure)."""
    text = f"{type(err).__name__} {err} {getattr(err, 'body', '')}".lower()
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    return (status in (None, 400, 403, 422)) and any(m in text for m in _MARKERS)


def next_step_line(settings: Any) -> str:
    """The ONE line shown with a refusal when no retry happens."""
    if fallback_model(settings):
        return "already retried once on the fallback model; rephrase, or /clear to reset context"
    return "set refusal_fallback_model in /config to retry on another model, or /clear to reset context"


def _drop_thinking(messages: list[dict[str, Any]]) -> None:
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            kept = [b for b in c if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
            if kept and len(kept) != len(c):
                m["content"] = kept
        if isinstance(m, dict):
            m.pop("reasoning_content", None)


def switch(agent: Any, model: str) -> bool:
    """Quietly point the agent at *model* (and its provider/endpoint/key). The old state is kept for :func:`restore`."""
    try:
        from axon.commands.common import update_settings
        from axon.commands.model_cmds import _key_for_preset, _target_preset
        from axon.providers.registry import provider_for
        from pydantic import SecretStr
        st = agent.settings
        saved = {"model": st.model, "base_url": st.base_url, "provider": getattr(st, "provider", None), "api_key": st.api_key}
        preset = _target_preset(agent, model)
        changes = dict(saved)
        chosen = model
        if preset is not None:
            if preset.id != "openrouter" and chosen.lower().startswith(f"{preset.id}/"):
                chosen = chosen[len(preset.id) + 1:]
            changes.update(base_url=preset.base_url, provider=preset.id)
            changes["api_key"] = SecretStr((_key_for_preset(preset) or "local") if getattr(preset, "requires_key", True) else "local")
        changes["model"] = chosen
        old_provider = agent.provider
        update_settings(agent, **changes)
        try:
            agent.provider = provider_for(chosen, agent.settings)
        except Exception:
            update_settings(agent, **saved)
            agent.provider = old_provider
            return False
        agent._refusal_restore = (saved, old_provider)
        return True
    except Exception:
        return False


def restore(agent: Any) -> None:
    saved = getattr(agent, "_refusal_restore", None)
    if not saved:
        return
    agent._refusal_restore = None
    try:
        from axon.commands.common import update_settings
        update_settings(agent, **saved[0])
        agent.provider = saved[1]
    except Exception:
        pass


def retry(agent: Any, turn: Any = None, *, reason: str = "") -> str:
    """Prepare ONE retry on the fallback model. Returns the one-line notice ("" when no retry is possible).

    *turn* is the refused assistant turn already appended to the conversation (None for a content-filter error,
    where nothing was appended)."""
    model = fallback_model(agent.settings)
    if not model or model == agent.settings.model or getattr(agent, "_refusal_restore", None):
        return ""
    msgs = agent.conversation.messages
    if turn is not None and msgs and msgs[-1].get("role") == "assistant":
        msgs.pop()
    _drop_thinking(msgs)
    details = getattr(turn, "stop_details", None) if turn is not None else None
    token = details.get("fallback_credit_token") if isinstance(details, dict) else None
    claim = bool(details.get("fallback_has_prefill_claim")) if isinstance(details, dict) else False
    was = agent.settings.model
    if not switch(agent, model):
        return ""
    _credit[0] = token if token and not claim else None   # a prefill claim needs the refused content echoed: not supported
    return f"declined by {was}{(' (' + reason + ')') if reason else ''}; retrying once on {model}"
