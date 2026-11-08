# Extending Axon

Every extension point is a plain file in a known folder. User-level files live in `~/.axon/` and
apply to all projects. Project-level files live in `<project>/.axon/` and can be committed for the
team. The matching Claude Code locations (`.claude/…`, `~/.claude/skills`) are read too, so an
existing setup works without changes. Copy-ready samples are in [`examples/`](../examples/README.md).

| Extension | User | Project | Inspect with |
|---|---|---|---|
| MCP servers | `~/.axon/mcp.json` | `.mcp.json`, `.axon/mcp.json` | `/mcp`, `axon mcp list` |
| Hooks | `~/.axon/settings.json` | `.axon/settings.json`, `.axon/settings.local.json` | `/hooks` |
| Skills | `~/.axon/skills/<name>/SKILL.md` | `.axon/skills/<name>/SKILL.md` | `/skills`, `/skill-doctor` |
| Custom commands | `~/.axon/commands/*.md` | `.axon/commands/*.md` | `/help` |
| Subagents | `~/.axon/agents/*.md` | `.axon/agents/*.md` | `/agents` |
| Output styles | `~/.axon/output-styles/*.md` | `.axon/output-styles/*.md` | `/output-style` |
| Plugins | `~/.axon/plugins/<name>/` | `.axon/plugins/<name>/` | `/plugin` |
| Memory | `~/.axon/AXON.md`, `~/.axon/rules/` | `AXON.md`, `.axon/rules/` | `/memory` |

After editing these files, run `/reload-plugins` to re-scan plugins, commands and skills without
restarting.

## MCP servers

