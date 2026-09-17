# Axon Codebase Architecture & Exhaustive File-by-File Implementation Specification

> **Repository:** `axon-terminal` (`axon`)  
> **Source Directory:** `src/axon/`  
> **Language:** Python 3.10+ (Zero third-party agent orchestration frameworks)  
> **Test Suite:** 528 passing invariant and integration tests  

---

## Table of Contents

1. [Executive Architectural Overview](#1-executive-architectural-overview)
   - [The 4 Downward Dependency Layers](#the-4-downward-dependency-layers)
   - [The 5 Invariant Laws of the Agent Engine](#the-5-invariant-laws-of-the-agent-engine)
2. [How Tool Calls Work (End-to-End Pipeline)](#2-how-tool-calls-work-end-to-end-pipeline)
   - [The Tool Contract & Context](#the-tool-contract--context)
   - [Schema Reflection & Export](#schema-reflection--export)
   - [6-Law Security & Permission Gating](#6-law-security--permission-gating)
   - [Pre-Tool Interception & Exit Code 2 Veto](#pre-tool-interception--exit-code-2-veto)
   - [Concurrent vs Serial Tool Dispatch](#concurrent-vs-serial-tool-dispatch)
   - [Staleness Detection & Read-Before-Write](#staleness-detection--read-before-write)
   - [Error Transformation (Law 4)](#error-transformation-law-4)
   - [Checkpoint Snapshotting & Instant `/rewind`](#checkpoint-snapshotting--instant-rewind)
   - [Post-Tool Hooks & Batch Assembly](#post-tool-hooks--batch-assembly)
3. [How Slash Commands Work (The Command Engine)](#3-how-slash-commands-work-the-command-engine)
   - [The REPL Command Interception Loop](#the-repl-command-interception-loop)
   - [Dispatch Table & Handler Signatures](#dispatch-table--handler-signatures)
   - [Terminal Shortcuts & Interactive Pickers](#terminal-shortcuts--interactive-pickers)
   - [Dynamic Skill Invocation](#dynamic-skill-invocation)
4. [Exhaustive File-by-File Implementation Deep Dive](#4-exhaustive-file-by-file-implementation-deep-dive)
   - [4.1 Root Package (`src/axon/`)](#41-root-package-srcaxon)
   - [4.2 Agent Core Engine (`src/axon/agent/`)](#42-agent-core-engine-srcaxonagent)
   - [4.3 Command Engine (`src/axon/commands/`)](#43-command-engine-srcaxoncommands)
   - [4.4 Extensibility Hooks (`src/axon/hooks/`)](#44-extensibility-hooks-srcaxonhooks)
   - [4.5 Model Context Protocol (`src/axon/mcp/`)](#45-model-context-protocol-srcaxonmcp)
   - [4.6 Security & Permissions (`src/axon/permissions/`)](#46-security--permissions-srcaxonpermissions)
   - [4.7 Wire Protocol Providers (`src/axon/providers/`)](#47-wire-protocol-providers-srcaxonproviders)
   - [4.8 Durability, Ledger & Sessions (`src/axon/session/`)](#48-durability-ledger--sessions-srcaxonsession)
   - [4.9 Custom Workflows & Skills (`src/axon/skills/`)](#49-custom-workflows--skills-srcaxonskills)
   - [4.10 The 24+ Native Runtime Tools (`src/axon/tools/`)](#410-the-24-native-runtime-tools-srcaxontools)
   - [4.11 Terminal UI & Presentation Engine (`src/axon/ui/`)](#411-terminal-ui--presentation-engine-srcaxonui)
5. [Summary Architecture Matrix](#5-summary-architecture-matrix)

---

## 1. Executive Architectural Overview

Axon is engineered from first principles in pure Python. It avoids framework abstractions (such as LangChain, CrewAI, or AutoGen) in favor of explicit state management, standard library primitives, and strongly typed dataclasses.

### The 4 Downward Dependency Layers

The codebase enforces a strict downward-only dependency hierarchy. Modules higher in the stack may import from modules lower in the stack, but lower modules **never** import upwards:

```
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 4: ui / cli (Terminal Presentation & REPL Input)                 │
│ src/axon/ui/  ·  src/axon/cli.py                                       │
│ • Terminal rendering, ANSI themes, paste bursts, markdown streaming    │
│ • Knows NOTHING about LLM providers or tool internals                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ depends downward
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 3: agent (The ReAct Core & Orchestration Engine)                 │
│ src/axon/agent/ (loop.py, state.py, prompt.py, context.py, etc.)       │
│ • Heartbeat loop (Reason → Act → Observe), Token budget ladder         │
│ • System prompt assembly, Subagent orchestration, File staleness       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ depends downward
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 2: capabilities & durability (Tools, Perms, Sessions, MCP)       │
│ src/axon/tools/ · permissions/ · session/ · hooks/ · mcp/ · skills/    │
│ • 24 native tools, 6-law permission engine, append-only JSONL storage  │
│ • Stdio JSON-RPC MCP bridge, Pre/Post tool shell hooks                 │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ depends downward
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 1: providers (Wire Protocol Adapters)                            │
│ src/axon/providers/ (base.py, anthropic.py, openai_compat.py, etc.)    │
│ • Turns normalized blocks into wire HTTP/SSE requests and back         │
│ • Isolated behind a Protocol for 100% deterministic offline testing    │
└────────────────────────────────────────────────────────────────────────┘
```

---

### The 5 Invariant Laws of the Agent Engine

Every interaction inside Axon is governed by five invariants enforced structurally in code:

1. **Law 1 — Pairing Invariant:**  
   Every `ToolUseBlock` emitted by the model must be answered by exactly one `ToolResultBlock` with matching `tool_use_id` in the immediately subsequent user message. In `src/axon/agent/loop.py`, the result array is pre-allocated by length before any tool executes. No unhandled exception, syntax error, or early return can shorten or corrupt the batch.
2. **Law 2 — Batching Invariant:**  
   All `ToolResultBlock`s produced from an assistant turn must be packed into a **single** user message. Splitting results across multiple user messages degrades model performance and causes it to stop requesting tools in parallel.
3. **Law 3 — Verbatim Replay (ADR-003):**  
   Provider-native assistant responses (`turn.native`) are preserved alongside normalized blocks and replayed verbatim in subsequent requests. This preserves cryptographic thinking signatures and caching tokens required by the Anthropic and OpenAI protocols.
4. **Law 4 — Errors Are Data:**  
   Tools that encounter exceptions, missing parameters, or OS failures produce `ToolResultBlock(is_error=True)` containing actionable instructions for the model. Tools **never** raise exceptions out of the loop.
5. **Law 5 — Interrupt Safety:**  
   If the user presses `Ctrl+C` while tools are executing, Axon catches `KeyboardInterrupt`, synthesizes error `ToolResultBlock`s for all unexecuted calls, appends them to close the turn, and only then unwinds to the REPL. The conversation history is never left corrupted.

---

## 2. How Tool Calls Work (End-to-End Pipeline)

Tool execution is not a simple function call; it is a multi-stage pipeline involving verification, governance, execution, and durability.

```
       Model Emits AssistantTurn with ToolUseBlock(s)
                            │
                            ▼
          1. Schema Validation & Registry Lookup
       (src/axon/tools/registry.py -> ToolRegistry.get)
                            │
                            ▼
           2. 6-Law Security & Permission Gate
    (src/axon/permissions/engine.py -> PermissionEngine.check)
       ├─ Deny Rule -> Returns ToolResultBlock(is_error=True)
       ├─ Ask Rule  -> Prompts User via src/axon/ui/approve.py
       └─ Allow / Bypass -> Proceeds
                            │
                            ▼
            3. Pre-Tool Lifecycle Shell Hook
        (src/axon/hooks/runner.py -> HookRunner.run)
       ├─ Exit Code 2 -> VETO! Replaces output with Hook stdout
       └─ Exit Code 0 -> Proceeds
                            │
                            ▼
          4. Pre-Edit SHA256 Snapshot Checkpoint
     (src/axon/session/checkpoint.py -> capture_before_edit)
                            │
                            ▼
                5. Execution Pipeline Dispatch
      Read-Only Batch? ──► Parallel ThreadPoolExecutor(max_workers=6)
      Mutating Batch?  ──► Serial Execution in Model Order
                            │
                            ▼
          6. Tool Run (e.g. Read, Write, Bash, etc.)
       (Staleness checks, workspace jail checks, sandboxing)
                            │
                            ▼
             7. Post-Tool Hooks & Output Truncation
        (Output capped at tool_output_cap, e.g. 150k chars)
                            │
                            ▼
         8. Consolidated Batch Appended to Conversation
    (Single User Message with all ToolResultBlocks -> Law 2)
```

### Detailed Pipeline Steps

1. **The Tool Contract (`Tool` ABC in [tools/base.py](file:///Users/gpr/Documents/axon/src/axon/tools/base.py)):**
   Every tool inherits from `Tool`. It declares:
   - `name: ClassVar[str]`
   - `description: ClassVar[str]`
   - `schema: ClassVar[dict[str, Any]]` (JSON schema for input validation)
   - `readonly: ClassVar[bool]` (whether tool mutates disk/system state)
   - `default_permission: ClassVar[Literal["allow", "ask", "deny"]]`
   - `run(self, args: dict[str, Any], ctx: ToolContext) -> str`
2. **Execution Context (`ToolContext`):**
   Passed to every tool invocation, containing:
   - `workspace: Path` (root of active project)
   - `file_state: FileState` (tracks `mtime` and `sha256` of all files read in session)
   - `todos: TodoState` (interactive plan and task checklists)
   - `settings: Settings` (global runtime configuration)
   - `ledger: Ledger` (financial cost and token accounting)
   - `agent: Agent` (allows sub-agent dispatch for `TaskTool`)
   - `checkpoints: CheckpointManager` (rollback snapshots)
3. **Staleness Detection (Read-Before-Write):**
   Tools modifying files (`Write`, `Edit`, `MultiEdit`, `Patch`) check `ctx.file_state`. If a file has not been read during the session, or if the file on disk has been modified externally since it was read (`mtime` or `sha256` mismatch), the tool refuses to write and returns an actionable error asking the model to re-read the file.
4. **Concurrency Model:**
   If every tool in a batch has `readonly = True` (e.g., `Glob` + `Grep` + `Read`), Axon dispatches them concurrently across a `ThreadPoolExecutor` up to `parallel_tools` (default: 6). If any tool is mutating (`readonly = False`), the entire batch runs serially in model-emitted order.
5. **Atomic Checkpoint Rollbacks:**
   Before any mutating tool touches disk, `CheckpointManager.capture_before_edit(path)` stores the file's pre-modification text. If a turn produces broken code or failing tests, typing `/rewind` restores files instantly.

---

## 3. How Slash Commands Work (The Command Engine)

Commands provide interactive control over the agent without restarting the session or wiping conversation state.

### The REPL Interception Cycle

When the user enters text in the REPL ([src/axon/cli.py](file:///Users/gpr/Documents/axon/src/axon/cli.py)):
1. **Shell Escapes (`!`):** If the input starts with `!` (e.g., `!pytest` or `!git status`), Axon runs the command directly in the host shell, streaming stdout/stderr without sending anything to the LLM.
2. **Slash Commands (`/`):** If the input starts with `/`, `dispatch_command(line, agent)` in [src/axon/commands/builtin.py](file:///Users/gpr/Documents/axon/src/axon/commands/builtin.py) intercepts it.
3. **Argument Parsing & Normalization:** The input is split into `(cmd, arg)`. Command aliases are resolved (e.g., `/q`, `/queue`, `/dequeue`).
4. **Command Execution:** The corresponding handler (`handle_<cmd>(agent, arg)`) executes. Handlers return a `CommandResult(handled=True, should_exit=...)`.
5. **Skill Fallback:** If `cmd` is not a builtin command, Axon checks `agent.skills.skills`. If a matching skill exists (e.g., `/debug` or `/verify`), it loads the skill prompt from markdown and dispatches a full agent turn automatically.

### Key Shortcuts & Commands Table

| Shortcut / Command | Handler Function | Action Taken |
|---|---|---|
| `Tab` / `Shift+Tab` | `handle_mode` | Cycles permission modes: `default` ➔ `acceptEdits` ➔ `plan` ➔ `bypass` |
| `Ctrl+P` or `@` | `run_fuzzy_file_finder` | Interactive arrow-key fuzzy search to insert workspace files directly into prompt |
| `←` (Left Arrow) | `handle_sessions_list` | Opens the full-screen interactive session switcher dashboard |
| `/model [name]` | `handle_model` | Dynamically switches active model (e.g. `deepseek-v4-flash`, `claude-opus-5`) |
| `/effort [tier]` | `handle_effort` | Changes reasoning effort scale: `reflex`, `balanced`, `synapse`, `quantum` |
| `/rewind` / `/undo` | `agent.checkpoints.rewind_last` | Restores files to their exact pre-turn state before tool modifications |
| `/compact` | `handle_compact` | Triggers context compaction ladder to reclaim token headroom |
| `/cost` | `handle_cost` | Renders total session cost, token usage, and prompt cache savings % |
| `/subagents` | `handle_subagents` | Renders live progress, tokens, and status of all concurrent worker agents |
| `/mcp [install]` | `handle_mcp` | Inspects, connects, and installs Model Context Protocol servers |
| `/learn [fact]` | `handle_learn` | Saves persistent learned memory to `.axon/memory/` |

---

## 4. Exhaustive File-by-File Implementation Deep Dive

Below is the technical specification of all **87 Python files** organized into their 11 packages.

---

### 4.1 Root Package (`src/axon/`)

#### 1. [`src/axon/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/__init__.py)
- **Role:** Package root and public API initialization.
- **Implementation:** Defines version constant `__version__ = "GPR_27"` and exports the high-level classes `Agent`, `Settings`, `create_default_registry`.

#### 2. [`src/axon/__main__.py`](file:///Users/gpr/Documents/axon/src/axon/__main__.py)
- **Role:** CLI module entry point for `python -m axon`.
- **Implementation:** Imports `main` from `axon.cli` and invokes it with `sys.exit(main())`.

#### 3. [`src/axon/cli.py`](file:///Users/gpr/Documents/axon/src/axon/cli.py)
- **Role:** Top-level CLI argument parser, wiring, REPL interactive loop, and print-mode runner.
- **Implementation:**
  - `build_parser()`: Configures `argparse` with flags: `--print` (`-p`), `--model`, `--mode`, `--effort`, `--workspace`, `--continue`, `--resume`, `--no-thinking`, `--dangerously-skip-permissions`.
  - `run_print_mode(agent, prompt, fmt, renderer)`: Executes a single non-interactive prompt. Supports `--output-format json` (used in programmatic CI/CD pipelines) or standard formatted text.
  - `run_repl(agent, renderer)`: Full interactive REPL. Clears terminal, displays startup banner, checks subagent status, reads user input with paste-burst protection, delegates slash commands, and runs `run_interactive_turn`.
  - `main()`: Resolves paths, loads settings via `Settings.load(overrides)`, initializes `SessionStore`, `Ledger`, `PermissionEngine`, `ToolRegistry`, `Provider`, and wires the `Agent`.

#### 4. [`src/axon/config.py`](file:///Users/gpr/Documents/axon/src/axon/config.py)
- **Role:** Strongly typed runtime settings management using Pydantic.
- **Implementation:**
  - Defines type aliases: `Mode = Literal["default", "acceptEdits", "plan", "bypass"]` and `Effort = Literal["reflex", "balanced", "synapse", "quantum", ...]`.
  - `PermissionConfig`: Pydantic model with `allow: list[str]` and `deny: list[str]`.
  - `HookSpec`: Pydantic model specifying `event: str`, `command: str`, and optional `tool: str`.
  - `Settings(BaseSettings)`: Immutable config object with `env_prefix="AXON_"`. Stores API keys (as `SecretStr`), `base_url`, `model`, `effort`, `max_tokens` (128k), `turn_token_budget` (2M), `max_iterations` (50), `compact_at` (0.85), `parallel_tools` (6), `bash_timeout_s` (180s).
  - Normalization validators: `normalize_model` strips quotes/spaces; `normalize_effort` maps user inputs (`xhigh` ➔ `quantum`, `high` ➔ `synapse`, `medium` ➔ `balanced`, `low` ➔ `reflex`).
  - `load()`: Hierarchical configuration loader merging defaults ➔ `~/.axon/config.toml` ➔ `.axon/config.toml` ➔ environment variables ➔ local `.env` ➔ CLI flags.

#### 5. [`src/axon/errors.py`](file:///Users/gpr/Documents/axon/src/axon/errors.py)
- **Role:** Centralized exception hierarchy.
- **Implementation:**
  - `AxonException`: Base exception class.
  - `ConfigError`: Raised when configuration files are unparseable or missing credentials.
  - `ProviderError`: Raised on non-recoverable wire protocol failures.
  - `ToolError`: Special tool exception. Implements `for_model()` which returns an actionable error string for the model (Law 4) rather than terminating the agent loop.
  - `ContextBudgetExceededError`: Raised when context limits cannot be recovered through compaction.
  - `PermissionDeniedError`: Raised when an unapproved action is attempted.
  - `HookExecutionError`: Raised when external hook IPC fails.

---

### 4.2 Agent Core Engine (`src/axon/agent/`)

#### 6. [`src/axon/agent/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/agent/__init__.py)
- **Role:** Agent package exports.
- **Implementation:** Exports `Agent`, `Conversation`, `FileState`, `TodoState`, `ContextManager`, `TurnResult`.

#### 7. [`src/axon/agent/loop.py`](file:///Users/gpr/Documents/axon/src/axon/agent/loop.py)
- **Role:** The ReAct Engine loop (`run_turn`). The primary core of Axon.
- **Implementation:**
  - `TurnResult`: Dataclass containing `final_text`, `stop_reason`, `iterations`, `tool_calls_count`, and `usage`.
  - `_sync_project_guide()`: Auto-initializes and updates `axon.md` in the user's workspace with directory layout, project directives, and recent accomplishments.
  - `Agent`: Primary coordinator containing references to `Provider`, `ToolRegistry`, `PermissionEngine`, `ContextManager`, `SessionStore`, `Ledger`, `CheckpointManager`, and `SubagentManager`.
  - `run_turn(user_input)`: Iterates up to `max_iterations`:
    1. Prepares context via `self.context.prepare(...)`.
    2. Streams reasoning and tool calls via `self.provider.stream(...)`.
    3. Records the native assistant turn verbatim (Law 3).
    4. Checks for termination (`stop_reason != "tool_use"`).
    5. Dispatches tools via `_execute_batch`.
    6. Appends all results into a single user message (Law 2).
  - `_execute_batch()`: Pre-allocates results array (Law 1), evaluates readonly concurrency, and wraps calls in interrupt handlers (Law 5).
  - `_run_one()`: Executes the per-tool pipeline: permission verification ➔ hook veto ➔ tool execution ➔ error capture ➔ output truncation.

#### 8. [`src/axon/agent/state.py`](file:///Users/gpr/Documents/axon/src/axon/agent/state.py)
- **Role:** In-memory session state containers.
- **Implementation:**
  - `Conversation`: Holds the raw message list sent to providers. Implements `append_user`, `append_assistant`, `append_tool_results`, and `token_estimate` (character-based heuristic ~3.7 chars/token).
  - `FileState`: Tracks `(mtime, sha256)` of files read during the session for staleness detection.
  - `TodoItem` & `TodoState`: Tracks interactive tasks with status (`pending`, `in_progress`, `completed`), descriptions, and progress percentages.
  - `MessageQueue`: Thread-safe buffer for user-queued messages injected while a turn is actively processing.

#### 9. [`src/axon/agent/prompt.py`](file:///Users/gpr/Documents/axon/src/axon/agent/prompt.py)
- **Role:** Dynamic system prompt formation.
- **Implementation:**
  - `IDENTITY`: Defines core identity as a terminal-native, investigative coding agent.
  - `OPERATING_RULES`: Directives covering investigation before action, style matching, minimal diffs, and verified task completion.
  - `tool_policy()`: Dynamically formats descriptions and usage guidelines for all registered tools.
  - `env_preamble()`: Injects working directory, active model, and permission mode warnings (e.g. Plan Mode restrictions).
  - `discover_project_context()`: Discovers and injects `axon.md`, `AGENTS.md`, or `CLAUDE.md`.
  - `discover_memory_context()`: Loads learned project conventions from `MemoryStore`.
  - `build_system()`: Combines all layers into structured system prompt blocks with cache markers.

#### 10. [`src/axon/agent/context.py`](file:///Users/gpr/Documents/axon/src/axon/agent/context.py)
- **Role:** Context window budgeting, sliding window enforcement, and the 3-rung Compaction Ladder.
- **Implementation:**
  - `get_effective_budget()`: Returns the minimum of configured token budget and active model limit.
  - `prepare()`: Evaluates conversation token usage against `compact_at = 0.85`:
    - **Sliding Window:** Trims older turns if `max_history_turns` is configured.
    - **Rung 1 (`_trim_large_results`):** Truncates tool outputs > 8,000 characters to first 3,000 + last 3,000 characters.
    - **Rung 2 (`_evict_stale_results`):** Replaces tool outputs older than 4 messages with an eviction placeholder.
    - **Rung 3 (`_summarize_older_turns`):** Compresses older conversational turns into structured bullet summaries.

#### 11. [`src/axon/agent/subagent.py`](file:///Users/gpr/Documents/axon/src/axon/agent/subagent.py)
- **Role:** Concurrency manager for isolated subagent worker threads.
- **Implementation:**
  - `SubagentTask`: Dataclass tracking subagent ID, prompt, step count, elapsed time, isolated `Conversation`, live logs, and token usage.
  - `SubagentManager`: Thread-safe registry for spawning, monitoring, and completing subagents.
  - `run_subagent()`: Instantiates an isolated `Agent` instance with its own conversation context and a step limit (`max_steps=15`). Returns the synthesized conclusion back to the coordinator agent.

#### 12. [`src/axon/agent/memory.py`](file:///Users/gpr/Documents/axon/src/axon/agent/memory.py)
- **Role:** Persistent memory management for learned codebase patterns.
- **Implementation:**
  - `MemoryItem`: Represents a learned fact with `title`, `content`, `category`, and `scope` (`project` vs `personal`).
  - `MemoryStore`: Manages storage under `.axon/memory/` (project) and `~/.axon/memory/` (global). Implements `save()`, `list_all()`, and `delete()`.

#### 13. [`src/axon/agent/images.py`](file:///Users/gpr/Documents/axon/src/axon/agent/images.py)
- **Role:** Multimodal vision ingestion service.
- **Implementation:**
  - `ImageIngestionService`: Inspects user inputs for image paths, validates MIME types (PNG, JPEG, WebP, GIF), converts images to Base64, generates thumbnails, and packages them into multimodal API content blocks.

#### 14. [`src/axon/agent/worktree.py`](file:///Users/gpr/Documents/axon/src/axon/agent/worktree.py)
- **Role:** Isolated git worktree manager.
- **Implementation:**
  - `WorktreeManager`: Creates temporary git worktrees (`git worktree add`) allowing subagents to test aggressive architectural refactors in complete isolation without dirtying the primary working tree. Cleans up worktrees upon completion.

---

### 4.3 Command Engine (`src/axon/commands/`)

#### 15. [`src/axon/commands/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/commands/__init__.py)
- **Role:** Command package exports.
- **Implementation:** Exports `dispatch_command` and `CommandResult`.

#### 16. [`src/axon/commands/builtin.py`](file:///Users/gpr/Documents/axon/src/axon/commands/builtin.py)
- **Role:** Comprehensive slash command dispatch engine (>128 KB of logic).
- **Implementation:**
  - Contains handler functions for over 40 slash commands:
    - `handle_help`: Renders rich markdown cheat sheet of commands.
    - `handle_model`: Switches active LLM with optional fuzzy picker.
    - `handle_effort`: Adjusts reasoning tier (`reflex`, `balanced`, `synapse`, `quantum`).
    - `handle_mode`: Switches permission mode (`default`, `acceptEdits`, `plan`, `bypass`).
    - `handle_cost`: Formats detailed ledger breakdown and cache savings.
    - `handle_rewind`: Calls `agent.checkpoints.rewind_last()` to undo turns.
    - `handle_subagents`: Displays live dashboard of active worker threads.
    - `handle_mcp`: Lists, installs, and tests Model Context Protocol servers.
    - `handle_learn` / `handle_memory`: Saves and manages persistent project memory.
    - `handle_diff` / `handle_review`: Displays git diffs and runs automated code reviews.
  - `dispatch_command(line, agent)`: Parses input, strips whitespace, matches commands against dispatch table, and executes fallback skill matching.

---

### 4.4 Extensibility Hooks (`src/axon/hooks/`)

#### 17. [`src/axon/hooks/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/hooks/__init__.py)
- **Role:** Hooks package exports.
- **Implementation:** Exports `HookRunner` and `HookOutcome`.

#### 18. [`src/axon/hooks/runner.py`](file:///Users/gpr/Documents/axon/src/axon/hooks/runner.py)
- **Role:** Pre- and post-tool lifecycle shell hook execution.
- **Implementation:**
  - `HookOutcome`: Frozen dataclass with `proceed: bool` and optional `override_output: str | None`.
  - `HookRunner`: Evaluates registered hooks for lifecycle events (`pre_tool`, `post_tool`).
  - Serializes tool invocation details as JSON and pipes them to the external hook process via `stdin`.
  - **Veto Protocol:** If the hook process exits with return code `2`, the action is vetoed, tool execution is bypassed, and stdout from the hook becomes the tool result.

---

### 4.5 Model Context Protocol (`src/axon/mcp/`)

#### 19. [`src/axon/mcp/bridge.py`](file:///Users/gpr/Documents/axon/src/axon/mcp/bridge.py)
- **Role:** Stdio-based JSON-RPC 2.0 client bridge for MCP servers.
- **Implementation:**
  - `MCPServerConnection`: Represents a live subprocess connection to an MCP server.
  - Spawns subprocesses with piped stdio, executes the JSON-RPC `initialize` handshake, and issues `tools/list`.
  - Dynamically synthesizes native `Tool` class instances from MCP schemas and registers them in Axon's `ToolRegistry`.
  - Translates agent tool calls into `tools/call` JSON-RPC messages and unpacks results.

#### 20. [`src/axon/mcp/manager.py`](file:///Users/gpr/Documents/axon/src/axon/mcp/manager.py)
- **Role:** Configuration management for MCP servers.
- **Implementation:**
  - Manages and merges configurations between global `~/.axon/mcp.json` and project-scoped `.axon/mcp.json`.
  - Implements `add_server()`, `remove_server()`, and `add_preset()`.

#### 21. [`src/axon/mcp/catalog.py`](file:///Users/gpr/Documents/axon/src/axon/mcp/catalog.py)
- **Role:** Pre-configured catalog of popular MCP servers.
- **Implementation:**
  - Declares `MCPServerPreset` definitions for SQLite, GitHub, PostgreSQL, Filesystem, Memory, and Brave Search MCP servers.

#### 22. [`src/axon/mcp/interactive.py`](file:///Users/gpr/Documents/axon/src/axon/mcp/interactive.py)
- **Role:** Interactive terminal UI for managing MCP servers.
- **Implementation:**
  - Interactive arrow-key menu to list active servers, install presets, configure environment variables, and test live connectivity.

---

### 4.6 Security & Permissions (`src/axon/permissions/`)

#### 23. [`src/axon/permissions/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/permissions/__init__.py)
- **Role:** Permissions package exports.
- **Implementation:** Exports `PermissionEngine`, `Rule`, `WorkspaceJail`.

#### 24. [`src/axon/permissions/engine.py`](file:///Users/gpr/Documents/axon/src/axon/permissions/engine.py)
- **Role:** 6-Law decision engine for evaluating tool authorizations.
- **Implementation:**
  - `Decision`: Frozen dataclass with `outcome: Literal["allow", "ask", "deny"]` and `reason: str`.
  - `PermissionEngine.check(tool, args, mode)` evaluates in strict priority order:
    1. **Structural Invariants:** Blocks root filesystem deletion (`rm -rf /`) unconditionally.
    2. **Deny Rules:** Explicit user deny rules always win over allow rules.
    3. **Allow Rules:** Explicit allow rules authorize execution.
    4. **Mode Defaults:**
       - `bypass`: Allows all operations.
       - `plan`: Allows readonly tools; denies all mutating actions.
       - `acceptEdits`: Allows filesystem modifications; asks for shell commands.
       - `default`: Asks before executing any mutating tool.

#### 25. [`src/axon/permissions/rules.py`](file:///Users/gpr/Documents/axon/src/axon/permissions/rules.py)
- **Role:** Rule representation and pattern matching.
- **Implementation:**
  - `Rule`: Models permissions in `Tool(pattern)` format (e.g. `Bash(pytest*)` or `Read(*.py)`).
  - Uses `fnmatch` to match tool arguments against configured rule patterns.

#### 26. [`src/axon/permissions/paths.py`](file:///Users/gpr/Documents/axon/src/axon/permissions/paths.py)
- **Role:** Workspace confinement and directory jail safety.
- **Implementation:**
  - `WorkspaceJail`: Resolves real paths, follows symlinks, and prevents path traversal attacks (`../`) attempting to access files outside the workspace root without explicit permission.

---

### 4.7 Wire Protocol Providers (`src/axon/providers/`)

#### 27. [`src/axon/providers/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/providers/__init__.py)
- **Role:** Providers package exports.
- **Implementation:** Exports normalized block types, usage dataclasses, and provider classes.

#### 28. [`src/axon/providers/base.py`](file:///Users/gpr/Documents/axon/src/axon/providers/base.py)
- **Role:** Normalized internal data model and Provider Protocol contract.
- **Implementation:**
  - Core dataclasses:
    - `TextBlock(text: str)`
    - `ThinkingBlock(text: str, signature: str | None)`
    - `ToolUseBlock(id: str, name: str, input: dict[str, Any])`
    - `ToolResultBlock(tool_use_id: str, content: str, is_error: bool)`
    - `Usage(input, output, cache_read, cache_write, reasoning)`
    - `AssistantTurn(blocks, stop_reason, usage, native)`
  - Streaming event classes: `TextDelta`, `ThinkingDelta`, `ToolUseStart`, `ToolArgsDelta`, `ToolBatchStart`, `LLMCallStart`.
  - `Provider(Protocol)`: Declares `name`, `stream()`, and `finalize()`.

#### 29. [`src/axon/providers/anthropic.py`](file:///Users/gpr/Documents/axon/src/axon/providers/anthropic.py)
- **Role:** Official Anthropic API client adapter.
- **Implementation:**
  - Interfaces with Anthropic's Python SDK.
  - Inserts 4-block cache control markers (`cache_control: {"type": "ephemeral"}`).
  - Streams thinking tokens, parses reasoning signatures, and accumulates streaming tool JSON deltas into normalized `AssistantTurn` records.

#### 30. [`src/axon/providers/openai_compat.py`](file:///Users/gpr/Documents/axon/src/axon/providers/openai_compat.py)
- **Role:** Raw HTTPX-based streaming SSE client for OpenAI-compatible proxies.
- **Implementation:**
  - Supports AgentRouter, OpenRouter, DeepSeek, Local Ollama, LM Studio, etc.
  - Parses streaming Server-Sent Events (`data: {...}`).
  - Handles parallel `tool_calls` chunks, reconstructs JSON arguments across delta fragments, and tracks `reasoning_content` deltas.

#### 31. [`src/axon/providers/registry.py`](file:///Users/gpr/Documents/axon/src/axon/providers/registry.py)
- **Role:** Dynamic model router and verified pricing registry.
- **Implementation:**
  - `PRICING`: Verified per-million token pricing table for models (DeepSeek, Claude Opus, GPT-5, GLM, etc.).
  - `MODEL_CONTEXT_LIMITS`: Context window table (1M token windows).
  - `provider_for(model, settings)`: Automatically routes requests to `AnthropicProvider` or `OpenAICompatProvider` based on model name and base URL.

#### 32. [`src/axon/providers/catalog.py`](file:///Users/gpr/Documents/axon/src/axon/providers/catalog.py)
- **Role:** Catalog of supported provider presets.
- **Implementation:**
  - Declares preconfigured endpoint definitions, default models, and environment variable requirements for Anthropic, OpenAI, AgentRouter, OpenRouter, DeepSeek, Gemini, and Ollama.

#### 33. [`src/axon/providers/verifier.py`](file:///Users/gpr/Documents/axon/src/axon/providers/verifier.py)
- **Role:** Provider connectivity and latency verification utility.
- **Implementation:**
  - `verify_provider_connectivity()`: Dispatches lightweight probe requests to measure live API endpoint roundtrip latency and test API key validity.

---

### 4.8 Durability, Ledger & Sessions (`src/axon/session/`)

#### 34. [`src/axon/session/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/session/__init__.py)
- **Role:** Session package exports.
- **Implementation:** Exports `SessionStore`, `Ledger`, `CheckpointManager`.

#### 35. [`src/axon/session/store.py`](file:///Users/gpr/Documents/axon/src/axon/session/store.py)
- **Role:** Append-only JSONL session event store with crash durability.
- **Implementation:**
  - Stores all transcripts in `~/.axon/sessions/<session_id>.jsonl`.
  - `append()`: Appends a JSON line followed by `file.flush()` and `os.fsync()` ensuring zero data loss on power loss.
  - Implements session listing, transcript recovery, branching (`branch_session`), and renaming.

#### 36. [`src/axon/session/ledger.py`](file:///Users/gpr/Documents/axon/src/axon/session/ledger.py)
- **Role:** Precise decimal financial accounting and token tracking.
- **Implementation:**
  - Uses Python `Decimal` arithmetic to track cumulative session costs down to micro-cents.
  - Tracks input tokens, output tokens, reasoning tokens, and prompt cache hit savings.
  - `uncached_counterfactual()`: Calculates what the session would have cost without prompt caching.
  - `savings_pct()`: Computes percentage saved by caching (often 60–85%).

#### 37. [`src/axon/session/checkpoint.py`](file:///Users/gpr/Documents/axon/src/axon/session/checkpoint.py)
- **Role:** Pre-modification snapshot manager enabling atomic `/rewind`.
- **Implementation:**
  - `Snapshot`: Represents a snapshot of files modified during a turn.
  - `capture_before_edit(path)`: Reads file contents before any edit modifies it.
  - `rewind_last()`: Restores files to their exact pre-turn state or deletes newly created files, enabling instant single-command rollbacks.

#### 38. [`src/axon/session/crypto.py`](file:///Users/gpr/Documents/axon/src/axon/session/crypto.py)
- **Role:** Optional AES-GCM session file encryption.
- **Implementation:**
  - Uses cryptography primitives to encrypt session logs at rest when `AXON_SESSION_PASSPHRASE` is set.

#### 39. [`src/axon/session/interactive.py`](file:///Users/gpr/Documents/axon/src/axon/session/interactive.py)
- **Role:** Interactive UI picker for sessions.
- **Implementation:**
  - Provides the full-screen terminal interface for browsing, resuming, branching, and deleting past sessions.

---

### 4.9 Custom Workflows & Skills (`src/axon/skills/`)

#### 40. [`src/axon/skills/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/skills/__init__.py)
- **Role:** Skills package exports.
- **Implementation:** Exports `SkillManager` and `Skill`.

#### 41. [`src/axon/skills/manager.py`](file:///Users/gpr/Documents/axon/src/axon/skills/manager.py)
- **Role:** Discovery, YAML frontmatter parsing, and execution of custom skills.
- **Implementation:**
  - Discovers skills from `~/.axon/skills/<name>/SKILL.md` (personal) and `.axon/skills/<name>/SKILL.md` (project).
  - Bundles built-in skills: `debug`, `code-review`, `verify`, `subagent-fanout`, `refactor`, `security-audit`, `test-gen`.
  - Supports dynamic shell backticks (`!`command``) in instructions to inject live context before dispatching to the agent.

#### 42. [`src/axon/skills/importer.py`](file:///Users/gpr/Documents/axon/src/axon/skills/importer.py)
- **Role:** Skill package installer and importer.
- **Implementation:**
  - Clones or downloads skills from Git repositories or URLs into the local `.axon/skills/` directory.

#### 43. [`src/axon/skills/interactive.py`](file:///Users/gpr/Documents/axon/src/axon/skills/interactive.py)
- **Role:** Interactive terminal UI for skills.
- **Implementation:**
  - Interactive manager to list available skills, view descriptions, and scaffold new skill templates via `/skill create`.

---

### 4.10 The 24+ Native Runtime Tools (`src/axon/tools/`)

#### 44. [`src/axon/tools/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/tools/__init__.py)
- **Role:** Tools package exports and default registry factory.
- **Implementation:**
  - `create_default_registry()`: Instantiates and registers all 27 native tool classes into a unified `ToolRegistry`.

#### 45. [`src/axon/tools/base.py`](file:///Users/gpr/Documents/axon/src/axon/tools/base.py)
- **Role:** Tool base class and execution context.
- **Implementation:**
  - Defines `Tool(ABC)` and `ToolContext`. Detailed in [Section 2](#the-tool-contract--context).

#### 46. [`src/axon/tools/registry.py`](file:///Users/gpr/Documents/axon/src/axon/tools/registry.py)
- **Role:** Tool catalog, dispatch, and schema translation.
- **Implementation:**
  - `ToolRegistry`: Manages `name ➔ Tool` dictionary.
  - `schemas(provider_style)`: Exports JSON schemas formatted for Anthropic (`input_schema`) or OpenAI (`type: "function"`).

#### 47. [`src/axon/tools/fs_read.py`](file:///Users/gpr/Documents/axon/src/axon/tools/fs_read.py)
- **Role:** `ReadTool`: Surgical file content reader.
- **Implementation:**
  - Reads text files with line numbering, offset, and limit support.
  - Records `(mtime, sha256)` in `FileState` to establish the staleness baseline for subsequent edits.

#### 48. [`src/axon/tools/fs_write.py`](file:///Users/gpr/Documents/axon/src/axon/tools/fs_write.py)
- **Role:** `WriteTool`, `EditTool`, `MultiEditTool`: Core file creation and modification tools.
- **Implementation:**
  - Captures pre-edit snapshot via `CheckpointManager`.
  - Verifies read-before-write staleness via `FileState`.
  - `EditTool`: Performs exact string replacement.
  - `MultiEditTool`: Performs multiple atomic search-and-replace edits in a single file.

#### 49. [`src/axon/tools/patch.py`](file:///Users/gpr/Documents/axon/src/axon/tools/patch.py)
- **Role:** `PatchTool`: Unified diff applicator.
- **Implementation:**
  - Parses standard unified diffs (`--- a/... +++ b/...`) and applies hunk modifications to target files with fuzzy line matching.

#### 50. [`src/axon/tools/diff_tool.py`](file:///Users/gpr/Documents/axon/src/axon/tools/diff_tool.py)
- **Role:** `DiffTool`: In-memory and on-disk diff generator.
- **Implementation:**
  - Compares working files against git index or compares two arbitrary text blocks, generating standard unified diffs.

#### 51. [`src/axon/tools/shell.py`](file:///Users/gpr/Documents/axon/src/axon/tools/shell.py)
- **Role:** `BashTool`: Controlled shell execution environment.
- **Implementation:**
  - Executes shell commands with timeout enforcement (`bash_timeout_s`, default: 180s).
  - Captures stdout/stderr, strips terminal ANSI escapes, truncates oversized output, and reports exact exit codes.

#### 52. [`src/axon/tools/search.py`](file:///Users/gpr/Documents/axon/src/axon/tools/search.py)
- **Role:** `GlobTool`, `GrepTool`, `LsTool`: High-speed navigation and search.
- **Implementation:**
  - `GlobTool`: Fast file matching using path patterns.
  - `GrepTool`: Regex content search across files using Ripgrep if available, falling back to Python regex.
  - `LsTool`: Directory listing with file sizes and type indicators.

#### 53. [`src/axon/tools/code_symbols.py`](file:///Users/gpr/Documents/axon/src/axon/tools/code_symbols.py)
- **Role:** `CodeSymbolsTool`: AST-aware code symbol extractor.
- **Implementation:**
  - Parses Python files using the `ast` module to extract classes, methods, functions, docstrings, and type signatures without reading whole files into prompt context.

#### 54. [`src/axon/tools/code_graph.py`](file:///Users/gpr/Documents/axon/src/axon/tools/code_graph.py)
- **Role:** `GoToDefinitionTool`, `FindReferencesTool`: Code navigation tools.
- **Implementation:**
  - Performs static code graph traversal across the workspace to locate symbol definitions and all call references.

#### 55. [`src/axon/tools/semantic_search.py`](file:///Users/gpr/Documents/axon/src/axon/tools/semantic_search.py)
- **Role:** `SemanticSearchTool`: Natural language codebase search.
- **Implementation:**
  - Searches codebase concepts using local TF-IDF / BM25 or embedding representations.

#### 56. [`src/axon/tools/file_tree.py`](file:///Users/gpr/Documents/axon/src/axon/tools/file_tree.py)
- **Role:** `FileTreeTool`: Visual directory tree renderer.
- **Implementation:**
  - Generates compact visual tree structures of directories, automatically respecting `.gitignore` patterns.

#### 57. [`src/axon/tools/todo.py`](file:///Users/gpr/Documents/axon/src/axon/tools/todo.py)
- **Role:** `TodoWriteTool`: Task and plan checklist manager.
- **Implementation:**
  - Updates the agent's `TodoState`. Renders active checklists in the terminal and tracks milestone completion.

#### 58. [`src/axon/tools/task.py`](file:///Users/gpr/Documents/axon/src/axon/tools/task.py)
- **Role:** `TaskTool` & `ExitPlanModeTool`: Subagent dispatch and plan approval.
- **Implementation:**
  - `TaskTool`: Spawns a concurrent subagent worker via `SubagentManager`.
  - `ExitPlanModeTool`: Submits the generated plan for user review to exit Plan Mode.

#### 59. [`src/axon/tools/doctor.py`](file:///Users/gpr/Documents/axon/src/axon/tools/doctor.py)
- **Role:** `DoctorTool`: System health and diagnostic auditor.
- **Implementation:**
  - Audits Python version, Git configuration, Ripgrep availability, API key credentials, and permissions.

#### 60. [`src/axon/tools/git.py`](file:///Users/gpr/Documents/axon/src/axon/tools/git.py)
- **Role:** `GitTool`: Git version control operations.
- **Implementation:**
  - Executes git status, diff, log, commit, and branch checks safely within the workspace.

#### 61. [`src/axon/tools/web.py`](file:///Users/gpr/Documents/axon/src/axon/tools/web.py)
- **Role:** `WebFetchTool`: Web content retrieval.
- **Implementation:**
  - Fetches URLs over HTTP, strips HTML tags, and converts pages to clean readable Markdown.

#### 62. [`src/axon/tools/web_search.py`](file:///Users/gpr/Documents/axon/src/axon/tools/web_search.py)
- **Role:** `WebSearchTool`: Live technical web search.
- **Implementation:**
  - Queries technical documentation and error solutions via search API providers.

#### 63. [`src/axon/tools/deep_research.py`](file:///Users/gpr/Documents/axon/src/axon/tools/deep_research.py)
- **Role:** `DeepResearchTool`: Autonomous multi-turn research engine.
- **Implementation:**
  - Decomposes a research question, performs multiple search queries, synthesizes findings, and saves a markdown brief to `~/.axon/research/`.

#### 64. [`src/axon/tools/table_search.py`](file:///Users/gpr/Documents/axon/src/axon/tools/table_search.py)
- **Role:** `TableSearchTool`: Tabular data search.
- **Implementation:**
  - Searches CSV, TSV, and Markdown tables using SQL-like filtering.

#### 65. [`src/axon/tools/http_tool.py`](file:///Users/gpr/Documents/axon/src/axon/tools/http_tool.py)
- **Role:** `HttpTool`: Arbitrary REST API client.
- **Implementation:**
  - Sends GET, POST, PUT, DELETE requests with custom headers and JSON bodies.

#### 66. [`src/axon/tools/process_tool.py`](file:///Users/gpr/Documents/axon/src/axon/tools/process_tool.py)
- **Role:** `ProcessTool`: Background process manager.
- **Implementation:**
  - Starts, monitors, and terminates long-running background processes (e.g. dev servers).

#### 67. [`src/axon/tools/env_tool.py`](file:///Users/gpr/Documents/axon/src/axon/tools/env_tool.py)
- **Role:** `EnvTool`: Environment variable inspector.
- **Implementation:**
  - Inspects active environment variables, masking sensitive secrets and keys.

#### 68. [`src/axon/tools/ui_diff.py`](file:///Users/gpr/Documents/axon/src/axon/tools/ui_diff.py)
- **Role:** `UiPreviewTool`: UI diff preview generator.
- **Implementation:**
  - Generates HTML/CSS previews of modified user interface components.

#### 69. [`src/axon/tools/notebook.py`](file:///Users/gpr/Documents/axon/src/axon/tools/notebook.py)
- **Role:** `NotebookEditTool`: Jupyter notebook editor.
- **Implementation:**
  - Reads, parses, and edits individual code/markdown cells in `.ipynb` files without corrupting JSON metadata.

---

### 4.11 Terminal UI & Presentation Engine (`src/axon/ui/`)

#### 70. [`src/axon/ui/__init__.py`](file:///Users/gpr/Documents/axon/src/axon/ui/__init__.py)
- **Role:** UI package exports.
- **Implementation:** Exports `Renderer`, `ask_approval`, `read_input`, and theme utilities.

#### 71. [`src/axon/ui/theme.py`](file:///Users/gpr/Documents/axon/src/axon/ui/theme.py)
- **Role:** ANSI color palette, typography formatting, and terminal dimension helpers.
- **Implementation:**
  - Defines 24-bit TrueColor and 256-color ANSI codes: `MINT`, `CYAN`, `PURPLE`, `GOLD`, `ROSE`, `SLATE`, `DARK_SLATE`, `BOLD`, `DIM`, `RST`.
  - Provides `term_width()` and `term_height()`.

#### 72. [`src/axon/ui/render.py`](file:///Users/gpr/Documents/axon/src/axon/ui/render.py)
- **Role:** Primary presentation renderer (over 73 KB of formatting logic).
- **Implementation:**
  - `Renderer`: Handles live streaming text deltas, thinking blocks with timing indicators, tool invocation cards, diff boxes, and turn footers.
  - Implements formatted boxes for read files, patches, error alerts, and subagent progress.

#### 73. [`src/axon/ui/input.py`](file:///Users/gpr/Documents/axon/src/axon/ui/input.py)
- **Role:** Advanced terminal input reader with paste-burst detection.
- **Implementation:**
  - `read_input()`: Intercepts raw terminal keystrokes.
  - Detects rapid paste bursts (>100 characters in milliseconds) to prevent terminal freezing and avoid treating pasted newlines as instant execution commands.
  - Supports multiline input, history navigation, and keyboard shortcuts (`Tab`, `Ctrl+P`).

#### 74. [`src/axon/ui/approve.py`](file:///Users/gpr/Documents/axon/src/axon/ui/approve.py)
- **Role:** Interactive permission confirmation dialog.
- **Implementation:**
  - `ask_approval(tool, args, decision)`: Renders a dedicated approval card displaying the command or file edit diff, offering choices: `[y] Allow once`, `[a] Always allow`, `[n] Deny`.

#### 75. [`src/axon/ui/picker.py`](file:///Users/gpr/Documents/axon/src/axon/ui/picker.py)
- **Role:** Generic arrow-key terminal selector.
- **Implementation:**
  - `pick()`: Interactive scrolling menu allowing users to select items using Up/Down arrow keys and Enter.

#### 76. [`src/axon/ui/fuzzy_picker.py`](file:///Users/gpr/Documents/axon/src/axon/ui/fuzzy_picker.py)
- **Role:** File fuzzy search picker (`Ctrl+P` / `@`).
- **Implementation:**
  - `run_fuzzy_file_finder()`: Live fuzzy search over workspace files with instant preview, allowing one-key insertion into prompt input.

#### 77. [`src/axon/ui/provider_picker.py`](file:///Users/gpr/Documents/axon/src/axon/ui/provider_picker.py)
- **Role:** Interactive provider and API key setup wizard.
- **Implementation:**
  - `run_provider_picker()`: Walkthrough wizard to connect providers, enter credentials, validate connectivity, and persist to `~/.axon/.env`.

#### 78. [`src/axon/ui/switcher.py`](file:///Users/gpr/Documents/axon/src/axon/ui/switcher.py)
- **Role:** Full-screen session switcher dashboard.
- **Implementation:**
  - Triggered by `←` (Left Arrow). Displays past sessions, timestamps, first prompts, token costs, and tags in an interactive terminal table.

#### 79. [`src/axon/ui/statusbar.py`](file:///Users/gpr/Documents/axon/src/axon/ui/statusbar.py)
- **Role:** Real-time bottom status bar and token capacity gauge.
- **Implementation:**
  - Renders active model, effort tier, permission mode, and context window gauge bar: `[████████░░░░░░] 58% (1.16M / 2.0M)`.

#### 80. [`src/axon/ui/subagent_monitor.py`](file:///Users/gpr/Documents/axon/src/axon/ui/subagent_monitor.py)
- **Role:** Live monitoring dashboard for concurrent subagents.
- **Implementation:**
  - Renders live progress bars, active steps, elapsed seconds, and recent log outputs for all running subagents.

#### 81. [`src/axon/ui/scroll_region.py`](file:///Users/gpr/Documents/axon/src/axon/ui/scroll_region.py)
- **Role:** Terminal scroll region and ANSI viewport controller.
- **Implementation:**
  - Uses DECSTBM ANSI sequences to pin the status bar to the terminal bottom while allowing conversation text to scroll cleanly above it.

#### 82. [`src/axon/ui/markdown.py`](file:///Users/gpr/Documents/axon/src/axon/ui/markdown.py)
- **Role:** Terminal markdown parser and syntax highlighter.
- **Implementation:**
  - Formats markdown headers, lists, blockquotes, tables, and fenced code blocks with language-specific syntax highlighting in pure ANSI escapes.

#### 83. [`src/axon/ui/notify.py`](file:///Users/gpr/Documents/axon/src/axon/ui/notify.py)
- **Role:** OS desktop notification emitter.
- **Implementation:**
  - Sends native OS desktop notifications (macOS `osascript`, Linux `notify-send`, Windows PowerShell balloon) when long-running turns complete.

#### 84. [`src/axon/ui/clipboard.py`](file:///Users/gpr/Documents/axon/src/axon/ui/clipboard.py)
- **Role:** System clipboard integration.
- **Implementation:**
  - Copies code blocks or full turn outputs directly to the system clipboard (`pbcopy`, `xclip`, `clip.exe`).

#### 85. [`src/axon/ui/in_flight.py`](file:///Users/gpr/Documents/axon/src/axon/ui/in_flight.py)
- **Role:** Animated spinner and in-flight activity indicator.
- **Implementation:**
  - Renders smooth Braille spinners (`⠋`, `⠙`, `⠹`, `⠸`) and elapsed timers while awaiting provider response streaming.

#### 86. [`src/axon/ui/live_turn.py`](file:///Users/gpr/Documents/axon/src/axon/ui/live_turn.py)
- **Role:** Interactive turn coordinator.
- **Implementation:**
  - Connects the REPL input, `Renderer`, and `Agent.run_turn()` event callbacks for unified terminal presentation.

---

## 5. Summary Architecture Matrix

| Package | Files | Core Responsibilities | Invariants / Protocols Enforced |
|---|---|---|---|
| **Root (`axon/`)** | 5 | CLI parsing, settings loading, entry points, exceptions | Hierarchical config (`.env` ➔ `~/.axon/config.toml`), zero-framework design |
| **`agent/`** | 9 | ReAct engine, state machine, compaction, prompts, subagents | Laws 1, 2, 3, 4, 5; 3-rung Compaction Ladder; 4 Loop Safety Brakes |
| **`commands/`** | 2 | 40+ slash commands, shortcuts, skill dispatches | REPL command interception, mode switching, instant session rewind |
| **`hooks/`** | 2 | Pre- and post-tool lifecycle shell scripts | Exit code 2 veto protocol, JSON stdin IPC |
| **`mcp/`** | 4 | Model Context Protocol stdio client bridge | Stdio JSON-RPC 2.0 handshake, dynamic tool synthesis |
| **`permissions/`**| 4 | 6-law permission matrix, workspace jail | Deny-wins hierarchy, symlink traversal prevention, invariant hard blocks |
| **`providers/`** | 7 | Wire protocols (Anthropic, OpenAI-compatible SSE) | Provider Protocol, streaming deltas, prompt caching markers |
| **`session/`** | 6 | Durability, ledger, rollback checkpoints, crypto | `os.fsync` crash durability, Decimal ledger, SHA256 pre-edit snapshots |
| **`skills/`** | 4 | Reusable markdown workflows, dynamic backtick contexts | YAML frontmatter parsing, project/global hierarchy |
| **`tools/`** | 27 | 24+ native tools (File I/O, Shell, AST, Web, Git) | Staleness detection `(mtime, sha256)`, Law 4 (`is_error=True`) |
| **`ui/`** | 17 | Terminal rendering, paste bursts, markdown, pickers | ANSI TrueColor, DECSTBM pinned scroll regions, paste burst detection |
| **Total** | **87** | **Complete autonomous terminal-native coding assistant** | **528 Passing Tests · 100% Deterministic & Isolated** |

---

*End of Specification Document.*
