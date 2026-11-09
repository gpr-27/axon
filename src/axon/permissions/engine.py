"""
PermissionEngine: allow / ask / deny decisions across modes and rules.

Evaluation order (first match wins):
  1. Hard invariants (catastrophic shell commands, credential / system paths)  -> deny
  1a. permissions.blockReads/WritesOutsideWorkingDirectories                   -> deny (every mode)
  2. Deny rules (Bash: also the files a command names)                         -> deny
  3. Plan mode: file edits and other mutating tools                            -> deny (Bash too)
  3a. Critical-path rm / rmdir; MCP tools flagged requiresUserInteraction       -> ask, every mode (hooks and allow rules can't skip it)
  3b. Ask rules (beat allow rules, hooks and bypass mode)                      -> ask
  3c. Network path reads (bypass runs them)                                    -> ask (hooks and allow rules can't skip it)
  3d. Protected-path writes (bypass runs them)                                 -> ask (allow rules can't skip it)
  4. Allow rules (persistent + session)                                       -> allow
  5. Tool ``default_permission == "deny"``                                    -> deny
  6. bypass mode (unless permissions.disableBypassPermissionsMode = "disable") -> allow
  7. Paths outside the workspace and the additional dirs (/add-dir)         -> ask
  7b. Plan mode: read-only tools inside the workspace                         -> allow
  8. acceptEdits: file edits, and mkdir/touch/rm/rmdir/mv/cp/sed in scope     -> allow
  9. Tool ``default_permission`` ("allow" -> allow, otherwise ask)
  dontAsk mode: every "ask" above becomes "deny".

Rules come from (all merged, deny always beats allow): ~/.axon/settings.json, <ws>/.axon/settings.json,
<ws>/.axon/settings.local.json (workspace and git root), the legacy <ws>/.axon/config.toml, <ws>/.claude/settings(.local).json,
--settings, --allowedTools/--disallowedTools and session rules. allow rules and additionalDirectories from repository files
apply only once the folder is trusted (axon.hooks.trust); deny and ask rules always apply.
New persistent rules are written to .axon/settings.local.json at the git root.
"""
from __future__ import annotations

import glob
import itertools
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from axon.storage import axon_home as _axon_home
from axon.config import Mode, Settings
from axon.permissions.paths import additional_dirs, critical_removal_targets, is_network_path, is_protected_path, is_within, outside_allowlist, protected_write
from axon.permissions.rules import (EDIT_TOOLS, READ_TOOLS, Rule, all_subcommands, apply_carve_outs, bare_simple_command, bash_file_targets, git_call, input_redirect_targets, parse_rule, prefix_rule_cannot_approve, primary_param_rule, redirect_targets, split_shell_commands,
                                    strip_allow_wrappers, strip_wrappers, tool_paths, validate_rule_spec)

DecisionOutcome = Literal["allow", "ask", "deny"]
RuleKind = Literal["allow", "deny", "ask"]
RULE_KINDS: tuple[str, ...] = ("allow", "ask", "deny")

# Task: subagents get read-only tools in plan mode. ScheduleWakeup only times a /loop the user started.
PLAN_MODE_ALLOWED = frozenset({"ExitPlanMode", "TodoWrite", "Task", "ScheduleWakeup"})
PLAN_MODE_ASK = frozenset({"WebFetch", "WebSearch"})  # network reads: allowed to ask in plan mode
READONLY_GIT = frozenset({"status", "diff", "log", "show", "branch", "blame", "remote", "describe", "rev-parse", "ls-files", "shortlog", "tag"})
LOCAL_SETTINGS = Path(".axon") / "settings.local.json"


@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    reason: str
    rule: str | None = None
    hard: bool = False            # an ask that a hook's "allow" cannot skip: an ask rule, a critical path, a network read


@dataclass(frozen=True)
class RuleEntry:
    kind: RuleKind
    rule: Rule
    source: str


BLOCK_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "Ls", "LSP", "Lsp"})
BLOCK_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "Patch", "NotebookEdit", "Notebook"})
_warned_rules: set[str] = set()


def _warn_once(text: str) -> None:
    if text not in _warned_rules:
        _warned_rules.add(text)
        from axon.config import _warn
        _warn(text)


# ─── Hard invariants ────────────────────────────────────────────────────────

_ROOT_TARGETS = {"/", "/*", "/.", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "$HOME/*", "${HOME}/", "${HOME}/*"}
_WHOLE_MACHINE_DIRS = {"/users", "/home", "/applications", "/library", "/usr", "/var", "/opt", "/private", "/volumes", "/system"}


