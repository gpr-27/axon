"""
E2E (agent A): agent-loop behaviour - parallel read-only batches, tool errors as data, unknown tools,
malformed tool JSON, max_tokens mid tool call, --max-turns, --max-budget-usd, auto-compaction,
--fallback-model, Retry-After, ultrathink boost and the stream-json event protocol.
"""
from __future__ import annotations

import json
import time

import pytest

from harness import AxonEnv
from test_e2e_a_common import ext_env, final, no_traceback, perms, results, run_tools  # noqa: F401 (fixture)

pytestmark = pytest.mark.e2e


def _ok(r) -> None:
    assert r.code == 0, str(r)
    no_traceback(r)


def _pairs_valid(messages: list[dict]) -> bool:
    """Every assistant tool_call id has a matching role=tool message (OpenAI wire)."""
    calls = [tc["id"] for m in messages if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    return all(c in answered for c in calls)


# ─── batches / errors as data ───────────────────────────────────────────────

def test_parallel_readonly_batch_runs_concurrently(axon_env: AxonEnv) -> None:
    """Three read-only calls in one assistant turn form one batch, are flagged parallel in tool_timing,
    and all three results come back to the model in the next request, in call order."""
    for n in ("a", "b", "c"):
        (axon_env.workspace / f"{n}.txt").write_text(f"CONTENT-{n}\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [[("Read", {"path": "a.txt"}), ("Grep", {"pattern": "CONTENT-b"}), ("Read", {"path": "c.txt"})]])
    _ok(r)
    batch = [e for e in ev if e["type"] == "tool_batch"]
    assert batch and batch[0]["count"] == 3
    timings = [e for e in ev if e["type"] == "tool_timing"]
    assert len(timings) == 3 and all(t["parallel"] for t in timings), timings
    nxt = axon_env.llm.completion_requests()[1]
    res = nxt.tool_results()
    assert len(res) == 3 and "CONTENT-a" in res[0] and "CONTENT-b" in res[1] and "CONTENT-c" in res[2]


def test_mutating_batch_is_sequential(axon_env: AxonEnv) -> None:
    """A batch containing a Write is not run in parallel, and the later Read sees the Write's result."""
    r, ev = run_tools(axon_env, [[("Write", {"path": "s.txt", "content": "SEQ-OK\n"}), ("Read", {"path": "s.txt"})]])
    _ok(r)
    timings = [e for e in ev if e["type"] == "tool_timing"]
    assert timings and not any(t["parallel"] for t in timings)
    assert "SEQ-OK" in results(ev, "Read")[0]["content"]


def test_tool_error_is_data_and_loop_continues(axon_env: AxonEnv) -> None:
    """A failing tool produces an error tool result fed back to the model; the agent continues and finishes."""
    axon_env.llm.enqueue(
        [{"tool": "Read", "args": {"path": "missing.txt"}}],
        {"steps": [{"text": "recovered"}],
         "expect": [lambda q: any("not found" in t.lower() for t in q.tool_results()) or "error not fed back"]},
    )
    r = axon_env.run(["-p", "--output-format", "json", "go"])
    _ok(r)
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert r.json()["result"].strip() == "recovered" and r.json()["is_error"] is False


def test_unknown_tool_is_reported_to_model(axon_env: AxonEnv) -> None:
    """Calling a tool that does not exist yields an error result listing the available tools."""
    r, ev = run_tools(axon_env, [("FrobnicateTool", {"x": 1})])
    _ok(r)
    res = results(ev)[0]
    assert res["is_error"] and "Unknown tool" in res["content"] and "Read" in res["content"]
    assert _pairs_valid(axon_env.llm.completion_requests()[1].messages)


def test_missing_required_argument_is_error(axon_env: AxonEnv) -> None:
    """A tool call missing a required argument (Write without content) fails as data; nothing is written."""
    r, ev = run_tools(axon_env, [("Write", {"path": "nocontent.txt"})])
    _ok(r)
    assert results(ev, "Write")[0]["is_error"]
    assert not (axon_env.workspace / "nocontent.txt").exists()


def test_malformed_tool_json_reported_to_model(ext_env: AxonEnv) -> None:
    """Tool arguments that are not valid JSON are not executed; the model receives a 're-issue with valid
    JSON' error and the tool_call/tool pairing stays valid."""
    ext_env.llm.enqueue([{"raw_tool": "Write", "raw_args": '{"path": "m.txt", "content": "unterminated'}],
                        [{"text": "after malformed"}])
    r = ext_env.run(["-p", "--output-format", "stream-json", "go"])
    _ok(r)
    ev = r.json_lines()
    res = results(ev, "Write")[0]
    assert res["is_error"] and "not valid JSON" in res["content"]
    assert not (ext_env.workspace / "m.txt").exists()
    last = ext_env.llm.completion_requests()[-1]
    assert _pairs_valid(last.messages)
    assert final(ev)["result"].strip() == "after malformed"


def test_max_tokens_mid_tool_call_does_not_execute_truncated_call(ext_env: AxonEnv) -> None:
    """finish_reason=length while streaming tool arguments: the truncated call is not executed (no file
    written), the run ends without a crash, and the persisted history stays valid for --continue."""
    ext_env.llm.enqueue([{"text": "Writing now."}, {"raw_tool": "Write", "raw_args": '{"path": "t.txt", "content": "abc'},
                         {"finish": "length"}])
    r = ext_env.run(["-p", "--output-format", "json", "go"])
    no_traceback(r)
    assert not (ext_env.workspace / "t.txt").exists()
    data = r.json()
    assert data["stop_reason"] in ("max_tokens", "end_turn"), data
    ext_env.llm.enqueue([{"text": "continued fine"}])
    r2 = ext_env.run(["-p", "--continue", "carry on"])
    _ok(r2)
    assert _pairs_valid(ext_env.llm.completion_requests()[-1].messages)


def test_max_tokens_plain_text_reports_stop_reason(ext_env: AxonEnv) -> None:
    """A text answer cut by finish_reason=length is surfaced with stop_reason=max_tokens in JSON output."""
    ext_env.llm.enqueue([{"text": "partial answer"}, {"finish": "length"}])
    r = ext_env.run(["-p", "--output-format", "json", "go"])
    no_traceback(r)
    assert r.json()["stop_reason"] == "max_tokens"
    assert "partial answer" in r.json()["result"]


# ─── limits ────────────────────────────────────────────────────────────────

def test_max_turns_one_stops_after_first_call(axon_env: AxonEnv) -> None:
    """--max-turns 1: one LLM call, the tool still runs, then the run stops with max_iterations (exit 1)."""
    axon_env.llm.enqueue(*[[{"tool": "Write", "args": {"path": f"t{i}.txt", "content": "x"}}] for i in range(3)])
    r = axon_env.run(["-p", "--max-turns", "1", "--output-format", "json", "go"])
    assert r.code == 1, str(r)
    d = r.json()
    assert d["stop_reason"] == "max_iterations" and d["iterations"] == 1 and d["is_error"]
    assert len(axon_env.llm.completion_requests()) == 1
    assert (axon_env.workspace / "t0.txt").exists() and not (axon_env.workspace / "t1.txt").exists()


def test_max_turns_from_settings_file(axon_env: AxonEnv) -> None:
    """maxTurns in .axon/settings.json caps iterations like --max-turns."""
    (axon_env.workspace / ".axon").mkdir()
    (axon_env.workspace / ".axon" / "settings.json").write_text(json.dumps({"maxTurns": 2}), encoding="utf-8")
    axon_env.llm.enqueue(*[[{"tool": "Bash", "args": {"command": "echo x", "description": "x"}}]] * 4)
    r = axon_env.run(["-p", "--output-format", "json", "go"])
    assert r.json()["stop_reason"] == "max_iterations" and r.json()["iterations"] == 2, str(r)


def test_budget_ceiling_stops_before_overspending(axon_env: AxonEnv) -> None:
    """--max-budget-usd: the guard projects the next request's cost and stops before sending it."""
    axon_env.llm.enqueue(*[[{"tool": "Bash", "args": {"command": "echo b", "description": "x"}}]] * 5)
    r = axon_env.run(["-p", "--output-format", "json", "--max-budget-usd", "0.05", "go"])
    assert r.code == 1, str(r)
    d = r.json()
    assert d["stop_reason"] == "budget_exceeded" and d["is_error"]
    assert "budget" in d["result"].lower()
    assert len(axon_env.llm.completion_requests()) < 5
    assert d["cost_usd"] <= 0.05


def test_budget_too_small_for_first_request(axon_env: AxonEnv) -> None:
    """A budget below the cost of the very first request stops without calling the provider at all."""
    r = axon_env.run(["-p", "--output-format", "json", "--max-budget-usd", "0.0001", "go"])
    assert r.code == 1, str(r)
    assert r.json()["stop_reason"] == "budget_exceeded"
    assert axon_env.llm.completion_requests() == []


def test_turn_token_budget_from_env(axon_env: AxonEnv) -> None:
    """AXON_TURN_TOKEN_BUDGET stops a tool loop once the turn's token usage reaches it."""
    axon_env.llm.enqueue(*[[{"tool": "Bash", "args": {"command": "echo t", "description": "x"}}]] * 5)
    r = axon_env.run(["-p", "--output-format", "json", "go"], env={"AXON_TURN_TOKEN_BUDGET": "9000"})
    d = r.json()
    assert d["stop_reason"] == "token_budget", d
    assert len(axon_env.llm.completion_requests()) == 2


# ─── compaction ─────────────────────────────────────────────────────────────

def test_auto_compaction_summarizes_older_turns(axon_env: AxonEnv) -> None:
    """With a tiny compactAt threshold, a long session is auto-compacted: a summary request (no tools) is
    made, and the next main request carries the summary instead of the old huge turn."""
    big = "HUGE-OLD-TURN " + ("lorem ipsum dolor sit amet " * 900)
    axon_env.llm.enqueue([{"text": "ack1"}])
    assert axon_env.run(["-p", big]).code == 0
    for i in (2, 3):
        axon_env.llm.enqueue([{"text": f"ack{i}"}])
        assert axon_env.run(["-p", "--continue", f"small turn {i}"]).code == 0
    axon_env.llm.enqueue([{"text": "SUMMARY-XYZ: user sent a big blob, then two small turns."}], [{"text": "post-compact answer"}])
    r = axon_env.run(["-p", "--continue", "--output-format", "stream-json", "--settings", json.dumps({"compactAt": 0.12}),
                      "final turn"])
    _ok(r)
    ev = r.json_lines()
    comp = [e for e in ev if e["type"] == "compaction"]
    assert comp and comp[0]["trigger"] == "auto" and comp[0]["messages_after"] < comp[0]["messages_before"], comp
    reqs = axon_env.llm.completion_requests()
    summary_req, main_req = reqs[-2], reqs[-1]
    assert summary_req.tool_names == [] and "HUGE-OLD-TURN" in summary_req.all_text()
    assert "SUMMARY-XYZ" in main_req.all_text() and "HUGE-OLD-TURN" not in main_req.all_text()
    assert "final turn" in main_req.last_user_text()
    assert final(ev)["result"].strip() == "post-compact answer"


def test_compaction_precompact_hook_can_block(axon_env: AxonEnv) -> None:
    """A PreCompact hook exiting 2 blocks auto-compaction (compaction event says blocked_by_hook)."""
    import sys
    hook = axon_env.root / "pc.py"
    hook.write_text("import sys\nsys.stdin.read()\nsys.exit(2)\n", encoding="utf-8")
    (axon_env.workspace / ".axon").mkdir()
    (axon_env.workspace / ".axon" / "settings.json").write_text(json.dumps({"hooks": {"PreCompact": [
        {"hooks": [{"type": "command", "command": f'"{sys.executable}" "{hook}"'}]}]}}), encoding="utf-8")
    big = "HUGE " + ("lorem ipsum " * 2500)
    axon_env.llm.enqueue([{"text": "a1"}])
    assert axon_env.run(["-p", big]).code == 0
    for i in (2, 3):
        axon_env.llm.enqueue([{"text": f"a{i}"}])
        assert axon_env.run(["-p", "--continue", f"t{i}"]).code == 0
    r = axon_env.run(["-p", "--continue", "--output-format", "stream-json", "--settings", json.dumps({"compactAt": 0.12}), "x"])
    no_traceback(r)
    comp = [e for e in r.json_lines() if e["type"] == "compaction"]
    assert comp and comp[0]["blocked_by_hook"] is True, comp
    assert "HUGE" in axon_env.llm.completion_requests()[-1].all_text()


# ─── provider failures ──────────────────────────────────────────────────────

def test_fallback_model_used_after_overload(axon_env: AxonEnv) -> None:
    """Repeated 529 overloads on the primary model switch the request to --fallback-model."""
    axon_env.llm.enqueue(*[[{"error": 529, "retry_after": 0}]] * 4, [{"text": "from fallback"}])
    r = axon_env.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=60)
    _ok(r)
    assert r.json()["result"].strip() == "from fallback"
    models = [q.body.get("model") for q in axon_env.llm.completion_requests()]
    assert models[0] == "gpt-4o" and models[-1] == "gpt-4o-mini", models


def test_fallback_model_on_500(axon_env: AxonEnv) -> None:
    """Persistent 500s on the primary also fail over to the fallback model."""
    axon_env.llm.enqueue(*[[{"error": 500, "retry_after": 0}]] * 4, [{"text": "fallback answered"}])
    r = axon_env.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=60)
    _ok(r)
    assert "fallback answered" in r.json()["result"]
    assert axon_env.llm.completion_requests()[-1].body["model"] == "gpt-4o-mini"


