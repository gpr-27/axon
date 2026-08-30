# 08 — Testing

Testing an LLM agent has a reputation for being impossible: the model is
non-deterministic, calls cost money, and behaviour changes between versions. Most agent
projects respond by not testing at all.

That reputation confuses two different things. The *model's choices* are
non-deterministic. The *harness* — the loop, the tools, the permission engine, the
protocol translation — is ordinary deterministic software, and it is where essentially
all the bugs live. Separating those two is the whole testing strategy.

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1 — Harness tests      no network, no cost, deterministic│
│    FakeProvider replays scripted turns. ~85% of the test suite.  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2 — Protocol tests     recorded SSE cassettes, no network │
│    Real captured bytes, replayed. Catches provider drift.        │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3 — Eval suite         real models, real cost, scored     │
│    20 fixture tasks. Measures capability, not correctness.        │
└─────────────────────────────────────────────────────────────────┘
```

## Layer 1 — FakeProvider

The keystone. Because `Provider` is a `Protocol` and the loop depends only on it, a
test-double that replays a scripted list of `AssistantTurn`s makes the entire agent loop
testable with zero network and zero cost.

```python
class FakeProvider:
    """Replays scripted turns. Records what it was asked, so tests can assert on
    the request shape as well as the loop's behaviour."""

    name = "fake"

    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.requests: list[dict] = []          # every call, for assertions

    def stream(self, **kw):
        self.requests.append(kw)
        turn = self._turns.pop(0)
        for b in turn.blocks:                   # emit plausible stream events
            if isinstance(b, TextBlock):
                yield TextDelta(b.text)
            elif isinstance(b, ToolUseBlock):
                yield ToolUseStart(b.id, b.name)
                yield ToolArgsDelta(b.id, json.dumps(b.input))
                yield ToolUseComplete(b.id)
        yield TurnComplete(turn.stop_reason, turn.usage)
        self._last = turn

    def finalize(self) -> AssistantTurn:
        return self._last
```

A helper keeps the tests readable:

```python
def scripted(*specs) -> list[AssistantTurn]:
    """scripted(("Read", {"path": "a.py"}), "All done.") → two turns."""
```

Now the loop is testable the way any state machine is:

```python
def test_loop_terminates_on_end_turn(tmp_path):
    agent = Agent(provider=FakeProvider(scripted("Nothing to do.")), workspace=tmp_path)
    result = agent.run_turn("hello")
    assert result.stop_reason == "end_turn"
    assert result.iterations == 1