Axon is an MCP client for stdio, Streamable HTTP, legacy SSE and WebSocket servers, and uses the Claude
Code layout. Later files override earlier ones for the same server name (the whole entry wins, fields are
not merged). The order is: plugin `.mcp.json`, then `~/.axon/mcp.json` (scope `user`), then
`<project>/.mcp.json` (scope `project`, shared with the team), then `<project>/.axon/mcp.json` (scope
`local`, just you). Servers your organization provides rank above all of these (see
[Managed MCP](#managed-mcp-and-allow--deny-lists)). A plugin server that points at the same URL or command
as one of your own counts once, and the same name with different endpoints in several scopes is flagged in
`/mcp`.

```json
{
  "mcpServers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" },
      "timeout": 60
    },
    "docs": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" }
    },
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/socket",
      "headersHelper": "/opt/bin/get-mcp-auth-headers.sh"
    },
    "sentry": {
      "type": "http",
      "url": "https://mcp.sentry.dev/mcp",
      "oauth": { "scopes": "org:read project:read", "callbackPort": 8080 }
    }
  }
}
```

Each server spec accepts `type` (`stdio`, `http`, `sse`, `ws`; `streamable-http` is an alias for `http`),
`command`, `args`, `env`, `cwd`, `url`, `headers`, `headersHelper`, `oauth`, `alwaysLoad`, `timeout` and
`disabled`. An entry with a `url` and no `type` is read as `http`. An entry with `"type": "sdk"` is skipped
(only an SDK host can register those).

### Environment variables

`${VAR}` and `${VAR:-default}` are expanded in `command`, `args`, `env`, `cwd`, `url` and `headers` when
the server starts, so the file keeps the reference rather than the secret; `/mcp get`, `axon mcp list` and
the warnings show the reference by name. A reference with no value and no default marks the server
`needs-config` and names the variable. Stdio servers receive `AXON_PROJECT_DIR` and `CLAUDE_PROJECT_DIR`
(the stable project root) in their environment; `${CLAUDE_PROJECT_DIR:-.}` also works in `command` / `args`.

In a remote server's `url` and `headers`, credential variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the
other provider keys, `AWS_*` credentials, `HTTPS_PROXY`, `NPM_TOKEN`, ...) read as empty, whether or not they
are set, so a repository's `.mcp.json` cannot send them to a host it names. Copy the value into a variable of
your own and reference that instead. Hidden leading or trailing whitespace in `command`, `url`, `args`,
`env` and `headers` (a pasted token with a trailing newline) is reported without echoing the value.

### Adding servers

```bash
axon mcp add github --env 'GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}' -- npx -y @modelcontextprotocol/server-github
axon mcp add --transport http --scope project docs https://mcp.example.com/mcp --header 'Authorization: Bearer ${DOCS_TOKEN}'
axon mcp add --transport http --client-id my-id --client-secret --callback-port 8080 svc https://mcp.example.com/mcp
axon mcp add-json local-db '{"command": "python", "args": ["db_server.py"]}'
axon mcp add-json events '{"type":"ws","url":"wss://mcp.example.com/socket"}' --scope local
```

`--scope` is `user` (default), `project` or `local`. `--env` takes several `KEY=value` pairs (put another
option between it and the server name). Server names contain only letters, numbers, hyphens and
underscores; adding a name that already exists in that scope is refused. `--client-secret` reads
`MCP_CLIENT_SECRET` or prompts with masked input, and stores the secret in `~/.axon/mcp-auth.json`
(mode 0600), never in the config. An `http` URL that rejects the Streamable HTTP POST (400, 404 or 405) is
retried as legacy SSE.

### Managing servers

```text
axon mcp list                         health of every server (connects each one: Connected, Needs authentication, ...)
axon mcp get <name>                   scope, status, endpoint, headers, OAuth state, warnings
axon mcp remove <name> [--scope S]    also deletes stored OAuth tokens; ambiguous names need --scope
axon mcp login <name> [--no-browser]  OAuth sign-in    axon mcp logout <name>
axon mcp reset-project-choices        forget .mcp.json approvals and rejections
axon mcp add-from-claude-desktop      import Claude Desktop's servers (duplicates get a _1 suffix)
axon mcp serve                        run Axon's own tools as an MCP server over stdio
```

```text
/mcp                       status of every server (menu in a terminal)
/mcp get <name>            details of one server
/mcp tools [server]        tools exposed to the model (named mcp__<server>__<tool>), and tools left out
/mcp resources [server]    resources; /mcp read <server> <uri>
/mcp prompts [server]      prompts; each one is also a slash command
/mcp enable|disable|remove <name>     /mcp logs <name>     /mcp presets
/mcp reconnect <name|all>  reconnect one server, or every server that failed or needs sign-in
/mcp login|logout <name>   OAuth sign-in / clear stored authentication
/mcp approve [name|all]    /mcp reject <name>    approve or reject .mcp.json servers
/mcp import                import servers from other tools' configs
/mcp test <name>           connect once and list the tools (health check)
/mcp scope <name> user|project|local      move a server to another scope
/mcp env <name> NAME=VALUE ... [--unset NAME]    keys and variables (secrets go to ~/.axon/.env)
/mcp timeout <name> <seconds>             time limit for the server
/mcp tool <name> [<tool>|*] allow|ask|deny|reset  permission rule per tool
/mcp dashboard [--check]   every connector, skill and plugin: enabled, connected, tools, last error, scope
/mcp export [file]         /mcp import <file>      the whole setup as one JSON file
/mcp auto on|off|status    /mcp suggest    /mcp updates       automatic behaviour (off by default)
```

These subcommands (and `axon mcp ...` for the same words) run the same code as
`/marketplace`; see [Marketplace](marketplace.md#controlling-what-is-installed) for what each one does.

Use `--mcp-config <file|json>` (and `--strict-mcp-config`) to load servers for one invocation only.
Permission rules can target `mcp__<server>` or `mcp__<server>__<tool>`.

### Project servers need your approval

A server from a project's `.mcp.json` is a command or URL that repository chose. In an interactive session
Axon lists new ones and asks before using them (`a` approve all, `o` one by one, `n` never, Enter decide
later); undecided servers stay unconnected and show `Pending approval`, and `/mcp approve` answers later.
Answers are kept per project in `~/.axon/mcp-state.json`. `enabledMcpjsonServers`,
`enableAllProjectMcpServers` and `disabledMcpjsonServers` in your own `~/.axon/settings.json` or
`.axon/settings.local.json` decide without a prompt (a project's committed settings cannot approve their own
servers; `disabledMcpjsonServers` rejects from any file). `axon -p` and SDK runs load project servers
without asking. Servers you add yourself with `--scope project` are approved on the spot. `/mcp disable` on
a project server is recorded in the same state file, so the shared `.mcp.json` is never edited.

### Authentication

* Static: `headers` (`"Authorization": "Bearer ${TOKEN}"`). A rejected configured `Authorization` header is
  reported as a failed connection, never as a request to sign in.
* `headersHelper`: a shell command that prints a JSON object of header names to values. It runs on every
  connection and again, once, after a 401/403 (10 second limit). Its headers override static ones; the
  environment has `CLAUDE_CODE_MCP_SERVER_NAME` / `CLAUDE_CODE_MCP_SERVER_URL` (and `AXON_MCP_SERVER_*`).
  Helpers from a project `.mcp.json` or a plugin run without credential-looking environment variables.
* OAuth 2.0 (HTTP and SSE): a server that answers `401`/`403` with no configured `Authorization` shows
  `Needs authentication`. Sign in with `/mcp login <name>` or `axon mcp login <name>`; Axon discovers the
  authorization server (RFC 9728 / RFC 8414), registers itself (Dynamic Client Registration) unless you
  gave `oauth.clientId`, runs the PKCE flow in your browser and refreshes the token automatically. Without a
  local browser (SSH, no display, `--no-browser`) it prints the URL and you paste the redirect URL back.
  `oauth` accepts `clientId`, `callbackPort` (redirect `http://localhost:PORT/callback`), `scopes` (one
  space-separated string; `offline_access` is added when the server supports it) and `authServerMetadataUrl`
  (https only). Tokens go only to an https (or localhost) token endpoint. Headers configured for the server
  (except `Authorization`) are also sent on its sign-in requests, for gateways that require them.

### Time limits, output limits, reconnecting

| Setting | Effect |
|---|---|
| `MCP_TIMEOUT` (ms) | how long one server may take to start (default 30 s) |
| `timeout` (server entry; values over 1000 are milliseconds, smaller ones seconds) | hard wall-clock limit per tool call; progress notifications do not extend it |
| `MCP_TOOL_TIMEOUT` (ms) | the same limit for servers without their own `timeout` (default about 28 h) |
| `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (ms, 0 = off) | abort a call with no response and no progress for this long (default 5 min remote, 30 min stdio; never shorter than the server's `timeout`) |
| `MAX_MCP_OUTPUT_TOKENS` | a result over this many tokens (default 25,000), or over 50,000 characters, is saved to a file in the session's `outputs` folder and replaced by a message naming it |
| `anthropic/maxResultSizeChars` (tool `_meta`) | a tool's own threshold, up to 500,000 characters, replacing both |
| `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` | cap for tool descriptions and server instructions (default 4,096 upfront, 16,384 behind ToolSearch) |

A local (stdio) server that is only slow, and misses the start-up limit, is retried twice in the background
(after 2 s with twice the limit, then after 5 s with four times, at most 120 s) and its tools appear when it
answers. A local server that crashes is restarted up to three times (1, 2, 4 s); after more than three
crashes in two minutes it is left `failed` with a message, and `/mcp reconnect <name>` tries again.

With `automation` turned on ([Marketplace](marketplace.md#automation)) the first connection also retries
after 2, 5 and 15 seconds, startup waits only `startup_wait`
seconds for servers (the rest connect in the background and register their tools when ready), and a
server that fails `auto_disable_after` connection rounds in a row is switched off with a notice. The
failure count lives in `~/.axon/state/mcp-health.json`.

Results over 10,000 tokens are logged as a warning in the server's log; error results over about 11,000
characters keep their first and last 5,000; PNG/JPEG/GIF/WebP images are saved as files and the model gets the
path. A remote server that drops is reconnected up to five times (1, 2, 4, 8, 16 s) and shows `reconnecting`;
a first HTTP/SSE connection retries transient errors (5xx, refused, timeout) three times; discovery requests
(`tools/list`, ...) retry too. A call that was in flight when a connection closed is repeated once on the new
connection when the tool is annotated `readOnlyHint` or `idempotentHint`. `list_changed` notifications
refresh a server's tools; a failed refresh keeps the previous ones.

### Tools, tool search, prompts, resources

* **Tool shaping.** A schema with `anyOf` / `oneOf` / `allOf` at its root is flattened (properties merged;
  `anyOf`/`oneOf` requirements are described in the tool description instead of enforced). A tool whose
  schema would make the API reject every request (a top-level property name outside `[A-Za-z0-9_.-]{1,64}`,
  a non-object `properties`) is left out and listed by `/mcp tools`. `_meta["anthropic/requiresUserInteraction"]: true`
  makes Axon ask before every call of that tool in every permission mode; `readOnlyHint` marks it read-only.
* **Tool search.** When MCP tool schemas would take more than 10% of the context window they move behind
  the `ToolSearch` tool. `ENABLE_TOOL_SEARCH=true` defers every MCP tool, `false` none, `auto` / `auto:N`
  use a threshold of 10% / N%. `"alwaysLoad": true` on a server (or `_meta["anthropic/alwaysLoad"]` on a
  tool) keeps tools out of deferral; `"alwaysLoad": false` always defers that server. `ToolSearch` waits for
  servers that are still connecting and tells the model which servers failed and why.
* **Instructions.** A server's `instructions` are added to the system prompt (each capped at 4,096 characters).
* **Prompts.** Each prompt is a slash command: `/mcp__<server>__<prompt> arg1 arg2` or `/<server>:<prompt>`
  (arguments split on whitespace; `key=value` also works). A server named `anthropic-skills` is hidden.
* **Resources.** `@server:uri` in a prompt inlines the resource; the `@` menu lists them. MCP Apps UI
  resources (`ui://` or `text/html;profile=mcp-app`) are not offered.
* **Elicitation.** In an interactive session a server can ask you for input (a form, or a link to open);
  `-p` runs do not advertise the capability. `roots/list` answers with the project and every `--add-dir`
  directory.

### Managed MCP and allow / deny lists

Administrators can restrict MCP with files in the managed directory (`/Library/Application Support/Axon`
on macOS, `/etc/axon` on Linux, `C:\Program Files\Axon` on Windows; the matching `ClaudeCode` folders work
too; `AXON_MANAGED_DIR` overrides):

* `managed-mcp.json` (`{"mcpServers": {...}}`): exclusive control. Only these servers (plus
  `managedMcpServers`) load, `axon mcp add` fails with *enterprise MCP configuration is active*, and
  `--mcp-config` / `--strict-mcp-config` are refused at startup. `{"mcpServers": {}}` disables MCP.
* `managed-settings.json`: `managedMcpServers` (remote https servers, no `command`/`env`/`${VAR}`; users
  cannot remove them and see only the host), `allowManagedMcpServersOnly`, `strictPluginOnlyCustomization`.
* `allowedMcpServers` / `deniedMcpServers` (any settings file; denylists always merge): entries are
  `{"serverUrl": "https://*.example.com/*"}`, `{"serverCommand": ["npx", "-y", "pkg"]}` or
  `{"serverName": "github"}`. A server matching the denylist never loads; with an allowlist, a remote server
  must match a `serverUrl` entry (a `serverName` entry counts only when there are no `serverUrl` entries) and a
  stdio server a `serverCommand` entry. Unset allows everything, `[]` allows nothing. A `serverName` entry is
  a label, not a security control. The lists are re-checked whenever a server is started.

### Differences from Claude Code

`axon mcp add` saves to `user` scope by default (Claude Code's default is its per-project `local` scope; Axon's
`local` scope is `<project>/.axon/mcp.json`). Without `ENABLE_TOOL_SEARCH`, MCP tools are deferred as soon as their definitions
exceed about 2,000 tokens (`auto`), so one server with a handful of tools stays loaded and anything bigger waits
behind `ToolSearch`. `ENABLE_TOOL_SEARCH=true` defers every MCP tool, `false` none. A `${VAR}` with no value and no default marks the
server `needs-config` and names the variable instead of loading it with the literal text. A stdio server is
stopped by closing its stdin and then ending its process tree after 1.5 seconds. `timeout` values of 1000 or
less are seconds, as before. There is no separate `WaitForMcpServers` tool: `ToolSearch` waits, and a call to a
server on its first connection waits up to 10 seconds.

### Not implemented

claude.ai connectors and Anthropic-hosted OAuth (account features), Claude Desktop/Remote Control/VS Code
surfaces, MCP tasks and protocol revision 2026-07-28 (beta, flag-gated), the discovery cache, automatic
backgrounding of long MCP calls, and `--channels` message pushing.

## Hooks

Hooks are shell commands that run at lifecycle events, using the same JSON format as Claude Code.
They are merged from `config.toml` `hooks`, `~/.axon/settings.json`, `<project>/.axon/settings.json`,
`<project>/.axon/settings.local.json` and enabled plugins. Set `disableAllHooks: true` to turn them
all off.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python .axon/hooks/block-dangerous-bash.py", "timeout": 10 }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit|Patch",
        "hooks": [{ "type": "command", "command": "python .axon/hooks/format-on-edit.py" }] }
    ]
  }
}
```

**Built-in handler.** Besides the configured hooks, `HookManager` runs one handler of its own for
`UserPromptSubmit` (`HookManager.internal_handlers`, see `axon.skills.auto`). It shows the one-line notices that
background work queued (`axon.notices`) and, with `automation.auto_skills` on, adds the matching skills. It
follows `disableAllHooks`, and a failure in it never blocks a prompt.

**Settings files.** Hooks are read from `~/.axon/settings.json`, `<project>/.axon/settings.json` and
`.claude/settings.json`, the matching `settings.local.json` files, `config.toml`, enabled plugins, and, for
the length of a run, from skills and subagents that register them (`HookManager.add_scoped`). The same
handler in several settings files runs once. `disableAllHooks` follows settings precedence (the last file
that sets it wins; `AXON_DISABLE_ALL_HOOKS=1` always disables). `allowedHttpHookUrls` (URL patterns with `*`)
and `httpHookAllowedEnvVars` restrict HTTP hooks.

**Events.** Fired by Axon: `SessionStart`, `UserPromptSubmit`, `UserPromptExpansion` (custom commands),
`PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`,
`Notification`, `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `Stop`, `StopFailure`,
`InstructionsLoaded`, `CwdChanged`, `DirectoryAdded`, `WorktreeCreate` (`--worktree`), `PreCompact`,
`PostCompact`, `PreModelSwitch`, `PostModelSwitch` (`/model`) and `SessionEnd`. These also fire:

