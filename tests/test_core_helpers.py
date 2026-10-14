"""Shared helpers for the CORE test modules (not a test file itself)."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry


def make_settings(workspace: Path, **kw: Any) -> Settings:
    base = {"workspace": workspace, "mode": "bypass", "api_key": "test-key-12345"}
    base.update(kw)
    return Settings(**base)


def make_agent(provider: Any, workspace: Path, registry: Any = None, events: list | None = None, **kw: Any) -> Agent:
    agent_kwargs = {k: kw.pop(k) for k in ("on_approval", "hooks", "agent_name", "extra_system") if k in kw}
    settings = make_settings(workspace, **kw)
    agent = Agent(
        provider=provider,
        tools=registry or create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(workspace=workspace),
        ledger=Ledger(),
        settings=settings,
        on_event=(events.append if events is not None else None),
        **agent_kwargs,
    )
    agent.retry_backoff_s = 0.0
    return agent
