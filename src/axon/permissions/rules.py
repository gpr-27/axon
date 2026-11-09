"""
Permission rule syntax and matching (Claude Code compatible).

    Bash                      every Bash command (Bash(*) is the same)
    Bash(npm run build)       exactly that command
    Bash(npm run *)           * is any text; a trailing " *" also matches the bare "npm run"
    Bash(git status:*)        ":*" at the very end is the same as " *"
    Edit(src/**)              the file-editing tools under src/
    Read(~/.ssh/**)           the file-reading tools under ~/.ssh
    WebFetch(domain:x.com)    fetches of x.com or its subdomains
    Agent(model:opus)         deny/ask only: a top-level input parameter with that value
    mcp__github               every tool of the "github" MCP server
    mcp__github__get_*        allow rules may glob only after a literal mcp__<server>__
    mcp__*, *                 deny/ask rules may glob the whole tool name

Path patterns follow gitignore:

    //path    absolute, from the filesystem root
    ~/path    from the home directory
    /path     from the settings source: the workspace for project and local files and for flags,
              ~/.axon for user settings, the file's own folder for --settings <file>
    path      relative to the workspace; a bare name matches at any depth; a single directory segment
              such as src/** matches at any depth in deny and ask rules, and only at the top in allow rules
    ./path    relative to the workspace, at that place only

``*`` stays inside a path segment, ``**`` crosses directories, ``!pattern`` carves a path out of the
relative deny or ask rules listed before it in the same file (see ``apply_carve_outs``). An allow rule needs the
requested path and the file it resolves to to match; a deny or ask rule needs either. Matching is
case-insensitive on Windows, where ``C:\\x`` compares as ``/c/x``.

Bash commands are split into sub-commands on ``; && || | |& & newline`` (quotes respected). A deny or ask rule
matches when any sub-command matches, including one inside a subshell, a substitution or a loop body, and looks past
any leading assignment and the wrappers. An allow rule needs every sub-command and looks past the fixed wrapper list
and known-safe assignments only.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

IS_WINDOWS = sys.platform == "win32"

EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "Patch", "NotebookEdit", "Notebook"})
READ_TOOLS = frozenset({"Read", "Ls", "Glob", "Grep", "CodeSearch", "FileTree", "CodeSymbols", "TableSearch", "Diff", "SemanticSearch", "CodeGraph"})
READ_DENY_ALSO_BLOCKS = frozenset({"Write", "Edit", "MultiEdit", "Patch"})
PATH_ARG_KEYS = ("path", "file_path", "notebook_path", "path_a", "path_b")
_GLOB_CHARS = set("*?[")


def git_call(args: dict[str, Any]) -> tuple[str, list[str], str]:
    """(subcommand, arguments, folder) of a Git tool call, wherever the call put ``-C <folder>``: the ``path``
    argument, before the subcommand, or among the arguments. The tool and the permission checks both read this."""
    sub = str(args.get("subcommand") or "").strip()
    extra = [a for a in (args.get("args") or []) if isinstance(a, str)] if isinstance(args.get("args"), list) else []
    folder = str(args.get("path") or "").strip()
    if sub == "-C" and extra:
        folder, extra = extra[0], extra[1:]
        sub, extra = (extra[0], extra[1:]) if extra else ("", [])
    if "-C" in extra and "--" not in extra[:extra.index("-C")] and extra.index("-C") + 1 < len(extra):
        i = extra.index("-C")
        folder, extra = extra[i + 1], extra[:i] + extra[i + 2:]
    return sub.lower(), extra, folder


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_like_outside_path(val: str) -> bool:
    """An argument that names a file outside the call's folder: absolute (POSIX, ``C:\\x`` or ``\\\\server``), ``~``,
    or climbing out with ``..``. Checked the same way on every platform."""
    return val.startswith(("/", "~", "../", "..\\", "\\")) or bool(_DRIVE_PATH.match(val))


def tool_paths(tool_name: str, args: dict[str, Any], keep_prefixed: bool = False) -> list[str]:
    """Return every path-like argument a tool call refers to. ``keep_prefixed``: a patch's ``a/x`` header
    is listed as well as ``x`` (for deny / protected-path checks; see ``patch_paths``)."""
    out: list[str] = []
    for k in PATH_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    if tool_name == "Git":
        _sub, git_args, folder = git_call(args)
        if folder and folder not in out:
            out.append(folder)
        for a in git_args:
            if a == "--":
                continue
            val = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
            if _looks_like_outside_path(val) and val not in out:
                out.append(val)         # a file named in the arguments (--flag=/path, or a path after --) is a path the call reads
    if tool_name == "Glob":
        base = _glob_pattern_base(args.get("pattern"))
        if base:
            out.append(base)  # Glob re-roots absolute / ~ / ../ patterns at their literal prefix
    if tool_name in ("Patch", "ApplyPatch", "apply_patch") and not out:
        # The files a unified diff touches are named in its ---/+++ headers, not in a path argument;
        # without them acceptEdits, deny rules and the outside-workspace check would never see them.
        out.extend(patch_paths(args.get("patch") or args.get("diff"), keep_prefixed))
    if not out and tool_name in ("Ls", "Glob", "Grep", "CodeSearch", "FileTree"):
        out.append(".")  # these default to the workspace root
    return out


def patch_paths(patch: Any, keep_prefixed: bool = False) -> list[str]:
    """File paths named by a unified diff's ``--- a/x`` / ``+++ b/x`` header pairs (/dev/null skipped),
    parsed the same way as axon.tools.patch.parse_patch. ``keep_prefixed`` also lists ``a/x`` itself:
    the Patch tool edits that file when a real directory called ``a`` holds it."""
    if not isinstance(patch, str):
        return []
    lines = patch.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines[:-1]):
        if not (line.startswith("--- ") and lines[i + 1].startswith("+++ ")):
            continue
        for raw in (line[4:], lines[i + 1][4:]):
            p = raw.strip().split("\t")[0].strip()
            if len(p) >= 2 and p[0] == p[-1] == '"':
                p = p[1:-1]
            if not p or p == "/dev/null":
                continue
            names = [p]
            if p.startswith(("a/", "b/")):
                names = [p[2:], p] if keep_prefixed else [p[2:]]
            out.extend(n for n in names if n not in out)
    return out


def _glob_pattern_base(pattern: Any) -> str | None:
    """Literal directory prefix of a Glob pattern that points outside its search root
    ("/abs/**/*.py", "~/x/*", "../other/*"), mirroring GlobTool's re-rooting; None otherwise."""
    if not isinstance(pattern, str):
        return None
    pat = pattern.strip().replace("\\", "/")
    if not (os.path.isabs(pat) or re.match(r"^[A-Za-z]:/", pat) or pat.startswith(("../", "~")) or pat == ".."):
        return None
    lit: list[str] = []
    for part in pat.split("/"):
        if any(ch in part for ch in "*?[{"):
            break
        lit.append(part)
    return "/".join(lit) or "/"


