"""
Tests for the consolidated tool set: registry, Read/Write/Edit/MultiEdit, Glob/Grep/Ls,
Patch, NotebookEdit, CodeSearch, workspace_files, Doctor, TodoWrite.
No network access.
"""
from __future__ import annotations

import json
import os
import subprocess
import shutil
import zlib
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import StaleFileError, ToolError
from axon.session.checkpoint import CheckpointManager
from axon.tools import create_default_registry, textio
from axon.tools import search as search_mod
from axon.tools._files import glob_to_regex, iter_files, workspace_files
from axon.tools.base import ToolContext
from axon.tools.registry import ToolRegistry


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=workspace),
        checkpoints=CheckpointManager(workspace),
    )


@pytest.fixture
def reg() -> ToolRegistry:
    return create_default_registry()


@pytest.fixture(params=["rg", "python"])
def backend(request, monkeypatch):
    """Run search tests against ripgrep (when installed) and the pure-Python fallback."""
    if request.param == "rg":
        if not search_mod.find_rg():
            pytest.skip("ripgrep not installed")
    else:
        monkeypatch.setattr(search_mod, "find_rg", lambda: None)
    return request.param


# ─── Registry ────────────────────────────────────────────────────────────────

def test_default_tool_set_is_compact(reg):
    names = set(reg.visible_names())
    for n in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "Ls", "Bash",
              "TodoWrite", "WebFetch", "WebSearch", "Task", "ExitPlanMode", "CodeSearch", "Patch",
              "Skill", "AskUserQuestion"):
        assert n in names
    for removed in ("FileTree", "CodeSymbols", "TableSearch", "DeepResearch", "Http", "Env", "Diff",
                    "UiPreview", "Git", "GoToDefinition", "FindReferences", "SemanticSearch", "Doctor"):
        assert removed not in names
    assert len(names) <= 22  # 20 built-ins + feature TOOLS (UsageStats)


@pytest.mark.parametrize("style", ["anthropic", "openai"])
def test_schemas_are_valid_json_schema(reg, style):
    schemas = reg.schemas(provider_style=style)
    names = []
    for s in schemas:
        if style == "anthropic":
            name, params = s["name"], s["input_schema"]
        else:
            assert s["type"] == "function"
            name, params = s["function"]["name"], s["function"]["parameters"]
        names.append(name)
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert set(params["required"]) <= set(params["properties"])
        for prop in params["properties"].values():
            assert "type" in prop or "enum" in prop
        json.dumps(params)
    assert names == sorted(names)
    assert "Doctor" not in names  # hidden
    assert "Doctor" in [s["name"] for s in reg.schemas(include_hidden=True)]


