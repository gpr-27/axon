"""
Glass-box tracing: structured events emitted by the agent loop and the per-turn
timeline (:class:`TurnTrace`) built from them.

Renderers consume these events through ``Agent.on_event`` (unknown event types must
be ignored); ``Agent.traces`` keeps every turn's :class:`TurnTrace`, which is also
appended to ``~/.axon/projects/<project>/traces/<session_id>.jsonl`` for ``/inspect``.
"""
from __future__ import annotations
import json
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

TRACE_LEVELS: tuple[str, ...] = ("compact", "normal", "detailed", "debug")
MAX_PREVIEW_CHARS = 2_000


# ─── Events ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMRequestInfo:
    """What is about to be sent to the provider (for detailed/debug trace levels)."""
    iteration: int
    model: str
    max_tokens: int
    effort: str
    thinking: bool
    message_count: int
    system_tokens: int
    tool_schema_tokens: int
    conversation_tokens: int
    tool_count: int
    cache_markers: int
    provider: str = ""


@dataclass(frozen=True)
class LLMCallEnd:
    iteration: int
    model: str
    usage: Any
    cost: float
    duration_s: float
    stop_reason: str
    started_at: float
    ended_at: float
    tool_use_count: int = 0
    text_chars: int = 0
    outcome: str = "ok"            # ok | interrupted | error
    error: str = ""
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    cache_miss_cause: str = ""
    context_used: int = 0
    context_window: int = 0


@dataclass(frozen=True)
class ToolTiming:
    id: str
    name: str
    started_at: float
    ended_at: float
    duration_s: float
    lines: int
    bytes: int
    est_tokens: int
    is_error: bool
    parallel: bool = False


@dataclass(frozen=True)
class PermissionDecisionEvent:
    id: str
    tool: str
    outcome: str                   # allow | ask | deny
    reason: str
    source: str                    # mode | rule | hook | user | invariant | no-approver | delegation
    approval: str | None = None    # once | always | deny (when the user was asked)


@dataclass(frozen=True)
class HookEvent:
    event: str
    ran: int
    blocked: bool = False
    message: str = ""
    additional_context: str = ""
    permission_decision: str | None = None
    warnings: tuple[str, ...] = ()
    tool_name: str = ""
    tool_use_id: str = ""


@dataclass(frozen=True)
class RetryEvent:
    attempt: int
    max_attempts: int
    delay_s: float
    error: str
    iteration: int = 0


@dataclass(frozen=True)
class CompactionEvent:
    trigger: str                   # auto | manual
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    usage: Any = None
    cost: float = 0.0
    model: str = ""
    blocked_by_hook: bool = False
    summary: str = ""
    preview: bool = False


@dataclass(frozen=True)
class SubagentEvent:
    phase: str                     # start | end
    task_id: str
    agent_type: str
    title: str
    model: str
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "running"
    duration_s: float = 0.0
    iterations: int = 0


@dataclass(frozen=True)
class SubagentStep:
    """One step inside a running subagent (LLM call or tool call), nested under its
    ``subagent`` step in the parent's TurnTrace."""
    task_id: str
    agent_type: str
    kind: str                      # llm | tool
    name: str = ""                 # tool name, or model for llm steps
    summary: str = ""              # tool args / result preview, or token summary
    is_error: bool = False
    duration_s: float = 0.0
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ThinkingBoost:
    """The prompt contained a thinking keyword (ultrathink / think harder / think hard)."""
    keyword: str
    effort: str
    previous_effort: str
    thinking: bool = True


@dataclass(frozen=True)
class SteerEvent:
    """Messages the user typed mid-turn, delivered to the model between tool rounds."""
    count: int
    texts: tuple[str, ...] = ()
    iteration: int = 0


@dataclass(frozen=True)
class GoalCheckEvent:
    """The model ended its turn while a /goal is set: one bounded re-prompt asks it to verify the goal."""
    goal: str
    iteration: int = 0


@dataclass(frozen=True)
class ToolRationale:
    """Why the model chose a tool: its own words just before the tool_use block,
    or a deterministic tag when it gave none (``source="heuristic"``). Never invented."""
    id: str
    name: str
    text: str
    source: str                    # model-text | model-thinking | heuristic


