"""
Skills: folders containing a SKILL.md (YAML frontmatter + markdown instructions).

Discovery (later sources override earlier ones with the same name):
  bundled  ->  plugins (<plugin>/skills/*)  ->  ~/.axon/skills  ->  ~/.claude/skills
           ->  <ws>/.claude/skills  ->  <ws>/.axon/skills

Frontmatter keys: ``name``, ``description``, ``when_to_use``, ``allowed-tools`` /
``disallowed-tools`` (comma separated or a YAML list), ``argument-hint``, ``arguments``
(named arguments: ``$name`` / ``${name}`` in the body), ``model``, ``effort``, ``paths``
(globs the skill applies to), ``context`` (``fork``), ``agent``, ``disable-model-invocation``
(only the user can run it) and ``user-invocable: false`` (hidden from slash commands).

Only the name + description of every skill goes into the system prompt
(:func:`skills_prompt_section`); the body is loaded on demand with
:func:`skill_content` / :meth:`SkillManager.execute_skill`.

Body features:
  ``$ARGUMENTS`` / ``$1`` ``$2`` ...   argument substitution
  ``!`cmd```                          shell output injection - executed ONLY when the
                                      caller passes an ``allow_shell`` callback that
                                      approves the command (permission engine).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from axon.storage import axon_home as _axon_home

ShellGate = Callable[[str], bool]
_SHELL_RE = re.compile(r"!`([^`\n]+)`")


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    scope: str  # "bundled", "plugin", "personal", "project"
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str = ""
    model: str | None = None
    when_to_use: str = ""
    disallowed_tools: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)
    effort: str | None = None
    paths: list[str] = field(default_factory=list)
    context: str = ""
    agent: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True

    @property
    def base_dir(self) -> Path | None:
        return None if self.scope == "bundled" else self.path.parent


# ─── Frontmatter ────────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Split ``---`` frontmatter from the body. Supports scalars, quoted strings,
    block scalars (``|`` ``>`` ``|-`` ``>-``) and ``- item`` lists."""
    text = text.removeprefix("﻿")  # files saved "UTF-8 with BOM" (common on Windows)
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_lines = lines[1:end]
    body = "\n".join(lines[end + 1:]).strip()

    result: dict[str, str | list[str]] = {}
    key: str | None = None
    buf: list[str] = []
    items: list[str] = []
    block = False

    def flush() -> None:
        if key is None:
            return
        if items:
            result[key] = [_unquote(x) for x in items]
        else:
            joined = ("\n" if block == "|" else " ").join(buf).strip()
            result[key] = _unquote(joined)

    for line in fm_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace() and ":" in line and not line.lstrip().startswith("- "):
            flush()
            k, _, v = line.partition(":")
            key, buf, items, block = k.strip(), [], [], False
            v = v.strip()
            if v in ("|", "|-", ">", ">-"):
                block = "|" if v.startswith("|") else True  # type: ignore[assignment]
            elif v.startswith("[") and v.endswith("]"):
                items = [x.strip() for x in _split_top(v[1:-1]) if x.strip()]
            elif v:
                buf.append(v)
        elif key is not None:
            s = line.strip()
            if s.startswith("- ") and not block:
                items.append(s[2:].strip())
            else:
                buf.append(s)
    flush()
    return result, body


def _split_top(s: str) -> list[str]:
    """Split on commas that are not inside parentheses or quotes."""
    out, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def as_list(v: str | list[str] | None) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [x.strip() for x in v if x.strip()]
    return [x.strip() for x in _split_top(v) if x.strip()]


# ─── Argument substitution & shell injection (shared with custom commands) ──