| Event | When it fires |
|---|---|
| `Setup` (`trigger: "init"`) | once per folder, the first time Axon starts in it, just before that session's `SessionStart` hooks (marker: `~/.axon/state/setup-done.json`). There is no `--init-only` / `--maintenance` flag. |
| `FileChanged` | the matcher lists literal file names (`.envrc\|.env`, relative to the working directory). They are compared by modification time when a prompt is submitted, after each tool and at session start; `file_name`, `file_path` and `event` (`add`, `change`, `unlink`) are in the payload. There is no background watcher, so a change is seen at the next of those moments. |
| `ConfigChange` | before Axon writes a settings or MCP file (`enabledPlugins`, skill overrides, `/mcp add` ...). `source` is `user_settings`, `project_settings`, `local_settings` or `mcp_config`. Exit 2 blocks the write. Edits you make by hand in an editor are not seen. |
| `Elicitation` / `ElicitationResult` | around an MCP server's request for input (interactive sessions only). `Elicitation` may answer by itself (`action`, `content`) or block, which declines without a dialog; `ElicitationResult` sees the answer and may change it. |

`WorktreeRemove`, `MessageDisplay` and `TeammateIdle` are accepted in configuration (Claude Code settings
load unchanged) and the manager interprets their output (`displayContent`, `watchPaths`), but Axon has no
agent teams and the terminal renderer does not call the hook, so they never fire. `/hooks events` lists all
events, which ones take a matcher and which can block.

