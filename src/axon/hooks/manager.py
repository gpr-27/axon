"""
Claude-Code-compatible lifecycle hooks.

Hooks are shell commands, HTTP endpoints, MCP tool calls, LLM prompts or agent checks that run at
well-defined points of the agent lifecycle. Configuration (all sources are merged, in this order):

1. ``settings.hooks`` (from ``~/.axon/config.toml`` / ``<ws>/.axon/config.toml``)
2. ``~/.axon/settings.json``
3. ``<workspace>/.claude/settings.json`` and ``<workspace>/.axon/settings.json``
4. ``<workspace>/.claude/settings.local.json`` and ``<workspace>/.axon/settings.local.json``
5. ``hooks/hooks.json`` of every enabled plugin (source ``plugin:<name>``; ``${AXON_PLUGIN_ROOT}`` /
   ``${CLAUDE_PLUGIN_ROOT}`` expand to the plugin directory and are also set in the hook's env)
6. hooks registered at runtime with :meth:`HookManager.add_scoped` (skills, subagents)

JSON format (identical to Claude Code)::

    {"hooks": {"PreToolUse": [
        {"matcher": "Bash|Edit",
         "hooks": [{"type": "command", "if": "Bash(git *)", "command": "python check.py", "timeout": 60}]}
    ]}}

Handler types: ``command`` (stdin JSON; ``args`` selects exec form without a shell, ``shell`` picks
bash or powershell, ``async``/``asyncRewake`` run in the background), ``http`` (POST), ``mcp_tool``,
``prompt`` (one LLM call answering ``{"ok": bool, "reason": str}``) and ``agent``. Common fields:
``if`` (permission-rule syntax, tool events only), ``timeout``, ``statusMessage``, ``once``,
``onFailure`` (``"continue"`` | ``"block"``). Matching hooks of one event run in parallel.

Each command receives a JSON payload on stdin (``session_id``, ``transcript_path``, ``cwd``,
``permission_mode``, ``hook_event_name`` plus event specific keys such as ``tool_name``,
``tool_input``, ``tool_response`` or ``prompt``).

Exit codes:
  * ``0``  success. stdout may be one JSON object with ``continue``, ``stopReason``,
    ``systemMessage``, ``terminalSequence``, ``decision``/``reason`` and ``hookSpecificOutput``
    (``additionalContext``, ``permissionDecision``, ``permissionDecisionReason``, ``updatedInput``,
    ``updatedToolOutput``, ...). For ``UserPromptSubmit``, ``UserPromptExpansion``,
    ``SessionStart`` and ``PostModelSwitch`` plain stdout is added as context. Output that looks like
    JSON but does not parse or validate is a non-blocking error.
  * ``2``  blocking error on events that can block (see ``CAN_BLOCK``); stderr is the reason.
  * other  non-blocking error, reported as a warning (``onFailure: "block"`` turns it into a block).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from axon import interruptible
from axon.waitmarks import waiting

INTERNAL_EVENTS = frozenset({"UserPromptSubmit"})

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
    "UserPromptExpansion",
    "PreModelSwitch",
    "PostModelSwitch",
    # Setup runs once per folder at the first start; FileChanged is polled; Elicitation comes from the MCP layer.
    "Setup",
    "MessageDisplay",
    "TeammateIdle",
    "ConfigChange",
    "FileChanged",
    "Elicitation",
    "ElicitationResult",
)

# Legacy / lowercase spellings accepted in config files.
_EVENT_ALIASES: dict[str, str] = {
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
}
_EVENT_ALIASES.update({e.lower(): e for e in EVENTS})
# snake_case spellings of every event (pre_tool_use, task_created, instructions_loaded, cwd_changed, ...)
_EVENT_ALIASES.update({re.sub(r"(?<!^)(?=[A-Z])", "_", e).lower(): e for e in EVENTS})

# Which payload key a matcher is tested against, per event. Events missing here have no matcher.
_MATCH_FIELD: dict[str, str] = {
    "PreToolUse": "tool_name",
    "PostToolUse": "tool_name",
    "SessionStart": "source",
    "Setup": "trigger",
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
    "UserPromptExpansion": "command_name",
    "PreModelSwitch": "to_model",
    "PostModelSwitch": "to_model",
    "ConfigChange": "source",
    "DirectoryAdded": "source",
    "FileChanged": "file_name",
    "Elicitation": "mcp_server_name",
    "ElicitationResult": "mcp_server_name",
}
_TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied")
HOOK_TYPES = ("command", "http", "mcp_tool", "prompt", "agent")

# Events where exit code 2 / a JSON block stops the action.
CAN_BLOCK: frozenset[str] = frozenset({
    "PreToolUse", "UserPromptSubmit", "UserPromptExpansion", "Stop", "SubagentStop", "TeammateIdle",
    "TaskCreated", "TaskCompleted", "ConfigChange", "PostToolBatch", "PreCompact", "PreModelSwitch",
    "Elicitation", "ElicitationResult", "WorktreeCreate", "WorktreeRemove",
})
# Exit 2 / a JSON block only feeds stderr or the reason back to the model: the action already ran.
_FEEDBACK_EVENTS: frozenset[str] = frozenset({"PostToolUse", "PostToolUseFailure"})
# Events whose JSON output is dropped entirely (only terminalSequence still fires on StopFailure).
_DISCARD_ALL: frozenset[str] = frozenset({"Setup", "InstructionsLoaded", "StopFailure", "SessionEnd"})
_IGNORE_CONTINUE: frozenset[str] = frozenset({
    "Notification", "PreCompact", "PostCompact", "MessageDisplay", "Elicitation", "ElicitationResult",
    "ConfigChange", "WorktreeCreate", "WorktreeRemove", "DirectoryAdded", "CwdChanged", "FileChanged",
    "TaskCreated", "SubagentStart", "PermissionRequest", "PermissionDenied",
})
_IGNORE_SYSTEM_MESSAGE: frozenset[str] = frozenset({
    "Notification", "PreCompact", "PostCompact", "MessageDisplay", "Elicitation", "ElicitationResult",
    "ConfigChange", "WorktreeCreate", "WorktreeRemove",
})
# Plain (non-JSON) stdout becomes model context on these events.
_PLAIN_CONTEXT_EVENTS: frozenset[str] = frozenset({
    "UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PostModelSwitch",
})
_ENV_FILE_EVENTS: frozenset[str] = frozenset({"SessionStart", "Setup", "CwdChanged", "FileChanged"})
_LLM_EVENTS: frozenset[str] = frozenset({
    "PermissionDenied", "PostToolBatch", "PostToolUse", "PostToolUseFailure", "PreToolUse", "Stop",
    "SubagentStop", "TaskCompleted", "TaskCreated", "TeammateIdle", "UserPromptExpansion", "UserPromptSubmit",
})
_NO_REMOTE_TYPES: dict[str, frozenset[str]] = {   # event -> handler types it does NOT run
    "SessionStart": frozenset({"http", "prompt", "agent"}),
    "Setup": frozenset({"http", "prompt", "agent"}),
}
# Events where exit code 2 and stderr are dropped without a notice.
_SILENT_EXIT2: frozenset[str] = frozenset({
    "Setup", "Notification", "InstructionsLoaded", "StopFailure", "PermissionDenied", "MessageDisplay",
    "DirectoryAdded",
})
_ONCE_PREFIX = "scoped:skill:"       # source prefix of hooks registered from skill frontmatter
_TOP_ALSO = ("additionalContext", "sessionTitle", "watchPaths", "initialUserMessage", "displayContent",
             "worktreePath", "reloadSkills", "retry", "suppressOriginalPrompt")
_OUTPUT_FIELDS = frozenset({"continue", "stopReason", "suppressOutput", "systemMessage", "terminalSequence",
                            "decision", "reason", "hookSpecificOutput", *_TOP_ALSO})
# Claude Code's built-in tool and agent names where Axon's own spelling differs.
_TOOL_ALIASES = {"Task": "Agent"}
AGENT_TYPE_NAMES = {"explore": "Explore", "plan": "Plan"}

DEFAULT_TIMEOUT_S = 600
PROMPT_TIMEOUT_S = 30
AGENT_TIMEOUT_S = 60
SESSION_END_TIMEOUT_S = 1.5
OUTPUT_CAP = 10_000
OUTPUT_PREVIEW = 2_000
MAX_PARALLEL_LLM = 12
_log = logging.getLogger("axon.hooks")

_EXACT = re.compile(r"^[A-Za-z0-9_\-\s,|]*$")
_EXACT_NARROW = re.compile(r"^[A-Za-z0-9_|]*$")
_TERMINAL_SEQ = re.compile(r"(?:\x1b\](?:0|1|2|9|99|777);[^\x07\x1b]*(?:\x07|\x1b\\)|\x07)+")


def normalize_event(name: str) -> str:
    """Map legacy or differently cased event names to the canonical name."""
    raw = str(name or "").strip()
    if raw in EVENTS:
        return raw
    return _EVENT_ALIASES.get(raw.lower(), raw)


def default_timeout(htype: str, event: str) -> float:
    """Claude Code's default timeout (seconds) for a handler type on an event."""
    if htype == "prompt":
        return PROMPT_TIMEOUT_S
    if htype == "agent":
        return AGENT_TIMEOUT_S
    if event in ("UserPromptSubmit", "PreModelSwitch", "PostModelSwitch"):
        return 30
    if event == "MessageDisplay":
        return 10
    if event == "SessionEnd":
        return _session_end_env() or SESSION_END_TIMEOUT_S
    return DEFAULT_TIMEOUT_S


