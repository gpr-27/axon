"""Semantic code search index: chunking, a compact local vector store and an incremental, resumable indexer.

Layout (``~/.axon/projects/<key>/semindex/``): ``vectors.f32`` (raw float32, unit-length rows) and ``meta.json``
(identity of the embedding model, dimension, per-file mtime/size/hash and the chunk table), ``consent.json`` (hosts the
user agreed to send file contents to). Search is brute-force cosine (numpy when importable, else stdlib ``array``).
Nothing outside the workspace is indexed; secrets files and secret-looking chunks are never embedded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.features.semindex_config import EmbedConfig
from axon.features.semindex_embed import EmbedError, Embedder

MAX_FILE_BYTES = 256 * 1024
WINDOW = 40
OVERLAP = 8
MAX_UNIT_LINES = 80
MIN_UNIT_LINES = 6
MAX_CHUNK_CHARS = 3000
BIG_INDEX_CHUNKS = 2000
LAZY_MAX_FILES = 3
LAZY_MAX_CHUNKS = 80
SNIPPET_LINES = 8
SNIPPET_CHARS = 500
CHECKPOINT_BATCHES = 10

_SKIP_EXT = frozenset(""".png .jpg .jpeg .gif .webp .ico .bmp .tiff .svg .pdf .zip .gz .tgz .bz2 .xz .7z .rar .jar .war .class .pyc
.pyo .so .dylib .dll .exe .o .a .bin .dat .db .sqlite .sqlite3 .mp3 .mp4 .mov .avi .wav .ogg .webm .woff .woff2 .ttf .otf .eot
.lock .map .csv .tsv .parquet .pkl .npy .npz .onnx .pt .h5 .ipynb .min.js .min.css .snap .pem .key .p12 .pfx .crt .cer""".split())
_SKIP_NAMES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "Gemfile.lock",
                         "composer.lock", "go.sum", "Pipfile.lock", "bun.lockb", "npm-shrinkwrap.json"})
_SKIP_DIRS = frozenset({"vendor", "third_party", "third-party", "dist", "build", "target", "site-packages", "coverage", "out",
                        ".axon", "__snapshots__"})
_BOUNDARY = {
    "py": re.compile(r"^(?:async\s+def|def|class)\s+\w"),
    "c": re.compile(r"^\s{0,4}(?:export\s+|default\s+|pub(?:\([a-z]+\))?\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|final\s+|abstract\s+|"
                    r"func\s|function[\s*(]|fn\s|impl\b|struct\s|enum\s|trait\s|interface\s|class\s|type\s+\w+\s*(?:=|struct|interface)|"
                    r"const\s+\w+\s*=\s*(?:async\s*)?(?:\(|function)|def\s|module\s|namespace\s|object\s|sealed\s|data\s+class)"),
}
_PY_EXT = {".py", ".pyi"}
_C_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".swift", ".c", ".h", ".cc",
          ".cpp", ".hpp", ".cs", ".php", ".rb", ".dart", ".vue", ".svelte", ".lua", ".sh", ".zig"}
_PRICE_PER_MTOK = {"openai": 0.02, "voyage": 0.06}      # rough USD per million tokens, an estimate only


@dataclass
class Chunk:
    path: str
    start: int          # 1-based, inclusive
    end: int
    text: str


# ─── chunking ────────────────────────────────────────────────────────────────────────────────────────────

def _windows(lines: list[str], lo: int, hi: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = lo
    while i < hi:
        j = min(hi, i + WINDOW)
        out.append((i, j))
        if j >= hi:
            break
        i = j - OVERLAP
    return out


def _starts(lines: list[str], ext: str) -> list[int]:
    rx = _BOUNDARY["py"] if ext in _PY_EXT else _BOUNDARY["c"] if ext in _C_EXT else None
    if rx is None:
        return []
    marks = [i for i, ln in enumerate(lines) if ln.strip() and rx.match(ln) and (ext not in _PY_EXT or not ln[:1].isspace() or ln.startswith("    ") and not ln.startswith("     "))]
    out = []
    for i in marks:             # take the comment / decorator lines directly above into the unit
        while i > 0 and lines[i - 1].lstrip().startswith(("#", "//", "@", "/*", "*", "///")) and lines[i - 1].strip():
            i -= 1
        if not out or i > out[-1]:
            out.append(i)
    return out


def chunk_source(path: str, text: str) -> list[Chunk]:
    """Logical units (functions / classes) when the language is known, else overlapping 40-line windows."""
    lines = text.splitlines()
    if not any(ln.strip() for ln in lines):
        return []
    ext = os.path.splitext(path)[1].lower()
    starts = _starts(lines, ext)
    spans: list[tuple[int, int]] = []
    if starts:
        bounds = ([0] if starts[0] > 0 else []) + starts + [len(lines)]
        units = [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]
        cur: tuple[int, int] | None = None
        for a, b in units:            # merge tiny neighbours so a one-line unit is not a chunk of its own
            if cur is None:
                cur = (a, b)
            elif cur[1] - cur[0] < MIN_UNIT_LINES and (b - cur[0]) <= WINDOW:
                cur = (cur[0], b)
            else:
                spans.append(cur)
                cur = (a, b)
        if cur:
            spans.append(cur)
        spans = [w for a, b in spans for w in ([(a, b)] if b - a <= MAX_UNIT_LINES else _windows(lines, a, b))]
    else:
        spans = _windows(lines, 0, len(lines))
    out: list[Chunk] = []
    for a, b in spans:
        body = "\n".join(lines[a:b])
        if not body.strip():
            continue
        if len(body) > MAX_CHUNK_CHARS:        # very long lines: cut, keep the line range
            body = body[:MAX_CHUNK_CHARS]
        out.append(Chunk(path, a + 1, b, body))
    return out


def embed_text(c: Chunk) -> str:
    return f"{c.path}\n{c.text}"


# ─── scanning ────────────────────────────────────────────────────────────────────────────────────────────

def _is_text_name(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    low = name.lower()
    if name in _SKIP_NAMES or low.endswith(".min.js") or low.endswith(".min.css"):
        return False
    if os.path.splitext(low)[1] in _SKIP_EXT:
        return False
    return not any(p in _SKIP_DIRS for p in rel.split("/")[:-1])


def _secret_path(rel: str) -> bool:
    try:
        from axon.agent.filechanges import is_secret
        return is_secret(rel)
    except Exception:
        low = rel.rsplit("/", 1)[-1].lower()
        return low.startswith(".env") or low.endswith((".pem", ".key", ".p12", ".pfx"))


def _secret_text(text: str) -> bool:
    try:
        from axon.features.memory_files import scan_secrets
        return bool(scan_secrets(text))
    except Exception:
        return False


def scan_workspace(ws: Path) -> tuple[dict[str, tuple[float, int]], dict[str, int]]:
    """{rel: (mtime, size)} of indexable files (ignore files, .axonignore, binaries, big files, secrets excluded)
    and a count of what was skipped by reason."""
    from axon.tools._files import iter_files
    ws = ws.resolve()
    files: dict[str, tuple[float, int]] = {}
    skipped = {"ignored_type": 0, "large": 0, "secret": 0, "outside": 0}
    for fp in iter_files(ws):
        try:
            rel = fp.relative_to(ws).as_posix()
        except ValueError:
            continue
        try:
            real = fp.resolve()
            real.relative_to(ws)         # a symlink leading outside the workspace is never read
        except (ValueError, OSError):
            skipped["outside"] += 1
            continue
        if _secret_path(rel):
            skipped["secret"] += 1
            continue
        if not _is_text_name(rel):
            skipped["ignored_type"] += 1
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
            skipped["large"] += st.st_size > MAX_FILE_BYTES
            continue
        files[rel] = (st.st_mtime, st.st_size)
    return files, skipped


def read_text(ws: Path, rel: str) -> str | None:
    """The file's text, or None for binary / unreadable / outside the workspace."""
    p = ws / rel
    try:
        p.resolve().relative_to(ws.resolve())
        data = p.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in data[:4096] or len(data) > MAX_FILE_BYTES:
        return None
    return data.decode("utf-8", "replace")


