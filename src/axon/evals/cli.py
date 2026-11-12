"""``axon eval`` and ``/eval``: argument parsing and the run / list / show / compare / rerun-failed commands."""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable

from axon.evals import report, store
from axon.evals.runner import RunConfig, expand_jobs, new_run_id, run_jobs
from axon.evals.tasks import Task, TaskError, load_tasks, shell_task_ids

USAGE = """axon eval [run] [--models a,b] [--efforts low,high] [--repeat K] [--jobs N] [--tasks FILE|DIR]
                [--tag T] [--id ID,ID] [--min-pass PCT] [--json] [--keep] [--allow-commands]
axon eval list | show RUN [--json] | compare RUN_A RUN_B [--json] | rerun-failed RUN [flags]

Runs a golden suite (or your own task files: JSON, JSONL or TOML) through the real agent loop, each task in a fresh
temp workspace, then grades with exact checks and, for tasks with a rubric, the /goal grader. Results are stored under
~/.axon/evals/<run-id>/. Exit code 1 when the pass rate is below --min-pass."""

VALUE_FLAGS = {"--models", "--efforts", "--repeat", "--jobs", "--tasks", "--tag", "--id", "--min-pass"}
BOOL_FLAGS = {"--json", "--keep", "--allow-commands", "--help", "-h"}


class UsageError(Exception):
    pass


def parse_flags(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    opts: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if name in VALUE_FLAGS:
            if not eq:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"{name} needs a value")
                val = argv[i]
            opts[name.lstrip("-").replace("-", "_")] = val
        elif a in BOOL_FLAGS:
            opts[a.lstrip("-").replace("-", "_")] = True
        elif a.startswith("-"):
            raise UsageError(f"unknown option {a}")
        else:
            rest.append(a)
        i += 1
    return opts, rest