# ─── Shell parsing ──────────────────────────────────────────────────────────

def _opens_ansi_c(cmd: str, quote_at: int) -> bool:
    """True when the ``'`` at ``quote_at`` starts a ``$'...'`` string, inside which a backslash escapes the next
    character (so ``\\'`` does not end it), unlike a plain single-quoted string."""
    if quote_at < 1 or cmd[quote_at - 1] != "$":
        return False
    slashes, j = 0, quote_at - 2
    while j >= 0 and cmd[j] == "\\":
        slashes, j = slashes + 1, j - 1
    return slashes % 2 == 0


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
            if ch == "\\" and quote in ('"', "$'") and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote or (quote == "$'" and ch == "'"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = "$'" if ch == "'" and _opens_ansi_c(cmd, i) else ch
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
            if ch == "&" and ((buf and buf[-1] in "<>") or cmd[i + 1:i + 2] == ">"):
                buf.append(ch)  # 2>&1, <&3 and the bash "&>file" / "&>>file" redirections
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


def redirect_targets(cmd: str) -> list[str]:
    """Files a shell line writes to with ``>``, ``>>``, ``>|``, ``&>`` or ``N>`` (quotes respected; fd
    duplications such as ``2>&1`` are not files)."""
    out: list[str] = []
    for sub in split_shell_commands(cmd):
        quote: str | None = None
        i, n = 0, len(sub)
        while i < n:
            ch = sub[i]
            i += 1
            if quote:
                if ch == "\\" and quote in ('"', "$'"):
                    i += 1
                elif ch == quote or (quote == "$'" and ch == "'"):
                    quote = None
            elif ch in "'\"":
                quote = "$'" if ch == "'" and _opens_ansi_c(sub, i - 1) else ch
            elif ch == "\\":
                i += 1
            elif ch == ">" and not (i < n and sub[i] == "("):
                while i < n and sub[i] in ">|":
                    i += 1
                dup = i < n and sub[i] == "&"
                while i < n and sub[i] in "& \t":
                    i += 1
                start = i
                while i < n and (quote or sub[i] not in " \t<>;|&"):
                    if quote:
                        quote = None if sub[i] == quote else quote
                    elif sub[i] in "'\"":
                        quote = sub[i]
                    i += 1
                target = sub[start:i].replace("'", "").replace('"', "")
                if not target:
                    out.append("$?")      # `>| file` (the | split the line): the file is unknown, so not a safe one
                elif not (dup and (target.isdigit() or target == "-")):
                    out.append(target)
    return out


def has_substitution(cmd: str) -> bool:
    """True if the command contains command/process substitution outside single quotes."""
    quote: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        i += 1
        if quote == "$'":
            if ch == "\\":
                i += 1                  # inside $'...' a backslash escapes the next character, even a quote
            elif ch == "'":
                quote = None
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if ch == "\\":
            i += 1  # an escaped quote (\' or \") neither opens nor closes a quote
            continue
        if ch == "'" and quote is None:
            quote = "$'" if _opens_ansi_c(cmd, i - 1) else "'"
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
            continue
        if ch == "`":
            return True
        if ch == "$" and i < n and cmd[i] == "(":
            return True
        if ch in "<>" and i < n and cmd[i] == "(" and quote is None:
            return True
    return False


def substitution_bodies(cmd: str) -> list[str]:
    """Inner commands of $( ), backticks, <( ) and >( ) outside single quotes (nesting-aware)."""
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote == "$'":
            if ch == "\\":
                i += 1                  # inside $'...' a backslash escapes the next character, even a quote
            elif ch == "'":
                quote = None
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "$'" if _opens_ansi_c(cmd, i) else "'"
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
        for sub in list(subs):
            payload = nested_shell_payload(sub)
            if payload:
                subs += all_subcommands(payload, _depth + 1)
    return subs


_NESTED_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})


