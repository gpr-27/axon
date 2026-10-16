"""E2E-F: slash commands do what they say.

``test_e2e_b_every_command.py`` only proves that every command runs without crashing. This module
checks *behaviour* for the commands that had no behavioural end-to-end test: each case runs one or
more ``axon -p [--continue] "/cmd ..."`` invocations of the real CLI (own AXON_HOME, workspace and
fake LLM server) and asserts on the printed result, the files/settings it changed, or the request
it sent to the model.

All cases run concurrently in a module fixture (each one isolated), one test per case reports.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from fake_llm import FakeLLMServer, Recorded
from harness import IS_WINDOWS, AxonEnv, Result, create_axon_env
from test_e2e_b_util import no_crash

pytestmark = pytest.mark.e2e

SEED_ANSWER = "SEED-ANSWER done.\n\n```python\nprint('seed')\n```"
SEED_SCRIPTS = (
    [{"text": "Seeding."}, {"tool": "Write", "args": {"path": "seed.py", "content": "print('seed')\n"}}],
    [{"text": "Running."}, {"tool": "Bash", "args": {"command": "echo BASH-OUT-123", "description": "echo"}}],
    [{"text": SEED_ANSWER}],
)

BUNDLED_SKILLS = ("api-tester", "benchmark", "db-migration", "deep-research", "docgen", "docker", "frontend-ui",
                  "git-workflow", "refactor", "security-audit", "skill-creator", "subagent-fanout", "table-search",
                  "test-gen")
# bundled skills whose bare name is taken by a built-in (reachable as /skill:<name>)
SHADOWED_SKILLS = ("code-review", "debug", "optimize", "verify")


@dataclass
class Ctx:
    env: AxonEnv
    results: list[Result]
    requests: list[Recorded]          # completion requests made by the steps (seeding excluded)
    clip: Path

    @property
    def out(self) -> str:
        return self.results[-1].output

    def o(self, i: int) -> str:
        return self.results[i].output

    @property
    def settings(self) -> dict[str, Any]:
        p = self.env.axon_home / "settings.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def user_texts(self) -> list[str]:
        return [r.last_user_text() for r in self.requests]


@dataclass
class Case:
    id: str
    steps: list[list[str] | str]
    check: Callable[[Ctx], None]
    seed: bool = False
    git: bool = False
    setup: Callable[[AxonEnv], None] | None = None
    replies: list[Any] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    posix_only: bool = False


def _shims(d: Path, clip: Path) -> dict[str, str]:
    """PATH shims: a fake clipboard (never touch the real one) and a `pytest` bound to this interpreter."""
    d.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        return {}
    for name in ("pbcopy", "wl-copy", "xclip", "xsel"):
        p = d / name
        p.write_text(f'#!/bin/sh\ncat > "{clip}"\n', encoding="utf-8")
        p.chmod(0o755)
    p = d / "pytest"
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m pytest -p no:cacheprovider "$@"\n', encoding="utf-8")
    p.chmod(0o755)
    return {"PATH": f"{d}{os.pathsep}{os.environ.get('PATH', '')}"}


def _git(env: AxonEnv, *args: str) -> None:
    subprocess.run(["git", *args], cwd=env.workspace, env=env.env, check=True, capture_output=True)


def _run_case(case: Case, root: Path) -> Ctx:
    llm = FakeLLMServer(model_ids=("gpt-4o", "fake-model")).start()
    try:
        env = create_axon_env(root, llm, git=case.git)
        clip = root / "clipboard.txt"
        env.env.update(_shims(root / "bin", clip))
        env.env.update(case.env)
        if case.setup:
            case.setup(env)
        if case.seed:
            llm.enqueue(*SEED_SCRIPTS)
            r = env.run(["-p", "create seed.py"])
            assert r.code == 0, str(r)
        llm.reset()
        llm.enqueue(*case.replies)
        results = []
        for step in case.steps:
            args = step if isinstance(step, list) else (["-p", "--continue", step] if case.seed else ["-p", step])
            results.append(env.run(args, timeout=90))
        return Ctx(env=env, results=results, requests=llm.completion_requests(), clip=clip)
    finally:
        llm.stop()


# ─── checks ────────────────────────────────────────────────────────────────

def _find(c: Ctx) -> None:
    assert "src/deep/widget_factory.py" in c.o(0) and "@src/deep/widget_factory.py" in c.o(0), c.o(0)
    assert "No workspace file matches 'zzzqqq'" in c.o(1), c.o(1)


def _find_setup(env: AxonEnv) -> None:
    (env.workspace / "src" / "deep").mkdir(parents=True)
    (env.workspace / "src" / "deep" / "widget_factory.py").write_text("x = 1\n", encoding="utf-8")
    (env.workspace / "README.md").write_text("# readme\n", encoding="utf-8")
    (env.workspace / "aaa.txt").write_text("a\n", encoding="utf-8")


def _git_setup(env: AxonEnv) -> None:
    (env.workspace / "a.txt").write_text("old\n", encoding="utf-8")
    _git(env, "add", "a.txt")
    _git(env, "commit", "-qm", "first commit subject")
    (env.workspace / "a.txt").write_text("new\n", encoding="utf-8")


def _git_dash(c: Ctx) -> None:
    out = c.out
    assert re.search(r"branch\s+\S+", out), out
    assert re.search(r"unstaged\s+1\b", out), out          # a.txt modified
    assert re.search(r"untracked\s+1\b", out), out         # seed.py from the seeded turn
    assert "first commit subject" in out, out


def _diff(c: Ctx) -> None:
    out = c.out
    assert "-old" in out and "+new" in out and "a.txt" in out, out
    assert "Untracked (1): seed.py" in out, out


def _notify(c: Ctx) -> None:
    assert "Desktop notifications: off" in c.o(0), c.o(0)
    assert "mode: off" in c.o(1) and "never" in c.o(1), c.o(1)
    assert "Desktop notifications: important" in c.o(2), c.o(2)
    assert "only after running 5s" in c.o(3), c.o(3)
    assert "mode: important" in c.o(4) and "running 5s or longer" in c.o(4), c.o(4)
    assert c.settings.get("notifications") == "important" and c.settings.get("notifyAfterSeconds") == 5, c.settings


def _recap(c: Ctx) -> None:
    assert len(c.requests) == 1, c.user_texts()
    assert "create seed.py" in c.requests[0].all_text(), "recap request lacks the conversation"
    assert "Recap" in c.out and "RECAP-TEXT-XYZ" in c.out, c.out


def _reload_setup(env: AxonEnv) -> None:
    d = env.workspace / ".axon" / "commands"
    d.mkdir(parents=True)
    (d / "hello.md").write_text("---\ndescription: say hello\n---\nSay hello to $ARGUMENTS\n", encoding="utf-8")


def _reload(c: Ctx) -> None:
    assert re.search(r"Reloaded: 1 custom command\(s\), \d+ skill\(s\)", c.o(0)), c.o(0)
    # the custom command is runnable and its body reaches the model
    assert any("Say hello to Bob" in t for t in c.user_texts()), c.user_texts()


def _statusbar(c: Ctx) -> None:
    out = c.out
    assert "gpt-4o" in out and "bypass" in out and "128.0k" in out, out
    a = re.search(r"ctx (\d+)%", out)
    b = re.search(r"\((\d+)%\)", out)
    assert a and b, out
    assert a.group(1) == b.group(1), f"status bar shows two different context percentages: {out}"


def _autocompact(c: Ctx) -> None:
    assert re.search(r"Auto-compact at 70% of the context limit \(~[\d.]+k of [\d.]+k tokens\)", c.o(0)), c.o(0)
    assert "Usage" in c.o(1) or "usage" in c.o(1), c.o(1)


def _bug(c: Ctx) -> None:
    files = sorted((c.env.axon_home / "bug-reports").glob("bug-*.md"))
    assert len(files) == 1, files
    text = files[0].read_text(encoding="utf-8")
    assert "the spinner froze" in text and "gpt-4o" in text, text
    assert "sk-e2e-test-key" not in text, "bug report leaks the API key"
    assert str(files[0]) in c.o(0).replace("\n", "") or files[0].name in c.o(0), c.o(0)
    assert "Usage" in c.o(1), c.o(1)
    assert len(list((c.env.axon_home / "bug-reports").glob("bug-*.md"))) == 1


def _env(c: Ctx) -> None:
    out = c.out.replace("\n", "")
    assert "Workspace" in out and c.env.workspace.name in out, out
    assert "Axon home" in out and ".axon" in out, out
    assert "Python" in out and sys.version.split()[0] in out, out


def _import_setup(env: AxonEnv) -> None:
    (env.workspace / ".cursorrules").write_text("Always write docstrings ZQX.\n", encoding="utf-8")


def _import(c: Ctx) -> None:
    rule = c.env.workspace / ".axon" / "rules" / "cursor.md"
    assert "dry run" in c.o(0), c.o(0)
    assert "wrote" in c.o(1) and rule.is_file(), c.o(1)
    assert "Always write docstrings ZQX." in rule.read_text(encoding="utf-8")
    assert c.requests and "Always write docstrings ZQX." in c.requests[-1].system_text, "imported rule not in prompt"
    assert "Nothing to import from gemini" in c.o(3), c.o(3)


def _security_review(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and "security review" in t[0].lower() and "Focus: auth" in t[0], t
    assert "Security review" in c.out and "REVIEW-REPLY" in c.out, c.out


def _simplify(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and "Review the changed code" in t[0] and "Focus: loops" in t[0], t
    assert "Simplify" in c.out, c.out


def _skill_doctor_setup(env: AxonEnv) -> None:
    d = env.workspace / ".axon" / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")


def _skill_doctor(c: Ctx) -> None:
    out = c.out
    assert re.search(r"broken\s+project", out) and "no description" in out, out
    assert re.search(r"api-tester\s+bundled.*ok", out), out


def _star_tag(c: Ctx) -> None:
    assert "★ Starred" in c.o(0), c.o(0)
    assert "☆ Unstarred" in c.o(1), c.o(1)
    assert "Tagged session #wip" in c.o(2), c.o(2)
    assert "#wip" in c.o(3) and "active" in c.o(3), c.o(3)


def _transcript(c: Ctx) -> None:
    out = c.out
    assert "Turn 1" in out and "create seed.py" in out, out
    assert "Write  seed.py" in out and "BASH-OUT-123" in out, out


def _terminal_setup(c: Ctx) -> None:
    assert "detected: iterm2" in c.out and "Shift+Return" in c.out, c.out


def _test_setup(passing: bool) -> Callable[[AxonEnv], None]:
    def setup(env: AxonEnv) -> None:
        (env.workspace / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n", encoding="utf-8")
        (env.workspace / "tests").mkdir()
        body = "assert 1 + 1 == 2" if passing else "assert 1 + 1 == 3, 'BROKEN-MATH'"
        (env.workspace / "tests" / "test_ok.py").write_text(f"def test_ok():\n    {body}\n", encoding="utf-8")
    return setup


def _test_pass(c: Ctx) -> None:
    assert "1 passed" in c.out and "Tests passed" in c.out, c.out


def _test_fail(c: Ctx) -> None:
    assert "1 failed" in c.out and "Tests failed" in c.out, c.out


def _tools(c: Ctx) -> None:
    assert re.search(r"Tools\s+\d+ available · mode bypass", c.o(0)), c.o(0)
    assert re.search(r"Write\s+allow", c.o(0)), c.o(0)
    assert "Parameters" in c.o(1) and re.search(r"\*path\s+string", c.o(1)) and "content" in c.o(1), c.o(1)
    assert re.search(r"Tools\s+\d+ available · mode plan", c.o(2)), c.o(2)
    assert re.search(r"Write\s+deny", c.o(2)) and re.search(r"Read\s+allow", c.o(2)), c.o(2)


def _changes(c: Ctx) -> None:
    assert re.search(r"A seed\.py\s+\+1\s+-0", c.out), c.out
    assert "1 file(s)" in c.out, c.out


def _color(c: Ctx) -> None:
    assert "Prompt colour red" in c.o(0), c.o(0)
    assert c.settings.get("promptColor") == "red", c.settings


def _keys(c: Ctx) -> None:
    out = c.out
    assert "AXON_API_KEY" in out and "OPENAI_API_KEY" in out, out
    assert "sk-e2e-test-key" not in out, "full API key printed"


def _copy(c: Ctx) -> None:
    assert "Copied last answer" in c.o(0), c.o(0)
    # the clipboard file now holds the code block (last step that copied)
    assert "1 code block" in c.o(1), c.o(1)
    assert c.clip.read_text(encoding="utf-8").strip() == "print('seed')"
    assert "No answer #5" in c.o(2), c.o(2)


def _copy_answer(c: Ctx) -> None:
    assert c.clip.read_text(encoding="utf-8") == SEED_ANSWER


def _history(c: Ctx) -> None:
    out = c.out
    assert "Conversation history  6 messages" in out, out
    assert re.search(r"1 user\s+create seed\.py", out) and "[tool_result]" in out and "SEED-ANSWER" in out, out


def _payload(c: Ctx) -> None:
    out = c.out
    for s in ("SYSTEM PROMPT", "TOOL DEFINITIONS", "PREVIOUS CONVERSATION", "tool_call Write", "BASH-OUT-123",
              "Estimated total"):
        assert s in out, (s, out)


def _statusline(c: Ctx) -> None:
    script = c.env.axon_home / "statusline.py"
    assert "none (built-in status bar)" in c.o(0), c.o(0)
    assert script.is_file() and "Status line command set" in c.o(1), c.o(1)
    assert "statusline.py" in c.o(2), c.o(2)
    assert "statusline.py" not in json.dumps(c.settings), c.settings     # /statusline off removed it


def _window(c: Ctx) -> None:
    assert "Sliding window set to 1 turns" in c.o(0), c.o(0)
    assert "Usage" in c.o(1), c.o(1)
    assert re.search(r"Window\s+unlimited", c.o(2)) and re.search(r"Messages now\s+6", c.o(2)), c.o(2)


def _main(c: Ctx) -> None:
    assert "Already in the main session (6 messages)" in c.out, c.out


def _serve(c: Ctx) -> None:
    r = c.results[0]
    assert r.duration < 30, "/serve must print instructions, not block"
    assert "axon serve --port" in r.output and c.env.workspace.name in r.output, r.output


def _setup_cmd(c: Ctx) -> None:
    assert "axon setup needs an interactive terminal" in c.out, c.out
    assert not (c.env.axon_home / "config.toml").exists(), "non-interactive /setup must not write config"


def _sessions(c: Ctx) -> None:
    assert "Create seed.py" in c.out or "seed" in c.out.lower(), c.out
    assert "active" in c.out and "Total:" in c.out, c.out


def _pricing(c: Ctx) -> None:
    assert re.search(r"gpt-4o\s+\$2\.500\s+\$10\.000", c.o(0)), c.o(0)
    assert "claude" not in c.o(0).lower(), c.o(0)


def _faq(c: Ctx) -> None:
    assert "401 Unauthorized" in c.out and "402" not in c.out.split("Topics:")[0], c.out


def _provider(c: Ctx) -> None:
    assert "needs an interactive terminal" in c.out, c.out


def _debug(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and "Help me debug" in t[0] and "create seed.py" in t[0], t
    assert "Debug trace level set to debug" in c.out and "request: model=gpt-4o" in c.out, c.out


def _thinking(c: Ctx) -> None:
    assert "Thinking display OFF" in c.o(0) and "Thinking display ON" in c.o(1), (c.o(0), c.o(1))


def _prompt(c: Ctx) -> None:
    t = c.user_texts()
    assert any("Investigate and fix this bug: login fails." in x for x in t), t      # value with a space kept
    tpl = c.env.workspace / ".axon" / "prompts" / "greet.md"
    assert tpl.is_file() and "Saved template greet" in c.o(1), c.o(1)
    assert any("Say hi to Bob Smith" in x for x in t), t
    assert "Say hi to {who}" in c.o(3), c.o(3)
    assert "No template 'nope'" in c.o(4), c.o(4)


def _retry(c: Ctx) -> None:
    assert "Nothing to retry" in c.out and not c.requests, c.out


def _tips(c: Ctx) -> None:
    assert re.search(r"^\s+1\. \S", c.o(0), re.M) and "tips on" in c.o(0), c.o(0)
    assert "Tips off" in c.o(1) and c.settings.get("tips") is False, (c.o(1), c.settings)
    assert "tips off" in c.o(2), c.o(2)


def _new(c: Ctx) -> None:
    assert "python-lib" in c.o(0) and "react-vite" in c.o(0), c.o(0)
    proj = c.env.workspace / "mylib"
    assert "Created python-lib project in mylib" in c.o(1), c.o(1)
    assert (proj / "pyproject.toml").is_file() and (proj / "src" / "mylib" / "__init__.py").is_file()


def _compact(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 2 and t[0] == "second prompt", t
    assert "Summarize this conversation" in t[1] and "create seed.py" in t[1], t
    assert re.search(r"Compacted 8 → \d+ messages", c.o(1)), c.o(1)
    assert "Summary of the earlier conversation" in c.o(2) and "COMPACT-SUMMARY" in c.o(2), c.o(2)


def _btw(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and "what is seed.py" in t[0], t
    assert "BTW-ANSWER" in c.o(0) and "Side Question" in c.o(0), c.o(0)
    assert "Conversation history  6 messages" in c.o(1), "side question leaked into the conversation"


def _fast(c: Ctx) -> None:
    assert "No small model configured" in c.o(0), c.o(0)
    assert "Fast mode on: fake-model (was gpt-4o" in c.o(1), c.o(1)


def _optimize(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and "make it faster" in t[0], t
    assert "Optimized prompt" in c.out and "OPTIMIZED-TEXT" in c.out, c.out


def _review(c: Ctx) -> None:
    t = c.user_texts()
    assert len(t) == 1 and t[0].startswith("## Skill: /code-review") and "auth module" in t[0], t
    assert "Code review" in c.out, c.out


def _queue(c: Ctx) -> None:
    assert "Queued #1" in c.out, c.out
    assert c.user_texts() == ["add later stuff"], c.user_texts()


def _learn(c: Ctx) -> None:
    mem = list((c.env.workspace / ".axon" / "memory").glob("*.md"))
    assert mem and any("tabs" in p.read_text(encoding="utf-8") for p in mem), mem
    assert "Remembered [project]" in c.o(0), c.o(0)
    assert "tabs" in c.requests[-1].system_text, "learned memory is not in the next prompt"


def _subagents(c: Ctx) -> None:
    assert "SUB-RESULT 42 files" in c.o(0), c.o(0)
    assert "_sub_1" in c.o(1) and "Count the files" in c.o(1) and "SUB-RESULT 42 files" in c.o(1), c.o(1)
    assert "Viewing subagent #1 (count files)" in c.o(1), c.o(1)
    assert "Subagent '9' not found" in c.o(2), c.o(2)


def _skill(name: str, via: str) -> Case:
    def check(c: Ctx) -> None:
        t = c.user_texts()
        assert len(t) == 1, t
        assert t[0].startswith(f"## Skill: /{name}"), t[0][:200]
        assert "ARGUMENTS: zz-args-1" in t[0], t[0][-300:]
        assert f"Skill /{name}" in c.out and "SKILL-DONE" in c.out, c.out
    return Case(f"skill-{name}", [f"{via} zz-args-1"], check, replies=[[{"text": "SKILL-DONE"}]])


CASES: list[Case] = [
    Case("find", ["/find widfac", "/find zzzqqq"], _find, setup=_find_setup),
    Case("git", ["/git"], _git_dash, seed=True, git=True, setup=_git_setup),
    Case("diff", ["/diff"], _diff, seed=True, git=True, setup=_git_setup),
    Case("notify", ["/notify off", "/notify", "/notify important", "/notify after 5", "/notify"], _notify),
    Case("recap", ["/recap"], _recap, seed=True, replies=[[{"text": "RECAP-TEXT-XYZ"}]]),
    Case("reload-plugins", ["/reload-plugins", "/hello Bob"], _reload, setup=_reload_setup),
    Case("statusbar", ["/statusbar"], _statusbar, seed=True),
    Case("autocompact", ["/autocompact 70", "/autocompact banana"], _autocompact),
    Case("bug", ["/bug the spinner froze", "/bug"], _bug, seed=True),
    Case("env", ["/env"], _env),
    Case("import", ["/import cursor --dry-run", "/import cursor", "hello", "/import gemini"], _import,
         setup=_import_setup),
    Case("security-review", ["/security-review auth"], _security_review, seed=True,
         replies=[[{"text": "REVIEW-REPLY"}]]),
    Case("simplify", ["/simplify loops"], _simplify, seed=True),
    Case("skill-doctor", ["/skill-doctor"], _skill_doctor, setup=_skill_doctor_setup),
    Case("star-tag", ["/star", "/star", "/tag wip", "/sessions"], _star_tag, seed=True),
    Case("transcript", ["/transcript"], _transcript, seed=True),
    Case("terminal-setup", ["/terminal-setup"], _terminal_setup, env={"TERM_PROGRAM": "iTerm.app"}),
    Case("test-pass", ["/test"], _test_pass, setup=_test_setup(True), posix_only=True),
    Case("test-fail", ["/test"], _test_fail, setup=_test_setup(False), posix_only=True),
    Case("tools", ["/tools", "/tools Write", ["--mode", "plan", "-p", "/tools"]], _tools),
    Case("changes", ["/changes"], _changes, seed=True),
    Case("color", ["/color red"], _color),
    Case("keys", ["/keys"], _keys),
    Case("copy", ["/copy", "/copy code", "/copy 5"], _copy, seed=True, posix_only=True),
    Case("copy-answer", ["/copy"], _copy_answer, seed=True, posix_only=True),
    Case("history", ["/history"], _history, seed=True),
    Case("payload", ["/payload"], _payload, seed=True),
    Case("statusline", ["/statusline", "/statusline default", "/statusline", "/statusline off"],
         _statusline),
    Case("window", ["/window 1", "/window -3", "/window"], _window, seed=True),
    Case("main", ["/main"], _main, seed=True),
    Case("serve", ["/serve"], _serve),
    Case("setup", ["/setup"], _setup_cmd),
    Case("sessions", ["/sessions"], _sessions, seed=True),
    Case("pricing", ["/pricing gpt-4o"], _pricing),
    Case("faq", ["/faq 401"], _faq),
    Case("provider", ["/provider"], _provider),
    Case("debug", ["/debug"], _debug, seed=True, replies=[[{"text": "DEBUG-REPLY"}]]),
    Case("thinking", ["/thinking off", "/thinking on"], _thinking),
    Case("prompt", ["/prompt bugfix description=login fails", "/prompt save greet Say hi to {who}",
                    "/prompt greet who=Bob Smith", "/prompt show greet", "/prompt nope"], _prompt),
    Case("retry", ["/retry"], _retry, seed=True),
    Case("tips", ["/tips", "/tips off", "/tips"], _tips),
    Case("new", ["/new", "/new python-lib mylib"], _new),
    Case("compact", ["second prompt", "/compact", "/history"], _compact, seed=True,
         replies=[[{"text": "second answer"}], [{"text": "COMPACT-SUMMARY of it all"}]]),
    Case("btw", ["/btw what is seed.py", "/history"], _btw, seed=True, replies=[[{"text": "BTW-ANSWER"}]]),
    Case("fast", ["/fast", ["--settings", json.dumps({"smallModel": "fake-model"}), "-p", "/fast"]], _fast),
    Case("optimize", ["/optimize make it faster"], _optimize, replies=[[{"text": "OPTIMIZED-TEXT"}]]),
    Case("review", ["/review auth module"], _review, seed=True),
    Case("queue", ["/queue add later stuff"], _queue, seed=True),
    Case("learn", ["/learn always use tabs for indentation", "hi again"], _learn),
    Case("subagents", [["-p", "delegate"], ["-p", "--continue", "/subagents 1"], ["-p", "--continue", "/subagents 9"]],
         _subagents, replies=[[{"tool": "Task", "args": {"subagent_type": "general-purpose", "description": "count files",
                                                         "prompt": "Count the files"}}],
                              [{"text": "SUB-RESULT 42 files"}], [{"text": "MAIN-FINAL"}]]),
    *[_skill(n, f"/{n}") for n in BUNDLED_SKILLS],
    *[_skill(n, f"/skill:{n}") for n in SHADOWED_SKILLS],
]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Ctx | BaseException]:
    base = tmp_path_factory.mktemp("cmdbeh")
    out: dict[str, Ctx | BaseException] = {}
    todo = [c for c in CASES if not (c.posix_only and IS_WINDOWS)]

    def work(case: Case) -> tuple[str, Ctx | BaseException]:
        try:
            return case.id, _run_case(case, base / case.id)
        except BaseException as e:  # reported by the test of that case
            return case.id, e

    with ThreadPoolExecutor(max_workers=max(4, min(10, os.cpu_count() or 4))) as ex:
        for cid, res in ex.map(work, todo):
            out[cid] = res
    return out


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_command_behaviour(runs: dict[str, Ctx | BaseException], case: Case) -> None:
    if case.posix_only and IS_WINDOWS:
        pytest.skip("uses POSIX PATH shims")
    res = runs[case.id]
    if isinstance(res, BaseException):
        raise res
    for r in res.results:
        no_crash(r)
        assert r.code == 0, str(r)
    case.check(res)


def test_delete_without_id_never_deletes_and_with_id_does(tmp_path: Path, fake_llm: FakeLLMServer) -> None:
    """Non-interactive `/delete` (no id) used to silently delete the latest session (the picker's
    default); now it prints usage. `/delete <id>` removes that session."""
    env = create_axon_env(tmp_path, fake_llm)
    fake_llm.enqueue([{"text": "hello back"}])
    assert env.run(["-p", "hello"]).code == 0
    files = sorted(env.sessions_dir().glob("*.jsonl"))
    assert len(files) == 1
    r0 = env.run(["-p", "--continue", "/delete"])
    no_crash(r0)
    assert "Usage: /delete" in r0.output, r0.output
    assert files[0].exists(), "/delete without an id deleted the session"
    sid = files[0].stem
    r1 = env.run(["-p", "--continue", f"/delete {sid}"])
    no_crash(r1)
    assert "Deleted session" in r1.output or "to the trash" in r1.output, r1.output
    assert not files[0].exists()
    assert sid not in env.run(["-p", "/sessions"]).output


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_output_shows_full_tool_output_in_repl(axon_env: AxonEnv) -> None:
    """In the REPL, `/output <n>` (the number the truncated timeline points at) prints every line of
    that tool call; `/output list` lists kept outputs; `/output <file>` shows workspace files only."""
    from test_e2e_d_helpers import open_repl
    (axon_env.workspace / "notes.txt").write_text("NOTE-LINE-ONE\nNOTE-LINE-TWO\n", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": "seq 1 300", "description": "count"}}],
                         [{"text": "COUNTED"}])
    s = open_repl(axon_env)
    try:
        out = s.turn("count to 300")
        m = re.search(r"/output (\d+) to view all", out)
        assert m, out[-2000:]
        n = m.group(1)
        full = s.command(f"/output {n}", r"300 lines")
        nums = {ln.split("│")[-1].strip() for ln in full.splitlines() if "│" in ln}
        assert {"1", "150", "300"} <= nums, full[-1500:]
        listing = s.command("/output list", r"Recent tool outputs")
        assert re.search(rf"{n}\s+Bash\s+seq 1 300", listing), listing
        f = s.command("/output notes.txt", r"NOTE-LINE-TWO")
        assert "NOTE-LINE-ONE" in f
        bad = s.command("/output ../../outside.txt", r"Only files inside the workspace")
        assert "Traceback" not in bad
        missing = s.command("/output 9999", r"No tool output #9999")
        assert "Traceback" not in missing
        s.sendline("/exit")
        assert s.wait(10) == 0
    finally:
        s.close()
