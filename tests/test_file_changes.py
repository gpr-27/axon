"""Every file change is shown: Write always diffs (created files as additions), and files that a shell
command creates, modifies or deletes are detected and drawn as a coloured change list with diffs."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from axon.agent import filechanges
from axon.providers.base import ToolExecutionResult, ToolExecutionStart
from axon.tools.base import ToolContext
from axon.tools.fs_write import WriteTool
from axon.ui import render
from axon.ui.render import Renderer, tool_result_rows
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

OLD_POST = "\n".join(f"old line {i}" for i in range(12)) + "\n"
NEW_POST = "\n".join(f"brand new line {i}" for i in range(9)) + "\n"


def _plain(rows: list[str]) -> list[str]:
    return [strip_ansi(r) for r in rows]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# ─── Write ─────────────────────────────────────────────────────────────────

def test_full_rewrite_still_shows_a_coloured_diff(tmp_path, monkeypatch, capsys):
    """A Write that replaces most of a file used to fall back to a plain code card with no counts."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "post.md").write_text(OLD_POST, encoding="utf-8")
    r = Renderer(trace_level="normal")
    r.on_event(ToolExecutionStart(id="w", name="Write", input={"path": "post.md", "content": NEW_POST}))
    (tmp_path / "post.md").write_text(NEW_POST, encoding="utf-8")
    r.on_event(ToolExecutionResult(id="w", name="Write", input={}, content="Successfully wrote ... Overwrote file."))
    out = strip_ansi(capsys.readouterr().out)
    assert "⟳ Overwrote post.md · +9 −12 · 9 lines" in out
    assert "- old line 0" in out and "+ brand new line 0" in out


def test_created_file_is_drawn_as_additions():
    rows = _plain(tool_result_rows("Write", {"path": "a.py", "content": "x = 1\ny = 2\n"},
                                   "Successfully wrote 12 characters (2 lines) to a.py. Created file.", width=80))
    assert rows[0].startswith("✚ Created a.py · +2 · ■ Python · 2 lines")
    assert rows[1].split() == ["1", "+", "x", "=", "1"]
    assert rows[2].split() == ["2", "+", "y", "=", "2"]


def test_write_tool_reports_line_counts(tmp_path):
    ctx = ToolContext(workspace=tmp_path, file_state=_file_state(), todos=None, settings=None)  # type: ignore[arg-type]
    out = WriteTool().run({"path": "n.txt", "content": "a\nb\n"}, ctx)
    assert out.endswith("Created file (+2).")
    ctx.file_state.record_read(tmp_path / "n.txt")
    out = WriteTool().run({"path": "n.txt", "content": "a\nc\nd\n"}, ctx)
    assert out.endswith("Overwrote file (+2 −1).")


def test_restored_overwrite_keeps_its_counts():
    """History has no old content, but the tool output carries the counts."""
    rows = _plain(tool_result_rows("Write", {"path": "m.py", "content": "a = 1\n"},
                                   "Successfully wrote 6 characters (1 lines) to m.py. Overwrote file (+1 −3).", width=80))
    assert rows[0].startswith("⟳ Overwrote m.py · +1 −3 · ■ Python · 1 line")


def _file_state():
    from axon.agent.state import FileState
    return FileState()


# ─── shell commands ────────────────────────────────────────────────────────

@pytest.mark.parametrize("use_git", [True, False])
def test_snapshot_detects_created_modified_and_deleted_files(tmp_path, use_git):
    if use_git and not shutil.which("git"):
        pytest.skip("git not installed")
    (tmp_path / "keep.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "same.txt").write_text("same\n", encoding="utf-8")
    if use_git:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    before = filechanges.take_snapshot(tmp_path)
    assert before is not None
    (tmp_path / "keep.py").write_text("a = 1\nb = 20\nc = 3\n", encoding="utf-8")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "new.md").write_text("# Title\n\nbody\n", encoding="utf-8")
    changes = {c.path: c for c in filechanges.changes_since(before, tmp_path)}
    assert set(changes) == {"keep.py", "gone.txt", "docs/new.md"}
    assert (changes["keep.py"].kind, changes["keep.py"].added, changes["keep.py"].removed) == ("modified", 2, 1)
    assert changes["keep.py"].old == "a = 1\nb = 2\n" and changes["keep.py"].new == "a = 1\nb = 20\nc = 3\n"
    assert (changes["gone.txt"].kind, changes["gone.txt"].removed) == ("deleted", 3)
    assert (changes["docs/new.md"].kind, changes["docs/new.md"].added) == ("created", 3)
    line = filechanges.summary_line(list(changes.values()))
    assert line.startswith("[Files changed by this command: ")
    assert "created docs/new.md (+3)" in line and "modified keep.py (+2 −1)" in line and "deleted gone.txt (−3)" in line


