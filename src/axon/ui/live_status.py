"""
Live status: "what is happening right now" and a state-based tip, shown under the spinner while a turn runs.

Everything comes from data Axon already has (the events the renderer receives, plus a few "waiting" marks
placed around hooks, MCP calls and compaction). No model call, no extra request, nothing invented: when the
state is not known the line only says which step the turn is on and for how long.

    waiting for claude-sonnet-4-5 via AgentRouter · 12s · no reply yet, the gateway may be buffering the answer; Esc stops it
    running Bash: Run the unit tests · 4s · step 3
    retrying after HTTP 429 (rate limited) in 3s · retry 2 of 5

Layout of this module: waiting marks (``waiting``), the snapshot (``View``), the pure text functions
(``now_candidates``), and ``LiveStatus``, which observes events, owns the tip picker and serves ``rows()``.
The spinner in ``render.py`` only asks for ``rows()`` about every half second and draws them.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from axon.ui import live_tips
from axon.ui.live_tips import TipFacts
from axon.ui.theme import display_width, glyph, sanitize, truncate
from axon.waitmarks import Wait, active_waits, begin_wait, end_wait, waiting  # noqa: F401 (re-exported)

NO_REPLY_S = 5.0            # no first byte after this long: say the gateway may be buffering
REFRESH_S = 0.5             # the spinner asks for new rows at most this often


# Hooks, MCP calls and compaction send no event while they run; their code marks the wait with
# axon.waitmarks.waiting(...), which is re-exported here.

# ─── snapshot ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolView:
    id: str
    name: str
    t0: float
    what: str = ""           # one line on what the call does (agent/decision.py wording)
    why: str = ""            # the model's own words before the call, if it gave any


@dataclass(frozen=True)
class SubView:
    task_id: str
    label: str               # "#2 explorer"
    t0: float
    steps: int = 0
    max_steps: int = 0
    activity: str = ""       # the subagent's own one-line state ("Read src/x.py", "Step 2 · waiting for model")


@dataclass(frozen=True)
class RetryView:
    until: float
    attempt: int
    max_attempts: int
    reason: str


@dataclass(frozen=True)
class View:
    """An immutable copy of the live state, so the text functions need no lock."""
    turn_t0: float = 0.0
    step: int = 0
    model: str = ""
    provider: str = ""
    phase: str = ""          # "" | waiting | thinking | writing | writing_call | tools | between
    phase_t0: float = 0.0
    out_chars: int = 0       # characters the model has written in this step (tokens are about chars / 4)
    writing_tool: str = ""
    tools: tuple[ToolView, ...] = ()
    subagents: tuple[SubView, ...] = ()
    retry: RetryView | None = None
    waits: tuple[Wait, ...] = ()
    context_pct: float = 0.0

    @property
    def tokens(self) -> int:
        return self.out_chars // 4

    @property
    def compacting(self) -> bool:
        return any(w.kind == "compact" for w in self.waits)


@dataclass(frozen=True)
class Rows:
    now: str = ""
    tip: str = ""


# ─── pure text ──────────────────────────────────────────────────────────────

def _dot() -> str:
    return glyph("dot")


def _join(*parts: str) -> str:
    return f" {_dot()} ".join(p for p in parts if p)


def secs(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def _clip(text: Any, limit: int = 60) -> str:
    s = " ".join(sanitize(str(text or "")).split())
    return s if len(s) <= limit else s[: max(1, limit - 1)] + glyph("ellipsis")


def _who(v: View) -> str:
    if v.model and v.provider:
        return f"{_clip(v.model, 40)} via {_clip(v.provider, 24)}"
    return _clip(v.model, 40) or "the model"


def _model_name(v: View) -> str:
    return _clip(v.model, 40) or "the model"


def _tokens_text(tokens: int) -> str:
    if tokens <= 0:
        return ""
    return f"~{tokens / 1000:.1f}k tokens so far" if tokens >= 1000 else f"~{tokens} tokens so far"


def _http_reason(error: str) -> str:
    m = re.search(r"HTTP (\d{3})", error or "")
    if not m:
        return ""
    code = int(m.group(1))
    words = {429: "rate limited", 408: "request timed out", 529: "overloaded"}.get(
        code, "server error" if 500 <= code < 600 else "")
    return f"HTTP {code} ({words})" if words else f"HTTP {code}"


def _user_lines(w: Wait, now: float) -> list[str]:
    what = _clip(w.info.get("what") or "your answer", 40)
    return [_join(f"waiting for you: {what}", secs(now - w.t0)), _join("waiting for you", secs(now - w.t0))]


def _retry_lines(v: View, now: float) -> list[str]:
    r = v.retry
    assert r is not None
    reason = _http_reason(r.reason) or _clip(r.reason, 50) or "a provider error"
    count = f"retry {r.attempt} of {r.max_attempts}"
    if "failover" in (r.reason or "").lower():
        return [_join("switching to the fallback model", _clip(r.reason, 80)), "switching to the fallback model"]
    left = r.until - now
    if left <= -1:         # the pause is over: the new request is out and nothing has come back yet
        return [_join(f"retry sent, waiting for {_who(v)}", secs(now - r.until), count),
                _join("retry sent, waiting", secs(now - r.until), count)]
    when = f"in {secs(math.ceil(left))}" if left > 0 else "now"
    return [_join(f"retrying after {reason} {when}", count), _join(f"retrying {when}", count), f"retrying {when}"]


def _compact_lines(w: Wait, v: View, now: float) -> list[str]:
    pct = v.context_pct
    why = f"context is at {pct:.0f}%" if pct else ""
    model = _clip(w.info.get("model") or "", 40)
    base = "compacting the conversation"
    doing = f"summarising older turns with {model}" if model else "summarising older turns"
    return [_join(f"{base} ({why})" if why else base, doing, secs(now - w.t0)),
            _join(base, secs(now - w.t0)), "compacting"]


def _hook_lines(w: Wait, now: float) -> list[str]:
    event = _clip(w.info.get("event") or "", 24)
    name = f"a {event} hook" if event else "a hook"
    return [_join(f"waiting for {name} to finish", secs(now - w.t0)), _join("waiting for a hook", secs(now - w.t0))]


def _mcp_lines(w: Wait, now: float) -> list[str]:
    server = _clip(w.info.get("server") or "an MCP server", 30)
    tool = _clip(w.info.get("tool") or "", 30)
    if w.kind == "mcp_connect":
        return [_join(f"waiting for MCP server {server} to connect", secs(now - w.t0)),
                _join("waiting for an MCP server to connect", secs(now - w.t0))]
    what = f"waiting for MCP server {server} to reply" + (f" ({tool})" if tool else "")
    return [_join(what, secs(now - w.t0)), _join("waiting for an MCP server", secs(now - w.t0))]


def _title_lines(w: Wait, now: float) -> list[str]:
    return [_join("naming this session", secs(now - w.t0))]


def _wait_lines(v: View, now: float) -> list[str]:
    """Lines for the most important active wait (the user first, then compaction, hooks, MCP, titling)."""
    order = ["user", "compact", "hook", "mcp_connect", "mcp_call", "title"]
    for kind in order:
        for w in reversed(v.waits):
            if w.kind != kind:
                continue
            if kind == "user":
                return _user_lines(w, now)
            if kind == "compact":
                return _compact_lines(w, v, now)
            if kind == "hook":
                return _hook_lines(w, now)
            if kind == "title":
                return _title_lines(w, now)
            return _mcp_lines(w, now)
    return []


def _subagent_lines(v: View, now: float) -> list[str]:
    sub = max(v.subagents, key=lambda s: s.t0)
    step = (f"step {sub.steps} of {sub.max_steps}" if sub.max_steps else f"step {sub.steps}") if sub.steps else "starting"
    doing = _clip(sub.activity, 40)
    more = f"{len(v.subagents) - 1} more running" if len(v.subagents) > 1 else ""
    head = f"subagent {_clip(sub.label, 30)}"
    return [_join(f"{head}: {step}", doing, more, secs(now - sub.t0)), _join(f"{head}: {step}", secs(now - sub.t0)),
            _join("a subagent is running", secs(now - sub.t0))]


def _tool_lines(v: View, now: float) -> list[str]:
    tail = _join(secs(now - min(t.t0 for t in v.tools)), f"step {v.step}" if v.step else "")
    if len(v.tools) > 1:
        names = ", ".join(dict.fromkeys(_clip(t.name, 20) for t in v.tools))
        return [_join(f"running {len(v.tools)} tools at once: {names}", tail),
                _join(f"running {len(v.tools)} tools at once", tail)]
    tool = v.tools[0]
    name = _clip(tool.name, 30)
    what = _clip(tool.what, 70)
    tail = _join(secs(now - tool.t0), f"step {v.step}" if v.step else "")
    return [_join(f"running {name}: {what}" if what else f"running {name}", tail),
            _join(f"running {name}", tail)]


def _model_lines(v: View, now: float) -> list[str]:
    waited = secs(now - v.phase_t0)
    if v.phase == "waiting":
        if now - v.phase_t0 >= NO_REPLY_S:
            return [_join(f"waiting for {_who(v)}", waited, "the gateway may be buffering the answer; Esc stops it"),
                    _join(f"waiting for {_who(v)}", waited, "gateway may be buffering; Esc stops it"),
                    _join(f"waiting for {_model_name(v)}", waited, "no reply yet; Esc stops it"),
                    _join("waiting for the model", waited, "Esc stops it")]
        return [_join(f"waiting for {_who(v)}", waited), _join("waiting for the model", waited)]
    tokens = _tokens_text(v.tokens)
    if v.phase == "thinking":
        return [_join(f"{_model_name(v)} is thinking", tokens, waited), _join("thinking", tokens)]
    if v.phase == "writing":
        return [_join(f"{_model_name(v)} is writing the answer", tokens, waited), _join("writing the answer", tokens)]
    if v.phase == "writing_call":
        call = _clip(v.writing_tool, 30) or "tool"
        return [_join(f"{_model_name(v)} is writing a {call} call", tokens, waited), _join(f"writing a {call} call", tokens)]
    return []


def _unknown_line(v: View, now: float) -> list[str]:
    age = secs(now - v.turn_t0) if v.turn_t0 else ""
    return [_join(f"step {v.step}" if v.step else "working", age)]


def now_candidates(v: View, now: float) -> list[str]:
    """The "what is happening now" line, longest wording first. Never empty."""
    if v.retry is not None:
        return _retry_lines(v, now)
    for lines in (_wait_lines(v, now), _subagent_lines(v, now) if v.subagents else [],
                  _tool_lines(v, now) if v.tools else [], _model_lines(v, now)):
        if lines:
            return lines
    return _unknown_line(v, now)


def fit(candidates: list[str], width: int) -> str:
    """The first wording that fits *width* columns; the shortest one, cut with an ellipsis, if none does."""
    for text in candidates:
        if display_width(text) <= width:
            return text
    return truncate(candidates[-1], max(8, width)) if candidates else ""


# ─── tool wording ───────────────────────────────────────────────────────────

def tool_phrase(name: str, inp: dict[str, Any], registry: Any = None) -> str:
    """One line on what a call does, from agent/decision.py ('Run the unit tests', 'read src/a.py', ...)."""
    try:
        from axon.agent import decision
        from axon.ui import render
        summary, _ = decision.describe_arguments(name, inp or {})
        if summary:
            return " ".join(summary.split())
        purpose = render.tool_purpose(name, registry)
        target = render.tool_target(name, inp or {})
        return f"{purpose}: {target}" if purpose and target else (purpose or target)
    except Exception:
        return ""


# ─── the live object ────────────────────────────────────────────────────────

class LiveStatus:
    """Observes the renderer's events and answers ``rows()`` for the spinner. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.time, agent_of: Callable[[], Any] | None = None,
                 provider_of: Callable[[str], str] | None = None) -> None:
        self.clock = clock
        self._agent_of = agent_of or (lambda: None)
        self._provider_of = provider_of or (lambda model: "")
        self.waits_of: Callable[[], list[Wait]] = active_waits
        self.subagents_of: Callable[[], list[SubView]] = self._running_subagents
        self.facts_of: Callable[[], TipFacts] = lambda: live_tips.facts_from_agent(self._agent_of())
        self.tips_enabled_of: Callable[[], bool] = self._tips_setting
        self.fallback_tip: Callable[[], str] | None = None
        self._lock = threading.RLock()
        self._picker = live_tips.TipPicker()
        self._facts = TipFacts()
        self._v = View()
        self._cache: tuple[float, int, Rows] | None = None
        self._said: dict[str, str] = {}      # rationale seen before its call started

    # ── switches ──
    def enabled(self) -> bool:
        return bool(getattr(getattr(self._agent_of(), "settings", None), "live_status", True))

    def _tips_setting(self) -> bool:
        try:
            from axon.features.tips import tip_engine
            return tip_engine(self._agent_of()).enabled()
        except Exception:
            return False

    # ── events ──
    def observe(self, event: Any, now: float | None = None) -> None:
        """Update the state from one renderer event. Never raises."""
        handler = getattr(self, "_on_" + type(event).__name__, None)
        if handler is None:
            return
        try:
            with self._lock:
                handler(event, self.clock() if now is None else now)
        except Exception:
            pass

    def _set(self, **changes: Any) -> None:
        self._v = replace(self._v, **changes)
        self._cache = None

    def _begin_turn(self, now: float) -> None:
        self._v = View(turn_t0=now)
        self._said.clear()
        self._picker.new_turn()
        self._picker.fallback = self.fallback_tip
        self._facts = self.facts_of()

    def _on_LLMCallStart(self, e: Any, now: float) -> None:
        if getattr(e, "iteration", 1) <= 1 or not self._v.turn_t0:
            self._begin_turn(now)
        model = str(getattr(e, "model", "") or "")
        self._set(step=int(getattr(e, "iteration", 0) or 0), model=model, provider=self._provider_of(model),
                  phase="waiting", phase_t0=now, out_chars=0, writing_tool="", retry=None)

    def _on_LLMRequestInfo(self, e: Any, now: float) -> None:
        if not self._v.provider and getattr(e, "provider", ""):
            self._set(provider=str(e.provider))

    def _enter(self, phase: str, now: float, add_chars: int = 0, **extra: Any) -> None:
        v = self._v
        self._set(phase=phase, phase_t0=v.phase_t0 if v.phase == phase else now, retry=None,
                  out_chars=v.out_chars + add_chars, **extra)

    def _on_ThinkingDelta(self, e: Any, now: float) -> None:
        self._enter("thinking", now, len(getattr(e, "text", "") or ""))

    def _on_TextDelta(self, e: Any, now: float) -> None:
        self._enter("writing", now, len(getattr(e, "text", "") or ""))

    def _on_ToolUseStart(self, e: Any, now: float) -> None:
        self._enter("writing_call", now, writing_tool=str(getattr(e, "name", "") or ""))

    def _on_ToolArgsDelta(self, e: Any, now: float) -> None:
        self._set(out_chars=self._v.out_chars + len(getattr(e, "fragment", "") or ""))

    def _on_LLMCallEnd(self, e: Any, now: float) -> None:
        used, window = int(getattr(e, "context_used", 0) or 0), int(getattr(e, "context_window", 0) or 0)
        pct = 100.0 * used / window if used and window else self._v.context_pct
        phase = "between" if self._v.phase != "tools" else "tools"
        self._set(context_pct=pct, phase=phase, phase_t0=now, retry=None)

    def _on_CacheStatus(self, e: Any, now: float) -> None:
        pct = float(getattr(e, "context_pct", 0.0) or 0.0)
        if pct:
            self._set(context_pct=pct)

    def _on_ToolBatchStart(self, e: Any, now: float) -> None:
        self._enter("tools", now)

    def _on_ToolExecutionStart(self, e: Any, now: float) -> None:
        inp = dict(getattr(e, "input", None) or {})
        name = str(getattr(e, "name", "") or "")
        said = str(getattr(e, "rationale", "") or "") if str(getattr(e, "rationale_source", "")) in ("text", "model-text") else ""
        tool = ToolView(id=str(e.id), name=name, t0=now, what=tool_phrase(name, inp, self._registry()),
                        why=" ".join(said.split()) or self._said.pop(str(e.id), ""))
        self._set(tools=self._v.tools + (tool,), phase="tools", phase_t0=self._v.phase_t0 or now, retry=None)

    def _on_ToolRationale(self, e: Any, now: float) -> None:
        """The model's own words before a call (a heuristic note is not what the model said, so it is ignored)."""
        if str(getattr(e, "source", "")) not in ("text", "model-text"):
            return
        call_id, text = str(getattr(e, "id", "")), " ".join(str(getattr(e, "text", "") or "").split())
        if any(t.id == call_id for t in self._v.tools):
            self._set(tools=tuple(replace(t, why=text) if t.id == call_id else t for t in self._v.tools))
        else:
            self._said[call_id] = text

    def _on_ToolExecutionResult(self, e: Any, now: float) -> None:
        left = tuple(t for t in self._v.tools if t.id != getattr(e, "id", None))
        self._set(tools=left, **({} if left else {"phase": "between", "phase_t0": now}))

    def _on_TurnComplete(self, e: Any, now: float) -> None:
        if str(getattr(e, "stop_reason", "")) != "tool_use":
            self._set(phase="", tools=(), retry=None)

    def _on_RetryEvent(self, e: Any, now: float) -> None:
        delay = float(getattr(e, "delay_s", 0.0) or 0.0)
        retry = RetryView(until=now + delay, attempt=int(getattr(e, "attempt", 0) or 0),
                          max_attempts=int(getattr(e, "max_attempts", 0) or 0), reason=str(getattr(e, "error", "") or ""))
        self._set(retry=retry, phase="waiting", phase_t0=now, out_chars=0)

    _on_ProviderRetry = _on_RetryEvent

    def _registry(self) -> Any:
        return getattr(self._agent_of(), "registry", None)

    # ── reading ──
    def view(self) -> View:
        with self._lock:
            return self._v

    def current_why(self, tool_name: str) -> str:
        """The model's own words before the running call of *tool_name* ("" when it gave none)."""
        with self._lock:
            for t in reversed(self._v.tools):
                if t.name == tool_name and t.why:
                    return t.why
        return ""

    def end_turn(self) -> None:
        with self._lock:
            self._v = View()
            self._cache = None
            self._picker.new_turn()

    def rows(self, width: int, show_tip: bool = True, now: float | None = None) -> Rows:
        """The two rows for the spinner, recomputed at most every ``REFRESH_S`` seconds."""
        now = self.clock() if now is None else now
        with self._lock:
            cached = self._cache
            if cached is not None and 0 <= now - cached[0] < REFRESH_S and cached[1] == width:
                return cached[2]
            v = self._v
            if not v.turn_t0:
                return Rows()
            v = self._with_live_marks(v)
            text = fit(now_candidates(v, now), width)
            tip = self._tip(v, now) if show_tip else ""
            rows = Rows(text, tip)
            self._cache = (now, width, rows)
            return rows

    def _with_live_marks(self, v: View) -> View:
        """Add what lives outside the events: the waiting marks and the running subagents."""
        try:
            return replace(v, waits=tuple(self.waits_of()), subagents=tuple(self.subagents_of()))
        except Exception:
            return v

    def _running_subagents(self) -> list[SubView]:
        manager = getattr(self._agent_of(), "subagents", None)
        tasks = manager.all_tasks() if manager is not None else []
        return [SubView(task_id=str(t.id), label=f"#{t.index} {_clip(t.agent_type, 20)}", t0=float(t.start_time or 0.0),
                        steps=int(t.steps or 0), max_steps=int(t.max_steps or 0), activity=str(t.activity or ""))
                for t in tasks if getattr(t, "status", "") == "running"]

    def _tip(self, v: View, now: float) -> str:
        try:
            if not self.tips_enabled_of():
                return ""
            return self._picker.pick(v, self._facts, now)
        except Exception:
            return ""


# ─── the renderer's instance, for prompts that need the model's own words ────

_CURRENT: LiveStatus | None = None


def set_current(live: LiveStatus | None) -> None:
    global _CURRENT
    _CURRENT = live


def current_why(tool_name: str) -> str:
    """What the model said just before the running *tool_name* call ("" when unknown)."""
    live = _CURRENT
    try:
        return live.current_why(tool_name) if live is not None else ""
    except Exception:
        return ""


def explainers_enabled() -> bool:
    """False when the user switched live status off (settings key liveStatus)."""
    live = _CURRENT
    try:
        return live.enabled() if live is not None else True
    except Exception:
        return True
