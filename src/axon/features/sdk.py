"""
Programmatic Python API for Axon (in the spirit of the Claude Agent SDK).

    from axon.features.sdk import AxonOptions, query, run

    for event in query("explain src/app.py", AxonOptions(mode="plan")):
        if event["type"] == "text_delta":
            print(event["text"], end="")

    result = run("fix the failing test", AxonOptions(mode="acceptEdits", max_turns=20))
    print(result.text, result.cost_usd, result.session_id)

Every event is a JSON-serializable dict with a ``type`` key:
``system/init``, ``llm_call_start``, ``llm_request``, ``text_delta``, ``thinking_delta``,
``tool_rationale``, ``tool_use``, ``permission``, ``hook``, ``tool_result``, ``tool_timing``,
``llm_call_end`` (usage + cost), ``cache_status``, ``retry``, ``compaction``, ``subagent``,
``budget``, ``memory_loaded`` and finally ``result``.

Approvals are non-interactive: the permission mode decides (``bypass`` / ``acceptEdits`` auto-allow
what they cover); anything that would need a human ("ask") is denied unless ``can_use_tool`` is given
and returns True for it. Nothing here reads stdin or prints.
"""
from __future__ import annotations

import dataclasses
import inspect
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypass")
INTERRUPTED_TEXT = "[Response interrupted]"
_FAILED_STOPS = {"error", "blocked", "interrupted", "budget_exceeded", "token_budget", "max_iterations"}
# Options a remote caller (e.g. `axon serve`) may set; code-only hooks are excluded.
JSON_OPTION_FIELDS = (
    "model", "provider", "base_url", "workspace", "mode", "effort", "thinking", "allowed_tools",
    "disallowed_tools", "system_prompt", "append_system_prompt", "max_turns", "trace_level",
    "resume", "continue_session", "connect_mcp", "max_budget_usd",
)


class SDKError(Exception):
    """Invalid SDK options or a failure to assemble the agent."""


@dataclass
class AxonOptions:
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    workspace: str | Path | None = None
    mode: str | None = None                    # default | acceptEdits | plan | bypass (None: settings)
    effort: str | None = None
    thinking: bool | None = None
    allowed_tools: list[str] = field(default_factory=list)      # allow rules, e.g. "Read", "Bash(git *)"
    disallowed_tools: list[str] = field(default_factory=list)   # bare names remove tools; "X(...)" deny rules
    system_prompt: str | None = None           # replaces Axon's system prompt entirely
    append_system_prompt: str | None = None
    max_turns: int | None = None
    trace_level: str | None = None
    max_budget_usd: float | None = None
    resume: str | None = None                  # session id (or name) to continue
    continue_session: bool = False             # resume the latest session of the workspace
    connect_mcp: bool = False                  # connect configured MCP servers (slow; off by default)
    can_use_tool: Callable[[str, dict[str, Any]], bool] | None = None
    provider_instance: Any = None              # pre-built Provider (tests, custom backends)
    settings_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, base: AxonOptions | None = None) -> AxonOptions:
        """Options from untrusted JSON (only :data:`JSON_OPTION_FIELDS`), layered over ``base``."""
        opts = dataclasses.replace(base) if base is not None else cls()
        if not data:
            return opts
        if not isinstance(data, dict):
            raise SDKError("options must be a JSON object")
        aliases = {"allowedTools": "allowed_tools", "disallowedTools": "disallowed_tools",
                   "systemPrompt": "system_prompt", "appendSystemPrompt": "append_system_prompt",
                   "maxTurns": "max_turns", "permissionMode": "mode", "permission_mode": "mode",
                   "cwd": "workspace", "traceLevel": "trace_level", "baseUrl": "base_url",
                   "continue": "continue_session", "maxBudgetUsd": "max_budget_usd"}
        for key, value in data.items():
            name = aliases.get(key, key)
            if name not in JSON_OPTION_FIELDS:
                raise SDKError(f"unknown or disallowed option: {key}")
            if name in ("allowed_tools", "disallowed_tools"):
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise SDKError(f"{key} must be a list of strings")
            setattr(opts, name, value)
        return opts


# ─── event serialization ───────────────────────────────────────────────────

