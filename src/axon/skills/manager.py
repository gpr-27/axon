"""
Skills: folders containing a SKILL.md (YAML frontmatter + markdown instructions).

Discovery (later sources override earlier ones with the same name):
  bundled  ->  <ws>/.claude/skills, <ws>/.axon/skills (every directory from the repository root down
  to the workspace, nearest wins)  ->  ~/.claude/skills, ~/.axon/skills (personal wins over project).
  Plugin skills (``<plugin>/skills/*`` and the manifest ``skills`` paths) are namespaced
  ``<plugin>:<skill>`` so they never clash. Skills in sub-directories below the workspace load when a
  file there is touched (:meth:`SkillManager.load_nested_for`); ``add_directory`` loads an extra
  directory's ``.claude/skills`` (prefixed with the directory name when the name clashes).
  The set is re-scanned automatically when a SKILL.md appears, changes or disappears.

Frontmatter keys: ``name``, ``description``, ``when_to_use``, ``allowed-tools`` /
``disallowed-tools`` (space/comma separated or a YAML list), ``argument-hint``, ``arguments``
(named arguments: ``$name`` / ``${name}`` in the body), ``model``, ``effort``, ``paths``
(globs the skill applies to), ``context`` (``fork``), ``agent``, ``background``, ``hooks``,
``shell`` (``bash`` | ``powershell``), ``disable-model-invocation`` (only the user can run it),
``user-invocable: false`` (hidden from slash commands), plus the passive ``metadata``,
``license`` and ``compatibility``. Booleans accept true/false/yes/no/on/off/1/0.

Settings (``settings.json``): ``skillOverrides`` (``on`` | ``name-only`` | ``user-invocable-only`` |
``off`` per skill name), ``disableBundledSkills``, ``disableClaudeSkills`` (do not read
``~/.claude/skills``), ``disableSkillShellExecution`` and
``skillListingMaxDescChars``.

Only the name + description of every skill goes into the system prompt
(:func:`skills_prompt_section`); the body is loaded on demand with
:func:`skill_content` / :meth:`SkillManager.execute_skill`.

Body features (see :func:`render_body`):
  ``$ARGUMENTS`` ``$ARGUMENTS[N]`` ``$0`` ``$1`` ...  argument substitution (0-based; ``\\$1`` is literal)
  ``$name`` / ``${name}``                           named arguments from ``arguments:``
  ``${CLAUDE_SKILL_DIR}`` ``${CLAUDE_PROJECT_DIR}`` ``${CLAUDE_SESSION_ID}`` ``${CLAUDE_EFFORT}``
  ``${CLAUDE_PLUGIN_ROOT}`` ``${CLAUDE_PLUGIN_DATA}`` (``AXON_`` spellings work too)
  ``!`cmd``` and fenced ```` ```! ```` blocks         shell output injection - executed ONLY when the
                                                    caller passes an ``allow_shell`` callback that
                                                    approves the command (permission engine). A command
                                                    that fails or is refused aborts the invocation.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from axon.storage import axon_home as _axon_home

ShellGate = Callable[[str], bool]
_SHELL_RE = re.compile(r"(?m)(?<![^\s])!`([^`\n]+)`|^```![ \t]*\n(.*?)\n```[ \t]*$", re.S)
SHELL_TIMEOUT = 120.0
DEFAULT_DESC_CHARS = 1536
_BOOL_TRUE = ("true", "yes", "1", "on")
_BOOL_FALSE = ("false", "no", "0", "off")


class SkillShellError(RuntimeError):
    """An injected ``!`cmd``` failed, was refused or is disabled: the whole invocation is aborted."""


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
    background: bool = True
    hooks: dict[str, Any] = field(default_factory=dict)
    shell: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    license: str = ""
    compatibility: str = ""
    plugin: str = ""                   # owning plugin name ("" for non-plugin skills)
    plugin_root: Path | None = None
    name_only: bool = False            # skillOverrides "name-only": listed without a description
    short_name: str = ""               # name without the plugin / directory prefix
    triggers: list[str] = field(default_factory=list)   # phrases that make the skill relevant to a prompt

    @property
    def base_dir(self) -> Path | None:
        return None if self.scope == "bundled" else self.path.parent


# ─── Frontmatter ────────────────────────────────────────────────────────────

_RAW_BLOCK_KEYS = {"hooks", "metadata"}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` frontmatter from the body. Supports scalars, quoted strings,
    block scalars (``|`` ``>`` ``|-`` ``>-``), ``- item`` lists and (for ``hooks`` /
    ``metadata``) nested maps, returned as parsed dicts."""
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

    result: dict[str, Any] = {}
    key: str | None = None
    buf: list[str] = []
    items: list[str] = []
    raw: list[str] = []
    block = False

    def flush() -> None:
        if key is None:
            return
        if key in _RAW_BLOCK_KEYS and raw:
            parsed = parse_yaml_block(raw)
            if parsed is not None:
                result[key] = parsed
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
            key, buf, items, raw, block = k.strip(), [], [], [], False
            v = v.strip()
            if v in ("|", "|-", ">", ">-"):
                block = "|" if v.startswith("|") else True  # type: ignore[assignment]
            elif v.startswith("[") and v.endswith("]"):
                items = [x.strip() for x in _split_top(v[1:-1]) if x.strip()]
            elif v:
                buf.append(v)
        elif key is not None:
            raw.append(line)
            s = line.strip()
            if s.startswith("- ") and not block:
                items.append(s[2:].strip())
            else:
                buf.append(s)
    flush()
    return result, body