@dataclass(frozen=True)
class CacheStatus:
    iteration: int
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    hit_pct: float
    miss: bool
    cause: str                     # why the prefix cache missed (empty on hit)
    context_tokens: int
    context_window: int
    context_pct: float


@dataclass(frozen=True)
class BudgetEvent:
    scope: str                     # turn | session
    action: str                    # warn | stop | downgrade
    spent_usd: float
    projected_usd: float
    limit_usd: float
    model: str
    new_model: str = ""
    message: str = ""


@dataclass(frozen=True)
class MemoryLoadedEvent:
    path: str
    reason: str                    # startup | on-demand | path-rule
    trigger: str = ""
    tokens: int = 0


TRACE_EVENT_TYPES = (
    LLMRequestInfo, LLMCallEnd, ToolTiming, PermissionDecisionEvent, HookEvent, RetryEvent,
    CompactionEvent, SubagentEvent, ToolRationale, CacheStatus, BudgetEvent, MemoryLoadedEvent,
    SubagentStep, ThinkingBoost, SteerEvent, GoalCheckEvent,
)


def cost_breakdown(model: str, usage: Any) -> dict[str, float]:
    """Per-request USD split into input / cache_write / cache_read / output (via pricing.cost_for rates)."""
    try:
        from axon.providers.pricing import price_for
        q = price_for(model)
        rates = {"input": q.input, "cache_write": q.cache_write, "cache_read": q.cache_read, "output": q.output}
    except Exception:
        return {}
    inp = int(getattr(usage, "input", 0) or 0)
    read = int(getattr(usage, "cache_read", 0) or 0)
    write = int(getattr(usage, "cache_write", 0) or 0)
    out = int(getattr(usage, "output", 0) or 0)
    parts = {"input": max(0, inp - read - write), "cache_write": write, "cache_read": read, "output": out}
    return {k: float(parts[k] * float(rates[k]) / 1_000_000) for k in parts}


def _canon_hash(obj: Any) -> str:
    """Stable hash of a JSON-like structure ignoring ``cache_control`` markers."""
    import hashlib

    h = hashlib.sha1()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            h.update(b"{")
            for k in sorted(o):
                if k == "cache_control":
                    continue
                h.update(str(k).encode("utf-8", "replace"))
                walk(o[k])
            h.update(b"}")
        elif isinstance(o, (list, tuple)):
            h.update(b"[")
            for v in o:
                walk(v)
            h.update(b"]")
        else:
            h.update(repr(o).encode("utf-8", "replace"))

    walk(obj)
    return h.hexdigest()


class CacheTracker:
    """Detects why a prompt-cache read missed by diffing request fingerprints."""

    TTL_S = 300.0

    def __init__(self) -> None:
        self.prev: dict[str, Any] | None = None
        self.ever_hit = False
        self.misses: list[dict[str, Any]] = []

    def fingerprint(self, model: str, effort: str, system: list[dict[str, Any]], tools: list[dict[str, Any]],
                    messages: list[dict[str, Any]]) -> dict[str, Any]:
        mem = [b for b in system if isinstance(b, dict) and str(b.get("text", "")).startswith("## Memory")]
        core = [b for b in system if b not in mem]
        return {
            "model": model, "effort": effort,
            "system": _canon_hash(core), "memory": _canon_hash(mem), "tools": _canon_hash(tools),
            "msg_count": len(messages), "messages": _canon_hash(messages), "messages_ref": messages,
            "t": time.time(),
        }

    def miss_cause(self, fp: dict[str, Any]) -> str:
        prev = self.prev
        if prev is None:
            return "first request of the session (cold cache)"
        if fp["model"] != prev["model"]:
            return f"model changed ({prev['model']} -> {fp['model']})"
        if fp["effort"] != prev["effort"]:
            return "reasoning effort changed"
        if fp["tools"] != prev["tools"]:
            return "tool definitions changed"
        if fp["system"] != prev["system"]:
            return "system prompt changed"
        if fp["memory"] != prev["memory"]:
            return "memory / instruction files changed"
        n = int(prev["msg_count"])
        msgs = fp.get("messages_ref") or []
        if len(msgs) < n or _canon_hash(msgs[:n]) != prev["messages"]:
            return "conversation history rewritten (compaction, rewind or trimming)"
        if fp["t"] - prev["t"] > self.TTL_S:
            return f"cache TTL expired ({int(fp['t'] - prev['t'])}s since the previous request)"
        return "prefix below the provider's minimum cacheable size, or provider-side eviction"

    def observe(self, fp: dict[str, Any], usage: Any, provider: str = "") -> tuple[bool, str]:
        """Record a finished request; returns (miss, cause)."""
        inp = int(getattr(usage, "input", 0) or 0)
        read = int(getattr(usage, "cache_read", 0) or 0)
        miss, cause = False, ""
        if read > 0:
            self.ever_hit = True
        elif inp > 0:
            miss = True
            if not self.ever_hit and provider != "anthropic" and self.prev is not None:
                cause = "provider reported no cache usage (caching unsupported or not reported)"
            else:
                cause = self.miss_cause(fp)
            self.misses.append({"t": fp["t"], "cause": cause, "input_tokens": inp})
        stored = dict(fp)
        stored.pop("messages_ref", None)
        self.prev = stored
        return miss, cause


