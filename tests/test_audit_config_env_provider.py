"""An AXON_BASE_URL from the environment must not be overridden by a provider saved in config.toml."""
from pathlib import Path

from axon.config import Settings
from axon.storage import axon_home


def _write_config(text: str) -> None:
    home = axon_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_env_base_url_of_a_known_provider_switches_to_it(tmp_path: Path, monkeypatch):
    _write_config('provider = "anthropic"\nbase_url = "https://api.anthropic.com"\n')
    monkeypatch.setenv("AXON_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("AXON_PROVIDER", raising=False)
    settings = Settings.load({"workspace": tmp_path}, require_api_key=False)
    assert settings.base_url == "http://localhost:11434/v1"
    assert settings.provider == "ollama"


def test_env_base_url_of_an_unknown_proxy_keeps_saved_provider(tmp_path: Path, monkeypatch):
    _write_config('provider = "xai"\n')
    monkeypatch.setenv("AXON_BASE_URL", "https://my-proxy.example.com/v1")
    monkeypatch.delenv("AXON_PROVIDER", raising=False)
    settings = Settings.load({"workspace": tmp_path}, require_api_key=False)
    assert settings.provider == "xai"


def test_explicit_provider_survives_env_base_url(tmp_path: Path, monkeypatch):
    _write_config('provider = "anthropic"\n')
    monkeypatch.setenv("AXON_BASE_URL", "http://localhost:11434/v1")
    settings = Settings.load({"workspace": tmp_path, "provider": "openai"}, require_api_key=False)
    assert settings.provider == "openai"
