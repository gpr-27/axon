"""
PermissionEngine: allow / ask / deny decisions across modes and rules.

Evaluation order (first match wins):
  1. Hard invariants (catastrophic shell commands, credential / system paths)  -> deny
  2. Deny rules                                                               -> deny
  3. Plan mode: anything that mutates (except ExitPlanMode/TodoWrite)          -> deny
  3b. Ask rules (``permissions.ask``; beat allow rules and bypass mode)        -> ask
  4. Allow rules (persistent + session)                                       -> allow
  5. Tool ``default_permission == "deny"``                                    -> deny
  6. bypass mode (unless permissions.disableBypassPermissionsMode = "disable") -> allow
  7. Paths outside the workspace and the additional dirs (/add-dir)         -> ask
  8. acceptEdits: file edits                                                  -> allow
  9. Tool ``default_permission`` ("allow" -> allow, otherwise ask)

Rules come from (all merged, deny always beats allow):
  settings.permissions (config.toml files), ~/.axon/settings.json,
  <ws>/.axon/settings.json, <ws>/.axon/settings.local.json, the legacy
  <ws>/.axon/config.toml [permissions] table, and <ws>/.claude/settings(.local).json.
New persistent rules are written to <ws>/.axon/settings.local.json.
"""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from axon.storage import axon_home as _axon_home
from axon.config import Mode, Settings
from axon.permissions.paths import additional_dirs, is_protected_path, is_within, outside_allowlist
from axon.permissions.rules import (EDIT_TOOLS, READ_TOOLS, Rule, all_subcommands, parse_rule, split_shell_commands,
                                    strip_wrappers, tool_paths)

DecisionOutcome = Literal["allow", "ask", "deny"]
RuleKind = Literal["allow", "deny", "ask"]
RULE_KINDS: tuple[str, ...] = ("allow", "ask", "deny")

PLAN_MODE_ALLOWED = frozenset({"ExitPlanMode", "TodoWrite", "Task"})  # Task: subagents get read-only tools in plan mode
PLAN_MODE_ASK = frozenset({"WebFetch", "WebSearch"})  # network reads: allowed to ask in plan mode
READONLY_GIT = frozenset({"status", "diff", "log", "show", "branch", "blame", "remote", "describe", "rev-parse", "ls-files", "shortlog", "tag"})
LOCAL_SETTINGS = Path(".axon") / "settings.local.json"


@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    reason: str
    rule: str | None = None


@dataclass(frozen=True)
class RuleEntry:
    kind: RuleKind
    rule: Rule
    source: str


# ─── Hard invariants ────────────────────────────────────────────────────────

_ROOT_TARGETS = {"/", "/*", "/.", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "$HOME/*", "${HOME}/", "${HOME}/*"}


def _is_root_target(arg: str) -> bool:
    a = arg.strip().strip("'\"")
    if a in _ROOT_TARGETS:
        return True
    if len(a) <= 4 and len(a) >= 2 and a[1] == ":" and a[0].isalpha() and a[2:] in ("", "\\", "/", "\\*", "/*"):
        return True  # C:  C:\  C:/  C:\*
    if a.startswith("/") and a.rstrip("/*") == "":
        return True
    return False


def shell_credential_access(cmd: str, workspace: Path | None = None) -> str | None:
    """Reason string when a shell command names a credential store (~/.ssh, ~/.aws, ~/.axon/.env, ...)
    as an argument or redirection target; the path tools' guard would be bypassed otherwise."""
    from axon.permissions.paths import credential_location
    for sub in all_subcommands(cmd):
        try:
            argv = shlex.split(strip_wrappers(sub), posix=True)
        except ValueError:
            argv = strip_wrappers(sub).split()
        for tok in argv[1:]:
            tok = tok.lstrip("<>0123456789&=").strip()
            if "=" in tok and not tok.startswith(("/", "~", ".")):
                tok = tok.split("=", 1)[1]  # --key=~/.ssh/id_rsa
            if not tok or tok.startswith("-") or not any(c in tok for c in "/\\~.$"):
                continue
            hit = credential_location(os.path.expandvars(tok), workspace)
            if hit:
                return hit
    return None


