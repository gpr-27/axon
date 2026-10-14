"""Tests for axon.features.analytics (/stats, /budget, /report) and the _charts helpers."""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import storage
from axon.features import _charts as ch
from axon.features import analytics as an

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def plain(s: str) -> str:
    return ANSI.sub("", s)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "axon_home"
    monkeypatch.setenv("AXON_HOME", str(h))
    return h


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "proj_alpha"
    w.mkdir()
    return w


def _usage(ws: Path, *, days_ago: int = 0, session="s1", model="claude-sonnet-4-5", source="main",
           inp=1000, out=200, read=0, write=0, cost=0.01, dur=1.5):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    storage.append_jsonl(storage.user_file("usage.jsonl"), {
        "ts": ts.isoformat(timespec="seconds"), "project": storage.project_key(ws), "workspace": str(ws),
        "session": session, "model": model, "source": source,
        "usage": {"input": inp, "output": out, "cache_read": read, "cache_write": write, "reasoning": 0},
        "cost": cost, "duration_s": dur,
    })


def _trace(ws: Path, session: str, turn: int, prompt: str, cost: float, tools: list[tuple[str, float, bool]],
           started: float | None = None):
    started = started or (datetime.now().timestamp() - 30)
    steps = [{"index": 1, "kind": "llm", "started_at": started, "ended_at": started + 2,
              "data": {"duration_s": 2.0}}]
    for i, (name, d, err) in enumerate(tools, start=2):
        steps.append({"index": i, "kind": "tool", "started_at": started + i, "ended_at": started + i + d,
                      "data": {"name": name, "duration_s": d, "is_error": err}})
    storage.append_jsonl(storage.traces_dir(ws) / f"{session}.jsonl", {
        "turn": turn, "prompt": prompt, "model": "claude-sonnet-4-5", "mode": "normal", "started_at": started,
        "ended_at": started + 10, "llm_calls": 2, "tool_calls": len(tools), "input_tokens": 5000,
        "output_tokens": 300, "cost": cost, "steps": steps,
    })


# ─── charts ────────────────────────────────────────────────────────────────

def test_sparkline_unicode_and_ascii():
    s = ch.sparkline([0, 1, 2, 4, 8], ascii_only=False)
    assert len(s) == 5 and s[0] == "▁" and s[-1] == "█"
    a = ch.sparkline([0, 1, 2, 4, 8], ascii_only=True)
    assert a.isascii() and a[-1] == "#"
    assert ch.sparkline([]) == ""
    assert ch.sparkline([0, 0, 0], ascii_only=True) == "___"


def test_sparkline_resample_keeps_totals():
    vals = list(range(100))
    r = ch.resample(vals, 10)
    assert len(r) == 10 and sum(r) == sum(vals)
    assert len(ch.sparkline(vals, width=20)) == 20


def test_hbar_width_and_ascii(monkeypatch):
    monkeypatch.setenv("AXON_ASCII", "1")
    b = plain(ch.hbar(5, 10, 20))
    assert len(b) == 20 and b.count("#") == 10
    assert plain(ch.hbar(0.001, 10, 20)).strip() == "|"  # non-zero is always visible
    assert plain(ch.hbar(0, 10, 5)) == " " * 5


def test_hbar_unicode_eighths():
    b = plain(ch.hbar(1, 16, 2, ascii_only=False))  # 1/16 of 2 cells = one eighth of a cell
    assert ch.display_width(b) == 2 and b[0] == "▏"
    assert plain(ch.hbar(3, 4, 2, ascii_only=False)) == "█▌"


def test_stacked_bar_sums_to_width():
    bar = plain(ch.stacked_bar([(10, "INFO"), (30, "SUCCESS"), (0.1, "WARNING"), (60, "CORAL")], 40, ascii_only=True))
    assert len(bar) == 40
    for f in "#=+.":
        assert f in bar  # tiny segments still get one cell
    assert plain(ch.stacked_bar([(0, "INFO")], 10)) == " " * 10