_TYPE_NAMES = {
    "TextDelta": "text_delta", "ThinkingDelta": "thinking_delta", "LLMCallStart": "llm_call_start",
    "LLMRequestInfo": "llm_request", "LLMCallEnd": "llm_call_end", "ToolExecutionStart": "tool_use",
    "ToolExecutionResult": "tool_result", "ToolBatchStart": "tool_batch", "ToolTiming": "tool_timing",
    "PermissionDecisionEvent": "permission", "HookEvent": "hook", "RetryEvent": "retry",
    "ProviderRetry": "retry", "CompactionEvent": "compaction", "SubagentEvent": "subagent",
    "ToolRationale": "tool_rationale", "CacheStatus": "cache_status", "BudgetEvent": "budget",
    "MemoryLoadedEvent": "memory_loaded",
}
# Raw provider stream fragments: redundant with tool_use / llm_call_end, so not surfaced.
_SKIP = {"ToolUseStart", "ToolArgsDelta", "ToolUseComplete", "TurnComplete"}


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    try:
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
    except Exception:
        pass
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def usage_to_dict(usage: Any) -> dict[str, int]:
    """Normalized token usage (same keys as `axon -p --output-format json`)."""
    def g(name: str) -> int:
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0
    return {
        "input_tokens": g("input"),
        "output_tokens": g("output"),
        "cache_read_tokens": g("cache_read"),
        "cache_write_tokens": g("cache_write"),
        "reasoning_tokens": g("reasoning"),
    }


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().removesuffix("_event")


def event_to_dict(ev: Any) -> dict[str, Any] | None:
    """Serialize an Agent.on_event object to a JSON-safe dict (``None`` for internal fragments).

    Used by :func:`query`, `axon serve` and `--output-format stream-json`."""
    if isinstance(ev, dict):
        return _jsonable(ev)
    cls = type(ev).__name__
    if cls in _SKIP or not (dataclasses.is_dataclass(ev) and not isinstance(ev, type)):
        return None
    kind = _TYPE_NAMES.get(cls) or _snake(cls)
    if cls in ("TextDelta", "ThinkingDelta"):
        return {"type": kind, "text": ev.text}
    if cls == "ToolExecutionStart":
        return {"type": kind, "id": ev.id, "name": ev.name, "input": _jsonable(ev.input)}
    if cls == "ToolExecutionResult":
        return {"type": kind, "tool_use_id": ev.id, "name": ev.name, "content": ev.content,
                "is_error": bool(ev.is_error)}
    if cls == "ToolBatchStart":
        return {"type": kind, "count": ev.total_count,
                "tools": [{"id": t.id, "name": t.name} for t in ev.tools]}
    if cls == "LLMCallStart":
        return {"type": kind, "iteration": ev.iteration, "max_iterations": ev.max_iterations,
                "model": ev.model, "message_count": ev.message_count, "tokens_in": ev.tokens_in,
                "fed_back": _jsonable(ev.tools_ingested)}
    data = _jsonable(ev)
    if cls in ("LLMCallEnd", "CompactionEvent") and getattr(ev, "usage", None) is not None:
        data["usage"] = usage_to_dict(ev.usage)
    return {"type": kind, **data}


def result_to_dict(agent: Any, result: Any, elapsed: float, cost_usd: float | None = None) -> dict[str, Any]:
    """The final ``result`` event for a finished TurnResult."""
    session_cost = 0.0
    try:
        session_cost = float(agent.get_combined_cost())
    except Exception:
        try:
            session_cost = float(agent.ledger.total())
        except Exception:
            pass
    stop = str(getattr(result, "stop_reason", "") or "")
    return {
        "type": "result",
        "result": getattr(result, "final_text", "") or "",
        "is_error": stop in _FAILED_STOPS,
        "stop_reason": stop,
        "error": getattr(result, "error", None),
        "session_id": str(getattr(getattr(agent, "session", None), "active_session_id", "") or ""),
        "cost_usd": round(float(cost_usd if cost_usd is not None else session_cost), 8),
        "session_cost_usd": round(session_cost, 8),
        "iterations": int(getattr(result, "iterations", 0) or 0),
        "tool_calls": int(getattr(result, "tool_calls_count", 0) or 0),
        "usage": usage_to_dict(getattr(result, "usage", None)),
        "duration_s": round(float(elapsed), 3),
    }


