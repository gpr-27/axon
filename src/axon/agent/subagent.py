"""
Subagents: agent definitions, the Task runner, and thread-safe progress tracking.

Agent types come from (later sources override earlier ones with the same name):
  1. built-ins: general-purpose, explore, plan, code-reviewer, test-runner, plus the feature
     agents bundled in axon/features/agents/*.md (e.g. analytics)
  2. agents/*.md of enabled plugins           (plugin:<name>)
  3. ~/.axon/agents/*.md                       (user)
  4. <ws>/.claude/agents/*.md                  (project, Claude Code compatible)
  5. <ws>/.axon/agents/*.md                    (project)
  6. session agents from ``axon --agents '<json>'`` (highest)

Definition files use a small YAML-style frontmatter::

    ---
    name: security-auditor
    description: Reviews diffs for security problems
    tools: Read, Grep, Glob, Bash
    model: claude-sonnet-4-5
    ---
    You are a security auditor ...
"""
from __future__ import annotations
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from axon import interruptible
from axon.agent.state import Conversation, record_usage
from axon.errors import ToolError
from axon.providers.base import Usage

if TYPE_CHECKING:
    from axon.agent.loop import Agent
    from axon.providers.base import StreamEvent

DEFAULT_SUBAGENT_ITERATIONS = 25
DEFAULT_MAX_CONCURRENT = 20
STEP_LIMIT_PREFIX = "Stopped at the step limit"   # first body line of an exhausted result (the UI keys on it)
# Model names in Claude agent files that mean "use the parent's model".
_INHERIT_MODELS = {"", "inherit", "default", "sonnet", "opus", "haiku"}

_REVIEW_TOOLS = ["Read", "Grep", "Glob", "Ls", "CodeSearch", "Bash"]
_TEST_TOOLS = ["Read", "Grep", "Glob", "Ls", "CodeSearch", "Bash"]
# Never given to subagents: recursion and plan-mode controls.
_EXCLUDED_FOR_SUBAGENTS = {"Task", "ExitPlanMode", "AskUserQuestion"}
# Session-scoped definitions from `axon --agents '<json>'` (rank above project and user files).
_SESSION_AGENTS: dict[str, "AgentDefinition"] = {}


@dataclass
class AgentDefinition:
    name: str
    description: str
    tools: list[str] | None = None        # None = every tool the parent has (minus Task)
    model: str | None = None              # None = inherit parent model
    system_prompt: str = ""
    source: str = "builtin"               # builtin | user | project | plugin:<name> | <file path>
    readonly: bool = False                # restrict to read-only tools
    path: Path | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    permission_mode: str | None = None    # default | acceptEdits | auto | dontAsk | bypassPermissions | plan
    skills: list[str] = field(default_factory=list)          # preloaded into the subagent's context
    mcp_servers: list[Any] = field(default_factory=list)     # names (shared) or {name: spec} (private to this agent)
    hooks: dict[str, Any] = field(default_factory=dict)      # lifecycle hooks scoped to this agent
    memory: str | None = None             # user | project | local: a persistent notes directory
    background: bool = False              # always run in the background
    effort: str | None = None
    isolation: str | None = None          # "worktree"
    color: str | None = None
    initial_prompt: str = ""              # first user turn when this agent is the main session (--agent)
    omit_claude_md: bool = False          # start without the user/project memory files
    omit_git_status: bool = False         # start without the git status snapshot
    spawn_types: list[str] | None = None  # Agent(a, b): the only subagent types this agent may start (--agent)
    fast_model: bool = False              # default to the session's small/fast model (explore, plan)
    fork: bool = False                    # inherits the conversation so far (subagent_type "fork")


BUILTIN_AGENTS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name="general-purpose",
        description="General agent for multi-step research or implementation tasks; has the same tools as the main agent (except Task).",
        system_prompt=(
            "You are a general-purpose subagent. Complete the delegated task autonomously with the tools available, "
            "verifying your work where possible."
        ),
    ),
    AgentDefinition(
        name="explore",
        description="Fast read-only codebase exploration: find files, symbols and usages, and answer questions about the code. "
                    "Say how thorough to be: quick, medium or very thorough.",
        readonly=True, omit_claude_md=True, omit_git_status=True, fast_model=True, max_turns=12,
        system_prompt=(
            "You are a read-only exploration subagent. Search broadly (Glob/Grep) and read only what you need. "
            "Never modify files. Report concrete findings with exact file paths and line numbers."
        ),
    ),
    AgentDefinition(
        name="plan",
        description="Read-only software architect: studies the code and returns a step-by-step implementation plan.",
        readonly=True, omit_claude_md=True, omit_git_status=True, fast_model=True, max_turns=15,
        system_prompt=(
            "You are a read-only planning subagent. Investigate the relevant code, then return an implementation plan: "
            "files to change, the change in each, ordering, risks, and how to verify. Never modify files."
        ),
    ),
    AgentDefinition(
        name="code-reviewer",
        description="Reviews recent changes (git diff) for bugs, regressions, security problems and style issues.",
        tools=list(_REVIEW_TOOLS),
        system_prompt=(
            "You are a meticulous code reviewer. Inspect the changes (e.g. `git diff`, `git diff --staged`) and the code they "
            "touch. Report only real problems, most severe first, each with file:line, why it is wrong and a concrete fix. "
            "Do not modify files."
        ),
    ),
    AgentDefinition(
        name="test-runner",
        description="Runs the project's tests (or a subset), and reports failures with their root causes.",
        tools=list(_TEST_TOOLS),
        system_prompt=(
            "You are a test-runner subagent. Discover how the project runs its tests, run the relevant ones, and report the "
            "pass/fail summary. For each failure give the test name, the error, and the likely root cause with file:line. "
            "Do not modify source files."
        ),
    ),
)


def _parse_list(raw: str) -> list[str]:
    """Comma-separated names (``Read, Grep`` or ``[Read, Grep]``); commas inside parentheses stay, so
    ``Agent(worker, researcher), Read`` is two entries."""
    from axon.agent.frontmatter import _split_top
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip("'\"").strip() for t in _split_top(raw) if t.strip("'\"").strip()]


_FIELD_ALIASES = {
    "allowedtools": "tools", "disallowedtools": "disallowed", "maxturns": "maxturns", "permissionmode": "permissionmode",
    "mcpservers": "mcpservers", "omitclaudemd": "omitclaudemd", "initialprompt": "initialprompt",
    "autocompactwindow": "autocompactwindow",
}
_KNOWN_FIELDS = {"name", "description", "tools", "disallowed", "model", "permissionmode", "maxturns", "skills", "mcpservers",
                 "hooks", "memory", "background", "omitclaudemd", "effort", "isolation", "color", "initialprompt",
                 "autocompactwindow", "experimental", "readonly"}
PERMISSION_MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan", "manual")
COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _field_key(key: str) -> str:
    k = str(key).strip().lower().replace("-", "").replace("_", "")
    return _FIELD_ALIASES.get(k, k)


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return _parse_list(str(value))


def _as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def split_agent_tools(entries: list[str]) -> tuple[list[str], list[str] | None]:
    """Normalize a ``tools`` list. ``Agent`` / ``Task`` (with or without a ``(type, ...)`` allowlist) become the
    ``Task`` tool; the allowlist is returned separately (None = any type). ``Bash(git push *)`` style specifiers
    keep only the tool name."""
    tools: list[str] = []
    spawn: list[str] | None = None
    for e in entries:
        m = re.match(r"^(Agent|Task)\s*(?:\((.*)\))?$", e.strip())
        if m:
            if "Task" not in tools:
                tools.append("Task")
            if m.group(2) is not None and m.group(2).strip():
                spawn = (spawn or []) + [t for t in _parse_list(m.group(2)) if t not in (spawn or [])]
            continue
        name = e.split("(", 1)[0].strip() if "(" in e else e.strip()
        if name and name not in tools:
            tools.append(name)
    return tools, spawn


def parse_agent_text(text: str, fallback_name: str) -> tuple[dict[str, Any], str, str | None]:
    """(normalized frontmatter fields, body, error). Field names are lower-cased without separators."""
    from axon.agent.frontmatter import parse_yaml_subset, split_frontmatter
    fm, body = split_frontmatter(text)
    if fm is None:
        return {}, text, None
    try:
        raw = parse_yaml_subset(fm)
    except ValueError as e:
        return {}, body, f"frontmatter does not parse: {e}"
    return {_field_key(k): v for k, v in raw.items()}, body, None


def parse_agent_file(path: Path, source: str, report: list[str] | None = None) -> AgentDefinition | None:
    """Read one agent file. Returns None for a file Claude Code would skip (no name when it has frontmatter,
    bad name, no description, unparseable YAML); the reason is appended to *report* when given."""
    try:
        raw = path.read_bytes()
        try:
            from axon.config import decode_config_bytes
            text = decode_config_bytes(raw)
        except Exception:
            text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None

    def skip(why: str) -> None:
        if report is not None:
            report.append(f"{path}: skipped, {why}")
        return None

    meta, body, error = parse_agent_text(text, path.stem)
    if error:
        return skip(error)
    has_fm = bool(meta) or text.lstrip().startswith("---")
    name = str(meta.get("name") or "").strip()
    if not name:
        if has_fm and source != "builtin" and not source.startswith("plugin:"):
            return skip("no 'name' in the frontmatter (treated as documentation)") if meta else None
        name = path.stem.strip()
    if not name:
        return None
    if name.startswith("-") or ":" in name or len(name) > 256:
        if not source.startswith("plugin:"):
            return skip(f"invalid name {name!r} (cannot start with '-', contain ':' or exceed 256 characters)")
    if meta and not str(meta.get("description") or "").strip() and not source.startswith("plugin:") and source != "builtin":
        return skip("has a name but no 'description'")
    if report is not None:
        unknown = [k for k in meta if k not in _KNOWN_FIELDS]
        if unknown:
            report.append(f"{path}: unrecognized field(s) ignored: {', '.join(unknown[:5])}")

    return _definition_from_meta(meta, body, name, source, path, f"Custom agent from {path.name}")


