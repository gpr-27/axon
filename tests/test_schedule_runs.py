"""Scheduled-run upgrades: cron and timezone, spend cap gate (fail closed), run log, pause, run now, overlap, missed."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.commands.schedule_cmds import parse_add  # noqa: E402
from axon.features import schedule_ext as ext  # noqa: E402
from axon.features import scheduler as sch  # noqa: E402
from test_feature_tasks import NOON, Clock, Runner, at, make, settle  # noqa: E402,F401


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / "proj"
    f.mkdir()
    return f


def job(folder: Path, **kw: Any) -> dict[str, Any]:
    return {"prompt": "check", "folder": str(folder), "kind": "interval", "every_s": 600, **kw}


# ─── cron and timezone ───────────────────────────────────────────────────────

def test_cron_validation_and_next_fire() -> None:
    assert ext.cron_next("30 9 * * 1-5", NOON) == at(2026, 11, 10, 9, 30)          # Monday noon -> Tuesday 09:30
    assert ext.cron_next("*/15 * * * *", NOON) == NOON + 900
    assert ext.cron_next("0 0 1 jan *", NOON) == at(2027, 1, 1, 0, 0)
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "a b c d e", "*/0 * * * *", "5-1 * * * *"):
        with pytest.raises(ext.CronError):
            ext.parse_cron(bad)


def test_timezone_next_fire_and_bad_zone(folder: Path) -> None:
    ts = at(2026, 11, 9, 0, 0)
    kol = ext.cron_next("30 9 * * *", ts, "Asia/Kolkata")
    assert datetime.fromtimestamp(kol, ext.zone("UTC")).strftime("%H:%M") == "04:00"      # 09:30 IST
    s, clock, _ = make(folder.parent)
    t = s.add(job(folder, kind="cron", cron="30 9 * * *", tz="Asia/Kolkata"))
    assert t["next_at"] == ext.cron_next("30 9 * * *", clock.t, "Asia/Kolkata")
    d = s.add(job(folder, kind="daily", at="09:30", tz="America/New_York"))
    assert datetime.fromtimestamp(d["next_at"], ext.zone("America/New_York")).strftime("%H:%M") == "09:30"
    with pytest.raises(sch.ScheduleError, match="timezone"):
        s.add(job(folder, kind="daily", at="09:30", tz="Mars/Base"))
    with pytest.raises(sch.ScheduleError, match="5 fields"):
        s.add(job(folder, kind="cron", cron="* * *"))


def test_next_preview_lists_fires(folder: Path) -> None:
    t = {"kind": "cron", "cron": "0 * * * *"}
    assert ext.next_fires(t, NOON, 3) == [NOON + 3600, NOON + 7200, NOON + 10800]


# ─── the cap gate ────────────────────────────────────────────────────────────

class FakeSession:
    model, provider = "unpriced-model-xyz", None
    opts: Any = None

    def __init__(self, opts: Any) -> None:
        FakeSession.opts = opts
        self.agent = SimpleNamespace(settings=SimpleNamespace(model=FakeSession.model, provider=FakeSession.provider))
        self.closed = False
        self.ran = False

    def cancel(self) -> None: ...

    def close(self) -> None:
        self.closed = True

    def run(self, prompt: str) -> Any:
        FakeSession.ran = True
        return SimpleNamespace(stop_reason="budget_exceeded", is_error=True, error="cap", text="", cost_usd=1.01, session_id="sid",
                               usage={"input_tokens": 10, "output_tokens": 5})


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeSession]:
    import axon.features.sdk as sdk
    monkeypatch.setattr(sdk, "AxonSession", FakeSession)
    FakeSession.ran, FakeSession.model = False, "unpriced-model-xyz"
    return FakeSession


def run_real(task: dict[str, Any], folder: Path) -> dict[str, Any]:
    t = sch.clean_task({**job(folder), **task})
    return sch.sdk_runner(t, sch.RunControl("r", "t"))


def test_unknown_price_fails_closed(fake_sdk: type[FakeSession], folder: Path) -> None:
    r = run_real({}, folder)
    assert r["status"] == "skipped" and r["failure"] == "price_unknown" and "No price is known" in r["error"] and not fake_sdk.ran


def test_token_cap_or_cap_off_allows_an_unpriced_model(fake_sdk: type[FakeSession], folder: Path) -> None:
    assert run_real({"max_tokens": 50000}, folder)["status"] == "budget" and fake_sdk.ran
    assert fake_sdk.opts.settings_overrides == {"turn_token_budget": 50000}
    fake_sdk.ran = False
    run_real({"cap_off": True}, folder)
    assert fake_sdk.ran and fake_sdk.opts.max_budget_usd is None


def test_priced_model_runs_with_the_dollar_cap_and_records_budget_exceeded(fake_sdk: type[FakeSession], folder: Path) -> None:
    fake_sdk.model = "gpt-4o"
    r = run_real({"budget_usd": 1.0}, folder)
    assert fake_sdk.opts.max_budget_usd == 1.0 and r["status"] == "budget" and r["failure"] == "cap_reached" and r["tokens_in"] == 10
    assert ext.outcome("budget") == "budget_exceeded"


# ─── the run log ─────────────────────────────────────────────────────────────

def test_run_log_entries(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(job(folder))
    s.run_now(t["id"], wait=True)
    r = s.history(t["id"])[0]
    assert r["outcome"] == "succeeded" and r["trigger"] == "manual" and r["session_id"] == "s-" + t["id"] and r["cost_usd"] == 0.02
    assert r["duration_s"] == 0 and r["failure"] == ""
    bad, _, _ = make(tmp_path / "b", runner=Runner("boom"))
    t2 = bad.add(job(folder))
    bad.run_now(t2["id"], wait=True)
    r2 = bad.history(t2["id"])[0]
    assert r2["outcome"] == "failed" and r2["failure"] == "error"


def test_retention_by_age_and_count(tmp_path: Path, folder: Path) -> None:
    s, clock, _ = make(tmp_path)
    t = s.add(job(folder))
    old = clock.t - 100 * 86400
    s._append({"run": "old", "task": t["id"], "status": "ok", "started": old, "ended": old})
    s._append({"run": "new", "task": t["id"], "status": "ok", "started": clock.t, "ended": clock.t})
    s._append({"run": "live", "task": t["id"], "status": "running", "started": old})
    s.prune()
    assert [r["run"] for r in s._records(s._file("runs.jsonl"))] == ["new", "live"]
    assert len(ext.retain([{"run": str(i), "ended": clock.t} for i in range(10)], clock.t, keep=3)) == 3


# ─── pause, run now, overlap, missed ─────────────────────────────────────────

def test_pause_skips_fires_and_resume_continues(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(job(folder))
    s.set_enabled(t["id"], False)
    clock.advance(1200)
    assert s.tick() == [] and runner.calls == [] and s.get(t["id"])["state"] == "paused"
    s.set_enabled(t["id"], True)
    assert s.get(t["id"])["next_at"] == clock.t + 600
    clock.advance(600)
    assert s.tick() == [t["id"]]
    settle(s)


def test_run_now_does_not_shift_the_next_fire(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(job(folder, kind="cron", cron="0 * * * *"))
    nxt = s.get(t["id"])["next_at"]
    clock.advance(60)
    s.run_now(t["id"], wait=True)
    assert s.get(t["id"])["next_at"] == nxt and len(runner.calls) == 1


def test_overlapping_fire_is_skipped_and_logged(tmp_path: Path, folder: Path) -> None:
    runner = Runner()
    runner.gate.clear()
    s, clock, _ = make(tmp_path, runner=runner)
    t = s.add(job(folder))
    clock.advance(600)
    s.tick()
    runner.entered.wait(5)
    clock.advance(600)
    assert s.tick() == []
    h = s.history(t["id"])
    assert h[0]["outcome"] == "skipped" and h[0]["error"] == ext.SKIP_OVERLAP
    runner.gate.set()
    settle(s)


def test_missed_cron_fire_is_logged_not_replayed(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(job(folder, kind="cron", cron="0 9 * * *"))
    assert t["catch_up"] is False
    clock.t = at(2026, 11, 12, 10, 0)
    assert s.tick() == [] and runner.calls == []
    h = s.history(t["id"])
    assert h[0]["outcome"] == "missed" and h[0]["missed"] == 3


# ─── the command line ────────────────────────────────────────────────────────

def test_parse_add_cron_and_caps(folder: Path) -> None:
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=folder))
    f = parse_add(agent, f"cron 30 9 * * 1-5 --max-cost off --max-tokens 40000 --tz Asia/Kolkata --folder {folder} do it")
    assert f["kind"] == "cron" and f["cron"] == "30 9 * * 1-5" and f["cap_off"] and f["max_tokens"] == 40000 and f["tz"] == "Asia/Kolkata"
    assert f["prompt"] == "do it"
    assert parse_add(agent, f"every 1h --max-cost 0.5 --folder {folder} x")["budget_usd"] == 0.5
