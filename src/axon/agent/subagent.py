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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from axon.agent.state import Conversation, record_usage
from axon.errors import ToolError
from axon.providers.base import Usage

if TYPE_CHECKING:
    from axon.agent.loop import Agent
    from axon.providers.base import StreamEvent

DEFAULT_SUBAGENT_ITERATIONS = 25
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
        description="Fast read-only codebase exploration: find files, symbols and usages, and answer questions about the code.",
        readonly=True,
        system_prompt=(
            "You are a read-only exploration subagent. Search broadly (Glob/Grep) and read only what you need. "
            "Never modify files. Report concrete findings with exact file paths and line numbers."
        ),
    ),
    AgentDefinition(
        name="plan",
        description="Read-only software architect: studies the code and returns a step-by-step implementation plan.",
        readonly=True,
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
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip().strip("'\"") for t in raw.split(",") if t.strip().strip("'\"")]


def parse_agent_file(path: Path, source: str) -> AgentDefinition | None:
    try:
        raw = path.read_bytes()
        try:
            from axon.config import decode_config_bytes
            text = decode_config_bytes(raw)
        except Exception:
            text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    meta: dict[str, Any] = {}
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            current_list: str | None = None
            for line in lines[1:end]:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                stripped = line.strip()
                if current_list and stripped.startswith("- "):
                    meta.setdefault(current_list, []).append(stripped[2:].strip().strip("'\""))
                    continue
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip().lower()
                val = val.strip()
                current_list = None
                if not val and key in ("tools", "allowed-tools", "allowed_tools", "disallowedtools", "disallowed-tools", "disallowed_tools"):
                    current_list = "disallowed" if key.startswith("disallow") else "tools"
                    meta[current_list] = []
                    continue
                if key in ("allowed-tools", "allowed_tools"):
                    key = "tools"
                if key in ("disallowedtools", "disallowed-tools", "disallowed_tools"):
                    key = "disallowed"
                meta[key] = _parse_list(val) if key in ("tools", "disallowed") else val.strip("'\"")
            body = "\n".join(lines[end + 1:])
    name = str(meta.get("name") or path.stem).strip()
    if not name:
        return None
    tools_val = meta.get("tools")
    tools: list[str] | None = None
    if isinstance(tools_val, list) and tools_val and tools_val != ["*"]:
        tools = tools_val
    model = str(meta.get("model") or "").strip() or None
    disallowed = meta.get("disallowed") if isinstance(meta.get("disallowed"), list) else []
    try:
        max_turns = int(str(meta.get("maxturns") or meta.get("max_turns") or "0")) or None
    except ValueError:
        max_turns = None
    return AgentDefinition(
        name=name,
        description=str(meta.get("description") or f"Custom agent from {path.name}").strip(),
        tools=tools,
        model=model,
        system_prompt=body.strip(),
        source=source,
        readonly=str(meta.get("readonly", "")).lower() in ("true", "yes", "1"),
        path=path,
        disallowed_tools=list(disallowed or []),
        max_turns=max_turns,
    )


def agent_definition_dirs(workspace: Path) -> list[tuple[str, Path]]:
    ws = Path(workspace)
    from axon import storage
    user_dir = storage.user_ext_dir("agents")
    return [
        ("user", user_dir),
        ("project", ws / ".claude" / "agents"),
        ("project", ws / ".axon" / "agents"),
    ]


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
    """Parse ``--agents`` JSON: ``{"name": {"description": ..., "prompt": ..., "tools": [...], "model": ...}}``.
    Raises ValueError on malformed input."""
    import json
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("--agents expects a JSON object mapping agent names to definitions")
    out: list[AgentDefinition] = []
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"agent '{name}' must be an object")
        desc = str(spec.get("description") or "").strip()
        prompt = str(spec.get("prompt") or spec.get("system_prompt") or "").strip()
        if not desc or not prompt:
            raise ValueError(f"agent '{name}' needs 'description' and 'prompt'")
        tools = spec.get("tools")
        if isinstance(tools, str):
            tools = _parse_list(tools)
        disallowed = spec.get("disallowedTools") or spec.get("disallowed_tools") or []
        if isinstance(disallowed, str):
            disallowed = _parse_list(disallowed)
        max_turns = spec.get("maxTurns") or spec.get("max_turns")
        out.append(AgentDefinition(
            name=str(name).strip(), description=desc, system_prompt=prompt,
            tools=[str(t) for t in tools] if isinstance(tools, list) and tools and tools != ["*"] else None,
            model=(str(spec.get("model")).strip() or None) if spec.get("model") else None,
            source="cli", readonly=bool(spec.get("readonly", False)),
            disallowed_tools=[str(t) for t in disallowed] if isinstance(disallowed, list) else [],
            max_turns=int(max_turns) if isinstance(max_turns, (int, str)) and str(max_turns).isdigit() else None,
        ))
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
    for plug in plugins:
        directory = plug.path / "agents"
        try:
            files = sorted(directory.glob("*.md")) if directory.is_dir() else []
        except OSError:
            files = []
        for f in files:
            d = parse_agent_file(f, f"plugin:{plug.name}")
            if d is not None:
                out.append(d)
    return out