def _definition_from_meta(meta: dict[str, Any], body: str, name: str, source: str, path: Path | None,
                          default_desc: str) -> AgentDefinition:
    tools, spawn = split_agent_tools(_as_list(meta.get("tools")))
    if tools == ["*"]:
        tools = []
    disallowed = [d.split("(", 1)[0].strip() if "(" in d else d for d in _as_list(meta.get("disallowed"))]
    disallowed = ["Task" if d == "Agent" else d for d in disallowed]
    model = str(meta.get("model") or "").strip() or None
    try:
        max_turns = int(str(meta.get("maxturns") or "0")) or None
    except ValueError:
        max_turns = None
    perm = str(meta.get("permissionmode") or "").strip() or None
    if perm is not None and perm not in PERMISSION_MODES:
        low = {m.lower(): m for m in PERMISSION_MODES}
        perm = low.get(perm.lower())
    plugin = source.startswith("plugin:")        # plugin agents may not carry hooks, mcpServers or permissionMode
    memory = str(meta.get("memory") or "").strip().lower() or None
    color = str(meta.get("color") or "").strip().lower() or None
    effort = str(meta.get("effort") or "").strip().lower() or None
    iso = str(meta.get("isolation") or "").strip().lower() or None
    hooks = meta.get("hooks") if isinstance(meta.get("hooks"), dict) else {}
    mcp = meta.get("mcpservers") if isinstance(meta.get("mcpservers"), list) else []
    return AgentDefinition(
        name=name,
        description=str(meta.get("description") or default_desc).strip(),
        tools=tools or None,
        model=model,
        system_prompt=body.strip(),
        source=source,
        readonly=_as_bool(meta.get("readonly")),
        path=path,
        disallowed_tools=disallowed,
        max_turns=max_turns,
        permission_mode=None if plugin else perm,
        skills=_as_list(meta.get("skills")),
        mcp_servers=[] if plugin else list(mcp),
        hooks={} if plugin else dict(hooks),
        memory=memory if memory in ("user", "project", "local") else None,
        background=_as_bool(meta.get("background")),
        effort=effort if effort in EFFORTS else None,
        isolation=iso if iso == "worktree" else None,
        color=color if color in COLORS else None,
        initial_prompt="" if plugin else str(meta.get("initialprompt") or "").strip(),
        omit_claude_md=_as_bool(meta.get("omitclaudemd")),
        spawn_types=spawn,
    )


def agent_definition_dirs(workspace: Path) -> list[tuple[str, Path]]:
    """Agent directories, lowest priority first (later ones override earlier ones): user, then the project
    directories walking from the repository root down to the workspace (the closest wins), then the
    ``.claude/agents`` of every ``--add-dir`` directory."""
    ws = Path(workspace)
    from axon import storage
    out: list[tuple[str, Path]] = [("user", storage.user_ext_dir("agents"))]
    chain: list[Path] = []
    cur = ws
    for _ in range(40):
        chain.append(cur)
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    for d in reversed(chain):
        out.append(("project", d / ".claude" / "agents"))
        out.append(("project", d / ".axon" / "agents"))
    return out


def extra_agent_dirs(settings: Any) -> list[tuple[str, Path]]:
    """``.claude/agents`` / ``.axon/agents`` inside directories added with --add-dir."""
    out: list[tuple[str, Path]] = []
    for d in getattr(settings, "additional_dirs", None) or []:
        out.append(("project", Path(d) / ".claude" / "agents"))
        out.append(("project", Path(d) / ".axon" / "agents"))
    return out


def _agent_files(directory: Path) -> list[Path]:
    """Every ``*.md`` under *directory*, subfolders included (a folder does not change an agent's identity)."""
    try:
        return sorted(directory.rglob("*.md")) if directory.is_dir() else []
    except OSError:
        return []


def feature_agent_definitions() -> list[AgentDefinition]:
    """Agents bundled with feature modules (``axon/features/agents/*.md``), treated as built-ins."""
    try:
        from axon.features.analytics import AGENTS_DIR
    except Exception:
        try:
            AGENTS_DIR = Path(__file__).resolve().parent.parent / "features" / "agents"
        except Exception:
            return []
    out: list[AgentDefinition] = []
    try:
        files = sorted(Path(AGENTS_DIR).glob("*.md")) if Path(AGENTS_DIR).is_dir() else []
    except OSError:
        files = []
    for f in files:
        d = parse_agent_file(f, "builtin")
        if d is not None:
            out.append(d)
    return out


def parse_agents_json(raw: str | dict[str, Any]) -> list[AgentDefinition]:
    """Parse ``--agents``: a JSON object (or the path of a JSON file) mapping agent names to
    ``{"description", "prompt", "tools", "disallowedTools", "model", "permissionMode", "maxTurns", "skills",
    "mcpServers", "hooks", "memory", "background", "effort", "isolation", "initialPrompt", ...}``.
    ``color`` and ``experimental`` are ignored. Raises ValueError on malformed input."""
    import json
    if isinstance(raw, str):
        text = raw.strip()
        if not text.startswith("{") and Path(text).expanduser().is_file():
            text = Path(text).expanduser().read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"not valid JSON: {e}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("--agents expects a JSON object mapping agent names to definitions")
    out: list[AgentDefinition] = []
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"agent '{name}' must be an object")
        name = str(name).strip()
        if not name or name.startswith("-") or ":" in name:
            raise ValueError(f"invalid agent name '{name}'")
        desc = str(spec.get("description") or "").strip()
        prompt = str(spec.get("prompt") or spec.get("system_prompt") or "").strip()
        if not desc:
            raise ValueError(f"agent '{name}' needs a 'description'")
        if not prompt and not spec.get("memory"):
            raise ValueError(f"agent '{name}' needs a 'prompt'")
        meta = {_field_key(k): v for k, v in spec.items() if k not in ("color", "experimental", "prompt", "system_prompt")}
        meta["description"] = desc
        d = _definition_from_meta(meta, prompt, name, "cli", None, desc)
        d.readonly = bool(spec.get("readonly", False))
        out.append(d)
    return out


def register_session_agents(defs: list[AgentDefinition]) -> None:
    """Install session-scoped agents (``--agents``); they override every file-based definition."""
    for d in defs:
        _SESSION_AGENTS[d.name] = d


def clear_session_agents() -> None:
    _SESSION_AGENTS.clear()


def plugin_agent_definitions(workspace: Path) -> list[AgentDefinition]:
    """agents/*.md of enabled plugins (``source="plugin:<name>"``); user/project files override them."""
    try:
        from axon.commands.plugins import enabled_plugins
        plugins = enabled_plugins(workspace)
    except Exception:  # noqa: BLE001 - plugins are optional; never break agent loading
        return []
    out: list[AgentDefinition] = []
    try:
        from axon.commands.plugins import plugin_agent_files
    except ImportError:  # pragma: no cover
        return []
    for plug in plugins:
        try:
            files = list(plugin_agent_files(plug))   # honours the manifest's ``agents`` key
        except Exception:  # noqa: BLE001
            files = []
        for f in files:
            d = parse_agent_file(f, f"plugin:{plug.name}")
            if d is not None:
                out.append(d)
    return out


def load_agent_definitions(workspace: Path, report: list[str] | None = None,
                           extra_dirs: list[tuple[str, Path]] | None = None) -> list[AgentDefinition]:
    """Built-in + feature agents, then plugin, user and project agent files, then --agents (later override).
    Names compare case-insensitively (a user ``Explore`` replaces the built-in ``explore``). *report* collects
    one line per file that was skipped or had fields that were ignored."""
    by_name: dict[str, AgentDefinition] = {a.name.lower(): a for a in BUILTIN_AGENTS}
    for d in feature_agent_definitions():
        by_name.setdefault(d.name.lower(), d)
    for d in plugin_agent_definitions(workspace):
        by_name[d.name.lower()] = d
    for source, directory in agent_definition_dirs(workspace) + list(extra_dirs or []):
        for f in _agent_files(directory):
            d = parse_agent_file(f, source, report)
            if d is not None:
                by_name[d.name.lower()] = d
    for name, d in _SESSION_AGENTS.items():
        by_name[name.lower()] = d
    return list(by_name.values())


FORK_AGENT = AgentDefinition(
    name="fork", description="Inherits the whole conversation so far; same system prompt, tools and model as the main agent.",
    fork=True,
)


def get_agent_definition(workspace: Path, name: str | None, extra_dirs: list[tuple[str, Path]] | None = None) -> AgentDefinition | None:
    wanted = (name or "general-purpose").strip().lower().removeprefix("agent-")
    if wanted == "fork":
        return FORK_AGENT
    for d in load_agent_definitions(workspace, extra_dirs=extra_dirs):
        if d.name.lower() == wanted:
            return d
    return None