# ─── store ───────────────────────────────────────────────────────────────────────────────────────────────

_LOCK = threading.RLock()


def index_dir(ws: Path) -> Path:
    from axon import storage
    return storage.project_subdir(ws, "semindex")


@dataclass
class Store:
    identity: str = ""
    dim: int = 0
    files: dict[str, dict[str, Any]] = field(default_factory=dict)      # rel -> {mtime,size,hash,n}
    chunks: list[tuple[str, int, int]] = field(default_factory=list)    # (rel, start, end), row order
    vecs: Any = None                                                    # array('f') of len(chunks)*dim

    @property
    def n(self) -> int:
        return len(self.chunks)


def load_store(ws: Path) -> Store:
    d = index_dir(ws)
    with _LOCK:
        try:
            meta = json.loads((d / "meta.json").read_text("utf-8"))
            dim, chunks = int(meta["dim"]), [(c[0], int(c[1]), int(c[2])) for c in meta["chunks"]]
            vecs = array("f")
            with open(d / "vectors.f32", "rb") as f:
                vecs.frombytes(f.read(len(chunks) * dim * 4))
            if len(vecs) != len(chunks) * dim:
                raise ValueError("vectors file is shorter than the chunk table")
            return Store(str(meta.get("identity", "")), dim, dict(meta.get("files", {})), chunks, vecs)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return Store(vecs=array("f"))


