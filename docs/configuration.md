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
- A project-level file cannot set `vimInsertModeRemaps`: a repository cannot remap your keystrokes.
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

Local endpoints need no key.

`/keys` (or `axon keys` outside the REPL) is the key manager. On a terminal it opens a menu that
shows each provider's key status: masked value (`sk-a…9f3c`), where it comes from, and whether it
was verified. From the menu you can add, replace, test or remove a key, open a `.env` in `$EDITOR`,
and choose where new keys are saved. For scripts:

```
/keys set <provider|VAR> [key] [--project] [--no-verify]   hidden prompt when the key is omitted; verified live
/keys test [provider|all]                                  latency and model count, or the exact error
/keys remove <provider|VAR> [--project|--user] [-y]
/keys where                                                files in order of precedence, git-ignore status
/keys edit [--project]                                     open the .env in $EDITOR
```

Keys are saved to `~/.axon/.env` by default. With `--project` they go to `<project>/.env` instead,
and Axon adds `.env` to `.gitignore` (or warns you if the file is already tracked by git). A
variable set in your shell always wins, then the project `.env`, then `~/.axon/.env`. Axon never
prints a full key. Keys typed after `/keys set` are masked in the activity journal.

### Endpoints (base URL redirection)

`/endpoint` (or `axon endpoint`) changes where a provider's requests go, for example to a proxy or
gateway, and adds custom OpenAI- or Anthropic-compatible endpoints. These are saved only in
`~/.axon/config.toml`, never in project files:

```toml
[endpoints.openai]                 # send OpenAI traffic through a proxy
base_url = "https://proxy.example.com/v1"

[endpoints.my-gateway]             # a new provider in /provider, /keys and /model
base_url = "https://llm.example.com/v1"
env_var  = "MY_GATEWAY_API_KEY"    # its key; leave it out for no key
model    = "qwen3-coder"           # optional default model
native_tools = false               # optional: see "Gateways that drop tools" below
```

```
/endpoint set <provider> <url>     /endpoint add <name> <url> [--key-env VAR] [--model M] [--use]
/endpoint reset <provider|name>    /endpoint test [provider|url]     /endpoint use <name>
/endpoint native-tools <name> true|false
```

#### Gateways that drop tools

Some OpenAI-compatible gateways accept the `tools` field and then ignore it, so every model answers
"I do not have access to any tools" and an agent cannot read a file. `/endpoint test <name>` sends one
tiny probe request (a trivial `echo` tool) and, if no tool call comes back, prints the command to fix it:

```
/endpoint native-tools my-gateway false
```

This saves `native_tools = false` for that endpoint (default `true`; `--native-tools false` also works on
`/endpoint add` and `/endpoint set`). Axon then sends no `tools` field. Instead it adds a short protocol to
the system message that lists each tool with its JSON schema and asks the model to write calls as
`<tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>`. Past calls and results are sent back as
`<tool_call>` and `<tool_result>` text, and Axon turns the model's blocks into normal tool calls (invalid JSON
becomes an error result the model can correct). In this mode the reply is shown once it is complete, not
token by token. It depends on the model following the format, so it is less reliable than native tool
calling; leave `native_tools` on for any endpoint that supports tools. The probe runs only when you ask for
it, never during normal chat.

`/settings` lists every option grouped by purpose: model and provider, permissions, display,
context and cost, data, and shell. On a terminal you can change any of them in place. Without a
terminal, use `/settings <key> <value>`.

## Example `config.toml`