def test_snapshot_skips_heavy_and_ignored_folders(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("1\n", encoding="utf-8")
    before = filechanges.take_snapshot(tmp_path)
    assert before is not None
    (tmp_path / "node_modules" / "y.js").write_text("2\n", encoding="utf-8")
    assert filechanges.changes_since(before, tmp_path) == []


def test_bash_result_carries_the_file_changes(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    events: list = []
    cmd = "echo world >> a.txt && echo fresh > b.txt"
    agent = make_agent(FakeProvider(scripted(("Bash", {"command": cmd}), "done")), tmp_path, events=events)
    agent.run_turn("change files")
    res = next(e for e in events if isinstance(e, ToolExecutionResult) and e.name == "Bash")
    kinds = {c.path: (c.kind, c.added, c.removed) for c in res.changes}
    assert kinds == {"a.txt": ("modified", 1, 0), "b.txt": ("created", 1, None)}
    assert "[Files changed by this command: " in res.content     # the model knows too, and history keeps it


def test_bash_changes_render_as_a_coloured_change_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    changes = (
        filechanges.FileChange("docs/new.md", "created", 3, None, None, "# Title\n\nbody\n"),
        filechanges.FileChange("keep.py", "modified", 2, 1, "a = 1\nb = 2\n", "a = 1\nb = 20\nc = 3\n"),
        filechanges.FileChange("gone.txt", "deleted", None, 3, "one\ntwo\nthree\n", None),
    )
    r = Renderer(trace_level="normal")
    r.on_event(ToolExecutionStart(id="b", name="Bash", input={"command": "python tool.py"}))
    r.on_event(ToolExecutionResult(id="b", name="Bash", input={"command": "python tool.py"}, content="ok",
                                   changes=changes))
    out = strip_ansi(capsys.readouterr().out)
    assert "3 files changed · +5 −4" in out
    assert "✚ Created  docs/new.md" in out and "⟳ Modified keep.py" in out and "✖ Deleted  gone.txt" in out
    assert "2 - b = 2" in out and "2 + b = 20" in out           # the modified file's diff is shown
    assert render.tool_result_rows("Bash", {"command": "x"}, "ok", width=80)  # no changes: unchanged path


def test_a_move_is_one_rename_not_a_delete_and_a_create(tmp_path):
    (tmp_path / "hi.tht").write_text("hello\nthere\n", encoding="utf-8")
    before = filechanges.take_snapshot(tmp_path)
    (tmp_path / "hi.tht").rename(tmp_path / "hi.txt")
    changes = filechanges.changes_since(before, tmp_path)
    assert [(c.kind, c.old_path, c.path) for c in changes] == [("renamed", "hi.tht", "hi.txt")]
    assert filechanges.summary_line(changes) == "[Files changed by this command: renamed hi.tht → hi.txt]"


def test_files_in_git_ignored_folders_are_tracked_too(tmp_path):
    """The LinkedIn folder in this repo is git-ignored, and a rename inside it went unseen."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("notes/\n.venv/\n", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("one\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    before = filechanges.take_snapshot(tmp_path)
    (tmp_path / "notes" / "a.md").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib.py").write_text("x\n", encoding="utf-8")       # dependency folder: skipped
    changes = filechanges.changes_since(before, tmp_path)
    assert [(c.path, c.kind, c.added, c.removed) for c in changes] == [("notes/a.md", "modified", 1, 0)]


def test_secret_files_are_listed_but_never_diffed(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=old\n", encoding="utf-8")
    before = filechanges.take_snapshot(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=new\n", encoding="utf-8")
    [c] = filechanges.changes_since(before, tmp_path)
    assert (c.path, c.kind, c.added, c.removed, c.old, c.new) == (".env", "modified", 1, 1, None, None)
    rows = _plain(render._cards.file_change_rows([c], 80))
    assert not any("API_KEY" in r for r in rows)


def test_rename_row_and_restored_history():
    change = filechanges.FileChange("linkedin/hi.txt", "renamed", None, None, "x\n", "x\n", old_path="linkedin/hi.tht")
    rows = _plain(render._cards.file_change_rows([change], 100))
    assert rows[0].startswith("✎ 1 file changed")
    assert "➜ Renamed  linkedin/hi.txt  from linkedin/hi.tht · same content" in rows[1]
    restored = _plain(tool_result_rows("Bash", {"command": "mv a b"},
                                       "ok\n[Files changed by this command: renamed a → b, created c.md (+2)]", width=100))
    text = "\n".join(restored)
    assert "➜ Renamed  b" in text and "from a" in text and "✚ Created  c.md" in text and "+2" in text
    assert "[Files changed" not in text          # the raw line becomes the change list