def _is_root_target(arg: str) -> bool:
    a = arg.strip().strip("'\"")
    if a in _ROOT_TARGETS:
        return True
    if len(a) <= 4 and len(a) >= 2 and a[1] == ":" and a[0].isalpha() and a[2:] in ("", "\\", "/", "\\*", "/*"):
        return True  # C:  C:\  C:/  C:\*
    if a.startswith("/") and a.rstrip("/*") == "":
        return True
    if "/" + a.strip("/*").lower().lstrip("/") in _WHOLE_MACHINE_DIRS and a.count("/") <= 2 and a.startswith("/"):
        return True         # /Users, /home, /usr ...: every user's files or the whole system
    return False


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_MAX_NESTED_SHELLS = 4
_MAX_GLOB_MATCHES = 500
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_BRACES = re.compile(r"\{([^{}]*,[^{}]*)\}")
_DECODER = r"(?:base64\s+(?:-d|-D|--decode)\b|base32\s+(?:-d|--decode)\b|xxd\s+-r\b|openssl\s+(?:enc\s+)?(?:base64\s+)?-d\b)"
_SHELL_NAME = r"(?:/\S*/)?(?:ba|z|da|k|fi)?sh\b"
_DECODED_EXEC = (
    re.compile(_DECODER + r"[^;\n]*\|\s*(?:sudo\s+)?" + _SHELL_NAME),                 # base64 -d | sh
    re.compile(r"(?:^|[;&|\s])eval\b[^;\n]*" + _DECODER),                             # eval "$(... base64 -d)"
    re.compile(r"\b" + _SHELL_NAME + r"\s+(?:-\w+\s+)*[\"']?(?:\$\(|<\(|`)[^;\n]*" + _DECODER),  # bash -c "$(... base64 -d)"
)


def _shell_payload(argv: list[str]) -> str | None:
    """The command text a ``bash -c '...'`` or ``eval ...`` will run, else None."""
    prog = os.path.basename(argv[0]).lower()
    if prog == "eval":
        return " ".join(argv[1:]) or None
    if prog not in _SHELLS:
        return None
    for i, arg in enumerate(argv[1:], start=1):
        if not arg.startswith("-"):
            return None  # a script file: its contents are not visible here
        if not arg.startswith("--") and "c" in arg:
            return argv[i + 1] if i + 1 < len(argv) else None
    return None


def _simple_commands(cmd: str, _depth: int = 0) -> list[list[str]]:
    """The argv of every simple command in a line, in order, including ``bash -c`` and ``eval`` bodies."""
    out: list[list[str]] = []
    for sub in all_subcommands(cmd):
        core = strip_wrappers(sub).strip().lstrip("({ ").rstrip(") ")
        try:
            argv = shlex.split(core, posix=True)
        except ValueError:
            argv = core.split()
        if not argv:
            continue
        out.append(argv)
        payload = _shell_payload(argv)
        if payload and _depth < _MAX_NESTED_SHELLS:
            out += _simple_commands(payload, _depth + 1)
    return out


def _expand_braces(tok: str) -> list[str]:
    variants, pending = [], [tok]
    while pending and len(variants) + len(pending) < 64:
        cur = pending.pop()
        m = _BRACES.search(cur)
        if not m:
            variants.append(cur)
            continue
        pending += [cur[:m.start()] + choice + cur[m.end():] for choice in m.group(1).split(",")]
    return variants + pending


def _reads_whole_tree(argv: list[str]) -> bool:
    """True for commands that read every file below a directory argument, hidden ones included."""
    prog = os.path.basename(argv[0]).lower()
    short = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
    long_flags = {a.split("=", 1)[0] for a in argv[1:] if a.startswith("--")}
    if prog in ("grep", "egrep", "fgrep"):
        return bool(set(short) & set("rR")) or bool(long_flags & {"--recursive", "--dereference-recursive"})
    if prog == "rg":  # ripgrep skips hidden files unless told otherwise
        return "." in short or "uu" in short or bool(long_flags & {"--hidden", "--unrestricted"})
    if prog == "cp":
        return bool(set(short) & set("rRa")) or bool(long_flags & {"--recursive", "--archive"})
    if prog == "scp":
        return "r" in short
    return prog in ("tar", "zip", "7z", "rsync")


def _dynamic_credential(tok: str) -> str | None:
    """A path whose start is only known at run time (``$(echo ~)/.ssh/x``), judged by its literal tail."""
    if not any(c in tok for c in "$`()"):
        return None
    from axon.permissions.paths import _CREDENTIAL_PARTS
    parts = tok.replace("\\", "/").split("/")
    for part in parts:
        if part in _CREDENTIAL_PARTS:
            return part
    if parts[-1:] == [".env"] and parts[-2:-1] == [_axon_home().name]:
        return ".axon/.env"
    return None


