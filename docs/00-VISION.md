# 00 — Vision

## The problem

`agentrouter_chat.py` is a good chat client. You type, a model answers, you read the
answer, and then **you** do the work. The model can describe the fix to a bug; it
cannot open the file, confirm the bug is where it thinks it is, apply the fix, run the
tests, or notice that its fix broke something else.

Every one of those verbs requires the same capability: the model must be able to *act
on the world and observe the result*, repeatedly, without a human in the loop for each
step. That capability is what separates a chat interface from an agent, and it is
almost entirely an engineering problem rather than a modelling one.

## What Axon is

A terminal coding assistant that closes that loop. You state an outcome; Axon
explores, plans, edits, executes, reads the output, and keeps going until the outcome
is reached or it has a concrete reason it cannot proceed.

Concretely, it is:

- A **ReAct agentic loop** — reason, act, observe, repeat — driven by the model's own
  `tool_use` requests rather than a hardcoded pipeline.
- A **tool suite** that spans the real work of coding: filesystem read/write/edit,
  shell execution, structured search, task tracking, web fetch, sub-agent dispatch.
- A **permission layer** so that a program which can run arbitrary shell commands is
  still safe to point at a real repository.
- A **context manager** so a session can run for hours without hitting the window.
- A **provider abstraction** so the same agent runs on Claude, GPT, or DeepSeek.

## Goals

| # | Goal | How it is measured |
|---|------|--------------------|
| G1 | Complete real multi-step coding tasks unattended | ≥80% pass rate on the 20-task eval suite ([`08-TESTING.md`](08-TESTING.md#layer-3--the-eval-harness)) |
| G2 | Never silently corrupt a file | 100% of stale-edit attempts rejected; zero partial writes |
| G3 | Survive long sessions | A 200-turn session never fails on context overflow |
| G4 | Be cheap enough to actually use | ≥70% cache-read ratio on turns 3+ of a session |
| G5 | Be safe by default | No filesystem write or shell command outside the workspace without explicit approval |
| G6 | Run on any of the four available models | The same eval task passes on ≥3 of 4 models |
| G7 | Be readable as a portfolio artifact | Every module under 400 lines; ≥80% test coverage on `agent/`, `tools/`, `providers/` |

## Non-goals

Named explicitly, because scope creep is how this project fails.

- **Not a framework.** No plugin marketplace, no DSL, no "bring your own agent." One
  opinionated agent that works.
- **Not an IDE extension.** Terminal only. No LSP, no editor protocol, no GUI.
- **Not a general assistant.** It is scoped to software work in a workspace directory.
- **Not multi-user or hosted.** Single user, local process, local filesystem.
- **Not a fine-tuning or RAG project.** No embeddings, no vector store. Structured
  search (`Glob`/`Grep`) beats semantic search for code, and costs nothing.
- **Not a Claude Code re-skin.** Axon does not wrap the Claude Agent SDK. The point of
  the exercise is that the harness is hand-built. Using the SDK would be the correct
  choice for a product and the wrong choice for this project.

## What "works like Claude Code" means, concretely

The phrase is doing a lot of work, so here it is decomposed into checkable claims.

| Behaviour | What it requires |
|-----------|------------------|
| Streams its reasoning and output as it goes | SSE parsing, incremental markdown rendering |
| Calls several tools at once when they are independent | Parallel `tool_use` blocks, concurrent execution, batched results |
| Asks before doing anything destructive | Permission engine with `allow`/`ask`/`deny` and modes |
| Refuses to edit a file it has not read | Per-file `(mtime, sha256)` state tracking |
| Shows a diff before applying an edit | Unified diff rendering in the tool card |
| Can be interrupted mid-task without corrupting state | Interrupt handling that still closes every open `tool_use` |
| Resumes a session after the process dies | Append-only JSONL transcript, replayed on `--resume` |
| Compacts its own history when the window fills | Token accounting plus a compaction ladder |
| Delegates wide searches to a sub-agent | `Task` tool spawning a nested loop with its own context |
| Tracks a visible plan across a long task | `TodoWrite` tool with rendered state |
| Reports what it spent | Per-call usage → cost ledger |
| Reads project conventions from a file | `AGENTS.md` discovery and injection into the system prompt |

If all thirteen rows are true, the phrase is earned. The roadmap in
[`07-ROADMAP.md`](07-ROADMAP.md) is organized so that each phase makes specific rows true.

## Success criteria

Axon is **done enough** when, from a cold start in an unfamiliar Python repository, a
single prompt like *"the CSV importer drops rows with quoted commas — find out why and
fix it, with a regression test"* results in:

1. The agent locating the relevant module without being told where it is.
2. A correct, minimal fix.
3. A new test that fails before the fix and passes after.
4. The full existing suite still passing.
5. A clear summary of what changed and why.
6. No file modified outside the repository, no command run without approval where
   approval was required.
7. Total cost under $0.50 and wall-clock under three minutes.

That is the bar. Everything in these documents exists to clear it.

---

Next: [`01-ARCHITECTURE.md`](01-ARCHITECTURE.md) — how the system is put together.