```toml
provider = "anthropic"
model = "claude-sonnet-4-5"
small_model = "claude-haiku-4-5"
mode = "default"              # default | acceptEdits | plan | bypass
trace_level = "compact"       # compact | normal | detailed | debug
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
| `turn_token_budget` | `AXON_TURN_TOKEN_BUDGET` | – | `0` |
| `max_history_turns` | `AXON_MAX_HISTORY_TURNS` | – | `0` |
| `session_cost_ceiling` | `AXON_SESSION_COST_CEILING` | – | `50.00` |
| `compact_at` | `AXON_COMPACT_AT` | `compactAt` | `0.75` |
| `compact_keep_turns` | `AXON_COMPACT_KEEP_TURNS` | `compactKeepTurns` | `2` |
| `compact_background` | `AXON_COMPACT_BACKGROUND` | `compactBackground` | `true` |
| `compact_background_at` | `AXON_COMPACT_BACKGROUND_AT` | `compactBackgroundAt` | `0.8` |
| `compact_server_side` | `AXON_COMPACT_SERVER_SIDE` | `compactServerSide` | `false` |
| `large_input_tokens` | `AXON_LARGE_INPUT_TOKENS` | `largeInputTokens` | `0` |
| `parallel_tools` | `AXON_PARALLEL_TOOLS` | `parallelTools` | `6` |
| `bash_timeout_s` | `AXON_BASH_TIMEOUT_S` | – | `180` |
| `sandbox_enabled` | `AXON_SANDBOX_ENABLED` | – | `false` |
| `sandbox_allow_network` | `AXON_SANDBOX_ALLOW_NETWORK` | – | `false` |
| `lsp_diagnostics_after_edit` | `AXON_LSP_DIAGNOSTICS_AFTER_EDIT` | – | `true` |
| `tool_output_cap` | `AXON_TOOL_OUTPUT_CAP` | – | `150000` |
| `permissions` | `AXON_PERMISSIONS` | `permissions` | `empty` |
| `settings_file` | `AXON_SETTINGS_FILE` | – | `none` |
| `hooks` | `AXON_HOOKS` | `hooks` | `empty` |
| `append_system_prompt` | `AXON_APPEND_SYSTEM_PROMPT` | `appendSystemPrompt` | `none` |
| `dangerously_skip_permissions` | `AXON_DANGEROUSLY_SKIP_PERMISSIONS` | – | `false` |
| `isolate_worktrees` | `AXON_ISOLATE_WORKTREES` | `isolateWorktrees` | `false` |
| `verbose` | `AXON_VERBOSE` | `verbose` | `false` |
| `trace_level` | `AXON_TRACE_LEVEL` | `traceLevel` | `"normal"` |
| `budget_turn_usd` | `AXON_BUDGET_TURN_USD` | `budget.turn_usd` | `none` |
| `budget_session_usd` | `AXON_BUDGET_SESSION_USD` | `budget.session_usd` | `none` |
| `budget_action` | `AXON_BUDGET_ACTION` | `budget.action` | `"stop"` |
| `forecast_threshold_usd` | `AXON_FORECAST_THRESHOLD_USD` | – | `0.25` |
| `instruction_files` | `AXON_INSTRUCTION_FILES` | `instructionFiles` | `"axon-md-or-agents-md"` |
| `auto_memory` | `AXON_AUTO_MEMORY` | `autoMemoryEnabled` | `true` |
| `memory_excludes` | `AXON_MEMORY_EXCLUDES` | `axonMdExcludes`, `claudeMdExcludes` | `[]` |
| `fallback_model` | `AXON_FALLBACK_MODEL` | `fallbackModel` | `none` |
| `routing` | `AXON_ROUTING` | `routing` | `none` |
| `additional_dirs` | `AXON_ADDITIONAL_DIRS` | `additionalDirectories` | `[]` |
| `system_prompt_override` | `AXON_SYSTEM_PROMPT_OVERRIDE` | `systemPrompt` | `none` |
| `output_style` | `AXON_OUTPUT_STYLE` | `outputStyle` | `"default"` |
| `reply_language` | `AXON_REPLY_LANGUAGE` | `replyLanguage` | `""` |
| `status_line` | `AXON_STATUS_LINE` | `statusLine` | `none` |
| `vim_mode` | `AXON_VIM_MODE` | `editorMode`, `vimMode` | `false` |
| `vim_insert_mode_remaps` | `AXON_VIM_INSERT_MODE_REMAPS` | `vimInsertModeRemaps` | `none` |
| `emoji_completion_enabled` | `AXON_EMOJI_COMPLETION_ENABLED` | `emojiCompletionEnabled` | `true` |
| `spellcheck` | `AXON_SPELLCHECK` | `spellcheck` | `none` |
| `editor_show_last_response` | `AXON_EDITOR_SHOW_LAST_RESPONSE` | `editorShowLastResponse` | `false` |
| `theme` | `AXON_THEME` | `theme` | `"auto"` |
| `cleanup_period_days` | `AXON_CLEANUP_PERIOD_DAYS` | `cleanupPeriodDays` | `0` |
| `verify_commands` | `AXON_VERIFY_COMMANDS` | `verifyCommands` | `[]` |
| `verify_timeout` | `AXON_VERIFY_TIMEOUT` | `verifyTimeout` | `900` |
| `repo_map` | `AXON_REPO_MAP` | `repoMap` | `false` |
| `repo_map_tokens` | `AXON_REPO_MAP_TOKENS` | `repoMapTokens` | `1500` |
| `suggestions` | `AXON_SUGGESTIONS` | `promptSuggestionEnabled` | `true` |
| `tips` | `AXON_TIPS` | `spinnerTipsEnabled` | `true` |
| `tips_llm` | `AXON_TIPS_LLM` | `tipsLlm` | `true` |
| `workbench_hint` | `AXON_WORKBENCH_HINT` | `workbenchHint` | `false` |
| `live_status` | `AXON_LIVE_STATUS` | `liveStatus` | `true` |
| `tips_refresh_turns` | `AXON_TIPS_REFRESH_TURNS` | `tipsRefreshTurns` | `5` |
| `context_limit` | `AXON_CONTEXT_LIMIT` | `contextLimit` | `0` |
| `max_width` | `AXON_MAX_WIDTH` | – | `0` |
| `screen_log` | `AXON_SCREEN_LOG` | – | `true` |
| `mouse_click_expand` | `AXON_MOUSE_CLICK_EXPAND` | `mouseClickExpand` | `false` |
| `notifications` | `AXON_NOTIFICATIONS` | `preferredNotifChannel` | `"important"` |
| `notify_after_seconds` | `AXON_NOTIFY_AFTER_SECONDS` | `notifyAfterSeconds` | `30.0` |
| `terminal_notify` | `AXON_TERMINAL_NOTIFY` | `terminalNotify` | `"off"` |
| `notify_sinks` | – | `notifications` (object form) | `{}` |
| `task_budget_tokens` | `AXON_TASK_BUDGET_TOKENS` | `taskBudgetTokens` | unset |
| `tips_refresh_minutes` | `AXON_TIPS_REFRESH_MINUTES` | `tipsRefreshMinutes` | `15.0` |
| `auto_title` | `AXON_AUTO_TITLE` | `autoTitle` | `true` |
| `title_llm` | `AXON_TITLE_LLM` | `titleLlm` | `true` |
| `title_refresh_turns` | `AXON_TITLE_REFRESH_TURNS` | `titleRefreshTurns` | `5` |
| `prompt_suggestions` | `AXON_PROMPT_SUGGESTIONS` | `promptSuggestions` | `true` |
| `respond_to_bash_commands` | `AXON_RESPOND_TO_BASH_COMMANDS` | `respondToBashCommands` | `true` |
| `disable_all_hooks` | `AXON_DISABLE_ALL_HOOKS` | `disableAllHooks` | `false` |
| `prompt_color` | `AXON_PROMPT_COLOR` | `promptColor` | `none` |
| `goal` | `AXON_GOAL` | – | `none` |
<!-- END GENERATED: settings -->

`permissions` takes `allow`, `deny` and `ask` lists. `hooks` maps event names to hook groups.
`budget` in `settings.json` maps to `budget_turn_usd`, `budget_session_usd` and `budget_action`.

### Status line refresh (`statusLine.refreshInterval`)

The status line command runs when the session data it receives changes (a new message, `/compact`, a mode or vim
mode change) and each time the prompt opens. For a clock or other outside data, `refreshInterval` also re-runs it
every N seconds while the prompt is idle. The minimum is 1; leave it out and nothing re-runs on a timer.

```json
{ "statusLine": { "type": "command", "command": "~/.axon/statusline.sh", "refreshInterval": 5 } }
```

### Vim INSERT-mode remaps (`vimInsertModeRemaps`)

Maps a two-key sequence typed in INSERT mode to Esc. Each key is exactly two printable characters and `"<Esc>"` is
the only target; other entries are ignored.

```json
{ "editorMode": "vim", "vimInsertModeRemaps": { "jk": "<Esc>" } }
```

The first key goes in as usual. The second, within one second, takes it back and switches to NORMAL mode, leaving
neither in the prompt. After a second, or with another key between them, both stay as text. Read from
`~/.axon/settings.json`, `~/.axon/config.toml`, `--settings` and the environment only, never from a project.

### Scheduled prompts in a session (`/loop`, `CronCreate`, `ScheduleWakeup`)

`/loop <prompt>` with no interval is self-paced: after each run the model picks the next delay, from 1 minute to 1
hour, and the reason is printed. `Esc` during the wait stops the loop (so does `Ctrl+C` on an empty prompt); the
model can also end it with `ScheduleWakeup` and `stop: true`. A run that neither reschedules nor stops gets one more
try 20 minutes later. A bare `/loop` runs the first non-empty of `.axon/loop.md`, `.claude/loop.md`,
`~/.axon/loop.md` and `~/.claude/loop.md` (read again on each run, cut at 25,000 bytes), else a built-in maintenance
prompt. `/loop 15m` runs the same on a fixed interval. `/loop list` lists, `/loop stop [id]` stops.

An interval becomes a cron step after its first run, which goes at once: `/loop 5m` is `*/5 * * * *`, so later runs land on the 5-minute marks. Seconds round up to
a minute, and an interval no step can hold, such as `7m` or `90m`, goes to the nearest one, and Axon says which.

The model schedules with `CronCreate`, `CronList` and `CronDelete`: a standard 5-field cron expression in local time
(`*`, `5`, `1-5`, `*/15`, lists; 0 or 7 is Sunday; day of month and day of week match if either does), recurring or
one-shot. A session holds at most 50 jobs, `/loop` jobs included. A recurring job (or loop) fires one final time at
its first run 7 days after creation, then deletes itself.

Jobs fire only between turns, when the prompt is empty, and only while the terminal session is open. A missed time
fires once, not once per miss. `/clear`, `/resume` and leaving the session end them in that session. Resuming it
(`--resume`, `--continue` or `/resume`) restores its `CronCreate` jobs and fixed-interval `/loop` jobs under the same ids, except
recurring jobs past their expiry and one-shots whose time has passed; the next run is the first one due from then. A
self-paced `/loop` is not restored, so run `/loop` again. In `-p`
mode, the SDK and the app without a terminal `CronCreate` refuses, because nothing would ever fire. `AXON_DISABLE_CRON=1`
removes the tools and `/loop` and stops anything scheduled.

Scheduling needs no permission, like `Task`: what a prompt leads to is checked when it runs, under the mode in force
then. Plan mode refuses `CronCreate` and `CronDelete`; deny rules apply as usual. A prompt from `CronCreate` or
`loop.md` reaches the model as labelled plain text, so a `/`, `!`, `#` or `@file` in it does nothing. Subagents do not
get these tools.

