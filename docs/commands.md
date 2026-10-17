# Command reference

Slash commands are typed inside a session. `/help` lists them, `/help <command>` shows usage, and
`Tab` completes names. Besides the built-ins below, these also appear as slash commands:

- **Custom commands**: Markdown files in `.axon/commands/` or `.claude/commands/`
- **Skills**: `/<skill>` or `/skill:<name>`
- **Plugin commands**: `/<plugin>:<name>`
- **MCP prompts**

A custom command whose name collides with a built-in stays reachable as `/cmd:<name>`.

The tables below are generated from the command registry by `scripts/gen_docs.py`.

<!-- BEGIN GENERATED: commands-reference -->
113 built-in commands in 13 groups.

Contents: [Session](#session) · [Context & Cost](#context--cost) · [Model & Modes](#model--modes) · [Code & Review](#code--review) · [Agents, Skills & Tools](#agents-skills--tools) · [Memory & Project](#memory--project) · [System](#system) · [Code](#code) · [Getting started](#getting-started) · [Git](#git) · [Insight](#insight) · [Integrations](#integrations) · [Solve](#solve)

## Session

| Command | Aliases | Description |
|---|---|---|
| `/activity` | `/journal` | Everything you and Axon did: prompts, commands, shell, tools, settings, errors |
| `/branch` | `/fork` | Fork the conversation into a new session |
| `/clear` | `/reset` | Start a fresh session (the old one stays resumable) |
| `/delete` |  | Delete a saved session (picker, id, or --all) |
| `/exit` | `/quit` | Save the session and quit |
| `/export` |  | Export the conversation (Markdown, JSON, text) to a file or the clipboard |
| `/history` | `/timeline` | List the messages in the conversation |
| `/library` | `/saved` | Everything saved: sessions, subagent runs, skills, agents, memory, hooks, plans |
| `/loop` | `/repeat` | Repeat a prompt N times or on an interval |
| `/project` | `/where` | Current project: name, path, data folder, sessions, journal, memory |
| `/rename` | `/title` | Rename the current session (no name: generate one) |
| `/restore` | `/trash` | List deleted sessions (the trash) and restore them |
| `/resume` | `/continue` | Resume a previous session of this workspace |
| `/retry` |  | Retry the last turn that failed with a provider error |
| `/rewind` | `/undo` `/checkpoint` | Restore code and/or conversation to an earlier prompt |
| `/sessions` | `/session` | List saved sessions with tokens and cost |
| `/star` | `/fav` | Toggle the favourite star on this session |
| `/tag` |  | Tag the current session (use '-' to clear) |

<details><summary><code>/activity</code> usage</summary>

```text
/activity                      the last 30 events of this project
/activity <N>                  the last N events
/activity --kind prompt,shell  only these kinds (--kinds lists them)
/activity --session <id>       only one session (id or prefix)
/activity <text> | --grep <t>  events containing text
/activity --since 2h|3d        only recent events
/activity --all-projects       every project's journal
/activity --export <file>      write the selection (.md or .jsonl)
/activity --path               where the journal lives
```

</details>

<details><summary><code>/branch</code> usage</summary>

```text
/branch [name]
```

</details>

<details><summary><code>/clear</code> usage</summary>

```text
/clear [name for the session being left]
```

</details>

<details><summary><code>/delete</code> usage</summary>

```text
/delete [session-id | --all]
```

</details>

<details><summary><code>/export</code> usage</summary>

```text
/export [md|json|txt] [path]      default path: ~/.axon/projects/<project>/exports/<session>.<ext>
/export [md|json|txt] clipboard   copy instead of saving
/export                          on a terminal: choose clipboard or file
```

</details>

<details><summary><code>/history</code> usage</summary>

```text
/history [window]
/history all [session-id]   every record of the transcript, rewound and compacted messages included
```

</details>

<details><summary><code>/library</code> usage</summary>

```text
/library                 everything saved for this project: sessions, subagent runs, skills,
                         agents, commands, memory, hooks, plans, todos, file history, outputs
/library <section>       the full list for one section (sessions, subagents, skills, agents,
                         commands, memory, hooks, plans, todos, files, outputs, library, usage)
/library --json          the same as JSON
```

</details>

<details><summary><code>/loop</code> usage</summary>

```text
/loop <N>x <prompt>          run a prompt N times in a row (e.g. /loop 3x run the tests and fix one failure)
/loop <interval> <prompt>    re-run every interval while idle: 30s, 5m, 1h (min 10s)
/loop <interval> <N>x <prompt>   ... at most N times
/loop                         list loops
/loop stop [id]               stop all loops (or one)
```

</details>

<details><summary><code>/rename</code> usage</summary>

```text
/rename [new title]   (without a title one is generated from the conversation)
```

</details>

<details><summary><code>/restore</code> usage</summary>

```text
/restore              list the trash (pick one on a terminal)
/restore <id>         restore a session
/restore --all        restore every trashed session
```

</details>

<details><summary><code>/resume</code> usage</summary>

```text
/resume [session-id or prefix]
```

</details>

<details><summary><code>/rewind</code> usage</summary>

```text
/rewind                     pick a prompt, then restore code and/or conversation (menu)
/rewind to <n> [mode]       modes: code_and_conversation (default), conversation, code, summarize_from_here
/rewind points              list prompts you can rewind to
/rewind <n>                 undo the file edits of the last n turns
/rewind list                show file checkpoints
```

</details>

<details><summary><code>/sessions</code> usage</summary>

```text
/sessions
/sessions delete <id>
/sessions clear
```

</details>

<details><summary><code>/tag</code> usage</summary>

```text
/tag <name> | /tag -
```

</details>

## Context & Cost

| Command | Aliases | Description |
|---|---|---|
| `/autocompact` |  | Set the auto-compact threshold (percent or tokens) |
| `/compact` |  | Summarise older turns to free context (optional focus) |
| `/context` | `/tokens` | Context usage by category; --why explains each block; limit sets the window size |
| `/cost` | `/usage` `/ledger` | Cost by source and model, cache savings, $/turn |
| `/faq` | `/faqs` | AgentRouter FAQ and error troubleshooting |
| `/payload` | `/breakdown` `/input` | Inspect the exact prompt sent to the model |
| `/pricing` | `/prices` | Per-model token prices (per 1M tokens) |
| `/recap` |  | Short summary of the session so far (one LLM call) |
| `/window` |  | Set the sliding context window in turns (0 = unlimited) |

<details><summary><code>/autocompact</code> usage</summary>

```text
/autocompact <percent 10-99>
/autocompact <tokens>   e.g. 500k (fraction of the model's window)
/autocompact auto       back to the default
```

</details>

<details><summary><code>/compact</code> usage</summary>

```text
/compact [instructions for the summary]
```

</details>

<details><summary><code>/context</code> usage</summary>

```text
/context          coloured usage grid + breakdown by category
/context --why    every injected block, its trigger and tokens
/context all      include per-tool token sizes
/context limit    how much of the model's window to use: 128k (default), 256k, 1m or max
```

</details>

<details><summary><code>/cost</code> usage</summary>

```text
/cost            session breakdown
/cost calls      every API call
/cost --project  all sessions of this project (from ~/.axon/usage.jsonl)
/cost --all      every project
```

</details>

<details><summary><code>/faq</code> usage</summary>

```text
/faq [402|400|401|sensitive|backup|credit|gpt6|pricing]
```

</details>

<details><summary><code>/payload</code> usage</summary>

```text
/payload          section summary with token counts
/payload full     full system prompt, tool list and messages
```

</details>

<details><summary><code>/pricing</code> usage</summary>

```text
/pricing [model filter]
```

</details>

<details><summary><code>/window</code> usage</summary>

```text
/window [turns]
```

</details>

## Model & Modes

| Command | Aliases | Description |
|---|---|---|
| `/effort` |  | Reasoning effort: reflex \| balanced \| synapse \| quantum |
| `/endpoint` | `/endpoints` `/base-url` `/baseurl` | Provider base URLs: redirect to a proxy, add custom endpoints, test, reset |
| `/fast` |  | Toggle the small/fast model for this session |
| `/goal` |  | Set a session goal shown in the status and sent to the model |
| `/keys` | `/key` `/apikey` | API keys: add/replace (verified), test, remove, where they are stored |
| `/mode` |  | Permission mode: default \| acceptEdits \| plan \| bypass |
| `/model` |  | Switch model (picker, name, 'random' or 'random:small') |
| `/permissions` | `/perms` `/permission` | Manage allow / ask / deny rules and working directories |
| `/plan` |  | Enter plan mode (read-only); '/plan off' leaves it |
| `/provider` | `/providers` | Connect or switch provider (API key + endpoint) |
| `/thinking` | `/thought` | Toggle streaming of model reasoning |
| `/trace` |  | Trace detail: compact \| normal \| detailed \| debug |

<details><summary><code>/effort</code> usage</summary>

```text
/effort [reflex|balanced|synapse|quantum]   (aliases: low, medium, high, max)
/effort auto      back to the default effort
/effort status    show the current effort
```

</details>

<details><summary><code>/endpoint</code> usage</summary>

```text
/endpoint                                        menu (a list without a terminal)
/endpoint set <provider> <url>                   redirect a provider to another base URL (proxy/gateway)
/endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--use]
/endpoint reset <provider|name>                  preset default URL again / delete a custom endpoint
/endpoint test [provider|url]                    connectivity, latency and model count
/endpoint use <provider|name> [--model M]        switch to it now (also saved as the default)
saved in ~/.axon/config.toml (user scope only)
```

</details>

<details><summary><code>/fast</code> usage</summary>

```text
/fast [on|off]   (uses settings.small_model; set it with smallModel in settings.json)
```

</details>

<details><summary><code>/goal</code> usage</summary>

```text
/goal                 show the goal
/goal <condition>     set it (the model keeps working toward it)
/goal clear           remove it
```

</details>

<details><summary><code>/keys</code> usage</summary>

```text
/keys                                       key manager (menu; a list without a terminal)
/keys set <provider|VAR> [key] [--project]  add or replace a key (hidden prompt when the key is omitted)
/keys test [provider|VAR|all]               live check: latency and model count, or the exact error
/keys remove <provider|VAR> [--project]     delete a key from ~/.axon/.env (or this project's .env)
/keys where                                 where keys are read from, in order
/keys edit [--project]                      open the .env file in $EDITOR
flags: --no-verify saves without a live check · -y skips confirmations
```

</details>

<details><summary><code>/mode</code> usage</summary>

```text
/mode [default|acceptEdits|plan|bypass]   (Shift+Tab cycles)
```

</details>

<details><summary><code>/model</code> usage</summary>

```text
/model                 interactive picker
/model <name>          switch (endpoint follows the model's provider)
/model random[:small]  surprise me
```

</details>

<details><summary><code>/permissions</code> usage</summary>

```text
/permissions                          interactive panel (Allow / Ask / Deny / Directories tabs); list without a terminal
/permissions list                     list mode and rules
/permissions allow <rule>             add an allow rule (saved to .axon/settings.local.json)
/permissions ask <rule>               always ask before matching calls (beats allow rules and bypass)
/permissions deny <rule>              add a deny rule
/permissions remove <allow|ask|deny> <rule>
/permissions session <allow|ask|deny> <rule>   rule for this session only
Rule examples: Bash(git status:*)  Bash(npm run *)  Edit(src/**)  Read(~/.ssh/**)  WebFetch(domain:docs.python.org)  mcp__github
```

</details>

<details><summary><code>/plan</code> usage</summary>

```text
/plan              enter plan mode
/plan <task>       enter plan mode and start planning the task
/plan off          back to default mode
```

</details>

<details><summary><code>/thinking</code> usage</summary>

```text
/thinking [on|off]
```

</details>

<details><summary><code>/trace</code> usage</summary>

```text
/trace [compact|normal|detailed|debug]
/trace last       same as /inspect
```

</details>

## Code & Review

| Command | Aliases | Description |
|---|---|---|
| `/btw` | `/ptw` | Quick side question, kept out of the conversation |
| `/copy` | `/cp` | Copy an answer (N = Nth latest), its code blocks, or the git diff |
| `/diff` |  | Uncommitted changes, or edits since checkpoint T<n> |
| `/find` | `/fuzzy` | Fuzzy-find a workspace file (Ctrl+P while typing) |
| `/output` | `/expand` `/view` `/more` | Full output of a tool call (/output 7), the last one, or a workspace file |
| `/paste` | `/multiline` | Multi-line input: paste text, finish with END |
| `/queue` | `/q` | Queue prompts to run after the current turn |
| `/review` | `/code-review` | Code review of the current changes (or a focus area) |
| `/security-review` |  | Security review of the branch's changes vs its base |
| `/simplify` |  | Simplify the changed code (reuse, dead code, efficiency) and apply fixes |
| `/test` | `/tests` | Detect and run the project's test suite |
| `/todos` | `/todo` | Show the agent's task checklist |
| `/transcript` | `/tr` `/scrollback` | Full-screen transcript: click or Enter expands a tool call's full output (Ctrl+F) |

<details><summary><code>/btw</code> usage</summary>

```text
/btw <question>
```

</details>

<details><summary><code>/copy</code> usage</summary>

```text
/copy            last answer
/copy <N>        Nth-latest answer (1 = last)
/copy code [N]   code blocks of an answer (picker when there are several)
/copy diff       git diff
```

</details>

<details><summary><code>/diff</code> usage</summary>

```text
/diff            working tree vs HEAD
/diff staged     staged changes
/diff T<n>       file edits made since checkpoint n (see /rewind list)
```

</details>

<details><summary><code>/find</code> usage</summary>

```text
/find [query]
```

</details>

<details><summary><code>/output</code> usage</summary>

```text
/output [n | list | path] [pager]
/output list saved   tool outputs recorded in this session (also after a resume)
```

</details>

<details><summary><code>/queue</code> usage</summary>

```text
/queue                show queue
/queue <prompt>       add
/queue drop <id>      remove
/queue run            run the next item now
/queue clear
```

</details>

<details><summary><code>/review</code> usage</summary>

```text
/review [low|medium|high|max] [--fix] [--comment] [--max-findings N|all] [focus | file | PR number]
```

</details>

<details><summary><code>/security-review</code> usage</summary>

```text
/security-review [focus]
```

</details>

<details><summary><code>/simplify</code> usage</summary>

```text
/simplify [focus]
```

</details>

<details><summary><code>/test</code> usage</summary>

```text
/test [extra args]
```

</details>

<details><summary><code>/transcript</code> usage</summary>

```text
/transcript   (keys: click/enter expand · e/c all · t turns · / search · n/N · y copy · q close)
```

</details>

## Agents, Skills & Tools

| Command | Aliases | Description |
|---|---|---|
| `/agents` |  | List, show, create or edit subagent definitions |
| `/debug` |  | Turn on debug tracing and have the model diagnose the last turn |
| `/explain` |  | Plain-language summary of a turn (no LLM call) |
| `/hooks` |  | Configured lifecycle hooks (PreToolUse, Stop, …) |
| `/inspect` |  | Re-print the detailed timeline of a turn (default: last) |
| `/main` | `/root` `/parent` | Return from a subagent transcript to the main session |
| `/mcp` |  | MCP servers: list, add, remove, connect, tools, prompts |
| `/plugin` | `/plugins` | Plugins bundling commands, skills, agents, hooks and MCP |
| `/reload-plugins` | `/reload` | Re-scan plugins, custom commands and skills |
| `/skill-doctor` | `/skills-doctor` | Token cost, usage and problems of every skill |
| `/skills` | `/skill` | Browse, create, install and manage skills |
| `/subagents` | `/sub` `/subagent` | Open a subagent's transcript from this session as a chat |
| `/tasks` | `/bashes` `/jobs` | Background shells, background turns and subagent tasks |
| `/tools` | `/tool` | Tools the model can use and their permission |

<details><summary><code>/agents</code> usage</summary>

```text
/agents                         list built-in and custom agents
/agents show <name>
/agents create [name] [--global] [description]   (wizard on a terminal)
/agents edit <name>
/agents delete <name>
```

</details>

<details><summary><code>/debug</code> usage</summary>

```text
/debug [what went wrong]
```

</details>

<details><summary><code>/explain</code> usage</summary>

```text
/explain [n|last]
```

</details>

<details><summary><code>/inspect</code> usage</summary>

```text
/inspect [n|last]
```

</details>

<details><summary><code>/mcp</code> usage</summary>

```text
/mcp [list|add|remove|enable|disable|connect|disconnect|tools|resources|prompts|logs|help]
```

</details>

<details><summary><code>/plugin</code> usage</summary>

```text
/plugin                         list
/plugin info <name>
/plugin create <name> [--global]
/plugin install <folder | https git URL> [--project]
/plugin enable|disable|remove <name>
```

</details>

<details><summary><code>/skills</code> usage</summary>

```text
/skills                         list
/skills show <name>
/skills create <name> [template] [--global]
/skills install <github-url> [--global] [--force]
/skills edit <name> · /skills delete <name> · /skills reload
```

</details>

<details><summary><code>/subagents</code> usage</summary>

```text
/subagents [n | main | queue]
```

</details>

<details><summary><code>/tasks</code> usage</summary>

```text
/tasks                   background shells, background turns (Ctrl+B) and subagent tasks
/tasks log <id>          last output lines of a shell (bash_1) or background turn
/tasks kill <id>         stop a background shell
```

</details>

<details><summary><code>/tools</code> usage</summary>

```text
/tools [name]
```

</details>

## Memory & Project

| Command | Aliases | Description |
|---|---|---|
| `/add-dir` |  | Allow another directory for reading and editing |
| `/cd` |  | Move the session to another workspace directory |
| `/import` |  | Import rules and MCP servers from Cursor, Gemini CLI or Codex |
| `/init` |  | Create AXON.md from a repo analysis (no overwrite) |
| `/learn` | `/remember` | Save a memory (same as /memory add) |
| `/memory` | `/mem` | Instruction files (AXON.md …) and saved memories |
| `/optimize` | `/enhance` | Rewrite a rough request into a precise agent prompt |
| `/prompt` | `/prompts` `/template` `/templates` | Prompt library: templates with {placeholders} |

<details><summary><code>/add-dir</code> usage</summary>

```text
/add-dir                     list extra working directories
/add-dir <path> [--save]     allow reading/editing <path> this session (--save: .axon/settings.local.json)
/add-dir remove <path>       stop allowing it
```

</details>

<details><summary><code>/cd</code> usage</summary>

```text
/cd <directory>    (project memory, permissions, skills and session storage follow)
```

</details>

<details><summary><code>/import</code> usage</summary>

```text
/import <cursor|gemini|codex|all> [--dry-run]
  cursor: .cursorrules, .cursor/rules/*.mdc (globs become path-scoped rules), .cursor/mcp.json
  gemini: GEMINI.md, ~/.gemini/GEMINI.md, .gemini/settings.json and ~/.gemini/settings.json MCP servers
  codex:  ~/.codex/AGENTS.md / instructions.md, ~/.codex/config.toml [mcp_servers]
```

</details>

<details><summary><code>/init</code> usage</summary>

```text
/init            create AXON.md (or suggest improvements if it exists)
/init local      create a personal, git-ignored AXON.local.md
/init --force    regenerate AXON.md after confirmation
```

</details>

<details><summary><code>/learn</code> usage</summary>

```text
/learn [--global] <rule or fact>
```

</details>

<details><summary><code>/memory</code> usage</summary>

```text
/memory                       list instruction files and saved memories
/memory add [--global] <text>  save a memory (LLM-distilled)
/memory edit <user|project|local|n|id> [new text]
/memory view <n|id>            /memory remove <n|id>
/memory search <text>          /memory clear
#<text>                        quick note (no LLM)
```

</details>

<details><summary><code>/optimize</code> usage</summary>

```text
/optimize <rough request>
```

</details>

<details><summary><code>/prompt</code> usage</summary>

```text
/prompt                         list templates
/prompt <name> [key=value ...]  fill placeholders (asks for missing ones) and send
/prompt show <name>
/prompt save <name> [--global] <template text>
```

</details>

## System

| Command | Aliases | Description |
|---|---|---|
| `/bug` | `/feedback` | Save a bug report with diagnostics |
| `/color` | `/colour` | Colour of the prompt bar for this session |
| `/config` | `/set` | Settings panel, or show/change one setting |
| `/doctor` |  | Diagnose installation, keys, tools and environment |
| `/env` | `/environment` | OS, Python, shell, paths and toolchains |
| `/focus` |  | Focus view: only prompts, one-line tool summaries and answers |
| `/help` |  | List commands, or details for one command |
| `/keybindings` | `/shortcuts` `/kb` | Keyboard shortcuts in effect (remap in ~/.axon/keybindings.json) |
| `/notify` |  | Desktop notifications: when they fire (important/all/off), or send a test |
| `/output-style` | `/output-styles` `/style` | Choose how answers are written (default, explanatory, learning, concise, custom) |
| `/release-notes` | `/changelog` `/whats-new` | What's new in this and earlier Axon versions |
| `/settings` | `/preferences` `/prefs` `/options` | All settings in one place: model, keys, endpoint, permissions, display, cost, data |
| `/status` |  | Version, model, mode, trace, session, cost, storage |
| `/statusbar` |  | Show the live status bar, or toggle it |
| `/statusline` |  | Custom status line command under the prompt |
| `/terminal-setup` |  | Set up Shift+Enter for newlines in your terminal |
| `/theme` |  | Colour theme: auto \| dark \| light \| high-contrast \| mono |
| `/vim` |  | Toggle vim key bindings in the prompt editor |

<details><summary><code>/bug</code> usage</summary>

```text
/bug <what went wrong>
```

</details>

<details><summary><code>/color</code> usage</summary>

```text
/color [default|red|orange|yellow|green|cyan|blue|purple|pink|gray]
```

</details>

<details><summary><code>/config</code> usage</summary>

```text
/config                          interactive panel (list without a terminal)
/config <key> <value> [--save]
/config <key>=<value>
```

</details>

<details><summary><code>/doctor</code> usage</summary>

```text
/doctor                 installation, keys, tools, terminal
/doctor prompt-audit    conflicting, duplicated or stale instructions across AXON.md, rules, skills, agents
```

</details>

<details><summary><code>/focus</code> usage</summary>

```text
/focus [on|off]   (switches the trace level to compact and back)
```

</details>

<details><summary><code>/help</code> usage</summary>

```text
/help [command]
```

</details>

<details><summary><code>/notify</code> usage</summary>

```text
/notify [important|all|off|after <seconds>|test [message]]
```

</details>

<details><summary><code>/output-style</code> usage</summary>

```text
/output-style                  list styles (picker on a terminal)
/output-style <name>           switch style (saved to ~/.axon/settings.json)
/output-style new <name> [description]   create ~/.axon/output-styles/<name>.md (--project for .axon/)
```

</details>

<details><summary><code>/release-notes</code> usage</summary>

```text
/release-notes [version|all]
```

</details>

<details><summary><code>/settings</code> usage</summary>

```text
/settings                       every option, grouped (menu on a terminal)
/settings <section>             one section: model, permissions, display, context, data, shell
/settings <key> <value>         change one option (e.g. /settings tips off)
```

</details>

<details><summary><code>/statusbar</code> usage</summary>

```text
/statusbar [toggle]
```

</details>

<details><summary><code>/statusline</code> usage</summary>

```text
/statusline                 show the current status line setting
/statusline <command>       run <command> for the status line (session JSON on stdin)
/statusline default         install a ready-made script (~/.axon/statusline.py)
/statusline off             remove the custom status line
```

</details>

<details><summary><code>/terminal-setup</code> usage</summary>

```text
/terminal-setup [vscode|cursor|iterm2|windows-terminal|wezterm|ghostty|alacritty|kitty|apple-terminal]
```

</details>

<details><summary><code>/theme</code> usage</summary>

```text
/theme [auto|dark|light|high-contrast|mono]
```

</details>

<details><summary><code>/vim</code> usage</summary>

```text
/vim [on|off]
```

</details>

## Code

| Command | Aliases | Description |
|---|---|---|
| `/index` |  | Build or refresh the incremental symbol index |
| `/map` | `/repomap` | Ranked repository map: files and their key symbols |

<details><summary><code>/index</code> usage</summary>

```text
/index [--rebuild]
```

</details>

<details><summary><code>/map</code> usage</summary>

```text
/map [path] [--tokens N]
```

</details>

## Getting started

| Command | Aliases | Description |
|---|---|---|
| `/new` | `/scaffold` | Create a runnable starter project from a template (list with /new) |
| `/next` | `/suggest` | Suggested next steps for the last turn (verify, fix, commit, compact...) |
| `/setup` | `/onboard` | Guided setup: provider, API key (verified), model, permission mode, trace level |
| `/tips` |  | Usage tips for what you are doing (model-written, grounded in the command list) |

<details><summary><code>/new</code> usage</summary>

```text
/new [<template> [name]] [--force]
```

</details>

<details><summary><code>/next</code> usage</summary>

```text
/next [n]
```

</details>

<details><summary><code>/setup</code> usage</summary>

```text
/setup [--provider P] [--model M] [--mode X] [--trace L]
```

</details>

<details><summary><code>/tips</code> usage</summary>

```text
/tips                list the current tips
/tips refresh        write fresh tips now
/tips off|on         hide / show tips
/tips llm off|on     registry-only / model-written tips
```

</details>

## Git

| Command | Aliases | Description |
|---|---|---|
| `/changes` |  | Files changed this session with +/- line counts |
| `/commit` |  | Generate a Conventional Commit message for staged changes and commit (confirmed) |
| `/git` | `/gs` | Git dashboard: branch, ahead/behind, change counts, recent commits, stashes |
| `/pr` |  | Summarize the branch vs base and open a PR with gh (confirmed) |

<details><summary><code>/commit</code> usage</summary>

```text
/commit [hint]
```

</details>

<details><summary><code>/pr</code> usage</summary>

```text
/pr [base]
```

</details>

## Insight

| Command | Aliases | Description |
|---|---|---|
| `/budget` |  | Show or set this session's spending limit |
| `/insights` |  | Ask the analytics subagent to explain your spending and suggest savings |
| `/report` |  | Write a self-contained HTML usage dashboard |
| `/stats` | `/analytics` | Usage analytics: cost, tokens, cache, models, sources, tools, expensive turns |

<details><summary><code>/budget</code> usage</summary>

```text
/budget [amount|off]
```

</details>

<details><summary><code>/insights</code> usage</summary>

```text
/insights [today|7d|30d|all] [--project|--all-projects]
```

</details>

<details><summary><code>/report</code> usage</summary>

```text
/report [--days N] [--out PATH] [--all-projects] [--open]
```

</details>

<details><summary><code>/stats</code> usage</summary>

```text
/stats [today|7d|30d|all] [--project|--all-projects]
```

</details>

## Integrations

| Command | Aliases | Description |
|---|---|---|
| `/serve` |  | Show how to run the local Axon HTTP/SSE API (axon serve) |

<details><summary><code>/serve</code> usage</summary>

```text
/serve [port]
```

</details>

## Solve

| Command | Aliases | Description |
|---|---|---|
| `/autofix` |  | Verify-and-fix loop until checks pass (default 3 attempts) |
| `/fix` |  | Error Doctor: run a command or parse pasted errors and have the agent fix them |
| `/plan-resume` |  | Continue the unfinished steps of a saved plan |
| `/plan-run` | `/solve` | Plan -> approve -> execute: model drafts steps, you review, agent runs each with a checklist |
| `/plans` |  | List saved plans for this workspace |
| `/verify` |  | Run the project's tests/lint/typecheck and show failures with file:line |

<details><summary><code>/autofix</code> usage</summary>

```text
/autofix [command] [--max N]
```

</details>

<details><summary><code>/fix</code> usage</summary>

```text
/fix [command | pasted error]
```

</details>

<details><summary><code>/plan-resume</code> usage</summary>

```text
/plan-resume [id]
```

</details>

<details><summary><code>/plan-run</code> usage</summary>

```text
/plan-run <task> [--yes]
```

</details>

<details><summary><code>/plans</code> usage</summary>

```text
/plans [n]
```

</details>

<details><summary><code>/verify</code> usage</summary>

```text
/verify [check names|all]
```

</details>
<!-- END GENERATED: commands-reference -->

## Input shortcuts

| Input | Effect |
|---|---|
| `?` | Keyboard shortcuts |
| `@path` | Attach a file or image (`Ctrl+P` fuzzy finder) |
| `!command` | Run a shell command and add its output to the conversation |
| `# note` / `#~ note` | Save a project memory, or a memory for all projects |
| `Shift+Tab` | Cycle permission mode |
| `Ctrl+O` | Cycle trace level |
| `Ctrl+B` | Move the running turn to the background (`/tasks`) |
| `Ctrl+G` | Edit the prompt in `$EDITOR` |
| `Ctrl+R` | Reverse-search prompt history |
| `Esc Esc` | Clear the draft, or open `/rewind` |
| Typing during a turn | Steers the running turn: the message is delivered with the next tool results |

## Command line

```text
axon [options] [prompt]                 interactive session (prompt optional)
axon -p "<prompt>" [--output-format text|json|stream-json]
axon setup [--provider P --model M --mode X --trace L]
axon doctor
axon mcp <list|add|add-json|remove|enable|disable|tools|prompts|logs|...>
axon stats [today|7d|30d|all]
axon report [--days N] [--out PATH] [--all-projects] [--open]
axon serve [--port 8765] [--host 127.0.0.1] [--token T] [--workspace DIR] [--mode M]
axon map [path]
axon new <template> <dir>
axon purge [project|caches|all] [--yes]
```

In print mode (`-p`), a prompt can be a slash command. The process exits with status 1 when the
command fails. Piped standard input is placed before the prompt:
`git diff | axon -p "review this"`.

<!-- BEGIN GENERATED: cli-help -->
```text
usage: axon [-h] [-v] [-p] [--output-format {text,json,stream-json}] [--model MODEL]
            [--context-limit SIZE] [--provider PROVIDER] [--base-url BASE_URL] [--mode MODE]
            [--effort {reflex,balanced,synapse,quantum,low,medium,high,xhigh,max}]
            [--workspace WORKSPACE] [-c] [-r [RESUME_ID]] [--fork-session] [-n SESSION_NAME]
            [--trace {compact,normal,detailed,debug}] [--max-budget-usd MAX_BUDGET_USD]
            [--settings SETTINGS_FILE] [--max-turns MAX_TURNS] [--allowedTools RULE [RULE ...]]
            [--disallowedTools RULE [RULE ...]] [--append-system-prompt APPEND_SYSTEM_PROMPT]
            [--verbose] [--no-thinking] [--dangerously-skip-permissions] [--add-dir DIR]
            [--system-prompt SYSTEM_PROMPT] [--system-prompt-file SYSTEM_PROMPT_FILE]
            [--append-system-prompt-file APPEND_SYSTEM_PROMPT_FILE] [--agent AGENT_NAME]
            [--agents AGENTS_JSON] [--tools TOOLS] [--mcp-config FILE_OR_JSON [FILE_OR_JSON ...]]
            [--strict-mcp-config] [--fallback-model FALLBACK_MODEL] [--debug [FILTER]]
            [--debug-file DEBUG_FILE] [-w [NAME]]
            [prompt ...]

A terminal-native agentic coding assistant.

positional arguments:
  prompt                Prompt to run (starts the REPL when omitted)

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  -p, --print           Run one prompt non-interactively and exit
  --output-format {text,json,stream-json}
                        Print-mode output format
  --model MODEL         Model to use
  --context-limit SIZE  How much of the model's context window to use: 128k (default), 256k, 1m or
                        max
  --provider PROVIDER   Provider preset id (e.g. anthropic, openai, openrouter, ollama)
  --base-url BASE_URL   Provider API base URL
  --mode, --permission-mode MODE
                        Permission mode: default, acceptEdits, plan, bypass (also
                        bypassPermissions, manual)
  --effort {reflex,balanced,synapse,quantum,low,medium,high,xhigh,max}
                        Reasoning effort tier
  --workspace WORKSPACE
                        Workspace root directory
  -c, --continue        Resume the latest session
  -r, --resume [RESUME_ID]
                        Resume a session by ID or name (no value: open the session picker)
  --fork-session        With --continue/--resume: copy the history to a new session
  -n, --name SESSION_NAME
                        Name the session
  --trace {compact,normal,detailed,debug}
                        Trace detail level
  --max-budget-usd MAX_BUDGET_USD
                        Session budget guard in USD
  --settings SETTINGS_FILE
                        Extra settings JSON file or inline JSON (highest file layer)
  --max-turns MAX_TURNS
                        Maximum agent iterations per prompt
  --allowedTools, --allowed-tools RULE [RULE ...]
                        Tools/rules to allow without asking, e.g. "Read" "Bash(git *)"
  --disallowedTools, --disallowed-tools RULE [RULE ...]
                        Tools to remove or rules to deny, e.g. "WebFetch" "Bash(rm *)"
  --append-system-prompt APPEND_SYSTEM_PROMPT
                        Extra text appended to the system prompt
  --verbose             Verbose output (hook warnings, full events)
  --no-thinking         Disable extended reasoning
  --dangerously-skip-permissions
                        Run every tool without asking (forces bypass mode)
  --add-dir DIR         Extra directory the session may read and edit (repeat for more)
  --system-prompt SYSTEM_PROMPT
                        Replace Axon's core system prompt
  --system-prompt-file SYSTEM_PROMPT_FILE
                        Read the replacement system prompt from a file
  --append-system-prompt-file APPEND_SYSTEM_PROMPT_FILE
                        File whose text is appended to the system prompt (before --append-system-
                        prompt)
  --agent AGENT_NAME    Run the main session as this subagent definition
  --agents AGENTS_JSON  JSON object of session-scoped subagent definitions
  --tools TOOLS         Restrict built-in tools: "Bash,Edit,Read", "" for none, "default" for all
  --mcp-config FILE_OR_JSON [FILE_OR_JSON ...]
                        Load MCP servers from JSON files or inline JSON strings
  --strict-mcp-config   Only use servers from --mcp-config
  --fallback-model FALLBACK_MODEL
                        Comma list of models to fail over to on 429/5xx/overload
  --debug [FILTER]      Debug log to ~/.axon/debug/<session>.log; optional filter e.g. "api,hooks"
                        or "!mcp"
  --debug-file DEBUG_FILE
                        Write the debug log to this file
  -w, --worktree [NAME]
                        Run the session in a git worktree (created or reused)

Subcommands: `axon mcp <list|add|remove|...>` MCP servers, `axon doctor` checks your setup, `axon
setup` guided provider setup, `axon purge` deletes stored sessions/traces, `axon new <template>`
starter project, `axon map` repository map, `axon stats` / `axon report` usage analytics, `axon
serve` local HTTP/SSE API, `axon activity` the journal of everything you did, `axon projects`
every project and its data folder, `axon keys` API keys (add, replace, test, remove), `axon
endpoint` base URLs and custom endpoints.
```
<!-- END GENERATED: cli-help -->
