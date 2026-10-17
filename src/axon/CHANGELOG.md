# Changelog

## 0.27.6

Parallel subagents are clear and correct
- Subagents are numbered in the order the model asked for them, not in the order their threads happened to start.
- A failed subagent is shown as failed everywhere: its card says `Failed` with the reason (for example
  `HTTP 400 Bad Request`), the summary counts it as failed rather than done, and the live view shows the reason.
  Previously the card said `Completed` and the body was the parent's retry advice.
- The summary is one aligned table (number, subagent and type, status, steps, time, tokens) instead of a box
  with uneven rows. Every `Task` call shows its agent type, and its routine "allowed by delegation" line is hidden.
- The live view (0 overview, 1-9 one subagent, Q queue) shows what each subagent was asked, its model and its
  failure reason. Subagent steps are labelled with their number, so two subagents' steps can be told apart.
- While the live view is drawn, output from the subagents is held and printed after it closes, so the view no
  longer leaves stacked copies or stray lines in the transcript.
- `/subagents` marks a restored failed subagent as failed and shows its reason when it is opened.

Subagents finish on AgentRouter
- AgentRouter's `HTTP 500 Upstream rejected the request as invalid` is intermittent: the same request failed on
  20-70% of attempts and passed on others, more often as the conversation grew. It was retried only twice, so a
  long subagent almost always lost one call and died. On AgentRouter it is now retried up to 8 times (other
  providers keep the short budget, since there the request really is malformed). In a 5-subagent test run 53 of
  123 requests were rejected and all five subagents finished; before, three of five failed.
- A subagent whose model call still fails resumes from where it stopped (its tool results are kept), up to twice.
  If it gives up, the parent gets the reason plus the tool calls that already ran, so it can retry or finish the
  work without redoing it.
- Read-only subagents (explore, plan) are listed as having no shell, so the model no longer asks them to run
  `git diff`, which made them waste steps hunting for a Bash tool.

Subagent and plan views
- Live view: a header with running/done/failed counts, tokens, wall time and a progress strip; tabs that always
  show number and status and shorten titles to fit; one block per subagent with steps, time, tool calls, tokens,
  retries and resumes, and what it is doing right now; a failed subagent's full reason. Opening a subagent shows
  what it was asked and a timeline of its steps, tool calls, results, thinking, retries and resumes.
- Summary table: full titles, type column, the full failure reason under the row, and a line with tool calls,
  retries and resumes. Result cards say `Subagent #n · title · status`, list the progress made before a failure,
  and carry the run time in the card border (no more `└ ┌` double corner).
- Plans (TodoWrite) show every item, numbered, with a progress bar; the item in progress is highlighted, done
  items are struck through, and long items wrap instead of being cut.
- After a fan-out the timeline shows the summary of that batch only (earlier subagents are no longer repeated),
  then each Task card in the order the model asked for them. Each subagent's own LLM and tool calls are no
  longer replayed into the main timeline (they were already in the live view); `/subagents <n>` and the
  `detailed` trace level still show them, labelled `subagent #n (type)`.
- Notes typed during a run say they go to the main agent and run after the subagents; the confirmation and the
  result of `/q` commands appear inside the live view instead of stacking lines above it.
- Steps read `step 12 · limit 25` while a subagent runs and `2 steps` once it finished (no half-empty bar).

Images
- SVG files are no longer attached as images (providers cannot decode them), and paths in a subagent's prompt,
  which the model writes, are never turned into image attachments. A subagent asked to read `docs/assets/*.svg`
  failed every call with `fail to decode image config(webp)`.
- An image the provider cannot decode is reported as such and not retried (it said "temporarily unavailable").

Subagent step limit
- A subagent is told three steps before its step limit to wrap up, and at the limit it makes one last call to
  write its report (what it found, what it did, what is left) instead of being cut off with nothing. The parent
  gets that report plus the tool calls that already ran. The main agent gets the warning too; `--max-turns N`
  still makes exactly N calls.