# ─── agent assembly ────────────────────────────────────────────────────────

def _split_rules(values: list[str] | None) -> list[str]:
    try:
        from axon.cli import _split_tool_list
        return _split_tool_list(list(values or []))
    except Exception:
        return [v.strip() for v in values or [] if v and v.strip()]


def build_settings(options: AxonOptions) -> Any:
    """Load Settings like the CLI does (config files, env, then options) without any prompts."""
    from axon.config import ConfigError, PermissionConfig, Settings

    if options.mode is not None and options.mode not in PERMISSION_MODES:
        raise SDKError(f"invalid mode {options.mode!r} (expected one of {', '.join(PERMISSION_MODES)})")
    ws = Path(options.workspace).expanduser().resolve() if options.workspace else None
    if ws is not None and not ws.is_dir():
        raise SDKError(f"workspace does not exist: {ws}")
    overrides: dict[str, Any] = {
        "model": options.model,
        "provider": options.provider,
        "base_url": options.base_url,
        "api_key": options.api_key,
        "mode": options.mode,
        "effort": options.effort,
        "thinking": options.thinking,
        "workspace": ws,
        "max_iterations": options.max_turns if options.max_turns and options.max_turns > 0 else None,
        "append_system_prompt": options.append_system_prompt,
        "trace_level": options.trace_level,
        "budget_session_usd": options.max_budget_usd if options.max_budget_usd and options.max_budget_usd > 0 else None,
    }
    overrides.update(options.settings_overrides or {})
    if options.provider_instance is not None and not overrides.get("api_key"):
        overrides["api_key"] = "sdk-provider-instance"
    try:
        settings = Settings.load(overrides, require_api_key=False)
    except ConfigError as e:
        raise SDKError(str(e)) from e
    if ws is not None and getattr(settings, "workspace", None) != ws:
        settings = settings.model_copy(update={"workspace": ws})

    allowed = _split_rules(options.allowed_tools)
    deny_rules = [d for d in _split_rules(options.disallowed_tools) if "(" in d]
    if allowed or deny_rules:
        perms = settings.permissions
        new_perms = PermissionConfig(**{
            **perms.model_dump(),
            "allow": list(dict.fromkeys(list(perms.allow) + allowed)),
            "deny": list(dict.fromkeys(list(perms.deny) + deny_rules)),
        })
        settings = settings.model_copy(update={"permissions": new_perms})
    return settings


def _approver(options: AxonOptions) -> Callable[[str, dict[str, Any], Any], str] | None:
    """Agent.on_approval for headless use: ``can_use_tool`` decides, otherwise none (=> deny)."""
    cb = options.can_use_tool
    if cb is None:
        return None

    def on_approval(tool: str, args: dict[str, Any], _decision: Any) -> str:
        try:
            return "once" if cb(tool, dict(args or {})) else "deny"
        except Exception:
            return "deny"
    return on_approval


def _assemble_fallback(settings: Any, provider: Any, on_event: Any, on_approval: Any,
                       disallowed: list[str]) -> Any:
    """Agent assembly used only when cli.build_agent cannot take a pre-built provider."""
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent
    from axon.permissions.engine import PermissionEngine
    from axon.session.ledger import Ledger
    from axon.session.store import SessionStore
    from axon.tools import create_default_registry

    tools = create_default_registry()
    bare = {d for d in disallowed if "(" not in d}
    if bare:
        tools = tools.subset(names=[t.name for t in tools.all_tools() if t.name not in bare])
    return Agent(provider=provider, tools=tools, permissions=PermissionEngine(settings),
                 context=ContextManager(settings), session=SessionStore(workspace=settings.workspace),
                 ledger=Ledger(), settings=settings, on_event=on_event, on_approval=on_approval)