def plain_words(sub: str) -> list[str]:
    """The words of one sub-command as the shell reads them: quotes and backslashes removed, and a program
    given by path (``/bin/rm``) named by its last part. Empty when the text cannot be split."""
    try:
        words = shlex.split(strip_wrappers(bare_simple_command(sub)), posix=True)
    except ValueError:
        return []
    if words:
        words[0] = words[0].rsplit("/", 1)[-1]
    return words


def nested_shell_payload(sub: str) -> str | None:
    """The text ``bash -c '...'``, ``sh -c ...`` or ``eval ...`` will run, else None. Deny and ask rules read it,
    so ``bash -c 'rm x'`` is as visible as ``rm x``."""
    words = plain_words(sub)
    if not words:
        return None
    if words[0] == "eval":
        return " ".join(words[1:]) or None
    if words[0] not in _NESTED_SHELLS:
        return None
    for i, arg in enumerate(words[1:], start=1):
        if not arg.startswith("-"):
            return None                        # a script file: its contents are not visible here
        if not arg.startswith("--") and "c" in arg:
            return words[i + 1] if i + 1 < len(words) else None
    return None


_ENV_ASSIGN = re.compile(r"""^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"(?:[^"\\]|\\.)*"|\S)*\s+""")
_SAFE_ENV = frozenset({"NODE_ENV", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "NO_COLOR", "FORCE_COLOR", "TERM",
                       "TZ", "CI", "DEBUG", "RUST_BACKTRACE", "RUST_LOG", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
                       "GOOS", "GOARCH", "CGO_ENABLED", "PAGER", "GIT_PAGER"})
# Wrappers that run their argument as the actual command. xargs counts only with no flags at all.
_BASH_WRAPPERS = ("timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob")
# Also looked through for deny and ask rules: they run their argument too, and a deny rule should see it.
_DENY_ONLY_WRAPPERS = ("sudo", "exec", "env")
EXEC_WRAPPERS = frozenset({"watch", "setsid", "ionice", "flock"})
FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-rm", "-fprint", "-fprint0", "-fprintf", "-fls", "-files0-from"})
_SHELL_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "time"})
_SHELL_ENDERS = frozenset({"done", "fi", "esac", "}", ")"})
_OPTION_WITH_VALUE = {"timeout": ("-k", "--kill-after", "-s", "--signal"), "nice": ("-n", "--adjustment"),
                      "stdbuf": ("-i", "-o", "-e", "--input", "--output", "--error")}