@dataclass
class SubagentTask:
    id: str
    index: int
    title: str
    prompt: str
    status: str = "pending"  # pending, running, completed, exhausted, error
    steps: int = 0
    max_steps: int = 15
    start_time: float = 0.0
    elapsed_s: float = 0.0
    conversation: Conversation = field(default_factory=Conversation)
    events: list[StreamEvent] = field(default_factory=list)
    result_text: str = ""
    error_msg: str | None = None
    tokens_consumed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    live_logs: list[str] = field(default_factory=list)
    agent_type: str = "general-purpose"
    model: str = ""
    # What the UI shows while it runs. *activity* is one line ("Read src/x.py", "Thinking", "Retrying ...");
    # *timeline* holds structured entries {"kind", "text", "is_error", "t"} where kind is one of
    # step | tool | result | thinking | retry | resume | error.
    activity: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    retries: int = 0          # provider retries (a request was rejected and sent again)
    resumes: int = 0          # turn-level resumes after a provider error (work kept, run continued)
    live_agent: Any = field(default=None, repr=False, compare=False)   # the running sub-Agent (None once finished)
    name: str = ""            # addressable name given by the delegating agent (SendMessage `to`)
    background: bool = False  # started with run_in_background (its report arrives as a message)
    stopped: bool = False     # stopped by the user: SendMessage will not resume it
    runs: int = 1             # 1 + how many times it was resumed
    color: str = ""
    isolation: str = ""
    worktree_note: str = ""   # where its edits went when they were not merged back

    def add_log(self, msg: str) -> None:
        self.live_logs.append(msg)
        if len(self.live_logs) > 30:
            self.live_logs.pop(0)

    def add_entry(self, kind: str, text: str, is_error: bool = False) -> None:
        self.timeline.append({"kind": kind, "text": text, "is_error": is_error, "t": time.time()})
        if len(self.timeline) > 300:
            del self.timeline[:50]


class SubagentManager:
    """Thread-safe manager for tracking subagents and their progress."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: list[SubagentTask] = []
        self._counter: int = 0
        self._threads: dict[str, int] = {}
        self._reserved: dict[str, int] = {}  # tool_use_id -> number, handed out in call order
        self.on_update: Callable[[list[SubagentTask]], None] | None = None

    def reserve(self, call_id: str) -> int:
        """Give the Task call *call_id* its subagent number now (the batch calls this in call order)."""
        with self._lock:
            self._counter += 1
            self._reserved[call_id] = self._counter
            return self._counter

    def register(self, prompt: str, max_steps: int = 15, title: str | None = None, agent_type: str = "general-purpose",
                 call_id: str = "", name: str = "") -> SubagentTask:
        with self._lock:
            if call_id and call_id in self._reserved:
                idx = self._reserved.pop(call_id)
            else:
                self._counter += 1
                idx = self._counter
            source = (title or "").strip() or (prompt.strip().splitlines()[0] if prompt.strip() else "")
            clean_title = source.strip("#* -:").replace("[Subtask]", "").strip()
            if len(clean_title) > 42:
                clean_title = clean_title[:39] + "..."
            if not clean_title:
                clean_title = f"Task #{idx}"
            task = SubagentTask(
                id=f"sub-{idx}",
                index=idx,
                title=clean_title,
                prompt=prompt,
                status="running",
                max_steps=max_steps,
                start_time=time.time(),
                agent_type=agent_type,
                name=name.strip(),
            )
            self._tasks.append(task)
            self._notify()
            return task

    def reopen(self, task: SubagentTask, max_steps: int) -> None:
        """Start a finished subagent's next run under the same id (SendMessage to a finished subagent)."""
        with self._lock:
            task.status = "running"
            task.steps = 0
            task.resumes = 0
            task.max_steps = max_steps
            task.start_time = time.time()
            task.result_text = ""
            task.error_msg = None
            task.activity = ""
            task.runs += 1
            task.stopped = False
            self._notify()

    def update_progress(self, task_id: str, steps: int) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.steps = steps
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    break
            self._notify()

    def complete(self, task_id: str, result_text: str, conversation: Conversation, status: str = "completed", usage: Any = None) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.status = status
                    t.live_agent = None
                    t.result_text = result_text
                    t.conversation = conversation
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    if usage:
                        t.input_tokens = (t.input_tokens if t.runs > 1 else 0) + (getattr(usage, "input", 0) or 0)
                        t.output_tokens = (t.output_tokens if t.runs > 1 else 0) + (getattr(usage, "output", 0) or 0)
                        t.tokens_consumed = t.input_tokens + t.output_tokens
                    break
            self._notify()

    def send(self, query: str, text: str) -> SubagentTask | None:
        """Hand *text* to a running subagent: it reads it between its steps, like a steer message."""
        with self._lock:
            task = self.get_task(query)
            live = getattr(task, "live_agent", None) if task is not None else None
        queue = getattr(live, "message_queue", None)
        if task is None or task.status != "running" or queue is None:
            return None
        queue.push(text, steer=True)
        task.add_entry("note", f"you: {text}")
        return task

    def total_tokens(self) -> int:
        with self._lock:
            return sum(t.tokens_consumed for t in self._tasks)

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    if t.status != "cancelled":
                        t.status = "error"
                    t.live_agent = None
                    t.error_msg = error
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    break
            self._notify()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update(list(self._tasks))
            except Exception:
                pass

    def all_tasks(self) -> list[SubagentTask]:
        with self._lock:
            return sorted(self._tasks, key=lambda t: t.index)

    def get_task(self, query: str) -> SubagentTask | None:
        with self._lock:
            q = str(query).strip().lower()
            for t in self._tasks:
                if t.id == q or str(t.index) == q or (t.name and t.name.lower() == q):
                    return t
            for t in reversed(self._tasks):
                if t.title.lower().startswith(q) and q:
                    return t
            return None

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks if t.status == "running")

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._counter = 0
            self._threads.clear()
            self._reserved.clear()

    # ── cancellation (Ctrl+X Ctrl+K) ──
    def bind_thread(self, task_id: str, ident: int | None) -> None:
        with self._lock:
            if ident is None:
                self._threads.pop(task_id, None)
            else:
                self._threads[task_id] = ident

    def cancel(self, task_id: str) -> bool:
        """Interrupt one running subagent (KeyboardInterrupt raised in its thread)."""
        import ctypes
        # Signal under the lock: once bind_thread(task_id, None) has run, the thread can no longer be interrupted
        # (an interrupt landing after the run would escape into the tool pool and the parent's turn).
        with self._lock:
            ident = self._threads.get(task_id)
            if not ident:
                return False
            live = None
            for t in self._tasks:
                if t.id == task_id:
                    t.stopped = True
                    live = t.live_agent
            try:
                signalled = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(KeyboardInterrupt)) == 1
            except Exception:
                signalled = False
        # The interrupt only fires when the subagent's thread runs Python again: abort its model
        # stream too, or a subagent waiting on the model keeps going until the reply arrives.
        abort = getattr(live, "_cancel_provider_stream", None)
        if signalled and callable(abort):
            abort(ident)
        return signalled

    def cancel_all(self) -> int:
        """Interrupt every running subagent; returns how many were signalled."""
        with self._lock:
            running = [tid for tid in self._threads if any(t.id == tid and t.status == "running" for t in self._tasks)]
        return sum(1 for tid in running if self.cancel(tid))


_APPROVAL_LOCK = threading.Lock()


