# Architecture

Axon is a single Python package (`src/axon`) with no runtime services. A turn is a ReAct loop:
the model reasons, requests tools, Axon runs them, and the results go back to the model until it
stops. Every step emits a structured event, and the renderer, the trace store, the cost ledger
and the SDK all consume that same event stream.

## Module map

| Package / module | Responsibility |
|---|---|
| `axon/cli.py` | Argument parsing, print mode (`-p`, `--output-format`), the interactive REPL, `!cmd`, `# note`, and the `mcp` / `doctor` / `purge` subcommands |
| `axon/config.py` | Typed `Settings` (pydantic) and the precedence chain across TOML, JSON, `.env`, environment and flags. Also API key resolution and the project base-URL guard |
| `axon/storage.py` | The on-disk layout under `~/.axon` (or `AXON_HOME`): project keys, sessions, traces, checkpoints, usage log, pruning |
| `axon/platform.py` | All OS-specific code: console VT/UTF-8 setup, raw key reading, shell detection (bash, zsh, pwsh, powershell, cmd, Git Bash) and process-tree kill |
| `axon/agent/loop.py` | `Agent`: the turn loop, retries, compaction, budget guard, steering, background turns, hook dispatch and tool batching |
| `axon/agent/prompt.py` · `context.py` | System prompt assembly (identity, environment, memory, skills, output style, goal), context accounting and compaction |
| `axon/agent/memory.py` | The AXON.md / CLAUDE.md hierarchy, rules, `@imports`, auto memory and saved items |
| `axon/agent/subagent.py` | Agent definitions (built-in, plugin, user, project, `--agents`), the `Task` runner and progress tracking |
| `axon/agent/trace.py` | Trace events (`LLMCallEnd`, `ToolTiming`, `PermissionDecisionEvent`, `HookEvent`, `CacheStatus`, …) and `TurnTrace` |
| `axon/agent/worktree.py` | `--worktree` and isolated git worktrees for subagents |
| `axon/providers/` | `anthropic.py` (native Messages API) and `openai_compat.py` (every OpenAI-compatible endpoint). Also `catalog.py` (presets), `registry.py` (model to provider routing), `pricing.py`, `failover.py`, `transport.py` (HTTP, retries) and `verifier.py` (live key checks) |
| `axon/tools/` | Built-in tools (`Read`, `Edit`, `Bash`, `Grep`, `Task`, …), the `ToolRegistry`, and the path helpers shared by file tools |
| `axon/permissions/` | `engine.py` (allow, ask and deny evaluation), `rules.py` (rule syntax and persistence) and `paths.py` (the workspace path jail) |
| `axon/hooks/` | Claude-Code-compatible lifecycle hooks: config merging, matchers, subprocess runner and JSON decisions |
| `axon/mcp/` | MCP client (stdio, HTTP, SSE transports), config discovery, tool and prompt bridge, and the `/mcp` command |
| `axon/skills/` | SKILL.md discovery, import and execution |
| `axon/commands/` | The slash-command registry (`registry.py`) and built-ins grouped by area (`session_cmds.py`, `context_cmds.py`, …), plus custom commands, plugins and output styles |
| `axon/session/` | JSONL transcripts (`store.py`), file checkpoints for `/rewind`, the cost `ledger.py` and optional encryption |
| `axon/features/` | Self-registering feature modules: analytics, repo map, git ops, SDK, server, solver, planner, onboarding, suggestions and scaffolding |
| `axon/ui/` | `render.py` (the timeline renderer), markdown streaming, the input editor (history, vim mode, completion), pickers, status line and `theme.py` (palettes, width measurement, glyph fallbacks) |

Feature modules plug in without touching the core. Each module listed in
`axon.features.FEATURE_MODULES` may export `COMMANDS` (slash commands) and `CLI_SUBCOMMANDS`
(`axon <name>`), and the registry picks them up lazily.

## Data flow of one turn

