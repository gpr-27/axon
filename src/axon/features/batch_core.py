"""Batch jobs: many independent prompts run at once, results kept as files keyed by ``custom_id``.

Two backends behind one record format and one set of commands:

* ``anthropic``: the official host only. The jobs go through ``POST /v1/messages/batches`` (half price, results
  within 24 h, up to 100,000 requests or 256 MB per batch, results kept 29 days; docs: build-with-claude/batch-processing).
  No beta header is needed. A batch is created, polled in the background and its ``.jsonl`` results downloaded.
* ``local``: every other provider. The same jobs run through a bounded worker pool on this machine (backoff on 429,
  resumable after a restart, normal price). OpenAI-compatible ``/v1/batches`` is not used: no provider documented to Axon.

A job is stored under ``~/.axon/batches/<id>/``: ``job.json`` (status, counts, cost), ``requests.jsonl`` (what was
sent, so errored items can be retried) and ``results/<custom_id>.md`` plus ``.json`` (the raw result) once done.
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

MAX_REQUESTS = 100_000
MAX_BYTES = 256 * 1024 * 1024
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
BATCH_DISCOUNT = Decimal("0.5")
DEFAULT_MAX_TOKENS = 4096
CONFIRM_ABOVE_USD = Decimal("1.00")
POLL_FIRST_S, POLL_MAX_S, POLL_GIVE_UP_S = 15.0, 120.0, 25 * 3600      # a batch expires after 24 h
TERMINAL = {"ended", "canceled", "failed"}


class BatchError(Exception):
    """A problem the user can act on (bad input, wrong host, limit hit)."""


# ─── input ────────────────────────────────────────────────────────────────
@dataclass
class Item:
    custom_id: str
    prompt: str
    label: str = ""          # where it came from (a path or a row number)


def safe_custom_id(raw: str, taken: set[str]) -> str:
    """*raw* as a valid, unique custom_id (letters, digits, '-' and '_', 1..64 characters)."""
    cid = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw)).strip("_") or "item"
    if len(cid) > 64:
        cid = f"{cid[:55]}_{hashlib.sha1(str(raw).encode()).hexdigest()[:8]}"
    base, n = cid, 1
    while cid in taken:
        n += 1
        suffix = f"_{n}"
        cid = base[:64 - len(suffix)] + suffix
    taken.add(cid)
    return cid


def render_template(template: str, values: dict[str, Any]) -> str:
    """Replace ``{name}`` for each known name; every other brace (code, JSON) is left alone."""
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)


def _is_glob(spec: str) -> bool:
    return any(c in spec for c in "*?[")


def load_items(inputs: list[str], template: str, *, base: Path | None = None, max_file_bytes: int = 1_000_000) -> list[Item]:
    """Items from globs / file paths (one item per file: ``{path}``, ``{name}``, ``{content}``), a ``.jsonl`` file
    (one object per line: ``custom_id``, ``prompt`` or ``messages``, other keys fill the template) or a ``.csv`` file
    (one row per item; a ``custom_id`` column is used when present). Without a template, a row's ``prompt`` is used."""
    base = base or Path.cwd()
    taken: set[str] = set()
    items: list[Item] = []
    if not inputs:
        raise BatchError("No input. Give --input with a glob (src/**/*.py), a .jsonl file or a .csv file.")
    for spec in inputs:
        p = Path(os.path.expanduser(spec))
        if not p.is_absolute():
            p = base / p
        suffix = p.suffix.lower()
        if not _is_glob(spec) and p.is_file() and suffix in (".jsonl", ".csv"):
            rows = _read_rows(p)
            for n, row in enumerate(rows, 1):
                row = dict(row)
                cid = safe_custom_id(row.pop("custom_id", "") or f"{p.stem}-{n}", taken)
                if isinstance(row.get("messages"), list):
                    items.append(Item(cid, json.dumps(row["messages"]), f"{p.name}:{n}"))
                    continue
                prompt = row.get("prompt") if not template else None
                if prompt is None:
                    if not template:
                        raise BatchError(f"{p.name} row {n} has no 'prompt' and no template was given.")
                    prompt = render_template(template, row)
                items.append(Item(cid, str(prompt), f"{p.name}:{n}"))
            continue
        paths = sorted(Path(x) for x in glob.glob(str(p), recursive=True)) if _is_glob(spec) else ([p] if p.exists() else [])
        files = [x for x in paths if x.is_file()]
        if p.is_dir():
            files = sorted(x for x in p.rglob("*") if x.is_file() and not any(part.startswith(".") for part in x.relative_to(p).parts))
        if not files:
            raise BatchError(f"Nothing matches {spec!r}.")
        for f in files:
            try:
                if f.stat().st_size > max_file_bytes:
                    raise BatchError(f"{f} is {f.stat().st_size // 1024} KB; a batch item is limited to {max_file_bytes // 1024} KB. "
                                     "Split it or leave it out.")
                content = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue                     # binary file picked up by a broad glob
            try:
                rel = str(f.resolve().relative_to(base.resolve()))
            except ValueError:
                rel = str(f)
            values = {"path": rel, "file": rel, "name": f.name, "content": content, "ext": f.suffix.lstrip(".")}
            prompt = render_template(template, values) if template else content
            if template and "{content}" not in template:
                prompt = f"{prompt}\n\nFile: {rel}\n\n{content}"
            items.append(Item(safe_custom_id(rel, taken), prompt, rel))
    if not items:
        raise BatchError("The input produced no items.")
    return items


