"""Data residency: the Messages API ``inference_geo`` parameter (docs: manage-claude/data-residency).

``"global"`` (default) or ``"us"``. Sent per request on the official Anthropic API only, for the models the docs list;
every other provider ignores the setting. A 400 that names the field makes the caller strip it and retry once.
``usage.inference_geo`` in a response says where inference ran.
"""
from __future__ import annotations

import re
from typing import Any

VALUES = ("global", "us")
SUPPORTED_PREFIXES = ("claude-fable-5", "claude-mythos-5", "claude-mythos-preview", "claude-opus-5", "claude-sonnet-5",
                      "claude-haiku-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


def normalize(value: Any) -> str:
    """The setting as a valid value, or '' (unset / unknown)."""
    v = str(value or "").strip().lower()
    return v if v in VALUES else ""


def supports(model: str, *, official: bool = True) -> bool:
    m = re.sub(r"(?<=\d)\.(?=\d)", "-", (model or "").lower().rsplit("/", 1)[-1])
    return bool(official) and m.startswith(SUPPORTED_PREFIXES)


def apply(kwargs: dict[str, Any], model: str, value: Any, *, official: bool = True) -> bool:
    geo = normalize(value)
    if not geo or not supports(model, official=official):
        return False
    body = dict(kwargs.get("extra_body") or {})
    body["inference_geo"] = geo
    kwargs["extra_body"] = body
    return True


def is_set(kwargs: dict[str, Any]) -> bool:
    return "inference_geo" in (kwargs.get("extra_body") or {})


def strip(kwargs: dict[str, Any]) -> bool:
    body = dict(kwargs.get("extra_body") or {})
    if "inference_geo" not in body:
        return False
    body.pop("inference_geo")
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    return True


def rejected(status: Any, text: str) -> bool:
    return status == 400 and "inference_geo" in (text or "").lower()


def reported(raw_usage: Any) -> str:
    """``usage.inference_geo`` of a response ('' when absent)."""
    v = raw_usage.get("inference_geo") if isinstance(raw_usage, dict) else getattr(raw_usage, "inference_geo", None)
    return v.strip().lower() if isinstance(v, str) else ""
