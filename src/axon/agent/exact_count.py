"""Exact prompt size from Anthropic's free ``/v1/messages/count_tokens`` endpoint, with the local estimate as fallback.

The endpoint is only asked when it can change a decision: the estimate is already near the auto-compact
threshold (60% of it or more), at most once per user turn (again later in a long turn only when the prompt grew
by 15% and 20 seconds passed), with a hard 2 second limit. The request goes out on a worker thread: when the
answer is late the turn goes on with the estimate and the number lands in the cache for the next reader.

The answer is remembered against the shape of the conversation it counted (model, message count, estimated
size). Every reader (auto-compact, the context gauges, /context, the Context inspector) asks
:meth:`ExactCounter.value_for`: a number means exact, ``None`` means "show the estimate with a ``~``".

Providers without that endpoint (OpenAI-compatible hosts, gateways, local servers) are calibrated instead: every
response reports its prompt size (``prompt_tokens``); that is the anchor, and what was added since is the local
estimate scaled by a ratio learned from the previous responses (reported / estimated), so the figure converges
on the provider's own tokenizer. It is shown without ``~``; ``~`` only appears before the first response.

Any failure switches counting off for a while and never reaches the user: 429 for a minute, an unsupported
request (400/404/405/422/501, for example a server tool in the request) for the rest of the session, anything
else (timeout, network) for 30 seconds. Only the official Anthropic host is asked; gateways and other
providers keep the estimate.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any

NEAR_FRACTION = 0.6        # ask once the estimate reaches this share of the auto-compact threshold
REGROW_FACTOR = 1.15       # ... and again, in the same turn, after the prompt grew by this much
REGROW_SECONDS = 20.0
HARD_TIMEOUT = 2.0
BACKOFF_RATE_LIMIT = 60.0
BACKOFF_OTHER = 30.0
UNSUPPORTED_STATUS = (400, 404, 405, 422, 501)


@dataclass(frozen=True)
class Reading:
    key: tuple
    tokens: int
    estimate: int
    at: float


@dataclass(frozen=True)
class Calibration:
    model: str
    prompt: int         # prompt tokens the provider reported for the last request
    est: int            # local estimate of the conversation sent in that request (clearing applied)
    overhead: int       # local estimate of system + tools of that request
    ratio: float        # reported / estimated, smoothed over the responses so far


class ExactCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reading: Reading | None = None
        self.disabled = False
        self.off_until = 0.0
        self.turn_counted = False
        self.calls = 0            # requests sent (tests, /doctor)
        self.calib: Calibration | None = None
        self.saved = 0            # tokens client-side context clearing keeps out of the request (agent/client_context)

    @staticmethod
    def key(conv: Any, model: str) -> tuple:
        return (str(model), len(conv.messages), int(conv.token_estimate()))

    def value_for(self, conv: Any, model: str) -> int | None:
        """The exact count when it was measured for this very conversation, else None."""
        with self._lock:
            r = self.reading
        if not conv.messages:
            return None
        if r is not None and r.key == self.key(conv, model):
            return r.tokens
        return self.calibrated(conv, model)

    def calibrated(self, conv: Any, model: str) -> int | None:
        """The provider's last reported prompt size plus the calibrated estimate of what was added since."""
        c = self.calib
        if c is None or c.model != str(model):
            return None
        now = max(0, int(conv.token_estimate()) - int(self.saved))
        return max(c.overhead, int(round(c.prompt + c.ratio * (now - c.est))))

    def note_usage(self, provider: Any, model: str, usage: Any, conv: Any, overhead: int) -> None:
        """Record a response's reported prompt size as the new anchor and refine the estimate ratio."""
        p = prompt_tokens(provider, usage)
        est = max(0, int(conv.token_estimate()) - int(self.saved))
        if p <= 0 or est <= 0:
            return
        new = min(4.0, max(0.25, (p - overhead) / est)) if p > overhead else 1.0
        old = self.calib
        ratio = new if old is None or old.model != str(model) else 0.5 * old.ratio + 0.5 * new
        self.calib = Calibration(str(model), int(p), est, int(overhead), ratio)

    def _fail(self, exc: BaseException) -> None:
        status = getattr(exc, "status_code", None)
        now = time.monotonic()
        if status == 429:
            self.off_until = now + BACKOFF_RATE_LIMIT
        elif isinstance(status, int) and status in UNSUPPORTED_STATUS:
            self.disabled = True
        else:
            self.off_until = now + BACKOFF_OTHER

    def refine(self, agent: Any, system: list[dict[str, Any]], tools: list[dict[str, Any]], *, first_iteration: bool) -> bool:
        """Count now when worthwhile. True when a fresh exact number is available afterwards."""
        if first_iteration:
            self.turn_counted = False
        settings = agent.settings
        if self.disabled or not bool(getattr(settings, "exact_token_count", True)):
            return False
        now = time.monotonic()
        if now < self.off_until:
            return False
        provider = getattr(agent, "provider", None)
        fn = getattr(provider, "count_tokens", None)
        if not callable(fn) or not getattr(provider, "_official", False):
            return False
        conv, ctx, model = agent.conversation, agent.context, settings.model
        if not conv.messages:
            return False
        estimate = conv.token_estimate() + ctx.overhead_tokens(system, tools)
        if estimate < NEAR_FRACTION * ctx.threshold(model):
            return False
        key = self.key(conv, model)
        last = self.reading
        if last is not None and last.key == key:
            return True
        if self.turn_counted and not (last is not None and estimate > last.estimate * REGROW_FACTOR and now - last.at > REGROW_SECONDS):
            return False
        self.turn_counted = True
        try:
            from axon.agent.context_edit import config_for
            edits = config_for(agent)
        except Exception:
            edits = None
        messages = copy.deepcopy(conv.messages)     # the turn goes on and may rewrite messages while the worker reads
        sys_copy, tool_copy = copy.deepcopy(system), copy.deepcopy(tools)
        done = threading.Event()

        def work() -> None:
            try:
                self.calls += 1
                try:
                    n = fn(model=model, system=sys_copy, messages=messages, tools=tool_copy, context_management=edits, timeout=HARD_TIMEOUT)
                except Exception as first:
                    if edits is None or getattr(first, "status_code", None) != 400:
                        raise
                    # the endpoint refused the context_management part: count the plain request instead
                    n = fn(model=model, system=sys_copy, messages=messages, tools=tool_copy, context_management=None, timeout=HARD_TIMEOUT)
                if isinstance(n, int) and n > 0:
                    with self._lock:
                        self.reading = Reading(key, n, estimate, time.monotonic())
            except BaseException as e:  # noqa: BLE001 - never reaches the turn
                self._fail(e)
            finally:
                done.set()

        threading.Thread(target=work, name="axon-count-tokens", daemon=True).start()
        done.wait(HARD_TIMEOUT + 0.25)
        if not done.is_set():
            self.off_until = max(self.off_until, time.monotonic() + BACKOFF_OTHER)
        return self.value_for(conv, model) is not None


