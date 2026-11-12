"""Running batch jobs: the Anthropic Message Batches client, the local worker pool and the operations on a stored job
(submit, refresh, results, cancel, retry, resume). No printing here; ``batch_cmd`` owns the commands."""
from __future__ import annotations

import json
import random
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from axon.features import batch_core as bc
from axon.features.batch_core import BatchError, Store


# ─── Anthropic Message Batches (official host) ────────────────────────────
class AnthropicBatches:
    """Thin client for /v1/messages/batches. *transport* is an httpx transport (tests pass a MockTransport)."""

    def __init__(self, base_url: str, api_key: str, transport: Any = None, timeout: float = 60.0) -> None:
        import httpx
        from axon.providers.anthropic_wire import api_version
        root = base_url.rstrip("/").removesuffix("/v1")
        self.base = f"{root}/v1/messages/batches"
        self.client = httpx.Client(transport=transport, timeout=timeout, headers={
            "x-api-key": api_key, "anthropic-version": api_version(base_url), "content-type": "application/json"})

    def _check(self, r: Any) -> Any:
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise BatchError(f"Anthropic answered {r.status_code}: {msg or r.reason_phrase}")
        return r

    def create(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        return self._check(self.client.post(self.base, json={"requests": requests})).json()

    def retrieve(self, batch_id: str) -> dict[str, Any]:
        return self._check(self.client.get(f"{self.base}/{batch_id}")).json()

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._check(self.client.get(self.base, params={"limit": limit})).json().get("data", [])

    def cancel(self, batch_id: str) -> dict[str, Any]:
        return self._check(self.client.post(f"{self.base}/{batch_id}/cancel")).json()

    def delete(self, batch_id: str) -> None:
        self._check(self.client.delete(f"{self.base}/{batch_id}"))

    def results(self, batch_id: str) -> Iterator[dict[str, Any]]:
        with self.client.stream("GET", f"{self.base}/{batch_id}/results") as r:
            if r.status_code >= 400:
                r.read()
                self._check(r)
            for line in r.iter_lines():
                if line.strip():
                    yield json.loads(line)


def native_credentials(settings: Any) -> tuple[str, str] | None:
    """(base_url, key) when the active provider is the official Anthropic host with a key, else None."""
    from axon.providers.transport import is_official_anthropic
    base = str(getattr(settings, "base_url", "") or "")
    if not is_official_anthropic(base):
        return None
    try:
        key = settings.api_key.get_secret_value()
    except Exception:
        key = ""
    return (base, key) if key and key != "local" else None


# ─── local worker pool (every other provider) ─────────────────────────────
CallFn = Callable[[dict[str, Any]], dict[str, Any]]      # params -> an Anthropic-shaped message dict


def provider_call(settings: Any) -> CallFn:
    """A call function that runs one request through the configured provider (a provider per call: they hold per-stream state)."""
    def call(params: dict[str, Any]) -> dict[str, Any]:
        from axon.agent.loop import make_provider
        from axon.providers.base import TextBlock
        model = params["model"]
        prov = make_provider(model, settings)
        system = [{"type": "text", "text": params["system"]}] if params.get("system") else []
        for _ in prov.stream(model=model, system=system, messages=params["messages"], tools=[],
                             max_tokens=int(params["max_tokens"]), effort=None, thinking=False):
            pass
        turn = prov.finalize()
        u = turn.usage
        return {"model": model, "content": [{"type": "text", "text": "".join(b.text for b in turn.blocks if isinstance(b, TextBlock))}],
                "stop_reason": turn.stop_reason,
                "usage": {"input_tokens": max(0, u.input - u.cache_read - u.cache_write), "output_tokens": u.output,
                          "cache_read_input_tokens": u.cache_read, "cache_creation_input_tokens": u.cache_write}}
    return call


def _rate_limited(e: BaseException) -> bool:
    t = f"{getattr(e, 'status_code', '')} {e}".lower()
    return any(k in t for k in ("429", "rate limit", "rate_limit", "too many requests", "overloaded", "529", "503"))


class LocalPool:
    """Runs a job's pending items with bounded concurrency. Persists after every item, so a restart can resume."""

    def __init__(self, store: Store, job: dict[str, Any], call: CallFn, *, workers: int = 4, retries: int = 5,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store, self.job, self.call = store, job, call
        self.workers, self.retries, self.sleep = max(1, workers), retries, sleep
        self.cancel_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _one(self, req: dict[str, Any]) -> None:
        cid = req["custom_id"]
        line: dict[str, Any]
        for attempt in range(self.retries + 1):
            if self.cancel_flag.is_set():
                line = {"custom_id": cid, "result": {"type": "canceled"}}
                break
            try:
                msg = self.call(req["params"])
                line = {"custom_id": cid, "result": {"type": "succeeded", "message": msg}}
                break
            except Exception as e:                      # noqa: BLE001 - every provider error becomes a per-item error
                if _rate_limited(e) and attempt < self.retries:
                    self.sleep(min(60.0, (2 ** attempt) + random.random()))
                    continue
                line = {"custom_id": cid, "result": {"type": "errored", "error": {"type": "api_error", "message": str(e)[:500]}}}
                break
        with self._lock:
            bc.record_result(self.job, line, Path(self.job["out_dir"]))
            if self.cancel_flag.is_set() and self.job["status"] == "in_progress":
                self.job["status"] = "canceling"
            self.store.save(self.job)

    def run(self, requests: list[dict[str, Any]]) -> None:
        """Run *requests* (blocking)."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            list(ex.map(self._one, requests))
        with self._lock:
            if self.cancel_flag.is_set():
                self.job["status"] = "canceled"
            bc.finish_if_done(self.job)
            if self.job["status"] == "canceling":
                self.job["status"] = "canceled"
            self.job["ended_at"] = self.job.get("ended_at") or bc.now_iso()
            bc.write_summary(self.job)
            self.store.save(self.job)

    def start(self, requests: list[dict[str, Any]]) -> threading.Thread:
        self.thread = threading.Thread(target=self.run, args=(requests,), name=f"batch-{self.job['id']}", daemon=True)
        self.thread.start()
        return self.thread


_POOLS: dict[str, LocalPool] = {}


# ─── operations ───────────────────────────────────────────────────────────
class Runner:
    poll_first = bc.POLL_FIRST_S
    def __init__(self, settings: Any, store: Store | None = None, *, transport: Any = None, call: CallFn | None = None,
                 workers: int = 4, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings, self.store = settings, store or Store()
        self.transport, self._call, self.workers, self.sleep = transport, call, workers, sleep

    # -- backend choice
    def backend(self, force: str | None = None) -> str:
        if force == "local":
            return "local"
        native = native_credentials(self.settings) is not None
        if force == "anthropic" and not native:
            raise BatchError("The native batch API needs the official Anthropic host (api.anthropic.com) and a key. "
                             "Without --backend anthropic the jobs run through a local worker pool.")
        return "anthropic" if native else "local"

    def api(self) -> AnthropicBatches:
        cred = native_credentials(self.settings)
        if cred is None:
            raise BatchError("This batch lives on the Anthropic API, but the active provider is not the official host.")
        return AnthropicBatches(cred[0], cred[1], transport=self.transport)

    def plan(self, items: list[bc.Item], model: str, *, max_tokens: int, system: str, force: str | None = None) -> dict[str, Any]:
        reqs = bc.build_requests(items, model, max_tokens=max_tokens, system=system)
        backend = self.backend(force)
        size = bc.check_limits(reqs) if backend == "anthropic" else 0
        disc = bc.BATCH_DISCOUNT if backend == "anthropic" else Decimal(1)
        est, priced = bc.estimate_cost(reqs, model, discount=disc, provider=getattr(self.settings, "provider", None))
        return {"requests": reqs, "backend": backend, "bytes": size, "estimate": est, "priced": priced, "discount": disc}

    def submit(self, plan: dict[str, Any], model: str, out_dir: str | None = None, *, start_pool: bool = True) -> dict[str, Any]:
        reqs, backend = plan["requests"], plan["backend"]
        if backend == "anthropic":
            created = self.api().create(reqs)
            bid = created["id"]
        else:
            bid = self.store.new_id("local")
        out = Path(out_dir).expanduser() if out_dir else self.store.dir(bid) / "results"
        job = bc.new_job(bid, backend, model, reqs, out, est_cost=plan["estimate"], discount=plan["discount"])
        if backend == "anthropic":
            self._apply_status(job, created)
        self.store.write_requests(bid, reqs)
        self.store.save(job)
        if backend == "local" and start_pool:
            self._pool(job).start(reqs)
        return job

    def _pool(self, job: dict[str, Any]) -> LocalPool:
        call = self._call or provider_call(self.settings)
        pool = LocalPool(self.store, job, call, workers=self.workers, sleep=self.sleep)
        _POOLS[job["id"]] = pool
        return pool

    @staticmethod
    def _apply_status(job: dict[str, Any], remote: dict[str, Any]) -> None:
        rc = remote.get("request_counts") or {}
        for k in ("processing", "succeeded", "errored", "canceled", "expired"):
            if k in rc:
                job["counts"][k] = int(rc[k] or 0)
        st = remote.get("processing_status")
        job["status"] = {"in_progress": "in_progress", "canceling": "canceling", "ended": "ended"}.get(st, job["status"])
        job["ended_at"] = remote.get("ended_at") or job.get("ended_at")
        job["expires_at"] = remote.get("expires_at")

    def refresh(self, bid: str, *, download: bool = True) -> dict[str, Any]:
        """Bring a stored job up to date (one retrieve call for a native batch; downloads results once it has ended)."""
        job = self.store.load(bid)
        if job["backend"] == "anthropic" and (job["status"] != "ended" or (download and not self._downloaded(job))):
            remote = self.api().retrieve(job["id"])
            self._apply_status(job, remote)
            if remote.get("processing_status") == "ended" and download:
                self.download(job)
            self.store.save(job)
        elif job["backend"] == "local":
            pool = _POOLS.get(job["id"])
            alive = pool is not None and pool.thread is not None and pool.thread.is_alive()
            if job["status"] in ("in_progress", "canceling") and not alive:
                job["note"] = f"interrupted: `/batch resume {job['id']}` runs the {job['counts'].get('processing', 0)} remaining item(s)"
        return job

    @staticmethod
    def _downloaded(job: dict[str, Any]) -> bool:
        return job["counts"].get("processing", 0) == 0 and Path(job["out_dir"], "summary.json").is_file()

    def download(self, job: dict[str, Any]) -> int:
        """Stream a native batch's results into files keyed by custom_id and write the summary."""
        n = 0
        remote_end = job.get("ended_at")
        job["counts"] = {"processing": job["total"], "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}   # recounted from the lines
        job["items"] = {c: "pending" for c in job["items"]}
        job["usage"], job["cost"] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "0"
        for line in self.api().results(job["id"]):
            bc.record_result(job, line, Path(job["out_dir"]))
            n += 1
        job["ended_at"] = remote_end
        bc.finish_if_done(job)
        bc.write_summary(job)
        self.store.save(job)
        return n

    def cancel(self, bid: str) -> dict[str, Any]:
        job = self.store.load(bid)
        if job["status"] in bc.TERMINAL:
            return job
        if job["backend"] == "anthropic":
            self._apply_status(job, self.api().cancel(job["id"]))
        else:
            pool = _POOLS.get(job["id"])
            if pool:
                pool.cancel_flag.set()
            else:                                         # nothing is running: mark the pending items canceled
                for cid, st in job["items"].items():
                    if st == "pending":
                        bc.record_result(job, {"custom_id": cid, "result": {"type": "canceled"}}, Path(job["out_dir"]))
                job["status"] = "canceled"
                bc.write_summary(job)
        self.store.save(job)
        return job

    def retry(self, bid: str) -> dict[str, Any]:
        """A new job holding the errored, expired and canceled items of *bid*."""
        old = self.store.load(bid)
        ids = set(bc.failed_ids(old))
        if not ids:
            raise BatchError(f"Nothing to retry in {old['id']}: no errored, expired or canceled items.")
        reqs = [r for r in self.store.read_requests(old["id"]) if r["custom_id"] in ids]
        if not reqs:
            raise BatchError(f"The stored requests of {old['id']} are gone, so it cannot be retried.")
        plan = {"requests": reqs, "backend": old["backend"], "discount": Decimal(old.get("discount", "1")),
                "estimate": Decimal(0), "priced": True}
        job = self.submit(plan, old["model"], None)
        job["note"] = f"retry of {old['id']}"
        self.store.save(job)
        return job

    def resume(self, bid: str) -> dict[str, Any]:
        """Run the unfinished items of a local job again (after a restart)."""
        job = self.store.load(bid)
        if job["backend"] != "local":
            raise BatchError("Only local jobs are resumed; a native batch keeps running on Anthropic's side (`/batch status`).")
        pool = _POOLS.get(job["id"])
        if pool and pool.thread and pool.thread.is_alive():
            raise BatchError(f"{job['id']} is still running.")
        pending = {c for c, s in job["items"].items() if s == "pending"}
        reqs = [r for r in self.store.read_requests(job["id"]) if r["custom_id"] in pending]
        job["status"], job["note"], job["ended_at"] = "in_progress", "", None
        self.store.save(job)
        self._pool(job).start(reqs)
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        return self.store.list()


def poll_in_background(runner: Runner, bid: str, on_done: Callable[[dict[str, Any]], None] | None = None,
                       *, first: float = bc.POLL_FIRST_S, longest: float = bc.POLL_MAX_S, give_up: float = bc.POLL_GIVE_UP_S,
                       sleep: Callable[[float], None] = time.sleep) -> threading.Thread:
    """Poll a job on a daemon thread (growing interval, bounded by *give_up* seconds) and call *on_done* once it ends."""
    def loop() -> None:
        waited, delay = 0.0, first
        while waited < give_up:
            try:
                job = runner.refresh(bid)
            except Exception:
                job = None
            if job and job["status"] in bc.TERMINAL:
                if on_done:
                    on_done(job)
                return
            sleep(delay)
            waited += delay
            delay = min(longest, delay * 1.5)
    t = threading.Thread(target=loop, name=f"batch-poll-{bid}", daemon=True)
    t.start()
    return t