def _strip_one_wrapper(c: str, names: tuple[str, ...]) -> str | None:
    """``c`` with one leading wrapper and its options removed, or None when it starts with none of ``names``."""
    word, _, rest = c.partition(" ")
    if word not in names or not rest.strip():
        return None
    if word == "command" and rest.lstrip().startswith(("-v", "-V")):
        return None                            # the query form looks a command up instead of running it
    rest = rest.lstrip()
    takes_value = _OPTION_WITH_VALUE.get(word, ())
    while rest.startswith("-"):
        flag, _, more = rest.partition(" ")
        rest = more.lstrip()
        if flag in takes_value or (word == "sudo" and flag in ("-u", "-g")):
            _, _, rest = rest.partition(" ")
            rest = rest.lstrip()
    if word == "timeout":                      # its first plain word is the duration
        _, _, rest = rest.partition(" ")
        rest = rest.lstrip()
    elif word == "nice" and rest[:1].isdigit():
        _, _, rest = rest.partition(" ")
        rest = rest.lstrip()
    if word == "env":
        while _ENV_ASSIGN.match(rest):
            rest = rest[_ENV_ASSIGN.match(rest).end():]  # type: ignore[union-attr]
    return rest or None


def strip_wrappers(cmd: str) -> str:
    """For deny and ask rules: remove every leading assignment and every wrapper."""
    c = cmd.strip()
    changed = True
    while changed:
        changed = False
        m = _ENV_ASSIGN.match(c)
        if m:
            c = c[m.end():].lstrip()
            changed = True
            continue
        inner = _strip_one_wrapper(c, _BASH_WRAPPERS + _DENY_ONLY_WRAPPERS)
        if inner is not None:
            c, changed = inner, True
            continue
        if c.startswith("xargs ") and not c[6:].lstrip().startswith("-"):
            c, changed = c[6:].lstrip(), True
    return c


def strip_allow_wrappers(cmd: str) -> str:
    """For allow rules: only the fixed wrapper list, known-safe assignments and flagless xargs."""
    c = cmd.strip()
    changed = True
    while changed:
        changed = False
        m = _ENV_ASSIGN.match(c)
        if m and c.split("=", 1)[0] in _SAFE_ENV:
            c, changed = c[m.end():].lstrip(), True
            continue
        inner = _strip_one_wrapper(c, _BASH_WRAPPERS)
        if inner is not None:
            c, changed = inner, True
            continue
        if c.startswith("xargs ") and not c[6:].lstrip().startswith("-"):
            c, changed = c[6:].lstrip(), True
    return c


def bare_simple_command(sub: str) -> str:
    """A sub-command without the shell syntax around it: ``do rm x`` -> ``rm x``, ``(rm x`` -> ``rm x``, ``done`` -> ''."""
    c = sub.strip()
    while True:
        c = c.lstrip("( \t")
        first, _, rest = c.partition(" ")
        if first in _SHELL_KEYWORDS and first != "time":
            c = rest.strip()
            continue
        break
    c = c.rstrip(") \t") if c.count(")") > c.count("(") else c
    if c.endswith(" }"):
        c = c[:-2].rstrip()
    if c in _SHELL_ENDERS:
        return ""
    if re.match(r"^(for|case|select)\b", c):
        return ""                              # the loop header runs nothing itself; its body is the next sub-command
    return c


def _command_regex(pat: str) -> re.Pattern[str]:
    """``*`` is any text, including spaces; everything else, ``?`` and ``[`` too, is literal."""
    return re.compile("^" + ".*".join(re.escape(p) for p in pat.split("*")) + "$", re.S)


def _command_matches(pattern: str, cmd: str) -> bool:
    cmd = " ".join(cmd.split())
    pat = " ".join(pattern.strip().split())
    if pat.endswith(":*"):                     # only recognised at the very end: Bash(git:* push) is a literal colon
        pat = pat[:-2] + " *"
    if "*" not in pat:
        return cmd == pat
    if pat.endswith(" *") and pat.count("*") == 1 and cmd == pat[:-2]:
        return True                            # a trailing " *" that is the only wildcard also matches the bare command
    return bool(_command_regex(pat).match(cmd))


def is_unparseable_compound(cmd: str) -> bool:
    """``npm test &&``: a dangling operator means the line cannot be split for allow-rule matching."""
    return bool(re.search(r"(&&|\|\|)\s*$", cmd.strip()))


def prefix_rule_cannot_approve(sub: str) -> bool:
    """Exec wrappers and find with an action run or destroy things a wildcard rule never named."""
    words = sub.split()
    if not words:
        return False
    prog = words[0].rsplit("/", 1)[-1]
    return prog in EXEC_WRAPPERS or (prog == "find" and any(w in FIND_ACTIONS for w in words[1:]))