### Next-prompt suggestion (`prompt_suggestions`)

After Axon finishes an answer in an interactive terminal, it asks the small model (`small_model`, else
the main model) to guess what you will type next. The guess shows as dim ghost text in the empty
prompt, followed by `tab to accept`. Tab (or Right / End on the empty prompt) moves it into the
prompt, where you can edit it; typing, Esc, Up or Ctrl+C dismisses it. Enter on the empty prompt does
not send it, and a suggestion you do not send is never saved to the prompt history. Turn it off with
`/suggestions off`, `"promptSuggestions": false` in `settings.json` or `/config`.

Cost: a few hundred input tokens (at most about 700: your last message, the answer and two earlier
messages are trimmed) and ~15 output tokens per turn on the small model, recorded in the
usage ledger with the source `suggest`. Nothing is generated for interrupted or failed turns, slash
commands, subagents, `-p` print mode or when the output is not a terminal.

### Large input (`large_input_tokens`)

A single message that is too large to send whole is saved in full to `pastes/` in the session folder. The
model receives its start and end, a note saying how many lines were left out, and the saved path; it reads the
rest with `Read` or `Grep`. The text you typed around a pasted block always reaches the model. The limit is:

| `large_input_tokens` | Limit |
|---|---|
| `0` (default) | Automatic: a quarter of the model's context window, at most 100,000 tokens |
| above `0` | That many tokens |
| below `0` | No limit: everything is sent |

