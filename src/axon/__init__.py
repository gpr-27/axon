"""
Axon package root.

The public names load on first use (PEP 562), so `axon --version`, `--help` and other quick commands
do not import the whole agent, its tools and the provider SDKs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.32.0"

__all__ = [
    "Agent",
    "TurnResult",
    "Settings",
    "AxonError",
    "ToolError",
    "PermissionDenied",
]

_LAZY = {
    "Agent": "axon.agent.loop",
    "TurnResult": "axon.agent.loop",
    "Settings": "axon.config",
    "AxonError": "axon.errors",
    "ToolError": "axon.errors",
    "PermissionDenied": "axon.errors",
}

if TYPE_CHECKING:  # pragma: no cover
    from axon.agent.loop import Agent, TurnResult
    from axon.config import Settings
    from axon.errors import AxonError, PermissionDenied, ToolError


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'axon' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
