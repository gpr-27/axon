# 10 — Claude Code Parity Map

The reference implementation for Axon is **Claude Code**. This document maps Claude Code's
feature surface, area by area, onto what Axon implements — so "works like Claude Code" is a
checklist rather than an adjective.

Four parity levels, used consistently:

| Level | Meaning |
|-------|---------|
| **Full** | Behaviourally equivalent. Same mental model, same result. |
| **Partial** | Core behaviour present, some surface missing. The gap is named. |
| **Deferred** | Designed, not in v1. Named phase or "post-v1". |
| **Out** | Deliberately not built. Reason given in [`00-VISION.md`](00-VISION.md#non-goals). |

The honest summary up front: **Axon targets Full parity on the agent core** — the loop,
the tool suite, permissions, context management, sessions — and explicitly **Out** on the
ecosystem surface (MCP, IDE extensions, plugins, hosted infrastructure). The core is where
the engineering is; the ecosystem is where the company is.

---

## 1. Tools

Claude Code's tool set, and Axon's implementation of each.

| Claude Code tool | Axon | Parity | Notes |
|---|---|:---:|---|
| `Read` | `Read` | **Full** | Line offsets, 2000-line default, `cat -n` numbering, image/PDF/notebook handling **Partial** — text + line ranges in v1, images post-v1 |
| `Write` | `Write` | **Full** | Read-before-overwrite enforced, atomic `tmp`+`os.replace` |
| `Edit` | `Edit` | **Full** | Exact string match, uniqueness requirement, `replace_all` |
| `MultiEdit` | `MultiEdit` | **Full** | Sequential edits, all-or-nothing |
| `NotebookEdit` | — | **Out** | `.ipynb` cell manipulation. Narrow audience, high surface area. |
| `Bash` | `Bash` | **Full** | Persistent session, sentinel protocol, timeout, process-group kill, output cap, required `description` |
| `Bash` background + `BashOutput`/`KillShell` | `Bash(run_in_background)` | **Deferred** | P6 if time allows. Needs a shell registry and a polling tool pair. |
| `Glob` | `Glob` | **Full** | mtime-descending sort |
| `Grep` | `Grep` | **Full** | ripgrep with pure-Python fallback, `-A/-B/-C`, `output_mode` |
| `Ls` | `Ls` | **Full** | |
| `Task` | `Task` | **Partial** | Sub-agent with context isolation and own budget. Custom agent *types* from `.axon/agents/*.md` is **Deferred** to post-v1; v1 has one general-purpose sub-agent. |
| `TodoWrite` | `TodoWrite` | **Full** | Single-`in_progress` constraint, survives compaction |
| `WebFetch` | `WebFetch` | **Full** | Cheap-model summarization, 15-min cache |
| `WebSearch` | — | **Out** | Requires a search provider Axon doesn't have. `WebFetch` covers the "read this URL" case. |
| `ExitPlanMode` | `ExitPlanMode` | **Full** | |
| `AskUserQuestion` | — | **Deferred** | The loop can already stop and ask by ending its turn; a structured multiple-choice tool is polish. |
| `Skill` | — | **Out** | Skills are a packaging system, not agent capability. |
| `SlashCommand` | — | **Out** | Model-invoked slash commands. Human-invoked commands are **Full** (see §4). |
| MCP tools | — | **Out** | See §9. |
| — | `Doctor` | *(addition)* | Axon-specific: self-diagnosis of endpoint, capabilities, environment. Claude Code has this as `/doctor`; Axon also exposes it as a tool so the agent can diagnose itself. |

**13 tools** at Full or Partial, against Claude Code's ~16 general-purpose tools. The three
`Out` entries (`NotebookEdit`, `WebSearch`, `Skill`) are absences a user would notice; the
rest of the gap is packaging.

Full contracts and schemas: [`03-TOOLS.md`](03-TOOLS.md).

---

## 2. The agent loop

The part that matters most, and where parity is highest.

| Behaviour | Axon | Parity |
|---|---|:---:|
| ReAct loop until `end_turn` | `agent/loop.py` | **Full** |
| Parallel tool execution in one turn | `ThreadPoolExecutor`, opt-in via `readonly` | **Full** |
| Every `tool_use` gets exactly one `tool_result` | [Law 1](02-AGENT-LOOP.md#law-1--pairing), structurally enforced | **Full** |
| Errors returned as data, not raised | [Law 4](02-AGENT-LOOP.md#law-4--errors-are-data) | **Full** |
| `Ctrl-C` mid-tool leaves a valid conversation | [Law 5](02-AGENT-LOOP.md#law-5--interrupts-still-close-the-turn) | **Full** |
| Extended thinking, streamed | `thinking: {type: "adaptive", display: "summarized"}` | **Full** |
| Effort control | `output_config: {effort}`, `--effort` flag | **Full** |
| `pause_turn` handling for long server-side work | continue the loop on `pause_turn` | **Full** |
| Sub-agent context isolation | `agent/subagent.py`, only final text returns | **Full** |
| Loop guards (iteration/token/time/no-progress) | 40 iters · 500k tokens · 10 min · repeat-detector | **Full** |
| Mid-conversation system messages (mode changes) | injected without invalidating cache | **Full** |
| Compaction on overflow | four-rung ladder | **Full** |

Where Axon deliberately differs: the guards are *visible and configurable*. Claude Code's
limits are internal; Axon's are in `Settings` and `/context` shows how close you are. That
is a teaching choice — the whole point of building this is that the mechanism is inspectable.

---

## 3. Permission model

| Claude Code | Axon | Parity |
|---|---|:---:|
| `default` mode | `default` | **Full** |
| `acceptEdits` mode | `acceptEdits` | **Full** |
| `plan` mode | `plan` + `ExitPlanMode` | **Full** |
| `bypassPermissions` | `bypass`, gated on `--dangerously-skip-permissions` + interactive confirm | **Full** |
| `Tool(pattern)` rule syntax | same syntax, deliberately | **Full** |
| `allow` / `deny` / `ask` arrays | `allow` / `deny` (ask is the default outcome) | **Partial** — an explicit `ask` array is trivial to add; v1 treats "no rule matched" as ask |
| Settings hierarchy (enterprise → user → project → local) | user `~/.axon/config.toml` → project `.axon/config.toml` | **Partial** — two levels, not four. No enterprise-managed policy. |
| "Always allow" writes the rule back | appends to project-local config | **Full** |
| Directory scoping (`--add-dir`) | single workspace root | **Deferred** |
| Path jail | `resolve()`-before-compare, `_BLOCKED_NAMES` | **Full** |
| Command deny-list | structural regex list, metacharacter fallback | **Full** |
| Hook-based veto | `PreToolUse` exit-code veto | **Full** |

The one place Axon is *stricter* than Claude Code: hard invariants (path jail, structural
deny-list) sit **above** every rule and mode, including `bypass`. There is no configuration
that lets the agent `rm -rf /`. Detail: [`06-SECURITY.md`](06-SECURITY.md#the-permission-engine).

---

## 4. Slash commands

Claude Code has ~40 slash commands. Most are account, IDE, or platform plumbing that has no
meaning in Axon. The ones that are *agent* commands, Axon implements.

**Implemented (Full):**

```
/help          /model         /mode          /clear         /compact
/context       /cost          /resume        /tools         /permissions
/doctor        /budget        /export        /init          /memory
/todos         /effort        /exit
```

| Command | Behaviour |
|---|---|
| `/context` | Visual token-budget breakdown: system, tools, history, per-file. Claude Code's `/context` equivalent. |
| `/cost` | Per-model ledger, cache hit ratio, and the uncached counterfactual |
| `/init` | Generate an `AGENTS.md` for the current repository by exploring it — Claude Code's `/init` |
| `/memory` | Open the applicable `AGENTS.md` in `$EDITOR`, reload on save |
| `/export` | Write the transcript to markdown |
| `/budget N` | Raise the session cost ceiling |
| `/todos` | Render current todo state |

**Custom commands — Partial.** `.axon/commands/*.md` with frontmatter (`description`,
`argument-hint`, `allowed-tools`) and `$ARGUMENTS` / `$1`-`$9` substitution: **implemented
in P6**. The `!`` `bash` `` `` pre-execution and `@file` inlining that Claude Code supports:
**Deferred**.

**Out:** `/login`, `/logout`, `/status`, `/usage`, `/privacy-settings`, `/bug`,
`/release-notes`, `/terminal-setup`, `/vim`, `/ide`, `/mcp`, `/plugin`, `/install-slack-app`,
`/statusline`, `/output-style`, `/agents`, `/skills`, `/artifacts`, `/tasks`, `/workflows`.
Each is account management, a platform integration, or an ecosystem feature — not agent
capability.

**Deferred:** `/rewind` (conversation + file checkpointing). Genuinely valuable and
genuinely hard: it needs a content-addressed snapshot of every file the agent touched, per
turn. Post-v1, and noted here so it is a decision rather than an oversight.

Detail: [`13-UI-AND-CLI.md`](13-UI-AND-CLI.md#slash-commands).

---

## 5. Memory and project configuration

| Claude Code | Axon | Parity |
|---|---|:---:|
| `CLAUDE.md` in project root | `AGENTS.md` in project root | **Full** |
| Walk up parent directories, collect all | same, nearest-last so nearest wins | **Full** |
| `~/.claude/CLAUDE.md` user-global | `~/.axon/AGENTS.md` | **Full** |
| `CLAUDE.local.md` | `AGENTS.local.md`, gitignored | **Full** |
| `@path/to/file` imports inside memory | **Deferred** | Recursive import resolution with a depth cap. Post-v1. |
| `#` shortcut to append a memory mid-session | `#` prefix appends to project `AGENTS.md` | **Full** |
| `/init` to generate one | **Full** | |
| Enterprise managed policy file | — | **Out** |

`AGENTS.md` rather than `CLAUDE.md` because Axon is not Claude Code and should not squat on
its filename in a repository that may contain both. `CLAUDE.md` is *also* read if present,
so pointing Axon at an existing Claude Code repository works with no setup.

Injection mechanics: [`12-SYSTEM-PROMPT.md`](12-SYSTEM-PROMPT.md#block-5--project-context-injection).

---

## 6. Hooks

| Claude Code hook | Axon | Parity |
|---|---|:---:|
| `PreToolUse` (can block) | **Full** | Exit code 2 vetoes; stdout becomes the `tool_result` |
| `PostToolUse` | **Full** | Observation only; cannot alter the result |
| `UserPromptSubmit` | **Full** | stdout is prepended to the prompt |
| `SessionStart` | **Full** | stdout is injected as session context |
| `SessionEnd` | **Full** | Cleanup, no output channel |
| `Stop` | **Deferred** | |
| `SubagentStop` | **Deferred** | |
| `PreCompact` | **Deferred** | |
| `Notification` | **Out** | No notification surface to hook |

Five of nine, covering the two that matter: `PreToolUse` for policy enforcement outside the
permission engine, and `UserPromptSubmit` for context injection. Same JSON-on-stdin,
exit-code-as-verdict contract as Claude Code, so existing hook scripts largely work.

---

## 7. Sessions and persistence

| Claude Code | Axon | Parity |
|---|---|:---:|
| `--continue` most recent session | **Full** | |
| `--resume [id]` with a picker | **Full** | Arrow-key picker |
| Transcript on disk, replayable | **Full** | Append-only JSONL, `fsync`, `0600` |
| Session id addressable | **Full** | ULID |
| Survives `SIGKILL` | **Full** | This is why it is append-only, not a JSON blob ([ADR-007](01-ARCHITECTURE.md#adr-007--append-only-jsonl-transcript-not-a-json-blob)) |
| Per-project transcript directory | **Full** | `~/.axon/projects/<slug>/` |
| Checkpoint/rewind | **Deferred** | See §4 |
| Cost ledger per session | **Full** | Claude Code shows this in `/cost`; Axon adds the uncached counterfactual |

---

## 8. Interface and invocation

| Claude Code | Axon | Parity |
|---|---|:---:|
| Interactive REPL | **Full** | |
| `-p / --print` headless one-shot | **Full** | |
| `--output-format json` | **Full** | For scripting; `stream-json` **Deferred** |
| Piped stdin (`cat x \| claude -p`) | **Full** | |
| `--model` | **Full** | Four models |
| `--allowedTools` / `--permission-mode` | **Full** | |
| `--append-system-prompt` | **Full** | |
| Streaming markdown render | **Full** | Stdlib-only ([ADR-009](01-ARCHITECTURE.md#adr-009--stdlib-only-terminal-ui)) |
| Tool cards with inline diffs | **Full** | |
| Approval prompt showing the real command | **Full** | |
| `@file` mention completion | **Partial** | `@path` inlines a file; tab-completion **Deferred** |
| Image paste | **Out** | |
| Statusline customization | **Out** | |
| Output styles | **Out** | |
| Vim keybindings | **Out** | |
| `Esc` to interrupt, `Esc Esc` to edit history | **Partial** | `Ctrl-C` interrupts cleanly; history editing **Deferred** |

Detail: [`13-UI-AND-CLI.md`](13-UI-AND-CLI.md).

---

## 9. Ecosystem — deliberately Out

| Feature | Why not |
|---|---|
| **MCP** (stdio/SSE/HTTP servers, `.mcp.json`, resources, OAuth) | The single largest surface in Claude Code and almost none of it is agent engineering — it is client implementation for someone else's protocol. The `Tool` ABC is designed so an `MCPTool` adapter would slot in without touching the loop, which is the interesting half; building the transport layer is not. |
| **IDE extensions** (VS Code, JetBrains) | A different product with a different runtime. |
| **Plugins / marketplaces** | Distribution, not capability. |
| **Skills** | A packaging convention over prompts. `AGENTS.md` covers the same need at v1 scale. |
| **GitHub Actions / CI integration** | `-p` mode with `--output-format json` already makes Axon scriptable, which is the substance. |
| **Hosted/cloud sessions, Slack, web** | Infrastructure. |
| **Multi-agent teams, workflows, remote agents** | Beyond a single-repository coding agent. |

Naming these explicitly is the point. A reader who sees "MCP support: no" alongside a
reason and a note that the extension point exists reads scope discipline. A reader who
discovers the absence themselves reads an incomplete project.

---

## 10. Where Axon is not a copy

Three places Axon deliberately does something Claude Code does not, because the goal is
understanding rather than imitation.

1. **Multi-provider by construction.** Claude Code runs Claude. Axon runs four models over
   two incompatible protocols with one agent core, which forced the normalization boundary
   that turned out to be the most instructive part of the build
   ([`04-PROVIDERS.md`](04-PROVIDERS.md#the-asymmetry-table)).
2. **The mechanism is inspectable.** `/context` shows the token budget by category.
   `/cost` shows the cache hit ratio and the uncached counterfactual. `axon doctor` shows
   which betas the endpoint actually supports. Claude Code hides its internals because it
   is a product; Axon exposes them because it is an explanation.
3. **Hard invariants above configuration.** No mode, rule, or flag can disable the path
   jail or the structural deny-list — `bypass` included.

---

## Parity scorecard

| Area | Full | Partial | Deferred | Out |
|---|:---:|:---:|:---:|:---:|
| Tools | 11 | 2 | 3 | 4 |
| Agent loop | 12 | 0 | 0 | 0 |
| Permissions | 10 | 2 | 1 | 1 |
| Slash commands | 18 | 1 | 1 | 20 |
| Memory | 5 | 0 | 1 | 1 |
| Hooks | 5 | 0 | 3 | 1 |
| Sessions | 7 | 0 | 1 | 0 |
| Interface | 9 | 3 | 4 | 0 |
| Ecosystem | 0 | 0 | 0 | 7 |

**The agent loop is the only row with no gaps, and that is intentional** — it is the row
that defines whether this is a coding agent or a chat client with file access.

---

Next: [`11-FILE-SPECS.md`](11-FILE-SPECS.md) — every file, specified.
