"""Run eval tasks: one fresh temp workspace per run, the real agent loop headless, exact checks then the rubric grader."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.evals import checks as ck
from axon.evals.tasks import Task

FILE_TOOLS = ("path", "file_path", "notebook_path")
SEED_COMMIT = "initial setup"


@dataclass
class RunConfig:
    models: list[str | None] = field(default_factory=lambda: [None])     # None: the active model
    efforts: list[str | None] = field(default_factory=lambda: [None])
    jobs: int = 2
    repeat: int = 1
    keep: bool = False
    base: dict[str, Any] = field(default_factory=dict)        # AxonOptions fields (provider, base_url, api_key, ...)
    provider_factory: Callable[[str | None, str | None], Any] | None = None      # tests: a fake provider per run
    grader_factory: Callable[[], Any] | None = None           # tests: a fake grader provider
    on_done: Callable[[dict[str, Any]], None] | None = None


def materialize(task: Task, ws: Path) -> None:
    for rel, body in task.setup_files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    if task.git:
        def git(*a: str) -> None:
            subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid", "-c", "commit.gpgsign=false", *a],
                           cwd=ws, capture_output=True, stdin=subprocess.DEVNULL, timeout=30, check=False)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", SEED_COMMIT)


def scoped_approver(ws: Path) -> Callable[[str, dict[str, Any]], bool]:
    """Approve tool calls only when every path argument stays inside the temp workspace (the run is never in the user's dir)."""
    root = ws.resolve()

    def inside(raw: Any) -> bool:
        try:
            p = Path(str(raw)).expanduser()
            p = (p if p.is_absolute() else root / p).resolve()
            return p == root or root in p.parents
        except Exception:
            return False

    def approve(tool: str, args: dict[str, Any]) -> bool:
        for k in FILE_TOOLS:
            if args.get(k) and not inside(args[k]):
                return False
        return True
    return approve


def _redact(text: str) -> str:
    try:
        from axon.session.activity import redact_text
        return redact_text(text)
    except Exception:
        return text


def save_transcript(src: Path | None, dest: Path) -> str:
    """Copy a session transcript with secrets masked. Returns the stored path ('' when there was none)."""
    if not src or not Path(src).is_file():
        return ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, encoding="utf-8", errors="replace") as fi, open(dest, "w", encoding="utf-8") as fo:
        for line in fi:
            fo.write(_redact(line))
    return str(dest)


def _options(cfg: RunConfig, task: Task, ws: Path, model: str | None, effort: str | None) -> Any:
    from axon.features.sdk import AxonOptions
    o = AxonOptions(workspace=ws, model=model, effort=effort, mode="default", max_turns=task.max_steps,
                    max_budget_usd=task.max_cost or None, can_use_tool=scoped_approver(ws), trace_level="compact")
    for k, v in cfg.base.items():
        if k not in ("workspace", "mode", "model", "effort", "can_use_tool") and v is not None:
            setattr(o, k, v)
    if cfg.provider_factory is not None:
        o.provider_instance = cfg.provider_factory(model, effort)
    return o


