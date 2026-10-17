# Changelog

## 0.27.5

Restored sessions show what the live view showed
- A restored step is drawn the same as it was live: the header with the provider and "reads N messages (~tokens)",
  the "includes N tool results from step K" line, and "thought for X.Xs" after each thinking block.
- These facts are saved with each LLM call from now on. Sessions recorded before this release keep their
  messages, tool calls and outputs, but not those numbers, because they were never saved.

## 0.27.4

Every file change is shown
- Write always shows what changed. Overwriting a file draws a coloured diff with `+added −removed`, including
  full rewrites (these used to fall back to a plain listing of the new file with no counts). A new file is drawn
  as green additions with `+N` in its header.
- Files changed by shell commands are tracked. Around each Bash command Axon snapshots the workspace and then
  lists every file the command created, modified, renamed or deleted (`✚ Created`, `⟳ Modified`, `➜ Renamed`,
  `✖ Deleted`) with line counts, followed by each file's diff. Files in git-ignored folders are included;
  dependency and cache folders (`node_modules`, `.venv`, `dist`, …) are skipped, and the contents of secret files
  such as `.env` or `*.pem` are never shown, only that they changed.
- The model sees a one-line summary of those changes, and restored sessions and `/inspect` show the counts too.

Windows
- Hooks run under PowerShell: a quoted command path is invoked, and a hook's exit code (2 blocks) is kept.
- Esc and Ctrl+C stop a streaming response on Windows.
- An interrupt between a tool call and its result no longer leaves the conversation invalid.
- An edit made outside Axon within the same modification-time tick is detected (file size is compared too).

Wording
- Axon is described as "the AI coding agent that shows its work".

## 0.27.3

Everything is saved, and can be shown again
- New `/library` (alias `/saved`) and `axon library [section] [--json]`: one view of everything stored for a
  project: sessions, subagent runs with their full reports, skills and their runs, agents, custom commands,
  memory, hook runs, plans, todos, file history, full tool outputs and model calls.
- Skill runs (typed `/name` or called by the model), subagent results (type, prompt, status, final report,
  tokens, cost, time), hook runs, custom command and MCP prompt runs, and plans are now written to the session
  transcript and the activity journal. `/resume` and `/history` show them inline; `/activity` has the new kinds
  `skill`, `subagent`, `hook`, `custom_command`, `plan` and `saved`.
- Every version of every skill, agent, command, memory, rule, prompt and `AXON.md` is copied to
  `~/.axon/projects/<project>/library/`, whether it was created by a command, written by the model or edited by
  hand (picked up at startup and by `/library`). Deleting a memory keeps its last version.
- Approved plans (ExitPlanMode) are saved as Markdown in the project's `plans/` folder.

Install
- README and Windows guide: install from PyPI with `uv tool install axon-terminal`, `pipx install axon-terminal`
  or `pip install axon-terminal`, with a note on the difference and on the unrelated `axon` package.

## 0.27.2

A readable timeline
- Every part of a turn has its own mark: `◆ Step N  LLM call` (model, how much it reads), `✻ Thinking` with the
  full reasoning behind `┊`, `↪ replied in …` (time, tokens, cache, cost, context), `● Tool` with a line saying
  what the tool does and why the model called it, `⎿` results with a preview, and the model's own text behind an
  accent bar `▎`. Plain wording replaces jargon ("fed back", "ctx", "plans N tool calls").
- Read, Grep, Glob and Ls show a preview of what they found, not just a count. Bash shows its exit code and
  output; a one-line output is shown as is. A leading `cd <workspace> &&` is hidden from command headers.
- `/output <n>` shows tool call *n* in full and `/output list` lists them; the `… +N more lines · /output n`
  hint is a clickable link to the full output in terminals that support links.
- `Ctrl+F` / `/transcript`: the whole session full-screen. Click (or Enter/Space) on a tool call, thought or answer
  expands it (input, full numbered output, reasoning, rendered answer) and collapses it again; running tools tick
  live; `e`/`c` expand or collapse all, `/` searches, `y` copies. Resumed sessions work too.