def _session_end_env() -> float:
    """AXON_SESSIONEND_HOOKS_TIMEOUT_MS / CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS in seconds (0: not set)."""
    for name in ("AXON_SESSIONEND_HOOKS_TIMEOUT_MS", "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"):
        try:
            return max(0.1, float(os.environ[name]) / 1000.0)
        except (KeyError, ValueError):
            continue
    return 0.0


def matcher_matches(matcher: str | None, value: str | None, event: str = "") -> bool:
    """Claude Code matcher semantics: ``*``/empty match all; letters, digits, ``_``, ``-``, spaces,
    ``,`` and ``|`` form an exact-string list; anything else is an unanchored regular expression.
    ``FileChanged`` and ``StopFailure`` only treat letters, digits, ``_`` and ``|`` as exact."""
    m = (matcher or "").strip()
    if m in ("", "*") or value is None:
        return True
    narrow = event in ("FileChanged", "StopFailure")
    if (_EXACT_NARROW if narrow else _EXACT).match(m):
        seps = r"\|" if narrow else r"[|,]"
        return value in [p.strip() for p in re.split(seps, m)]
    try:
        return re.search(m, value) is not None     # JavaScript RegExp.test: a match anywhere in the value
    except re.error:
        return m == value


def valid_terminal_sequence(seq: str) -> bool:
    """True when ``seq`` is made only of allowlisted OSC 0/1/2/9/99/777 sequences and BEL."""
    return bool(seq) and _TERMINAL_SEQ.fullmatch(seq) is not None


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
    except Exception:
        pass
    try:
        from axon.platform import default_shell  # type: ignore[attr-defined]

        prefix = list(default_shell())
        if prefix:
            return prefix + [command]
    except Exception:
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


# How long the output of a hook that has exited is still read: a process it left behind may hold the pipe open.
_EXIT_GRACE_S = 0.5


def _read_hook_output(proc: subprocess.Popen, stdin_data: str, timeout: float | None) -> tuple[str, str, bool]:
    """(stdout, stderr, suspect). Reads until the pipes close, or shortly after the hook's own process exited
    (``suspect``: a background process still holds them). Raises subprocess.TimeoutExpired at the timeout."""
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    pending: str | None = stdin_data
    exited_at: float | None = None
    while True:
        try:
            out, err = proc.communicate(pending, timeout=0.1)
            return out or "", err or "", False
        except subprocess.TimeoutExpired as e:
            pending = None
            now = time.monotonic()
            if proc.poll() is not None:
                exited_at = exited_at if exited_at is not None else now
                if now - exited_at >= _EXIT_GRACE_S:
                    def text(b: Any) -> str:
                        return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else (b or "")
                    return text(e.output), text(e.stderr), True
            if deadline is not None and now >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout or 0.0) from None


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        from axon.platform import kill_process_tree  # type: ignore[attr-defined]

        kill_process_tree(proc)
        return
    except Exception:
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
    command: str                              # command line, URL, prompt text or "server:tool" (display)
    timeout: float = DEFAULT_TIMEOUT_S
    source: str = "settings"
    type: str = "command"                     # command | http | mcp_tool | prompt | agent
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    allowed_env_vars: list[str] = field(default_factory=list)
    run_async: bool = False                   # fire-and-forget; output is delivered on a later turn
    plugin_root: str = ""                     # set for hooks contributed by a plugin
    if_cond: str = ""                         # permission-rule syntax, e.g. "Bash(git *)"; tool events only
    args: list[str] | None = None             # exec form: `command` is the executable, no shell
    shell: str = ""                           # "bash" | "powershell" | "" (platform default)
    on_failure: str = ""                      # "block" | "continue" | "" (event default)
    status_message: str = ""
    once: bool = False
    async_rewake: bool = False                # background; exit code 2 wakes the model
    prompt: str = ""                          # prompt / agent hooks
    model: str = ""
    continue_on_block: bool = False
    server: str = ""                          # mcp_tool hooks
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    explicit_timeout: bool = False

    def matches(self, value: str | None) -> bool:
        return matcher_matches(self.matcher, value, self.event)

    @property
    def key(self) -> tuple[Any, ...]:
        """Identity for de-duplication: the same handler in several settings files runs once;
        a plugin's or skill's copy stays separate."""
        scope = self.source if self.source.startswith(("plugin:", "scoped:")) else ""
        return (self.event, self.matcher, self.type, self.command, tuple(self.args or ()), self.if_cond,
                self.prompt, self.server, self.tool, scope)


@dataclass
class HookResult:
    """Aggregated outcome of all hooks that ran for one event."""

    blocked: bool = False
    message: str = ""
    additional_context: str = ""
    permission_decision: str | None = None  # "allow" | "ask" | "defer" | "deny"
    permission_reason: str = ""
    updated_input: dict[str, Any] | None = None
    stop: bool = False  # {"continue": false}
    stop_reason: str = ""
    system_message: str = ""
    warnings: list[str] = field(default_factory=list)
    ran: int = 0
    # Event specific outputs.
    updated_tool_output: Any = None         # PostToolUse: replaces the tool result
    updated_permissions: list[dict[str, Any]] = field(default_factory=list)   # PermissionRequest
    interrupt: bool = False                 # PermissionRequest deny + interrupt
    retry: bool = False                     # PermissionDenied
    terminal_sequence: str = ""             # allowlisted OSC / BEL to emit
    session_title: str = ""
    initial_user_message: str = ""
    suppress_original_prompt: bool = False
    watch_paths: list[str] | None = None
    reload_skills: bool = False
    display_content: str | None = None      # MessageDisplay
    action: str = ""                        # Elicitation / ElicitationResult: accept | decline | cancel
    content: dict[str, Any] | None = None
    worktree_path: str = ""                 # WorktreeCreate
    classifier_context: str = ""
    feedback: str = ""                      # stderr of exit-2 hooks on events that cannot block
    outputs: list[str] = field(default_factory=list)   # raw stdout of every hook (WorktreeCreate path)
    compact_instructions: str = ""          # PreCompact: plain stdout, added to the summary instructions
    ask_source: str = ""                    # where a PreToolUse "ask" came from: settings | skill | plugin:<name>

    @property
    def proceed(self) -> bool:
        return not self.blocked and not self.stop


_PERMISSION_RANK = {"allow": 1, "ask": 2, "defer": 3, "deny": 4}


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


def cap_text(text: str, label: str = "hook output", directory: Path | None = None) -> str:
    """Strings over 10,000 characters are saved to a file (in the session directory when there is one)
    and replaced by its path plus a preview."""
    if len(text) <= OUTPUT_CAP:
        return text
    path = ""
    try:
        d = Path(directory) if directory is not None else Path(tempfile.gettempdir()) / "axon-hook-output"
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="hook-", suffix=".txt", dir=str(d))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        path = name
    except OSError:
        pass
    where = f" saved to {path}" if path else ""
    return f"[{label} was {len(text):,} characters{where}; first {OUTPUT_PREVIEW:,} shown]\n{text[:OUTPUT_PREVIEW]}"


@dataclass
class _Outcome:
    """What running one handler produced, before it is interpreted."""

    code: int | None = None       # exit code (HTTP 2xx -> 0); None when the handler did not run to the end
    stdout: str = ""
    stderr: str = ""
    failure: str = ""             # why it did not run to the end (start error, timeout, HTTP error)
    http: bool = False
    data: dict[str, Any] | None = None   # prompt / agent hooks: already-parsed decision
    timed_out: bool = False
    suspect: bool = False         # the hook exited but something it started still holds its output open


def escape_reminder_tags(text: str) -> str:
    """A hook's text must not open or close the system reminder it is delivered in."""
    return re.sub(r"<(/?system-reminder)>", r"&lt;\1>", text)


_DECISIONS = ("allow", "deny", "ask", "defer", "")
_ACTIONS = ("accept", "decline", "cancel", "")