def run_one(task: Task, model: str | None, effort: str | None, rep: int, cfg: RunConfig, run_dir: Path) -> dict[str, Any]:
    """One task run. Never raises: failures to start are an ``error`` outcome."""
    t0 = time.time()
    res: dict[str, Any] = {"task": task.id, "model": model or "", "effort": effort or "", "rep": rep, "status": "error",
                           "failed_check": "", "detail": "", "checks": [], "steps": 0, "tool_calls": 0, "tools": [],
                           "tokens": {}, "cost": 0.0, "duration_s": 0.0, "transcript": "", "stop_reason": ""}
    ws = Path(tempfile.mkdtemp(prefix="axon-eval-")).resolve()
    sess: Any = None
    try:
        materialize(task, ws)
        before = ck.snapshot(ws)
        from axon.features.sdk import AxonSession
        sess = AxonSession(_options(cfg, task, ws, model, effort))
        res["model"] = res["model"] or str(sess.agent.settings.model)
        timer = threading.Timer(task.timeout, sess.cancel)
        timer.daemon = True
        timer.start()
        tools: list[str] = []
        final: dict[str, Any] = {}
        try:
            for ev in sess.query(task.prompt):
                if ev.get("type") == "tool_use":
                    tools.append(str(ev.get("name") or ""))
                elif ev.get("type") == "result":
                    final = ev
        finally:
            timer.cancel()
        res.update(steps=int(final.get("iterations", 0)), tool_calls=len(tools), tools=tools, tokens=final.get("usage") or {},
                   cost=float(final.get("cost_usd", 0.0)), stop_reason=str(final.get("stop_reason", "")))
        answer = str(final.get("result", ""))
        timed_out = final.get("stop_reason") == "interrupted"
        results = ck.run_checks(task, ws, before=before, answer=answer, tools=tools)
        failed = [c for c in results if not c.passed]
        verdict_ok = True
        if task.rubric and not failed:
            verdict_ok = _grade_rubric(sess.agent, task, answer, cfg, res)
        res["checks"] = [c.to_dict() for c in results]
        if failed:
            res.update(status="fail", failed_check=failed[0].name, detail=failed[0].detail)
        elif not verdict_ok:
            res["status"] = "fail"
        else:
            res["status"] = "pass"
        if res["status"] == "fail" and timed_out:
            res["detail"] = ("timed out after %ds; " % task.timeout) + res["detail"]
        elif res["status"] == "fail" and final.get("is_error") and not res["detail"]:
            res["detail"] = f"run ended with {res['stop_reason']}: {str(final.get('error') or answer)[:120]}"
        res["transcript"] = save_transcript(getattr(getattr(sess.agent, "session", None), "active_file", None),
                                            run_dir / "transcripts" / f"{task.id}__{_slug(res['model'])}__{effort or 'default'}__{rep}.jsonl")
    except Exception as e:                           # noqa: BLE001 - one broken run must not end the suite
        res.update(status="error", detail=f"{type(e).__name__}: {e}"[:300])
    finally:
        try:
            if sess is not None:
                sess.close()
        except Exception:
            pass
        res["duration_s"] = round(time.time() - t0, 2)
        res["detail"] = _redact(str(res["detail"]))
        if cfg.keep:
            res["workspace"] = str(ws)
        else:
            shutil.rmtree(ws, ignore_errors=True)
    return res


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in s)[:60]


def _grade_rubric(agent: Any, task: Task, answer: str, cfg: RunConfig, res: dict[str, Any]) -> bool:
    """The /goal grader (fresh context, strict JSON, small model) on the rubric; adds its cost to the run."""
    from axon.agent import goal as goal_mod
    if cfg.grader_factory is not None:
        agent.goal_provider = cfg.grader_factory()
    g = goal_mod.grade(agent, task.rubric, task.prompt, answer)
    res["cost"] = round(float(res["cost"]) + float(g.cost or 0.0), 8)
    v = g.verdict or {}
    res["grader"] = {"model": g.model, "verdict": v.get("verdict"), "summary": v.get("summary", "")}
    if v.get("verdict") == "satisfied":
        return True
    gaps = [c for c in (v.get("per_criterion") or []) if isinstance(c, dict) and not c.get("met")]
    name = "rubric: " + str((gaps[0].get("criterion") if gaps else v.get("summary")) or "not satisfied")[:80]
    res["checks"].append({"name": name, "passed": False, "detail": str((gaps[0].get("gap") if gaps else "") or v.get("summary") or "")[:200]})
    res["failed_check"] = name
    res["detail"] = res["checks"][-1]["detail"]
    return False


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def expand_jobs(tasks: list[Task], cfg: RunConfig) -> list[tuple[Task, str | None, str | None, int]]:
    return [(t, m, e, r) for t in tasks for m in cfg.models for e in cfg.efforts for r in range(1, max(1, cfg.repeat) + 1)]


def run_jobs(jobs: list[tuple[Task, str | None, str | None, int]], cfg: RunConfig, run_dir: Path,
             on_result: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    """Run *jobs* with bounded concurrency; each result goes to *on_result* as it finishes. Ordered like *jobs*."""
    out: list[dict[str, Any] | None] = [None] * len(jobs)
    lock = threading.Lock()

    def work(i: int) -> None:
        t, m, e, r = jobs[i]
        res = run_one(t, m, e, r, cfg, run_dir)
        out[i] = res
        if on_result:
            with lock:
                on_result(res)

    with ThreadPoolExecutor(max_workers=max(1, int(cfg.jobs))) as ex:
        list(ex.map(work, range(len(jobs))))
    return [r for r in out if r is not None]
