"""
Shared HTTP plumbing for every provider: endpoint URL normalization, request headers,
retry/backoff policy (Retry-After aware), effort mapping and status-based error classification.

Both provider implementations and the key verifier import from here so the rules live in one place.
"""
from __future__ import annotations

import email.utils
import json
import random
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from axon.errors import ProviderError

# ─── Hosts ──────────────────────────────────────────────────────────────────
AGENTROUTER_HOSTS = ("agentrouter.org", "air-outer.com")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal")

# AgentRouter only serves requests from recognised coding clients, so requests to it carry the
# same client fingerprint the official SDK sends. Every other host gets an honest user agent.
_AGENTROUTER_FINGERPRINT = {
    "user-agent": "Anthropic/Python 1.0.0",
    "x-stainless-lang": "python",
    "x-stainless-os": "MacOS",
    "x-stainless-arch": "arm64",
    "x-stainless-runtime": "CPython",
}


def _axon_version() -> str:
    try:
        from axon import __version__
        return str(__version__)
    except Exception:
        return "dev"


def host_of(url: str | None) -> str:
    """Lower-cased host name of a URL ('' if it cannot be parsed)."""
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""


def port_of(url: str | None) -> int | None:
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return urlsplit(raw).port
    except ValueError:
        return None


def is_agentrouter(url: str | None) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in AGENTROUTER_HOSTS)


LOCAL_INFERENCE_PORTS = frozenset({11434, 1234})  # Ollama, LM Studio


def is_local_inference_url(url: str | None) -> bool:
    """A loopback URL on a well-known free local inference port (Ollama / LM Studio). Plain
    localhost proxies in front of paid APIs are *not* treated as free."""
    if not is_local_url(url):
        return False
    try:
        from urllib.parse import urlparse
        return urlparse(str(url)).port in LOCAL_INFERENCE_PORTS
    except Exception:
        return False


def is_local_url(url: str | None) -> bool:
    """True for loopback / LAN inference servers (Ollama, LM Studio, llama.cpp, vLLM on localhost)."""
    h = host_of(url)
    if not h:
        return False
    if h in LOCAL_HOSTS or h.endswith(".local") or h.endswith(".localhost"):
        return True
    return h.startswith("127.") or h.startswith("192.168.") or h.startswith("10.")


def is_azure(url: str | None) -> bool:
    h = host_of(url)
    return h.endswith(".openai.azure.com") or h.endswith(".cognitiveservices.azure.com") or h.endswith(".services.ai.azure.com")


def is_official_anthropic(url: str | None) -> bool:
    return host_of(url) == "api.anthropic.com"


def is_official_openai(url: str | None) -> bool:
    return host_of(url) == "api.openai.com"


def default_headers(base_url: str) -> dict[str, str]:
    headers = {"content-type": "application/json", "accept": "text/event-stream, application/json"}
    if is_agentrouter(base_url):
        headers.update(_AGENTROUTER_FINGERPRINT)
    else:
        headers["user-agent"] = f"axon/{_axon_version()}"
    return headers


# ─── Endpoint URLs ──────────────────────────────────────────────────────────
_VERSION_SEGMENT = re.compile(r"/v\d+(?:alpha|beta)?\d*$", re.IGNORECASE)


def _api_root(base_url: str) -> str:
    """Return the OpenAI-compatible API root (the path that `/chat/completions` hangs off)."""
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/completions"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    h = host_of(base)
    if h == "generativelanguage.googleapis.com":
        if not base.endswith("/openai"):
            base = base.removesuffix("/v1beta").removesuffix("/v1") + "/v1beta/openai"
        return base
    if h == "openrouter.ai":
        return base.removesuffix("/v1").removesuffix("/api") + "/api/v1"
    if h == "api.groq.com":
        return base.removesuffix("/v1").removesuffix("/openai") + "/openai/v1"
    if h == "api.fireworks.ai":
        return base.removesuffix("/v1").removesuffix("/inference") + "/inference/v1"
    if base.endswith("/openai/v1") or _VERSION_SEGMENT.search(base):
        return base
    return f"{base}/v1"


def chat_completions_url(base_url: str) -> str:
    return f"{_api_root(base_url)}/chat/completions"


def responses_url(base_url: str) -> str:
    return f"{_api_root(base_url)}/responses"


def anthropic_messages_url(base_url: str) -> str:
    base = (base_url or "https://api.anthropic.com").strip().rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


AZURE_DEFAULT_API_VERSION = "2024-10-21"


