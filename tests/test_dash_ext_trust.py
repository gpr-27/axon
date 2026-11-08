"""The Extensions page trusts a folder only for the exact list the person saw, and reloads hooks afterwards."""
import json
from types import SimpleNamespace

import pytest

from axon.features import dash_ext
from axon.features.dash_common import DashError
from axon.hooks import trust


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    trust.configure(True)
    yield folder
    trust.configure(False)
    trust.reset_session()


def _srv(folder, reloads):
    rt = SimpleNamespace(agent=SimpleNamespace(hooks=SimpleNamespace(reload=lambda: reloads.append(1))))
    return SimpleNamespace(workspace=folder, all_runtimes=lambda: [rt])


def test_view_lists_what_would_run_and_is_untrusted(ws):
    v = dash_ext.trust_view(ws)
    assert v["status"] == "untrusted" and not v["allowed"] and v["items"][0]["kind"] == "hook"
    assert "echo hi" in v["items"][0]["detail"] and v["sha"]


def test_trust_needs_the_matching_list_then_reloads_hooks(ws):
    reloads = []
    srv = _srv(ws, reloads)
    with pytest.raises(DashError):
        dash_ext.trust_post(srv, {"action": "trust", "sha": "stale"})
    assert dash_ext.trust_view(ws)["status"] == "untrusted"
    dash_ext.trust_post(srv, {"action": "trust", "sha": dash_ext.trust_view(ws)["sha"]})
    assert dash_ext.trust_view(ws)["status"] == "trusted" and reloads
    dash_ext.trust_post(srv, {"action": "revoke"})
    assert dash_ext.trust_view(ws)["status"] == "untrusted"
    with pytest.raises(DashError):
        dash_ext.trust_post(srv, {"action": "nope"})


def test_the_page_turns_folder_trust_on_and_puts_it_back():
    from axon.features.dashboard import _configure_gates
    from axon.mcp import state
    trust.configure(False)
    state.set_interactive(None)
    restore = _configure_gates()
    assert trust.is_interactive() and state.is_interactive() and not state.can_prompt()
    restore()
    assert not trust.is_interactive() and not state.is_interactive() and state.can_prompt() is False
