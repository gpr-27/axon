"""What a project looks like right now, as plain data: files and languages, git state, tests, spend, chats, notes.

No terminal and no web imports. The app (Insights, the empty chat's starter prompts, the project menu, the
first-run welcome) and the terminal ``/overview`` command read these same functions, so both show the same figures.

* ``overview(ws)``: everything the Insights section shows, one dict. Every part is real or absent; nothing is guessed.
* ``starter_prompts(ws)``: up to four prompts chosen from the real project (a real file, real git changes).
* ``detect_test_command(ws)`` / ``last_test_result(ws)`` / ``record_test_result(...)``: the test command and what Axon last saw.
* ``recent_projects()`` / ``remove_recent(path)``: projects with saved sessions, newest first; a missing folder is flagged.
* ``welcome_state()`` / ``mark_welcome(...)``: whether the first-run welcome was finished or skipped.
* ``render_text(data, width)``: the ``/overview`` text.

State lives under AXON_HOME (state/welcome.json, state/recent_removed.json, projects/<key>/overview/last_test.json),
written atomically; a corrupt file reads as empty.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".next", ".tox", ".mypy_cache",
             ".pytest_cache", ".idea", ".gradle", "vendor", ".axon", ".cache", "site-packages"}
SCAN_LIMIT = 8000
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".scala": "Scala", ".lua": "Lua", ".sh": "Shell", ".ps1": "PowerShell",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".vue": "Vue", ".svelte": "Svelte", ".sql": "SQL", ".md": "Markdown", ".json": "JSON",
    ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".dart": "Dart", ".r": "R", ".ex": "Elixir", ".exs": "Elixir", ".zig": "Zig",
}
# Prose, data and config do not count as "code" when choosing a file to write tests for.
CODE_LANGS = {"Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin", "Swift", "Ruby", "PHP", "C", "C++", "C#", "Scala", "Lua", "Dart", "Elixir", "Zig", "Vue", "Svelte"}
_TEST_NAME = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|^tests?$|^__init__\.|^conftest\.)", re.I)
_TEST_RUN = re.compile(r"(^|[\s;&|])(pytest|py\.test|python3?\s+-m\s+(pytest|unittest)|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+(run\s+)?test|cargo\s+test|go\s+test)\b")


# ─── small files ─────────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> bool:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _home() -> Path:
    from axon import storage
    return storage.axon_home()


def _git(ws: Path, *args: str, timeout: float = 10) -> str | None:
    from axon.features.gitops import run_git
    p = run_git(ws, *args, timeout=timeout)
    return p.stdout if p.returncode == 0 else None


# ─── files and languages ─────────────────────────────────────────────────────

def list_files(ws: Path, limit: int = SCAN_LIMIT) -> tuple[list[str], bool]:
    """Relative POSIX paths of the project's files (git's list when it is a repo), and whether the list was cut."""
    ws = Path(ws)
    out = _git(ws, "ls-files", "-z", "--cached", "--others", "--exclude-standard", timeout=15)
    files: list[str] = []
    if out is not None and out.strip("\0"):
        for p in out.split("\0"):
            if p and not any(part in SKIP_DIRS for part in p.split("/")[:-1]):
                try:
                    if (ws / p).is_file():
                        files.append(p)
                except OSError:
                    continue
                if len(files) >= limit:
                    return files, True
        return files, False
    for root, dirs, names in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            if n.startswith("."):
                continue
            files.append(Path(root, n).relative_to(ws).as_posix())
            if len(files) >= limit:
                return files, True
    return files, False


def languages(files: list[str]) -> list[dict[str, Any]]:
    """[{name, files, pct}] biggest first, counting code and markup files only (data files such as JSON stay out of the top)."""
    count: dict[str, int] = {}
    for f in files:
        lang = LANGUAGES.get(Path(f).suffix.lower())
        if lang:
            count[lang] = count.get(lang, 0) + 1
    total = sum(count.values())
    rows = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": k, "files": v, "pct": round(100.0 * v / total, 1)} for k, v in rows[:6]] if total else []


def _has_test(stem: str, files: list[str]) -> bool:
    s = stem.lower()
    for f in files:
        n = Path(f).name.lower()
        if n.startswith((f"test_{s}.", f"{s}_test.", f"{s}.test.", f"{s}.spec.")):
            return True
    return False


def pick_test_target(ws: Path, files: list[str]) -> str:
    """A real source file that has no matching test file, the largest one; '' when there is none."""
    best, best_size = "", 0
    for f in files:
        p = Path(f)
        if LANGUAGES.get(p.suffix.lower()) not in CODE_LANGS:
            continue
        parts = [x.lower() for x in p.parts]
        if _TEST_NAME.search(p.name) or any(x in ("tests", "test", "__tests__", "spec", "examples", "scripts") for x in parts[:-1]):
            continue
        try:
            size = (Path(ws) / f).stat().st_size
        except OSError:
            continue
        if size < 200 or size > 400_000 or _has_test(p.stem, files):
            continue
        if size > best_size:
            best, best_size = f, size
    return best


# ─── git ─────────────────────────────────────────────────────────────────────

def git_state(ws: Path, commits: int = 5) -> dict[str, Any]:
    """{repo, branch, changed, commits: [{hash, subject, when, age_s}]}; repo False outside a repository."""
    ws = Path(ws)
    inside = _git(ws, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return {"repo": False, "branch": "", "changed": 0, "commits": []}
    branch = (_git(ws, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    status = _git(ws, "status", "--porcelain=v1", "-z", "--untracked-files=normal") or ""
    changed = len([p for p in status.split("\0") if len(p) >= 4])
    log = _git(ws, "log", f"-{int(commits)}", "--format=%h%x1f%ct%x1f%s") or ""
    now = time.time()
    rows = []
    for line in log.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        try:
            ts = float(parts[1])
        except ValueError:
            continue
        rows.append({"hash": parts[0], "subject": parts[2][:140], "when": ts, "age_s": max(0, int(now - ts))})
    return {"repo": True, "branch": branch if branch != "HEAD" else "detached", "changed": changed, "commits": rows}


# ─── tests ───────────────────────────────────────────────────────────────────

def detect_test_command(ws: Path) -> dict[str, str]:
    """{name, command} of the test runner the folder points to, or {}. The same rules as the terminal's /test."""
    ws = Path(ws)
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists() or (ws / "setup.cfg").exists() or (ws / "tests").is_dir():
        return {"name": "pytest", "command": "pytest -q" if shutil.which("pytest") else f"{Path(sys.executable).name} -m pytest -q"}
    if (ws / "package.json").exists():
        return {"name": "npm test", "command": "npm test"}
    if (ws / "Cargo.toml").exists():
        return {"name": "cargo test", "command": "cargo test"}
    if (ws / "go.mod").exists():
        return {"name": "go test", "command": "go test ./..."}
    return {}