def test_schema_sanitizer_drops_unknown_required():
    from axon.tools.registry import _sanitize_schema
    out = _sanitize_schema({"properties": {"a": {"type": "string"}}, "required": ["a", "zzz"]})
    assert out == {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert _sanitize_schema(None) == {"type": "object", "properties": {}, "required": []}


def test_unregister_subset_and_filters(reg):
    assert reg.unregister("WebSearch") is not None
    assert "WebSearch" not in reg
    assert reg.unregister("NoSuchTool") is None

    sub = reg.subset(["Read", "Grep", "CodeSymbols", "Bogus"])
    assert sub.names() == ["Read", "Grep", "CodeSearch"]  # alias resolved, unknown ignored

    ro = reg.subset(readonly_only=True)
    assert all(t.readonly or t.name == "ExitPlanMode" for t in ro.all_tools())
    assert "Write" not in ro

    f = reg.filtered(allow=["Read", "Gl*", "Grep"], deny=["Grep"])
    assert sorted(f.names()) == ["Glob", "Read"]
    assert "Bash" not in reg.subset(deny=["Bash"])


def test_aliases_resolve_with_preset_args(reg, ctx, workspace):
    (workspace / "m.py").write_text("def fn(a):\n    pass\n", encoding="utf-8")
    assert reg.get("LS").name == "Ls"
    assert reg.get("notebook_edit").name == "NotebookEdit"
    out = reg.get("CodeSymbols").run({"path": "m.py"}, ctx)
    assert "def fn(a)" in out
    tree = reg.get("FileTree").run({"path": ".", "max_depth": 2}, ctx)
    assert "directories" in tree
    with pytest.raises(ToolError, match="Unknown tool"):
        reg.get("Http")


# ─── Read ────────────────────────────────────────────────────────────────────

def test_read_line_numbers_and_paging(reg, ctx, workspace):
    (workspace / "f.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    out = reg.get("Read").run({"path": "f.txt", "offset": 3, "limit": 2}, ctx)
    assert "     3→line 3" in out and "     4→line 4" in out
    assert "line 5" not in out
    assert "offset=5" in out


def test_read_form_feed_does_not_shift_line_numbers(reg, ctx, workspace):
    (workspace / "ff.txt").write_text("a\x0cb\nc\n", encoding="utf-8")
    out = reg.get("Read").run({"path": "ff.txt"}, ctx)
    assert "     2→c" in out


def test_read_binary_image_pdf(reg, ctx, workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    assert "Binary file" in reg.get("Read").run({"path": "blob.bin"}, ctx)

    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"\x00" * 20
    (workspace / "pic.png").write_bytes(png)
    out = reg.get("Read").run({"path": "pic.png"}, ctx)
    assert "Image file" in out and "64x32" in out

    stream = zlib.compress(b"BT /F1 12 Tf 72 712 Td (Hello PDF World) Tj ET")
    pdf = (b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Length " + str(len(stream)).encode()
           + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream\nendobj\n%%EOF")
    (workspace / "doc.pdf").write_bytes(pdf)
    out = reg.get("Read").run({"path": "doc.pdf"}, ctx)
    assert "Hello PDF World" in out


def test_read_notebook_and_cp1252(reg, ctx, workspace):
    nb = {"cells": [{"cell_type": "code", "source": ["print(1)"], "outputs": [], "execution_count": None, "metadata": {}}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (workspace / "n.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    out = reg.get("Read").run({"path": "n.ipynb"}, ctx)
    assert "[Cell 0] (code)" in out and "print(1)" in out

    (workspace / "latin.txt").write_bytes("caf\xe9 cr\xe8me\n".encode("cp1252"))
    out = reg.get("Read").run({"path": "latin.txt"}, ctx)
    assert "café crème" in out and "cp1252" in out


def test_read_missing_suggests(reg, ctx, workspace):
    (workspace / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="config.yaml"):
        reg.get("Read").run({"path": "config.yml"}, ctx)


# ─── Write / Edit / MultiEdit ────────────────────────────────────────────────

def test_edit_preserves_crlf_and_encoding(reg, ctx, workspace):
    f = workspace / "win.txt"
    f.write_bytes("alpha\r\nbéta\r\ngamma\r\n".encode("cp1252"))
    reg.get("Read").run({"path": "win.txt"}, ctx)
    reg.get("Edit").run({"path": "win.txt", "old_string": "béta\ngamma", "new_string": "BETA\ngamma\ndelta"}, ctx)
    assert f.read_bytes() == "alpha\r\nBETA\r\ngamma\r\ndelta\r\n".encode("cp1252")


def test_write_keeps_lf_and_crlf_styles(reg, ctx, workspace):
    f = workspace / "new.txt"
    reg.get("Write").run({"path": "new.txt", "content": "a\nb\n"}, ctx)
    assert f.read_bytes() == b"a\nb\n"  # no CRLF translation on any OS
    g = workspace / "crlf.txt"
    g.write_bytes(b"x\r\ny\r\n")
    reg.get("Read").run({"path": "crlf.txt"}, ctx)
    reg.get("Write").run({"path": "crlf.txt", "content": "p\nq\n"}, ctx)
    assert g.read_bytes() == b"p\r\nq\r\n"


def test_write_existing_requires_read(reg, ctx, workspace):
    (workspace / "e.txt").write_text("old", encoding="utf-8")
    with pytest.raises(StaleFileError):
        reg.get("Write").run({"path": "e.txt", "content": "new"}, ctx)


def test_edit_errors_are_helpful(reg, ctx, workspace):
    f = workspace / "h.py"
    f.write_text("def a():\n    x = 1\n    x = 1\n", encoding="utf-8")
    reg.get("Read").run({"path": "h.py"}, ctx)
    with pytest.raises(ToolError, match=r"appears 2 times .*lines 2, 3"):
        reg.get("Edit").run({"path": "h.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    with pytest.raises(ToolError, match="ignoring whitespace exists at line 2"):
        reg.get("Edit").run({"path": "h.py", "old_string": "\tx = 1", "new_string": "y"}, ctx)
    with pytest.raises(ToolError, match="line-number prefix"):
        reg.get("Edit").run({"path": "h.py", "old_string": "     1→def a():", "new_string": "z"}, ctx)
    res = reg.get("Edit").run({"path": "h.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, ctx)
    assert "2 occurrences" in res
    assert f.read_text(encoding="utf-8").count("x = 2") == 2


def test_edit_detects_stale_file(reg, ctx, workspace):
    f = workspace / "s.txt"
    f.write_text("one\n", encoding="utf-8")
    reg.get("Read").run({"path": "s.txt"}, ctx)
    f.write_text("changed elsewhere\n", encoding="utf-8")
    os.utime(f, ns=(1, 1))
    with pytest.raises(StaleFileError):
        reg.get("Edit").run({"path": "s.txt", "old_string": "changed", "new_string": "x"}, ctx)


def test_multiedit_atomic_and_checkpointed(reg, ctx, workspace):
    f = workspace / "m.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    reg.get("Read").run({"path": "m.txt"}, ctx)
    with pytest.raises(ToolError, match="No edits were applied"):
        reg.get("MultiEdit").run({"path": "m.txt", "edits": [
            {"old_string": "a", "new_string": "A"}, {"old_string": "zzz", "new_string": "Z"}]}, ctx)
    assert f.read_text(encoding="utf-8") == "a\nb\nc\n"
    reg.get("MultiEdit").run({"path": "m.txt", "edits": [
        {"old_string": "a", "new_string": "A"}, {"old_string": "A\nb", "new_string": "A\nB"}]}, ctx)
    assert f.read_text(encoding="utf-8") == "A\nB\nc\n"
    assert ctx.checkpoints.rewind_last()
    assert f.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_atomic_write_retries_locked_target(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(textio.os, "replace", flaky)
    monkeypatch.setattr(textio.time, "sleep", lambda s: None)
    target = tmp_path / "locked.txt"
    textio.atomic_write_text(target, "ok\n")
    assert target.read_text(encoding="utf-8") == "ok\n" and calls["n"] == 3
    assert not list(tmp_path.glob("*.axon-tmp"))


def test_atomic_write_preserves_exec_bit(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    f = tmp_path / "run.sh"
    f.write_text("echo hi\n", encoding="utf-8")
    f.chmod(0o755)
    textio.atomic_write_text(f, "echo bye\n")
    assert f.stat().st_mode & 0o111


# ─── Glob / Grep / Ls ────────────────────────────────────────────────────────

def _tree(ws: Path) -> None:
    (ws / ".gitignore").write_text("build/\n*.log\n!keep.log\n", encoding="utf-8")
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "pkg" / "mod.py").write_text("def Target():\n    return 1\n\nx = target_value\n", encoding="utf-8")
    (ws / "src" / "app.ts").write_text("export function Target() {}\n", encoding="utf-8")
    (ws / "build").mkdir()
    (ws / "build" / "out.py").write_text("Target\n", encoding="utf-8")
    (ws / "debug.log").write_text("Target\n", encoding="utf-8")
    (ws / "keep.log").write_text("Target\n", encoding="utf-8")
    (ws / "node_modules" / "lib").mkdir(parents=True)
    (ws / "node_modules" / "lib" / "x.py").write_text("Target\n", encoding="utf-8")


def test_glob_respects_gitignore_and_sorts(reg, ctx, workspace, backend):
    _tree(workspace)
    out = reg.get("Glob").run({"pattern": "**/*.py"}, ctx)
    assert "src/pkg/mod.py" in out
    assert "build/out.py" not in out and "node_modules" not in out
    assert "\\" not in out
    assert reg.get("Glob").run({"pattern": "*.py"}, ctx).startswith("No files matched")
    out = reg.get("Glob").run({"pattern": "**/*.{py,ts}", "path": "src"}, ctx)
    assert "src/app.ts" in out and "src/pkg/mod.py" in out
    logs = reg.get("Glob").run({"pattern": "*.log"}, ctx)
    assert "keep.log" in logs and "debug.log" not in logs


def test_grep_modes_and_filters(reg, ctx, workspace, backend):
    _tree(workspace)
    g = reg.get("Grep")
    content = g.run({"pattern": "Target"}, ctx)
    assert "src/pkg/mod.py:1:def Target():" in content
    assert "build/out.py" not in content and "node_modules" not in content and "debug.log" not in content
    files = g.run({"pattern": "Target", "output_mode": "files_with_matches"}, ctx)
    assert "src/app.ts" in files and "Found" in files
    count = g.run({"pattern": "target", "-i": True, "output_mode": "count", "glob": "*.py"}, ctx)
    assert "src/pkg/mod.py:2" in count
    typed = g.run({"pattern": "Target", "type": "ts"}, ctx)
    assert "src/app.ts" in typed and "mod.py" not in typed
    ctxed = g.run({"pattern": "return 1", "-B": 1, "path": "src/pkg/mod.py"}, ctx)
    assert "src/pkg/mod.py-1-def Target():" in ctxed and "src/pkg/mod.py:2:    return 1" in ctxed
    assert g.run({"pattern": "-dash-pattern"}, ctx).startswith("No matches")  # pattern starting with '-'
    limited = g.run({"pattern": "Target", "head_limit": 1}, ctx)
    assert "truncated at head_limit=1" in limited


def test_grep_invalid_regex(reg, ctx, workspace, backend):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ToolError):
        reg.get("Grep").run({"pattern": "foo(bar"}, ctx)


def test_ls_listing_and_tree(reg, ctx, workspace):
    _tree(workspace)
    ls = reg.get("Ls")
    flat = ls.run({}, ctx)
    assert "src/" in flat and "keep.log" in flat and ".gitignore" in flat
    tree = ls.run({"depth": 3, "ignore": ["*.log"]}, ctx)
    assert "mod.py" in tree and "node_modules/ (skipped)" in tree and "debug.log" not in tree
    assert "directories" in tree
    with pytest.raises(ToolError, match="not a directory"):
        ls.run({"path": "keep.log"}, ctx)


def test_glob_translation():
    assert glob_to_regex("**/*.py").match("a/b/c.py")
    assert glob_to_regex("**/*.py").match("c.py")
    assert not glob_to_regex("*.py").match("a/c.py")
    assert glob_to_regex("src/{a,b}/*.ts").match("src/b/x.ts")
    assert glob_to_regex("file[0-9].txt").match("file7.txt")


def test_workspace_files_git_and_fallback(tmp_path):
    _tree(tmp_path)
    plain = workspace_files(tmp_path)
    assert "src/pkg/mod.py" in plain and "keep.log" in plain
    assert not any(p.startswith(("build/", "node_modules/")) or p == "debug.log" for p in plain)
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "node_modules" / "lib" / "x.py").unlink()
        files = workspace_files(tmp_path, limit=100)
        assert "src/pkg/mod.py" in files and "build/out.py" not in files and "debug.log" not in files
    assert len(workspace_files(tmp_path, limit=2)) == 2
    assert all(isinstance(p, Path) for p in iter_files(tmp_path))


# ─── Patch ───────────────────────────────────────────────────────────────────

def test_patch_multi_hunk_multi_file(reg, ctx, workspace):
    a = workspace / "a.py"
    a.write_text("".join(f"line{i}\n" for i in range(1, 21)), encoding="utf-8")
    b = workspace / "b.txt"
    b.write_bytes(b"one\r\ntwo\r\n")
    old = workspace / "old.txt"
    old.write_text("bye\n", encoding="utf-8")
    for n in ("a.py", "b.txt", "old.txt"):
        reg.get("Read").run({"path": n}, ctx)
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -2,3 +2,3 @@
 line2
-line3
+LINE3
 line4
@@ -17,3 +17,4 @@
 line17
--- not a header, just a removed line starting with dashes
+line18
+inserted
 line19
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
-one
+ONE
 two
--- /dev/null
+++ b/new/created.txt
@@ -0,0 +1,2 @@
+hello
+world
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""
    a.write_text(a.read_text(encoding="utf-8").replace("line18\n", "-- not a header, just a removed line starting with dashes\n"), encoding="utf-8")
    reg.get("Read").run({"path": "a.py"}, ctx)
    res = reg.get("Patch").run({"patch": patch}, ctx)
    assert "patched a.py" in res and "created new/created.txt" in res and "deleted old.txt" in res
    text = a.read_text(encoding="utf-8")
    assert "LINE3\n" in text and "line18\ninserted\nline19" in text and "-- not a header" not in text
    assert b.read_bytes() == b"ONE\r\ntwo\r\n"
    assert (workspace / "new" / "created.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    assert not old.exists()


def test_patch_offsets_and_failure_is_atomic(reg, ctx, workspace):
    f = workspace / "c.py"
    f.write_text("header\nextra\nalpha\nbeta\ngamma\n", encoding="utf-8")
    g = workspace / "d.py"
    g.write_text("keep\n", encoding="utf-8")
    reg.get("Read").run({"path": "c.py"}, ctx)
    reg.get("Read").run({"path": "d.py"}, ctx)
    # Line numbers are off by one (stale), context still locates the hunk.
    ok = "--- a/c.py\n+++ b/c.py\n@@ -2,2 +2,2 @@\n alpha\n-beta\n+BETA\n"
    reg.get("Patch").run({"patch": ok}, ctx)
    assert f.read_text(encoding="utf-8") == "header\nextra\nalpha\nBETA\ngamma\n"
    bad = ("--- a/d.py\n+++ b/d.py\n@@ -1 +1 @@\n-keep\n+changed\n"
           "--- a/c.py\n+++ b/c.py\n@@ -1,1 +1,1 @@\n-nonexistent\n+x\n")
    reg.get("Read").run({"path": "c.py"}, ctx)
    with pytest.raises(ToolError, match="does not apply"):
        reg.get("Patch").run({"patch": bad}, ctx)
    assert g.read_text(encoding="utf-8") == "keep\n"


def test_patch_headerless_with_path(reg, ctx, workspace):
    f = workspace / "e.txt"
    f.write_text("x\ny\n", encoding="utf-8")
    reg.get("Read").run({"path": "e.txt"}, ctx)
    reg.get("ApplyPatch").run({"path": "e.txt", "patch": "@@\n x\n-y\n+z\n"}, ctx)
    assert f.read_text(encoding="utf-8") == "x\nz\n"


# ─── NotebookEdit ────────────────────────────────────────────────────────────

def test_notebook_edit_modes(reg, ctx, workspace):
    nb_path = workspace / "a.ipynb"
    tool = reg.get("NotebookEdit")
    tool.run({"notebook_path": "a.ipynb", "edit_mode": "insert", "new_source": "x = 1\ny = 2"}, ctx)
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    assert nb["cells"][0]["source"] == ["x = 1\n", "y = 2"]
    cid = nb["cells"][0]["id"]
    tool.run({"notebook_path": "a.ipynb", "cell_id": cid, "new_source": "# Title", "cell_type": "markdown"}, ctx)
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    assert nb["cells"][0]["cell_type"] == "markdown" and "outputs" not in nb["cells"][0]
    with pytest.raises(ToolError, match="out of bounds"):
        tool.run({"notebook_path": "a.ipynb", "edit_mode": "delete", "cell_index": "5"}, ctx)
    with pytest.raises(ToolError, match="integer"):
        tool.run({"notebook_path": "a.ipynb", "edit_mode": "delete", "cell_index": "abc"}, ctx)
    fresh = FileState()
    ctx2 = ToolContext(workspace=workspace, file_state=fresh, todos=TodoState(), settings=ctx.settings)
    with pytest.raises(StaleFileError):
        tool.run({"notebook_path": "a.ipynb", "edit_mode": "delete", "cell_index": 0}, ctx2)


# ─── CodeSearch ──────────────────────────────────────────────────────────────

def test_codesearch_actions(reg, ctx, workspace):
    (workspace / "auth.py").write_text(
        'MAX_RETRIES = 3\n\nclass TokenValidator:\n    """Validates JWT tokens."""\n'
        "    def validate(self, token, *, leeway=0, **opts):\n        return decode_jwt(token)\n\n"
        "def unrelated_math(x):\n    return x * 2\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dep.py").write_text("class TokenValidator: pass\n", encoding="utf-8")
    cs = reg.get("CodeSearch")
    sym = cs.run({"action": "symbols", "path": "auth.py"}, ctx)
    assert "class TokenValidator" in sym and "def validate(self, token, *, leeway, **opts)" in sym
    assert "MAX_RETRIES" in sym
    d = cs.run({"action": "definition", "query": "TokenValidator.validate"}, ctx)
    assert "auth.py:5" in d and "node_modules" not in d
    d2 = cs.run({"query": "TokenValidator"}, ctx)
    assert "[CLASS] auth.py:3" in d2 and "node_modules" not in d2
    s = cs.run({"action": "search", "query": "validate jwt token"}, ctx)
    first = s.split("\n1. ", 1)[1].split("\n", 1)[0]
    assert "TokenValidator" in first or "validate" in first
    with pytest.raises(ToolError, match="requires 'query'"):
        cs.run({"action": "search"}, ctx)


# ─── Doctor / TodoWrite ──────────────────────────────────────────────────────

def test_doctor_reports_without_leaking_keys(reg, ctx, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    out = reg.get("Doctor").run({}, ctx)
    assert "Python Version :" in out and "Platform       :" in out and "Shell Engine   :" in out
    assert "OPENAI_API_KEY" in out and "sk-super-secret-value" not in out
    assert "Ripgrep (rg)" in out and "ANSI/VT" in out
    out.encode("cp1252")  # printable on legacy Windows consoles


def test_todowrite_validation(reg, ctx):
    tool = reg.get("TodoWrite")
    with pytest.raises(ToolError, match="invalid status"):
        tool.run({"todos": [{"content": "a", "status": "done"}]}, ctx)
    with pytest.raises(ToolError, match="empty 'content'"):
        tool.run({"todos": [{"content": " ", "status": "pending"}]}, ctx)
    out = tool.run({"todos": [{"content": "a", "status": "in_progress"}]}, ctx)
    assert "Updated todos (1 items)" in out


# ─── Result summaries (UI transparency) ─────────────────────────────────────

def test_tool_summaries(reg, ctx, workspace, backend):
    _tree(workspace)
    (workspace / "f.txt").write_text("".join(f"l{i}\n" for i in range(200)), encoding="utf-8")

    def run(name, args):
        tool = reg.get(name)
        return tool.summarize(args, tool.run(args, ctx))

    assert run("Read", {"path": "f.txt"}).startswith("200 lines · ")
    assert run("Read", {"path": "f.txt", "offset": 11, "limit": 5}) .endswith("(lines 11-15)")
    assert run("Grep", {"pattern": "Target"}) == "3 matches in 3 files"
    assert run("Grep", {"pattern": "Target", "output_mode": "files_with_matches"}) == "3 files"
    assert run("Grep", {"pattern": "nomatchzzz"}) == "no matches"
    assert run("Glob", {"pattern": "**/*.py"}) == "1 file"
    assert run("Ls", {"depth": 2}).endswith("files")
    assert run("Write", {"path": "w.txt", "content": "a\nb\n"}).startswith("created · 2 lines")
    assert "1 occurrence replaced" in run("Edit", {"path": "w.txt", "old_string": "a", "new_string": "c"})
    assert run("CodeSearch", {"action": "symbols", "path": "src"}) == "2 symbols in 2 files"
    assert run("TodoWrite", {"todos": [{"content": "x", "status": "completed"}, {"content": "y", "status": "pending"}]}) == "1/2 completed"
    assert reg.get("Read").summarize({}, "File not found: x", is_error=True) == "error: File not found: x"

