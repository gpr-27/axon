# 05 — Context and Cost

A coding agent fills its own context window faster than any other kind of application.
A single `Read` of a 600-line file is ~8k tokens; a `pytest` failure dump is ~5k; a wide
`Grep` is ~15k. Twenty turns of that is 200k tokens of mostly-stale tool output. Without
active management the session dies, and the turns before it dies get progressively more
expensive because every one re-sends the whole history.

This document covers the two halves of that problem: keeping the session alive, and
keeping it cheap.

## Token accounting

**Two different numbers, never conflated.**

*Actual* usage comes from the provider's response and is the only thing used for cost.
It is never estimated.

```python
@dataclass
class Usage:
    input: int = 0; output: int = 0
    cache_read: int = 0; cache_write: int = 0; reasoning: int = 0

# Anthropic:  input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
# OpenAI:     prompt_tokens, completion_tokens, prompt_cache_hit_tokens,
#             completion_tokens_details.reasoning_tokens
```

*Projected* size is a pre-flight estimate of the next request, used to decide whether to
compact. On the Anthropic path, `client.messages.count_tokens` gives an exact answer for
one extra cheap call. Where that is unavailable, the fallback is `len(text) / 3.7` —
calibrated against measured counts on code, which is denser than prose. The fallback runs
20% conservative on purpose: over-compacting costs a little quality, under-compacting
costs the session.

Never `tiktoken`. It is the wrong tokenizer for Claude and gives confidently wrong
numbers.

## The compaction ladder

Four rungs, cheapest first. `context.prepare()` climbs only as far as it needs to.

```
context usage
   100% ─────────────────────────────────────────── hard failure
    85% ══════ RUNG 3  full compaction ═══════════
    75% ══════ RUNG 2  evict stale tool results ══
    60% ══════ RUNG 1  trim oversized outputs ════
     0% ─────────────────────────────────────────── fresh session
         RUNG 0 (always on): prompt caching
```

### Rung 0 — Prompt caching (always)

Not a compaction strategy, but it belongs first because it addresses the same cost curve
and is nearly free.

Caching is a **prefix match**. The API renders a request as `tools` → `system` →
`messages`, and a cache hit requires everything before the breakpoint to be
byte-identical to a previous request. Consequences that shape the whole design:

- **Tool schemas must be stable.** Generated descriptions, timestamps, or dict-ordering
  instability in the schema block invalidate every cache read for the session. Schemas
  are built once at startup and frozen.
- **Nothing volatile in the system prompt.** The environment preamble includes the
  working directory and the model name, which are stable, but *not* the current time or
  the token count. A clock in the system prompt costs a full cache miss per turn.
- **History grows append-only.** Any edit to an earlier message invalidates the cache
  from that point forward — which is a hidden cost of client-side compaction, and a
  reason to prefer the cheaper rungs.

Three breakpoints, at the boundaries most likely to stay stable:

```python
# 1. end of tool definitions   — never changes within a session
tools[-1]["cache_control"] = {"type": "ephemeral"}
# 2. end of the system prompt  — changes only on /model or AGENTS.md reload
system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
# 3. end of the last stable conversation prefix (excluding the newest 2 turns)
messages[stable_idx]["content"][-1]["cache_control"] = {"type": "ephemeral"}
```

The fourth breakpoint is held in reserve for a long compaction summary.

Cache reads bill at ~10% of input, writes at ~125%. So caching pays back after a single
reuse, and on a long session it dominates the bill:

| Turn | Input tokens | Uncached | With caching |
|-----:|-------------:|---------:|-------------:|
| 1 | 12,000 | $0.072 | $0.090 (write) |
| 5 | 48,000 | $0.288 | $0.041 |
| 20 | 165,000 | $0.990 | $0.113 |
| 40 | 310,000 | $1.860 | $0.198 |
| **Session total** | | **~$28** | **~$3.10** |

Roughly a 9× reduction. Verified by asserting `usage.cache_read > 0` on turn 3+ — a
silent cache miss is the most expensive bug in the system, so it is a monitored metric,
not an assumption. `/cost` shows the hit ratio.

### Rung 1 — Trim oversized outputs (60%)

Individual tool results over 10k tokens are the low-hanging fruit. Re-truncate the
largest ones in place, harder than their original cap, keeping head and tail:

```
[… 4,200 lines elided by Axon to save context. Re-run the command or
   Read the file with an offset to see the middle. …]
```

Cheap, lossy in a way the model can recover from, and it usually buys enough room to
avoid the rungs above. It does invalidate the cache from the earliest trimmed message,
so trimming is batched — several results at once rather than one per turn.

### Rung 2 — Evict stale tool results (75%)

The observation that makes this work: **in a coding agent, old tool results are almost
always dead weight.** A `Grep` from turn 3 that led to reading a file has served its
purpose; the file contents matter, the search that found them does not. Meanwhile the
model's own reasoning and the user's instructions stay relevant throughout.

So evict tool results older than N turns, keeping the `tool_use` blocks that requested
them so the transcript stays coherent and Law 1 is not violated:

