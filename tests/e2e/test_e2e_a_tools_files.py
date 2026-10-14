"""
E2E (agent A): file tools - Read, Write, Edit, MultiEdit, NotebookEdit, Patch - driven through the real
`axon -p` CLI against the fake LLM. Assertions are on workspace files, stream-json tool results and
the tool results the model receives in the next request.
"""
from __future__ import annotations

import base64
import json

import pytest

from harness import AxonEnv
from test_e2e_a_common import final, no_traceback, results, run_tools

pytestmark = pytest.mark.e2e

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _ok(r) -> None:
    assert r.code == 0, str(r)
    no_traceback(r)


# ─── Read ───────────────────────────────────────────────────────────────────

def test_read_returns_numbered_lines_to_model(axon_env: AxonEnv) -> None:
    """Read output is line-numbered and is delivered to the model as the tool result of the next request."""
    (axon_env.workspace / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "a.txt"})])
    _ok(r)
    out = results(ev, "Read")[0]
    assert not out["is_error"]
    assert "1→alpha" in out["content"] and "3→gamma" in out["content"]
    nxt = axon_env.llm.completion_requests()[1]
    assert any("2→beta" in t for t in nxt.tool_results())


def test_read_offset_and_limit_window(axon_env: AxonEnv) -> None:
    """Read honours offset/limit: only lines 5..7 are returned and a continuation hint names offset=8."""
    (axon_env.workspace / "n.txt").write_text("".join(f"line{i}\n" for i in range(1, 21)), encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "n.txt", "offset": 5, "limit": 3})])
    _ok(r)
    c = results(ev, "Read")[0]["content"]
    assert "5→line5" in c and "7→line7" in c
    assert "line4" not in c and "line8\n" not in c and "8→" not in c
    assert "offset=8" in c


def test_read_binary_file_is_not_dumped(axon_env: AxonEnv) -> None:
    """A binary file is reported as binary (size shown) instead of being decoded into garbage text."""
    (axon_env.workspace / "blob.dat").write_bytes(bytes(range(256)) * 4)
    r, ev = run_tools(axon_env, [("Read", {"path": "blob.dat"})])
    _ok(r)
    c = results(ev, "Read")[0]["content"]
    assert "Binary file" in c and "1,024 bytes" in c
    assert "\x00" not in c


def test_read_image_reports_dimensions(axon_env: AxonEnv) -> None:
    """Reading a PNG yields an image description with its pixel dimensions, not raw bytes."""
    (axon_env.workspace / "pic.png").write_bytes(PNG_1x1)
    r, ev = run_tools(axon_env, [("Read", {"path": "pic.png"})])
    _ok(r)
    c = results(ev, "Read")[0]["content"]
    assert "Image file" in c and "1x1" in c


def test_read_missing_file_is_error_result_not_crash(axon_env: AxonEnv) -> None:
    """Reading a non-existent file returns an is_error tool result and the turn still finishes normally."""
    r, ev = run_tools(axon_env, [("Read", {"path": "nope/missing.txt"})])
    _ok(r)
    res = results(ev, "Read")[0]
    assert res["is_error"] and "not found" in res["content"].lower()
    assert final(ev)["result"].strip() == "FINAL-ANSWER"