**Matcher.** `*`, empty or omitted matches everything. A value made only of letters, digits, `_`, `-`,
spaces, `,` and `|` is an exact string or a list (`Edit|Write`, `Edit, Write`); anything else is an
unanchored regular expression (`^Notebook`, `mcp__memory__.*`; a bare `mcp__memory` matches no tool).
`FileChanged` and `StopFailure` only treat letters, digits, `_` and `|` as exact. What is matched:
tool name (tool events), `source` (`SessionStart`: `startup`, `resume`, `clear`, `compact`, `fork`;
`ConfigChange`; `DirectoryAdded`), `trigger` (`PreCompact`, `PostCompact`, `Setup`), `reason`
(`SessionEnd`), `notification_type`, `agent_type` (`SubagentStart/Stop`), `error` (`StopFailure`),
`load_reason` (`InstructionsLoaded`), `command_name` (`UserPromptExpansion`), `to_model` (model switch events)
and `mcp_server_name` (elicitation). Other events have no matcher.

**`if`.** On a handler, `"if": "Bash(git *)"` or `"Edit(*.ts)"` (permission-rule syntax, one rule) narrows a
tool event further, so the process is only spawned when the call matches. For Bash every subcommand,
`$()` and backtick body is checked. On events that are not tool events a handler with `if` never runs.

**Handler types and fields.**

| `type` | Fields | Notes |
|---|---|---|
| `command` | `command`, `args`, `shell` (`bash`/`powershell`), `async`, `asyncRewake` | With `args` the command is spawned directly, no shell, `${CLAUDE_PROJECT_DIR}` / `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` are substituted into each element. `async` runs in the background; its `additionalContext` / `systemMessage` reach the model on the next turn. `asyncRewake` is bounded by `timeout` and delivers stderr when it exits 2. |
| `http` | `url`, `headers`, `allowedEnvVars` | POST with the event JSON. 2xx empty body = success, 2xx JSON object = output, anything else = error. |
| `mcp_tool` | `server`, `tool`, `input` (`${tool_input.file_path}` is filled from the event JSON) | The tool's text is read like stdout. Not on `SessionStart` / `Setup`. |
| `prompt` | `prompt` (`$ARGUMENTS`), `model`, `continueOnBlock` | One model call (small model by default) answering `{"ok": true}` or `{"ok": false, "reason": "..."}`; `impossible: true` lets `Stop` end the turn. |
| `agent` | `prompt`, `model` | Same contract as `prompt`; Axon evaluates it in a single model call without a tool loop. |

Common fields: `timeout` (seconds; defaults 600 for command/http/mcp_tool, 30 for prompt, 60 for agent,
30 on `UserPromptSubmit` and the model-switch events, 10 on `MessageDisplay`, 1.5 on `SessionEnd`,
raised per hook up to 60), `statusMessage`, `once` (skill hooks), `onFailure` (`"block"` makes a start
failure, a non-0/2 exit, an HTTP error, a timeout or invalid output block the action like exit code 2;
it has no effect on `Stop`, `SubagentStop`, `TaskCompleted`, `TeammateIdle` or background hooks).
All hooks matching one event run in parallel.

**Input.** JSON on stdin (the POST body for `http`): `session_id`, `prompt_id`, `transcript_path`, `cwd`,
`permission_mode`, `hook_event_name`, plus event-specific keys (`tool_name`, `tool_input`, `tool_use_id`,
`tool_response`, `duration_ms`, `prompt`, `last_assistant_message`, `source`, `model`, `agent_type`, ...).
The command runs in the project directory with `CLAUDE_PROJECT_DIR` / `AXON_PROJECT_DIR` set
(`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` for plugin hooks). `SessionStart`, `CwdChanged` (and
`Setup` / `FileChanged`) hooks also get `CLAUDE_ENV_FILE`: `export NAME=value` lines appended there become
environment variables of later Bash commands (plain assignments only; `CwdChanged` clears the file first).

**Output.**

| Exit code | Meaning |
|---|---|
| `0` | Success. stdout is one JSON object (below), or plain text. Plain text becomes context on `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart` and `PostModelSwitch`. Output that starts with `{` and ends with `}` but does not parse or validate is a non-blocking error. |
| `2` | Blocking error on events that can block (`PreToolUse`, `UserPromptSubmit`, `UserPromptExpansion`, `Stop`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `PostToolBatch`, `PreCompact`, `PreModelSwitch`, `WorktreeCreate`, ...): stderr is the reason. On `PostToolUse` / `PostToolUseFailure` stderr is shown to the model after the tool ran. `PermissionRequest` ignores exit 2. Elsewhere stderr is a warning. |
| Other | Non-blocking warning; valid JSON on stdout still applies. |

