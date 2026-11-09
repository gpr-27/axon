"""
The ReAct agent loop: reason -> act -> observe -> iterate.

Invariants enforced here:
  1. Pairing       every tool_use gets exactly one tool_result (errors, interrupts, truncation included)
  2. Batching      all results of one assistant turn go back in ONE message
  3. Verbatim      provider-native assistant content is replayed unchanged
  4. Errors=data   tool failures become error results, never exceptions
  5. Interrupts    Ctrl+C always leaves the conversation valid for the next request
"""
from __future__ import annotations
import contextlib
import json
import logging
import os
import re
import threading
import time
import weakref
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterator
from axon import interruptible
from axon.config import Settings
from axon.errors import ToolError
from axon.agent import filechanges
from axon.agent.state import Conversation, FileState, TodoState, record_usage
from axon.agent import large_input, suggest
from axon.tools import diagnostics
from axon.agent.prompt import build_system
from axon.agent.context import ContextManager
from axon.agent.trace import (
    LargeInputEvent,
    BudgetEvent,
    CacheStatus,
    CacheTracker,
    CompactionEvent,
    MemoryLoadedEvent,
    ToolRationale,
    cost_breakdown,
    HookEvent,
    LLMCallEnd,
    LLMRequestInfo,
    PermissionDecisionEvent,
    RetryEvent,
    GoalCheckEvent,
    SteerEvent,
    ThinkingBoost,
    ToolTiming,
    TurnTrace,
    save_trace,
)
from axon.tools.base import ToolContext
from axon.tools.registry import ToolRegistry
from axon.permissions.engine import PermissionEngine
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.providers.base import (
    AssistantTurn,
    LLMCallStart,
    ThinkingBlock,
    Provider,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseStart,
    Usage,
)
from axon.skills.manager import SkillManager
from axon.session.checkpoint import CheckpointManager

# Tools that are not read-only but may still run concurrently with each other.
PARALLEL_SAFE_TOOLS = frozenset({"Task"})
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})
_RETRYABLE_NAMES = ("timeout", "connect", "ratelimit", "overloaded", "internalserver", "remoteprotocol", "readerror")
INTERRUPTED_NOTE = "[Response interrupted by user]"
FILE_PATH_KEYS = ("path", "file_path", "notebook_path")
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "Patch", "NotebookEdit"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "Patch", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash"})   # their file changes are detected by a workspace snapshot
MAX_THRASH_COMPACTIONS = 3
_BASH_READ_RE = None
STEER_PREFIX = "[User added while you were working]:"
SHELL_CONTEXT_CAP = 30_000
POST_COMPACT_FILES = 5
POST_COMPACT_FILE_CHARS = 8_000
# Step limit: a note goes out with the tool results once this many steps (model calls) are left, and at a
# subagent's limit one last call (no tool may run) asks for its report instead of cutting it off mid-work.
STEP_WARNING_LEFT = 3
STEP_WARNING_SUB = ("[Axon: {left} step{s} left before your step limit ({limit}). Stop exploring soon and write "
                    "your final report.]")
STEP_WARNING_MAIN = ("[Axon: {left} step{s} left before the step limit ({limit}). Finish the current change soon "
                     "and wrap up with a summary.]")
WRAP_UP_NOTE = ("[Axon: you reached the step limit ({limit} steps). Tools are no longer available; do not call any. "
                "Write your final report now: what you found (exact file paths and line numbers), what you did "
                "(files changed, commands run and their results), and what remains undone or unverified.]")

# Thinking keywords (most specific first) -> effort tier for that turn.
THINK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("ultrathink", "quantum"),
    ("think harder", "quantum"),
    ("think intensely", "quantum"),
    ("think very hard", "quantum"),
    ("megathink", "synapse"),
    ("think hard", "synapse"),
    ("think deeply", "synapse"),
)
_EFFORT_RANK = {"reflex": 0, "balanced": 1, "synapse": 2, "quantum": 3}


def detect_thinking_keyword(text: str) -> tuple[str, str] | None:
    """(keyword, effort) when the prompt asks for more thinking ("ultrathink", "think hard", ...)."""
    import re
    low = (text or "").lower()
    for kw, effort in THINK_KEYWORDS:
        if re.search(r"(?<![\w-])" + re.escape(kw) + r"(?![\w-])", low):
            return kw, effort
    return None


MAX_OVERFLOW_RETRIES = 2
TITLE_RECENT_PROMPTS = 5
TITLE_SNIPPET_CHARS = 300
TITLE_REFRESH_INSTRUCTION = ("Write a 3-6 word title for this coding session, saying what it is about now. Weigh recent "
                             "work most. If the current title still fits, repeat it exactly. Reply with the title only, "
                             "no quotes or punctuation.")