def test_no_fallback_on_auth_error(axon_env: AxonEnv) -> None:
    """A 401 is not a failover condition: no request is sent to the fallback model."""
    axon_env.llm.enqueue([{"error": 401}])
    r = axon_env.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=30)
    assert r.code == 1
    assert [q.body["model"] for q in axon_env.llm.completion_requests()] == ["gpt-4o"]


def test_retry_after_header_is_honoured(axon_env: AxonEnv) -> None:
    """A 429 with Retry-After: 2 delays the retry by about two seconds, then succeeds."""
    axon_env.llm.enqueue([{"error": 429, "retry_after": 2}], [{"text": "after retry-after"}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "hi"], timeout=40)
    _ok(r)
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 2
    assert reqs[1].ts - reqs[0].ts >= 1.8
    retry = [e for e in r.json_lines() if e["type"] == "retry"]
    assert retry and retry[0]["delay_s"] >= 1.8


def test_retry_on_anthropic_overloaded(axon_env: AxonEnv) -> None:
    """Anthropic-wire 529 overloaded_error is retried and the retry succeeds."""
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"error": 529, "retry_after": 0}], [{"text": "anthropic recovered"}])
    r = axon_env.run(["-p", "hi"], timeout=30)
    _ok(r)
    assert "anthropic recovered" in r.output and len(axon_env.llm.completion_requests()) == 2