_READ_COMMANDS = frozenset({"cat", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg", "diff", "stat", "file", "nl", "sort", "uniq", "cut", "od", "xxd", "strings", "md5sum", "sha1sum", "sha256sum", "sha512sum", "base64"})
_WRITE_COMMANDS = frozenset({"tee", "touch", "rm", "rmdir", "mkdir", "truncate", "shred"})


def _words(sub: str) -> list[str]:
    try:
        return shlex.split(sub, posix=True)
    except ValueError:
        return sub.split()


def bash_file_targets(cmd: str) -> tuple[list[str], list[str]]:
    """(files read, files written) that a shell line names through commands Axon recognises and redirections."""
    reads, writes = [], list(redirect_targets(cmd))
    reads += input_redirect_targets(cmd)
    for sub in all_subcommands(cmd):
        c = strip_wrappers(bare_simple_command(sub))
        words = [w for w in _words(re.sub(r"\d*(?:>>?|>\||&>>?|<)\s*\S+", " ", c)) if w]
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        args = words[1:]
        plain = [a for a in args if not a.startswith("-")]
        if prog in ("grep", "egrep", "fgrep", "rg") and plain:
            plain = plain[1:]                  # the pattern
        if prog in _READ_COMMANDS:
            reads += plain
        elif prog in _WRITE_COMMANDS:
            writes += plain
        elif prog == "sed":
            inplace = any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args)
            files = plain[1:] if plain else []
            (writes if inplace else reads).extend(files)
        elif prog in ("cp", "mv", "install") and len(plain) >= 2:
            reads += plain[:-1]
            writes += plain[-1:]
            if prog == "mv":
                writes += plain[:-1]
    return reads, writes


def input_redirect_targets(cmd: str) -> list[str]:
    """Files a shell line reads with ``<`` (here-docs, here-strings, ``<&3`` and ``<(...)`` are not files)."""
    out: list[str] = []
    for sub in split_shell_commands(cmd):
        for m in re.finditer(r"""(?<![<\\])<(?![<&(])\s*("[^"]*"|'[^']*'|[^\s;|&<>]+)""", sub):
            out.append(m.group(1).strip("'\""))
    return out


# ─── Path globbing ──────────────────────────────────────────────────────────

def _glob_to_regex(pat: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and pat[i + 1:i + 2] in ("[", "]", "*", "?", "!", "\\"):
            out.append(re.escape(pat[i + 1]))  # the escaped form generated rules use for a literal path
            i += 2
            continue
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
                body = pat[i + 1:j].replace("\\", "\\\\")
                if body[:1] == "!":  # glob negation ([!abc]) is [^abc] in a regex
                    body = "^" + body[1:]
                elif body[:1] == "^":  # a literal caret, not a negation, in glob syntax
                    body = "\\" + body
                out.append("[" + body + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE if IS_WINDOWS else 0)


def _posix_form(p: str) -> str:
    """Windows paths compare as /c/Users/x, so //c/** and //**/.env mean what they say."""
    p = p.replace("\\", "/")
    if IS_WINDOWS and re.match(r"^[A-Za-z]:(/|$)", p):
        return "/" + p[0].lower() + p[2:]
    return p


def _abs_posix(p: str | Path) -> str:
    s = os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))
    try:
        s = os.path.realpath(s)
    except OSError:
        pass
    return _posix_form(s)


def _requested_posix(p: str | Path) -> str:
    """The path as it was asked for: absolute and collapsed, symlinks not followed."""
    return _posix_form(os.path.normpath(os.path.abspath(os.path.expanduser(str(p)))))


def _has_glob(text: str) -> bool:
    return any(c in text for c in _GLOB_CHARS)


def pattern_shape(pattern: str) -> str:
    """'absolute' (//x), 'home' (~/x), 'anchored' (/x), 'here' (./x) or 'relative' (x)."""
    pat = pattern.strip().replace("\\\\", "/") if IS_WINDOWS else pattern.strip()
    if pat.startswith("//"):
        return "absolute"
    if pat.startswith("~"):
        return "home"
    if pat.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", pat):
        return "absolute" if not pat.startswith("/") else "anchored"
    if pat.startswith("./"):
        return "here"
    return "relative"