def test_table_alignment_with_ansi_and_wide_chars():
    rows = [[ch.paint("alpha", "BOLD"), "1"], ["日本語", "1234"]]
    lines = [plain(x) for x in ch.table(["name", "n"], rows, aligns="lr", indent=0)]
    widths = {ch.display_width(x) for x in lines}
    assert len(widths) == 1  # every row has the same visible width
    assert lines[2].endswith("    1") and lines[3].endswith("1234")


def test_table_truncates_first_column():
    lines = ch.table(["x", "v"], [["a" * 100, "1"]], max_width=30, indent=0)
    assert all(ch.display_width(plain(x)) <= 30 for x in lines)


def test_bar_chart_rows():
    out = [plain(x) for x in ch.bar_chart([("a", 1, "$1"), ("bb", 4, "$4")], width=40)]
    assert len(out) == 2 and out[1].rstrip().endswith("$4")


# ─── parsing & formatting ──────────────────────────────────────────────────

def test_parse_period():
    today = date(2026, 10, 15)
    assert an.parse_period("today", today).days == 1
    p = an.parse_period("7d", today)
    assert p.days == 7 and datetime.fromtimestamp(p.since).date() == date(2026, 10, 9)
    assert an.parse_period("30", today).days == 30
    assert an.parse_period("all", today).since is None
    assert an.parse_period("bogus", today) is None
    assert an.parse_period(None, today).key == "7d"


def test_parse_stats_args(ws):
    period, key, scope = an.parse_stats_args("30d", ws)
    assert period.days == 30 and key == storage.project_key(ws) and "proj_alpha" in scope
    period, key, scope = an.parse_stats_args("--all-projects today", ws)
    assert key is None and period.key == "today"
    assert isinstance(an.parse_stats_args("nope", ws), str)


def test_formatters():
    assert an.fmt_usd(0) == "$0.00"
    assert an.fmt_usd(0.0031) == "$0.0031"
    assert an.fmt_usd(1.5) == "$1.50"
    assert an.fmt_tokens(1234) == "1.2k" and an.fmt_tokens(2_500_000) == "2.5M" and an.fmt_tokens(12) == "12"
    assert an.fmt_dur(0.25) == "250ms" and an.fmt_dur(75) == "1m 15s"
    assert an.parse_amount("$5") == Decimal("5") and an.parse_amount("x") is None and an.parse_amount("-1") is None


# ─── aggregation ───────────────────────────────────────────────────────────

def test_empty_stats_are_friendly(home, ws):
    st = an.collect(an.parse_period("7d"), storage.project_key(ws))
    assert st.empty
    text = plain("\n".join(an.render_stats(st)))
    assert "No usage yet" in text


def test_collect_totals_models_sources(home, ws):
    _usage(ws, inp=10_000, out=500, read=8_000, write=1_000, cost=0.02, session="s1")
    _usage(ws, inp=2_000, out=100, cost=0.01, session="s1", source="explorer", model="claude-haiku-4-5")
    _usage(ws, days_ago=3, inp=1_000, out=50, cost=0.005, session="s2")
    _usage(ws, days_ago=20, cost=99.0, session="old")  # outside 7d
    st = an.collect(an.parse_period("7d"), storage.project_key(ws))
    t = st.totals
    assert t.calls == 3
    assert t.input == 13_000 and t.output == 650 and t.cache_read == 8_000 and t.cache_write == 1_000
    assert t.cost == pytest.approx(0.035)
    assert t.hit_pct == pytest.approx(8_000 / 13_000 * 100)
    assert len(st.days) == 7 and sum(a.calls for _, a in st.days) == 3
    assert [m for m, _ in st.by_model][0] == "claude-sonnet-4-5"
    assert {s for s, _ in st.by_source} == {"main", "explorer"}
    assert {s.id for s in st.sessions} == {"s1", "s2"}
    assert st.avg_latency == pytest.approx(1.5)
    assert t.savings > 0  # 8k cache reads at 0.1x beat 1k cache writes at 1.25x


