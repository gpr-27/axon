"""Eval task format: parsing, validation and loading (built-in golden suite or the user's own files)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

BUILTIN_FILE = Path(__file__).with_name("golden.jsonl")
CHECK_TYPES = {
    "file_exists", "file_absent", "file_contains", "file_not_contains", "file_regex", "file_not_regex", "file_equals",
    "command", "tests_pass", "diff_only", "answer_contains", "answer_regex", "tool_called",
}
SHELL_CHECKS = {"command", "tests_pass"}
DEFAULT_STEPS = 12
DEFAULT_COST = 0.50
DEFAULT_TIMEOUT = 300.0


class TaskError(ValueError):
    """A task file or task is invalid."""


@dataclass
class Task:
    id: str
    prompt: str
    setup_files: dict[str, str] = field(default_factory=dict)
    git: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    rubric: str = ""
    tags: list[str] = field(default_factory=list)
    max_steps: int = DEFAULT_STEPS
    max_cost: float = DEFAULT_COST
    timeout: float = DEFAULT_TIMEOUT
    builtin: bool = False

    @property
    def has_shell_checks(self) -> bool:
        return any(c.get("type") in SHELL_CHECKS for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "prompt": self.prompt,
                             "setup": {"files": self.setup_files, "git": self.git}, "checks": self.checks,
                             "tags": self.tags, "max_steps": self.max_steps, "max_cost": self.max_cost,
                             "timeout": self.timeout}
        if self.rubric:
            d["rubric"] = self.rubric
        return d


def safe_rel(path: Any, what: str = "path") -> str:
    """A relative path that stays inside the workspace, in posix form; TaskError otherwise."""
    p = str(path or "").replace("\\", "/")
    pp = PurePosixPath(p)
    if not p or pp.is_absolute() or (len(p) > 1 and p[1] == ":") or ".." in pp.parts:
        raise TaskError(f"{what} {path!r} must be relative and inside the workspace")
    return str(pp)


def _check(raw: Any, tid: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") not in CHECK_TYPES:
        raise TaskError(f"task {tid}: a check needs a type, one of {', '.join(sorted(CHECK_TYPES))}")
    c = dict(raw)
    t = c["type"]
    need = {"file_exists": "path", "file_absent": "path", "file_contains": "path", "file_not_contains": "path",
            "file_regex": "path", "file_not_regex": "path", "file_equals": "path", "command": "cmd"}
    if t in need and not c.get(need[t]):
        raise TaskError(f"task {tid}: check {t} needs {need[t]!r}")
    if "path" in need.get(t, ""):
        c["path"] = safe_rel(c["path"], f"task {tid}: check path")
    if t in ("file_contains", "file_not_contains", "answer_contains") and "text" not in c:
        raise TaskError(f"task {tid}: check {t} needs 'text'")
    if t in ("file_regex", "file_not_regex", "answer_regex") and not c.get("pattern"):
        raise TaskError(f"task {tid}: check {t} needs 'pattern'")
    if t == "file_equals" and "content" not in c:
        raise TaskError(f"task {tid}: check file_equals needs 'content'")
    if t == "tool_called" and not c.get("name"):
        raise TaskError(f"task {tid}: check tool_called needs 'name'")
    if t == "diff_only":
        files = c.get("files")
        if not isinstance(files, list):
            raise TaskError(f"task {tid}: check diff_only needs a 'files' list (it may be empty)")
        c["files"] = [safe_rel(f, f"task {tid}: diff_only file") for f in files]
    return c


def parse_task(raw: Any, *, builtin: bool = False) -> Task:
    if not isinstance(raw, dict):
        raise TaskError("a task must be an object")
    tid = str(raw.get("id") or "").strip()
    if not tid:
        raise TaskError("a task needs an id")
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise TaskError(f"task {tid}: a prompt is required")
    setup = raw.get("setup") or {}
    if not isinstance(setup, dict):
        raise TaskError(f"task {tid}: setup must be an object")
    files = setup.get("files") or {}
    if not isinstance(files, dict):
        raise TaskError(f"task {tid}: setup.files must map path to content")
    files = {safe_rel(k, f"task {tid}: setup file"): str(v) for k, v in files.items()}
    checks = [_check(c, tid) for c in (raw.get("checks") or [])]
    rubric = str(raw.get("rubric") or "").strip()
    if not checks and not rubric:
        raise TaskError(f"task {tid}: needs at least one check or a rubric")
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    try:
        steps = int(raw.get("max_steps") or DEFAULT_STEPS)
        cost = float(raw.get("max_cost") or DEFAULT_COST)
        timeout = float(raw.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError) as e:
        raise TaskError(f"task {tid}: max_steps, max_cost and timeout must be numbers") from e
    return Task(id=tid, prompt=prompt, setup_files=files, git=bool(setup.get("git")), checks=checks, rubric=rubric,
                tags=[str(t) for t in tags], max_steps=max(1, steps), max_cost=max(0.0, cost),
                timeout=max(1.0, timeout), builtin=builtin)


def parse_text(text: str, suffix: str, *, builtin: bool = False) -> list[Task]:
    """Tasks from JSON (object, list or {"tasks": [...]}), JSONL or TOML (``[[tasks]]`` or one top-level task)."""
    suffix = suffix.lower()
    raws: list[Any]
    try:
        if suffix == ".jsonl":
            raws = [json.loads(ln) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        elif suffix == ".toml":
            try:
                import tomllib
            except ImportError:  # Python 3.10
                import tomli as tomllib  # type: ignore[no-redef]
            data = tomllib.loads(text)
            raws = list(data["tasks"]) if isinstance(data.get("tasks"), list) else [data]
        elif suffix == ".json":
            data = json.loads(text)
            raws = data if isinstance(data, list) else (data["tasks"] if isinstance(data, dict) and isinstance(data.get("tasks"), list) else [data])
        else:
            raise TaskError(f"unsupported task file type {suffix!r} (use .json, .jsonl or .toml)")
    except TaskError:
        raise
    except Exception as e:
        raise TaskError(f"cannot parse task file: {e}") from e
    tasks = [parse_task(r, builtin=builtin) for r in raws]
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise TaskError(f"duplicate task id {t.id!r}")
        seen.add(t.id)
    return tasks


def load_file(path: Path, *, builtin: bool = False) -> list[Task]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise TaskError(f"cannot read {path}: {e}") from e
    try:
        return parse_text(text, Path(path).suffix, builtin=builtin)
    except TaskError as e:
        raise TaskError(f"{path}: {e}") from e


def builtin_tasks() -> list[Task]:
    return load_file(BUILTIN_FILE, builtin=True)


def load_tasks(spec: str | None) -> list[Task]:
    """The built-in suite when *spec* is empty, else the task files at *spec* (a file or a directory)."""
    if not spec:
        return builtin_tasks()
    p = Path(spec).expanduser()
    if p.is_dir():
        files = sorted(f for f in p.rglob("*") if f.suffix.lower() in (".json", ".jsonl", ".toml") and f.is_file())
        if not files:
            raise TaskError(f"no .json, .jsonl or .toml task files in {p}")
    elif p.is_file():
        files = [p]
    else:
        raise TaskError(f"no such task file or directory: {p}")
    out: list[Task] = []
    for f in files:
        out.extend(load_file(f))
    seen: set[str] = set()
    for t in out:
        if t.id in seen:
            raise TaskError(f"duplicate task id {t.id!r} across task files")
        seen.add(t.id)
    return out


def shell_task_ids(tasks: list[Task]) -> list[str]:
    return [t.id for t in tasks if not t.builtin and t.has_shell_checks]