def test_read_crlf_file_announces_line_endings(axon_env: AxonEnv) -> None:
    """Read flags CRLF files so the model knows edits will preserve them."""
    (axon_env.workspace / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    r, ev = run_tools(axon_env, [("Read", {"path": "w.txt"})])
    _ok(r)
    c = results(ev, "Read")[0]["content"]
    assert "CRLF" in c and "1→one" in c and "\r" not in c


# ─── Write ──────────────────────────────────────────────────────────────────

def test_write_creates_new_file_with_parents(axon_env: AxonEnv) -> None:
    """Write creates a new file (and missing parent directories) with the exact content."""
    r, ev = run_tools(axon_env, [("Write", {"path": "deep/er/new.txt", "content": "x=1\ny=2\n"})])
    _ok(r)
    assert (axon_env.workspace / "deep/er/new.txt").read_text(encoding="utf-8") == "x=1\ny=2\n"
    assert "Created file" in results(ev, "Write")[0]["content"]


def test_write_overwrite_requires_prior_read(axon_env: AxonEnv) -> None:
    """Overwriting an existing file that was never Read in the session is refused; the file is unchanged."""
    target = axon_env.workspace / "keep.txt"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Write", {"path": "keep.txt", "content": "CLOBBERED\n"})])
    _ok(r)
    res = results(ev, "Write")[0]
    assert res["is_error"] and "Read" in res["content"]
    assert target.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_write_overwrite_allowed_after_read(axon_env: AxonEnv) -> None:
    """After a Read in the same session, Write may overwrite the file."""
    target = axon_env.workspace / "keep.txt"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "keep.txt"}), ("Write", {"path": "keep.txt", "content": "NEW\n"})])
    _ok(r)
    assert not results(ev, "Write")[0]["is_error"]
    assert target.read_text(encoding="utf-8") == "NEW\n"
    assert "Overwrote" in results(ev, "Write")[0]["content"]


def test_write_detects_external_modification_after_read(axon_env: AxonEnv) -> None:
    """If the file changes on disk between Read and Write (here: via Bash), the stale Write must not
    silently clobber the newer content."""
    target = axon_env.workspace / "race.txt"
    target.write_text("v1\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [
        ("Read", {"path": "race.txt"}),
        ("Bash", {"command": "sleep 1; echo v2-external > race.txt", "description": "modify"}),
        ("Write", {"path": "race.txt", "content": "v3-from-model\n"}),
    ])
    _ok(r)
    res = results(ev, "Write")[0]
    assert res["is_error"], res
    assert "v2-external" in target.read_text(encoding="utf-8")


# ─── Edit ───────────────────────────────────────────────────────────────────

def test_edit_unique_replacement(axon_env: AxonEnv) -> None:
    """Edit replaces a unique old_string exactly once."""
    f = axon_env.workspace / "e.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "e.py"}), ("Edit", {"path": "e.py", "old_string": "y = 2", "new_string": "y = 3"})])
    _ok(r)
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 3\n"
    assert not results(ev, "Edit")[0]["is_error"]


def test_edit_ambiguous_match_lists_lines_and_changes_nothing(axon_env: AxonEnv) -> None:
    """A non-unique old_string is rejected with the matching line numbers and a replace_all hint."""
    f = axon_env.workspace / "d.txt"
    f.write_text("foo\nbar\nfoo\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "d.txt"}), ("Edit", {"path": "d.txt", "old_string": "foo", "new_string": "X"})])
    _ok(r)
    res = results(ev, "Edit")[0]
    assert res["is_error"] and "2 times" in res["content"] and "1, 3" in res["content"] and "replace_all" in res["content"]
    assert f.read_text(encoding="utf-8") == "foo\nbar\nfoo\n"


def test_edit_replace_all(axon_env: AxonEnv) -> None:
    """replace_all=true replaces every occurrence."""
    f = axon_env.workspace / "d.txt"
    f.write_text("foo\nbar\nfoo\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "d.txt"}),
                                 ("Edit", {"path": "d.txt", "old_string": "foo", "new_string": "X", "replace_all": True})])
    _ok(r)
    assert f.read_text(encoding="utf-8") == "X\nbar\nX\n"
    assert "2 occurrences" in results(ev, "Edit")[0]["content"]


