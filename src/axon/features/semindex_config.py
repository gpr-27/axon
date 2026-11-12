"""Semantic search settings: the ``semanticSearch`` settings.json block and the resolved embedding configuration.

The block is user-level only (config.py drops the ``embedding_*`` keys from project settings): a repository must not
choose where file contents are sent. The API key is never stored in a settings file: ``keyEnv`` names the environment
variable (or .env / vault entry) that holds it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

BACKENDS = ("ollama", "openai", "voyage")
DEFAULTS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": "http://localhost:11434", "model": "nomic-embed-text", "key_env": ""},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "text-embedding-3-small", "key_env": "OPENAI_API_KEY"},
    "voyage": {"base_url": "https://api.voyageai.com/v1", "model": "voyage-code-4", "key_env": "VOYAGE_API_KEY"},
}
_BLOCK_KEYS = {"enabled": "embedding_enabled", "backend": "embedding_backend", "model": "embedding_model",
               "baseUrl": "embedding_base_url", "base_url": "embedding_base_url", "keyEnv": "embedding_key_env",
               "key_env": "embedding_key_env", "maxChunks": "embedding_max_chunks", "maxIndexSize": "embedding_max_chunks"}


def split_block(block: dict[str, Any]) -> dict[str, Any]:
    """The ``semanticSearch`` object -> Settings fields."""
    out: dict[str, Any] = {}
    for k, v in block.items():
        field = _BLOCK_KEYS.get(k)
        if field is None or v is None:
            continue
        if field == "embedding_enabled":
            v = bool(v)
        elif field == "embedding_max_chunks":
            try:
                v = max(100, int(v))
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip()
        out[field] = v
    return out


@dataclass(frozen=True)
class EmbedConfig:
    enabled: bool
    backend: str
    model: str
    base_url: str
    key_env: str
    max_chunks: int

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc or self.base_url

    @property
    def is_local(self) -> bool:
        h = (urlparse(self.base_url).hostname or "").lower()
        return h in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    @property
    def identity(self) -> str:
        """What a stored index was built with; a different identity means the vectors are not comparable."""
        return f"{self.backend}|{self.model}"


def resolve(settings: Any) -> EmbedConfig:
    g = lambda n, d=None: getattr(settings, n, d)      # noqa: E731
    backend = str(g("embedding_backend") or "ollama").strip().lower()
    if backend not in BACKENDS:
        backend = "ollama"
    d = DEFAULTS[backend]
    return EmbedConfig(
        enabled=bool(g("embedding_enabled", False)), backend=backend,
        model=str(g("embedding_model") or d["model"]).strip(),
        base_url=str(g("embedding_base_url") or d["base_url"]).strip().rstrip("/"),
        key_env=str(g("embedding_key_env") if g("embedding_key_env") is not None else d["key_env"]).strip() or d["key_env"],
        max_chunks=int(g("embedding_max_chunks", 100000) or 100000))


def lookup_key(cfg: EmbedConfig, workspace: Any = None) -> str:
    """The API key from the configured variable via the shared key lookup (env, project/user .env). '' when none."""
    if not cfg.key_env:
        return ""
    try:
        from axon.providers.keystore import key_status
        st = key_status(cfg.key_env, workspace)
        if st.is_set:
            return str(st.value or "")
    except Exception:
        pass
    import os
    return os.environ.get(cfg.key_env, "").strip()