Sizes are estimates (about 3.7 characters per token, more tokens for non-ASCII text). The prompt shows the
size of a paste when it collapses into a chip, and says when only the start and end will be sent. Before the
request goes out, Axon prints what was saved and what it costs; input of 20,000 tokens or more gets that
notice even when it is sent whole. The same shrinking is applied to oversized messages in a resumed session,
and when a provider refuses a request as too large (HTTP 413 or a "context length" error) Axon shrinks the
oversized messages and old tool results, compacts, and retries up to twice.

### Session titles (`auto_title`, `title_refresh_turns`)

A new session is named by a short call on the session's model (the one you are using, followed when you switch
model or provider), after a quick title from the first words. `title_llm = false` skips the model and keeps the
first-words title. An automatic title is rewritten on turn 3 and then every `title_refresh_turns` turns (default
`5`; `0` never) from the recent messages and the latest answer; the old title is kept if it still fits. A title
you set with `/rename` is never rewritten, and slash commands do not count as turns. A session you resume keeps
its title: resuming marks an existing title as set, so refreshes only apply to sessions started in the running process. `/retitle` renames the current chat and
`/retitle all` the past ones still named after their first words.

### Bash sandbox (`sandbox.enabled`, `sandbox.allowNetwork`)

Off by default. `/sandbox on` runs every Bash command under the operating system's sandbox. See
[the architecture notes](architecture.md#shell-guard-and-the-sandbox) for what it does and does not cover.
`settings.json` form: `{"sandbox": {"enabled": true, "allowNetwork": false}}`.

### Automation

The `automation` block turns on the automatic parts of the marketplace: background connecting and
reconnecting of MCP servers, switching off a server that keeps failing, adding matching skills to your prompt,
connector suggestions for the project and update checks. Everything is off until `"enabled": true`.

```json
{ "automation": { "enabled": true, "startup_wait": 2, "auto_disable_after": 3, "auto_skills": true } }
```

It is read from `[automation]` in `~/.axon/config.toml` and `<project>/.axon/config.toml`, then from
`~/.axon/settings.json` and `<project>/.axon/settings.json` / `settings.local.json` (the Axon files only, not
`.claude/`). `auto_install` and `auto_update` are honoured from your own user files only. Change the
block with `/marketplace auto on|off|status|set <name> <value>`; every key is described in
[Marketplace](marketplace.md#automation).

### Other environment variables

| Variable | Effect |
|---|---|
| `AXON_HOME` | Root of all Axon state (default `~/.axon`) |
| `AXON_DISABLE_CRON` | `1`: no `/loop` or scheduling tools, and scheduled prompts stop firing |
| `AXON_SHELL` | Shell for tool commands: `bash`, `pwsh`, `powershell`, `cmd` or a path |
| `AXON_THEME` | `auto`, `dark`, `light`, `high-contrast` or `mono` |
| `AXON_TRACE_LEVEL` | Default trace level for the renderer |
| `NO_COLOR` | Disable colour |
| `FORCE_COLOR`, `AXON_FORCE_COLOR` | Force colour on (`truecolor` forces 24-bit) |
| `AXON_NO_HYPERLINKS`, `AXON_HYPERLINKS` | Turn clickable OSC 8 file links off or on |
| `AXON_SERVE_TOKEN` | Default bearer token for `axon serve` |
| `MCP_TIMEOUT`, `MCP_TOOL_TIMEOUT`, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` | MCP startup limit, per-call limit and idle limit, in milliseconds (see [Extending](extending.md#time-limits-output-limits-reconnecting)) |
| `MAX_MCP_OUTPUT_TOKENS`, `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` | Size of an MCP result kept in the conversation (default 25,000 tokens) and of MCP tool descriptions |
| `ENABLE_TOOL_SEARCH` | `true`, `false`, `auto` or `auto:N`: defer MCP tool schemas behind `ToolSearch` |
| `MCP_CLIENT_SECRET` | OAuth client secret for `axon mcp add --client-secret` without a terminal |
| `AXON_MANAGED_DIR` | Folder holding `managed-mcp.json` / `managed-settings.json` (default: the system managed folder) |
| Provider keys | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `CEREBRAS_API_KEY`, `AZURE_OPENAI_API_KEY`, `AGENTROUTER_API_KEY` |

## Keyboard shortcuts

At a permission prompt, `Tab` on **Yes** or **No** opens a comment field for a note to the model. `Enter` answers with
the note; `Tab` or `Shift+Tab` closes the field and keeps the text for when you answer that option; `Esc` declines
without it. In the plain-text prompt, type the note after a colon: `y: use staging` or `n: not on main`. After **Yes** the model gets the note after the result, after **No** as the reason, and it carries on.
**No** without a note stops the turn, along with the rest of that step, and waits for you; the model learns what
happened with your next message. A subagent's prompt and an SDK callback do not stop the turn. The options that allow
more than this one call and the `WebFetch` and `Http` prompts offer no field. Keys typed in the first 0.35 seconds of
a prompt are dropped, Tab included, so type-ahead can never answer one or open the field.

`/keybindings` lists the shortcuts in effect. To remap them, edit `~/.axon/keybindings.json`:

```json
{ "bindings": { "chat:externalEditor": ["ctrl+e"], "app:todos": null } }
```

## Spend caps, task budgets and notification sinks

`/budget` shows the caps, what is spent and what is left. `/budget session <usd|off>` and `/budget turn <usd|off>` set the
caps; when the session cap is reached Axon pauses at a safe point (never between a tool call and its result), keeps the
whole conversation, and says so once. `/budget raise <usd>` (or the web page's button) lifts the cap and continues
from where it paused. Subagent spend counts toward the session cap.

`/budget task <tokens|off>` (setting `task_budget_tokens`, at least 20000) sends the Messages API `task_budget`
(a beta advisory countdown the model paces itself against). It is sent only to Claude models that take it, on the Claude
API; elsewhere it is silently left out. The Task tool's `budget` argument, or `budget:` in an agent's frontmatter, gives a
subagent its own.

Notification sinks live in one `notifications` object in settings.json (the plain string form still means the desktop
mode only):

```json
"notifications": {
  "mode": "important",
  "web": true,
  "allowPrivate": false,
  "webhooks": [{"url": "https://example.com/hook", "secret": "whsec_...", "events": ["requires_action", "idle"]}]
}
```

Events: `requires_action`, `budget_reached`, `idle`, `run_failed`, `scheduled_task_finished`, `subagent_finished`. A webhook
gets `{"id","type","time","session","summary"}` with headers `webhook-id`, `webhook-timestamp` and
`webhook-signature: v1,<base64 HMAC-SHA256 of "id.timestamp.body">` (the Standard Webhooks scheme the Managed Agents
webhooks use). The `id` is the same on every retry, so dedupe on it and reject timestamps older than five minutes. Delivery
is on a background thread with a 5 second timeout and three attempts; it never delays a turn. Only http and https are
allowed, and private or loopback targets are refused unless `allowPrivate` is set. Open dashboard and phone pages get the
events too and show an in-page alert plus a tab-title change (and a browser notification if allowed); iOS only delivers
alerts while the page is open, since web push needs an installed app. `/notify status|test|webhook <url>|off` manages them.

## Semantic code search (opt-in)

Off by default. `/index --backend ollama --model nomic-embed-text` (or `openai`, `voyage`) checks the backend, enables the
feature and `/index` builds the index in the background (`axon index` does it in the foreground). Settings block in the
user `settings.json` (never in a project file; the key is read from the named environment variable, not stored):

```json
{ "semanticSearch": { "enabled": true, "backend": "ollama", "model": "nomic-embed-text",
                      "baseUrl": "http://localhost:11434", "keyEnv": "", "maxChunks": 100000 } }
```

Backends: Ollama (`/api/embed`), any OpenAI-compatible `/v1/embeddings` endpoint, Voyage AI. **Remote backends receive the
text of the indexed files and your search queries**; the first index asks for confirmation and remembers the host
(`/index --status` shows it). Secret files (`.env`, `*.pem`, keys), secret-looking chunks, binaries, big files, lockfiles,
vendored directories and anything ignored by `.gitignore` or `.axonignore` are never sent. The `semantic_search` tool is
available to the model only while the feature is on and an index exists. `/index off` disables it (and can delete the index).

## OpenTelemetry trace export (`telemetry`)

Off by default. When on, Axon exports OpenTelemetry **traces** over OTLP/HTTP (JSON) to a collector you choose. It works
with every provider: each model request is a `gen_ai` client span whatever backend served it. No OpenTelemetry package is
needed. `/telemetry status|on|off|test` controls it; `/usage` shows one line while it is on.

```json
"telemetry": {
  "enabled": true,
  "endpoint": "http://localhost:4318",
  "headers": { "authorization": "Bearer ..." },
  "sampleRate": 1.0,
  "captureContent": false,
  "resourceAttributes": { "deployment.environment": "dev" },
  "serviceName": "axon",
  "propagate": false
}
```

`endpoint` gets `/v1/traces` appended unless it already ends with it. Without `endpoint`, the standard
`OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), `OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_RESOURCE_ATTRIBUTES` and `OTEL_SERVICE_NAME` are read; `AXON_ENABLE_TELEMETRY=1` turns it on from the environment.
**Only your user settings and your own environment can set these**: a project's settings.json (including its `env`
block) cannot, the same rule as `notify_sinks`.