def test_collect_all_and_project_scope(home, ws, tmp_path):
    other = tmp_path / "proj_beta"
    other.mkdir()
    _usage(ws, cost=1.0)
    _usage(other, cost=2.0, session="b1")
    _usage(ws, days_ago=100, cost=4.0, session="ancient")
    assert an.collect(an.parse_period("all"), storage.project_key(ws)).totals.cost == pytest.approx(5.0)
    assert an.collect(an.parse_period("all")).totals.cost == pytest.approx(7.0)
    assert an.collect(an.parse_period("today")).totals.cost == pytest.approx(3.0)


def test_corrupt_usage_lines_are_skipped(home, ws):
    _usage(ws, cost=0.5)
    with open(storage.user_file("usage.jsonl"), "a", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(json.dumps({"ts": "garbage", "cost": 3}) + "\n")
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "usage": "x", "cost": "nan?"}) + "\n")
    st = an.collect(an.parse_period("all"))
    assert st.totals.calls == 2 and st.totals.cost == pytest.approx(0.5)


def test_traces_tools_and_turns(home, ws):
    _usage(ws, session="s1", cost=0.2)
    _trace(ws, "s1", 1, "fix the failing test", 0.15, [("Read", 0.1, False), ("Bash", 2.0, True), ("Read", 0.3, False)])
    _trace(ws, "s1", 2, "now refactor", 0.05, [("Edit", 0.2, False)])
    st = an.collect(an.parse_period("7d"), storage.project_key(ws))
    tools = {a.name: a for a in st.tools}
    assert tools["Read"].calls == 2 and tools["Read"].total_s == pytest.approx(0.4)
    assert tools["Bash"].errors == 1
    assert st.tools[0].name == "Read"
    assert st.turns[0].prompt == "fix the failing test" and st.turns[0].cost == pytest.approx(0.15)
    s1 = st.sessions[0]
    assert s1.turns == 2 and s1.title == "fix the failing test"
    text = plain("\n".join(an.render_stats(st, width=100)))
    for needle in ("Top tools", "Most expensive turns", "By model", "By source", "fix the failing test", "Bash"):
        assert needle in text


def test_session_title_falls_back_to_transcript(home, ws):
    _usage(ws, session="abc")
    path = storage.sessions_dir(ws) / "abc.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "timestamp": 1, "data": {}}) + "\n")
        f.write(json.dumps({"type": "user_message", "timestamp": 2, "data": {"role": "user", "content": "hello\x1b[31m world"}}) + "\n")
        f.write(json.dumps({"type": "user_message", "timestamp": 3, "data": {"role": "user", "content": "again"}}) + "\n")
    st = an.collect(an.parse_period("today"), storage.project_key(ws))
    s = st.sessions[0]
    assert s.turns == 2 and "\x1b" not in s.title and "hello" in s.title


def test_render_stats_daily_and_weekly(home, ws):
    for d in range(10):
        _usage(ws, days_ago=d, cost=0.01 * (d + 1))
    short = plain("\n".join(an.render_stats(an.collect(an.parse_period("7d")), width=100)))
    assert "Cost over time" in short and "calls" in short
    long = plain("\n".join(an.render_stats(an.collect(an.parse_period("30d")), width=100)))
    assert "wk " in long


def test_cmd_stats_prints(home, ws, capsys):
    _usage(ws, cost=0.25)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws))
    an.cmd_stats(agent, "today")
    out = plain(capsys.readouterr().out)
    assert "Axon usage" in out and "$0.250" in out
    an.cmd_stats(agent, "what")
    assert "Unknown argument" in capsys.readouterr().out
    an.cmd_stats(SimpleNamespace(), "")  # agent without settings -> all projects, no crash
    assert "Axon usage" in plain(capsys.readouterr().out)


