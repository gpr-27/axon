"""
E2E-C: provider presets, routing and API-key discovery.

In-process checks cover every preset in providers/catalog.py (endpoint, wire, key env var); CLI
runs prove each provider's own key env var (XAI_API_KEY, GROQ_API_KEY, ...) is found and sent
without prompting, and that AgentRouter pricing sync never touches the network in tests.
"""
from __future__ import annotations

import json

import pytest

from harness import AxonEnv
from test_e2e_c_support import env_without_keys, ext_llm, no_traceback, python_snippet, run_raw, xenv  # noqa: F401

pytestmark = pytest.mark.e2e

from axon.providers.catalog import PROVIDER_PRESETS  # noqa: E402

PRESET_IDS = [p.id for p in PROVIDER_PRESETS]

# Official endpoint each preset must resolve to (documented provider URLs).
EXPECTED_ENDPOINT = {
    "agentrouter": "https://agentrouter.org/v1/chat/completions",
    "ollama": "http://localhost:11434/v1/chat/completions",
    "lmstudio": "http://localhost:1234/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "xai": "https://api.x.ai/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "together": "https://api.together.xyz/v1/chat/completions",
    "fireworks": "https://api.fireworks.ai/inference/v1/chat/completions",
    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
    "azure": None,  # needs AZURE_OPENAI_ENDPOINT; checked separately
}
EXPECTED_KEY_VAR = {
    "agentrouter": "AXON_API_KEY", "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "xai": "XAI_API_KEY", "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY", "cerebras": "CEREBRAS_API_KEY", "azure": "AZURE_OPENAI_API_KEY",
    "ollama": None, "lmstudio": None,
}


