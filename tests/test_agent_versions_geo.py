"""Agent definition versions (history, diff, rollback, pins) and the inference_geo setting (fakes only)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import storage
from axon.agent import agent_history as ah
from axon.agent.subagent import get_agent_definition, load_agent_definitions
from axon.providers import geo_wire
from axon.providers.base import Usage

TEXT1 = "---\nname: rev\ndescription: Reviews code\n---\nYou review. Version one.\n"
TEXT2 = "---\nname: rev\ndescription: Reviews code\n---\nYou review. Version two.\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    ah._LAST.clear()
    ws = tmp_path / "ws"
    ws.mkdir()
    f = tmp_path / "home" / "agents" / "rev.md"
    f.parent.mkdir(parents=True)
    f.write_text(TEXT1)
    storage.set_active_session(ws, "sess-1")
    yield ws, f
    storage.set_active_session("", "")
    ah._LAST.clear()


def _agent(ws):
    from axon.config import Settings
    return SimpleNamespace(settings=Settings(api_key="k", workspace=ws))


def _vers(ws):
    d, scope = ah.find_definition(ws, "rev")
    return d, scope, ah.versions(scope, "rev")


def test_history_records_on_load_and_dedupes(env):
    ws, f = env
    load_agent_definitions(ws)
    load_agent_definitions(ws)
    ah._LAST.clear()
    load_agent_definitions(ws)               # a fresh process sees the same hash on disk: still one version
    d, scope, vs = _vers(ws)
    assert [v["v"] for v in vs] == [1] and vs[0]["text"] == TEXT1 and vs[0]["who"] == "user" and d.version == 1
    f.write_text(TEXT2)
    d2, _, vs = _vers(ws)
    assert [v["v"] for v in vs] == [1, 2] and d2.version == 2 and len(vs[1]["sha256"]) == 64


def test_history_is_bounded(env, monkeypatch):
    ws, f = env
    monkeypatch.setattr(ah, "MAX_VERSIONS", 3)
    for i in range(6):
        f.write_text(TEXT1 + f"\nrev {i}\n")
        load_agent_definitions(ws)
    _, _, vs = _vers(ws)
    assert [v["v"] for v in vs] == [4, 5, 6]


def test_diff_and_commands(env, capsys):
    from axon.commands.agent_history_cmds import run
    ws, f = env
    load_agent_definitions(ws)
    f.write_text(TEXT2)
    agent = _agent(ws)
    run(agent, "diff", "rev")
    out = capsys.readouterr().out
    assert "-You review. Version one." in out and "+You review. Version two." in out
    run(agent, "history", "rev")
    assert "v2" in capsys.readouterr().out


def test_rollback_keeps_current_as_its_own_version(env):
    from axon.commands.agent_history_cmds import run
    ws, f = env
    load_agent_definitions(ws)
    f.write_text(TEXT2)                       # edited outside Axon, never loaded yet
    run(_agent(ws), "rollback", "rev 1")
    assert f.read_text() == TEXT1
    _, _, vs = _vers(ws)
    assert [(v["v"], v["text"], v["who"]) for v in vs] == [(1, TEXT1, "user"), (2, TEXT2, "user"), (3, TEXT1, "edit-command")]


def test_rollback_refuses_plugin_managed(env, tmp_path, capsys):
    ws, _ = env
    pf = tmp_path / "plug" / "agents" / "pg.md"
    pf.parent.mkdir(parents=True)
    pf.write_text("---\nname: pg\ndescription: d\n---\nbody\n")
    from axon.agent.subagent import parse_agent_file
    d = parse_agent_file(pf, "plugin:acme")
    assert d.version == 1
    scope = ah.scope_for("plugin:acme", pf)
    with pytest.raises(ValueError, match="plugin"):
        ah.rollback(ws, d, scope, 1)
    assert pf.read_text().endswith("body\n")
    assert ah.latest(scope, "pg")["who"] == "plugin"


def test_pin_survives_file_change_and_resume(env):
    from axon.commands.agent_history_cmds import run
    ws, f = env
    load_agent_definitions(ws)
    agent = _agent(ws)
    run(agent, "pin", "rev 1")
    f.write_text(TEXT2)
    d = get_agent_definition(ws, "rev")
    assert "Version one" in d.system_prompt and d.version == 1
    # a later process / resumed session: same session id, pin read back from the session folder
    ah._LAST.clear()
    d = get_agent_definition(ws, "rev@v1")           # the label shown on the card resolves back to the name
    assert "Version one" in d.system_prompt
    storage.set_active_session(ws, "other-session")
    assert "Version two" in get_agent_definition(ws, "rev").system_prompt
    storage.set_active_session(ws, "sess-1")
    run(agent, "pin", "rev off")
    assert "Version two" in get_agent_definition(ws, "rev").system_prompt


def test_pin_note_when_disk_differs(env):
    ws, f = env
    load_agent_definitions(ws)
    d, scope, _ = _vers(ws)
    ah.set_pin(ws, "rev", scope, 1)
    f.write_text(TEXT2)
    d, _, _ = _vers(ws)
    assert "pinned to v1" in ah.pin_note(ws, d) and "v2" in ah.pin_note(ws, d)
    assert ah.version_label(d) == "rev@v2" and ah.version_label(SimpleNamespace(name="x", version=0)) == "x"


def test_inference_geo_request_shape_and_gating():
    kw = {"extra_body": {"output_config": {"effort": "high"}}}
    assert geo_wire.apply(kw, "claude-sonnet-5-5", "US")
    assert kw["extra_body"]["inference_geo"] == "us" and kw["extra_body"]["output_config"]
    k2 = {}
    assert not geo_wire.apply(k2, "claude-sonnet-5-5", "")                      # unset: nothing sent
    assert not geo_wire.apply(k2, "claude-sonnet-5-5", "eu")                    # not a documented value
    assert not geo_wire.apply(k2, "claude-sonnet-5-5", "us", official=False)    # other providers ignore it
    assert not geo_wire.apply(k2, "gpt-5", "us") and k2 == {}
    assert geo_wire.strip(kw) and "inference_geo" not in kw["extra_body"]
    assert geo_wire.reported({"inference_geo": "US"}) == "us" and geo_wire.reported(SimpleNamespace()) == ""


def _prov(base="https://api.anthropic.com", geo="us"):
    from axon.config import Settings
    from axon.providers.anthropic import AnthropicProvider
    return AnthropicProvider(Settings(api_key="k", base_url=base, inference_geo=geo))


def _drive(prov, monkeypatch, fail_first=None, model="claude-sonnet-5-5"):
    from axon.providers.base import AssistantTurn, TextBlock, TurnComplete
    seen = []

    def fake_run(kwargs, state, emitted, thinking, wire_model=""):
        seen.append({k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()})
        if fail_first is not None and len(seen) == 1:
            raise fail_first
        state.turn = AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn", usage=Usage())
        yield TurnComplete("end_turn", Usage())

    monkeypatch.setattr(prov, "_run", fake_run)
    monkeypatch.setattr("axon.providers.transport._sleep", lambda *_: None)
    list(prov.stream(model=model, system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=1000, effort="quantum"))
    return seen


def test_provider_sends_geo_and_strips_it_after_a_400(monkeypatch):
    class Err(Exception):
        status_code = 400
        message = "inference_geo: Extra inputs are not permitted"
        body = None

    prov = _prov()
    seen = _drive(prov, monkeypatch, fail_first=Err())
    assert seen[0]["extra_body"]["inference_geo"] == "us"
    assert len(seen) == 2 and "inference_geo" not in seen[1].get("extra_body", {}) and prov._geo_off
    assert "inference_geo" not in _drive(prov, monkeypatch)[0].get("extra_body", {})     # stays off this session


def test_provider_geo_unset_or_other_provider(monkeypatch):
    assert "inference_geo" not in _drive(_prov(geo=""), monkeypatch)[0].get("extra_body", {})
    assert "inference_geo" not in _drive(_prov(base="https://agentrouter.org"), monkeypatch)[0].get("extra_body", {})


def test_usage_geo_read_and_shown(monkeypatch):
    from axon.providers.anthropic import AnthropicProvider
    u = AnthropicProvider._usage(SimpleNamespace(input_tokens=5, output_tokens=2, inference_geo="us"), Usage())
    assert u.inference_geo == "us"
    from axon.commands import usage_screen
    ledger = SimpleNamespace(entries=[SimpleNamespace(usage=Usage(inference_geo="global"))])
    monkeypatch.setattr(usage_screen, "session_ledger", lambda a: ledger)
    ag = SimpleNamespace(settings=SimpleNamespace(inference_geo="us"))
    assert "us" in usage_screen._geo_line(ag) and "ran in global" in usage_screen._geo_line(ag)
    assert usage_screen._geo_line(SimpleNamespace(settings=SimpleNamespace(inference_geo=""))) == ""


def test_settings_alias():
    from axon.config import Settings
    assert Settings(api_key="k").inference_geo == ""
