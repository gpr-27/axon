"""A custom endpoint's key lives under its own name, tied to its host, never in AgentRouter's AXON_API_KEY."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401

from axon.providers.catalog import custom_key_var, read_env_file  # noqa: E402


def test_key_name_is_tied_to_the_host():
    assert custom_key_var("https://api.example.com/v1") == "CUSTOM_API_EXAMPLE_COM_API_KEY"
    assert custom_key_var("http://10.0.0.5:8080/v1") == "CUSTOM_10_0_0_5_8080_API_KEY"
    assert custom_key_var("https://a.com") != custom_key_var("https://b.com")


def test_custom_key_is_not_saved_as_axon_api_key(c, env, llm):
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "m", "key": "sk-custom-key-aaaaaaaaaa"})
    assert st == 200, body
    saved = read_env_file()
    assert "AXON_API_KEY" not in saved
    assert saved[custom_key_var(llm.url)] == "sk-custom-key-aaaaaaaaaa"


def test_real_axon_api_key_is_untouched_and_not_sent_to_a_custom_host(c, env, llm):
    from axon.providers.catalog import save_api_key
    save_api_key("AXON_API_KEY", "sk-agentrouter-real-0000000000")
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "m", "key": "sk-custom-key-bbbbbbbbbb"})
    assert st == 200, body
    saved = read_env_file()
    assert saved["AXON_API_KEY"] == "sk-agentrouter-real-0000000000"
    assert saved[custom_key_var(llm.url)] == "sk-custom-key-bbbbbbbbbb"
    # applying the custom endpoint again with no key reuses its own key, not AXON_API_KEY
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "m"})
    assert st == 200, body
    assert c.srv.provider_choice["api_key"] == "sk-custom-key-bbbbbbbbbb"


def test_a_different_host_never_borrows_axon_api_key(c, env, llm):
    from axon.providers.catalog import save_api_key
    save_api_key("AXON_API_KEY", "sk-agentrouter-real-0000000000")
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": "http://127.0.0.2:9/v1", "model": "m"})
    assert st == 200, body
    assert c.srv.provider_choice["api_key"] != "sk-agentrouter-real-0000000000"
    assert custom_key_var("http://127.0.0.2:9/v1") not in read_env_file()


def test_an_endpoint_already_in_use_keeps_working_after_the_upgrade(c, env, llm):
    """Before: its key was saved as AXON_API_KEY. It is copied to the endpoint's own name; AXON_API_KEY stays."""
    from axon.providers.catalog import save_api_key
    save_api_key("AXON_API_KEY", "sk-legacy-key-dddddddddd")
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "m"})
    assert st == 200, body
    saved = read_env_file()
    assert saved[custom_key_var(llm.url)] == "sk-legacy-key-dddddddddd"
    assert saved["AXON_API_KEY"] == "sk-legacy-key-dddddddddd"


def test_startup_prefers_the_endpoints_own_key(monkeypatch, tmp_path):
    from axon.config import Settings
    from axon.providers.catalog import save_api_key
    url = "https://llm.example.org/v1"
    save_api_key("AXON_API_KEY", "sk-agentrouter-real-0000000000")
    save_api_key(custom_key_var(url), "sk-own-key-cccccccccc")
    s = Settings.load({"workspace": tmp_path, "base_url": url, "model": "x"}, require_api_key=False)
    assert s.api_key.get_secret_value() == "sk-own-key-cccccccccc"