- The spinner shows a live timer for the current tool or step, and a tip underneath.
- Tool headers in parallel batches stay together and in call order; sequential batches print immediately.
- Routine approvals (bypass mode, read-only tools) are no longer printed at the normal level; denials and
  user approvals still are. Cache-miss explanations moved to the detailed level.
- `/resume`, `axon -c` and `axon -r` redraw the restored conversation like the live timeline: prompts, tool
  calls with result previews, thinking summaries and answers.
- Suggestions after a turn list the reason for each (`→ Suggested next  (/next <n> runs one)`), and Ctrl+O
  explains what the new trace level shows.

Tips
- Usage tips are written by the model from what you are doing (recent prompts, tools, errors, commands you
  already use) and checked against the real command and key-binding list; registry-derived tips are used
  until then. Shown under the spinner, in the banner and after some turns. `/tips`, `/tips refresh`,
  `/tips off`, settings `tips` and `tips_llm`. The call is logged as "tips" in `/cost`.

Context size
- New `context_limit` setting, 128k tokens by default: Axon uses at most that much of the model's window, even
  on 1M-token models. Choose 128k, 256k, 1M or the model's maximum with `/context limit` (a picker), `/settings`,
  or `--context-limit`. The context gauge, `/context` and the status line show the chosen limit.
- Auto-compact now runs at 75% of the context limit (`compact_at` 0.75), e.g. 96k of 128k.

Screen log
- Everything shown in the REPL (timelines, tool output, command output, errors, your prompts) is also kept as
  plain text in `~/.axon/projects/<project>/screen/<started>-<session>.log`, never truncated. Spinner frames,
  the tip row and prompt redraws are not recorded. Setting `screen_log` (default on).

Notifications
- Desktop notifications now follow one policy (setting `notifications`, default `important`): only when Axon
  waits for you (a permission prompt in default/acceptEdits/plan mode, or a question), a turn failed, a turn ran
  `notify_after_seconds` (30) or longer, or a background turn finished, and only while another app is in
  front. Quick turns, tests, CI, scripts, print mode and subagents never notify; repeats are suppressed.
- `/notify important|all|off`, `/notify after <seconds>`, `/notify test`.

Shell
- `!cmd`: the model now responds to the command's output (what it shows, errors and fixes). Turn it off with
  `respond_to_bash_commands = false`. A bare `cd` only changes directory.
- The prompt echo of `!cmd` no longer shows the `!` twice.

Reliability
- DeepSeek thinking-mode tool calls without reasoning are sent with a `reasoning_content` placeholder, fixing
  "Upstream rejected the request as invalid" / "reasoning_content must be passed back" failures.
- A 5xx that says the request itself is invalid is reported as such (not as an outage) and retried less.
- When a provider call fails mid-turn, the work done is kept: type `continue` or the new `/retry`.
- The Bash tool finds the workspace `.venv` (and drops a stale `VIRTUAL_ENV`), so `python` resolves.
- `/context` reports the full window and the reply reserve separately, matching the context gauge.
- `/clear` starts turn numbering at 1 again.

Install
- One-line installers that need no clone: `curl -fsSL …/install.sh | bash` and `irm …/install.ps1 | iex`,
  using uv, pipx or a private venv; `--ref`, `--method`, `--dry-run` and `--uninstall` options.
- GitHub Actions: CI on Linux, macOS and Windows (tests and installers) and a PyPI release workflow.

### Also in 0.27.2 (drafted earlier as 0.28.0)

Providers and cost
- Official provider presets for Anthropic, OpenAI, OpenRouter, DeepSeek, Gemini, Groq, Together and local
  engines (Ollama, LM Studio). The endpoint and API key follow the model you pick.
