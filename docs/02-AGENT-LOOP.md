# 02 — The Agent Loop

The loop is about 320 lines and it is the whole product. Everything else is a detail
in service of it.

## ReAct, concretely

The academic framing is *Reason → Act → Observe*, repeated. In an API-driven agent
those three steps map onto something much more mundane:

| ReAct step | What actually happens |
|---|---|
| **Reason** | The model emits `thinking` and `text` blocks explaining its plan |
| **Act** | The same response emits `tool_use` blocks and `stop_reason: "tool_use"` |
| **Observe** | You execute the tools and send back `tool_result` blocks |

The key insight — and the reason no orchestration framework is needed — is that
**the model drives the control flow.** You do not decide when to search versus edit
versus run tests. You expose capabilities, describe them well, and the model sequences
them. Your job is to run the loop faithfully and never lie to the model about what
happened.

The loop terminates when `stop_reason` is anything other than `"tool_use"`.

## The loop

```python
def run_turn(self, user_input: str) -> TurnResult:
    self.conversation.append_user(user_input)

    for iteration in range(self.settings.max_iterations):
        # ── 1. PREPARE ────────────────────────────────────────────────
        self.context.prepare(self.conversation)      # compact + cache marks

        # ── 2. REASON + ACT ──────────────────────────────────────────
        stream = self.provider.stream(
            model=self.settings.model,
            system=self.system_prompt,
            messages=self.conversation.messages,
            tools=self.registry.schemas(mode=self.mode),
            max_tokens=self.settings.max_tokens,
            effort=self.settings.effort,
            thinking=self.settings.thinking,
        )
        for event in stream:
            self.renderer.handle(event)              # live output
        turn = self.provider.finalize()

        # ── 3. RECORD (verbatim — ADR-003) ───────────────────────────
        self.conversation.append_assistant(turn)
        self.ledger.record(turn.usage)
        self.store.append(turn)

        # ── 4. TERMINATE? ────────────────────────────────────────────
        if turn.stop_reason != "tool_use":
            return self._finish(turn)

        # ── 5. OBSERVE ───────────────────────────────────────────────
        tool_uses = [b for b in turn.blocks if isinstance(b, ToolUseBlock)]
        results = self._execute_batch(tool_uses)     # never raises

        # ── 6. FEED BACK (all results, ONE message — Law 2) ──────────
        self.conversation.append_tool_results(results)
        self.store.append_results(results)

    return self._finish_exhausted()                  # iteration cap hit
```

That is it. The subtlety is entirely in `_execute_batch` and in the five laws below.

## The five laws