# ─── /budget ───────────────────────────────────────────────────────────────

class _Ledger:
    def __init__(self, v):
        self.v = Decimal(v)

    def total(self):
        return self.v


def test_budget_set_show_off(settings, capsys):
    agent = SimpleNamespace(settings=settings, ledger=_Ledger("1.25"))
    an.cmd_budget(agent, "5")
    assert agent.settings.session_cost_ceiling == Decimal("5")
    out = plain(capsys.readouterr().out)
    assert "$1.25 of $5.00" in out and "$3.75 remaining" in out
    an.cmd_budget(agent, "$0.50")
    assert "limit reached" in plain(capsys.readouterr().out)
    an.cmd_budget(agent, "off")
    assert agent.settings.session_cost_ceiling == Decimal("0")
    assert "no limit set" in plain(capsys.readouterr().out)
    an.cmd_budget(agent, "abc")
    assert "Invalid amount" in capsys.readouterr().out
    assert agent.settings.session_cost_ceiling == Decimal("0")


def test_budget_without_ledger_or_settings(capsys):
    an.cmd_budget(SimpleNamespace(), "")
    assert "No active session" in capsys.readouterr().out
    agent = SimpleNamespace(settings=SimpleNamespace(session_cost_ceiling=Decimal("2")))
    an.cmd_budget(agent, "")
    assert "$0.00 of $2.00" in plain(capsys.readouterr().out)


# ─── HTML report ───────────────────────────────────────────────────────────

def test_report_default_location_and_content(home, ws, capsys):
    _usage(ws, inp=5000, read=4000, cost=0.03, model="<script>alert(1)</script>")
    _usage(ws, days_ago=2, cost=0.02, session="s2")
    _trace(ws, "s1", 1, 'evil "<img src=x onerror=alert(1)>"', 0.03, [("Grep", 0.2, False)])
    rc = an.run_report(["--days", "7"], default_workspace=ws)
    assert rc == 0
    path = an.default_report_path()
    assert path.is_file() and path.parent == storage.cache_dir("reports")
    assert not list(ws.glob("*.html"))  # never written into the workspace
    doc = path.read_text(encoding="utf-8")
    assert "<script>" not in doc and "<img" not in doc
    assert html.escape("<script>alert(1)</script>") in doc
    for needle in ("Cost per day", "Tokens per day", "Cost by model", "Cache efficiency", "Tool usage",
                   "Sessions", "Most expensive turns", "prefers-color-scheme", "<svg"):
        assert needle in doc
    assert "http://" not in doc and "https://" not in doc  # no external assets
    out = capsys.readouterr().out
    assert path.resolve().as_uri() in out


def test_report_empty_and_out_path(home, tmp_path, capsys):
    out = tmp_path / "out" / "r.html"
    assert an.run_report(["--out", str(out)]) == 0
    doc = out.read_text(encoding="utf-8")
    assert "No usage yet" in doc
    d = tmp_path / "dir"
    d.mkdir()
    assert an.run_report(["--out", str(d), "--days", "0"]) == 0
    assert (d / an.default_report_path().name).is_file()


def test_report_bad_args_returns_nonzero(home, capsys):
    assert an.run_report(["--days", "x"]) != 0


def test_svg_helpers():
    assert an.svg_line_chart([]) == ""
    svg = an.svg_line_chart([("a", 1, "t1"), ("b", 3, "t2")])
    assert svg.startswith("<svg") and svg.count("<circle") == 2
    bars = an.svg_stacked_bars([("a", (1, 2, 0, 1), "x")], ["p", "q", "r", "s"])
    assert bars.count('rx="2"') == 3  # zero segment skipped
    assert an.svg_donut([("m", 0)], "$0") == ""
    assert "<circle" in an.svg_donut([("only", 2)], "$2")
    assert an.svg_donut([("a", 1), ("b", 1)], "$2").count("<path") == 2