def shell_credential_access(cmd: str, workspace: Path | None = None) -> str | None:
    """Reason string when a shell command reaches a credential store (~/.ssh, ~/.aws, ~/.axon/.env, ...)
    through an argument, redirection, glob, ``cd``, shell variable, nested shell or recursive read;
    the path tools' guard would be bypassed otherwise. This reads the command text, so it cannot see
    what a script or interpreter does at run time."""
    from axon.permissions.paths import credential_location, holds_credentials
    start = Path(workspace) if workspace is not None else Path.cwd()
    cwd = start
    variables: dict[str, str] = {}

    def expand(tok: str) -> str:
        tok = _VAR_REFERENCE.sub(lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), tok)
        return os.path.expandvars(tok)

    for argv in _simple_commands(cmd):
        assignment = _ASSIGNMENT.match(argv[0])
        if assignment and len(argv) == 1:
            variables[assignment.group(1)] = expand(assignment.group(2))
            continue
        prog = os.path.basename(argv[0]).lower()
        recursive = _reads_whole_tree(argv)
        # "cat<file" arrives as one word, so the program word can hold a redirection target too
        first = [re.split(r"[<>]", argv[0])[-1]] if re.search(r"[<>]", argv[0]) else []
        for raw in first + argv[1:]:
            tok = raw.lstrip("<>0123456789&=").strip()
            if "=" in tok and not tok.startswith(("/", "~", ".")):
                tok = tok.split("=", 1)[1]  # --key=~/.ssh/id_rsa
            if not tok or tok.startswith("-"):
                continue
            if not any(c in tok for c in "/\\~.$*?[{"):
                # A bare word is a file in the current folder; it only matters when it is a symlink, which a cloned
                # repository can point at ~/.ssh/id_rsa (``cat link``).
                try:
                    if not (cwd / tok).is_symlink():
                        continue
                except (OSError, ValueError):
                    continue
            for variant in _expand_braces(expand(tok)):
                candidates = [variant]
                if any(c in variant for c in "*?["):
                    try:
                        matches = glob.iglob(os.path.expanduser(variant), root_dir=str(cwd))
                        candidates += list(itertools.islice(matches, _MAX_GLOB_MATCHES))
                    except (OSError, ValueError, re.error):
                        pass
                for candidate in candidates:
                    hit = _dynamic_credential(candidate) or credential_location(candidate, cwd)
                    if not hit and recursive:
                        hit = holds_credentials(candidate, cwd)
                    if hit:
                        return hit
        if recursive and prog in ("grep", "egrep", "fgrep", "rg") and cwd != start:
            hit = holds_credentials(cwd)  # with no path argument these search the current directory
            if hit:
                return hit
        if prog in ("cd", "pushd"):
            targets = [a for a in argv[1:] if not a.startswith("-")]
            target = expand(targets[0]) if targets else "~"
            if not any(c in target for c in "$`()"):
                cwd = cwd / os.path.expanduser(target)
    return None


def catastrophic_command(cmd: str) -> str | None:
    """Return a reason when a shell command is irrecoverably destructive, or hides what it runs."""
    compact = cmd.replace(" ", "")
    if ":(){:|:&};:" in compact:
        return "fork bomb"
    if any(p.search(cmd) for p in _DECODED_EXEC):
        return "decoded payload run by a shell, so its contents cannot be checked"
    for argv in _simple_commands(cmd):
        prog = os.path.basename(argv[0]).lower()
        if prog in ("rm", "rm.exe"):
            flags = "".join(a.lstrip("-") for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
            long_flags = {a for a in argv[1:] if a.startswith("--")}
            recursive = "r" in flags.lower() or "--recursive" in long_flags
            if recursive and any(_is_root_target(a) for a in argv[1:] if not a.startswith("-")):
                return "recursive removal of a root/home directory"
            if "--no-preserve-root" in long_flags:
                return "rm --no-preserve-root"
        elif prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in argv[1:]) and any(
                _is_root_target(a) for a in argv[1:] if not a.startswith("-")):
            if "-delete" in argv or any(x in ("rm", "shred", "unlink") for x in argv):
                return "find that deletes everything below a root/home directory"
        elif prog in ("chmod", "chown", "chgrp") and any(_is_root_target(a) for a in argv[1:] if not a.startswith("-")):
            flags = "".join(a.lstrip("-") for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
            if "R" in flags or "--recursive" in argv:
                return f"recursive {prog} of a root/home directory"
        elif prog == "mv" and any(_is_root_target(a) for a in argv[1:-1] if not a.startswith("-")):
            return "moving a root/home directory away"
        elif prog in ("mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat", "diskpart", "format", "format.com"):
            return f"disk formatting command '{prog}'"
        elif prog == "dd" and any(a.startswith("of=/dev/") for a in argv[1:]):
            return "dd onto a raw device"
        elif prog in ("remove-item", "del", "rd", "rmdir") and any(_is_root_target(a) for a in argv[1:] if not a.startswith(("-", "/"))):
            if any(f.lower() in ("-recurse", "/s", "-r") for f in argv[1:]):
                return "recursive removal of a drive root"
    return None


def critical_removal(cmd: str, workspace: Path, extra_dirs: list[Path] | None = None) -> str | None:
    """The target when a shell line removes the filesystem root, a top-level directory, the home folder, the workspace
    or a parent of it, or a glob below an additional directory. Other commands aren't checked."""
    exact, under_glob = critical_removal_targets(workspace, extra_dirs)
    variables: dict[str, str] = {}
    for argv in _simple_commands(cmd):
        prog = os.path.basename(argv[0]).lower()
        if prog not in ("rm", "rmdir"):
            continue
        for raw in argv[1:]:
            if raw.startswith("-"):
                continue
            expanded = os.path.expanduser(os.path.expandvars(raw))
            glob_tail = expanded.endswith(("/*", "/**"))
            base = expanded.rstrip("*").rstrip("/") or "/"
            target = base if os.path.isabs(base) else os.path.join(str(workspace), base)
            try:
                real = str(Path(target).resolve())
            except (OSError, RuntimeError):
                real = os.path.normpath(target)
            parent = os.path.dirname(real)
            if real == os.path.dirname(real) or parent == os.path.dirname(parent) or real in exact or (glob_tail and real in under_glob):
                return raw
    del variables
    return None


# ─── Settings files ─────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _perm_list(data: dict[str, Any], key: str) -> list[str]:
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict) or not isinstance(perms.get(key), list):
        return []
    return [s for s in perms[key] if isinstance(s, str) and s.strip()]


