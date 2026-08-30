# 09 — Resume and Presentation

This document is about how to present Axon once it exists. It is written last because
everything in it depends on the finished thing being real.

> **A rule for this file.** Every number below is a placeholder written as
> `‹measured›`. Fill them from an actual `axon eval --all` scorecard and an actual
> `/cost` output — not from the illustrative figures used elsewhere in these docs. A
> resume bullet with an invented number is a bullet you cannot defend in an interview,
> and the entire value of this project as a credential is that you can defend every
> claim in it.

## The positioning line

One sentence, used at the top of the README and as the answer to "what did you build?":

> **Axon** — a terminal coding agent that reads, edits, and runs code in your repository
> through a ReAct tool-use loop, with a permission engine, context compaction, and one
> agent core that runs unchanged across four models on two incompatible wire protocols.

What that sentence is doing: it names the *hard* parts (permissions, compaction, protocol
normalization) rather than the visible ones (a CLI, a chat UI). Anyone who has built an
agent recognizes those three as the parts that are actually difficult; anyone who has not
still reads it as substantial.

Avoid: "a Claude Code clone." It invites comparison on completeness, which you lose, and
it hides the engineering, which is what you want discussed. "A coding agent I built to
understand how they work, then hardened until I could use it daily" is the honest and
stronger framing.

## Resume bullets

Four to five bullets, each with a measured number, ordered by how much engineering they
imply. Trim to three if space is short — keep the first, second, and last.

> **Axon — Terminal coding agent** · Python 3.12 · `github.com/‹you›/axon`
>
> - Built an autonomous coding agent on a **ReAct tool-use loop** with ‹13› tools
>   (file read/write/edit, persistent shell, ripgrep search, sub-agent dispatch);
>   solves **‹18›/20** benchmark tasks on a held-out fixture suite at **$‹0.21›** and
>   **‹11›** model turns per task.
> - Designed a **provider abstraction** normalizing Anthropic Messages and
>   OpenAI-compatible protocols — divergent in tool-argument encoding, result batching,
>   and streaming-delta structure — so the agent core runs across **4 models with zero
>   changes** above the provider boundary.
> - Cut long-session cost **‹9›×** (**$‹28›→$‹3.10›** per 40-turn session) with
>   prefix-stable **prompt caching** (‹91›% hit ratio) and a four-tier context ladder
>   that keeps ‹200›k-token sessions inside a ‹200›k window.
> - Implemented a **capability-based permission engine** — path jail with symlink
>   resolution, structural command deny-list, four trust modes — and a
>   **read-before-edit invariant** enforced by `(mtime, sha256)` state tracking that
>   makes stale-write corruption structurally impossible.
> - Achieved **‹82›% test coverage** on the agent core by testing the loop against a
>   scripted fake provider and the protocol parsers against recorded SSE cassettes —
>   deterministic, zero network, zero API cost in CI.

Why these five: each names a *problem* and a *mechanism*, not a technology list. "Used the
Anthropic SDK" is not an accomplishment; "normalized two protocols that disagree on result
batching" is. The last bullet is the one senior engineers notice, because "how do you test
a non-deterministic agent" is the question they would have asked next.

## What to demo

A 90-second terminal recording, in the README as an animated GIF or asciinema cast. One
take, no cuts, real repository, real latency.

```
1. axon                                    (0:00)  — cold start in a repo you didn't write
2. › there's a bug in the CSV importer, rows with quoted commas get dropped.
     find it, fix it, add a test                    — one instruction, then hands off
3. Grep → Read → Read                     (0:15)  — it locates the bug itself
4. ⏺ Edit  src/importer.py                (0:35)  — the diff renders inline
   [y] allow once  [a] always  [n] deny            — the approval gate is visible
5. ⏺ Bash  pytest tests/ -q               (0:55)  — it verifies its own work
   12 passed
6. › /cost                                (1:20)  — $0.14 · cache hit 89%
```

Every beat is deliberate. Step 2 shows it works from a single instruction. Step 3 shows
navigation, not just editing. Step 4 shows you thought about safety. Step 5 shows the
agent closes its own loop. Step 6 shows you measured. Do not demo the REPL's colours.

