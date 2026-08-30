# 01 — Architecture

## Layering

Four layers, each depending only on the one below it. The dependency direction is
strictly downward — the agent loop never imports from `ui/`, and `providers/` never
imports from `tools/`.

```
┌─ ui / cli ────────────────────────────────────────────────────────┐
│  Terminal I/O only. Rendering, input, slash commands, spinners.   │
│  Knows nothing about the API. Fully replaceable.                  │
└───────────────────────────────────────────────────────────────────┘
┌─ agent ───────────────────────────────────────────────────────────┐
│  The loop. Orchestrates: ask provider → execute tools → repeat.   │
│  Owns conversation state, context budget, permission decisions.    │
└───────────────────────────────────────────────────────────────────┘
┌─ tools / permissions / session ───────────────────────────────────┐
│  Capability + policy + durability. Each tool is an isolated unit  │
│  with a schema and a `run()`. Permissions gate them. Sessions      │
│  persist them.                                                     │
└───────────────────────────────────────────────────────────────────┘
┌─ providers ───────────────────────────────────────────────────────┐
│  Wire protocol. Turns internal types into HTTP and back. The only │
│  layer that knows Anthropic and OpenAI shapes exist.              │
└───────────────────────────────────────────────────────────────────┘
```

**Why this order matters.** The agent loop is the part worth testing, and it is also
the part hardest to test if it touches the network or the terminal. By putting
`providers` below it behind a `Protocol`, the entire loop becomes testable with a
`FakeProvider` that replays scripted turns — no network, no cost, deterministic. That
single boundary is what makes [`08-TESTING.md`](08-TESTING.md) possible.

## Module map

```
axon/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── .env.example                 # AXON_API_KEY=sk-...
├── .gitignore                   # .env, sessions/, __pycache__, .axon/
├── AGENTS.md                    # conventions Axon reads about its own repo
│
├── src/axon/
│   ├── __main__.py              #  ~20  python -m axon
│   ├── cli.py                   # ~220  argparse, REPL vs -p print mode, wiring
│   ├── config.py                # ~140  Settings: env → .env → ~/.axon/config.toml
│   ├── errors.py                #  ~60  exception hierarchy
│   │
│   ├── providers/
│   │   ├── base.py              # ~180  Protocol + internal block types  ← the contract
│   │   ├── anthropic.py         # ~260  official SDK, SSE accumulator
│   │   ├── openai_compat.py     # ~280  raw httpx2 + fingerprint headers
│   │   ├── registry.py          #  ~90  model → provider routing, PRICING table
│   │   └── capabilities.py      # ~110  probe & cache which betas the proxy supports
│   │
│   ├── agent/
│   │   ├── loop.py              # ~320  the ReAct engine            ← the core
│   │   ├── state.py             # ~200  Conversation, FileState, TodoState
│   │   ├── prompt.py            # ~180  system prompt + env preamble + AGENTS.md
│   │   ├── context.py           # ~240  token budget, compaction ladder, cache marks
│   │   └── subagent.py          # ~130  Task tool → nested loop, own budget
│   │
│   ├── tools/
│   │   ├── base.py              # ~110  Tool ABC: name, schema, run(), readonly flag
│   │   ├── registry.py          #  ~90  discovery, schema export, dispatch
│   │   ├── fs_read.py           # ~140  Read
│   │   ├── fs_write.py          # ~200  Write, Edit, MultiEdit
│   │   ├── shell.py             # ~230  Bash — persistent session, timeout, caps
│   │   ├── search.py            # ~210  Glob, Grep, Ls
│   │   ├── todo.py              #  ~90  TodoWrite
│   │   ├── web.py               # ~130  WebFetch
│   │   └── task.py              #  ~70  Task (sub-agent dispatch)
│   │
│   ├── permissions/
│   │   ├── engine.py            # ~170  allow / ask / deny decision
│   │   ├── rules.py             # ~150  `Tool(pattern)` matching, deny-wins
│   │   └── paths.py             # ~110  workspace jail, symlink resolution
│   │
│   ├── session/
│   │   ├── store.py             # ~190  append-only JSONL, resume, list
│   │   └── ledger.py            # ~120  usage → cost accounting
│   │
│   ├── ui/
│   │   ├── theme.py             # ~110  ANSI palette (ported)
│   │   ├── input.py             # ~170  paste-burst detection, readline history (ported)
│   │   ├── picker.py            # ~120  arrow-key selector (ported)
│   │   ├── render.py            # ~280  streaming markdown, tool cards, diffs
│   │   └── approve.py           # ~110  the permission prompt
│   │
│   ├── commands/
│   │   └── builtin.py           # ~300  /help /model /clear /compact /cost /resume …
│   └── hooks/
│       └── runner.py            # ~120  pre/post tool-use shell hooks
│
└── tests/                       # mirrors src/, plus tests/cassettes/ and tests/evals/
```