def azure_chat_url(endpoint: str, deployment: str, api_version: str | None = None) -> str:
    """Azure OpenAI chat URL. Supports both the classic deployment route and the /openai/v1 route."""
    base = endpoint.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/openai/v1"):
        return f"{base}/chat/completions"
    base = base.split("/openai/")[0].removesuffix("/openai")
    ver = api_version or AZURE_DEFAULT_API_VERSION
    return f"{base}/openai/deployments/{deployment}/chat/completions?api-version={ver}"


# ─── Retry policy ───────────────────────────────────────────────────────────
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})
MAX_RETRY_AFTER_S = 60.0

# Indirection so tests can neutralise real sleeping.
_sleep = time.sleep


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        val = headers.get(name)
    except Exception:
        return None
    if isinstance(val, bytes):
        val = val.decode("latin-1", "ignore")
    if isinstance(val, (int, float)):
        return str(val)
    return val if isinstance(val, str) else None


def parse_retry_after(headers: Any) -> float | None:
    """Seconds to wait according to retry-after-ms / retry-after (seconds or HTTP-date)."""
    ms = _header(headers, "retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000.0)
        except ValueError:
            pass
    ra = _header(headers, "retry-after")
    if not ra:
        return None
    ra = ra.strip()
    try:
        return max(0.0, float(ra))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(ra)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


def backoff_delay(attempt: int, retry_after: float | None = None, *, base: float = 0.5, cap: float = 20.0) -> float:
    """Exponential backoff with jitter; a server-provided Retry-After wins (bounded)."""
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER_S)
    exp = min(cap, base * (2 ** max(0, attempt)))
    return exp * (0.5 + random.random() * 0.5)


# Gateways (AgentRouter in particular) report an upstream "invalid request" rejection as HTTP 5xx
# ("Upstream rejected the request as invalid"). When the request really is malformed, retrying the
# identical body fails every time, so such answers get a reduced retry budget instead of the full one.
# They are still retried a little because AgentRouter also returns that exact wording intermittently
# for valid requests (it spreads traffic over several upstream channels).
MAX_INVALID_5XX_RETRIES = 2

_INVALID_REQUEST_MARKERS = (
    "rejected the request as invalid",
    "invalid_request_error",
    "invalid request",
    "must be passed back",
)


def is_invalid_request_body(body: Any) -> bool:
    """True when an error body says the *request* was invalid (as opposed to a server fault)."""
    if body is None:
        return False
    raw = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
    if not isinstance(raw, str):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            raw = str(raw)
    text = raw.lower()
    return any(m in text for m in _INVALID_REQUEST_MARKERS)


def should_retry_status(status: int | None, body: Any, attempt: int) -> bool:
    """Whether a non-2xx answer is worth retrying. *attempt* is the 0-based index of the failed try."""
    if status not in RETRYABLE_STATUS:
        return False
    if status is not None and status >= 500 and is_invalid_request_body(body):
        return attempt < MAX_INVALID_5XX_RETRIES
    return True


def max_retries_for(settings: Any, default: int = 3) -> int:
    val = getattr(settings, "max_retries", None)
    try:
        return max(0, int(val)) if val is not None else default
    except (TypeError, ValueError):
        return default


def request_timeout_for(settings: Any, default: float = 600.0) -> float:
    val = getattr(settings, "request_timeout", None) or getattr(settings, "timeout", None)
    try:
        return float(val) if val else default
    except (TypeError, ValueError):
        return default


# ─── Effort mapping ─────────────────────────────────────────────────────────
_EFFORT_CANON = {
    "minimal": "low", "reflex": "low", "low": "low",
    "balanced": "medium", "medium": "medium",
    "synapse": "high", "high": "high",
    "quantum": "xhigh", "xhigh": "xhigh", "max": "xhigh", "hyper": "xhigh",
}


def canonical_effort(effort: str | None) -> str | None:
    """Map Axon effort names (reflex/balanced/synapse/quantum) onto low/medium/high/xhigh."""
    if not effort:
        return None
    return _EFFORT_CANON.get(str(effort).strip().lower())


def openai_reasoning_effort(effort: str | None) -> str | None:
    """OpenAI-style reasoning_effort accepts low/medium/high; xhigh collapses to high."""
    c = canonical_effort(effort)
    if c is None:
        return None
    return "high" if c == "xhigh" else c


# ─── Error classification ───────────────────────────────────────────────────
def extract_error_detail(body: Any) -> str:
    """Pull the human-readable message out of a JSON or text error body."""
    if body is None:
        return ""
    data = body
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        txt = body.strip()
        try:
            data = json.loads(txt)
        except (ValueError, TypeError):
            return txt[:500]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or err.get("code") or err.get("type")
            if msg:
                return str(msg)[:500]
        elif isinstance(err, str):
            return err[:500]
        if data.get("message"):
            return str(data["message"])[:500]
    if isinstance(data, list) and data:
        return extract_error_detail(data[0])
    return str(data)[:500]


