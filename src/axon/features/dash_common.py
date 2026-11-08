"""Small pieces shared by the dashboard modules."""
from __future__ import annotations


class DashError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