- Fallback models: when the primary model keeps failing (429, 5xx, overload), the next model in the chain is tried.
- Per-call cost accounting from one pricing table, covering cache reads and writes. `/cost` breaks spend down
  by source and model, shows prompt-cache hit rate and savings, and shows the last cache-miss cause.
- Every LLM call is recorded in `~/.axon/usage.jsonl`. `/stats` and `axon stats` read it for analytics.

Glass-box trace
- Each turn is drawn as a numbered timeline: every LLM call (tokens, cache, time, cost), every tool call with
  its arguments and result size, permission decisions, hook outcomes, retries and compaction.
- Trace levels `compact`, `normal`, `detailed` and `debug`. Set them with `/trace`, `--trace` or Ctrl+O.
- `/inspect [n]` replays a stored turn timeline. `/explain` summarises a turn in plain language without an LLM call.

MCP, hooks, skills and agents
- MCP client for stdio, HTTP and SSE servers: `axon mcp add/remove/list`, `/mcp`, MCP prompts as slash
  commands, and resources.
- Claude-Code-compatible hooks: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop,
  PreCompact, Notification and SessionEnd, with matchers, JSON decisions and additionalContext.
- Skills (SKILL.md), custom slash commands (`.axon/commands`, `.claude/commands`), plugins and subagents
  (`.axon/agents`). `/agents` now has a creation wizard.
- Output styles (`/output-style`): built-in default, explanatory, learning and concise styles, plus custom
  Markdown styles.

Sessions
- Full transcripts: user input, assistant text and thinking, tool calls and results, usage, cost, timings
  and permission decisions.
- `/rewind` menu: restore code and conversation, conversation only, or code only, or summarise from a chosen
  prompt.
- `/resume` picker with search, `/rename`, `/branch`, and `/export` to a file or the clipboard.

Workspace and permissions
- `permissions.ask` rules, which beat allow rules. `/permissions` has an interactive allow/ask/deny panel.
- "Don't ask again" approvals are saved to `.axon/settings.local.json`.
- `/add-dir` and `--add-dir` allow extra directories, and `/cd` moves the session to another workspace.

Terminal experience
- `/config` panel for model, mode, effort, trace, theme, vim mode, suggestions and output style.
- Custom status line command (`/statusline`), `/theme`, `/vim`, `/color`, `/terminal-setup` (Shift+Enter
  setup), and `/fast` to switch to the small model.
- `/loop` re-runs a prompt N times or on an interval. `/goal` keeps a session goal in view.
- `/context` draws a coloured grid of context usage. `/btw` side questions now see recent session context.
- Windows PowerShell and cmd: VT mode, UTF-8, key handling and shell selection all work natively.

Solver, planning and analytics
- `/verify` runs the project's detected checks, `/fix` turns failures into a precise fix prompt, and `/autofix`
  loops until the checks pass. `/plan-run` (alias `/solve`) drafts a plan you approve, then executes it step by step.
- `/stats`, `axon stats` and `axon report` (a self-contained HTML dashboard) read the usage log. `/insights` explains
  your spending.
- `axon serve` exposes a local HTTP/SSE API, and `axon.features.sdk` provides `query()` and `run()` for Python.
- `axon setup` provides guided onboarding. `/new` and `axon new` scaffold starter projects from 11 templates.

Fixes
- `axon mcp add <name> -- <command> ...` registers a stdio server. Before this fix, the `--` separator
  left the transport unset.

Project
- The developer scripts moved to `scripts/` (`setup_env.py`, `axon_run.py`, `gen_docs.py`); `check_models.py` stays at the repository root.
  `pyproject.toml` is now the only dependency list.
- New documentation in `docs/` (architecture, configuration, commands, extending, Windows, testing), with
  reference tables generated from the code by `scripts/gen_docs.py`.

## 0.27.8

- Windows optimizations and a streamlined quick start.

## 0.27.7

- Provider-specific models, API key verifier, enhanced fuzzy picker and session tags.