def _provider_label(base_url: str) -> str:
    try:
        from axon.providers.catalog import find_preset_by_url
        p = find_preset_by_url(base_url)
        if p:
            return p.name
    except Exception:
        pass
    return host_of(base_url) or base_url or "provider"


def _key_hint(base_url: str) -> str:
    try:
        from axon.providers.catalog import find_preset_by_url
        p = find_preset_by_url(base_url)
        if p and p.env_vars:
            return " or ".join(p.env_vars)
    except Exception:
        pass
    return "AXON_API_KEY"


_STATUS_IN_TEXT = re.compile(r"(?:\bHTTP\s*|^|\b(?:status|code)\s*[:=]?\s*)([1-5]\d\d)\b", re.IGNORECASE)


def status_from_text(text: str) -> int | None:
    m = _STATUS_IN_TEXT.search(text or "")
    return int(m.group(1)) if m else None


def classify_error(
    *,
    status: int | None,
    body: Any = None,
    base_url: str = "",
    model: str = "",
    url: str = "",
    exc: BaseException | None = None,
) -> ProviderError:
    """Build a ProviderError with an actionable message, keyed on the HTTP status code.

    Provider-specific body markers (AgentRouter quota / language / content filters) take priority
    because those gateways reuse generic status codes for them.
    """
    detail = extract_error_detail(body)
    text = f"{detail} {exc or ''}".lower()
    label = _provider_label(base_url)

    def err(msg: str) -> ProviderError:
        e = ProviderError(msg, status=status, body=body if body is not None else (str(exc) if exc else None))
        e.retryable = status in RETRYABLE_STATUS  # type: ignore[attr-defined]
        return e

    if "sensitive_words_detected" in text or "sensitive words" in text or "sensitive_word" in text:
        return err(
            "Content filter triggered (sensitive_words_detected).\n"
            "  This is AgentRouter's sensitive word detection to prevent abuse.\n"
            "  How to fix: Run `/clear` to reset context, or start a new session with `/sessions`.\n"
            "  Run `/faq sensitive` for more details."
        )
    if "budget pool" in text:
        return err(
            "HTTP 402 Budget Pool Quota Exhausted.\n"
            "  Claude and GPT models on AgentRouter are released in daily batches on a first-come, first-served basis:\n"
            "    - 00:00 Beijing time (16:00 UTC)\n"
            "    - 08:00 Beijing time (00:00 UTC)\n"
            "    - 16:00 Beijing time (08:00 UTC)\n"
            "  How to fix:\n"
            "     1. Switch to DeepSeek with `/model deepseek-v4-flash` for uninterrupted use with unlimited quota.\n"
            "     2. Or wait for the next batch release time slot.\n"
            "     3. Run `/faq 402` for more details."
        )
    if "content blocked" in text or "unsupported language" in text:
        return err(
            "HTTP 400 Content Blocked: Language restriction triggered.\n"
            "  AgentRouter currently only supports Chinese, English, French, German, and Russian.\n"
            "  How to fix: Modify or translate your request into a supported language and retry.\n"
            "  Run `/faq 400` for more details."
        )

    if status is None and exc is not None and not _is_connection_error(exc):
        status = status_from_text(str(exc))

    suffix = f"\n  Details: {detail}" if detail else ""
    if status == 400:
        if any(k in text for k in ("context_length", "context length", "prompt is too long", "maximum context", "too many tokens", "reduce the length")):
            return err(f"HTTP 400 from {label}: the conversation exceeds the model's context window.\n  How to fix: run `/compact` or `/clear`, or pick a model with a larger window via `/model`.{suffix}")
        return err(f"HTTP 400 Bad Request from {label} (model '{model}').{suffix}")
    if status == 401:
        hint = ""
        if is_agentrouter(base_url):
            hint = ("\n     Verify your token at https://agentrouter.org/console or https://ps.air-outer.com"
                    "\n     Supported client documentation: https://ps.air-outer.com/docs/claude-code.html"
                    "\n     Run `/faq 401` for more details.")
        return err(
            f"HTTP 401 Unauthorized from {label}: API key missing, invalid, or expired.\n"
            f"  How to fix: set {_key_hint(base_url)} (or run `/keys` / `/provider` to update credentials).{hint}{suffix}"
        )
    if status == 402:
        return err(f"HTTP 402 Payment Required from {label}: the account is out of credits or billing is not set up.{suffix}")
    if status == 403:
        return err(f"HTTP 403 Forbidden from {label}: this key is not allowed to use model '{model}' (permissions, region or organization restriction).{suffix}")
    if status == 404:
        where = url or base_url
        return err(
            f"HTTP 404 Not Found from endpoint '{where}'.\n"
            f"  The model '{model}' or path is not available on this provider.\n"
            f"  Fix: Run `/model` to select an available model or `/provider` to re-configure the endpoint.{suffix}"
        )
    if status == 413:
        return err(f"HTTP 413 from {label}: request too large. Remove large attachments or run `/compact`.{suffix}")
    if status == 429:
        return err(f"HTTP 429 Rate limited by {label} after several retries. Wait a moment and try again, or switch models with `/model`.{suffix}")
    if status is not None and status >= 500 and is_invalid_request_body(body if body is not None else detail):
        e = err(
            f"{label} rejected the request as invalid (HTTP {status}). This is a rejection of the request "
            f"itself, not an outage.\n"
            f"  How to fix: retry once more (AgentRouter also sends this intermittently for valid requests); "
            f"if it keeps failing, run `/compact` to rebuild the conversation sent to the model, or switch "
            f"models with `/model`.{suffix}"
        )
        e.retryable = False  # type: ignore[attr-defined]
        return e
    if status is not None and status >= 500:
        return err(f"HTTP {status} from {label}: the provider is temporarily unavailable (retries exhausted). Try again shortly.{suffix}")
    if status is not None:
        return err(f"HTTP {status} from {label}.{suffix}")

    if exc is not None:
        return connection_error(exc, base_url)
    return err(f"Request to {label} failed.{suffix}")