def _atomic_write(path: Path, text: str) -> None:
    from axon.session.store import atomic_write_text
    atomic_write_text(path, text)


def _tee_targets(sub: str) -> list[str]:
    core = strip_wrappers(bare_simple_command(sub))
    try:
        words = shlex.split(core)
    except ValueError:
        return []
    if not words or words[0].rsplit("/", 1)[-1] != "tee":
        return []
    return [w for w in words[1:] if not w.startswith("-")]


def _escape_path(path: str) -> str:
    """A generated rule matches only the literal path that was approved."""
    return re.sub(r"([\\\[\]*?!])", r"\\\1", path)


def _exclude_local_settings_from_git(path: Path) -> None:
    """The first write in a repository that doesn't already ignore the file adds it to the global git excludes."""
    import subprocess
    line = "**/.axon/settings.local.json"
    try:
        safe = ["git", "-c", "core.fsmonitor=false"]
        inside = subprocess.run(safe + ["rev-parse", "--is-inside-work-tree"], cwd=str(path.parent), capture_output=True, text=True, timeout=5)
        if inside.stdout.strip() != "true":
            return
        if subprocess.run(safe + ["check-ignore", "-q", str(path)], cwd=str(path.parent), capture_output=True, timeout=5).returncode == 0:
            return
        configured = subprocess.run(["git", "config", "--global", "core.excludesFile"], capture_output=True, text=True, timeout=5).stdout.strip()
        if configured.startswith(("/", "~")):
            target = Path(configured).expanduser()
        else:
            target = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "ignore"
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if line not in existing.splitlines():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(("" if not existing or existing.endswith("\n") else "\n") + line + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


class PermissionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session_rules: list[RuleEntry] = []
        self.reload()

    # ── rule sources ──
    @property
    def workspace(self) -> Path:
        return Path(self.settings.workspace)

    def rule_sources(self) -> list[tuple[str, Path, str]]:
        """(label, path, format) for every file rules are read from."""
        ws = self.workspace
        return [
            ("user", _axon_home() / "settings.json", "json"),
            ("project", ws / ".axon" / "settings.json", "json"),
            ("local", ws / LOCAL_SETTINGS, "json"),
            ("local-root", self.local_settings_path(), "json"),
            ("project-config", ws / ".axon" / "config.toml", "toml"),
            ("claude", ws / ".claude" / "settings.json", "json"),
            ("claude-local", ws / ".claude" / "settings.local.json", "json"),
            ("cli", Path(str(getattr(self.settings, "settings_file", None) or "")) if getattr(self.settings, "settings_file", None) else ws / ".axon" / ".no-settings-file", "json"),
        ]

    def _is_home_folder(self, base: str) -> bool:
        try:
            ws_dir = (self.workspace / base).resolve()
            return ws_dir == (_axon_home() if base == ".axon" else Path.home() / base).resolve()
        except OSError:
            return False

    def _anchor(self, label: str, path: Path) -> Path | None:
        if label == "user":
            return _axon_home()
        if label == "cli":
            return path.parent
        return None                                # the workspace

    def reload(self) -> None:
        from axon.hooks import trust as _trust
        entries: list[RuleEntry] = []
        seen: set[tuple[str, str, str]] = set()
        held: list[tuple[str, str]] = []

        def add(kind: RuleKind, spec: str, source: str, anchor: Path | None, from_file: bool) -> None:
            text = spec.strip()
            key = (kind, text, source)
            if not text or key in seen:
                return
            seen.add(key)
            rule = parse_rule(text)
            if from_file and rule.pattern and rule.tool.startswith("mcp__"):
                _warn_once(f"Skipping rule '{text}' from {source} settings: a parameter rule on an MCP tool works only with --disallowedTools.")
                return
            self._warn_about_rule(kind, rule, text)
            entries.append(RuleEntry(kind, Rule(rule.tool, rule.pattern, anchor), source))

        dirs: list[str] = []
        user_dirs: list[str] = []
        bypass_off = block_reads = block_writes = False
        loaded: set[str] = set()
        grants_ok: bool | None = None
        axon_home_here, claude_home_here = self._is_home_folder(".axon"), self._is_home_folder(".claude")
        for label, path, fmt in self.rule_sources():
            if not path.is_file() or (label != "user" and label != "cli" and str(path) in loaded):
                continue
            if (axon_home_here and label in ("project", "local", "local-root", "project-config")) or (claude_home_here and label in ("claude", "claude-local")):
                continue                           # ~/.axon is your own settings folder, not a project's
            loaded.add(str(path))
            data = _read_toml(path) if fmt == "toml" else _read_json(path)
            anchor = self._anchor(label, path)
            for kind in ("deny", "ask"):
                for spec in _perm_list(data, kind):
                    add(kind, spec, label, anchor, True)  # type: ignore[arg-type]
            repo_supplied = label in ("project", "project-config", "claude") or (
                label in ("local", "local-root", "claude-local") and not _trust.local_file_is_personal(path))
            if repo_supplied and (_perm_list(data, "allow") or _perm_list(data, "additionalDirectories")):
                if grants_ok is None:
                    grants_ok = _trust.grants_allowed(self.workspace)
                if not grants_ok:
                    held += [("allow", x) for x in _perm_list(data, "allow")]
                    held += [("additionalDirectories", x) for x in _perm_list(data, "additionalDirectories")]
            if not repo_supplied or grants_ok:
                for spec in _perm_list(data, "allow"):
                    add("allow", spec, label, anchor, True)
                extra = _perm_list(data, "additionalDirectories")
                dirs += extra
                if not repo_supplied:
                    user_dirs += extra
            perms_obj = data.get("permissions") if isinstance(data, dict) else None
            if isinstance(perms_obj, dict):
                if str(perms_obj.get("disableBypassPermissionsMode", "")).lower() in ("disable", "true"):
                    bypass_off = True
                block_reads = block_reads or perms_obj.get("blockReadsOutsideWorkingDirectories") is True
                block_writes = block_writes or perms_obj.get("blockWritesOutsideWorkingDirectories") is True
        known = {(e.kind, str(e.rule)) for e in entries}
        cfg = getattr(self.settings, "permissions", None)
        for kind in RULE_KINDS:                    # user config.toml, --allowedTools, --disallowedTools, inline --settings
            for spec in list(getattr(cfg, kind, []) or []):
                if (kind, str(parse_rule(spec))) not in known:
                    add(kind, spec, "config", None, False)  # type: ignore[arg-type]
        self.entries = entries
        self.held_grants = held
        self.file_dirs = [Path(d).expanduser() for d in dirs]
        self.user_file_dirs = [Path(d).expanduser() for d in user_dirs]
        self.bypass_disabled = bypass_off  # permissions.disableBypassPermissionsMode: "disable"
        self.block_reads = block_reads
        self.block_writes = block_writes

    def _warn_about_rule(self, kind: str, rule: Rule, text: str) -> None:
        if primary_param_rule(rule.tool, rule.pattern):
            _warn_once(f"Ignoring rule '{text}': that parameter is the tool's own content. Use {rule.tool}(...) with the tool's normal syntax.")
        elif kind == "allow" and rule.tool == "Bash" and re.match(r"^\S+ \*\s*\S", (rule.pattern or "").strip()):
            _warn_once(f"Allow rule '{text}' has a * before the subcommand, so it covers every subcommand. Put the * after the subcommand.")
        elif kind == "allow" and "*" in rule.tool and not rule.tool.lower().startswith("mcp__"):
            _warn_once(f"Skipping allow rule '{text}': an allow rule may glob a tool name only after mcp__<server>__.")

    def warn_unknown_rule_tools(self, known_names: Any) -> None:
        """A deny or ask rule naming no known tool is usually a typo. Names with _ or * are exempt, as are removed tools."""
        known = {str(n).lower() for n in known_names} | {"bash", "read", "edit", "agent", "cd", "skill", "write", "websearch", "webfetch", "powershell", "taskoutput"}
        for e in self.entries:
            if e.kind != "allow" and "_" not in e.rule.tool and "*" not in e.rule.tool and e.rule.tool.lower() not in known:
                _warn_once(f"The {e.kind} rule '{e.rule}' names no known tool, so it never applies. Check the spelling.")

    def removes_tool(self, name: str) -> bool:
        """A bare deny rule (Bash, Bash(*), mcp__*) takes the tool out of what the model is told about."""
        return any(e.kind == "deny" and (not e.rule.pattern or e.rule.pattern.strip() == "*") and e.rule.matches(name, {}, self.workspace, kind="deny")
                   for e in self.entries + self.session_rules)

    @property
    def allow_rules(self) -> list[Rule]:
        return [e.rule for e in self.entries + self.session_rules if e.kind == "allow"]

    @property
    def ask_rules(self) -> list[Rule]:
        return [e.rule for e in self.entries + self.session_rules if e.kind == "ask"]

    def working_dirs(self) -> list[Path]:
        """Extra directories treated like the workspace (/add-dir, --add-dir, additionalDirectories)."""
        ws = self.workspace
        extra = [Path(p).expanduser() if Path(p).expanduser().is_absolute() else ws / Path(p)
                 for p in list(getattr(self.settings, "additional_dirs", []) or []) + list(getattr(self, "file_dirs", []))]
        return additional_dirs(extra)

    @property
    def deny_rules(self) -> list[Rule]:
        return [e.rule for e in self.entries + self.session_rules if e.kind == "deny"]

    def list_rules(self) -> list[RuleEntry]:
        return list(self.entries) + list(self.session_rules)

    # ── decision ──
    def check(self, tool: Any, args: dict[str, Any], mode: Mode | None = None) -> Decision:
        mode = mode or self.settings.mode
        decision = self._check(tool, args, mode)
        if mode == "dontAsk" and decision.outcome == "ask":
            return Decision("deny", f"dontAsk mode denies anything that would prompt. {decision.reason}", decision.rule)
        return decision

    def _groups(self, kind: str) -> list[list[Rule]]:
        """Rules of one kind, one list per settings source: a ! pattern reaches only rules from its own source."""
        groups: dict[str, list[Rule]] = {}
        for e in self.entries + self.session_rules:
            if e.kind == kind:
                groups.setdefault(e.source, []).append(e.rule)
        return list(groups.values())

    def _restricting_rule(self, kind: str, name: str, args: dict[str, Any], server: str | None = None) -> Rule | None:
        ws = self.workspace
        for group in self._groups(kind):
            hit = apply_carve_outs(group, name, args, ws, kind) if server is None else next(
                (r for r in group if not r.negated and r.matches(name, args, ws, kind=kind, server=server)), None)
            if hit:
                return hit
        if name != "Bash":
            return None
        # Read and Edit rules also cover the files a command names: cat, sed, tee, cp and redirection targets.
        reads, writes = bash_file_targets(str(args.get("command", "")))
        for group in self._groups(kind):
            for tool_name, selector, targets in (("Read", "Read", reads), ("Edit", "Edit", writes), ("Edit", "Read", writes if kind == "deny" else [])):
                for target in targets:
                    if any(c in target for c in "$`"):
                        continue
                    hit = next((r for r in group if r.pattern and not r.negated and r.tool == selector
                                and r.matches(tool_name, {"file_path": target}, ws, kind="deny" if selector == "Read" and tool_name == "Edit" else kind)), None)
                    if hit:
                        return hit
        return None

    def _block_dirs(self) -> list[Path]:
        ws = self.workspace
        extra = [Path(p).expanduser() if Path(p).expanduser().is_absolute() else ws / Path(p)
                 for p in list(getattr(self.settings, "additional_dirs", []) or []) + list(getattr(self, "user_file_dirs", []))]
        return additional_dirs(extra)

    def _outside_block(self, raw: str) -> bool:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return not (is_within(self.workspace, p) or any(is_within(d, p) for d in self._block_dirs()))

    def _is_settings_file(self, raw: str) -> bool:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p.name in ("settings.json", "settings.local.json") and p.parent.name in (".axon", ".claude") or p.resolve() == (_axon_home() / "settings.json").resolve()

    def _check(self, tool: Any, args: dict[str, Any], mode: Mode) -> Decision:
        if mode == "bypass" and getattr(self, "bypass_disabled", False):
            mode = "default"  # bypass is locked off by settings
        name = getattr(tool, "name", str(tool))
        args = args if isinstance(args, dict) else {}
        ws = self.workspace
        readonly = bool(getattr(tool, "readonly", False))
        default_perm = getattr(tool, "default_permission", "ask")
        server = getattr(tool, "server_name", None) or getattr(tool, "server", None)
        server = server if isinstance(server, str) else None
        command = str(args.get("command", "")) if name == "Bash" else ""

        # 1. Hard invariants
        if name == "Bash":
            reason = catastrophic_command(command)
            if reason:
                return Decision("deny", f"Blocked by structural invariant: {reason}.")
            cred = shell_credential_access(command, ws)
            if cred:
                return Decision("deny", f"Access to protected credential location '{cred}' is blocked.")
        paths = tool_paths(name, args, keep_prefixed=True)
        for p in paths:
            if "\x00" in str(p):
                return Decision("deny", "The path contains a NUL character (a zero byte), which a file name cannot hold. "
                                        "Send the path again without it.")
            why = is_protected_path(p, ws)
            if why:
                return Decision("deny", f"Access to {why} is blocked.")

        # 1a. The file tools stay inside the working directories, in every mode, when a settings file says so.
        if getattr(self, "block_reads", False) and name in BLOCK_READ_TOOLS:
            for p in paths:
                if self._outside_block(p) and not any(is_within(a, Path(p).expanduser()) for a in outside_allowlist()):
                    return Decision("deny", f"permissions.blockReadsOutsideWorkingDirectories refuses reads outside the working directories: {p}. "
                                            "Ask the user to add the directory with /add-dir.")
        if getattr(self, "block_writes", False) and name in BLOCK_WRITE_TOOLS:
            for p in paths:
                if self._is_settings_file(p) or self._outside_block(p):
                    return Decision("deny", f"permissions.blockWritesOutsideWorkingDirectories refuses this write: {p}. "
                                            "Settings files are always refused while it is on; edit them yourself.")

        # 2. Deny rules
        hit = self._restricting_rule("deny", name, args, server)
        if hit:
            return Decision("deny", f"Blocked by deny rule: {hit}", str(hit))

        # 3. Plan mode denies every mutating tool, shell commands included.
        if mode == "plan" and not readonly and name not in PLAN_MODE_ALLOWED and name not in PLAN_MODE_ASK and not self._readonly_git(name, args):
            return Decision("deny", f"Plan mode prevents mutating action '{name}'.")

        # 3a. No allow rule, hook or mode approves these.
        if command:
            target = critical_removal(command, ws, [d for d in self.working_dirs()])
            if target:
                return Decision("ask", f"This removes a critical path: {target}", hard=True)
        # Tools whose permission prompt is the point (an MCP server marked the tool
        # _meta["anthropic/requiresUserInteraction"]): a person answers every call, in every mode.
        if getattr(tool, "requires_user_interaction", False) is True:
            return Decision("ask", f"'{name}' requires your approval on every call (set by its MCP server).", hard=True)
        if mode == "dontAsk" and name == "AskUserQuestion":
            return Decision("deny", "dontAsk mode never waits for an answer, so AskUserQuestion is denied.")

        # 3b. Ask rules beat allow rules, hooks and bypass: the user asked to be consulted
        hit = self._restricting_rule("ask", name, args, server)
        if hit:
            return Decision("ask", f"Ask rule: {hit}", str(hit), hard=True)

        # 3c. Looking up a network path can contact the host it names.
        if mode != "bypass" and name in READ_TOOLS and any(is_network_path(p) for p in paths):
            return Decision("ask", f"'{name}' reads from a network path, which always needs your approval.", hard=True)

        # 3d. Protected paths: allow rules don't pre-approve these writes; only bypass mode runs them unprompted.
        if mode != "bypass" and name in EDIT_TOOLS:
            for p in paths:
                why = protected_write(p, ws)
                if why:
                    return Decision("ask", f"'{name}' writes to the protected path {why}: {p}")

        # 4. Allow rules
        for r in self.allow_rules:
            if r.matches(name, args, ws, kind="allow", server=server):
                if name == "Bash" and r.pattern and r.pattern.strip() != "*" and self._redirects_outside(r, args):
                    continue        # `Bash(git diff *)` must not let `git diff > ~/.bashrc` through: ask
                return Decision("allow", f"Allowed by rule: {r}", str(r))

        # 5. Tools that are denied by default
        if default_perm == "deny":
            return Decision("deny", f"Tool '{name}' is disabled by default.")

        # 6. Bypass
        if mode == "bypass":
            return Decision("allow", "Bypass mode active.")

        # 7. Outside-workspace paths always need a human (plan mode included: read-only exploration
        #    is free inside the workspace, not across the whole disk)
        outside = [p for p in paths if not self._inside_or_allowlisted(p, name)]
        if outside:
            return Decision("ask", f"'{name}' targets a path outside the workspace: {outside[0]}")

        if mode == "plan":
            if name in PLAN_MODE_ALLOWED or default_perm == "allow" or self._readonly_git(name, args):
                return Decision("allow", "Plan mode permits read-only exploration.")
            return Decision("ask", f"'{name}' requires approval.")

        # 8. acceptEdits
        if mode == "acceptEdits" and name in EDIT_TOOLS:
            return Decision("allow", "acceptEdits mode allows file edits inside the workspace.")

        if self._readonly_git(name, args):
            return Decision("allow", "Read-only git command.")

        # 9. Tool default
        if default_perm == "allow":
            return Decision("allow", f"'{name}' is allowed by default.")
        if mode == "acceptEdits":
            return Decision("ask", "acceptEdits requires confirmation for shell/network commands.")
        return Decision("ask", f"Command or modification '{name}' requires user approval.")

    def _redirects_outside(self, rule: Rule, args: dict[str, Any]) -> bool:
        """True when a prefix/glob allow rule matched a command that redirects its output to a file outside the
        workspace or to a protected path. A rule that is the whole command line was approved as it stands."""
        cmd = str(args.get("command", ""))
        if " ".join((rule.pattern or "").split()) == " ".join(cmd.split()):
            return False
        _reads, tee_and_writes = bash_file_targets(cmd)
        tees = [t for s in all_subcommands(cmd) for t in _tee_targets(s)]
        for target in redirect_targets(cmd) + [t for t in tees if t in tee_and_writes]:
            if target in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                continue
            if target.startswith("~") or any(c in target for c in "*?["):
                return True         # a ~ or glob target needs your approval: the shell decides what it becomes
            target = os.path.expandvars(target)
            if "$" in target or is_protected_path(target, self.workspace) or protected_write(target, self.workspace) \
                    or not self._inside_or_allowlisted(target, "Bash"):
                return True
        for target in input_redirect_targets(cmd):
            if target == "/dev/null":
                continue
            if any(c in target for c in "*?[$") or not self._inside_or_allowlisted(target, "Bash"):
                return True
        return False

    def _readonly_git(self, name: str, args: dict[str, Any]) -> bool:
        if name != "Git":
            return False
        sub, extra, _folder = git_call(args)
        if sub not in READONLY_GIT:
            return False
        if sub in ("branch", "tag", "remote") and any(not a.startswith("-") or a in ("-d", "-D", "--delete", "-m", "-M") for a in extra):
            return sub == "remote" and all(a in ("-v", "--verbose") for a in extra)
        return True

    def _inside_or_allowlisted(self, raw: str, tool_name: str) -> bool:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        if is_within(self.workspace, p):
            return True
        if any(is_within(d, p) for d in self.working_dirs()):
            return True
        if tool_name in READ_TOOLS:
            return any(is_within(a, p) for a in outside_allowlist())
        return False

    # ── rule management ──
    def add_session_rule(self, kind: RuleKind, spec: str) -> Rule:
        rule = parse_rule(validate_rule_spec(spec))
        self.session_rules.append(RuleEntry(kind, rule, "session"))
        return rule

    def remove_session_rule(self, kind: RuleKind, spec: str) -> bool:
        spec_n = str(parse_rule(spec))
        for e in list(self.session_rules):
            if e.kind == kind and str(e.rule) == spec_n:
                self.session_rules.remove(e)
                return True
        return False

    def local_settings_path(self, project_dir: Path | None = None) -> Path:
        from axon.config import repo_root_for_local_settings
        return repo_root_for_local_settings(Path(project_dir or self.workspace)) / LOCAL_SETTINGS

    def add_rule(self, kind: RuleKind, spec: str, project_dir: Path | None = None) -> Rule:
        """Persist a rule into <ws>/.axon/settings.local.json and activate it."""
        rule = parse_rule(validate_rule_spec(spec))
        spec_s = str(rule)
        path = self.local_settings_path(project_dir)
        from axon.session.store import read_json_object_strict
        data = read_json_object_strict(path)      # ValueError (file untouched) when it cannot be parsed
        perms = data.setdefault("permissions", {})
        if not isinstance(perms, dict):
            perms = data["permissions"] = {}
        lst = perms.setdefault(kind, [])
        if spec_s not in lst:
            lst.append(spec_s)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if created:
            _exclude_local_settings_from_git(path)
        if not any(e.kind == kind and str(e.rule) == spec_s for e in self.entries):
            self.entries.append(RuleEntry(kind, rule, "local"))
        return rule

    def remove_rule(self, kind: RuleKind, spec: str) -> list[str]:
        """Remove a rule from every writable source. Returns the labels it was removed from."""
        target = str(parse_rule(spec))
        removed: list[str] = []
        if self.remove_session_rule(kind, spec):
            removed.append("session")
        for label, path, fmt in self.rule_sources():
            if not path.is_file():
                continue
            if fmt == "json":
                data = _read_json(path)
                perms = data.get("permissions")
                if isinstance(perms, dict) and isinstance(perms.get(kind), list):
                    new = [s for s in perms[kind] if not (isinstance(s, str) and str(parse_rule(s)) == target)]
                    if len(new) != len(perms[kind]):
                        perms[kind] = new
                        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                        removed.append(label)
            else:
                data = _read_toml(path)
                perms = data.get("permissions")
                if isinstance(perms, dict) and isinstance(perms.get(kind), list):
                    new = [s for s in perms[kind] if not (isinstance(s, str) and str(parse_rule(s)) == target)]
                    if len(new) != len(perms[kind]):
                        perms[kind] = new
                        try:
                            import tomli_w
                            _atomic_write(path, tomli_w.dumps(data))
                            removed.append(label)
                        except Exception:
                            pass
        before = len(self.entries)
        self.entries = [e for e in self.entries if not (e.kind == kind and str(e.rule) == target)]
        if len(self.entries) != before and "config" not in removed and not removed:
            removed.append("config (until restart)")
        return removed

    def grant_persistent(self, rule: Rule, project_dir: Path) -> None:
        """Persist an 'always allow' rule (backward compatible API)."""
        self.add_rule("allow", str(rule), project_dir)

    @staticmethod
    def narrow_rule_for(tool_name: str, args: dict[str, Any], workspace: Path) -> Rule:
        """The narrowest useful rule that would allow this exact kind of call again."""
        if tool_name == "Bash":
            cmd = " ".join(str(args.get("command", "")).split())
            subs = split_shell_commands(cmd)
            if len(subs) == 1 and cmd and not any(c in cmd for c in "`$<>"):
                try:
                    argv = shlex.split(cmd)
                except ValueError:
                    argv = cmd.split()
                if argv:
                    words = [argv[0]]
                    if len(argv) > 1 and not argv[1].startswith("-") and "/" not in argv[1] and "\\" not in argv[1] and "." not in argv[1]:
                        words.append(argv[1])
                    if prefix_rule_cannot_approve(strip_allow_wrappers(cmd)):
                        return Rule("Bash", cmd)  # a prefix rule can't approve an exec wrapper or a find action
                    return Rule("Bash", " ".join(words) + ":*")
            return Rule("Bash", cmd)  # exact full command
        if tool_name == "Git":
            sub = str(args.get("subcommand", "")).strip()
            return Rule("Git", f"{sub}:*") if sub else Rule("Git")
        if tool_name in ("WebFetch", "Http"):
            from urllib.parse import urlparse
            host = urlparse(str(args.get("url", ""))).hostname
            return Rule(tool_name, f"domain:{host}") if host else Rule(tool_name)
        paths = tool_paths(tool_name, args)
        if paths and (tool_name in EDIT_TOOLS or tool_name in READ_TOOLS):
            p = Path(paths[0]).expanduser()
            if not p.is_absolute():
                p = Path(workspace) / p
            sel = "Edit" if tool_name in EDIT_TOOLS else "Read"
            try:
                if is_within(workspace, p):
                    rel = _escape_path(os.path.relpath(p.resolve(), Path(workspace).resolve()).replace("\\", "/"))
                    # A bare name would match at any depth, so the rule says which file it means.
                    return Rule(sel, rel if "/" in rel else "./" + rel)
            except ValueError:
                pass
            return Rule(sel, "/" + _escape_path(str(p).replace("\\", "/")))  # //abs: / alone anchors at the settings file
        return Rule(tool_name)

    def grant_for(self, tool: Any, args: dict[str, Any], workspace: Path | None = None) -> Rule:
        """Persist and activate a narrow allow rule for an 'always allow' approval."""
        name = getattr(tool, "name", str(tool))
        ws = Path(workspace or self.workspace)
        call = args if isinstance(args, dict) else {}
        rule = self.narrow_rule_for(name, call, ws)
        self.add_rule("allow", str(rule), ws)
        return rule
