"""
Permission rule syntax and matching (Claude Code compatible).

    Bash                      every Bash command
    Bash(git status:*)        "git status" or anything starting with "git status "
    Bash(npm run *)           shell-style glob over the whole command
    Edit(src/**)              any file-editing tool (Write/Edit/MultiEdit/Patch) under src/
    Read(~/.ssh/**)           any read tool (Read/Ls/Glob/Grep/...) under ~/.ssh
    WebFetch(domain:x.com)    fetches of x.com or its subdomains
    mcp__github               every tool exposed by the "github" MCP server
    mcp__github__create_issue one MCP tool

Path patterns: ``~/`` is the home directory, absolute paths (``/x``, ``//x``,
``C:/x``) are absolute, anything else is relative to the workspace. ``**`` crosses
directories, ``*`` and ``?`` do not. A pattern without wildcards also matches
everything below it when it names a directory. Matching is case-insensitive on
Windows.

Bash commands are split into sub-commands on ``; && || | & newline`` (quotes
respected). A *deny* rule matches when any sub-command matches; an *allow* rule only
when every sub-command matches and the command contains no ``$( )`` / backtick
substitution (unless the rule is the exact full command).
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

IS_WINDOWS = sys.platform == "win32"

EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "Patch", "NotebookEdit", "Notebook"})
READ_TOOLS = frozenset({"Read", "Ls", "Glob", "Grep", "CodeSearch", "FileTree", "CodeSymbols", "TableSearch", "Diff", "SemanticSearch", "CodeGraph"})
PATH_ARG_KEYS = ("path", "file_path", "notebook_path", "path_a", "path_b")
_GLOB_CHARS = set("*?[")


def tool_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return every path-like argument a tool call refers to."""
    out: list[str] = []
    for k in PATH_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    if not out and tool_name in ("Ls", "Glob", "Grep", "CodeSearch", "FileTree"):
        out.append(".")  # these default to the workspace root
    return out


# ─── Shell parsing ──────────────────────────────────────────────────────────

def split_shell_commands(cmd: str) -> list[str]:
    """Split a shell line into sub-commands on ; && || | & and newlines, honouring quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if ch in ";\n|&":
            # '&&', '||', '|&', '&', '|', ';', newline all separate commands;
            # keep redirections like 2>&1 intact.
            if ch == "&" and buf and buf[-1] == ">":
                buf.append(ch)
                i += 1
                continue
            parts.append("".join(buf))
            buf = []
            i += 1
            while i < n and cmd[i] in "|&":
                i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def has_substitution(cmd: str) -> bool:
    """True if the command contains command/process substitution outside single quotes."""
    quote: str | None = None
    for i, ch in enumerate(cmd):
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if ch == "'" and quote is None:
            quote = "'"
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
            continue
        if ch == "`":
            return True
        if ch == "$" and i + 1 < len(cmd) and cmd[i + 1] == "(":
            return True
        if ch in "<>" and i + 1 < len(cmd) and cmd[i + 1] == "(" and quote is None:
            return True
    return False


def substitution_bodies(cmd: str) -> list[str]:
    """Inner commands of $( ), backticks, <( ) and >( ) outside single quotes (nesting-aware)."""
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "`":
            end = cmd.find("`", i + 1)
            end = n if end < 0 else end
            out.append(cmd[i + 1:end])
            i = end + 1
            continue
        elif ch in "$<>" and i + 1 < n and cmd[i + 1] == "(" and (ch == "$" or quote is None):
            if ch == "$" and i + 2 < n and cmd[i + 2] == "(":  # $(( arithmetic )) is not a command
                i += 3
                continue
            depth, j = 1, i + 2
            while j < n and depth:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            out.append(cmd[i + 2:j - 1] if depth == 0 else cmd[i + 2:])
            i = j
            continue
        i += 1
    return [b for b in out if b.strip()]


def all_subcommands(cmd: str, _depth: int = 0) -> list[str]:
    """Every simple command in a shell line, including those inside command/process substitutions."""
    subs = split_shell_commands(cmd)
    if _depth < 8:
        for body in substitution_bodies(cmd):
            subs += all_subcommands(body, _depth + 1)
    return subs


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def strip_wrappers(cmd: str) -> str:
    """Remove leading env assignments and sudo/command/exec wrappers (used for deny matching)."""
    c = cmd.strip()
    changed = True
    while changed:
        changed = False
        m = _ENV_ASSIGN.match(c)
        if m:
            c = c[m.end():]
            changed = True
        for w in ("sudo ", "command ", "exec ", "nohup ", "time ", "env "):
            if c.startswith(w):
                c = c[len(w):].lstrip()
                # drop sudo flags such as -E / -u root
                while c.startswith("-"):
                    flag, _, rest = c.partition(" ")
                    c = rest.lstrip()
                    if flag in ("-u", "-g") and c:
                        _, _, c = c.partition(" ")
                        c = c.lstrip()
                changed = True
    return c


def _command_matches(pattern: str, cmd: str) -> bool:
    cmd = " ".join(cmd.split())
    pat = pattern.strip()
    if pat.endswith(":*"):
        prefix = " ".join(pat[:-2].split())
        return cmd == prefix or cmd.startswith(prefix + " ")
    if any(c in pat for c in _GLOB_CHARS):
        return fnmatch.fnmatchcase(cmd, " ".join(pat.split()))
    return cmd == " ".join(pat.split())


# ─── Path globbing ──────────────────────────────────────────────────────────

def _glob_to_regex(pat: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append("[" + pat[i + 1:j].replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE if IS_WINDOWS else 0)


def _abs_posix(p: str | Path) -> str:
    s = os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))
    try:
        s = os.path.realpath(s)
    except OSError:
        pass
    return s.replace("\\", "/")


def _pattern_to_abs(pattern: str, workspace: Path | None) -> str:
    pat = pattern.strip().replace("\\", "/")
    if pat.startswith("//"):
        pat = pat[1:]
    if pat.startswith("~"):
        base = str(Path.home()).replace("\\", "/")
        pat = base.rstrip("/") + pat[1:]
    elif not (pat.startswith("/") or re.match(r"^[A-Za-z]:/", pat)):
        ws = _abs_posix(workspace) if workspace is not None else _abs_posix(Path.cwd())
        if pat.startswith("./"):
            pat = pat[2:]
        pat = ws.rstrip("/") + "/" + pat
    # Normalise the literal (non-glob) head so symlinked roots compare equal
    head_parts = []
    rest_parts = pat.split("/")
    for idx, part in enumerate(rest_parts):
        if any(c in part for c in _GLOB_CHARS):
            rest = "/".join(rest_parts[idx:])
            head = "/".join(head_parts) or "/"
            return _abs_posix(head).rstrip("/") + "/" + rest
        head_parts.append(part)
    return _abs_posix(pat)


def path_matches(pattern: str, path: str, workspace: Path | None = None) -> bool:
    """Match a path argument against a rule's path pattern."""
    if pattern.strip() in ("*", "**"):
        return True
    raw = Path(path.strip()).expanduser()
    if not raw.is_absolute():
        raw = (Path(workspace) if workspace is not None else Path.cwd()) / raw
    target = _abs_posix(raw)
    pat_abs = _pattern_to_abs(pattern, workspace)
    if pat_abs.endswith("/"):
        pat_abs += "**"
    if not any(c in pat_abs for c in _GLOB_CHARS):
        a = pat_abs.lower() if IS_WINDOWS else pat_abs
        t = target.lower() if IS_WINDOWS else target
        return t == a or t.startswith(a.rstrip("/") + "/")
    if _glob_to_regex(pat_abs).match(target):
        return True
    # "dir/**" should also match "dir" itself
    if pat_abs.endswith("/**"):
        base = pat_abs[:-3]
        a = base.lower() if IS_WINDOWS else base
        t = target.lower() if IS_WINDOWS else target
        return t == a
    return False