- The Task tool takes `max_steps` (default 25, or `maxTurns` from the agent's definition), capped by
  `max_iterations`. `maxTurns` was not capped before.
- Step-limit cards say `⚠ Step limit` (they said `Completed`).

Tool calls are drawn the same way for every tool
- Input: `● Name  target`, then every other argument as an aligned `key  value` row, typed and wrapped (not
  dropped, not raw JSON). Bash shows `$ command` with multi-line commands kept readable and its own
  description as the purpose; MCP tools read `server › tool`.
- Output: one summary row (`✓ exit 0` / `✗ exit 1`, test bar, line counts, size, `in <cwd>` when it is not the
  workspace), then the body: JSON pretty-printed with its shape, code highlighted, errors first in full behind
  a `▎` bar, `(no output)` when empty. /inspect, restored sessions and the redraw after a resize use the same
  rows as the live view.

Connecting to a provider
- After the API key, Axon lists the models that key can use (the provider's own `/models`, chat models only),
  you pick one, and the key is checked with that model before it is saved. A model the key cannot use lets you
  pick again; a rejected key stops before the list; Esc cancels. Groq's preset lists its current models
  (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`); it failed with "model
  llama-3.3-70b-versatile does not exist".
- The key check retries server errors (AgentRouter's intermittent 500) and treats a 429 as a working key.
- `~/.axon/.env` values ignore a trailing `# comment` and a UTF-8 BOM.

Reliability
- Every retryable AgentRouter 5xx gets the 8-retry budget (529 overload still fails over quickly). A 5xx asking for
  `reasoning_content` is repaired at once instead of retried.
- Stream errors inside an HTTP 200 (Anthropic `overloaded_error`, an OpenAI-format error chunk) are retried when
  nothing was shown yet and reported with their real status, not "HTTP 200".
- Orphaned or duplicate tool results no longer make the Anthropic API reject the request.
- An interrupted fan-out stops its subagents (they kept running in the background); a cancel arriving as a
  subagent finished could crash the turn.
- Automatic compaction is saved to the session, so a resumed session replays the compacted history.

Permissions and tools
- Patch is checked against the paths inside the diff: it could write outside the workspace in acceptEdits mode,
  and Edit deny rules did not apply to it. Plan mode asks before reading outside the workspace; Glob with an
  absolute or `../` pattern asks too. "Always allow" sticks for compound commands with glob characters.
- Patch: creating a file that exists is refused (it prepended), renames with changes work, `-U0` insertions land
  on the right line.
- Bash keeps `SSH_AUTH_SOCK` and git identity variables, keeps lines ending in `\r`, names the signal that killed
  a command, and says when a failing command printed nothing.
- Edit says when a file had to be converted to UTF-8; CodeSearch reads files with a BOM; Grep without ripgrep no
  longer hangs on a FIFO; temp files of parallel writes no longer collide.

Commands, sessions, hooks, MCP
- `/inspect`, `/explain`, `/debug` and `/bug` find saved traces after a resume; `/help <word>` searches commands;
  `/tools <name>` handles MCP schemas with list types; `/endpoint set` needs an unambiguous provider name;
  `/memory edit project` opens AXON.md; `/test -k "a and b"` keeps the quotes; `/config compact_at 85%` works;
  `/agents delete` and `/skills delete` keep a library copy first.
- `/compact` usage counts in a resumed session's cost; `/rewind` timestamps skip tool results; branch names with
  `/` are kept whole.
- Skill arguments are substituted in one pass (`$5` in an argument was eaten); frontmatter with a BOM is read;
  hooks accept snake_case event names; MCP `${VAR}` may refer to the server's own `env`, and one malformed server
  entry no longer stops all MCP servers from loading.

Terminal and input
- Enter on an empty line in the live subagent view, or a non-ASCII digit there or in pickers and vim mode, no
  longer crashes; long input with wide characters keeps the typing end visible.
- A window resize keeps paste chips and the cursor; forward-delete removes a whole paste chip.
- Print mode with `--output-format json|stream-json` prints JSON for an empty prompt or Ctrl+C, and `-c -p` no
  longer replays the conversation first. Ctrl+C during `!cmd` keeps the output captured so far.

Features and docs
- `/git` status shows renamed files by their new name; `/changes` in a repo without commits marks new files
  added; `/fix` no longer resends `/verify` failures it already handled; `/autofix` works from a subfolder and
  updates what `/fix` reads; `/verify` skips the Windows Store python stub; `/plan-resume` gives each step its
  retries again; `axon setup --provider X` keeps the configured mode and trace level.
- Docs corrected against the code (write-tool references, bypass mode and outside paths, the Bash output cap, test
  counts, `/stats`, `axon setup`), and the subagent docs cover step limits, resumes and read-only agents.

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
