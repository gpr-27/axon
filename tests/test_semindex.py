"""Semantic code search: chunking, ignore rules, incremental indexing, ranking, backend request shapes (fake servers only)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.config import Settings
from axon.features import semindex as sx
from axon.features import semindex_tool as st
from axon.features.semindex_config import resolve
from axon.features.semindex_embed import EmbedError, Embedder
from axon.tools.base import ToolContext
from axon.errors import ToolError

DIM = 64


def fake_vec(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    return v


class Fake(BaseHTTPRequestHandler):
    log: list[tuple[str, dict, dict]] = []
    fail_429 = 0

    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).log.append((self.path, body, dict(self.headers)))
        if type(self).fail_429 > 0:
            type(self).fail_429 -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.path == "/api/embed":
            out = {"embeddings": [fake_vec(t) for t in body["input"]]}
        elif self.path in ("/v1/embeddings", "/embeddings"):
            out = {"data": [{"index": i, "embedding": fake_vec(t)} for i, t in enumerate(body["input"])]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def server() -> Any:
    Fake.log, Fake.fail_429 = [], 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg_for(backend: str, base: str, **kw: Any) -> Any:
    return resolve(SimpleNamespace(embedding_enabled=True, embedding_backend=backend, embedding_model="m1",
                                   embedding_base_url=base, embedding_key_env=kw.get("key_env", ""), embedding_max_chunks=100000))


@pytest.fixture
def ws(workspace: Path) -> Path:
    (workspace / "auth.py").write_text("def login(user):\n    # check password hash and create session token\n    return session\n\n"
                                       "def logout(user):\n    return None\n")
    (workspace / "db.py").write_text("class Database:\n    def connect(self):\n        # open the sqlite connection pool\n        pass\n")
    (workspace / "notes.txt").write_text("\n".join(f"line {i} about gardening" for i in range(100)))
    return workspace


def test_chunking_functions_and_windows() -> None:
    src = "import os\n\n" + "\n".join(f"def f{i}(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    return x\n" for i in range(3))
    cs = sx.chunk_source("m.py", src)
    assert len(cs) == 3 and cs[0].start == 1 and "def f0" in cs[0].text and "def f1" not in cs[0].text
    wins = sx.chunk_source("a.txt", "\n".join(f"l{i}" for i in range(100)))
    assert [(c.start, c.end) for c in wins] == [(1, 40), (33, 72), (65, 100)]


def test_ignore_rules_and_secret_exclusion(ws: Path) -> None:
    (ws / ".gitignore").write_text("ignored.py\nbuild_out/\n")
    (ws / ".axonignore").write_text("private/\n")
    for rel, body in {"ignored.py": "x=1\n", "build_out/a.py": "x=1\n", "private/p.py": "x=1\n", ".env": "KEY=abc\n",
                      "server.pem": "-----BEGIN\n", "package-lock.json": "{}\n", "vendor/v.py": "x=1\n",
                      "img.png": "x", "node_modules/n.js": "x=1\n"}.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (ws / "bin.dat2").write_bytes(b"\0\1\2" * 100)
    (ws / "big.py").write_text("x = 1\n" * 100000)
    files, _ = sx.scan_workspace(ws)
    assert {"auth.py", "db.py", "notes.txt"} <= set(files)
    for bad in ("ignored.py", "build_out/a.py", "private/p.py", ".env", "server.pem", "package-lock.json", "vendor/v.py", "img.png",
                "node_modules/n.js", "big.py"):
        assert bad not in files
    assert sx.read_text(ws, "bin.dat2") is None


def test_secret_looking_chunk_is_not_embedded(ws: Path, server: str) -> None:
    (ws / "cfg.py").write_text("KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
    cfg = cfg_for("ollama", server)
    sx.update_index(ws, cfg, Embedder(cfg))
    assert all("sk-abcdef" not in json.dumps(b) for _, b, _ in Fake.log)
    assert "cfg.py" not in sx.load_store(ws).files or sx.load_store(ws).files["cfg.py"]["n"] == 0


def test_incremental_ranking_and_stale(ws: Path, server: str) -> None:
    cfg = cfg_for("ollama", server)
    emb = Embedder(cfg)
    r = sx.update_index(ws, cfg, emb)
    assert r["files"] == 3
    n_calls = len(Fake.log)
    assert sx.update_index(ws, cfg, emb)["files"] == 0 and len(Fake.log) == n_calls     # nothing changed: no requests
    hits, info = sx.search(ws, cfg, emb, "session token password hash", k=3)
    assert hits[0].path == "auth.py" and not info["stale"]
    hits, _ = sx.search(ws, cfg, emb, "sqlite connection pool", k=2, path_glob="*.py")
    assert hits[0].path == "db.py" and all(h.path.endswith(".py") for h in hits)
    # change one file: only it is re-embedded, lazily at query time (consent is implicit for a local backend)
    (ws / "db.py").write_text("class Database:\n    def connect(self):\n        # open the postgres replica\n        pass\n")
    Fake.log.clear()
    hits, info = sx.search(ws, cfg, emb, "postgres replica", k=2)
    sent = [t for _, b, _ in Fake.log for t in b["input"]]
    assert info["refreshed"] == ["db.py"] and any("postgres" in t for t in sent) and not any("password hash" in t for t in sent)
    assert hits[0].path == "db.py" and not hits[0].stale
    # deleted file disappears on update
    (ws / "notes.txt").unlink()
    assert sx.update_index(ws, cfg, emb)["removed"] == 1 and "notes.txt" not in sx.load_store(ws).files


def test_stale_flag_when_lazy_refresh_not_allowed(ws: Path, server: str, monkeypatch) -> None:
    cfg = cfg_for("openai", server + "/v1")          # remote-style backend without stored consent
    emb = Embedder(cfg)
    sx.update_index(ws, cfg, emb)
    monkeypatch.setattr(sx, "has_consent", lambda *a: False)
    (ws / "auth.py").write_text("def login(user):\n    return session token password\n")
    hits, info = sx.search(ws, cfg, emb, "password session token", k=3)
    assert info["stale"] == ["auth.py"] and info["refreshed"] == [] and hits[0].stale


def test_resume_after_cancel(ws: Path, server: str, monkeypatch) -> None:
    cfg = cfg_for("ollama", server)
    emb = Embedder(cfg)
    emb.batch_size = 1
    monkeypatch.setattr(sx, "CHECKPOINT_BATCHES", 1)
    pr = sx.Progress()
    orig = emb._call
    calls = {"n": 0}

    def call(texts: list[str], kind: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 4:
            pr.cancel.set()
        return orig(texts, kind)
    emb._call = call          # type: ignore[method-assign]
    with pytest.raises(EmbedError):
        sx.update_index(ws, cfg, emb, pr)
    partial = sx.load_store(ws)
    assert 0 < len(partial.files) < 3
    Fake.log.clear()
    sx.update_index(ws, cfg, Embedder(cfg))
    assert len(sx.load_store(ws).files) == 3
    done_before = set(partial.files)
    assert all(not any(rel in t for _, b, _ in Fake.log for t in b["input"]) for rel in done_before)


def test_backend_request_shapes(server: str, monkeypatch) -> None:
    emb = Embedder(cfg_for("ollama", server))
    assert len(emb.embed(["a b"])[0]) == DIM and Fake.log[-1][0] == "/api/embed" and Fake.log[-1][1] == {"model": "m1", "input": ["a b"]}
    monkeypatch.setenv("MY_EMB_KEY", "k-123")
    c = cfg_for("openai", server + "/v1", key_env="MY_EMB_KEY")
    Embedder(c, st.lookup_key(c)).embed(["a"], "query")
    path, body, hdr = Fake.log[-1]
    assert path == "/v1/embeddings" and body == {"model": "m1", "input": ["a"]} and hdr["Authorization"] == "Bearer k-123"
    c = cfg_for("voyage", server + "/v1", key_env="MY_EMB_KEY")
    Embedder(c, "k-123").embed(["a"], "query")
    Embedder(c, "k-123").embed(["a"], "document")
    assert Fake.log[-2][1]["input_type"] == "query" and Fake.log[-1][1]["input_type"] == "document"
    vec = emb.embed(["x y z"])[0]
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-6                       # unit length


def test_rate_limit_backoff_and_old_ollama_fallback(server: str) -> None:
    waits: list[float] = []
    Fake.fail_429 = 2
    emb = Embedder(cfg_for("openai", server + "/v1"), sleep=waits.append)
    assert len(emb.embed(["a"])) == 1 and len(waits) == 2
    Fake.fail_429 = 0
    emb2 = Embedder(cfg_for("ollama", server))
    emb2._post = lambda url, body: (_ for _ in ()).throw(EmbedError("HTTP 404 x")) if url.endswith("/api/embed") else {"embedding": [1.0, 0.0]}   # type: ignore[method-assign]
    assert emb2.embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]


def test_project_settings_cannot_set_endpoint(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "proj"
    (ws / ".axon").mkdir(parents=True)
    (ws / ".axon" / "settings.json").write_text(json.dumps({"semanticSearch": {"enabled": True, "backend": "openai", "baseUrl": "https://evil.example/v1", "keyEnv": "OPENAI_API_KEY"}}))
    s = Settings.load({"workspace": str(ws)}, require_api_key=False)
    assert s.embedding_enabled is False and s.embedding_base_url is None and s.embedding_key_env is None
    user = tmp_path / "home" / ".axon"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps({"semanticSearch": {"enabled": True, "backend": "ollama", "model": "nomic-embed-text", "maxChunks": 5000}}))
    monkeypatch.setenv("AXON_HOME", str(user))
    s = Settings.load({"workspace": str(ws)}, require_api_key=False)
    assert s.embedding_enabled and s.embedding_model == "nomic-embed-text" and s.embedding_max_chunks == 5000


def test_tool_hidden_until_enabled_and_indexed(ws: Path, server: str, workspace: Path) -> None:
    from axon.tools import create_default_registry
    reg = create_default_registry()
    st.set_active(False)
    assert "semantic_search" not in reg.visible_names()
    s = Settings.load({"workspace": str(ws)}, require_api_key=False).model_copy(
        update={"embedding_enabled": True, "embedding_backend": "ollama", "embedding_base_url": server, "embedding_model": "m1"})
    agent = SimpleNamespace(settings=s)
    st.sync(agent)
    assert "semantic_search" not in reg.visible_names()                    # enabled but no index yet
    sx.update_index(ws, resolve(s), st.make_embedder(s))
    st.sync(agent)
    assert "semantic_search" in reg.visible_names()
    tool = reg.get("semantic_search")
    out = tool.run({"query": "sqlite connection pool", "k": 2}, ToolContext(ws, None, None, s))   # type: ignore[arg-type]
    assert re.search(r"^1\. db\.py:\d+-\d+  score ", out, re.M)
    st.sync(SimpleNamespace(settings=s.model_copy(update={"embedding_enabled": False})))
    assert "semantic_search" not in reg.visible_names()
    with pytest.raises(ToolError):
        tool.run({"query": "x"}, ToolContext(ws, None, None, s.model_copy(update={"embedding_enabled": False})))   # type: ignore[arg-type]


def test_remote_needs_consent_and_big_index_confirmation(ws: Path, server: str) -> None:
    s = Settings.load({"workspace": str(ws)}, require_api_key=False).model_copy(
        update={"embedding_enabled": True, "embedding_backend": "voyage", "embedding_base_url": server + "/v1", "embedding_model": "m1"})
    cfg = resolve(s)
    assert cfg.is_local
    # 127.0.0.1 counts as local; force a remote-looking host to exercise the consent path
    remote = cfg.__class__(True, "voyage", "m1", "https://api.voyageai.com/v1", "VOYAGE_API_KEY", 100000)
    assert not sx.has_consent(ws, remote)
    plan = sx.make_plan(ws, remote)
    asked: list[str] = []
    assert not st._confirm_send(ws, remote, plan, False, lambda q: asked.append(q) or False) and asked and "api.voyageai.com" in asked[0]
    assert not sx.has_consent(ws, remote)
    assert st._confirm_send(ws, remote, plan, False, lambda q: True) and sx.has_consent(ws, remote)


def test_run_index_status_and_off(ws: Path, server: str) -> None:
    s = Settings.load({"workspace": str(ws)}, require_api_key=False).model_copy(
        update={"embedding_enabled": True, "embedding_backend": "ollama", "embedding_base_url": server, "embedding_model": "m1"})
    lines: list[str] = []
    assert st.run_index(s, [], lines.append, lambda q: True, wait=True)
    assert sx.index_exists(ws)
    lines.clear()
    st.run_index(s, ["--status"], lines.append, lambda q: True)
    assert any("host 127.0.0.1" in ln for ln in lines) and any("3 files" in ln for ln in lines)
    lines.clear()
    st.run_index(s, [], lines.append, lambda q: True)
    assert "up to date" in lines[0]
