"""
Backward-compatible hook runner facade over :class:`axon.hooks.manager.HookManager`.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from axon.hooks.manager import HookManager


@dataclass(frozen=True)
class HookOutcome:
    proceed: bool
    override_output: str | None = None


class HookRunner:
    """Legacy API: ``HookRunner(settings).run(event, payload) -> HookOutcome``."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.manager = HookManager(settings, getattr(settings, "workspace", None))

    def run(self, event: str, payload: dict[str, Any]) -> HookOutcome:
        res = self.manager.run(event, payload)
        if res.proceed:
            return HookOutcome(proceed=True)
        return HookOutcome(proceed=False, override_output=res.message or res.stop_reason)
