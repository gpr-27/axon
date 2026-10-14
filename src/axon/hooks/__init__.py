"""
Lifecycle hooks (Claude-Code-compatible).
"""
from axon.hooks.manager import EVENTS, HookCommand, HookManager, HookResult, normalize_event, platform_shell_argv
from axon.hooks.runner import HookOutcome, HookRunner

__all__ = [
    "EVENTS",
    "HookCommand",
    "HookManager",
    "HookResult",
    "HookOutcome",
    "HookRunner",
    "normalize_event",
    "platform_shell_argv",
]