def save_store(ws: Path, st: Store) -> None:
    d = index_dir(ws)
    with _LOCK:
        tmp = d / "vectors.f32.tmp"
        with open(tmp, "wb") as f:
            st.vecs.tofile(f)
        os.replace(tmp, d / "vectors.f32")
        meta = {"version": 1, "identity": st.identity, "dim": st.dim, "files": st.files, "chunks": [list(c) for c in st.chunks]}
        tmpm = d / "meta.json.tmp"
        tmpm.write_text(json.dumps(meta, separators=(",", ":")), "utf-8")
        os.replace(tmpm, d / "meta.json")


def index_exists(ws: Path) -> bool:
    try:
        return (index_dir(ws) / "meta.json").is_file() and load_store(ws).n > 0
    except Exception:
        return False


def delete_index(ws: Path) -> None:
    import shutil
    with _LOCK:
        shutil.rmtree(index_dir(ws), ignore_errors=True)


def has_consent(ws: Path, cfg: EmbedConfig) -> bool:
    if cfg.is_local:
        return True
    try:
        data = json.loads((index_dir(ws) / "consent.json").read_text("utf-8"))
        return cfg.host in data.get("hosts", [])
    except (OSError, ValueError):
        return False


def give_consent(ws: Path, cfg: EmbedConfig) -> None:
    d = index_dir(ws)
    try:
        hosts = json.loads((d / "consent.json").read_text("utf-8")).get("hosts", [])
    except (OSError, ValueError):
        hosts = []
    if cfg.host not in hosts:
        hosts.append(cfg.host)
    (d / "consent.json").write_text(json.dumps({"hosts": hosts}), "utf-8")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


# ─── indexer ─────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    changed: list[str]
    removed: list[str]
    unchanged: int
    chunks: int
    tokens: int
    skipped: dict[str, int]
    over_limit: bool = False

    def cost(self, backend: str) -> float | None:
        p = _PRICE_PER_MTOK.get(backend)
        return None if p is None else self.tokens / 1e6 * p


def _prepare(ws: Path, rel: str) -> tuple[list[Chunk], str] | None:
    text = read_text(ws, rel)
    if text is None:
        return None
    chunks = [c for c in chunk_source(rel, text) if not _secret_text(c.text)]
    return chunks, _hash(text)