class _ParentCheckpoints:
    """A subagent's handle on its parent's file checkpoints: the files it edits are captured into the parent's
    pending snapshot, so /rewind undoes them too. Only the parent seals a turn."""

    def __init__(self, parent_checkpoints: Any) -> None:
        self._cp = parent_checkpoints

    def capture_before_edit(self, p: Path) -> None:
        self._cp.capture_before_edit(p)

    def commit_turn_snapshot(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def clear(self) -> None:
        return None


def _shared_checkpoints(parent: Any, sub_ws: Path) -> Any:
    """The parent's checkpoints for a subagent working in the same workspace (None: a private manager, e.g.
    in a git worktree, whose paths /rewind must not restore)."""
    cp = getattr(parent, "checkpoints", None)
    if cp is None or Path(sub_ws) != Path(parent.settings.workspace):
        return None
    return _ParentCheckpoints(cp)


def _subagent_approver(parent: Agent) -> Callable[[str, dict[str, Any], Any], str] | None:
    """Serialize the parent's interactive approval prompt across subagent threads.
    Without a parent approver, subagents get none (the loop then denies 'ask' decisions)."""
    cb = getattr(parent, "on_approval", None)
    if cb is None:
        return None

    def approve(tool: str, args: dict[str, Any], decision: Any) -> str:
        with _APPROVAL_LOCK:
            return cb(tool, args, decision)

    return approve


def _resolve_model(parent: Agent, agent_def: AgentDefinition, override: str | None = None) -> str:
    """Model for one run: the Task call's ``model``, else the definition's, else AXON_SUBAGENT_MODEL, else the parent's."""
    import os
    for cand in (override, agent_def.model, os.environ.get("AXON_SUBAGENT_MODEL")):
        m = (cand or "").strip()
        if m and m.lower() not in _INHERIT_MODELS:
            return m
        if m and cand is not os.environ.get("AXON_SUBAGENT_MODEL"):
            return parent.settings.model        # an explicit "inherit" / alias stops the search
    if agent_def.fast_model and agent_def.source == "builtin":
        small = str(getattr(parent.settings, "small_model", "") or "").strip()
        if small:
            return small
    return parent.settings.model


def _check_spawn_allowed(parent: Any, agent_def: AgentDefinition) -> None:
    """``tools: Agent(worker, researcher)`` on a ``--agent`` main session: only those types may be started."""
    allowed = getattr(parent, "allowed_subagent_types", None)
    if allowed and agent_def.name.lower() not in {a.lower() for a in allowed}:
        raise ToolError(f"This agent may only start these subagent types: {', '.join(allowed)} (not '{agent_def.name}').")


def max_concurrent_subagents() -> int:
    import os
    try:
        return max(1, int(os.environ.get("AXON_MAX_CONCURRENT_SUBAGENTS", "") or DEFAULT_MAX_CONCURRENT))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT


def _check_concurrency(parent: Any) -> None:
    limit = max_concurrent_subagents()
    if parent.subagents.running_count() >= limit:
        raise ToolError(f"Concurrent subagent limit reached ({limit} running). Do not retry now: wait for a running "
                        "subagent to finish (its report arrives as a message), or stop one with /stop.")


def _effective_mode(parent_mode: str, wanted: str | None) -> tuple[str, bool]:
    """(permission mode, never_prompt) for a subagent. The parent's bypass / acceptEdits / plan modes win over the
    definition's ``permissionMode``; otherwise the definition's mode applies. ``dontAsk`` means no prompts
    (calls that would ask are denied); ``bypassPermissions`` and ``auto`` are only honoured when the parent has them."""
    if wanted is None or parent_mode in ("bypass", "acceptEdits", "plan"):
        return parent_mode, False
    if wanted == "dontAsk":
        return parent_mode, True
    if wanted in ("acceptEdits", "plan"):
        return wanted, False
    if wanted in ("default", "manual"):
        return "default", False
    return parent_mode, False


def _match_tools(reg: Any, patterns: list[str]) -> set[str]:
    """Tool names in *reg* named by *patterns*: exact names (aliases such as ``Agent`` resolved), ``mcp__server``
    for a whole MCP server, and ``*`` globs for MCP tools. A ``Bash(git push *)`` specifier names the whole tool."""
    import fnmatch
    names = [t.name for t in reg.all_tools()]
    out: set[str] = set()
    for raw in patterns:
        pat = raw.split("(", 1)[0].strip()
        if not pat:
            continue
        if pat.startswith("mcp__"):
            if any(c in pat for c in "*?["):
                out |= {n for n in names if fnmatch.fnmatchcase(n, pat)}
            else:
                out |= {n for n in names if n == pat or n.startswith(pat + "__")}
        else:
            out |= {t.name for t in reg.subset(names=[pat]).all_tools()}
    return out


def _build_registry(parent: Agent, agent_def: AgentDefinition, plan: bool = False) -> Any:
    """The subagent's tools: the parent's, minus recursion/plan-mode controls and ``disallowedTools``
    (applied first), narrowed to ``tools`` when given; read-only for readonly agents and in plan mode."""
    reg = parent.registry
    blocked = set(_EXCLUDED_FOR_SUBAGENTS) | _match_tools(reg, agent_def.disallowed_tools)
    pool = [t.name for t in reg.all_tools() if t.name not in blocked]
    if agent_def.tools:
        wanted = _match_tools(reg, agent_def.tools)
        if agent_def.memory:
            wanted |= {"Read", "Write", "Edit"}      # the agent keeps its notes with these
        names = [n for n in pool if n in wanted]
        if not names:
            raise ToolError(f"Agent '{agent_def.name}' would be spawned with zero tools: none of its tools "
                            f"({', '.join(agent_def.tools)}) is available to subagents.")
    else:
        names = pool
    readonly = agent_def.readonly or plan or getattr(parent.settings, "mode", "default") == "plan"
    return reg.subset(names=names, readonly_only=readonly)


def _attach_inline_mcp(registry: Any, agent_def: AgentDefinition, workspace: Path) -> Any:
    """Connect the MCP servers defined inline in the agent's ``mcpServers`` (private to this run) and add their
    tools to *registry*. Returns the bridge to disconnect afterwards, or None. Names that only reference a server
    configured for the session need nothing: their tools are already in the parent's registry."""
    specs: dict[str, Any] = {}
    for entry in agent_def.mcp_servers:
        if isinstance(entry, dict):
            for key, spec in entry.items():
                if isinstance(spec, dict):
                    specs[str(key)] = spec
    if not specs:
        return None
    try:
        from axon.mcp.bridge import MCPToolBridge
        bridge = MCPToolBridge(workspace)
        for tool in bridge.connect_all(specs):
            registry.register(tool)
        return bridge
    except Exception:  # noqa: BLE001 - a broken server must not stop the subagent
        return None


def agent_memory_dir(workspace: Path, agent_def: AgentDefinition) -> Path | None:
    """The persistent notes directory for ``memory: user|project|local`` (None when the agent has no memory)."""
    if not agent_def.memory:
        return None
    from axon import storage
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_def.name).strip("-") or "agent"
    if agent_def.memory == "user":
        return storage.axon_home() / "agent-memory" / slug
    sub = "agent-memory" if agent_def.memory == "project" else "agent-memory-local"
    return Path(workspace) / ".axon" / sub / slug


def _memory_prompt(parent: Any, agent_def: AgentDefinition) -> str:
    if not agent_def.memory or not getattr(parent.settings, "auto_memory", True):
        return ""
    d = agent_memory_dir(parent.settings.workspace, agent_def)
    if d is None:
        return ""
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    index = ""
    try:
        raw = (d / "MEMORY.md").read_text(encoding="utf-8", errors="replace") if (d / "MEMORY.md").is_file() else ""
        lines = raw.splitlines()[:200]
        index = "\n".join(lines)[:25_000]
    except OSError:
        pass
    return (f"## Persistent memory ({agent_def.memory})\n\nYou have a persistent memory directory at `{d}`. It survives across "
            "conversations: check it before you start, and save what you learn (codepaths, patterns, decisions) as short "
            "notes there with Read/Write/Edit. Keep `MEMORY.md` as a concise index; only its first 200 lines or 25KB are "
            "shown below, so curate it when it grows.\n\n"
            + (f"### MEMORY.md\n{index}" if index.strip() else "MEMORY.md is empty so far."))


def _skills_prompt(parent: Any, agent_def: AgentDefinition) -> str:
    mgr = getattr(parent, "skills", None)
    if not agent_def.skills or mgr is None:
        return ""
    out: list[str] = []
    for name in agent_def.skills[:32]:
        try:
            skill = mgr.get(name)
            if skill is None or getattr(skill, "disable_model_invocation", False):
                continue
            out.append(mgr.render(skill))
        except Exception:  # noqa: BLE001 - a missing or broken skill is skipped
            continue
    return "## Preloaded skills\n\n" + "\n\n".join(out) if out else ""


_SPEED_RULES = ("Work fast: issue all independent reads, greps and globs together in ONE step (parallel tool calls) "
               "instead of one per step, stop as soon as you can answer, and write the report as soon as you have it.")


def _role_prompt(parent: Any, agent_def: AgentDefinition, workspace: Path) -> str:
    parts = [agent_def.system_prompt.strip() if agent_def.fork else (agent_def.system_prompt.strip() + "\n\n" + _SPEED_RULES).strip(),
             _skills_prompt(parent, agent_def), _memory_prompt(parent, agent_def)]
    return "\n\n".join(p for p in parts if p)


def _scoped_hooks(parent: Any, agent_def: AgentDefinition) -> Any:
    """The parent's hooks plus the agent's own frontmatter ``hooks`` (``Stop`` becomes ``SubagentStop``)."""
    base = getattr(parent, "hooks", None)
    if not agent_def.hooks or base is None or not hasattr(base, "add_scoped"):
        return base
    try:
        import copy
        section = {("SubagentStop" if str(k).lower() == "stop" else k): v for k, v in agent_def.hooks.items()}
        scoped = copy.copy(base)                       # a private manager: the parent's hooks never see these
        scoped._scoped = {k: list(v) for k, v in getattr(base, "_scoped", {}).items()}
        return scoped if scoped.add_scoped(section, f"agent:{agent_def.name}") else base
    except Exception:  # noqa: BLE001
        return base


_TAG_RE = re.compile(r"<(/?)(system-reminder|system|assistant|human)\b", re.I)
_ROLE_RE = re.compile(r"(?m)^([ \t]*)(Human|Assistant):")
_PERM_RE = re.compile(r"bypassPermissions|--dangerously-skip-permissions")


def scan_report(text: str) -> str:
    """A subagent's report on its way to the delegating agent: text that imitates the harness (a
    ``<system-reminder>`` tag, a ``Human:`` line) gets a backslash so it reads as ordinary text; a marker line
    says so. Nothing is removed or reworded."""
    if not text:
        return text
    found: list[str] = []
    if _TAG_RE.search(text):
        found.append("tag imitating harness output")
        text = _TAG_RE.sub(lambda m: f"<\\{m.group(1)}{m.group(2)}", text)
    if _ROLE_RE.search(text):
        found.append("role-prefixed line")
        text = _ROLE_RE.sub(lambda m: f"{m.group(1)}\\{m.group(2)}:", text)
    if _PERM_RE.search(text):
        found.append("permission setting mentioned")
    if found:
        text = f"[harness: subagent output matched instruction-shaped pattern(s): {', '.join(found)}]\n{text}"
    return text