def test_loop_executes_tool_and_continues(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    fake = FakeProvider(scripted(("Read", {"path": "a.py"}), "The file sets x to 1."))
    agent = Agent(provider=fake, workspace=tmp_path)

    agent.run_turn("what's in a.py?")

    assert len(fake.requests) == 2                      # asked twice: before and after
    results = fake.requests[1]["messages"][-1]["content"]
    assert results[0]["type"] == "tool_result"
    assert "x = 1" in results[0]["content"]
```

The second test asserts on `fake.requests` — what the loop *sent* — which is where
protocol bugs surface. A loop that executes the tool but sends the result in the wrong
shape passes a naive test and fails this one.

## Invariant tests

The five laws from [`02-AGENT-LOOP.md`](02-AGENT-LOOP.md#the-five-laws) each get a
dedicated test. These are the highest-value tests in the suite because each one guards a
failure mode that is silent or catastrophic.

```python
def test_law1_every_tool_use_gets_a_result(tmp_path):
    """Even for unknown tools, denied tools, and crashing tools."""
    fake = FakeProvider(scripted(
        [("Read", {"path": "a.py"}),          # succeeds
         ("NoSuchTool", {}),                   # unknown
         ("Bash", {"command": "rm -rf /", "description": "x"}),   # denied
         ("Read", {"path": "/etc/passwd"})],   # outside the jail
        "done",
    ))
    agent = Agent(provider=fake, workspace=tmp_path)
    agent.run_turn("go")

    sent = fake.requests[1]["messages"][-1]["content"]
    assert len(sent) == 4                                  # four in, four out
    assert {b["tool_use_id"] for b in sent} == {"t0", "t1", "t2", "t3"}
    assert sum(1 for b in sent if b.get("is_error")) == 3


def test_law2_results_batched_into_one_message(tmp_path):
    fake = FakeProvider(scripted(
        [("Glob", {"pattern": "*"}), ("Ls", {"path": "."})], "done"))
    Agent(provider=fake, workspace=tmp_path).run_turn("go")

    msgs = fake.requests[1]["messages"]
    assert msgs[-1]["role"] == "user"
    assert len(msgs[-1]["content"]) == 2       # both results, ONE message
    assert msgs[-2]["role"] == "assistant"     # nothing interleaved


def test_law3_native_content_replayed_verbatim(tmp_path):
    sentinel = [{"type": "thinking", "thinking": "…", "signature": "SIG-ABC"},
                {"type": "tool_use", "id": "t0", "name": "Ls", "input": {"path": "."}}]
    fake = FakeProvider([turn_with_native(sentinel), scripted("done")[0]])
    Agent(provider=fake, workspace=tmp_path).run_turn("go")

    replayed = fake.requests[1]["messages"][-2]["content"]
    assert replayed == sentinel                # byte-identical, signature intact


def test_law4_tool_crash_becomes_error_result_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(ReadTool, "run", lambda *a, **k: 1 / 0)
    fake = FakeProvider(scripted(("Read", {"path": "a.py"}), "done"))
    Agent(provider=fake, workspace=tmp_path).run_turn("go")     # must not raise

    r = fake.requests[1]["messages"][-1]["content"][0]
    assert r["is_error"] and "ZeroDivisionError" in r["content"]


def test_law5_interrupt_still_closes_the_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(BashTool, "run", _raise(KeyboardInterrupt))
    agent = Agent(provider=FakeProvider(scripted(
        ("Bash", {"command": "sleep 99", "description": "x"}), "done")),
        workspace=tmp_path)

    with pytest.raises(KeyboardInterrupt):
        agent.run_turn("go")

    last = agent.conversation.messages[-1]
    assert last["role"] == "user"                          # turn was closed
    assert last["content"][0]["is_error"]
    assert "Interrupted" in last["content"][0]["content"]
    agent.validate()                                        # conversation is well-formed
```

`agent.validate()` is a helper worth building: it walks the conversation and asserts every
`tool_use` has exactly one matching `tool_result` in the next message. It runs at the end
of every loop test, so Law 1 is checked everywhere rather than only in its own test.

## Tool tests

Ordinary unit tests against `tmp_path`. Every tool gets: a happy path, each error path,
and its output cap.

```python
def test_edit_rejects_unread_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with pytest.raises(ToolError, match="have not read"):
        EditTool().run({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
                       ctx=ctx(tmp_path))

def test_edit_rejects_externally_modified_file(tmp_path):
    p = tmp_path / "a.py"; p.write_text("x = 1\n")
    c = ctx(tmp_path)
    ReadTool().run({"path": "a.py"}, ctx=c)
    p.write_text("x = 99\n")                      # someone else changed it
    with pytest.raises(ToolError, match="changed since you read it"):
        EditTool().run({"path": "a.py", "old_string": "x = 99", "new_string": "x = 2"}, ctx=c)

def test_edit_rejects_ambiguous_match(tmp_path):
    ...   # old_string appearing twice without replace_all
def test_edit_is_atomic_on_failure(tmp_path):
    ...   # simulate a crash mid-write; original content intact, no .axon-tmp left
```

Permission rules are table-driven, which is the only sane way to cover a matcher:

```python
@pytest.mark.parametrize("mode,tool,args,expected", [
    ("default", "Read",  {"path": "a.py"},                        "allow"),
    ("default", "Edit",  {"path": "a.py"},                        "ask"),
    ("plan",    "Edit",  {"path": "a.py"},                        "deny"),
    ("accept",  "Edit",  {"path": "a.py"},                        "allow"),
    ("accept",  "Bash",  {"command": "pytest"},                   "ask"),
    ("bypass",  "Bash",  {"command": "rm -rf /"},                 "deny"),  # invariant beats mode
    ("default", "Read",  {"path": ".env"},                        "deny"),  # deny beats allow
    ("default", "Bash",  {"command": "git status --short"},       "allow"), # prefix rule
    ("default", "Bash",  {"command": "git status; rm -rf /"},     "ask"),   # metachars skip allow
])
def test_permission_matrix(mode, tool, args, expected):
    assert engine(mode).check(TOOLS[tool], args, mode).outcome == expected
```

The last two rows encode the honest limitation from
[`06-SECURITY.md`](06-SECURITY.md#rule-syntax): prefix matching is convenience, and
anything with shell metacharacters falls back to asking.

## Layer 2 — Cassettes

Provider parsing is tested against real captured bytes. A recording mode writes raw SSE to
`tests/cassettes/<name>.sse` during a live run; tests replay them through the accumulator
with no network.

```
tests/cassettes/
├── anthropic-text-only.sse
├── anthropic-thinking-summarized.sse
├── anthropic-single-tool.sse
├── anthropic-parallel-tools.sse          # 3 tool_use blocks in one message
├── anthropic-split-json-fragments.sse    # input_json_delta broken mid-string
├── anthropic-max-tokens-truncation.sse
├── openai-tool-calls.sse
├── openai-reasoning-content.sse
└── openai-interleaved-fragments.sse      # two tool_calls interleaved by index
```

`anthropic-split-json-fragments.sse` is the one that matters most. Tool arguments arrive
as `partial_json` fragments split at arbitrary byte boundaries — including inside a string
literal or an escape sequence. Any implementation that tries to parse incrementally, or
that treats each fragment as valid JSON, breaks on this cassette and on nothing else.

`openai-interleaved-fragments.sse` is its counterpart: two parallel tool calls whose
argument fragments arrive interleaved, distinguished only by `index`. Keying by `id`
instead of `index` passes every other test and fails this one.

## Layer 3 — The eval harness

Layers 1 and 2 verify the harness is correct. Neither says whether the agent is any
*good*. That needs real models on real tasks, which costs money and is therefore run
deliberately rather than in CI.

Twenty tasks across five categories, each a fixture repository plus an instruction plus a
checker script:

```
tests/evals/
├── bugfix/          (5)  a failing test; fix the cause, not the test
├── feature/         (4)  add an endpoint, a flag, a validator
├── refactor/        (3)  extract a function, rename across files
├── navigate/        (4)  answer a question about unfamiliar code
└── multistep/       (4)  bug → fix → test → verify, unattended
```

```yaml
# tests/evals/bugfix/csv-quoted-commas/task.yaml
prompt: >
  The CSV importer drops rows containing quoted commas. Find out why,
  fix it, and add a regression test.
checks:
  - run: pytest tests/ -q
    expect_exit: 0
  - run: git diff --stat
    expect_matches: "src/importer.py"
  - run: git diff --stat
    expect_not_matches: "tests/test_importer.py::test_existing"   # didn't weaken a test
  - assert_new_test_covers: "quoted comma"
limits:
  max_turns: 25
  max_cost_usd: 0.60
```

`expect_not_matches` on the existing tests is the anti-cheat. The most common way an agent
"fixes" a failing test is by changing the test, and a checker that only runs `pytest`
rewards that.

Results land in a scorecard, which is where the resume numbers come from:

```
$ axon eval --all --model claude-opus-5

  category      pass    turns(avg)   cost(avg)   time(avg)
  ─────────────────────────────────────────────────────────
  bugfix         5/5          8.2      $0.147        41s
  feature        4/4         11.6      $0.213        58s
  refactor       2/3          14.0      $0.286        72s
  navigate       4/4          5.4      $0.081        26s
  multistep      3/4         19.2      $0.402       104s
  ─────────────────────────────────────────────────────────
  total        18/20  (90%)  11.3      $0.211        58s
```

Running the same suite across all four models produces the comparison table, and running
it before and after a change is how a regression gets caught. Non-determinism is handled
by reporting a pass *rate* over three runs rather than pretending a single run is a
verdict.

## CI

```yaml
# .github/workflows/test.yml
- pytest tests/ -m "not eval" --cov=src/axon --cov-fail-under=80
- ruff check src/ tests/
- mypy src/axon --strict
```

Layers 1 and 2 only — no network, no key, no cost, so CI is fast and works on forks. The
eval suite runs manually before a release and its scorecard is committed to the repository
so the numbers in the README are traceable to a specific run.

## What is deliberately not tested

- **Model quality.** Whether Claude writes a good fix is not Axon's test surface. The eval
  suite measures it; the unit tests do not pretend to.
- **Terminal rendering.** ANSI output is verified by eye. Snapshot-testing escape
  sequences is high-maintenance and low-value.
- **The proxy.** `axon doctor` reports its behaviour; the test suite does not depend on it
  being up.

---

Next: [`09-RESUME.md`](09-RESUME.md) — how to present the finished thing.