def _resume(agent: Any, options: AxonOptions) -> None:
    target: str | None = None
    if options.continue_session and not options.resume:
        target = agent.session.latest()
        if not target:
            raise SDKError("no previous session to continue in this workspace")
    elif options.resume:
        try:
            from axon.cli import find_session
            target, candidates = find_session(agent, options.resume)
        except ImportError:
            target, candidates = (options.resume if agent.session.exists(options.resume) else None), []
        if target is None:
            raise SDKError(f"ambiguous session {options.resume!r}: {', '.join(candidates[:8])}" if candidates
                           else f"no session found: {options.resume}")
    if not target:
        return
    try:
        agent.session.open(target)
        agent.conversation = agent.session.load(target)
        agent.conversation.repair()
        agent.ledger = agent.session.load_ledger(target, agent.settings.model)
    except Exception as e:
        raise SDKError(f"could not resume session {target}: {e}") from e
    try:
        from axon.agent.trace import load_traces
        agent.traces = load_traces(agent.settings.workspace, target, legacy_session_dir=agent.session.session_dir)
    except Exception:
        pass


def build_agent(options: AxonOptions | None = None, *, on_event: Callable[[Any], None] | None = None) -> Any:
    """Assemble an Agent exactly like ``axon`` does (no printing, no REPL, no stdin prompts)."""
    options = options or AxonOptions()
    settings = build_settings(options)
    on_approval = _approver(options)
    disallowed = _split_rules(options.disallowed_tools)
    provider = options.provider_instance
    try:
        from axon.cli import build_agent as cli_build_agent
        params = inspect.signature(cli_build_agent).parameters
    except (ImportError, AttributeError, TypeError, ValueError):
        cli_build_agent, params = None, {}
    try:
        if cli_build_agent is not None and (provider is None or "provider" in params):
            kwargs: dict[str, Any] = {"on_event": on_event, "on_approval": on_approval,
                                      "connect_mcp": bool(options.connect_mcp), "disallowed_tools": disallowed}
            if provider is not None:
                kwargs["provider"] = provider
            agent = cli_build_agent(settings, **kwargs)
        else:
            if provider is None:
                from axon.providers.registry import provider_for
                provider = provider_for(settings.model, settings)
            agent = _assemble_fallback(settings, provider, on_event, on_approval, disallowed)
    except SDKError:
        raise
    except Exception as e:
        raise SDKError(f"failed to start agent: {e}") from e

    # Headless: never open the live subagent monitor (it reads keys from the terminal).
    agent._interactive_monitor = lambda: False
    if options.system_prompt:
        extra = settings.append_system_prompt
        blocks = [{"type": "text", "text": options.system_prompt}]
        if extra:
            blocks.append({"type": "text", "text": extra})
        agent.system_blocks = lambda: [dict(b) for b in blocks]
    _resume(agent, options)
    return agent


# ─── running ───────────────────────────────────────────────────────────────

@dataclass
class Result:
    text: str
    is_error: bool
    stop_reason: str
    session_id: str
    cost_usd: float
    session_cost_usd: float
    usage: dict[str, int]
    duration_s: float
    iterations: int
    tool_calls: int
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_event(cls, ev: dict[str, Any], events: list[dict[str, Any]] | None = None) -> Result:
        return cls(text=ev.get("result", ""), is_error=bool(ev.get("is_error")), stop_reason=ev.get("stop_reason", ""),
                   session_id=ev.get("session_id", ""), cost_usd=float(ev.get("cost_usd", 0.0)),
                   session_cost_usd=float(ev.get("session_cost_usd", 0.0)), usage=dict(ev.get("usage") or {}),
                   duration_s=float(ev.get("duration_s", 0.0)), iterations=int(ev.get("iterations", 0)),
                   tool_calls=int(ev.get("tool_calls", 0)), error=ev.get("error"), events=list(events or []))

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("events", None)
        return d


_DONE = object()