def fork_prompt(parent: Any, task: str) -> str:
    """The task wrapped with the conversation so far (a fork starts from the same context as the main agent)."""
    try:
        from axon.commands.code_cmds import session_context
        context_text, n = session_context(parent)
    except Exception:  # noqa: BLE001
        context_text, n = "", 0
    if not n:
        return task
    return (f"<conversation_so_far>\n{context_text}\n</conversation_so_far>\n\nYou are a fork of the conversation above. "
            f"Do only this task and report back concisely:\n{task}")


def _make_provider(parent: Agent, model: str, settings: Any) -> Any:
    """A fresh provider per subagent for real providers (their turn state is per instance);
    otherwise share the parent's (the loop serializes calls on a shared provider)."""
    prov = parent.provider
    if type(prov).__module__.startswith("axon.providers"):
        try:
            from axon.agent.loop import make_provider
            return make_provider(model, settings, role="subagent")
        except Exception:
            return prov
    return prov


def _persist_subagent_run(parent: Any, task: Any, agent_type: str, model: str, prompt: str, status: str,
                          cost: float, usage: Any, iterations: int, elapsed: float, result_text: str) -> None:
    """Save the finished run to the parent transcript and the activity journal (never raises)."""
    try:
        from axon.session.library import record_run
        parent_sess = str(getattr(parent.session, "active_session_id", "") or "").rsplit("_sub_", 1)[0]
        record_run("subagent_result", {
            "task_id": task.id, "index": task.index, "agent_type": agent_type, "title": task.title,
            "model": model, "prompt": prompt, "status": status, "result": result_text,
            "input_tokens": int(getattr(usage, "input", 0) or 0), "output_tokens": int(getattr(usage, "output", 0) or 0),
            "cost": round(float(cost or 0), 8), "iterations": int(iterations or 0), "elapsed_s": round(elapsed, 2),
            "sub_session_id": f"{parent_sess}_sub_{task.index}",
        }, agent=parent)
    except Exception:
        pass

# Turn-level resumes after a provider error (on top of the provider's own retries), with a growing pause.
SUBAGENT_RESUMES = 2
SUBAGENT_RESUME_DELAY_S = 1.0
_RESUME_PROMPT = ("continue (the previous request failed with a provider error; pick up where you left off "
                  "using the tool results above)")


def _failure_report(task: Any, reason: str) -> str:
    """What the delegating agent sees when a subagent fails: the reason plus the tool calls that already ran,
    so it can retry, narrow the task or do the rest itself without repeating finished work."""
    steps = int(getattr(task, "steps", 0) or 0)
    when = f" after {steps} step{'s' if steps != 1 else ''}" if steps else ""
    head = f"[Subagent #{task.index} ({task.title}) failed{when}: {reason}]"
    resumed = f" (resumed {task.resumes}x before giving up)" if getattr(task, "resumes", 0) else ""
    progress = _progress_section(task, f"the failure{resumed}")
    return f"{head}\n\n{progress}" if progress else head


def _progress_section(task: Any, before: str) -> str:
    """'Progress before <before>:' and the last 20 tool calls the subagent made ('' when it made none)."""
    tools = [e["text"] for e in getattr(task, "timeline", []) if e.get("kind") == "tool"]
    if not tools:
        return ""
    shown = tools[-20:]
    more = f"\n- … {len(tools) - len(shown)} earlier tool calls" if len(tools) > len(shown) else ""
    return f"Progress before {before}:{more}\n" + "\n".join(f"- {t}" for t in shown)


def _step_limit(parent: Any, agent_def: AgentDefinition, requested: Any) -> int:
    """Steps (model calls) for one subagent: the Task call's max_steps, else the agent's maxTurns, else 25;
    always within [1, the session's max_iterations]."""
    cap = max(1, int(getattr(parent.settings, "max_iterations", DEFAULT_SUBAGENT_ITERATIONS) or 1))
    want = requested if requested not in (None, "", 0) else (agent_def.max_turns or DEFAULT_SUBAGENT_ITERATIONS)
    try:
        want = int(want)
    except (TypeError, ValueError):
        want = DEFAULT_SUBAGENT_ITERATIONS
    return max(1, min(want, cap))


def _sub_error_text(result: Any) -> str:
    """The one-line reason a subagent's model call failed (the parent's retry advice is dropped)."""
    raw = str(getattr(result, "error", None) or getattr(result, "final_text", "") or "")
    first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    first = first.removeprefix("Provider error: ").strip()
    return first or "the model call failed"