# ─── Timeline ──────────────────────────────────────────────────────────────

def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _preview(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit:,} more chars]"


@dataclass
class Step:
    index: int
    kind: str                      # llm | tool | permission | hook | retry | compaction | subagent
    started_at: float
    ended_at: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.ended_at or self.started_at) - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "kind": self.kind, "started_at": self.started_at,
                "ended_at": self.ended_at, "data": _jsonable(self.data)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Step:
        return cls(index=int(d.get("index", 0)), kind=str(d.get("kind", "")),
                   started_at=float(d.get("started_at", 0.0)), ended_at=float(d.get("ended_at", 0.0)),
                   data=dict(d.get("data") or {}))


@dataclass
class TurnTrace:
    turn: int
    prompt: str
    model: str
    mode: str
    agent_name: str = "main"
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    steps: list[Step] = field(default_factory=list)
    final_text: str = ""
    stop_reason: str = ""
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._open_llm: Step | None = None
        self._open_tools: dict[str, Step] = {}
        self._pending_why: dict[str, dict[str, str]] = {}

    # recording -------------------------------------------------------
    def _add(self, kind: str, data: dict[str, Any], started_at: float | None = None) -> Step:
        step = Step(index=len(self.steps) + 1, kind=kind, started_at=started_at or time.time(), data=data)
        self.steps.append(step)
        return step

    def record(self, event: Any) -> None:
        from axon.providers.base import LLMCallStart, ThinkingDelta, ToolExecutionResult, ToolExecutionStart
        with self._lock:
            now = time.time()
            if isinstance(event, LLMCallStart):
                self._open_llm = self._add("llm", {"iteration": event.iteration, "model": event.model,
                                                   "tokens_in_est": event.tokens_in, "message_count": event.message_count,
                                                   "fed_back": _jsonable(event.tools_ingested)})
            elif isinstance(event, LLMRequestInfo) and self._open_llm is not None:
                self._open_llm.data["request"] = _jsonable(event)
            elif isinstance(event, ThinkingDelta) and self._open_llm is not None:
                cur = self._open_llm.data.get("thinking", "")
                if len(cur) < MAX_PREVIEW_CHARS:
                    self._open_llm.data["thinking"] = (cur + event.text)[:MAX_PREVIEW_CHARS]
            elif isinstance(event, LLMCallEnd):
                step = self._open_llm or self._add("llm", {"iteration": event.iteration, "model": event.model}, event.started_at)
                step.started_at = event.started_at
                step.ended_at = event.ended_at
                u = event.usage
                step.data.update({
                    "stop_reason": event.stop_reason, "cost": event.cost, "duration_s": event.duration_s,
                    "cost_breakdown": dict(event.cost_breakdown or {}),
                    "cache_miss_cause": event.cache_miss_cause,
                    "context_used": event.context_used, "context_window": event.context_window,
                    "outcome": event.outcome, "error": event.error, "tool_use_count": event.tool_use_count,
                    "usage": _jsonable(u),
                })
                self.llm_calls += 1
                self.input_tokens += int(getattr(u, "input", 0) or 0)
                self.output_tokens += int(getattr(u, "output", 0) or 0)
                self.cache_read_tokens += int(getattr(u, "cache_read", 0) or 0)
                self.cache_write_tokens += int(getattr(u, "cache_write", 0) or 0)
                self.cost += float(event.cost or 0.0)
                self._open_llm = None
            elif isinstance(event, ToolExecutionStart):
                step = self._add("tool", {"id": event.id, "name": event.name, "input": _jsonable(event.input)})
                if event.id in self._pending_why:
                    step.data["why"] = self._pending_why.pop(event.id)
                self._open_tools[event.id] = step
            elif isinstance(event, PermissionDecisionEvent):
                step = self._open_tools.get(event.id)
                if step is not None:
                    step.data["permission"] = _jsonable(event)
                else:
                    self._add("permission", _jsonable(event))
            elif isinstance(event, HookEvent):
                step = self._open_tools.get(event.tool_use_id) if event.tool_use_id else None
                if step is not None:
                    step.data.setdefault("hooks", []).append(_jsonable(event))
                else:
                    self._add("hook", _jsonable(event))
            elif isinstance(event, ToolExecutionResult):
                step = self._open_tools.get(event.id) or self._add("tool", {"id": event.id, "name": event.name, "input": _jsonable(event.input)})
                step.ended_at = now
                step.data.update({"is_error": event.is_error, "result_preview": _preview(event.content)})
                self.tool_calls += 1
            elif isinstance(event, ToolTiming):
                step = self._open_tools.pop(event.id, None)
                if step is not None:
                    step.started_at, step.ended_at = event.started_at, event.ended_at
                    step.data.update({"duration_s": event.duration_s, "lines": event.lines, "bytes": event.bytes,
                                      "est_tokens": event.est_tokens, "parallel": event.parallel})
            elif isinstance(event, RetryEvent):
                self._add("retry", _jsonable(event))
            elif isinstance(event, CompactionEvent):
                self._add("compaction", _jsonable(event))
                self.cost += float(event.cost or 0.0)
            elif isinstance(event, SubagentEvent):
                if event.phase == "end":
                    start = self._subagent_step(event.task_id)
                    if start is not None:
                        start.ended_at = now
                        start.data.update({k: v for k, v in _jsonable(event).items() if k not in ("task_id", "title")})
                        return
                step = self._add("subagent", _jsonable(event))
                step.data.setdefault("steps", [])
            elif isinstance(event, SubagentStep):
                parent = self._subagent_step(event.task_id)
                if parent is None:
                    parent = self._add("subagent", {"task_id": event.task_id, "agent_type": event.agent_type,
                                                    "phase": "start", "steps": []})
                sub = parent.data.setdefault("steps", [])
                if len(sub) < 500:
                    sub.append({**_jsonable(event), "t": now})
            elif isinstance(event, ThinkingBoost):
                self._add("thinking_boost", _jsonable(event))
            elif isinstance(event, SteerEvent):
                self._add("steer", _jsonable(event))
            elif isinstance(event, GoalCheckEvent):
                self._add("goal_check", _jsonable(event))
            elif isinstance(event, ToolRationale):
                step = self._open_tools.get(event.id)
                if step is not None:
                    step.data["why"] = {"text": event.text, "source": event.source}
                else:
                    self._pending_why[event.id] = {"text": event.text, "source": event.source}
            elif isinstance(event, CacheStatus):
                target = next((st for st in reversed(self.steps) if st.kind == "llm"), None)
                if target is not None:
                    target.data["cache"] = _jsonable(event)
            elif isinstance(event, BudgetEvent):
                self._add("budget", _jsonable(event))
            elif isinstance(event, MemoryLoadedEvent):
                self._add("memory", _jsonable(event))

    def _subagent_step(self, task_id: str) -> Step | None:
        for st in reversed(self.steps):
            if st.kind == "subagent" and st.data.get("task_id") == task_id:
                return st
        return None

    def finish(self, final_text: str, stop_reason: str) -> None:
        with self._lock:
            self.ended_at = time.time()
            self.final_text = _preview(final_text, 20_000)
            self.stop_reason = stop_reason
            for step in self.steps:
                if not step.ended_at:
                    step.ended_at = self.ended_at

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.ended_at or time.time()) - self.started_at)

    # persistence -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "turn": self.turn, "prompt": _preview(self.prompt, 4_000), "model": self.model, "mode": self.mode,
                "agent_name": self.agent_name, "started_at": self.started_at, "ended_at": self.ended_at,
                "final_text": self.final_text, "stop_reason": self.stop_reason, "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "cost": self.cost, "steps": [s.to_dict() for s in self.steps],
            }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnTrace:
        t = cls(turn=int(d.get("turn", 0)), prompt=str(d.get("prompt", "")), model=str(d.get("model", "")),
                mode=str(d.get("mode", "")), agent_name=str(d.get("agent_name", "main")))
        for k in ("started_at", "ended_at", "cost"):
            setattr(t, k, float(d.get(k, 0.0) or 0.0))
        for k in ("llm_calls", "tool_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            setattr(t, k, int(d.get(k, 0) or 0))
        t.final_text = str(d.get("final_text", ""))
        t.stop_reason = str(d.get("stop_reason", ""))
        t.steps = [Step.from_dict(s) for s in d.get("steps", []) if isinstance(s, dict)]
        return t


def trace_file(workspace: Path, session_id: str) -> Path:
    """``~/.axon/projects/<project>/traces/<session_id>.jsonl``."""
    from axon import storage
    return storage.traces_dir(Path(workspace)) / f"{session_id}.jsonl"


def save_trace(workspace: Path | None, session_id: str, trace: TurnTrace) -> None:
    if workspace is None or not session_id:
        return
    try:
        from axon import storage
        storage.append_jsonl(trace_file(workspace, session_id), trace.to_dict())
    except Exception:
        pass


def load_traces(workspace: Path, session_id: str, legacy_session_dir: Path | None = None) -> list[TurnTrace]:
    """Saved traces of a session (also reads the legacy ``<session_dir>/<id>.traces.jsonl``)."""
    out: list[TurnTrace] = []
    paths: list[Path] = []
    try:
        paths.append(trace_file(workspace, session_id))
    except Exception:
        pass
    if legacy_session_dir is not None:
        paths.append(Path(legacy_session_dir) / f"{session_id}.traces.jsonl")
    for path in paths:
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(TurnTrace.from_dict(json.loads(line)))
                    except Exception:
                        continue
        except OSError:
            continue
        if out:
            break
    return out


def summarize(trace: TurnTrace) -> str:
    """Plain-language summary of a turn (no LLM needed)."""
    tools = [s for s in trace.steps if s.kind == "tool"]
    errors = [s for s in tools if s.data.get("is_error")]
    names: dict[str, int] = {}
    for s in tools:
        names[str(s.data.get("name"))] = names.get(str(s.data.get("name")), 0) + 1
    tool_txt = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in names.items()) or "no tools"
    parts = [
        f"Turn {trace.turn}: {trace.llm_calls} LLM call(s), {len(tools)} tool call(s) ({tool_txt}).",
        f"Tokens: {trace.input_tokens:,} in ({trace.cache_read_tokens:,} cached) / {trace.output_tokens:,} out; "
        f"cost ${trace.cost:.4f}; {trace.duration_s:.1f}s; stop: {trace.stop_reason or 'n/a'}.",
    ]
    if errors:
        parts.append(f"{len(errors)} tool call(s) returned errors: " + ", ".join(str(s.data.get("name")) for s in errors) + ".")
    for kind, label in (("retry", "provider retries"), ("compaction", "context compactions"), ("subagent", "subagent events")):
        n = sum(1 for s in trace.steps if s.kind == kind)
        if n:
            parts.append(f"{n} {label}.")
    return " ".join(parts)