def test_edit_not_found_gives_reread_hint(axon_env: AxonEnv) -> None:
    """old_string not present -> error result telling the model to re-read the file."""
    f = axon_env.workspace / "d.txt"
    f.write_text("hello\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "d.txt"}), ("Edit", {"path": "d.txt", "old_string": "absent", "new_string": "x"})])
    _ok(r)
    res = results(ev, "Edit")[0]
    assert res["is_error"] and "not found" in res["content"] and "Re-read" in res["content"]


def test_edit_without_read_is_refused(axon_env: AxonEnv) -> None:
    """Edit on a file never Read in this session is refused and the file is untouched."""
    f = axon_env.workspace / "d.txt"
    f.write_text("hello\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Edit", {"path": "d.txt", "old_string": "hello", "new_string": "bye"})])
    _ok(r)
    assert results(ev, "Edit")[0]["is_error"]
    assert f.read_text(encoding="utf-8") == "hello\n"


def test_edit_preserves_crlf_line_endings(axon_env: AxonEnv) -> None:
    """Editing a CRLF file (old/new strings given with LF) keeps every line ending CRLF."""
    f = axon_env.workspace / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    r, ev = run_tools(axon_env, [("Read", {"path": "win.txt"}),
                                 ("Edit", {"path": "win.txt", "old_string": "two\nthree", "new_string": "TWO\nTHREE\nFOUR"})])
    _ok(r)
    assert not results(ev, "Edit")[0]["is_error"], results(ev, "Edit")
    assert f.read_bytes() == b"one\r\nTWO\r\nTHREE\r\nFOUR\r\n"


def test_edit_identical_strings_rejected(axon_env: AxonEnv) -> None:
    """old_string == new_string is rejected as a no-op edit."""
    f = axon_env.workspace / "d.txt"
    f.write_text("same\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "d.txt"}), ("Edit", {"path": "d.txt", "old_string": "same", "new_string": "same"})])
    _ok(r)
    assert results(ev, "Edit")[0]["is_error"]


# ─── MultiEdit ──────────────────────────────────────────────────────────────

def test_multiedit_applies_sequential_edits(axon_env: AxonEnv) -> None:
    """MultiEdit applies its edits in order (the second edit sees the result of the first)."""
    f = axon_env.workspace / "m.txt"
    f.write_text("a b c\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "m.txt"}), ("MultiEdit", {"path": "m.txt", "edits": [
        {"old_string": "a", "new_string": "A1"}, {"old_string": "A1 b", "new_string": "A2 B"}]})])
    _ok(r)
    assert not results(ev, "MultiEdit")[0]["is_error"]
    assert f.read_text(encoding="utf-8") == "A2 B c\n"


def test_multiedit_is_atomic_on_failure(axon_env: AxonEnv) -> None:
    """If any edit in a MultiEdit fails, none are applied (all-or-nothing) and the error names the edit."""
    f = axon_env.workspace / "m.txt"
    f.write_text("keep1\nkeep2\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "m.txt"}), ("MultiEdit", {"path": "m.txt", "edits": [
        {"old_string": "keep1", "new_string": "CHANGED"}, {"old_string": "missing", "new_string": "z"}]})])
    _ok(r)
    res = results(ev, "MultiEdit")[0]
    assert res["is_error"] and "#2" in res["content"]
    assert f.read_text(encoding="utf-8") == "keep1\nkeep2\n"


# ─── NotebookEdit ───────────────────────────────────────────────────────────

def _nb(cells: list[dict]) -> str:
    return json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def test_notebookedit_insert_markdown_cell(axon_env: AxonEnv) -> None:
    """NotebookEdit inserts a markdown cell at the given index, producing a valid notebook."""
    nb = axon_env.workspace / "n.ipynb"
    nb.write_text(_nb([{"cell_type": "code", "id": "c1", "source": ["print(1)"], "metadata": {}, "outputs": [],
                        "execution_count": None}]), encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "n.ipynb"}), ("NotebookEdit", {
        "notebook_path": "n.ipynb", "edit_mode": "insert", "cell_index": 1, "new_source": "# Title", "cell_type": "markdown"})])
    _ok(r)
    assert not results(ev, "NotebookEdit")[0]["is_error"], results(ev, "NotebookEdit")
    data = json.loads(nb.read_text(encoding="utf-8"))
    assert [c["cell_type"] for c in data["cells"]] == ["code", "markdown"]
    assert "".join(data["cells"][1]["source"]) == "# Title"


