"""
Axon exception hierarchy.
"""
from __future__ import annotations
from typing import Any

class AxonError(Exception):
    """Base exception for all Axon errors."""
    pass

class ConfigError(AxonError):
    """Configuration error (missing keys, malformed TOML). Fatal at startup."""
    pass

class ProviderError(AxonError):
    """Transport / API failure carrying HTTP status code and response body."""
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body

class ToolError(AxonError):
    """
    A tool failed in a way the model should see.
    The message text is consumed by the model to self-correct.
    """
    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message or message

class PermissionDenied(ToolError):
    """Tool invocation denied by the permission engine or path jail."""
    pass

class StaleFileError(ToolError):
    """Read-before-edit invariant violation."""
    pass

class InterruptedTurn(AxonError):
    """User interrupted execution mid-turn (Law 5)."""
    def __init__(self, message: str = "Interrupted by user", partial_results: list[Any] | None = None):
        super().__init__(message)
        self.partial_results = partial_results or []

class BudgetExceeded(AxonError):
    """Cost or token budget ceiling hit."""
    pass