```python
ToolResultBlock(
    tool_use_id=original_id,
    content="[Result cleared to reclaim context. Re-run this tool if you need it again.]",
    is_error=False,
)
```

Where `clear_tool_uses_20250919` is available, the server does this — cheaper and better
targeted, and it preserves the cache prefix. Where it is not, the client-side version
above is equivalent in effect.

Never evicted: the most recent 3 turns, anything from `TodoWrite`, and any result the
model has referenced in later text.

### Rung 3 — Full compaction (85%)

The last resort, because it is the most lossy and it destroys the cache prefix entirely.

Where the `compact_20260112` beta is available, the server compacts and returns opaque
blocks that must be appended verbatim to the conversation
([Law 3](02-AGENT-LOOP.md#law-3--verbatim-replay)).

Otherwise, client-side: summarize the oldest ~70% of history with a cheap model into a
structured hand-off, and replace it with a single synthetic user message.

The summary is structured rather than prose, because a prose summary of a coding session
loses exactly the details that matter:

```markdown
## Session summary (turns 1-28, compacted)

### Goal
<the user's original request, verbatim — never paraphrased>

### Files read
src/routes/users.py, src/models/user.py, tests/test_users.py

### Files modified
- src/routes/users.py:47 — added a guard for missing `email`

### Commands run and outcomes
- `pytest tests/test_users.py` → 11 passed, 1 failed (test_missing_email)
- `pytest tests/test_users.py` → 12 passed

### Established facts
- Validation lives in `src/validators.py`, not in the route handlers
- The project uses Flask 3.x with blueprints registered in `src/app.py`
- Tests run with `pytest -q` from the repository root

### Open work
<current TodoWrite state, verbatim>

### Next step
Add a regression test for the empty-string case.
```

Preserved verbatim across compaction, never summarized: the original user goal, the
`TodoWrite` state, the `FileState` map, and the last 3 turns. Those four things are the
agent's identity across the boundary; lose them and it starts over.

## Cost ledger

Per-call accounting, aggregated per session and per sub-agent.

```python
def record(self, model: str, u: Usage) -> Decimal:
    p = PRICING[model]
    cost = (Decimal(u.input)      / 1_000_000 * p["input"]
          + Decimal(u.output)     / 1_000_000 * p["output"]
          + Decimal(u.cache_read) / 1_000_000 * p["cache_read"]
          + Decimal(u.cache_write)/ 1_000_000 * p["input"] * Decimal("1.25"))
    ...
```

`Decimal`, not `float` — money. Sub-agent costs are attributed to their own line and
rolled into the parent total, so `/cost` shows where spend actually went:

```
/cost
  Session 01JQ8… · 34 min · 41 turns
  ┌──────────────────┬──────────┬──────────┬───────────┬─────────┐
  │ model            │    input │   output │ cache rd  │    cost │
  ├──────────────────┼──────────┼──────────┼───────────┼─────────┤
  │ claude-opus-5    │   41,203 │   28,914 │   612,880 │ $1.4823 │
  │ deepseek-v4-…    │   18,440 │    2,106 │         0 │ $0.0495 │
  ├──────────────────┼──────────┼──────────┼───────────┼─────────┤
  │ total            │   59,643 │   31,020 │   612,880 │ $1.5318 │
  └──────────────────┴──────────┴──────────┴───────────┴─────────┘
  cache hit ratio 91.2%   ·   est. without caching  $4.87
  sub-agents: 3 (Task) · $0.0495
```

Showing the counterfactual makes the caching work visible, which is both satisfying and
the thing that catches a regression when the ratio drops.

## Proxy-injected preamble

A measured anomaly worth recording. On an identical ~20-token prompt with one tool:

| Model | `prompt_tokens` |
|---|---:|
| `deepseek-v4-flash` | 362 |
| `gpt-5.6-sol` | **4,434** |

`gpt-5.6-sol` appears to carry roughly 4,000 tokens of proxy-injected preamble on every
request. At $3/1M that is ~$0.013 of unavoidable overhead per call — negligible once,
material across a 40-turn agentic session (~$0.53 before any real work).

Two consequences: cost projections for that model add a 4k floor per call, and it is a
poor choice for high-frequency cheap sub-tasks despite its headline price.
`deepseek-v4-flash` is the better summarizer on both price and overhead.

## Budgets and pre-flight checks

Before each request, `context.prepare()` also enforces hard limits — an agent left
running unattended should not be able to spend arbitrarily:

| Limit | Default | On trip |
|---|---|---|
| Per-turn token budget | 500k | Stop, report, offer `/continue` |
| Per-session cost ceiling | $5.00 | Stop, require explicit raise |
| Single tool result | 30k chars | Truncate with a marker |
| Projected request size | 80% of window | Climb the ladder |

The cost ceiling is a soft stop with a clear message, not a crash. `/budget 20` raises
it for the session.

---

Next: [`06-SECURITY.md`](06-SECURITY.md) — why it is safe to give this program a shell.
