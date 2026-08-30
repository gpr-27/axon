"""
Tool base class and execution context.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, TYPE_CHECKING
from axon.config import Settings
from axon.agent.state import FileState, TodoState

if TYPE_CHECKING:
    from axon.agent.loop import Agent
    from axon.session.ledger import Ledger
    from axon.session.checkpoint import CheckpointManager

@dataclass
class ToolContext:
    workspace: Path
    file_state: FileState
    todos: TodoState
    settings: Settings
    ledger: Ledger | None = None
    agent: Agent | None = None  # For sub-agent dispatch
    checkpoints: CheckpointManager | None = None

class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    schema: ClassVar[dict[str, Any]]
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[Literal["allow", "ask", "deny"]] = "ask"

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Execute the tool and return the output string or raise ToolError."""
        ...

    def render_call(self, args: dict[str, Any]) -> str:
        """Render a concise one-line summary for the terminal UI."""
        formatted_args = " ".join(f'{k}:"{v}"' if isinstance(v, str) else f"{k}:{v}" for k, v in args.items())
        return f"{self.name} {formatted_args}".strip()
