"""Tests for axon.features.onboarding (axon setup / /setup / needs_setup)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.features import onboarding as ob
from axon.providers.catalog import PROVIDER_PRESETS

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


@pytest.fixture(autouse=True)
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon_home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    # Blank every provider key so the machine's real keys never leak in (and are restored after).
    for p in PROVIDER_PRESETS:
        for var in p.env_vars:
            monkeypatch.setenv(var, "")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "")
    return home


class FakeIO(ob.SetupIO):
    def __init__(self, answers: list[str], secrets: list[str] | None = None) -> None:
        self.answers = list(answers)
        self.secrets = list(secrets or [])
        super().__init__(input_fn=self._input, secret_fn=self._secret, use_selector=False)

    def _input(self, prompt: str) -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def _secret(self, prompt: str) -> str:
        if not self.secrets:
            raise EOFError
        return self.secrets.pop(0)


def _settings(**kw):
    from pydantic import SecretStr
    base = {"base_url": "https://api.anthropic.com", "provider": "anthropic", "api_key": SecretStr(""),
            "mode": "default", "trace_level": "normal", "workspace": Path.cwd(), "model": "m"}
    base.update(kw)
    return SimpleNamespace(**base)


# ── needs_setup ────────────────────────────────────────────────────────────

def test_needs_setup_false_with_key_in_settings() -> None:
    from pydantic import SecretStr
    assert ob.needs_setup(_settings(api_key=SecretStr("sk-ant-real"))) is False


def test_needs_setup_false_for_local_endpoint() -> None:
    assert ob.needs_setup(_settings(base_url="http://localhost:11434/v1", provider="ollama")) is False


def test_needs_setup_uses_provider_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(base_url="https://api.x.ai/v1", provider="xai")
    assert ob.needs_setup(s) is True
    monkeypatch.setenv("XAI_API_KEY", "xai-123")
    assert ob.needs_setup(s) is False


def test_needs_setup_reads_dotenv_file(axon_home: Path) -> None:
    s = _settings()
    assert ob.needs_setup(s) is True
    (axon_home / ".env").write_text('ANTHROPIC_API_KEY="sk-ant-file"\n', encoding="utf-8")
    assert ob.needs_setup(s) is False


def test_needs_setup_placeholder_key_counts_as_missing() -> None:
    from pydantic import SecretStr
    assert ob.needs_setup(_settings(api_key=SecretStr("your_api_key_here"))) is True


# ── detection ──────────────────────────────────────────────────────────────

def test_detect_env_keys_marks_env_and_file(monkeypatch: pytest.MonkeyPatch, axon_home: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    (axon_home / ".env").write_text("GROQ_API_KEY=gsk_x\n", encoding="utf-8")
    keys = ob.detect_env_keys()
    assert keys["openai"] == ("OPENAI_API_KEY", "env")
    assert keys["groq"] == ("GROQ_API_KEY", ".env")
    assert "anthropic" not in keys


def test_detect_local_providers_skips_closed_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ob, "_port_open", lambda port, host="127.0.0.1", timeout=0.3: port == 11434)
    import axon.providers.catalog as cat
    monkeypatch.setattr(cat, "fetch_local_ollama_models", lambda timeout=0.6: ["qwen2.5-coder:7b"])
    monkeypatch.setattr(cat, "fetch_local_lmstudio_models", lambda timeout=0.6: pytest.fail("port closed"))
    assert ob.detect_local_providers() == {"ollama": ["qwen2.5-coder:7b"]}


# ── config writing ─────────────────────────────────────────────────────────

def test_write_user_config_preserves_other_keys(axon_home: Path) -> None:
    cfg = axon_home / "config.toml"
    cfg.write_text('model = "old"\nmax_iterations = 7\n\n[permissions]\nallow = ["Read"]\n', encoding="utf-8")
    ob.write_user_config({"model": "new", "mode": "plan"})
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["model"] == "new" and data["mode"] == "plan"
    assert data["max_iterations"] == 7 and data["permissions"] == {"allow": ["Read"]}


def test_write_user_config_refuses_malformed_file(axon_home: Path) -> None:
    cfg = axon_home / "config.toml"
    cfg.write_text("model = = broken", encoding="utf-8")
    with pytest.raises(ValueError):
        ob.write_user_config({"model": "x"})
    assert cfg.read_text(encoding="utf-8") == "model = = broken"


# ── wizard ─────────────────────────────────────────────────────────────────

def test_interactive_wizard_full_flow(axon_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls = []

    def verify(preset, key, base_url=None, model=None):
        calls.append((preset.id, key, base_url, model))
        return True, "OK"

    io = FakeIO(answers=["anthropic", "1", "claude-sonnet-4-5", "default", "detailed"], secrets=["sk-ant-secret"])
    code, res = ob.run_setup([], io=io, verify_fn=verify, interactive=True, detect_local=lambda: {})
    assert code == 0 and res is not None
    assert calls and calls[0][0] == "anthropic" and calls[0][1] == "sk-ant-secret"
    data = tomllib.loads((axon_home / "config.toml").read_text(encoding="utf-8"))
    assert data == {"provider": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5",
                    "mode": "default", "trace_level": "detailed"}
    assert 'ANTHROPIC_API_KEY="sk-ant-secret"' in (axon_home / ".env").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "Try these" in out and "Key works" in out and "ms round trip" in out


def test_wizard_uses_existing_env_key_without_saving(monkeypatch: pytest.MonkeyPatch, axon_home: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    io = FakeIO(answers=["openai", "1", "gpt-5", "acceptEdits", "normal"])
    code, res = ob.run_setup([], io=io, verify_fn=lambda *a, **k: (True, "OK"), interactive=True,
                             detect_local=lambda: {})
    assert code == 0 and res.api_key == "sk-openai-env" and res.key_saved_to is None
    assert not (axon_home / ".env").exists()


def test_wizard_failed_verification_then_cancel_changes_nothing(axon_home: Path) -> None:
    io = FakeIO(answers=["anthropic", "3"], secrets=["sk-bad"])
    code, res = ob.run_setup([], io=io, verify_fn=lambda *a, **k: (False, "invalid x-api-key (HTTP 401)"),
                             interactive=True, detect_local=lambda: {})
    assert code == 130 and res is None
    assert not (axon_home / "config.toml").exists() and not (axon_home / ".env").exists()


def test_wizard_retry_with_new_key(axon_home: Path) -> None:
    results = iter([(False, "HTTP 401"), (True, "OK")])
    io = FakeIO(answers=["deepseek", "1", "1", "deepseek-chat", "default", "normal"], secrets=["bad", "good-key"])
    code, res = ob.run_setup([], io=io, verify_fn=lambda *a, **k: next(results), interactive=True,
                             detect_local=lambda: {})
    assert code == 0 and res.verified and res.api_key == "good-key"
    assert 'DEEPSEEK_API_KEY="good-key"' in (axon_home / ".env").read_text(encoding="utf-8")


def test_non_tty_without_provider_prints_instructions(capsys: pytest.CaptureFixture[str]) -> None:
    code, res = ob.run_setup([], interactive=False)
    assert code == 1 and res is None
    err = capsys.readouterr().err
    assert "config.toml" in err and "axon setup --provider" in err


def test_scripted_setup_for_local_provider(axon_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ob, "_port_open", lambda *a, **k: False)
    code, res = ob.run_setup(["--provider", "ollama", "--model", "llama3.1:8b", "--mode", "plan", "--trace", "compact"],
                             interactive=False, detect_local=lambda: {})
    assert code == 0
    data = tomllib.loads((axon_home / "config.toml").read_text(encoding="utf-8"))
    assert data["base_url"] == "http://localhost:11434/v1" and data["model"] == "llama3.1:8b"
    assert data["mode"] == "plan" and data["trace_level"] == "compact"


def test_scripted_setup_requires_key_for_cloud_provider(axon_home: Path) -> None:
    code, _ = ob.run_setup(["--provider", "anthropic"], interactive=False, detect_local=lambda: {})
    assert code == 2 and not (axon_home / "config.toml").exists()


def test_unknown_provider_flag() -> None:
    code, _ = ob.run_setup(["--provider", "nope"], interactive=False, detect_local=lambda: {})
    assert code == 2


def test_feature_contract_exports() -> None:
    assert "setup" in ob.CLI_SUBCOMMANDS
    assert any(c.name == "/setup" for c in ob.COMMANDS)