def _validate(data: dict[str, Any]) -> list[str]:
    """Schema problems of a hook's JSON output (wrong types make the whole output a non-blocking error)."""
    errs: list[str] = []

    def want(obj: dict[str, Any], key: str, kind: type, label: str) -> None:
        if key in obj and obj[key] is not None and not isinstance(obj[key], kind):
            errs.append(f"{label}{key} must be of type {kind.__name__}")

    for k in ("stopReason", "systemMessage", "terminalSequence", "reason", "decision"):
        want(data, k, str, "")
    for k in ("continue", "suppressOriginalPrompt"):
        want(data, k, bool, "")
    spec = data.get("hookSpecificOutput")
    if spec is not None and not isinstance(spec, dict):
        errs.append("hookSpecificOutput must be an object")
        return errs
    if isinstance(spec, dict):
        for k in ("additionalContext", "permissionDecisionReason", "sessionTitle", "initialUserMessage",
                  "displayContent", "worktreePath", "classifierContext", "hookEventName"):
            want(spec, k, str, "hookSpecificOutput.")
        for k in ("retry", "reloadSkills", "suppressOriginalPrompt"):
            want(spec, k, bool, "hookSpecificOutput.")
        want(spec, "updatedInput", dict, "hookSpecificOutput.")
        want(spec, "watchPaths", list, "hookSpecificOutput.")
        want(spec, "content", dict, "hookSpecificOutput.")
        if str(spec.get("permissionDecision") or "").lower() not in _DECISIONS:
            errs.append("hookSpecificOutput.permissionDecision must be allow, deny, ask or defer")
        if str(spec.get("action") or "").lower() not in _ACTIONS:
            errs.append("hookSpecificOutput.action must be accept, decline or cancel")
        dec = spec.get("decision")
        if dec is not None and not isinstance(dec, dict):
            errs.append("hookSpecificOutput.decision must be an object")
    return errs


def parse_output(stdout: str, lenient: bool = False) -> tuple[str, dict[str, Any] | None, str]:
    """Classify hook stdout: ('empty'|'text'|'json'|'invalid', data, problem). ``lenient`` (async hooks)
    also accepts a one-line JSON object among other text."""
    s = (stdout or "").strip()
    if not s:
        return "empty", None, ""

    def load(text: str) -> tuple[dict[str, Any] | None, str]:
        try:
            d = json.loads(text)
        except json.JSONDecodeError as e:
            return None, f"output is not valid JSON ({e.msg})"
        if not isinstance(d, dict):
            return None, ""
        extra = sorted(set(d) - _OUTPUT_FIELDS)
        if extra:
            _log.debug("Hook JSON output had unrecognized keys: %s", ", ".join(extra))
        errs = _validate(d)
        return (d, "") if not errs else (None, "JSON output failed validation: " + "; ".join(errs))

    def scan() -> dict[str, Any] | None:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                d2, _p = load(line)
                if d2 is not None:
                    return d2
        return None

    if s.startswith("{") and s.endswith("}"):
        d, problem = load(s)
        if d is not None:
            return "json", d, ""
        if lenient:
            d2 = scan()
            if d2 is not None:
                return "json", d2, ""
        lines = [ln for ln in s.splitlines() if ln.strip()]
        if len(lines) >= 2:      # several JSON lines: text, unless one of them is an output object that sets a field
            try:
                objs = [json.loads(ln) for ln in lines]
            except json.JSONDecodeError:
                objs = None
            if objs is not None:
                if any(isinstance(o, dict) and set(o) & _OUTPUT_FIELDS for o in objs):
                    return "invalid", None, "several JSON values on stdout; print one JSON object and nothing else"
                return "text", None, ""
        return ("invalid", None, problem) if problem else ("text", None, "")
    if lenient:
        d2 = scan()
        if d2 is not None:
            return "json", d2, ""
    _log.debug("Hook output does not start with {, treating as plain text")
    return "text", None, ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:500]
    return ""


def _subst_vars(text: str, mapping: dict[str, str]) -> str:
    return re.sub(r"\$\{(CLAUDE_PROJECT_DIR|CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA|AXON_PROJECT_DIR|AXON_PLUGIN_ROOT|AXON_PLUGIN_DATA)\}",
                  lambda m: mapping.get(m.group(1), m.group(0)), text)


def _payload_path(payload: dict[str, Any], dotted: str) -> str:
    cur: Any = payload
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return cur if isinstance(cur, str) else json.dumps(cur, default=str)


def _fill_input(value: Any, payload: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([\w.]+)\}", lambda m: _payload_path(payload, m.group(1)), value)
    if isinstance(value, dict):
        return {k: _fill_input(v, payload) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_input(v, payload) for v in value]
    return value


_ACTIVE: list[Any] = []       # weakrefs to the session's HookManagers (those built with settings)


def active_manager() -> "HookManager | None":
    """The newest live HookManager of a session, for code that has no agent at hand (MCP elicitation,
    settings writers). Managers built without settings (worktree helpers, tests of a single event) are skipped."""
    import weakref  # noqa: F401 - documents that _ACTIVE holds weakrefs
    for ref in reversed(_ACTIVE):
        mgr = ref()
        if mgr is not None:
            return mgr
    return None


def fire(event: str, payload: dict[str, Any] | None = None) -> "HookResult | None":
    """Run ``event`` on the active session's hooks; None when there is no session or no matching hook."""
    mgr = active_manager()
    if mgr is None or not mgr.has(event):
        return None
    try:
        return mgr.run(event, payload)
    except Exception:  # noqa: BLE001 - a hook problem never breaks the action that fired it
        return None


def config_change(path: Path | str, kind: str = "settings") -> None:
    """Fire ConfigChange before Axon writes a settings / MCP file. A hook that blocks (exit 2) stops the
    write: ValueError. ``source`` is user_settings | project_settings | local_settings | mcp_config."""
    p = Path(path)
    try:
        from axon import storage
        user = storage.axon_home() in p.parents
    except Exception:  # noqa: BLE001
        user = False
    if kind == "mcp":
        source = "mcp_config"
    else:
        source = "user_settings" if user else ("local_settings" if ".local." in p.name else "project_settings")
    res = fire("ConfigChange", {"source": source, "file_path": str(p)})
    if res is not None and res.blocked and source != "policy_settings":
        raise ValueError(f"a ConfigChange hook blocked the change to {p}" + (f": {res.message}" if res.message else ""))


