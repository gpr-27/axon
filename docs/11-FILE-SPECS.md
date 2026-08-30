# 11 — File Specifications

[`01-ARCHITECTURE.md`](01-ARCHITECTURE.md#module-map) gives the module *map* — names, sizes,
one-line purposes. This document is the *build spec*: for every file, its exact public API,
what it may import, what it must not, its invariants, and the phase it is written in.

**How to use this.** Work down a phase's file list in order. Each spec is a contract — write
the file to satisfy the signatures listed, then write its test file. If you find yourself
needing an import the spec forbids, the design is wrong and belongs in a decision record,
not in a quiet exception.

**Dependency rule, enforced by review.** Imports point strictly downward through the layers.
`ui/` may import from `agent/`; `agent/` may never import from `ui/`. `tools/` may not import
`providers/`. Anything that needs to cross upward takes a callback.

```
        cli.py ──────────► commands/ ──► ui/
           │                 │            │
           ▼                 ▼            ▼
        agent/ ◄──────────────────────────┘  (ui reads agent state, never the reverse)
        │  │  │
        │  │  └──► tools/ ──► permissions/ ──► config.py
        │  └─────► session/                    errors.py
        └────────► providers/                  (both importable everywhere)
```

Legend for the tables: **API** is the complete public surface — anything not listed is
private. **LOC** is a target, not a limit to hit; the hard rule is ≤400.

---

## Root files

### `pyproject.toml` — P0
```toml
[project]
name = "axon"
requires-python = ">=3.12"
dependencies = [
  "anthropic>=1.0",        # Anthropic path + fingerprint headers
  "httpx2>=0.1",           # OpenAI-compat path (anthropic 1.x rides on httpx2, not httpx)
  "pydantic>=2.9",         # Settings, SecretStr
  "pydantic-settings>=2.6",
  "tomli-w>=1.0",          # writing back "always allow" rules
]
[project.optional-dependencies]
dev = ["pytest", "pytest-cov", "ruff", "mypy"]
[project.scripts]
axon = "axon.cli:main"
```
No terminal-UI dependency — that is [ADR-009](01-ARCHITECTURE.md#adr-009--stdlib-only-terminal-ui).

### `.env.example`, `.gitignore`, `AGENTS.md` — P0
`.gitignore` must contain `.env`, `.axon/`, `__pycache__/`, `*.egg-info`, `.coverage`
**before the first commit**. `AGENTS.md` describes Axon's own conventions so Axon can work
on itself — the first real dogfood test.

---

## `src/axon/` — top level

### `errors.py` · ~60 LOC · P0
The exception hierarchy. Written first because everything raises from it.

| Symbol | Purpose |
|---|---|
| `AxonError(Exception)` | Base. |
| `ConfigError` | Missing key, malformed TOML. Fatal at startup. |
| `ProviderError` | Transport/API failure. Carries `status`, `body`. |
| `ToolError` | **A tool failed in a way the model should see.** Message text is read by the model — write it as instruction, not diagnosis. |
| `PermissionDenied(ToolError)` | Subclass, so it also becomes a `tool_result`. |
| `StaleFileError(ToolError)` | Read-before-edit violation. |
| `InterruptedTurn(AxonError)` | Carries `partial_results` so [Law 5](02-AGENT-LOOP.md#law-5--interrupts-still-close-the-turn) can close the turn. |
| `BudgetExceeded(AxonError)` | Token/cost ceiling. |

**Invariant:** every `ToolError` subclass is catchable as one type in the loop, because the
loop converts exactly that type into `is_error` results. Non-`ToolError` exceptions are
*also* converted, but they mean a bug and are logged as such.

Imports: nothing.

### `config.py` · ~140 LOC · P0
```python
class Settings(BaseSettings):
    api_key: SecretStr = Field(alias="AXON_API_KEY")
    base_url: str = "https://agentrouter.org"
    model: str = "claude-opus-5"
    effort: Effort = "xhigh"
    thinking: bool = True
    mode: Mode = "default"
    workspace: Path = Field(default_factory=Path.cwd)
    max_tokens: int = 32_000
    max_iterations: int = 40
    turn_token_budget: int = 500_000
    session_cost_ceiling: Decimal = Decimal("5.00")
    compact_at: float = 0.85
    parallel_tools: int = 6
    bash_timeout_s: int = 120
    tool_output_cap: int = 30_000
    permissions: PermissionConfig      # allow/deny lists
    hooks: dict[str, list[HookSpec]]

    @classmethod
    def load(cls, cli_overrides: dict) -> "Settings": ...
```
Precedence, lowest first: defaults → `~/.axon/config.toml` → `./.axon/config.toml` →
environment → `.env` → CLI flags.

**Invariants:** frozen after construction (`model_config = {"frozen": True}`); `api_key` is
`SecretStr` so it never appears in a traceback or `repr`; exactly one call site for
`.get_secret_value()` per provider ([ADR-010](01-ARCHITECTURE.md#adr-010--typed-settings-object-no-module-level-mutable-config)).

Imports: `pydantic`, `errors`. **Never** imports anything else in `axon`.

### `cli.py` · ~220 LOC · P0 (skeleton) → P2 (REPL)
```python
def main(argv: list[str] | None = None) -> int: ...
def build_parser() -> argparse.ArgumentParser: ...
def run_print_mode(agent: Agent, prompt: str, fmt: str) -> int: ...
def run_repl(agent: Agent, ui: Renderer) -> int: ...
```
Flags: `-p/--print`, `--output-format {text,json}`, `--model`, `--mode`, `--effort`,
`--continue`, `--resume [ID]`, `--allowed-tools`, `--append-system-prompt`,
`--dangerously-skip-permissions`, `--no-thinking`, `--workspace`, `doctor`, `eval`.

Reads stdin when not a TTY so `cat spec.md | axon -p "implement this"` works. Its only job
is wiring — no agent logic lives here.

### `__main__.py` · ~20 LOC · P0
`sys.exit(main())`.

---

## `providers/` — P0, P5

### `providers/base.py` · ~180 LOC · P0 — **the contract**
The most important file in the project: everything above it depends on these types, and
nothing in it depends on a wire format.

```python
# Block types — the normalized internal representation
@dataclass(frozen=True)
class TextBlock:      text: str
@dataclass(frozen=True)
class ThinkingBlock:  text: str; signature: str | None = None
@dataclass(frozen=True)
class ToolUseBlock:   id: str; name: str; input: dict[str, Any]   # ALWAYS parsed
@dataclass(frozen=True)
class ToolResultBlock: tool_use_id: str; content: str; is_error: bool = False
Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock

StopReason = Literal["end_turn","max_tokens","stop_sequence","tool_use","pause_turn","refusal"]

@dataclass
class Usage:
    input: int = 0; output: int = 0
    cache_read: int = 0; cache_write: int = 0; reasoning: int = 0
    def __add__(self, other: "Usage") -> "Usage": ...

@dataclass
class AssistantTurn:
    blocks: list[Block]; stop_reason: StopReason; usage: Usage
    native: Any                     # provider-native content, replayed verbatim (ADR-003)
    @property
    def tool_uses(self) -> list[ToolUseBlock]: ...
    @property
    def text(self) -> str: ...

# Stream events — what the renderer consumes
TextDelta(text) | ThinkingDelta(text) | ToolUseStart(id, name)
  | ToolArgsDelta(id, fragment) | ToolUseComplete(id) | TurnComplete(stop_reason, usage)

class Provider(Protocol):
    name: str
    def stream(self, *, model, system, messages, tools,
               max_tokens, effort, thinking) -> Iterator[StreamEvent]: ...
    def finalize(self) -> AssistantTurn: ...
    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict]: ...
    def supports(self, feature: str) -> bool: ...
```

**Invariants.** `ToolUseBlock.input` is a parsed `dict` at this boundary — never a JSON
string; the string-vs-object asymmetry dies here. Blocks are frozen. `native` is opaque
above this layer.

Imports: stdlib + `errors` only. **No SDK imports** — that is what makes `FakeProvider`
possible ([`08-TESTING.md`](08-TESTING.md#layer-1--fakeprovider)).

### `providers/anthropic.py` · ~260 LOC · P0
```python
class AnthropicProvider:
    def __init__(self, settings: Settings) -> None: ...     # Anthropic(auth_token=…)
    def stream(self, **kw) -> Iterator[StreamEvent]: ...
    def finalize(self) -> AssistantTurn: ...
    def encode_tool_results(self, results) -> list[dict]: ...  # ONE user message
    def count_tokens(self, **kw) -> int: ...
    def supports(self, feature: str) -> bool: ...
```
Three traps to encode, each verified against the current API rather than recalled:
`thinking={"type":"adaptive","display":"summarized"}` — `budget_tokens` is a **400** on
Opus 5/4.8/4.7 and Sonnet 5/Fable 5; `display` defaults to `"omitted"` so omitting it
yields no visible reasoning; assistant prefill **400s**, so output shape is constrained via
the system prompt or a tool schema.

Private: `_accumulate(event)`, `_tool_buffers: dict[int, str]` keyed by content-block index,
`_json_parse_or_error`.

### `providers/openai_compat.py` · ~280 LOC · P5
```python
class OpenAICompatProvider:
    URL = "{base_url}/v1/chat/completions"
    def _headers(self) -> dict: ...       # Bearer + FINGERPRINT
    def encode_tool_results(self, results) -> list[dict]: ...  # N messages, one per result
```
Must set `stream_options={"include_usage": True}` or usage is silently zero. Tool-call
fragments accumulate keyed by `index`, **not** `id` — the id arrives only in the first
fragment ([`04-PROVIDERS.md`](04-PROVIDERS.md#openaicompatprovider)).

### `providers/registry.py` · ~90 LOC · P0 stub → P5
```python
PROVIDER_FOR: dict[str, type]
PRICING: dict[str, dict[str, float]]
def provider_for(model: str, settings: Settings) -> Provider: ...
def known_models() -> list[str]: ...
```

### `providers/capabilities.py` · ~110 LOC · P4
```python
FEATURES: dict[str, tuple[str | None, dict | None]]
def probe(model: str, settings: Settings, refresh: bool = False) -> dict[str, bool]: ...
```
Cached in `~/.axon/capabilities.json`, 7-day TTL, keyed by `(base_url, model)`.

---

## `agent/` — the core

### `agent/state.py` · ~200 LOC · P0 → P1
Three state holders, deliberately separate because they have different lifetimes.

```python
class Conversation:
    messages: list[dict]                     # provider-encodable
    def append_user(self, text: str) -> None: ...
    def append_assistant(self, turn: AssistantTurn) -> None: ...   # replays `native`
    def append_tool_results(self, results: list[ToolResultBlock]) -> None: ...  # ONE call, whole batch
    def validate(self) -> None: ...          # raises if any tool_use lacks its result
    def token_estimate(self) -> int: ...

class FileState:                             # ADR-006
    _seen: dict[Path, tuple[int, str]]       # path → (mtime_ns, sha256)
    def record_read(self, p: Path) -> None: ...
    def check_writable(self, p: Path) -> None: ...   # raises StaleFileError
    def invalidate(self, p: Path) -> None: ...

class TodoState:
    items: list[Todo]
    def replace(self, items: list[dict]) -> None: ...  # enforces ≤1 in_progress
    def render(self) -> str: ...
```
**`append_tool_results` takes the whole batch in one call.** Making the batch the unit of
the API is how [Law 2](02-AGENT-LOOP.md#law-2--batching) is enforced structurally rather
than remembered.

### `agent/loop.py` · ~320 LOC · P0 — **the ReAct engine**
```python
class Agent:
    def __init__(self, provider, tools, permissions, context,
                 session, ledger, settings, on_event=None): ...
    def run_turn(self, user_input: str) -> TurnResult: ...
    def _execute_all(self, tool_uses) -> list[ToolResultBlock]: ...
    def _run_one(self, block) -> ToolResultBlock: ...    # permission → hook → execute
    def _check_guards(self, i: int, turn) -> None: ...
```
Full implementation in [`02-AGENT-LOOP.md`](02-AGENT-LOOP.md). The five laws are its
acceptance criteria; each has a test in
[`08-TESTING.md`](08-TESTING.md#invariant-tests).

Imports: `providers.base`, `tools`, `permissions`, `session`, `agent.*`. **Never `ui`** —
it emits events through `on_event`, which is what lets `-p` mode and the REPL share it.

### `agent/prompt.py` · ~180 LOC · P0 → P6
```python
def build_system(settings, tools, capabilities, project_context) -> list[dict]: ...
def discover_project_context(cwd: Path) -> str: ...     # AGENTS.md / CLAUDE.md walk-up
def env_preamble(settings) -> str: ...
```
Returns a **list** of blocks so `cache_control` can be attached to the last one. Full text
in [`12-SYSTEM-PROMPT.md`](12-SYSTEM-PROMPT.md).

**Invariant: nothing volatile.** No clock, no token count, no turn number — one variable
element costs a full cache miss every turn
([`05-CONTEXT-AND-COST.md`](05-CONTEXT-AND-COST.md#rung-0--prompt-caching-always)).

### `agent/context.py` · ~240 LOC · P4
```python
class ContextManager:
    def prepare(self, conv: Conversation, system, tools) -> PreparedRequest: ...
    def project_tokens(self, ...) -> int: ...
    def apply_cache_marks(self, system, tools, messages) -> None: ...
    def _trim_large_results(self, conv) -> int: ...       # rung 1
    def _evict_stale_results(self, conv) -> int: ...       # rung 2
    def _compact(self, conv) -> None: ...                  # rung 3
```

### `agent/subagent.py` · ~130 LOC · P6
```python
def run_subagent(prompt: str, parent: Agent, *, tools=None,
                 max_iterations=15, token_budget=100_000) -> str: ...
```
Fresh `Conversation`, read-only tool subset by default, own ledger line. **Only the final
text crosses back** — that asymmetry is the entire justification for the tool.

---

## `tools/` — P0, P1, P6

### `tools/base.py` · ~110 LOC · P0
```python
@dataclass
class ToolContext:
    workspace: Path; file_state: FileState; todos: TodoState
    settings: Settings; ledger: Ledger; agent: "Agent | None"   # sub-agent dispatch only

class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]        # prompt engineering — see 03-TOOLS.md
    schema: ClassVar[dict]           # JSON Schema, additionalProperties: false
    readonly: ClassVar[bool] = False  # gates parallelism (ADR-005)
    default_permission: ClassVar[Literal["allow","ask","deny"]] = "ask"
    @abstractmethod
    def run(self, args: dict, ctx: ToolContext) -> str: ...
    def render_call(self, args: dict) -> str: ...   # the one-line UI summary
```
**Invariants.** `run` returns a string for the model or raises `ToolError`. It never prints,
never touches the network except in `WebFetch`, and never mutates a file it did not resolve
through `permissions.paths`. Schemas are built once and frozen.

### `tools/registry.py` · ~90 LOC · P0
```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...          # raises ToolError, not KeyError
    def schemas(self, provider_style: str) -> list[dict]: ...
    def subset(self, names: list[str] | None, readonly_only=False) -> "ToolRegistry": ...
```
`get` on an unknown name must raise `ToolError` so [Law 1](02-AGENT-LOOP.md#law-1--pairing)
holds when the model hallucinates a tool.

### `tools/fs_read.py` · ~140 LOC · P0 — `Read`
`readonly=True`, `default_permission="allow"`. Args `path`, `offset`, `limit`. Emits
`cat -n`-style numbering. Records into `FileState` on success — **this is what makes `Edit`
legal later**. 2000-line default; oversized files are truncated with an explicit pointer to
use `offset`.

### `tools/fs_write.py` · ~200 LOC · P1 — `Write`, `Edit`, `MultiEdit`
All three: `ctx.file_state.check_writable(p)` before touching anything, atomic
`tmp` + `os.replace`, `invalidate` + `record_read` after. `Edit` requires `old_string` to
appear **exactly once** unless `replace_all=True`, and its error names the count — an
ambiguous match is a bug, and telling the model *how* ambiguous lets it fix itself.

### `tools/shell.py` · ~230 LOC · P1 — `Bash`
Persistent `/bin/bash` via `subprocess.Popen`, one per session. Command completion detected
by a random sentinel echoed with `$?`:
```python
sentinel = f"__AXON_{self._id}_{seq}__"
self._proc.stdin.write(f'{command}\nprintf "\\n{sentinel}%d\\n" "$?"\n')
```
Timeout kills the **process group** (`os.killpg`), not just the shell. Env scrubbed of
`*_API_KEY`/`*_TOKEN`/`*_SECRET`. Output capped at 30k, truncated in the *middle*. Requires
a `description` arg — it is what the approval prompt shows next to the real command.

### `tools/search.py` · ~210 LOC · P1 — `Glob`, `Grep`, `Ls`
All `readonly=True`, `default_permission="allow"`. `Grep` shells to `rg --json` when
available and falls back to a pure-Python walker; behaviour and output shape must be
identical either way, which is a test. `Glob` sorts mtime-descending — recently-touched
files are what the agent almost always wants.

### `tools/todo.py` · ~90 LOC · P6 — `TodoWrite`
Externalized memory that survives compaction. Enforces at most one `in_progress`.

### `tools/web.py` · ~130 LOC · P6 — `WebFetch`
Fetch → strip to text → summarize with `deepseek-v4-flash` against the caller's question.
15-minute cache. Output is framed as untrusted data
([`06-SECURITY.md`](06-SECURITY.md#prompt-injection)).

### `tools/task.py` · ~70 LOC · P6 — `Task`
Thin wrapper over `agent.subagent.run_subagent`. Depth cap 1: a sub-agent's registry
excludes `Task`.

---

## `permissions/` — P1, P2

### `permissions/paths.py` · ~110 LOC · P1
```python
def resolve_in_workspace(root: Path, raw: str) -> Path: ...   # raises PermissionDenied
_BLOCKED_NAMES = {".git", ".ssh", ".aws", ".gnupg", "node_modules", ".axon"}
```
`resolve()` **before** the containment comparison. Forgetting that is the classic bug and
`workspace/link → /etc` is the test that catches it.

### `permissions/rules.py` · ~150 LOC · P2
```python
@dataclass(frozen=True)
class Rule:
    tool: str; pattern: str | None
    def matches(self, tool_name: str, args: dict) -> bool: ...
def parse_rules(spec: list[str]) -> list[Rule]: ...
```
Glob for paths, `prefix:*` for commands. **Any command containing shell metacharacters
never matches an allow rule** — documented limitation, tested explicitly.

### `permissions/engine.py` · ~170 LOC · P2
```python
class PermissionEngine:
    def check(self, tool: Tool, args: dict, mode: Mode) -> Decision: ...
    def grant_persistent(self, rule: Rule) -> None: ...   # "always allow" → project config
```
Order is load-bearing: hard invariants → deny → allow → mode default.

**Invariant: `check()` never reads tool output.** It sees the tool name and arguments only.
This is the single control that stops prompt injection from escalating.

---

## `session/` — P3

### `session/store.py` · ~190 LOC · P3
```python
class SessionStore:
    def open(self, session_id: str | None = None) -> Session: ...
    def append(self, record: dict) -> None: ...           # one JSON line + fsync
    def load(self, session_id: str) -> Conversation: ...
    def list_recent(self, limit=20) -> list[SessionMeta]: ...
    def latest(self) -> str | None: ...
```
Append-only JSONL at `~/.axon/projects/<slug>/<ulid>.jsonl`, mode `0600`. Secrets redacted
on write. Append-only is what makes `SIGKILL` survivable
([ADR-007](01-ARCHITECTURE.md#adr-007--append-only-jsonl-transcript-not-a-json-blob)).

### `session/ledger.py` · ~120 LOC · P3
```python
class Ledger:
    def record(self, model: str, usage: Usage, *, tag="main") -> Decimal: ...
    def total(self) -> Decimal: ...
    def uncached_counterfactual(self) -> Decimal: ...
    def render(self) -> str: ...
```
`Decimal` throughout — money.

---

## `ui/` — P2

Detail in [`13-UI-AND-CLI.md`](13-UI-AND-CLI.md). Signatures only here.

| File | LOC | API |
|---|---:|---|
| `ui/theme.py` | 110 | `Theme` dataclass, `C` palette, `supports_color()`, `strip_ansi()` |
| `ui/input.py` | 170 | `read_input(prompt) -> str` — paste-burst detection, readline history, multiline |
| `ui/picker.py` | 120 | `pick(options, title) -> int \| None` — arrow keys, raw mode via `termios` |
| `ui/render.py` | 280 | `Renderer.on_event(e)`, `.tool_card(...)`, `.diff(old,new)`, `.spinner(...)`, `.markdown(text)` |
| `ui/approve.py` | 110 | `ask_approval(tool, args, decision) -> Literal["once","always","deny"]` |

`Renderer` consumes `StreamEvent` and nothing else — it cannot tell which provider produced
the stream, and it is the only place in the codebase that writes to stdout.

---

## `commands/` and `hooks/` — P6

### `commands/builtin.py` · ~300 LOC · P6
```python
@dataclass
class Command:
    name: str; help: str
    run: Callable[[CommandContext, str], CommandResult]
COMMANDS: dict[str, Command]
def dispatch(line: str, ctx: CommandContext) -> CommandResult | None: ...
def load_custom(dirs: list[Path]) -> dict[str, Command]: ...   # .axon/commands/*.md
```
Returns `CommandResult(handled, message, mutated_state)`. A command never calls the model
directly except `/compact` and `/init`.

### `hooks/runner.py` · ~120 LOC · P6
```python
class HookRunner:
    def run(self, event: str, payload: dict) -> HookOutcome: ...
```
JSON on stdin, exit code as verdict: `0` proceed, `2` block (stdout becomes the
`tool_result`), other = log and proceed. 5-second timeout, never inherits the API key.

---

## Build order

The exact sequence. Each row's tests are written with it, not after.

| # | File | Phase | Blocks |
|--:|------|:-----:|--------|
| 1 | `errors.py` | P0 | everything |
| 2 | `config.py` | P0 | providers, cli |
| 3 | `providers/base.py` | P0 | **all of `agent/`, `tools/`, tests** |
| 4 | `tools/base.py`, `tools/registry.py` | P0 | tools |
| 5 | `tools/fs_read.py` | P0 | the P0 acceptance test |
| 6 | `agent/state.py` | P0 | loop |
| 7 | `agent/prompt.py` | P0 | loop |
| 8 | `providers/anthropic.py` | P0 | loop |
| 9 | **`agent/loop.py`** | P0 | ← the P0 gate |
| 10 | `cli.py` (print mode), `__main__.py` | P0 | |
| 11 | `tests/fakes.py` (`FakeProvider`) | P0 | **every later test** |
| 12 | `permissions/paths.py` | P1 | fs_write, shell |
| 13 | `tools/fs_write.py`, `shell.py`, `search.py` | P1 | |
| 14 | `permissions/rules.py`, `engine.py` | P2 | |
| 15 | `ui/*` | P2 | REPL |
| 16 | `cli.py` (REPL) | P2 | |
| 17 | `session/store.py`, `ledger.py` | P3 | |
| 18 | `agent/context.py`, `providers/capabilities.py` | P4 | |
| 19 | `providers/openai_compat.py`, `registry.py` | P5 | |
| 20 | `agent/subagent.py`, `tools/{task,todo,web}.py` | P6 | |
| 21 | `commands/builtin.py`, `hooks/runner.py` | P6 | |
| 22 | `tests/evals/*`, `doctor` | P7 | |

**Item 11 is placed deliberately.** `FakeProvider` is written the moment the loop exists,
before any other tool or subsystem — every subsequent file is then testable the day it is
written, with no network and no cost.

---

## Test file mapping

`tests/` mirrors `src/axon/` one-for-one, plus three additions:

```
tests/
├── fakes.py                    FakeProvider, scripted(), ctx() helpers
├── conftest.py                 tmp_path workspace, settings, registry fixtures
├── test_invariants.py          the five laws — highest-value tests in the suite
├── cassettes/                  recorded SSE (see 08-TESTING.md)
├── evals/                      20 fixture repos + task.yaml + checkers
└── <mirror of src/axon/>
```

---

Next: [`12-SYSTEM-PROMPT.md`](12-SYSTEM-PROMPT.md) — the prompt, in full.
