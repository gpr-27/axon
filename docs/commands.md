# Command reference

Slash commands are typed inside a session. `/help` lists them, `/help <command>` shows usage, and
`Tab` completes names. Besides the built-ins below, these also appear as slash commands:

- **Custom commands**: Markdown files in `.axon/commands/` or `.claude/commands/`
- **Skills**: `/<skill>` or `/skill:<name>`
- **Plugin commands**: `/<plugin>:<name>`
- **MCP prompts**

A custom command whose name collides with a built-in stays reachable as `/cmd:<name>`; a skill with the same name as a custom command wins (the file stays reachable as `/cmd:<name>`).

## Menus: nothing to type

In a terminal, every command run with no arguments is driven with the keys (`↑↓` move, `Enter` select, `Esc` back, `/` filter a long list):

- `/menu` (also `/help`, or `Alt+H` at the prompt) lists every command by category; `Enter` opens the command and its own menu, `Esc` comes back to the list.
- Commands that show something (`/status`, `/cost`, `/context`, `/doctor`, ...) print their report, then ask **What next?** with the related views and actions.
- Commands that do something (`/clear`, `/compact`, `/commit`, ...) say in one line what they do, then offer **Run it**, the choices their arguments offer (a question appears for any text they need) and **Cancel**.
- `/tools` lists every tool with its permission; `Enter` shows the full description and all parameters, with *Always allow / Ask each time / Deny / Remove my rule*. `/permissions` lists the rules; pick one to change or remove it, or *Add a rule* (tool, allow/ask/deny, where to save).

