"""Embedding backends for semantic search: local Ollama, any OpenAI-compatible /v1/embeddings, and Voyage AI.

Stdlib only. ``Embedder.embed(texts, kind)`` returns unit-length vectors; requests are retried with backoff on 429 and
5xx answers. ``kind`` is 'document' or 'query' (used by Voyage's ``input_type``)."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

from axon.features.semindex_config import EmbedConfig


class EmbedError(Exception):
    pass


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    batch_size = 32

    def __init__(self, cfg: EmbedConfig, key: str = "", timeout: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                 retries: int = 5) -> None:
        self.cfg, self.key, self.timeout, self._sleep, self.retries = cfg, key, timeout, sleep, retries
        if cfg.backend == "voyage":
            self.batch_size = 64
        self.cancelled: Callable[[], bool] = lambda: False

    # -- http ---------------------------------------------------------------------------------------------
    def _post(self, url: str, body: dict[str, Any]) -> Any:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "axon-semindex"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        delay = 1.0
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:     # noqa: S310 (user-configured endpoint)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = min(60.0, float(ra)) if ra else delay
                    except ValueError:
                        wait = delay
                    self._sleep(wait)
                    delay = min(delay * 2, 30.0)
                    continue
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                raise EmbedError(f"HTTP {e.code} from {urlparse(url).netloc}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                if attempt < self.retries and not isinstance(e, ValueError):
                    self._sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise EmbedError(f"cannot reach {urlparse(url).netloc or url}: {e}") from None
        raise EmbedError("embedding request failed")

    # -- backends -----------------------------------------------------------------------------------------
    def _endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if self.cfg.backend == "ollama":
            return base
        if urlparse(base).path in ("", "/"):
            base += "/v1"
        return base + "/embeddings"

    def _call(self, texts: list[str], kind: str) -> list[list[float]]:
        c = self.cfg
        if c.backend == "ollama":
            base = self._endpoint()
            try:
                res = self._post(base + "/api/embed", {"model": c.model, "input": texts})
                vecs = res.get("embeddings")
            except EmbedError as e:
                if "HTTP 404" not in str(e):
                    raise
                vecs = [(self._post(base + "/api/embeddings", {"model": c.model, "prompt": t}) or {}).get("embedding")
                        for t in texts]           # older Ollama: one text per request
            if not isinstance(vecs, list) or len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
                raise EmbedError("Ollama returned no embeddings (is the model pulled? ollama pull " + c.model + ")")
            return vecs
        body: dict[str, Any] = {"model": c.model, "input": texts}
        if c.backend == "voyage":
            body["input_type"] = "query" if kind == "query" else "document"
        res = self._post(self._endpoint(), body)
        items = res.get("data") if isinstance(res, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbedError("the endpoint returned no embeddings (does it serve /v1/embeddings?)")
        items = sorted(items, key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        return [d["embedding"] for d in items]

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            if self.cancelled():
                raise EmbedError("cancelled")
            out.extend(_norm([float(x) for x in v]) for v in self._call(texts[i:i + self.batch_size], kind))
        return out

    def probe(self) -> int:
        """Embed one short text; returns the vector dimension."""
        return len(self.embed(["def hello(): pass"], "query")[0])
