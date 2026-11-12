"""The live block of parallel calls and the shared-axis bars of the agent board (pure row builders)."""
from types import SimpleNamespace as N

from axon.ui import parallel_board as pb
from axon.ui import subagent_monitor as sm
from axon.ui.theme import display_width, strip_ansi


def _rows():
    return [pb.CallRow("1", "Read", "src/a.py", "done", 10.0, 10.3, 4200),
            pb.CallRow("2", "Grep", "def f 世界", "running", 10.0),
            pb.CallRow("3", "Bash", "pytest -q", "failed", 10.0, 11.0, 300),
            pb.CallRow("4", "Glob", "**/*.py")]


def test_header_counts_each_state_once():
    assert strip_ansi(pb.header_text(_rows())) == "4 calls running in parallel · 1 done · 1 failed · 1 queued"
    done = [pb.CallRow("1", "Read", state="done"), pb.CallRow("2", "Read", state="done")]
    assert pb.header_text(done) == "2 calls finished · 2 done"


def test_rows_have_state_name_time_and_size_and_fit_the_width():
    for width in (40, 60, 100):
        out = pb.call_rows(_rows(), width, 13.0, hint="esc to interrupt")
        assert len(out) == 5 and all(display_width(r) <= width for r in out)
    text = [strip_ansi(r) for r in pb.call_rows(_rows(), 100, 13.0)]
    assert "0.3s" in text[1] and "4.1 KB" in text[1] and text[1].lstrip().startswith(("✓", "+"))
    assert text[2].rstrip().endswith("3s")                      # running: whole seconds, no size yet
    assert "1.0s" in text[3] and "300 B" in text[3]
    assert "Glob" in text[4] and "s" not in text[4].split("Glob")[1].replace("**/*.py", "")


def test_too_narrow_or_nothing_to_show_draws_nothing():
    assert pb.call_rows(_rows(), 30, 0.0) == [] and pb.call_rows([], 100, 0.0) == []


def test_overflow_keeps_running_calls_and_counts_the_rest():
    many = [pb.CallRow(str(i), "Read", f"f{i}", "done", 1, 2, 10) for i in range(30)]
    many[25].state, many[25].t1 = "running", 0.0
    out = [strip_ansi(r) for r in pb.call_rows(many, 80, 5.0, max_rows=8)]
    assert len(out) == 8 and any("f25" in r for r in out) and "more calls" in out[-1]


def test_board_tracks_a_batch_and_goes_quiet_when_all_are_done():
    clock = [100.0]
    b = pb.ParallelBoard(clock=lambda: clock[0])
    b.begin([("a", "Read"), ("b", "Grep")])
    assert [r.state for r in b.snapshot()] == ["queued", "queued"]
    b.start("a", "Read", "x.py")
    clock[0] = 102.0
    b.finish("a", size=10)
    assert b.snapshot()[0].state == "done" and b.snapshot()[0].elapsed(clock[0]) == 2.0
    assert b.frame_rows(80, 30) is not None
    b.finish("b", is_error=True)
    assert b.snapshot() == [] and b.frame_rows(80, 30) is None
    b.begin([("c", "Read")])                       # one call is not a parallel batch
    assert b.frame_rows(80, 30) is None


def test_span_bar_is_a_shared_axis():
    a, b, c = pb.span_bar(0, 5, 0, 10, 10, "#", ".")
    assert (a, b, c) == ("", "#####", ".....")
    a, b, c = pb.span_bar(5, 10, 0, 10, 10, "#", ".")
    assert (a, b, c) == (".....", "#####", "")
    a, b, c = pb.span_bar(3, 3.01, 0, 10, 10, "#", ".")      # a short span is still one cell
    assert len(a + b + c) == 10 and len(b) == 1


def _task(i, status, start, elapsed, **kw):
    return N(id=f"s{i}", index=i, label=f"#{i}", title=f"Investigate area {i}", name="", agent_type="explore", status=status,
             start_time=start, elapsed_s=elapsed, steps=2, max_steps=25, tool_calls=i, tokens_consumed=1000 * i, cost=0.01 * i,
             activity=f"Read src/f{i}.py", result_text="ok", error_msg="boom", **kw)


def test_agent_table_draws_overlapping_bars_cost_and_axis():
    now = 100.0
    tasks = [_task(1, "completed", 60.0, 20.0), _task(2, "running", 70.0, 0.0), _task(3, "queued", 0.0, 0.0)]
    rows = [strip_ansi(r) for r in sm.agent_table(tasks, 120, 1, now, bars=True)]
    assert "cost" in rows[0] and "0s" in rows[0] and "40s" in rows[0]
    assert all(display_width(r) <= 120 for r in rows)
    bar = lambda row: "".join(ch for ch in row if ch in "━=")
    assert bar(rows[1]) and bar(rows[2]) and len(bar(rows[2])) > len(bar(rows[1]))      # the running one is still going
    assert "$0.020" in rows[2]


def test_agent_table_without_bars_and_narrow_keep_their_layout():
    tasks = [_task(1, "completed", 60.0, 20.0)]
    plain = [strip_ansi(r) for r in sm.agent_table(tasks, 120, None, 100.0)]
    assert "cost" not in plain[0] and not any("━" in r for r in plain)
    narrow = [strip_ansi(r) for r in sm.agent_table(tasks, 60, None, 100.0, bars=True)]
    assert all(display_width(r) <= 60 for r in narrow) and not any("━" in r for r in narrow)


def test_list_footer_mentions_agent_command_once():
    foot = " ".join(strip_ansi(r) for r in sm._list_footer([_task(1, "running", 1.0, 0.0)], 0, 120, False))
    assert foot.count("/agent") == 1