Roughly 5,800 lines of source. No module exceeds 400 lines — a constraint from goal G7,
and a practical one: a file you can hold in your head is a file you can change safely.

## The internal data model

Everything above the provider layer speaks these types and only these types. This is
the single most important design decision in the project.

```python
# providers/base.py

@dataclass(frozen=True)
class TextBlock:
    text: str

@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None      # Anthropic only; None on OpenAI-compat

@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]             # ALWAYS a parsed dict, never a JSON string

@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False

Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0                # OpenAI-compat reports this separately

StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"
]

@dataclass
class AssistantTurn:
    blocks: list[Block]
    stop_reason: StopReason
    usage: Usage
    native: Any        # provider-native content, replayed verbatim — see ADR-003


class Provider(Protocol):
    name: str

    def stream(
        self, *, model: str, system: str, messages: list[Message],
        tools: list[ToolSpec], max_tokens: int,
        effort: str | None, thinking: bool,
    ) -> Iterator[StreamEvent]: ...

    def finalize(self) -> AssistantTurn: ...
    def supports(self, feature: str) -> bool: ...
```

Two properties are worth calling out.

**`ToolUseBlock.input` is always a `dict`.** The OpenAI protocol delivers tool
arguments as a JSON *string* (`"{\"expr\":\"4817*2903\"}"`), and streaming delivers it
as fragments that must be concatenated first. Parsing happens once, inside the
provider, so no tool ever has to think about it. Tools that string-match serialized
arguments break the moment escaping changes.

**`native` sits alongside `blocks`.** See ADR-003 — this looks redundant and is not.

## Control flow, one turn

```
user types "fix the failing test"
        │
        ▼
  cli.py ──► Conversation.append(user message)
        │
        ▼
  agent/loop.py ─────────────────────────────────────────┐
        │                                               │
        │  1. context.prepare()                         │
        │       ├─ estimate tokens                      │
        │       ├─ compact if over threshold            │
        │       └─ place cache breakpoints              │
        │  2. provider.stream(...)                      │
        │       └─► ui/render.py  (live text/thinking)  │
        │  3. turn = provider.finalize()                │
        │  4. Conversation.append(turn)   ← verbatim    │
        │  5. if stop_reason != "tool_use": DONE ───────┼──► print summary, cost
        │  6. for each ToolUseBlock, concurrently:      │
        │       ├─ permissions.check()  → allow/ask/deny│
        │       ├─ hooks.pre()                          │
        │       ├─ tools.registry.execute()             │
        │       └─ hooks.post()                         │
        │  7. Conversation.append(ALL results, ONE msg) │
        │  8. loop back to 1 ───────────────────────────┘
        │
        ▼
  session/store.py appends every step to the JSONL transcript as it happens
```

Step 7 is where most implementations go wrong; [`02-AGENT-LOOP.md`](02-AGENT-LOOP.md)
explains why in detail.

## Decision records

Short-form ADRs. Each records a choice, the rejected alternative, and the reason —
so the *why* survives after the code changes.

### ADR-001 — Hand-build the harness; do not use the Claude Agent SDK

**Decision.** Implement the loop, tools, permissions, and context management from
scratch against the raw Messages API.

**Rejected.** `claude-agent-sdk`, which is Claude Code packaged as a library and would
provide all of the above in about thirty lines.

**Why.** For a product, the SDK is the correct answer and this ADR would be inverted.
For this project the harness *is* the deliverable — the thing being demonstrated is
understanding of how an agent works, which a wrapper hides. Recorded explicitly so the
tradeoff is visible rather than looking like ignorance of the SDK's existence.

### ADR-002 — Official `anthropic` SDK for Claude; raw `httpx2` for OpenAI-compat

**Decision.** Two different transport strategies, one per provider.

