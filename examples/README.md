# Axon configuration examples

Ready-to-copy files. Each one is valid for the loaders in this version of Axon (the test suite
parses every file here with the real loaders). Copy a file, then edit it.

Axon keeps its own files in two places:

- **User level**: `~/.axon/` (or `$AXON_HOME`). Applies to every project. On Windows this is
  `C:\Users\<you>\.axon\`.
- **Project level**: `<project>/.axon/`. Applies to one repository. Commit it to share it with
  your team. Use the `*.local.*` variants for personal settings.

The quickest way to create `~/.axon/config.toml` and store your API key is to run `axon setup`.

| Example file | Copy to | What it does |
|---|---|---|
| `config.toml` | `~/.axon/config.toml` | Provider, model, permission mode, trace level, budgets, rules, hooks (TOML) |
| `settings.json` | `<project>/.axon/settings.json`, `<project>/.axon/settings.local.json` or `~/.axon/settings.json` | Claude-Code-style settings: model, `permissions` allow/deny + `defaultMode`, `env`, `hooks`, `budget`, `trace_level` |
| `mcp.json` | `~/.axon/mcp.json`, `<project>/.mcp.json` or `<project>/.axon/mcp.json` | MCP servers (stdio and http), with `${VAR}` / `${VAR:-default}` expansion |
| `hooks/*.py` | `<project>/.axon/hooks/` | Hook scripts used by `settings.json` (stdlib Python, run on any OS) |
| `agents/*.md` | `~/.axon/agents/` or `<project>/.axon/agents/` | Subagents the model can delegate to (Task tool, `/agents`) |
| `commands/*.md` | `~/.axon/commands/` or `<project>/.axon/commands/` | Custom slash commands: `review-pr.md` becomes `/review-pr` |
| `skills/release-notes/SKILL.md` | `~/.axon/skills/release-notes/` or `<project>/.axon/skills/release-notes/` | A skill: instructions the model loads when they are relevant (`/skills`) |
| `output-styles/explanatory.md` | `~/.axon/output-styles/` or `<project>/.axon/output-styles/` | A response style in Claude Code format, selected with `/output-style` |
| `AXON.md` | `<project>/AXON.md` (or `AXON.local.md`) | Project memory loaded into every session, with `@path` imports |

## Precedence

Settings, from lowest to highest priority: `~/.axon/config.toml`, `<project>/.axon/config.toml`,
`~/.axon/settings.json`, `<project>/.axon/settings.json`, `<project>/.axon/settings.local.json`,
`.env` files, `AXON_*` environment variables, then command-line flags. Permission `allow`/`deny`
lists and hooks are **merged** across files, not replaced. Axon also reads the matching
`.claude/` files, so an existing Claude Code setup works as-is.

Project files cannot set `api_key` or `base_url`. This stops a cloned repository from sending
your key to another server. Keep keys in `~/.axon/.env` or in your environment.

## settings.json

JSON does not allow comments, so the explanations live here. Keys that start with `_` are ignored.

- `permissions.defaultMode`: `default` (ask before edits and commands), `acceptEdits`, `plan`
  (read-only) or `bypassPermissions`.
- Rule syntax: `Tool`, `Bash(prefix:*)`, `Bash(glob *)`, `Edit(path/**)`, `Read(~/.ssh/**)`,
  `WebFetch(domain:example.com)`, `mcp__server` or `mcp__server__tool`. `deny` always wins.
- `env`: variables exported for the session, for tools, hooks and MCP servers. The env block can
  never override `AXON_API_KEY` or `AXON_BASE_URL`.
- `hooks`: events are `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`,
  `SubagentStop`, `PreCompact`, `Notification` and `SessionEnd`, plus the extended events listed in
  [docs/extending.md](../docs/extending.md#hooks). `matcher` is a regex over the tool name (for tool
  events), or over the source/trigger/reason for the other events.

## Hooks

Each hook command gets the event as JSON on stdin (`tool_name`, `tool_input`, `cwd`,
`session_id`, ...). It runs in the project directory, and `AXON_PROJECT_DIR` is set.

- Exit code `0`: continue. JSON on stdout may contain `decision: "block"`, `reason` or
  `hookSpecificOutput.permissionDecision` (`allow` / `ask` / `deny`).
- Exit code `2`: block. stderr is shown to the model and to you.
- Any other exit code is a non-blocking warning.

The scripts are started with `python`. If your system only has `python3` (some Linux and macOS
installs) or `py` (Windows), change the command in `settings.json` to match. Run `/hooks` to see
which hooks are loaded and where each one came from.

## Agents, commands and skills

All three are markdown files with a `---` frontmatter block:

- **agents**: `name`, `description` (tells the model when to use the agent), `tools` (a comma
  list or a YAML list; leave it out for all tools), `model` (`inherit` uses the session model),
  `readonly`, `maxTurns`. The example `code-reviewer` and `test-runner` agents replace the
  built-in agents with the same names.
- **commands**: `description`, `argument-hint`, `allowed-tools`, `model`. In the body, `$ARGUMENTS`
  is replaced by everything typed after the command, and `$1`..`$9` by single words.
  `@path` includes a file from the workspace, and `` !`cmd` `` inserts a command's output (only
  after the permission check passes). Subfolders become namespaces: `frontend/component.md` is
  `/frontend:component`.
- **skills**: a folder containing `SKILL.md` with `name`, `description`, `allowed-tools` and
  `argument-hint`. Only the name and description go into the prompt. The model loads the body
  when it needs it.

## Output styles

`output-styles/explanatory.md` uses the Claude Code output-style format (`name`, `description`,
optional `keep-coding-instructions`, then the instructions). Copy it to `~/.axon/output-styles/`
or `<project>/.axon/output-styles/` and select it with `/output-style explanatory` or the
`outputStyle` setting. Axon also has built-in `default`, `explanatory`, `learning` and `concise`
styles. A custom file with the same name as a built-in replaces it.

For the full file formats see [docs/extending.md](../docs/extending.md).

## AXON.md imports

`@relative/path.md`, `@~/file.md` and absolute paths are imported recursively, up to 4 levels
deep. Imports inside code spans and code fences are ignored, and so are `<!-- -->` block comments.
