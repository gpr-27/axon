"""/usage: the data behind each tab, the width of every row, the quota store and the autocompact warning."""
from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fakes import FakeProvider
from test_core_helpers import make_agent
from test_keyboard_menus import axon_home  # noqa: F401  (fixture: isolated AXON_HOME)

from axon.commands import usage_screen as us
from axon.features import quota
from axon.providers.base import Usage
from axon.ui.statusbar import AUTOCOMPACT_WARNING, autocompact_warning
from axon.ui.theme import display_width, strip_ansi


@pytest.fixture
def agent(workspace, axon_home):  # noqa: F811
    a = make_agent(FakeProvider([]), workspace)
    a.ledger.record("gpt-4o", Usage(input=1200, output=300, cache_read=400), source="main")
    a.ledger.record("claude-sonnet-4-6", Usage(input=50_000, output=900, cache_write=2000), source="subagent_1")
    quota.clear()
    return a


# ─── autocompact warning ────────────────────────────────────────────────────

def test_autocompact_warning_threshold() -> None:
    assert autocompact_warning(79_000, 200_000, 100_000) == ""            # 79% of the threshold
    assert autocompact_warning(80_000, 200_000, 100_000) == AUTOCOMPACT_WARNING   # exactly 80%
    assert autocompact_warning(150_000, 200_000, 100_000) == AUTOCOMPACT_WARNING  # past the threshold
    assert autocompact_warning(80_000, 200_000, 0.5) == AUTOCOMPACT_WARNING       # a share of the window
    assert autocompact_warning(0, 200_000, 0.5) == "" and autocompact_warning(5, 0, 0.5) == ""
    assert AUTOCOMPACT_WARNING == ("Autocompact will trigger soon, older messages will be summarized. "
                                   "/compact now to choose what's kept.")


def test_warning_is_pinned_on_usage_and_context_tabs_only(agent, monkeypatch) -> None:
    monkeypatch.setattr("axon.ui.statusbar.agent_autocompact_warning", lambda a: AUTOCOMPACT_WARNING)
    assert us.pinned_lines(agent, "Usage", 80) and us.pinned_lines(agent, "Context", 80)
    assert not us.pinned_lines(agent, "Status", 80)
    text = strip_ansi(" ".join(x.strip() for x in us.pinned_lines(agent, "Usage", 60)))
    assert "Autocompact will trigger soon" in text and "choose what's kept." in text
    assert all(display_width(x) <= 60 for x in us.pinned_lines(agent, "Usage", 60))


# ─── quota store ────────────────────────────────────────────────────────────

def test_rate_limits_are_parsed_and_stored() -> None:
    quota.clear()
    assert quota.snapshot() == []
    now = time.time()
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))
    n = quota.record_rate_limits({
        "Anthropic-RateLimit-Requests-Limit": "50", "anthropic-ratelimit-requests-remaining": "20",
        "anthropic-ratelimit-requests-reset": reset,
        "anthropic-ratelimit-input-tokens-limit": "100000", "anthropic-ratelimit-input-tokens-remaining": "90000",
        "anthropic-ratelimit-input-tokens-reset": reset, "content-type": "x"}, provider="anthropic", now=now)
    assert n == 2
    rows = {r.name: r for r in quota.snapshot()}
    assert rows["requests"].limit == 50 and rows["requests"].used_fraction == pytest.approx(0.6)
    assert rows["input-tokens"].used_fraction == pytest.approx(0.1)
    assert abs(rows["requests"].reset_at - (now + 600)) < 2
    assert quota.record_rate_limits({"x": "1"}) == 0 and quota.record_rate_limits(None) == 0
    quota.clear()


def test_openai_style_durations_and_expiry() -> None:
    quota.clear()
    quota.record_rate_limits({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "4",
                              "x-ratelimit-reset-requests": "1m30s"}, now=1000.0)
    (row,) = quota.snapshot(include_expired=True)
    assert row.reset_at == pytest.approx(1090.0) and row.expired and quota.snapshot() == []
    quota.clear()


def test_reset_text_is_local_time() -> None:
    text = quota.local_reset_text(time.time() + 3600)
    assert text.startswith("resets ") and ("am" in text or "pm" in text)


def test_usage_tab_shows_rate_limit_bar_only_with_data(agent) -> None:
    rows = strip_ansi("\n".join(us.usage_lines(agent, 100)))
    assert "no limit data from this provider" in rows
    quota.record_rate_limits({"anthropic-ratelimit-tokens-limit": "1000", "anthropic-ratelimit-tokens-remaining": "250",
                              "anthropic-ratelimit-tokens-reset": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 900))})
    rows = strip_ansi("\n".join(us.usage_lines(agent, 100)))
    assert "no limit data" not in rows and "Tokens" in rows and "75%" in rows and "resets" in rows
    quota.clear()


# ─── session block ──────────────────────────────────────────────────────────

