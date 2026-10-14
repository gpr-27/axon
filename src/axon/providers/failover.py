"""
Multi-provider failover: when the primary model keeps failing with 429 / 5xx / overload / timeout
(after the provider's own retries) and nothing has streamed yet, try the next model of the
configured chain (settings.fallback_model / settings.routing[role]). Each hop is emitted as a
RetryEvent so the turn trace shows it ("anthropic HTTP 529 -> openai_compat:gpt-5").
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator

from axon.errors import ProviderError
from axon.providers.base import (
    AssistantTurn,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolResultBlock,
    ToolUseStart,
    retry_event,
)
from axon.providers.transport import is_failover_error

_CONTENT_EVENTS = (TextDelta, ThinkingDelta, ToolUseStart, ToolArgsDelta)


class FailoverProvider:
    """Provider wrapper presenting the primary provider's identity (name, tool encoding)."""

    def __init__(self, primary: Any, *, model: str, settings: Any, chain: list[str]) -> None:
        self.primary = primary
        self.primary_model = model
        self.settings = settings
        self.chain = list(chain)
        self._fallbacks: dict[str, Any] = {}
        self._local = threading.local()

    @property
    def name(self) -> str:  # tool-schema style and result encoding follow the primary
        return getattr(self.primary, "name", "openai_compat")

    def __getattr__(self, item: str) -> Any:
        # Delegate unknown attributes (settings, _client, _url, ...) to the primary provider.
        if item in ("primary", "_local", "_fallbacks"):
            raise AttributeError(item)
        return getattr(self.primary, item)

    def cancel(self, thread_ident: int | None = None) -> bool:
        """Abort the in-flight stream on the primary and every fallback provider."""
        hit = False
        for prov in [self.primary, *list(self._fallbacks.values())]:
            fn = getattr(prov, "cancel", None)
            if callable(fn):
                try:
                    hit = bool(fn(thread_ident)) or hit
                except Exception:
                    pass
        return hit

    def _fallback_provider(self, model: str) -> Any:
        prov = self._fallbacks.get(model)
        if prov is None:
            from axon.providers.registry import provider_for
            prov = provider_for(model, self.settings, failover=False)
            self._fallbacks[model] = prov
        return prov

    def stream(self, **kwargs: Any) -> Iterator[StreamEvent]:
        candidates: list[tuple[str, Any]] = [(self.primary_model, self.primary)] + [(m, None) for m in self.chain]
        self._local.active = None
        last_error: BaseException | None = None
        for hop, (model, prov) in enumerate(candidates):
            if prov is None:
                try:
                    prov = self._fallback_provider(model)
                except Exception as e:  # misconfigured fallback entry: skip it, keep the reason
                    last_error = e
                    continue
            call = dict(kwargs)
            if hop > 0:
                call["model"] = model
            self._local.active = (model, prov)
            emitted = False
            started = time.time()
            try:
                for ev in prov.stream(**call):
                    if isinstance(ev, _CONTENT_EVENTS):
                        emitted = True
                    yield ev
                return
            except ProviderError as e:
                if emitted or not is_failover_error(e) or hop == len(candidates) - 1:
                    raise
                last_error = e
                status = getattr(e, "status", None)
                yield retry_event(
                    hop + 1,
                    len(candidates) - 1,
                    0.0,
                    f"{getattr(prov, 'name', 'provider')}:{model} {('HTTP ' + str(status)) if status else 'unavailable'} "
                    f"-> failover to {candidates[hop + 1][0]}, +{time.time() - started:.1f}s",
                )
        if last_error is not None:
            raise last_error

    def finalize(self) -> AssistantTurn:
        active = getattr(self._local, "active", None)
        self._local.active = None
        if active is None:
            return self.primary.finalize()
        model, prov = active
        turn = prov.finalize()
        if turn.model is None:
            turn.model = model
        return turn

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        return self.primary.encode_tool_results(results)

    def supports(self, feature: str) -> bool:
        return bool(self.primary.supports(feature)) or feature == "failover"


__all__ = ["FailoverProvider"]