def load_agent_definitions(workspace: Path) -> list[AgentDefinition]:
    """Built-in + feature agents, then user and project agent files, then --agents (later override)."""
    by_name: dict[str, AgentDefinition] = {a.name: a for a in BUILTIN_AGENTS}
    for d in feature_agent_definitions():
        by_name.setdefault(d.name, d)
    for d in plugin_agent_definitions(workspace):
        by_name[d.name] = d
    for source, directory in agent_definition_dirs(workspace):
        try:
            files = sorted(directory.glob("*.md")) if directory.is_dir() else []
        except OSError:
            files = []
        for f in files:
            d = parse_agent_file(f, source)
            if d is not None:
                by_name[d.name] = d
    by_name.update(_SESSION_AGENTS)
    return list(by_name.values())


def get_agent_definition(workspace: Path, name: str | None) -> AgentDefinition | None:
    wanted = (name or "general-purpose").strip().lower()
    for d in load_agent_definitions(workspace):
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

    def add_log(self, msg: str) -> None:
        self.live_logs.append(msg)
        if len(self.live_logs) > 30:
            self.live_logs.pop(0)


class SubagentManager:
    """Thread-safe manager for tracking subagents and their progress."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: list[SubagentTask] = []
        self._counter: int = 0
        self._threads: dict[str, int] = {}
        self.on_update: Callable[[list[SubagentTask]], None] | None = None

    def register(self, prompt: str, max_steps: int = 15, title: str | None = None, agent_type: str = "general-purpose") -> SubagentTask:
        with self._lock:
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
            )
            self._tasks.append(task)
            self._notify()
            return task

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
                    t.result_text = result_text
                    t.conversation = conversation
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    if usage:
                        t.input_tokens = getattr(usage, "input", 0) or 0
                        t.output_tokens = getattr(usage, "output", 0) or 0
                        t.tokens_consumed = t.input_tokens + t.output_tokens
                    break
            self._notify()

    def total_tokens(self) -> int:
        with self._lock:
            return sum(t.tokens_consumed for t in self._tasks)

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.status = "error"
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
            return list(self._tasks)

    def get_task(self, query: str) -> SubagentTask | None:
        with self._lock:
            q = str(query).strip().lower()
            for t in self._tasks:
                if t.id == q or str(t.index) == q or t.title.lower().startswith(q):
                    return t
            return None

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._counter = 0
            self._threads.clear()

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
        with self._lock:
            ident = self._threads.get(task_id)
        if not ident:
            return False
        try:
            return ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(KeyboardInterrupt)) == 1
        except Exception:
            return False

    def cancel_all(self) -> int:
        """Interrupt every running subagent; returns how many were signalled."""
        with self._lock:
            running = [tid for tid in self._threads if any(t.id == tid and t.status == "running" for t in self._tasks)]
        return sum(1 for tid in running if self.cancel(tid))


_APPROVAL_LOCK = threading.Lock()


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


def _resolve_model(parent: Agent, agent_def: AgentDefinition) -> str:
    m = (agent_def.model or "").strip()
    if m.lower() in _INHERIT_MODELS:
        return parent.settings.model
    return m


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


def _build_registry(parent: Agent, agent_def: AgentDefinition) -> Any:
    reg = parent.registry
    base = reg.subset(names=agent_def.tools) if agent_def.tools else reg.subset()
    blocked = set(_EXCLUDED_FOR_SUBAGENTS)
    for name in agent_def.disallowed_tools:
        try:
            blocked.add(reg.get(name).name)
        except Exception:
            blocked.add(name)
    names = [t.name for t in base.all_tools() if t.name not in blocked]
    readonly = agent_def.readonly or getattr(parent.settings, "mode", "default") == "plan"
    sub = base.subset(names=names, readonly_only=readonly)
    if readonly:
        sub = sub.subset(names=[t.name for t in sub.all_tools() if t.name not in blocked])
    return sub


def run_subagent(
    prompt: str,
    parent: Agent,
    max_iterations: int | None = None,
    subagent_type: str | None = None,
    description: str = "",
) -> str:
    """Run an isolated subagent turn and return its final report.

    The subagent gets its own Conversation, a restricted tool registry (never the Task
    tool, enforcing depth 1), its own provider instance, and its own session file. Its
    usage is recorded once into the parent ledger with ``source=<agent name>``."""
    if not hasattr(parent, "subagents") or parent.subagents is None:
        parent.subagents = SubagentManager()

    agent_def = get_agent_definition(parent.settings.workspace, subagent_type)
    if agent_def is None:
        available = ", ".join(sorted(d.name for d in load_agent_definitions(parent.settings.workspace)))
        raise ToolError(f"Unknown subagent_type '{subagent_type}'. Available: {available}")

    max_iter = int(max_iterations or agent_def.max_turns or min(DEFAULT_SUBAGENT_ITERATIONS, max(1, parent.settings.max_iterations)))
    model = _resolve_model(parent, agent_def)
    task = parent.subagents.register(prompt, max_steps=max_iter, title=description or None, agent_type=agent_def.name)
    task.model = model
    started = time.time()

    def emit_parent(phase: str, status: str, cost: float = 0.0, usage: Any = None, iterations: int = 0) -> None:
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
            emit_parent("end", "error")
            return f"[Subagent #{task.index} blocked by SubagentStart hook: {msg}]"
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
        if isinstance(event, LLMCallStart):
            parent.subagents.update_progress(task.id, event.iteration)
            task.add_log(f"Step {event.iteration}: model call")
        elif isinstance(event, ToolExecutionStart):
            arg_str = str(event.input)
            if len(arg_str) > 50:
                arg_str = arg_str[:47] + "..."
            task.add_log(f"{event.name}({arg_str})")
        elif isinstance(event, ToolExecutionResult):
            st = "Error" if event.is_error else "Result"
            c_str = str(event.content).strip().replace("\n", " ")
            if len(c_str) > 55:
                c_str = c_str[:52] + "..."
            task.add_log(f"  -> {st}: {c_str}")
        elif isinstance(event, ThinkingDelta):
            th = (getattr(event, "text", "") or "").strip()
            if len(th) > 10:
                task.add_log(f"Thinking: {th[:55]}...")

    from axon.agent.loop import Agent
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.agent.worktree import WorktreeManager, WorktreeInfo

    worktree_info: WorktreeInfo | None = None
    sub_ws = parent.settings.workspace
    if getattr(parent.settings, "isolate_worktrees", False) and not agent_def.readonly:
        try:
            worktree_info = WorktreeManager.create_worktree(parent.settings.workspace, task.id)
            if worktree_info:
                sub_ws = worktree_info.worktree_path
                task.add_log(f"Isolated git worktree: {sub_ws.name}")
                if callable(hook):
                    hook("WorktreeCreate", {"worktree_path": str(sub_ws), "agent_id": task.id})
        except Exception:
            worktree_info = None

    try:
        sub_settings = parent.settings.model_copy(update={"max_iterations": max_iter, "workspace": sub_ws, "model": model})
        parent_sess_id = parent.session.active_session_id.rsplit("_sub_", 1)[0]
        sub_store = SessionStore(sub_ws, session_dir=parent.session.session_dir)
        sub_store.open(f"{parent_sess_id}_sub_{task.index}")
        sub_ledger = Ledger()
        sub_agent = Agent(
            provider=_make_provider(parent, model, sub_settings),
            tools=_build_registry(parent, agent_def),
            permissions=parent.permissions,
            context=parent.context,
            session=sub_store,
            ledger=sub_ledger,
            settings=sub_settings,
            skills=getattr(parent, "skills", None),
            on_event=sub_on_event,
            on_approval=_subagent_approver(parent),
            hooks=getattr(parent, "hooks", None),
            agent_name=agent_def.name,
            extra_system=agent_def.system_prompt,
            git_status=getattr(parent, "_git_snapshot", None),
        )

        sub_prompt = (
            f"{prompt}\n\n"
            "When you are done, reply with a concise final report for the agent that delegated this task: what you did or "
            "found, exact file paths (with line numbers where useful), and any open issues. Your final message is the only "
            "thing the delegating agent will see."
        )
        parent.subagents.bind_thread(task.id, threading.get_ident())
        try:
            result = sub_agent.run_turn(sub_prompt)
        except KeyboardInterrupt:
            parent.subagents.bind_thread(task.id, None)
            parent.subagents.complete(task.id, "[Cancelled by user]", sub_agent.conversation, status="cancelled")
            emit_parent("end", "cancelled")
            return f"[Subagent #{task.index} ({task.title}) was cancelled by the user before finishing.]"
        parent.subagents.bind_thread(task.id, None)
        final_text = (result.final_text or "").strip()
        sub_usage = result.usage
        tokens_total = (sub_usage.input or 0) + (sub_usage.output or 0)

        if worktree_info:
            merged_ok, merge_msg = WorktreeManager.merge_back(worktree_info)
            final_text += f"\n\n[Worktree] {merge_msg}" if not merged_ok or "applied" in merge_msg.lower() else ""
            WorktreeManager.cleanup(worktree_info)
            if callable(hook):
                hook("WorktreeRemove", {"worktree_path": str(worktree_info.worktree_path), "agent_id": task.id})
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
        emit_parent("end", "exhausted" if result.stop_reason == "max_iterations" else
                    ("error" if result.stop_reason in ("error", "interrupted") else "completed"),
                    cost=sub_cost, usage=agg, iterations=result.iterations)

        header = f"[Subagent #{task.index} Result ({task.title}) | {tokens_total:,} tokens (in: {sub_usage.input:,} · out: {sub_usage.output:,})]"
        if result.stop_reason == "max_iterations":
            parent.subagents.complete(task.id, final_text or "[Exhausted iteration ceiling]", sub_agent.conversation, status="exhausted", usage=sub_usage)
            return f"{header}:\nReached the iteration limit ({max_iter} steps) before finishing.\n{final_text}".rstrip()
        if not final_text:
            parent.subagents.complete(task.id, "No text output", sub_agent.conversation, status="completed", usage=sub_usage)
            return f"{header}:\nCompleted ({result.iterations} steps) with no text output."
        status = "error" if result.stop_reason in ("error", "interrupted") else "completed"
        parent.subagents.complete(task.id, final_text, sub_agent.conversation, status=status, usage=sub_usage)
        return f"{header}:\n{final_text}"
    except ToolError:
        raise
    except Exception as e:
        parent.subagents.fail(task.id, str(e))
        emit_parent("end", "error")
        return f"[Subagent #{task.index} failed: {type(e).__name__}: {e}]"
    finally:
        if worktree_info:
            try:
                WorktreeManager.cleanup(worktree_info)
            except Exception:
                pass

def _short_args(inp: Any, limit: int = 80) -> str:
    if isinstance(inp, dict):
        for k in ("path", "file_path", "command", "pattern", "query", "url", "skill"):
            if inp.get(k):
                v = " ".join(str(inp[k]).split())
                return v if len(v) <= limit else v[: limit - 3] + "..."
    v = " ".join(str(inp).split())
    return v if len(v) <= limit else v[: limit - 3] + "..."


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
                                    from axon.agent.state import Conversation
                                    sub_file = getattr(agent, "session", None) and (agent.session.session_dir / f"{parent_sess_id}_sub_{task_idx}.jsonl")
                                    if sub_file and sub_file.exists():
                                        try:
                                            task.conversation = agent.session.read_conversation(f"{parent_sess_id}_sub_{task_idx}")
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

    # 2. Discover any additional subagent session files on disk (_sub_*.jsonl)
    s_dir = getattr(agent, "session", None) and getattr(agent.session, "session_dir", None)
    if s_dir and s_dir.exists():
        pattern = f"{parent_sess_id}_sub_*.jsonl"
        for sub_path in sorted(s_dir.glob(pattern)):
            stem = sub_path.stem
            m_idx = re.search(r"_sub_(\d+)$", stem)
            if not m_idx:
                continue
            f_idx = int(m_idx.group(1))
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