def catastrophic_command(cmd: str) -> str | None:
    """Return a reason when a shell command is irrecoverably destructive."""
    compact = cmd.replace(" ", "")
    if ":(){:|:&};:" in compact:
        return "fork bomb"
    for sub in all_subcommands(cmd):
        core = strip_wrappers(sub)
        try:
            argv = shlex.split(core, posix=True)
        except ValueError:
            argv = core.split()
        if not argv:
            continue
        prog = os.path.basename(argv[0]).lower()
        if prog in ("rm", "rm.exe"):
            flags = "".join(a.lstrip("-") for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
            long_flags = {a for a in argv[1:] if a.startswith("--")}
            recursive = "r" in flags.lower() or "--recursive" in long_flags
            if recursive and any(_is_root_target(a) for a in argv[1:] if not a.startswith("-")):
                return "recursive removal of a root/home directory"
            if "--no-preserve-root" in long_flags:
                return "rm --no-preserve-root"
        elif prog in ("mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat", "diskpart", "format", "format.com"):
            return f"disk formatting command '{prog}'"
        elif prog == "dd" and any(a.startswith("of=/dev/") for a in argv[1:]):
            return "dd onto a raw device"
        elif prog in ("remove-item", "del", "rd", "rmdir") and any(_is_root_target(a) for a in argv[1:] if not a.startswith(("-", "/"))):
            if any(f.lower() in ("-recurse", "/s", "-r") for f in argv[1:]):
                return "recursive removal of a drive root"
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


def _perm_lists(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return [], []
    allow = [s for s in perms.get("allow", []) or [] if isinstance(s, str)]
    deny = [s for s in perms.get("deny", []) or [] if isinstance(s, str)]
    return allow, deny


def _perm_list(data: dict[str, Any], key: str) -> list[str]:
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict) or not isinstance(perms.get(key), list):
        return []
    return [s for s in perms[key] if isinstance(s, str) and s.strip()]


def _atomic_write(path: Path, text: str) -> None:
    from axon.session.store import atomic_write_text
    atomic_write_text(path, text)


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
        Path.home()
        return [
            ("user", _axon_home() / "settings.json", "json"),
            ("project", ws / ".axon" / "settings.json", "json"),
            ("local", ws / LOCAL_SETTINGS, "json"),
            ("project-config", ws / ".axon" / "config.toml", "toml"),
            ("claude", ws / ".claude" / "settings.json", "json"),
            ("claude-local", ws / ".claude" / "settings.local.json", "json"),
        ]

    def reload(self) -> None:
        entries: list[RuleEntry] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: RuleKind, spec: str, source: str) -> None:
            key = (kind, spec.strip())
            if not spec.strip() or key in seen:
                return
            seen.add(key)
            entries.append(RuleEntry(kind, parse_rule(spec), source))

        cfg = getattr(self.settings, "permissions", None)
        for kind in RULE_KINDS:
            for s in list(getattr(cfg, kind, []) or []):
                add(kind, s, "config")  # type: ignore[arg-type]
        dirs: list[str] = []
        bypass_off = False
        for label, path, fmt in self.rule_sources():
            if not path.is_file():
                continue
            data = _read_toml(path) if fmt == "toml" else _read_json(path)
            for kind in RULE_KINDS:
                for s in _perm_list(data, kind):
                    add(kind, s, label)  # type: ignore[arg-type]
            dirs += _perm_list(data, "additionalDirectories")
            perms_obj = data.get("permissions") if isinstance(data, dict) else None
            if isinstance(perms_obj, dict) and str(perms_obj.get("disableBypassPermissionsMode", "")).lower() in ("disable", "true"):
                bypass_off = True
        self.entries = entries
        self.file_dirs = [Path(d).expanduser() for d in dirs]
        self.bypass_disabled = bypass_off  # permissions.disableBypassPermissionsMode: "disable"

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
        if mode == "bypass" and getattr(self, "bypass_disabled", False):
            mode = "default"  # bypass is locked off by settings
        name = getattr(tool, "name", str(tool))
        args = args if isinstance(args, dict) else {}
        ws = self.workspace
        readonly = bool(getattr(tool, "readonly", False))
        default_perm = getattr(tool, "default_permission", "ask")

        # 1. Hard invariants
        if name == "Bash":
            reason = catastrophic_command(str(args.get("command", "")))
            if reason:
                return Decision("deny", f"Blocked by structural invariant: {reason}.")
            cred = shell_credential_access(str(args.get("command", "")), ws)
            if cred:
                return Decision("deny", f"Access to protected credential location '{cred}' is blocked.")
        paths = tool_paths(name, args)
        for p in paths:
            why = is_protected_path(p, ws)
            if why:
                return Decision("deny", f"Access to {why} is blocked.")

        # 2. Deny rules
        for r in self.deny_rules:
            if r.matches(name, args, ws, for_deny=True):
                return Decision("deny", f"Blocked by deny rule: {r}", str(r))

        # 3. Plan mode is strictly read-only
        if mode == "plan" and not readonly and name not in PLAN_MODE_ALLOWED and name not in PLAN_MODE_ASK and not self._readonly_git(name, args):
            return Decision("deny", f"Plan mode prevents mutating action '{name}'.")

        # 3b. Ask rules beat allow rules (and bypass): the user asked to be consulted
        for r in self.ask_rules:
            if r.matches(name, args, ws, for_deny=True):  # any sub-command matching is enough to ask
                return Decision("ask", f"Ask rule: {r}", str(r))

        # 4. Allow rules
        for r in self.allow_rules:
            if r.matches(name, args, ws):
                return Decision("allow", f"Allowed by rule: {r}", str(r))

        # 5. Tools that are denied by default
        if default_perm == "deny":
            return Decision("deny", f"Tool '{name}' is disabled by default.")

        # 6. Bypass
        if mode == "bypass":
            return Decision("allow", "Bypass mode active.")

        if mode == "plan":
            if name in PLAN_MODE_ALLOWED or default_perm == "allow" or self._readonly_git(name, args):
                return Decision("allow", "Plan mode permits read-only exploration.")
            return Decision("ask", f"'{name}' requires approval.")

        # 7. Outside-workspace paths always need a human
        outside = [p for p in paths if not self._inside_or_allowlisted(p, name)]
        if outside:
            return Decision("ask", f"'{name}' targets a path outside the workspace: {outside[0]}")

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

    def _readonly_git(self, name: str, args: dict[str, Any]) -> bool:
        if name != "Git":
            return False
        sub = str(args.get("subcommand", "")).strip().lower()
        if sub not in READONLY_GIT:
            return False
        extra = [str(a) for a in (args.get("args") or [])]
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
        rule = parse_rule(spec)
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
        return Path(project_dir or self.workspace) / LOCAL_SETTINGS

    def add_rule(self, kind: RuleKind, spec: str, project_dir: Path | None = None) -> Rule:
        """Persist a rule into <ws>/.axon/settings.local.json and activate it."""
        rule = parse_rule(spec)
        spec_s = str(rule)
        path = self.local_settings_path(project_dir)
        data = _read_json(path) if path.is_file() else {}
        perms = data.setdefault("permissions", {})
        if not isinstance(perms, dict):
            perms = data["permissions"] = {}
        lst = perms.setdefault(kind, [])
        if spec_s not in lst:
            lst.append(spec_s)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
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
                    rel = os.path.relpath(p.resolve(), Path(workspace).resolve()).replace("\\", "/")
                    return Rule(sel, rel)
            except ValueError:
                pass
            return Rule(sel, str(p).replace("\\", "/"))
        return Rule(tool_name)

    def grant_for(self, tool: Any, args: dict[str, Any], workspace: Path | None = None) -> Rule:
        """Persist and activate a narrow allow rule for an 'always allow' approval."""
        name = getattr(tool, "name", str(tool))
        ws = Path(workspace or self.workspace)
        rule = self.narrow_rule_for(name, args if isinstance(args, dict) else {}, ws)
        self.add_rule("allow", str(rule), ws)
        return rule