Each of these is a bug I would otherwise write, stated as an invariant. Each gets a
test in [`08-TESTING.md`](08-TESTING.md#invariant-tests).

### Law 1 — Pairing

> Every `tool_use` block must be answered by exactly one `tool_result` block with a
> matching `tool_use_id`, in the immediately following user message.

Miss one and the next request is malformed — the API returns 400 and the session is
unrecoverable without surgery on the transcript. This is the single most common way an
agent loop breaks, and it happens through completely reasonable-looking code: a
`continue` in the execution loop, an exception that escapes, an early `return` on a
denied permission.

The defence is structural rather than disciplinary. `_execute_batch` is written so that
the result list is built by construction:

```python
def _execute_batch(self, tool_uses: list[ToolUseBlock]) -> list[ToolResultBlock]:
    # Pre-seed one slot per tool_use. Law 1 now holds no matter what happens below.
    results: list[ToolResultBlock | None] = [None] * len(tool_uses)
    ...
    # Any slot still None at the end is filled with an error result.
    return [
        r if r is not None else ToolResultBlock(
            tool_use_id=tu.id,
            content="Tool did not complete. No result was produced.",
            is_error=True,
        )
        for r, tu in zip(results, tool_uses)
    ]
```

The list is allocated to the correct length before any tool runs. No control-flow path
can shorten it.

### Law 2 — Batching

> All `tool_result` blocks from one assistant turn go into a **single** user message.

On the Anthropic protocol this is required by the API. But it also has a behavioural
consequence that is easy to miss: if you split results across multiple user messages,
the model observes a transcript in which its parallel tool calls were answered
sequentially, and it learns from that pattern *within the session* to stop calling
tools in parallel. Performance quietly degrades and nothing errors.

The OpenAI protocol inverts this — it wants one `{"role": "tool"}` message per result.
The provider layer handles the conversion; the loop always thinks in one batch.

### Law 3 — Verbatim replay

> Append the provider's native assistant content to the conversation, never a
> re-serialization of the normalized blocks.

See [ADR-003](01-ARCHITECTURE.md#adr-003--keep-provider-native-content-alongside-normalized-blocks).
Thinking blocks carry signatures; compaction emits opaque blocks. Rebuilding the
message from `blocks` drops them and the next request fails signature verification.

### Law 4 — Errors are data

> A tool that fails produces `ToolResultBlock(is_error=True)` with a message the model
> can act on. It never raises out of the loop, and it is never silently dropped.

Good error content is a design surface, not an afterthought. Compare:

```
✗  "Error"
✗  "KeyError: 'email'"
✓  "Edit failed: the string to replace was not found in src/routes/users.py.
    The file may have changed since you read it. Re-read it and try again."
```

The third one lets the model recover on the next iteration without a human. The first
two produce a confused retry loop. Every tool's error paths get written with "what
would let the model fix this?" as the question.

### Law 5 — Interrupts still close the turn

> If the user hits `Ctrl-C` while tools are executing, every pending `tool_use` still
> gets a `tool_result` before control returns to the prompt.

This is the law that only shows up in real use. A naive implementation catches
`KeyboardInterrupt`, unwinds to the REPL, and leaves the conversation ending in an
assistant message with unanswered `tool_use` blocks. The *next* thing the user types
produces a 400 that looks completely unrelated to the interrupt.

```python
try:
    results = self._execute_batch(tool_uses)
except KeyboardInterrupt:
    results = [
        ToolResultBlock(tu.id, "Interrupted by user before completion.", is_error=True)
        for tu in tool_uses
    ]
    self.conversation.append_tool_results(results)   # close the turn, THEN unwind
    raise
```

Append first, re-raise second. The conversation is left in a valid state and the model
gets told what happened, so a follow-up message continues coherently.

## Parallel execution

One assistant turn can contain several `tool_use` blocks. Dispatch depends on whether
the batch mutates anything ([ADR-005](01-ARCHITECTURE.md#adr-005--concurrency-is-opt-in-per-tool-via-a-readonly-flag)):

```python
if all(self.registry.get(tu.name).readonly for tu in tool_uses):
    with ThreadPoolExecutor(max_workers=self.settings.max_parallel) as pool:
        futures = {pool.submit(self._run_one, tu): i
                   for i, tu in enumerate(tool_uses)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()     # slot-indexed, order preserved
else:
    for i, tu in enumerate(tool_uses):
        results[i] = self._run_one(tu)               # serial, model's order
```

Results are written into pre-indexed slots, so the returned order always matches the
`tool_use` order regardless of completion order. That keeps transcripts readable and
diffable.

The common win: the model opens a task with `Glob("**/*.py")` + `Grep("def create_user")`
+ `Read("README.md")` in one turn. Serially that is three round-trips of latency;
concurrently it is one.

`_run_one` is where the per-tool pipeline lives:

```python
def _run_one(self, tu: ToolUseBlock) -> ToolResultBlock:
    tool = self.registry.get(tu.name)
    if tool is None:
        return ToolResultBlock(tu.id, f"Unknown tool: {tu.name}", is_error=True)

    decision = self.permissions.check(tool, tu.input, mode=self.mode)
    if decision.outcome == "deny":
        return ToolResultBlock(tu.id, f"Denied: {decision.reason}", is_error=True)
    if decision.outcome == "ask":
        if not self.ui.confirm(tool, tu.input, decision):
            return ToolResultBlock(tu.id, "User declined this action.", is_error=True)

    if veto := self.hooks.pre(tool, tu.input):
        return ToolResultBlock(tu.id, f"Blocked by hook: {veto}", is_error=True)

    try:
        out = tool.run(tu.input, ctx=self.tool_context)
    except ToolError as e:
        return ToolResultBlock(tu.id, e.for_model(), is_error=True)
    except Exception as e:                      # never let an unexpected bug kill the loop
        log.exception("tool %s crashed", tu.name)
        return ToolResultBlock(tu.id, f"{type(e).__name__}: {e}", is_error=True)

    self.hooks.post(tool, tu.input, out)
    return ToolResultBlock(tu.id, self._truncate(out), is_error=False)
```

Note the bare `except Exception`. Normally a smell; here it is the point. A bug in the
`Grep` tool should degrade that one tool call, not end the user's session.

## Loop guards

An unbounded agentic loop is an unbounded bill. Four independent brakes:

| Guard | Default | Behaviour on trip |
|---|---|---|
| Iteration cap | 40 | Stop, tell the user, offer `/continue` |
| Wall-clock budget | 10 min | Same |
| Token budget | 500k for the turn | Same |
| No-progress detector | 3 identical calls | Inject a system nudge; abort on the 5th |

The no-progress detector is the interesting one. Agents get stuck in loops that are
individually reasonable — re-reading the same file, re-running a failing command
unchanged. Hash `(tool_name, canonical_json(input))` per turn and count:

```python
key = (tu.name, json.dumps(tu.input, sort_keys=True))
self.repeat_counts[key] += 1
if self.repeat_counts[key] == 3:
    self.conversation.inject_system(
        f"You have called {tu.name} with identical arguments three times and the "
        f"result has not changed. Change your approach rather than repeating it."
    )
```

A mid-conversation system message works on the Opus 5 family and costs one short
insertion. In practice this is enough to break most stuck loops.

## Sub-agents

The `Task` tool spawns a **nested loop** with its own conversation, its own iteration
budget, and a restricted tool set. Its transcript never enters the parent's context;
only its final text answer comes back as the `tool_result`.

```
parent conversation                    sub-agent conversation
├─ user: "where is auth handled?"
├─ assistant: Task(prompt="find all
│             auth-related modules")
│                                      ├─ user: <the prompt>
│                                      ├─ assistant: Glob + Grep
│                                      ├─ user: <90k tokens of matches>
│                                      ├─ assistant: Read × 4
│                                      ├─ user: <40k tokens of files>
│                                      └─ assistant: "Auth lives in
│                                          src/auth/{jwt,middleware}.py …"
├─ user: tool_result ← 400 tokens ─────┘   (130k tokens discarded)
└─ assistant: continues with a clean context
```

This is the highest-leverage context optimization in the system. A wide search costs
the parent 400 tokens instead of 130,000. It is why Claude Code can work in large
repositories without drowning.

Sub-agent constraints:

- **No `Task`.** Depth is capped at one. Recursive delegation is a token bomb.
- **Read-only by default.** A sub-agent that explores cannot also edit, unless the
  parent explicitly requests a writing sub-agent.
- **Own budget.** 20 iterations, its own token cap, its own cost line in the ledger.
- **Isolated failure.** A sub-agent that exhausts its budget returns what it found so
  far as a non-error result. Partial information beats none.

## Interaction with context management

Step 1 of the loop calls `context.prepare()` *before* every request, not after. That
ordering matters: compaction must happen while there is still room for the response,
not once the request has already been rejected. [`05-CONTEXT-AND-COST.md`](05-CONTEXT-AND-COST.md)
covers the ladder of strategies.

---

Next: [`03-TOOLS.md`](03-TOOLS.md) — what the agent can actually do.