def _literal_head_variants(pat: str) -> list[str]:
    """``pat`` as written and with its literal directories resolved, so //etc/** also covers /private/etc."""
    parts = pat.split("/")
    head: list[str] = []
    rest: list[str] = []
    for idx, part in enumerate(parts):
        if _has_glob(part):
            rest = parts[idx:]
            break
        head.append(part)
    literal = "/".join(head) or "/"
    tail = ("/" + "/".join(rest)) if rest else ""
    real = _abs_posix(literal)
    return list(dict.fromkeys([literal.rstrip("/") + tail if tail else literal, real.rstrip("/") + tail if tail else real]))


def _rule_patterns(pattern: str, workspace: Path | None, anchor: Path | None, kind: str) -> list[str]:
    """The absolute patterns a rule's path pattern stands for, for this kind of rule."""
    pat = pattern.strip()
    if IS_WINDOWS:
        pat = pat.replace("\\", "/")
    ws = _requested_posix(workspace) if workspace is not None else _requested_posix(Path.cwd())
    shape = pattern_shape(pat)
    if shape == "absolute":
        out = [_posix_form(pat[1:] if pat.startswith("//") else pat)]
    elif shape == "home":
        out = [_posix_form(str(Path.home())).rstrip("/") + pat[1:]]
    elif shape == "anchored":
        base = _requested_posix(anchor) if anchor is not None else ws
        out = [_posix_form(pat)]                                  # how a leading / always read: an absolute path
        if kind != "allow":
            out.append(base.rstrip("/") + pat)                    # and, to be safe, the settings folder too
    elif shape == "here":
        out = [ws.rstrip("/") + "/" + pat[2:]]
    else:
        body = pat.rstrip("/")
        out = [ws.rstrip("/") + "/" + pat]
        if "/" not in body:                    # a bare name: gitignore matches it at any depth
            out.append(ws.rstrip("/") + "/**/" + pat)
        elif kind != "allow" and body.endswith("/**") and "/" not in body[:-3]:
            out.append(ws.rstrip("/") + "/**/" + pat)  # src/**: any depth for deny and ask, the top for allow
    variants: list[str] = []
    for p in out:
        variants += _literal_head_variants(p)
    return list(dict.fromkeys(variants))


def _one_pattern_matches(pat_abs: str, target: str) -> bool:
    if pat_abs.endswith("/"):
        pat_abs += "**"
    if not _has_glob(pat_abs) and "\\" not in pat_abs:
        a = pat_abs.lower() if IS_WINDOWS else pat_abs
        t = target.lower() if IS_WINDOWS else target
        return t == a or t.startswith(a.rstrip("/") + "/")
    if _glob_to_regex(pat_abs).match(target):
        return True
    if pat_abs.endswith("/**"):                # "dir/**" also matches "dir" itself
        a = pat_abs[:-3].lower() if IS_WINDOWS else pat_abs[:-3]
        return (target.lower() if IS_WINDOWS else target) == a
    return False


def path_matches(pattern: str, path: str, workspace: Path | None = None, *, anchor: Path | None = None, kind: str = "deny") -> bool:
    """Match a path argument against a rule's path pattern. An allow rule needs both the requested path and the
    file it resolves to to match; a deny or ask rule needs either."""
    if pattern.strip() in ("*", "**"):
        return True
    raw = Path(path.strip()).expanduser()
    if not raw.is_absolute():
        raw = (Path(workspace) if workspace is not None else Path.cwd()) / raw
    requested, resolved = _requested_posix(raw), _abs_posix(raw)
    patterns = _rule_patterns(pattern, workspace, anchor, kind)
    hit_requested = any(_one_pattern_matches(p, requested) for p in patterns)
    hit_resolved = any(_one_pattern_matches(p, resolved) for p in patterns)
    return (hit_requested and hit_resolved) if kind == "allow" else (hit_requested or hit_resolved)


def domain_matches(rule_domain: str, host: str) -> bool:
    """``x.com`` is that host and its subdomains, ``*.x.com`` any subdomain at any depth but not x.com, ``*`` every host; any
    other ``*`` stays between two dots, so example.* never matches example.evil.com. A trailing dot is dropped."""
    dom, host = rule_domain.strip().lower().rstrip("."), host.lower().rstrip(".")
    if not host or not dom:
        return False
    if dom == "*":
        return True
    if dom.startswith("*."):
        return host.endswith(dom[1:]) and host != dom[2:] and "*" not in dom[2:]
    if "*" not in dom:
        return host == dom or host.endswith("." + dom)
    return bool(re.match("^" + "[^.]*".join(re.escape(p) for p in dom.split("*")) + "$", host))


# ─── Rules ──────────────────────────────────────────────────────────────────