JSON fields (any event, unless the event discards them): `continue: false` + `stopReason` stop the turn,
`systemMessage` (shown as a hook warning), `terminalSequence` (an OSC 0/1/2/9/99/777 or BEL sequence Axon
writes to the terminal), `decision: "block"` + `reason`, and `hookSpecificOutput`:
`additionalContext`, `permissionDecision` (`allow`, `ask`, `deny`; `defer` is ignored outside `-p`
integrations) + `permissionDecisionReason`, `updatedInput` (replaces the whole tool input),
`updatedToolOutput` (`PostToolUse`: replaces the result the model sees), `decision` on `PermissionRequest`
(`behavior`, `updatedInput`, `updatedPermissions` with `addRules` / `setMode` for the session, `message`,
`interrupt`), `sessionTitle` (`SessionStart`, `UserPromptSubmit`), `reloadSkills`, `suppressOriginalPrompt`,
`displayContent`, `action` + `content` (elicitation), `worktreePath`. When hooks disagree on a permission the
order is deny > defer > ask > allow. `additionalContext`, `systemMessage` and plain stdout over 10,000
characters are saved to a file and replaced by its path and a 2,000-character preview. A `Stop` hook that
blocks or returns `additionalContext` keeps the turn going (`stop_hook_active` is true), at most 8 times
in a row (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`). `WorktreeCreate` prints the new worktree path as the last
line of stdout and replaces git for `axon --worktree`.

**Browsing.** `/hooks` lists every hook with its origin (user, project, local settings, plugin, config),
`/hooks <n>` shows one in full, `/hooks <Event>` filters, `/hooks events` shows all events and `/hooks reload`
re-reads the files. Each run also appears in the timeline (`› hook PostToolUse ran (1 command)`) together
with its decision. Working examples are in [`examples/hooks/`](../examples/hooks).

### Folder trust

Hooks, plugins and MCP servers that come with a folder run programs, so Axon asks before it starts any of
them from a folder you have not trusted. In a terminal, the first time you open a folder that contains
project hooks (`<ws>/.claude|.axon/settings[.local].json`, `<ws>/.axon/config.toml`), hooks or MCP servers of
a project plugin (`<ws>/.axon/plugins`), or servers in `<ws>/.axon/mcp.json`, Axon lists exactly what would
run, in full, and asks yes or no. If you say no, none of it loads, Axon works as usual and says so in one
line; `/trust yes` reviews and allows it later. A folder with nothing executable gets no prompt. Your own
`~/.axon` configuration is never gated, and `<ws>/.mcp.json` keeps its own per-server approval prompt.

The answer is stored in `~/.axon/trusted-folders.json` together with a hash of the listed commands. If the
list changes (a `git pull` adds or edits a hook), the folder is untrusted again and you are asked about the
new list. Installing a plugin into the project yourself, after reviewing what it runs, keeps a trusted
(or executable-free) folder trusted.

```text
/trust                 this folder: what would run, trusted or not
/trust yes             list it and trust it
/trust revoke [path]   stop trusting this folder (or another one)
/trust list            every trusted folder
```

**Non-interactive runs** (`axon -p`, the SDK, scripts) do not ask: running Axon non-interactively in a
repository is an explicit choice, so project hooks, plugins and servers load as they always did. To refuse
them there, set `{"trust": {"requireForNonInteractive": true}}` in `~/.axon/settings.json` (or
`AXON_TRUST_REQUIRE=1`). Then a folder is used non-interactively only after you trusted it from a terminal
(or `/trust yes`), and `-p` prints a one-line notice when something was held back. A repository cannot set
this key for you: only your own settings file is read.

Not implemented (Claude Code features outside the terminal CLI or without an Axon counterpart):
managed-policy settings (`allowManagedHooksOnly`), `defer` round trips,
`classifierContext` (no auto-mode classifier), PermissionDenied `retry`, background-session worktrees and
`${user_config.*}` plugin options.

## Skills

A skill is a folder containing a `SKILL.md`. Only the name and description go into the system
prompt. The model loads the body with the `Skill` tool when it becomes relevant, and you can run a
skill directly as `/<name>` or `/skill:<name>`.

```markdown
---
name: release-notes
description: Draft user-facing release notes from the git history since the last tag.
allowed-tools: Bash(git log:*), Bash(git tag:*), Read
argument-hint: "[from-ref]"
---
Produce release notes for the changes since `$ARGUMENTS` ...
```

All frontmatter keys are optional; booleans accept `true/false/yes/no/on/off/1/0`:

| Key | Meaning |
| --- | --- |
| `name` | Command name (default: the folder name). Names with control, zero-width or `<` `>` characters are ignored |
| `description`, `when_to_use` | What the skill does and when to use it. Listed to the model, capped at 1,536 characters together (`skillListingMaxDescChars`). Without `description` the first line of the body is used |
| `argument-hint`, `arguments` | Autocomplete hint; named positional arguments for `$name` placeholders (space-separated or a YAML list) |
| `disable-model-invocation` | Only you can run it (`/name`); it is not listed to the model |
| `user-invocable: false` | Only the model can use it; hidden from `/` and refused when typed |
| `allowed-tools` | Tools pre-approved for the turn that runs the skill (space/comma separated or a list; `${CLAUDE_SKILL_DIR}` works in rules) |
| `disallowed-tools` | Tools denied for that turn |
| `model`, `effort` | Model / effort for that turn (`inherit` keeps the session model) |
| `context: fork`, `agent`, `background` | Run the skill as the prompt of a fresh subagent (`agent`: `Explore`, `Plan`, `general-purpose` or any agent file; default general-purpose). Runs in the background unless `background: false` |
| `hooks` | Hooks registered when the skill is invoked and kept for the session (same format as `settings.json`, nested YAML) |
| `paths` | Globs the skill applies to (shown with the skill; with `automation.auto_skills` matching files make the skill more likely to be added to a prompt) |
| `triggers` | Phrases that make the skill relevant to a prompt (list or comma-separated). With `automation.auto_skills` on, a prompt containing one gets the skill added |
| `shell` | `bash` (default) or `powershell` for the `!` commands |
| `metadata`, `license`, `compatibility` | Passive data for your own tooling |

Frontmatter is only read when `---` is the very first line of the file. A skill folder can hold
supporting files (`reference.md`, `scripts/…`) that `SKILL.md` links to; the model reads them on demand.

Skills installed from the marketplace or by `/skills install` arrive as the whole folder (`SKILL.md`,
`scripts/`, `references/`, `assets/`), downloaded as one GitHub tarball. Every path must stay inside the
folder, a symlink that points outside it rejects the skill, and the download is capped at 300 files, 2 MB
per file and 8 MB in all. The install lists the files first and flags executable scripts; Axon never runs
them by itself (the skill's instructions may ask the model to, and each run goes through your permission
prompts). The commit and a hash of the files are recorded in `.axon-source.json`; `/marketplace update <id>`
shows what changed (added, removed and edited files, a diff of `SKILL.md`, a warning about edits you made
yourself) and asks before replacing anything.

### Body: arguments, variables, shell

| Placeholder | Value |
| --- | --- |
| `$ARGUMENTS` | everything typed after the skill name (if no placeholder receives anything, `ARGUMENTS: …` is appended) |
| `$ARGUMENTS[N]`, `$N` | the Nth argument, **0-based** (`$0` is the first), shell-style quoting; a missing one stays as written |
| `$name` / `${name}` | a name from `arguments:`; the last name takes the rest |
| `\$1`, `\$ARGUMENTS` | a literal (single backslash) |
| `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_SESSION_ID}`, `${CLAUDE_EFFORT}` | `AXON_` spellings work too |
| `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}`, `${user_config.KEY}` | in plugin skills (sensitive options show a placeholder) |

Argument text is inserted literally and is never scanned again (so it cannot smuggle in a `!` command).

``!`cmd` `` (at the start of a line or after whitespace) and a fenced ```` ```! ```` block run before the
skill is sent and are replaced by their output (stderr merged, 2-minute timeout). Each command is
checked against your permission rules; one that is denied, not approved, or that fails (non-zero exit;
exit 1 from `grep`/`rg`/`diff`/`find`/`test`/`git diff` is a normal result) **aborts the whole
invocation**. `"disableSkillShellExecution": true` replaces every command by
`[shell command execution disabled by policy]` (bundled skills are exempt).