**Why.** Forced by the proxy. `agentrouter.org` returns `401 unauthorized_client_error`
unless the request carries `user-agent: Anthropic/Python*` plus `x-stainless-*`
headers. The `anthropic` SDK emits these natively. The `openai` SDK emits its own
User-Agent and is **rejected**, so the OpenAI-compatible path must be hand-rolled over
`httpx2` with the header set. Verified by probe — see
[`04-PROVIDERS.md`](04-PROVIDERS.md#verified-probe-results).

**Note.** `anthropic` 1.x is built on `httpx2`, not `httpx`. Both are installed;
`import httpx2 as httpx` is correct and mixing them will fail.

### ADR-003 — Keep provider-native content alongside normalized blocks

**Decision.** `AssistantTurn` carries both `blocks` (normalized) and `native` (the raw
provider content). The conversation replays `native`; the UI and tools read `blocks`.

**Rejected.** Normalize on the way in, re-serialize on the way out.

**Why.** Round-tripping is lossy in ways that break the API. Anthropic thinking blocks
carry a cryptographic `signature`; server-side compaction emits opaque block types;
future block types will exist that the normalizer does not know about. Re-serializing
from `blocks` drops or mangles them and the next request fails. Replaying `native`
verbatim is the only correct approach. The normalized view exists for everything
*except* the next request.

### ADR-004 — Every tool result returns, including failures

**Decision.** A tool that raises, times out, is denied by the permission engine, or is
interrupted still produces a `ToolResultBlock` with `is_error=True`.

**Why.** The API contract requires exactly one `tool_result` per `tool_use`; a missing
one is a 400 that kills the session. It is also better behaviour — the model reads the
error and adapts, which is the whole point of the observe step. Errors are data, not
control flow.

### ADR-005 — Concurrency is opt-in per tool via a `readonly` flag

**Decision.** Tools declare `readonly: bool`. Read-only tools in one batch execute
concurrently in a thread pool; any batch containing a mutating tool executes serially
in the order the model requested.

**Rejected.** Full concurrency; no concurrency.

**Why.** Two `Edit`s to the same file racing is corruption. Two `Grep`s racing is free
speed, and search fan-out is the most common parallel pattern. The flag captures the
distinction with one bit and no scheduler.

### ADR-006 — Per-file `(mtime_ns, sha256)` state, enforced before mutation

**Decision.** `Read` records file identity. `Write`/`Edit` on an existing file require
a recorded read, and reject if the file changed since.

**Why.** This is the difference between an agent that edits code and an agent that
destroys code. Without it, the model edits against a remembered version of a file that
another process — or its own earlier edit — has already changed. See
[`03-TOOLS.md`](03-TOOLS.md#the-read-before-edit-invariant).

### ADR-007 — Append-only JSONL transcript, not a JSON blob

**Decision.** One JSON object per line, `fsync`'d as the session proceeds.

**Rejected.** The predecessor's approach — rewrite a whole `.json` file per turn.

**Why.** Crash-safety and cost. A killed process loses at most the last line rather
than the file. Appending is O(1) rather than O(session). And a JSONL transcript is
directly replayable as a test cassette.

### ADR-008 — Capability probing rather than assumed feature support

**Decision.** A `capabilities.py` module probes the endpoint once per model, caches the
result under `~/.axon/`, and the context manager degrades based on it.

**Why.** Axon talks to a proxy, not to Anthropic directly. Betas like server-side
compaction (`compact_20260112`), context editing (`clear_tool_uses_20250919`), and task
budgets may or may not pass through, and the answer can change without notice. Assuming
support produces mystery 400s; probing produces a documented degradation path.

### ADR-009 — Stdlib-only terminal UI

**Decision.** ANSI escapes, `readline`, `termios`, and `select` — no `rich`, no
`prompt_toolkit`.

**Why.** `agentrouter_chat.py` already proves the approach works, and its theme,
paste-burst input, and arrow-key picker port directly. It keeps the dependency list to
`anthropic` + `httpx2` + `pydantic`, which matters for a tool people install to try.
The cost is hand-writing incremental markdown rendering, which is a bounded problem.

### ADR-010 — Typed settings object; no module-level mutable config

**Decision.** A `pydantic` `Settings` model, constructed once at startup, passed down
explicitly.

**Why.** The predecessor script has a live bug that makes the case: `Session.__init__`
never assigns `self.stream`, but `to_dict()`, `from_dict()`, and four call sites read
it — an `AttributeError` waiting on the first save or stream toggle. A typed model with
declared defaults makes that class of bug unrepresentable, and secrets get a
`SecretStr` that does not leak into logs or `repr()`.

---

Next: [`02-AGENT-LOOP.md`](02-AGENT-LOOP.md) — the engine itself.
