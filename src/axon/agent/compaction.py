"""Compaction beyond the one blocking client-side summary.

* **Background compaction** (every provider): when the prompt nears the auto-compact point, the older turns are
  summarized on a worker thread (a fresh provider on the cheap ``small_model``, else the active model) while the
  user and the agent keep working. At the next step boundary the summary is swapped in for exactly the messages it
  covered; every message appended meanwhile stays after it. It never starts or swaps in the middle of a tool
  exchange (the cut is before a user turn and a result is never separated from its call), a stale result is
  discarded, and :meth:`BackgroundCompaction.cancel` drops it.
* **Server-side compaction** (official Anthropic host, supported models, ``compact_server_side``): the API writes the
  summary (``compact_20260112`` edit, ``pause_after_compaction``). The paused reply carries one ``compaction`` block;
  :func:`apply_server_compaction` puts it first, keeps the newest ``compact_keep_turns`` turns word for word after it
  (their thinking blocks removed, as the docs require) and the loop asks again. Anywhere else, and whenever it is
  refused, the client-side path above is what compacts.

Earlier messages are never edited in place (only replaced as a whole prefix), so signed thinking of the kept tail stays
valid wherever the provider can keep it; the Anthropic provider already removes thinking from turns before the
current one.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from axon.agent.state import Conversation, is_tool_result_message

# Models the docs list for compaction at a token threshold (a dated suffix also matches).
SERVER_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-mythos-preview", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5",
    "claude-haiku-4-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)
SERVER_MIN_TRIGGER = 50_000        # the API's lower bound for the trigger
WAIT_FOR_SUMMARY_S = 120.0         # how long a turn that needs the room now waits for a running background summary
SERVER_INSTRUCTIONS = (
    "You are compacting a coding-assistant conversation so work can continue in a fresh context. Write a dense, factual "
    "summary. Preserve: the user's goals and explicit instructions; key decisions; files read or modified (exact paths) "
    "and what changed; commands run and their outcomes; errors and how they were resolved; open questions; and the "
    "precise next steps. Do not invent details. Use markdown headings. Do not call any tools; respond with the summary "
    "text only."
)


def _k(n: int) -> str:
    n = int(n)
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else (f"{round(n / 1000)}k" if n >= 1000 else str(n))


def has_open_tool_call(messages: list[dict[str, Any]]) -> bool:
    """True when the last message is an assistant turn whose tool calls have no results yet (a tool exchange is running)."""
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") != "assistant":
        return False
    content = last.get("content")
    return bool(last.get("tool_calls")) or (isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content))


def strip_thinking_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of *messages* without thinking blocks (an assistant turn left empty is dropped)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") == "assistant" and isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
            if not kept:
                continue
            m = {**m, "content": kept}
        out.append(m)
    return out


# ── server-side compaction ──────────────────────────────────────────────────

def supports_server_compaction(model: str) -> bool:
    m = str(model or "").lower().split("/")[-1]
    if m.endswith("[1m]"):
        m = m[:-4]
    return any(m == p or m.startswith(p + "-") for p in SERVER_MODELS)


def server_active(agent: Any) -> bool:
    """Server-side compaction would be sent with the next call (setting on, official host, supported model, no pins)."""
    s = agent.settings
    if not bool(getattr(s, "compact_server_side", False)):
        return False
    provider = getattr(agent, "provider", None)
    if not getattr(provider, "_official", False) or getattr(provider, "_compact_off", False):
        return False
    wire = getattr(provider, "_wire_model", lambda m: m)(s.model)
    if not supports_server_compaction(wire):
        return False
    return not getattr(getattr(agent, "context", None), "pins", None)   # pinned results must stay verbatim: client-side only


def server_edit(agent: Any) -> dict[str, Any] | None:
    """The ``compact_20260112`` edit for the next call, or None."""
    if not server_active(agent):
        return None
    ctx = agent.context
    text = SERVER_INSTRUCTIONS
    try:
        extra = agent._compact_instructions("")
        if extra:
            text = f"{text}\n\n{extra}"
    except Exception:
        pass
    return {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": max(SERVER_MIN_TRIGGER, int(ctx.threshold(agent.settings.model)))},
            "pause_after_compaction": True, "instructions": text[:8000]}


def find_compaction_block(turn: Any) -> dict[str, Any] | None:
    native = getattr(turn, "native", None)
    blocks = native.get("content") if isinstance(native, dict) else native
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "compaction":
            return b
    return None


def apply_server_compaction(agent: Any, turn: Any, model: str, cost: float = 0.0) -> bool:
    """Handle a reply that stopped with ``compaction``. True when the history now starts with the summary block
    (the caller asks again); False when the API produced no summary (nothing changed; server compaction is turned off
    for the session so the client-side path takes over)."""
    from axon.agent.trace import CompactionEvent
    conv = agent.conversation
    block = find_compaction_block(turn)
    summary = str((block or {}).get("content") or "").strip()
    if not summary:
        provider = getattr(agent, "provider", None)
        if provider is not None:
            provider._compact_off = True
        agent.journal("compact", {"trigger": "server", "changed": False, "note": "the API paused without a summary"})
        return False
    before = len(conv.messages)
    tokens_before = conv.token_estimate()
    ctx = agent.context
    cut = ctx.split_point(conv)
    if cut <= 0:
        starts = [i for i, m in enumerate(conv.messages) if not is_tool_result_message(m) and m.get("role") == "user"]
        cut = starts[-1] if starts else 0
    tail = strip_thinking_blocks(conv.messages[cut:])
    conv.messages = [{"role": "assistant", "content": [dict(block)]}, *tail]
    tokens_after = conv.token_estimate()
    usage = getattr(turn, "usage", None)
    agent._emit(CompactionEvent(trigger="server", messages_before=before, messages_after=len(conv.messages), tokens_before=tokens_before,
                                tokens_after=tokens_after, usage=None, cost=float(cost or 0.0), model=model, summary=summary[:4000]))
    agent.journal("compact", {"trigger": "server", "messages_before": before, "messages_after": len(conv.messages),
                              "tokens_before": tokens_before, "changed": True, "cost_usd": round(float(cost or 0), 6)})
    record: dict[str, Any] = {"messages": conv.messages, "trigger": "server"}
    if usage is not None and hasattr(usage, "input"):
        record["model"] = model
        record["usage"] = {k: int(getattr(usage, k, 0) or 0) for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    try:
        agent.session.append("compaction", record)
    except Exception:
        pass
    agent._git_snapshot = None
    agent._repo_map = None
    try:
        agent.reload_memory()
        agent._post_compact_context = agent._reread_recent_files()
    except Exception:
        pass
    return True


# ── background compaction ───────────────────────────────────────────────────

Summarizer = Callable[[Conversation, str], "tuple[str | None, Any, str]"]


class BackgroundCompaction:
    """One background summary at a time for an agent. All state changes the conversation only from :meth:`step`
    (the agent's own thread, at a step boundary)."""

    def __init__(self, agent: Any, summarize: Summarizer | None = None) -> None:
        self.agent = agent
        self.summarize: Summarizer = summarize or self._summarize
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._provider: Any = None
        self._gen = 0                      # bumped by cancel(): a result of an older generation is dropped
        self._old: list[dict[str, Any]] = []   # the messages the running summary covers (identity checked at the swap)
        self._cut = 0
        self._result: tuple[str | None, Any, str] | None = None
        self._retry_after = 0              # do not start again before the conversation reaches this many messages

    # -- state --
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._result is not None

    @property
    def active(self) -> bool:
        return self.running or self.ready

    def enabled(self) -> bool:
        s = self.agent.settings
        return bool(getattr(s, "compact_background", True)) and not getattr(self.agent, "is_subagent", False) and not server_active(self.agent)

    # -- the worker --
    def _summarize(self, snapshot: Conversation, instructions: str) -> tuple[str | None, Any, str]:
        from axon.agent.loop import make_provider
        s = self.agent.settings
        models = [s.small_model or s.model]
        if models[0] != s.model:
            models.append(s.model)           # the cheap model failed (e.g. it belongs to a provider the session left): the active one writes it
        summary: str | None = None
        usage: Any = None
        model = models[0]
        for model in models:
            try:
                provider = make_provider(model, s, role="compact")     # a fresh instance: never waits for the main stream
            except Exception:
                continue
            self._provider = provider
            summary, usage = self.agent.context.generate_summary(snapshot, provider, model, instructions=instructions, quiet=True)
            if summary:
                break
        self._provider = None
        return summary, usage, model

    def _work(self, gen: int, snapshot: Conversation, instructions: str) -> None:
        try:
            result = self.summarize(snapshot, instructions)
        except Exception:
            result = (None, None, "")
        with self._lock:
            if gen == self._gen:
                self._result = result

    # -- the loop's entry point --
    def step(self, system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> bool:
        """Called at the start of each model call, between tool exchanges. Swaps a finished summary in, waits for a running
        one when the prompt is already over the auto-compact point, and starts one when it nears it.
        True when the conversation was compacted (the caller rebuilds the system blocks)."""
        agent = self.agent
        conv = agent.conversation
        if self.ready:
            return self._swap()
        if self.running:
            if conv.messages and agent.context.needs_compaction(conv, system, tools, model=agent.settings.model):
                self._thread.join(WAIT_FOR_SUMMARY_S)     # type: ignore[union-attr]
                return self._swap() if self.ready else False
            return False
        if not self.enabled() or len(conv.messages) <= 4 or len(conv.messages) < self._retry_after:
            return False
        if has_open_tool_call(conv.messages):
            return False
        ctx = agent.context
        used = ctx.projected_tokens(conv, system, tools)
        at = float(getattr(agent.settings, "compact_background_at", 0.8) or 0.8)
        if used < ctx.threshold(agent.settings.model) * max(0.1, min(at, 1.0)):
            return False
        snapshot = Conversation(list(conv.messages))
        cut = ctx.split_point(snapshot)
        if cut <= 0:
            return False
        self._old = snapshot.messages[:cut]
        self._cut = cut
        with self._lock:
            self._result = None
            gen = self._gen
        self._thread = threading.Thread(target=self._work, args=(gen, snapshot, agent._compact_instructions("")), daemon=True,
                                        name="axon-bg-compact")
        self._thread.start()
        self._emit("start", tokens=used)
        return False

    def _emit(self, phase: str, tokens: int = 0) -> None:
        from axon.agent.trace import CompactionEvent
        n = len(self.agent.conversation.messages)
        self.agent._emit(CompactionEvent(trigger="background", messages_before=n, messages_after=n, tokens_before=tokens,
                                         tokens_after=tokens, phase=phase))

    def _swap(self) -> bool:
        with self._lock:
            result, self._result = self._result, None
        self._thread = None
        if result is None:
            return False
        summary, usage, model = result
        msgs = self.agent.conversation.messages
        cut = self._cut
        valid = len(msgs) >= cut and all(a is b for a, b in zip(self._old, msgs[:cut]))
        n = len(msgs)
        if not summary or not valid:
            self._retry_after = n + 8          # failed or the conversation moved under it: do not retry at once
            self._emit("failed" if not summary else "stale")
            return False
        done = self.agent.compact(trigger="background", summary=summary, usage_in=usage, model_in=model, cut=cut)
        self._retry_after = 0
        return bool(done)

    def finish(self, timeout: float = WAIT_FOR_SUMMARY_S) -> bool:
        """Wait for a running summary and swap it in (a caller that needs the room now). True when compacted."""
        if self.running:
            self._thread.join(timeout)         # type: ignore[union-attr]
        return self._swap() if self.ready else False

    def cancel(self, announce: bool = True) -> bool:
        """Drop a running or finished background summary. True when there was one."""
        was = self.active
        with self._lock:
            self._gen += 1
            self._result = None
        prov = self._provider
        if prov is not None and hasattr(prov, "cancel"):
            try:
                prov.cancel()
            except Exception:
                pass
        self._thread = None
        if was and announce:
            self._emit("cancelled")
        return was


def background(agent: Any) -> BackgroundCompaction:
    bg = getattr(agent, "_bg_compact", None)
    if bg is None:
        bg = BackgroundCompaction(agent)
        agent._bg_compact = bg
    return bg


def cancel_background(agent: Any, announce: bool = False) -> bool:
    bg = getattr(agent, "_bg_compact", None)
    return bg.cancel(announce) if bg is not None else False


def describe_event(e: Any) -> str:
    """The one status line for a background compaction event, or "" for a finished one (the usual compaction line shows it)."""
    phase = str(getattr(e, "phase", "") or "")
    if phase == "start":
        return "compacting in the background…"
    if phase == "failed":
        return "background compaction failed; the context is compacted when it is needed"
    if phase == "stale":
        return "background summary dropped: the conversation changed while it was written"
    if phase == "cancelled":
        return "background compaction cancelled"
    return ""
