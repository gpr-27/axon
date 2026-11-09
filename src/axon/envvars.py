"""
Reading environment variables the way the documentation describes them.

On/off variables take ``1 true yes on`` and ``0 false no off`` in any casing. Numeric variables take plain
digits, scientific notation and digit separators (``2e3`` is 2000, ``64_000`` is 64000) unless the variable's
documentation says plain digits only, which :func:`plain_int` implements.
"""
from __future__ import annotations

import os
import re

_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def flag(name: str) -> bool | None:
    """True, False, or None when the variable is unset, empty or not a recognised on/off word."""
    val = os.environ.get(name, "").strip().lower()
    if val in _ON:
        return True
    return False if val in _OFF else None


def on(name: str) -> bool:
    return flag(name) is True


def off(name: str) -> bool:
    return flag(name) is False


def number(name: str, default: float | None = None) -> float | None:
    """A numeric variable; anything that is not a finite number reads as unset."""
    raw = os.environ.get(name, "").strip().replace("_", "")
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val == val and val not in (float("inf"), float("-inf")) else default


def integer(name: str, default: int | None = None) -> int | None:
    val = number(name)
    return default if val is None else int(val)


def plain_int(name: str, default: int | None = None) -> int | None:
    """An integer written in plain digits only; any other spelling reads as unset."""
    raw = os.environ.get(name, "").strip()
    return int(raw) if re.fullmatch(r"\d+", raw) else default
