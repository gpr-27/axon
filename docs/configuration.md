# Configuration

Axon reads settings from TOML files, Claude-Code-style JSON files, `.env` files, environment
variables and command-line flags. Run `axon setup` to write a working `~/.axon/config.toml`, use
`/config` inside a session to view or change settings, and use `/status` to see what is in effect.

## Files and precedence

Layers are applied from lowest to highest priority, and a later layer wins for scalar values:

| # | Source | Scope |
|---|---|---|
| 1 | Built-in defaults (table below) | |
| 2 | `~/.axon/config.toml` | user |
| 3 | `<project>/.axon/config.toml` | project |
| 4 | `~/.axon/settings.json` | user |
| 5 | `<project>/.claude/settings.json`, then `<project>/.axon/settings.json` | project, shared |
| 6 | `<project>/.claude/settings.local.json`, then `<project>/.axon/settings.local.json` | project, personal |
| 7 | `--settings <file or inline JSON>` | invocation |
| 8 | `.env` files: `<project>/.env`, then `~/.axon/.env`. Only `AXON_*` and provider key variables are read, and only if not already set | |
| 9 | Environment variables `AXON_<SETTING>` | process |
| 10 | Command-line flags | invocation |

Merging rules:

- **Permission rules** (`allow`, `ask`, `deny`), `memory_excludes` and additional directories are
  **merged** across all layers. Deny always beats allow.