def _yaml_scalar(v: str) -> Any:
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_yaml_scalar(x) for x in _split_top(v[1:-1]) if x.strip()]
    u = _unquote(v)
    if u is not v and (v[:1] in "\"'"):
        return u
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_yaml_block(lines: list[str]) -> Any:
    """Parse the indented block under a frontmatter key (nested maps, ``- `` lists, scalars).
    A deliberately small YAML subset: enough for ``hooks:`` and ``metadata:``."""
    toks = [(len(ln) - len(ln.lstrip(" ")), ln.strip()) for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]
    if not toks:
        return None
    pos = 0

    def parse(indent: int) -> Any:
        nonlocal pos
        if pos >= len(toks):
            return None
        ind, text = toks[pos]
        if text == "-" or text.startswith("- "):
            out_list: list[Any] = []
            while pos < len(toks) and toks[pos][0] == ind and (toks[pos][1] == "-" or toks[pos][1].startswith("- ")):
                item = toks[pos][1][1:].strip()
                if not item:
                    pos += 1
                    out_list.append(parse(toks[pos][0]) if pos < len(toks) and toks[pos][0] > ind else None)
                elif re.match(r"^[A-Za-z_][\w./-]*\s*:(\s|$)", item):
                    toks[pos] = (ind + 2, item)
                    out_list.append(parse(ind + 2))
                else:
                    out_list.append(_yaml_scalar(item))
                    pos += 1
            return out_list
        out: dict[str, Any] = {}
        while pos < len(toks) and toks[pos][0] == ind and not (toks[pos][1] == "-" or toks[pos][1].startswith("- ")):
            k, _, v = toks[pos][1].partition(":")
            k, v = _unquote(k.strip()), v.strip()
            pos += 1
            if v:
                out[k] = _yaml_scalar(v)
            elif pos < len(toks) and (toks[pos][0] > ind or (toks[pos][0] == ind and toks[pos][1].startswith("- "))):
                out[k] = parse(toks[pos][0])
            else:
                out[k] = None
        return out

    try:
        return parse(toks[0][0])
    except (IndexError, ValueError):
        return None


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


