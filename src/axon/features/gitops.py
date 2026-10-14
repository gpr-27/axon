"""
Git workflow helpers: /commit, /pr, /git (status dashboard), /changes.

All git calls go through ``run_git`` (``shutil.which("git")``, utf-8 with errors="replace",
stdin closed, no terminal prompts) so they behave the same on Windows, macOS and Linux.
Every destructive action (staging, committing, pushing, creating a PR) asks for confirmation;
when stdin is not a TTY the confirmation is treated as "no" and the action is aborted.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.features import FeatureCommand

MAX_DIFF_CHARS = 14_000
CONVENTIONAL_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
_CC_RE = re.compile(r"^(" + "|".join(CONVENTIONAL_TYPES) + r")(\([^)]+\))?!?: \S")


# ─── low-level git ──────────────────────────────────────────────────────────

def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(cwd: Path | str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run git with utf-8 decoding; never raises (returncode 127 when git is missing/fails)."""
    git = shutil.which("git")
    if not git:
        return subprocess.CompletedProcess(["git", *args], 127, "", "git not found on PATH")
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_PAGER", "cat")
    try:
        return subprocess.run(
            [git, "-c", "core.quotepath=off", "-c", "color.ui=false", *args],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(["git", *args], 127, "", str(e))


def _ok(cwd: Path, *args: str) -> str | None:
    r = run_git(cwd, *args)
    return r.stdout if r.returncode == 0 else None


def repo_root(cwd: Path | str) -> Path | None:
    out = _ok(Path(cwd), "rev-parse", "--show-toplevel")
    return Path(out.strip()) if out and out.strip() else None


# ─── status model ───────────────────────────────────────────────────────────

@dataclass
class FileChange:
    path: str
    added: int = 0
    deleted: int = 0
    status: str = "M"
    binary: bool = False


@dataclass
class RepoStatus:
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def repo_status(cwd: Path) -> RepoStatus:
    st = RepoStatus()
    out = _ok(cwd, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
    if out is None:
        return st
    items = out.split("\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if not item:
            continue
        if item.startswith("# branch.head "):
            st.branch = item[len("# branch.head "):]
        elif item.startswith("# branch.upstream "):
            st.upstream = item[len("# branch.upstream "):]
        elif item.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", item)
            if m:
                st.ahead, st.behind = int(m.group(1)), int(m.group(2))
        elif item.startswith("? "):
            st.untracked.append(item[2:])
        elif item.startswith(("1 ", "2 ")):
            parts = item.split(" ", 8)
            xy, path = parts[1], parts[-1]
            if item.startswith("2 "):
                i += 1  # skip original path of a rename/copy
            if xy[0] != ".":
                st.staged.append(path)
            if xy[1] != ".":
                st.unstaged.append(path)
        elif item.startswith("u "):
            st.conflicts.append(item.split(" ", 10)[-1])
    return st


def numstat(cwd: Path, *args: str) -> list[FileChange]:
    """Parse ``git diff --numstat -z <args>`` (handles renames and binary files)."""
    out = _ok(cwd, "diff", "--numstat", "-z", "-M", *args)
    if not out:
        return []
    changes: list[FileChange] = []
    items = out.split("\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if not item:
            continue
        parts = item.split("\t", 2)
        if len(parts) < 3:
            continue
        a, d, path = parts
        if path == "":  # rename: next two entries are old and new path
            path = items[i + 1] if i + 1 < len(items) else ""
            i += 2
        binary = a == "-"
        changes.append(FileChange(path=path, added=0 if binary else int(a), deleted=0 if binary else int(d), binary=binary))
    return changes


def _name_status(cwd: Path, *args: str) -> dict[str, str]:
    out = _ok(cwd, "diff", "--name-status", "-z", "-M", *args) or ""
    items = out.split("\0")
    res: dict[str, str] = {}
    i = 0
    while i < len(items):
        code = items[i]
        i += 1
        if not code:
            continue
        if code[0] in "RC":
            if i + 1 < len(items):
                res[items[i + 1]] = code[0]
            i += 2
        else:
            if i < len(items):
                res[items[i]] = code[0]
            i += 1
    return res


def staged_changes(cwd: Path) -> list[FileChange]:
    changes = numstat(cwd, "--cached")
    codes = _name_status(cwd, "--cached")
    for c in changes:
        c.status = codes.get(c.path, "M")
    return changes


# ─── output helpers ─────────────────────────────────────────────────────────

_COLOR_KEYS = ("BOLD", "DIM", "RST", "CYAN", "TEAL", "GOLD", "MINT", "SLATE", "ROSE", "DIFF_ADD", "DIFF_DEL")


def _theme() -> dict[str, str]:
    try:
        from axon.ui import theme as t
        return {k: getattr(t, k, "") for k in _COLOR_KEYS}
    except Exception:
        return {k: "" for k in _COLOR_KEYS}


def _out(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)


def _block_char() -> str:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█".encode(enc)
        return "█"
    except (UnicodeEncodeError, LookupError):
        return "#"


def _bar(n: int, total: int, width: int = 20) -> str:
    if total <= 0 or n <= 0:
        return ""
    return _block_char() * max(1, round(width * n / total))


def _is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def confirm(question: str, default: bool = False) -> bool:
    """Ask y/N on the terminal. Non-interactive stdin → False (destructive actions abort)."""
    if not _is_tty():
        return False
    hint = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _out()
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def _ws(agent: Any) -> Path:
    settings = getattr(agent, "settings", None)
    return Path(getattr(settings, "workspace", None) or Path.cwd()).resolve()


def _require_repo(agent: Any) -> Path | None:
    c = _theme()
    if not git_available():
        _out(f"{c['ROSE']}git is not installed or not on PATH.{c['RST']}")
        return None
    root = repo_root(_ws(agent))
    if root is None:
        _out(f"{c['ROSE']}Not inside a git repository: {_ws(agent)}{c['RST']}")
    return root


def _print_changes(title: str, changes: list[FileChange], c: dict[str, str], limit: int = 25) -> None:
    if not changes:
        return
    add = sum(x.added for x in changes)
    dele = sum(x.deleted for x in changes)
    _out(f"  {c['BOLD']}{title}{c['RST']} {c['DIM']}({len(changes)} files, {c['RST']}"
         f"{c['DIFF_ADD']}+{add}{c['RST']} {c['DIFF_DEL']}-{dele}{c['RST']}{c['DIM']}){c['RST']}")
    width = min(60, max(len(x.path) for x in changes))
    for x in changes[:limit]:
        stat = "binary" if x.binary else f"{c['DIFF_ADD']}+{x.added:<5}{c['RST']}{c['DIFF_DEL']}-{x.deleted:<5}{c['RST']}"
        _out(f"    {c['SLATE']}{x.status}{c['RST']} {x.path:<{width}}  {stat}")
    if len(changes) > limit:
        _out(f"    {c['DIM']}... {len(changes) - limit} more{c['RST']}")


# ─── LLM text generation ────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.search(r"```[\w-]*\n(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    return t.strip().strip('"').strip("'").strip()


def llm_generate(agent: Any, system: str, prompt: str, source: str, max_tokens: int = 600) -> str | None:
    """One non-tool LLM call via agent.provider (stream + finalize). Returns text or None."""
    provider = getattr(agent, "provider", None)
    if provider is None or not hasattr(provider, "stream"):
        return None
    settings = getattr(agent, "settings", None)
    model = getattr(settings, "model", None) or getattr(provider, "model", None) or ""
    try:
        for _ in provider.stream(
            model=model,
            system=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=max_tokens,
            effort="low",
            thinking=False,
        ):
            pass
        turn = provider.finalize()
    except KeyboardInterrupt:
        return None
    except Exception:
        return None
    usage = getattr(turn, "usage", None)
    ledger = getattr(agent, "ledger", None)
    if usage is not None and ledger is not None:
        try:
            from axon.agent.state import record_usage
            record_usage(ledger, getattr(turn, "model", None) or model, usage, source=source)
        except Exception:
            pass
    text = getattr(turn, "text", "") or ""
    return _strip_fences(text) or None


COMMIT_SYSTEM = (
    "You write git commit messages in the Conventional Commits format. Output ONLY the message: "
    "a subject line `type(scope): summary` (imperative mood, lowercase type, <= 72 chars, no period), "
    "then optionally a blank line and a short body of '- ' bullets explaining what and why. "
    f"Allowed types: {', '.join(CONVENTIONAL_TYPES)}. No code fences, no commentary."
)


def _scope_for(paths: list[str]) -> str:
    dirs = [p.split("/") for p in paths]
    if not dirs:
        return ""
    common: list[str] = []
    for parts in zip(*[d[:-1] for d in dirs]):
        if all(x == parts[0] for x in parts):
            common.append(parts[0])
        else:
            break
    skip = {"src", "lib", "app", "pkg", "packages", "internal"}
    meaningful = [x for x in common if x not in skip]
    if meaningful:
        return meaningful[-1]
    if len(paths) == 1:
        return os.path.splitext(paths[0].rsplit("/", 1)[-1])[0]
    return ""


def heuristic_commit_message(changes: list[FileChange], hint: str = "") -> str:
    """Deterministic Conventional-Commits message from the staged file list (LLM fallback)."""
    paths = [c.path for c in changes]
    low = [p.lower() for p in paths]
    hint_l = hint.lower()
    if any(w in hint_l for w in ("fix", "bug", "crash", "error")):
        ctype = "fix"
    elif low and all(re.search(r"(^|/)(tests?|spec)/|test_|_test\.|\.test\.|\.spec\.", p) for p in low):
        ctype = "test"
    elif low and all(p.endswith((".md", ".rst", ".txt", ".adoc")) or p.startswith("docs/") for p in low):
        ctype = "docs"
    elif low and all(p.startswith((".github/", ".gitlab", "ci/")) for p in low):
        ctype = "ci"
    elif low and all(p.rsplit("/", 1)[-1] in ("pyproject.toml", "package.json", "setup.py", "setup.cfg",
                                               "requirements.txt", "cargo.toml", "go.mod", "makefile",
                                               "package-lock.json", "poetry.lock", "uv.lock") for p in low):
        ctype = "build"
    elif changes and all(c.status == "A" for c in changes):
        ctype = "feat"
    elif changes and all(c.status == "D" for c in changes):
        ctype = "chore"
    else:
        ctype = "refactor" if hint_l.startswith("refactor") else "chore"
    scope = _scope_for(paths)
    if hint.strip():
        summary = hint.strip().rstrip(".")
        summary = summary[0].lower() + summary[1:] if summary else summary
    else:
        verb = {"A": "add", "D": "remove", "R": "rename"}.get(
            changes[0].status if changes and len({c.status for c in changes}) == 1 else "", "update")
        names = [p.rsplit("/", 1)[-1] for p in paths]
        if len(names) <= 3:
            summary = f"{verb} {', '.join(names)}"
        else:
            summary = f"{verb} {len(names)} files"
    subject = f"{ctype}({scope}): {summary}" if scope else f"{ctype}: {summary}"
    if len(subject) > 72:
        subject = subject[:69].rstrip() + "..."
    body = [f"- {c.status} {c.path} (+{c.added} -{c.deleted})" for c in changes[:20]]
    if len(changes) > 20:
        body.append(f"- ... and {len(changes) - 20} more files")
    return subject + ("\n\n" + "\n".join(body) if len(changes) > 1 else "")


def _clean_commit_message(text: str) -> str | None:
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    subject = lines[0].strip().strip("`").rstrip(".")
    if subject.lower().startswith(("commit message:", "subject:")):
        subject = subject.split(":", 1)[1].strip()
    if not subject:
        return None
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return subject + ("\n\n" + "\n".join(rest).strip() if rest else "")


def generate_commit_message(agent: Any, cwd: Path, changes: list[FileChange], hint: str = "") -> tuple[str, str]:
    """(message, origin) where origin is "llm" or "heuristic"."""
    diff = _ok(cwd, "diff", "--cached", "--no-ext-diff", "-U2") or ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
    stat = "\n".join(f"{c.status} {c.path} (+{c.added} -{c.deleted})" for c in changes)
    recent = (_ok(cwd, "log", "-n", "8", "--format=%s") or "").strip()
    prompt = (
        f"Staged files:\n{stat}\n\n"
        + (f"Recent commit subjects (match their style):\n{recent}\n\n" if recent else "")
        + (f"Author hint: {hint}\n\n" if hint.strip() else "")
        + f"Staged diff:\n{diff}"
    )
    text = llm_generate(agent, COMMIT_SYSTEM, prompt, source="git_commit")
    msg = _clean_commit_message(text) if text else None
    if msg:
        return msg, "llm"
    return heuristic_commit_message(changes, hint), "heuristic"


# ─── /commit ────────────────────────────────────────────────────────────────

def commit_with_message(cwd: Path, message: str) -> subprocess.CompletedProcess:
    """``git commit -F <tempfile>`` (keeps multi-line messages and unicode intact on every OS)."""
    fd, tmp = tempfile.mkstemp(prefix="axon-commit-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(message.rstrip() + "\n")
        return run_git(cwd, "commit", "-F", tmp, timeout=120)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def handle_commit(agent: Any, arg: str) -> None:
    c = _theme()
    root = _require_repo(agent)
    if root is None:
        return
    hint = (arg or "").strip()
    st = repo_status(root)
    if st.conflicts:
        _out(f"{c['ROSE']}Resolve merge conflicts first: {', '.join(st.conflicts[:5])}{c['RST']}")
        return
    staged = staged_changes(root)
    unstaged = numstat(root)
    _out(f"{c['BOLD']}{c['CYAN']}Commit{c['RST']}  {c['DIM']}{st.branch or '(detached)'} · {root}{c['RST']}")
    _print_changes("Staged", staged, c)
    _print_changes("Unstaged (tracked)", unstaged, c)
    if st.untracked:
        _out(f"  {c['DIM']}{len(st.untracked)} untracked file(s) will not be included{c['RST']}")
    if not staged:
        if not unstaged:
            _out(f"{c['GOLD']}Nothing to commit.{c['RST']}")
            return
        if not _is_tty():
            _out(f"{c['GOLD']}Nothing staged. Aborted (non-interactive; stage files with `git add` first).{c['RST']}")
            return
        if not confirm(f"Nothing staged. Stage all {len(unstaged)} tracked change(s) (git add -u)?"):
            _out(f"{c['DIM']}Aborted.{c['RST']}")
            return
        r = run_git(root, "add", "-u")
        if r.returncode != 0:
            _out(f"{c['ROSE']}git add failed: {r.stderr.strip()}{c['RST']}")
            return
        staged = staged_changes(root)
        if not staged:
            _out(f"{c['GOLD']}Nothing to commit.{c['RST']}")
            return
    _out(f"  {c['DIM']}Generating commit message...{c['RST']}")
    message, origin = generate_commit_message(agent, root, staged, hint)
    _out()
    _out(f"  {c['BOLD']}Message{c['RST']} {c['DIM']}({'generated by model' if origin == 'llm' else 'heuristic fallback'}){c['RST']}")
    for line in message.splitlines():
        _out(f"  {c['MINT']}|{c['RST']} {line}")
    _out()
    if not confirm("Commit with this message?"):
        _out(f"{c['DIM']}Aborted. Nothing committed.{c['RST']}")
        return
    r = commit_with_message(root, message)
    if r.returncode != 0:
        _out(f"{c['ROSE']}git commit failed:{c['RST']}\n{(r.stderr or r.stdout).strip()}")
        return
    sha = (_ok(root, "rev-parse", "--short", "HEAD") or "").strip()
    _out(f"{c['MINT']}Committed {sha}{c['RST']}  {message.splitlines()[0]}")


# ─── /pr ────────────────────────────────────────────────────────────────────

PR_SYSTEM = (
    "You write GitHub pull request descriptions. Output the title on the first line (<= 72 chars, "
    "Conventional-Commits style if the repo uses it), then a blank line, then a markdown body with "
    "'## Summary' (2-5 bullets) and '## Changes' sections. No code fences around the whole answer."
)


def default_base(cwd: Path) -> str:
    out = _ok(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if out and out.strip():
        return out.strip()
    for cand in ("origin/main", "origin/master", "main", "master", "develop"):
        if run_git(cwd, "rev-parse", "--verify", "--quiet", cand).returncode == 0:
            return cand
    return "main"


def heuristic_pr(branch: str, commits: list[str], stat: str) -> tuple[str, str]:
    if len(commits) == 1:
        title = commits[0]
    else:
        title = re.sub(r"[-_/]+", " ", branch).strip() or "Update"
        title = title[0].upper() + title[1:]
    body = "## Summary\n" + "\n".join(f"- {s}" for s in commits[:30])
    if len(commits) > 30:
        body += f"\n- ... and {len(commits) - 30} more commits"
    if stat.strip():
        body += "\n\n## Changes\n```\n" + stat.strip() + "\n```"
    return title[:72], body


def handle_pr(agent: Any, arg: str) -> None:
    c = _theme()
    root = _require_repo(agent)
    if root is None:
        return
    base = (arg or "").strip().split()[0] if (arg or "").strip() else default_base(root)
    st = repo_status(root)
    branch = st.branch
    if not branch or branch == "(detached)":
        _out(f"{c['ROSE']}Not on a branch (detached HEAD).{c['RST']}")
        return
    if run_git(root, "rev-parse", "--verify", "--quiet", base).returncode != 0:
        _out(f"{c['ROSE']}Base '{base}' not found. Usage: /pr [base]{c['RST']}")
        return
    log = _ok(root, "log", "--reverse", "--format=%s", f"{base}..HEAD") or ""
    commits = [ln for ln in log.splitlines() if ln.strip()]
    if not commits:
        _out(f"{c['GOLD']}No commits on {branch} that are not in {base}.{c['RST']}")
        return
    stat = _ok(root, "diff", "--stat=100", f"{base}...HEAD") or ""
    _out(f"{c['BOLD']}{c['CYAN']}Pull request{c['RST']}  {c['DIM']}{branch} -> {base} · {len(commits)} commit(s){c['RST']}")
    for s in commits[-10:]:
        _out(f"    {c['SLATE']}*{c['RST']} {s}")
    last_stat = stat.strip().splitlines()[-1] if stat.strip() else ""
    if last_stat:
        _out(f"  {c['DIM']}{last_stat.strip()}{c['RST']}")
    diff = _ok(root, "diff", "--no-ext-diff", "-U1", f"{base}...HEAD") or ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
    _out(f"  {c['DIM']}Generating title and description...{c['RST']}")
    text = llm_generate(agent, PR_SYSTEM,
                        f"Branch: {branch}\nBase: {base}\nCommits:\n" + "\n".join(commits)
                        + f"\n\nDiff stat:\n{stat}\n\nDiff:\n{diff}", source="git_pr", max_tokens=1200)
    title, body = "", ""
    if text:
        lines = text.strip().splitlines()
        title = lines[0].lstrip("# ").strip().strip("`")
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        body = "\n".join(lines[1:]).strip()
    if not title:
        title, body = heuristic_pr(branch, commits, stat)
    _out()
    _out(f"  {c['BOLD']}Title{c['RST']}  {title}")
    _out(f"  {c['BOLD']}Body{c['RST']}")
    for line in body.splitlines():
        _out(f"  {c['MINT']}|{c['RST']} {line}")
    _out()
    gh = shutil.which("gh")
    if not gh:
        _out(f"{c['DIM']}GitHub CLI (gh) not found; copy the title/body above to open the PR manually.{c['RST']}")
        return
    if not st.upstream:
        if not confirm(f"Branch '{branch}' has no upstream. Push it to origin (git push -u origin {branch})?"):
            _out(f"{c['DIM']}Aborted. Branch not pushed, no PR created.{c['RST']}")
            return
        r = run_git(root, "push", "-u", "origin", branch, timeout=180)
        if r.returncode != 0:
            _out(f"{c['ROSE']}git push failed:{c['RST']} {(r.stderr or r.stdout).strip()}")
            return
    elif st.ahead:
        if confirm(f"Push {st.ahead} unpushed commit(s) to {st.upstream} first?"):
            r = run_git(root, "push", timeout=180)
            if r.returncode != 0:
                _out(f"{c['ROSE']}git push failed:{c['RST']} {(r.stderr or r.stdout).strip()}")
                return
    if not confirm(f"Create pull request '{title}' with gh?"):
        _out(f"{c['DIM']}Aborted. No PR created.{c['RST']}")
        return
    base_branch = base.split("/", 1)[1] if base.startswith("origin/") else base
    fd, tmp = tempfile.mkstemp(prefix="axon-pr-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + "\n")
        try:
            r = subprocess.run([gh, "pr", "create", "--base", base_branch, "--title", title, "--body-file", tmp],
                               cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            _out(f"{c['ROSE']}gh pr create failed: {e}{c['RST']}")
            return
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        _out(f"{c['ROSE']}gh pr create failed:{c['RST']} {(r.stderr or r.stdout).strip()}")
        return
    _out(f"{c['MINT']}Pull request created:{c['RST']} {r.stdout.strip()}")


# ─── /git dashboard ─────────────────────────────────────────────────────────

def handle_git(agent: Any, arg: str) -> None:
    c = _theme()
    root = _require_repo(agent)
    if root is None:
        return
    st = repo_status(root)
    _out(f"{c['BOLD']}{c['CYAN']}Git{c['RST']}  {c['DIM']}{root}{c['RST']}")
    track = ""
    if st.upstream:
        track = f" -> {st.upstream}  " + (f"{c['MINT']}ahead {st.ahead}{c['RST']} " if st.ahead else "") \
            + (f"{c['ROSE']}behind {st.behind}{c['RST']}" if st.behind else "") \
            + ("" if st.ahead or st.behind else f"{c['DIM']}up to date{c['RST']}")
    else:
        track = f"  {c['DIM']}(no upstream){c['RST']}"
    _out(f"  {c['SLATE']}{'branch':<10}{c['RST']} {c['BOLD']}{st.branch or '(unknown)'}{c['RST']}{track}")
    counts = [("staged", len(st.staged), c["MINT"]), ("unstaged", len(st.unstaged), c["GOLD"]),
              ("untracked", len(st.untracked), c["SLATE"])]
    if st.conflicts:
        counts.append(("conflicts", len(st.conflicts), c["ROSE"]))
    total = max(1, max(n for _, n, _ in counts))
    for label, n, color in counts:
        _out(f"  {c['SLATE']}{label:<10}{c['RST']} {n:>4}  {color}{_bar(n, total)}{c['RST']}")
    stash = _ok(root, "stash", "list") or ""
    _out(f"  {c['SLATE']}{'stashes':<10}{c['RST']} {len([x for x in stash.splitlines() if x.strip()]):>4}")
    log = _ok(root, "log", "-n", "5", "--format=%h\x1f%s\x1f%cr\x1f%an") or ""
    if log.strip():
        _out()
        _out(f"  {c['BOLD']}Recent commits{c['RST']}")
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            sha, subj, when, who = parts
            if len(subj) > 64:
                subj = subj[:61] + "..."
            _out(f"    {c['GOLD']}{sha}{c['RST']} {subj}  {c['DIM']}{when} · {who}{c['RST']}")
    else:
        _out(f"  {c['DIM']}No commits yet.{c['RST']}")


# ─── /changes ───────────────────────────────────────────────────────────────

def _line_delta(old: bytes | None, new_path: Path) -> tuple[int, int, str]:
    """(+added, -deleted, status) between an original snapshot and the current file."""
    try:
        new = new_path.read_bytes() if new_path.is_file() else None
    except OSError:
        new = None
    if old is None and new is None:
        return 0, 0, "?"
    status = "A" if old is None else ("D" if new is None else "M")
    a = (old or b"").decode("utf-8", errors="replace").splitlines()
    b = (new or b"").decode("utf-8", errors="replace").splitlines()
    added = deleted = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            deleted += i2 - i1
        if op in ("replace", "insert"):
            added += j2 - j1
    return added, deleted, status


def session_changes(agent: Any) -> list[FileChange] | None:
    """Files edited this session (from the checkpoint manager), or None when unavailable."""
    cp = getattr(agent, "checkpoints", None)
    if cp is None:
        return None
    originals: dict[Path, bytes | None] = {}
    snaps: list[Any] = []
    try:
        snaps = list(getattr(cp, "history", []) or [])
        pending = getattr(cp, "_pending_files", None) or {}
        for snap in snaps:
            for p, data in (getattr(snap, "files", {}) or {}).items():
                originals.setdefault(Path(p), data)
        for p, data in pending.items():
            originals.setdefault(Path(p), data)
    except Exception:
        return None
    ws = _ws(agent)
    out: list[FileChange] = []
    for p in sorted(originals, key=str):
        added, deleted, status = _line_delta(originals[p], p)
        if status == "M" and added == 0 and deleted == 0:
            continue
        try:
            rel = p.resolve().relative_to(ws).as_posix()
        except ValueError:
            rel = str(p)
        out.append(FileChange(rel, added, deleted, status))
    return out


def handle_changes(agent: Any, arg: str) -> None:
    c = _theme()
    changes = session_changes(agent)
    source = "edited this session"
    if not changes:
        root = repo_root(_ws(agent)) if git_available() else None
        if root is None:
            if changes is None:
                _out(f"{c['GOLD']}No session edit history and not a git repository.{c['RST']}")
            else:
                _out(f"{c['DIM']}No files changed this session.{c['RST']}")
            return
        has_head = run_git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
        changes = numstat(root, "HEAD") if has_head else numstat(root, "--cached")
        codes = _name_status(root, "HEAD") if has_head else {}
        for ch in changes:
            ch.status = codes.get(ch.path, "M")
        for u in repo_status(root).untracked:
            try:
                n = len((root / u).read_bytes().decode("utf-8", errors="replace").splitlines())
            except OSError:
                n = 0
            changes.append(FileChange(u, n, 0, "?"))
        source = "uncommitted changes (git, vs HEAD)"
        if not changes:
            _out(f"{c['DIM']}No changes: working tree is clean.{c['RST']}")
            return
    _out(f"{c['BOLD']}{c['CYAN']}Changes{c['RST']}  {c['DIM']}{source}{c['RST']}")
    total = max(1, max(x.added + x.deleted for x in changes))
    width = min(60, max(len(x.path) for x in changes))
    add_c, del_c = _block_char(), _block_char()
    for x in changes:
        n_add = round(16 * x.added / total) if x.added else 0
        n_del = round(16 * x.deleted / total) if x.deleted else 0
        bar = f"{c['DIFF_ADD']}{add_c * max(n_add, 1 if x.added else 0)}{c['RST']}" \
              f"{c['DIFF_DEL']}{del_c * max(n_del, 1 if x.deleted else 0)}{c['RST']}"
        _out(f"  {c['SLATE']}{x.status}{c['RST']} {x.path:<{width}}  {c['DIFF_ADD']}+{x.added:<5}{c['RST']}"
             f"{c['DIFF_DEL']}-{x.deleted:<5}{c['RST']} {bar}")
    _out(f"  {c['DIM']}{len(changes)} file(s) · +{sum(x.added for x in changes)} "
         f"-{sum(x.deleted for x in changes)}{c['RST']}")


COMMANDS = [
    FeatureCommand(name="/commit", handler=handle_commit, category="Git",
                   description="Generate a Conventional Commit message for staged changes and commit (confirmed)",
                   usage="/commit [hint]"),
    FeatureCommand(name="/pr", handler=handle_pr, category="Git",
                   description="Summarize the branch vs base and open a PR with gh (confirmed)",
                   usage="/pr [base]"),
    FeatureCommand(name="/git", handler=handle_git, category="Git",
                   description="Git dashboard: branch, ahead/behind, change counts, recent commits, stashes",
                   usage="/git", aliases=("/gs",)),
    FeatureCommand(name="/changes", handler=handle_changes, category="Git",
                   description="Files changed this session with +/- line counts",
                   usage="/changes"),
]

__all__ = [
    "run_git", "repo_root", "repo_status", "numstat", "staged_changes", "heuristic_commit_message",
    "generate_commit_message", "commit_with_message", "heuristic_pr", "session_changes", "confirm",
    "COMMANDS",
]
