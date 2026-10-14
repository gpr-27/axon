# Changelog

## 0.28.0

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
- The developer scripts moved to `scripts/` (`setup_env.py`, `axon_run.py`, `check_models.py`, `gen_docs.py`).
  `pyproject.toml` is now the only dependency list.
- New documentation in `docs/` (architecture, configuration, commands, extending, Windows, testing), with
  reference tables generated from the code by `scripts/gen_docs.py`.

## 0.27.8

- Windows optimizations and a streamlined quick start.

## 0.27.7

- Provider-specific models, API key verifier, enhanced fuzzy picker and session tags.