def _name_glob(selector: str, name: str) -> bool:
    return bool(re.match("^" + ".*".join(re.escape(p) for p in selector.split("*")) + "$", name, re.I))


def allow_glob_is_anchored(selector: str) -> bool:
    """An allow rule may glob a tool name only after a literal ``mcp__<server>__``."""
    m = re.match(r"^mcp__([^*]+?)__", selector)
    return bool(m)


def _tool_selector_matches(selector: str, tool_name: str, kind: str = "deny", server: str | None = None) -> bool:
    if selector in ("", "*"):
        return kind != "allow"                 # an unanchored allow glob approves nothing
    s, t = selector.lower(), tool_name.lower()
    if s == t:
        return True
    if selector == "Agent" and tool_name == "Task":
        return True
    if selector == "Edit" and tool_name in EDIT_TOOLS:
        return True
    if selector == "Read" and tool_name in READ_TOOLS:
        return True
    if selector == "Read" and kind == "deny" and tool_name in READ_DENY_ALSO_BLOCKS:
        return True                            # a Read deny also stops Edit and Write on that path, not NotebookEdit
    if "*" in s:
        if kind == "allow" and not allow_glob_is_anchored(selector):
            return False
        return _name_glob(s, t)
    if s.startswith("mcp__") and s.count("__") == 1:
        if not t.startswith(s + "__"):
            return False                       # mcp__server: every tool of that server
        # As an allow rule it names the server exactly; mcp__reports must not approve a server called reports__v2.
        return kind != "allow" or server is None or server.lower() == s[5:]
    if s.startswith("mcp__") and kind != "allow" and t.startswith(s + "__"):
        return True                            # deny/ask: mcp__reports__v2 covers the server reports__v2
    return False


# The field a tool's own rule syntax already covers; Bash(command:rm *) would be bypassed by a compound command.
PRIMARY_PARAMS = {"Bash": "command", "PowerShell": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
                  "Grep": "path", "Glob": "path", "NotebookEdit": "notebook_path", "WebFetch": "url"}
_PARAM_RULE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", re.S)


def primary_param_rule(tool: str, pattern: str | None) -> bool:
    """True for a rule such as Bash(command:rm *), which Axon ignores and warns about."""
    m = _PARAM_RULE.match(pattern or "")
    canonical = "Bash" if tool == "Bash" else tool
    return bool(m and PRIMARY_PARAMS.get(canonical) == m.group(1))


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None = None
    # Where a /path pattern anchors. None: the workspace (project and local files, flags, session rules).
    anchor: Path | None = field(default=None, compare=False)

    def __str__(self) -> str:
        return f"{self.tool}({self.pattern})" if self.pattern else self.tool

    @property
    def negated(self) -> bool:
        return (self.pattern or "").strip().startswith("!")

    def matches(
        self,
        tool_name: str,
        args: dict[str, Any],
        workspace: Path | None = None,
        *,
        for_deny: bool = False,
        kind: str | None = None,
        server: str | None = None,
    ) -> bool:
        kind = kind or ("deny" if for_deny else "allow")
        restrict = kind != "allow"
        if not _tool_selector_matches(self.tool, tool_name, kind, server):
            return False
        if not self.pattern or self.pattern.strip() == "*":
            return True
        pat = self.pattern.strip()

        if restrict:
            m = _PARAM_RULE.match(pat)
            if m and m.group(1) != "domain":
                name, value = m.group(1), m.group(2)
                if PRIMARY_PARAMS.get(tool_name) == name:
                    return False
                if name in args:               # a parameter the call leaves out is never matched
                    actual = args[name]
                    if isinstance(actual, bool):
                        actual = "true" if actual else "false"
                    if not isinstance(actual, (str, int, float)):
                        return False
                    return bool(_command_regex(value).match(str(actual)))

        if tool_name == "Bash":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return False
            whole = " ".join(pat.split()) == " ".join(cmd.split())
            if restrict:
                if whole:
                    return True
                for s in all_subcommands(cmd):
                    s = bare_simple_command(s)
                    if not s:
                        continue
                    if _command_matches(pat, s) or _command_matches(pat, strip_wrappers(s)):
                        return True
                    words = plain_words(s)     # '/bin/rm x', '\rm x' and 'r"m" x' are all rm
                    if words and _command_matches(pat, strip_wrappers(" ".join(words))):
                        return True
                return False
            if is_unparseable_compound(cmd):
                return False
            # A rule that is literally the whole line approves it. With a * in it the rule reads as a pattern,
            # so a wrapper or a find action stays out of reach.
            if whole and not ("*" in pat and any(prefix_rule_cannot_approve(s) for s in split_shell_commands(cmd))):
                return True
            if has_substitution(cmd):
                return False
            subs = [bare_simple_command(s) for s in split_shell_commands(cmd)]
            subs = [s for s in subs if s]
            return bool(subs) and all(sub_allowed_by(pat, s) for s in subs)

        if tool_name == "Task":      # Agent(Explore): match the subagent type
            kind_name = str(args.get("subagent_type") or "general-purpose").strip().lower()
            return fnmatch.fnmatchcase(kind_name, pat.lower())

        if pat.startswith("domain:"):
            host = urlparse(str(args.get("url", "")).strip()).hostname or ""
            return domain_matches(pat[len("domain:"):], host)

        paths = tool_paths(tool_name, args, keep_prefixed=restrict)
        if paths:
            if pat.startswith("!"):
                if not restrict:
                    return False               # a carve-out is not an approval
                return any(carve_out_matches(pat, p, workspace) for p in paths)
            hits = [path_matches(pat, p, workspace, anchor=self.anchor, kind=kind) for p in paths]
            return any(hits) if restrict else all(hits)

        if tool_name == "Git":
            line = " ".join(str(x) for x in [args.get("subcommand", "")] + list(args.get("args", []) or []) if x)
            return _command_matches(pat, line)

        # Generic: match any string argument
        for v in args.values():
            if isinstance(v, str) and fnmatch.fnmatchcase(v, pat):
                return True
        return False