Every menu entry runs the same handler as the typed command (`/cost calls` and the menu's *Every API call* are the same call). Typed arguments, scripts and `axon -p` keep the plain text output, including its "type this next" hint lines.

The tables below are generated from the command registry by `scripts/gen_docs.py`.

<!-- BEGIN GENERATED: commands-reference -->
144 built-in commands in 13 groups.

Contents: [Session](#session) · [Context & Cost](#context--cost) · [Model & Modes](#model--modes) · [Code & Review](#code--review) · [Agents, Skills & Tools](#agents-skills--tools) · [Memory & Project](#memory--project) · [System](#system) · [Code](#code) · [Getting started](#getting-started) · [Git](#git) · [Insight](#insight) · [Integrations](#integrations) · [Solve](#solve)

## Session

| Command | Aliases | Description |
|---|---|---|
| `/activity` | `/journal` | Everything you and Axon did: prompts, commands, shell, tools, settings, errors |
| `/bookmark` |  | Bookmark the latest reply, so you can find it again (the app lists it too) |
| `/bookmarks` |  | List this chat's bookmarks, show one in full, or remove one |
| `/branch` | `/fork` | Fork the conversation into a new session |
| `/clear` | `/reset` | Start a fresh session (the old one stays resumable) |
| `/delete` |  | Delete a saved session (picker, id, or --all) |
| `/exit` | `/quit` | Save the session and quit |
| `/export` |  | Export the conversation (Markdown, web page, PDF, JSON, text) to a file or the clipboard |
| `/files` | `/attachments` | Files, images and documents you gave this session, and where each is saved |
| `/history` |  | List the messages in the conversation |
| `/library` | `/saved` | Everything saved: sessions, subagent runs, skills, agents, memory, hooks, plans |
| `/loop` | `/repeat` | Repeat a prompt N times or on an interval |
| `/overview` |  | This project at a glance: files, git, tests, spend this week, last chats, memory |
| `/project` | `/where` | Current project: name, path, data folder, sessions, journal, memory |
| `/rename` | `/title` | Rename the current session (no name: generate one) |
| `/restore` | `/trash` | List deleted sessions (the trash) and restore them |
| `/resume` | `/continue` | Resume a previous session of this workspace |
| `/retitle` |  | Give past chats short AI-written titles |
| `/retry` |  | Retry the last turn that failed with a provider error |
| `/rewind` | `/undo` `/checkpoint` | Restore code and/or conversation to an earlier prompt |
| `/search` |  | Full-text search of this project's session transcripts (/find searches file names) |
| `/sessions` | `/session` | List saved sessions with tokens and cost |
| `/star` | `/fav` | Toggle the favourite star on this session |
| `/tag` |  | Tag the current session (use '-' to clear) |
| `/workbench` | `/panel` | One screen: environment, context, usage, connectors, changes, tasks, plan |

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

<details><summary><code>/bookmark</code> usage</summary>

```text
/bookmark [note]   marks the latest reply; /bookmarks lists them
```

</details>

<details><summary><code>/bookmarks</code> usage</summary>

```text
/bookmarks               list
/bookmarks show <n>      print the message in full
/bookmarks remove <n>    remove a bookmark
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
/export [md|html|pdf|json|txt] [path]   default path: ~/.axon/projects/<project>/exports/<session>.<ext>
/export [md|json|txt] clipboard   copy instead of saving
/export html|pdf                 one file with secrets removed (pdf: open it in a browser and print)
/export <format> --no-tools      leave out tool calls and results
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

<details><summary><code>/overview</code> usage</summary>

```text
/overview          files and languages, git and recent commits, the test command and its last result, spend this week, last chats, memory notes
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

<details><summary><code>/retitle</code> usage</summary>

```text
/retitle [all]   (no argument: this chat; all: every past chat still named after its first words)
```

</details>

<details><summary><code>/rewind</code> usage</summary>

```text
/rewind                     pick a prompt, then restore code and/or conversation (menu)
/rewind to <n> [mode] [--overwrite]   modes: code_and_conversation (default), conversation, code, summarize_from_here
/rewind points              list prompts you can rewind to
/rewind list                the file checkpoints: time, prompt, files, +/- lines
/rewind show <n>            what checkpoint n changed
/rewind restore <n> [--keep-mine|--overwrite] [--yes]   put the files back to before checkpoint n (undo it by restoring the new checkpoint)
/rewind <n> [--overwrite]   undo the file edits of the last n turns
```

</details>

<details><summary><code>/search</code> usage</summary>

```text
/search <text>          this project
/search --all <text>    every project
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

<details><summary><code>/workbench</code> usage</summary>

```text
/workbench          one screen: environment, context, usage, connectors, changes, background tasks, plan and todos
                   keys e c u k g b p jump to a part, Enter opens it in detail, r refreshes, Esc closes
/workbench --plain print it as text (also what you get without a terminal)
/workbench hint on|off   show a short /workbench pointer in the status line (off by default)
```

</details>

## Context & Cost

| Command | Aliases | Description |
|---|---|---|
| `/autocompact` |  | Set the auto-compact threshold (percent or tokens) |
| `/compact` |  | Summarise older turns to free context (optional focus) |
| `/context` | `/tokens` | Context usage by category; --why explains each block; limit sets the window size |
| `/cost` | `/ledger` | Cost by source and model, cache savings, $/turn |
| `/ctxdiff` |  | What entered and left the prompt between two model calls, and why the cache missed |
| `/ctxmap` |  | What fills the context window now, and what could be dropped |
| `/faq` | `/faqs` | Help for the current provider; AgentRouter's FAQ with /faq agentrouter |
| `/payload` | `/breakdown` `/input` | Inspect the exact prompt sent to the model |
| `/pin` |  | Keep a tool result in context in full (never trimmed or compacted away) |
| `/pricing` | `/prices` | Per-model token prices (per 1M tokens) |
| `/recap` |  | Short summary of the session so far (one LLM call) |
| `/receipts` |  | Claims in the last answer (tests pass, no other callers...) matched to the tool calls behind them |
| `/timeline` |  | Steps of a turn as a bar chart: time, tokens, cost, cache-miss cause |
| `/trail` |  | Decision trail of a tool call: why, what allowed it, result, what came next |
| `/unpin` |  | Release pinned tool results |
| `/usage` | `/limits` | One screen: status, config, usage and limits, stats, context |
| `/whatif` |  | Reprice this session's tokens at other models' prices, with and without caching |
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
/compact --last   what the last compaction replaced, with its summary
```

</details>

<details><summary><code>/context</code> usage</summary>

```text
/context          coloured usage grid + breakdown by category
/context --why    every injected block, its trigger and tokens
/context all      include per-tool token sizes
/context limit    how much of the model's window to use: auto (default: the model's full window), 128k, 256k or 1m
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

<details><summary><code>/ctxdiff</code> usage</summary>

```text
/ctxdiff [n]   compare model call n with call n-1 (default: the last two calls)
```

</details>

<details><summary><code>/ctxmap</code> usage</summary>

```text
/ctxmap              items by size with share, and what can be dropped
/ctxmap drop <id>     drop a tool result from context (full text stays on disk)
```

</details>

<details><summary><code>/faq</code> usage</summary>

```text
/faq [agentrouter | 402|400|401|sensitive|backup|credit|gpt6|pricing]
```

</details>

<details><summary><code>/payload</code> usage</summary>

```text
/payload          section summary with token counts (a menu to expand one in a terminal)
/payload full     full system prompt, tool list and messages
/payload system|tools|history|last   one section in full
```

</details>

<details><summary><code>/pin</code> usage</summary>

```text
/pin                 list the pins and their token total
/pin <path|id>        pin the result of a file the model read, or a tool-result id
```

</details>

<details><summary><code>/pricing</code> usage</summary>

```text
/pricing [model filter]
```

</details>

<details><summary><code>/timeline</code> usage</summary>

```text
/timeline [turn]   default: the last turn
```

</details>

<details><summary><code>/trail</code> usage</summary>

```text
/trail [n]   tool call number n of this session (default: the last one)
```

</details>

<details><summary><code>/unpin</code> usage</summary>

```text
/unpin               release every pin
/unpin <path|id>      release one
```

</details>

<details><summary><code>/usage</code> usage</summary>

```text
/usage            tabbed screen (←/→ switch tabs, Esc closes); plain text without a terminal
/usage status|config|usage|stats|context   open on one tab
/usage --plain   print every tab as text
```

</details>

<details><summary><code>/whatif</code> usage</summary>

```text
/whatif [model]   price the recorded tokens at a model (or a name prefix such as gpt-5); /whatif all lists every priced model
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
/endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--import] [--use]
/endpoint reset <provider|name>                  preset default URL again / delete a custom endpoint
/endpoint test [provider|url] [--model M]        connectivity, latency, model count and tool-call support
/endpoint native-tools <name> true|false         false: the gateway drops `tools` (tools go in the prompt)
/endpoint use <provider|name> [--model M]        switch to it now (also saved as the default)
/endpoint models <name> [import|add <model>...|clear]   list, import from its /models, add or forget models
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
| `/worktree` | `/wt` `/worktrees` | Git worktrees: list with changes, switch, create, remove |

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
/diff            working tree vs HEAD (terminal: files, inline diffs; 1-4 switch compare target)
/diff staged|commit|branch <name>   open on another compare target
/diff list       the changed files as text
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
/review hunks [accept <id|all> | reject <id>]   edits made this session, hunk by hunk
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

<details><summary><code>/worktree</code> usage</summary>

```text
/worktree                 worktrees with branch and change counts (terminal: pick one to switch)
/worktree new <name>      create one on branch axon/<name> from HEAD
/worktree switch <name>   move this session into a worktree (same as /cd <path>)
/worktree remove <name>   remove one (refuses one with uncommitted changes, the main one and the current one)
```

</details>

## Agents, Skills & Tools

| Command | Aliases | Description |
|---|---|---|
| `/agents` |  | List, show, create or edit subagent definitions |
| `/debug` |  | Turn on debug tracing and have the model diagnose the last turn |
| `/explain` |  | Plain-language summary of a turn (no LLM call) |
| `/hooks` |  | Browse configured lifecycle hooks (PreToolUse, Stop, ...) |
| `/inspect` |  | Re-print the detailed timeline of a turn (default: last) |
| `/list-agents` | `/peers` | List the subagents of this session, with their status and tokens |
| `/main` | `/root` `/parent` | Return from a subagent transcript to the main session |
| `/marketplace` | `/connectors` `/store` | Browse, install and control connectors (MCP), skills and plugins |
| `/mcp` |  | MCP servers: list, add, remove, connect, tools, prompts |
| `/plugin` | `/plugins` | Plugins bundling commands, skills, agents, hooks and MCP |
| `/reload-plugins` | `/reload` | Re-scan plugins, custom commands and skills |
| `/reload-skills` |  | Re-scan skill and command directories without restarting |
| `/skill-doctor` | `/skills-doctor` | Token cost, usage and problems of every skill |
| `/skills` | `/skill` | Browse, create, install and manage skills |
| `/stop` |  | Stop running subagents (their transcripts are kept) |
| `/subagents` | `/sub` `/subagent` | Open a subagent's transcript from this session as a chat |
| `/subtask` |  | Fork the conversation into a background subagent; the result comes back here |
| `/tasks` | `/bashes` `/jobs` | Background shells, background turns and subagent tasks |
| `/tell` | `/msg` | Send a message to a running subagent; it reads it before its next step |
| `/tools` | `/tool` | Tools the model can use and their permission |
| `/trust` |  | Review and allow project hooks, plugins and MCP servers for this folder |

<details><summary><code>/agents</code> usage</summary>

```text
/agents                         list built-in and custom agents
/agents show <name>
/agents check                   files that were skipped or have unrecognized fields
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

<details><summary><code>/hooks</code> usage</summary>

```text
/hooks                  list every configured hook, grouped by event
/hooks <n>              full text and origin of hook number n
/hooks <Event>          hooks of one event with what it can do
/hooks events           all events (also those with no hook) and their matcher
/hooks reload           re-read the settings files
```

</details>

<details><summary><code>/inspect</code> usage</summary>

```text
/inspect [n|last]
```

</details>

<details><summary><code>/marketplace</code> usage</summary>

```text
/marketplace                          browse (terminal) or list the connectors
/marketplace list|search|info|categories      find connectors, skills and plugins
/marketplace install|remove <id> · add <url|command> · create-skill · create-plugin
/marketplace enable|disable <id>     switch a connector, skill or plugin on or off
/marketplace status [--check] [--all]  everything installed: enabled, connected, tools, last error, scope
/marketplace test <id> · reconnect <id|all> · logs <id> [lines]
/marketplace scope <id> user|project|local
/marketplace env <id> NAME=VALUE ... [--unset NAME] · timeout <id> <seconds>
/marketplace tool <id> [<tool>|*] allow|ask|deny|reset [--global]
/marketplace export [file] [--include-secrets] · import <file> [--dry-run] [--overwrite]
/marketplace auto on|off|status|set <name> <value>     automatic behaviour (off by default)
/marketplace suggest [--reset] · updates · update <id>
```

</details>

<details><summary><code>/mcp</code> usage</summary>

```text
/mcp [list|get|add|remove|enable|disable|reconnect|login|logout|approve|tools|resources|prompts|logs|test|scope|env|timeout|tool|dashboard|export|import|auto|suggest|updates|help]
```

</details>

<details><summary><code>/plugin</code> usage</summary>

```text
/plugin                         installed plugins and marketplaces
/plugin discover [marketplace]
/plugin install <name>@<marketplace> [--scope user|project|local]
/plugin install <name> --marketplace <source>
/plugin install <folder | https git URL> [--project]
/plugin marketplace add|list|update|remove [<source|name>]
/plugin enable|disable <name> [--scope …] · uninstall|update|info <name>
/plugin configure <name> [KEY=VALUE …]
/plugin validate <path> · create <name> [--global] · reload
/plugin status · test <name> · scope <name> user|project · export [file] · import <file>
```

</details>

<details><summary><code>/skills</code> usage</summary>

```text
/skills                         list
/skills show <name>
/skills create <name> [template] [--global]
/skills install <github-url> [--global] [--force]
/skills edit <name> · /skills delete <name> · /skills reload
/skills enable|disable <name> · /skills test <name> · /skills scope <name> user|project · /skills status
```

</details>

<details><summary><code>/stop</code> usage</summary>

```text
/stop            stop every running subagent
/stop <id>       stop one task
```

</details>

<details><summary><code>/subagents</code> usage</summary>

```text
/subagents [n | main | queue]
/subagents edit <n>   edit a finished subagent's report (the main conversation sees the edit)
/tell <n> <message>   talk to a running subagent
```

</details>

<details><summary><code>/subtask</code> usage</summary>

```text
/subtask <task>
```

</details>

<details><summary><code>/tasks</code> usage</summary>

```text
/tasks                   background shells, background turns (Ctrl+B) and subagent tasks
/tasks log <id>          last output lines of a shell (bash_1) or background turn
/tasks kill <id>         stop a background shell
```

</details>

<details><summary><code>/tell</code> usage</summary>

```text
/tell <id|n> <message>
```

</details>

<details><summary><code>/tools</code> usage</summary>

```text
/tools [name]
```

</details>

<details><summary><code>/trust</code> usage</summary>

```text
/trust · /trust yes · /trust revoke [path] · /trust list
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
| `/memory` | `/mem` | Browse, search and manage memory: global, project and repo scopes |
| `/optimize` | `/enhance` | Rewrite a rough request into a precise agent prompt |
| `/prompt` | `/prompts` `/template` `/templates` | Saved prompts with fill-in fields |
| `/schedule` | `/cron` | Run a prompt in a folder daily, weekly, on an interval or once (while Axon is open) |

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
/memory [--scope global|project|repo] [--type user|feedback|project|reference]   browse
/memory add [--global|--project|--repo] <text>   save (LLM-distilled; default --repo = .axon/memory)
/memory view|edit|remove <n|id|scope:id>   edit <user|project|local> opens the AXON.md file
/memory move <n|id> <global|project|repo>   /memory search <text>   /memory clear [scope]
/memory restore [n|name]       list or bring back deleted/overwritten versions
/memory reindex [scope]        rebuild MEMORY.md from the files   /memory doctor   health + stale
/memory why                    what was loaded into this session, with tokens
/memory export <scope> [file]  /memory import <file> [--scope S] [--yes]
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
/prompt                         the saved prompts (menu)
/prompt <name> [key=value ...]  fill in the fields (asks for missing ones) and send
/prompt list [words]            list or search the saved prompts
/prompt show <name>             show one prompt and its fields
/prompt save <name> [--global] [-d description] <text with {{fields}}>   save for this project (or for every project)
/prompt delete <name>           delete a saved prompt, or remove a starter
/prompt export [file]           write the saved prompts to one file
/prompt import <file> [--global] [--replace|--rename]   add the prompts of an exported file
/prompt restore-starters        bring back removed starter prompts
```

</details>

<details><summary><code>/schedule</code> usage</summary>

```text
/schedule                                    open the task menu
/schedule list                               every task, when it runs next and how the last run went
/schedule add every 30m <prompt>             run every 30 minutes (m, h, d; at least 1m)
/schedule add daily 09:00 <prompt>           run every day at 09:00
/schedule add weekly mon,wed 09:00 <prompt>  run on those days
/schedule add once 2026-11-09 14:30 <prompt> run once (or: once in 2h)
    options: --mode readonly|edits   --budget 1.50   --folder DIR   (default: read-only, $1.00, this folder)
/schedule run|pause|resume|delete|history <id>
Tasks run only while Axon is open (this terminal or the app).
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
| `/help` |  | Browse every command with the arrow keys (or list them as text) |
| `/keybindings` | `/shortcuts` `/kb` | Keyboard shortcuts in effect (remap in ~/.axon/keybindings.json) |
| `/notify` |  | Desktop notifications: when they fire (important/all/off), or send a test |
| `/output-style` | `/output-styles` `/style` | Choose how answers are written (default, explanatory, learning, concise, custom) |
| `/release-notes` | `/changelog` `/whats-new` | What's new in this and earlier Axon versions |
| `/sandbox` |  | Run Bash under an OS sandbox (writes confined to the workspace) |
| `/settings` | `/preferences` `/prefs` `/options` | All settings in one place: model, keys, endpoint, permissions, display, cost, data |
| `/status` |  | Version, model, mode, trace, session, cost, storage |
| `/statusbar` |  | Show the live status bar, or toggle it |
| `/statusline` |  | Custom status line command under the prompt |
| `/storage` |  | Show everything Axon stores in ~/.axon, with sizes and leftovers |
| `/suggestions` |  | Show a guess of your next message as ghost text in the empty prompt (Tab accepts it) |
| `/terminal-setup` |  | Set up Shift+Enter for newlines in your terminal |
| `/theme` |  | Colour theme: auto \| axon \| axon-light \| dark \| light \| high-contrast \| mono |
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
/notify [important|all|off|after <seconds>|terminal <off|bell|osc9|osc777>|test [message]]
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

<details><summary><code>/sandbox</code> usage</summary>

```text
/sandbox [on|off|status]   (setting sandbox.enabled; sandbox.allowNetwork lets commands use the network)
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

<details><summary><code>/suggestions</code> usage</summary>

```text
/suggestions [on|off]
```

</details>

<details><summary><code>/terminal-setup</code> usage</summary>

```text
/terminal-setup [vscode|cursor|iterm2|windows-terminal|wezterm|ghostty|alacritty|kitty|apple-terminal]
```

</details>

<details><summary><code>/theme</code> usage</summary>

```text
/theme [auto|axon|axon-light|dark|light|high-contrast|mono]
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
| `/menu` | `/commands` | Walk through every command with the arrow keys |
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
| `/budget` |  | Show or set the spending limits: this session, and this project per day and per month |
| `/compare` |  | Ask 2 to 4 models the same question at once, read-only, with time, tokens and cost for each |
| `/insights` |  | Ask the analytics subagent to explain your spending and suggest savings |
| `/report` |  | Write a self-contained HTML usage dashboard |
| `/stats` | `/analytics` | Usage analytics: cost, tokens, cache, models, sources, tools, expensive turns |

<details><summary><code>/budget</code> usage</summary>

```text
/budget [amount|off]
/budget day [amount|off]
/budget month [amount|off]
```

</details>

<details><summary><code>/compare</code> usage</summary>

```text
/compare <model> <model> [<model> ...] <prompt>
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
| `/open` | `/dash` `/web` | Open the Axon app in your browser (chat, files, shell, commands, usage) |
| `/serve` |  | Show how to run the local Axon HTTP/SSE API (axon serve) |

<details><summary><code>/open</code> usage</summary>

```text
/open [port] [--no-open] | /open stop
```

</details>

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

## Notes on specific commands

The reference above is generated from the code. These notes cover behaviour it does not spell out.

**Insight commands** work from data Axon already recorded in the session; a number that was not recorded is left out.
- `/trail [n]` shows one tool call: why it was chosen, what allowed it, its result and what came next.
- `/timeline [turn]` draws the steps of a turn as a bar chart of time, tokens and cost, with the cause of a
  cache miss. It replaced `/timeline` as an alias of `/history`; `/history` itself remains and lists the messages.
- `/ctxmap` lists what fills the context window (estimates). `/ctxmap drop <id>` replaces a tool result in the
  live conversation with a short stub and saves the full text under the session's `dropped/` folder. **A drop
  does not survive `/resume`**: the stub is not written to the transcript, so a resumed session reloads the
  original result. Pinned results cannot be dropped.
- `/pin <path|id>` keeps a tool result in context in full, so it is never trimmed or compacted away;
  `/unpin` releases it. Pins are saved in the transcript and come back on resume.
- `/ctxdiff [n]` compares model call n with the call before it: what entered and left the prompt, and why the cache missed.
- `/whatif [model]` reprices the session's recorded tokens at another model's prices, with and without caching;
  `/whatif all` lists every priced model. Prices of models with no known price are not guessed.
- `/receipts` matches claims in the last answer (tests pass, lint is clean, no other callers, files edited) to the
  tool calls of that turn. It is a pattern match: it shows a matching call ran, not that the claim is true.
- `/compact --last` shows what the last compaction replaced, with its summary.
- `/search <text>` searches the transcripts of this project (`--all`: every project); `/find` searches file names.
  Encrypted transcripts are skipped and listed.
- `/review hunks` lists the edits Axon made this session hunk by hunk, against each file's content before its
  first edit. `accept <id|all>` marks hunks as reviewed; `reject <id>` reverts that one hunk and refuses if the file
  changed since the list was printed.

**Tools** (the table in the README lists them all). Notes on some of them:
- `Read` reads text with line paging. For PDFs, `pages` takes a range such as `"3"`, `"2-5"` or `"4-"` (at most 20
  pages per call); ranges need `pdftotext` (poppler) or the optional `pypdf` package, otherwise only a basic text
  extraction is available for short PDFs. Notebooks (`.ipynb`) are read as cells with outputs. Images (png, jpg,
  gif, webp) up to 5 MB are attached to the result when the provider accepts images in tool results; otherwise
  the model gets the size and any text found by OCR.
- `Monitor` waits for a background shell (`Bash` with `run_in_background`) to print a line matching a regex or to
  exit, for up to `timeout_s` (default 60, at most 600), instead of polling `BashOutput`.
- `Git` runs a fixed list of git subcommands in the workspace. Read-only ones (`status`, `diff`, `log`, `blame`,
  `show`, `rev-parse`, `ls-files`, and `branch`, `tag`, `remote` when they only list) run without a prompt;
  changing ones (`commit`, `checkout`, `reset`, `push`, ...) ask. Flags that can run helper programs or write files are
  refused; use `Bash` for anything else.
- `LSP` asks a language server found on `PATH` (pyright, typescript-language-server, gopls, rust-analyzer, clangd)
  for `diagnostics`, `definition`, `references` or `hover`. With `lsp_diagnostics_after_edit` (default on), new
  language-server errors are appended to `Edit`, `Write` and `MultiEdit` results. Without a server, the tool
  says which one to install.
- `CheckpointList` lists this session's file-edit checkpoints, newest first. `CheckpointUndo` (asks first)
  restores the files changed in the newest `steps` checkpoints (files the model created are deleted) and overwrites
  later changes to them, including yours. Only edits made by `Write`, `Edit`, `MultiEdit` and patches have checkpoints.
- `EnterPlanMode` switches to read-only plan mode for a risky task; the model then presents its plan with
  `ExitPlanMode`, and nothing is edited or run until you approve.

**Files.** Files dropped into the prompt are copied into the session at drop time; `/files` (alias `/attachments`)
lists them with the saved paths. See [architecture](architecture.md#large-input-and-attachments).

**Safety.** `/sandbox [on|off|status]` runs Bash under an OS sandbox. The always-on shell guard reads command text
only and cannot see what a script does, so the sandbox is the stronger control; see
[architecture](architecture.md#shell-guard-and-the-sandbox). `/trust` reviews project hooks, plugins and MCP servers
for the current folder; `axon -p` and the SDK keep running them unless `trust.requireForNonInteractive` is set, see
[folder trust](extending.md#folder-trust).

**Memory.** `/memory` works on three scopes: `global` (all projects), `project` (this project, private, under
`~/.axon/projects/<key>/memory`) and `repo` (`<workspace>/.axon/memory`, shared if you commit it; the default for `add`).
A replaced or deleted memory goes to a trash folder first, which is what `restore` reads. Subcommands: `add`, `view`,
`edit`, `remove`, `move <n|id> <scope>`, `search`, `clear [scope]`, `restore` (list or bring back deleted or
overwritten versions), `reindex` (rebuild `MEMORY.md` from the files), `doctor` (health and stale entries),
`why` (what was loaded into this session, with tokens), `export <scope> [file]` and `import <file>`.

**Theme.** `/theme` takes `auto`, `axon`, `axon-light`, `dark`, `light`, `high-contrast` or `mono`. `axon` and
`axon-light` are the Axon palettes for dark and light terminals; `auto` picks between them from the terminal
background, and `dark` and `light` are the older palettes.

**Browser app.** `/open` (aliases `/dash`, `/web`) starts the local app; see [app.md](app.md).

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
axon mcp <list|get|add|add-json|remove|login|logout|reset-project-choices|serve|...>
axon stats [today|7d|30d|all]
axon report [--days N] [--out PATH] [--all-projects] [--open]
axon serve [--port 8765] [--host 127.0.0.1] [--token T] [--workspace DIR] [--mode M]
axon map [path]
axon new <template> <dir>
axon purge [project|caches|all] [--yes]
axon agents [--json] [--workspace DIR]   # list subagent definitions and skipped files
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
  --context-limit SIZE  How much of the model's context window to use: auto (default: the model's
                        full window), 128k, 256k or 1m
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
setup` guided provider setup, `axon purge` deletes stored sessions, `axon new <template>` starter
project, `axon map` repository map, `axon stats` / `axon report` usage analytics, `axon serve`
local HTTP/SSE API, `axon activity` the journal of everything you did, `axon projects` every
project and its data folder, `axon keys` API keys (add, replace, test, remove), `axon endpoint`
base URLs and custom endpoints.
```
<!-- END GENERATED: cli-help -->