def _test_file(ws: Path) -> Path:
    from axon import storage
    return storage.project_dir(Path(ws), create=False) / "overview" / "last_test.json"


def record_test_result(ws: Path, command: str, exit_code: int, source: str = "/test") -> None:
    """Remember a test run that Axon made (the terminal's /test calls this). Never raises."""
    try:
        _write_json(_test_file(ws), {"command": str(command)[:300], "ok": exit_code == 0, "exit": int(exit_code), "at": time.time(), "source": source})
    except Exception:
        pass


def _traced_test_runs(ws: Path, max_files: int = 6) -> list[dict[str, Any]]:
    """Test commands the agent ran through its Bash tool, from the newest saved traces."""
    from axon import storage
    root = storage.project_dir(Path(ws), create=False) / "sessions"
    try:
        files = sorted(root.glob("*/trace.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    except OSError:
        return []
    runs = []
    for f in files:
        for turn in storage.read_jsonl(f, limit=40):
            for st in turn.get("steps") or []:
                d = st.get("data") if isinstance(st, dict) else None
                if not isinstance(d, dict) or st.get("kind") != "tool" or d.get("name") != "Bash":
                    continue
                inp = d.get("input") if isinstance(d.get("input"), dict) else {}
                cmd = str(inp.get("command") or "")
                if cmd and _TEST_RUN.search(cmd) and "is_error" in d:
                    runs.append({"command": cmd[:300], "ok": not d.get("is_error"), "at": float(st.get("ended_at") or st.get("started_at") or 0), "source": "chat"})
    return runs


def last_test_result(ws: Path) -> dict[str, Any] | None:
    """{command, ok, at, source} of the newest run Axon saw (its /test, or a test command in a chat), or None."""
    best = None
    rec = _read_json(_test_file(ws), None)
    if isinstance(rec, dict) and isinstance(rec.get("at"), (int, float)):
        best = {"command": str(rec.get("command") or ""), "ok": bool(rec.get("ok")), "at": float(rec["at"]), "source": str(rec.get("source") or "/test")}
    for r in _traced_test_runs(ws):
        if best is None or r["at"] > best["at"]:
            best = r
    return best


# ─── spend, chats, notes ─────────────────────────────────────────────────────

def spend_week(ws: Path) -> dict[str, Any]:
    """Cost, calls and tokens of the last 7 days in this project, from the usage log."""
    from axon import storage
    from axon.features import analytics
    since = time.time() - 7 * 86400
    recs = analytics.load_usage(since=since, project=storage.project_key(Path(ws)))
    tokens = sum(r.input + r.output for r in recs)   # input already includes cache reads and writes, as on the Usage page
    return {"cost": round(sum(r.cost for r in recs), 4), "calls": len(recs), "tokens": tokens, "sessions": len({r.session for r in recs if r.session})}


def last_chats(ws: Path, limit: int = 5) -> list[dict[str, Any]]:
    from axon.session.store import SessionStore
    try:
        metas = SessionStore(Path(ws)).list_recent(limit=limit)
    except Exception:
        return []
    out = []
    for m in [x for x in metas if int(getattr(x, "message_count", 0) or 0) > 0]:
        title = " ".join(str(getattr(m, "first_prompt", "") or "").split())[:90]
        out.append({"id": m.session_id, "title": title or "(no prompt yet)", "when": float(getattr(m, "created_at", 0) or 0),
                    "messages": int(getattr(m, "message_count", 0) or 0), "cost": round(float(getattr(m, "total_cost", 0) or 0), 4)})
    return out


def memory_notes(ws: Path, limit: int = 5) -> dict[str, Any]:
    from axon.features import memory_files as mf
    try:
        items = mf.list_memories(Path(ws))
    except Exception:
        items = []
    items = sorted(items, key=lambda m: str(getattr(m, "updated", "") or ""), reverse=True)
    return {"total": len(items), "items": [{"name": str(m.name)[:80], "scope": m.scope, "type": m.type} for m in items[:limit]]}


# ─── the overview and the starter prompts ────────────────────────────────────

def overview(ws: Path) -> dict[str, Any]:
    ws = Path(ws)
    files, cut = list_files(ws)
    return {"workspace": str(ws), "name": ws.name, "files": {"count": len(files), "truncated": cut, "languages": languages(files)},
            "git": git_state(ws), "tests": {"detected": detect_test_command(ws), "last": last_test_result(ws)},
            "spend": spend_week(ws), "chats": last_chats(ws), "memory": memory_notes(ws), "at": time.time()}


def starter_prompts(ws: Path) -> list[dict[str, str]]:
    """[{id, label, prompt}], at most four, chosen from what is really in the folder."""
    ws = Path(ws)
    files, _ = list_files(ws, limit=3000)
    if not files:
        return [{"id": "start", "label": "Help me start something here", "prompt": "This folder is empty. Ask me what I want to build, then suggest a plan and set it up."}]
    out = [{"id": "explain", "label": "Explain this project", "prompt": "Explain this project: what it is for, how it is organised and where to start reading."}]
    g = git_state(ws, commits=3)
    last = last_test_result(ws)
    code = [f for f in files if LANGUAGES.get(Path(f).suffix.lower()) in CODE_LANGS]
    if g["repo"] and (g["changed"] or (g["commits"] and g["commits"][0]["age_s"] < 14 * 86400)):
        out.append({"id": "recent", "label": "What changed recently?", "prompt": "What changed recently in this project? Summarise the uncommitted changes and the latest commits in plain words."})
    if last and not last["ok"]:
        out.append({"id": "fixtests", "label": "Fix the failing tests", "prompt": f"The last test run failed ({last['command']}). Run it, find out why it fails and fix it."})
    if code:
        out.append({"id": "bugs", "label": "Find bugs", "prompt": "Look through this project for bugs and risky code. List what you find, most serious first, and do not change anything yet."})
        target = pick_test_target(ws, files)
        if target:
            out.append({"id": "tests", "label": f"Write tests for {target}", "prompt": f"Write tests for {target}. Match the way this project already tests things, and run them."})
    return out[:4]


# ─── recent projects ─────────────────────────────────────────────────────────

def _removed_file() -> Path:
    return _home() / "state" / "recent_removed.json"


def recent_projects(limit: int = 12, exclude: Path | str | None = None) -> list[dict[str, Any]]:
    """Projects that have saved sessions, newest first: {path, name, used, exists, sessions}. A folder that is gone is kept
    (so it can be removed from the list) with ``exists`` False. Removed entries stay out until the project is used again."""
    removed = _read_json(_removed_file(), {})
    removed = removed if isinstance(removed, dict) else {}
    root = _home() / "projects"
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    skip = ""
    if exclude:
        try:
            skip = str(Path(exclude).resolve())
        except OSError:
            skip = str(exclude)
    rows = []
    for d in dirs:
        meta = _read_json(d / "project.json", {})
        ws = meta.get("workspace") if isinstance(meta, dict) else None
        sdir = d / "sessions"
        if not ws or not isinstance(ws, str) or not sdir.is_dir():
            continue
        try:
            mt = max([sdir.stat().st_mtime] + [p.stat().st_mtime for p in list(sdir.iterdir())[:200]])
        except OSError:
            mt = 0.0
        if ws in removed and mt <= float(removed.get(ws) or 0):
            continue
        if skip and ws == skip:
            continue
        try:
            n = sum(1 for p in sdir.iterdir() if p.is_dir() or p.suffix == ".jsonl")
        except OSError:
            n = 0
        if n == 0:
            continue
        rows.append({"path": ws, "name": Path(ws).name or ws, "used": mt, "exists": Path(ws).is_dir(), "sessions": n})
    rows.sort(key=lambda r: r["used"], reverse=True)
    return rows[:limit]


def remove_recent(path: str) -> bool:
    """Hide a project from the recent list. Its folder and chats are untouched. True when it was in the list."""
    known = {r["path"] for r in recent_projects(limit=500)}
    if path not in known:
        return False
    removed = _read_json(_removed_file(), {})
    removed = removed if isinstance(removed, dict) else {}
    removed[path] = time.time()
    return _write_json(_removed_file(), removed)


# ─── first-run welcome ───────────────────────────────────────────────────────

def _welcome_file() -> Path:
    return _home() / "state" / "welcome.json"


def welcome_state() -> dict[str, Any]:
    d = _read_json(_welcome_file(), {})
    d = d if isinstance(d, dict) else {}
    return {"done": bool(d.get("done")), "skipped": bool(d.get("skipped")), "at": d.get("at")}


def mark_welcome(skipped: bool = False) -> dict[str, Any]:
    _write_json(_welcome_file(), {"done": True, "skipped": bool(skipped), "at": time.time()})
    return welcome_state()


def reset_welcome() -> None:
    try:
        _welcome_file().unlink()
    except OSError:
        pass


# ─── text for /overview ──────────────────────────────────────────────────────

def _age(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def render_text(d: dict[str, Any], width: int = 100) -> list[str]:
    """Plain lines, none longer than *width*."""
    def cut(s: str) -> str:
        return s if len(s) <= width else s[: max(1, width - 1)] + "…"
    f, g, t, sp = d["files"], d["git"], d["tests"], d["spend"]
    lines = [cut(f"{d['name']}  {d['workspace']}"), ""]
    langs = ", ".join(f"{x['name']} {x['pct']:.0f}%" for x in f["languages"][:4])
    lines.append(cut(f"Files      {f['count']}{'+' if f['truncated'] else ''}" + (f"  ({langs})" if langs else "")))
    if g["repo"]:
        lines.append(cut(f"Git        {g['branch']}, " + (f"{g['changed']} changed" if g["changed"] else "clean")))
        for c in g["commits"][:5]:
            lines.append(cut(f"           {c['hash']}  {c['subject']}  ({_age(c['age_s'])})"))
    else:
        lines.append("Git        not a git repository")
    if t["detected"]:
        last = t["last"]
        tail = "no run seen yet" if not last else ("passed" if last["ok"] else "failed") + f" {_age(time.time() - last['at'])} ({last['command']})"
        lines.append(cut(f"Tests      {t['detected']['command']}: {tail}"))
    else:
        lines.append("Tests      no test runner found")
    lines.append(cut(f"Last 7 days  ${sp['cost']:.2f}, {sp['calls']} model calls, {sp['tokens']:,} tokens" if sp["calls"] else "Last 7 days  no model calls in this project"))
    if d["chats"]:
        lines += ["", "Last chats"]
        lines += [cut(f"  {c['title']}  ({c['messages']} messages, {_age(time.time() - c['when'])})") for c in d["chats"]]
    m = d["memory"]
    lines += ["", cut(f"Memory     {m['total']} note{'s' if m['total'] != 1 else ''}" + (": " + ", ".join(i["name"] for i in m["items"][:4]) if m["items"] else ""))]
    return lines
