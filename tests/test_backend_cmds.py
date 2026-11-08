"""Terminal views: /review hunks, /trail, /timeline, /ctxmap, /search print the data functions' results."""
from __future__ import annotations

from pathlib import Path

from axon.commands import dispatch_command
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _run(agent, line: str, capsys) -> str:
    capsys.readouterr()
    dispatch_command(agent, line)
    return strip_ansi(capsys.readouterr().out)


def _agent(workspace: Path):
    f = workspace / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    prov = FakeProvider(scripted(("Read", {"path": str(f)}), ("Edit", {"path": str(f), "old_string": "two", "new_string": "TWO"}), "done"))
    agent = make_agent(prov, workspace)
    agent.run_turn("edit it")
    return agent, f


def test_review_hunks_lists_then_rejects(workspace: Path, capsys) -> None:
    agent, f = _agent(workspace)
    assert "TWO" in f.read_text(encoding="utf-8")
    out = _run(agent, "/review hunks", capsys)
    assert "a.txt" in out and "- two" in out and "+ TWO" in out
    hid = next(w for w in out.split() if w.count("-") == 2 and len(w.split("-")[0]) == 4)
    out = _run(agent, f"/review hunks reject {hid}", capsys)
    assert "Reverted" in out and f.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_review_hunks_refuses_after_the_file_changed(workspace: Path, capsys) -> None:
    agent, f = _agent(workspace)
    out = _run(agent, "/review hunks", capsys)
    hid = next(w for w in out.split() if w.count("-") == 2 and len(w.split("-")[0]) == 4)
    f.write_text(f.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    out = _run(agent, f"/review hunks reject {hid}", capsys)
    assert "changed since" in out and "TWO" in f.read_text(encoding="utf-8")


def test_trail_timeline_ctxmap(workspace: Path, capsys) -> None:
    agent, _ = _agent(workspace)
    out = _run(agent, "/trail", capsys)
    assert "Edit" in out and "Permission" in out and "Why" in out and "Then" in out
    out = _run(agent, "/timeline", capsys)
    assert "Turn 1" in out and "Edit" in out
    out = _run(agent, "/ctxmap", capsys)
    assert "Context map" in out and "tool_result" in out
    rid = next(l.split()[0] for l in out.splitlines() if "drop" in l.split()[-1:] and "Edit" in l)
    out = _run(agent, f"/ctxmap drop {rid}", capsys)
    # This Edit's result is smaller than the note a drop would leave, so it stays. tests/test_ctxmap.py drops a large one.
    assert "so it was kept" in out


def test_search_command(workspace: Path, capsys) -> None:
    agent, _ = _agent(workspace)
    agent.session.append_user("find the quokka please")
    out = _run(agent, "/search quokka", capsys)
    assert "quokka" in out and "turn" in out
    assert "No matches" in _run(agent, "/search wombatless", capsys)