def _read_rows(p: Path) -> list[dict[str, Any]]:
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        return [dict(r) for r in csv.DictReader(text.splitlines())]
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise BatchError(f"{p.name} line {n} is not valid JSON ({e}).") from e
        if isinstance(obj, str):
            obj = {"prompt": obj}
        if not isinstance(obj, dict):
            raise BatchError(f"{p.name} line {n} must be a JSON object or a string.")
        rows.append(obj)
    return rows


# ─── requests ─────────────────────────────────────────────────────────────
def build_requests(items: list[Item], model: str, *, max_tokens: int = DEFAULT_MAX_TOKENS, system: str = "") -> list[dict[str, Any]]:
    """The ``requests`` array of the batch create call (also what the local pool runs)."""
    out = []
    for it in items:
        content: Any = it.prompt
        try:                                  # a JSONL row with "messages" was stored as JSON text
            parsed = json.loads(it.prompt)
            if isinstance(parsed, list) and parsed and all(isinstance(m, dict) and "role" in m for m in parsed):
                out.append({"custom_id": it.custom_id, "params": _params(model, max_tokens, system, parsed)})
                continue
        except ValueError:
            pass
        out.append({"custom_id": it.custom_id, "params": _params(model, max_tokens, system, [{"role": "user", "content": content}])})
    return out