def heuristic_title(prompt: str, limit: int = 50) -> str:
    """Short session title from the first prompt (no LLM): first sentence, markdown stripped."""
    import re
    try:
        from axon.session.store import clean_session_topic
        text = clean_session_topic(prompt)
    except Exception:
        text = " ".join(str(prompt or "").split())
    text = re.sub(r"<[^>]+>[\s\S]*?</[^>]+>", "", text)
    text = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].strip(" .!?:;,")
    if len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[: cut if cut > limit // 2 else limit].rstrip(" ,;:") + "…"
    return text


def _bash_read_target(command: str) -> str | None:
    """`cat x` / `head -n 5 x` / `Get-Content x` of a single file counts as a read."""
    import re
    global _BASH_READ_RE
    if _BASH_READ_RE is None:
        _BASH_READ_RE = re.compile(r"^\s*(?:cat|head|tail|less|more|type|Get-Content|gc)\s+(?:-{1,2}[\w=-]+\s+(?:\d+\s+)?)*(\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)\s*$", re.IGNORECASE)
    m = _BASH_READ_RE.match(command or "")
    return m.group(1).strip("\"'") if m else None


def set_terminal_title(title: str) -> None:
    """OSC 0 window title ("axon · <session name>") on interactive terminals."""
    import sys
    try:
        if not (sys.stdout.isatty() and title):
            return
        clean = "".join(ch for ch in title if ch.isprintable())[:80]
        sys.stdout.write(f"\033]0;axon · {clean}\007")
        sys.stdout.flush()
    except Exception:
        pass


def tool_rationales(turn: AssistantTurn, registry: Any = None, previous_tools: list[ToolUseBlock] | None = None) -> dict[str, tuple[str, str]]:
    """Map tool_use id -> (rationale, source). The text is the model's own sentence that best explains
    the call (one naming its arguments or the tool wins; see ``decision.pick_rationale``), from its
    text or else its thinking; otherwise a deterministic tag. Never invented."""
    from axon.agent.decision import pick_rationales
    edited = set()
    for tu in previous_tools or []:
        if tu.name in WRITE_TOOLS and isinstance(tu.input, dict):
            edited.add(str(tu.input.get("path") or tu.input.get("file_path") or ""))
    blocks: list[tuple[str, Any]] = []
    for b in turn.blocks:
        if isinstance(b, TextBlock):
            blocks.append(("text", b.text))
        elif isinstance(b, ThinkingBlock):
            blocks.append(("thinking", b.text))
        elif isinstance(b, ToolUseBlock):
            blocks.append(("tool", (b.id, b.name, b.input)))
    out = pick_rationales(blocks)
    for b in turn.tool_uses:
        if b.id in out:
            continue
        tag = ""
        target = str(b.input.get("path") or b.input.get("file_path") or "") if isinstance(b.input, dict) else ""
        if edited and (b.name == "Bash" or (target and target in edited)):
            tag = "verifies previous edit"
        else:
            try:
                if registry is not None and registry.get(b.name).readonly:
                    tag = "read-only lookup"
            except Exception:
                tag = ""
        if tag:
            out[b.id] = (tag, "heuristic")
    return out


_log = logging.getLogger("axon.agent")
# Provider-call events go to a child logger so `--debug api` selects them (`--debug agent` still does too).
_api_log = logging.getLogger("axon.agent.api")
_DEBUG_SKIP = ("TextDelta", "ThinkingDelta", "ToolArgsDelta")
_API_EVENTS = ("LLMCallStart", "LLMRequestInfo", "LLMCallEnd", "CacheStatus", "ProviderRetry", "RetryEvent")


def _debug_event(agent_name: str, event: Any) -> None:
    name = type(event).__name__
    if name in _DEBUG_SKIP:
        return
    try:
        from dataclasses import asdict, is_dataclass
        data = asdict(event) if is_dataclass(event) else str(event)
        text = json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        text = repr(event)
    (_api_log if name in _API_EVENTS else _log).debug("[%s] %s %s", agent_name, name, text[:2000])


_PROVIDER_LOCKS: dict[int, threading.RLock] = {}
_PROVIDER_LOCKS_GUARD = threading.Lock()


def _provider_lock(provider: Any) -> threading.RLock:
    """Providers keep per-call state between stream() and finalize(); calls on one
    shared provider instance (e.g. parallel subagents) must not interleave."""
    with _PROVIDER_LOCKS_GUARD:
        lock = _PROVIDER_LOCKS.get(id(provider))
        if lock is None:
            lock = threading.RLock()
            _PROVIDER_LOCKS[id(provider)] = lock
            _forget_lock_when_collected(provider)
        return lock


def _forget_lock_when_collected(provider: Any) -> None:
    """Drop the provider's lock once the provider is garbage collected, so the table does not grow."""
    try:
        weakref.finalize(provider, _PROVIDER_LOCKS.pop, id(provider), None)
    except TypeError:  # not weak-referenceable: keep the lock
        pass


@contextlib.contextmanager
def _pause_input() -> Any:
    """Pause the in-flight keystroke listener and live spinners so prompts can read stdin."""
    with contextlib.ExitStack() as stack:
        try:
            from axon.ui.in_flight import paused  # type: ignore[attr-defined]
            stack.enter_context(paused())
        except Exception:
            pass
        try:
            from axon.ui.render import pause_spinners  # type: ignore[attr-defined]
            stack.enter_context(pause_spinners())
        except Exception:
            pass
        yield


def _is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    name = type(exc).__name__.lower()
    cause = type(exc.__cause__).__name__.lower() if exc.__cause__ else ""
    return any(k in name or k in cause for k in _RETRYABLE_NAMES)


def make_provider(model: str, settings: Any, role: str = "main") -> Any:
    """provider_for(model, settings, role=...) for a model that may belong to another provider than the main one."""
    from axon.providers.registry import provider_for, settings_for_model
    settings = settings_for_model(model, settings)
    if getattr(settings, "model", model) != model:
        settings = settings.model_copy(update={"model": model})
    try:
        return provider_for(model, settings, role=role)
    except TypeError:
        return provider_for(model, settings)


def _fed_back_fact(t: dict[str, Any]) -> dict[str, Any]:
    """The facts the timeline shows for one tool result fed into the next call (no full output)."""
    return {k: t.get(k) for k in ("id", "name", "target", "lines", "bytes", "tokens", "is_error") if k in t}


def classify_stop_failure(text: str) -> str:
    """Map an API error message to the StopFailure ``error`` type Claude Code reports."""
    t = (text or "").lower()
    table = (
        ("rate_limit", ("rate limit", "rate_limit", "429", "too many requests")),
        ("overloaded", ("overloaded", "529")),
        ("authentication_failed", ("authentication", "invalid api key", "unauthorized", "401", "invalid x-api-key")),
        ("billing_error", ("billing", "credit balance", "insufficient", "payment required", "402", "quota")),
        ("model_not_found", ("model not found", "model_not_found", "does not exist", "unknown model")),
        ("max_output_tokens", ("max_tokens", "max output", "maximum output")),
        ("invalid_request", ("invalid_request", "invalid request", "400", "bad request")),
        ("server_error", ("500", "502", "503", "504", "server error", "internal error", "bad gateway", "unavailable")),
    )
    for name, needles in table:
        if any(n in t for n in needles):
            return name
    return "unknown"


def _stop_hook_cap() -> int:
    """Consecutive continuations Stop hooks may force before the turn ends (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP)."""
    try:
        return max(1, int(os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP") or os.environ.get("AXON_STOP_HOOK_BLOCK_CAP") or 8))
    except ValueError:
        return 8


def _as_float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _sync_project_guide(*_args: Any, **_kwargs: Any) -> None:
    """Deprecated no-op. Axon never writes axon.md on its own; /init creates it explicitly."""
    return None


@dataclass
class BackgroundTurn:
    """A turn detached with Ctrl+B: it keeps running on its own thread while the prompt is free."""
    id: str
    prompt: str
    started_at: float
    status: str = "running"          # running | completed | error | interrupted
    ended_at: float = 0.0
    result: Any = None               # TurnResult when finished
    error: str = ""
    log: list[str] = field(default_factory=list)
    reported: bool = False           # the REPL already printed the outcome
    thread: Any = None

    @property
    def elapsed_s(self) -> float:
        return max(0.0, (self.ended_at or time.time()) - self.started_at)

    @property
    def done(self) -> bool:
        return self.status != "running"

    def add_log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > 200:
            del self.log[:50]


@dataclass
class TurnResult:
    final_text: str
    stop_reason: str
    iterations: int
    tool_calls_count: int
    usage: Usage
    error: str | None = None


class Agent:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        permissions: PermissionEngine,
        context: ContextManager,
        session: SessionStore,
        ledger: Ledger,
        settings: Settings,
        skills: SkillManager | None = None,
        checkpoints: CheckpointManager | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        on_approval: Callable[[str, dict[str, Any], Any], str] | None = None,
        hooks: Any = None,
        agent_name: str = "main",
        extra_system: str = "",
        git_status: str | None = None,
    ) -> None:
        self.provider = provider
        self.registry = tools
        self.permissions = permissions
        self.context = context
        self.session = session
        self.ledger = ledger
        self.settings = settings
        self.skills = skills or SkillManager(settings.workspace)
        self.checkpoints = checkpoints or CheckpointManager(settings.workspace)
        self.on_event = on_event
        self.on_approval = on_approval
        self.agent_name = agent_name
        self.is_subagent = agent_name != "main"
        self.extra_system = extra_system
        self.max_retries = 2
        self.retry_backoff_s = 1.0

        self.conversation = Conversation()
        self.file_state = FileState()
        self.todos = TodoState()
        self.todos.bind(self.settings.workspace, lambda: getattr(getattr(self, "session", None), "active_session_id", "") or "")
        from axon.agent.state import MessageQueue
        self.message_queue: MessageQueue = MessageQueue()
        self.renderer: Any = None
        from axon.agent.subagent import SubagentManager
        self.subagents: SubagentManager = SubagentManager()

        if hooks is None:
            try:
                from axon.hooks import HookManager
                hooks = HookManager(settings, settings.workspace)
            except Exception:
                hooks = None
        self.hooks = hooks
        self._install_hook_callbacks()
        self._git_snapshot: str | None = git_status
        self._session_context: str = ""
        self._session_started = False
        self._turn_counter = 0
        self._approval_lock = threading.Lock()
        self._partial_results: list[ToolResultBlock | None] = []
        self._last_cut_usage = Usage()
        self._cut_usage = Usage()       # tokens the provider reported before a stream was cut (still billed to the person)
        self._current_turn_prompt = ""
        # Set when the last turn ended on a provider error: {"prompt": str, "resume": bool}. /retry uses it.
        self.last_failed_turn: dict[str, Any] | None = None
        self._pending_summary: str | None = None
        self.last_compaction: str | None = None
        self.traces: list[TurnTrace] = []
        self._trace: TurnTrace | None = None
        self._last_call_window: tuple[float, float] = (0.0, 0.0)
        self._batch_parallel = False
        self._held_events: list[Any] | None = None  # subagent events queued while the live monitor is drawn
        self._cache = CacheTracker()
        self._memory: Any = None
        self._memory_text: str | None = None
        self._memory_lock = threading.Lock()
        self._consecutive_compactions = 0
        self._budget_warned: set[str] = set()
        self._notice_marks: set[str] = set()      # budget notices (50, 80, 100 percent) this chat already gave
        self._prev_round_tools: list[ToolUseBlock] = []
        self._rationales: dict[str, tuple[str, str]] = {}
        self._tool_timings: dict[str, float] = {}
        self._pending_context: list[str] = []       # `!cmd` output etc., prepended to the next prompt
        self._hook_stop: str = ""                    # set when a hook returned continue:false mid-turn
        self.decline_ends_turn = False               # set where a person answers prompts (the terminal, the web page)
        self._declined: bool = False                 # they answered a prompt with a plain No this turn
        self._prompt_id: str = ""
        self._post_compact_context: str = ""        # files re-read after a compaction
        self._repo_map: str | None = None
        self.started_at = time.time()
        self._titled: bool | None = None            # None = unknown (check the transcript once)
        self._title: str | None = None
        self._title_auto: bool | None = None        # True: Axon wrote the name, so later turns may rewrite it
        self._title_turns = 0
        self.goal: str | None = getattr(settings, "goal", None)   # /goal (P-CMD sets it)
        self.pending_input: str | None = None       # prefilled into the next prompt (rewind, /diff "a")
        self.title_provider: Any = None             # optional provider for LLM titles (small_model)
        self.suggestions = suggest.SuggestionBox()  # the guessed next prompt, shown as ghost text
        self.suggest_provider: Any = None           # optional provider for it (small_model); built on demand
        # Background turns (Ctrl+B): id -> BackgroundTurn
        self.background_tasks: dict[str, BackgroundTurn] = {}
        self._turn_thread: threading.Thread | None = None
        self._last_cancel_at = 0.0                  # when Esc last interrupted the turn (repeat presses are ignored)
        self._side_provider: Any = None             # a second provider streaming for this turn (compaction)
        self._bg_request = threading.Event()
        self._bg_counter = 0
        self._active_bg: BackgroundTurn | None = None

    # ── events / tracing ────────────────────────────────────────────
    def _emit(self, event: Any) -> None:
        if _log.isEnabledFor(logging.DEBUG):
            _debug_event(self.agent_name, event)
        if self._held_events is not None:
            # The live subagent monitor owns the screen while it is drawn; worker threads must not print into it.
            self._held_events.append(event)
            return
        self._deliver(event)

    def _deliver(self, event: Any, show: bool = True) -> None:
        trace = self._trace
        if trace is not None:
            try:
                trace.record(event)
            except Exception:
                pass
        if self.on_event and show:
            self.on_event(event)

    @property
    def last_trace(self) -> TurnTrace | None:
        return self.traces[-1] if self.traces else None

    # ── hook plumbing ───────────────────────────────────────────────
    def _install_hook_callbacks(self) -> None:
        """Give the hook manager what prompt/agent/mcp_tool hooks need from the running agent."""
        h = self.hooks
        if self.is_subagent or h is None or not hasattr(h, "llm"):
            return          # a subagent shares its parent's hooks: they keep calling the parent
        h.llm = self._hook_llm
        h.mcp_call = self._hook_mcp_call
        h.on_async = self._on_async_hook

    def _hook_llm(self, prompt: str, model: str | None, timeout: float, agent: bool = False) -> str:
        """One model call for a ``prompt``/``agent`` hook (cheap model unless the hook names one).
        Agent hooks run as a single evaluation without a tool loop."""
        want = model or self.settings.small_model or self.settings.model
        prov = self.provider
        if want != self.settings.model and type(prov).__module__.startswith("axon.providers"):
            try:
                prov = make_provider(want, self.settings, role="hook")
            except Exception:
                prov, want = self.provider, self.settings.model
        system = [{"type": "text", "text": "You evaluate a lifecycle hook for a coding agent. Follow the instructions "
                                           "and reply with only the JSON object requested."}]

        caller: dict[str, int] = {}

        def call() -> Any:
            caller["thread"] = threading.get_ident()
            with _provider_lock(prov):
                for _ in prov.stream(model=want, system=system, messages=[{"role": "user", "content": prompt}],
                                     tools=[], max_tokens=800, effort="reflex", thinking=False):
                    pass
                return prov.finalize()

        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="axon-hook-llm")
        try:
            turn = interruptible.wait_future(ex.submit(call), timeout=max(1.0, float(timeout)))
        except FuturesTimeout as e:
            raise TimeoutError(str(e)) from e
        except KeyboardInterrupt:
            cancel = getattr(prov, "cancel", None)
            if callable(cancel) and "thread" in caller:
                cancel(caller["thread"])     # Esc: stop the hook's model call too
            raise
        finally:
            ex.shutdown(wait=False)
        try:
            if getattr(turn, "usage", None) is not None:
                cost = _as_float(record_usage(self.ledger, want, turn.usage, source="hook"))
                self._log_usage(want, turn.usage, cost, "hook")
        except Exception:
            pass
        return str(turn.text or "")

    def _hook_mcp_call(self, server: str, tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run an MCP tool for an ``mcp_tool`` hook through the normal tool registry."""
        try:
            tool = self.registry.get(f"mcp__{server}__{tool_name}")
        except ToolError as e:
            return str(e), True
        ctx = ToolContext(workspace=self.settings.workspace, file_state=self.file_state, todos=self.todos,
                          settings=self.settings, ledger=self.ledger, agent=self, checkpoints=self.checkpoints)
        try:
            return str(tool.run(args, ctx)), False
        except Exception as e:
            return str(e), True

    @staticmethod
    def _hook_tool_output(value: Any) -> str:
        """A hook's ``updatedToolOutput`` as the text the model sees."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("stdout"), str):
            err = value.get("stderr")
            return value["stdout"] + (f"\n{err}" if isinstance(err, str) and err else "")
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def _apply_permission_updates(self, updates: list[dict[str, Any]]) -> None:
        """``updatedPermissions`` from a PermissionRequest hook: session rules and mode changes
        (other destinations are not written to settings files from a hook)."""
        for upd in updates:
            kind = str(upd.get("type") or "")
            try:
                if kind == "addRules":
                    behavior = str(upd.get("behavior") or "allow")
                    for r in upd.get("rules") or []:
                        spec = str(r.get("toolName") or "")
                        if r.get("ruleContent"):
                            spec = f"{spec}({r['ruleContent']})"
                        if spec and behavior in ("allow", "deny", "ask"):
                            self.permissions.add_session_rule(behavior, spec)
                elif kind == "setMode":
                    mode = {"acceptEdits": "acceptEdits", "plan": "plan", "default": "default", "manual": "default",
                            "bypassPermissions": "bypass", "dontAsk": "dontAsk"}.get(str(upd.get("mode") or ""))
                    if mode:
                        self.set_mode(mode)
            except Exception:
                continue

    def expansion_hook(self, command_name: str, args: str, source: str, prompt: str,
                       expansion_type: str = "slash_command") -> tuple[bool, str, str]:
        """UserPromptExpansion hooks for a typed command that expands into a prompt.
        Returns (allowed, extra_context, message)."""
        res = self._hook("UserPromptExpansion", {
            "expansion_type": expansion_type, "command_name": command_name, "command_args": args,
            "command_source": source, "prompt": prompt})
        if res is None:
            return True, "", ""
        if res.blocked or getattr(res, "stop", False):
            return False, "", res.message or getattr(res, "stop_reason", "") or "blocked by a UserPromptExpansion hook"
        return True, res.additional_context or "", ""

    def model_switch_hook(self, phase: str, from_model: str, to_model: str, requested: str | None = None,
                          source: str = "command") -> tuple[bool, str]:
        """PreModelSwitch (phase "pre": returns (allowed, reason)) / PostModelSwitch (phase "post": the hook's
        context is queued for the next prompt)."""
        payload = {"from_model": from_model, "to_model": to_model, "requested_model": requested, "source": source,
                   "context_tokens": int(self.conversation.token_estimate() or 0) if self.conversation.messages else 0}
        if phase == "pre":
            res = self._hook("PreModelSwitch", payload)
            if res is None:
                return True, ""
            if res.blocked or res.permission_decision in ("deny", "ask") or getattr(res, "stop", False):
                return False, res.message or res.permission_reason or "blocked by a PreModelSwitch hook"
            return True, ""
        res = self._hook("PostModelSwitch", payload)
        if res is not None and res.additional_context:
            self._pending_context.append(f"<system-reminder>\n{res.additional_context}\n</system-reminder>")
        return True, ""

    def _on_async_hook(self, rewake: bool) -> None:
        """A background hook produced output; it is delivered with the next prompt or tool result."""

    def _absorb_async_hooks(self) -> str:
        """Text from finished background hooks (empty when there is none)."""
        h = self.hooks
        if h is None or not hasattr(h, "drain_async"):
            return ""
        try:
            msgs = h.drain_async()
        except Exception:
            return ""
        return "\n\n".join(f"<system-reminder>\n{m}\n</system-reminder>" for m in msgs) if msgs else ""

    @staticmethod
    def _emit_terminal_sequence(seq: str) -> None:
        """Write an allowlisted OSC/BEL sequence a hook returned (``terminalSequence``) to the terminal."""
        import sys
        try:
            if sys.stdout.isatty():
                sys.stdout.write(seq)
                sys.stdout.flush()
        except Exception:
            pass

    # ── session lifecycle ───────────────────────────────────────────
    def _hook(self, event: str, payload: dict[str, Any], tool_use_id: str = "") -> Any:
        if self.hooks is None:
            return None
        try:
            if hasattr(self.hooks, "has") and not self.hooks.has(event):
                return None
            sid = getattr(self.session, "active_session_id", "") or ""
            self.hooks.session_id = sid
            sdir = getattr(self.session, "session_dir", None)
            if sdir is not None and sid:
                self.hooks.transcript_path = str(self.session._file(sid)) if hasattr(self.session, "_file") else str(sdir / f"{sid}.jsonl")
            payload = {"agent_name": self.agent_name, **payload}
            if self.is_subagent:
                payload.setdefault("agent_type", self.agent_name)
            if tool_use_id and event != "PermissionRequest":
                payload.setdefault("tool_use_id", tool_use_id)
            self.hooks.prompt_id = getattr(self, "_prompt_id", "") or ""
            try:
                self.hooks.cwd = str(self.settings.workspace)
            except Exception:
                pass
            res = self.hooks.run(event, payload)
        except Exception:
            return None
        warnings = list(getattr(res, "warnings", None) or [])
        sysmsg = str(getattr(res, "system_message", "") or "").strip()
        if sysmsg:
            warnings.append(f"hook message: {sysmsg}")
        seq = str(getattr(res, "terminal_sequence", "") or "")
        if seq and not self.is_subagent:
            self._emit_terminal_sequence(seq)
        if getattr(res, "ran", 0):
            try:
                self._emit(HookEvent(
                    event=event,
                    ran=int(res.ran),
                    blocked=bool(res.blocked or getattr(res, "stop", False)),
                    message=str(res.message or getattr(res, "stop_reason", "") or ""),
                    additional_context=str(res.additional_context or ""),
                    permission_decision=getattr(res, "permission_decision", None),
                    warnings=tuple(warnings),
                    tool_name=str(payload.get("tool_name", "")),
                    tool_use_id=tool_use_id,
                ))
            except Exception:
                pass
            try:
                from axon.session.library import preview, record_run
                record_run("hook_run", {
                    "event": event, "ran": int(res.ran), "blocked": bool(res.blocked or getattr(res, "stop", False)),
                    "message": preview(res.message or getattr(res, "stop_reason", "") or ""),
                    "additional_context": preview(res.additional_context or ""),
                    "permission_decision": getattr(res, "permission_decision", None),
                    "warnings": [str(w) for w in warnings], "tool_name": str(payload.get("tool_name", "")),
                    "tool_use_id": tool_use_id,
                }, agent=self)
            except Exception:
                pass
        if warnings and self.settings.verbose:
            import sys
            for w in warnings:
                try:
                    sys.stderr.write(f"  hook warning: {w}\n")
                except Exception:
                    pass
        return res

    def journal(self, kind: str, data: Any = None, sync: bool = False) -> None:
        """Append to the project's activity journal (axon.session.activity); never raises.
        Subagents write to the main project's journal under their own session id."""
        try:
            from axon.session import activity
            ws = None if self.is_subagent else self.settings.workspace
            if self.is_subagent and isinstance(data, dict):
                data = {**data, "agent": self.agent_name}
            activity.record(kind, data, workspace=ws, sync=sync,
                            session_id=str(getattr(self.session, "active_session_id", "") or ""))
        except Exception:
            pass

    def start_session(self, source: str = "startup") -> Any:
        """Fire SessionStart hooks once; their context is added to the system prompt."""
        self._session_started = True
        self._bind_checkpoints()
        if not self.is_subagent:
            self.record_mode()
            try:
                from axon.session import activity
                activity.bind(self, workspace=self.settings.workspace)
            except Exception:
                pass
            self.journal("session_start", {"source": source, "model": self.settings.model, "mode": self.settings.mode,
                                           "workspace": str(self.settings.workspace), "cwd": os.getcwd(),
                                           "transcript": str(getattr(self.session, "active_file", "") or "")})
            if source == "startup":
                # Save a version of every skill/agent/memory/... file that changed outside Axon.
                from axon.session.library import sync_all
                threading.Thread(target=sync_all, args=(self.settings.workspace,), daemon=True,
                                 name="axon-library-sync").start()
        res = self._hook("SessionStart", {"source": source, "model": self.settings.model})
        if res is not None and getattr(res, "additional_context", ""):
            self._session_context = res.additional_context
        if res is not None and getattr(res, "session_title", "") and not self.is_subagent:
            try:
                self.session.rename_session(res.session_title)
            except Exception:
                pass
        if res is not None and getattr(res, "reload_skills", False):
            try:
                self.skills.reload()
            except Exception:
                pass
        if self.hooks is not None and getattr(self.hooks, "has", lambda _e: False)("InstructionsLoaded"):
            try:
                for node in list(getattr(self.memory, "loaded", []) or []):
                    self._hook("InstructionsLoaded", {"file_path": str(getattr(node, "path", "")),
                                                      "load_reason": "session_start",
                                                      "memory_type": str(getattr(node, "reason", ""))})
            except Exception:
                pass
        return res

    def _bind_checkpoints(self) -> None:
        """Persist /rewind snapshots under the active session id."""
        sid = str(getattr(self.session, "active_session_id", "") or "")
        cp = self.checkpoints
        if sid and cp is not None and hasattr(cp, "set_session") and getattr(cp, "session_id", None) != sid:
            try:
                cp.set_session(sid)
            except Exception:
                pass

    def record_mode(self, mode: str | None = None) -> None:
        """Persist the permission mode in the transcript so a resume can restore it."""
        try:
            self.session.append("permission_mode", {"mode": mode or self.settings.mode})
        except Exception:
            pass

    def set_mode(self, mode: str) -> None:
        """Switch the permission mode (settings + permission engine) and record it."""
        previous = self.settings.mode
        self.settings = self.settings.model_copy(update={"mode": mode})
        try:
            self.permissions.settings = self.settings
        except Exception:
            pass
        self.record_mode(mode)
        if previous != mode:
            self.journal("mode", {"from": previous, "to": mode})

    def end_session(self, reason: str = "exit") -> Any:
        return self._hook("SessionEnd", {"reason": reason})

    def _provider_name(self, model: str) -> str:
        """Provider shown next to the model in the timeline; '' when it cannot be determined."""
        try:
            from axon.ui.render import provider_name
            return provider_name(self.settings, model)
        except Exception:
            return ""

    def display_model(self) -> str:
        """The model actually called: an alias such as "opus" or "default" resolved to the real id the
        provider receives (what the timeline, status line and cost should show)."""
        model = str(self.settings.model or "")
        wire = getattr(self.provider, "_wire_model", None)
        if callable(wire):
            try:
                return str(wire(model) or model)
            except Exception:
                pass
        try:
            from axon.providers.catalog import is_model_alias, resolve_model_alias
            if is_model_alias(model):
                return resolve_model_alias(model)
        except Exception:
            pass
        return model

    def notify(self, message: str, notification_type: str = "info", desktop: bool = True) -> Any:
        """Run the Notification hook and, for events that need you, send a desktop notification
        (policy in axon.ui.notify: permission prompts, questions, failures, long turns)."""
        res = self._hook("Notification", {"message": message, "notification_type": notification_type})
        if desktop and not self.is_subagent:
            try:
                from axon.ui.notify import notify_event
                notify_event(notification_type, message, settings=self.settings)
            except Exception:
                pass
        return res

    def reset_for_new_session(self, session_id: str | None = None) -> str:
        """Completely refresh state for a fresh session."""
        old_id = str(getattr(self.session, "active_session_id", "") or "")
        self.journal("clear", {"old_session": old_id, "messages": len(self.conversation.messages),
                               "transcript": str(getattr(self.session, "active_file", "") or "")}, sync=True)
        self.conversation = Conversation([])
        if getattr(self, "ledger", None) is not None:
            self.ledger.clear()
        else:
            self.ledger = Ledger()
        for attr in ("file_state", "todos", "message_queue", "subagents", "checkpoints"):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "clear"):
                obj.clear()
        self._session_context = ""
        self.last_failed_turn = None
        self._git_snapshot = None
        self._pending_context = []
        from axon.commands.workspace_cmds import stop_loops
        stop_loops(self)                 # scheduled prompts belong to the session that asked for them
        self._post_compact_context = ""
        self._repo_map = None
        self._titled = None
        self._title = None
        self._title_auto = None
        self._title_turns = 0
        self.reload_memory()
        self.traces = []
        self._cache = CacheTracker()
        new_id = self.session.open(session_id)
        self._bind_checkpoints()
        if self._session_started:
            self.start_session("clear")
        return new_id

    # ── extra context: `!` shell output ────────────────────────────
    def add_shell_context(self, command: str, output: str, exit_code: int | None = None) -> str:
        """Record a user-run `!command` so the model sees it with the next prompt (CC bash mode).
        Returns the context block that was queued."""
        out = output or ""
        full_path = ""
        if len(out) > SHELL_CONTEXT_CAP:
            try:
                from axon import storage
                saved = storage.save_full_output(self.settings.workspace, str(getattr(self.session, "active_session_id", "")),
                                                 f"shell-{int(time.time())}", out)
                full_path = str(saved) if saved else ""
            except Exception:
                full_path = ""
            half = SHELL_CONTEXT_CAP // 2
            note = f"; full output: {full_path}" if full_path else ""
            out = f"{out[:half]}\n[... {len(out) - 2 * half:,} characters omitted{note} ...]\n{out[-half:]}"
        code = "" if exit_code is None else f"\n<bash-exit-code>{exit_code}</bash-exit-code>"
        block = (f"<bash-input>{command}</bash-input>\n<bash-stdout>{out.rstrip()}</bash-stdout>{code}\n"
                 "(The user ran this command in the terminal; its output is shown for context.)")
        self._pending_context.append(block)
        try:
            self.session.append("shell_command", {"command": command, "output": out, "exit_code": exit_code,
                                                  **({"full_output": full_path} if full_path else {})})
        except Exception:
            pass
        self._last_shell_full_output = full_path
        return block

    def take_pending_context(self) -> str:
        if not self._pending_context:
            return ""
        text = "\n\n".join(self._pending_context)
        self._pending_context = []
        return text

    # ── background turns (Ctrl+B) ──────────────────────────────────
    @property
    def busy(self) -> bool:
        """True while a turn runs on a worker thread (foreground or backgrounded)."""
        t = self._turn_thread
        return t is not None and t.is_alive()

    def background_current_turn(self) -> BackgroundTurn:
        """Detach the running turn: it keeps working on its thread, its live output is captured into
        ``background_tasks[id].log`` instead of the terminal, approvals are declined (nobody is
        watching), and the foreground waiter returns control to the prompt."""
        if not self.busy:
            raise RuntimeError("no turn is running on a worker thread")
        if self._active_bg is not None and not self._active_bg.done:
            return self._active_bg
        self._bg_counter += 1
        rec = BackgroundTurn(id=f"bg-{self._bg_counter}", prompt=self._current_turn_prompt or "",
                             started_at=time.time(), thread=self._turn_thread)
        self.background_tasks[rec.id] = rec
        self._active_bg = rec
        self._fg_on_event = self.on_event
        self._fg_on_approval = self.on_approval
        self.on_event = self._background_sink(rec)
        self.on_approval = None if self.on_approval is None else self._background_approval
        try:
            from axon.ui.render import _ACTIVE_SPINNERS  # type: ignore[attr-defined]
            for sp in list(_ACTIVE_SPINNERS):
                sp.stop()
        except Exception:
            pass
        self._hook("TaskCreated", {"task_id": rec.id, "task_type": "background_turn", "task_subject": rec.prompt.strip().splitlines()[0][:200] if rec.prompt.strip() else "",
                                    "task_description": rec.prompt[:2000], "prompt": rec.prompt[:2000]})
        self.journal("background", {"action": "start", "id": rec.id, "prompt": rec.prompt[:500]})
        self._bg_request.set()
        return rec

    def _background_sink(self, rec: BackgroundTurn) -> Callable[[Any], None]:
        def sink(event: Any) -> None:
            try:
                if isinstance(event, ToolExecutionStart):
                    rec.add_log(f"{event.name} {json.dumps(event.input, default=str)[:120]}")
                elif isinstance(event, ToolExecutionResult):
                    rec.add_log(("  error: " if event.is_error else "  -> ") + " ".join(str(event.content).split())[:120])
                elif isinstance(event, LLMCallStart):
                    rec.add_log(f"step {event.iteration}: model call")
            except Exception:
                pass
        return sink

    @staticmethod
    def _background_approval(tool: str, args: dict[str, Any], decision: Any) -> str:
        return "deny"

    def _finish_background(self, result: Any, error: BaseException | None) -> None:
        rec = self._active_bg
        if rec is None:
            return
        rec.ended_at = time.time()
        rec.result = result
        if error is not None:
            rec.status = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
            rec.error = f"{type(error).__name__}: {error}"
        else:
            rec.status = "error" if getattr(result, "stop_reason", "") == "error" else "completed"
        self.on_event = getattr(self, "_fg_on_event", self.on_event)
        self.on_approval = getattr(self, "_fg_on_approval", self.on_approval)
        self._active_bg = None
        self._hook("TaskCompleted", {"task_id": rec.id, "task_type": "background_turn", "status": rec.status,
                                     "task_subject": rec.prompt.strip().splitlines()[0][:200] if rec.prompt.strip() else "",
                                     "last_assistant_message": str(getattr(result, "final_text", "") or "")[:4000]})
        self.journal("background", {"action": "finish", "id": rec.id, "status": rec.status, "error": rec.error or "",
                                    "elapsed_s": round(rec.ended_at - rec.started_at, 3)}, sync=True)
        self.notify(f"Background turn {rec.id} {rec.status}", "background_task")

    def run_turn_threaded(self, user_input: str, poll_s: float = 0.1) -> TurnResult | None:
        """Run a turn on a worker thread so it can be backgrounded (Ctrl+B) or interrupted.

        Returns the TurnResult, or None when the turn was moved to the background. A Ctrl+C in the
        waiting thread is forwarded to the worker; exceptions from the turn are re-raised here."""
        if self.busy:
            raise RuntimeError("a turn is already running")
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                try:
                    box["result"] = self.run_turn(user_input)
                except BaseException as e:  # noqa: BLE001 - re-raised in the waiting thread
                    box["error"] = e
                finally:
                    if self._active_bg is not None and threading.current_thread() is self._active_bg.thread:
                        self._finish_background(box.get("result"), box.get("error"))
            except KeyboardInterrupt:
                pass  # an Esc that landed after the turn had finished: nothing left to stop
            finally:
                done.set()

        self._bg_request.clear()
        self._last_cancel_at = 0.0
        t = threading.Thread(target=work, name="axon-turn", daemon=True)
        self._turn_thread = t
        t.start()
        # A cancel that lands after the turn finished can kill the worker before it sets `done`:
        # a dead worker ends the wait too.
        while not done.is_set() and t.is_alive():
            try:
                done.wait(poll_s)
            except KeyboardInterrupt:
                self.cancel_current_turn()
                continue
            if self._bg_request.is_set() and not done.is_set():
                return None
        if "error" in box:
            raise box["error"]
        if "result" not in box:
            raise KeyboardInterrupt()
        return box["result"]

    # A second Esc this soon after the first only aborts the model stream again: another
    # KeyboardInterrupt would land in the clean-up the first one started.
    REPEAT_CANCEL_S = 1.0

    def cancel_current_turn(self) -> bool:
        """Interrupt the running turn: a KeyboardInterrupt is raised inside the worker thread (or in
        the main thread when the turn runs there)."""
        t = self._turn_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            import ctypes
            now = time.monotonic()
            if now - getattr(self, "_last_cancel_at", 0.0) < self.REPEAT_CANCEL_S:
                self._cancel_provider_stream(t.ident)
                return True
            self._last_cancel_at = now
            try:
                n = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident or 0), ctypes.py_object(KeyboardInterrupt))
            except Exception:
                n = 0
            # The async exception only fires when the worker runs Python code again: abort a model
            # stream blocked in a socket read so it does (Esc / Ctrl+C take effect immediately).
            self._cancel_provider_stream(t.ident)
            return n == 1
        # Only when a turn really runs on the main thread. In the web app the main thread is the server, so a Stop
        # that lands just after a turn ended must do nothing rather than interrupt it and shut the app down.
        if not getattr(self, "_turn_on_main", False):
            return False
        import _thread
        _thread.interrupt_main()
        self._cancel_provider_stream(threading.main_thread().ident)
        return True

    def _cancel_provider_stream(self, thread_ident: int | None) -> None:
        for provider in (self.provider, getattr(self, "_side_provider", None)):
            cancel = getattr(provider, "cancel", None)
            if callable(cancel):
                try:
                    cancel(thread_ident)
                except Exception:
                    pass

    # ── session title ──────────────────────────────────────────────
    def _has_title(self) -> bool:
        if self._titled is not None:
            return self._titled
        titled = False
        try:
            from axon.session.store import iter_records
            path = getattr(self.session, "active_file", None)
            if path is not None and path.is_file():
                titled = any(r.get("type") == "session_rename" for r in iter_records(path))
        except Exception:
            titled = False
        self._titled = titled
        return titled

    def reload_title(self) -> str:
        """Read the name back from the transcript (after a resume), keeping whether Axon or the user chose it."""
        self._titled = self._title = self._title_auto = None
        return self.session_title

    def mark_titled(self, title: str | None = None) -> None:
        """The user named the session: automatic titles leave it alone from now on."""
        self._titled = True
        self._title_auto = False
        if title:
            self._title = title

    @property
    def session_title(self) -> str:
        """The session's name (/rename, -n or auto title); "" when untitled."""
        if self._title is None:
            title = ""
            auto = False
            try:
                from axon.session.store import iter_records
                path = getattr(self.session, "active_file", None)
                if path is not None and path.is_file():
                    for r in iter_records(path):
                        if r.get("type") == "session_rename" and isinstance(r.get("data"), dict):
                            title = str(r["data"].get("title") or "")
                            auto = bool(r["data"].get("auto"))
            except Exception:
                title = ""
            self._title = title
            self._title_auto = auto
            if title:
                self._titled = True
        return self._title

    def maybe_auto_title(self, prompt: str) -> str | None:
        """After the first turn, name the session from its prompt unless the user named it.
        Uses ``title_provider`` (small_model) when set, else a cheap heuristic."""
        if self.is_subagent or not getattr(self.settings, "auto_title", True) or self._has_title():
            return None
        title = ""
        if self.title_provider is not None:
            title = self._llm_title(prompt)
        title = title or heuristic_title(prompt)
        return self._apply_title(title, auto=True)

    def _apply_title(self, title: str, auto: bool = False) -> str | None:
        """Save *title* as the session's name and show it in the terminal title."""
        if not title:
            return None
        try:
            title = self.session.rename_session(title, auto=auto)
        except Exception:
            return None
        self._titled = True
        self._title = title
        self._title_auto = auto
        set_terminal_title(title)
        return title

    def _title_in_two_steps(self, prompt: str) -> None:
        """Name the session now from the first words of the prompt, then let the model write a better title in
        the background. The first title is saved at once, so a process that ends early still leaves a title."""
        first = self._apply_title(heuristic_title(prompt), auto=True)
        if first is None:
            return
        threading.Thread(target=self._upgrade_title, args=(prompt, first), daemon=True, name="axon-title").start()

    def _upgrade_title(self, prompt: str, first: str) -> None:
        better = self._llm_title(prompt)
        if better and better != first and self._title == first:      # the user has not renamed it meanwhile
            self._apply_title(better, auto=True)

    def _title_refresh_due(self) -> bool:
        """Automatic titles are rewritten on turn 3 and then every ``title_refresh_turns`` turns."""
        every = int(getattr(self.settings, "title_refresh_turns", 5) or 0)
        if every <= 0 or self.is_subagent or self.title_provider is None:
            return False
        if not getattr(self.settings, "auto_title", True) or not self.session_title or not self._title_auto:
            return False
        return self._title_turns == 3 or self._title_turns % every == 0

    def _refresh_title(self, current: str, prompts: list[str], answer: str) -> None:
        """Rewrite an automatic title so it says what the session is about now."""
        lines = [f"Current title: {current}", "Recent user messages, oldest first:"]
        lines += [f"- {' '.join(p[:TITLE_SNIPPET_CHARS].split())}" for p in prompts[-TITLE_RECENT_PROMPTS:]]
        lines.append(f"Latest answer: {' '.join(str(answer)[:TITLE_SNIPPET_CHARS].split())}")
        better = self._llm_title("\n".join(lines), instruction=TITLE_REFRESH_INSTRUCTION)
        if better and better != current and self._title == current and self._title_auto:
            self._apply_title(better, auto=True)

    def _after_turn_title(self, user_input: str, result: TurnResult) -> None:
        """Name a new session, or keep an automatic name up to date with the work."""
        if self.is_subagent or not getattr(self.settings, "auto_title", True) or user_input.lstrip().startswith("/"):
            return
        self._title_turns += 1
        if not self._has_title():
            if self.title_provider is not None:
                self._title_in_two_steps(user_input)
            else:
                self.maybe_auto_title(user_input)
        elif self._title_refresh_due():
            prompts = suggest.earlier_user_messages(self.conversation.messages) + [user_input]
            threading.Thread(target=self._refresh_title, args=(self.session_title, prompts, result.final_text),
                             daemon=True, name="axon-title").start()

    def _current_title_provider(self, model: str, cached: Any) -> Any:
        """The cached title provider was built at startup; rebuild it so it follows a provider switch."""
        try:
            return make_provider(model, self.settings, role="title")
        except Exception:
            return cached

    def _llm_title(self, prompt: str, instruction: str = "") -> str:
        prov = self.title_provider
        model = self.settings.model
        if type(prov).__module__.startswith("axon.providers"):
            prov = self._current_title_provider(model, prov)
        system = [{"type": "text", "text": instruction or "Write a 3-6 word title for a coding session that starts with the user "
                                           "message below. Reply with the title only, no quotes or punctuation."}]
        try:
            with _provider_lock(prov):
                for _ in prov.stream(model=model, system=system, messages=[{"role": "user", "content": prompt[:2000]}],
                                     tools=[], max_tokens=48, effort="reflex", thinking=False):
                    pass
                turn = prov.finalize()
            if getattr(turn, "usage", None) is not None:
                cost = _as_float(record_usage(self.ledger, model, turn.usage, source="title"))
                self._log_usage(model, turn.usage, cost, "title")
            text = " ".join(str(turn.text or "").split()).strip("\"'. ")
            return text[:60]
        except Exception:
            return ""

    # ── next-prompt suggestion ─────────────────────────────────────
    @property
    def next_suggestion(self) -> str:
        """The guessed next prompt ("" when there is none)."""
        return self.suggestions.text

    def _maybe_suggest(self, user_input: str, result: TurnResult) -> None:
        """After a normal final answer, guess the user's next message in a background thread."""
        box = self.suggestions
        if self.is_subagent or not box.live or not getattr(self.settings, "prompt_suggestions", True):
            return
        if result.stop_reason != "end_turn" or not str(result.final_text or "").strip():
            return
        if user_input.lstrip().startswith("/"):
            return
        earlier = suggest.earlier_user_messages(self.conversation.messages)
        ticket = box.start()
        threading.Thread(target=self._suggestion_worker, args=(ticket, user_input, result.final_text, earlier),
                         daemon=True, name="axon-suggest").start()

    def _suggestion_worker(self, ticket: int, user_input: str, answer: str, earlier: list[str]) -> None:
        text = ""
        try:
            if self.suggestions.is_current(ticket):
                text = self._llm_suggestion(user_input, answer, earlier)
        except Exception:
            text = ""
        self.suggestions.finish(ticket, text)

    def _suggestion_provider(self, model: str) -> Any:
        """The small model's provider, rebuilt each time so it follows a provider switch."""
        cached = self.suggest_provider
        if cached is not None and not type(cached).__module__.startswith("axon.providers"):
            return cached
        try:
            return make_provider(model, self.settings, role="suggest")
        except Exception:
            return cached

    def _llm_suggestion(self, user_input: str, answer: str, earlier: list[str]) -> str:
        model = self.settings.small_model or self.settings.model
        prov = self._suggestion_provider(model)
        if prov is None:
            return ""
        system, messages = suggest.build_request(user_input, answer, earlier)
        with _provider_lock(prov):
            for _ in prov.stream(model=model, system=system, messages=messages, tools=[],
                                 max_tokens=suggest.MAX_TOKENS, effort="reflex", thinking=False):
                pass
            turn = prov.finalize()
        if getattr(turn, "usage", None) is not None:
            cost = _as_float(record_usage(self.ledger, model, turn.usage, source="suggest"))
            self._log_usage(model, turn.usage, cost, "suggest")
        return suggest.clean_reply(str(turn.text or ""), self._command_names())

    def _command_names(self) -> set[str]:
        try:
            from axon.commands.registry import list_command_names
            return {name.lstrip("/").lower() for name, _ in list_command_names(agent=self)}
        except Exception:
            return set()

    def get_combined_cost(self) -> Decimal:
        """Combined session cost (main agent plus all subagents)."""
        total = self.ledger.total()
        try:
            if hasattr(self.session, "load_ledger"):
                parent_id = self.session.active_session_id.rsplit("_sub_", 1)[0]
                s_ledger = self.session.load_ledger(parent_id, self.settings.model, include_subagents=True)
                if s_ledger and s_ledger.total() > Decimal("0"):
                    return max(total, s_ledger.total())
        except Exception:
            pass
        return total

    def get_combined_tokens(self) -> int:
        total = self.ledger.total_tokens()
        try:
            if hasattr(self.session, "load_ledger"):
                parent_id = self.session.active_session_id.rsplit("_sub_", 1)[0]
                s_ledger = self.session.load_ledger(parent_id, self.settings.model, include_subagents=True)
                if s_ledger:
                    return max(total, s_ledger.total_tokens())
        except Exception:
            pass
        return total

    # ── prompt assembly ─────────────────────────────────────────────
    def system_blocks(self) -> list[dict[str, Any]]:
        if self._git_snapshot is None:
            from axon.agent.prompt import git_snapshot
            self._git_snapshot = git_snapshot(self.settings.workspace)
        skills = list(self.skills.skills.values()) if self.skills is not None else []
        blocks = build_system(
            self.settings,
            self.registry,
            skills,
            git_status=self._git_snapshot,
            session_context=self._session_context,
            memory_text=self.memory_text(),
            repo_map=self.repo_map_text(),
        )
        if self._post_compact_context:
            blocks.append({"type": "text", "text": self._post_compact_context})
        goal_txt = self._goal_section()
        if goal_txt:
            blocks.append({"type": "text", "text": goal_txt})
        if self.extra_system:
            blocks.append({"type": "text", "text": f"## Your role ({self.agent_name})\n\n{self.extra_system}"})
        return blocks

    @property
    def active_goal(self) -> str:
        g = getattr(self, "goal", None) or getattr(self.settings, "goal", None)
        return str(g).strip() if g else ""

    def _goal_section(self) -> str:
        if self.is_subagent or not self.active_goal:
            return ""
        try:
            from axon.commands.prefs_cmds import goal_prompt_section
            return str(goal_prompt_section(self) or "")
        except (ImportError, AttributeError):
            return f"# Session goal\nThe user set this goal for the session: {self.active_goal}"

    def repo_map_text(self) -> str:
        """Repo map block, computed once per session (and after compaction) to keep the prompt cache warm."""
        if self._repo_map is None:
            if self.is_subagent or not getattr(self.settings, "repo_map", False):
                self._repo_map = ""
            else:
                from axon.agent.prompt import repo_map_block
                self._repo_map = repo_map_block(self.settings)
        return self._repo_map

    @property
    def memory(self) -> Any:
        """The MemoryLoader (startup memory is read once and held until /clear or compaction)."""
        if self._memory is None:
            from axon.agent.memory import MemoryLoader
            loader = MemoryLoader(
                self.settings.workspace,
                instruction_files=str(getattr(self.settings, "instruction_files", "axon-md-or-agents-md")),
                auto_memory=bool(getattr(self.settings, "auto_memory", True)),
                excludes=list(getattr(self.settings, "memory_excludes", []) or []),
            )
            try:
                loader.load_startup()
            except Exception:
                pass
            self._memory = loader
        return self._memory

    def memory_text(self) -> str:
        if self._memory_text is None:
            try:
                from axon.agent.memory import render_memory_context
                self._memory_text = render_memory_context(self.settings.workspace, loader=self.memory)
            except Exception:
                self._memory_text = ""
        return self._memory_text

    def reload_memory(self) -> None:
        self._memory = None
        self._memory_text = None

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self.registry.schemas(provider_style="anthropic" if self.provider.name == "anthropic" else "openai")

    def _max_output_tokens(self) -> int:
        try:
            from axon.providers.registry import get_max_output
            cap = get_max_output(self.settings.model, default=min(int(self.settings.max_tokens), 32_000))
        except Exception:
            cap = int(self.settings.max_tokens)
        return max(256, min(int(self.settings.max_tokens), int(cap)))

    def _encoder(self) -> Any:
        return self.provider.encode_tool_results if self.provider.name != "anthropic" else None

    # ── compaction ──────────────────────────────────────────────────
    def _compaction_provider(self) -> tuple[Any, str]:
        """The summariser runs on settings.small_model when set (fresh provider), else the main model."""
        model = self.settings.small_model or self.settings.model
        provider = self.provider
        if model != self.settings.model and type(provider).__module__.startswith("axon.providers"):
            try:
                provider = make_provider(model, self.settings, role="compact")
            except Exception:
                provider, model = self.provider, self.settings.model
        return provider, model

    def _compact_instructions(self, instructions: str) -> str:
        if instructions:
            return instructions
        try:
            from axon.agent.memory import compact_instructions
            return compact_instructions(self.memory_text())
        except Exception:
            return ""

    def _generate_summary(self, instructions: str) -> tuple[str | None, Any, str]:
        """(summary, usage, model). When the small model fails (e.g. it belongs to the provider the session
        just switched away from), the main model writes the summary instead of a lossy heuristic one."""
        provider, model = self._compaction_provider()
        text = self._compact_instructions(instructions)
        self._side_provider = provider      # so Esc aborts this stream too (it may not be self.provider)
        try:
            with _provider_lock(provider):
                summary, usage = self.context.generate_summary(self.conversation, provider, model, instructions=text)
        finally:
            self._side_provider = None
        if summary is None and provider is not self.provider:
            provider, model = self.provider, self.settings.model
            with _provider_lock(provider):
                summary, usage = self.context.generate_summary(self.conversation, provider, model, instructions=text)
        return summary, usage, model

    def compact_preview(self, instructions: str = "") -> str | None:
        """Generate the summary that compaction would use, without changing the conversation."""
        summary, usage, model = self._generate_summary(instructions)
        cost = _as_float(record_usage(self.ledger, model, usage, source="compact")) if usage is not None else 0.0
        if usage is not None:
            self._log_usage(model, usage, cost, "compact")
        n = len(self.conversation.messages)
        t = self.conversation.token_estimate()
        self._emit(CompactionEvent(trigger="preview", messages_before=n, messages_after=n, tokens_before=t, tokens_after=t,
                                   usage=usage, cost=cost, model=model, summary=(summary or "")[:4000], preview=True))
        self._pending_summary = summary
        return summary

    def compact(self, trigger: str = "manual", instructions: str = "", summary: str | None = None) -> bool:
        """Summarize older turns (PreCompact hook first) on the cheap model. Pass ``summary`` (e.g. from
        :meth:`compact_preview`) to apply an already generated summary. Returns True if compacted."""
        res = self._hook("PreCompact", {"trigger": trigger, "custom_instructions": instructions or None})
        if res is not None and getattr(res, "blocked", False):
            n = len(self.conversation.messages)
            t = self.conversation.token_estimate()
            self._emit(CompactionEvent(trigger=trigger, messages_before=n, messages_after=n, tokens_before=t, tokens_after=t, blocked_by_hook=True))
            return False
        before = len(self.conversation.messages)
        tokens_before = self.conversation.token_estimate()
        usage, cost, model = None, 0.0, ""
        if summary is None and trigger != "manual" and self.context.split_point(self.conversation) <= 0:
            return False    # no older turns to summarize: a paid summary call would change nothing
        if summary is None:
            summary, usage, model = self._generate_summary(instructions)
            if usage is not None:
                cost = _as_float(record_usage(self.ledger, model, usage, source="compact"))
                self._log_usage(model, usage, cost, "compact")
        self.context._summarize_older_turns(self.conversation, summary_text=summary)
        changed = len(self.conversation.messages) != before
        if changed and isinstance(getattr(self.context, "last_receipt", None), dict):
            self.context.last_receipt["trigger"] = trigger
        self._emit(CompactionEvent(
            trigger=trigger,
            messages_before=before,
            messages_after=len(self.conversation.messages),
            tokens_before=tokens_before,
            tokens_after=self.conversation.token_estimate(),
            usage=usage,
            cost=cost,
            model=model,
            summary=(summary or "")[:4000],
        ))
        self.journal("compact", {"trigger": trigger, "messages_before": before, "messages_after": len(self.conversation.messages),
                                 "tokens_before": tokens_before, "changed": changed, "cost_usd": round(float(cost or 0), 6)})
        if changed:
            self.last_compaction = trigger
            # A typed record, so a resumed session replays the compacted history (not everything before it)
            # and its ledger still counts the summary call.
            record: dict[str, Any] = {"messages": self.conversation.messages, "trigger": trigger}
            if usage is not None and hasattr(usage, "input"):
                record["model"] = model
                record["usage"] = {k: int(getattr(usage, k, 0) or 0) for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
            try:
                self.session.append("compaction", record)
            except Exception:
                pass
            # Re-read instructions and git state from disk, and let SessionStart hooks re-inject context.
            self._git_snapshot = None
            self._repo_map = None
            self.reload_memory()
            self._post_compact_context = self._reread_recent_files()
            if self._session_started and not self.is_subagent:
                self.start_session("compact")
        self._hook("PostCompact", {"trigger": trigger, "compacted": changed,
                                   "compact_summary": (summary or "")[:20_000], "summary": (summary or "")[:4000]})
        return changed

    def _recent_edited_files(self, limit: int = POST_COMPACT_FILES) -> list[str]:
        """Most recently written/edited file paths, newest first, from the turn traces."""
        seen: list[str] = []
        for tr in reversed(self.traces):
            for st in reversed(list(tr.steps)):
                if st.kind != "tool" or st.data.get("name") not in WRITE_TOOLS or st.data.get("is_error"):
                    continue
                inp = st.data.get("input") or {}
                path = next((str(inp.get(k)) for k in FILE_PATH_KEYS if isinstance(inp, dict) and inp.get(k)), "")
                if path and path not in seen:
                    seen.append(path)
                if len(seen) >= limit:
                    return seen
        return seen

    def _reread_recent_files(self) -> str:
        """After compaction the summary loses exact file contents: re-read the files edited most
        recently (capped) so the model keeps working on their current state."""
        from pathlib import Path as _P
        parts: list[str] = []
        for raw in self._recent_edited_files():
            p = _P(raw).expanduser()
            if not p.is_absolute():
                p = self.settings.workspace / p
            try:
                if not p.is_file() or p.stat().st_size > 2_000_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            note = ""
            if len(text) > POST_COMPACT_FILE_CHARS:
                text, note = text[:POST_COMPACT_FILE_CHARS], "\n[... truncated; Read the file for the rest ...]"
            try:
                self.file_state.record_read(p)
            except Exception:
                pass
            parts.append(f"### {raw}\n```\n{text}{note}\n```")
        if not parts:
            return ""
        return ("## Files you edited recently (re-read from disk after context compaction)\n\n"
                "These are their current contents; prefer them over anything in the summary.\n\n" + "\n\n".join(parts))

    # ── main loop ───────────────────────────────────────────────────
    def run_turn(self, user_input: str) -> TurnResult:
        """Run the ReAct loop until the model stops, a limit is hit, or the user interrupts."""
        on_main = threading.current_thread() is threading.main_thread()
        if on_main:
            self._turn_on_main = True           # cancel_current_turn may interrupt the main thread only now
        try:
            return self._traced_turn(user_input)
        finally:
            if on_main:
                self._turn_on_main = False

    def _traced_turn(self, user_input: str) -> TurnResult:
        self._turn_counter += 1
        if not self.is_subagent:
            self.suggestions.clear()               # last turn's guess is stale now
        if not self.is_subagent:
            try:
                from axon.features.insights import sync_pins
                sync_pins(self)                    # pins of a resumed session, before anything is trimmed
            except Exception:
                pass
        trace = TurnTrace(
            turn=len(self.traces) + 1,
            prompt=user_input,
            model=self.settings.model,
            mode=self.settings.mode,
            agent_name=self.agent_name,
        )
        self.traces.append(trace)
        self._trace = trace
        result: TurnResult | None = None
        self._full_outputs: dict[str, str] = {}
        self._call_meta: dict[str, Any] = {}
        self._think_blocks: list[float] = []
        self.journal("turn_start", {"turn": trace.turn, "prompt": user_input, "model": self.settings.model,
                                    "mode": self.settings.mode})
        boost = self._apply_thinking_boost(user_input)
        turn_error: BaseException | None = None
        try:
            result = self._run_turn(user_input)
            return result
        except BaseException as e:
            turn_error = e
            if isinstance(e, KeyboardInterrupt):
                # An interrupt between recording tool calls and running them leaves them unanswered.
                try:
                    self.conversation.repair()
                except Exception:
                    pass
            raise
        finally:
            try:
                u = result.usage if result is not None else None
                self.journal("turn_end", {
                    "turn": trace.turn,
                    "stop_reason": result.stop_reason if result is not None else (
                        "interrupted" if isinstance(turn_error, KeyboardInterrupt) or turn_error is None else "exception"),
                    "error": (str(result.error or "")[:2000] if result is not None else
                              (f"{type(turn_error).__name__}: {turn_error}"[:2000] if turn_error is not None
                               and not isinstance(turn_error, KeyboardInterrupt) else "")),
                    "iterations": result.iterations if result is not None else None,
                    "tool_calls": result.tool_calls_count if result is not None else None,
                    "input_tokens": int(getattr(u, "input", 0) or 0) if u is not None else int(trace.input_tokens or 0),
                    "output_tokens": int(getattr(u, "output", 0) or 0) if u is not None else int(trace.output_tokens or 0),
                    "cost_usd": round(float(trace.cost or 0.0), 6),
                    "duration_s": round(time.time() - float(trace.started_at or time.time()), 3),
                    "answer": (result.final_text or "")[:500] if result is not None else "",
                }, sync=True)
            except Exception:
                pass
            if boost is not None:
                try:
                    self.settings = self.settings.model_copy(update={"effort": boost[0], "thinking": boost[1]})
                except Exception:
                    pass
            try:
                self.checkpoints.commit_turn_snapshot(self._turn_counter)
            except Exception:
                pass
            trace.finish(result.final_text if result else INTERRUPTED_NOTE, result.stop_reason if result else "interrupted")
            self._trace = None
            if not self.is_subagent:
                save_trace(self.settings.workspace, getattr(self.session, "active_session_id", ""), trace)
                if result is not None:
                    self._after_turn(user_input, result)

    def _apply_thinking_boost(self, user_input: str) -> tuple[Any, bool] | None:
        """``ultrathink`` / ``think hard`` in the prompt raise effort and force thinking for this turn.
        Returns the previous (effort, thinking) to restore, or None when nothing changed."""
        hit = detect_thinking_keyword(user_input)
        if hit is None:
            return None
        kw, effort = hit
        prev_effort, prev_thinking = self.settings.effort, bool(self.settings.thinking)
        new_effort = effort if _EFFORT_RANK.get(effort, 0) > _EFFORT_RANK.get(str(prev_effort), 0) else prev_effort
        if new_effort == prev_effort and prev_thinking:
            self._emit(ThinkingBoost(keyword=kw, effort=str(prev_effort), previous_effort=str(prev_effort)))
            return None
        try:
            self.settings = self.settings.model_copy(update={"effort": new_effort, "thinking": True})
        except Exception:
            return None
        self._emit(ThinkingBoost(keyword=kw, effort=str(new_effort), previous_effort=str(prev_effort)))
        return prev_effort, prev_thinking

    def _after_turn(self, user_input: str, result: TurnResult) -> None:
        if result.stop_reason == "error":
            detail = str(result.error or result.final_text)[:2000]
            self._hook("StopFailure", {"error": classify_stop_failure(detail), "error_details": detail,
                                       "last_assistant_message": result.final_text[:4000]})
            return
        if result.stop_reason in ("blocked", "budget_exceeded"):
            return
        if str(result.final_text or "").strip():
            self._hook("MessageDisplay", {"message": str(result.final_text)[:4000]})
        self._maybe_suggest(user_input, result)
        self._after_turn_title(user_input, result)

    def drain_steer_messages(self) -> list[str]:
        """Messages typed mid-turn (queue items flagged steer=True), removed from the queue."""
        q = getattr(self, "message_queue", None)
        drain = getattr(q, "drain_steer", None)
        if not callable(drain):
            return []
        try:
            return [it.text for it in drain() if str(getattr(it, "text", "")).strip()]
        except Exception:
            return []

    def _deliver_steer(self, results: list[ToolResultBlock], iteration: int) -> list[ToolResultBlock]:
        """Append mid-turn user messages to the last tool result of this round (same user message,
        so tool_use/tool_result pairing and role alternation stay valid for every provider)."""
        texts = self.drain_steer_messages()
        if not texts or not results:
            if texts:  # nothing to attach to: put them back for the next turn
                for t in texts:
                    self.message_queue.push(t)
            return results
        results = self._with_note(results, "\n".join(f"{STEER_PREFIX} {t}" for t in texts))
        self._emit(SteerEvent(count=len(texts), texts=tuple(t[:500] for t in texts), iteration=iteration))
        self.journal("steer", {"action": "delivered", "texts": texts, "iteration": iteration})
        try:
            for t in texts:
                self.session.append("steer_message", {"text": t, "iteration": iteration})
        except Exception:
            pass
        return results

    @staticmethod
    def _with_note(results: list[ToolResultBlock], note: str) -> list[ToolResultBlock]:
        """*note* appended to the last tool result (same user message: pairing and alternation stay valid)."""
        if not results:
            return results
        last = results[-1]
        return results[:-1] + [ToolResultBlock(tool_use_id=last.tool_use_id, content=f"{last.content}\n\n{note}",
                                               is_error=last.is_error, images=last.images)]

    def _merge_user_note(self, note: str) -> Callable[[], None]:
        """Add *note* as user text after the last message without breaking provider rules: merged into a trailing
        user message (Anthropic tool_result blocks come first, then the text), else a new user message (after
        OpenAI role=tool results). Returns a function that takes the note out again."""
        msgs = self.conversation.messages
        last = msgs[-1] if msgs else None
        merged = last is not None and last.get("role") == "user" and isinstance(last.get("content"), (str, list))
        if merged:
            c = last["content"]
            new = {**last, "content": f"{c}\n\n{note}" if isinstance(c, str) else list(c) + [{"type": "text", "text": note}]}
            msgs[-1] = new
        else:
            new = {"role": "user", "content": note}
            msgs.append(new)

        def restore() -> None:
            ms = self.conversation.messages
            idx = next((i for i, m in enumerate(ms) if m is new), None)
            if idx is not None and merged:
                ms[idx] = last
            elif idx is not None:
                del ms[idx]
        return restore

    def _record_call(self, turn: AssistantTurn | None, outcome: str, err: BaseException | None, iteration: int,
                     fingerprint: dict[str, Any] | None, context_tokens: int) -> tuple[float, str]:
        """Ledger, usage log, LLMCallEnd and cache status for one finished model call. Returns (cost, model)."""
        cost = 0.0
        call_model = (getattr(turn, "model", None) if turn is not None else None) or self.settings.model
        cut = Usage()
        if turn is not None:
            cost = _as_float(record_usage(self.ledger, call_model, turn.usage, source="main" if not self.is_subagent else self.agent_name))
        elif outcome == "error":
            cut = getattr(self, "_cut_usage", None) or Usage()
            self._cut_usage = Usage()
            if cut.input or cut.output or cut.cache_read or cut.cache_write:
                cost = _as_float(record_usage(self.ledger, call_model, cut, source="main" if not self.is_subagent else self.agent_name))
            else:
                cut = Usage()
        self._last_cut_usage = cut
        t0, t1 = self._last_call_window
        cache_status = None
        if turn is not None and fingerprint is not None:
            cache_status = self._cache_status(fingerprint, turn.usage, iteration, context_tokens,
                                              getattr(turn, "diagnostics", None))
        self._annotate_manifest(cost, cache_status, call_model, turn)
        if turn is not None:
            self._log_usage(call_model, turn.usage, cost, self.agent_name if self.is_subagent else "main", t1 - t0)
        elif cut != Usage():
            self._log_usage(call_model, cut, cost, self.agent_name if self.is_subagent else "main", t1 - t0)
        self._emit(LLMCallEnd(
            iteration=iteration,
            model=call_model,
            usage=turn.usage if turn is not None else cut,
            cost=cost,
            duration_s=max(0.0, t1 - t0),
            stop_reason=turn.stop_reason if turn is not None else "error",
            started_at=t0,
            ended_at=t1,
            tool_use_count=len(turn.tool_uses) if turn is not None else 0,
            text_chars=len(turn.text) if turn is not None else 0,
            outcome=outcome,
            error=str(err) if err else "",
            cost_breakdown=cost_breakdown(call_model, turn.usage if turn is not None else cut) if (turn is not None or cut != Usage()) else {},
            cache_miss_cause=cache_status.cause if cache_status is not None else "",
            cache_miss_detail=self._cache_detail(call_model, turn, cache_status),
            context_used=context_tokens,
            context_window=self.context.context_window(self.settings.model),
        ))
        try:   # the session record keeps the context size too, so a restored turn shows the same gauge
            if isinstance(self._call_meta, dict):
                self._call_meta["context_used"] = int(context_tokens or 0)
                self._call_meta["context_window"] = int(self.context.context_window(self.settings.model) or 0)
        except Exception:
            pass
        if cache_status is not None:
            self._emit(cache_status)
        if not self.is_subagent:
            self._budget_notices()
        return cost, call_model

    def _budget_notices(self) -> None:
        """A quiet notice, once each, when the session, daily or monthly budget reaches 50, 80 and 100 percent."""
        try:
            from axon.features import cost_forecast as cf
            for n in cf.due_notices(self.settings, self.settings.workspace, float(self.ledger.total()), self._notice_marks):
                self._emit(BudgetEvent(scope=n["scope"], action="notice", spent_usd=n["spent"], projected_usd=0.0,
                                       limit_usd=n["limit"], model=self.settings.model, message=n["message"], percent=n["step"]))
        except Exception:
            pass

    def _step_limit_wrap_up(self, system_blocks: list[dict[str, Any]], tool_schemas: list[dict[str, Any]],
                            iterations: int, last_round: list[dict[str, Any]], user_input: str) -> tuple[str | None, Usage]:
        """At the step limit, one last call that asks for the final report (found / did / left undone) instead of
        losing the work. The tool definitions are still sent (Anthropic rejects tool_use history without them, and
        the prompt cache stays warm) but the note forbids calls and any that come back are dropped unexecuted.
        Returns (report or None when the call failed or wrote nothing, usage of the call)."""
        limit = max(1, int(self.settings.max_iterations))
        note = WRAP_UP_NOTE.format(limit=limit)
        restore = self._merge_user_note(note)
        iteration = iterations + 1
        try:
            self.context.prepare(self.conversation, system_blocks, tool_schemas, model=self.settings.model)
            tokens_in = self.context.projected_tokens(self.conversation, system_blocks, tool_schemas)
            self._emit(LLMCallStart(iteration=iteration, max_iterations=self.settings.max_iterations,
                                    model=self.display_model(), message_count=len(self.conversation.messages),
                                    tokens_in=tokens_in, tools_ingested=list(last_round), user_prompt=user_input,
                                    mode=self.settings.mode))
            self._think_blocks = []
            self._call_meta = {"message_count": len(self.conversation.messages), "tokens_in": tokens_in,
                               "provider": self._provider_name(self.display_model()),
                               "fed_back": [_fed_back_fact(t) for t in last_round], "wrap_up": True,
                               "tool_count": len(tool_schemas), "prompt_tools": self._prompt_tools_active()}
            try:
                fingerprint = self._cache.fingerprint(self.settings.model, str(self.settings.effort), system_blocks, tool_schemas, self.conversation.messages)
            except Exception:
                fingerprint = None
            turn, outcome, err = self._call_model(system_blocks, tool_schemas, iteration)
            cost, call_model = self._record_call(turn, outcome, err, iteration, fingerprint, tokens_in)
        except Exception:
            restore()
            return None, Usage()
        usage = turn.usage if turn is not None else Usage()
        text = (turn.text or "").strip() if turn is not None and outcome == "ok" else ""
        if not text:
            restore()
            return None, usage
        clean = turn if not turn.tool_uses else AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn",
                                                              usage=turn.usage, model=turn.model)
        self.conversation.append_assistant(clean)
        t0, t1 = self._last_call_window
        try:
            # A typed record, not a user message: a resumed session replays results -> report, which stays valid.
            self.session.append("step_limit", {"limit": limit, "note": note})
        except Exception:
            pass
        try:
            self.session.append_turn(clean, model=call_model, cost=cost, duration_s=max(0.0, t1 - t0),
                                     call=self._call_meta, thinking_s=list(self._think_blocks))
        except TypeError:
            self.session.append_turn(clean)
        self.journal("step_limit", {"limit": limit, "report_chars": len(text), "dropped_tool_calls": len(turn.tool_uses)})
        return text, usage

    def _fit_large_input(self, text: str) -> str:
        """Shrink an input too large to send whole, and any oversized message left from a resumed session.
        Tells the user what happened and what it saves, before the request goes out."""
        window = self.context.context_window(self.settings.model)
        limit = large_input.inline_limit(window, int(getattr(self.settings, "large_input_tokens", 0) or 0))
        if limit is None:
            return text
        large_input.shrink_messages(self.conversation.messages, limit)
        fitted, path = large_input.fit(text, limit)
        full = large_input.estimate_tokens(text)
        if path is None and full < large_input.COST_NOTICE_TOKENS:
            return text
        sent = large_input.estimate_tokens(fitted)
        self._emit(LargeInputEvent(lines=text.count("\n") + 1, tokens_full=full, tokens_sent=sent, limit=limit,
                                   path=str(path or ""), spilled=path is not None, model=self.settings.model,
                                   cost_full=self._input_cost(full), cost_sent=self._input_cost(sent)))
        return fitted

    def _input_cost(self, tokens: int) -> float:
        try:
            from axon.providers.pricing import cost_for
            return float(cost_for(self.settings.model, Usage(input=tokens)))
        except Exception:
            return 0.0

    def _recover_from_overflow(self, attempt: int) -> bool:
        """The provider refused the request as too large, or the context filled mid-reply. Shrink oversized
        messages and tool results, and summarize older turns, each attempt harder. True if anything shrank."""
        before = self.conversation.token_estimate()
        # Sized from the request that failed, not the window on record: a provider's real limit can be lower.
        limit = max(large_input.MIN_RECOVERY_LIMIT, before // (4 if attempt == 1 else 16))
        large_input.shrink_messages(self.conversation.messages, limit)
        self.context._trim_large_results(self.conversation)
        self.context._evict_stale_results(self.conversation)
        try:
            self.compact(trigger="overflow")
        except Exception as e:
            self.journal("error", {"error": f"compaction after overflow failed: {type(e).__name__}: {e}"[:2000]})
        after = self.conversation.token_estimate()
        self.journal("context_overflow", {"attempt": attempt, "tokens_before": before, "tokens_after": after})
        return after < before

    def _drop_from(self, message: dict[str, Any]) -> None:
        """Remove *message* and everything after it. Found by identity: compaction or the history window
        may have shortened the list since the message was added."""
        messages = self.conversation.messages
        index = next((i for i, m in enumerate(messages) if m is message), None)
        if index is not None:
            del messages[index:]

    def _stop_result(self, text: str, reason: str, iterations: int, tool_calls: int, usage: Usage, error: str | None = None) -> TurnResult:
        return TurnResult(final_text=text, stop_reason=reason, iterations=iterations, tool_calls_count=tool_calls, usage=usage, error=error)

    def _run_turn(self, user_input: str) -> TurnResult:
        total_usage = Usage()
        raw_prompt = user_input
        self.last_failed_turn = None
        ceiling = Decimal(str(self.settings.session_cost_ceiling or 0))
        if ceiling > 0 and self.ledger.total() >= ceiling:
            return self._stop_result(
                f"Session cost ceiling reached (${self.ledger.total():.4f} >= ${ceiling}). "
                "Raise session_cost_ceiling in ~/.axon/config.toml or start a new session.",
                "budget_exceeded", 0, 0, total_usage,
            )

        if not self.is_subagent:           # the project's daily and monthly budgets stop new turns like the session ceiling does
            try:
                from axon.features import cost_forecast as cf
                hit = cf.period_limit_hit(self.settings.workspace)
            except Exception:
                hit = None
            if hit:
                return self._stop_result(cf.limit_message(hit), "budget_exceeded", 0, 0, total_usage)

        self._hook_stop = ""
        self._declined = False
        if not self.is_subagent:
            import uuid
            self._prompt_id = str(uuid.uuid4())
            res = self._hook("UserPromptSubmit", {"prompt": user_input})
            if res is not None:
                title = str(getattr(res, "session_title", "") or "")
                if title:
                    try:
                        self.session.rename_session(title)
                    except Exception:
                        pass
                if res.blocked or getattr(res, "stop", False):
                    msg = res.message or getattr(res, "stop_reason", "") or "blocked"
                    tail = "" if getattr(res, "suppress_original_prompt", False) else f"\nOriginal prompt: {raw_prompt}"
                    return self._stop_result(f"Prompt blocked by UserPromptSubmit hook: {msg}{tail}", "blocked", 0, 0, total_usage)
                if res.additional_context:
                    user_input = f"{user_input}\n\n<user-prompt-submit-hook>\n{res.additional_context}\n</user-prompt-submit-hook>"

        pending = self.take_pending_context()
        late = self._absorb_async_hooks()
        if late:
            pending = f"{pending}\n\n{late}" if pending else late
        if pending:
            user_input = f"{pending}\n\n{user_input}" if user_input.strip() else pending
        self.conversation.repair()
        user_input = self._fit_large_input(user_input)
        # A subagent's prompt is written by the model: paths in it are files to read, not images to attach.
        # The same goes for a scheduled prompt.
        model_written = self.is_subagent or self.__dict__.pop("_model_written_prompt", False)
        self.conversation.append_user(user_input, attach_images=not model_written)
        prompt_message = self.conversation.messages[-1]
        self.session.append_user(user_input)
        self._current_turn_prompt = user_input

        total_tool_calls = 0
        final_text = ""
        iterations = 0
        stop_hook_forced = False
        stop_continuations = 0
        goal_checked = False
        last_round: list[dict[str, Any]] = []
        system_blocks = self.system_blocks()
        tool_schemas = self.tool_schemas()
        limit = max(1, int(self.settings.max_iterations))
        step_warned = False
        overflow_retries = 0

        while iterations < limit:
            if iterations > 0:
                if ceiling > 0 and self.ledger.total() >= ceiling:
                    return self._stop_result(
                        (final_text + "\n\n" if final_text else "") + f"[Stopped: session cost ceiling ${ceiling} reached.]",
                        "budget_exceeded", iterations, total_tool_calls, total_usage,
                    )
                budget = int(self.settings.turn_token_budget or 0)
                if budget > 0 and (total_usage.input + total_usage.output) >= budget:
                    return self._stop_result(
                        (final_text + "\n\n" if final_text else "") + f"[Stopped: turn token budget of {budget:,} tokens reached.]",
                        "token_budget", iterations, total_tool_calls, total_usage,
                    )
            iterations += 1

            if len(self.conversation.messages) > 4 and self.context.needs_compaction(
                self.conversation, system_blocks, tool_schemas, model=self.settings.model
            ):
                self._consecutive_compactions += 1
                if self._consecutive_compactions > MAX_THRASH_COMPACTIONS:
                    self._consecutive_compactions = 0
                    return self._stop_result(
                        "Context is still over the compaction threshold after repeated compactions. "
                        "Use /clear, or shrink large tool outputs or instruction files.",
                        "error", iterations, total_tool_calls, total_usage, error="compaction thrash",
                    )
                try:
                    if self.compact(trigger="auto"):
                        system_blocks = self.system_blocks()
                    else:
                        self._consecutive_compactions = 0      # nothing older to compact, so this is not thrashing
                except Exception as e:
                    self.journal("error", {"error": f"auto-compaction failed: {type(e).__name__}: {e}"[:2000], "iteration": iterations})
            else:
                self._consecutive_compactions = 0
            stop = self._budget_guard(system_blocks, tool_schemas, iterations, total_tool_calls, total_usage, final_text)
            if stop is not None:
                return stop
            self.context.prepare(self.conversation, system_blocks, tool_schemas, model=self.settings.model)

            tokens_in = self.context.projected_tokens(self.conversation, system_blocks, tool_schemas)
            self._emit(
                    LLMCallStart(
                        iteration=iterations,
                        max_iterations=self.settings.max_iterations,
                        model=self.display_model(),
                        message_count=len(self.conversation.messages),
                        tokens_in=tokens_in,
                        tools_ingested=list(last_round),
                        user_prompt=user_input,
                        mode=self.settings.mode,
                    )
                )
            # The same context facts the live timeline shows, saved with this call's turn so a restored
            # session draws the step exactly as it was drawn live.
            self._think_blocks = []
            self._call_meta = {
                "message_count": len(self.conversation.messages),
                "tokens_in": tokens_in,
                "provider": self._provider_name(self.display_model()),
                "fed_back": [_fed_back_fact(t) for t in last_round],
                # what the model chose from: the tool definitions sent with this call
                "tool_count": len(tool_schemas),
                "prompt_tools": self._prompt_tools_active(),
            }

            try:
                fingerprint = self._cache.fingerprint(self.settings.model, str(self.settings.effort), system_blocks, tool_schemas, self.conversation.messages)
            except Exception:
                fingerprint = None
            context_tokens = self.context.projected_tokens(self.conversation, system_blocks, tool_schemas)
            turn, outcome, err = self._call_model(system_blocks, tool_schemas, iterations)
            if turn is not None:
                total_usage = total_usage + turn.usage
            cost, call_model = self._record_call(turn, outcome, err, iterations, fingerprint, context_tokens)
            total_usage = total_usage + self._last_cut_usage
            t0, t1 = self._last_call_window

            if outcome == "error" and overflow_retries < MAX_OVERFLOW_RETRIES and large_input.is_context_overflow(err):
                overflow_retries += 1
                if self._recover_from_overflow(overflow_retries):
                    system_blocks = self.system_blocks()
                    continue

            if outcome == "error":
                msg = f"Provider error: {err}"
                if iterations - overflow_retries <= 1:
                    # Nothing happened this turn: roll the prompt back so the next request stays valid.
                    self._drop_from(prompt_message)
                    hint = "Your message was not answered and is not in the history; /retry sends it again."
                else:
                    # Tool calls and results of this turn stay in the history, so the model can pick up
                    # exactly where the failed request left off.
                    hint = (f"Progress from this turn is kept ({total_tool_calls} tool call(s) already ran); "
                            "type 'continue' or /retry to resume.")
                self.last_failed_turn = {"prompt": raw_prompt, "resume": iterations > 1}
                self.journal("error", {"error": f"{type(err).__name__}: {err}"[:2000], "iteration": iterations,
                                       "model": call_model, "tool_calls_so_far": total_tool_calls})
                try:
                    # A typed record (not a fake assistant message): resume replays the history without the
                    # unanswered prompt, and /retry in a resumed session can resend it.
                    self.session.append("turn_failed", {"prompt": raw_prompt, "resume": iterations > 1,
                                                        "rolled_back": iterations == 1, "error": msg[:4000],
                                                        "tool_calls": total_tool_calls})
                except Exception:
                    pass
                return self._stop_result(f"{msg}\n  {hint}", "error", iterations, total_tool_calls, total_usage,
                                         error=f"{err}\n  {hint}")

            assert turn is not None
            self.conversation.append_assistant(turn)
            try:
                self.session.append_turn(turn, model=call_model, cost=cost, duration_s=max(0.0, t1 - t0),
                                         call=self._call_meta, thinking_s=list(self._think_blocks))
            except TypeError:
                self.session.append_turn(turn)
            final_text = turn.text

            if outcome == "interrupted":
                return self._stop_result(final_text or INTERRUPTED_NOTE, "interrupted", iterations, total_tool_calls, total_usage)

            tool_uses = turn.tool_uses
            if (not tool_uses and turn.stop_reason == "model_context_window_exceeded"
                    and overflow_retries < MAX_OVERFLOW_RETRIES):
                overflow_retries += 1
                self._recover_from_overflow(overflow_retries)
                follow = "[The context filled up during your reply, so older content was shortened. Continue from where you stopped.]"
                self.conversation.append_user(follow)
                self.session.append_user(follow)
                system_blocks = self.system_blocks()
                continue
            if not tool_uses:
                event = "SubagentStop" if self.is_subagent else "Stop"
                res = self._hook(event, {"stop_hook_active": stop_hook_forced, "last_assistant_message": final_text})
                feedback = ""
                if res is not None and not getattr(res, "stop", False):
                    if res.blocked and res.message:
                        feedback = res.message
                    elif res.additional_context:
                        feedback = res.additional_context
                if feedback and stop_continuations < _stop_hook_cap():
                    stop_hook_forced = True
                    stop_continuations += 1
                    follow = f"[{event} hook feedback] {feedback}"
                    self.conversation.append_user(follow)
                    self.session.append_user(follow)
                    continue
                goal = "" if self.is_subagent else self.active_goal
                if goal and not goal_checked and turn.stop_reason not in ("max_tokens", "refusal") \
                        and iterations < max(1, int(self.settings.max_iterations)):
                    goal_checked = True  # at most one extra round per turn
                    self._emit(GoalCheckEvent(goal=goal, iteration=iterations))
                    follow = (f"[Goal check] The session goal is: {goal}\n"
                              "Does your work now satisfy it? If yes, reply with one short confirmation line. "
                              "If not, say what is missing and continue working toward it.")
                    self.conversation.append_user(follow)
                    self.session.append_user(follow)
                    continue
                return self._stop_result(final_text, turn.stop_reason, iterations, total_tool_calls, total_usage)

            total_tool_calls += len(tool_uses)
            try:
                self._rationales = tool_rationales(turn, self.registry, self._prev_round_tools)
                for tu_id, (why, source) in self._rationales.items():
                    name = next((t.name for t in tool_uses if t.id == tu_id), "")
                    self._emit(ToolRationale(id=tu_id, name=name, text=why, source=source))
            except Exception:
                self._rationales = {}
            self._prev_round_tools = list(tool_uses)
            if turn.stop_reason in ("max_tokens", "model_context_window_exceeded"):
                results = [
                    ToolResultBlock(
                        tool_use_id=tu.id,
                        content="Not executed: the response hit the max_tokens limit while writing this tool call, so its "
                                "arguments may be truncated. Retry with smaller steps (e.g. write large files in parts).",
                        is_error=True,
                    )
                    for tu in tool_uses
                ]
            else:
                try:
                    results = self._execute_batch(tool_uses)
                except KeyboardInterrupt:
                    partial = list(self._partial_results) if len(self._partial_results) == len(tool_uses) else [None] * len(tool_uses)
                    results = [
                        r if r is not None else ToolResultBlock(tool_use_id=tu.id, content="Interrupted by user before completion.", is_error=True)
                        for r, tu in zip(partial, tool_uses)
                    ]
                    self.conversation.append_tool_results(results, provider_encoder=self._encoder())
                    self._append_results(results)
                    raise
                except Exception as e:
                    results = [
                        ToolResultBlock(tool_use_id=tu.id, content=f"Tool execution failed unexpectedly: {type(e).__name__}: {e}", is_error=True)
                        for tu in tool_uses
                    ]

            batch_stop = self._hook_stop
            if turn.stop_reason != "max_tokens":
                calls = []
                for tu, r in zip(tool_uses, results):
                    calls.append({"tool_name": tu.name, "tool_input": tu.input if isinstance(tu.input, dict) else {},
                                  "tool_use_id": tu.id, "tool_response": str(r.content)[:20_000]})
                batch = self._hook("PostToolBatch", {"tool_calls": calls, "errors": sum(1 for r in results if r.is_error)})
                if batch is not None:
                    if batch.additional_context:
                        results = self._with_note(results, f"<system-reminder>\n{batch.additional_context}\n</system-reminder>")
                    if batch.blocked or getattr(batch, "stop", False):
                        batch_stop = batch_stop or batch.message or getattr(batch, "stop_reason", "") or "Stopped by PostToolBatch hook"
            late = self._absorb_async_hooks()
            if late:
                results = self._with_note(results, late)
            results = self._deliver_steer(results, iterations)
            left = limit - iterations
            if not step_warned and 0 < left <= STEP_WARNING_LEFT:
                step_warned = True
                warning = STEP_WARNING_SUB if self.is_subagent else STEP_WARNING_MAIN
                results = self._with_note(results, warning.format(left=left, s="" if left == 1 else "s", limit=limit))
                self.journal("step_warning", {"left": left, "limit": limit, "iteration": iterations})
            self.conversation.append_tool_results(results, provider_encoder=self._encoder())
            self._append_results(results)
            last_round = self._observations(tool_uses, results, iterations)
            stop_continuations = 0
            if batch_stop:
                self._hook_stop = ""
                return self._stop_result((final_text + "\n\n" if final_text else "") + f"[Stopped by hook: {batch_stop}]",
                                         "blocked", iterations, total_tool_calls, total_usage)
            if self._declined:
                self._declined = False
                return self._stop_result(final_text, "declined", iterations, total_tool_calls, total_usage)

        budget = int(self.settings.turn_token_budget or 0)
        over_budget = (ceiling > 0 and self.ledger.total() >= ceiling) or (
            budget > 0 and (total_usage.input + total_usage.output) >= budget)
        # Subagents only: their report is all the delegating agent gets. The main agent keeps exactly
        # max_iterations calls (--max-turns N means N); the user sees its work and can say "continue".
        if self.is_subagent and not over_budget:
            report, wrap_usage = self._step_limit_wrap_up(system_blocks, tool_schemas, iterations, last_round, user_input)
            total_usage = total_usage + wrap_usage
            if report:
                return self._stop_result(report, "max_iterations", iterations, total_tool_calls, total_usage)
        return self._stop_result(
            (final_text + "\n\n" if final_text else "") + f"[Axon reached the maximum iteration ceiling ({self.settings.max_iterations} iterations) without concluding.]",
            "max_iterations", iterations, total_tool_calls, total_usage,
        )

    def _append_results(self, results: list[ToolResultBlock]) -> None:
        timings = {r.tool_use_id: self._tool_timings.pop(r.tool_use_id) for r in results if r.tool_use_id in self._tool_timings}
        full = getattr(self, "_full_outputs", None) or {}
        full = {r.tool_use_id: full[r.tool_use_id] for r in results if r.tool_use_id in full}
        try:
            if full:
                self.session.append_results(results, timings=timings, full_outputs=full)
            else:
                self.session.append_results(results, timings=timings)
        except TypeError:
            self.session.append_results(results)

    def _log_usage(self, model: str, usage: Usage, cost: float, source: str, duration_s: float | None = None) -> None:
        """Append every LLM call to the global usage log (~/.axon/usage.jsonl)."""
        try:
            from axon import storage
            storage.record_usage(
                workspace=self.settings.workspace,
                session_id=str(getattr(self.session, "active_session_id", "") or ""),
                model=model,
                source=source,
                usage={"input": int(usage.input or 0), "output": int(usage.output or 0),
                       "cache_read": int(usage.cache_read or 0), "cache_write": int(usage.cache_write or 0),
                       "reasoning": int(getattr(usage, "reasoning", 0) or 0)},
                cost=float(cost or 0.0),
                duration_s=round(duration_s, 3) if duration_s is not None else None,
            )
        except Exception:
            pass

    def _cache_status(self, fingerprint: dict[str, Any], usage: Usage, iteration: int, context_tokens: int,
                      diagnostics: Any = None) -> CacheStatus | None:
        try:
            from axon.features.insights import api_cause
            miss, cause = self._cache.observe(fingerprint, usage, str(getattr(self.provider, "name", "")))
            inp = int(usage.input or 0)
            read = int(usage.cache_read or 0)
            resent, source = 0, ""
            if miss and self._cache.last_prev_read > 0:
                resent, source = max(0, self._cache.last_prev_read - read), "local"
            api = api_cause(diagnostics)
            if api is not None and (api[1] > 0 or miss):    # the API's own answer wins over the local comparison
                miss, cause, source = True, api[0], "api"
                if api[1] > 0:
                    resent = api[1]
            window = self.context.context_window(self.settings.model)
            return CacheStatus(
                iteration=iteration,
                input_tokens=inp,
                cached_tokens=read,
                cache_write_tokens=int(usage.cache_write or 0),
                hit_pct=round(100.0 * read / inp, 1) if inp else 0.0,
                miss=miss,
                cause=cause,
                context_tokens=context_tokens,
                context_window=window,
                context_pct=round(100.0 * context_tokens / window, 1) if window else 0.0,
                resent_tokens=resent,
                source=source if miss else "",
            )
        except Exception:
            return None

    def _annotate_manifest(self, cost: float, status: CacheStatus | None, model: str, turn: AssistantTurn | None) -> None:
        """Attach the cost and the cache verdict to the prompt manifest of this call (read by /ctxdiff)."""
        try:
            if turn is None or not self._cache.manifests:
                return
            m = self._cache.manifests[-1]
            m["cost"] = float(cost)
            if status is not None:
                from axon.features.insights import extra_cost
                m.update(miss=status.miss, cause=status.cause, resent=status.resent_tokens,
                         extra_cost=extra_cost(model, status.resent_tokens, turn.usage) if status.miss else None)
        except Exception:
            pass

    @staticmethod
    def _cache_detail(model: str, turn: AssistantTurn | None, status: CacheStatus | None) -> str:
        """` · 41k tokens re-sent · +$0.11` for a cache miss whose size is known; empty otherwise."""
        if turn is None or status is None or not status.miss or status.resent_tokens <= 0:
            return ""
        try:
            from axon.features.insights import extra_cost, miss_detail
            return miss_detail(status.resent_tokens, extra_cost(model, status.resent_tokens, turn.usage))
        except Exception:
            return ""

    def _projected_request_cost(self, system_blocks: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]) -> float:
        tokens = self.context.projected_tokens(self.conversation, system_blocks, tool_schemas)
        try:
            from axon.providers.pricing import cost_for
            return float(cost_for(self.settings.model, Usage(input=tokens, output=min(2048, self._max_output_tokens()))))
        except Exception:
            return 0.0

    def _budget_guard(self, system_blocks: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], iterations: int,
                      tool_calls: int, usage: Usage, final_text: str) -> TurnResult | None:
        """Live budget guard: project the next request's cost and warn / stop / downgrade before sending."""
        turn_limit = getattr(self.settings, "budget_turn_usd", None)
        sess_limit = getattr(self.settings, "budget_session_usd", None)
        period: list[tuple[str, float, float]] = []
        if not self.is_subagent:
            try:
                from axon.features import cost_forecast as cf
                st = cf.budget_status(self.settings.workspace)
                period = [(k, st[k]["spent"], st[k]["limit"]) for k in cf.SCOPES if st.get(k)]
            except Exception:
                period = []
        if not turn_limit and not sess_limit and not period:
            return None
        projected = self._projected_request_cost(system_blocks, tool_schemas)
        spent_turn = float(self._trace.cost) if self._trace is not None else 0.0
        spent_sess = float(self.ledger.total())
        checks = [("turn", spent_turn, turn_limit), ("session", spent_sess, sess_limit), *period]
        for scope, spent, limit in checks:
            if not limit or spent + projected <= float(limit):
                continue
            action = str(getattr(self.settings, "budget_action", "stop"))
            small = self.settings.small_model
            if action == "downgrade" and small and small != self.settings.model:
                old = self.settings.model
                from axon.providers.registry import settings_for_model
                self.settings = settings_for_model(small, self.settings).model_copy(update={"model": small})
                if type(self.provider).__module__.startswith("axon.providers"):
                    try:
                        self.provider = make_provider(small, self.settings, role="main")
                    except Exception:
                        pass
                self._emit(BudgetEvent(scope=scope, action="downgrade", spent_usd=spent, projected_usd=projected,
                                       limit_usd=float(limit), model=old, new_model=small,
                                       message=f"Switched to {small} to stay within the {scope} budget."))
                return None
            if action == "warn":
                key = f"{scope}:{self._turn_counter}"
                if key not in self._budget_warned:
                    self._budget_warned.add(key)
                    self._emit(BudgetEvent(scope=scope, action="warn", spent_usd=spent, projected_usd=projected,
                                           limit_usd=float(limit), model=self.settings.model,
                                           message=f"The next request would exceed the {scope} budget."))
                return None
            msg = (f"[Stopped by budget guard: {scope} spend ${spent:.4f} + next request ~${projected:.4f} "
                   f"would exceed the ${float(limit):.2f} {scope} budget.]")
            self._emit(BudgetEvent(scope=scope, action="stop", spent_usd=spent, projected_usd=projected,
                                   limit_usd=float(limit), model=self.settings.model, message=msg))
            return self._stop_result((final_text + "\n\n" if final_text else "") + msg, "budget_exceeded", iterations, tool_calls, usage)
        return None

    def _request_info(self, system_blocks: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], iteration: int) -> LLMRequestInfo:
        sys_text = "".join(str(b.get("text", "")) for b in system_blocks)
        markers = sum(1 for b in system_blocks if isinstance(b, dict) and "cache_control" in b)
        markers += sum(1 for t in tool_schemas if isinstance(t, dict) and "cache_control" in t)
        for m in self.conversation.messages[-6:]:
            c = m.get("content")
            if isinstance(c, list):
                markers += sum(1 for b in c if isinstance(b, dict) and "cache_control" in b)
        return LLMRequestInfo(
            iteration=iteration,
            model=self.settings.model,
            max_tokens=self._max_output_tokens(),
            effort=str(self.settings.effort),
            thinking=bool(self.settings.thinking),
            message_count=len(self.conversation.messages),
            system_tokens=int(len(sys_text) / 3.7),
            tool_schema_tokens=int(len(json.dumps(tool_schemas, default=str, separators=(",", ":"))) / 4.0),
            conversation_tokens=self.conversation.token_estimate(),
            tool_count=len(tool_schemas),
            cache_markers=markers,
            provider=str(getattr(self.provider, "name", "")),
            prompt_tools=self._prompt_tools_active(),
        )

    def _prompt_tools_active(self) -> bool:
        """True when the endpoint has no native tool calling and the tools are described in the prompt."""
        check = getattr(self.provider, "_prompt_tools_mode", None)
        try:
            return bool(check()) if callable(check) else False
        except Exception:
            return False

    def _call_model(self, system_blocks: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], iteration: int = 0) -> tuple[AssistantTurn | None, str, BaseException | None]:
        """Stream one model call. Returns (turn, outcome, error) with outcome ok|interrupted|error.

        Transient failures are retried (with backoff) only before any content was streamed."""
        attempt = 0
        try:
            self._emit(self._request_info(system_blocks, tool_schemas, iteration))
        except Exception:
            pass
        started = time.time()
        while True:
            partial_text: list[str] = []
            emitted = False
            lock = _provider_lock(self.provider)
            try:
                with lock:
                    stream: Iterator[StreamEvent] = self.provider.stream(
                        model=self.settings.model,
                        system=system_blocks,
                        messages=self.conversation.messages,
                        tools=tool_schemas,
                        max_tokens=self._max_output_tokens(),
                        effort=self.settings.effort,
                        thinking=self.settings.thinking,
                    )
                    think_started: float | None = None
                    for event in stream:
                        if isinstance(event, (TextDelta, ToolUseStart)):
                            emitted = True
                        if isinstance(event, TextDelta):
                            partial_text.append(event.text)
                        # Thinking blocks are timed the way the live view times them: from the first thinking
                        # chunk to the first text or tool call (or the end of the stream).
                        if isinstance(event, ThinkingDelta):
                            if think_started is None:
                                think_started = time.time()
                        elif think_started is not None and isinstance(event, (TextDelta, ToolBatchStart)):
                            self._think_blocks.append(round(max(0.1, time.time() - think_started), 3))
                            think_started = None
                        self._emit(event)
                    if think_started is not None:
                        self._think_blocks.append(round(max(0.1, time.time() - think_started), 3))
                    turn = self.provider.finalize()
                self._last_call_window = (started, time.time())
                return self._normalize_turn(turn), "ok", None
            except KeyboardInterrupt:
                text = ""
                usage = Usage()
                try:
                    with lock:
                        t = self.provider.finalize()
                    text = t.text or ""
                    usage = t.usage
                except BaseException:
                    pass
                text = (text or "".join(partial_text)).rstrip()
                full = f"{text}\n\n{INTERRUPTED_NOTE}" if text else INTERRUPTED_NOTE
                self._last_call_window = (started, time.time())
                # Never replay partial tool calls: their arguments are incomplete and unpaired.
                return AssistantTurn(blocks=[TextBlock(text=full)], stop_reason="end_turn", usage=usage), "interrupted", None
            except Exception as e:
                if not emitted and attempt < self.max_retries and self._should_retry(e):
                    attempt += 1
                    delay = self.retry_backoff_s * (2 ** (attempt - 1))
                    self._emit(RetryEvent(attempt=attempt, max_attempts=self.max_retries, delay_s=delay,
                                          error=f"{type(e).__name__}: {e}", iteration=iteration))
                    self.journal("retry", {"attempt": attempt, "max_attempts": self.max_retries, "delay_s": delay,
                                           "error": f"{type(e).__name__}: {e}"[:1000], "iteration": iteration})
                    try:
                        interruptible.sleep(delay)
                    except KeyboardInterrupt:
                        return AssistantTurn(blocks=[TextBlock(text=INTERRUPTED_NOTE)]), "interrupted", None
                    continue
                self._last_call_window = (started, time.time())
                self._cut_usage = Usage()
                try:       # a stream cut part-way was still processed (and billed) by the provider: keep what it reported
                    with lock:
                        self._cut_usage = self.provider.finalize().usage
                except BaseException:
                    pass
                return None, "error", e

    def _should_retry(self, exc: BaseException) -> bool:
        """Retry only when the provider did not already retry internally (Axon's own providers do)."""
        if getattr(exc, "retryable", None) is False:
            return False
        if type(self.provider).__module__.startswith("axon.providers"):
            return False
        return _is_retryable(exc)

    @staticmethod
    def _normalize_turn(turn: AssistantTurn) -> AssistantTurn:
        """Replace an empty assistant turn (which providers reject on replay) with a placeholder."""
        native = turn.native
        native_empty = native is None or native == [] or (
            isinstance(native, dict) and not native.get("content") and not native.get("tool_calls")
        )
        if not turn.blocks and native_empty:
            return AssistantTurn(blocks=[TextBlock(text="(no response)")], stop_reason=turn.stop_reason, usage=turn.usage)
        return turn

    @staticmethod
    def _observations(tool_uses: list[ToolUseBlock], results: list[ToolResultBlock], iteration: int) -> list[dict[str, Any]]:
        """Structured metadata about tool results fed into the next model call (for the UI)."""
        out: list[dict[str, Any]] = []
        total = len(tool_uses)
        for idx, (tu, res) in enumerate(zip(tool_uses, results), 1):
            c_str = str(res.content) if res else ""
            lines = c_str.splitlines()
            target = (
                tu.input.get("path") or tu.input.get("command") or tu.input.get("pattern")
                or tu.input.get("query") or tu.input.get("url") or ""
            ) if isinstance(tu.input, dict) else ""
            out.append({
                "id": tu.id,
                "name": tu.name,
                "input": dict(tu.input) if isinstance(tu.input, dict) else {},
                "target": str(target)[:70],
                "lines": len(lines),
                "bytes": len(c_str.encode("utf-8", errors="replace")),
                "tokens": max(1, int(len(c_str) / 3.7)),
                "preview": (lines[0].strip() if lines else "(No output text returned)")[:90],
                "is_error": bool(res.is_error) if res else True,
                "source_round": iteration,
                "dest_call": iteration + 1,
                "obs_num": idx,
                "total_in_round": total,
            })
        return out

    # ── tool execution ──────────────────────────────────────────────
    def _parallel_safe(self, tu: ToolUseBlock) -> bool:
        if tu.name in PARALLEL_SAFE_TOOLS:
            return True
        try:
            return bool(self.registry.get(tu.name).readonly)
        except Exception:
            return True  # unknown tools resolve instantly to an error result

    def _execute_batch(self, tool_uses: list[ToolUseBlock]) -> list[ToolResultBlock]:
        """Execute tool uses. Every slot is pre-allocated so each tool_use gets a result (Law 1).
        Read-only tools (and Task) run concurrently, bounded by settings.parallel_tools."""
        if tool_uses:
            self._emit(
                ToolBatchStart(
                    total_count=len(tool_uses),
                    tools=tool_uses,
                    user_prompt=self._current_turn_prompt,
                    mode=self.settings.mode,
                )
            )

        results: list[ToolResultBlock | None] = [None] * len(tool_uses)
        self._partial_results = results
        has_tasks = any(tu.name == "Task" for tu in tool_uses)
        workers = max(1, min(int(self.settings.parallel_tools or 1), len(tool_uses)))
        parallel = all(self._parallel_safe(tu) for tu in tool_uses) and (
            (len(tool_uses) > 1 and workers > 1) or has_tasks
        )
        self._batch_parallel = parallel

        if parallel:
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="axon-tool")
            # Number the batch's subagents in the order the model asked for them, before any thread starts,
            # so "Subagent 1" is always the first Task call (not whichever thread happened to run first).
            batch_nums: set[int] = set()
            for tu in tool_uses:
                if tu.name == "Task" and isinstance(tu.input, dict) and str(tu.input.get("prompt") or tu.input.get("subtask") or "").strip():
                    batch_nums.add(self.subagents.reserve(tu.id))  # same check TaskTool.run makes, so rejected calls leave no gap
            try:
                monitored = has_tasks and self._interactive_monitor()
                if monitored:                # from before the first thread starts: no "subagent started" line above the frame
                    self._held_events = []
                future_map = {pool.submit(self._run_one, tu): idx for idx, tu in enumerate(tool_uses)}
                if monitored:
                    try:
                        from axon.ui.subagent_monitor import run_live_subagent_monitor
                        with _pause_input():
                            run_live_subagent_monitor(future_map, self.subagents, agent=self)
                    except Exception as e:  # a monitor/UI bug must not cancel the batch
                        self.journal("error", {"error": f"live subagent monitor failed: {type(e).__name__}: {e}"})
                    finally:
                        held, self._held_events = self._held_events or [], None
                        self._deliver_after_monitor(held, tool_uses, batch_nums)
                pending = set(future_map)
                while pending:
                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for fut in done:
                        idx = future_map[fut]
                        try:
                            results[idx] = fut.result()
                        except Exception as e:
                            results[idx] = self._error_result(tool_uses[idx], f"Tool error: {type(e).__name__}: {e}")
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                if has_tasks:
                    # Nobody waits for the batch any more: stop its subagents instead of letting them keep
                    # calling the model and editing files in the background.
                    try:
                        self.subagents.cancel_all()
                    except Exception:
                        pass
                raise
            pool.shutdown(wait=True)
        else:
            for idx, tu in enumerate(tool_uses):
                try:
                    results[idx] = self._run_one(tu)
                except Exception as e:
                    results[idx] = self._error_result(tu, f"Tool error: {type(e).__name__}: {e}")

        final: list[ToolResultBlock] = []
        for r, tu in zip(results, tool_uses):
            final.append(r if r is not None else self._error_result(tu, "Tool execution did not produce a result."))

        return final

    def _deliver_after_monitor(self, held: list[Any], tool_uses: list[ToolUseBlock], batch_nums: set[int]) -> None:
        """Print a fan-out once the live monitor closes: the summary of this batch's subagents, then each Task
        call with its result card, in the order the model asked for them. The subagents' own step-by-step events
        were already on screen in the monitor; they go to the trace (and /subagents <n>) but are not replayed."""
        tasks = [t for t in self.subagents.all_tasks() if t.index in batch_nums]
        if tasks:
            try:
                from axon.ui.render import render_subagent_dashboard
                render_subagent_dashboard(tasks)
            except Exception:
                pass
        order = {tu.id: i for i, tu in enumerate(tool_uses)}
        quiet = ("SubagentEvent", "SubagentStep", "RetryEvent", "ProviderRetry")
        rank = lambda ev: order.get(getattr(ev, "id", None) or getattr(ev, "tool_use_id", None), -1)  # noqa: E731
        for ev in sorted(held, key=rank):
            self._deliver(ev, show=type(ev).__name__ not in quiet)

    def _interactive_monitor(self) -> bool:
        if self.is_subagent or self.on_event is None:
            return False
        import sys
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:
            return False

    def _error_result(self, block: ToolUseBlock, content: str) -> ToolResultBlock:
        res = ToolResultBlock(tool_use_id=block.id, content=content, is_error=True)
        self._emit(ToolExecutionResult(id=block.id, name=block.name, input=block.input, content=content, is_error=True))
        return res

    def _perm_event(self, block: ToolUseBlock, outcome: str, reason: str, source: str, approval: str | None = None,
                    rule: str = "") -> None:
        self._emit(PermissionDecisionEvent(id=block.id, tool=block.name, outcome=outcome, reason=reason, source=source,
                                           approval=approval, rule=str(rule or ""),
                                           mode=str(getattr(self.settings, "mode", "") or "")))
        try:
            self.session.append_permission(block.id, block.name, outcome, reason, source=source, approval=approval)
        except Exception:
            pass
        if source in ("user", "hook", "no-approver") or outcome == "deny":
            self.journal("permission", {"tool_use_id": block.id, "tool": block.name, "outcome": outcome, "reason": reason,
                                        "source": source, "approval": approval})
        if outcome == "deny" and source != "hook":
            self._hook("PermissionDenied", {"tool_name": block.name, "tool_input": block.input if isinstance(block.input, dict) else {},
                                            "reason": reason, "source": source}, tool_use_id=block.id)

    _FULL_OUTPUT_RE = re.compile(r"\(full output, [\d,]+ chars, saved to (.+?\.txt)\)")   # a folder name may hold ")"

    def _note_full_output(self, block: ToolUseBlock, text: str) -> None:
        """Remember a complete-output file a tool saved itself (Bash), so the transcript references it."""
        m = self._FULL_OUTPUT_RE.search(text) if "(full output, " in text else None
        if m:
            if not isinstance(getattr(self, "_full_outputs", None), dict):
                self._full_outputs = {}
            self._full_outputs[block.id] = m.group(1)

    def _cap_output(self, text: str, block: ToolUseBlock | None = None) -> str:
        """Shorten a tool output for the model. The complete output is never dropped: it is saved to
        ``projects/<key>/outputs/<session>/<tool_use_id>.txt`` and referenced from the result."""
        cap = int(getattr(self.settings, "tool_output_cap", 0) or 0)
        saved_by_tool = (getattr(self, "_full_outputs", None) or {}).get(block.id, "") if block is not None else ""
        if saved_by_tool and len(text) <= cap + 4096:
            return text  # the tool already cut it to the cap (its markers/notes add a little) and saved it in full
        if cap > 0 and len(text) > cap:
            half = cap // 2
            omitted = len(text) - 2 * half
            where = saved_by_tool
            try:
                if where:
                    raise StopIteration  # the tool already saved its complete output (e.g. Bash)
                from axon import storage
                name = f"{block.id}-{block.name}" if block is not None else f"output-{int(time.time() * 1000)}"
                saved = storage.save_full_output(self.settings.workspace,
                                                 str(getattr(self.session, "active_session_id", "") or ""), name, text)
                if saved is not None:
                    where = str(saved)
                    if block is not None:
                        if not isinstance(getattr(self, "_full_outputs", None), dict):
                            self._full_outputs = {}
                        self._full_outputs[block.id] = where
            except StopIteration:
                pass
            except Exception:
                where = ""
            note = f"; the full output is saved at {where}" if where else ""
            return f"{text[:half]}\n\n[... {omitted:,} characters omitted{note} ...]\n\n{text[-half:]}"
        return text

    def _grant_session(self, tool_name: str) -> None:
        """'Don't ask again this session': allow every call of *tool_name* until the session ends."""
        try:
            add = getattr(self.permissions, "add_session_rule", None)
            if callable(add):
                add("allow", tool_name)
                return
        except Exception:
            pass
        self._grant_always(tool_name, {})

    def _grant_always(self, tool_name: str, args: dict[str, Any]) -> None:
        try:
            grant_for = getattr(self.permissions, "grant_for", None)
            if callable(grant_for):
                grant_for(tool_name, args, self.settings.workspace)
                return
            from axon.permissions.rules import Rule
            self.permissions.grant_persistent(Rule(tool=tool_name, pattern="*"), self.settings.workspace)
        except Exception:
            pass

    def _after_file_access(self, tool_name: str, args: dict[str, Any], content: str) -> str:
        """On-demand memory (nested AXON.md, path-scoped rules) and MEMORY.md size checks."""
        target: str | None = None
        if tool_name in FILE_TOOLS:
            for k in FILE_PATH_KEYS:
                if args.get(k):
                    target = str(args[k])
                    break
        elif tool_name == "Bash":
            target = _bash_read_target(str(args.get("command", "")))
        if not target:
            return content
        from pathlib import Path as _P
        p = _P(target).expanduser()
        if not p.is_absolute():
            p = self.settings.workspace / p
        notes: list[str] = []
        try:  # skills in a sub-directory's .claude/skills load the first time a file there is touched
            if self.skills is not None and hasattr(self.skills, "load_nested_for"):
                self.skills.load_nested_for(p)
        except Exception:
            pass
        try:
            with self._memory_lock:
                loaded = self.memory.on_file_access(p)
                rendered = self.memory.render(loaded) if loaded else ""
            for node in loaded:
                self._emit(MemoryLoadedEvent(path=str(node.path), reason=node.reason, trigger=node.trigger or str(p), tokens=node.tokens))
                self._hook("InstructionsLoaded", {"file_path": str(node.path), "load_reason": "on_demand",
                                                  "trigger_file_path": str(p)})
            if rendered:
                notes.append(f"<system-reminder>\nAdditional instructions now apply (loaded because this file was accessed):\n\n{rendered}\n</system-reminder>")
        except Exception:
            pass
        if tool_name in WRITE_TOOLS and p.name == "MEMORY.md":
            try:
                from axon.agent.memory import auto_memory_dir, check_memory_index
                if p.resolve() == (auto_memory_dir(self.settings.workspace) / "MEMORY.md").resolve():
                    msg = check_memory_index(p)
                    if msg:
                        notes.append(msg)
            except Exception:
                pass
        return content + ("\n\n" + "\n\n".join(notes) if notes else "")

    def _run_one(self, block: ToolUseBlock) -> ToolResultBlock:
        """Pipeline: PreToolUse hook -> permission check -> execution -> PostToolUse hook (Law 4)."""
        t0 = time.time()
        res = self._run_one_inner(block)
        t1 = time.time()
        self._tool_timings[block.id] = t1 - t0
        text = str(res.content)
        try:
            from axon.session.activity import summarize_input
            inp = block.input if isinstance(block.input, dict) else {}
            target = (inp.get("path") or inp.get("file_path") or inp.get("command") or inp.get("pattern")
                      or inp.get("query") or inp.get("url") or inp.get("description") or "")
            full = (getattr(self, "_full_outputs", None) or {}).get(block.id, "")
            self.journal("tool", {"id": block.id, "name": block.name, "target": str(target)[:300],
                                  "input": summarize_input(inp), "is_error": bool(res.is_error),
                                  "duration_s": round(t1 - t0, 3), "output_chars": len(text),
                                  "output_preview": text[:300], **({"full_output": full} if full else {})})
            if block.name in WRITE_TOOLS and not res.is_error and target:
                from axon.session.library import record_saved
                record_saved(str(target), action="written", source=f"model:{block.name}", agent=self)
        except Exception:
            pass
        self._emit(ToolTiming(
            id=block.id,
            name=block.name,
            started_at=t0,
            ended_at=t1,
            duration_s=t1 - t0,
            lines=len(text.splitlines()),
            bytes=len(text.encode("utf-8", errors="replace")),
            est_tokens=max(1, int(len(text) / 3.7)) if text else 0,
            is_error=bool(res.is_error),
            parallel=self._batch_parallel,
        ))
        return res

    def _tool_start_event(self, block: ToolUseBlock) -> ToolExecutionStart:
        kwargs = dict(id=block.id, name=block.name, input=block.input, mode=self.settings.mode,
                      user_prompt=self._current_turn_prompt)
        why = self._rationales.get(block.id)
        if why:
            kwargs.update(rationale=why[0], rationale_source=why[1])
        parse_error = getattr(block, "parse_error", None)
        if parse_error:
            kwargs.update(parse_error=str(parse_error))
        return ToolExecutionStart(**kwargs)

    def _run_one_inner(self, block: ToolUseBlock) -> ToolResultBlock:
        self._emit(self._tool_start_event(block))

        parse_error = getattr(block, "parse_error", None)
        if parse_error:
            return self._error_result(block, f"{parse_error}. Re-issue the call with a valid JSON object of arguments.")

        try:
            tool = self.registry.get(block.name)
        except ToolError as e:
            return self._error_result(block, str(e))

        tool_input: dict[str, Any] = dict(block.input) if isinstance(block.input, dict) else {}
        hook_perm: str | None = None
        pre_context = ""
        pre = self._hook("PreToolUse", {"tool_name": tool.name, "tool_input": tool_input}, tool_use_id=block.id)
        if pre is not None:
            if pre.updated_input is not None:
                tool_input = dict(pre.updated_input)       # updatedInput replaces the whole input
            if pre.blocked or pre.permission_decision == "deny" or getattr(pre, "stop", False):
                reason = pre.message or getattr(pre, "stop_reason", "") or "no reason given"
                if getattr(pre, "stop", False):
                    self._hook_stop = self._hook_stop or str(getattr(pre, "stop_reason", "") or reason)
                self._perm_event(block, "deny", f"PreToolUse hook: {reason}", "hook")
                return self._error_result(block, f"Blocked by PreToolUse hook: {reason}")
            # "defer" only means something to a calling process in -p mode; elsewhere it is ignored.
            hook_perm = pre.permission_decision if pre.permission_decision in ("allow", "ask") else None
            pre_context = pre.additional_context

        decision = self.permissions.check(tool, tool_input, self.settings.mode)
        outcome = decision.outcome
        reason = str(decision.reason)
        if getattr(decision, "rule", None):
            source = "rule"
        elif "invariant" in reason.lower():
            source = "invariant"
        else:
            source = "mode"
        hard_ask = bool(getattr(decision, "hard", False))   # an ask rule, critical path or network read: no hook skips it
        if tool.name == "Task" and outcome != "deny" and not hard_ask:
            # Delegation itself is safe; each subagent tool call is permission-checked on its own.
            outcome, source, reason = "allow", "delegation", "Delegation is allowed; subagent tool calls are checked individually."
        elif tool.name == "Task" and outcome == "deny" and not getattr(decision, "rule", None) and self.settings.mode == "plan":
            outcome, source, reason = "allow", "delegation", "Plan mode: subagents receive read-only tools only."
        if outcome == "deny":
            self._perm_event(block, "deny", reason, source, rule=getattr(decision, "rule", None) or "")
            return self._error_result(block, f"Permission Denied: {decision.reason}")
        if hook_perm == "allow" and outcome == "ask" and not hard_ask:
            outcome, source, reason = "allow", "hook", "Allowed by PreToolUse hook."
        elif hook_perm == "ask" and outcome == "allow":
            outcome, source, reason = "ask", "hook", "PreToolUse hook requested confirmation."

        if outcome != "allow":
            req = self._hook("PermissionRequest", {"tool_name": tool.name, "tool_input": tool_input, "reason": reason},
                             tool_use_id=block.id)
            req_dec = getattr(req, "permission_decision", None) if req is not None else None
            if req is not None and getattr(req, "updated_input", None) is not None:
                tool_input = dict(req.updated_input)
            if req_dec == "allow":
                outcome, source, reason = "allow", "hook", "Allowed by PermissionRequest hook."
                self._apply_permission_updates(getattr(req, "updated_permissions", None) or [])
            elif req_dec == "deny":
                why = str(getattr(req, "message", "") or "denied")
                if getattr(req, "interrupt", False):
                    self._hook_stop = self._hook_stop or f"PermissionRequest hook denied {tool.name} and stopped: {why}"
                self._perm_event(block, "deny", f"PermissionRequest hook: {why}", "hook")
                return self._error_result(block, f"Permission denied by PermissionRequest hook: {why}")
        comment = ""
        if self._declined:
            self._perm_event(block, "deny", "The user declined an earlier action in this step.", "user")
            return self._error_result(block, "Not run: the user declined an earlier action in this step and the turn stopped.")
        if outcome == "allow":
            self._perm_event(block, "allow", reason, source, rule=getattr(decision, "rule", None) or "")
        else:
            if self.on_approval is None:
                self._perm_event(block, "deny", "Approval required but no interactive approver is available.", "no-approver")
                return self._error_result(
                    block,
                    f"Permission required for '{tool.name}' but no interactive approver is available. "
                    "Add an allow rule, or run with --mode acceptEdits / bypass.",
                )
            self.notify(f"Axon needs your permission to use {tool.name}", "permission_prompt")
            with self._approval_lock, _pause_input():
                approval = self.on_approval(tool.name, tool_input, decision)
            self._perm_event(block, "deny" if approval == "deny" else "allow", reason, "user", approval=str(approval),
                             rule=getattr(decision, "rule", None) or "")
            comment = str(getattr(approval, "comment", "") or "")
            if approval == "deny":
                # A reason lets the model carry on. A plain No stops the turn, in the main conversation only.
                self._declined = not comment and self.decline_ends_turn and not self.is_subagent
                return self._error_result(block, f"User declined this action. Their reason: {comment}" if comment else "User declined this action.")
            if approval == "always":
                self._grant_session(tool.name)
            elif approval == "rule":
                self._grant_always(tool.name, tool_input)

        ctx = ToolContext(
            workspace=self.settings.workspace,
            file_state=self.file_state,
            todos=self.todos,
            settings=self.settings,
            ledger=self.ledger,
            agent=self,
            checkpoints=self.checkpoints,
            tool_use_id=block.id,
        )
        snapshot = self._snapshot_files(tool.name, tool_input)
        t_tool = time.monotonic()
        try:
            out = tool.run(tool_input, ctx)
            self._note_full_output(block, str(out))
            content, is_error = self._cap_output(str(out), block), False
        except ToolError as e:
            content, is_error = self._cap_output(str(e), block), True
        except Exception as e:
            content, is_error = f"Unexpected {type(e).__name__}: {e}", True
        changes = self._file_changes(snapshot)
        if changes:
            content = f"{content}\n{filechanges.summary_line(changes)}"

        if not is_error:
            content = self._after_file_access(tool.name, tool_input, content)
            content = diagnostics.after_edit(tool.name, tool_input, content, ctx)

        duration_ms = int((time.monotonic() - t_tool) * 1000)
        post = self._hook(
            "PostToolUse",
            {"tool_name": tool.name, "tool_input": tool_input, "duration_ms": duration_ms,
             "tool_response": {"content": content[:100_000], "is_error": is_error}},
            tool_use_id=block.id,
        )
        if post is not None:
            if getattr(post, "stop", False):
                self._hook_stop = self._hook_stop or str(getattr(post, "stop_reason", "") or "Stopped by PostToolUse hook")
            if getattr(post, "updated_tool_output", None) is not None:
                content = self._hook_tool_output(post.updated_tool_output)
            extra = []
            if post.blocked and post.message:
                extra.append(f"[PostToolUse hook feedback]\n{post.message}")
            if post.additional_context:
                extra.append(f"[PostToolUse hook context]\n{post.additional_context}")
            if extra:
                content = content + "\n\n" + "\n\n".join(extra)
        if comment:
            content = f"{content}\n\n[The user approved this action with a comment]\n{comment}"
        if pre_context:
            content = f"{content}\n\n[PreToolUse hook context]\n{pre_context}"

        if is_error:
            fail = self._hook("PostToolUseFailure", {"tool_name": tool.name, "tool_input": tool_input,
                                                     "error": content[:20_000], "is_interrupt": False,
                                                     "duration_ms": duration_ms}, tool_use_id=block.id)
            if fail is not None and getattr(fail, "stop", False):
                self._hook_stop = self._hook_stop or str(getattr(fail, "stop_reason", "") or "Stopped by PostToolUseFailure hook")
            if fail is not None and fail.blocked and fail.message:
                content = f"{content}\n\n[PostToolUseFailure hook feedback]\n{fail.message}"
            if fail is not None and getattr(fail, "additional_context", ""):
                content = f"{content}\n\n[PostToolUseFailure hook context]\n{fail.additional_context}"
        res = ToolResultBlock(tool_use_id=block.id, content=content, is_error=is_error,
                              images=() if is_error else tuple(ctx.images))
        self._emit(ToolExecutionResult(id=block.id, name=block.name, input=tool_input, content=res.content,
                                       is_error=res.is_error, changes=tuple(changes)))
        return res

    def _snapshot_files(self, tool_name: str, tool_input: dict[str, Any]) -> filechanges.Snapshot | None:
        """Before a foreground shell command: the workspace's files, so the files it changes can be shown."""
        if tool_name not in SHELL_TOOLS or tool_input.get("run_in_background"):
            return None
        try:
            return filechanges.take_snapshot(self.settings.workspace)
        except Exception:
            return None

    def _file_changes(self, snapshot: filechanges.Snapshot | None) -> list[filechanges.FileChange]:
        if snapshot is None:
            return []
        try:
            return filechanges.changes_since(snapshot)
        except Exception:
            return []