### Where skills load

Later sources override earlier ones: bundled, project (`.claude/skills` then `.axon/skills` of every
directory from the repository root down to the working directory, nearest wins), then personal
(`~/.claude/skills`, `~/.axon/skills`; **personal beats project**). A skill replaces a bundled skill of
the same name, and beats a command file of the same name (`/cmd:<name>` still reaches the file).
Built-in commands win over skills; reach the skill as `/skill:<name>`.

- **Plugin skills** are namespaced `/<plugin>:<skill>` and also answer to the bare name while it is unique.
- **Nested skills** (`apps/web/.claude/skills`) load the first time a file below that folder is read or
  edited; a clashing name becomes `apps/web:deploy`. `/add-dir`ed directories load their
  `.claude/skills`, prefixed with the folder name on a clash (`cli:verify`).
- Skill folders may be symlinks. Folders named `synced` or `anthropic-skills*` are ignored.
- **Live reload:** adding, editing or deleting a `SKILL.md` is picked up within a second, no restart;
  `/reload-skills` forces a rescan.

Switch a skill on or off with `/skills enable|disable <name>` (or `/marketplace enable|disable <name>`):
the choice is saved as `skillOverrides` in `~/.axon/settings.json` (`--project` for `.axon/settings.json`), and a
disabled skill leaves the skill list, the system prompt and `/<name>`. `/skills` lists disabled skills at the
end.

Settings: `skillOverrides` (`{"deploy": "off"}` hides it, `"name-only"` lists it without a description,
`"user-invocable-only"` hides it from the model; plugin skills are not affected), `disableBundledSkills`,
`disableClaudeSkills` (Axon stops reading `~/.claude/skills`; Claude Code keeps them, and Axon's own
`~/.axon/skills` is still read), `disableSkillShellExecution`, `skillListingMaxDescChars`.

Typing several skills at the start of a message (`/write-tests /fix-issue 123`) runs up to six inline
skills in one turn; the trailing text is `$ARGUMENTS` for each. Forked skills are not stacked.

Bundled skills: `/code-review`, `/debug`, `/verify`, `/refactor`, `/security-audit`, `/test-gen`,
`/docgen`, `/git-workflow`, `/skill-creator`, `/deep-research`, … plus the Claude Code ones that are not
built-in commands: `/batch`, `/update-config`, `/fewer-permission-prompts`, `/run`, `/run-skill-generator`.
`/skills` browses, creates and installs skills. `/skill-doctor` reports each skill's token cost,
usage and problems; `/plugin validate <dir>` checks `SKILL.md` frontmatter.

## Custom slash commands

A Markdown file becomes a command: `review-pr.md` becomes `/review-pr`, and `frontend/component.md`
becomes `/frontend:component`.

```markdown
---
description: Fix a GitHub issue end to end, with a test
argument-hint: "<issue number or URL>"
allowed-tools: Bash(gh issue view:*), Bash(git diff:*)
model: claude-sonnet-4-5
---
Fix issue $1. Read it with `gh issue view $1`, write a failing test, fix, run the tests.
Extra context: $ARGUMENTS
```