def prompt_tokens(provider: Any, usage: Any) -> int:
    """Prompt size a response reported. Anthropic counts cached tokens apart from ``input``; OpenAI-style
    usage (what openai_compat stores) already includes them in ``input``."""
    inp = int(getattr(usage, "input", 0) or 0)
    cached = int(getattr(usage, "cache_read", 0) or 0) + int(getattr(usage, "cache_write", 0) or 0)
    if getattr(provider, "name", "") == "openai_compat":
        return max(inp, cached)
    return inp + cached


def note_call(agent: Any, turn: Any, system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
    """After a model call: calibrate the local estimate against the usage the response reported."""
    try:
        usage = getattr(turn, "usage", None)
        if usage is None:
            return
        ctx = agent.context
        ctx.exact.note_usage(getattr(agent, "provider", None), agent.settings.model, usage, agent.conversation,
                             ctx.overhead_tokens(system, tools))
    except Exception:
        pass


def reading(agent: Any) -> tuple[int, bool]:
    """(tokens, exact) for the live conversation, for any display: exact only when the endpoint counted it."""
    conv, ctx = agent.conversation, agent.context
    exact = ctx.exact.value_for(conv, agent.settings.model)
    if exact is not None:
        return exact, True
    est = conv.token_estimate()
    return est, False
