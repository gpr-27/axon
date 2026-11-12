"""Spend-cap pause and resume, task budgets, and the signed webhook / notification sinks (fakes only)."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.agent import budget_pause as bp
from axon.features import budget_cmd, notify
from axon.providers import task_budget as tb
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _pairs_ok(messages: list[dict[str, Any]]) -> bool:
    """Every tool_use is answered by exactly one tool_result in the next message, and nothing is answered twice."""
    uses: list[str] = []
    results: list[str] = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        ids = [b.get("id") for b in c if isinstance(b, dict) and b.get("type") == "tool_use"]
        uses += ids
        if ids:
            nxt = messages[i + 1]["content"] if i + 1 < len(messages) else []
            got = [b.get("tool_use_id") for b in nxt if isinstance(b, dict) and b.get("type") == "tool_result"]
            if sorted(got) != sorted(ids):
                return False
        results += [b.get("tool_use_id") for b in c if isinstance(b, dict) and b.get("type") == "tool_result"]
    return sorted(uses) == sorted(results) and len(set(results)) == len(results)


def test_session_cap_pauses_after_tool_result_and_resumes_without_rerunning(workspace: Path) -> None:
    (workspace / "a.txt").write_text("hello")
    prov = FakeProvider(scripted(("Read", {"path": "a.txt"}), "all done"))
    events: list[Any] = []
    agent = make_agent(prov, workspace, events=events, model="claude-opus-5", session_cost_ceiling=Decimal("0.000001"))
    res = agent.run_turn("read it")
    assert res.stop_reason == "budget_exceeded"
    p = bp.pending(agent)
    assert p and p["scope"] == "session" and p["resume"] is True
    assert "/budget raise 5" in p["message"] and "/budget off" in p["message"]
    assert len(prov.requests) == 1                                   # nothing was sent after the cap
    assert _pairs_ok(agent.conversation.messages)                    # the tool_use and its result are both kept
    out = bp.raise_cap(agent, 5)
    assert out["raised"] == ["session"] and bp.pending(agent) is None
    res2 = agent.run_turn(out["prompt"])
    assert res2.final_text == "all done"
    assert _pairs_ok(agent.conversation.messages)
    results = [b for m in agent.conversation.messages if isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(results) == 1                                         # the Read ran once


def test_cap_hit_before_any_answer_takes_the_prompt_back_and_resumes_it(workspace: Path) -> None:
    prov = FakeProvider(scripted("hi there"))
    agent = make_agent(prov, workspace, model="claude-opus-5", budget_session_usd=0.000001)
    res = agent.run_turn("hello")
    assert res.stop_reason == "budget_exceeded" and agent.conversation.messages == []
    p = bp.pending(agent)
    assert p and p["resume"] is False and bp.resume_prompt(agent) == "hello"
    out = bp.raise_cap(agent, 5)
    assert out["prompt"] == "hello"
    assert agent.run_turn(out["prompt"]).final_text == "hi there"


def test_budget_command_words(workspace: Path, monkeypatch) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    said: list[str] = []
    monkeypatch.setattr(budget_cmd, "_say", lambda kind, text: said.append(text))
    assert budget_cmd.handle(agent, "task 50000") and agent.settings.task_budget_tokens == 50000
    assert budget_cmd.handle(agent, "task 100") and agent.settings.task_budget_tokens == 50000   # below the minimum
    assert budget_cmd.handle(agent, "task off") and agent.settings.task_budget_tokens is None
    assert budget_cmd.handle(agent, "turn 0.5") and agent.settings.budget_turn_usd == 0.5
    assert budget_cmd.handle(agent, "turn off") and not agent.settings.budget_turn_usd
    assert not budget_cmd.handle(agent, "session 3")                 # the older handler owns it


def test_subagent_spend_counts_toward_the_session_cap() -> None:
    parent = SimpleNamespace(ledger=SimpleNamespace(total=lambda: 1.0), parent_agent=None)
    child = SimpleNamespace(ledger=SimpleNamespace(total=lambda: 0.5), parent_agent=parent)
    assert bp.session_spent(child) == pytest.approx(1.5)


# ─── task budget ─────────────────────────────────────────────────────────────

def test_task_budget_only_for_supported_models_on_the_official_api() -> None:
    k: dict[str, Any] = {"extra_body": {"output_config": {"effort": "high"}}}
    assert tb.apply(k, "claude-opus-5-5", 100_000, official=True)
    assert k["extra_body"]["output_config"] == {"effort": "high", "task_budget": {"type": "tokens", "total": 100_000}}
    assert k["extra_headers"]["anthropic-beta"] == tb.BETA_HEADER
    for model, official in (("claude-sonnet-4-6", True), ("gpt-5", True), ("claude-opus-5-5", False)):
        k2: dict[str, Any] = {}
        assert not tb.apply(k2, model, 100_000, official=official) and k2 == {}
    assert not tb.apply({}, "claude-opus-5-5", None, official=True)
    assert tb.clamp_total(100) == tb.MIN_TOTAL
    assert tb.strip(k) and "task_budget" not in k["extra_body"]["output_config"] and "extra_headers" not in k


def test_task_tracker_counts_only_new_input_plus_output() -> None:
    t = tb.Tracker()
    t.add(SimpleNamespace(input=1000, cache_read=0, cache_write=0, output=200))
    t.add(SimpleNamespace(input=1500, cache_read=0, cache_write=0, output=300))
    assert t.used == 500 + 300


def test_subagent_frontmatter_and_task_tool_take_a_budget() -> None:
    from axon.agent.subagent import _budget_tokens
    assert _budget_tokens("50000") == 50000 and _budget_tokens("junk") is None


# ─── webhooks ────────────────────────────────────────────────────────────────

SECRET = notify.new_secret()


def test_signature_verifies_and_rejects_tampering_and_replays() -> None:
    ev = notify.make_event("idle", "done")
    body = notify.body_bytes(ev)
    h = notify.headers_for(ev, body, SECRET)
    assert notify.verify(SECRET, h, body)["id"] == ev["id"]
    with pytest.raises(ValueError):
        notify.verify(SECRET, h, body + b" ")
    with pytest.raises(ValueError):
        notify.verify("whsec_" + "AAAA" * 11, h, body)
    old = notify.headers_for(ev, body, SECRET, now=time.time() - 3600)
    with pytest.raises(ValueError):
        notify.verify(SECRET, old, body)


def test_retries_keep_the_event_id_and_succeed(monkeypatch) -> None:
    seen: list[dict[str, str]] = []
    codes = iter([503, 200])
    monkeypatch.setattr(notify, "_send", lambda url, headers, body, timeout: (seen.append(headers), next(codes))[1])
    monkeypatch.setattr(notify, "_sleep", lambda s: None)
    monkeypatch.setattr(notify, "_resolve", lambda host: ["93.184.216.34"])
    ev = notify.make_event("requires_action", "waiting")
    r = notify.deliver(ev, {"url": "https://example.com/h", "secret": SECRET})
    assert r["ok"] and r["attempts"] == 2
    assert {h["webhook-id"] for h in seen} == {ev["id"]}
    assert all(notify.verify(SECRET, h, notify.body_bytes(ev)) for h in seen)


def test_failing_webhook_never_breaks_or_delays_the_turn(workspace: Path, monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> int:
        time.sleep(0.5)
        raise OSError("down")
    monkeypatch.setattr(notify, "_send", boom)
    monkeypatch.setattr(notify, "_sleep", lambda s: None)
    monkeypatch.setattr(notify, "_resolve", lambda host: ["93.184.216.34"])
    agent = make_agent(FakeProvider(scripted("ok")), workspace,
                       notify_sinks={"webhooks": [{"url": "https://example.com/h", "secret": SECRET}]})
    t0 = time.time()
    res = agent.run_turn("hi")
    assert res.final_text == "ok" and time.time() - t0 < 0.4


def test_private_and_non_http_targets() -> None:
    assert not notify.check_target("ftp://example.com/x")[0]
    assert not notify.check_target("https://user:pw@example.com/x")[0]
    ok, notes = notify.check_target("http://localhost:8080/hook", resolve=lambda h: ["127.0.0.1"])
    assert not ok and "private or local" in notes[0]
    ok, notes = notify.check_target("http://localhost:8080/hook", True, resolve=lambda h: ["127.0.0.1"])
    assert ok and any("private or local" in n for n in notes)
    assert "token" not in notify.shown_url("https://example.com/hook?token=abc")


def test_summary_is_redacted_and_short() -> None:
    s = notify.clean_summary("key sk-ant-api03-" + "a" * 40 + "\x1b[31m " + "x" * 500)
    assert "sk-ant-api03" not in s and "\x1b" not in s and len(s) <= notify.MAX_SUMMARY


def test_events_fire_for_approval_wait_finish_and_budget(workspace: Path) -> None:
    got: list[dict[str, Any]] = []
    notify.add_listener(got.append)
    try:
        agent = make_agent(FakeProvider(scripted("fine")), workspace)
        notify.from_agent_notification(agent, "permission_prompt", "Axon needs your permission to use Bash")
        agent.run_turn("hi")
        kinds = [e["type"] for e in got]
        assert "requires_action" in kinds and "idle" in kinds
        capped = make_agent(FakeProvider(scripted("x")), workspace, model="claude-opus-5", budget_session_usd=0.000001)
        capped.run_turn("hello")
        assert "budget_reached" in [e["type"] for e in got]
        assert len({e["id"] for e in got}) == len(got)               # unique ids
    finally:
        notify.remove_listener(got.append)


def test_notify_test_reports_each_sink(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(notify, "_send", lambda *a, **k: 200)
    monkeypatch.setattr(notify, "_resolve", lambda host: ["93.184.216.34"])
    agent = make_agent(FakeProvider(scripted("x")), workspace,
                       notify_sinks={"webhooks": [{"url": "https://example.com/h", "secret": SECRET}]})
    lines = [t for t, _ in notify.run_test(agent, "hello")]
    assert any(l.startswith("Webhook https://example.com/h: ok") for l in lines)
    assert any(l.startswith("Web pages") for l in lines)