class HookManager:
    """Loads hook configuration and runs hooks for lifecycle events."""

    def __init__(self, settings: Any = None, workspace: Path | str | None = None) -> None:
        self.settings = settings
        ws = workspace if workspace is not None else getattr(settings, "workspace", None)
        self.workspace = Path(ws) if ws is not None else Path.cwd()
        self.session_id: str = ""
        self.transcript_path: str = ""
        self.prompt_id: str = ""
        self.cwd: str = ""                    # the model's current directory (defaults to the workspace)
        # Callbacks the agent loop installs: ``llm(prompt, model, timeout, agent) -> reply text`` for
        # prompt/agent hooks, ``mcp_call(server, tool, args) -> (text, is_error)`` for mcp_tool hooks,
        # ``on_async(rewake)`` when a background hook delivered output.
        self.llm: Callable[[str, str | None, float, bool], str] | None = None
        self.mcp_call: Callable[[str, str, dict[str, Any]], tuple[str, bool]] | None = None
        self.on_async: Callable[[bool], None] | None = None
        self._base: list[HookCommand] = []
        self._scoped: dict[str, list[HookCommand]] = {}
        self._hooks: list[HookCommand] = []
        self.load_errors: list[str] = []
        self.allowed_http_urls: list[str] | None = None
        self.http_allowed_env_vars: list[str] | None = None
        self._async_msgs: list[tuple[str, bool]] = []
        self._lock = threading.Lock()
        self._live_procs: set[subprocess.Popen] = set()   # command hooks still running (Esc kills them)
        self._once_done: set[tuple[Any, ...]] = set()     # ``once`` hooks that already ran this session
        self._async_session_text: dict[tuple[Any, ...], str] = {}
        self._start_dir = os.getcwd()
        # Built-in handlers: fn(event, payload) -> {"additional_context": str, "system_message": str}.
        # They run for UserPromptSubmit after the configured hooks (see axon.skills.auto).
        self.internal_handlers: list[Callable[[str, dict[str, Any]], dict[str, str]]] = []
        self._file_snapshot: dict[str, int | None] = {}
        self._dynamic_watch: list[str] = []               # watchPaths returned by hooks
        self._poll_lock = threading.Lock()
        self._launched = False                            # the first SessionStart has run
        if settings is not None:
            import weakref
            _ACTIVE.append(weakref.ref(self, lambda r: _ACTIVE.remove(r) if r in _ACTIVE else None))
        self.reload()
        self._install_internal_handlers()

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
        self.allowed_http_urls = None
        self.http_allowed_env_vars = None
        if self._all_disabled():
            self._base = []
            self._rebuild()
            return
        from axon.hooks import trust
        project_ok = trust.allowed(self.workspace)      # untrusted folder: only user-scope hooks load
        if not project_ok:
            self.load_errors.append(trust.blocked_line(self.workspace))
        cfg = (getattr(self.settings, "hooks", None) or {}) if project_ok else self._user_toml_hooks()
        if isinstance(cfg, dict):
            self._add_from(cfg, "config.toml", hooks)
        for label, path in self.config_files():
            if not path.is_file():
                continue
            if label != "user" and not project_ok:
                continue
            data = _read_json(path)
            if data is None:
                self.load_errors.append(f"Could not parse {path}")
                continue
            section = data.get("hooks", {})
            if isinstance(section, dict):
                self._add_from(section, f"{label}:{path}", hooks)
            urls = data.get("allowedHttpHookUrls")
            if isinstance(urls, list):
                self.allowed_http_urls = (self.allowed_http_urls or []) + [str(u) for u in urls]
            envs = data.get("httpHookAllowedEnvVars")
            if isinstance(envs, list):
                self.http_allowed_env_vars = (self.http_allowed_env_vars or []) + [str(v) for v in envs]
        self._add_plugin_hooks(hooks)
        self._base = hooks
        self._rebuild()

    @staticmethod
    def _user_toml_hooks() -> dict[str, Any]:
        """``hooks`` of ~/.axon/config.toml alone (the merged settings also carry <ws>/.axon/config.toml)."""
        try:
            import tomllib
            from axon import storage
            data = tomllib.loads(storage.user_file("config.toml").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - missing or unreadable: no user hooks
            return {}
        hooks = data.get("hooks")
        return hooks if isinstance(hooks, dict) else {}

    def _rebuild(self) -> None:
        # Deduplicate identical handlers registered for the same event+matcher.
        seen: set[tuple[Any, ...]] = set()
        unique: list[HookCommand] = []
        disabled = self._all_disabled()
        for h in ([] if disabled else self._base + [x for v in self._scoped.values() for x in v]):
            if h.key not in seen:
                seen.add(h.key)
                unique.append(h)
        self._hooks = unique

    def add_scoped(self, section: dict[str, Any], source: str) -> int:
        """Register hooks for the lifetime of a skill or subagent (same format as settings).
        Returns how many handlers were added; :meth:`remove_scoped` drops them again."""
        out: list[HookCommand] = []
        self._add_from(section, f"scoped:{source}", out)
        if out:
            self._scoped.setdefault(source, []).extend(out)
            self._rebuild()
        return len(out)

    def remove_scoped(self, source: str) -> None:
        removed = self._scoped.pop(source, None)
        if removed is not None:
            self._once_done.difference_update(h.key for h in removed)   # registered again: it may run once more
            self._rebuild()

    def _add_plugin_hooks(self, out: list[HookCommand]) -> None:
        try:
            from axon.commands.plugins import enabled_plugins, plugin_hooks
            plugins = enabled_plugins(self.workspace)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not disable settings hooks
            self.load_errors.append(f"Could not load plugin hooks: {e}")
            return
        from axon.hooks import trust
        project_ok = trust.allowed(self.workspace)
        for plug in plugins:
            if plug.scope == "project" and not project_ok:
                continue
            section = plugin_hooks(plug)
            if not section:
                continue
            start = len(out)
            self._add_from(section, f"plugin:{plug.name}", out)
            for h in out[start:]:
                h.plugin_root = str(plug.path)

    def _all_disabled(self) -> bool:
        if os.environ.get("AXON_DISABLE_ALL_HOOKS", "").strip().lower() in ("1", "true", "yes"):
            return True
        disabled = bool(getattr(self.settings, "disable_all_hooks", False))
        # Settings precedence: user < project < local, the last file that sets it wins.
        for _label, path in self.config_files():
            if path.is_file():
                data = _read_json(path) or {}
                if isinstance(data.get("disableAllHooks"), bool):
                    disabled = data["disableAllHooks"]
        return disabled

    def _add_from(self, section: dict[str, Any], source: str, out: list[HookCommand]) -> None:
        for raw_event, event_cfg in section.items():
            event = normalize_event(raw_event)
            for matcher, items in _iter_groups(event_cfg):
                for h in items:
                    hook = self._parse_handler(event, matcher, h, source)
                    if hook is not None:
                        out.append(hook)

    def _parse_handler(self, event: str, matcher: str, h: dict[str, Any], source: str) -> HookCommand | None:
        htype = str(h.get("type", "command") or "command")
        if htype not in HOOK_TYPES:
            return None
        if htype in _NO_REMOTE_TYPES.get(event, ()):
            self.load_errors.append(f"{event} runs command hooks only; skipped a {htype} hook ({source})")
            return None
        if htype == "agent" and event == "PermissionRequest":
            return None
        if htype in ("prompt", "agent") and event not in _LLM_EVENTS and not (htype == "prompt" and event == "PermissionRequest"):
            self.load_errors.append(f"{event} does not support {htype} hooks; skipped ({source})")
            return None
        explicit = False
        timeout: float = default_timeout(htype, event)
        raw_t = h.get("timeout")
        if raw_t not in (None, "", 0, False):
            try:
                t = float(raw_t)
                if t > 0:
                    timeout = min(t, 60.0) if event == "SessionEnd" else t
                    explicit = True
            except (TypeError, ValueError):
                pass
        if float(timeout).is_integer():
            timeout = int(timeout)
        common: dict[str, Any] = dict(
            if_cond=str(h.get("if") or "").strip(),
            on_failure=str(h.get("onFailure") or "").strip().lower(),
            status_message=str(h.get("statusMessage") or ""),
            once=bool(h.get("once", False)),
            explicit_timeout=explicit,
        )
        if common["on_failure"] not in ("block", "continue"):
            common["on_failure"] = ""
        if htype == "http":
            url = str(h.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return None
            headers = h.get("headers") if isinstance(h.get("headers"), dict) else {}
            allowed = h.get("allowedEnvVars") or h.get("allowed_env_vars") or []
            return HookCommand(event, matcher, url, timeout, source, type="http", url=url,
                               headers={str(k): str(v) for k, v in headers.items()},
                               allowed_env_vars=[str(x) for x in allowed] if isinstance(allowed, list) else [], **common)
        if htype in ("prompt", "agent"):
            text = str(h.get("prompt") or "").strip()
            if not text:
                return None
            return HookCommand(event, matcher, text, timeout, source, type=htype, prompt=text,
                               model=str(h.get("model") or ""), continue_on_block=bool(h.get("continueOnBlock", False)),
                               **common)
        if htype == "mcp_tool":
            server, tool = str(h.get("server") or "").strip(), str(h.get("tool") or "").strip()
            if not server or not tool:
                return None
            inp = h.get("input") if isinstance(h.get("input"), dict) else {}
            return HookCommand(event, matcher, f"{server}:{tool}", timeout, source, type="mcp_tool",
                               server=server, tool=tool, input=inp, **common)
        cmd = str(h.get("command") or "").strip()
        if not cmd:
            return None
        args = h.get("args")
        shell = str(h.get("shell") or "").strip().lower()
        return HookCommand(
            event, matcher, cmd, timeout, source,
            run_async=bool(h.get("async", False)) or bool(h.get("asyncRewake", False)),
            args=[str(a) for a in args] if isinstance(args, list) else None,
            shell=shell if shell in ("bash", "powershell") else "",
            async_rewake=bool(h.get("asyncRewake", False)), **common)

    # ── inspection ───────────────────────────────────────────────────
    def describe(self) -> list[dict[str, Any]]:
        return [
            {"event": h.event, "matcher": h.matcher or "*", "command": h.command, "source": h.source,
             "timeout": h.timeout, "type": h.type, "if": h.if_cond, "async": h.run_async,
             "args": list(h.args) if h.args is not None else None, "shell": h.shell,
             "onFailure": h.on_failure, "statusMessage": h.status_message, "model": h.model}
            for h in self._hooks
        ]

    def has(self, event: str) -> bool:
        ev = normalize_event(event)
        if ev in INTERNAL_EVENTS and self.internal_handlers and not self._all_disabled():
            return True
        return any(h.event == ev for h in self._hooks)

    def _install_internal_handlers(self) -> None:
        try:
            from axon.skills.auto import prompt_handler
        except Exception as e:  # noqa: BLE001 - optional built-in
            _log.debug("built-in prompt handler unavailable: %s", e)
            return
        self.internal_handlers.append(prompt_handler(self.workspace))

    def _run_internal(self, ev: str, payload: dict[str, Any], result: HookResult) -> None:
        if ev not in INTERNAL_EVENTS or not self.internal_handlers or self._all_disabled():
            return
        payload = {**payload, "session_id": payload.get("session_id") or self.session_id}
        contexts, messages = [], []
        for handler in self.internal_handlers:
            try:
                out = handler(ev, payload)
            except Exception as e:  # noqa: BLE001 - a built-in helper must never break a prompt
                _log.debug("internal %s handler failed: %s", ev, e)
                continue
            contexts.append(out.get("additional_context", ""))
            messages.append(out.get("system_message", ""))
        extra_context = "\n".join(c for c in contexts if c)
        extra_message = "\n".join(m for m in messages if m)
        if extra_context:
            result.additional_context = "\n".join(c for c in (result.additional_context, extra_context) if c)
        if extra_message:
            result.system_message = "\n".join(m for m in (result.system_message, extra_message) if m)

    def matching(self, event: str, payload: dict[str, Any] | None = None) -> list[HookCommand]:
        ev = normalize_event(event)
        payload = payload or {}
        field_name = _MATCH_FIELD.get(ev)
        if field_name == "file_name":
            fp = payload.get("file_path")
            value: Any = os.path.basename(str(fp)) if fp else payload.get("file_name")
        else:
            value = payload.get(field_name) if field_name else None
        if value is None and ev in _TOOL_EVENTS:
            value = payload.get("tool")
        values = [None] if value is None else [str(value)]
        if value is not None and ev in _TOOL_EVENTS and str(value) in _TOOL_ALIASES:
            values.append(_TOOL_ALIASES[str(value)])
        if value is not None and ev in ("SubagentStart", "SubagentStop"):
            low = {v: k for k, v in AGENT_TYPE_NAMES.items()}.get(str(value))
            values += [low] if low else ([AGENT_TYPE_NAMES[str(value)]] if str(value) in AGENT_TYPE_NAMES else [])
        return [h for h in self._hooks if h.event == ev and not (h.once and h.key in self._once_done)
                and any(h.matches(v) for v in values)]

    def _if_ok(self, hook: HookCommand, event: str, payload: dict[str, Any]) -> bool:
        """``if``: permission-rule syntax, evaluated on tool events only (elsewhere the hook never runs)."""
        if not hook.if_cond:
            return True
        if event not in _TOOL_EVENTS:
            return False
        try:
            from axon.permissions.rules import parse_rule
            args = payload.get("tool_input")
            return parse_rule(hook.if_cond).matches(str(payload.get("tool_name") or ""),
                                                    args if isinstance(args, dict) else {},
                                                    self.workspace, for_deny=True)
        except Exception:  # noqa: BLE001 - when the rule cannot be evaluated the hook runs
            return True

    # ── async delivery ───────────────────────────────────────────────
    def drain_async(self) -> list[str]:
        """Output of finished background hooks (``additionalContext`` / ``systemMessage``, or the
        stderr of an ``asyncRewake`` hook that exited 2), to be shown to the model on its next turn."""
        with self._lock:
            msgs, self._async_msgs = [t for t, _r in self._async_msgs], []
        return msgs

    def has_async_output(self) -> bool:
        with self._lock:
            return bool(self._async_msgs)

    def _push_async(self, text: str, rewake: bool) -> None:
        if not text.strip():
            return
        with self._lock:
            self._async_msgs.append((cap_text(text.strip()), rewake))
        cb = self.on_async
        if cb is not None:
            try:
                cb(rewake)
            except Exception:  # noqa: BLE001
                pass

    def _start_async(self, hook: HookCommand, event: str, payload: dict[str, Any], stdin_data: str) -> None:
        _log.debug("Hooks: %s [%s] moved to the background", event, self._label(hook))

        def work() -> None:
            out = self._execute(hook, event, payload, stdin_data, timeout=hook.timeout if hook.async_rewake else None)
            if out.failure:
                _log.debug("%s async hook failed: %s", event, out.failure)
                return
            if hook.async_rewake and out.code == 2:
                self._push_async(out.stderr or out.stdout, True)
                return
            kind, data, _p = parse_output(out.stdout, lenient=True)
            if kind != "json" or data is None:
                return
            spec = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
            parts = [escape_reminder_tags(str(spec.get("additionalContext") or data.get("additionalContext") or "")),
                     str(data.get("systemMessage") or "")]
            text = "\n".join(p for p in parts if p)
            if event == "SessionStart":       # an async SessionStart hook repeats itself on every start: same text, once
                seen = self._async_session_text.get(hook.key)
                if seen == text:
                    return
                self._async_session_text[hook.key] = text
            self._push_async(text, False)

        threading.Thread(target=work, daemon=True, name="axon-hook-async").start()

    # ── execution ────────────────────────────────────────────────────
    def run(self, event: str, payload: dict[str, Any] | None = None) -> HookResult:
        ev = normalize_event(event)
        if ev == "SessionStart" and (payload or {}).get("source") == "startup":
            self._first_run_setup()
        if ev in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            self.poll_watched_files()
        result = self._run_configured(event, payload)
        self._run_internal(normalize_event(event), dict(payload or {}), result)
        if ev == "SessionStart":
            self._launched = True
        return result

    # Setup: runs once per folder, the first time Axon starts in it (before that session's SessionStart hooks).
    def _first_run_setup(self) -> None:
        try:
            from axon import storage
            key = str(self.workspace.resolve())
            done = storage.read_state("setup-done", {})
            done = done if isinstance(done, dict) else {}
            if key in done:
                return
            done[key] = int(time.time())
            storage.write_state("setup-done", done)
        except Exception:  # noqa: BLE001 - without a marker Setup would repeat every start: skip it instead
            return
        if self.has("Setup"):
            self._run_configured("Setup", {"trigger": "init"})

    # FileChanged: each segment of a matcher (split on ``|``) is a literal file name in the working directory;
    # hooks add absolute paths with ``watchPaths``. They are checked when a prompt is submitted, after each
    # tool and at session start; there is no background watcher.
    def _watched_names(self) -> list[str]:
        names: list[str] = []
        for h in self._hooks:
            if h.event == "FileChanged":
                names += [n.strip() for n in (h.matcher or "").split("|") if n.strip()]
        return list(dict.fromkeys(names))

    def poll_watched_files(self) -> None:
        if not self.has("FileChanged") and not self._dynamic_watch:
            return
        if not self._poll_lock.acquire(blocking=False):
            return
        base = Path(self.cwd or self.workspace)
        try:
            for name in [*self._watched_names(), *self._dynamic_watch]:
                path = base / name
                try:
                    stamp: int | None = path.stat().st_mtime_ns
                except OSError:
                    stamp = None
                key = str(path)
                if key not in self._file_snapshot:
                    self._file_snapshot[key] = stamp          # baseline: the first look is not a change
                    continue
                before = self._file_snapshot[key]
                if stamp == before:
                    continue
                self._file_snapshot[key] = stamp
                kind = "unlink" if stamp is None else ("add" if before is None else "change")
                self._run_configured("FileChanged", {"file_path": key, "file_name": path.name, "event": kind})
        finally:
            self._poll_lock.release()

    def _session_end_budget(self, hooks: list[HookCommand]) -> float:
        """SessionEnd hooks share 1.5 seconds. A longer per-hook timeout in a settings file raises that (up to 60
        seconds; a plugin's does not), and AXON_SESSIONEND_HOOKS_TIMEOUT_MS sets it outright."""
        env = _session_end_env()
        if env:
            return env
        longest = [float(h.timeout) for h in hooks if h.explicit_timeout and not h.source.startswith("plugin:")]
        return min(60.0, max([SESSION_END_TIMEOUT_S, *longest]))

    def _run_configured(self, event: str, payload: dict[str, Any] | None = None) -> HookResult:
        ev = normalize_event(event)
        payload = dict(payload or {})
        result = HookResult()
        hooks = [h for h in self.matching(ev, payload) if self._if_ok(h, ev, payload)]
        if any(h.type == "mcp_tool" for h in hooks) and self._no_mcp_context(ev, payload):
            _log.debug("mcp_tool hooks are not available for the '%s' hook event (no MCP client context)", ev)
            hooks = [h for h in hooks if h.type != "mcp_tool"]
        if not hooks:
            return result
        self._once_done.update(h.key for h in hooks if h.once)
        payload.setdefault("session_id", self.session_id)
        if self.prompt_id:
            payload.setdefault("prompt_id", self.prompt_id)
        payload.setdefault("transcript_path", self.transcript_path)
        payload.setdefault("cwd", self.cwd or str(self.workspace))
        mode = getattr(self.settings, "mode", None)
        if mode:
            payload.setdefault("permission_mode", {"bypass": "bypassPermissions"}.get(str(mode), str(mode)))
        payload["hook_event_name"] = ev
        if ev in _ENV_FILE_EVENTS:
            from axon.hooks import envfile
            envfile.file_for(self.session_id)
            if ev == "CwdChanged":
                envfile.clear()
        try:
            stdin_data = json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            stdin_data = "{}"

        sync: list[HookCommand] = []
        for hook in hooks:
            result.ran += 1
            if hook.run_async and hook.type == "command":
                self._start_async(hook, ev, payload, stdin_data)
            else:
                sync.append(hook)
        sem = threading.Semaphore(MAX_PARALLEL_LLM)
        budget = self._session_end_budget(sync) if ev == "SessionEnd" else None

        def task(h: HookCommand) -> _Outcome:
            if h.type in ("prompt", "agent"):
                with sem:
                    return self._execute(h, ev, payload, stdin_data)
            if budget is not None:
                return self._execute(h, ev, payload, stdin_data, timeout=min(float(h.timeout), budget))
            return self._execute(h, ev, payload, stdin_data)

        if len(sync) <= 1:
            outcomes = [task(h) for h in sync]
        else:
            outcomes = self._run_in_parallel(task, sync)
        messages: list[str] = []
        contexts: list[str] = []
        for hook, out in zip(sync, outcomes):
            _log.debug("%s %s hook %r -> exit %s failure=%r stdout=%r stderr=%r", ev, hook.type, hook.command, out.code,
                       out.failure, out.stdout[:500], out.stderr[:500])
            self._process(hook, ev, out, result, messages, contexts, payload)
        result.message = "\n".join(m for m in messages if m).strip()
        result.additional_context = "\n".join(c for c in contexts if c).strip()
        if result.watch_paths is not None and ev in ("SessionStart", "CwdChanged", "FileChanged"):
            self._dynamic_watch = [p for p in dict.fromkeys(result.watch_paths) if os.path.isabs(p)]
        return result

    def _no_mcp_context(self, event: str, payload: dict[str, Any]) -> bool:
        """Setup, and SessionStart at launch, fire before the session's MCP servers are available."""
        return event == "Setup" or (event == "SessionStart" and not self._launched
                                    and payload.get("source") in ("startup", "resume", "fork"))

    @staticmethod
    def _succeeded(out: _Outcome) -> bool:
        if out.failure or out.timed_out:
            return False
        if out.data is not None:
            return out.data.get("ok") is not False
        return out.code == 0

    def _run_in_parallel(self, task: Callable[[HookCommand], _Outcome], hooks: list[HookCommand]) -> list[_Outcome]:
        """Run the hooks on worker threads and wait for them in short slices, so Esc is not held up."""
        pool = ThreadPoolExecutor(max_workers=min(len(hooks), 32), thread_name_prefix="axon-hook")
        try:
            futures = [pool.submit(task, h) for h in hooks]
            return [interruptible.wait_future(f) for f in futures]
        except KeyboardInterrupt:
            self._kill_running_hooks()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, hook: HookCommand, event: str, payload: dict[str, Any], stdin_data: str,
                 timeout: Any = "hook") -> _Outcome:
        t = hook.timeout if isinstance(timeout, str) else timeout
        try:
            with waiting("hook", event=event):      # the status line under the spinner says what is being waited for
                if hook.type == "http":
                    return self._exec_http(hook, event, stdin_data, t)
                if hook.type == "mcp_tool":
                    return self._exec_mcp(hook, payload, t)
                if hook.type in ("prompt", "agent"):
                    return self._exec_llm(hook, event, stdin_data, t)
                return self._exec_command(hook, event, stdin_data, t, payload)
        except Exception as e:  # noqa: BLE001 - a hook must never take the agent down
            return _Outcome(failure=f"{type(e).__name__}: {e}")

    def _hook_env(self, hook: HookCommand, event: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        if (os.environ.get("AXON_SUBPROCESS_ENV_SCRUB") or os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB")) == "1":
            try:
                from axon.tools.shell import _is_secret_env
                env = {k: v for k, v in env.items() if not _is_secret_env(k)}
            except Exception:  # noqa: BLE001
                pass
        effort = (payload or {}).get("effort")
        if isinstance(effort, dict) and effort.get("level"):
            env["CLAUDE_EFFORT"] = env["AXON_EFFORT"] = str(effort["level"])
        env["AXON_PROJECT_DIR"] = str(self.workspace)
        env["CLAUDE_PROJECT_DIR"] = str(self.workspace)
        env["AXON_HOOK_EVENT"] = event
        if hook.plugin_root:
            env["AXON_PLUGIN_ROOT"] = hook.plugin_root
            env["CLAUDE_PLUGIN_ROOT"] = hook.plugin_root
            try:
                from axon import storage
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", hook.source.split(":", 1)[-1])
                env["CLAUDE_PLUGIN_DATA"] = env["AXON_PLUGIN_DATA"] = str(Path(storage.user_file("plugin-data")) / name)
            except Exception:  # noqa: BLE001
                pass
            env.update({f"CLAUDE_PLUGIN_OPTION_{k.upper()}": v for k, v in self._plugin_options(hook).items()})
            env.update({f"AXON_PLUGIN_OPTION_{k.upper()}": v for k, v in self._plugin_options(hook).items()})
        if event in _ENV_FILE_EVENTS and hook.shell != "powershell":
            from axon.hooks import envfile
            path = envfile.current_path()
            if path is not None:
                env["CLAUDE_ENV_FILE"] = str(path)
                env["AXON_ENV_FILE"] = str(path)
        return env

    def _plugin_options(self, hook: HookCommand) -> dict[str, str]:
        """The plugin's ``userConfig`` values, for ``${user_config.*}`` and CLAUDE_PLUGIN_OPTION_<KEY>."""
        if not hook.source.startswith("plugin:"):
            return {}
        name = hook.source.split(":", 1)[1]
        try:
            from axon.commands.plugins import enabled_plugins, plugin_user_config_values
            for plug in enabled_plugins(self.workspace):
                if plug.name == name:
                    return {str(k): str(v) for k, v in plugin_user_config_values(plug, self.workspace).items()}
        except Exception:  # noqa: BLE001 - no options configured
            pass
        return {}

    def _argv(self, hook: HookCommand, env: dict[str, str]) -> list[str] | str:
        mapping = {k: env[k] for k in ("CLAUDE_PROJECT_DIR", "AXON_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT",
                                       "AXON_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "AXON_PLUGIN_DATA") if k in env}
        if hook.args is not None:                     # exec form: no shell, placeholders substituted
            exe = _subst_vars(hook.command, mapping)
            if not re.search(r"[/\\]", exe) and re.search(r"\s", exe):
                _log.warning("Hook command %r has whitespace and `args`: there is no executable with that name. "
                             "Move the extra tokens into args.", exe)
            options = self._plugin_options(hook)

            def config(text: str) -> str:
                return re.sub(r"\$\{user_config\.([A-Za-z_]\w*)\}", lambda m: options.get(m.group(1), ""), text)
            return [config(exe)] + [config(_subst_vars(a, mapping)) for a in hook.args]
        cmd = hook.command
        if hook.source.startswith("plugin:") and "${user_config." in cmd:
            raise ValueError("a shell-form plugin hook cannot use ${user_config.*}: read $CLAUDE_PLUGIN_OPTION_<KEY> "
                             "or set args to use exec form")
        if hook.shell == "powershell":
            cmd = re.sub(r"\$\{(CLAUDE_PROJECT_DIR|CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA)\}", r"${env:\1}", cmd)
            for exe in ("pwsh", "powershell"):
                found = shutil.which(exe)
                if found:
                    return [found, "-NoProfile", "-NonInteractive", "-Command", _powershell_hook_command(cmd)]
        elif hook.shell == "bash":
            found = shutil.which("bash")
            if found:
                return [found, "-c", cmd]
        return platform_shell_argv(cmd)

    def _hook_cwd(self) -> str | None:
        """The current directory, or the first of these that still exists: the directory the session started
        in, the project root, the home directory, the system temp directory."""
        for i, cand in enumerate((self.cwd, str(self.workspace), self._start_dir, str(Path.home()), tempfile.gettempdir())):
            if cand and os.path.isdir(cand):
                if i and self.cwd:
                    _log.warning("Hook working directory %s no longer exists; running from %s", self.cwd, cand)
                return cand
        return None

    def _exec_command(self, hook: HookCommand, event: str, stdin_data: str, timeout: float | None,
                      payload: dict[str, Any] | None = None) -> _Outcome:
        env = self._hook_env(hook, event, payload)
        cwd = self._hook_cwd()
        started = time.monotonic()
        match = str((payload or {}).get(_MATCH_FIELD.get(event, ""), "") or "")
        title = f"{event}:{match}" if match else event

        def note(how: str) -> None:
            _log.debug("Hooks: %s [%s] %s (%dms)", title, hook.command, how, (time.monotonic() - started) * 1000)
        try:  # own process group, so a timeout / Esc kills the hook's children too, not just its shell
            from axon.platform import new_process_group_kwargs
            group = new_process_group_kwargs()
        except Exception:  # noqa: BLE001
            group = {}
        try:
            proc = subprocess.Popen(
                self._argv(hook, env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                **group,
            )
        except Exception as e:
            note("failed to start")
            return _Outcome(failure=f"failed to start ({hook.command}): {e}")
        with self._lock:
            self._live_procs.add(proc)
        suspect = False
        try:
            stdout, stderr, suspect = _read_hook_output(proc, stdin_data, timeout)
        except subprocess.TimeoutExpired:
            note(f"timed out after {(timeout or 0) * 1000:.0f}ms")
            _kill_tree(proc)
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            return _Outcome(failure=f"timed out after {timeout:g}s ({hook.command})", timed_out=True)
        except KeyboardInterrupt:
            _kill_tree(proc)        # Esc: the hook must not outlive the turn
            raise
        except Exception as e:
            _kill_tree(proc)
            return _Outcome(failure=f"error ({hook.command}): {e}")
        finally:
            with self._lock:
                self._live_procs.discard(proc)
        note(f"finished with status {proc.returncode}")
        return _Outcome(code=proc.returncode, stdout=(stdout or "").strip(), stderr=(stderr or "").strip(), suspect=suspect)

    def _kill_running_hooks(self) -> None:
        """Esc while several hooks ran on worker threads: stop their processes."""
        with self._lock:
            procs = list(self._live_procs)
        for proc in procs:
            _kill_tree(proc)

    def _http_allowed(self, url: str) -> bool:
        if self.allowed_http_urls is None:
            return True
        return any(fnmatch.fnmatchcase(url, pat) for pat in self.allowed_http_urls)

    def _exec_http(self, hook: HookCommand, event: str, stdin_data: str, timeout: float | None) -> _Outcome:
        """POST the hook payload as JSON. A 2xx JSON body is handled like command stdout; other
        statuses and network errors are failures. Header values may use ``$VAR`` / ``${VAR}`` only for
        variables listed in ``allowedEnvVars`` (and in ``httpHookAllowedEnvVars`` when that is set)."""
        import urllib.error
        import urllib.request

        if not self._http_allowed(hook.url):
            return _Outcome(failure=f"http hook {hook.url} is not in allowedHttpHookUrls; skipped")
        allowed = set(hook.allowed_env_vars)
        if self.http_allowed_env_vars is not None:
            allowed &= set(self.http_allowed_env_vars)

        def expand(value: str) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1) or m.group(2)
                return os.environ.get(name, "") if name in allowed else ""
            return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, value)

        headers = {"Content-Type": "application/json", "User-Agent": "axon-hooks"}
        headers.update({k: expand(v) for k, v in hook.headers.items()})
        req = urllib.request.Request(hook.url, data=stdin_data.encode("utf-8"), headers=headers, method="POST")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            """A redirect would carry the custom headers (secrets) to another host and skip allowedHttpHookUrls."""
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                return None      # the 3xx response then surfaces as an HTTPError

        opener = urllib.request.build_opener(_NoRedirect)

        def post() -> tuple[str, int]:
            with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - user-configured URL
                return resp.read(1_000_000).decode("utf-8", errors="replace"), int(getattr(resp, "status", 200) or 200)

        try:
            body, status = interruptible.call(post)     # on a helper thread, so Esc is not held by the socket read
        except urllib.error.HTTPError as e:
            return _Outcome(failure=f"http hook {hook.url} returned HTTP {e.code}", http=True)
        except TimeoutError:
            return _Outcome(failure=f"timed out after {timeout:g}s (http hook {hook.url})", http=True, timed_out=True)
        except Exception as e:
            return _Outcome(failure=f"http hook {hook.url} failed: {type(e).__name__}: {e}", http=True)
        if not 200 <= status < 300:
            return _Outcome(failure=f"http hook {hook.url} returned HTTP {status}", http=True)
        return _Outcome(code=0, stdout=body.strip(), http=True)

    def _exec_mcp(self, hook: HookCommand, payload: dict[str, Any], timeout: float | None = None) -> _Outcome:
        if self.mcp_call is None:
            return _Outcome(failure=f"mcp_tool hook {hook.server}:{hook.tool}: MCP is not available")
        args = _fill_input(hook.input, payload)
        mcp_call = self.mcp_call
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="axon-hook-mcp")
        try:
            text, is_error = interruptible.wait_future(
                pool.submit(mcp_call, hook.server, hook.tool, args if isinstance(args, dict) else {}), timeout=timeout)
        except FuturesTimeout:
            return _Outcome(failure=f"timed out after {timeout:g}s (mcp_tool {hook.server}:{hook.tool})", timed_out=True)
        finally:
            pool.shutdown(wait=False)
        if is_error:
            return _Outcome(failure=f"mcp_tool {hook.server}:{hook.tool} returned an error: {_first_line(text)}")
        return _Outcome(code=0, stdout=(text or "").strip())

    def _exec_llm(self, hook: HookCommand, event: str, stdin_data: str, timeout: float | None) -> _Outcome:
        if self.llm is None:
            return _Outcome(failure=f"{hook.type} hook: no model is available to evaluate it")
        text = hook.prompt.replace("\\$", "\x00")
        text = text.replace("$ARGUMENTS", stdin_data) if "$ARGUMENTS" in text else f"{text}\n\n{stdin_data}"
        text = text.replace("\x00", "$")
        text += ('\n\nRespond with only a JSON object: {"ok": true} to allow, or {"ok": false, "reason": "..."} '
                 'to block' + (', adding "impossible": true when the condition can never be satisfied.'
                               if hook.type == "prompt" else "."))
        try:
            reply = self.llm(text, hook.model or None, float(timeout or PROMPT_TIMEOUT_S), hook.type == "agent")
        except TimeoutError:
            return _Outcome(failure=f"timed out after {timeout:g}s ({hook.type} hook)", timed_out=True)
        data = self._extract_ok(reply or "")
        if data is None:
            return _Outcome(failure=f"{hook.type} hook did not return a JSON decision: {_first_line(reply or '')[:200]}")
        return _Outcome(code=0, stdout=reply or "", data=data)

    @staticmethod
    def _extract_ok(reply: str) -> dict[str, Any] | None:
        for m in re.finditer(r"\{[^{}]*\}", reply, re.S):
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and "ok" in d:
                return d
        return None

    # ── interpretation ───────────────────────────────────────────────
    @staticmethod
    def _label(hook: HookCommand) -> str:
        text = hook.command if hook.type not in ("agent", "prompt") else f"{hook.type}: {hook.prompt[:60]}"
        return text if len(text) <= 120 else text[:117] + "..."

    def _fail(self, hook: HookCommand, event: str, text: str, result: HookResult, messages: list[str],
              timed_out: bool = False) -> None:
        note = f"{event} hook error: [{self._label(hook)}]: {text}"
        result.warnings.append(note)
        if hook.on_failure == "block" and event not in ("Stop", "SubagentStop", "TaskCompleted", "TeammateIdle") \
                and not hook.run_async:
            why = "timed out" if timed_out else "failed"
            msg = f'{note}\n{why}; blocking because onFailure is "block"'
            if event == "PermissionRequest":
                self._merge_permission(result, "deny")
                messages.append(msg)
            elif event in CAN_BLOCK or event in _FEEDBACK_EVENTS:
                result.blocked = True
                messages.append(msg)
        elif timed_out and event == "PreModelSwitch":
            result.blocked = True
            messages.append(note)

    def _cap(self, text: str, label: str = "hook output") -> str:
        """10,000 character cap; the rest goes to a file in the session directory."""
        directory = Path(self.transcript_path).parent / "hook-output" if self.transcript_path else None
        return cap_text(text, label, directory)

    def _process(self, hook: HookCommand, event: str, out: _Outcome, result: HookResult,
                 messages: list[str], contexts: list[str], payload: dict[str, Any] | None = None) -> None:
        if out.suspect and event in ("PreToolUse", "PermissionRequest") and '"allow"' in out.stdout:
            result.blocked = True
            result.permission_decision = "deny"
            messages.append(f"{event} hook error: [{self._label(hook)}]: the hook exited while a process it started still "
                            "holds its output open; refusing to honor the suspect capture. Print the decision first and "
                            "redirect that process's stdout and stderr.")
            return
        if out.data is not None:
            self._apply_llm(hook, event, out.data, result, messages)
            return
        if out.failure:
            self._fail(hook, event, out.failure, result, messages, out.timed_out)
            return
        code, stdout, stderr = out.code, out.stdout, out.stderr
        result.outputs.append(stdout)
        kind, data, problem = parse_output(stdout)
        if code == 2:
            if event == "PermissionRequest":      # exit 2 is not honored here; a JSON decision still is
                if data:
                    self._apply(hook, event, data, result, messages, contexts)
                return
            if event in ("Stop", "SubagentStop", "TaskCompleted") and not stdout \
                    and "No such file or directory" in stderr:
                self._fail(hook, event, f"Failed with non-blocking status code: {_first_line(stderr)}", result, messages)
                return
            before = len(messages)
            if data and event not in ("Elicitation", "ElicitationResult"):
                self._apply(hook, event, data, result, messages, contexts)
            if event in CAN_BLOCK or event in _FEEDBACK_EVENTS:
                result.blocked = True
                if event in ("Elicitation", "ElicitationResult"):
                    result.action = "decline"
                if len(messages) == before:
                    text = stderr or stdout or f"Blocked by {event} hook: {hook.command}"
                    messages.append(text)
            elif stderr and event not in _SILENT_EXIT2:
                result.feedback = (result.feedback + "\n" + stderr).strip()
                result.warnings.append(f"{event} hook: {_first_line(stderr)}")
            return
        if code != 0:
            if kind == "json" and data is not None and hook.on_failure != "block" \
                    and event not in ("Elicitation", "ElicitationResult"):
                self._apply(hook, event, data, result, messages, contexts)
                return
            detail = _first_line(stderr) or _first_line(stdout) or "no output"
            self._fail(hook, event, f"Failed with non-blocking status code: {detail}", result, messages)
            return
        if kind == "invalid":
            self._fail(hook, event, problem, result, messages)
            return
        if kind == "json" and data is not None:
            self._apply(hook, event, data, result, messages, contexts)
            return
        if kind == "text" and out.http:
            self._fail(hook, event, "2xx response body is not a JSON object", result, messages)
            return
        if kind == "text" and event == "PreCompact":
            cut = stdout if len(stdout) <= OUTPUT_CAP else stdout[:OUTPUT_CAP] + "\n[hook output truncated]"
            result.compact_instructions = "\n".join(p for p in (result.compact_instructions, cut) if p)
            return
        if kind == "text" and event in _PLAIN_CONTEXT_EVENTS:
            contexts.append(self._cap(escape_reminder_tags(stdout)))

    def _apply_llm(self, hook: HookCommand, event: str, data: dict[str, Any], result: HookResult,
                   messages: list[str]) -> None:
        if data.get("ok") is not False:
            return
        reason = str(data.get("reason") or "").strip() or "Blocked by a prompt hook"
        cont = hook.continue_on_block or hook.type == "agent"
        if event in ("Stop", "SubagentStop"):
            if data.get("impossible") and hook.type == "prompt":
                return
            result.blocked = True
            messages.append(reason)
        elif event == "PreToolUse":
            self._merge_permission(result, "deny")
            messages.append(reason)
            if not cont:
                result.stop, result.stop_reason = True, reason
        elif event == "PostToolUse":
            if cont:
                result.blocked = True
                messages.append(reason)
            else:
                result.stop, result.stop_reason = True, reason
        elif event in ("PostToolBatch", "UserPromptSubmit", "UserPromptExpansion"):
            result.blocked = True
            messages.append(reason)
            if event == "PostToolBatch":
                result.stop, result.stop_reason = True, reason
        elif event in ("PostToolUseFailure", "TaskCreated", "TaskCompleted"):
            result.blocked = True
            messages.append(reason)
        elif event == "TeammateIdle":
            if cont:
                result.blocked = True
                messages.append(reason)
            else:
                result.stop, result.stop_reason = True, reason

    def _apply(self, hook: HookCommand, event: str, data: dict[str, Any], result: HookResult,
               messages: list[str], contexts: list[str]) -> None:
        ts = data.get("terminalSequence")
        if isinstance(ts, str) and valid_terminal_sequence(ts):
            result.terminal_sequence += ts
        if event in _DISCARD_ALL:
            return
        reason = str(data.get("reason") or "").strip()
        decision = str(data.get("decision") or "").strip().lower()
        if decision == "block" and (event in CAN_BLOCK or event in _FEEDBACK_EVENTS):
            result.blocked = True
            messages.append(reason or f"Blocked by {event} hook: {hook.command}")
            if event in ("Elicitation", "ElicitationResult"):
                result.action = "decline"
        elif decision in ("approve", "allow") and event == "PreToolUse":
            self._merge_permission(result, "allow")
        if data.get("continue") is False and event not in _IGNORE_CONTINUE:
            result.stop = True
            result.stop_reason = str(data.get("stopReason") or reason or "Stopped by hook").strip()
        if data.get("systemMessage") and event not in _IGNORE_SYSTEM_MESSAGE:
            result.system_message = self._cap((result.system_message + "\n" + str(data["systemMessage"])).strip(),
                                              "systemMessage")

        if decision == "block" and event == "PreModelSwitch":
            self._merge_permission(result, "deny")
        hso = data.get("hookSpecificOutput")
        spec: dict[str, Any] = {k: data[k] for k in _TOP_ALSO if k in data}
        if isinstance(hso, dict):
            spec.update(hso)
        ctx = spec.get("additionalContext")
        if ctx:
            contexts.append(self._cap(escape_reminder_tags(str(ctx)), "additionalContext"))
        perm = str(spec.get("permissionDecision") or "").strip().lower()
        # PermissionRequest hooks answer with {"decision": {"behavior": "allow"|"deny", ...}}.
        beh = spec.get("decision")
        if isinstance(beh, dict) and str(beh.get("behavior") or "").lower() in ("allow", "deny"):
            perm = str(beh["behavior"]).lower()
            if beh.get("message") and perm == "deny":
                spec["permissionDecisionReason"] = beh.get("message")
            if isinstance(beh.get("updatedInput"), dict):
                spec["updatedInput"] = beh["updatedInput"]
            if isinstance(beh.get("updatedPermissions"), list):
                result.updated_permissions.extend(p for p in beh["updatedPermissions"] if isinstance(p, dict))
            if beh.get("interrupt") is True and perm == "deny":
                result.interrupt = True
        if perm in _PERMISSION_RANK:
            self._merge_permission(result, perm)
            perm_reason = str(spec.get("permissionDecisionReason") or "").strip()
            if perm_reason:
                result.permission_reason = perm_reason
            if perm == "deny":
                messages.append(perm_reason or f"Denied by {event} hook: {hook.command}")
            elif perm == "ask":
                result.ask_source = ("plugin:" + hook.source.split(":", 1)[1] if hook.source.startswith("plugin:")
                                     else "skill" if hook.source.startswith(_ONCE_PREFIX) else "settings")
                if perm_reason:
                    result.system_message = (result.system_message + "\n" + perm_reason).strip()
        upd = spec.get("updatedInput")
        if isinstance(upd, dict):
            result.updated_input = dict(upd)
        for key in ("updatedToolOutput", "updatedMCPToolOutput"):
            if key in spec and spec[key] is not None:
                result.updated_tool_output = spec[key]
        if spec.get("classifierContext"):
            result.classifier_context = str(spec["classifierContext"])[:2000]
        if spec.get("retry") is True:
            result.retry = True
        if spec.get("sessionTitle"):
            result.session_title = str(spec["sessionTitle"]).strip()
        if spec.get("initialUserMessage"):
            result.initial_user_message = self._cap(str(spec["initialUserMessage"]), "initialUserMessage")
        if spec.get("suppressOriginalPrompt") is True:
            result.suppress_original_prompt = True
        wp = spec.get("watchPaths")
        if isinstance(wp, list):
            result.watch_paths = (result.watch_paths or []) + [str(p) for p in wp]
        if spec.get("reloadSkills") is True:
            result.reload_skills = True
        if isinstance(spec.get("displayContent"), str):
            result.display_content = spec["displayContent"]
        action = str(spec.get("action") or "").lower()
        if action in ("accept", "decline", "cancel") and event in ("Elicitation", "ElicitationResult"):
            if result.action != "decline":     # a decline from any hook wins
                result.action = action
                if isinstance(spec.get("content"), dict):
                    result.content = spec["content"]
        if spec.get("worktreePath"):
            result.worktree_path = str(spec["worktreePath"])

    @staticmethod
    def _merge_permission(result: HookResult, decision: str) -> None:
        """Several hooks may disagree: deny > defer > ask > allow."""
        cur = result.permission_decision
        if cur is None or _PERMISSION_RANK[decision] > _PERMISSION_RANK.get(cur, 0):
            result.permission_decision = decision
