# Changelog

A timestamped record of every change made to this repository — what changed, when,
and why. Newest entries at the top.

**Convention.** Every editing session appends a `### HH:MM TZ — <summary>` block under
the current date, with one table row per file touched. Rows use `created` /
`modified` / `deleted` / `renamed`. No change ships without a row here. Newest date at
the top; entries within a date run in the order they happened.

---

## 2026-08-27

### 16:22 IST — Verified agentrouter transport capabilities

Ran three throwaway probes against `agentrouter.org` to confirm the provider design
is buildable before committing it to the docs. Findings recorded in
[`docs/04-PROVIDERS.md`](docs/04-PROVIDERS.md#verified-probe-results).

| File | Change |
|------|--------|
| _(none — probes were throwaway scripts in `/tmp`, not kept)_ | — |

**Results:**

| Transport | Client | Outcome |
|---|---|---|
| `POST /v1/messages` · `claude-opus-5` | official `anthropic` SDK | ✅ 200 · `stop_reason=tool_use` · `input` parsed as object |
| `POST /v1/chat/completions` · `gpt-5.6-sol` | official `openai` SDK | ❌ 401 `unauthorized_client_error` |
| `POST /v1/chat/completions` · `gpt-5.6-sol` | raw `httpx2` + Anthropic UA headers | ✅ 200 · `finish_reason=tool_calls` |
| `POST /v1/chat/completions` · `deepseek-v4-flash` | raw `httpx2` + Anthropic UA headers | ✅ 200 · reported cache hits + reasoning tokens |
| either endpoint · no fingerprint headers | plain `httpx` | ❌ 401 on all 4 models |

**Conclusion:** the proxy gates on `user-agent: Anthropic/Python*` plus `x-stainless-*`.
Tool calling works on both protocols. The Anthropic path uses the official SDK; the
OpenAI-compatible path must use raw `httpx2` with the header set carried over from
`agentrouter_chat.py`.

**Incidental finding:** `gpt-5.6-sol` reported `prompt_tokens: 4434` for a ~20-token
prompt, versus `362` for `deepseek-v4-flash` on the identical request. The proxy
appears to inject a large preamble for that model. Flagged as a cost caveat in
[`docs/05-CONTEXT-AND-COST.md`](docs/05-CONTEXT-AND-COST.md#proxy-injected-preamble).

---

### 16:25 IST — Project scaffolded; planning documentation authored (00-07)

Created the `axon/` project folder and the first eight design documents. **No
implementation code** — design only, pending review.

| File | Change |
|------|--------|
| `README.md` | created — product framing, architecture diagram, doc index, lineage from `agentrouter_chat.py` |
| `CHANGELOG.md` | created — this file; establishes the timestamped-record convention |
| `docs/00-VISION.md` | created — problem statement, goals, non-goals, success criteria |
| `docs/01-ARCHITECTURE.md` | created — layer model, module map, internal data model, 10 decision records |
| `docs/02-AGENT-LOOP.md` | created — ReAct engine, five loop invariants, parallel execution, interrupts, sub-agents |
| `docs/03-TOOLS.md` | created — 13 tool contracts with JSON Schemas, read-before-edit invariant |
| `docs/04-PROVIDERS.md` | created — protocol asymmetry table, streaming accumulators, probe results, capability probing |
| `docs/05-CONTEXT-AND-COST.md` | created — token accounting, compaction ladder, cache breakpoint placement, cost ledger |
| `docs/06-SECURITY.md` | created — threat model, path jail, command policy, prompt-injection posture, secret handling |
| `docs/07-ROADMAP.md` | created — 8 phases with acceptance tests, ~21 working days |

---

### 16:58 IST — Documentation set completed (08-09)

Wrote the final two documents, closing every cross-reference the earlier docs pointed
at. The documentation set is now complete: 12 files, ~2,880 lines.

| File | Change |
|------|--------|
| `docs/08-TESTING.md` | created — three test layers (FakeProvider / SSE cassettes / eval suite), invariant tests for the five loop laws, table-driven permission matrix, 20-task eval harness with anti-cheat checks, CI config |
| `docs/09-RESUME.md` | created — positioning line, five resume bullets, 90-second demo script, seven interview questions with prepared answers, README opening structure |
| `CHANGELOG.md` | modified — corrected the 16:25 entry, which listed `08-TESTING.md` and `09-RESUME.md` as created before they actually were; they are now recorded at their real timestamps. Clarified the ordering convention. |

**Note on `docs/09-RESUME.md`:** every metric in it is a `‹measured›` placeholder, not a
number. They are to be filled from a real `axon eval --all` scorecard once P7 runs —
deliberately, so no resume claim exists that cannot be defended.

**Still blocked:** no implementation code, per instruction. Next step is review of the
plan, then [P0](docs/07-ROADMAP.md#p0--vertical-slice-2-days).

---

### 17:14 IST — Fixed three broken cross-document anchors

Ran a link validator over all 12 files (66 internal links). Three anchors pointed at
headings that did not exist under GitHub's slug rules. All 66 now resolve.

| File | Change |
|------|--------|
| `docs/00-VISION.md` | modified — G1 row: `08-TESTING.md#the-eval-harness` → `#layer-3--the-eval-harness` |
| `docs/07-ROADMAP.md` | modified — P7 build list: same anchor correction |
| `docs/09-RESUME.md` | modified — ADR-001 link corrected to the real heading text (`#adr-001--hand-build-the-harness-do-not-use-the-claude-agent-sdk`) |

---

### 17:27 IST — Reframed toward product parity; added implementation-level specs (10-13)

**Why.** Feedback: the docs were reading as a derivative of `agentrouter_chat.py`, and the
ask is a real product in the Claude Code / Cursor class. Two genuine gaps behind that:
(1) no document mapped Claude Code's actual feature surface onto Axon, so "works like
Claude Code" was an adjective rather than a checklist; (2) the docs were design-level —
`01-ARCHITECTURE.md` names 30 modules with line estimates, which is a map, not a build
spec, so "how do we implement all files" was unanswered.

| File | Change |
|------|--------|
| `docs/10-CLAUDE-CODE-PARITY.md` | created — Claude Code's surface across 10 areas (tools, loop, permissions, ~40 slash commands, memory, hooks, sessions, interface, ecosystem) mapped to Full / Partial / Deferred / Out, with a reason per gap and a parity scorecard. Records that the agent-loop row is the only one with zero gaps, deliberately. |
| `docs/11-FILE-SPECS.md` | created — **the build spec**. Every file: complete public API signatures, allowed and forbidden imports, invariants, LOC target, phase. Plus the layer dependency diagram, the 22-step build order, and the `tests/` mirror layout. |
| `docs/12-SYSTEM-PROMPT.md` | created — the system prompt in full (5 blocks, cache-ordered), the failure each operating rule prevents, environment preamble with the never-include list, `AGENTS.md` discovery and trust-bounded framing, the three mode variants, and how to iterate on it against the eval suite. |
| `docs/13-UI-AND-CLI.md` | created — invocation surface and flags, the annotated rendered turn, `Renderer`/`MarkdownStream`/diff rendering, paste-burst input, exact `Ctrl-C` semantics, approval prompt with rule derivation, all 18 slash commands incl. the `/context` mockup, the 5 hook events, and the editor-surface + codebase-indexing tracks as explicit post-v1. |
| `docs/10-CLAUDE-CODE-PARITY.md` | modified — fixed a placeholder link left in the `06-SECURITY.md` reference |
| `README.md` | modified — opening reframed to the Claude Code / Cursor product class with the engine-vs-surface distinction; added a "What it can do" capability table (read / write / execute / search / explore); doc index extended to 14 files with three reading paths; **Lineage rewritten** — `agentrouter_chat.py` is now correctly positioned as where API access was proven and where terminal ergonomics carry over from, not as the thing being extended |

**Scope note.** Cursor's real differentiator is codebase indexing (embeddings + vector
retrieval) and an editor surface. Both are recorded as post-v1 tracks in
[`docs/13-UI-AND-CLI.md`](docs/13-UI-AND-CLI.md#where-the-surface-goes-next) rather than
folded into v1 — the engine is surface-agnostic by construction, so each is an addition
rather than a rewrite, but a repo-wide index would change the retrieval design and guessing
at it now would be wrong.

Documentation set is now 14 files, ~4,350 lines. **Still no implementation code.**

---

## Template for future entries

```markdown
### HH:MM TZ — <one-line summary of the change>

<Optional: why this change was made, and anything a future reader needs to know.>

| File | Change |
|------|--------|
| `path/to/file.py` | created — what it does |
| `path/to/other.py` | modified — what changed and why |
| `path/to/gone.py`  | deleted — why it was removed |
```