def sub_allowed_by(pattern: str, sub: str) -> bool:
    """One simple command against one allow pattern."""
    if "*" in pattern and prefix_rule_cannot_approve(strip_allow_wrappers(sub)):
        return False
    return _command_matches(pattern, sub) or _command_matches(pattern, strip_allow_wrappers(sub))


def carve_out_matches(pattern: str, path: str, workspace: Path | None) -> bool:
    """``!pattern`` is always read relative to the workspace, so it cannot reach a //, ~/ or / rule."""
    body = pattern.strip()[1:]
    if body.startswith(("/", "~")):
        return False
    return path_matches(body, path, workspace, kind="deny")


def carve_out_can_reopen(rule: Rule) -> bool:
    """A carve-out reaches only relative rules, and not one that blocks a directory as a whole."""
    pat = (rule.pattern or "").strip()
    if not pat or pat.startswith("!") or pattern_shape(pat) not in ("relative", "here"):
        return False
    return not pat.rstrip("/").endswith("/**") and not pat.endswith("/")


def apply_carve_outs(rules: list[Rule], tool_name: str, args: dict[str, Any], workspace: Path | None, kind: str) -> Rule | None:
    """The first rule from one settings source that still matches after that source's ``!`` patterns."""
    hits: list[Rule] = []
    for r in rules:
        if r.negated:
            if r.matches(tool_name, args, workspace, kind=kind):
                hits = [h for h in hits if not carve_out_can_reopen(h)]
        elif r.matches(tool_name, args, workspace, kind=kind):
            hits.append(r)
    return hits[0] if hits else None


def parse_rule(spec: str) -> Rule:
    """Parse 'Tool(pattern)' or bare 'Tool'."""
    spec = spec.strip()
    if "(" in spec and spec.endswith(")"):
        tool_part, pat_part = spec[:-1].split("(", 1)
        pat = pat_part.strip()
        return Rule(tool=tool_part.strip(), pattern=pat or None)
    return Rule(tool=spec, pattern=None)


_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:*\-]*$")


def validate_rule_spec(spec: str) -> str:
    """The rule text cleaned up, or a ValueError with a plain message. Only used when someone adds a rule;
    rules already in settings files are read with ``parse_rule``, which never refuses."""
    text = (spec or "").strip()
    if not text:
        raise ValueError("A rule cannot be empty. Example: Bash(git status:*)")
    tool, sep, rest = text.partition("(")
    ok = bool(_TOOL_NAME.match(tool.strip()))
    if sep:
        ok = ok and text.endswith(")") and len(text) > len(tool) + 1
    if not ok:
        raise ValueError(f"'{text}' is not a rule. Use a tool name, or a tool name with a pattern in brackets, "
                         "for example Bash(git status:*), Edit(src/**) or WebFetch(domain:docs.python.org).")
    return text


def parse_rules(specs: list[str]) -> list[Rule]:
    return [parse_rule(s) for s in specs if isinstance(s, str) and s.strip()]
