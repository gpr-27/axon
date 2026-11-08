"""Compare models, the cost forecast, budget notices, project budgets (day, month) and the spending summary."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon import storage  # noqa: E402
from axon.features import analytics  # noqa: E402
from axon.features import compare_models as cm  # noqa: E402
from axon.features import cost_forecast as cf  # noqa: E402
from fake_llm import script_from_rules  # noqa: E402
from test_dashboard import Client, c, env, llm, server  # noqa: E402,F401


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "proj"
    ws.mkdir()
    return ws


def log_call(ws: Path, cost: float, ts: float | None = None, source: str = "main", project: str | None = None, model: str = "gpt-4o") -> None:
    path = storage.user_file("usage.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts if ts is not None else time.time(), "project": project or storage.project_key(ws), "workspace": str(ws),
           "session": "s1", "model": model, "source": source, "usage": {"input": 10, "output": 5}, "cost": cost, "duration_s": 1.0}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ─── spending summary and periods ────────────────────────────────────────────

def test_periods_start_at_local_midnight_monday_and_the_first():
    now = datetime(2026, 11, 8, 15, 30).timestamp()        # a Sunday
    st = cf.period_starts(now)
    assert datetime.fromtimestamp(st["day"]) == datetime(2026, 11, 8)
    assert datetime.fromtimestamp(st["week"]) == datetime(2026, 11, 2)           # the Monday before
    assert datetime.fromtimestamp(st["month"]) == datetime(2026, 11, 1)


def test_spend_summary_splits_today_week_month_and_projects(home):
    now = datetime(2026, 11, 8, 15, 30).timestamp()
    day = 86400
    log_call(home, 1.0, now - 3600)                      # today
    log_call(home, 2.0, now - 3 * day)                   # this week (Thursday)
    log_call(home, 4.0, now - 7 * day)                   # this month, not this week
    log_call(home, 8.0, now - 20 * day)                  # last month
    log_call(home, 0.5, now - 60, source="compare")
    log_call(home, 16.0, now - 60, project="other-project")
    s = cf.spend_summary(home, now)
    p = s["project"]
    assert p["today"]["cost"] == pytest.approx(1.5) and p["week"]["cost"] == pytest.approx(3.5) and p["month"]["cost"] == pytest.approx(7.5)
    assert p["today"]["compare"] == pytest.approx(0.5)
    assert s["all"]["today"]["cost"] == pytest.approx(17.5)
    assert s["all"]["month"]["cost"] == pytest.approx(23.5)


def test_spend_summary_without_a_log_is_zero(home):
    s = cf.spend_summary(home)
    assert s["project"]["month"]["cost"] == 0 and s["all"]["month"]["calls"] == 0


# ─── project budgets ─────────────────────────────────────────────────────────

def test_budget_set_clear_and_validation(home):
    now = time.time()
    cf.set_project_budget(home, "day", "5", now)
    cf.set_project_budget(home, "month", 50, now)
    st = cf.budget_status(home, now)
    assert st["day"]["limit"] == 5 and st["month"]["limit"] == 50 and st["day"]["spent"] == 0
    for bad in ("abc", "-3", "nan", "inf", 1e9, True, [1]):
        with pytest.raises(ValueError):
            cf.set_project_budget(home, "day", bad, now)
    with pytest.raises(ValueError):
        cf.set_project_budget(home, "week", "5", now)
    cf.set_project_budget(home, "day", "off", now)
    assert cf.budget_status(home, now)["day"] is None and cf.budget_status(home, now)["month"]["limit"] == 50


def test_budget_uses_the_real_log_of_this_project_only(home):
    now = time.time()
    cf.set_project_budget(home, "day", 10, now)
    log_call(home, 2.5, now - 10)
    log_call(home, 40.0, now - 10, project="somewhere-else")
    log_call(home, 1.0, now - 2 * 86400)                  # not today
    b = cf.budget_status(home, now)["day"]
    assert b["spent"] == pytest.approx(2.5) and b["frac"] == pytest.approx(0.25) and b["remaining"] == pytest.approx(7.5)
    assert cf.period_limit_hit(home, now) is None
    log_call(home, 8.0, now - 5)
    hit = cf.period_limit_hit(home, now)
    assert hit["scope"] == "day" and "daily budget" in cf.limit_message(hit)


def test_corrupt_budget_file_reads_as_no_budget(home):
    storage.state_dir()
    (storage.state_dir() / f"{cf._state_name(home)}.json").write_text("{not json", encoding="utf-8")
    assert cf.budget_status(home) == {"day": None, "month": None}
    cf.set_project_budget(home, "month", 3)             # and it can be set again over the broken file
    assert cf.budget_status(home)["month"]["limit"] == 3


# ─── notices at 50, 80, 100 ──────────────────────────────────────────────────

def test_notice_step_picks_the_highest_new_step():
    assert cf.notice_step(0.2, set()) is None
    assert cf.notice_step(0.5, set()) == 50
    assert cf.notice_step(0.79, {50}) is None
    assert cf.notice_step(0.95, {50}) == 80
    assert cf.notice_step(0.95, {50, 80}) is None
    assert cf.notice_step(3.0, {50, 80}) == 100


def test_session_notices_come_once_each():
    from types import SimpleNamespace
    s = SimpleNamespace(session_cost_ceiling=10, budget_session_usd=None, workspace=Path("/nonexistent-ws"))
    marks: set[str] = set()
    got = []
    for spent in (1, 4, 5.2, 6, 8.1, 8.5, 9.9, 10.5, 12):
        got += [n["step"] for n in cf.due_notices(s, s.workspace, spent, marks)]
    assert got == [50, 80, 100]
    s2 = SimpleNamespace(session_cost_ceiling=0, budget_session_usd=4.0, workspace=Path("/nonexistent-ws"))
    assert cf.session_limit(s2) == 4.0 and cf.session_limit(SimpleNamespace(session_cost_ceiling=10, budget_session_usd=3)) == 3
    m2: set[str] = set()
    jump = cf.due_notices(s2, s2.workspace, 3.9, m2)               # one big jump says only the highest step
    assert [n["step"] for n in jump] == [80] and "session:50" in m2 and "session:80" in m2


def test_project_notices_are_remembered_across_restarts(home):
    from types import SimpleNamespace
    now = time.time()
    s = SimpleNamespace(session_cost_ceiling=0, budget_session_usd=None, workspace=home)
    cf.set_project_budget(home, "day", 10, now)
    log_call(home, 5.5, now - 5)
    first = cf.due_notices(s, home, 0.0, set(), now)
    assert [(n["scope"], n["step"]) for n in first] == [("day", 50)] and "Daily budget" in first[0]["message"]
    assert cf.due_notices(s, home, 0.0, set(), now) == []           # a new process (new marks) stays quiet
    log_call(home, 3.0, now - 4)
    assert [n["step"] for n in cf.due_notices(s, home, 0.0, set(), now)] == [80]
    # a new limit starts the notices over: 8.5 of 9 is past 80% again
    cf.set_project_budget(home, "day", 9, now)
    assert [n["step"] for n in cf.due_notices(s, home, 0.0, set(), now)] == [80]


# ─── forecast ────────────────────────────────────────────────────────────────

def test_unknown_price_shows_nothing():
    assert cf.estimate("totally-unknown-model-xyz", 50_000, 100, cf.Turns(), 0.01) is None


def test_local_model_shows_nothing():
    assert cf.estimate("llama3.2:latest", 50_000, 100, cf.Turns(), 0.01, "ollama") is None


def test_cheap_turn_is_not_shown_and_expensive_is():
    cheap = cf.estimate("gpt-4o", 500, 20, cf.Turns(), 0.25)
    assert cheap is not None and cheap["show"] is False
    big = cf.estimate("gpt-4o", 400_000, 20, cf.Turns(), 0.25)
    assert big["show"] is True and big["high"] >= 0.25 and big["low"] <= big["high"]
    assert "400k" in big["basis"] and "no earlier turns" in big["basis"] and big["text"].startswith("Estimate")
    assert cf.estimate("gpt-4o", 400_000, 20, cf.Turns(), 0)["show"] is False            # threshold 0 = off


def test_range_uses_the_chats_own_history():
    t = cf.Turns(count=3, calls=(1, 4), output=(200, 1200), growth=3000, cache_share=0.0)
    one = cf.estimate("gpt-4o", 100_000, 0, cf.Turns(), 0.0)
    est = cf.estimate("gpt-4o", 100_000, 0, t, 0.0)
    assert est["high"] > est["low"] > 0 and est["high"] > one["high"]
    assert "1 to 4 model calls" in est["basis"] and "last 3 turns" in est["basis"]
    cached = cf.estimate("gpt-4o", 100_000, 0, cf.Turns(count=3, calls=(1, 1), output=(100, 100), cache_share=0.9), 0.0)
    plain = cf.estimate("gpt-4o", 100_000, 0, cf.Turns(count=3, calls=(1, 1), output=(100, 100), cache_share=0.0), 0.0)
    assert cached["low"] < plain["low"] and cached["high"] == pytest.approx(plain["high"])


def test_recent_turns_reads_the_transcript(tmp_path):
    f = tmp_path / "session.jsonl"
    rows = []
    for calls, out in ((1, 100), (3, 300)):
        rows.append({"type": "user_message", "data": {"role": "user", "content": "x"}})
        for i in range(calls):
            rows.append({"type": "assistant_turn", "data": {"usage": {"input": 1000 + 500 * i, "output": out // calls, "cache_read": 0}}})
    rows.append({"type": "assistant_turn", "data": "garbage"})
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    t = cf.recent_turns(f)
    assert t.count == 2 and t.calls == (1, 3) and t.output == (100, 300) and t.growth == 500
    assert cf.recent_turns(tmp_path / "missing.jsonl").count == 0


# ─── the app: forecast, budgets, compare ────────────────────────────────────

def _new_chat(c):
    st = c.post("/api/chat/new")
    return json.loads(st[2])["session"]


def _wait(fn, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def _send_and_wait(c, sid, text):
    st, _, b = c.post("/api/chat/send", {"session": sid, "text": text})
    assert st == 200, b
    _wait(lambda: not json.loads(c.get(f"/api/chat/state?session={sid}")[2])["busy"])


def test_forecast_route_hides_cheap_turns_and_shows_when_threshold_is_low(c, env, llm):
    sid = _new_chat(c)
    r = c.json(f"/api/cost/forecast?session={sid}&chars=40")
    assert r["forecast"] is None and r["threshold"] == 0.25                      # a fresh chat is cheap: no warning
    rt = c.srv.runtime(sid, create=False)
    from axon.commands.common import update_settings
    update_settings(rt.agent, forecast_threshold_usd=0.0000001)
    f = c.json(f"/api/cost/forecast?session={sid}&chars=4000")["forecast"]
    assert f and f["show"] and f["model"] == "gpt-4o" and f["context_tokens"] > 1000 and "estimate" in f["text"].lower()
    update_settings(rt.agent, forecast_threshold_usd=0)
    assert c.json(f"/api/cost/forecast?session={sid}")["forecast"] is None        # 0 turns it off
    assert c.json("/api/cost/forecast?session=nope")["forecast"] is None


def test_forecast_against_the_fake_llm_reports_the_error(c, env, llm):
    """The estimate for a first turn versus what the (fake) provider reported for it."""
    sid = _new_chat(c)
    rt = c.srv.runtime(sid, create=False)
    from axon.commands.common import update_settings
    update_settings(rt.agent, forecast_threshold_usd=0.0000001)
    llm.enqueue([{"text": "A short, plain answer to the question you asked."}])
    text = "Explain what a mutex is in two sentences."
    est = c.json(f"/api/cost/forecast?session={sid}&chars={len(text)}")["forecast"]
    before = len(llm.completion_requests())
    _send_and_wait(c, sid, text)
    req = llm.completion_requests()[before]
    actual_in = max(1, len(json.dumps(req.body)) // 4)                  # what the fake provider reports as input
    ledger_in = rt.agent.ledger.total_input_tokens
    assert ledger_in == actual_in
    err = (est["context_tokens"] - actual_in) / actual_in
    print(f"forecast context {est['context_tokens']} tokens vs reported {actual_in}: {err:+.0%}")
    assert abs(err) < 0.6          # the estimate and the provider's own count agree to within the estimator's tolerance
    actual_cost = float(rt.agent.ledger.total())
    assert est["low"] * 0.5 <= actual_cost <= est["high"] * 2.0


def test_budget_notices_reach_the_chat_once_each_and_a_spent_budget_stops_turns(c, env, llm):
    sid = _new_chat(c)
    rt = c.srv.runtime(sid, create=False)
    from axon.commands.common import update_settings
    update_settings(rt.agent, session_cost_ceiling=0.00003)
    llm.enqueue([{"text": "one"}], [{"text": "two"}])
    _send_and_wait(c, sid, "first")
    evs = [e for _, e in rt.hub.read(0, 0.01)[0] if e["type"] == "budget" and e.get("action") == "notice"]
    assert [e["percent"] for e in evs] == [100] and evs[0]["scope"] == "session" and "Session budget" in evs[0]["message"]
    st, _, b = c.post("/api/chat/send", {"session": sid, "text": "second"})
    _wait(lambda: not json.loads(c.get(f"/api/chat/state?session={sid}")[2])["busy"])
    evs2 = [e for _, e in rt.hub.read(0, 0.01)[0] if e["type"] == "budget" and e.get("action") == "notice"]
    assert len(evs2) == 1                                                       # said once, not again


def test_project_day_budget_stops_the_next_turn(env, llm, server):
    rt = server.new_runtime()
    cf.set_project_budget(env.ws, "day", 1.0)
    log_call(env.ws, 1.5)
    res = rt.agent.run_turn("hello")
    assert res.stop_reason == "budget_exceeded" and "daily budget" in res.final_text and not llm.completion_requests()
    cf.set_project_budget(env.ws, "day", "off")
    assert rt.agent.run_turn("hello").stop_reason != "budget_exceeded"


def test_budget_routes(c, env):
    d = c.json("/api/cost/budget")
    assert d["day"] is None and d["month"] is None and d["project"] == env.ws.name
    st, _, b = c.post("/api/cost/budget", {"scope": "day", "amount": "2.5"})
    assert st == 200 and json.loads(b)["day"]["limit"] == 2.5
    assert c.post("/api/cost/budget", {"scope": "month", "amount": "x"})[0] == 400
    assert c.post("/api/cost/budget", {"scope": "week", "amount": "3"})[0] == 400
    assert c.post("/api/cost/budget", {"scope": "day", "amount": "off"})[0] == 200
    assert c.json("/api/cost/budget")["day"] is None
    assert c.post("/api/cost/budget", {"scope": "day", "amount": "3"}, origin="http://evil.example")[0] in (403, 400)
    assert c.post("/api/cost/budget", {"scope": "day", "amount": "3"}, cookie=False)[0] == 401


def test_workbench_usage_has_the_spending_summary(c, env):
    sid = _new_chat(c)
    log_call(env.ws, 0.25)
    u = c.json(f"/api/workbench/usage?session={sid}")
    assert u["spend"]["project"]["today"]["cost"] >= 0.25


def test_usage_card_has_spending_once(c, env, llm):
    sid = _new_chat(c)
    log_call(env.ws, 0.25)
    cf.set_project_budget(env.ws, "month", 10)
    st, _, b = c.post("/api/chat/send", {"session": sid, "text": "/usage", "cards": True})
    data = json.loads(b)
    blob = json.dumps(data)
    assert "Spending" in blob and "This week" in blob and "This month" in blob and "Monthly budget" in blob
    assert blob.count('"Today"') == 1                                          # the summary is not repeated elsewhere on the card


def test_budget_command_sets_and_shows_project_limits(env, server, capsys):
    rt = server.new_runtime()
    analytics.cmd_budget(rt.agent, "day 5")
    analytics.cmd_budget(rt.agent, "month 40")
    out = capsys.readouterr().out
    assert "Daily budget" in out and "Monthly budget" in out and "$5.00" in out and "$40.00" in out
    assert cf.budget_status(env.ws)["day"]["limit"] == 5
    analytics.cmd_budget(rt.agent, "day nonsense")
    assert "dollars" in capsys.readouterr().out
    analytics.cmd_budget(rt.agent, "day off")
    assert cf.budget_status(env.ws)["day"] is None
    analytics.cmd_budget(rt.agent, "3")                                        # the plain form is still the session budget
    assert float(rt.agent.settings.session_cost_ceiling) == 3
    analytics.cmd_budget(rt.agent, "session off")
    assert float(rt.agent.settings.session_cost_ceiling) == 0


# ─── compare ─────────────────────────────────────────────────────────────────

def _answer_by_model(extra=None):
    def fn(req):
        model = req.body.get("model", "?")
        if extra:
            got = extra(req, model)
            if got is not None:
                return got
        return [{"text": f"answer from {model}: " + "word " * 12}]
    return script_from_rules(fn)


def _poll(c, run_id, offs=None):
    return json.loads(c.get(f"/api/compare/state?id={run_id}&offs={offs or ''}")[2])


def _run_compare(c, sid, models=("gpt-4o", "gpt-4.1"), prompt="Which is better?", **kw):
    st, _, b = c.post("/api/compare/start", {"session": sid, "prompt": prompt, "models": list(models), **kw})
    assert st == 200, b
    run = json.loads(b)
    _wait(lambda: _poll(c, run["id"])["done"])
    return run["id"]


def test_models_route_lists_provider_models_with_prices(c, env, llm, monkeypatch):
    from axon.providers import pricing
    real = pricing.is_priced
    monkeypatch.setattr(pricing, "is_priced", lambda m, p=None: False if m == "fake-model" else real(m, p))   # other tests add to the shared price table
    sid = _new_chat(c)
    d = c.json(f"/api/compare/models?session={sid}")
    assert "gpt-4o" in d["models"] and d["priced"]["gpt-4o"] is True and d["priced"].get("fake-model") is False and d["max"] == 4


def test_two_models_answer_side_by_side_without_crossed_events(c, env, llm):
    llm.default_script = _answer_by_model()
    sid = _new_chat(c)
    run = _run_compare(c, sid, models=("gpt-4o", "gpt-4.1", "gpt-4o-mini"))
    s = _poll(c, run)
    texts = {col["model"]: col["delta"] for col in s["columns"]}
    for model, text in texts.items():
        assert text.startswith(f"answer from {model}:") and text.count("answer from") == 1       # nothing from a sibling
    assert all(col["status"] == "done" and col["seconds"] >= 0 and col["input"] > 0 and col["output"] > 0 for col in s["columns"])
    assert all(col["cost"] > 0 and col["priced"] for col in s["columns"])
    assert s["total_cost"] == pytest.approx(sum(col["cost"] for col in s["columns"])) and s["read_only"] and "Nothing in your project changes" in s["note"]
    # the page asks only for what it has not seen
    first = _poll(c, run, offs=",".join(str(col["length"]) for col in s["columns"]))
    assert all(col["delta"] == "" for col in first["columns"])


def test_compare_is_read_only_a_write_attempt_changes_nothing(c, env, llm):
    target = env.ws / "should-not-exist.txt"

    def tools(req, model):
        if model == "gpt-4o" and not req.tool_results():
            return [{"tool": "Write", "args": {"file_path": str(target), "content": "pwned"}}]
        if model == "gpt-4.1" and not req.tool_results():
            return [{"tool": "Bash", "args": {"command": f"touch {target}"}}]
        return None
    llm.default_script = _answer_by_model(tools)
    sid = _new_chat(c)
    run = _run_compare(c, sid)
    assert not target.exists()
    seen = {name for r in llm.completion_requests() for name in r.tool_names}
    assert seen and seen <= set(cm.READ_ONLY_TOOLS) and not ({"Write", "Edit", "Bash", "MultiEdit", "WebFetch"} & seen)
    assert all(col["status"] in ("done", "error") for col in _poll(c, run)["columns"])


def test_compare_can_read_files_and_shows_what_it_looked_at(c, env, llm):
    def tools(req, model):
        if model == "gpt-4o" and not req.tool_results():
            return [{"tool": "Read", "args": {"file_path": "README.md"}}]
        if req.tool_results():
            return [{"text": "I read the readme."}]
        return None
    llm.default_script = _answer_by_model(tools)
    sid = _new_chat(c)
    s = _poll(c, _run_compare(c, sid))
    col = next(x for x in s["columns"] if x["model"] == "gpt-4o")
    assert any(t["name"] == "Read" and "README.md" in t["summary"] for t in col["tools"]) and "I read the readme." in col["delta"]


def test_compare_costs_are_counted_in_usage_as_compare(c, env, llm):
    llm.default_script = _answer_by_model()
    sid = _new_chat(c)
    before = c.json("/api/manage/usage?period=all&scope=project")["kpis"]["calls"] if not c.json("/api/manage/usage?period=all&scope=project").get("empty") else 0
    run = _run_compare(c, sid)
    s = _poll(c, run)
    recs = analytics.load_usage(None, storage.project_key(env.ws))
    mine = [r for r in recs if r.source == "compare"]
    assert len(mine) == 2 and {r.model for r in mine} == {"gpt-4o", "gpt-4.1"}
    assert sum(r.cost for r in mine) == pytest.approx(s["total_cost"])
    assert all(r.session.startswith(storage.active_session_id() or "") or "_sub_" in r.session for r in mine)
    u = c.json("/api/manage/usage?period=all&scope=project")
    assert u["kpis"]["calls"] == before + 2
    assert any(row["name"] == "compare" for row in u["by_source"]) or any("ompar" in json.dumps(row) for row in u["by_source"])
    # and the day budget sees it
    assert cf.budget_status(env.ws) == {"day": None, "month": None}
    cf.set_project_budget(env.ws, "day", 100)
    assert cf.budget_status(env.ws)["day"]["spent"] >= s["total_cost"]


def test_unpriced_model_is_marked_and_not_counted(c, env, llm, monkeypatch):
    from axon.providers import pricing
    real = pricing.is_priced
    monkeypatch.setattr(pricing, "is_priced", lambda m, p=None: False if m == "fake-model" else real(m, p))   # the fake server counts as a local provider
    llm.default_script = _answer_by_model()
    sid = _new_chat(c)
    s = _poll(c, _run_compare(c, sid, models=("gpt-4o", "fake-model")))
    col = next(x for x in s["columns"] if x["model"] == "fake-model")
    assert col["priced"] is False and col["cost"] is None and s["unpriced"] == 1
    assert s["total_cost"] == pytest.approx(next(x for x in s["columns"] if x["model"] == "gpt-4o")["cost"])


def test_stop_one_column_leaves_the_other_running(c, env, llm):
    def slow(req, model):
        if model == "gpt-4.1":
            return [{"text": "starting "}, {"delay": 30}, {"text": "never reached"}]
        return None
    llm.default_script = _answer_by_model(slow)
    sid = _new_chat(c)
    st, _, b = c.post("/api/compare/start", {"session": sid, "prompt": "go", "models": ["gpt-4o", "gpt-4.1"]})
    run = json.loads(b)["id"]
    _wait(lambda: next(x for x in _poll(c, run)["columns"] if x["model"] == "gpt-4o")["status"] == "done")
    _wait(lambda: "starting" in next(x for x in _poll(c, run)["columns"] if x["model"] == "gpt-4.1")["delta"])
    assert json.loads(c.post("/api/compare/stop", {"id": run, "column": 1})[2])["ok"]
    s = _wait(lambda: (lambda p: p if p["done"] else None)(_poll(c, run)), 20)
    states = {x["model"]: x["status"] for x in s["columns"]}
    assert states == {"gpt-4o": "done", "gpt-4.1": "stopped"}
    assert "never reached" not in next(x for x in s["columns"] if x["model"] == "gpt-4.1")["delta"]
    assert json.loads(c.post("/api/compare/stop", {"id": run, "column": 1})[2])["ok"] is False        # already over


def test_stop_all_and_bad_requests(c, env, llm):
    sid = _new_chat(c)
    assert c.post("/api/compare/start", {"session": sid, "prompt": "", "models": ["gpt-4o", "gpt-4.1"]})[0] == 400
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["gpt-4o"]})[0] == 400
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["gpt-4o", "gpt-4o"]})[0] == 400
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["a", "b", "c", "d", "e"]})[0] == 400
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["a b", "c"]})[0] == 400
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": "gpt-4o"})[0] == 400
    assert c.post("/api/compare/start", {"session": "no-such-chat", "prompt": "x", "models": ["a", "b"]})[0] == 404
    assert c.get("/api/compare/state?id=nope")[0] == 404
    assert c.post("/api/compare/stop", {"id": "nope"})[0] == 404
    assert c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["gpt-4o", "gpt-4.1"]}, origin="http://evil.example")[0] in (400, 403)
    llm.default_script = _answer_by_model(lambda req, m: [{"text": "hi "}, {"delay": 20}])
    run = json.loads(c.post("/api/compare/start", {"session": sid, "prompt": "x", "models": ["gpt-4o", "gpt-4.1"]})[2])["id"]
    assert c.post("/api/compare/stop", {"id": run})[0] == 200
    s = _wait(lambda: (lambda p: p if p["done"] else None)(_poll(c, run)), 20)
    assert {x["status"] for x in s["columns"]} <= {"stopped", "done"}


def test_a_failing_model_shows_its_error_in_its_own_column(c, env, llm):
    def boom(req, model):
        if model == "gpt-4.1":
            return [{"error": 401, "message": "invalid x-api-key sk-test-secret-0123456789abcdef"}]
        return None
    llm.default_script = _answer_by_model(boom)
    llm.enqueue()
    sid = _new_chat(c)
    s = _poll(c, _run_compare(c, sid))
    bad = next(x for x in s["columns"] if x["model"] == "gpt-4.1")
    good = next(x for x in s["columns"] if x["model"] == "gpt-4o")
    assert good["status"] == "done" and bad["status"] == "error" and bad["error"]
    assert "sk-test-secret-0123456789abcdef" not in json.dumps(s)


def test_continue_with_this_one_forks_the_chat_with_that_answer_and_model(c, env, llm):
    llm.default_script = _answer_by_model()
    sid = _new_chat(c)
    _send_and_wait(c, sid, "earlier message")
    run = _run_compare(c, sid, prompt="Compare these please")
    s = _poll(c, run)
    st, _, b = c.post("/api/compare/continue", {"id": run, "column": 1})
    assert st == 200, b
    new = json.loads(b)
    assert new["session"] != sid and new["model"] == "gpt-4.1"
    rt = c.srv.runtime(new["session"], create=False)
    msgs = rt.agent.conversation.messages
    assert any("earlier message" in json.dumps(m) for m in msgs)
    assert msgs[-2] == {"role": "user", "content": "Compare these please"}
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == next(x for x in s["columns"] if x["idx"] == 1)["delta"]
    old = c.srv.runtime(sid, create=False)
    assert old.agent.settings.model == "gpt-4o"                          # the first chat keeps its own model
    assert c.post("/api/compare/continue", {"id": run, "column": 9})[0] == 400
    assert c.post("/api/compare/continue", {"id": "nope", "column": 0})[0] == 404


def test_compare_leaves_no_transcript_in_the_chat(c, env, llm):
    llm.default_script = _answer_by_model()
    sid = _new_chat(c)
    rt = c.srv.runtime(sid, create=False)
    folder = Path(rt.agent.session._file(sid)).parent
    before = sorted(p.name for p in folder.rglob("*"))
    _run_compare(c, sid)
    assert sorted(p.name for p in folder.rglob("*")) == before
    assert c.json(f"/api/subagents?session={sid}")["subagents"] == []


# ─── the terminal ────────────────────────────────────────────────────────────

def test_parse_command():
    known = {"gpt-4o", "gpt-4.1"}
    assert cm.parse_command("gpt-4o gpt-4.1 explain this file", known) == (["gpt-4o", "gpt-4.1"], "explain this file")
    assert cm.parse_command("a b -- why is the sky blue", known) == (["a", "b"], "why is the sky blue")
    assert cm.parse_command("gpt-4o", known) == (["gpt-4o"], "")
    assert cm.parse_command("", known) == ([], "")
    assert cm.parse_command("m1 m2 m3 m4 m5 hello", set())[0] == ["m1", "m2", "m3", "m4"]


def test_compare_command_prints_each_answer_in_turn(env, llm, server, capsys):
    llm.default_script = _answer_by_model()
    rt = server.new_runtime()
    cm.cmd_compare(rt.agent, "gpt-4o gpt-4.1 -- say something")
    out = capsys.readouterr().out
    assert "Read-only" in out and out.count("answer from gpt-4o") == 1 and out.count("answer from gpt-4.1") == 1
    assert "compare" in out.lower() and "Cost of this comparison" in out
    recs = [r for r in analytics.load_usage(None, storage.project_key(env.ws)) if r.source == "compare"]
    assert len(recs) == 2


def test_compare_command_usage_and_registration(env, server, capsys):
    rt = server.new_runtime()
    cm.cmd_compare(rt.agent, "")
    assert "Usage: /compare" in capsys.readouterr().out
    cm.cmd_compare(rt.agent, "gpt-4o gpt-4.1")
    assert "Usage: /compare" in capsys.readouterr().out
    from axon.features import feature_commands
    assert "/compare" in {c.name for c in feature_commands()}


def test_the_page_script_and_menu_are_wired(c):
    js = c.get("/static/app.js")[2].decode()
    for needle in ("function cfCompareOpen", "function cfForecastLine", "function cfSpendRows", "function cfProjectBudgetSection",
                   "Compare models…", "cfCompareOpen(C)", "cfProjectBudgetSection()"):
        assert needle in js
    assert "innerHTML" not in js.split("// ---------- cost forecast under the message box")[1].split("function cfCompareOpen")[0]
    assert ".cf-cols" in c.get("/static/app.css")[2].decode()


def test_progress_spending_matches_the_usage_overview_and_refreshes_on_turn_end(c, env, llm):
    """The Progress summary and the Usage overview sum the same log: for the same period they agree to the cent, also right
    after a turn; and the page refetches the summary when a turn starts or ends."""
    from axon.features.dashboard import overview_data
    sid = _new_chat(c)
    llm.enqueue([{"text": "ok"}])
    for _ in range(2):
        _send_and_wait(c, sid, "a turn that costs something")
        starts = cf.period_starts()
        mine = c.json(f"/api/workbench/usage?session={sid}")["spend"]["project"]
        for key, since in (("today", starts["day"]), ("week", starts["week"]), ("month", starts["month"])):
            per = analytics.Period(key, key, since, None)
            stats = analytics.collect(per, project=storage.project_key(env.ws))
            assert mine[key]["cost"] == pytest.approx(overview_data(env.ws, "30d", "project", stats=stats)["kpis"]["cost"])
        assert mine["today"]["cost"] > 0
    js = c.get("/static/app.js")[2].decode()
    assert "WB_BUSY.usage = Object.assign(WB_BUSY.usage" in js