def run_subagent(
    prompt: str,
    parent: Agent,
    max_iterations: int | None = None,
    subagent_type: str | None = None,
    description: str = "",
    call_id: str = "",
    *,
    model: str | None = None,
    name: str = "",
    isolation: str | None = None,
    effort: str | None = None,
    resume: SubagentTask | None = None,
    task: SubagentTask | None = None,
) -> str:
    """Run an isolated subagent turn and return its final report.

    The subagent gets its own Conversation, a restricted tool registry (never the Task
    tool, enforcing depth 1), its own provider instance, and its own session file. Its
    usage is recorded once into the parent ledger with ``source=<agent name>``.

    *resume* continues a finished subagent (its history is kept, *prompt* is the new message); *task* is a
    task the caller already registered (background runs); *model*, *effort* and *isolation* override the
    definition for this run."""
    if not hasattr(parent, "subagents") or parent.subagents is None:
        parent.subagents = SubagentManager()

    if resume is not None:
        agent_def = get_agent_definition(parent.settings.workspace, resume.agent_type, extra_dirs=extra_agent_dirs(parent.settings))
        if agent_def is None:
            agent_def = get_agent_definition(parent.settings.workspace, "general-purpose")
    else:
        agent_def = get_agent_definition(parent.settings.workspace, subagent_type, extra_dirs=extra_agent_dirs(parent.settings))
    if agent_def is None:
        available = ", ".join(sorted(d.name for d in load_agent_definitions(parent.settings.workspace)))
        raise ToolError(f"Unknown subagent_type '{subagent_type}'. Available: {available}")
    _check_spawn_allowed(parent, agent_def)

    max_iter = _step_limit(parent, agent_def, max_iterations)
    run_model = _resolve_model(parent, agent_def, model)
    model = run_model
    iso = (isolation or agent_def.isolation or ("worktree" if getattr(parent.settings, "isolate_worktrees", False) else "")) or ""
    iso = iso if iso == "worktree" else ""
    if resume is not None:
        task = resume
        if task.status != "running":
            parent.subagents.reopen(task, max_iter)
    elif task is None:
        _check_concurrency(parent)
        task = parent.subagents.register(prompt, max_steps=max_iter, title=description or None, agent_type=agent_def.name,
                                         call_id=call_id, name=name)
    task.model = model
    task.color = agent_def.color or ""
    task.isolation = iso
    started = time.time()

    def emit_parent(phase: str, status: str, cost: float = 0.0, usage: Any = None, iterations: int = 0,
                    result_text: str = "") -> None:
        if phase == "end":
            _persist_subagent_run(parent, task, agent_def.name, model, prompt, status, cost, usage, iterations,
                                  time.time() - started, result_text)
        emit = getattr(parent, "_emit", None)
        if not callable(emit):
            return
        try:
            from axon.agent.trace import SubagentEvent
            emit(SubagentEvent(
                phase=phase, task_id=task.id, agent_type=agent_def.name, title=task.title, model=model,
                cost=cost, input_tokens=int(getattr(usage, "input", 0) or 0),
                output_tokens=int(getattr(usage, "output", 0) or 0), status=status,
                duration_s=time.time() - started if phase == "end" else 0.0, iterations=iterations,
                detail=result_text if status in ("error", "cancelled") else "",
            ))
        except Exception:
            pass

    emit_parent("start", "running")
    hook = getattr(parent, "_hook", None)
    if callable(hook):
        res = hook("SubagentStart", {"agent_type": agent_def.name, "agent_id": task.id, "prompt": prompt[:20_000],
                                     "description": description})
        extra_ctx = str(getattr(res, "additional_context", "") or "") if res is not None else ""
        if res is not None and getattr(res, "blocked", False):
            msg = str(getattr(res, "message", "") or "blocked")
            parent.subagents.fail(task.id, f"Blocked by SubagentStart hook: {msg}")
            emit_parent("end", "error", result_text=f"Blocked by SubagentStart hook: {msg}")
            return f"[Subagent #{task.index} ({task.title}) failed: blocked by SubagentStart hook: {msg}]"
        if extra_ctx:
            prompt = f"{prompt}\n\n<subagent-start-hook>\n{extra_ctx}\n</subagent-start-hook>"

    def nested(kind: str, **kw: Any) -> None:
        emit = getattr(parent, "_emit", None)
        if not callable(emit):
            return
        try:
            from axon.agent.trace import SubagentStep
            emit(SubagentStep(task_id=task.id, agent_type=agent_def.name, kind=kind, **kw))
        except Exception:
            pass

    pending_args: dict[str, str] = {}
    thinking_buf: list[str] = []

    def sub_on_event(event: StreamEvent) -> None:
        task.events.append(event)
        if len(task.events) > 2000:
            del task.events[:500]
        from axon.providers.base import LLMCallStart, ThinkingDelta, ToolExecutionStart, ToolExecutionResult
        try:
            from axon.agent.trace import LLMCallEnd, ToolTiming
        except Exception:  # pragma: no cover
            LLMCallEnd = ToolTiming = ()  # type: ignore[assignment,misc]
        if isinstance(event, LLMCallEnd):
            u = event.usage
            nested("llm", name=event.model, summary=f"{event.stop_reason} · {event.tool_use_count} tool call(s)",
                   is_error=event.outcome == "error", duration_s=event.duration_s, cost=float(event.cost or 0.0),
                   input_tokens=int(getattr(u, "input", 0) or 0), output_tokens=int(getattr(u, "output", 0) or 0))
        elif isinstance(event, ToolExecutionStart):
            pending_args[event.id] = _short_args(event.input)
        elif isinstance(event, ToolTiming):
            args_txt = pending_args.pop(event.id, "")
            nested("tool", name=event.name,
                   summary=(f"{args_txt} -> " if args_txt else "") + f"{event.lines} lines · ~{event.est_tokens} tok",
                   is_error=event.is_error, duration_s=event.duration_s)
        if isinstance(event, LLMCallStart) and event.iteration > max_iter:
            # The extra call at the step limit: the subagent writes its report (no tools run).
            parent.subagents.update_progress(task.id, max_iter)
            task.add_log("Step limit reached: writing the final report")
            task.add_entry("step", f"Step limit ({max_iter}) reached · writing the final report")
            task.activity = "Step limit reached · writing the final report"
            thinking_buf.clear()
        elif isinstance(event, LLMCallStart):
            parent.subagents.update_progress(task.id, event.iteration)
            task.add_log(f"Step {event.iteration}: model call")
            task.add_entry("step", f"Step {event.iteration} · asking {model}")
            task.activity = f"Step {event.iteration} · waiting for {model}"
            thinking_buf.clear()
        elif isinstance(event, ToolExecutionStart):
            arg_str = str(event.input)
            if len(arg_str) > 50:
                arg_str = arg_str[:47] + "..."
            task.add_log(f"{event.name}({arg_str})")
            task.tool_calls += 1
            target = _short_args(event.input, 120)
            task.add_entry("tool", f"{event.name} {target}".rstrip())
            task.activity = f"{event.name} {_short_args(event.input, 60)}".rstrip()
        elif isinstance(event, ToolExecutionResult):
            st = "Error" if event.is_error else "Result"
            c_str = str(event.content).strip().replace("\n", " ")
            if len(c_str) > 55:
                c_str = c_str[:52] + "..."
            task.add_log(f"  -> {st}: {c_str}")
            raw = str(event.content or "")
            first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "(no output)")
            n = len(raw.splitlines())
            task.add_entry("result", f"{first[:140]}" + (f"  ({n} lines)" if n > 1 else ""), is_error=bool(event.is_error))
        elif isinstance(event, ThinkingDelta):
            th = (getattr(event, "text", "") or "").strip()
            if len(th) > 10:
                task.add_log(f"Thinking: {th[:55]}...")
            thinking_buf.append(getattr(event, "text", "") or "")
            text = " ".join("".join(thinking_buf).split())
            if text:
                task.activity = f"Thinking · {text[-70:]}"
                if task.timeline and task.timeline[-1]["kind"] == "thinking":
                    task.timeline[-1]["text"] = text[:400]
                else:
                    task.add_entry("thinking", text[:400])
        elif type(event).__name__ in ("RetryEvent", "ProviderRetry"):
            task.retries += 1
            why = _sub_error_text(type("E", (), {"error": getattr(event, "error", "")})())
            att, mx, delay = getattr(event, "attempt", 0), getattr(event, "max_attempts", 0), float(getattr(event, "delay_s", 0) or 0)
            line = f"Retry {att}/{mx} in {delay:.1f}s · {why}"
            task.add_log(line)
            task.add_entry("retry", line, is_error=True)
            task.activity = f"Retrying ({att}/{mx}) · {why}"

    from axon.agent.loop import Agent
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.agent.worktree import WorktreeManager, WorktreeInfo

    worktree_info: WorktreeInfo | None = None
    sub_ws = parent.settings.workspace
    if iso == "worktree" and not agent_def.readonly:
        try:
            worktree_info = WorktreeManager.create_worktree(parent.settings.workspace, task.id)
            if worktree_info:
                sub_ws = worktree_info.worktree_path
                task.add_log(f"Isolated git worktree: {sub_ws.name}")
                if callable(hook):
                    hook("WorktreeCreate", {"worktree_path": str(sub_ws), "agent_id": task.id})
            else:
                task.add_log("Worktree isolation unavailable (not a git repository with a commit): running in the workspace")
        except Exception:
            worktree_info = None
    mcp_bridge: Any = None

    try:
        mode, no_prompt = _effective_mode(parent.settings.mode, agent_def.permission_mode)
        updates: dict[str, Any] = {"max_iterations": max_iter, "workspace": sub_ws, "model": model, "mode": mode,
                                   "parallel_tools": max(4, int(getattr(parent.settings, "parallel_tools", 1) or 1))}
        eff = effort or agent_def.effort
        if eff:
            try:
                from axon.config import Settings
                updates["effort"] = Settings.normalize_effort(eff)
            except Exception:
                pass
        from axon.providers.registry import settings_for_model
        sub_settings = settings_for_model(model, parent.settings).model_copy(update=updates)
        parent_sess_id = parent.session.active_session_id.rsplit("_sub_", 1)[0]
        sub_store = SessionStore(sub_ws, session_dir=parent.session.session_dir)
        sub_store.open(f"{parent_sess_id}_sub_{task.index}")
        sub_ledger = Ledger()
        registry = _build_registry(parent, agent_def, plan=(mode == "plan"))
        mcp_bridge = _attach_inline_mcp(registry, agent_def, sub_ws)
        role = _role_prompt(parent, agent_def, sub_ws)
        sub_agent = Agent(
            provider=_make_provider(parent, model, sub_settings),
            tools=registry,
            permissions=parent.permissions,
            context=parent.context,
            checkpoints=_shared_checkpoints(parent, sub_ws),
            session=sub_store,
            ledger=sub_ledger,
            settings=sub_settings,
            skills=getattr(parent, "skills", None),
            on_event=sub_on_event,
            on_approval=None if no_prompt else _subagent_approver(parent),
            hooks=_scoped_hooks(parent, agent_def),
            agent_name=agent_def.name,
            extra_system=role,
            git_status="" if agent_def.omit_git_status else getattr(parent, "_git_snapshot", None),
        )
        if agent_def.omit_claude_md:
            sub_agent._memory_text = ""
        sub_agent.parent_agent = parent
        sub_agent.subagents = parent.subagents      # siblings can reach each other with SendMessage
        sub_agent.subagent_task = task
        if resume is not None and getattr(resume.conversation, "messages", None):
            sub_agent.conversation = Conversation(list(resume.conversation.messages))

        task.live_agent = sub_agent
        if resume is not None:
            sub_prompt = f"{prompt}\n\n(You have at most {max_iter} steps (model calls) for this message.)"
        elif agent_def.fork:
            sub_prompt = (f"{fork_prompt(parent, prompt)}\n\nWhen you are done, reply with a concise final report for the "
                          f"agent that forked you. You have at most {max_iter} steps (model calls).")
        else:
            sub_prompt = (
                f"{prompt}\n\n"
                "When you are done, reply with a concise final report for the agent that delegated this task: what you did or "
                "found, exact file paths (with line numbers where useful), and any open issues. Your final message is the only "
                f"thing the delegating agent will see. You have at most {max_iter} steps (model calls); budget them so the "
                "report gets written."
            )
        parent.subagents.bind_thread(task.id, threading.get_ident())
        try:
            result = sub_agent.run_turn(sub_prompt)
            spent = [result.usage]
            # A provider error ends the turn, but the tool calls and results so far stay in the subagent's
            # history: resume from there (or resend the prompt if nothing ran) instead of losing the run.
            while result.stop_reason == "error" and task.resumes < SUBAGENT_RESUMES:
                task.resumes += 1
                why = _sub_error_text(result)
                failed = getattr(sub_agent, "last_failed_turn", None) or {}
                again = _RESUME_PROMPT if failed.get("resume") else sub_prompt
                line = f"Resume {task.resumes}/{SUBAGENT_RESUMES} after: {why}"
                task.add_log(line)
                task.add_entry("resume", line, is_error=True)
                task.activity = f"Resuming ({task.resumes}/{SUBAGENT_RESUMES}) · {why}"
                interruptible.sleep(SUBAGENT_RESUME_DELAY_S * task.resumes)
                result = sub_agent.run_turn(again)
                spent.append(result.usage)
            if result.stop_reason == "interrupted" and task.stopped:
                # Stopped while it waited for the model (that interrupt ends the turn quietly): report a cancel.
                raise KeyboardInterrupt
            # Unbound inside the try: a cancel signalled just before this still lands in the handler below.
            parent.subagents.bind_thread(task.id, None)
        except KeyboardInterrupt:
            parent.subagents.bind_thread(task.id, None)
            parent.subagents.complete(task.id, "[Cancelled by user]", sub_agent.conversation, status="cancelled")
            parent.subagents.fail(task.id, "Stopped by the user")
            emit_parent("end", "cancelled", result_text="Stopped by the user")
            return f"[Subagent #{task.index} ({task.title}) failed: stopped by the user before it finished.]"
        final_text = (result.final_text or "").strip()
        sub_usage = Usage(
            input=sum(int(getattr(u, "input", 0) or 0) for u in spent),
            output=sum(int(getattr(u, "output", 0) or 0) for u in spent),
            cache_read=sum(int(getattr(u, "cache_read", 0) or 0) for u in spent),
            cache_write=sum(int(getattr(u, "cache_write", 0) or 0) for u in spent),
            reasoning=sum(int(getattr(u, "reasoning", 0) or 0) for u in spent),
        )
        tokens_total = (sub_usage.input or 0) + (sub_usage.output or 0)

        if worktree_info:
            merged_ok, merge_msg = WorktreeManager.merge_back(worktree_info)
            if merged_ok:
                final_text += f"\n\n[Worktree] {merge_msg}" if "applied" in merge_msg.lower() else ""
                WorktreeManager.cleanup(worktree_info)
                if callable(hook):
                    hook("WorktreeRemove", {"worktree_path": str(worktree_info.worktree_path), "agent_id": task.id})
            else:
                # Its edits could not be applied to the workspace: keep the worktree so nothing is lost.
                task.worktree_note = (f"{merge_msg.strip()} The changes are kept in {worktree_info.worktree_path} "
                                      f"(branch {worktree_info.branch_name}).")
                final_text += f"\n\n[Worktree] {task.worktree_note}"
            worktree_info = None

        # One aggregated record in the parent ledger for the whole subagent run.
        agg = Usage(
            input=sub_ledger.total_input_tokens or (sub_usage.input or 0),
            output=sub_ledger.total_output_tokens or (sub_usage.output or 0),
            cache_read=sub_ledger.total_cache_read_tokens or getattr(sub_usage, "cache_read", 0),
            cache_write=sub_ledger.total_cache_write_tokens or getattr(sub_usage, "cache_write", 0),
            reasoning=sub_ledger.total_reasoning_tokens or getattr(sub_usage, "reasoning", 0),
        )
        sub_cost = 0.0
        if getattr(parent, "ledger", None) is not None and (agg.input or agg.output):
            try:
                sub_cost = float(record_usage(parent.ledger, model, agg, source=agent_def.name) or 0)
            except Exception:
                pass
        if result.stop_reason in ("error", "interrupted"):
            reason = _sub_error_text(result) if result.stop_reason == "error" else "stopped before it finished"
            if result.stop_reason == "error":
                task.add_entry("error", reason, is_error=True)
            parent.subagents.complete(task.id, reason, sub_agent.conversation, status="error", usage=sub_usage)
            parent.subagents.fail(task.id, reason)
            emit_parent("end", "error", cost=sub_cost, usage=agg, iterations=result.iterations, result_text=reason)
            return _failure_report(task, reason)

        emit_parent("end", "exhausted" if result.stop_reason == "max_iterations" else "completed",
                    cost=sub_cost, usage=agg, iterations=result.iterations, result_text=final_text)

        header = f"[Subagent #{task.index} Result ({task.title}) | {tokens_total:,} tokens (in: {sub_usage.input:,} · out: {sub_usage.output:,})]"
        if result.stop_reason == "max_iterations":
            parent.subagents.complete(task.id, final_text or "[Stopped at the step limit]", sub_agent.conversation, status="exhausted", usage=sub_usage)
            return _exhausted_report(header, task, max_iter, scan_report(final_text))
        if not final_text:
            parent.subagents.complete(task.id, "No text output", sub_agent.conversation, status="completed", usage=sub_usage)
            return f"{header}:\nCompleted ({result.iterations} steps) with no text output."
        parent.subagents.complete(task.id, final_text, sub_agent.conversation, status="completed", usage=sub_usage)
        return f"{header}:\n{scan_report(final_text)}"
    except ToolError as e:
        parent.subagents.fail(task.id, str(e))
        emit_parent("end", "error", result_text=str(e))
        raise
    except Exception as e:
        parent.subagents.fail(task.id, str(e))
        emit_parent("end", "error", result_text=f"{type(e).__name__}: {e}")
        return f"[Subagent #{task.index} ({task.title}) failed: {type(e).__name__}: {e}]"
    finally:
        if worktree_info:
            try:
                WorktreeManager.cleanup(worktree_info)
            except Exception:
                pass
        if mcp_bridge is not None:
            try:
                mcp_bridge.disconnect_all()
            except Exception:
                pass


