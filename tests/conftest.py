"""
Pytest configuration and shared fixtures for Axon.
"""
import os
import sys
from pathlib import Path

# Tests must never open a real browser window (the usage report, MCP sign-in and similar pages do). Programs
# started by tests, such as the e2e `axon` subprocesses, inherit this: $BROWSER names the command webbrowser runs.
os.environ["BROWSER"] = "true"
# ... and must never show a desktop notification either (macOS plays a sound for each one).
os.environ["AXON_NO_NOTIFY"] = "1"
_here = Path(__file__).parent
_root = _here.parent
for p in [str(_root), str(_root / "src"), str(_here)]:
    if p not in sys.path:
        sys.path.insert(0, p)
import pytest
from axon.config import Settings
from axon.tools import create_default_registry
from axon.permissions.engine import PermissionEngine
from axon.agent.context import ContextManager
from axon.session.store import SessionStore
from axon.session.ledger import Ledger

@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process calls of webbrowser.open*() do nothing and report that no browser opened."""
    import webbrowser
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def isolate_user_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate user home directory so test runs never overwrite ~/.axon/config.toml."""
    fake_home = tmp_path_factory.mktemp("axon_fake_home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    from axon import storage
    storage.set_active_session("", "")  # the "current session" registry is process-wide state
    return fake_home

@pytest.fixture(autouse=True)
def _fresh_tip_engine():
    """The tips engine is a process-wide singleton (footer/spinner state): start each test clean."""
    try:
        from axon.features.tips import reset_tip_engine
    except Exception:
        yield
        return
    reset_tip_engine()
    yield
    reset_tip_engine()

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


@pytest.fixture(autouse=True)
def _private_clipboard(tmp_path_factory, monkeypatch):
    """Copies made by tests go to a temp file, never to the developer's real clipboard."""
    monkeypatch.setenv("AXON_CLIPBOARD_FILE", str(tmp_path_factory.mktemp("clip") / "clipboard.txt"))
