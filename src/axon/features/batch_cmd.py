"""`/batch` (in a session) and `axon batch` (terminal): run many independent prompts at once.

    /batch <template> --input <glob|file.jsonl|file.csv> [--model M] [--max-tokens N] [--system S] [--out DIR]
                      [--backend local|anthropic] [--workers N] [--yes] [--wait]
    /batch status [id]       one status line (newest job without an id)
    /batch results <id> [--out DIR]   copy the result files (one per custom_id) and print the summary
    /batch cancel <id>       /batch retry <id>      /batch resume <id>      /batch list

The template may use {path} {name} {content} for file inputs and {column} for JSONL / CSV rows.
Example: /batch "Review {path} for bugs and unclear names." --input "src/**/*.py"
"""
from __future__ import annotations

import shlex
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from axon.features import batch_core as bc
from axon.features.batch_core import BatchError
from axon.features.batch_run import Runner, poll_in_background

FULL_USAGE = (__doc__ or "").strip()
SUBS = {"status", "results", "cancel", "retry", "resume", "list", "run", "help"}


def parse_run_args(tokens: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"inputs": [], "model": None, "max_tokens": bc.DEFAULT_MAX_TOKENS, "system": "", "out": None,
                            "yes": False, "wait": False, "backend": None, "workers": 4, "template": ""}
    words: list[str] = []
    it = iter(tokens)
    for t in it:
        if t in ("--input", "-i"):
            opts["inputs"].append(_need(it, t))
        elif t == "--model":
            opts["model"] = _need(it, t)
        elif t == "--max-tokens":
            opts["max_tokens"] = int(_need(it, t))
        elif t == "--system":
            opts["system"] = _need(it, t)
        elif t == "--out":
            opts["out"] = _need(it, t)
        elif t == "--backend":
            opts["backend"] = _need(it, t)
        elif t == "--workers":
            opts["workers"] = int(_need(it, t))
        elif t in ("--yes", "-y"):
            opts["yes"] = True
        elif t == "--wait":
            opts["wait"] = True
        elif t.startswith("--"):
            raise BatchError(f"Unknown option {t}.")
        else:
            words.append(t)
    opts["template"] = " ".join(words)
    return opts


def _need(it: Any, flag: str) -> str:
    try:
        return next(it)
    except StopIteration:
        raise BatchError(f"{flag} needs a value.") from None


def _out(text: str = "") -> None:
    print(text, flush=True)


def run_command(settings: Any, arg: str, *, runner: Runner | None = None, confirm: Callable[[str], bool] | None = None,
                emit: Callable[[str], None] = _out, foreground: bool = False, cwd: Path | None = None) -> int:
    """The whole `/batch` grammar. Returns a process exit code."""
    try:
        tokens = shlex.split(arg) if isinstance(arg, str) else list(arg)
    except ValueError as e:
        emit(f"batch: {e}")
        return 2
    sub = tokens[0] if tokens and tokens[0] in SUBS else ("run" if tokens else "help")
    rest = tokens[1:] if tokens and tokens[0] in SUBS else tokens
    runner = runner or Runner(settings)
    try:
        if sub == "help":
            emit(FULL_USAGE)
        elif sub == "list":
            jobs = runner.list_jobs()
            if not jobs:
                emit("No batches yet.")
            for j in jobs[:30]:
                emit(f"  {bc.summary_line(j)}  ({j['backend']}, {j['created_at'][:16].replace('T', ' ')}Z)")
        elif sub == "status":
            job = runner.refresh(rest[0] if rest else "")
            emit(bc.summary_line(job))
            if job.get("note"):
                emit(f"  {job['note']}")
        elif sub == "results":
            return _results(runner, rest, emit)
        elif sub == "cancel":
            emit(bc.summary_line(runner.cancel(_one_id(rest))))
        elif sub == "retry":
            job = runner.retry(_one_id(rest))
            emit(f"Retrying {job['total']} item(s): {bc.summary_line(job)}")
            _follow(runner, job, emit, foreground)
        elif sub == "resume":
            job = runner.resume(_one_id(rest))
            emit(f"Resumed: {bc.summary_line(job)}")
            _follow(runner, job, emit, foreground)
        else:
            return _run(settings, runner, rest, emit, confirm, foreground, cwd)
    except BatchError as e:
        emit(f"batch: {e}")
        return 1
    return 0


def _one_id(rest: list[str]) -> str:
    if not rest:
        raise BatchError("Give the batch id (`/batch list` shows them).")
    return rest[0]