**Span tree per user turn:** `axon.turn` (root) with children `chat <model>` (one per model request, kind CLIENT),
`execute_tool <name>`, `invoke_agent <type>` (a subagent run, linked to the turn, with its own `chat` and
`execute_tool` steps below it), `axon.compaction`, `axon.goal_check`, `axon.advisor` and `axon.permission_check`.

**Sent by default (metadata only):** `gen_ai.conversation.id` (the Axon session id), `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` /
`cache_read.input_tokens` / `cache_creation.input_tokens`, `gen_ai.response.finish_reasons`,
`gen_ai.response.time_to_first_chunk`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.agent.name`, and
`axon.*` attributes: cost in USD, retries, iteration, tool status and duration, permission decision, source and mode,
result size (lines and bytes), subagent status, compaction counts. Also the resource (`service.name`, `service.version`)
and your `resourceAttributes`. **Never sent by default:** prompts, replies, thinking, file contents, tool input or output.

**`captureContent: true`** adds `gen_ai.input.messages` (your prompt), `gen_ai.output.messages` (the reply),
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`, advisor question and advice, and a subagent's task title.
Text is passed through the same secret redactor as the session journal and capped at 0.75 MB per span
(`axon.content.truncated` is set when cut). Redaction is pattern based: treat the collector as trusted.

`sampleRate` (0 to 1) is decided per turn. `propagate: true` adds a W3C `traceparent` header to model requests, only
when `base_url` is an endpoint you configured yourself (a gateway, a local server), never a public vendor host unless
you list it in `propagateHosts`; the header carries only trace and span ids.

Export runs on a background thread with a bounded queue (2048 spans; overflow is dropped and counted), 3 s timeouts, 3
tries with backoff, and a flush at exit. A failing or slow collector never slows or fails a turn.
