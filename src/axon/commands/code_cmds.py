"""Code & review commands: /diff /review /test /copy /output /transcript /find /paste /todos /queue /btw."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from axon.commands.common import (
    err, git, header, info, is_tty, ok, one_shot, run_cmd, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Code & Review"


def _print_diff(text: str) -> None:
    try:
        from axon.ui.render import render_diff_tool_box
        print(f"\n{render_diff_tool_box(text, max_show=400)}\n")
        return
    except Exception:
        pass
    for ln in text.splitlines():
        color = MINT if ln.startswith("+") and not ln.startswith("+++") else ROSE if ln.startswith("-") and not ln.startswith("---") else CYAN if ln.startswith("@@") else SLATE
        print(f"  {color}{ln}{RST}")
    print()


def _checkpoint_diff(agent: Any, n: int) -> str:
    cp = getattr(agent, "checkpoints", None)
    snaps = cp.list_snapshots() if cp is not None and hasattr(cp, "list_snapshots") else []
    if not 1 <= n <= len(snaps):
        return ""
    snap = snaps[n - 1]
    ws = ws_of(agent)
    out: list[str] = []
    for path, original in snap.files.items():
        try:
            rel = str(Path(path).resolve().relative_to(ws.resolve()))
        except ValueError:
            rel = str(path)
        before = (original or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        try:
            after = Path(path).read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True) if Path(path).exists() else []
        except OSError:
            after = []
        out.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
    return "".join(out)


def changed_files(ws: Path, args: list[str]) -> list[tuple[str, str]]:
    """(path, "+a -d" stats) for each changed tracked file, plus untracked files."""
    code, out = git(ws, *args, "--numstat")
    rows: list[tuple[str, str]] = []
    if code == 0:
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) == 3:
                rows.append((parts[2], f"+{parts[0]} -{parts[1]}"))
    if args == ["diff"]:
        _, untracked = git(ws, "ls-files", "--others", "--exclude-standard")
        rows += [(f, "untracked") for f in untracked.splitlines() if f.strip()]
    return rows


def _diff_viewer(agent: Any, args: list[str]) -> CommandResult:
    """Interactive diff: Up/Down choose a file, Enter shows its diff, a attaches it to the next prompt."""
    from axon.ui.picker import SelectItem, Selector
    ws = ws_of(agent)
    files = changed_files(ws, args)
    if not files:
        ok("Working tree clean.")
        return CommandResult(handled=True)

    def file_diff(path: str, stat: str) -> str:
        if stat == "untracked":
            try:
                body = (ws / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return ""
            return f"--- /dev/null\n+++ b/{path}\n" + "\n".join("+" + ln for ln in body[:400])
        return git(ws, *args, "--", path)[1]

    def on_key(sel: Any, key: str) -> bool:
        cur = sel.current()
        if cur is not None and key in ("a", "tab"):
            path, stat = cur.value
            agent.pending_input = f"About my change to {path}:\n```diff\n{file_diff(path, stat)[:20_000]}\n```\n"
            sel.finish(("attach", path))
            return True
        return False

    while True:
        items = [SelectItem(label=p, value=(p, st), hint=st) for p, st in files]
        chosen = Selector(items, title="Changes", subtitle=f"{len(files)} file(s) · Enter view · a attach to next prompt",
                          filterable=False, on_key=on_key, max_visible=14).run()
        if chosen is None:
            return CommandResult(handled=True)
        if isinstance(chosen, tuple) and chosen[0] == "attach":
            ok(f"Diff of {chosen[1]} will be attached to your next prompt.")
            return CommandResult(handled=True)
        path, stat = chosen.value
        text = file_diff(path, stat)
        header(path, stat)
        _print_diff(text) if text.strip() else info("(no textual diff)")


@command("diff", category=CAT, description="Uncommitted changes, or edits since checkpoint T<n>",
         aliases=("changes",), usage="/diff            working tree vs HEAD\n/diff staged     staged changes\n/diff T<n>       file edits made since checkpoint n (see /rewind list)")
def handle_diff(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    ws = ws_of(agent)
    m = re.fullmatch(r"[Tt](\d+)", a)
    if m:
        text = _checkpoint_diff(agent, int(m.group(1)))
        if not text:
            info(f"No checkpoint T{m.group(1)} with changes. /rewind list shows checkpoints.")
        else:
            _print_diff(text)
        return CommandResult(handled=True)
    args = ["diff", "--cached"] if a.lower() in ("staged", "cached", "--cached") else ["diff"]
    if is_tty() and a.lower() in ("", "staged", "cached", "--cached"):
        return _diff_viewer(agent, args)
    code, stat = git(ws, *args, "--stat")
    if code != 0:
        err(stat.strip() or "Not a git repository.")
        return CommandResult(handled=True)
    code, text = git(ws, *args)
    _, untracked = git(ws, "ls-files", "--others", "--exclude-standard")
    if not text.strip():
        ok("Working tree clean." if not untracked.strip() else "No changes to tracked files.")
    else:
        header("Changes", stat.strip().splitlines()[-1] if stat.strip() else "")
        _print_diff(text)
    if untracked.strip():
        files = untracked.strip().splitlines()
        print(f"  {GOLD}Untracked ({len(files)}):{RST} {SLATE}{', '.join(files[:12])}{' …' if len(files) > 12 else ''}{RST}\n")
    return CommandResult(handled=True)


_DEFAULT_REVIEW = (
    "Review the uncommitted changes in this repository (run `git diff` and `git status`). "
    "Look for bugs, edge cases, security problems, missing tests and style inconsistencies. "
    "Report findings grouped by severity with file:line references; do not modify files."
)

_REVIEW_EFFORT = {
    "low": "Report only a few findings you are highly confident are real bugs (at most 5).",
    "medium": "Report the clear correctness problems plus notable risks (at most 10 findings).",
    "high": "Be thorough: include likely bugs and medium-confidence issues with your confidence.",
    "max": "Be exhaustive: every bug, edge case, risk and cleanup you can find, marking uncertain ones.",
}
_REVIEW_EFFORT["xhigh"] = _REVIEW_EFFORT["max"]

_SECURITY_REVIEW = (
    "Do a security review of the changes on this branch versus its base (try `git merge-base HEAD origin/main` "
    "or `main`/`master`, then `git diff <base>...HEAD`; include uncommitted changes from `git diff`). Look for "
    "injection (SQL, shell, path traversal), authn/authz gaps, secrets in code or logs, unsafe deserialization, "
    "SSRF, XSS/CSRF, insecure crypto or randomness, race conditions and dependency risks. For each finding give "
    "severity, file:line, an exploit scenario and the fix. Only report issues introduced or touched by the diff; "
    "say explicitly when nothing exploitable was found. Do not modify files."
)

_SIMPLIFY = (
    "Review the changed code (git diff against the base branch plus uncommitted changes) for reuse, "
    "simplification and efficiency: duplicated logic that existing helpers already cover, needless abstraction, "
    "dead code, over-long functions, redundant work in loops. Then apply the clear improvements yourself without "
    "changing behaviour, keep each change small, and run the relevant tests afterwards. Summarise what you changed."
)


def build_review_prompt(arg: str, base_prompt: str = _DEFAULT_REVIEW) -> tuple[str, str]:
    """Review prompt from `/review` arguments: [low|medium|high|max] [--fix] [--comment]
    [--max-findings N|all] [PR number | focus]. Returns (prompt, label)."""
    words = arg.split()
    effort = next((w.lower() for w in words if w.lower() in _REVIEW_EFFORT), "")
    fix = "--fix" in words
    comment = "--comment" in words
    max_findings = ""
    rest: list[str] = []
    skip = False
    for i, w in enumerate(words):
        if skip:
            skip = False
            continue
        if w == "--max-findings" and i + 1 < len(words):
            max_findings, skip = words[i + 1], True
        elif w.startswith("--max-findings="):
            max_findings = w.split("=", 1)[1]
        elif w.lower() in _REVIEW_EFFORT or w in ("--fix", "--comment"):
            continue
        else:
            rest.append(w)
    target = " ".join(rest)
    prompt = base_prompt
    if fix:
        prompt = prompt.replace("; do not modify files.", ".") + (
            "\n\nAfter reporting, fix every finding you are confident about directly in the code, then re-run "
            "the relevant tests and list what you changed.")
    if effort:
        prompt += f"\n\n{_REVIEW_EFFORT[effort]}"
    if max_findings and max_findings.lower() != "all":
        prompt += f"\n\nReport at most {max_findings} findings, most important first."
    elif max_findings.lower() == "all":
        prompt += "\n\nReport every finding; do not cap the list."
    if target.isdigit():
        prompt += f"\n\nReview pull request #{target} instead (use `gh pr diff {target}` if the GitHub CLI is available)."
        if comment:
            prompt += (f"\nPost each finding as an inline review comment on PR #{target} with `gh api` "
                       "(repos/{owner}/{repo}/pulls/<n>/comments) and finish with a summary review.")
    elif target:
        prompt += f"\n\nFocus: {target}"
    label = " · ".join(x for x in (effort, "fix" if fix else "", "comment" if comment else "", target) if x)
    return prompt, label


@command("review", category=CAT, description="Code review of the current changes (or a focus area)",
         aliases=("code-review",),
         usage="/review [low|medium|high|max] [--fix] [--comment] [--max-findings N|all] [focus | file | PR number]")
def handle_review(agent: Any, arg: str) -> CommandResult:
    mgr = getattr(agent, "skills", None)
    base = _DEFAULT_REVIEW
    try:
        if mgr is not None and mgr.get("code-review") is not None:
            from axon.commands.custom import make_shell_gate
            sk = mgr.get("code-review")
            base = mgr.execute_skill("code-review", "", allow_shell=make_shell_gate(agent, sk.allowed_tools)) + "\n\n" + _DEFAULT_REVIEW
    except Exception:
        base = _DEFAULT_REVIEW
    prompt, label = build_review_prompt(arg, base)
    print(f"\n  {TEAL}▸ Code review{RST}{SLATE}{' · ' + label if label else ''}{RST}\n")
    agent.run_turn(prompt)
    return CommandResult(handled=True)


@command("security-review", category=CAT, description="Security review of the branch's changes vs its base",
         usage="/security-review [focus]")
def handle_security_review(agent: Any, arg: str) -> CommandResult:
    prompt = _SECURITY_REVIEW + (f"\n\nFocus: {arg.strip()}" if arg.strip() else "")
    print(f"\n  {TEAL}▸ Security review{RST}\n")
    agent.run_turn(prompt)
    return CommandResult(handled=True)


@command("simplify", category=CAT, description="Simplify the changed code (reuse, dead code, efficiency) and apply fixes",
         usage="/simplify [focus]")
def handle_simplify(agent: Any, arg: str) -> CommandResult:
    prompt = _SIMPLIFY + (f"\n\nFocus: {arg.strip()}" if arg.strip() else "")
    print(f"\n  {TEAL}▸ Simplify{RST}\n")
    agent.run_turn(prompt)
    return CommandResult(handled=True)


def _detect_tests(ws: Path, extra: str) -> tuple[list[str], str]:
    import shlex
    try:  # quoted arguments stay whole: /test -k "login and not slow"
        extra_args = shlex.split(extra, posix=(sys.platform != "win32")) if extra else []
    except ValueError:
        extra_args = extra.split()
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists() or (ws / "setup.cfg").exists() or (ws / "tests").is_dir():
        if shutil.which("pytest"):
            return ["pytest", "-q", *extra_args], "pytest"
        return [sys.executable, "-m", "pytest", "-q", *extra_args], "pytest"
    if (ws / "package.json").exists():
        return ["npm", "test", *(["--", *extra_args] if extra_args else [])], "npm test"
    if (ws / "Cargo.toml").exists():
        return ["cargo", "test", *extra_args], "cargo test"
    if (ws / "go.mod").exists():
        return ["go", "test", *(extra_args or ["./..."])], "go test"
    return [], ""


@command("test", category=CAT, description="Detect and run the project's test suite", aliases=("tests",),
         usage="/test [extra args]")
def handle_test(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    argv, name = _detect_tests(ws, arg.strip())
    if not argv:
        info("No test runner detected (pytest, npm, cargo, go).")
        return CommandResult(handled=True)
    if shutil.which(argv[0]) is None and argv[0] != sys.executable:
        err(f"{argv[0]} is not on PATH.")
        return CommandResult(handled=True)
    print(f"\n  {CYAN}▸ {name}:{RST} {WHITE}{' '.join(argv)}{RST}\n")
    try:
        proc = run_cmd(argv, ws, timeout=600)
    except subprocess.TimeoutExpired:
        err("Tests timed out after 10 minutes.")
        return CommandResult(handled=True)
    except OSError as e:
        err(f"Could not run tests: {e}")
        return CommandResult(handled=True)
    lines = (proc.stdout + ("\n" + proc.stderr if proc.returncode else "")).strip().splitlines()
    for ln in lines[-25:]:
        print(f"    {ln}")
    if proc.returncode == 0:
        print(f"\n  {MINT}{BOLD}✓ Tests passed{RST}\n")
    else:
        print(f"\n  {ROSE}{BOLD}✗ Tests failed (exit {proc.returncode}){RST}  {SLATE}ask the agent to fix them, or /review{RST}\n")
    return CommandResult(handled=True)


def _assistant_text(m: dict[str, Any]) -> str:
    c = m.get("content", "")
    if isinstance(c, str):
        return c if c.strip() else ""
    if isinstance(c, list):
        texts = [str(b.get("text", "")) for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if any(t.strip() for t in texts):
            return "\n".join(texts)
    return ""


def answers(agent: Any) -> list[str]:
    """Final assistant text of every turn, oldest first (one entry per user prompt that got an answer)."""
    from axon.session.store import is_user_prompt
    out: list[str] = []
    current = ""
    for m in agent.conversation.messages:
        if not isinstance(m, dict):
            continue
        if is_user_prompt(m):
            if current:
                out.append(current)
            current = ""
        elif m.get("role") == "assistant":
            t = _assistant_text(m)
            if t:
                current = t
    if current:
        out.append(current)
    return out


def _last_assistant_text(agent: Any) -> str:
    a = answers(agent)
    return a[-1] if a else ""


def code_blocks(text: str) -> list[tuple[str, str]]:
    """(language, code) for every fenced block."""
    return [(lang.strip(), body.rstrip("\n")) for lang, body in re.findall(r"```([^\n]*)\n([\s\S]*?)```", text)]


def _pick_block(blocks: list[tuple[str, str]]) -> str | None:
    """Let the user choose one block (or all) on a terminal; None means all."""
    from axon.ui.picker import SelectItem, Selector
    items = [SelectItem(label="All code blocks", value=-1)]
    for i, (lang, body) in enumerate(blocks):
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        items.append(SelectItem(label=f"{i + 1}. {lang or 'text'}  {first[:60]}", value=i, hint=f"{len(body.splitlines())} lines"))
    chosen = Selector(items, title="Copy code block").run()
    if chosen is None:
        return ""
    return None if chosen.value == -1 else blocks[chosen.value][1]


@command("copy", category=CAT, description="Copy an answer (N = Nth latest), its code blocks, or the git diff",
         aliases=("cp",), usage="/copy            last answer\n/copy <N>        Nth-latest answer (1 = last)\n/copy code [N]   code blocks of an answer (picker when there are several)\n/copy diff       git diff")
def handle_copy(agent: Any, arg: str) -> CommandResult:
    from axon.ui.clipboard import copy_to_clipboard
    words = arg.strip().lower().split()
    sub = words[0] if words and not words[0].isdigit() else ""
    nums = [int(w) for w in words if w.isdigit()]
    n = nums[0] if nums else 1
    if sub == "diff":
        code, text = git(ws_of(agent), "diff")
        label = "git diff"
        text = text if code == 0 else ""
    elif sub and sub != "code":
        usage("/copy [N] | /copy code [N] | /copy diff")
        return CommandResult(handled=True)
    else:
        all_answers = answers(agent)
        if n < 1 or n > len(all_answers):
            info(f"No answer #{n} (this session has {len(all_answers)}).")
            return CommandResult(handled=True)
        text = all_answers[-n]
        label = "last answer" if n == 1 else f"answer #{n} from the end"
        if sub == "code":
            blocks = code_blocks(text)
            picked = _pick_block(blocks) if len(blocks) > 1 and is_tty() else None
            if picked == "":
                info("Copy cancelled.")
                return CommandResult(handled=True)
            if picked is not None:
                text, label = picked, "1 code block"
            else:
                text = "\n\n".join(b for _, b in blocks)
                label = f"{len(blocks)} code block(s)"
    if not text.strip():
        info("Nothing to copy.")
        return CommandResult(handled=True)
    if copy_to_clipboard(text):
        ok(f"Copied {label} ({len(text):,} chars)")
    else:
        err("No clipboard utility available (pbcopy, clip, xclip, wl-copy).")
    return CommandResult(handled=True)


def _recorded_outputs(agent: Any) -> list[dict[str, Any]]:
    """Tool outputs saved in the session transcript (+ full-output files): used after a resume, when
    the in-memory store of this process is empty."""
    fn = getattr(getattr(agent, "session", None), "recorded_tool_outputs", None)
    try:
        return list(fn()) if callable(fn) else []
    except Exception:
        return []


@command("output", category=CAT, description="Full output of a tool call (/output 7), the last one, or a workspace file",
         aliases=("expand", "view", "more"), usage="/output [n | list | path] [pager]\n/output list saved   tool outputs recorded in this session (also after a resume)")
def handle_output(agent: Any, arg: str) -> CommandResult:
    from axon.ui.render import get_last_tool_output, get_tool_output, highlight_code, list_tool_outputs
    words = arg.split()
    use_pager = "pager" in words
    target = " ".join(w for w in words if w != "pager").strip()
    out, title = "", "Last tool output"
    if target in ("list", "ls", "saved", "list saved"):
        rows = list_tool_outputs() if target in ("list", "ls") else []
        if not rows:
            saved = _recorded_outputs(agent)
            if saved:
                header("Tool outputs saved in this session", "/output <n> shows one in full")
                for r in saved[-40:]:
                    count = len(str(r["content"]).splitlines())
                    print(f"  {DARK_SLATE}{r['n']:>5}{RST}  {WHITE}{r['name']}{RST}  {SLATE}{r['target'][:70]} · {count} lines{RST}")
                print()
                return CommandResult(handled=True)
        if not rows:
            info("No tool output yet.")
            return CommandResult(handled=True)
        header("Recent tool outputs", "/output <n> shows one in full")
        for n, name, tgt, count in rows:
            print(f"  {DARK_SLATE}{n:>5}{RST}  {WHITE}{name}{RST}  {SLATE}{tgt[:70]} · {count} lines{RST}")
        print()
        return CommandResult(handled=True)
    if target.lstrip("#").isdigit() and not (ws_of(agent) / target).is_file():
        item = get_tool_output(int(target.lstrip("#")))
        if item is None and not list_tool_outputs():
            rec = next((r for r in _recorded_outputs(agent) if r["n"] == int(target.lstrip("#"))), None)
            if rec is not None:
                item = (rec["name"], rec["target"], rec["content"])
        if item is None:
            err(f"No tool output #{target.lstrip('#')} (it may be too old). /output list shows what is kept.")
            return CommandResult(handled=True)
        name, tgt, out = item
        title = f"#{target.lstrip('#')} {name} {tgt}".strip()
        if not out:
            info("That tool call produced no output.")
            return CommandResult(handled=True)
        target = ""
    if target:
        from axon.permissions.paths import is_protected_path, is_within
        ws = ws_of(agent)
        p = Path(target).expanduser()
        p = p if p.is_absolute() else ws / p
        if not is_within(ws, p) or is_protected_path(p, ws):
            err("Only files inside the workspace can be shown.")
            return CommandResult(handled=True)
        if not p.is_file():
            err(f"No such file: {target}")
            return CommandResult(handled=True)
        out = p.read_text(encoding="utf-8", errors="replace")
        title = target
    elif not out:
        out = get_last_tool_output() if list_tool_outputs() else ""
        if not out:
            saved = _recorded_outputs(agent)
            if saved:
                out = str(saved[-1]["content"])
                title = f"#{saved[-1]['n']} {saved[-1]['name']} {saved[-1]['target']}".strip()
    if not out:
        info("No tool output yet.")
        return CommandResult(handled=True)
    lines = out.splitlines()
    if use_pager and is_tty():
        import pydoc
        pydoc.pager("\n".join(f"{i:5d}  {ln}" for i, ln in enumerate(lines, 1)))
        return CommandResult(handled=True)
    header(title, f"{len(lines)} lines · {len(out):,} chars · /transcript (Ctrl+F) expands any tool call")
    for i, ln in enumerate(lines, 1):
        print(f"  {DARK_SLATE}{i:5d} │{RST} {highlight_code(ln)}")
    print()
    return CommandResult(handled=True)


@command("transcript", category=CAT,
         description="Full-screen transcript: click or Enter expands a tool call's full output (Ctrl+F)",
         aliases=("tr", "scrollback"),
         usage="/transcript   (keys: click/enter expand · e/c all · t turns · / search · n/N · y copy · q close)")
def handle_transcript(agent: Any, arg: str) -> CommandResult:
    from axon.ui.transcript_view import open_transcript_viewer
    open_transcript_viewer(agent)
    return CommandResult(handled=True)


@command("find", category=CAT, description="Fuzzy-find a workspace file (Ctrl+P while typing)", aliases=("fuzzy",),
         usage="/find [query]")
def handle_find(agent: Any, arg: str) -> CommandResult:
    from axon.ui.fuzzy_picker import run_fuzzy_file_finder
    query = arg.strip()
    chosen = run_fuzzy_file_finder(ws_of(agent), query)
    if chosen:
        print(f"\n  {MINT}✓{RST} {WHITE}{chosen}{RST}  {SLATE}mention it as {BOLD}@{chosen}{RST}\n")
    elif query:
        info(f"No workspace file matches '{query}'.")
    return CommandResult(handled=True)


@command("paste", category=CAT, description="Multi-line input: paste text, finish with END", aliases=("multiline",))
def handle_paste(agent: Any, arg: str) -> CommandResult:
    print(f"\n  {TEAL}Paste your text, then type {BOLD}{GOLD}END{RST}{TEAL} on its own line:{RST}")
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line.strip() == "END":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        info("Paste cancelled.")
        return CommandResult(handled=True)
    text = "\n".join(lines).strip()
    if not text:
        info("Empty paste ignored.")
        return CommandResult(handled=True)
    print(f"\n  {TEAL}⚡ Sending {len(text):,} chars…{RST}\n")
    agent.run_turn(text)
    return CommandResult(handled=True)


@command("todos", category=CAT, description="Show the agent's task checklist", aliases=("todo",))
def handle_todos(agent: Any, arg: str) -> CommandResult:
    todos = getattr(agent, "todos", None)
    if todos is None or not getattr(todos, "items", None):
        info("No task list yet. The agent creates one with TodoWrite on multi-step work.")
        return CommandResult(handled=True)
    from axon.ui.render import render_todo_box
    print(f"\n{render_todo_box(todos.render())}\n")
    return CommandResult(handled=True)


def _run_queued_shell(agent: Any, cmd: str) -> None:
    from axon.commands.custom import make_shell_gate
    from axon.skills.manager import run_shell_capture
    if not make_shell_gate(agent, [])(cmd):
        err(f"Not permitted: {cmd}")
        return
    code, out = run_shell_capture(cmd, ws_of(agent), timeout=float(getattr(agent.settings, "bash_timeout_s", 180)))
    print(out)
    print(f"  {SLATE}(exit {code}){RST}\n")


@command("queue", category=CAT, description="Queue prompts to run after the current turn", aliases=("q",),
         usage="/queue                show queue\n/queue <prompt>       add\n/queue drop <id>      remove\n/queue run            run the next item now\n/queue clear")
def handle_queue(agent: Any, arg: str) -> CommandResult:
    from axon.ui.render import render_queue_box
    if getattr(agent, "message_queue", None) is None:
        from axon.agent.state import MessageQueue
        agent.message_queue = MessageQueue()
    q = agent.message_queue
    a = arg.strip()
    if not a:
        print(f"\n{render_queue_box(q)}\n")
        return CommandResult(handled=True)
    sub, _, rest = a.partition(" ")
    sub_l = sub.lower()
    if sub_l in ("drop", "rm", "remove", "delete") and rest.strip():
        try:
            item_id = int(rest.strip().lstrip("#"))
        except ValueError:
            usage("/queue drop <id>")
            return CommandResult(handled=True)
        if q.remove(item_id):
            ok(f"Removed #{item_id}")
        else:
            err(f"No queued message #{item_id}")
        return CommandResult(handled=True)
    if sub_l in ("clear", "empty", "reset") and not rest:
        n = len(q)
        q.clear()
        ok(f"Cleared {n} queued message(s)")
        return CommandResult(handled=True)
    if sub_l in ("pop", "next", "run") and not rest:
        nxt = q.pop()
        if not nxt:
            info("Queue is empty.")
            return CommandResult(handled=True)
        text = nxt.text.strip()
        print(f"\n  {TEAL}▸ Running queued #{nxt.id}:{RST} {WHITE}{text}{RST}\n")
        if text.startswith("!"):
            _run_queued_shell(agent, text[1:].strip())
        elif text.startswith("/"):
            from axon.commands.registry import dispatch_command
            dispatch_command(text, agent)
        else:
            agent.run_turn(text)
        return CommandResult(handled=True)
    item = q.push(a)
    print(f"\n  {MINT}✓ Queued #{item.id}{RST} {SLATE}({len(q)} pending){RST} {WHITE}{item.text}{RST}\n")
    return CommandResult(handled=True)


_BTW_SYSTEM = (
    "You are a factual, concise technical assistant answering a quick side question. "
    "Answer directly in 2-6 sentences or a short code snippet. Say so when you are unsure."
)


def session_context(agent: Any, max_messages: int = 40, max_chars: int = 12_000) -> tuple[str, int]:
    """Readable digest of the recent conversation (about the last 20 exchanges) for side questions:
    user prompts and answers in full, tool calls and results as one-liners. Returns (text, n_messages)."""
    msgs = list(getattr(getattr(agent, "conversation", None), "messages", []) or [])[-max_messages:]
    lines: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "?"))
        c = m.get("content", "")
        if isinstance(c, str):
            if c.strip():
                lines.append(f"{role}: {c.strip()[:1500]}")
            continue
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and str(b.get("text", "")).strip():
                lines.append(f"{role}: {str(b['text']).strip()[:1500]}")
            elif t == "tool_use":
                import json as _json
                lines.append(f"[tool call {b.get('name')}: {_json.dumps(b.get('input'), ensure_ascii=False, default=str)[:200]}]")
            elif t == "tool_result":
                first = " ".join(str(b.get("content", "")).split())[:160]
                lines.append(f"[tool result: {first}]")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text, len(msgs) if lines else 0


@command("btw", category=CAT, description="Quick side question, kept out of the conversation",
         aliases=("ptw",), usage="/btw <question>")
def handle_btw(agent: Any, arg: str) -> CommandResult:
    from axon.ui.render import render_side_question_box
    question = arg.strip()
    if not question:
        usage("/btw <question>   (a quick side question that is not added to the conversation)")
        return CommandResult(handled=True)
    context = session_context(agent)
    note = f" {SLATE}(with context of the last {context[1]} message(s)){RST}" if context[1] else ""
    print(f"\n  {CYAN}⚡ Side question:{RST} {WHITE}{question}{RST}{note}\n")
    prompt = (f"<session_context>\n{context[0]}\n</session_context>\n\nSide question (answer it; do not continue the task): {question}"
              if context[1] else question)
    from axon.session.activity import record_side_question
    try:
        answer = one_shot(agent, _BTW_SYSTEM, prompt, max_tokens=800, source="side_question") or "(no answer)"
    except Exception as e:
        record_side_question(agent, question, error=f"{type(e).__name__}: {e}", source="btw")
        err(f"Side question failed: {e}")
        return CommandResult(handled=True)
    record_side_question(agent, question, answer, source="btw")
    print(f"{render_side_question_box(question, answer)}\n")
    return CommandResult(handled=True)
