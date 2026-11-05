"""Regression tests from the code review of mcp/, marketplace/, skills/, hooks/, permissions/, features/."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from axon.hooks import envfile
from axon.hooks.manager import HookManager
from axon.mcp.oauth import OAuthError, OAuthManager, TokenStore
from axon.mcp.config import ServerSpec
from axon.permissions.rules import Rule, has_substitution, path_matches


@pytest.fixture(autouse=True)
def _axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon_home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    return home


# ── permissions: command substitution hidden behind an escaped quote ─────────

def test_has_substitution_sees_through_escaped_quotes() -> None:
    evasion = "git status \"\\\"'\" $(touch pwned) ''"
    assert has_substitution(evasion)
    assert has_substitution("git status \\' $(touch pwned) \\'")
    assert Rule("Bash", "git status:*").matches("Bash", {"command": evasion}) is False
    # still not fooled the other way: quoted / escaped text is just text
    assert not has_substitution("echo '$(x)'")
    assert not has_substitution("echo \\$(x)")
    assert Rule("Bash", "git status:*").matches("Bash", {"command": "git status -s"}) is True


def test_glob_negation_in_path_rules(tmp_path: Path) -> None:
    assert path_matches("src/[!t]*.py", "src/a.py", tmp_path)
    assert not path_matches("src/[!t]*.py", "src/t.py", tmp_path)
    assert path_matches("src/[!t]*.py", "src/!.py", tmp_path)       # '!' is just another character that is not t
    assert path_matches("src/[^a].py", "src/^.py", tmp_path)       # a literal caret in glob syntax
    assert not path_matches("src/[^a].py", "src/b.py", tmp_path)


# ── hooks ────────────────────────────────────────────────────────────────────

def _manager(workspace: Path, hooks: dict[str, Any]) -> HookManager:
    (workspace / ".axon").mkdir(parents=True, exist_ok=True)
    (workspace / ".axon" / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return HookManager(None, workspace)


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_hook_timeout_kills_the_hooks_children(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "slow.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen(['sleep', '60'])\nopen({str(pidfile)!r}, 'w').write(str(p.pid))\ntime.sleep(60)\n",
        encoding="utf-8")
    cmd = f'"{sys.executable}" "{script}"'
    hm = _manager(tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd, "timeout": 1}]}]})
    result = hm.run("PreToolUse", {"tool_name": "Bash"})
    assert any("timed out" in w for w in result.warnings)
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    alive = True
    while alive and time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            alive = False
    assert not alive, "the hook's child survived the timeout"


def test_once_hook_runs_a_single_time(tmp_path: Path) -> None:
    counter = tmp_path / "count.txt"
    script = tmp_path / "count.py"
    script.write_text(f"open({str(counter)!r}, 'a').write('x')\n", encoding="utf-8")
    cmd = f'"{sys.executable}" "{script}"'
    hm = _manager(tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd, "once": True}]}]})
    for _ in range(3):
        hm.run("PreToolUse", {"tool_name": "Bash"})
    assert counter.read_text() == "x"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_env_file_lives_in_a_private_directory() -> None:
    envfile.reset()
    path = envfile.file_for("sess")
    try:
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert path.parent.name.startswith("axon-hook-env-") and path.is_file()
    finally:
        envfile.reset()


# ── OAuth ────────────────────────────────────────────────────────────────────

def _spec() -> ServerSpec:
    return ServerSpec.from_dict("srv", {"type": "http", "url": "https://mcp.example/mcp"})


def _signed_in(tmp_path: Path, handler: Any) -> OAuthManager:
    mgr = OAuthManager(_spec(), store=TokenStore(tmp_path / "auth.json"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    mgr.store.update(mgr.key, client={"client_id": "c"},
                     tokens={"access_token": "old", "refresh_token": "r-1", "expires_at": time.time() - 10})
    return mgr


def _oauth_world(token_response: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/token"):
            if isinstance(token_response, Exception):
                raise token_response
            return token_response
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={"authorization_endpoint": "https://auth.example/authorize",
                                             "token_endpoint": "https://auth.example/token"})
        return httpx.Response(404)
    return handler


def test_refresh_keeps_tokens_when_the_network_is_down(tmp_path: Path) -> None:
    mgr = _signed_in(tmp_path, _oauth_world(httpx.ConnectError("offline")))
    assert mgr.refresh() is False
    assert mgr.tokens().get("refresh_token") == "r-1"            # still signed in once the network is back


def test_refresh_keeps_tokens_on_a_server_error(tmp_path: Path) -> None:
    mgr = _signed_in(tmp_path, _oauth_world(httpx.Response(503, json={"error": "temporarily_unavailable"})))
    assert mgr.refresh() is False
    assert mgr.tokens().get("refresh_token") == "r-1"


def test_refresh_drops_tokens_the_server_rejected(tmp_path: Path) -> None:
    mgr = _signed_in(tmp_path, _oauth_world(httpx.Response(400, json={"error": "invalid_grant"})))
    assert mgr.refresh() is False
    assert mgr.tokens() == {}


def test_authorization_url_must_be_https(tmp_path: Path) -> None:
    mgr = OAuthManager(_spec(), store=TokenStore(tmp_path / "auth.json"))
    client = {"client_id": "c"}
    with pytest.raises(OAuthError, match="non-https"):
        mgr.build_authorization_url({"authorization_endpoint": "file:///Applications/Calculator.app"}, client,
                                    "http://localhost:1/callback", "s", "c")
    url = mgr.build_authorization_url({"authorization_endpoint": "https://auth.example/authorize"}, client,
                                      "http://localhost:1/callback", "s", "c")
    assert url.startswith("https://auth.example/authorize?")


# ── MCP transport / bridge ───────────────────────────────────────────────────

def test_legacy_sse_refuses_an_endpoint_on_another_origin() -> None:
    from axon.mcp.transports import LegacySSETransport, TransportError

    q: list[Any] = []
    gate = threading.Event()

    def stream():
        yield b"event: endpoint\ndata: https://evil.example/steal\n\n"
        gate.wait(2)

    def handler(request: httpx.Request) -> httpx.Response:
        q.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=stream())

    t = LegacySSETransport("https://old.example/sse", client=httpx.Client(transport=httpx.MockTransport(handler)),
                           name="old", connect_timeout=3, header_provider=lambda: {"Authorization": "Bearer secret"})
    try:
        with pytest.raises(TransportError, match="own origin"):
            t.start(lambda m: None, lambda r: None)
        assert t.endpoint is None
    finally:
        gate.set()
        t.close()


def test_unpublish_also_removes_a_toolsearch_revealed_tool() -> None:
    from axon.mcp.bridge import _registry_unregister
    from axon.tools.interactive import _Revealed

    class Wrapper:
        name = "mcp__srv__t"

    class Registry:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def unregister(self, name: str) -> None:
            self.tools.pop(name, None)

    wrapper, reg = Wrapper(), Registry()
    reg.tools[wrapper.name] = _Revealed(wrapper)                # what ToolSearch registers after revealing a tool
    _registry_unregister(reg, wrapper.name, wrapper)
    assert wrapper.name not in reg.tools
    other = Wrapper()
    reg.tools[wrapper.name] = other                             # a newer tool of that name stays
    _registry_unregister(reg, wrapper.name, wrapper)
    assert reg.tools[wrapper.name] is other


def test_removing_a_server_forgets_tokens_stored_under_its_expanded_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.mcp import commands
    from axon.mcp.config import MCPConfig

    monkeypatch.setenv("MCP_REVIEW_HOST", "mcp.example")
    ws = tmp_path / "ws"
    ws.mkdir()
    MCPConfig(ws).add_raw("srv", {"type": "http", "url": "https://${MCP_REVIEW_HOST}/mcp"}, "user")
    spec = MCPConfig(ws).get_spec("srv")
    assert spec is not None
    store = TokenStore()
    key = OAuthManager(spec.resolved(ws)).key
    store.update(key, name="srv", tokens={"access_token": "t"})
    from axon.mcp.bridge import shutdown_mcp_bridge
    ctx = commands._Ctx(SimpleNamespace(settings=SimpleNamespace(workspace=ws)))
    try:
        commands._cmd_remove(ctx, ["srv"])
    finally:
        shutdown_mcp_bridge()                                   # the bridge is a process-wide singleton
    assert store.get(key) == {}


# ── marketplace / skills ─────────────────────────────────────────────────────

def test_project_scope_install_is_approved(tmp_path: Path) -> None:
    from axon.marketplace import catalog, installer
    from axon.mcp import state
    from axon.mcp.config import MCPConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    entry = catalog.find_entry(catalog.load_connectors(), "fetch")
    result = installer.install_connector(entry, ws, scope="project", check=False)
    assert result.ok, result.message
    assert state.project_choice(ws, "fetch") == "approved"
    state.set_interactive(True)
    try:
        spec = MCPConfig(ws).get_spec("fetch")
    finally:
        state.set_interactive(None)
    assert spec is not None and spec.approval == "approved"


def test_import_skips_connector_names_that_are_not_valid(tmp_path: Path) -> None:
    from axon.marketplace import transfer
    from axon.mcp.config import MCPConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    data = {"format": transfer.FORMAT, "version": transfer.VERSION, "connectors": [
        {"name": "bad name/../x", "scope": "user", "config": {"command": "true"}},
        {"name": "", "scope": "user", "config": {"command": "true"}},
        {"name": "good-one", "scope": "user", "config": {"command": "true"}}]}
    report = transfer.import_setup(ws, data)
    assert report.changed == ["good-one"]
    assert set(MCPConfig(ws).load_specs()) == {"good-one"}


def test_skill_update_check_falls_back_to_master(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.skills import importer

    calls: list[str] = []

    def fake_download(url: str) -> str:
        calls.append(url)
        if "/main/" in url:
            raise OSError("404")
        return "---\nname: s\n---\nbody"

    monkeypatch.setattr(importer, "_download", fake_download)
    assert importer.fetch_skill_text("owner/repo/skills/s").startswith("---")
    assert calls[0].endswith("/main/skills/s/SKILL.md") and "/master/" in calls[1]
    with pytest.raises(OSError):                                  # an explicit ref is never rewritten
        importer.fetch_skill_text("https://github.com/owner/repo/tree/main/skills/s")


# ── features ─────────────────────────────────────────────────────────────────

def test_fix_snippets_never_read_outside_the_workspace(tmp_path: Path) -> None:
    from axon.features.solver import Failure, snippet

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("a\nb\nc\n", encoding="utf-8")
    outside = tmp_path / "secret.json"
    outside.write_text('{"private_key": "x"}\n', encoding="utf-8")
    assert "b" in snippet(ws, Failure("ok.py", 2, "boom"))
    assert snippet(ws, Failure(str(outside), 1, "boom")) == ""
    assert snippet(ws, Failure("../secret.json", 1, "boom")) == ""


def test_plan_verify_commands_go_through_the_permission_engine(tmp_path: Path) -> None:
    from axon.features import planner
    from axon.features.planner import Plan, PlanStep
    from axon.permissions.engine import Decision

    marker = tmp_path / "ran.txt"
    cmd = f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"'

    class Perms:
        def __init__(self, outcome: str) -> None:
            self.outcome, self.seen = outcome, []

        def check(self, tool: Any, args: dict[str, Any], mode: Any = None) -> Decision:
            self.seen.append(args["command"])
            return Decision(self.outcome, "test")

    def agent(outcome: str) -> Any:
        a = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path, mode="default", effort=None, model="m"),
                            permissions=Perms(outcome), traces=[], run_turn=lambda prompt: SimpleNamespace(
                                final_text="done", stop_reason="end_turn", tool_calls_count=0, error=None),
                            ledger=None)
        return a

    def run(outcome: str) -> Plan:
        plan = Plan("p", "task", "goal", [PlanStep("one", verify=f"`{cmd}`")], workspace=str(tmp_path))
        planner.run_step(agent(outcome), plan, 0, tmp_path)
        return plan

    plan = run("deny")
    assert not marker.exists() and plan.steps[0].status == "done"
    run("allow")
    assert marker.read_text() == "x"


# ── unparsable settings.json is never overwritten ────────────────────────────

def test_write_rule_refuses_to_overwrite_an_unparsable_settings_file(tmp_path: Path) -> None:
    from axon.marketplace import control

    bad = tmp_path / "settings.json"
    bad.write_text('{"permissions": {"deny": ["Bash(rm:*)"],}', encoding="utf-8")   # trailing comma, cut off
    with pytest.raises(ValueError, match="will not overwrite"):
        control.write_rule(bad, "allow", "mcp__srv")
    assert "Bash(rm:*)" in bad.read_text(encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / ".axon").mkdir(parents=True)
    (ws / ".axon" / "settings.local.json").write_text("[1, 2]", encoding="utf-8")
    from axon.mcp.config import MCPConfig
    MCPConfig(ws).add_raw("srv", {"command": "true"}, "user")
    result = control.set_tool_policy(ws, "srv", "*", "allow")
    assert not result.ok and "will not overwrite" in result.message
    assert (ws / ".axon" / "settings.local.json").read_text(encoding="utf-8") == "[1, 2]"
    fresh = tmp_path / "new" / "settings.json"
    control.write_rule(fresh, "allow", "mcp__srv")                                   # a missing file is created
    assert json.loads(fresh.read_text())["permissions"]["allow"] == ["mcp__srv"]


def test_moving_a_plugin_with_a_broken_settings_file_changes_nothing(tmp_path: Path) -> None:
    from axon.commands import marketplace as plugin_market
    from axon.commands import plugins as plugin_core
    from axon.marketplace import control, plugin_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    src = plugin_market.install_root(ws, "user") / "demo"
    src.mkdir(parents=True)
    (src / "plugin.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
    plugin = plugin_tools.installed_plugins(ws).get("demo")
    assert plugin is not None and plugin.scope == "user"
    broken = plugin_core.scope_settings_file(ws, "project")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{ not json", encoding="utf-8")
    result = control.move_plugin_scope(ws, "demo", "project")
    assert not result.ok and "will not overwrite" in result.message
    assert src.is_dir() and not (plugin_market.install_root(ws, "project") / "demo").exists()
    assert broken.read_text(encoding="utf-8") == "{ not json"


def test_status_check_probes_servers_the_live_session_does_not_have(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.marketplace import status
    from axon.marketplace.installer import HealthResult
    from axon.mcp.config import MCPConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    for name in ("live", "idle"):
        MCPConfig(ws).add_raw(name, {"command": "true"}, "user")
    live = SimpleNamespace(connected=True, tools=[1, 2], error="")
    monkeypatch.setattr(status, "live_connections", lambda: {"live": live})
    probed: list[str] = []

    def health(spec: ServerSpec, workspace: Path, timeout: float) -> HealthResult:
        probed.append(spec.name)
        return HealthResult("connected", tools=["a", "b", "c"])

    rows = {r.name: r for r in status.connector_rows(ws, True, 5, health)}
    assert probed == ["idle"] and rows["live"].tools == "2" and rows["idle"].connected == "yes" and rows["idle"].tools == "3"


def test_plugin_install_rolls_back_when_settings_cannot_be_updated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.commands import marketplace as plugin_market
    from axon.commands import plugins as plugin_core
    from axon.marketplace import catalog, plugin_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    entry = next(p for p in catalog.load_plugins() if p.kind == "git")

    def fake_fetch(e: Any, work: Path) -> Path:
        d = work / "src"
        d.mkdir()
        (d / "plugin.json").write_text(json.dumps({"name": e.name, "version": "1"}), encoding="utf-8")
        return d

    monkeypatch.setattr(plugin_tools, "_fetch", fake_fetch)
    settings = plugin_core.scope_settings_file(ws, "user")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ nope", encoding="utf-8")
    with pytest.raises(plugin_tools.PluginInstallError, match="will not overwrite"):
        plugin_tools.install_plugin(entry, ws, "user")
    assert not (plugin_market.install_root(ws, "user") / entry.name).exists()
    assert settings.read_text(encoding="utf-8") == "{ nope"


def test_mcp_get_masks_secret_header_and_env_values():
    from axon.mcp.commands import _show_value
    assert _show_value("Authorization", "Bearer abcdefghijklmnop") == "Bear…"
    assert _show_value("API_KEY", "sk-1234567890") == "sk-1…"
    assert _show_value("X-Api-Key", "short") == "…"
    assert _show_value("Authorization", "Bearer ${TOKEN}") == "Bearer ${TOKEN}"      # a reference, not a secret
    assert _show_value("Accept", "application/json") == "application/json"