def make_plan(ws: Path, cfg: EmbedConfig, store: Store | None = None) -> Plan:
    st = store or load_store(ws)
    files, skipped = scan_workspace(ws)
    same = st.identity == cfg.identity
    changed, unchanged, chunks, chars = [], 0, 0, 0
    for rel, (mt, size) in files.items():
        old = st.files.get(rel) if same else None
        if old and old.get("size") == size and old.get("mtime") == mt:
            unchanged += 1
            continue
        prep = _prepare(ws, rel)
        if prep is None:
            continue
        cs, h = prep
        if old and old.get("hash") == h:           # touched but identical: refresh the stamp only (done in update)
            unchanged += 1
            continue
        changed.append(rel)
        chunks += len(cs)
        chars += sum(len(embed_text(c)) for c in cs)
    removed = [r for r in st.files if r not in files] if same else []
    total = st.n + chunks if same else chunks
    return Plan(changed, removed, unchanged, chunks, chars // 4, skipped, over_limit=total > cfg.max_chunks)


class Progress:
    """Shared, thread-safe progress of one indexing run (one status line)."""

    def __init__(self) -> None:
        self.running = False
        self.done_files = 0
        self.total_files = 0
        self.done_chunks = 0
        self.total_chunks = 0
        self.error = ""
        self.finished = 0.0
        self.cancel = threading.Event()
        self.note = ""

    def line(self) -> str:
        if self.running:
            pct = int(100 * self.done_chunks / self.total_chunks) if self.total_chunks else 0
            return f"indexing {self.done_chunks}/{self.total_chunks} chunks ({pct}%), {self.done_files}/{self.total_files} files"
        if self.error:
            return f"index stopped: {self.error}"
        return self.note or "idle"


_RUNS: dict[str, Progress] = {}


def progress_for(ws: Path) -> Progress:
    return _RUNS.setdefault(str(Path(ws).resolve()), Progress())


def update_index(ws: Path, cfg: EmbedConfig, embedder: Embedder, progress: Progress | None = None, rebuild: bool = False,
                 only: list[str] | None = None, max_chunks: int | None = None) -> dict[str, int]:
    """Embed new / changed files (or just ``only``), drop deleted ones and save. Resumable: progress is saved every few
    batches and a file is recorded only once all of its chunks have vectors. Returns counts."""
    pr = progress or Progress()
    ws = ws.resolve()
    st = Store(vecs=array("f")) if rebuild else load_store(ws)
    if st.identity and st.identity != cfg.identity:
        st = Store(vecs=array("f"))                 # other model: vectors are not comparable
    st.identity = cfg.identity
    files, _ = scan_workspace(ws)
    todo: list[tuple[str, list[Chunk], str, float, int]] = []
    refresh = 0
    for rel in (only if only is not None else list(files)):
        if rel not in files:
            continue
        mt, size = files[rel]
        old = st.files.get(rel)
        if old and old.get("size") == size and old.get("mtime") == mt:
            continue
        prep = _prepare(ws, rel)
        if prep is None:
            continue
        cs, h = prep
        if old and old.get("hash") == h:
            old["mtime"], old["size"] = mt, size
            refresh += 1
            continue
        todo.append((rel, cs, h, mt, size))
    gone = set(st.files) - set(files) if only is None else {r for r in (only or []) if r not in files and r in st.files}
    pr.total_files, pr.done_files = len(todo), 0
    pr.total_chunks, pr.done_chunks = sum(len(t[1]) for t in todo), 0
    if max_chunks is not None and pr.total_chunks > max_chunks:
        raise EmbedError(f"{pr.total_chunks} chunks exceed the lazy limit of {max_chunks}")
    replace = gone | {t[0] for t in todo}

    def drop(paths: set[str]) -> None:
        """Remove the rows of ``paths`` from the store."""
        if not paths:
            return
        keep = [i for i, c in enumerate(st.chunks) if c[0] not in paths]
        if len(keep) != st.n:
            dim = st.dim
            nv = array("f")
            for i in keep:
                nv.extend(st.vecs[i * dim:(i + 1) * dim])
            st.chunks, st.vecs = [st.chunks[i] for i in keep], nv
        for p in paths:
            st.files.pop(p, None)

    drop(gone)
    if only is None and not todo and not gone and not refresh:
        return {"files": 0, "chunks": 0, "removed": 0}
    embedder.cancelled = pr.cancel.is_set
    batches = 0
    embedded_files = embedded_chunks = 0
    pending_save = False
    try:
        for rel, cs, h, mt, size in todo:
            if pr.cancel.is_set():
                raise EmbedError("cancelled")
            vecs: list[list[float]] = []
            texts = [embed_text(c) for c in cs]
            for i in range(0, len(texts), embedder.batch_size):
                vecs.extend(embedder.embed(texts[i:i + embedder.batch_size]))
                pr.done_chunks += min(embedder.batch_size, len(texts) - i)
                batches += 1
            if vecs and not st.dim:
                st.dim = len(vecs[0])
            if vecs and len(vecs[0]) != st.dim:
                raise EmbedError("the backend changed its vector size; use /index --rebuild")
            drop({rel})
            for c, v in zip(cs, vecs):
                st.chunks.append((rel, c.start, c.end))
                st.vecs.extend(v)
            st.files[rel] = {"mtime": mt, "size": size, "hash": h, "n": len(cs)}
            pr.done_files += 1
            embedded_files, embedded_chunks, pending_save = embedded_files + 1, embedded_chunks + len(cs), True
            if batches >= CHECKPOINT_BATCHES:
                save_store(ws, st)
                batches, pending_save = 0, False
    finally:
        if pending_save or gone or refresh or rebuild:
            save_store(ws, st)
    return {"files": embedded_files, "chunks": embedded_chunks, "removed": len(gone)}


# ─── background runs ─────────────────────────────────────────────────────────────────────────────────────

def start_background(ws: Path, cfg: EmbedConfig, embedder: Embedder, rebuild: bool = False,
                     on_done: Callable[[Progress], None] | None = None) -> Progress | None:
    """Index in a daemon thread. Returns the Progress, or None when a run is already going."""
    pr = progress_for(ws)
    with _LOCK:
        if pr.running:
            return None
        pr.running, pr.error, pr.note = True, "", ""
        pr.cancel.clear()

    def run() -> None:
        try:
            res = update_index(ws, cfg, embedder, pr, rebuild=rebuild)
            pr.note = f"index up to date: {res['files']} files ({res['chunks']} chunks) embedded, {res['removed']} removed"
        except EmbedError as e:
            pr.error = str(e)
        except Exception as e:                  # never take the agent down
            pr.error = f"{type(e).__name__}: {e}"
        finally:
            pr.running, pr.finished = False, time.time()
            if on_done:
                try:
                    on_done(pr)
                except Exception:
                    pass

    threading.Thread(target=run, name="axon-semindex", daemon=True).start()
    return pr


# ─── search ──────────────────────────────────────────────────────────────────────────────────────────────

def _scores(st: Store, q: list[float]) -> list[float]:
    dim = st.dim
    try:
        import numpy as np
        m = np.frombuffer(st.vecs, dtype=np.float32).reshape(st.n, dim)
        return (m @ np.asarray(q, dtype=np.float32)).tolist()
    except ImportError:
        pass
    v, out = st.vecs, []
    for i in range(st.n):
        o = i * dim
        out.append(sum(v[o + j] * q[j] for j in range(dim)))
    return out


def stale_files(ws: Path, st: Store, rels: set[str] | None = None) -> set[str]:
    """Indexed files that changed or vanished since they were embedded."""
    out: set[str] = set()
    for rel in (rels if rels is not None else set(st.files)):
        meta = st.files.get(rel)
        if not meta:
            continue
        try:
            s = (ws / rel).stat()
        except OSError:
            out.add(rel)
            continue
        if s.st_size != meta.get("size") or s.st_mtime != meta.get("mtime"):
            out.add(rel)
    return out


@dataclass
class Hit:
    path: str
    start: int
    end: int
    score: float
    stale: bool = False


def rank(st: Store, qvec: list[float], k: int, path_glob: str | None = None) -> list[Hit]:
    if not st.n or len(qvec) != st.dim:
        return []
    rx = None
    if path_glob:
        from axon.tools._files import glob_to_regex
        rx = glob_to_regex(path_glob)
    scores = _scores(st, qvec)
    order = sorted(range(st.n), key=lambda i: -scores[i])
    hits: list[Hit] = []
    for i in order:
        rel, a, b = st.chunks[i]
        if rx is not None and not rx.match(rel):
            continue
        if any(h.path == rel and a <= h.end and b >= h.start for h in hits):
            continue                         # overlaps a better chunk of the same file
        hits.append(Hit(rel, a, b, float(scores[i])))
        if len(hits) >= k:
            break
    return hits


def snippet(ws: Path, hit: Hit) -> str:
    text = read_text(ws, hit.path)
    if text is None:
        return ""
    lines = text.splitlines()[hit.start - 1:hit.end]
    body = "\n".join(ln.rstrip()[:160] for ln in lines[:SNIPPET_LINES])
    if len(lines) > SNIPPET_LINES:
        body += f"\n… ({len(lines) - SNIPPET_LINES} more lines)"
    return body[:SNIPPET_CHARS]


def search(ws: Path, cfg: EmbedConfig, embedder: Embedder, query: str, k: int = 8, path_glob: str | None = None) -> tuple[list[Hit], dict[str, Any]]:
    """Ranked hits plus info ({'stale': [...], 'refreshed': [...]}). Changed files among the hits are re-embedded first
    (at most LAZY_MAX_FILES files / LAZY_MAX_CHUNKS chunks, only when sending is allowed); the rest are flagged."""
    ws = ws.resolve()
    st = load_store(ws)
    if not st.n:
        return [], {"stale": [], "refreshed": []}
    if st.identity != cfg.identity:
        raise EmbedError(f"the index was built with {st.identity.replace('|', ' / ')}; run /index --rebuild for {cfg.backend} / {cfg.model}")
    qvec = embedder.embed([query], "query")[0]
    hits = rank(st, qvec, k, path_glob)
    info: dict[str, Any] = {"stale": [], "refreshed": []}
    bad = sorted(stale_files(ws, st, {h.path for h in hits}))
    if bad and has_consent(ws, cfg):
        todo = [r for r in bad if (ws / r).exists()][:LAZY_MAX_FILES]
        try:
            if todo:
                update_index(ws, cfg, embedder, only=todo, max_chunks=LAZY_MAX_CHUNKS)
                info["refreshed"] = todo
        except EmbedError:
            pass
        st = load_store(ws)
        hits = rank(st, qvec, k, path_glob)
        bad = sorted(stale_files(ws, st, {h.path for h in hits}))
    for h in hits:
        h.stale = h.path in bad
    info["stale"] = bad
    return hits, info
