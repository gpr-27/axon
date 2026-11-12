"""Eval run storage (~/.axon/evals/<run-id>/), summaries and run-to-run comparison."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from axon.evals.tasks import Task


def evals_root() -> Path:
    from axon.storage import axon_home
    return axon_home() / "evals"


def run_path(run_id: str) -> Path:
    return evals_root() / run_id


def resolve_run(ref: str) -> Path | None:
    """A run directory by id, unique id prefix, or 'last'."""
    root = evals_root()
    if not root.is_dir():
        return None
    ids = sorted(p.name for p in root.iterdir() if (p / "summary.json").is_file())
    if ref in ("last", "latest"):
        return root / ids[-1] if ids else None
    if ref in ids:
        return root / ref
    m = [i for i in ids if i.startswith(ref)]
    return root / m[0] if len(m) == 1 else None


def avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def combo_key(r: dict[str, Any]) -> tuple[str, str]:
    return (r.get("model") or "", r.get("effort") or "")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    combos: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in results:
        combos.setdefault(combo_key(r), []).append(r)
    rows = []
    for (m, e), rs in combos.items():
        passed = sum(1 for r in rs if r["status"] == "pass")
        failed: dict[str, str] = {}
        for r in rs:
            if r["status"] != "pass":
                failed.setdefault(r["task"], r.get("failed_check") or r["status"])
        rows.append({"model": m, "effort": e, "runs": len(rs), "passed": passed,
                     "pass_rate": round(passed / len(rs), 4) if rs else 0.0,
                     "avg_steps": round(avg([float(r.get("steps", 0)) for r in rs]), 2),
                     "avg_cost": round(avg([float(r.get("cost", 0)) for r in rs]), 6),
                     "avg_time": round(avg([float(r.get("duration_s", 0)) for r in rs]), 2),
                     "failed": failed})
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "pass")
    return {"combos": rows, "runs": total, "passed": ok, "pass_rate": round(ok / total, 4) if total else 0.0,
            "total_cost": round(sum(float(r.get("cost", 0)) for r in results), 6)}


def save_run(run_id: str, meta: dict[str, Any], tasks: list[Task], results: list[dict[str, Any]]) -> Path:
    d = run_path(run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
    (d / "tasks.jsonl").write_text("".join(json.dumps(t.to_dict()) + "\n" for t in tasks), encoding="utf-8")
    summary = {"id": run_id, "created": meta.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"), **meta, **summarize(results)}
    (d / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return d


def load_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    results = [json.loads(ln) for ln in (path / "results.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    return summary, results


def load_run_tasks(path: Path) -> list[Task]:
    from axon.evals.tasks import parse_task
    f = path / "tasks.jsonl"
    if not f.is_file():
        return []
    from axon.evals.tasks import builtin_tasks
    stock = {t.id: t.to_dict() for t in builtin_tasks()}
    out = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            raw = json.loads(ln)
            out.append(parse_task(raw, builtin=stock.get(raw.get("id")) == raw))     # unchanged built-ins stay exempt
    return out


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    root = evals_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir(), reverse=True):
        try:
            out.append(json.loads((p / "summary.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out


def task_rates(results: list[dict[str, Any]]) -> dict[tuple[str, str, str], tuple[int, int]]:
    """{(task, model, effort): (passed, runs)}."""
    out: dict[tuple[str, str, str], list[int]] = {}
    for r in results:
        k = (r["task"], *combo_key(r))
        v = out.setdefault(k, [0, 0])
        v[0] += 1 if r["status"] == "pass" else 0
        v[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def compare(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    """Per (task, model, effort) pass-rate change from run *a* to run *b*."""
    ra, rb = task_rates(a), task_rates(b)
    reg, imp, same, only = [], [], 0, []
    for k in sorted(set(ra) & set(rb)):
        (pa, na), (pb, nb) = ra[k], rb[k]
        fa, fb = pa / na, pb / nb
        item = {"task": k[0], "model": k[1], "effort": k[2], "a": f"{pa}/{na}", "b": f"{pb}/{nb}"}
        if fb < fa:
            reg.append(item)
        elif fb > fa:
            imp.append(item)
        else:
            same += 1
    for k in sorted(set(ra) ^ set(rb)):
        only.append({"task": k[0], "model": k[1], "effort": k[2], "in": "a" if k in ra else "b"})
    sa, sb = summarize(a), summarize(b)
    return {"regressions": reg, "improvements": imp, "unchanged": same, "only_in_one": only,
            "pass_rate_a": sa["pass_rate"], "pass_rate_b": sb["pass_rate"],
            "cost_a": sa["total_cost"], "cost_b": sb["total_cost"]}