class AxonSession:
    """A multi-turn conversation: each :meth:`query` continues the same session."""

    def __init__(self, options: AxonOptions | None = None, agent: Any = None) -> None:
        self.options = options or AxonOptions()
        self._queue: queue.Queue[Any] | None = None
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._turn_cost = 0.0
        self._started = False
        self.agent = agent if agent is not None else build_agent(self.options, on_event=self._on_event)
        if agent is not None:
            self.agent.on_event = self._on_event

    # Called from the worker thread and from tool threads.
    def _on_event(self, ev: Any) -> None:
        if self._cancel.is_set() and threading.current_thread() is self._worker:
            raise KeyboardInterrupt  # handled by the agent loop as a user interrupt
        q = self._queue
        if q is None:
            return
        try:
            d = event_to_dict(ev)
        except Exception:
            d = None
        if d is None:
            return
        if d["type"] in ("llm_call_end", "compaction"):
            try:
                self._turn_cost += float(d.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
        q.put(d)

    @property
    def session_id(self) -> str:
        return str(getattr(self.agent.session, "active_session_id", "") or "")

    def init_event(self) -> dict[str, Any]:
        s = self.agent.settings
        try:
            import axon
            version = getattr(axon, "__version__", "")
        except Exception:
            version = ""
        try:
            tools = list(self.agent.registry.names())
        except Exception:
            tools = []
        return {"type": "system", "subtype": "init", "session_id": self.session_id, "model": s.model,
                "provider": getattr(self.agent.provider, "name", ""), "workspace": str(s.workspace),
                "mode": s.mode, "trace_level": getattr(s, "trace_level", ""), "tools": tools,
                "max_turns": s.max_iterations, "axon_version": version}

    def cancel(self) -> None:
        """Interrupt the running turn (takes effect at the next streamed event)."""
        self._cancel.set()

    def _work(self, prompt: str, q: queue.Queue[Any]) -> None:
        t0 = time.time()
        try:
            result = self.agent.run_turn(prompt)
        except KeyboardInterrupt:
            from axon.agent.loop import TurnResult
            from axon.providers.base import Usage
            result = TurnResult(final_text=INTERRUPTED_TEXT, stop_reason="interrupted", iterations=0,
                                tool_calls_count=0, usage=Usage())
        except BaseException as e:  # surfaced as a result event, never lost in the thread
            from axon.agent.loop import TurnResult
            from axon.providers.base import Usage
            result = TurnResult(final_text=f"Axon error: {type(e).__name__}: {e}", stop_reason="error",
                                iterations=0, tool_calls_count=0, usage=Usage(), error=str(e))
        q.put((_DONE, result, time.time() - t0))

    def query(self, prompt: str) -> Iterator[dict[str, Any]]:
        """Run one turn, yielding events as they happen; the last one has ``type == "result"``."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise SDKError("prompt must be a non-empty string")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise SDKError("a turn is already running in this session")
            q: queue.Queue[Any] = queue.Queue()
            self._queue = q
            self._cancel.clear()
            self._turn_cost = 0.0
            if not self._started:
                self._started = True
                try:
                    self.agent.start_session("resume" if (self.options.resume or self.options.continue_session) else "startup")
                except Exception:
                    pass
            self._worker = threading.Thread(target=self._work, args=(prompt, q), name="axon-sdk-turn", daemon=True)
        yield self.init_event()
        self._worker.start()
        finished = False
        try:
            while True:
                item = q.get()
                if isinstance(item, tuple) and item and item[0] is _DONE:
                    _, result, elapsed = item
                    finished = True
                    yield result_to_dict(self.agent, result, elapsed, cost_usd=self._turn_cost)
                    return
                yield item
        finally:
            if not finished:
                self.cancel()  # consumer stopped iterating: stop the turn
            self._queue = None if finished else self._queue

    def run(self, prompt: str) -> Result:
        events = list(self.query(prompt))
        return Result.from_event(events[-1], events)

    def close(self, reason: str = "exit") -> None:
        self.cancel()
        if self._worker is not None:
            self._worker.join(timeout=10)
        if self._started:
            try:
                self.agent.end_session(reason)
            except Exception:
                pass
            self._started = False

    def __enter__(self) -> AxonSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def query(prompt: str, options: AxonOptions | None = None) -> Iterator[dict[str, Any]]:
    """One-shot: build an agent, run ``prompt``, yield JSON-serializable events."""
    session = AxonSession(options)
    try:
        yield from session.query(prompt)
    finally:
        session.close()


def run(prompt: str, options: AxonOptions | None = None) -> Result:
    """One-shot blocking call returning the final :class:`Result` (with all events)."""
    events = list(query(prompt, options))
    return Result.from_event(events[-1], events)


__all__ = ["AxonOptions", "AxonSession", "Result", "SDKError", "build_agent", "build_settings",
           "event_to_dict", "result_to_dict", "usage_to_dict", "query", "run"]