def _is_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
                "RemoteProtocolError", "ReadError", "WriteError", "NetworkError", "APIConnectionError", "APITimeoutError"):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, ProviderError)


def connection_error(exc: BaseException, base_url: str) -> ProviderError:
    s = str(exc).lower()
    refused = any(k in s for k in ("connection refused", "[errno 61]", "[errno 111]", "winerror 10061", "actively refused"))
    timeout = "timed out" in s or "timeout" in s or "Timeout" in type(exc).__name__
    dns = any(k in s for k in ("getaddrinfo", "name or service not known", "nodename nor servname", "nameresolution", "temporary failure in name resolution"))
    port = port_of(base_url)
    host = host_of(base_url)
    if refused and (port == 11434 or "ollama" in host):
        msg = (f"Cannot connect to Ollama at {base_url} (Connection refused).\n"
               f"  How to fix:\n"
               f"     1. Start Ollama by running: `ollama serve` (or open the Ollama application)\n"
               f"     2. Or switch back to cloud AI anytime by typing: `/provider`.")
    elif refused and (port == 1234 or "lmstudio" in host):
        msg = (f"Cannot connect to LM Studio at {base_url} (Connection refused).\n"
               f"  How to fix:\n"
               f"     1. Open LM Studio -> Developer / 'Local Server' tab -> 'Start Server' (port 1234)\n"
               f"     2. Or switch back to cloud AI anytime by typing: `/provider`.")
    elif refused and is_local_url(base_url):
        msg = (f"Cannot connect to local AI endpoint at {base_url} (Connection refused).\n"
               f"  Ensure your local server is running, or run `/provider` to switch to cloud.")
    elif is_agentrouter(base_url) and (refused or timeout or dns):
        msg = (f"Cannot connect to AgentRouter at {base_url}.\n"
               f"  Backup Domain Available:\n"
               f"     AgentRouter provides an official alternative domain: https://ps.air-outer.com\n"
               f"     Switch anytime by running `/provider` or setting base_url to https://ps.air-outer.com\n"
               f"     Run `/faq domain` for details.")
    elif timeout:
        msg = f"Request to {_provider_label(base_url)} timed out ({exc}). Check your network or try again."
    elif dns:
        msg = f"Cannot resolve host '{host}' for {base_url}. Check the base URL and your network connection."
    else:
        msg = f"Network error talking to {_provider_label(base_url)} at {base_url}: {exc}"
    e = ProviderError(msg, status=None, body=str(exc))
    e.retryable = True  # type: ignore[attr-defined]
    return e


def is_failover_error(exc: BaseException) -> bool:
    """429 / 5xx / overload / timeout / connection failures that justify trying another model."""
    if getattr(exc, "retryable", False):
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and status in RETRYABLE_STATUS:
        return True
    text = str(exc).lower()
    return "overloaded" in text or "rate limit" in text


def is_retryable_exception(exc: BaseException) -> bool:
    return _is_connection_error(exc) and not isinstance(exc, ProviderError)


def headers_of(resp: Any) -> Mapping[str, Any] | None:
    h = getattr(resp, "headers", None)
    return h if h is not None else None
