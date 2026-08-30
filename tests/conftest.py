"""
Pytest configuration and shared fixtures for Axon.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import pytest
from axon.config import Settings
from axon.tools import create_default_registry
from axon.permissions.engine import PermissionEngine
from axon.agent.context import ContextManager
from axon.session.store import SessionStore
from axon.session.ledger import Ledger

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Fixture providing isolated temporary workspace."""
    ws = tmp_path / "test_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws

@pytest.fixture
def settings(workspace: Path) -> Settings:
    return Settings.load({
        "workspace": workspace,
        "mode": "bypass",
        "api_key": "test-key-12345",
    })

@pytest.fixture
def registry():
    return create_default_registry()

@pytest.fixture
def permissions(settings: Settings):
    return PermissionEngine(settings)

@pytest.fixture
def context_manager(settings: Settings):
    return ContextManager(settings)

@pytest.fixture
def session_store(workspace: Path):
    return SessionStore(workspace=workspace)

@pytest.fixture
def ledger():
    return Ledger()