def test_feature_registration():
    names = {c.name for c in an.COMMANDS}
    assert {"/stats", "/budget", "/report"} <= names
    assert set(an.CLI_SUBCOMMANDS) == {"report", "stats"}
    from axon.features import cli_subcommands, feature_commands
    assert "report" in cli_subcommands()
    assert "/stats" in {c.name for c in feature_commands()}


def test_cli_stats(home, ws, monkeypatch, capsys):
    monkeypatch.chdir(ws)
    _usage(ws, cost=0.4)
    assert an.cli_stats(["today"]) == 0
    assert "$0.400" in plain(capsys.readouterr().out)
    assert an.cli_stats(["zzz"]) == 2


# ─── UsageStats tool, analytics subagent, /insights ───────────────────────

def test_usage_stats_tool_metadata():
    from axon.tools.base import Tool
    tool = an.TOOLS[0]
    assert isinstance(tool, Tool) and isinstance(tool, an.UsageStatsTool)
    assert tool.name == "UsageStats" and tool.readonly and tool.default_permission == "allow"
    props = tool.schema["properties"]
    assert set(props) == {"period", "group_by", "project_only"}
    assert "UsageStats 30d by tool" in tool.render_call({"period": "30d", "group_by": "tool"})


@pytest.mark.parametrize("group_by,needle", [("model", "claude-haiku-4-5"), ("source", "[explorer]"),
                                             ("day", date.today().isoformat()), ("tool", "Bash"),
                                             ("session", "fix the failing test")])
def test_usage_stats_tool_groupings(home, ws, group_by, needle):
    _usage(ws, inp=10_000, read=6_000, cost=0.02)
    _usage(ws, source="explorer", model="claude-haiku-4-5", cost=0.01)
    _trace(ws, "s1", 1, "fix the failing test", 0.03, [("Bash", 1.0, True)])
    ctx = SimpleNamespace(workspace=ws)
    out = an.UsageStatsTool().run({"period": "7d", "group_by": group_by}, ctx)
    assert "\x1b" not in out
    assert "cost $0.030" in out and "2 LLM calls" in out and "hit" in out
    assert needle in out
    summary = an.UsageStatsTool().summarize({}, out)
    assert summary.startswith("cost $0.030")


def test_usage_stats_tool_scope_and_errors(home, ws, tmp_path):
    from axon.errors import ToolError
    other = tmp_path / "other_proj"
    other.mkdir()
    _usage(ws, cost=1.0)
    _usage(other, cost=2.0, session="o1")
    tool = an.UsageStatsTool()
    assert "cost $1.00" in tool.run({"period": "all"}, SimpleNamespace(workspace=ws))
    assert "cost $3.00" in tool.run({"period": "all", "project_only": False}, SimpleNamespace(workspace=ws))
    assert "cost $3.00" in tool.run({"period": "all", "project_only": "false"}, SimpleNamespace(workspace=ws))
    with pytest.raises(ToolError):
        tool.run({"period": "fortnight"}, SimpleNamespace(workspace=ws))
    with pytest.raises(ToolError):
        tool.run({"group_by": "planet"}, SimpleNamespace(workspace=ws))


def test_usage_stats_tool_empty(home, ws):
    out = an.UsageStatsTool().run({}, SimpleNamespace(workspace=ws))
    assert "no usage recorded yet" in out


def test_usage_stats_tool_runs_through_registry(home, ws, settings, registry):
    """The tool works inside a real ToolRegistry/ToolContext, as the agent loop would call it."""
    from axon.agent.state import FileState, TodoState
    from axon.tools.base import ToolContext
    _usage(settings.workspace, cost=0.5)
    try:
        registry.register(an.UsageStatsTool())
    except Exception:
        pytest.skip("registry has no register()")
    tool = registry.get("UsageStats")
    ctx = ToolContext(workspace=settings.workspace, file_state=FileState(), todos=TodoState(), settings=settings)
    assert "cost $0.500" in tool.run({"period": "today"}, ctx)


