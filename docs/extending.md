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

Axon uses the Claude Code layout. Later files override earlier ones for the same server name. The
order is: plugin `.mcp.json`, then `~/.axon/mcp.json` (scope `user`), then `<project>/.mcp.json`
(scope `project`), then `<project>/.axon/mcp.json` (scope `local`).

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
    }
  }
}
```

Each server spec accepts `type` (`stdio`, `http` or `sse`), `command`, `args`, `env`, `cwd`, `url`,
`headers`, `timeout` and `disabled`. `${VAR}` and `${VAR:-default}` are expanded when the server
starts, so the file keeps the reference rather than the secret.

From the shell or inside a session:

```bash
axon mcp add github --env 'GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}' -- npx -y @modelcontextprotocol/server-github
axon mcp add --transport http --scope project docs https://mcp.example.com/mcp --header 'Authorization: Bearer ${DOCS_TOKEN}'
axon mcp add-json local-db '{"command": "python", "args": ["db_server.py"]}'
axon mcp list
```

```text
/mcp                       status of every server (menu in a terminal)
/mcp tools [server]        tools exposed to the model (named mcp__<server>__<tool>)
/mcp resources [server]    resources; /mcp read <server> <uri>
/mcp prompts [server]      prompts; each one is also a slash command
/mcp enable|disable|remove <name>     /mcp logs <name>     /mcp presets
/mcp import                import servers from other tools' configs
```

Use `--mcp-config <file|json>` (and `--strict-mcp-config`) to load servers for one invocation only.
Permission rules can target `mcp__<server>` or `mcp__<server>__<tool>`.

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

**Events.** The core events are `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`Stop`, `SubagentStop`, `PreCompact`, `Notification` and `SessionEnd`. The extended events are
`PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `PostCompact`,
`SubagentStart`, `TaskCreated`, `TaskCompleted`, `StopFailure`, `InstructionsLoaded`,
`CwdChanged`, `DirectoryAdded`, `WorktreeCreate` and `WorktreeRemove`.

**Matcher.** A regular expression. For tool events it is tested against the tool name. For other
events it is tested against the source, trigger or reason (for example `startup` or `resume` for
`SessionStart`).

**Input.** The hook receives JSON on stdin: `session_id`, `cwd` and `hook_event_name`, plus
event-specific keys such as `tool_name`, `tool_input`, `tool_response` or `prompt`. The command
runs in the project directory with `AXON_PROJECT_DIR` set.

**Output.**

| Exit code | Meaning |
|---|---|
| `0` | Continue. JSON on stdout may contain `decision: "block"`, `continue: false`, `reason`, `systemMessage` and `hookSpecificOutput` (`permissionDecision`: `allow`, `ask` or `deny`; `additionalContext`; `updatedInput`). For `UserPromptSubmit` and `SessionStart`, plain stdout is added as context. |
| `2` | Block. stderr is shown to the model and to you. |
| Other | Non-blocking warning |

Each hook run is shown in the timeline (`› hook PostToolUse ran (1 command)`) together with its
decision. Working examples are in [`examples/hooks/`](../examples/hooks).

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

These frontmatter keys are supported: `name`, `description`, `when_to_use`, `allowed-tools` /
`disallowed-tools`, `argument-hint`, `arguments` (named `$name` placeholders), `model`, `effort`,
`paths` (globs the skill applies to), `context: fork`, `agent`, `disable-model-invocation` and
`user-invocable: false`.

In the body, `$ARGUMENTS` and `$1`, `$2`, … are substituted. A ``!`cmd` `` line injects the
command's output, but only after the permission check passes.

Skills are discovered in this order, and later sources override earlier ones: bundled, plugins,
`~/.axon/skills`, `~/.claude/skills`, `<project>/.claude/skills`, `<project>/.axon/skills`.
`/skills` browses, creates and installs skills. `/skill-doctor` reports each skill's token cost,
usage and problems.

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

The body supports `$ARGUMENTS`, `$1` to `$9`, `@path` (includes a workspace file) and ``!`cmd` ``
(inserts the output once permitted). Commands are loaded from plugins, `~/.axon/commands`,
`~/.claude/commands`, `<project>/.claude/commands` and `<project>/.axon/commands`. Later locations
override earlier ones.

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
`agents/`, `~/.axon/agents`, `<project>/.claude/agents`, `<project>/.axon/agents`, then
`--agents '<json>'`. Use `/agents` to list them or create one with a wizard, `--agent <name>` to
run the main session as an agent, and `/subagents` to open a subagent's transcript.

## Output styles

An output style is a named addition to the system prompt that changes how answers are written. The
built-in styles are `default`, `explanatory`, `learning` and `concise`. A custom style uses the
Claude Code format:

```markdown
---
name: Explanatory
description: Explains the reasoning behind each change
keep-coding-instructions: true
---
Before a non-trivial change, say what you will do and why ...
```

Styles are searched in this order: built-in, `~/.axon/output-styles`, `<project>/.claude/output-styles`,
`<project>/.axon/output-styles`, then plugin `output-styles/`. Select one with `/output-style <name>`
or with the `output_style` (`outputStyle`) setting.

## Plugins

A plugin is a folder with a `plugin.json` manifest (or `.claude-plugin/plugin.json`) that bundles
any of the extensions above:

```text
my-plugin/
├── plugin.json            {"name": "my-plugin", "version": "1.0.0", "description": "..."}
├── commands/*.md          → /my-plugin:<name>
├── skills/<name>/SKILL.md
├── agents/*.md
├── output-styles/*.md
├── hooks/hooks.json       (or "hooks" in the manifest)
└── .mcp.json              (or "mcpServers" in the manifest)
```

`${AXON_PLUGIN_ROOT}` (and `${CLAUDE_PLUGIN_ROOT}`) in hook commands and MCP specs expand to the
plugin directory. Plugins live in `~/.axon/plugins/<name>/` or `<project>/.axon/plugins/<name>/`.

```text
/plugin                                   list
/plugin create <name> [--global]          scaffold a new plugin
/plugin install <folder | https git URL> [--project]
/plugin enable|disable|remove <name>
```

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
every memory file, up to 4 levels deep.

## Project templates

`/new <template> [name]` and `axon new <template> <dir>` create a runnable starter project. The
bundled templates are `python-cli`, `python-lib`, `fastapi`, `flask`, `node-cli`, `express`,
`react-vite`, `typescript-lib`, `go-cli`, `rust-cli` and `static-site`. Each template is a folder
of `.tmpl` files under `src/axon/features/templates/` with a `template.json`.