def test_notebookedit_replace_and_delete(axon_env: AxonEnv) -> None:
    """NotebookEdit replace changes a cell's source; delete removes a cell."""
    nb = axon_env.workspace / "n.ipynb"
    cells = [{"cell_type": "code", "id": f"c{i}", "source": [f"x={i}"], "metadata": {}, "outputs": [],
              "execution_count": None} for i in range(3)]
    nb.write_text(_nb(cells), encoding="utf-8")
    r, ev = run_tools(axon_env, [
        ("Read", {"path": "n.ipynb"}),
        ("NotebookEdit", {"notebook_path": "n.ipynb", "edit_mode": "replace", "cell_index": 0, "new_source": "x=100"}),
        ("NotebookEdit", {"notebook_path": "n.ipynb", "edit_mode": "delete", "cell_index": 2}),
    ])
    _ok(r)
    assert not any(x["is_error"] for x in results(ev, "NotebookEdit")), results(ev, "NotebookEdit")
    data = json.loads(nb.read_text(encoding="utf-8"))
    assert len(data["cells"]) == 2
    assert "".join(data["cells"][0]["source"]) == "x=100"


def test_notebookedit_requires_read(axon_env: AxonEnv) -> None:
    """NotebookEdit on a notebook not Read in the session is refused and the notebook is untouched."""
    nb = axon_env.workspace / "n.ipynb"
    raw = _nb([{"cell_type": "code", "source": ["1"], "metadata": {}, "outputs": [], "execution_count": None}])
    nb.write_text(raw, encoding="utf-8")
    r, ev = run_tools(axon_env, [("NotebookEdit", {"notebook_path": "n.ipynb", "edit_mode": "delete", "cell_index": 0})])
    _ok(r)
    assert results(ev, "NotebookEdit")[0]["is_error"]
    assert nb.read_text(encoding="utf-8") == raw


# ─── Patch ──────────────────────────────────────────────────────────────────

def test_patch_multi_hunk(axon_env: AxonEnv) -> None:
    """A unified diff with two hunks is applied to the file."""
    f = axon_env.workspace / "p.txt"
    f.write_text("a\nb\nc\nd\ne\nf\ng\nh\n", encoding="utf-8")
    patch = ("--- a/p.txt\n+++ b/p.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
             "@@ -6,3 +6,3 @@\n f\n-g\n+G\n h\n")
    r, ev = run_tools(axon_env, [("Read", {"path": "p.txt"}), ("Patch", {"patch": patch})])
    _ok(r)
    res = results(ev, "Patch")[0]
    assert not res["is_error"] and "2 hunks" in res["content"]
    assert f.read_text(encoding="utf-8") == "a\nB\nc\nd\ne\nf\nG\nh\n"


def test_patch_creates_new_file(axon_env: AxonEnv) -> None:
    """A /dev/null -> b/new.txt diff creates the new file."""
    patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+first\n+second\n"
    r, ev = run_tools(axon_env, [("Patch", {"patch": patch})])
    _ok(r)
    assert not results(ev, "Patch")[0]["is_error"], results(ev, "Patch")
    assert (axon_env.workspace / "new.txt").read_text(encoding="utf-8") == "first\nsecond\n"


def test_patch_context_mismatch_is_rejected_cleanly(axon_env: AxonEnv) -> None:
    """A hunk whose context does not match leaves the file unchanged and reports an error."""
    f = axon_env.workspace / "p.txt"
    f.write_text("x\ny\nz\n", encoding="utf-8")
    patch = "--- a/p.txt\n+++ b/p.txt\n@@ -1,3 +1,3 @@\n q\n-r\n+R\n s\n"
    r, ev = run_tools(axon_env, [("Read", {"path": "p.txt"}), ("Patch", {"patch": patch})])
    _ok(r)
    assert results(ev, "Patch")[0]["is_error"]
    assert f.read_text(encoding="utf-8") == "x\ny\nz\n"
