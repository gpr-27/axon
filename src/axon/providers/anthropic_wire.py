"""Anthropic wire constants in one place: the API version header and the beta-header registry.

Every call site that talks to an Anthropic-format endpoint (the SDK client, the /models listing, the key
verifier, the ping request) asks :func:`api_version` for the ``anthropic-version`` value, and every optional
beta feature adds its header through :func:`add_beta`, so nothing is spelled twice.

API version
-----------
``2026-10-21`` is used on the official API host only. Its breaking changes (checked against Axon):
  * a ``document`` block inside an ``assistant`` message is a 400   -> Axon never sends one (documents go in user turns);
  * ``citations`` on user ``text`` blocks are a 400                   -> Axon sets none;
  * ``temperature`` / ``top_p`` / ``top_k`` are a 400                  -> Axon never sends them;
  * Admin / Analytics field renames and Authorization-Bearer keys     -> Axon calls neither (and sends x-api-key itself).
Anthropic-compatible gateways and local servers keep ``2023-06-01``: they may validate the header against a list
they have not updated. ``AXON_ANTHROPIC_VERSION`` overrides both (an escape hatch, not a setting).
"""
from __future__ import annotations

import os
from typing import Any

LEGACY_API_VERSION = "2023-06-01"
API_VERSION = "2026-10-21"

# Beta features Axon can send (name -> header value). New betas are added here and nowhere else.
BETA_CONTEXT_MANAGEMENT = "context-management-2025-06-27"
BETA_COMPACT = "compact-2026-01-12"        # server-side compaction at a token threshold (edit type compact_20260112)
COMPACT_EDIT = "compact_20260112"
BETAS: dict[str, str] = {"context_management": BETA_CONTEXT_MANAGEMENT, "compact": BETA_COMPACT}


def api_version(base_url: str = "") -> str:
    """The ``anthropic-version`` value for requests to *base_url* (empty: assume a non-official host)."""
    forced = os.environ.get("AXON_ANTHROPIC_VERSION", "").strip()
    if forced:
        return forced
    try:
        from axon.providers.transport import is_official_anthropic
        if base_url and is_official_anthropic(base_url):
            return API_VERSION
    except Exception:
        pass
    return LEGACY_API_VERSION


def add_beta(kwargs: dict[str, Any], beta: str) -> None:
    """Merge *beta* into ``kwargs["extra_headers"]["anthropic-beta"]`` (comma separated, no duplicates)."""
    headers = dict(kwargs.get("extra_headers") or {})
    have = [b.strip() for b in str(headers.get("anthropic-beta", "")).split(",") if b.strip()]
    if beta not in have:
        have.append(beta)
    headers["anthropic-beta"] = ",".join(have)
    kwargs["extra_headers"] = headers


def drop_beta(kwargs: dict[str, Any], beta: str) -> None:
    """Remove *beta* from the request headers again (a gateway rejected it)."""
    headers = dict(kwargs.get("extra_headers") or {})
    have = [b.strip() for b in str(headers.get("anthropic-beta", "")).split(",") if b.strip() and b.strip() != beta]
    if have:
        headers["anthropic-beta"] = ",".join(have)
    else:
        headers.pop("anthropic-beta", None)
    if headers:
        kwargs["extra_headers"] = headers
    else:
        kwargs.pop("extra_headers", None)


def has_compaction(config: dict[str, Any] | None) -> bool:
    """True when a ``context_management`` value carries the server-side compaction edit."""
    return any(isinstance(e, dict) and e.get("type") == COMPACT_EDIT for e in (config or {}).get("edits", []) or [])


def apply_context_management(kwargs: dict[str, Any], config: dict[str, Any]) -> None:
    """Put the ``context_management`` field and its beta header(s) on a Messages (or count_tokens) request."""
    body = dict(kwargs.get("extra_body") or {})
    body["context_management"] = config
    kwargs["extra_body"] = body
    if any(isinstance(e, dict) and e.get("type") != COMPACT_EDIT for e in config.get("edits", []) or []):
        add_beta(kwargs, BETA_CONTEXT_MANAGEMENT)
    if has_compaction(config):
        add_beta(kwargs, BETA_COMPACT)


def strip_compaction_edit(kwargs: dict[str, Any]) -> bool:
    """Remove only the compaction edit (an endpoint or model rejected it), keeping any clearing edit. True when removed."""
    body = dict(kwargs.get("extra_body") or {})
    cm = body.get("context_management")
    if not has_compaction(cm):
        return False
    edits = [e for e in cm.get("edits", []) if not (isinstance(e, dict) and e.get("type") == COMPACT_EDIT)]
    drop_beta(kwargs, BETA_COMPACT)
    if edits:
        body["context_management"] = {**cm, "edits": edits}
        kwargs["extra_body"] = body
    else:
        body.pop("context_management", None)
        drop_beta(kwargs, BETA_CONTEXT_MANAGEMENT)
        if body:
            kwargs["extra_body"] = body
        else:
            kwargs.pop("extra_body", None)
    return True


def strip_context_management(kwargs: dict[str, Any]) -> bool:
    """Undo :func:`apply_context_management` (an endpoint rejected it). True when something was removed."""
    body = dict(kwargs.get("extra_body") or {})
    had = body.pop("context_management", None) is not None
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    drop_beta(kwargs, BETA_CONTEXT_MANAGEMENT)
    drop_beta(kwargs, BETA_COMPACT)
    return had


def compaction_iterations(raw_usage: Any) -> list[dict[str, int]]:
    """The ``compaction`` entries of ``usage.iterations`` (token counts per summarizing call), SDK object or dict."""
    items = raw_usage.get("iterations") if isinstance(raw_usage, dict) else getattr(raw_usage, "iterations", None)
    if items is None and not isinstance(raw_usage, dict):
        extra = getattr(raw_usage, "model_extra", None)
        items = extra.get("iterations") if isinstance(extra, dict) else None
    out: list[dict[str, int]] = []
    for it in items or []:
        get = (lambda k, it=it: it.get(k)) if isinstance(it, dict) else (lambda k, it=it: getattr(it, k, None))
        if get("type") != "compaction":
            continue
        row = {k: int(get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        out.append(row)
    return out
