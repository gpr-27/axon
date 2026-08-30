# 07 — Roadmap

Eight phases. Each ends with a **single concrete acceptance test** — a command that either
works or does not. No phase is "done" because the code was written; it is done because the
test passes.

The ordering follows one rule: **get an end-to-end loop working on day two, then widen
it.** A vertical slice through every layer, however thin, surfaces integration problems
while they are still cheap. Building the tool suite first and the loop last is how this
project would fail.

| Phase | Focus | Days | Cumulative |
|:-----:|-------|:----:|:----------:|
| P0 | Vertical slice — one tool, one provider, working loop | 2 | 2 |
| P1 | The tool suite + file-state safety | 3 | 5 |
| P2 | Permission engine + the real terminal UI | 3 | 8 |
| P3 | Sessions, resume, cost ledger | 2 | 10 |
| P4 | Context management + prompt caching | 3 | 13 |
| P5 | Second provider + normalization | 2 | 15 |
| P6 | Sub-agents, todos, slash commands, hooks, plan mode | 4 | 19 |
| P7 | Hardening, eval harness, showcase | 2 | 21 |

~21 working days at a steady pace. Phases 0-3 produce something genuinely useful; 4-7 make
it good.

---

## P0 — Vertical slice (2 days)

The riskiest integration, first. Nothing else is built until a real model calls a real tool
and the loop terminates correctly.

