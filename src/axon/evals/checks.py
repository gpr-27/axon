"""Exact checks run against the temp workspace (and the agent's answer / tool calls) after a task run."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axon.evals.tasks import Task

IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".axon"}
OUTPUT_CAP = 4000
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def snapshot(root: Path) -> dict[str, str]:
    """{relative posix path: sha1} of every file under *root* except caches and .git."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            p = Path(dirpath) / fn
            try:
                out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def clean_env() -> dict[str, str]:
    """The environment for shell checks: no credentials from the user's shell reach task commands."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def expand(cmd: str) -> str:
    return cmd.replace("{python}", shlex.quote(sys.executable))


def run_command(cmd: str, cwd: Path, timeout: float) -> tuple[int | None, str, str]:
    """(exit code or None on timeout, stdout, stderr), run in *cwd* with a timeout."""
    try:
        p = subprocess.run(expand(cmd), shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           env=clean_env(), stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout[-OUTPUT_CAP:], p.stderr[-OUTPUT_CAP:]
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out[-OUTPUT_CAP:], ""
    except OSError as e:
        return -1, "", str(e)


def _read(ws: Path, rel: str) -> str | None:
    try:
        return (ws / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _label(c: dict[str, Any]) -> str:
    t = c["type"]
    for k in ("path", "cmd", "text", "pattern", "name"):
        if c.get(k):
            return f"{t}:{str(c[k])[:60]}"
    return t


def run_check(c: dict[str, Any], ws: Path, *, before: dict[str, str], answer: str, tools: list[str]) -> CheckResult:
    t = c["type"]
    name = c.get("name") or _label(c)
    try:
        if t == "file_exists":
            return CheckResult(name, (ws / c["path"]).is_file(), "" if (ws / c["path"]).is_file() else "file is missing")
        if t == "file_absent":
            return CheckResult(name, not (ws / c["path"]).exists(), "" if not (ws / c["path"]).exists() else "file still exists")
        if t in ("file_contains", "file_not_contains", "file_regex", "file_not_regex", "file_equals"):
            body = _read(ws, c["path"])
            if body is None:
                return CheckResult(name, False, f"{c['path']} is missing")
            if t == "file_contains":
                ok = c["text"] in body
                return CheckResult(name, ok, "" if ok else f"{c['path']} does not contain {c['text']!r}")
            if t == "file_not_contains":
                ok = c["text"] not in body
                return CheckResult(name, ok, "" if ok else f"{c['path']} still contains {c['text']!r}")
            if t in ("file_regex", "file_not_regex"):
                found = re.search(c["pattern"], body, re.M | re.S) is not None
                ok = found if t == "file_regex" else not found
                return CheckResult(name, ok, "" if ok else f"{c['path']} {'does not match' if t == 'file_regex' else 'matches'} /{c['pattern']}/")
            exact = body == str(c["content"])
            ok = exact or (not c.get("strict") and body.strip() == str(c["content"]).strip())
            return CheckResult(name, ok, "" if ok else f"{c['path']} differs from the expected content")
        if t in ("command", "tests_pass"):
            cmd = c.get("cmd") or "{python} -m unittest discover -q"
            code, out, err = run_command(cmd, ws, float(c.get("timeout") or 60))
            if code is None:
                return CheckResult(name, False, "command timed out")
            want = int(c.get("exit_code", 0))
            if code != want:
                return CheckResult(name, False, f"exit code {code}, expected {want}: {(err or out).strip()[-200:]}")
            for needle in ([c["stdout_contains"]] if isinstance(c.get("stdout_contains"), str) else list(c.get("stdout_contains") or [])):
                if needle not in out:
                    return CheckResult(name, False, f"stdout does not contain {needle!r}")
            return CheckResult(name, True)
        if t == "diff_only":
            allowed = set(c["files"])
            extra = [f for f in changed_files(before, snapshot(ws)) if f not in allowed]
            return CheckResult(name, not extra, "" if not extra else "also changed: " + ", ".join(extra[:6]))
        if t == "answer_contains":
            ok = (c["text"].lower() in answer.lower()) if not c.get("case_sensitive") else c["text"] in answer
            return CheckResult(name, ok, "" if ok else f"the answer does not contain {c['text']!r}")
        if t == "answer_regex":
            ok = re.search(c["pattern"], answer, re.I | re.S) is not None
            return CheckResult(name, ok, "" if ok else f"the answer does not match /{c['pattern']}/")
        if t == "tool_called":
            n = sum(1 for x in tools if x.lower() == str(c["name"]).lower())
            ok = n >= int(c.get("min", 1))
            return CheckResult(name, ok, "" if ok else f"{c['name']} was called {n} time(s)")
    except re.error as e:
        return CheckResult(name, False, f"bad regex: {e}")
    except Exception as e:                       # noqa: BLE001 - a broken check fails, never crashes the run
        return CheckResult(name, False, f"check error: {type(e).__name__}: {e}")
    return CheckResult(name, False, f"unknown check type {t}")


def run_checks(task: Task, ws: Path, *, before: dict[str, str], answer: str, tools: list[str]) -> list[CheckResult]:
    return [run_check(c, ws, before=before, answer=answer, tools=tools) for c in task.checks]