- **Hooks** from every file are merged. See [extending.md](extending.md#hooks).
- `AXON_HOME` moves the whole `~/.axon` directory, for example to keep test runs isolated.
- `.env` files in parent directories are never read.

### Security rules

- A project-level file (`<project>/.axon/config.toml`, a project `settings*.json` or `<project>/.env`)
  **cannot set `api_key` or `base_url`** if that would send your user-level key to another host.
  Axon ignores the value and prints a warning. Local endpoints (`localhost`, `127.0.0.1`) are allowed.
- The `env` block of `settings.json` cannot override `AXON_API_KEY` or `AXON_BASE_URL`.
- New "don't ask again" approvals are written to `<project>/.axon/settings.local.json`, which is
  personal. Keep it out of version control. Axon's own `.gitignore` ignores `.axon/`.

### API keys

Axon resolves the key in this order:

1. `api_key` from settings, or `AXON_API_KEY`
2. The environment variables of the configured provider preset, such as `ANTHROPIC_API_KEY`,
   checked in the process environment and then in `~/.axon/.env`
3. The first of `AXON_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `DEEPSEEK_API_KEY`, `GEMINI_API_KEY` and `TOGETHER_API_KEY` that is set
4. In an interactive terminal, a hidden prompt. The key is verified live, then appended to
   `~/.axon/.env` (mode `0600` on POSIX).

Local endpoints need no key. `/keys` shows and sets keys per provider, and each key is verified
before it is saved.

## Example `config.toml`

```toml
provider = "anthropic"
model = "claude-sonnet-4-5"
small_model = "claude-haiku-4-5"
mode = "default"              # default | acceptEdits | plan | bypass
trace_level = "normal"        # compact | normal | detailed | debug
fallback_model = ["gpt-5", "deepseek-chat"]
budget_session_usd = 5.0
budget_action = "warn"        # warn | stop | downgrade

[permissions]
allow = ["Read", "Grep", "Glob", "Bash(git status*)", "Bash(pytest *)"]
ask   = ["Bash(git push*)"]
deny  = ["Read(~/.ssh/**)", "Bash(rm -rf *)"]
```

A fuller, commented version is in [examples/config.toml](../examples/config.toml).

## Example `settings.json` (Claude Code format)

```json
{
  "model": "claude-sonnet-4-5",
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": ["Bash(npm test*)", "Edit(src/**)"],
    "deny": ["WebFetch(domain:internal.example.com)"],
    "additionalDirectories": ["../shared-lib"]
  },
  "env": { "NODE_ENV": "development" },
  "budget": { "session_usd": 5, "action": "warn" },
  "outputStyle": "explanatory"
}
```

`permissions.defaultMode` accepts `default`, `acceptEdits`, `plan` and `bypassPermissions`
(`bypass` and `manual` are also understood). Keys that start with `_` are ignored, so you can use
them as comments.

## Permission rule syntax

| Rule | Matches |
|---|---|
| `Read` | Every call of a tool |
| `Bash(git status)` | That exact command |
| `Bash(git *)` / `Bash(npm:*)` | Commands with a glob or prefix |
| `Edit(src/**)` | File tools on paths that match a glob (relative to the workspace) |
| `Read(~/.ssh/**)` | Absolute or home-relative paths |
| `WebFetch(domain:example.com)` | Fetches to a domain |
| `mcp__github` / `mcp__github__create_issue` | Every tool of an MCP server, or one of them |

Use `/permissions` for an interactive panel with Allow, Ask, Deny and Directories tabs. The
`--allowedTools` and `--disallowedTools` flags add rules for one invocation.

## All settings

Each setting can be set in `config.toml` (by its name), in `settings.json` (by its camelCase key
where one exists), or as the environment variable `AXON_<NAME>`.

<!-- BEGIN GENERATED: settings -->
| Setting (`config.toml`) | Env var | `settings.json` key | Default |
|---|---|---|---|
| `api_key` | `AXON_API_KEY` | – | `none` |
| `base_url` | `AXON_BASE_URL` | `baseUrl` | `"https://agentrouter.org"` |
| `provider` | `AXON_PROVIDER` | `provider` | `none` |
| `model` | `AXON_MODEL` | `model` | `"deepseek-v4-flash"` |
| `small_model` | `AXON_SMALL_MODEL` | `smallModel` | `none` |
| `effort` | `AXON_EFFORT` | `effortLevel` | `"quantum"` |
| `thinking` | `AXON_THINKING` | `alwaysThinkingEnabled` | `true` |
| `mode` | `AXON_MODE` | `permissions.defaultMode` | `"bypass"` |
| `workspace` | `AXON_WORKSPACE` | – | `current directory` |
| `max_tokens` | `AXON_MAX_TOKENS` | – | `128000` |
| `max_iterations` | `AXON_MAX_ITERATIONS` | `maxTurns` | `50` |
| `max_retries` | `AXON_MAX_RETRIES` | `maxRetries` | `3` |
| `turn_token_budget` | `AXON_TURN_TOKEN_BUDGET` | – | `2000000` |
| `max_history_turns` | `AXON_MAX_HISTORY_TURNS` | – | `0` |
| `session_cost_ceiling` | `AXON_SESSION_COST_CEILING` | – | `50.00` |
| `compact_at` | `AXON_COMPACT_AT` | `compactAt` | `0.85` |
| `parallel_tools` | `AXON_PARALLEL_TOOLS` | `parallelTools` | `6` |
| `bash_timeout_s` | `AXON_BASH_TIMEOUT_S` | – | `180` |
| `tool_output_cap` | `AXON_TOOL_OUTPUT_CAP` | – | `150000` |
| `permissions` | `AXON_PERMISSIONS` | `permissions` | `empty` |
| `hooks` | `AXON_HOOKS` | `hooks` | `empty` |
| `append_system_prompt` | `AXON_APPEND_SYSTEM_PROMPT` | `appendSystemPrompt` | `none` |
| `dangerously_skip_permissions` | `AXON_DANGEROUSLY_SKIP_PERMISSIONS` | – | `false` |
| `isolate_worktrees` | `AXON_ISOLATE_WORKTREES` | `isolateWorktrees` | `false` |
| `verbose` | `AXON_VERBOSE` | `verbose` | `false` |
| `trace_level` | `AXON_TRACE_LEVEL` | `traceLevel` | `"normal"` |
| `budget_turn_usd` | `AXON_BUDGET_TURN_USD` | `budget.turn_usd` | `none` |
| `budget_session_usd` | `AXON_BUDGET_SESSION_USD` | `budget.session_usd` | `none` |
| `budget_action` | `AXON_BUDGET_ACTION` | `budget.action` | `"stop"` |
| `instruction_files` | `AXON_INSTRUCTION_FILES` | `instructionFiles` | `"axon-md-or-agents-md"` |
| `auto_memory` | `AXON_AUTO_MEMORY` | `autoMemoryEnabled` | `true` |
| `memory_excludes` | `AXON_MEMORY_EXCLUDES` | `axonMdExcludes`, `claudeMdExcludes` | `[]` |
| `fallback_model` | `AXON_FALLBACK_MODEL` | `fallbackModel` | `none` |
| `routing` | `AXON_ROUTING` | `routing` | `none` |
| `additional_dirs` | `AXON_ADDITIONAL_DIRS` | `additionalDirectories` | `[]` |
| `system_prompt_override` | `AXON_SYSTEM_PROMPT_OVERRIDE` | `systemPrompt` | `none` |
| `output_style` | `AXON_OUTPUT_STYLE` | `outputStyle` | `"default"` |
| `status_line` | `AXON_STATUS_LINE` | `statusLine` | `none` |
| `vim_mode` | `AXON_VIM_MODE` | `editorMode`, `vimMode` | `false` |
| `theme` | `AXON_THEME` | `theme` | `"auto"` |
| `cleanup_period_days` | `AXON_CLEANUP_PERIOD_DAYS` | `cleanupPeriodDays` | `30` |
| `verify_commands` | `AXON_VERIFY_COMMANDS` | `verifyCommands` | `[]` |
| `verify_timeout` | `AXON_VERIFY_TIMEOUT` | `verifyTimeout` | `900` |
| `repo_map` | `AXON_REPO_MAP` | `repoMap` | `false` |
| `repo_map_tokens` | `AXON_REPO_MAP_TOKENS` | `repoMapTokens` | `1500` |
| `suggestions` | `AXON_SUGGESTIONS` | `promptSuggestionEnabled` | `true` |
| `auto_title` | `AXON_AUTO_TITLE` | `autoTitle` | `true` |
| `respond_to_bash_commands` | `AXON_RESPOND_TO_BASH_COMMANDS` | `respondToBashCommands` | `false` |
| `disable_all_hooks` | `AXON_DISABLE_ALL_HOOKS` | `disableAllHooks` | `false` |
| `prompt_color` | `AXON_PROMPT_COLOR` | `promptColor` | `none` |
| `goal` | `AXON_GOAL` | – | `none` |
<!-- END GENERATED: settings -->

`permissions` takes `allow`, `deny` and `ask` lists. `hooks` maps event names to hook groups.
`budget` in `settings.json` maps to `budget_turn_usd`, `budget_session_usd` and `budget_action`.

### Other environment variables

| Variable | Effect |
|---|---|
| `AXON_HOME` | Root of all Axon state (default `~/.axon`) |
| `AXON_SHELL` | Shell for tool commands: `bash`, `pwsh`, `powershell`, `cmd` or a path |
| `AXON_THEME` | `auto`, `dark`, `light`, `high-contrast` or `mono` |
| `AXON_TRACE_LEVEL` | Default trace level for the renderer |
| `NO_COLOR` | Disable colour |
| `FORCE_COLOR`, `AXON_FORCE_COLOR` | Force colour on (`truecolor` forces 24-bit) |
| `AXON_NO_HYPERLINKS`, `AXON_HYPERLINKS` | Turn clickable OSC 8 file links off or on |
| `AXON_SERVE_TOKEN` | Default bearer token for `axon serve` |
| Provider keys | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `CEREBRAS_API_KEY`, `AZURE_OPENAI_API_KEY`, `AGENTROUTER_API_KEY` |

## Keyboard shortcuts

`/keybindings` lists the shortcuts in effect. To remap them, edit `~/.axon/keybindings.json`:

```json
{ "bindings": { "chat:externalEditor": ["ctrl+e"], "app:todos": null } }
```