def test_provider_error_mid_tool_loop_is_reported(axon_env: AxonEnv) -> None:
    """A 401 after a successful tool round ends the run with an error result (exit 1, no traceback);
    the tool's side effect from the first round remains."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "made.txt", "content": "x"}}], [{"error": 401}])
    r = axon_env.run(["-p", "--output-format", "json", "go"], timeout=30)
    assert r.code == 1
    no_traceback(r)
    assert r.json()["stop_reason"] == "error"
    assert (axon_env.workspace / "made.txt").is_file()


# ─── thinking boost ─────────────────────────────────────────────────────────

def test_ultrathink_boosts_effort_anthropic(axon_env: AxonEnv) -> None:
    """'ultrathink' in the prompt raises the request effort and enables thinking (Anthropic wire)."""
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    assert axon_env.run(["-p", "--effort", "reflex", "plain question"]).code == 0
    r = axon_env.run(["-p", "--effort", "reflex", "--output-format", "stream-json", "please ultrathink about this"])
    _ok(r)
    plain, boosted = axon_env.llm.completion_requests()
    assert plain.body["output_config"]["effort"] == "low"
    assert boosted.body["output_config"]["effort"] in ("high", "max", "xhigh")
    assert "thinking" in boosted.body
    boost = [e for e in r.json_lines() if e["type"] == "thinking_boost"]
    assert boost and boost[0]["keyword"] == "ultrathink" and boost[0]["previous_effort"] == "reflex"


def test_ultrathink_boost_openai_reasoning_model(axon_env: AxonEnv) -> None:
    """For an OpenAI reasoning model, 'ultrathink' raises reasoning_effort in the request body."""
    axon_env.env["AXON_MODEL"] = "o3-mini"
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    assert axon_env.run(["-p", "--effort", "reflex", "plain"]).code == 0
    assert axon_env.run(["-p", "--effort", "reflex", "ultrathink: design it"]).code == 0
    plain, boosted = axon_env.llm.completion_requests()
    assert plain.body.get("reasoning_effort") == "low"
    assert boosted.body.get("reasoning_effort") == "high"


def test_ultrathink_is_more_than_think_hard(axon_env: AxonEnv) -> None:
    """The keyword tiers are distinct: 'ultrathink' (quantum) must request more effort than 'think hard'
    (synapse) on the Anthropic wire."""
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    assert axon_env.run(["-p", "--effort", "reflex", "think hard about it"]).code == 0
    assert axon_env.run(["-p", "--effort", "reflex", "ultrathink about it"]).code == 0
    hard, ultra = (q.body["output_config"]["effort"] for q in axon_env.llm.completion_requests())
    order = ["low", "medium", "high", "xhigh", "max"]
    assert order.index(ultra) > order.index(hard), (hard, ultra)


def test_no_thinking_flag_removes_thinking(axon_env: AxonEnv) -> None:
    """--no-thinking omits the thinking parameter on the Anthropic wire."""
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"text": "a"}])
    assert axon_env.run(["-p", "--no-thinking", "hello"]).code == 0
    assert "thinking" not in axon_env.llm.completion_requests()[0].body


# ─── stream-json protocol ───────────────────────────────────────────────────

def test_stream_json_event_sequence_for_tool_round(axon_env: AxonEnv) -> None:
    """stream-json emits, in order: llm_call_start -> llm_call_end -> tool_batch -> tool_use -> permission ->
    tool_result -> tool_timing -> (next call) -> text_delta -> result; every line is valid JSON."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "echo hi", "description": "x"})], final="all done")
    _ok(r)
    assert all(ln.strip().startswith("{") for ln in r.stdout.splitlines() if ln.strip())
    types = [e["type"] for e in ev]
    seq = ["llm_call_start", "llm_call_end", "tool_batch", "tool_use", "permission", "tool_result", "tool_timing",
           "llm_call_start", "text_delta", "result"]
    pos = -1
    for t in seq:
        nxt = types.index(t, pos + 1)
        assert nxt > pos, (t, types)
        pos = nxt
    assert types[-1] == "result"