def test_analytics_agent_definition_file():
    d = an.analytics_agent_definition()
    assert d is not None and d.name == "analytics"
    assert d.tools == ["UsageStats", "Read", "Glob", "Grep"]
    assert d.model == "inherit" and d.readonly
    assert "UsageStats" in d.system_prompt and len(d.system_prompt) > 500
    assert (an.AGENTS_DIR / "__init__.py").is_file()  # shipped as package data


def test_insights_prompt_contains_real_snapshot(home, ws):
    _usage(ws, cost=0.42, source="explorer")
    p = an.insights_prompt(an.parse_period("7d"), storage.project_key(ws), "project x")
    assert "$0.420" in p and "UsageStats" in p and "[explorer]" in p


def test_insights_uses_subagent_task_path(home, ws, monkeypatch, capsys):
    import axon.agent.subagent as sa
    calls = {}

    def fake_run_subagent(prompt, parent, max_iterations=None, subagent_type=None, description=""):
        calls.update(prompt=prompt, type=subagent_type, parent=parent)
        return "## Summary\nYou spent **$0.42**."

    monkeypatch.setattr(sa, "run_subagent", fake_run_subagent)
    _usage(ws, cost=0.42)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws), subagents=object(),
                            run_turn=lambda p: pytest.fail("run_turn must not be used"))
    an.cmd_insights(agent, "7d")
    assert calls["type"] == "analytics" and calls["parent"] is agent and "$0.420" in calls["prompt"]
    out = plain(capsys.readouterr().out)
    assert "analytics subagent" in out and "$0.42" in out


def test_insights_falls_back_to_run_turn_when_agent_unknown(home, ws, monkeypatch):
    import axon.agent.subagent as sa
    from axon.errors import ToolError

    def unknown(*a, **k):
        raise ToolError("Unknown subagent_type 'analytics'. Available: explore")

    monkeypatch.setattr(sa, "run_subagent", unknown)
    seen = []
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws), subagents=object(), run_turn=seen.append)
    an.cmd_insights(agent, "30d --all-projects")
    assert len(seen) == 1 and "<analyst-instructions>" in seen[0] and "last 30 days" in seen[0]


def test_insights_without_subagents_runs_turn(home, ws):
    seen = []
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws), run_turn=seen.append)
    an.cmd_insights(agent, "today")
    assert len(seen) == 1 and "today" in seen[0]


def test_insights_bad_args_and_bare_agent(home, ws, capsys):
    an.cmd_insights(SimpleNamespace(settings=SimpleNamespace(workspace=ws)), "zzz")
    assert "/insights" in capsys.readouterr().out
    an.cmd_insights(SimpleNamespace(), "")  # no run_turn: prints the stats dashboard instead
    assert "Axon usage" in plain(capsys.readouterr().out)


def test_insights_registered():
    assert "/insights" in {c.name for c in an.COMMANDS}
    assert all("/insights" not in c.aliases for c in an.COMMANDS)


def test_subagent_usage_folds_into_parent_session(home, ws):
    _usage(ws, session="p1", cost=0.02)
    _usage(ws, session="p1_sub_1", source="subagent:explore", cost=0.03)
    _trace(ws, "p1", 1, "parent prompt", 0.02, [])
    _trace(ws, "p1_sub_1", 1, "sub prompt", 0.03, [])
    st = an.collect(an.parse_period("today"), storage.project_key(ws))
    assert [s.id for s in st.sessions] == ["p1"]
    s = st.sessions[0]
    assert s.calls == 2 and abs(float(s.cost) - 0.05) < 1e-9 and s.turns == 1 and "parent prompt" in s.title
    assert "subagent:explore" in dict(st.by_source)
