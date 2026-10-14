"""
Next-step suggestions after every turn (heuristics only, no LLM call).

Signals come from the last TurnTrace (``agent.traces[-1]``), the TurnResult, the todo list,
the workspace git status and the context usage of the last LLM call:

  errors / failed tests        -> /fix      (falls back to /autofix, then a plain prompt)
  pending plan todos           -> continue  (plain prompt)
  files edited, not yet tested -> /verify   (falls back to /test)
  git dirty after edits        -> /commit   (falls back to a plain prompt)
  context >= 70 % of window    -> /compact
  turn cost spike              -> /stats    (falls back to /cost)
  first turn, no AXON.md       -> /init

API::

    suggest_next(agent, result=None) -> list[Suggestion]   # max 3, deterministic order
    render_suggestions(list) -> str                         # one dim line, "" when empty

``/next [n]`` lists the suggestions with reasons and runs the chosen one.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from axon.features import FeatureCommand
from axon.ui.theme import BOLD, DIM, GOLD, MINT, RST, SLATE, WHITE, glyph, safe_text

MAX_SUGGESTIONS = 3
CONTEXT_PCT_THRESHOLD = 70.0
COST_SPIKE_MIN_USD = 0.05
COST_SPIKE_FACTOR = 3.0

EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "Patch", "NotebookEdit"})
_TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox|npm (?:run )?test|pnpm (?:run )?test|yarn (?:run )?test|bun test|"
    r"jest|vitest|mocha|go test|cargo test|dotnet test|mvn (?:-\S+ )*test|gradle(?:w)? test|rspec|phpunit|ctest|make test)\b"
)
_FAIL_RE = re.compile(
    r"(Command exited with non-zero code [1-9]|\b\d+ failed\b|\bFAILED\b|\bFAIL\b|AssertionError|"
    r"Traceback \(most recent call last\)|\berror(?:\[E\d+\])?:|--- FAIL|test result: FAILED|npm ERR!)"
)
FAILED_STOPS = frozenset({"error", "blocked", "max_iterations", "token_budget", "budget_exceeded"})
INSTRUCTION_FILES = ("AXON.md", "axon.md", ".axon/AXON.md", "CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")


@dataclass(frozen=True)
class Suggestion:
    label: str        # short text shown in the footer, e.g. "/verify"
    command: str      # slash command line or a plain prompt to send to the model
    reason: str       # why it is suggested

    @property
    def is_prompt(self) -> bool:
        return not self.command.startswith("/")


# ─── Signal extraction ─────────────────────────────────────────────────────

def _last_trace(agent: Any) -> Any:
    trace = getattr(agent, "last_trace", None)
    if trace is not None:
        return trace
    traces = getattr(agent, "traces", None) or []
    return traces[-1] if traces else None


def _steps(trace: Any, kind: str) -> list[Any]:
    return [s for s in (getattr(trace, "steps", None) or []) if getattr(s, "kind", "") == kind]


def _data(step: Any) -> dict[str, Any]:
    d = getattr(step, "data", None)
    return d if isinstance(d, dict) else {}


def edited_files(trace: Any) -> list[str]:
    out: list[str] = []
    for s in _steps(trace, "tool"):
        d = _data(s)
        if d.get("name") in EDIT_TOOLS and not d.get("is_error"):
            inp = d.get("input") if isinstance(d.get("input"), dict) else {}
            path = str(inp.get("path") or inp.get("file_path") or inp.get("notebook_path") or "")
            if path and path not in out:
                out.append(path)
            elif not path:
                out.append("?")
    return out


def _bash_steps(trace: Any) -> list[tuple[str, str, bool]]:
    out = []
    for s in _steps(trace, "tool"):
        d = _data(s)
        if d.get("name") == "Bash":
            inp = d.get("input") if isinstance(d.get("input"), dict) else {}
            out.append((str(inp.get("command", "")), str(d.get("result_preview", "")), bool(d.get("is_error"))))
    return out


def tests_outcome(trace: Any) -> str:
    """'failed' | 'passed' | '' (no test command ran). The last test run wins."""
    status = ""
    for cmd, result, is_error in _bash_steps(trace):
        if _TEST_CMD_RE.search(cmd):
            status = "failed" if (is_error or _FAIL_RE.search(result)) else "passed"
    return status


def tool_errors(trace: Any) -> list[str]:
    """Names of failed tool calls, but only when the turn's LAST tool call failed (errors the
    model already recovered from are not worth a /fix)."""
    tools = _steps(trace, "tool")
    if not tools or not _data(tools[-1]).get("is_error"):
        return []
    return [str(_data(s).get("name") or "tool") for s in tools if _data(s).get("is_error")]


def context_pct(trace: Any) -> float:
    for s in reversed(_steps(trace, "llm")):
        d = _data(s)
        cache = d.get("cache") if isinstance(d.get("cache"), dict) else {}
        if cache.get("context_pct"):
            return float(cache["context_pct"])
        used, window = d.get("context_used") or 0, d.get("context_window") or 0
        if used and window:
            return 100.0 * float(used) / float(window)
    return 0.0


def pending_todos(agent: Any) -> int:
    items = getattr(getattr(agent, "todos", None), "items", None) or []
    return sum(1 for t in items if getattr(t, "status", "") != "completed")


def cost_spike(agent: Any, trace: Any) -> float:
    """Cost of the last turn when it is a spike (>= $0.05 and >= 3x the previous average), else 0."""
    cost = float(getattr(trace, "cost", 0.0) or 0.0)
    if cost < COST_SPIKE_MIN_USD:
        return 0.0
    traces = list(getattr(agent, "traces", None) or [])
    prev = [float(getattr(t, "cost", 0.0) or 0.0) for t in traces if t is not trace]
    if not prev:
        return 0.0
    avg = sum(prev) / len(prev)
    return cost if cost >= COST_SPIKE_FACTOR * max(avg, 1e-9) else 0.0


def _workspace(agent: Any) -> Path | None:
    ws = getattr(getattr(agent, "settings", None), "workspace", None)
    try:
        return Path(ws) if ws else None
    except TypeError:
        return None


def git_dirty(workspace: Path | None, timeout: float = 2.0) -> bool:
    if workspace is None or not workspace.is_dir():
        return False
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def has_instructions(workspace: Path | None) -> bool:
    if workspace is None:
        return True
    return any((workspace / name).is_file() for name in INSTRUCTION_FILES)


# ─── Command availability ──────────────────────────────────────────────────

def command_available(name: str) -> bool:
    try:
        from axon.commands.registry import resolve_builtin
    except (ImportError, AttributeError):
        return True
    try:
        return resolve_builtin(name.lstrip("/")) is not None
    except Exception:
        return True


def _first_available(names: Iterable[str], available: Callable[[str], bool]) -> str | None:
    for n in names:
        if available(n):
            return n
    return None


# ─── Public API ────────────────────────────────────────────────────────────

_LAST_RESULT: dict[tuple[int, int], Any] = {}


def suggest_next(agent: Any, result: Any = None, *, available: Callable[[str], bool] | None = None,
                 check_git: bool = True) -> list[Suggestion]:
    """Up to three next steps for the turn that just finished, most important first."""
    trace = _last_trace(agent)
    key = (id(agent), id(trace))  # the result belongs to this agent's latest turn only
    if result is not None:
        _LAST_RESULT.clear()
        _LAST_RESULT[key] = result
    else:
        result = _LAST_RESULT.get(key)
    avail = available or command_available
    if trace is None and result is None:
        return []
    out: list[Suggestion] = []

    def add(names: Iterable[str], reason: str, fallback: tuple[str, str] | None = None) -> None:
        cmd = _first_available(names, avail)
        if cmd is None:
            if fallback is None:
                return
            sug = Suggestion(label=fallback[0], command=fallback[1], reason=reason)
        else:
            sug = Suggestion(label=cmd, command=cmd, reason=reason)
        if all(s.command != sug.command for s in out):
            out.append(sug)

    edits = edited_files(trace) if trace is not None else []
    tests = tests_outcome(trace) if trace is not None else ""
    errors = tool_errors(trace) if trace is not None else []
    stop = str(getattr(result, "stop_reason", "") or getattr(trace, "stop_reason", "") or "")
    failed_turn = bool(getattr(result, "error", None)) or stop in FAILED_STOPS

    if tests == "failed":
        add(("/fix", "/autofix"), "tests failed in the last Bash run",
            ("fix the tests", "Fix the failing tests from the last run, then re-run them."))
    elif failed_turn or errors:
        why = f"{len(errors)} tool call(s) failed ({', '.join(sorted(set(errors)))})" if errors else f"turn ended with '{stop}'"
        add(("/fix", "/autofix"), why, ("fix it", "Investigate and fix the error from the last step."))

    todos = pending_todos(agent)
    if todos:
        add((), f"{todos} plan item(s) still pending", ("continue", f"Continue with the remaining {todos} todo item(s)."))

    if edits and not tests:
        n = len([e for e in edits if e != "?"]) or len(edits)
        add(("/verify", "/test", "/autofix"), f"{n} file(s) edited but no tests were run",
                ("run tests", "Run the project's tests and report the results."))
    if edits and check_git and git_dirty(_workspace(agent)):
        add(("/commit",), "working tree has uncommitted changes",
            ("commit", "Review the diff and create a git commit with a descriptive message."))

    if trace is not None:
        pct = context_pct(trace)
        if pct >= CONTEXT_PCT_THRESHOLD:
            add(("/compact",), f"context is {pct:.0f}% full")
        spike = cost_spike(agent, trace)
        if spike:
            add(("/stats", "/cost"), f"this turn cost ${spike:.4f}, well above the session average")

    traces = getattr(agent, "traces", None) or []
    if len(traces) <= 1 and not has_instructions(_workspace(agent)):
        add(("/init",), "no AXON.md yet: /init records how to build and test this repo")

    return out[:MAX_SUGGESTIONS]


def render_suggestions(suggestions: list[Suggestion]) -> str:
    """One dim line: ``Next: [1] /verify · [2] /commit · [3] /stats`` ("" when there is nothing)."""
    if not suggestions:
        return ""
    sep = f" {glyph('dot')} "
    body = sep.join(f"[{i}] {s.label}" for i, s in enumerate(suggestions[:MAX_SUGGESTIONS], 1))
    return safe_text(f"  {DIM}Next: {body}{RST}")


# ─── /next ─────────────────────────────────────────────────────────────────

def run_suggestion(agent: Any, sug: Suggestion) -> Any:
    """Run a suggestion: slash commands through the dispatcher, plain prompts as a new turn."""
    if not sug.is_prompt:
        try:
            from axon.commands.builtin import dispatch_command
        except (ImportError, AttributeError):
            dispatch_command = None  # type: ignore[assignment]
        if dispatch_command is not None:
            return dispatch_command(sug.command, agent)
    run_turn = getattr(agent, "run_turn", None)
    if callable(run_turn):
        return run_turn(sug.command)
    return None


def _print(text: str) -> None:
    try:
        print(safe_text(text), flush=True)
    except Exception:
        pass


def handle_next(agent: Any, arg: str) -> None:
    sugs = suggest_next(agent)
    if not sugs:
        _print(f"  {SLATE}No suggestions right now - ask Axon something first.{RST}")
        return
    a = (arg or "").strip()
    if a.isdigit():
        idx = int(a) - 1
        if 0 <= idx < len(sugs):
            run_suggestion(agent, sugs[idx])
        else:
            _print(f"  {GOLD}Pick a number between 1 and {len(sugs)}.{RST}")
        return
    _print(f"\n  {GOLD}{BOLD}Suggested next steps{RST}")
    for i, s in enumerate(sugs, 1):
        kind = "prompt" if s.is_prompt else "command"
        shown = s.command if not s.is_prompt else f'"{s.command}"'
        _print(f"   {MINT}[{i}]{RST} {WHITE}{s.label:<10}{RST} {SLATE}{s.reason}{RST}")
        _print(f"       {DIM}{kind}: {shown}{RST}")
    try:
        from axon import platform as plat
        interactive = plat.is_interactive()
    except Exception:
        interactive = False
    if not interactive:
        _print(f"  {DIM}Run one with /next <n>.{RST}")
        return
    try:
        from axon.ui.picker import SelectItem, Selector
        items = [SelectItem(label=s.label, value=s, hint=s.reason) for s in sugs]
        picked = Selector(items, title="Run a suggestion", footer="enter run · 1-3 pick · esc skip").run()
    except Exception:
        picked = None
    if picked is not None and isinstance(getattr(picked, "value", None), Suggestion):
        run_suggestion(agent, picked.value)


COMMANDS = [
    FeatureCommand(name="/next", handler=handle_next, category="Getting started", aliases=("/suggest",),
                   description="Suggested next steps for the last turn (verify, fix, commit, compact...)",
                   usage="/next [n]"),
]

__all__ = ["Suggestion", "suggest_next", "render_suggestions", "run_suggestion", "handle_next",
           "edited_files", "tests_outcome", "context_pct", "COMMANDS"]