Command files accept the same frontmatter as skills except `name` and `paths` (`description`,
`argument-hint`, `arguments`, `model`, `effort`, `allowed-tools`, `disallowed-tools`, `context: fork` +
`agent` + `background`, `shell`, `user-invocable`) and the same body features (`$ARGUMENTS`, `$ARGUMENTS[N]`,
`$0`.., named arguments, `${CLAUDE_*}` variables, ``!`cmd` `` and ```` ```! ```` blocks), plus `@path`
(includes a workspace file). Commands are loaded from plugins (`commands/`, the manifest `commands` paths
or its object map), `~/.claude/commands`, `~/.axon/commands`, `<project>/.claude/commands` and
`<project>/.axon/commands`. Later locations override earlier ones. Prefer a skill for new work: skills
also carry supporting files and win over a command with the same name.

## Subagents

The model delegates work through the `Task` tool. Each subagent has an isolated context, and only
its final report returns to the main conversation. Its LLM calls and tool calls appear nested in
the parent's timeline, and its cost is included in `/cost`.

The built-in agents are `general-purpose`, `explore`, `plan`, `code-reviewer`, `test-runner` and
`analytics`. Define your own:

```markdown
---
name: security-auditor
description: Reviews diffs for security problems. Use after changes to auth, crypto or input handling.
tools: Read, Grep, Glob, Bash
model: inherit
readonly: true
maxTurns: 15
---
You are a security auditor. Report findings with path:line, severity and a concrete fix.
```

Definitions are loaded in this order, and later sources override earlier ones: built-ins, plugin
`agents/`, `~/.axon/agents`, then every `.claude/agents` and `.axon/agents` from the repository root
down to the workspace (the closest wins), the same folders of `--add-dir` directories, and
`--agents '<json>'` (a JSON object, or the path of a JSON file). Agent folders are scanned
recursively, and names compare case-insensitively, so your own `Explore` replaces the built-in.
Use `/agents` to list them, `/agents show <name>`, `/agents check` for files that were skipped or have
fields Axon does not recognize, or `axon agents [--json]` from the shell. Run the main session as an
agent with `--agent <name>`, and open a subagent's transcript with `/subagents`.

### Agent file fields

Only `name` and `description` are required. A file with frontmatter but no `name`, a `name` that starts
with `-` or contains `:`, no `description`, or YAML that does not parse is skipped (`/agents check` says
why); a file with no frontmatter at all is read as an agent named after the file.

| Field | Meaning |
| --- | --- |
| `tools` | Allowlist, comma separated or a YAML list. `mcp__server` / `mcp__server__*` name a whole MCP server. `Agent(worker, researcher)` limits which subagent types an agent started with `--agent` may start; `Agent` alone allows any. Omitted = every tool the parent has |
| `disallowedTools` | Removed first, then `tools` is resolved against what is left. `Bash(git push *)` still removes the whole tool |
| `model` | `inherit` (default), or a model id; the Task call's `model` overrides it. `AXON_SUBAGENT_MODEL` sets the default for agents that name no model |
| `permissionMode` | `default`, `acceptEdits`, `plan` or `dontAsk` (never prompts: calls that would ask are denied). The session's `bypass`, `acceptEdits` and `plan` modes win over it; `bypassPermissions` and `auto` are only honoured when the session already has them |
| `maxTurns` | Step limit; at the limit the report is marked partial and the subagent can be resumed |
| `skills` | Skills whose full text is preloaded into the subagent (up to 32; `disable-model-invocation` skills are skipped) |
| `mcpServers` | Names (already configured servers) or inline `- name: {command: ...}` definitions that live only for this run |
| `hooks` | Hooks scoped to this agent (`PreToolUse`, `PostToolUse`, `Stop` which becomes `SubagentStop`, ...); the parent's hooks never see them |
| `memory` | `user` (`~/.axon/agent-memory/<name>/`), `project` (`.axon/agent-memory/<name>/`) or `local` (`.axon/agent-memory-local/<name>/`): the first 200 lines / 25 KB of `MEMORY.md` are in its prompt and it gets Read/Write/Edit to keep notes |
| `background` | Always run in the background |
| `effort` | `low`, `medium`, `high`, `xhigh` or `max` for this agent |
| `isolation` | `worktree`: run in a temporary git worktree (see below) |
| `color` | `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink` or `cyan` (shown by `/agents`) |
| `initialPrompt` | Submitted as the first turn when the agent is the main session (`--agent`) |
| `omitClaudeMd` | Start without the user and project memory files (`explore` and `plan` always do, and skip the git snapshot) |
| `readonly` | Axon extension: read-only tools only |

Unknown fields are ignored. Plugin agents ignore `hooks`, `mcpServers`, `permissionMode` and `initialPrompt`.
`--agent` applies the agent's prompt, model, tools, `permissionMode`, `effort`, `maxTurns`, skills, memory and
hooks to the main session (an explicit `--mode` wins over `permissionMode`).

### Background, forks, messages and resuming

- `run_in_background` on the Task call (or `background: true` in the definition) starts the subagent and
  returns at once; its report arrives later as a message that wakes the main agent. Foreground is the default.
- `subagent_type: "fork"` (or `/subtask`) hands the subagent the conversation so far, with the main agent's
  tools and model; forks run in the background. `Agent(fork)` in `permissions.deny` stops the model from forking.
- Give a subagent a `name`. `SendMessage(to, message)` then steers it while it runs (it reads the message
  before its next step), or resumes it once it has finished, with its whole history, in the background.
  `to` is the number, `sub-N`, or the name. Subagents hold `SendMessage` too: `to: "main"` reports to the main
  agent and another subagent's name reaches that sibling. A subagent you stopped yourself is not resumed.
  No message grants permissions or changes settings.
- `isolation: "worktree"` (Task call or definition) runs the subagent in its own worktree under the project's
  data folder, and applies its edits back to your workspace when it finishes. If they cannot be applied
  (conflict), the worktree and branch are kept and the report says where. Without a git repository it runs in
  the workspace. Axon creates the worktree from `HEAD`.
- At most 20 subagents run at once (`AXON_MAX_CONCURRENT_SUBAGENTS`); another start fails and the model is told
  not to retry. Subagents cannot start subagents of their own.
- `@agent-<name>` or `@"name (agent)"` in a prompt makes the model delegate to that subagent.
- `Agent(Explore)` in `permissions.deny` (or `--disallowedTools "Agent(Explore)"`) blocks one type; `Agent`
  is the Task tool under its Claude Code name everywhere a tool name is accepted.
- A subagent's report is scanned on its way back: text that imitates harness output (`<system-reminder>`,
  `Human:` lines) is escaped with a backslash and a `[harness: ...]` line says so. Nothing is removed.

Each subagent has a step limit (25 by default, `maxTurns` in its definition, or `max_steps` on the
Task call, capped by `max_iterations`). Three steps before the limit it is told to wrap up; at the
limit it writes its report without tools, so its findings are kept.

A `readonly: true` agent (and every subagent while the session is in `plan` mode) gets only
read-only tools, so it has no shell. If a subagent's model call fails after the provider's own
retries, the run resumes from where it stopped, up to 2 times; when it still fails, its report
lists the progress it made before the failure. Paths in a subagent's prompt are files for it to
read: images are never attached to the prompt automatically.

Not in Axon: agent teams (shared task list, split-pane teammates), `claude agents` background-session
view, cross-session / cross-machine messaging, dynamic workflows, managed-settings agents, and cloud or
desktop features.

## Output styles

An output style is a named addition to the system prompt that changes how answers are written. The
built-in styles are `default` (no addition), `proactive`, `concise`, `explanatory` and `learning`. A
custom style is a Markdown file in the Claude Code format:

```markdown
---
name: Diagrams first
description: Lead every explanation with a diagram
keep-coding-instructions: true
---
When explaining code or data flow, start with a Mermaid diagram, then explain in prose.
```

`keep-coding-instructions` defaults to **false** for custom styles: Axon's standard software-engineering
rules are then left out of the prompt, which suits non-coding roles. Set it to `true` to change only how
Axon communicates. A plugin style may set `force-for-plugin: true` to apply whenever the plugin is enabled
(it overrides your choice; the first one loaded wins).

Styles are searched in this order, later wins: built-in, plugins (`output-styles/` or the manifest
`outputStyles` paths), `~/.claude/output-styles`, `~/.axon/output-styles`, then `.claude/output-styles` and
`.axon/output-styles` of every directory from the repository root down to the working directory (nearest
wins). Files are re-read each message, so an edit applies from the next message. Select one with
`/output-style <name>` (case-insensitive) or the `outputStyle` setting.

## Plugins

A plugin is a folder that bundles any of the extensions above. The manifest is optional
(`.claude-plugin/plugin.json`, or `plugin.json` at the root); without one the standard layout is loaded.

```text
my-plugin/
├── .claude-plugin/plugin.json   {"name": "my-plugin", "version": "1.0.0", "description": "..."}
├── skills/<name>/SKILL.md       → /my-plugin:<name>   (or one SKILL.md at the plugin root)
├── commands/*.md                → /my-plugin:<name>
├── agents/*.md
├── output-styles/*.md
├── hooks/hooks.json             ({"hooks": {...}} wrapper)
└── .mcp.json
```

Manifest keys: `name`, `displayName`, `version`, `description`, `author`, `defaultEnabled` (false starts it
disabled), `dependencies` (`"name"`, `"name@marketplace"` or `{name, marketplace}`), `userConfig` and the
component keys. Paths start with `./`, must exist and stay inside the plugin.

| Key | Form | Effect on the default folder |
| --- | --- | --- |
| `skills` | path or paths (a folder of skill folders, or one folder holding `SKILL.md`; `"."` = the plugin root) | adds to `skills/` |
| `commands` | path(s) to `.md` files / folders, or an object map `{"name": {"source" or "content", "description", "argumentHint", "model", "allowedTools"}}` | replaces `commands/` |
| `agents` | `.md` file paths | replaces `agents/` |
| `outputStyles` | file or folder paths | replaces `output-styles/` |
| `hooks`, `mcpServers` | a `.json` path, an inline object, or an array of either | merged with `hooks/hooks.json` / `.mcp.json` (a later MCP server name wins) |

`${CLAUDE_PLUGIN_ROOT}` (`${AXON_PLUGIN_ROOT}`), `${CLAUDE_PLUGIN_DATA}` (a per-plugin folder under
`~/.axon/plugins/data/` that survives updates), `${CLAUDE_PROJECT_DIR}` and `${user_config.KEY}` expand in
hook commands, MCP specs and skill / command bodies. `userConfig` options (`type`, `title`, `description`,
`default`, `options`, `sensitive`, `required`, `min`/`max`) are set with
`/plugin configure <name> KEY=VALUE`; plain values are saved under `pluginConfigs` in
`~/.axon/settings.json`, sensitive ones in `~/.axon/plugins/secrets.json` (mode 0600) and never shown
in skill or command text. `lspServers`, `monitors`, `channels`, `themes`, `workflows`, `bin/` and
`settings.json` defaults of the Claude Code format are not used by Axon.

### Marketplaces

A marketplace is a folder (or git repo, or hosted JSON) with `.claude-plugin/marketplace.json`:

```json
{ "name": "my-market", "owner": {"name": "Me"},
  "plugins": [ {"name": "greet", "source": "./plugins/greet", "description": "..."} ] }
```

Add it from `owner/repo[#ref]`, a git URL (`https://…`, `git@host:path`, optional `#ref`), a path starting
with `./` or `/`, or an `https://…/marketplace.json` URL. Entry `source` is a path relative to the
marketplace root, `{"source": "github", "repo"}`, `{"source": "git-subdir", "url", "path"}` or
`{"source": "url", "url"}` (`ref` picks a branch or tag; `sha` pins one commit, which does not have to be the
branch tip: Axon fetches that commit and checks that HEAD is exactly it); `archive`, `npm` and `command` sources are not
supported. `strict` entries add `commands`/`agents`/`skills`/`outputStyles` to the plugin's own manifest, and
an entry without a `plugin.json` is the manifest. The reserved Anthropic marketplace names are refused.

```text
/plugin marketplace add <source> | list | update [name] | remove <name>
/plugin discover [marketplace]            plugins on offer
/plugin install <name>@<marketplace> [--scope user|project|local]
/plugin install <name> --marketplace <source>      add the marketplace, then install
/plugin install <folder | https git URL> [--project]      a plain plugin, no marketplace
/plugin enable|disable <name> [--scope …]   (enabling also enables dependencies; disabling is refused while another enabled plugin needs it)
/plugin update <name> · uninstall <name> · info <name> · configure <name> [KEY=VALUE …]
/plugin validate <path>                   plugin, marketplace or skills folder: errors and warnings
/plugin create <name> [--global] · /plugin reload
```

Scopes decide where `enabledPlugins` is written: `user` → `~/.axon/settings.json`, `project` →
`.axon/settings.json`, `local` → `.axon/settings.local.json` (the `.claude/settings*.json` files are read
too). Plugin files live in `~/.axon/plugins/<name>/` (user) or `<project>/.axon/plugins/<name>/` (project and
local); marketplace installs record their origin in `.axon-install.json`. The `/plugin` command is text
based (there is no tabbed panel), updates are manual (`/plugin marketplace update` then
`/plugin update`), and there is no auto-update.

**Integrity.** An install records the commit it came from and a hash of its files in `.axon-install.json`.
Before a marketplace plugin is installed or enabled through `/marketplace`, Axon lists every hook command
and MCP server it would run; an update shows the changed files, the hook / MCP commands that now run or no
longer run, and a warning if you edited the installed files, then asks. The automatic update
(`automation.auto_update`) skips any plugin or skill update that changes a hook, an MCP server or an
executable script and leaves it for `/marketplace update`.

## Memory and rules

`AXON.md` files hold project instructions. Their load order is described in the
[README](../README.md#memory). Rules in `.axon/rules/**/*.md` can be limited to certain paths with
frontmatter:

```markdown
---
paths: ["src/api/**/*.py"]
---
API handlers must validate input with pydantic models.
```

A path-scoped rule loads the first time a tool touches a matching file. `@path` imports work in
every memory file, up to 4 levels deep. Claude Code's user-level `~/.claude/CLAUDE.md` and
`~/.claude/rules/**/*.md` are loaded alongside `~/.axon/AXON.md` and `~/.axon/rules/`.

### Saved memory

Saved memories live in three scopes. Each is a folder of one `<slug>.md` file per memory plus a
`MEMORY.md` index of one-line pointers. The first 200 lines / 25 KB of every index load into the
prompt; the model reads a file when its pointer is relevant.

| Scope | Folder | Use |
|---|---|---|
| `global` | `~/.axon/memory/` | preferences for every project (always loaded) |
| `project` | `~/.axon/projects/<key>/memory/` | private notes for this project; where the model saves |
| `repo` | `<workspace>/.axon/memory/` | notes kept in the repo (shared if you commit them); the default of `/memory add` and `#note` |

A memory file starts with `name`, `description` (one line), `type` (`user`, `feedback`, `project` or
`reference`), `scope`, `created` and `updated`. Writes are atomic. Before a memory is overwritten,
deleted, moved or re-indexed its previous text goes to `~/.axon/memory-trash/` (global) or
`~/.axon/projects/<key>/memory-trash/<scope>/`, and `/memory restore` brings it back. Text that looks
like an API key or token is redacted before it is saved, and Axon says so.

`/memory` browses by scope and type (view, edit in `$EDITOR`, move, delete); `/memory doctor` reports
orphans, broken links, duplicates, an over-long index, memories not updated in 90 days and memories
that name a file that no longer exists; `/memory reindex` rebuilds the indexes; `/memory why` shows
which instruction and memory files were loaded and why; `/memory export` and `/memory import` move a
scope as one JSON file (import shows a preview and asks before anything is added). Old-format memory
files (`title`/`category`) keep working and are upgraded by `/memory reindex`.

## Project templates

`/new <template> [name]` and `axon new <template> <dir>` create a runnable starter project. The
bundled templates are `python-cli`, `python-lib`, `fastapi`, `flask`, `node-cli`, `express`,
`react-vite`, `typescript-lib`, `go-cli`, `rust-cli` and `static-site`. Each template is a folder
of `.tmpl` files under `src/axon/features/templates/` with a `template.json`.