Also worth recording separately, if the first is tight: the read-before-edit rejection and
the agent's unprompted self-correction. It is fifteen seconds and it demonstrates
[Law 4](02-AGENT-LOOP.md#law-4--errors-are-data) better than any paragraph.

## Interview questions this project invites

Prepare these. They will be asked, and the project is only a credential if the answers are
fluent.

**"Walk me through what happens when I type a request."**
The whole loop, out loud: build the request (system prompt, tool schemas, history) → stream
→ accumulate deltas into blocks → if `stop_reason == "tool_use"`, check permissions,
execute (parallel when all read-only), append *all* results in one user message → loop →
terminate on `end_turn`. Then the guards: iteration cap, token budget, no-progress
detector. Roughly 90 seconds of speech. Practise it.

**"How do you test something non-deterministic?"**
Separate the model's choices from the harness. The harness is deterministic and holds all
the bugs, so a fake provider replaying scripted turns tests the loop, the tools, and the
permission engine with no network. Protocol parsers get recorded SSE cassettes. Model
quality is a separate eval suite with a pass rate, not a unit test.
([`08-TESTING.md`](08-TESTING.md))

**"What was the hardest bug?"**
Have one real answer. The strongest candidate is the pairing invariant: an unhandled tool
exception, or a `Ctrl-C` during execution, leaves a `tool_use` with no `tool_result`, and
every subsequent request 400s with a message that points at the *conversation*, not at the
tool that crashed twenty seconds earlier. The fix is structural rather than defensive —
pre-seed a result slot per `tool_use` and fill it on every path including the interrupt
path — which is the more interesting half of the story.
([Law 1](02-AGENT-LOOP.md#law-1--pairing), [Law 5](02-AGENT-LOOP.md#law-5--interrupts-still-close-the-turn))

**"Why not use LangChain / the Agent SDK?"**
Answer on the merits, without disparaging either. The Agent SDK *is* this, done properly —
using it would have produced a working tool and no understanding, and the point was the
understanding. A framework would also have hidden the exact three things that turned out to
be the hard parts: cache-prefix stability, protocol asymmetry, and the pairing invariant.
Also note where you *would* reach for the SDK: shipping to a team on a deadline.
([ADR-001](01-ARCHITECTURE.md#adr-001--hand-build-the-harness-do-not-use-the-claude-agent-sdk))

**"How do you keep it from destroying my machine?"**
Layered, and honest about the top layer. Hard invariants that no config can override (path
jail with `resolve()` before containment, structural deny-list), then deny-before-allow
rules, then mode defaults, then a human prompt showing the *real* command. Then the
limitation, volunteered rather than extracted: this is policy inside one process, not a
sandbox — an approved `pytest` still runs as the user, and OS-level containment is the
next piece of work. Volunteering the gap reads as engineering judgment; being caught
without it reads as the opposite. ([`06-SECURITY.md`](06-SECURITY.md))

**"What does prompt caching require of the design?"**
That the request prefix be byte-identical turn to turn — which means frozen tool schemas,
no clock in the system prompt, append-only history, and a real cost to client-side
compaction because editing old messages invalidates everything after it. The design
consequence is the four-rung ladder: use the cheap rungs first specifically because they
preserve more of the prefix.
([`05-CONTEXT-AND-COST.md`](05-CONTEXT-AND-COST.md#rung-0--prompt-caching-always))

**"What would you do differently?"**
Have a real answer here too — "nothing" is a bad signal. Candidates: start with the
provider abstraction rather than retrofitting it (ADR-003 was discovered, not planned);
build the eval harness in P1 instead of P7, because without it every "is this better?"
question was answered by vibes for three weeks; and skip the second provider until the
first was fully hardened.

## The README's opening 200 words

Most readers stop there, so it carries the whole project. Order that works:

1. **One sentence** on what it is (the positioning line).
2. **The demo GIF**, immediately — before any prose. It is the only thing that proves the
   claim.
3. **Three bullets** of what is interesting, not what is included: the protocol
   normalization, the cost engineering, the permission model.
4. **Install and run**, four lines, working.
5. **The benchmark table** with real numbers.
6. Then the architecture and doc links for the minority who keep reading.

What not to open with: a feature checklist, a comparison table against Claude Code, or an
explanation of what an AI agent is.

## Honesty in the presentation

Two things belong in the README explicitly, because a reader who discovers them
independently discounts everything else on the page:

- **What it does not do.** No OS sandbox, no IDE integration, no MCP, single-repository
  scope. The [non-goals list](00-VISION.md#non-goals) is already written; a short version
  of it in the README converts a potential criticism into evidence of scope discipline.
- **What it borrows.** The tool set, the permission model, and the `Tool(pattern)` rule
  syntax are modelled on Claude Code, deliberately, so the mental model transfers. Saying
  so is strictly better than being told so. The terminal UI derives from
  `agentrouter_chat.py`, the project's own predecessor — noted in the
  [lineage section](../README.md#lineage).

The measured numbers do the rest of the work. A project that reports 18/20 with the two
failures named is more credible than one that reports 20/20.

---

That is the complete plan: [`00-VISION.md`](00-VISION.md) through this file. Nothing here
has been implemented yet — the next step is
[P0](07-ROADMAP.md#p0--vertical-slice-2-days), and the acceptance test for P0 is one
command.