def _results(runner: Runner, rest: list[str], emit: Callable[[str], None]) -> int:
    out = None
    ids = []
    it = iter(rest)
    for t in it:
        if t == "--out":
            out = _need(it, t)
        else:
            ids.append(t)
    job = runner.refresh(ids[0] if ids else "")
    if job["status"] not in bc.TERMINAL:
        emit(f"Not finished yet: {bc.summary_line(job)}")
        return 1
    where = Path(job["out_dir"])
    if out:
        n = bc.copy_results(job, Path(out).expanduser())
        where = Path(out).expanduser()
        emit(f"Copied {n} file(s) to {where}")
    emit(bc.summary_line(job))
    emit(f"  results: {where}/<custom_id>.md  (raw: <custom_id>.json, errors: <custom_id>.error.json, summary.json)")
    if bc.failed_ids(job):
        emit(f"  {len(bc.failed_ids(job))} item(s) did not succeed: `/batch retry {job['id']}`")
    return 0


def _run(settings: Any, runner: Runner, rest: list[str], emit: Callable[[str], None], confirm: Callable[[str], bool] | None,
         foreground: bool, cwd: Path | None) -> int:
    o = parse_run_args(rest)
    items = bc.load_items(o["inputs"], o["template"], base=cwd)
    model = o["model"] or str(settings.model)
    max_tokens = min(o["max_tokens"], int(getattr(settings, "max_tokens", o["max_tokens"]) or o["max_tokens"]))
    runner.workers = o["workers"]
    plan = runner.plan(items, model, max_tokens=max_tokens, system=o["system"], force=o["backend"])
    backend, est = plan["backend"], plan["estimate"]
    where = ("Anthropic Message Batches, 50% of the normal price, most finish within an hour" if backend == "anthropic"
             else f"local worker pool ({runner.workers} at a time), normal price")
    cost = f"${est:.2f}" if plan["priced"] else "unknown (model not priced)"
    emit(f"{len(items)} item(s) · {model} · {where} · estimated cost {cost}")
    if plan["priced"] and est > bc.CONFIRM_ABOVE_USD and not o["yes"]:
        ask = confirm or _default_confirm
        if not ask(f"Estimated cost ${est:.2f} is above ${bc.CONFIRM_ABOVE_USD}. Submit?"):
            emit("Not submitted.")
            return 1
    job = runner.submit(plan, model, o["out"])
    emit(f"Submitted {job['id']}. `/batch status {job['id']}` checks it; results go to {job['out_dir']}")
    _follow(runner, job, emit, foreground or o["wait"])
    return 0


def _default_confirm(question: str) -> bool:
    from axon.commands.common import confirm
    return confirm(question, default=False)


def _follow(runner: Runner, job: dict[str, Any], emit: Callable[[str], None], foreground: bool) -> None:
    """Foreground: print progress until the job ends. Otherwise poll on a daemon thread and print one line when it ends."""
    def done(j: dict[str, Any]) -> None:
        emit(f"batch finished: {bc.summary_line(j)}  (`/batch results {j['id']} --out DIR`)")
    if not foreground:
        poll_in_background(runner, job["id"], done)
        return
    delay, last = runner.poll_first, ""
    while True:
        j = runner.refresh(job["id"])
        line = bc.summary_line(j)
        if line != last:
            emit(line)
            last = line
        if j["status"] in bc.TERMINAL:
            return
        runner.sleep(delay)
        delay = min(bc.POLL_MAX_S, delay * 1.3)


# ─── entry points ─────────────────────────────────────────────────────────
def handle_batch(agent: Any, arg: str) -> bool:
    run_command(agent.settings, arg, cwd=Path(getattr(agent.settings, "workspace", Path.cwd())))
    return True


def batch_main(argv: list[str]) -> int:
    from axon.config import Settings
    try:
        settings = Settings.load({"workspace": Path.cwd()}, require_api_key=False)
    except Exception as e:                  # noqa: BLE001
        sys.stderr.write(f"axon batch: {e}\n")
        return 2
    if any(a in ("-h", "--help") for a in argv):
        print(FULL_USAGE.replace("/batch", "axon batch"))
        return 0
    runner = Runner(settings)
    # A local pool dies with this process, so the terminal command always stays in the foreground for it.
    is_local = runner.backend(_flag(argv, "--backend")) == "local"
    return run_command(settings, argv, runner=runner, foreground=is_local and (not argv or argv[0] not in ("status", "list", "results", "cancel")),
                       cwd=Path.cwd())


def _flag(argv: list[str], name: str) -> str | None:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


try:
    from axon.features import FeatureCommand
    COMMANDS = [FeatureCommand(name="/batch", handler=handle_batch, category="Features", usage=FULL_USAGE,
                               description="Run many prompts at once: Message Batches at half price, or a local worker pool")]
except Exception:                          # pragma: no cover
    COMMANDS = []

CLI_SUBCOMMANDS = {"batch": batch_main}
__all__ = ["COMMANDS", "CLI_SUBCOMMANDS", "handle_batch", "batch_main", "run_command", "parse_run_args"]