```text
 user input ──► cli.handle_line ──► "/cmd"  → commands.dispatch_command
                     │               "!cmd" → shell output added to context
                     │               "# x"  → memory.add
                     ▼
               Agent.run_turn
                 1. session cost ceiling check
                 2. hook: UserPromptSubmit           (may block or add context)
                 3. session.append_user  ──────────► sessions/<id>.jsonl
                 ┌─ loop (≤ max_iterations) ───────────────────────────────────────┐
                 │ 4. auto-compact when context ≥ compact_at  (PreCompact hook)     │
                 │ 5. budget guard: projected cost of the next request             │
                 │ 6. LLMCallStart ─► provider.stream(system, tools, messages)      │
                 │      retries / fallback models on 429·5xx·overload (RetryEvent)  │
                 │ 7. LLMCallEnd: usage, cost (pricing.py) ─► ledger + usage.jsonl  │
                 │    CacheStatus: hit rate, miss cause                             │
                 │ 8. no tool calls?  hook: Stop  (may force another round) ─► done │
                 │ 9. for each tool call (read-only ones in parallel):              │
                 │      hook: PreToolUse ─► PermissionEngine.check ─► ask user?     │
                 │      tool.run (edits snapshot the file first) ─► cap output      │
                 │      hook: PostToolUse ─► ToolTiming, ToolExecutionResult        │
                 │10. all results in ONE message; steering messages appended       │
                 └──────────────────────────────────────────────────────────────────┘
                 11. TurnTrace ─► traces/<id>.jsonl   (replayed by /inspect)
                 12. auto title (StopFailure hook on error); the CLI then prints the
                     footer, next-step suggestions and an optional desktop notification
```

Every arrow labelled with an event (`LLMCallStart`, `ToolTiming`, …) is emitted through
`Agent.on_event`. The terminal `Renderer`, the SDK's `query()` generator and `axon serve`'s SSE
stream are interchangeable consumers of these events.

### Loop invariants

The loop enforces five invariants, which are documented in `agent/loop.py` and covered by
`tests/test_invariants.py`:

1. **Pairing.** Every `tool_use` gets exactly one `tool_result`, including on error, interrupt or truncation.
2. **Batching.** All results of one assistant turn go back in a single message.
3. **Verbatim.** Provider-native assistant content is replayed unchanged, so prompt caching keeps working.
4. **Errors are data.** Tool failures become error results, never exceptions.
5. **Interrupts.** Ctrl+C always leaves the conversation valid for the next request.

### Permission evaluation

`permissions/engine.py` decides `allow`, `ask` or `deny`. The first match wins:

1. Hard invariants, such as catastrophic shell commands or credential and system paths: **deny**
2. Deny rules: **deny**
3. Plan mode and a mutating tool: **deny**
4. Ask rules, which beat both allow rules and bypass mode: **ask**
5. Allow rules (persistent and session): **allow**
6. A tool whose default is `deny`: **deny**
7. Bypass mode: **allow**
8. A path outside the workspace and the added directories: **ask**
9. acceptEdits mode and a file edit: **allow**
10. Otherwise, the tool's default permission

### Providers

`providers/registry.py` resolves a model name to a preset in `catalog.py`. This covers explicit
`provider/model` prefixes, known model families and the configured `base_url`. It then builds
either the Anthropic client or the OpenAI-compatible client. Both normalise streaming into the
same events (`TextDelta`, `ThinkingDelta`, `ToolUseStart`, `TurnComplete`, …), so the loop never
branches on the provider. `failover.py` wraps a chain of models for `fallback_model`.

### Persistence

Everything is append-only JSONL under `~/.axon/projects/<project-key>/`. Transcripts hold user
input, assistant text and thinking, tool calls and results, usage, cost, timings and permission
decisions. That is enough for `/resume`, `/rewind`, `/export`, `/stats` and the HTML report to
work from real data. See the storage tree in the [README](../README.md#storage-layout).
