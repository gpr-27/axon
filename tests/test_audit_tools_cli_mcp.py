import argparse

from axon.cli import _overrides_from_args, _provider_override
from axon.mcp.transports import StdioTransport


def test_base_url_alone_switches_only_to_a_known_provider():
    assert _provider_override(None, None) is None
    assert _provider_override("openai", "http://x") == "openai"
    assert _provider_override(None, "http://localhost:11434/v1") == "ollama"
    assert _provider_override(None, "https://my.custom.example/v1") is None


def test_overrides_use_provider_override():
    ns = argparse.Namespace(model=None, provider=None, base_url="https://my.custom.example/v1", mode=None,
                            effort=None, workspace=None, no_thinking=False, dangerously_skip_permissions=False,
                            max_turns=None, append_system_prompt=None, verbose=False, trace=None,
                            max_budget_usd=None, settings_file=None)
    o = _overrides_from_args(ns)
    assert o["provider"] is None and o["base_url"] == "https://my.custom.example/v1"


def test_stdio_start_failure_closes_log(tmp_path):
    t = StdioTransport("x", "definitely-not-a-real-binary-xyz", [])
    t.log_path = tmp_path / "x.log"
    try:
        t.start(lambda m: None, lambda r: None)
    except Exception:
        pass
    assert t._log_file is None
