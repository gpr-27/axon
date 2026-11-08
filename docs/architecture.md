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
| `axon/storage.py` | The on-disk layout under `~/.axon` (or `AXON_HOME`): project keys, the per-session folder helpers (`session_dir`, `session_subdir`, `trace_path`, ...), usage log, pruning |
| `axon/session/relayout.py` | One-time, idempotent move of the older flat layout (`sessions/<id>.jsonl`, `traces/`, `outputs/<id>/`, ...) into per-session folders |
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
| `axon/mcp/` | MCP client (stdio, HTTP, SSE, WebSocket transports, OAuth, headersHelper), config discovery and merging, allow/deny and managed policy, project-server approvals, output limits, tool and prompt bridge, `axon mcp serve`, and the `/mcp` command |
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
                 3. session.append_user  ──────────► sessions/<id>/session.jsonl
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
                 11. TurnTrace ─► sessions/<id>/trace.jsonl   (replayed by /inspect)
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

### Shell guard and the sandbox

Two separate controls cover the `Bash` tool.

**The shell guard** (`permissions/engine.py`) is part of permission evaluation and always on. It reads the
command text only. It denies commands that are irrecoverably destructive (recursive removal of a root or home
directory, `mkfs`, `dd` onto a raw device, a fork bomb, a decoded payload piped into a shell) and commands
whose arguments, redirections, globs, `cd` targets, shell variables, nested shells or recursive reads reach a
credential store (`~/.ssh`, `~/.aws`, `~/.axon/.env` and similar). Because it parses text, it cannot see what a
program does at run time: `python3 -c "open(...)"` or a script that reads a credential file is not caught.

**The sandbox** (`/sandbox on`, setting `sandbox.enabled`; `permissions/sandbox.py`) is the stronger control:
the operating system enforces it for every Bash command and every process the command starts. Reads work
anywhere except credential stores; writes are allowed only in the workspace, the added directories and the
temp directory; the network is blocked unless `sandbox.allowNetwork` is true. It uses `sandbox-exec` on macOS
and `bwrap` (bubblewrap) on Linux; other systems have no backend. If it is on and no backend works, Bash
commands are refused instead of running unsandboxed. It does not cover the `Read`, `Write` and `Edit` tools or
MCP servers (those run inside Axon; the permission rules and protected-path checks apply to them), hooks, or
web tools. `/sandbox status` prints exactly what is and is not enforced on this machine.

### Large input and attachments

`agent/large_input.py` keeps one huge message from filling the window. A message above the limit
(`large_input_tokens`, see [configuration](configuration.md#large-input-large_input_tokens)) is saved to the
session's `pastes/` folder and the model gets its start and end plus the path. In the prompt, a long paste
collapses into a `[Pasted text #N]` chip; when the chips and your text together are too large, each oversized
chip is shrunk separately so the text typed between them is sent in full.

`agent/attachments.py` handles files dropped into the prompt. The file is copied to the session's
`attachments/` folder at the moment of the drop (a drop often names a temporary file), the prompt shows a
`[File #N: name]` chip, and on submit the chip becomes a line giving the saved path, which the model reads
with its file tools. Images go through `agent/images.py` instead. Credential and system files are refused,
and a file over 2 GB is not copied: it is referenced where it is. `attachments/index.jsonl` lists everything
kept, and `/files` (alias `/attachments`) prints it with the saved paths. A drop is recognised from a
bracketed paste, or, in terminals without one, from a burst of keys that is nothing but existing file paths
outside the workspace.

### Session lease

A process that opens a session writes `writer.lease` (pid, host, owner, start time) in the session folder.
A lease whose process has ended counts as free, so a crash never locks a session; a lease from another
machine (a shared folder) cannot be checked and is assumed live. When you resume a session that another live
process holds, an interactive terminal asks whether to fork into a new session (recommended), continue
anyway, or quit; `-p` and scripts print a warning and continue. A read-only folder never blocks a session.
The lease keeps two writers from interleaving one history; it is not a security control.

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

Every chat session owns exactly one folder, `projects/<project-key>/sessions/<session-id>/`, and
everything that belongs to the session lives only there: `session.jsonl` (transcript),
`subagents/<n>.jsonl` (subagent transcripts, with `<n>.trace.jsonl` traces), `screen.log`,
`trace.jsonl`, `outputs/` (full tool outputs and background shell logs), `checkpoints/` (`/rewind`
data), `images/`, `pastes/`, `todos.json`, `exports/` and `debug.log`. All code reaches these through
the helpers in `axon/storage.py` (`session_dir`, `session_subdir`, `session_file`, `transcript_path`);
artifacts created without an explicit session id (pasted images and large pastes) go to the session
the `SessionStore` opened last, and to `sessions/_unattached/` when none is active. Deleting a session
moves its folder to `trash/`; pruning removes whole folders.

Older flat layouts stay readable. `SessionStore.open()` and the startup step in `session/layout.py`
call `session/relayout.py`, which moves (never copies) a session's scattered files into its folder.
It is idempotent, never overwrites (a colliding file with different content is parked in
`<session>/legacy/`, an identical one is dropped) and leaves images and pastes of the old layout in
place because they carry no session id.