def test_session_usage_per_model_and_totals(agent) -> None:
    su = us.session_usage(agent)
    assert su.calls == 2 and su.total_cost == Decimal(str(agent.ledger.total()))
    assert {m.model for m in su.models} == {"gpt-4o", "claude-sonnet-4-6"}
    m = next(m for m in su.models if m.model == "gpt-4o")
    assert (m.input, m.output, m.cache_read) == (1200, 300, 400)
    assert su.api_s is None and su.added is None        # nothing timed, no edits: shown as n/a, never made up


def test_session_usage_counts_edit_lines_and_api_time(agent) -> None:
    def tool(name, inp, **kw):
        return SimpleNamespace(kind="tool", started_at=1.0, ended_at=2.0, data={"name": name, "input": inp, **kw})
    steps = [SimpleNamespace(kind="llm", started_at=10.0, ended_at=14.5, data={}),
             tool("Write", {"path": "a.py", "content": "1\n2\n3\n"}),
             tool("Edit", {"path": "a.py", "old_string": "1\n2", "new_string": "1\nx\ny\nz"}),
             tool("Edit", {"path": "b.py", "old_string": "a", "new_string": "b"}, is_error=True)]
    agent.traces = [SimpleNamespace(started_at=9.0, steps=steps)]
    agent.started_at = 9.0
    su = us.session_usage(agent, now=69.0)
    assert su.api_s == pytest.approx(4.5) and su.wall_s == pytest.approx(60.0)
    assert (su.added, su.removed, su.edited_files) == (3 + 3, 1, 1)     # the failed edit is not counted
    assert us.dur(su.wall_s) == "1m 00s" and us.dur(None) == "n/a"


# ─── footprint ──────────────────────────────────────────────────────────────

def test_footprint_has_every_source_and_a_total(agent) -> None:
    rows = us.footprint(agent)
    groups = {r.group for r in rows}
    assert {"System prompt", "Tools", "Skills", "Memory"} <= groups
    assert all(r.tokens >= 0 for r in rows) and sum(r.tokens for r in rows) > 0
    text = strip_ansi("\n".join(us.usage_lines(agent, 100)))
    assert "per turn (approximate)" in text and "Footprint every turn" in text


# ─── widths ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", [40, 60, 100, 200])
def test_every_tab_row_fits_the_width(agent, width) -> None:
    agent.settings = agent.settings.model_copy(update={"model": "モデル-日本語-😀-" + "x" * 30})
    for tab in us.TABS:
        for line in us.tab_lines(agent, tab, width - 2):
            assert display_width(line) <= width - 2, (tab, width, strip_ansi(line))


def test_table_rows_align_columns_with_wide_characters() -> None:
    rows = us.table_rows(["Model", "In", "Cost"], [["中文模型名" * 4, "1", "$0.1"], ["gpt-4o", "22", "$12.00"]], 40)
    widths = {display_width(r) for r in rows}
    assert len(widths) == 1 and widths.pop() <= 40


def test_plain_text_has_every_tab_and_no_escapes(agent) -> None:
    text = us.plain_text(agent, 80)
    for tab in us.TABS:
        assert f"== {tab} ==" in text
    assert "\x1b" not in text


# ─── contributors ───────────────────────────────────────────────────────────

def test_contributors_group_the_usage_log(agent, axon_home) -> None:  # noqa: F811
    import json
    now = time.time()
    log = axon_home / "usage.jsonl"
    recs = [
        {"ts": now - 600, "project": "p1", "workspace": "/w/alpha", "session": "s1", "model": "gpt-4o", "source": "main",
         "usage": {"input": 100, "output": 10}, "cost": 0.5},
        {"ts": now - 3 * 86400, "project": "p2", "workspace": "/w/beta", "session": "s2", "model": "m2",
         "source": "subagent_1", "usage": {"input": 100, "output": 10}, "cost": 0.25},
        {"ts": now - 20 * 86400, "project": "p2", "workspace": "/w/beta", "session": "s3", "model": "m2", "source": "main",
         "usage": {"input": 1, "output": 1}, "cost": 9.0},
    ]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    c = us.contributors(now, refresh=True)
    assert c.cost24 == pytest.approx(0.5) and c.cost7 == pytest.approx(0.75)      # the 20-day-old record is outside 7d
    assert c.by_project["alpha"] == pytest.approx((0.5, 0.5)) and c.by_project["beta"] == pytest.approx((0.0, 0.25))
    assert set(c.by_model) == {"gpt-4o", "m2"}
    us._contrib_cache = None


def test_provider_response_feeds_the_limits_screen(monkeypatch):
    from axon.features import quota
    from axon.providers.transport import note_rate_limits
    seen = []
    monkeypatch.setattr(quota, "record_rate_limits", lambda h, provider="": seen.append((h, provider)))
    note_rate_limits({"anthropic-ratelimit-tokens-limit": "100"}, "anthropic")
    assert seen == [({"anthropic-ratelimit-tokens-limit": "100"}, "anthropic")]


def test_limit_bookkeeping_never_breaks_a_request(monkeypatch):
    from axon.features import quota
    from axon.providers.transport import note_rate_limits

    def boom(*a, **k):
        raise ValueError("bad header")
    monkeypatch.setattr(quota, "record_rate_limits", boom)
    note_rate_limits({"x": "y"}, "anthropic")
