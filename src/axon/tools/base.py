"""
Tool base class, execution context and small helpers shared by every tool.
"""
from __future__ import annotations

import inspect
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, TYPE_CHECKING

from axon.config import Settings
from axon.agent.state import FileState, TodoState
from axon.errors import ToolError

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
    # Hidden tools stay callable through ToolRegistry.get() (e.g. by slash
    # commands) but are not advertised to the model in schemas().
    hidden: ClassVar[bool] = False

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Execute the tool and return the output string or raise ToolError."""
        ...

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        """One-line, human-readable result summary for the UI trace (e.g. "200 lines · 6.1 KB",
        "4 matches in 2 files"). Never sent to the model. Subclasses override for precision."""
        return default_summary(output, is_error)

    def render_call(self, args: dict[str, Any]) -> str:
        """Render a concise one-line summary for the terminal UI."""
        parts = []
        for k, v in args.items():
            if isinstance(v, str):
                v = v if len(v) <= 60 else v[:57] + "..."
                parts.append(f'{k}:"{v}"')
            elif isinstance(v, (list, dict)):
                parts.append(f"{k}:[{len(v)}]")
            else:
                parts.append(f"{k}:{v}")
        return f"{self.name} {' '.join(parts)}".strip()


# ─── Shared helpers ──────────────────────────────────────────────────────────

def human_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


def default_summary(output: str, is_error: bool = False) -> str:
    text = (output or "").strip()
    if not text:
        return "error (no message)" if is_error else "no output"
    first = text.split("\n", 1)[0].strip()
    if len(first) > 90:
        first = first[:87] + "..."
    if is_error:
        return f"error: {first}"
    n = text.count("\n") + 1
    return first if n == 1 else f"{first} · {plural(n, 'line')}"


def _call_resolver(fn: Any, root: Path, raw: str | Path, allow_git_read: bool) -> Path:
    """Call axon.permissions.paths.resolve_in_workspace by keyword so we keep
    working if its parameter order/names evolve (root/ws/workspace, raw/path)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(root, raw, allow_git_read=allow_git_read)
    kwargs: dict[str, Any] = {}
    root_key = next((k for k in ("root", "ws", "workspace") if k in params), None)
    raw_key = next((k for k in ("raw", "path", "p") if k in params), None)
    if root_key and raw_key:
        kwargs[root_key] = root
        kwargs[raw_key] = raw
        if "allow_git_read" in params:
            kwargs["allow_git_read"] = allow_git_read
        return fn(**kwargs)
    if "allow_git_read" in params:
        return fn(root, raw, allow_git_read=allow_git_read)
    return fn(root, raw)


def resolve_path(ctx: ToolContext, raw: str | Path, *, allow_git_read: bool = False) -> Path:
    """Resolve a model-supplied path through the permission path jail."""
    from axon.permissions import paths as _paths
    raw_s = str(raw).strip() if raw is not None else ""
    if not raw_s:
        raw_s = "."
    # Models sometimes send quoted paths or file:// URIs.
    if len(raw_s) >= 2 and raw_s[0] == raw_s[-1] and raw_s[0] in "\"'":
        raw_s = raw_s[1:-1]
    if raw_s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        parsed = urlparse(raw_s)
        raw_s = unquote(parsed.path)
        if os.name == "nt" and len(raw_s) >= 3 and raw_s[0] == "/" and raw_s[2] == ":":
            raw_s = raw_s[1:]
    return _call_resolver(_paths.resolve_in_workspace, ctx.workspace, raw_s, allow_git_read)


def display_path(p: Path, workspace: Path) -> str:
    """Workspace-relative path with forward slashes, or absolute (forward slashes)."""
    try:
        rel = p.resolve().relative_to(workspace.resolve())
        s = rel.as_posix()
        return s if s != "." else "."
    except (ValueError, OSError):
        return p.as_posix()


def output_cap(ctx: ToolContext, default: int = 30_000) -> int:
    """Per-call output budget: the smaller of the tool default and settings.tool_output_cap."""
    try:
        cap = int(getattr(ctx.settings, "tool_output_cap", 0) or 0)
    except (TypeError, ValueError):
        cap = 0
    return min(default, cap) if cap > 0 else default


def cap_text(text: str, limit: int, hint: str = "") -> str:
    """Truncate text to `limit` chars on a line boundary with a visible marker."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.6:
        cut = limit
    omitted = len(text) - cut
    msg = f"\n\n[... output truncated: {omitted:,} more characters"
    if hint:
        msg += f". {hint}"
    return text[:cut] + msg + " ...]"


def int_arg(args: dict[str, Any], key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Parse an integer argument leniently (models send "5" or 5.0), with clamping."""
    val = args.get(key)
    if val is None or val == "":
        out = default
    else:
        try:
            out = int(float(val))
        except (TypeError, ValueError) as e:
            raise ToolError(f"Argument '{key}' must be an integer, got {val!r}.") from e
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def bool_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
    val = args.get(key, default)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(val)


def capture_checkpoint(ctx: ToolContext, p: Path) -> None:
    """Snapshot a file before modification, if a checkpoint manager is attached."""
    cp = getattr(ctx, "checkpoints", None)
    if cp is None:
        return
    fn = getattr(cp, "capture_before_edit", None)
    if fn is None:
        return
    try:
        fn(p)
    except Exception:
        # Checkpointing is best-effort; never block an edit on it.
        pass
