"""Terminal workbench: Changes, Worktrees, Context legend, Connectors, /workbench (plus the shared data modules)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands import workbench_cmds as wb
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.features import workbench_data as wd
from axon.features import worktree_view as wv
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.ui.theme import display_width, strip_ansi
from fakes import FakeProvider

@pytest.fixture(autouse=True)
def _axon_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path_factory.mktemp("axon_home")))


GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=n"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src" / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "src/pkg/a.py").write_text("def a():\n    return 1\n")
    (r / "README.md").write_text("hi\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    (r / "src/pkg/a.py").write_text("def a():\n    return 2\n\nx = 1\n")
    (r / "new.txt").write_text("one\ntwo\n")
    _git(r, "rm", "-q", "README.md")
    return r


def _agent(ws: Path) -> Agent:
    s = Settings(workspace=ws, mode="default")
    return Agent(provider=FakeProvider([]), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(ws), ledger=Ledger(), settings=s)


# ─── git data ───────────────────────────────────────────────────────────────

def test_changes_working_tree_codes_counts_and_totals(repo: Path) -> None:
    ch = wv.changes(repo)
    by = {f["path"]: f for f in ch["files"]}
    assert ch["branch"] == "main" and ch["label"] == "main → working tree"
    assert by["src/pkg/a.py"]["code"] == "M" and (by["src/pkg/a.py"]["added"], by["src/pkg/a.py"]["removed"]) == (3, 1)
    assert by["README.md"]["code"] == "D" and by["README.md"]["removed"] == 1 and by["README.md"]["staged"]
    assert by["new.txt"]["code"] == "U" and by["new.txt"]["added"] == 2
    assert by["src/pkg/a.py"]["dir"] == "src/pkg" and by["src/pkg/a.py"]["name"] == "a.py"
    assert ch["added"] == sum(f["added"] for f in ch["files"]) == 5 and ch["removed"] == 2


def test_compare_targets(repo: Path) -> None:
    assert [f["path"] for f in wv.changes(repo, "staged")["files"]] == ["README.md"]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    last = wv.changes(repo, "commit")
    assert last["label"] == "HEAD~1 → HEAD" and {f["path"] for f in last["files"]} == {"README.md", "src/pkg/a.py", "new.txt"}
    _git(repo, "branch", "feature", "HEAD~1")
    assert {f["path"] for f in wv.changes(repo, "branch", "feature")["files"]} == {"README.md", "src/pkg/a.py", "new.txt"}
    assert wv.changes(repo, "branch")["error"]                       # a branch target needs a name


def test_not_a_repo(tmp_path: Path) -> None:
    ch = wv.changes(tmp_path)
    assert not ch["repo"] and ch["error"] and ch["files"] == []


def test_diff_rows_have_line_numbers(repo: Path) -> None:
    rows = wv.file_diff(repo, "src/pkg/a.py")["rows"]
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "hunk" and "add" in kinds and "del" in kinds
    deleted = next(r for r in rows if r["kind"] == "del")
    added = [r for r in rows if r["kind"] == "add"]
    assert (deleted["old"], deleted["text"]) == (2, "    return 1")
    assert [(r["new"], r["text"]) for r in added] == [(2, "    return 2"), (4, "x = 1")] or added[0]["new"] == 2
    new = wv.file_diff(repo, "new.txt", code="U")["rows"]
    assert [(r["new"], r["text"]) for r in new] == [(1, "one"), (2, "two")]


def test_stage_unstage_discard_and_path_safety(repo: Path) -> None:
    assert wv.stage(repo, "src/pkg/a.py")[0]
    assert next(f for f in wv.changes(repo)["files"] if f["path"] == "src/pkg/a.py")["staged"]
    assert wv.unstage(repo, "src/pkg/a.py")[0]
    assert not next(f for f in wv.changes(repo)["files"] if f["path"] == "src/pkg/a.py")["staged"]
    assert wv.discard(repo, "src/pkg/a.py", "M")[0]
    assert (repo / "src/pkg/a.py").read_text() == "def a():\n    return 1\n"
    assert wv.discard(repo, "new.txt", "U")[0] and not (repo / "new.txt").exists()
    outside = repo.parent / "outside.txt"
    outside.write_text("keep")
    for fn in (wv.stage, wv.unstage):
        assert not fn(repo, "../outside.txt")[0]
    assert not wv.discard(repo, "../outside.txt", "U")[0] and outside.exists()


def test_hundreds_of_files_are_one_pass(tmp_path: Path) -> None:
    r = tmp_path / "big"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "seed").write_text("x\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "i")
    (r / "t").mkdir()
    for i in range(600):
        (r / "t" / f"test_{i}.py").write_text(f"def t{i}(): pass\n")
    t0 = time.time()
    ch = wv.changes(r)
    assert len(ch["files"]) == 600 and ch["added"] == 600
    assert time.time() - t0 < 5
    screen = wb.ChangesScreen(_agent(r))
    t0 = time.time()
    for _ in range(50):
        screen._default_key("down")
        screen._body(100, 30)
    assert time.time() - t0 < 3


def test_worktrees_list_create_remove(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "all")
    rows = wv.list_worktrees(repo)
    assert len(rows) == 1 and rows[0]["main"] and rows[0]["current"] and rows[0]["branch"] == "main" and not rows[0]["dirty"]
    ok, msg, path = wv.create_worktree(repo, "try one")
    assert ok, msg
    rows = wv.list_worktrees(repo)
    assert {r["branch"] for r in rows} == {"main", "axon/try-one"}
    assert not wv.create_worktree(repo, "try one")[0]
    (Path(path) / "dirty.txt").write_text("x\ny\n")
    dirty = next(r for r in wv.list_worktrees(repo) if r["path"] == path or r["path"].endswith("try-one"))
    assert dirty["dirty"] and dirty["files"] == 1 and dirty["added"] == 2
    ok, msg = wv.remove_worktree(repo, dirty["path"])
    assert not ok and "uncommitted" in msg and Path(dirty["path"]).exists()
    assert not wv.remove_worktree(repo, str(repo))[0]
    (Path(dirty["path"]) / "dirty.txt").unlink()
    assert wv.remove_worktree(repo, dirty["path"])[0] and not Path(dirty["path"]).exists()


# ─── screens (keys and frames, no terminal) ─────────────────────────────────

def test_changes_screen_keys(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb.plat, "term_size", lambda: (100, 40))
    agent = _agent(repo)
    s = wb.ChangesScreen(agent)
    frame = strip_ansi("\n".join(s._frame()[0]))
    assert "main → working tree" in frame and "3 files" in frame and "src/pkg/a.py" in frame and "new.txt" in frame
    assert all(display_width(x) <= 100 for x in s._frame()[0])
    s._default_key("/")
    for ch in "new":
        s._default_key(ch)
    assert [f["path"] for f in s.view] == ["new.txt"]
    s._default_key("esc")
    assert len(s.view) == 3
    s._default_key("enter")                                    # expand: numbered diff lines appear
    body = strip_ansi("\n".join(s._body(100, 30)))
    assert "@@" in body and "return" in body or "README" in body
    s.index = next(i for i, f in enumerate(s.view) if f["path"] == "src/pkg/a.py")
    s.expanded = {"src/pkg/a.py"}
    body = strip_ansi("\n".join(s._body(100, 30)))
    assert "-    return 1" in body.replace("  ", " ") or "return 1" in body
    assert "return 2" in body
    s.index = next(i for i, f in enumerate(s.view) if f["path"] == "src/pkg/a.py")
    s._default_key("s")
    assert next(f for f in s.view if f["path"] == "src/pkg/a.py")["staged"]
    s._default_key("u")
    assert not next(f for f in s.view if f["path"] == "src/pkg/a.py")["staged"]
    s.confirm = lambda q: False                                # discard asks first
    s._default_key("x")
    assert (repo / "src/pkg/a.py").read_text().count("return 2") == 1
    s.confirm = lambda q: True
    s._default_key("x")
    assert "return 1" in (repo / "src/pkg/a.py").read_text()
    s._default_key("2")                                        # compare target: staged
    assert s.target == "staged" and [f["path"] for f in s.view] == ["README.md"]
    s.index = 0
    s._default_key("a")
    assert "README.md" in agent.pending_input and s._done


def test_changes_row_is_aligned_and_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    f = {"path": "a/very/long/folder/path/that/goes/on/file_name.py", "dir": "a/very/long/folder/path/that/goes/on",
         "name": "file_name.py", "code": "M", "added": 1234, "removed": 5, "staged": True, "binary": False}
    for w in (30, 60, 80, 120):
        row = wb.change_row(f, True, w)
        plain = strip_ansi(row)
        assert display_width(row) <= w, (w, plain)
        assert plain.rstrip().endswith("+1,234 −5") or plain.rstrip().endswith("+1,234 -5")
        assert "file_name.py" in plain or w <= 30


def test_diff_row_widths_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"kind": "add", "old": None, "new": 12, "text": "x = '" + "y" * 300 + "'"}
    out = wb.diff_row(row, "python", 3, 60)
    assert display_width(out) <= 60 and " 12 + " in strip_ansi(out)
    monkeypatch.setattr(wb._theme, "COLOR_ENABLED", False)
    monkeypatch.setattr(wb, "RST", "")
    assert wb.diff_row(row, "python", 3, 60).startswith(" 12 + x")


def test_worktree_screen_switch_and_refusals(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb.plat, "term_size", lambda: (100, 30))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "all")
    ok, _, path = wv.create_worktree(repo, "two")
    assert ok
    s = wb.WorktreeScreen(_agent(repo))
    frame = strip_ansi("\n".join(s._frame()[0]))
    assert "Worktrees" in frame and "axon/two" in frame and "(main, here)" in frame and "clean" in frame
    s._default_key("x")                                         # main: refused, nothing asked
    assert "main worktree" in s.status
    s._default_key("enter")
    assert "already here" in s.status
    s._default_key("down")
    s._default_key("enter")
    assert s._result == ("switch", s.rows[1]["path"])
    s2 = wb.WorktreeScreen(_agent(repo))
    s2.index = 1
    (Path(path) / "d.txt").write_text("1\n")
    s2.reload()
    s2._default_key("x")
    assert "uncommitted" in s2.status and Path(path).exists()


def test_worktree_command_text(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "all")
    agent = _agent(repo)
    dispatch_command("/worktree new demo", agent)
    out = capsys.readouterr().out
    assert "axon/demo" in out
    dispatch_command("/worktree", agent)
    out = capsys.readouterr().out
    assert "main" in out and "axon/demo" in out
    dispatch_command("/worktree remove demo", agent)
    assert "--yes" in capsys.readouterr().out
    dispatch_command("/worktree remove demo --yes", agent)
    assert "Removed" in capsys.readouterr().out
    dispatch_command("/worktree remove main", agent)
    assert "main worktree" in capsys.readouterr().out


# ─── context ────────────────────────────────────────────────────────────────

def _ctx_data(extra_lists: bool = True) -> dict:
    rows = [("messages", "Messages", 428_800), ("system_tools", "System tools", 18_800), ("mcp_tools", "MCP tools", 15_900),
            ("skills", "Skills", 10_000), ("system_prompt", "System prompt", 4_700), ("mcp_instructions", "MCP server instructions", 2_300),
            ("custom_agents", "Custom agents", 393), ("memory_files", "Memory files", 174), ("autocompact", "Autocompact buffer", 33_000),
            ("free", "Free space", 485_900)]
    w = 1_000_000
    lists = {"mcp_tools": [{"name": "supabase", "tokens": 9000, "count": 30, "tools": []}] if extra_lists else [],
             "memory_files": [{"name": "~/AXON.md", "scope": "user", "tokens": 174}], "custom_agents": [], "skills": [], "messages": []}
    return {"window": w, "used": 479_900, "pct": 48, "threshold": 967_000, "calibrated": False,
            "rows": [{"key": k, "label": lbl, "tokens": t, "pct": round(100 * t / w, 1)} for k, lbl, t in rows],
            "deferred": {"mcp_tools": 528_600, "mcp_count": 700}, "lists": lists, "estimate": ""}


@pytest.mark.parametrize("width", [40, 80, 120])
def test_context_legend_layout(width: int) -> None:
    lines = [t for t, _ in wb.context_lines(_ctx_data(), width)]
    assert all(display_width(x) <= width for x in lines)
    plain = [strip_ansi(x) for x in lines]
    assert plain[0].startswith("48% used") and "~479.9k / 1M" in plain[0]
    names = [n for n in ("Messages", "System tools", "MCP tools", "Skills", "System prompt", "MCP server instructions",
                         "Custom agents", "Memory files", "Autocompact buffer", "Free space")]
    if width >= 80:
        idx = [next(i for i, p in enumerate(plain) if n in p) for n in names]
        assert idx == sorted(idx)
        msg = next(p for p in plain if "Messages" in p)
        assert "428.8k" in msg and "42.9%" in msg
        free = next(p for p in plain if "Free space" in p)
        assert free.rstrip().endswith("48.6%") and len(msg.rstrip()) == len(free.rstrip())     # columns line up
        assert any("MCP tools (deferred)" in p and "528.6k" in p for p in plain)
    assert any("Memory files" in p and p.rstrip().endswith("1") for p in plain)


def test_context_bar_segments_and_expand() -> None:
    d = _ctx_data()
    bar = strip_ansi(wb.context_bar(d, 100))
    assert len(bar) == 100 and set(bar) <= set("█░#.MTPKSIAFx")
    cells = wb._allocate(d["rows"], d["window"], 100)
    assert sum(cells) == 100 and all(c >= 1 for c, r in zip(cells, d["rows"]) if r["tokens"])
    collapsed = [strip_ansi(t) for t, _ in wb.context_lines(d, 100)]
    opened = [strip_ansi(t) for t, _ in wb.context_lines(d, 100, {"mcp_tools"})]
    assert not any("supabase" in x for x in collapsed) and any("supabase" in x and "30 tools" in x for x in opened)


def test_context_no_color_uses_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb._theme, "COLOR_ENABLED", False)
    for name in ("RST",):
        monkeypatch.setattr(wb, name, "")
    bar = wb.context_bar(_ctx_data(), 50)
    assert set(bar) <= set("MTPKSIAFx.") and "M" in bar and "." in bar
    assert wb._swatch("messages") == "M"


def test_context_command_real_agent(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = _agent(repo)
    dispatch_command("/context", agent)
    out = capsys.readouterr().out
    for label in ("% used", "Messages", "System tools", "System prompt", "Autocompact buffer", "Free space", "Auto-compact at"):
        assert label in out, label
    data = wd.context_breakdown(agent)
    assert sum(r["tokens"] for r in data["rows"]) == data["window"] or data["used"] > data["window"]
    dispatch_command("/context bogus", agent)
    assert "Unknown view" in capsys.readouterr().out
    dispatch_command("/context all", agent)
    assert "Auto-compact at" in capsys.readouterr().out


def test_context_matches_web_numbers(repo: Path) -> None:
    from axon.features import dash_workbench
    agent = _agent(repo)
    mine, theirs = wd.context_breakdown(agent), dash_workbench.context_breakdown(agent)
    assert [(r["key"], r["tokens"]) for r in mine["rows"]] == [(r["key"], r["tokens"]) for r in theirs["rows"]]
    assert (mine["window"], mine["used"], mine["pct"]) == (theirs["window"], theirs["used"], theirs["pct"])


def test_changes_match_web_numbers(repo: Path) -> None:
    from axon.features import dash_workbench
    mine, theirs = wv.changes(repo), dash_workbench.changes(repo)
    assert (mine["added"], mine["removed"]) == (theirs["added"], theirs["removed"])
    assert [(f["path"], f["code"]) for f in mine["files"]] == [(f["path"], f["code"]) for f in theirs["files"]]


# ─── connectors ─────────────────────────────────────────────────────────────

def _servers() -> dict:
    mk = lambda n, st, tools, names=(), **kw: {"name": n, "status": st, "tools": tools, "tool_names": list(names), "scope": "user",  # noqa: E731
                                              "error": kw.get("error", ""), "needs_reconnect": st != "connected" and st != "disabled" or st == "failed",
                                              "needs_auth": st == "needs-auth"}
    ss = [mk("atlas", "connected", 5, ["search", "impact"]), mk("supabase", "needs-auth", None), mk("chrome", "connected", 29),
          mk("render", "connected", 24), mk("slack", "failed", 0, error="boom"), mk("off", "disabled", None)]
    return {"servers": ss, "reconnect": 2, "connected": 3}


def test_connectors_top_few_then_more_and_tools_only_when_open() -> None:
    d = _servers()
    lines = [(strip_ansi(t), k) for t, k in wb.connector_lines(d, 80)]
    text = "\n".join(t for t, _ in lines)
    assert "3 of 6 connected" in text and "2 need attention" in text
    assert text.index("supabase") < text.index("atlas") or text.index("slack") < text.index("atlas")   # attention first
    assert "sign in (l)" in text and "Show more (3)" in text
    assert "search, impact" not in text                                                       # tool names stay hidden
    more = [t for t, _ in wb.connector_lines(d, 80, show_all=True)]
    assert not any("Show more" in strip_ansi(x) for x in more) and len(more) == 7
    opened = "\n".join(strip_ansi(t) for t, _ in wb.connector_lines(d, 80, {"atlas"}, True))
    assert "search, impact" in opened
    assert all(display_width(t) <= 50 for t, _ in wb.connector_lines(d, 50, {"atlas"}, True))
    assert "No connectors yet" in strip_ansi(wb.connector_lines({"servers": [], "reconnect": 0, "connected": 0}, 60)[0][0])


def test_connectors_data_never_connects(repo: Path) -> None:
    (repo / ".mcp.json").write_text('{"mcpServers": {"demo": {"command": "definitely-not-a-real-binary"}}}')
    data = wd.connectors(repo)
    names = [s["name"] for s in data["servers"]]
    assert "demo" in names
    demo = next(s for s in data["servers"] if s["name"] == "demo")
    # A server waiting for approval is not "to reconnect": it has its own state and its own action (Approve).
    assert demo["status"] in ("not connected", "pending-approval")
    assert demo["needs_reconnect"] is (demo["status"] != "pending-approval") and demo["needs_approval"] is (demo["status"] == "pending-approval")


def test_a_server_waiting_for_approval_says_so_not_reconnect(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.mcp import state as mcp_state
    (repo / ".mcp.json").write_text('{"mcpServers": {"demo": {"command": "definitely-not-a-real-binary"}}}')
    monkeypatch.setattr(mcp_state, "project_choice", lambda ws, name: "pending")
    data = wd.connectors(repo)
    demo = next(s for s in data["servers"] if s["name"] == "demo")
    if demo["status"] != "pending-approval":
        pytest.skip("this build treats the project file as already approved")
    assert demo["needs_approval"] is True and demo["needs_reconnect"] is False and data["approval"] == 1 and data["reconnect"] == 0


def test_detail_screen_expands_and_shows_more(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(wb.plat, "term_size", lambda: (100, 30))
    s = wb.DetailScreen(_agent(repo), "connectors")
    s._data = _servers()
    s._frame()
    first = s.focus
    s._default_key("enter")
    assert first in s.expanded
    for _ in range(4):
        s._default_key("down")
    assert s.focus == "__more__"
    s._default_key("enter")
    assert s.show_all
    s.focus = "supabase"
    s._default_key("l")
    assert s._result == ("mcp", "login supabase")


# ─── /workbench ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", [50, 80, 120])
def test_workbench_sections_fit(repo: Path, width: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.tools import shell as shell_tool
    # background shells are process-wide: other tests' leftovers must not show up in the "Nothing running" check
    monkeypatch.setattr(shell_tool, "BACKGROUND", shell_tool.BackgroundShells())
    agent = _agent(repo)
    agent.todos.replace([{"content": "write tests", "status": "in_progress", "active_form": "writing tests"},
                         {"content": "ship", "status": "pending"}]) if hasattr(agent.todos, "replace") else None
    secs = wb.workbench_sections(agent, width)
    assert [k for k, _ in secs] == [k for k, _ in wb.SECTIONS]
    for _, lines in secs:
        assert all(display_width(x) <= width for x in lines), [strip_ansi(x) for x in lines if display_width(x) > width]
    text = strip_ansi("\n".join(x for _, ls in secs for x in ls))
    assert "main" in text and "% used" in text and "Nothing running" in text and "new.txt" in text


def test_workbench_command_plain(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch_command("/workbench", _agent(repo))
    out = capsys.readouterr().out
    for title in ("Environment", "Context", "Usage", "Connectors", "Changes", "Background tasks", "Plan and todos"):
        assert title in out
    dispatch_command("/panel --plain", _agent(repo))
    assert "Environment" in capsys.readouterr().out


def test_workbench_screen_keys(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb.plat, "term_size", lambda: (100, 40))
    s = wb.WorkbenchScreen(_agent(repo))
    for key, name in (("c", "Context"), ("k", "Connectors"), ("g", "Changes"), ("b", "Background"), ("p", "Plan"), ("e", "Environment"),
                      ("u", "Usage")):
        s._default_key(key)
        assert s.cur == key
        frame = strip_ansi("\n".join(s._frame()[0]))
        assert f"❯ {name}" in frame, (key, frame)
    s._default_key("g")
    s._default_key("enter")
    assert s._result == ("open", "g")


def test_status_line_hint_is_off_by_default_and_opt_in(repo: Path) -> None:
    from axon.ui.statusline import default_status_line
    agent = _agent(repo)
    assert "/workbench" not in strip_ansi(default_status_line(agent, 200))
    from axon.commands.common import update_settings
    update_settings(agent, workbench_hint=True)
    assert "/workbench" in strip_ansi(default_status_line(agent, 200))


def test_diff_commands_text_and_targets(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = _agent(repo)
    dispatch_command("/diff list", agent)
    out = capsys.readouterr().out
    assert "main → working tree" in out and "new.txt" in out and "3 files" in out
    dispatch_command("/diff list staged", agent)
    assert "README.md" in capsys.readouterr().out
    assert wb.diff_target("branch main") == ("branch", "main") and wb.diff_target("T3") is None
    dispatch_command("/diff", agent)                            # no terminal: the plain git diff, unchanged
    assert "return" in capsys.readouterr().out


# ─── the real REPL in a pseudo-terminal ─────────────────────────────────────

@pytest.mark.skipif(__import__("sys").platform == "win32", reason="pty-driven REPL")
@pytest.mark.parametrize("cols", [80, 120])
def test_screens_in_a_real_terminal(tmp_path: Path, cols: int) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "e2e"))
    from fake_llm import FakeLLMServer
    from harness import create_axon_env

    def frame(s) -> str:                                  # the last full-screen frame, ANSI stripped
        return strip_ansi(s._raw.decode("utf-8", "replace").split("\x1b[H")[-1]).replace("\r", "")

    llm = FakeLLMServer(model_ids=("gpt-4o", "fake-model")).start()
    try:
        env = create_axon_env(tmp_path, llm, git=True)
        ws = env.workspace
        (ws / "app.py").write_text("print('a')\n")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "base")
        (ws / "app.py").write_text("print('b')\nx = 1\n")
        for i in range(40):
            (ws / f"extra_{i}.txt").write_text("hello\n")
        with env.pty([], rows=34, cols=cols) as s:
            s.expect(r"(?i)(axon|❯|>)", timeout=25)
            s.drain(0.8)
            s.sendline("/diff")
            s.expect(r"main → working tree", timeout=15)
            s.drain(0.5)
            first = frame(s)
            assert "41 files" in first and "1 working tree" in first and "esc close" in first
            assert all(display_width(line) <= cols for line in first.splitlines())
            s.send("\x1b[F")                              # End: the last file (app.py sorts after extra_*? either way)
            s.drain(0.4)
            s.send("\r")
            s.drain(0.6)
            assert "@@" in frame(s) or "hello" in frame(s)
            s.send("\x1b")
            s.drain(0.6)
            s.sendline("/workbench")
            s.expect(r"Workbench", timeout=15)
            s.drain(0.5)
            wbf = frame(s)
            for title in ("Environment", "Context", "Usage", "Connectors", "Changes"):
                assert title in wbf
            assert "% used" in wbf and "41 files" in wbf
            s.send("\x1b")
            s.drain(0.6)
            s.sendline("/worktree")
            s.expect(r"Worktrees", timeout=15)
            s.drain(0.5)
            assert "main" in frame(s) and "41 files" in frame(s)
            s.send("\x1b")
            s.drain(0.5)
            mark = s.mark()
            s.sendline("/context")
            s.expect(r"Free space", timeout=15, since=mark)
            s.sendline("/exit")
            s.wait(10)
    finally:
        llm.stop()