def _split_words(s: str) -> list[str]:
    """Split on commas and on whitespace outside parentheses/quotes (``Bash(git add *) Read``)."""
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
        elif (ch == "," or ch.isspace()) and depth == 0:
            if cur:
                out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def as_list(v: str | list[str] | None) -> list[str]:
    """A list-valued field written as a YAML list, ``a, b`` or ``a b`` (spaces inside parentheses
    belong to the item: ``Bash(git add *) Read`` is two entries)."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if not isinstance(v, str):
        return []
    return [x.strip() for x in _split_words(v) if x.strip()]


def as_bool(v: object, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    low = str(v).strip().lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    return default


def name_refused(name: str) -> bool:
    """True for names Claude Code refuses: control / zero-width / private-use / bidi characters,
    ``<`` ``>``, or blanks other than one plain space between words."""
    if not name or name != name.strip() or "  " in name or "<" in name or ">" in name:
        return True
    for ch in name:
        if ch == " ":
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zs", "Zl", "Zp") or unicodedata.bidirectional(ch) in (
                "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "LRM", "RLM", "ALM"):
            return True
    return False


# ─── Argument substitution, variables & shell injection (shared with custom commands) ──

class _ArgSubst:
    """One-pass ``$ARGUMENTS`` / ``$ARGUMENTS[N]`` / ``$N`` / named-argument substitution.

    Substituted text is never scanned again; ``\\$1`` (a single backslash) is a literal; an indexed
    placeholder with no argument stays as written and does not count as having received one."""

    def __init__(self, arguments: str, names: list[str] | None = None) -> None:
        self.arguments = arguments
        try:
            self.positional = shlex.split(arguments) if arguments else []
        except ValueError:
            self.positional = arguments.split()
        self.named: dict[str, str] = {}
        valid = [n for n in (names or []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and n != "ARGUMENTS"]
        all_names = names or []
        for i, name in enumerate(all_names):
            if name not in valid:
                continue
            last = i == len(all_names) - 1
            self.named[name] = (" ".join(self.positional[i:]) if last else (self.positional[i] if i < len(self.positional) else "")) if self.positional else ""
        alts = [r"\$ARGUMENTS\[\d+\]", r"\$ARGUMENTS", r"\$\d+\b"]
        alts += [r"\$\{" + re.escape(n) + r"\}|\$" + re.escape(n) + r"\b" for n in sorted(self.named, key=len, reverse=True)]
        self._rx = re.compile(r"(\\*)(" + "|".join(f"(?:{a})" for a in alts) + ")")
        self.used = False

    def apply(self, text: str) -> str:
        def repl(m: re.Match[str]) -> str:
            bs, tok = m.group(1), m.group(2)
            if len(bs) == 1:                     # \$1 -> literal $1
                return tok
            if tok == "$ARGUMENTS":
                val: str | None = self.arguments
            elif tok.startswith("$ARGUMENTS["):
                k = int(tok[len("$ARGUMENTS["):-1])
                val = self.positional[k] if k < len(self.positional) else None
            elif tok[1:].isdigit():
                k = int(tok[1:])
                val = self.positional[k] if k < len(self.positional) else None
            else:
                val = self.named.get(tok.strip("${}"), "")
            if val is None:
                return m.group(0)
            self.used = True
            return bs + val
        return self._rx.sub(repl, text)


def substitute_arguments(body: str, arguments: str, names: list[str] | None = None) -> str:
    """Replace ``$ARGUMENTS``, ``$ARGUMENTS[N]``, positional ``$0``/``$1``... (0-based) and named
    ``$name`` / ``${name}`` (``names`` in order map onto the positional words; the last name takes
    the rest). If nothing received the arguments, append them so they are never silently dropped."""
    sub = _ArgSubst(arguments, names)
    out = sub.apply(body)
    if arguments and not sub.used:
        out = f"{out.rstrip()}\n\nARGUMENTS: {arguments}"
    return out


_VAR_RE = re.compile(r"\$\{(?:CLAUDE|AXON)_([A-Z_]+)\}")
_USER_CFG_RE = re.compile(r"\$\{user_config\.([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_variables(text: str, variables: dict[str, str] | None, user_config: dict[str, str] | None = None) -> str:
    """Replace ``${CLAUDE_SKILL_DIR}`` and friends (``AXON_`` spellings too) and ``${user_config.KEY}``.
    Unknown variables are left untouched."""
    if variables:
        text = _VAR_RE.sub(lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0), text)
    if user_config:
        text = _USER_CFG_RE.sub(lambda m: str(user_config[m.group(1)]) if m.group(1) in user_config else m.group(0), text)
    return text


def shell_argv(command: str, shell: str | None = None) -> list[str]:
    """argv that runs a command string in the platform shell (``shell`` = ``bash`` | ``powershell``)."""
    want = (shell or "").strip().lower()
    if want in ("powershell", "pwsh"):
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if not ps:
            raise SkillShellError("`shell: powershell` was requested but PowerShell was not found")
        return [ps, "-NoProfile", "-NonInteractive", "-Command", command]
    if want == "bash":
        sh = shutil.which("bash")
        if not sh:
            raise SkillShellError("This skill requires bash (`shell: bash` in frontmatter) but bash was not found")
        return [sh, "-c", command]
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


def run_shell_capture(command: str, cwd: Path, timeout: float = SHELL_TIMEOUT, shell: str | None = None) -> tuple[int, str]:
    """Run a command in the platform shell; return (exit code, output with stderr merged in)."""
    try:
        proc = subprocess.run(
            shell_argv(command, shell), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"[timed out after {timeout:.0f}s]"
    except OSError as e:
        return 127, f"[failed to start shell: {e}]"


_EXIT1_OK = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "diff", "cmp", "test", "[", "find"}


def _exit1_is_result(command: str) -> bool:
    """Exit code 1 from a search / comparison command is a normal result, not a failure."""
    last = re.split(r"\|\||&&|\||;", command)[-1].strip()
    try:
        words = shlex.split(last)
    except ValueError:
        words = last.split()
    if not words:
        return False
    first = Path(words[0]).name
    return first in _EXIT1_OK or (first == "git" and "diff" in words[1:3])


def _has_control_chars(s: str) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in "\t\n\r" for c in s)


def _shell_gate_fail(cmd: str) -> SkillShellError:
    return SkillShellError(f'Shell command permission check failed for pattern "{cmd}"')


def _run_injection(cmd: str, fenced: bool, cwd: Path, allow_shell: ShellGate | None,
                   shell: str | None, disabled: bool) -> str:
    if disabled:
        return "[shell command execution disabled by policy]"
    if allow_shell is None:
        return f"[shell context `{cmd.strip()[:200]}` not run: not permitted]"
    if not allow_shell(cmd.strip()):
        raise _shell_gate_fail(cmd.strip())
    code, out = run_shell_capture(cmd.strip(), cwd, shell=shell)
    if code != 0 and not (code == 1 and _exit1_is_result(cmd)):
        raise SkillShellError(f'Shell command failed for pattern "{cmd.strip()}" (exit code {code}): {out[:2000]}')
    return out[:20000]


def expand_shell_injections(body: str, cwd: Path, allow_shell: ShellGate | None, *,
                            shell: str | None = None, disabled: bool = False,
                            transform: Callable[[str], str] | None = None) -> str:
    """Replace ``!`cmd``` (at line start / after whitespace) and fenced ```` ```! ```` blocks with the
    command output. The output is plain text and is never scanned again. With no ``allow_shell`` a
    notice replaces the command; a refused or failing command raises :class:`SkillShellError`.
    ``transform`` rewrites each command first (argument / variable substitution)."""
    if _has_control_chars("".join(m.group(1) or m.group(2) or "" for m in _SHELL_RE.finditer(body))):
        raise SkillShellError("No shell command was run: a `!` command contains a raw control character")

    def repl(m: re.Match[str]) -> str:
        fenced = m.group(1) is None
        cmd = m.group(2) if fenced else m.group(1)
        if transform is not None:
            cmd = transform(cmd)
        return _run_injection(cmd, fenced, cwd, allow_shell, shell, disabled)
    return _SHELL_RE.sub(repl, body)


def render_body(body: str, arguments: str = "", *, names: list[str] | None = None,
                variables: dict[str, str] | None = None, user_config: dict[str, str] | None = None,
                cwd: Path | None = None, allow_shell: ShellGate | None = None,
                shell: str | None = None, shell_disabled: bool = False) -> str:
    """The full body pipeline: shell commands are lifted out first (so text typed as an argument is
    never executed), arguments and variables are substituted into the rest and into each command, then
    the command output is inserted verbatim."""
    sub = _ArgSubst(arguments, names)
    slots: list[tuple[bool, str]] = []

    def lift(m: re.Match[str]) -> str:
        fenced = m.group(1) is None
        slots.append((fenced, m.group(2) if fenced else m.group(1)))
        return f"\x00SHELL{len(slots) - 1}\x00"

    skeleton = _SHELL_RE.sub(lift, body)
    skeleton = sub.apply(skeleton)
    cmds = [(f, expand_variables(sub.apply(c), variables, user_config)) for f, c in slots]
    if arguments and not sub.used:
        skeleton = f"{skeleton.rstrip()}\n\nARGUMENTS: {arguments}"
    skeleton = expand_variables(skeleton, variables, user_config)
    if not slots:
        return skeleton
    if _has_control_chars("".join(c for _, c in cmds)):
        raise SkillShellError("No shell command was run: a `!` command contains a raw control character")
    cwd = cwd or Path.cwd()
    # Run every command before substituting so a failing one aborts without side effects ordering surprises.
    outputs = [_run_injection(c, f, cwd, allow_shell, shell, shell_disabled) for f, c in cmds]
    return re.sub(r"\x00SHELL(\d+)\x00", lambda m: outputs[int(m.group(1))], skeleton)


def effective_description(description: str, when_to_use: str = "", limit: int = DEFAULT_DESC_CHARS) -> str:
    """Listing text: ``description`` + ``when_to_use`` on one line, capped at ``limit`` characters."""
    desc = " ".join((description or "").split())
    when = " ".join((when_to_use or "").split())
    text = f"{desc} {when}".strip() if when else desc
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# ─── Manager ────────────────────────────────────────────────────────────────

_RESERVED_DIRS = {"synced"}
_RELOAD_CHECK_SECS = 1.0


def _git_root(start: Path) -> Path | None:
    cur = start
    for _ in range(64):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _plugin_skill_sources(workspace: Path) -> list[tuple[Any, list[Path]]]:
    """(plugin, SKILL.md files) for every enabled plugin."""
    try:
        from axon.commands.plugins import enabled_plugins, plugin_skill_files
        return [(p, plugin_skill_files(p)) for p in enabled_plugins(workspace)]
    except Exception:
        return []


class SkillManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self._skills: dict[str, Skill] = {}
        self.extra_dirs: list[Path] = []
        self._nested_targets: set[Path] = set()
        self.overrides: dict[str, str] = {}
        self.disabled: dict[str, Skill] = {}   # skills switched off with skillOverrides "off"
        self.problems: list[str] = []
        self._sig: tuple[Any, ...] = ()
        self._checked_at = 0.0
        self.discover()

    # `skills` re-scans the skill directories when a SKILL.md was added, edited or removed
    # (checked at most once a second), so every consumer sees live changes without a restart.
    @property
    def skills(self) -> dict[str, Skill]:
        now = time.monotonic()
        if now - self._checked_at >= _RELOAD_CHECK_SECS:
            self._checked_at = now
            try:
                if self._signature() != self._sig:
                    self.discover()
            except OSError:
                pass
        return self._skills

    @skills.setter
    def skills(self, value: dict[str, Skill]) -> None:
        self._skills = value

    def refresh_if_changed(self) -> bool:
        """Re-scan now if any skill file changed; True when it did."""
        self._checked_at = 0.0
        before = self._sig
        _ = self.skills
        return self._sig != before

    # -- locations -------------------------------------------------------

    def project_dirs(self) -> list[Path]:
        """Directories whose ``.claude/skills`` / ``.axon/skills`` apply, farthest (repo root) first."""
        ws = self.workspace
        try:
            ws = ws.resolve()
        except OSError:
            pass
        root = _git_root(ws)
        chain = [ws]
        if root is not None:
            cur = ws
            while cur != root and cur.parent != cur:
                cur = cur.parent
                chain.append(cur)
        return list(reversed(chain))

    def skill_roots(self) -> list[tuple[Path, str]]:
        """(skills directory, scope) in increasing precedence (plugins are handled separately)."""
        home = Path.home()
        roots: list[tuple[Path, str]] = []
        for d in self.project_dirs():
            roots.append((d / ".claude" / "skills", "project"))
            roots.append((d / ".axon" / "skills", "project"))
        from axon.skills.settings import get_bool_setting
        if not get_bool_setting(self.workspace, "disableClaudeSkills", False):
            roots.append((home / ".claude" / "skills", "personal"))   # Claude Code's own skills folder
        roots.append((_axon_home() / "skills", "personal"))
        return roots

    def _signature(self) -> tuple[Any, ...]:
        sig: list[Any] = []

        def stat(p: Path) -> None:
            try:
                sig.append((str(p), p.stat().st_mtime_ns))
            except OSError:
                sig.append((str(p), 0))

        roots = [r for r, _ in self.skill_roots()] + [d / ".claude" / "skills" for d in self.extra_dirs]
        for root in roots:
            if root.is_dir():
                stat(root)
                try:
                    for child in sorted(root.iterdir()):
                        if child.is_dir():
                            stat(child / "SKILL.md")
                except OSError:
                    pass
        for _plugin, files in _plugin_skill_sources(self.workspace):
            for f in files:
                stat(f)
        for f in self._settings_files():
            stat(f)
        return tuple(sig)

    def _settings_files(self) -> list[Path]:
        from axon.skills.settings import settings_files
        return settings_files(self.workspace)

    # -- discovery -------------------------------------------------------

    def discover(self) -> dict[str, Skill]:
        from axon.skills.settings import get_bool_setting, get_dict_setting
        self._skills = {}
        self.disabled = {}
        self.problems = []
        self.overrides = {
            str(k): str(v).strip().lower() for k, v in get_dict_setting(self.workspace, "skillOverrides").items()
        }
        if not get_bool_setting(self.workspace, "disableBundledSkills", False):
            self._register_bundled_skills()
        for root, scope in self.skill_roots():
            self._scan_root(root, scope)
        for d in self.extra_dirs:
            self._scan_extra_dir(d)
        for target in sorted(self._nested_targets):
            self._load_nested(target)
        for plugin, files in _plugin_skill_sources(self.workspace):
            for f in files:
                s = self._load_skill(f, "plugin", plugin=plugin)
                if s:
                    self._skills[s.name] = s
        self._apply_overrides()
        try:
            self._sig = self._signature()
        except OSError:
            self._sig = ()
        self._checked_at = time.monotonic()
        return self._skills

    def _scan_root(self, root: Path, scope: str, prefix: str = "") -> None:
        if not root.is_dir():
            return
        try:
            children = sorted(root.glob("*/SKILL.md"))
        except OSError:
            return
        for p in children:
            dname = p.parent.name
            if dname.lower() in _RESERVED_DIRS or dname.lower().startswith("anthropic-skills"):
                self.problems.append(f"{p}: reserved skill folder name '{dname}'")
                continue
            if name_refused(dname):
                self.problems.append(f"{p}: skill folder name contains characters that are not allowed")
                continue
            s = self._load_skill(p, scope)
            if s:
                if prefix and s.name in self._skills:
                    s.name = f"{prefix}:{s.name}"
                self._skills[s.name] = s

    def _scan_extra_dir(self, directory: Path) -> None:
        """``--add-dir`` / ``/add-dir``: that directory's ``.claude/skills``. A name that clashes gets
        the directory's folder name as prefix (``/cli:verify``)."""
        root = Path(directory) / ".claude" / "skills"
        if not root.is_dir():
            return
        prefix = Path(directory).resolve().name
        for p in sorted(root.glob("*/SKILL.md")):
            s = self._load_skill(p, "project")
            if not s:
                continue
            if s.name in self._skills:
                s.name = f"{prefix}:{s.name}"
            self._skills[s.name] = s

    def add_directory(self, directory: Path) -> int:
        """Load the skills of an added directory; returns how many skills were added."""
        d = Path(directory)
        if d not in self.extra_dirs:
            self.extra_dirs.append(d)
        before = len(self._skills)
        self.discover()
        return len(self._skills) - before

    def load_nested_for(self, file_path: Path | str) -> list[Skill]:
        """Load ``.claude/skills`` / ``.axon/skills`` of the directories between the workspace and
        ``file_path`` (the first time a file there is touched). A clashing name is qualified with the
        directory's path relative to the workspace (``apps/web:deploy``)."""
        try:
            target = Path(file_path).resolve()
            target.relative_to(self.workspace.resolve())
        except (OSError, ValueError):
            return []
        self._nested_targets.add(target)
        return self._load_nested(target)

    def _load_nested(self, target: Path) -> list[Skill]:
        added: list[Skill] = []
        try:
            ws = self.workspace.resolve()
            rel = target.relative_to(ws)
        except (OSError, ValueError):
            return added
        cur = ws
        for part in rel.parts[:-1]:
            cur = cur / part
            for base in (".claude", ".axon"):
                root = cur / base / "skills"
                if not root.is_dir():
                    continue
                prefix = str(cur.relative_to(ws)).replace("\\", "/")
                for p in sorted(root.glob("*/SKILL.md")):
                    s = self._load_skill(p, "project")
                    if s is None or any(x.path == p for x in self._skills.values()):
                        continue
                    if s.name in self._skills:
                        s.name = f"{prefix}:{s.name}"
                    self._skills[s.name] = s
                    added.append(s)
        return added

    def _apply_overrides(self) -> None:
        for name, mode in self.overrides.items():
            s = self._skills.get(name)
            if s is None or s.scope == "plugin":
                continue  # plugin skills are managed through /plugin
            if mode == "off":
                self.disabled[name] = self._skills.pop(name)
            elif mode == "user-invocable-only":
                s.disable_model_invocation = True
            elif mode == "name-only":
                s.name_only = True

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
            self._skills[b.name] = b
        from axon.skills.bundled import bundled_extra
        for name, description, instructions, flags in bundled_extra():
            self._skills[name] = Skill(
                name=name, description=description, instructions=instructions,
                path=Path(f"bundled://{name}"), scope="bundled", **flags,
            )

    def _load_skill(self, path: Path, scope: str, plugin: Any = None) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fm, body = parse_frontmatter(content)
        raw_name = fm.get("name") if isinstance(fm.get("name"), str) else ""
        dir_name = path.parent.name
        if plugin is not None and path.parent.resolve() == Path(plugin.path).resolve():
            dir_name = plugin.name  # a single skill at the plugin root
        short = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(raw_name or dir_name)).strip("-") or dir_name
        if raw_name and name_refused(str(raw_name)):
            short = re.sub(r"[^A-Za-z0-9_.:-]+", "-", dir_name).strip("-") or dir_name  # name field ignored
            self.problems.append(f"{path}: frontmatter name contains characters that are not allowed")
        name = short
        plugin_name = ""
        plugin_root: Path | None = None
        if plugin is not None:
            plugin_name = plugin.name
            plugin_root = Path(plugin.path)
            name = short if short.startswith(f"{plugin_name}:") else f"{plugin_name}:{short}"
        desc = fm.get("description", "")
        desc = desc if isinstance(desc, str) else " ".join(desc)
        if not desc:
            desc = next((ln.strip().lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")[:200]
        model = fm.get("model")

        def text(key: str) -> str:
            v = fm.get(key, fm.get(key.replace("_", "-"), ""))
            if isinstance(v, dict):
                return ""
            return v if isinstance(v, str) else " ".join(str(x) for x in v)

        def flag(key: str, default: bool) -> bool:
            return as_bool(fm.get(key, fm.get(key.replace("-", "_"))), default)

        hooks = fm.get("hooks")
        meta = fm.get("metadata")
        effort = text("effort")
        agent_name = text("agent")
        shell = text("shell").strip().lower()
        return Skill(
            name=name,
            description=desc,
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
            background=flag("background", True),
            hooks=hooks if isinstance(hooks, dict) else {},
            shell=shell if shell in ("bash", "powershell") else "",
            metadata=meta if isinstance(meta, dict) else {},
            license=text("license"),
            compatibility=text("compatibility")[:500],
            plugin=plugin_name,
            plugin_root=plugin_root,
            short_name=short,
            triggers=as_list(fm.get("triggers")),
        )

    @staticmethod
    def _parse_frontmatter(fm_text: str) -> dict[str, str]:
        """Backward-compatible helper: parse a frontmatter block (without --- fences)."""
        data, _ = parse_frontmatter(f"---\n{fm_text}\n---\n")
        return {k: (v if isinstance(v, str) else ", ".join(map(str, v)) if isinstance(v, list) else "") for k, v in data.items()}

    def get(self, name: str) -> Skill | None:
        n = name.lstrip("/")
        skills = self.skills
        if n in skills:
            return skills[n]
        low = n.lower()
        for k, v in skills.items():
            if k.lower() == low:
                return v
        # a plugin skill is also reachable by its bare name when nothing else uses it
        hits = [v for v in skills.values() if v.plugin and v.short_name.lower() == low]
        if len(hits) == 1:
            return hits[0]
        return None

    # -- rendering -------------------------------------------------------

    def variables(self, skill: Skill, session_id: str = "", effort: str = "") -> dict[str, str]:
        """Values for ``${CLAUDE_*}`` / ``${AXON_*}`` in a skill body and its ``allowed-tools``."""
        v = {
            "PROJECT_DIR": str(self.workspace),
            "SESSION_ID": session_id,
            "EFFORT": effort or (skill.effort or ""),
        }
        if skill.base_dir is not None:
            v["SKILL_DIR"] = str(skill.base_dir)
        if skill.plugin and skill.plugin_root is not None:
            v["PLUGIN_ROOT"] = str(skill.plugin_root)
            try:
                from axon.commands.plugins import plugin_data_dir
                from axon.commands.plugins import Plugin as _P
                pl = _P(name=skill.plugin, path=skill.plugin_root, scope="user")
                v["PLUGIN_DATA"] = str(plugin_data_dir(pl))
            except Exception:
                pass
        return v

    def user_config_for(self, skill: Skill) -> dict[str, str]:
        if not skill.plugin or skill.plugin_root is None:
            return {}
        try:
            from axon.commands.plugins import Plugin as _P, plugin_user_config_values
            return plugin_user_config_values(_P(name=skill.plugin, path=skill.plugin_root, scope="user"),
                                             self.workspace, include_sensitive=False)
        except Exception:
            return {}

    def allowed_tools_for(self, skill: Skill, session_id: str = "") -> list[str]:
        """``allowed-tools`` with ``${CLAUDE_SKILL_DIR}`` etc. expanded (so a rule matches the exact
        script command the body tells the model to run)."""
        vs = self.variables(skill, session_id)
        return [expand_variables(t, vs) for t in skill.allowed_tools]

    def render(self, skill: Skill, arguments: str = "", allow_shell: ShellGate | None = None, *,
               session_id: str = "", effort: str = "") -> str:
        from axon.skills.settings import get_bool_setting
        disabled = skill.scope != "bundled" and get_bool_setting(self.workspace, "disableSkillShellExecution", False)
        body = render_body(
            skill.instructions, arguments, names=skill.arguments,
            variables=self.variables(skill, session_id, effort), user_config=self.user_config_for(skill),
            cwd=self.workspace, allow_shell=allow_shell, shell=skill.shell or None, shell_disabled=disabled,
        )
        header = f"## Skill: /{skill.name}"
        if skill.base_dir is not None:
            header += f"\nBase directory for this skill: {skill.base_dir}"
        return f"{header}\n\n{body}"

    def execute_skill(self, name: str, arguments: str = "", allow_shell: ShellGate | None = None, **kw: Any) -> str:
        """Resolve a skill and return its ready-to-send instructions."""
        skill = self.get(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found.")
        return self.render(skill, arguments, allow_shell, **kw)

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
        skills = self.skills
        for skill_name, keywords in intent_keywords.items():
            if skill_name in skills and any(kw in text for kw in keywords):
                matched.append(skills[skill_name])
        for name, skill in skills.items():
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


def _desc_limit(workspace: Path | None) -> int:
    try:
        from axon.skills.settings import get_setting
        v = get_setting(workspace, "skillListingMaxDescChars", None)
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass
    try:
        env = int(os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET", "0"))
        if env > 0:
            return env
    except ValueError:
        pass
    return DEFAULT_DESC_CHARS


def skills_prompt_section(skills: list[Skill], workspace: Path | None = None) -> str:
    """System-prompt block listing skills (name + description only; ``description`` + ``when_to_use``
    capped at ``skillListingMaxDescChars``, default 1,536)."""
    if not skills:
        return ""
    limit = _desc_limit(workspace)
    lines = [
        "## Available Skills",
        "Skills are task playbooks. When one is relevant, load its full instructions with the "
        "Skill tool (or the user can run /<name>) before acting.",
    ]
    for s in sorted(skills, key=lambda x: x.name):
        if getattr(s, "disable_model_invocation", False):
            continue  # user-only skills are not advertised to the model
        if getattr(s, "name_only", False):
            lines.append(f"- {s.name}")
            continue
        desc = " ".join((s.description or "").split())
        when = " ".join((getattr(s, "when_to_use", "") or "").split())
        if when:
            desc = f"{desc} Use when: {when}" if desc else f"Use when: {when}"
        desc = effective_description(desc, "", limit)
        paths = getattr(s, "paths", None) or []
        if paths:
            desc += f" (applies to {', '.join(paths[:4])})"
        lines.append(f"- {s.name}: {desc}" if desc else f"- {s.name}")
    return "\n".join(lines)


def skill_content(name: str, workspace: Path | None = None, arguments: str = "",
                  session_id: str = "") -> str:
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
    try:
        return mgr.render(skill, arguments, allow_shell=None, session_id=session_id)
    except SkillShellError as e:
        return f"Skill '{skill.name}' could not be rendered: {e}"


def _raw_hint(value: object) -> str:
    """``argument-hint: [env]`` is a hint string, not a YAML list: show it verbatim as "[env]"."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value or "")