def _csv(v: Any) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _threshold(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(str(v).rstrip("%"))
    except ValueError as e:
        raise UsageError("--min-pass must be a number, 0-1 or a percentage") from e
    return x / 100 if x > 1 else x


def _slim(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("task", "model", "effort", "rep", "status", "failed_check", "detail", "steps", "tool_calls",
                                  "cost", "duration_s", "transcript")}


def select_tasks(opts: dict[str, Any]) -> list[Task]:
    tasks = load_tasks(opts.get("tasks"))
    if opts.get("tag"):
        want = set(_csv(opts["tag"]))
        tasks = [t for t in tasks if want & set(t.tags)]
    if opts.get("id"):
        want = set(_csv(opts["id"]))
        missing = want - {t.id for t in tasks}
        if missing:
            raise UsageError("no such task id: " + ", ".join(sorted(missing)))
        tasks = [t for t in tasks if t.id in want]
    if not tasks:
        raise UsageError("no tasks selected")
    return tasks


def check_safety(tasks: list[Task], opts: dict[str, Any]) -> str:
    """Why this run must not start ('' when it may): user task files with shell checks need --allow-commands."""
    ids = shell_task_ids(tasks)
    if ids and not opts.get("allow_commands"):
        return ("these tasks run shell commands from the task file: " + ", ".join(ids[:8]) + (" ..." if len(ids) > 8 else "")
                + ". Read them, then pass --allow-commands (built-in tasks are exempt).")
    return ""


def _config(opts: dict[str, Any], base: dict[str, Any] | None, defaults: dict[str, Any]) -> RunConfig:
    def num(name: str, default: int) -> int:
        try:
            return max(1, int(opts.get(name, default)))
        except (TypeError, ValueError) as e:
            raise UsageError(f"--{name} must be a whole number") from e
    models = _csv(opts.get("models")) or defaults.get("models") or [None]
    efforts = _csv(opts.get("efforts")) or defaults.get("efforts") or [None]
    return RunConfig(models=list(models), efforts=list(efforts), jobs=num("jobs", defaults.get("jobs", 2)),
                     repeat=num("repeat", 1), keep=bool(opts.get("keep")), base=dict(base or {}))


def _execute(tasks: list[Task], jobs: list, cfg: RunConfig, meta: dict[str, Any], opts: dict[str, Any], out: Callable[[str], None],
             min_pass: float | None) -> int:
    run_id = new_run_id()
    run_dir = store.run_path(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    as_json = bool(opts.get("json"))
    done = [0]

    def progress(res: dict[str, Any]) -> None:
        done[0] += 1
        if sys.stderr.isatty() and not as_json:
            sys.stderr.write(f"\r{done[0]}/{len(jobs)} runs")
            sys.stderr.flush()

    if not as_json:
        out(f"eval {run_id}: {len(tasks)} task(s) x {len(cfg.models)} model(s) x {len(cfg.efforts)} effort(s) x {cfg.repeat} "
            f"= {len(jobs)} run(s), {cfg.jobs} at a time")
    try:
        results = run_jobs(jobs, cfg, run_dir, progress)
    except KeyboardInterrupt:
        out("interrupted; nothing stored")
        return 130
    if sys.stderr.isatty() and not as_json:
        sys.stderr.write("\r" + " " * 20 + "\r")
    meta = {**meta, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "models": [m or "" for m in cfg.models],
            "efforts": [e or "" for e in cfg.efforts], "repeat": cfg.repeat}
    store.save_run(run_id, meta, tasks, results)
    summary = store.summarize(results)
    code = 1 if (min_pass is not None and summary["pass_rate"] < min_pass) else 0
    if as_json:
        out(json.dumps({"run_id": run_id, "path": str(run_dir), **summary, "min_pass": min_pass, "ok": code == 0,
                        "results": [_slim(r) for r in results]}, indent=2))
    else:
        out(report.table(summary))
        out(f"stored in {run_dir}")
        if min_pass is not None and code:
            out(f"pass rate {summary['pass_rate'] * 100:.0f}% is below --min-pass {min_pass * 100:.0f}%")
    return code


def cmd_run(opts: dict[str, Any], out: Callable[[str], None], base: dict[str, Any] | None, defaults: dict[str, Any],
            cfg_hook: Callable[[RunConfig], None] | None) -> int:
    tasks = select_tasks(opts)
    why = check_safety(tasks, opts)
    if why:
        out("axon eval: refused: " + why)
        return 2
    cfg = _config(opts, base, defaults)
    if cfg_hook:
        cfg_hook(cfg)
    return _execute(tasks, expand_jobs(tasks, cfg), cfg, {"tasks": opts.get("tasks") or "builtin"}, opts, out, _threshold(opts.get("min_pass")))


def cmd_rerun(ref: str, opts: dict[str, Any], out: Callable[[str], None], base: dict[str, Any] | None, defaults: dict[str, Any],
              cfg_hook: Callable[[RunConfig], None] | None) -> int:
    path = store.resolve_run(ref)
    if path is None:
        out(f"axon eval: no such run: {ref}")
        return 2
    summary, results = store.load_run(path)
    tasks = {t.id: t for t in store.load_run_tasks(path)}
    failed = [r for r in results if r["status"] != "pass"]
    if not failed:
        out(f"{path.name}: nothing failed")
        return 0
    chosen = [tasks[i] for i in dict.fromkeys(r["task"] for r in failed) if i in tasks]
    why = check_safety(chosen, opts)
    if why:
        out("axon eval: refused: " + why)
        return 2
    cfg = _config(opts, base, defaults)
    if cfg_hook:
        cfg_hook(cfg)
    seen: set[tuple[str, str, str]] = set()
    jobs = []
    for r in failed:
        k = (r["task"], r["model"], r["effort"])
        if k in seen or r["task"] not in tasks:
            continue
        seen.add(k)
        for rep in range(1, max(1, int(opts.get("repeat", summary.get("repeat", 1)))) + 1):
            jobs.append((tasks[r["task"]], r["model"] or None, r["effort"] or None, rep))
    return _execute(chosen, jobs, cfg, {"tasks": f"rerun of {path.name}", "rerun_of": path.name}, opts, out, _threshold(opts.get("min_pass")))


def run_command(argv: list[str], *, out: Callable[[str], None] = print, base: dict[str, Any] | None = None,
                defaults: dict[str, Any] | None = None, cfg_hook: Callable[[RunConfig], None] | None = None) -> int:
    defaults = defaults or {}
    try:
        opts, rest = parse_flags(argv)
        if opts.get("help") or opts.get("h"):
            out(USAGE)
            return 0
        sub = rest[0] if rest else "run"
        args = rest[1:] if rest else []
        if sub == "run":
            return cmd_run(opts, out, base, defaults, cfg_hook)
        if sub == "list":
            out(report.runs_text(store.list_runs()))
            return 0
        if sub in ("show", "compare"):
            need = 1 if sub == "show" else 2
            if len(args) != need:
                raise UsageError(f"{sub} takes {need} run id(s)")
            paths = [store.resolve_run(a) for a in args]
            for a, p in zip(args, paths):
                if p is None:
                    out(f"axon eval: no such run: {a}")
                    return 2
            loaded = [store.load_run(p) for p in paths if p]
            if sub == "show":
                summary, results = loaded[0]
                out(json.dumps({**summary, "results": [_slim(r) for r in results]}, indent=2) if opts.get("json")
                    else f"{summary['id']}  {summary.get('created', '')}\n{report.table(store.summarize(results))}")
                return 0
            c = store.compare(loaded[0][1], loaded[1][1])
            out(json.dumps(c, indent=2) if opts.get("json") else report.compare_text(c, paths[0].name, paths[1].name))
            return 0
        if sub in ("rerun-failed", "rerun"):
            if len(args) != 1:
                raise UsageError("rerun-failed takes one run id")
            return cmd_rerun(args[0], opts, out, base, defaults, cfg_hook)
        if sub in ("help",):
            out(USAGE)
            return 0
        raise UsageError(f"unknown eval command {sub!r}")
    except UsageError as e:
        out(f"axon eval: {e}\n{USAGE}")
        return 2
    except TaskError as e:
        out(f"axon eval: {e}")
        return 2


def eval_main(argv: list[str]) -> int:
    return run_command(argv, out=lambda s: print(s, flush=True))


def base_from_agent(agent: Any) -> dict[str, Any]:
    """Provider routing of the live session, so /eval runs against the current model and endpoint."""
    s = agent.settings
    base: dict[str, Any] = {}
    for k in ("provider", "base_url"):
        v = getattr(s, k, None)
        if v:
            base[k] = v
    try:
        key = s.api_key.get_secret_value()
        if key:
            base["api_key"] = key
    except Exception:
        pass
    return base


def handle_eval(agent: Any, arg: str) -> bool:
    import shlex
    try:
        argv = shlex.split(arg or "")
    except ValueError as e:
        print(f"/eval: {e}")
        return True
    model = str(getattr(agent.settings, "model", "") or "") or None
    effort = getattr(agent.settings, "effort", None)
    run_command(argv, out=lambda s: print(s, flush=True), base=base_from_agent(agent),
                defaults={"models": [model] if model else None, "efforts": [str(effort)] if effort else None, "jobs": 2})
    return True