def _push_to(agent: Any, text: str) -> bool:
    """Queue *text* as a steer message for *agent* (read between its steps, or as its next turn when idle)."""
    queue = getattr(agent, "message_queue", None)
    if queue is None:
        return False
    try:
        from axon.ui.in_flight import push_queued
        push_queued(queue, text, steer=True)
    except Exception:  # noqa: BLE001
        queue.push(text, steer=True)
    return True


def _main_agent(agent: Any) -> Any:
    return getattr(agent, "parent_agent", None) or agent


def spawn_background(prompt: str, parent: Agent, *, resume: SubagentTask | None = None, subagent_type: str | None = None,
                     max_iterations: int | None = None, description: str = "", call_id: str = "", model: str | None = None,
                     name: str = "", isolation: str | None = None, effort: str | None = None) -> str:
    """Run a subagent on its own thread and return at once; its report is queued for the delegating agent as a
    message when it finishes. Errors in choosing the agent type are raised here, before anything starts."""
    if not hasattr(parent, "subagents") or parent.subagents is None:
        parent.subagents = SubagentManager()
    ws = parent.settings.workspace
    if resume is not None:
        agent_def = get_agent_definition(ws, resume.agent_type, extra_dirs=extra_agent_dirs(parent.settings)) or FORK_AGENT
        task = resume
        parent.subagents.reopen(task, _step_limit(parent, agent_def, max_iterations))
        verb = "Resumed"
    else:
        agent_def = get_agent_definition(ws, subagent_type, extra_dirs=extra_agent_dirs(parent.settings))
        if agent_def is None:
            available = ", ".join(sorted(d.name for d in load_agent_definitions(ws)))
            raise ToolError(f"Unknown subagent_type '{subagent_type}'. Available: {available}")
        _check_spawn_allowed(parent, agent_def)
        _check_concurrency(parent)
        task = parent.subagents.register(prompt, max_steps=_step_limit(parent, agent_def, max_iterations),
                                         title=description or None, agent_type=agent_def.name, call_id=call_id, name=name)
        verb = "Started"
    task.background = True

    def work() -> None:
        try:
            report = run_subagent(prompt, parent, max_iterations=max_iterations, subagent_type=subagent_type,
                                  description=description, model=model, name=name, isolation=isolation, effort=effort,
                                  resume=task if resume is not None else None, task=None if resume is not None else task)
        except Exception as e:  # noqa: BLE001
            report = f"[Subagent #{task.index} ({task.title}) failed: {type(e).__name__}: {e}]"
            parent.subagents.fail(task.id, str(e))
        _push_to(_main_agent(parent), f"[Background subagent {task.id} ({task.title}) finished]\n{report}")

    threading.Thread(target=work, daemon=True, name=f"subagent-{task.id}").start()
    who = f"{task.id}" + (f", name '{task.name}'" if task.name else "")
    return (f"{verb} subagent #{task.index} ({task.title}) in the background ({who}). Its report will arrive as a "
            "message when it finishes; carry on with other work and do not poll. Message it meanwhile (or resume it "
            f"later) with SendMessage(to=\"{task.name or task.id}\").")


def delegate(prompt: str, parent: Agent, *, background: bool | None = None, subagent_type: str | None = None, **kw: Any) -> str:
    """Start a subagent for the Task tool: in the foreground (the report is the result), or in the background
    (``run_in_background``, the definition's ``background: true``, or a fork unless told otherwise)."""
    agent_def = get_agent_definition(parent.settings.workspace, subagent_type, extra_dirs=extra_agent_dirs(parent.settings))
    if background is None:
        background = bool(agent_def and (agent_def.background or agent_def.fork))
    if background:
        return spawn_background(prompt, parent, subagent_type=subagent_type, **kw)
    return run_subagent(prompt, parent, subagent_type=subagent_type, **kw)


def send_message(agent: Any, to: str, message: str) -> str:
    """SendMessage: hand *message* to a running subagent (it reads it before its next step), resume a finished one
    in the background with its full history, or, from a subagent, report to ``main``."""
    main = _main_agent(agent)
    key = str(to or "").strip().lstrip("#@")
    text = str(message or "").strip()
    if not key or not text:
        raise ToolError("SendMessage needs 'to' and 'message'.")
    me = getattr(agent, "subagent_task", None)
    if key.lower() in ("main", "lead", "parent", "user"):
        if main is agent:
            raise ToolError("You are the main agent: there is nobody above you to message.")
        who = f"{me.id} ({me.title})" if me is not None else "a subagent"
        _push_to(main, f"[Message from subagent {who}]\n{text}")
        return "Sent to the main agent."
    mgr = getattr(main, "subagents", None)
    task = mgr.get_task(key) if mgr is not None else None
    if task is None and key.lower().startswith("sub-"):
        task = mgr.get_task(key[4:]) if mgr is not None else None
    if task is None:
        known = ", ".join(f"{t.id}" + (f" ({t.name})" if t.name else "") for t in (mgr.all_tasks() if mgr is not None else []))
        raise ToolError(f"No subagent '{to}'. Known subagents: {known or 'none yet'}.")
    if me is not None and task.id == me.id:
        raise ToolError("A subagent cannot message itself.")
    prefix = f"[Message from subagent {me.id}] " if me is not None else ""
    if task.status == "running":
        if mgr.send(task.id, prefix + text) is None:
            raise ToolError(f"{task.id} is still starting; try again in a moment.")
        return f"Delivered to {task.id}: it reads the message before its next step."
    if task.stopped:
        raise ToolError(f"{task.id} was stopped by the user and is not resumed by messages.")
    if not getattr(task.conversation, "messages", None):
        raise ToolError(f"{task.id} has no saved history to resume.")
    return spawn_background(prefix + text, main, resume=task)


