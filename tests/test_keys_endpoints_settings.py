"""/keys key manager, /endpoint redirection, /settings hub, `axon keys|endpoint` and the setup options.

No network: live verification (keystore.verify_fn / count_models) and endpoint probes
(endpoints.http_get) are replaced in every test that would touch them.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.providers import endpoints as ep
from axon.providers import keystore as ks
from axon.providers.catalog import PROVIDER_PRESETS, find_preset_by_url, get_preset_by_id

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

KEY_A = "sk-live-AAAAAAAAAAAAAAAAAAAA1111"
KEY_B = "sk-live-BBBBBBBBBBBBBBBBBBBB2222"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "axon_home"
    h.mkdir()
    monkeypatch.setenv("AXON_HOME", str(h))
    for p in PROVIDER_PRESETS:
        for var in p.env_vars:
            monkeypatch.setenv(var, "")
    for var in ("MY_GW_API_KEY", "MYGW_API_KEY", "CUSTOM_TOKEN_API_KEY"):
        monkeypatch.setenv(var, "")
    calls: list[tuple] = []
    monkeypatch.setattr(ks, "verify_fn", lambda preset, key, base_url=None, model=None: (calls.append((preset.id, key)) or (True, "OK")))
    monkeypatch.setattr(ks, "count_models", lambda *a, **k: 7)
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: pytest.fail("network call in a unit test"))
    ep.apply_endpoint_overrides({})
    yield h
    ep.apply_endpoint_overrides({})


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    w.mkdir()
    return w


def _agent(ws: Path, **kw):
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent
    from axon.config import Settings
    from axon.permissions.engine import PermissionEngine
    from axon.session.ledger import Ledger
    from axon.session.store import SessionStore
    from axon.tools.registry import create_default_registry
    settings = Settings(workspace=ws, **kw)
    return Agent(provider=None, tools=create_default_registry(), permissions=PermissionEngine(settings),
                 context=ContextManager(settings), session=SessionStore(ws), ledger=Ledger(), settings=settings)


# ─── keystore: files ───────────────────────────────────────────────────────

def test_set_env_value_replaces_in_place_and_preserves_comments(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# my keys\nexport OPENAI_API_KEY=old\nOTHER=1\n\n# trailing comment\nOPENAI_API_KEY=dupe\n", encoding="utf-8")
    ks.set_env_value(f, "OPENAI_API_KEY", KEY_A)
    lines = f.read_text(encoding="utf-8").splitlines()
    assert lines == ["# my keys", f'export OPENAI_API_KEY="{KEY_A}"', "OTHER=1", "", "# trailing comment"]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # atomic write left no temp file
    if os.name != "nt":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_set_env_value_appends_and_validates(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("A=1\n", encoding="utf-8")
    ks.set_env_value(f, "NEW_API_KEY", f"'{KEY_B}'")
    assert f.read_text(encoding="utf-8") == f'A=1\nNEW_API_KEY="{KEY_B}"\n'
    with pytest.raises(ValueError):
        ks.set_env_value(f, "BAD NAME", "x")
    with pytest.raises(ValueError):
        ks.set_env_value(f, "OK_NAME", "two\nlines")


def test_remove_env_value_keeps_other_lines(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# keep\nA_API_KEY=x\nB=2\nexport A_API_KEY=y\n", encoding="utf-8")
    assert ks.remove_env_value(f, "A_API_KEY") is True
    assert f.read_text(encoding="utf-8") == "# keep\nB=2\n"
    assert ks.remove_env_value(f, "A_API_KEY") is False


def test_mask_never_reveals_the_key() -> None:
    assert ks.mask_key("sk-abcdefghijklmnopqrstuvwx9f3c") == "sk-a…9f3c"
    assert ks.mask_key("short") == "••••"
    assert ks.mask_key("") == ""
    assert "abcdefgh" not in ks.mask_key("sk-abcdefghijklmnopqrstuvwx9f3c")


# ─── keystore: source detection ────────────────────────────────────────────

def test_source_detection_env_project_user_config(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / ".env").write_text(f'OPENAI_API_KEY="{KEY_A}"\n', encoding="utf-8")
    st = ks.key_status("OPENAI_API_KEY", ws)
    assert (st.source, st.value, st.also_in) == ("user", KEY_A, [])
    (ws / ".env").write_text(f"OPENAI_API_KEY={KEY_B}\n", encoding="utf-8")
    st = ks.key_status("OPENAI_API_KEY", ws)
    assert (st.source, st.value, st.also_in) == ("project", KEY_B, ["user"])  # project .env is loaded first
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-CCCCCCCCCCCCCCCCCC")
    assert ks.key_status("OPENAI_API_KEY", ws).source == "env"
    monkeypatch.setenv("OPENAI_API_KEY", KEY_A)  # value loaded from ~/.axon/.env into the process
    assert ks.key_status("OPENAI_API_KEY", ws).source == "user"
    (home / "config.toml").write_text(f'api_key = "{KEY_B}"\n', encoding="utf-8")
    st = ks.key_status("AXON_API_KEY", ws)
    assert st.source == "config" and st.value == KEY_B


def test_resolve_target_provider_or_variable() -> None:
    assert ks.resolve_target("openai")[1] == "OPENAI_API_KEY"
    assert ks.resolve_target("Google Gemini")[0].id == "gemini"
    assert ks.resolve_target("GOOGLE_API_KEY") == (get_preset_by_id("gemini"), "GOOGLE_API_KEY")
    assert ks.resolve_target("MY_TOKEN_KEY") == (None, "MY_TOKEN_KEY")
    assert ks.resolve_target("zzzz-nothing") is None


# ─── git-ignore ────────────────────────────────────────────────────────────

def _git_init(ws: Path) -> None:
    subprocess.run(["git", "init", "-q", str(ws)], check=True)


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_project_env_gets_git_ignored(ws: Path) -> None:
    _git_init(ws)
    assert ks.gitignore_state(ws) == "not-ignored"
    state, msg = ks.ensure_gitignored(ws)
    assert state == "ignored" and ".gitignore" in msg
    assert ".env" in (ws / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ks.gitignore_state(ws) == "ignored"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_tracked_project_env_is_reported(ws: Path) -> None:
    _git_init(ws)
    (ws / ".env").write_text("X=1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", ".env"], check=True)
    state, msg = ks.ensure_gitignored(ws)
    assert state == "tracked" and "git rm --cached" in msg


def test_no_repo_state(ws: Path) -> None:
    assert ks.gitignore_state(ws) == "no-repo"


# ─── redaction ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    (f"/keys set gemini {KEY_A}", "/keys set gemini [REDACTED]"),
    (f"/keys set openai {KEY_A} --project", "/keys set openai [REDACTED] --project"),
    (f"/key openai {KEY_A}", "/key openai [REDACTED]"),
    (f"/apikey set OPENAI_API_KEY={KEY_A}", "/apikey set OPENAI_API_KEY=[REDACTED]"),
    ("/keys set gemini", "/keys set gemini"),
    ("/keys test all", "/keys test all"),
    ("/keys", "/keys"),
    (f"/keysmith {KEY_A}", f"/keysmith {KEY_A}"),
    ("explain /keys please", "explain /keys please"),
])
def test_redact_command_line(line: str, expected: str) -> None:
    assert ks.redact_command_line(line) == expected


def test_keys_command_is_journaled_without_the_key(ws: Path) -> None:
    from axon.commands.builtin import dispatch_command
    from axon.session import activity
    agent = _agent(ws, base_url="https://api.openai.com/v1", model="gpt-4o")
    raw = "sk-zz" + "q7" * 12  # matches no secret pattern on its own
    dispatch_command(agent, f"/keys set openai {raw}")
    entries = activity.read(ws, kinds=["command"])
    assert entries, "the command was not journaled"
    text = repr(entries)
    assert raw not in text and "[REDACTED]" in text


# ─── /keys and `axon keys` ─────────────────────────────────────────────────

def test_keys_set_user_and_project_then_remove(home: Path, ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.keys_cmds import run_keys
    assert run_keys(f"set openai {KEY_A}", workspace=ws) == 0
    assert f'OPENAI_API_KEY="{KEY_A}"' in (home / ".env").read_text(encoding="utf-8")
    assert os.environ["OPENAI_API_KEY"] == KEY_A
    assert ks.check_state("OPENAI_API_KEY", KEY_A) == "verified"
    assert run_keys(f"set openai {KEY_B} --project", workspace=ws) == 0
    assert f'OPENAI_API_KEY="{KEY_B}"' in (ws / ".env").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert KEY_A not in out and KEY_B not in out and "sk-l…2222" in out
    # in both files: non-interactive remove needs a scope
    assert run_keys("remove openai", workspace=ws) == 2
    assert run_keys("remove openai --project", workspace=ws) == 0
    assert "OPENAI_API_KEY" not in (ws / ".env").read_text(encoding="utf-8")
    assert run_keys("remove OPENAI_API_KEY", workspace=ws) == 0
    assert "OPENAI_API_KEY" not in (home / ".env").read_text(encoding="utf-8")


def test_keys_set_rejected_key_is_not_saved(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.keys_cmds import run_keys
    monkeypatch.setattr(ks, "verify_fn", lambda *a, **k: (False, "invalid x-api-key (HTTP 401)"))
    assert run_keys(f"set anthropic {KEY_A}", workspace=ws) == 1
    assert not (home / ".env").exists()
    assert "HTTP 401" in capsys.readouterr().out
    assert run_keys(f"set anthropic {KEY_A} --no-verify", workspace=ws) == 0
    assert "ANTHROPIC_API_KEY" in (home / ".env").read_text(encoding="utf-8")


def test_keys_set_applies_to_running_session(ws: Path) -> None:
    from axon.commands.builtin import dispatch_command
    agent = _agent(ws, base_url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-2.5-flash")
    dispatch_command(agent, f"/keys set gemini {KEY_A}")
    assert agent.settings.api_key.get_secret_value() == KEY_A


def test_keys_legacy_form_still_works(home: Path, ws: Path) -> None:
    from axon.commands.keys_cmds import run_keys
    assert run_keys(f"groq {KEY_A}", workspace=ws) == 0
    assert "GROQ_API_KEY" in (home / ".env").read_text(encoding="utf-8")


def test_keys_list_test_where(home: Path, ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.keys_cmds import run_keys
    (home / ".env").write_text(f"# note\nDEEPSEEK_API_KEY={KEY_A}\n", encoding="utf-8")
    assert run_keys("", workspace=ws) == 0  # non-TTY: listing
    out = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY" in out and "sk-l…1111" in out and "~/.axon/.env" in out and "untested" in out
    assert KEY_A not in out
    assert run_keys("test deepseek", workspace=ws) == 0
    out = capsys.readouterr().out
    assert "7 models available" in out and " ms" in out
    assert run_keys("", workspace=ws) == 0
    assert "verified" in capsys.readouterr().out
    assert run_keys("where", workspace=ws) == 0
    out = capsys.readouterr().out
    assert str(home / ".env") in out and "project .env" in out and "shell environment" in out


def test_keys_test_reports_exact_error(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.keys_cmds import run_keys
    (home / ".env").write_text(f"MISTRAL_API_KEY={KEY_A}\n", encoding="utf-8")
    monkeypatch.setattr(ks, "verify_fn", lambda *a, **k: (False, "Unauthorized (HTTP 401)"))
    assert run_keys("test mistral", workspace=ws) == 1
    assert "Unauthorized (HTTP 401)" in capsys.readouterr().out
    assert ks.check_state("MISTRAL_API_KEY", KEY_A) == "failed"


def test_keys_edit_without_terminal_prints_path(home: Path, ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.keys_cmds import run_keys
    assert run_keys("edit", workspace=ws) == 0
    assert (home / ".env").exists() and str(home / ".env") in capsys.readouterr().out


def test_keys_cli_subcommand(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
    from axon.features import cli_subcommands
    subs = cli_subcommands()
    assert {"keys", "endpoint"} <= set(subs)
    monkeypatch.chdir(ws)
    assert subs["keys"](["set", "xai", KEY_A, "--project"]) == 0
    assert "XAI_API_KEY" in (ws / ".env").read_text(encoding="utf-8")
    assert subs["keys"](["list"]) == 0
    assert "sk-l…1111" in capsys.readouterr().out


# ─── endpoints ─────────────────────────────────────────────────────────────

class _Resp(SimpleNamespace):
    def json(self):
        return self.body


def test_endpoint_override_save_and_reset(home: Path) -> None:
    (home / "config.toml").write_text('model = "gpt-4o"\nbase_url = "https://api.openai.com/v1"\n', encoding="utf-8")
    change = ep.save_endpoint("openai", "https://proxy.example.com/v1/")
    assert change.new_url == "https://proxy.example.com/v1" and change.active_changed
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["endpoints"]["openai"]["base_url"] == "https://proxy.example.com/v1"
    assert data["base_url"] == "https://proxy.example.com/v1" and data["model"] == "gpt-4o"
    assert get_preset_by_id("openai").base_url == "https://proxy.example.com/v1"
    assert find_preset_by_url("https://proxy.example.com/v1").id == "openai"
    back = ep.reset_endpoint("openai")
    assert back is not None and back.new_url == "https://api.openai.com/v1"
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert "endpoints" not in data and data["base_url"] == "https://api.openai.com/v1"
    assert get_preset_by_id("openai").base_url == "https://api.openai.com/v1"


def test_custom_endpoint_becomes_a_provider(home: Path) -> None:
    ep.save_endpoint("my-gw", "https://llm.example.com/v1", env_var="MY_GW_API_KEY", model="qwen3-coder")
    p = get_preset_by_id("my-gw")
    assert p is not None and p.category == "Custom" and p.env_var == "MY_GW_API_KEY" and p.default_model == "qwen3-coder"
    assert find_preset_by_url("https://llm.example.com/v1").id == "my-gw"
    assert ks.resolve_target("MY_GW_API_KEY")[0].id == "my-gw"
    assert ep.reset_endpoint("my-gw") is not None
    assert get_preset_by_id("my-gw") is None


def test_settings_load_applies_user_endpoints_only(home: Path, ws: Path) -> None:
    from axon.config import Settings
    (home / "config.toml").write_text('[endpoints.groq]\nbase_url = "https://groq-proxy.example.com/v1"\n', encoding="utf-8")
    (ws / ".axon").mkdir()
    (ws / ".axon" / "config.toml").write_text('[endpoints.openai]\nbase_url = "https://evil.example.com/v1"\n', encoding="utf-8")
    Settings.load({"workspace": ws, "api_key": "sk-test-key-123"})
    assert get_preset_by_id("groq").base_url == "https://groq-proxy.example.com/v1"
    assert get_preset_by_id("openai").base_url == "https://api.openai.com/v1"  # project file cannot redirect


def test_endpoint_validation(home: Path) -> None:
    with pytest.raises(ValueError):
        ep.save_endpoint("openai", "not a url")
    with pytest.raises(ValueError):
        ep.save_endpoint("Bad Name!", "https://x.example.com")
    assert ep.url_warning("http://remote.example.com/v1")
    assert not ep.url_warning("http://localhost:8000/v1")


def test_endpoint_connectivity_test(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return _Resp(status_code=200, body={"data": [{"id": "a"}, {"id": "b"}]})

    monkeypatch.setattr(ep, "http_get", fake_get)
    res = ep.test_endpoint("https://llm.example.com/v1", KEY_A)
    assert res.ok and res.models == 2 and seen["url"] == "https://llm.example.com/v1/models"
    assert seen["headers"]["authorization"] == f"Bearer {KEY_A}"
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: _Resp(status_code=401, body={}))
    res = ep.test_endpoint("https://llm.example.com/v1", KEY_A)
    assert res.reachable and not res.ok and "rejected" in res.message

    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(ep, "http_get", boom)
    res = ep.test_endpoint("https://llm.example.com/v1")
    assert not res.reachable and "refused" in res.message


def test_endpoint_command_set_add_use_reset(home: Path, ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.builtin import dispatch_command
    agent = _agent(ws, base_url="https://api.openai.com/v1", model="gpt-4o", api_key="sk-test-key-123")
    dispatch_command(agent, "/endpoint set openai https://proxy.example.com/v1")
    assert agent.settings.base_url == "https://proxy.example.com/v1"  # the running session follows
    dispatch_command(agent, "/endpoint add mygw https://llm.example.com/v1 --model qwen3")
    out = capsys.readouterr().out
    assert "MYGW_API_KEY" in out and "/keys set MYGW_API_KEY" in out
    dispatch_command(agent, "/endpoint use mygw")
    assert agent.settings.provider == "mygw" and agent.settings.model == "qwen3"
    assert agent.settings.base_url == "https://llm.example.com/v1"
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["provider"] == "mygw" and data["base_url"] == "https://llm.example.com/v1"
    dispatch_command(agent, "/endpoint")
    out = capsys.readouterr().out
    assert "custom URL (default https://api.openai.com/v1)" in out and "custom · key MYGW_API_KEY" in out
    dispatch_command(agent, "/endpoint reset openai")
    assert get_preset_by_id("openai").base_url == "https://api.openai.com/v1"


def test_endpoint_cli_subcommand(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    from axon.features.credentials import endpoint_main
    monkeypatch.chdir(ws)
    assert endpoint_main(["set", "deepseek", "https://ds-proxy.example.com"]) == 0
    assert endpoint_main(["list"]) == 0
    assert "ds-proxy.example.com" in capsys.readouterr().out
    assert endpoint_main(["reset", "deepseek"]) == 0
    assert endpoint_main(["bogus-subcommand"]) == 2


# ─── /settings hub ─────────────────────────────────────────────────────────

def test_settings_listing_without_terminal(ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.builtin import dispatch_command
    from axon.commands.settings_cmds import SECTIONS
    agent = _agent(ws, api_key="sk-test-key-123")
    dispatch_command(agent, "/settings")
    out = capsys.readouterr().out
    for sec in SECTIONS:
        assert sec in out
    for label in ("API keys", "Endpoint", "Permission mode", "Trace level", "Auto-compact at", "Bash timeout",
                  "Reply to !commands", "Where things are stored"):
        assert label in out
    dispatch_command(agent, "/settings shell")
    out = capsys.readouterr().out
    assert "Bash timeout" in out and "Trace level" not in out


def test_settings_change_values_persist(home: Path, ws: Path) -> None:
    import json
    from axon.commands.builtin import dispatch_command
    agent = _agent(ws, api_key="sk-test-key-123")
    dispatch_command(agent, "/settings tips off")
    dispatch_command(agent, "/settings respond_to_bash_commands off")
    dispatch_command(agent, "/settings bash_timeout_s 240")
    dispatch_command(agent, "/settings budget_turn_usd 0.5")
    dispatch_command(agent, "/settings trace_level detailed")
    assert agent.settings.tips is False and agent.settings.respond_to_bash_commands is False
    assert agent.settings.bash_timeout_s == 240 and agent.settings.budget_turn_usd == 0.5
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["tips"] is False and data["respondToBashCommands"] is False and data["budget"]["turn_usd"] == 0.5
    assert data["traceLevel"] == "detailed"
    assert tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["bash_timeout_s"] == 240
    res = dispatch_command(agent, "/settings bash_timeout_s soon")
    assert res is not None and res.success is False and agent.settings.bash_timeout_s == 240


def test_settings_alias_and_config_coexist() -> None:
    from axon.commands.registry import resolve_builtin
    assert resolve_builtin("settings").name == "settings"
    assert resolve_builtin("config").name == "config"


# ─── axon setup options ────────────────────────────────────────────────────

def test_setup_custom_endpoint_and_project_env(home: Path, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.features import onboarding as ob
    monkeypatch.chdir(ws)
    monkeypatch.setenv("CORP_API_KEY", KEY_A)
    code, res = ob.run_setup(["--provider", "corp", "--base-url", "https://llm.corp.example.com/v1",
                              "--key-env", "CORP_API_KEY", "--model", "corp-coder", "--skip-verify"],
                             interactive=False, detect_local=lambda: {})
    assert code == 0 and res.provider == "corp" and res.base_url == "https://llm.corp.example.com/v1"
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["endpoints"]["corp"]["env_var"] == "CORP_API_KEY" and data["provider"] == "corp"


def test_setup_existing_key_test_then_replace_into_project_env(home: Path, ws: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.features import onboarding as ob

    class IO(ob.SetupIO):
        def __init__(self, answers, secrets):
            self.answers, self.secrets = list(answers), list(secrets)
            super().__init__(input_fn=lambda p: self.answers.pop(0), secret_fn=lambda p: self.secrets.pop(0),
                             use_selector=False)

    monkeypatch.setenv("OPENAI_API_KEY", KEY_A)
    checked = []

    def verify(preset, key, base_url=None, model=None):
        checked.append(key)
        return True, "OK"

    # provider, "test it first", then "replace", key location "project", model, mode, trace
    io = IO(["openai", "2", "3", "2", "gpt-5", "default", "normal"], [KEY_B])
    code, res = ob.run_setup([], io=io, verify_fn=verify, interactive=True, detect_local=lambda: {},
                             settings=SimpleNamespace(workspace=ws, mode="default", trace_level="normal"))
    assert code == 0 and res.api_key == KEY_B
    assert checked[0] == KEY_A and checked[-1] == KEY_B
    assert f'OPENAI_API_KEY="{KEY_B}"' in (ws / ".env").read_text(encoding="utf-8")
    assert not (home / ".env").exists()


def test_every_builtin_answers_dash_dash_help(ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.builtin import dispatch_command
    agent = _agent(ws, api_key="sk-test-key-123")
    for name in ("keys", "endpoint", "settings", "model", "compact"):
        res = dispatch_command(agent, f"/{name} --help")
        assert res is not None and res.handled
        out = capsys.readouterr().out
        assert f"/{name}" in out and ("/" + name) in out
    dispatch_command(agent, "/endpoint -h")
    assert "/endpoint set <provider> <url>" in capsys.readouterr().out