# ─── Rules ──────────────────────────────────────────────────────────────────

def _tool_selector_matches(selector: str, tool_name: str) -> bool:
    if selector in ("*", ""):
        return True
    s, t = selector.lower(), tool_name.lower()
    if s == t:
        return True
    if selector == "Edit" and tool_name in EDIT_TOOLS:
        return True
    if selector == "Read" and tool_name in READ_TOOLS:
        return True
    if s.startswith("mcp__"):
        base = s[:-1] if s.endswith("*") else s
        if base.endswith("__"):
            return t.startswith(base)
        if base.count("__") == 1:  # server-level rule: mcp__server
            return t.startswith(base + "__")
    return False


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None = None

    def __str__(self) -> str:
        return f"{self.tool}({self.pattern})" if self.pattern else self.tool

    def matches(
        self,
        tool_name: str,
        args: dict[str, Any],
        workspace: Path | None = None,
        *,
        for_deny: bool = False,
    ) -> bool:
        if not _tool_selector_matches(self.tool, tool_name):
            return False
        if not self.pattern or self.pattern.strip() == "*":
            return True
        pat = self.pattern.strip()

        if tool_name == "Bash":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return False
            exact = not pat.endswith(":*") and not any(c in pat for c in _GLOB_CHARS)
            if exact and _command_matches(pat, cmd):  # an exact rule may name a whole compound line
                return True
            subs = split_shell_commands(cmd)
            if for_deny:
                subs = all_subcommands(cmd)
                return any(_command_matches(pat, s) or _command_matches(pat, strip_wrappers(s)) for s in subs)
            if has_substitution(cmd):
                return False
            return bool(subs) and all(_command_matches(pat, s) for s in subs)

        if pat.startswith("domain:"):
            url = str(args.get("url", "")).strip()
            host = (urlparse(url).hostname or "").lower()
            dom = pat[len("domain:"):].strip().lower()
            return bool(host) and (host == dom or host.endswith("." + dom))

        paths = tool_paths(tool_name, args)
        if paths:
            hits = [path_matches(pat, p, workspace) for p in paths]
            return any(hits) if for_deny else all(hits)

        if tool_name == "Git":
            line = " ".join(str(x) for x in [args.get("subcommand", "")] + list(args.get("args", []) or []) if x)
            return _command_matches(pat, line)

        # Generic: match any string argument
        for v in args.values():
            if isinstance(v, str) and fnmatch.fnmatchcase(v, pat):
                return True
        return False


def parse_rule(spec: str) -> Rule:
    """Parse 'Tool(pattern)' or bare 'Tool'."""
    spec = spec.strip()
    if "(" in spec and spec.endswith(")"):
        tool_part, pat_part = spec[:-1].split("(", 1)
        pat = pat_part.strip()
        return Rule(tool=tool_part.strip(), pattern=pat or None)
    return Rule(tool=spec, pattern=None)


def parse_rules(specs: list[str]) -> list[Rule]:
    return [parse_rule(s) for s in specs if isinstance(s, str) and s.strip()]