def _params(model: str, max_tokens: int, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    p: dict[str, Any] = {"model": model, "max_tokens": max(1, int(max_tokens)), "messages": messages}
    if system:
        p["system"] = system
    return p


def check_limits(requests: list[dict[str, Any]]) -> int:
    """Raise BatchError when the batch is over the documented limits; return its size in bytes."""
    if len(requests) > MAX_REQUESTS:
        raise BatchError(f"{len(requests):,} requests is over the {MAX_REQUESTS:,} limit for one batch. Split the input.")
    size = len(json.dumps({"requests": requests}).encode("utf-8"))
    if size > MAX_BYTES:
        raise BatchError(f"The batch is {size / 1_048_576:.0f} MB, over the {MAX_BYTES // 1_048_576} MB limit. Split the input.")
    return size


def estimate_cost(requests: list[dict[str, Any]], model: str, *, discount: Decimal = BATCH_DISCOUNT, provider: str | None = None) -> tuple[Decimal, bool]:
    """(USD estimate, priced). Input is chars/4; output is assumed to be min(max_tokens, 1024) per request."""
    from axon.providers import pricing
    tin = sum(len(json.dumps(r["params"].get("messages"))) + len(str(r["params"].get("system") or "")) for r in requests) // 4
    tout = sum(min(int(r["params"].get("max_tokens", 1024)), 1024) for r in requests)
    res = pricing.cost_details(model, {"input": tin, "output": tout}, provider=provider)
    return res.cost * discount, res.priced


# ─── store ────────────────────────────────────────────────────────────────
def batches_dir() -> Path:
    from axon import storage
    return storage.ensure_dir(storage.axon_home() / "batches")


_LOCK = threading.RLock()


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or batches_dir()

    def dir(self, bid: str) -> Path:
        return self.root / bid

    def new_id(self, backend: str) -> str:
        return ("local_" + uuid.uuid4().hex[:12]) if backend == "local" else ""

    def save(self, job: dict[str, Any]) -> None:
        with _LOCK:
            d = self.dir(job["id"])
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "job.json.tmp"
            tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
            tmp.replace(d / "job.json")

    def load(self, bid: str) -> dict[str, Any]:
        job = self.find(bid)
        if job is None:
            raise BatchError(f"No batch {bid!r}. `/batch list` shows the known ones.")
        return job

    def find(self, bid: str) -> dict[str, Any] | None:
        """Exact id, else a unique prefix (or the newest with no id given)."""
        with _LOCK:
            if not bid:
                jobs = self.list()
                return jobs[0] if jobs else None
            f = self.dir(bid) / "job.json"
            if f.is_file():
                return json.loads(f.read_text(encoding="utf-8"))
            hits = [j for j in self.list() if j["id"].startswith(bid)]
            return hits[0] if len(hits) == 1 else None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for f in self.root.glob("*/job.json"):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda j: j.get("created_at", ""), reverse=True)

    def write_requests(self, bid: str, requests: list[dict[str, Any]]) -> None:
        d = self.dir(bid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "requests.jsonl").write_text("\n".join(json.dumps(r) for r in requests) + "\n", encoding="utf-8")

    def read_requests(self, bid: str) -> list[dict[str, Any]]:
        f = self.dir(bid) / "requests.jsonl"
        if not f.is_file():
            return []
        return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def new_job(bid: str, backend: str, model: str, requests: list[dict[str, Any]], out_dir: Path, *, est_cost: Decimal = Decimal(0),
            discount: Decimal = Decimal(1)) -> dict[str, Any]:
    return {"id": bid, "backend": backend, "model": model, "created_at": now_iso(), "status": "in_progress",
            "total": len(requests), "counts": {"processing": len(requests), "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
            "items": {r["custom_id"]: "pending" for r in requests}, "out_dir": str(out_dir), "estimated_cost": str(est_cost),
            "discount": str(discount), "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "cost": "0",
            "ended_at": None, "note": ""}


# ─── results ──────────────────────────────────────────────────────────────
def result_text(message: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in message.get("content", []) if isinstance(b, dict) and b.get("type") == "text")


def usage_of(message: dict[str, Any]) -> dict[str, int]:
    """Axon's usage convention (input = the whole prompt) from an Anthropic usage object."""
    u = message.get("usage") or {}
    read, write = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
    return {"input": int(u.get("input_tokens") or 0) + read + write, "output": int(u.get("output_tokens") or 0),
            "cache_read": read, "cache_write": write}


def record_result(job: dict[str, Any], line: dict[str, Any], results_dir: Path) -> None:
    """Apply one result line (``{"custom_id", "result": {"type", ...}}``): write its files, update counts, usage and cost."""
    from axon.providers import pricing
    cid = str(line.get("custom_id", ""))
    res = line.get("result") or {}
    kind = str(res.get("type", ""))
    if not CUSTOM_ID_RE.match(cid) or cid not in job["items"]:
        return
    prev = job["items"].get(cid)
    if prev in ("succeeded", "errored", "canceled", "expired"):
        job["counts"][prev] = max(0, job["counts"].get(prev, 0) - 1)
    else:
        job["counts"]["processing"] = max(0, job["counts"].get("processing", 0) - 1)
    results_dir.mkdir(parents=True, exist_ok=True)
    for ext in (".md", ".json", ".error.json"):
        (results_dir / f"{cid}{ext}").unlink(missing_ok=True)
    kind = kind if kind in ("succeeded", "errored", "canceled", "expired") else "errored"
    if kind == "succeeded":
        msg = res.get("message") or {}
        (results_dir / f"{cid}.md").write_text(result_text(msg), encoding="utf-8")
        (results_dir / f"{cid}.json").write_text(json.dumps(line, indent=1), encoding="utf-8")
        u = usage_of(msg)
        for k, v in u.items():
            job["usage"][k] = int(job["usage"].get(k, 0)) + v
        add = pricing.cost_for(str(msg.get("model") or job["model"]), u) * Decimal(job.get("discount", "1"))
        job["cost"] = str(Decimal(job.get("cost", "0")) + add)
    else:
        (results_dir / f"{cid}.error.json").write_text(json.dumps(line, indent=1), encoding="utf-8")
    job["items"][cid] = kind
    job["counts"][kind] = job["counts"].get(kind, 0) + 1


def finish_if_done(job: dict[str, Any]) -> None:
    if job["counts"].get("processing", 0) == 0 and job["status"] not in TERMINAL:
        job["status"] = "ended"
    if job["status"] in TERMINAL and not job.get("ended_at"):
        job["ended_at"] = now_iso()


def write_summary(job: dict[str, Any]) -> Path:
    d = Path(job["out_dir"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / "summary.json"
    p.write_text(json.dumps({k: job[k] for k in ("id", "backend", "model", "status", "total", "counts", "usage", "cost", "discount")}, indent=1),
                 encoding="utf-8")
    return p


def failed_ids(job: dict[str, Any]) -> list[str]:
    return [c for c, s in job["items"].items() if s in ("errored", "expired", "canceled")]


def summary_line(job: dict[str, Any]) -> str:
    """One status line: ``id · status · 8/10 succeeded · 1 errored · 1 expired · $0.0123 (50% batch rate)``."""
    c = job["counts"]
    parts = [job["id"], job["status"], f"{c.get('succeeded', 0)}/{job['total']} succeeded"]
    for k in ("processing", "errored", "expired", "canceled"):
        if c.get(k):
            parts.append(f"{c[k]} {'running' if k == 'processing' else k}")
    cost = Decimal(job.get("cost", "0"))
    rate = " (50% batch rate)" if Decimal(job.get("discount", "1")) != 1 else ""
    if job["status"] in TERMINAL or cost:
        parts.append(f"${cost:.4f}{rate}")
    return " · ".join(parts)


def copy_results(job: dict[str, Any], dest: Path) -> int:
    """Copy a job's result files into *dest*; returns how many files."""
    import shutil
    src = Path(job["out_dir"])
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
            n += 1
        elif f.is_dir():
            shutil.copytree(f, dest / f.name, dirs_exist_ok=True)
            n += sum(1 for _ in f.iterdir())
    return n