**Build**
- `config.py` — `Settings` with `SecretStr`, `.env` loading
- `providers/base.py` — the block types and the `Provider` protocol ([the data model](01-ARCHITECTURE.md#the-internal-data-model))
- `providers/anthropic.py` — official SDK, non-streaming first, then the SSE accumulator
- `tools/base.py`, `tools/registry.py`, `tools/fs_read.py` — `Read` only
- `agent/loop.py` — the loop, with the pre-seeded result list from [Law 1](02-AGENT-LOOP.md#law-1--pairing)
- `agent/prompt.py` — a minimal system prompt
- `cli.py` — `-p/--print` one-shot mode only, no REPL

**Deliberately deferred:** streaming polish, permissions, all other tools, the REPL.

**Acceptance**
```bash
$ axon -p "read pyproject.toml and tell me the project version"
# → makes a Read tool call, receives the result, answers with the version, exits 0
```

**Why this first:** it proves the proxy passes tool calls in a real loop (not just the
one-shot probe), that the SSE accumulator reassembles `input_json_delta` fragments into
valid JSON, and that termination on `stop_reason != "tool_use"` works. Every later phase
assumes all three.

---

## P1 — The tool suite and file-state safety (3 days)

**Build**
- `Write`, `Edit`, `MultiEdit` with atomic `tmp` + `os.replace`
- `Bash` — persistent session, sentinel protocol, timeout, process-group kill, output cap
- `Glob`, `Grep` (ripgrep with Python fallback), `Ls`
- `agent/state.py` — `FileState` with `(mtime_ns, sha256)`
- `permissions/paths.py` — the path jail with `resolve()` containment
- Parallel execution for read-only batches ([ADR-005](01-ARCHITECTURE.md#adr-005--concurrency-is-opt-in-per-tool-via-a-readonly-flag))

**Acceptance**
```bash
$ cd /tmp/fixture-repo
$ axon -p "there's a bug in calc.py — divide() crashes on zero. \
           fix it and add a test, then run pytest"
# → Greps/Reads to find it, Edits calc.py, Writes a test, runs pytest, reports passing
```

Plus these must hold:
```bash
$ axon -p "edit calc.py to add a docstring"        # without reading it first
# → tool_result is_error: "You have not read calc.py in this session…"
# → the agent then Reads it and retries successfully, unprompted

$ axon -p "read /etc/passwd"
# → PermissionDenied; path resolves outside the workspace
```

The unprompted retry is the real test — it proves error messages are written well enough
for the model to self-correct, which is [Law 4](02-AGENT-LOOP.md#law-4--errors-are-data)
working.

---

## P2 — Permissions and the terminal UI (3 days)

**Build**
- `permissions/engine.py`, `permissions/rules.py` — modes, `Tool(pattern)` matching,
  deny-wins ordering, hard invariants first
- `ui/approve.py` — the approval prompt with the real command and a unified diff
- `ui/theme.py`, `ui/input.py`, `ui/picker.py` — ported from `agentrouter_chat.py`
- `ui/render.py` — streaming markdown, tool cards, spinner
- `cli.py` — the actual REPL
- The `Bash` deny list and environment scrubbing

**Acceptance**
```bash
$ axon                                   # interactive
› delete all the .pyc files in this repo
# → shows: ⏺ Bash — remove compiled Python files
#          find . -name "*.pyc" -delete
#          [y] allow once  [a] always allow  [n] deny
› n
# → tool_result is_error "User declined this action."
# → the agent acknowledges the refusal and does NOT retry the same command
```

And:
```bash
$ axon --mode plan
› refactor the auth module
# → explores with Read/Grep only; every mutating call is denied by the mode;
#   finishes by calling ExitPlanMode with a written plan
```

---

## P3 — Sessions, resume, cost (2 days)

**Build**
- `session/store.py` — append-only JSONL, `fsync`, secret redaction on write, `0600`
- `session/ledger.py` — `Decimal` cost accounting per model and per sub-agent
- `--resume` / `--continue`, `/resume` with the arrow-key picker
- `/cost` rendering

**Acceptance**
```bash
$ axon -p "start refactoring the user model, take your time"
# ... kill the process with SIGKILL mid-task ...
$ axon --continue
› what were you doing?
# → answers accurately from replayed history, including which files it had already
#   edited, and picks up where it stopped
```

`SIGKILL` specifically, not `Ctrl-C` — that tests durability, not graceful shutdown.
Then the same test with `Ctrl-C` mid-tool-execution verifies
[Law 5](02-AGENT-LOOP.md#law-5--interrupts-still-close-the-turn): the next message must not
400.

---

## P4 — Context management and caching (3 days)

The phase that separates a demo from a tool.

**Build**
- `agent/context.py` — token projection, the four-rung ladder
- `providers/capabilities.py` — probe and cache beta support
- Cache breakpoint placement: tools → system → stable prefix
- `/compact`, `/context` (a visual budget breakdown)

**Acceptance**
```bash
$ axon -p "read every .py file in this 200-file repo one at a time, \
           then summarize the architecture"
# → completes without a context-overflow error
# → the transcript shows compaction firing at ~85%
# → the post-compaction turns still reference the original goal and the todo state

$ axon -p "..." && axon /cost
# → cache hit ratio > 70% on turns 3+
```

The cache assertion is a hard gate. A silent cache miss triples the bill and produces no
error, so it becomes a monitored metric here and stays one.

---

## P5 — Second provider (2 days)

**Build**
- `providers/openai_compat.py` — raw `httpx2` with the fingerprint headers, SSE
  accumulator, indexed `tool_calls` fragment assembly
- The result-batching inversion (one message per result) from
  [the asymmetry table](04-PROVIDERS.md#the-asymmetry-table)
- `providers/registry.py` — routing and `PRICING`
- `/model` switching mid-session, dropping thinking blocks on a cross-provider switch

**Acceptance**
```bash
$ for m in claude-opus-5 gpt-5.6-sol deepseek-v4-flash; do
    axon --model $m -p "fix the failing test in /tmp/fixture-repo"
  done
# → all three complete the task; zero changes to agent/, tools/, or permissions/
```

The second clause is the real test. If adding a provider required touching the loop, the
abstraction in [ADR-003](01-ARCHITECTURE.md#adr-003--keep-provider-native-content-alongside-normalized-blocks)
is wrong and this is when it gets fixed.

---

## P6 — The rest of the Claude Code surface (4 days)

**Build**
- `agent/subagent.py` + `tools/task.py` — nested loop, depth cap 1, read-only default,
  own budget and ledger line
- `tools/todo.py` — `TodoWrite` with the single-`in_progress` constraint
- `tools/web.py` — `WebFetch` with cheap-model summarization
- `commands/builtin.py` — `/help /model /mode /clear /compact /context /cost /resume
  /tools /permissions /doctor /budget /export`
- `hooks/runner.py` — pre/post tool-use shell hooks with veto
- `AGENTS.md` discovery (walk up from cwd) and injection into the system prompt
- Plan mode + `ExitPlanMode`

**Acceptance**
```bash
$ axon -p "where is authentication handled in this codebase?"
# → dispatches a Task sub-agent
# → /context shows the parent gained < 1k tokens from a search that cost the
#   sub-agent > 50k
```

That token asymmetry is the entire justification for sub-agents; if it does not appear,
the isolation is not working.

---

## P7 — Hardening and showcase (2 days)

**Build**
- Test coverage to ≥80% on `agent/`, `tools/`, `providers/`
- The eval harness and 20 fixture tasks ([`08-TESTING.md`](08-TESTING.md#layer-3--the-eval-harness))
- `axon doctor` — environment, endpoint probes, capability report
- README demo recording; benchmark table filled with measured numbers
- `pyproject.toml` polish, `pipx`-installable

**Acceptance**
```bash
$ pytest -q                 # → all pass, coverage ≥80% on core packages
$ axon eval --all           # → ≥16/20 tasks pass
$ axon doctor               # → clean report on a fresh machine
$ pipx install .            # → works from a clean checkout, README steps verbatim
```

`axon eval --all` is what turns [`09-RESUME.md`](09-RESUME.md) from adjectives into
numbers.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|:----------:|:------:|------------|
| Proxy changes its client gate; 401s return | medium | high | `axon doctor` diagnoses in one command; `--base-url` allows pointing at the first-party API instead |
| Compaction/context-edit betas absent through the proxy | **high** | medium | Already assumed absent — client-side fallbacks are the primary path, betas are the optimization ([ADR-008](01-ARCHITECTURE.md#adr-008--capability-probing-rather-than-assumed-feature-support)) |
| Prompt caching not honoured by the proxy | medium | high | Detected by the P4 cache-ratio gate; cost projections keep an uncached column |
| Weaker models handle parallel tool use poorly | medium | low | Provider capability flag caps parallelism to 1 for those models |
| Scope creep past P7 | **high** | medium | The non-goals list in [`00-VISION.md`](00-VISION.md#non-goals) is the answer; P7 is the ship line |
| Agent damages a real repo during development | medium | high | Develop against `/tmp` fixtures; `default` mode; never `bypass` outside a container |

The second row is worth noting as a deliberate inversion: rather than hoping the betas
work and scrambling if they do not, the client-side path is built first and the betas are
treated as a bonus. That makes the high-likelihood risk a non-event.

---

Next: [`08-TESTING.md`](08-TESTING.md) — how any of this gets verified.
