"""
Claude-Code-compatible lifecycle hooks.

Hooks are shell commands that run at well-defined points of the agent lifecycle.
Configuration (all sources are merged, in this order):

1. ``settings.hooks`` (from ``~/.axon/config.toml`` / ``<ws>/.axon/config.toml``)
2. ``~/.axon/settings.json``
3. ``<workspace>/.axon/settings.json``
4. ``<workspace>/.axon/settings.local.json``
5. ``hooks/hooks.json`` of every enabled plugin (source ``plugin:<name>``; ``${AXON_PLUGIN_ROOT}`` /
   ``${CLAUDE_PLUGIN_ROOT}`` expand to the plugin directory and are also set in the hook's env)

JSON format (identical to Claude Code)::

    {"hooks": {"PreToolUse": [
        {"matcher": "Bash|Edit",
         "hooks": [{"type": "command", "command": "python check.py", "timeout": 60}]}
    ]}}

Each command receives a JSON payload on stdin (``session_id``, ``cwd``,
``hook_event_name`` plus event specific keys such as ``tool_name``,
``tool_input``, ``tool_response`` or ``prompt``).

Exit codes:
  * ``0``  success. stdout may contain JSON with ``decision: "block"``,
    ``continue: false``, ``reason``, ``stopReason``, ``systemMessage`` and
    ``hookSpecificOutput`` (``additionalContext``, ``permissionDecision``,
    ``permissionDecisionReason``, ``updatedInput``). For ``UserPromptSubmit`` and
    ``SessionStart`` plain stdout is added as context.
  * ``2``  blocking error. stderr is fed back to the model / user.
  * other  non-blocking error, reported as a warning.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "SessionEnd",
    # Extended lifecycle events (each one is fired by the agent loop / CLI where it really happens).
    "PostToolUseFailure",
    "PostToolBatch",
    "PermissionRequest",
    "PermissionDenied",
    "PostCompact",
    "SubagentStart",
    "TaskCreated",
    "TaskCompleted",
    "StopFailure",
    "InstructionsLoaded",
    "CwdChanged",
    "DirectoryAdded",
    "WorktreeCreate",
    "WorktreeRemove",
)

# Legacy / lowercase spellings accepted in config files.
_EVENT_ALIASES: dict[str, str] = {
    "pre_tool": "PreToolUse",
    "pre_tool_use": "PreToolUse",
    "pretooluse": "PreToolUse",
    "post_tool": "PostToolUse",
    "post_tool_use": "PostToolUse",
    "posttooluse": "PostToolUse",
    "user_prompt_submit": "UserPromptSubmit",
    "userpromptsubmit": "UserPromptSubmit",
    "session_start": "SessionStart",
    "sessionstart": "SessionStart",
    "session_end": "SessionEnd",
    "sessionend": "SessionEnd",
    "stop": "Stop",
    "subagent_stop": "SubagentStop",
    "subagentstop": "SubagentStop",
    "pre_compact": "PreCompact",
    "precompact": "PreCompact",
    "notification": "Notification",
    "post_tool_use_failure": "PostToolUseFailure",
    "permission_request": "PermissionRequest",
    "permission_denied": "PermissionDenied",
    "post_compact": "PostCompact",
    "subagent_start": "SubagentStart",
    "task_completed": "TaskCompleted",
    "stop_failure": "StopFailure",
}
_EVENT_ALIASES.update({e.lower(): e for e in EVENTS})

# Which payload key a matcher is tested against, per event.
_MATCH_FIELD: dict[str, str] = {
    "PreToolUse": "tool_name",
    "PostToolUse": "tool_name",
    "SessionStart": "source",
    "PreCompact": "trigger",
    "SessionEnd": "reason",
    "Notification": "notification_type",
    "PostToolUseFailure": "tool_name",
    "PermissionRequest": "tool_name",
    "PermissionDenied": "tool_name",
    "PostCompact": "trigger",
    "SubagentStart": "agent_type",
    "SubagentStop": "agent_type",
    "StopFailure": "error",
    "InstructionsLoaded": "load_reason",
    "TaskCreated": "task_type",
    "TaskCompleted": "task_type",
}
_TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied")
HOOK_TYPES = ("command", "http")

DEFAULT_TIMEOUT_S = 60
_log = logging.getLogger("axon.hooks")


def normalize_event(name: str) -> str:
    """Map legacy or differently cased event names to the canonical name."""
    raw = str(name or "").strip()
    if raw in EVENTS:
        return raw
    return _EVENT_ALIASES.get(raw.lower(), raw)


def _powershell_hook_command(command: str) -> str:
    """Adapt a hook command written for sh (Claude Code style) to PowerShell.

    A leading quoted path (``"C:\\py\\python.exe" "hook.py"``) is a string expression in PowerShell,
    not a call, so the call operator is added; and PowerShell reduces a native command's exit code
    to 0/1, so the real code is passed on (exit code 2 is how a hook blocks).
    """
    stripped = command.lstrip()
    if stripped[:1] in ('"', "'"):
        command = "& " + stripped
    return command + "\nif ($LASTEXITCODE) { exit $LASTEXITCODE }"


def platform_shell_argv(command: str) -> list[str] | str:
    """Return argv (or a cmd.exe command line) that runs ``command`` in the platform shell.

    Uses ``axon.platform.shell_command_argv``/``default_shell`` when available; otherwise falls
    back to bash/sh on POSIX and PowerShell (or cmd.exe) on Windows.
    """
    try:
        from axon.platform import shell_command_argv, shell_info  # type: ignore[attr-defined]

        if shell_info().is_powershell:
            command = _powershell_hook_command(command)
        argv = shell_command_argv(command)
        if argv:
            return argv
    except (ImportError, AttributeError, Exception):
        pass
    try:
        from axon.platform import default_shell  # type: ignore[attr-defined]

        prefix = list(default_shell())
        if prefix:
            return prefix + [command]
    except (ImportError, AttributeError, Exception):
        pass
    if sys.platform == "win32":
        for exe in ("pwsh", "powershell"):
            found = shutil.which(exe)
            if found:
                return [found, "-NoProfile", "-NonInteractive", "-Command", command]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    for exe in ("bash", "sh"):
        found = shutil.which(exe)
        if found:
            return [found, "-c", command]
    return ["sh", "-c", command]


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        from axon.platform import kill_process_tree  # type: ignore[attr-defined]

        kill_process_tree(proc)
        return
    except (ImportError, AttributeError, Exception):
        pass
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            proc.kill()
    except Exception:
        pass


@dataclass
class HookCommand:
    event: str
    matcher: str
    command: str
    timeout: int = DEFAULT_TIMEOUT_S
    source: str = "settings"
    type: str = "command"                     # command | http
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    allowed_env_vars: list[str] = field(default_factory=list)
    run_async: bool = False                   # fire-and-forget (result ignored)
    plugin_root: str = ""                     # set for hooks contributed by a plugin

    def matches(self, value: str | None) -> bool:
        m = (self.matcher or "").strip()
        if m in ("", "*"):
            return True
        if value is None:
            return True
        try:
            return re.fullmatch(m, value) is not None
        except re.error:
            return m == value


@dataclass
class HookResult:
    """Aggregated outcome of all hooks that ran for one event."""

    blocked: bool = False
    message: str = ""
    additional_context: str = ""
    permission_decision: str | None = None  # "allow" | "deny" | "ask"
    updated_input: dict[str, Any] | None = None
    stop: bool = False  # {"continue": false}
    stop_reason: str = ""
    system_message: str = ""
    warnings: list[str] = field(default_factory=list)
    ran: int = 0

    @property
    def proceed(self) -> bool:
        return not self.blocked and not self.stop


_PERMISSION_RANK = {"allow": 1, "ask": 2, "deny": 3}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            data = json.loads(text) if text.strip() else {}
            return data if isinstance(data, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _iter_groups(event_cfg: Any) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    """Yield (matcher, [hook dicts]) from Claude-style groups or legacy flat specs."""
    if not isinstance(event_cfg, list):
        return
    for group in event_cfg:
        if hasattr(group, "model_dump"):
            group = group.model_dump()
        if not isinstance(group, dict):
            continue
        matcher = str(group.get("matcher") or group.get("tool") or "")
        inner = group.get("hooks")
        if isinstance(inner, list) and inner:
            hooks = [h.model_dump() if hasattr(h, "model_dump") else h for h in inner]
            yield matcher, [h for h in hooks if isinstance(h, dict)]
        elif group.get("command"):
            yield matcher, [{"type": "command", "command": group["command"], "timeout": group.get("timeout")}]


class HookManager:
    """Loads hook configuration and runs hooks for lifecycle events."""

    def __init__(self, settings: Any = None, workspace: Path | str | None = None) -> None:
        self.settings = settings
        ws = workspace if workspace is not None else getattr(settings, "workspace", None)
        self.workspace = Path(ws) if ws is not None else Path.cwd()
        self.session_id: str = ""
        self.transcript_path: str = ""
        self._hooks: list[HookCommand] = []
        self.load_errors: list[str] = []
        self.reload()

    # ── configuration ────────────────────────────────────────────────
    def config_files(self) -> list[tuple[str, Path]]:
        from axon import storage
        user_settings = storage.user_file("settings.json")
        return [
            ("user", user_settings),
            ("project", self.workspace / ".claude" / "settings.json"),
            ("project", self.workspace / ".axon" / "settings.json"),
            ("local", self.workspace / ".claude" / "settings.local.json"),
            ("local", self.workspace / ".axon" / "settings.local.json"),
        ]

    def reload(self) -> None:
        hooks: list[HookCommand] = []
        self.load_errors = []
        if self._all_disabled():
            self._hooks = []
            return
        cfg = getattr(self.settings, "hooks", None) or {}
        if isinstance(cfg, dict):
            self._add_from(cfg, "config.toml", hooks)
        for label, path in self.config_files():
            if not path.is_file():
                continue
            data = _read_json(path)
            if data is None:
                self.load_errors.append(f"Could not parse {path}")
                continue
            section = data.get("hooks", {})
            if isinstance(section, dict):
                self._add_from(section, f"{label}:{path}", hooks)
        self._add_plugin_hooks(hooks)
        # Deduplicate identical commands registered for the same event+matcher.
        seen: set[tuple[str, str, str]] = set()
        unique: list[HookCommand] = []
        for h in hooks:
            key = (h.event, h.matcher, h.command)
            if key not in seen:
                seen.add(key)
                unique.append(h)
        self._hooks = unique

    def _add_plugin_hooks(self, out: list[HookCommand]) -> None:
        try:
            from axon.commands.plugins import enabled_plugins, plugin_hooks
            plugins = enabled_plugins(self.workspace)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not disable settings hooks
            self.load_errors.append(f"Could not load plugin hooks: {e}")
            return
        for plug in plugins:
            section = plugin_hooks(plug)
            if not section:
                continue
            start = len(out)
            self._add_from(section, f"plugin:{plug.name}", out)
            for h in out[start:]:
                h.plugin_root = str(plug.path)

    def _all_disabled(self) -> bool:
        if bool(getattr(self.settings, "disable_all_hooks", False)):
            return True
        if os.environ.get("AXON_DISABLE_ALL_HOOKS", "").strip().lower() in ("1", "true", "yes"):
            return True
        for _label, path in self.config_files():
            if path.is_file():
                data = _read_json(path) or {}
                if data.get("disableAllHooks") is True:
                    return True
        return False

    @staticmethod
    def _add_from(section: dict[str, Any], source: str, out: list[HookCommand]) -> None:
        for raw_event, event_cfg in section.items():
            event = normalize_event(raw_event)
            for matcher, items in _iter_groups(event_cfg):
                for h in items:
                    htype = str(h.get("type", "command") or "command")
                    if htype not in HOOK_TYPES:
                        continue
                    try:
                        timeout = int(h.get("timeout") or DEFAULT_TIMEOUT_S)
                    except (TypeError, ValueError):
                        timeout = DEFAULT_TIMEOUT_S
                    if htype == "http":
                        url = str(h.get("url") or "").strip()
                        if not url.lower().startswith(("http://", "https://")):
                            continue
                        headers = h.get("headers") if isinstance(h.get("headers"), dict) else {}
                        allowed = h.get("allowedEnvVars") or h.get("allowed_env_vars") or []
                        out.append(HookCommand(
                            event, matcher, url, max(1, timeout), source, type="http", url=url,
                            headers={str(k): str(v) for k, v in headers.items()},
                            allowed_env_vars=[str(x) for x in allowed] if isinstance(allowed, list) else [],
                        ))
                        continue
                    cmd = str(h.get("command") or "").strip()
                    if not cmd:
                        continue
                    out.append(HookCommand(event, matcher, cmd, max(1, timeout), source,
                                           run_async=bool(h.get("async", False))))

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"event": h.event, "matcher": h.matcher or "*", "command": h.command, "source": h.source,
             "timeout": h.timeout, "type": h.type}
            for h in self._hooks
        ]

    def has(self, event: str) -> bool:
        ev = normalize_event(event)
        return any(h.event == ev for h in self._hooks)

    def matching(self, event: str, payload: dict[str, Any] | None = None) -> list[HookCommand]:
        ev = normalize_event(event)
        payload = payload or {}
        field_name = _MATCH_FIELD.get(ev)
        value = payload.get(field_name) if field_name else None
        if value is None and ev in _TOOL_EVENTS:
            value = payload.get("tool")
        return [h for h in self._hooks if h.event == ev and h.matches(None if value is None else str(value))]

    # ── execution ────────────────────────────────────────────────────
    def run(self, event: str, payload: dict[str, Any] | None = None) -> HookResult:
        ev = normalize_event(event)
        payload = dict(payload or {})
        hooks = self.matching(ev, payload)
        result = HookResult()
        if not hooks:
            return result
        payload.setdefault("session_id", self.session_id)
        payload.setdefault("transcript_path", self.transcript_path)
        payload.setdefault("cwd", str(self.workspace))
        mode = getattr(self.settings, "mode", None)
        if mode:
            payload.setdefault("permission_mode", {"bypass": "bypassPermissions"}.get(str(mode), str(mode)))
        payload["hook_event_name"] = ev
        try:
            stdin_data = json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            stdin_data = "{}"

        messages: list[str] = []
        contexts: list[str] = []
        for hook in hooks:
            result.ran += 1
            self._run_one(hook, ev, stdin_data, result, messages, contexts)
        result.message = "\n".join(m for m in messages if m).strip()
        result.additional_context = "\n".join(c for c in contexts if c).strip()
        return result

    def _run_one(
        self,
        hook: HookCommand,
        event: str,
        stdin_data: str,
        result: HookResult,
        messages: list[str],
        contexts: list[str],
    ) -> None:
        if hook.run_async:
            import threading
            threading.Thread(target=self._exec_command, args=(hook, event, stdin_data, HookResult()),
                             daemon=True, name="axon-hook-async").start()
            return
        if hook.type == "http":
            outcome = self._exec_http(hook, event, stdin_data, result)
        else:
            outcome = self._exec_command(hook, event, stdin_data, result)
        if outcome is None:
            _log.debug("%s %s hook failed: %s", event, hook.type, result.warnings[-1:] or "")
            return
        code, stdout, stderr = outcome
        _log.debug("%s %s hook %r -> exit %s stdout=%r stderr=%r", event, hook.type, hook.command, code,
                   stdout[:500], stderr[:500])
        self._interpret(hook, event, code, stdout, stderr, result, messages, contexts)

    def _exec_command(self, hook: HookCommand, event: str, stdin_data: str,
                      result: HookResult) -> tuple[int, str, str] | None:
        env = dict(os.environ)
        env["AXON_PROJECT_DIR"] = str(self.workspace)
        env["CLAUDE_PROJECT_DIR"] = str(self.workspace)
        env["AXON_HOOK_EVENT"] = event
        if hook.plugin_root:
            env["AXON_PLUGIN_ROOT"] = hook.plugin_root
            env["CLAUDE_PLUGIN_ROOT"] = hook.plugin_root
        cwd = str(self.workspace) if self.workspace.is_dir() else None
        try:
            proc = subprocess.Popen(
                platform_shell_argv(hook.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            result.warnings.append(f"{event} hook failed to start ({hook.command}): {e}")
            return None
        try:
            stdout, stderr = proc.communicate(stdin_data, timeout=hook.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            result.warnings.append(f"{event} hook timed out after {hook.timeout}s: {hook.command}")
            return None
        except Exception as e:
            result.warnings.append(f"{event} hook error ({hook.command}): {e}")
            return None
        return proc.returncode, (stdout or "").strip(), (stderr or "").strip()

    def _exec_http(self, hook: HookCommand, event: str, stdin_data: str,
                   result: HookResult) -> tuple[int, str, str] | None:
        """POST the hook payload as JSON. 2xx -> the body is handled like command stdout;
        other statuses and network errors are non-blocking warnings. Header values may use
        ``$VAR`` / ``${VAR}`` only for variables listed in ``allowedEnvVars``."""
        import urllib.error
        import urllib.request

        def expand(value: str) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1) or m.group(2)
                return os.environ.get(name, "") if name in hook.allowed_env_vars else ""
            return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, value)

        headers = {"Content-Type": "application/json", "User-Agent": "axon-hooks"}
        headers.update({k: expand(v) for k, v in hook.headers.items()})
        req = urllib.request.Request(hook.url, data=stdin_data.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=hook.timeout) as resp:  # noqa: S310 - user-configured URL
                body = resp.read(1_000_000).decode("utf-8", errors="replace")
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as e:
            result.warnings.append(f"{event} http hook {hook.url} returned HTTP {e.code}")
            return None
        except Exception as e:
            result.warnings.append(f"{event} http hook {hook.url} failed: {type(e).__name__}: {e}")
            return None
        if not 200 <= status < 300:
            result.warnings.append(f"{event} http hook {hook.url} returned HTTP {status}")
            return None
        return 0, body.strip(), ""

    def _interpret(self, hook: HookCommand, event: str, code: int, stdout: str, stderr: str,
                   result: HookResult, messages: list[str], contexts: list[str]) -> None:
        if code == 2:
            result.blocked = True
            messages.append(stderr or stdout or f"Blocked by {event} hook: {hook.command}")
            return
        if code != 0:
            detail = stderr or stdout or "no output"
            result.warnings.append(f"{event} hook exited {code} ({hook.command}): {detail[:500]}")
            return

        data: Any = None
        if stdout.startswith("{"):
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            if stdout and event in ("UserPromptSubmit", "SessionStart"):
                contexts.append(stdout)
            return

        reason = str(data.get("reason") or "").strip()
        decision = str(data.get("decision") or "").strip().lower()
        if decision == "block":
            result.blocked = True
            messages.append(reason or f"Blocked by {event} hook: {hook.command}")
        elif decision in ("approve", "allow"):
            self._merge_permission(result, "allow")
        if data.get("continue") is False:
            result.stop = True
            result.stop_reason = str(data.get("stopReason") or reason or "Stopped by hook").strip()
        if data.get("systemMessage"):
            result.system_message = (result.system_message + "\n" + str(data["systemMessage"])).strip()

        specific = data.get("hookSpecificOutput")
        if isinstance(specific, dict):
            ctx = specific.get("additionalContext")
            if ctx:
                contexts.append(str(ctx))
            perm = str(specific.get("permissionDecision") or "").strip().lower()
            # PermissionRequest hooks answer with {"decision": {"behavior": "allow"|"deny", "message": ...}}.
            beh = specific.get("decision")
            if isinstance(beh, dict) and str(beh.get("behavior") or "").lower() in ("allow", "deny"):
                perm = str(beh["behavior"]).lower()
                if beh.get("message") and perm == "deny":
                    specific = {**specific, "permissionDecisionReason": beh.get("message")}
                if isinstance(beh.get("updatedInput"), dict):
                    specific = {**specific, "updatedInput": beh["updatedInput"]}
            if perm in _PERMISSION_RANK:
                self._merge_permission(result, perm)
                perm_reason = str(specific.get("permissionDecisionReason") or "").strip()
                if perm == "deny":
                    messages.append(perm_reason or f"Denied by {event} hook: {hook.command}")
                elif perm_reason:
                    result.system_message = (result.system_message + "\n" + perm_reason).strip()
            upd = specific.get("updatedInput")
            if isinstance(upd, dict):
                result.updated_input = {**(result.updated_input or {}), **upd}

    @staticmethod
    def _merge_permission(result: HookResult, decision: str) -> None:
        cur = result.permission_decision
        if cur is None or _PERMISSION_RANK[decision] > _PERMISSION_RANK.get(cur, 0):
            result.permission_decision = decision