@pytest.fixture
def clean_keys(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No real API keys / ~/.axon/.env leak into in-process routing checks."""
    import os
    for k in list(os.environ):
        if k.endswith("_API_KEY") or k.startswith("AXON_") or k.startswith("AZURE_OPENAI"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "axon-home"))
    monkeypatch.setenv("AXON_OFFLINE", "1")
    return monkeypatch


def _settings(**kw):
    from pydantic import SecretStr

    from axon.config import Settings
    kw.setdefault("api_key", SecretStr(""))
    return Settings(**kw)


def test_catalog_has_every_expected_preset() -> None:
    """The catalog exposes exactly the providers this suite knows the endpoints for."""
    assert set(PRESET_IDS) == set(EXPECTED_ENDPOINT), sorted(set(PRESET_IDS) ^ set(EXPECTED_ENDPOINT))


@pytest.mark.parametrize("pid", PRESET_IDS)
def test_preset_resolves_endpoint_wire_and_key_env_var(pid: str, clean_keys) -> None:
    """resolve_route(provider=<id>) -> the preset's official endpoint, wire format and its own key env var."""
    from axon.providers.catalog import get_preset_by_id
    from axon.providers.registry import resolve_route
    from axon.providers.transport import anthropic_messages_url, chat_completions_url
    preset = get_preset_by_id(pid)
    assert preset is not None
    var = EXPECTED_KEY_VAR[pid]
    assert (preset.env_var or None) == var
    if var:
        clean_keys.setenv(var, f"key-for-{pid}")
    if pid == "azure":
        clean_keys.setenv("AZURE_OPENAI_ENDPOINT", "https://e2e-res.openai.azure.com")
    route = resolve_route(preset.default_model, _settings(provider=pid))
    assert route.preset is preset
    assert route.api_format == preset.api_format
    url = anthropic_messages_url(route.base_url) if route.api_format == "anthropic" else chat_completions_url(route.base_url)
    if pid == "azure":
        assert route.base_url == "https://e2e-res.openai.azure.com"
    else:
        assert url == EXPECTED_ENDPOINT[pid], (pid, route.base_url)
    assert route.api_key == (f"key-for-{pid}" if var else "")


@pytest.mark.parametrize("pid", [p for p in PRESET_IDS if p != "azure"])
def test_preset_base_url_maps_back_to_preset(pid: str) -> None:
    """find_preset_by_url(preset.base_url) identifies the same preset (used for pricing + key choice)."""
    from axon.providers.catalog import find_preset_by_url, get_preset_by_id
    preset = get_preset_by_id(pid)
    assert find_preset_by_url(preset.base_url) is preset


def test_key_for_one_vendor_never_sent_to_another(clean_keys) -> None:
    """An OpenAI key configured for api.openai.com is not reused when routing to xAI without XAI_API_KEY."""
    from pydantic import SecretStr

    from axon.providers.registry import resolve_route
    s = _settings(provider="xai", base_url="https://api.openai.com/v1", api_key=SecretStr("sk-openai-secret"))
    route = resolve_route("grok-4", s)
    assert route.preset.id == "xai" and route.api_key == ""


def test_key_discovered_from_axon_home_dotenv(clean_keys, tmp_path) -> None:
    """GROQ_API_KEY saved only in $AXON_HOME/.env is picked up by routing."""
    from axon.providers.registry import resolve_route
    home = tmp_path / "axon-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text('export GROQ_API_KEY="gsk-from-dotenv"\n', encoding="utf-8")
    route = resolve_route("llama-3.3-70b-versatile", _settings(provider="groq"))
    assert route.api_key == "gsk-from-dotenv"


def test_model_family_routes_without_explicit_provider(clean_keys) -> None:
    """With no provider set, bare model families route to their vendor presets."""
    from axon.providers.catalog import find_preset_for_model
    cases = {"claude-sonnet-4-6": "anthropic", "gpt-4.1": "openai", "grok-4": "xai", "gemini-2.5-pro": "gemini",
             "deepseek-chat": "deepseek", "mistral-large-latest": "mistral", "qwen2.5-coder:7b": "ollama",
             "meta-llama/llama-3.3-70b-instruct": "openrouter"}
    got = {m: (find_preset_for_model(m).id if find_preset_for_model(m) else None) for m in cases}
    assert got == cases


# ─── CLI: per-provider key env vars are found without prompting ─────────────

_CLI_KEY_CASES = [p.id for p in PROVIDER_PRESETS if p.env_vars]


@pytest.mark.parametrize("pid", _CLI_KEY_CASES)
def test_cli_finds_provider_key_env_var_and_sends_it(pid: str, xenv: AxonEnv) -> None:
    """`axon -p` with only <PROVIDER>_API_KEY set (no AXON_API_KEY) authenticates with that key, no prompt."""
    from axon.providers.catalog import get_preset_by_id
    preset = get_preset_by_id(pid)
    var = preset.env_vars[-1]  # the provider-specific name (AGENTROUTER_API_KEY, GOOGLE_API_KEY, ...)
    key = f"{pid}-secret-123"
    extra = {"AXON_PROVIDER": pid, "AXON_MODEL": preset.default_model, "AXON_BASE_URL": xenv.llm.url, var: key}
    if pid == "azure":
        extra["AZURE_OPENAI_ENDPOINT"] = xenv.llm.url
    xenv.llm.enqueue([{"text": f"hello from {pid}"}])
    r = run_raw(env_without_keys(xenv, **extra), ["-p", "--output-format", "json", "hi"], xenv.workspace, timeout=40)
    assert r.code == 0, str(r)
    assert r.json()["result"].strip() == f"hello from {pid}"
    req = xenv.llm.completion_requests()[0]
    h = req.headers
    if preset.auth_style == "api-key":
        assert h.get("api-key") == key, h
    elif preset.api_format == "anthropic":
        assert key in (h.get("x-api-key"), (h.get("authorization") or "").removeprefix("Bearer "))
    else:
        assert h.get("authorization") == f"Bearer {key}", h
    assert req.wire == preset.api_format


def test_cli_local_preset_needs_no_key(xenv: AxonEnv) -> None:
    """--provider ollama with no key anywhere runs and sends no Authorization header."""
    xenv.llm.enqueue([{"text": "local ok"}])
    r = run_raw(env_without_keys(xenv, AXON_PROVIDER="ollama", AXON_MODEL="qwen2.5-coder:7b"), ["-p", "hi"], xenv.workspace)
    assert r.code == 0, str(r)
    assert "authorization" not in xenv.llm.completion_requests()[0].headers


def test_cli_missing_key_for_cloud_provider_fails_fast_without_prompt(xenv: AxonEnv) -> None:
    """No key for a cloud endpoint + non-TTY stdin: exit 2 with a friendly message, no request, no hang."""
    env = env_without_keys(xenv, AXON_PROVIDER="xai", AXON_MODEL="grok-4")
    env.pop("AXON_BASE_URL", None)
    r = run_raw(env, ["-p", "hi"], xenv.workspace, timeout=30)
    assert r.code == 2, str(r)
    no_traceback(r)
    assert "API key" in r.output and ("axon setup" in r.output or "AXON_API_KEY" in r.output)
    assert xenv.llm.requests == []


def test_cli_key_from_axon_home_dotenv_file(xenv: AxonEnv) -> None:
    """XAI_API_KEY only present in $AXON_HOME/.env is used by the CLI."""
    (xenv.axon_home / ".env").write_text("XAI_API_KEY=xai-from-file\n", encoding="utf-8")
    xenv.llm.enqueue([{"text": "file key ok"}])
    r = run_raw(env_without_keys(xenv, AXON_PROVIDER="xai", AXON_MODEL="grok-4"), ["-p", "hi"], xenv.workspace)
    assert r.code == 0, str(r)
    assert xenv.llm.completion_requests()[0].headers.get("authorization") == "Bearer xai-from-file"


def test_cli_provider_flag_overrides_env_provider(xenv: AxonEnv) -> None:
    """--provider anthropic beats AXON_PROVIDER=openai: the Anthropic wire is used."""
    xenv.llm.enqueue([{"text": "flag wins"}])
    r = xenv.run(["-p", "--provider", "anthropic", "--model", "claude-haiku-4-5", "hi"], env={"AXON_PROVIDER": "openai"})
    assert r.code == 0, str(r)
    assert xenv.llm.completion_requests()[0].wire == "anthropic"


# ─── AgentRouter pricing sync stays offline ─────────────────────────────────

def test_agentrouter_route_does_not_fetch_pricing_over_network(xenv: AxonEnv) -> None:
    """A run routed to the agentrouter preset makes no /api/pricing request (AXON_OFFLINE)."""
    xenv.llm.enqueue([{"text": "ar"}])
    r = xenv.run(["-p", "hi"], env={"AXON_PROVIDER": "agentrouter", "AXON_MODEL": "deepseek-v4-flash"})
    assert r.code == 0, str(r)
    assert [q.path for q in xenv.llm.requests if "pricing" in q.path] == []
    assert len(xenv.llm.requests) == 1


def test_agentrouter_pricing_sync_is_a_noop_when_offline(xenv: AxonEnv) -> None:
    """sync_agentrouter_pricing(agentrouter.org) returns False and never opens a URL under AXON_OFFLINE."""
    code = (
        "import urllib.request, json\n"
        "calls=[]\n"
        "def boom(*a, **k):\n"
        "    calls.append(a); raise AssertionError('network used')\n"
        "urllib.request.urlopen = boom\n"
        "from axon.providers import pricing\n"
        "ok = pricing.sync_agentrouter_pricing('https://agentrouter.org')\n"
        "pricing.sync_agentrouter_pricing_async('https://agentrouter.org')\n"
        "import threading; [t.join(2) for t in threading.enumerate() if t.name=='axon-pricing-sync']\n"
        "print(json.dumps({'ok': ok, 'calls': len(calls), 'synced': pricing.is_synced()}))\n"
    )
    p = python_snippet(xenv, code)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1]) == {"ok": False, "calls": 0, "synced": False}


def test_agentrouter_pricing_sync_would_fetch_when_online(xenv: AxonEnv) -> None:
    """Control: without AXON_OFFLINE (and outside pytest) the sync does try the AgentRouter feed."""
    code = (
        "import urllib.request, json, io\n"
        "calls=[]\n"
        "class R(io.BytesIO):\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self,*a): pass\n"
        "def fake(req, timeout=0):\n"
        "    calls.append(req.full_url)\n"
        "    return R(json.dumps({'data':[{'model_name':'e2e-new-model','model_ratio':1.5,'completion_ratio':4}]}).encode())\n"
        "urllib.request.urlopen = fake\n"
        "from axon.providers import pricing\n"
        "ok = pricing.sync_agentrouter_pricing('https://agentrouter.org')\n"
        "q = pricing.price_for('e2e-new-model', 'agentrouter')\n"
        "print(json.dumps({'ok': ok, 'calls': calls, 'in': float(q.input), 'out': float(q.output)}))\n"
    )
    env = {"AXON_OFFLINE": ""}
    p = python_snippet(xenv, code, extra_env=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout.strip().splitlines()[-1])
    assert data == {"ok": True, "calls": ["https://agentrouter.org/api/pricing"], "in": 3.0, "out": 12.0}, data
