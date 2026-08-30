# Axon

**A terminal-native agentic coding assistant.** Axon reads your codebase, plans changes,
edits files, runs commands, reads the output, and iterates — until the task is actually done.

It is a from-scratch implementation of the class of product Claude Code and Cursor's agent
mode belong to: a ReAct agentic loop over a real tool suite, with a permission engine,
context compaction, prompt caching, session persistence, and sub-agents. No agent framework.
No LangChain. Every layer is hand-built and understood.

The reference model is **Claude Code** — its tool set, its permission semantics, its
`Tool(pattern)` rule syntax — mapped feature by feature in
[`docs/10-CLAUDE-CODE-PARITY.md`](docs/10-CLAUDE-CODE-PARITY.md). Claude Code and Cursor's
agent mode share the same engine; what differs is the surface. Axon builds the engine, with
a terminal as v1's surface and an editor surface as a second front-end on the same core
([where this goes next](docs/13-UI-AND-CLI.md#where-the-surface-goes-next)).

> **Status: planning.** This repository currently contains the design only.
> No implementation code has been written yet. See [`docs/07-ROADMAP.md`](docs/07-ROADMAP.md).

---

## What it will look like

```
$ axon
  ● axon  claude-opus-5   ~/projects/api   main ✓

› the /users endpoint 500s when the email field is missing — find it and fix it

  ⏺ Grep  pattern:"def create_user" glob:"**/*.py"
    └ 1 match · src/routes/users.py:42

  ⏺ Read  src/routes/users.py  offset:30 limit:40
    └ 40 lines

  I found it. Line 47 indexes payload["email"] directly, so a missing key raises
  KeyError before validation runs. The handler needs to validate first.

  ⏺ Edit  src/routes/users.py
    ├ - email = payload["email"]
    ├ + email = payload.get("email")
    ├ + if not email:
    ├ +     return jsonify(error="email is required"), 400
    └ 4 insertions, 1 deletion

  ⏺ Bash  pytest tests/test_users.py -q
    └ 12 passed in 0.84s

  Fixed. The missing-email case now returns 400 with a clear message instead of
  crashing. All 12 tests in the users suite pass.

  ─ 4 tools · 18.2k in · 1.1k out · 14.8k cached · $0.041 · 22s
```

---

## The design in one diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  CLI / REPL          arg parsing · slash commands · rendering    │
├──────────────────────────────────────────────────────────────────┤
│  AGENT LOOP          the ReAct engine — think → act → observe    │
│    ├── Context Manager      token budget · compaction · caching  │
│    ├── Permission Engine    allow / ask / deny · modes · rules    │
│    ├── Tool Registry        13 tools · JSON Schema · concurrency  │
│    └── Session Store        append-only JSONL · resume · ledger   │
├──────────────────────────────────────────────────────────────────┤
│  PROVIDER LAYER      one internal format, two wire protocols     │
│    ├── AnthropicProvider    /v1/messages    · tool_use blocks    │
│    └── OpenAIProvider       /v1/chat/...    · tool_calls array   │
└──────────────────────────────────────────────────────────────────┘
                              ↓
                       agentrouter.org
        claude-opus-5 · claude-opus-4-8 · gpt-5.6-sol · deepseek-v4-flash
```

The load-bearing idea: **the agent loop never knows which provider it is talking to.**
Both wire protocols normalize into one set of internal block types, so the loop, the
tools, and the permission engine are written once and work everywhere.

---

## Documentation

Read in order. Each document is self-contained and cross-linked.

| # | Document | What it covers |
|---|----------|----------------|
| — | [`CHANGELOG.md`](CHANGELOG.md) | Timestamped record of every change made to this repo |
| 00 | [`docs/00-VISION.md`](docs/00-VISION.md) | The problem, goals, explicit non-goals, success criteria |
| 01 | [`docs/01-ARCHITECTURE.md`](docs/01-ARCHITECTURE.md) | Layers, module map, data model, decision records |
| 02 | [`docs/02-AGENT-LOOP.md`](docs/02-AGENT-LOOP.md) | The ReAct engine, the five invariants, interrupts, sub-agents |
| 03 | [`docs/03-TOOLS.md`](docs/03-TOOLS.md) | All 13 tool contracts, schemas, read-before-edit enforcement |
| 04 | [`docs/04-PROVIDERS.md`](docs/04-PROVIDERS.md) | Protocol normalization, streaming, verified probe results |
| 05 | [`docs/05-CONTEXT-AND-COST.md`](docs/05-CONTEXT-AND-COST.md) | Compaction, caching, token accounting, cost ledger |
| 06 | [`docs/06-SECURITY.md`](docs/06-SECURITY.md) | Threat model, path jail, command policy, prompt injection |
| 07 | [`docs/07-ROADMAP.md`](docs/07-ROADMAP.md) | 8 phases, acceptance test per phase, ~21 days |
| 08 | [`docs/08-TESTING.md`](docs/08-TESTING.md) | FakeProvider, cassettes, invariant tests, eval harness |
| 09 | [`docs/09-RESUME.md`](docs/09-RESUME.md) | How to present this: bullets, demo script, interview prep |
| 10 | [`docs/10-CLAUDE-CODE-PARITY.md`](docs/10-CLAUDE-CODE-PARITY.md) | Claude Code's full feature surface → what Axon builds, area by area |
| 11 | [`docs/11-FILE-SPECS.md`](docs/11-FILE-SPECS.md) | **Every file**: public API, allowed imports, invariants, build order |
| 12 | [`docs/12-SYSTEM-PROMPT.md`](docs/12-SYSTEM-PROMPT.md) | The system prompt in full, and why each rule exists |
| 13 | [`docs/13-UI-AND-CLI.md`](docs/13-UI-AND-CLI.md) | Terminal rendering, input handling, slash commands, hooks |

**Reading paths.** To understand the design: 00 → 01 → 02 → 04.
To understand scope: 10, then 00's non-goals.
To start building: 11 (file specs) alongside 07 (phase order).

---

## Why this is worth building

Most "I built an AI agent" projects are a `while` loop around one API call with two
toy tools. The hard parts — the parts that make a coding agent actually usable — are
the ones nobody implements:

- **Correctness under mutation.** An agent that edits files must know whether the file
  changed since it last read it. Axon tracks `(mtime, sha256)` per file and refuses
  stale edits. See [Read-before-edit](docs/03-TOOLS.md#the-read-before-edit-invariant).
- **Protocol asymmetry.** Anthropic wants every tool result batched into one message;
  OpenAI wants one message per result. Get this wrong and the model silently stops
  calling tools in parallel. See [the asymmetry table](docs/04-PROVIDERS.md#the-asymmetry-table).
- **Surviving a long session.** A coding agent fills its context with tool output.
  Without compaction it dies at turn 40. See [Context management](docs/05-CONTEXT-AND-COST.md).
- **Not destroying the user's machine.** `Bash` is arbitrary code execution by
  construction. See [the threat model](docs/06-SECURITY.md).
- **Paying attention to cost.** Prompt caching cuts the bill by ~90% on a long
  session, but only if the prefix is stable and ordered correctly.

Each of those is a real engineering problem with a real solution documented here.
That is the difference between a demo and a product.

---

## Prerequisites

- Python 3.12+ (developed on 3.14.5)
- An `agentrouter.org` API key, exported as `AXON_API_KEY`
- Optional: `ripgrep` on `PATH` (Grep falls back to a pure-Python scanner without it)

```bash
cp .env.example .env      # then edit .env — it is gitignored
export AXON_API_KEY=sk-...
```

> **Security note.** The predecessor script `agentrouter_chat.py` has its API key
> hardcoded in plaintext. Axon loads the key from the environment only, and never
> logs it. That key should be rotated before this repo is ever made public — see
> [`docs/06-SECURITY.md`](docs/06-SECURITY.md#secret-handling).

---

## What it can do

The capability surface, concretely. Full contracts in
[`docs/03-TOOLS.md`](docs/03-TOOLS.md), per-file build specs in
[`docs/11-FILE-SPECS.md`](docs/11-FILE-SPECS.md).

| Capability | Tools | Notes |
|---|---|---|
| **Read any file** | `Read` | Line offsets, `cat -n` numbering, 2000-line default |
| **Write and edit code** | `Write`, `Edit`, `MultiEdit` | Atomic writes; refuses stale edits |
| **Run commands** | `Bash` | Persistent shell, timeout, process-group kill, output cap |
| **Find files** | `Glob` | mtime-descending, so recent work surfaces first |
| **Search code** | `Grep` | ripgrep with a pure-Python fallback |
| **List directories** | `Ls` | |
| **Explore unfamiliar code** | `Task` | Sub-agent with isolated context; returns only conclusions |
| **Track multi-step work** | `TodoWrite` | Survives context compaction |
| **Read the web** | `WebFetch` | Summarized by a cheap model, framed as untrusted |
| **Plan before acting** | `ExitPlanMode` | Paired with `plan` mode, where nothing mutates |
| **Diagnose itself** | `Doctor` | Endpoint, capability, and environment report |

---

## Lineage

Axon is a new codebase, not a refactor. It began after `agentrouter_chat.py` — a 1,155-line
single-file chat REPL — proved out API access to the four models and hit its natural ceiling:
it could talk about code but could not *act* on it, because it had no tools, no loop, and no
way to observe the result of anything.

What carries over is terminal ergonomics that were already solved there: the ANSI theme
system, paste-burst-aware input, the arrow-key model picker, and per-turn cost accounting.
Everything else — the agent loop, the tool suite, permissions, context management, the
provider abstraction — is new work specified in these documents.