def test_stream_json_permission_event_fields(axon_env: AxonEnv) -> None:
    """Permission events carry tool, outcome, reason and source, linked to the tool_use id."""
    r, ev = run_tools(axon_env, [("Write", {"path": "p.txt", "content": "x"})], "--mode", "plan")
    no_traceback(r)
    p = perms(ev, "Write")[0]
    use = [e for e in ev if e["type"] == "tool_use"][0]
    assert p["id"] == use["id"] and p["outcome"] == "deny" and p["source"] and "Plan" in p["reason"]


def test_stream_json_tool_timing_fields(axon_env: AxonEnv) -> None:
    """tool_timing reports duration_s, started/ended timestamps and output size for each tool call."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "sleep 0.3; echo timed", "description": "x"})])
    _ok(r)
    t = [e for e in ev if e["type"] == "tool_timing"][0]
    assert t["name"] == "Bash" and t["duration_s"] >= 0.25 and t["ended_at"] >= t["started_at"]
    assert t["bytes"] > 0 and t["is_error"] is False


def test_stream_json_llm_call_end_has_usage_and_cost(axon_env: AxonEnv) -> None:
    """llm_call_end carries usage token counts and a positive cost; result.cost_usd equals their sum."""
    axon_env.llm.enqueue([{"text": "x"}, {"usage": {"input": 1000, "output": 50}}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "hi"])
    _ok(r)
    ev = r.json_lines()
    ends = [e for e in ev if e["type"] == "llm_call_end"]
    assert ends[0]["usage"]["input_tokens"] == 1000 and ends[0]["usage"]["output_tokens"] == 50
    assert ends[0]["cost"] > 0
    assert abs(final(ev)["cost_usd"] - sum(e["cost"] for e in ends)) < 1e-6


def test_stream_json_text_deltas_reassemble_answer(axon_env: AxonEnv) -> None:
    """Concatenated text_delta events equal the final result text."""
    answer = "The quick brown fox jumps over the lazy dog, streamed in chunks."
    axon_env.llm.enqueue([{"text": answer}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "hi"])
    _ok(r)
    ev = r.json_lines()
    assert "".join(e["text"] for e in ev if e["type"] == "text_delta") == answer
    assert final(ev)["result"] == answer


def test_stream_json_subagent_events(axon_env: AxonEnv) -> None:
    """A Task call emits subagent lifecycle events in the stream."""
    axon_env.llm.enqueue([{"tool": "Task", "args": {"prompt": "do", "subagent_type": "explore", "description": "look"}}],
                         [{"text": "sub done"}], [{"text": "main done"}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "go"])
    _ok(r)
    kinds = {e["type"] for e in r.json_lines()}
    assert "subagent" in kinds, kinds


def test_json_output_reports_tool_calls_and_iterations(axon_env: AxonEnv) -> None:
    """--output-format json: tool_calls counts every executed call across rounds; iterations counts LLM calls."""
    r, ev = run_tools(axon_env, [[("Glob", {"pattern": "*"}), ("Ls", {})], ("Bash", {"command": "true", "description": "x"})])
    _ok(r)
    d = final(ev)
    assert d["tool_calls"] == 3 and d["iterations"] == 3