def _exhausted_report(header: str, task: Any, max_iter: int, report: str) -> str:
    """What the delegating agent sees when a subagent hit its step limit: the header, the limit, the report the
    subagent wrote at the limit, and the tool calls it made (so finished work is not repeated)."""
    parts = [f"{header}:\n{STEP_LIMIT_PREFIX} ({max_iter} steps) before finishing."]
    if report:
        parts.append(report)
    progress = _progress_section(task, "the step limit")
    if progress:
        parts.append(progress)
    return "\n\n".join(parts)


def _short_args(inp: Any, limit: int = 80) -> str:
    if isinstance(inp, dict):
        for k in ("path", "file_path", "command", "pattern", "query", "url", "skill"):
            if inp.get(k):
                v = " ".join(str(inp[k]).split())
                return v if len(v) <= limit else v[: limit - 3] + "..."
    v = " ".join(str(inp).split())
    return v if len(v) <= limit else v[: limit - 3] + "..."


def _subagent_number(result: Any) -> int | None:
    """The N in a Task result header ("[Subagent #N Result ...", "[Subagent #N (...) failed ...")."""
    match = re.match(r"^\[Subagent #(\d+)\b", str(result or ""))
    return int(match.group(1)) if match else None


def sync_subagents_for_session(agent: Agent) -> None:
    """Ensure agent.subagents contains all subagent tasks belonging to the active session."""
    import json
    import re
    if not hasattr(agent, "subagents") or agent.subagents is None or not isinstance(agent.subagents, SubagentManager):
        agent.subagents = SubagentManager()
    else:
        agent.subagents.clear()
    parent_sess_id = agent.session.active_session_id.rsplit("_sub_", 1)[0]
    seen_indices: set[int] = set()

    # 1. Parse Task tool calls from conversation messages (Anthropic + OpenAI formats)
    if hasattr(agent, "conversation") and agent.conversation.messages:
        from axon.providers.base import ToolUseBlock, ToolResultBlock
        task_idx = 0
        for m in agent.conversation.messages:
            role = m.get("role")
            content = m.get("content")
            task_calls: list[tuple[str, dict[str, Any]]] = []  # (tool_use_id, input_dict)

            # Anthropic style: list of blocks in content
            if role == "assistant" and isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") == "Task":
                        task_calls.append((blk.get("id") or "", blk.get("input", {})))
                    elif isinstance(blk, ToolUseBlock) or type(blk).__name__ == "ToolUseBlock":
                        if getattr(blk, "name", "") == "Task":
                            task_calls.append((getattr(blk, "id", "") or "", getattr(blk, "input", {})))

            # OpenAI style: tool_calls array in message
            if role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn_name = ""
                    fn_args = {}
                    if isinstance(tc, dict):
                        fn_obj = tc.get("function", {})
                        fn_name = fn_obj.get("name", "")
                        raw_args = fn_obj.get("arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                fn_args = json.loads(raw_args)
                            except Exception:
                                fn_args = {"prompt": raw_args}
                        elif isinstance(raw_args, dict):
                            fn_args = raw_args
                        tc_id = tc.get("id", "")
                    else:
                        fn_name = getattr(getattr(tc, "function", None), "name", "")
                        raw_args = getattr(getattr(tc, "function", None), "arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                fn_args = json.loads(raw_args)
                            except Exception:
                                fn_args = {"prompt": raw_args}
                        elif isinstance(raw_args, dict):
                            fn_args = raw_args
                        tc_id = getattr(tc, "id", "")
                    if fn_name == "Task":
                        task_calls.append((tc_id, fn_args))

            for tu_id, inp in task_calls:
                task_idx += 1
                seen_indices.add(task_idx)
                prompt_txt = inp.get("prompt") or inp.get("subtask") or f"Subtask {task_idx}"
                task = agent.subagents.register(
                    prompt_txt,
                    title=inp.get("description") or None,
                    agent_type=str(inp.get("subagent_type") or "general-purpose"),
                )
                task.status = "completed"
                if tu_id:
                    for m2 in agent.conversation.messages:
                        m2_content = m2.get("content")
                        if m2.get("role") == "user" and isinstance(m2_content, list):
                            for res_blk in m2_content:
                                res_match = False
                                res_content = ""
                                if isinstance(res_blk, dict):
                                    if res_blk.get("tool_use_id") == tu_id:
                                        res_match = True
                                        res_content = res_blk.get("content", "")
                                elif isinstance(res_blk, ToolResultBlock) or type(res_blk).__name__ == "ToolResultBlock":
                                    if getattr(res_blk, "tool_use_id", None) == tu_id:
                                        res_match = True
                                        res_content = getattr(res_blk, "content", "")

                                if res_match:
                                    task.result_text = res_content
                                    number = _subagent_number(res_content) or task_idx
                                    task.index = number
                                    task.id = f"sub-{number}"
                                    seen_indices.add(number)
                                    fail_m = re.match(r"^\[Subagent #\d+ (?:\(.*?\) )?failed:\s*(.*)\]\s*$",
                                                      str(res_content or ""), re.DOTALL)
                                    if fail_m:
                                        task.status = "error"
                                        task.error_msg = fail_m.group(1).strip()
                                    elif re.match(r"^\[Subagent #\d+ Result[^\]]*\]:\s*(?:Stopped at the step limit|Reached the iteration limit)",
                                                  str(res_content or "")):
                                        task.status = "exhausted"
                                    from axon.agent.state import Conversation
                                    sub_file = getattr(agent, "session", None) and agent.session._file(f"{parent_sess_id}_sub_{number}")
                                    if sub_file and sub_file.exists():
                                        try:
                                            task.conversation = agent.session.read_conversation(f"{parent_sess_id}_sub_{number}")
                                        except Exception:
                                            task.conversation = Conversation([
                                                {"role": "user", "content": prompt_txt},
                                                {"role": "assistant", "content": task.result_text},
                                            ])
                                    else:
                                        task.conversation = Conversation([
                                            {"role": "user", "content": prompt_txt},
                                            {"role": "assistant", "content": task.result_text},
                                        ])
                                    break

    # 2. Discover any additional subagent transcripts on disk (sessions/<id>/subagents/<n>.jsonl)
    s_dir = getattr(agent, "session", None) and getattr(agent.session, "session_dir", None)
    if s_dir and s_dir.exists():
        for sub_path in agent.session._subagent_files(parent_sess_id):
            m_idx = re.search(r"(\d+)$", sub_path.stem)
            if not m_idx:
                continue
            f_idx = int(m_idx.group(1))
            stem = f"{parent_sess_id}_sub_{f_idx}"
            if f_idx in seen_indices:
                continue
            seen_indices.add(f_idx)
            try:
                sub_conv = agent.session.read_conversation(stem)
                p_text = ""
                r_text = ""
                for sm in sub_conv.messages:
                    if sm.get("role") == "user" and not p_text:
                        p_text = sm.get("content", "")
                        if isinstance(p_text, list):
                            p_text = "".join(str(x) for x in p_text)
                    elif sm.get("role") == "assistant":
                        c = sm.get("content", "")
                        if isinstance(c, str) and c:
                            r_text = c
                if not p_text:
                    p_text = f"Subagent Task #{f_idx}"
                task = agent.subagents.register(p_text)
                task.index = f_idx
                task.id = f"sub-{f_idx}"
                task.status = "completed"
                task.result_text = r_text
                task.conversation = sub_conv
            except Exception:
                pass

    if seen_indices:   # numbers parsed from results can be ahead of the count: new subagents must not reuse them
        agent.subagents._counter = max(agent.subagents._counter, max(seen_indices))

    # 3. Synchronize token counts and costs for each task
    model_name = getattr(getattr(agent, "settings", None), "model", None)
    if not isinstance(model_name, str) or not model_name:
        from axon.config import Settings
        model_name = str(Settings.model_fields["model"].default)
    for t in agent.subagents.all_tasks():
        sub_stem = f"{parent_sess_id}_sub_{t.index}"
        if getattr(agent, "session", None) and hasattr(agent.session, "load_ledger"):
            try:
                sub_l = agent.session.load_ledger(sub_stem, model_name)
                if sub_l.total_input_tokens + sub_l.total_output_tokens > 0:
                    t.input_tokens = sub_l.total_input_tokens
                    t.output_tokens = sub_l.total_output_tokens
                    t.tokens_consumed = t.input_tokens + t.output_tokens
            except Exception:
                pass
        if getattr(t, "tokens_consumed", 0) == 0 and t.result_text:
            m_tok = re.search(r"(\d[\d,]*)\s*tokens\s*\(in:\s*(\d[\d,]*)\s*·\s*out:\s*(\d[\d,]*)\)", t.result_text)
            if m_tok:
                t.tokens_consumed = int(m_tok.group(1).replace(",", ""))
                t.input_tokens = int(m_tok.group(2).replace(",", ""))
                t.output_tokens = int(m_tok.group(3).replace(",", ""))