def substitute_arguments(body: str, arguments: str, names: list[str] | None = None) -> str:
    """Replace $ARGUMENTS, positional $1..$9 and named ``$name`` / ``${name}`` (``names`` in
    order map onto the positional words; the last name takes the rest). If the body has no
    placeholder and arguments were given, append them so they are never silently dropped."""
    try:
        positional = shlex.split(arguments) if arguments else []
    except ValueError:
        positional = arguments.split()
    named: dict[str, str] = {}
    valid = [n for n in (names or []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and n != "ARGUMENTS"]
    for i, name in enumerate(names or []):
        if name not in valid:
            continue
        last = i == len(names or []) - 1
        named[name] = (" ".join(positional[i:]) if last else (positional[i] if i < len(positional) else "")) if positional else ""
    # One pass over the body: text that was substituted in (e.g. an argument "costs $5") is never
    # scanned again for placeholders.
    alts = [r"\$ARGUMENTS", r"\$[1-9]\b"] + [r"\$\{" + n + r"\}|\$" + n + r"\b" for n in sorted(named, key=len, reverse=True)]
    rx = re.compile("|".join(f"(?:{a})" for a in alts))
    used = False

    def repl(m: re.Match[str]) -> str:
        nonlocal used
        used = True
        tok = m.group(0)
        if tok == "$ARGUMENTS":
            return arguments
        if len(tok) == 2 and tok[1].isdigit():
            k = int(tok[1])
            return positional[k - 1] if k <= len(positional) else ""
        return named.get(tok.strip("${}"), "")

    out = rx.sub(repl, body)
    if arguments and not used:
        out = f"{out.rstrip()}\n\nARGUMENTS: {arguments}"
    return out


def shell_argv(command: str) -> list[str]:
    """argv that runs a command string in the platform shell."""
    try:
        from axon.platform import default_shell  # type: ignore[import-not-found]
        prefix = list(default_shell())
        if prefix:
            return prefix + [command]
    except Exception:
        pass
    if sys.platform == "win32":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-Command", command]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    sh = shutil.which("bash") or shutil.which("sh") or "sh"
    return [sh, "-c", command]


def run_shell_capture(command: str, cwd: Path, timeout: float = 15.0) -> tuple[int, str]:
    """Run a command in the platform shell; return (exit code, combined output)."""
    try:
        proc = subprocess.run(
            shell_argv(command), cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr and proc.stderr.strip() else "")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"[timed out after {timeout:.0f}s]"
    except OSError as e:
        return 127, f"[failed to start shell: {e}]"


def expand_shell_injections(body: str, cwd: Path, allow_shell: ShellGate | None) -> str:
    """Replace !`cmd` with its output when ``allow_shell(cmd)`` approves, else a notice."""
    def repl(m: re.Match[str]) -> str:
        cmd = m.group(1).strip()
        if allow_shell is None or not allow_shell(cmd):
            return f"[shell context `{cmd}` not run: not permitted]"
        code, out = run_shell_capture(cmd, cwd)
        suffix = "" if code == 0 else f"\n[exit code {code}]"
        return f"\n```\n$ {cmd}\n{out[:20000]}{suffix}\n```\n"
    return _SHELL_RE.sub(repl, body)


# ─── Manager ────────────────────────────────────────────────────────────────

def _plugin_skill_roots(workspace: Path) -> list[Path]:
    try:
        from axon.commands.plugins import plugin_dirs
        return [d / "skills" for d in plugin_dirs(workspace) if (d / "skills").is_dir()]
    except Exception:
        return []


class SkillManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.skills: dict[str, Skill] = {}
        self.discover()

    def skill_roots(self) -> list[tuple[Path, str]]:
        home = Path.home()
        roots: list[tuple[Path, str]] = [(r, "plugin") for r in _plugin_skill_roots(self.workspace)]
        roots += [
            (_axon_home() / "skills", "personal"),
            (home / ".claude" / "skills", "personal"),
            (self.workspace / ".claude" / "skills", "project"),
            (self.workspace / ".axon" / "skills", "project"),
        ]
        return roots

    def discover(self) -> dict[str, Skill]:
        self.skills.clear()
        self._register_bundled_skills()
        for root, scope in self.skill_roots():
            if not root.is_dir():
                continue
            for p in sorted(root.glob("*/SKILL.md")):
                s = self._load_skill(p, scope)
                if s:
                    self.skills[s.name] = s
        return self.skills

    def _register_bundled_skills(self) -> None:
        """Built-in prompt skills (overridable by a user skill of the same name)."""
        bundled = [
            Skill(
                name="code-review",
                description="Comprehensive multi-file code review analyzing logic errors, performance, and security.",
                instructions="Conduct a thorough code review. Inspect modified files, look for edge case failures, unhandled exceptions, type safety, and adherence to project architecture conventions.",
                path=Path("bundled://code-review"),
                scope="bundled",
            ),
            Skill(
                name="debug",
                description="Systematic root-cause debugging for errors, test failures, or crashes.",
                instructions="Debug the reported issue step by step: reproduce the error, inspect tracebacks, locate the failure root cause, and propose a minimal verified fix.",
                path=Path("bundled://debug"),
                scope="bundled",
            ),
            Skill(
                name="verify",
                description="Build and run tests to verify that changes work without regressions.",
                instructions="Run project test commands (e.g. pytest or npm test), check exit codes, and report exact validation results.",
                path=Path("bundled://verify"),
                scope="bundled",
            ),
            Skill(
                name="subagent-fanout",
                description="Deconstructs complex research/refactor tasks into parallel subagents and synthesizes findings.",
                instructions="Decompose the problem into 2-5 independent subtasks. Spawn isolated subagents using the `Task` tool for each subtask in parallel, then synthesize their conclusions into a cohesive executive report.",
                path=Path("bundled://subagent-fanout"),
                scope="bundled",
            ),
            Skill(
                name="refactor",
                description="Systematic architecture and code refactoring with design patterns and zero regressions.",
                instructions="Analyze code smells and technical debt. Plan semantic refactoring with clean abstractions, modularity, and strict typing while preserving all existing behaviors.",
                path=Path("bundled://refactor"),
                scope="bundled",
            ),
            Skill(
                name="security-audit",
                description="Static vulnerability audit for command injection, secret leakage, and insecure deserialization.",
                instructions="Scan the codebase for security vulnerabilities: look for shell injection in subprocess calls, unsanitized inputs, exposed API keys/tokens, and path traversal vectors.",
                path=Path("bundled://security-audit"),
                scope="bundled",
            ),
            Skill(
                name="test-gen",
                description="Automated unit and integration test generation with high branch coverage.",
                instructions="Analyze target functions, classes, and edge cases. Author clean pytest or unit tests with mock fixtures, parameterization, and boundary condition validation.",
                path=Path("bundled://test-gen"),
                scope="bundled",
            ),
            Skill(
                name="optimize",
                description="Algorithmic profiling, memory leak detection, and performance optimization.",
                instructions="Identify computational bottlenecks, O(N^2) complexity, unnecessary I/O or memory allocations, and propose cache or data structure optimizations.",
                path=Path("bundled://optimize"),
                scope="bundled",
            ),
            Skill(
                name="deep-research",
                description="Multi-round iterative deep research with sub-query planning, source crawling, and matrix synthesis.",
                instructions="Deconstruct the complex topic into sub-topics. Use `WebSearch` to find sources and `WebFetch` to read them for each angle. Build a comprehensive report with executive summary, comparative analysis tables, and verified citations.",
                path=Path("bundled://deep-research"),
                scope="bundled",
            ),
            Skill(
                name="table-search",
                description="High-speed structured table scanning and matrix extraction across Markdown, CSV, and JSON data.",
                instructions="Use `Grep` and `CodeSearch` to locate Markdown tables, CSV datasets and JSON records matching the keywords, then `Read` them to filter and extract the matching rows and columns.",
                path=Path("bundled://table-search"),
                scope="bundled",
            ),
            Skill(
                name="docgen",
                description="Generates technical documentation, API specifications, and architecture walkthroughs.",
                instructions="Document modules, classes, and exported APIs. Generate markdown documentation with usage examples and clear structural overview.",
                path=Path("bundled://docgen"),
                scope="bundled",
            ),
            Skill(
                name="git-workflow",
                description="Conventional commits, automated changelog entries, and branch management.",
                instructions="Inspect recent changes via `Bash` (`git status`, `git diff`). Draft conventional commit messages (feat:, fix:, refactor:, test:) and update CHANGELOG.md.",
                path=Path("bundled://git-workflow"),
                scope="bundled",
            ),
            Skill(
                name="skill-creator",
                description="Interactive skill scaffolding wizard for creating custom project and personal skills.",
                instructions="Guide the user through creating a new custom Axon skill. Create the skill at `.axon/skills/<name>/SKILL.md` (for project scope) or `~/.axon/skills/<name>/SKILL.md` (for personal global scope). Include YAML frontmatter with 'name:' and 'description:', followed by clear markdown workflow instructions. Never create or reference `.codex`, `.cursor`, or `openai.yaml`.",
                path=Path("bundled://skill-creator"),
                scope="bundled",
            ),
            Skill(
                name="api-tester",
                description="Automated HTTP/REST endpoint probing, payload validation, and latency benchmarking.",
                instructions="Analyze API routes and schemas. Use `Bash` (curl or the project's HTTP client) to send GET/POST/PUT/DELETE requests, and `WebFetch` for public endpoints; validate response codes, JSON schemas and status headers.",
                path=Path("bundled://api-tester"),
                scope="bundled",
            ),
            Skill(
                name="docker",
                description="Dockerfile, docker-compose, and container lifecycle analysis and optimization.",
                instructions="Analyze Dockerfiles and docker-compose.yml configurations. Check for multi-stage builds, minimal base images, secure non-root users, and volume caching.",
                path=Path("bundled://docker"),
                scope="bundled",
            ),
            Skill(
                name="db-migration",
                description="Database schema migration generator, index analyzer, and SQL query optimizer.",
                instructions="Inspect database schema definitions and SQL queries. Check for missing indexes, N+1 query bottlenecks, safe column additions, and rollback strategies.",
                path=Path("bundled://db-migration"),
                scope="bundled",
            ),
            Skill(
                name="frontend-ui",
                description="Modern responsive UI layout builder with semantic HTML, CSS styling, and accessibility.",
                instructions="Design and construct clean frontend components with responsive flex/grid layouts, theme color palettes, accessible ARIA attributes, and micro-interactions.",
                path=Path("bundled://frontend-ui"),
                scope="bundled",
            ),
            Skill(
                name="benchmark",
                description="Execution speed profiling, memory footprint analysis, and stress testing.",
                instructions="Run benchmark scripts with timing loops. Measure execution throughput, memory allocations, CPU utilization, and pinpoint performance regressions.",
                path=Path("bundled://benchmark"),
                scope="bundled",
            ),
        ]
        for b in bundled:
            self.skills[b.name] = b

    def _load_skill(self, path: Path, scope: str) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fm, body = parse_frontmatter(content)
        raw_name = fm.get("name") if isinstance(fm.get("name"), str) else ""
        name = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(raw_name or path.parent.name)).strip("-") or path.parent.name
        desc = fm.get("description", "")
        model = fm.get("model")

        def flag(key: str, default: bool) -> bool:
            v = fm.get(key, fm.get(key.replace("-", "_")))
            return default if v is None else str(v).strip().lower() in ("true", "yes", "1", "on")

        def text(key: str) -> str:
            v = fm.get(key, fm.get(key.replace("_", "-"), ""))
            return v if isinstance(v, str) else " ".join(v)

        effort = text("effort")
        agent_name = text("agent")
        return Skill(
            name=name,
            description=desc if isinstance(desc, str) else " ".join(desc),
            instructions=body,
            path=path,
            scope=scope,
            allowed_tools=as_list(fm.get("allowed-tools") or fm.get("allowed_tools")),
            argument_hint=_raw_hint(fm.get("argument-hint")),
            model=model if isinstance(model, str) and model and model != "inherit" else None,
            when_to_use=text("when_to_use"),
            disallowed_tools=as_list(fm.get("disallowed-tools") or fm.get("disallowed_tools")),
            arguments=as_list(fm.get("arguments")),
            effort=effort or None,
            paths=as_list(fm.get("paths")),
            context=text("context").lower(),
            agent=agent_name or None,
            disable_model_invocation=flag("disable-model-invocation", False),
            user_invocable=flag("user-invocable", True),
        )

    @staticmethod
    def _parse_frontmatter(fm_text: str) -> dict[str, str]:
        """Backward-compatible helper: parse a frontmatter block (without --- fences)."""
        data, _ = parse_frontmatter(f"---\n{fm_text}\n---\n")
        return {k: (v if isinstance(v, str) else ", ".join(v)) for k, v in data.items()}

    def get(self, name: str) -> Skill | None:
        n = name.lstrip("/")
        if n in self.skills:
            return self.skills[n]
        low = n.lower()
        for k, v in self.skills.items():
            if k.lower() == low:
                return v
        return None

    def render(self, skill: Skill, arguments: str = "", allow_shell: ShellGate | None = None) -> str:
        body = substitute_arguments(skill.instructions, arguments, getattr(skill, "arguments", None))
        cwd = self.workspace
        body = expand_shell_injections(body, cwd, allow_shell)
        header = f"## Skill: /{skill.name}"
        if skill.base_dir is not None:
            header += f"\nBase directory for this skill: {skill.base_dir}"
        return f"{header}\n\n{body}"

    def execute_skill(self, name: str, arguments: str = "", allow_shell: ShellGate | None = None) -> str:
        """Resolve a skill and return its ready-to-send instructions."""
        skill = self.get(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found.")
        return self.render(skill, arguments, allow_shell)

    def auto_match(self, user_input: str) -> list[Skill]:
        """Suggest skills whose intent keywords appear in the prompt."""
        if not user_input or not user_input.strip():
            return []
        text = user_input.lower()
        intent_keywords = {
            "code-review": ("review", "audit code", "code review", "pr review", "inspect changes"),
            "debug": ("debug", "traceback", "fix error", "fix bug", "exception", "crash", "segfault"),
            "test-gen": ("write test", "generate test", "unit test", "test suite", "coverage"),
            "verify": ("verify", "run test", "check test", "pytest", "npm test", "cargo test"),
            "optimize": ("optimize", "profiling", "speed up", "performance", "slow", "bottleneck"),
            "deep-research": ("deep research", "investigate topic", "architecture benchmark"),
            "refactor": ("refactor", "clean up code", "redesign architecture", "technical debt"),
            "security-audit": ("security audit", "vulnerability", "sql injection", "xss", "cve"),
        }
        matched: list[Skill] = []
        for skill_name, keywords in intent_keywords.items():
            if skill_name in self.skills and any(kw in text for kw in keywords):
                matched.append(self.skills[skill_name])
        for name, skill in self.skills.items():
            if (f"/{name}" in text or f"skill {name}" in text) and skill not in matched:
                matched.append(skill)
        return matched


# ─── Agent-facing helpers ───────────────────────────────────────────────────

_managers: dict[str, SkillManager] = {}


def _manager_for(workspace: Path | None) -> SkillManager:
    ws = Path(workspace or Path.cwd()).resolve()
    key = str(ws)
    mgr = _managers.get(key)
    if mgr is None:
        mgr = SkillManager(ws)
        _managers[key] = mgr
    return mgr


def skills_prompt_section(skills: list[Skill]) -> str:
    """System-prompt block listing skills (name + description only)."""
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "Skills are task playbooks. When one is relevant, load its full instructions with the "
        "Skill tool (or the user can run /<name>) before acting.",
    ]
    for s in sorted(skills, key=lambda x: x.name):
        if getattr(s, "disable_model_invocation", False):
            continue  # user-only skills are not advertised to the model
        desc = " ".join((s.description or "").split())[:240]
        when = " ".join((getattr(s, "when_to_use", "") or "").split())[:160]
        if when:
            desc = f"{desc} Use when: {when}" if desc else f"Use when: {when}"
        paths = getattr(s, "paths", None) or []
        if paths:
            desc += f" (applies to {', '.join(paths[:4])})"
        lines.append(f"- {s.name}: {desc}" if desc else f"- {s.name}")
    return "\n".join(lines)


def skill_content(name: str, workspace: Path | None = None, arguments: str = "") -> str:
    """Full instructions of a skill for the agent's Skill tool. Shell injections are
    not executed here (the agent can run commands itself, through permissions)."""
    mgr = _manager_for(workspace)
    skill = mgr.get(name)
    if skill is None:
        mgr.discover()
        skill = mgr.get(name)
    if skill is None:
        available = ", ".join(sorted(k for k, v in mgr.skills.items() if not getattr(v, "disable_model_invocation", False))) or "none"
        return f"Unknown skill '{name}'. Available skills: {available}"
    if getattr(skill, "disable_model_invocation", False):
        return f"Skill '{skill.name}' can only be run by the user (/{skill.name}); it is not available to the model."
    return mgr.render(skill, arguments, allow_shell=None)


def _raw_hint(value: object) -> str:
    """``argument-hint: [env]`` is a hint string, not a YAML list: show it verbatim as "[env]"."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value or "")
