"""
Agent package exports.

Names load on first use (PEP 562): importing a light submodule such as ``axon.agent.state`` must not pull in
the prompt builder, the tool registry and the whole loop (which also imports ``axon.tools``, which imports
``axon.agent.state`` again).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "Conversation",
    "FileState",
    "TodoState",
    "Todo",
    "build_system",
    "discover_project_context",
    "ContextManager",
    "run_subagent",
    "Agent",
    "TurnResult",
]

_LAZY = {
    "Conversation": "axon.agent.state",
    "FileState": "axon.agent.state",
    "TodoState": "axon.agent.state",
    "Todo": "axon.agent.state",
    "build_system": "axon.agent.prompt",
    "discover_project_context": "axon.agent.prompt",
    "ContextManager": "axon.agent.context",
    "run_subagent": "axon.agent.subagent",
    "Agent": "axon.agent.loop",
    "TurnResult": "axon.agent.loop",
}

if TYPE_CHECKING:  # pragma: no cover
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent, TurnResult
    from axon.agent.prompt import build_system, discover_project_context
    from axon.agent.state import Conversation, FileState, Todo, TodoState
    from axon.agent.subagent import run_subagent


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'axon.agent' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
