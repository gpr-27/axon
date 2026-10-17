"""
Regression tests for file/shell/search tool and permission-engine bugs found in review:
Patch create/rename/-U0 hunks, Patch/Glob paths seen by the permission engine, plan mode
outside the workspace, Bash env scrubbing / output cleaning / exit messages, CodeSearch on
BOM files, Edit re-encoding notes and summaries, Grep fallback on FIFOs.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.permissions import PermissionEngine
from axon.permissions.rules import patch_paths, tool_paths
from axon.tools import create_default_registry, textio
from axon.tools import search as search_mod
from axon.tools import shell as shell_mod
from axon.tools.base import ToolContext
from axon.tools.registry import ToolRegistry

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell / FIFO semantics")


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=Settings(workspace=workspace))


@pytest.fixture
def reg() -> ToolRegistry:
    return create_default_registry()


def _read(reg: ToolRegistry, ctx: ToolContext, rel: str) -> None:
    reg.get("Read").run({"path": rel}, ctx)


# ─── Patch ───────────────────────────────────────────────────────────────────

def test_patch_creation_refuses_existing_file(reg, ctx, workspace):
    f = workspace / "x.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    _read(reg, ctx, "x.txt")
    with pytest.raises(ToolError, match="already exists"):
        reg.get("Patch").run({"patch": "--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+new\n"}, ctx)
    assert f.read_text(encoding="utf-8") == "a\nb\n"  # previously "new" was silently prepended


def test_patch_rename_with_content_change(reg, ctx, workspace):
    (workspace / "old.txt").write_text("one\ntwo\n", encoding="utf-8")
    _read(reg, ctx, "old.txt")
    out = reg.get("Patch").run(
        {"patch": "--- a/old.txt\n+++ b/new.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"}, ctx)
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "one\nTWO\n"
    assert not (workspace / "old.txt").exists()
    assert "new.txt" in out and "old.txt" in out


def test_patch_zero_context_insertion_goes_after_line(reg, ctx, workspace):
    f = workspace / "z.txt"
    f.write_text("1\n2\n3\n", encoding="utf-8")
    _read(reg, ctx, "z.txt")
    reg.get("Patch").run({"patch": "--- a/z.txt\n+++ b/z.txt\n@@ -2,0 +3 @@\n+X\n"}, ctx)
    assert f.read_text(encoding="utf-8") == "1\n2\nX\n3\n"


def test_patch_headerless_hunk_with_path_modifies_file(reg, ctx, workspace):
    f = workspace / "e.txt"
    f.write_text("x\ny\n", encoding="utf-8")
    _read(reg, ctx, "e.txt")
    reg.get("Patch").run({"path": "e.txt", "patch": "@@\n x\n-y\n+z\n"}, ctx)
    assert f.read_text(encoding="utf-8") == "x\nz\n"


# ─── Permission engine: paths it must see ────────────────────────────────────

def test_patch_paths_parsed_from_headers():
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+z\n"
    assert patch_paths(diff) == ["src/a.py", "new.py"]
    assert tool_paths("Patch", {"patch": diff}) == ["src/a.py", "new.py"]
    assert tool_paths("Patch", {"patch": diff, "path": "only.py"}) == ["only.py"]


def test_patch_outside_workspace_asks_in_accept_edits(workspace):
    eng = PermissionEngine(Settings(workspace=workspace))
    patch_tool = create_default_registry().get("Patch")
    outside = {"patch": "--- /dev/null\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+x\n"}
    assert eng.check(patch_tool, outside, "acceptEdits").outcome == "ask"
    inside = {"patch": "--- a/in.txt\n+++ b/in.txt\n@@ -1 +1 @@\n-a\n+b\n"}
    assert eng.check(patch_tool, inside, "acceptEdits").outcome == "allow"


def test_edit_deny_rule_applies_to_patch(workspace):
    eng = PermissionEngine(Settings(workspace=workspace))
    eng.add_session_rule("deny", "Edit(secrets/**)")
    patch_tool = create_default_registry().get("Patch")
    d = eng.check(patch_tool, {"patch": "--- a/secrets/k.txt\n+++ b/secrets/k.txt\n@@ -1 +1 @@\n-a\n+b\n"}, "bypass")
    assert d.outcome == "deny"


def test_plan_mode_read_outside_workspace_asks(workspace, tmp_path):
    eng = PermissionEngine(Settings(workspace=workspace))
    reg = create_default_registry()
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "f.txt").write_text("x", encoding="utf-8")
    assert eng.check(reg.get("Read"), {"path": str(other / "f.txt")}, "plan").outcome == "ask"
    assert eng.check(reg.get("Read"), {"path": "inside.txt"}, "plan").outcome == "allow"


def test_glob_absolute_pattern_outside_workspace_asks(workspace, tmp_path):
    eng = PermissionEngine(Settings(workspace=workspace))
    glob = create_default_registry().get("Glob")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert eng.check(glob, {"pattern": f"{other.as_posix()}/**/*.py"}, "default").outcome == "ask"
    assert eng.check(glob, {"pattern": "../elsewhere/*"}, "default").outcome == "ask"
    assert eng.check(glob, {"pattern": "**/*.py"}, "default").outcome == "allow"


def test_always_allow_rule_for_compound_command_with_glob_chars_matches_again(workspace):
    eng = PermissionEngine(Settings(workspace=workspace))
    bash = create_default_registry().get("Bash")
    args = {"command": "ls *.py | wc -l", "description": "count"}
    assert eng.check(bash, args, "default").outcome == "ask"
    eng.add_session_rule("allow", str(eng.narrow_rule_for("Bash", args, workspace)))
    assert eng.check(bash, args, "default").outcome == "allow"
    # ...without widening to other commands
    assert eng.check(bash, {"command": "ls *.py | rm -rf x"}, "default").outcome == "ask"


# ─── Bash ────────────────────────────────────────────────────────────────────

def test_child_env_keeps_ssh_agent_and_git_identity(monkeypatch, workspace):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ada@example.com")
    monkeypatch.setenv("MY_SERVICE_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("BASIC_AUTH", "u:p")
    env = shell_mod._child_env(workspace)
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert env["GIT_AUTHOR_NAME"] == "Ada" and env["GIT_AUTHOR_EMAIL"] == "ada@example.com"
    for secret in ("MY_SERVICE_API_KEY", "GITHUB_TOKEN", "BASIC_AUTH"):
        assert secret not in env


def test_clean_output_keeps_line_ending_in_carriage_return():
    assert shell_mod._clean_output("Downloading 100%\r") == "Downloading 100%"
    assert shell_mod._clean_output("10%\r50%\r100%\ndone\n") == "100%\ndone\n"


def test_capture_streams_every_chunk_to_callback():
    got: list[str] = []
    cap = shell_mod._Capture(1000, got.append)
    cap.add("hello ")
    cap.add("x" * 2000)
    assert "".join(got) == "hello " + "x" * 2000


@posix_only
def test_bash_signal_exit_and_empty_output_are_described(ctx):
    bash = shell_mod.BashTool()
    out = bash.run({"command": "exit 3", "description": "fail"}, ctx)
    assert out.startswith("Command exited with non-zero code 3 and no output.")
    out = bash.run({"command": "kill -9 $$", "description": "die"}, ctx)
    assert "non-zero code -9 (killed by SIGKILL)" in out


@posix_only
def test_bash_saved_full_output_has_no_cwd_sentinel(workspace):
    ctx = ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                      settings=Settings(workspace=workspace, tool_output_cap=5000))
    bash = shell_mod.BashTool()
    out = bash.run({"command": f"{sys.executable} -c \"print('y' * 80000)\"", "description": "big"}, ctx)
    m = re.search(r"saved to (\S+?)\)", out)
    assert m, out[-500:]
    saved = Path(m.group(1)).read_text(encoding="utf-8")
    assert "__AXON_CWD_" not in saved and "y" * 1000 in saved


# ─── CodeSearch / Edit / Grep / textio ───────────────────────────────────────

def test_codesearch_outlines_python_file_with_bom(reg, ctx, workspace):
    (workspace / "b.py").write_bytes(b"\xef\xbb\xbfdef foo():\n    pass\n")
    out = reg.get("CodeSearch").run({"action": "symbols", "path": "b.py"}, ctx)
    assert "def foo()" in out and "Syntax error" not in out


def test_edit_reports_fallback_to_utf8(reg, ctx, workspace):
    f = workspace / "legacy.txt"
    f.write_bytes("caf\xe9 au lait\n".encode("cp1252"))
    _read(reg, ctx, "legacy.txt")
    out = reg.get("Edit").run({"path": "legacy.txt", "old_string": "lait", "new_string": "lait ☕"}, ctx)
    assert "re-encoded as UTF-8" in out
    assert f.read_bytes().decode("utf-8") == "caf\xe9 au lait ☕\n"
    # A representable edit keeps the original encoding and says nothing.
    _read(reg, ctx, "legacy.txt")
    f.write_bytes("caf\xe9\n".encode("cp1252"))
    _read(reg, ctx, "legacy.txt")
    out = reg.get("Edit").run({"path": "legacy.txt", "old_string": "caf", "new_string": "Caf"}, ctx)
    assert "re-encoded" not in out and f.read_bytes() == "Caf\xe9\n".encode("cp1252")


def test_edit_summary_with_parenthesized_path(reg, ctx, workspace):
    d = workspace / "app" / "(marketing)"
    d.mkdir(parents=True)
    (d / "page.tsx").write_text("a\n", encoding="utf-8")
    _read(reg, ctx, "app/(marketing)/page.tsx")
    edit = reg.get("Edit")
    out = edit.run({"path": "app/(marketing)/page.tsx", "old_string": "a", "new_string": "b\nc"}, ctx)
    assert edit.summarize({}, out) == "1 occurrence replaced, +1 lines"


@posix_only
def test_grep_python_fallback_skips_fifo(reg, ctx, workspace, monkeypatch):
    monkeypatch.setattr(search_mod, "find_rg", lambda: None)
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    os.mkfifo(workspace / "pipe.txt")  # reading it would block forever
    out = reg.get("Grep").run({"pattern": "needle", "output_mode": "files_with_matches"}, ctx)
    assert "a.txt" in out and "pipe.txt" not in out


def test_atomic_write_temp_names_are_unique(tmp_path, monkeypatch):
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(textio.os, "replace", spy)
    target = tmp_path / "t.txt"
    textio.atomic_write_bytes(target, b"1")
    textio.atomic_write_bytes(target, b"2")
    assert len(set(seen)) == 2 and target.read_bytes() == b"2"


@posix_only
def test_kill_shell_on_finished_shell_says_so(ctx):
    import time
    started = shell_mod.BashTool().run({"command": "true", "description": "bg", "run_in_background": True}, ctx)
    shell_id = started.split()[3]
    shell = shell_mod.BACKGROUND.get(shell_id)
    for _ in range(100):
        if shell.proc.poll() is not None:
            break
        time.sleep(0.02)
    out = shell_mod.KillShellTool().run({"shell_id": shell_id}, ctx)
    assert "already exited with code 0" in out and not out.startswith("Killed")


def test_webfetch_start_char_past_end_is_explained(reg, ctx, monkeypatch):
    import httpx
    from axon.tools import _http
    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, text="short body", headers={"content-type": "text/plain"})))
    out = reg.get("WebFetch").run({"url": "https://example.com/x", "start_char": 5000}, ctx)
    assert "past the end" in out and "10 chars" in out


def test_bash_ampersand_redirect_is_not_a_command_separator(workspace):
    from axon.permissions.rules import split_shell_commands
    assert split_shell_commands("npm test &>/dev/null") == ["npm test &>/dev/null"]
    assert split_shell_commands("a &>> log; b") == ["a &>> log", "b"]
    assert split_shell_commands("a & b") == ["a", "b"]
    eng = PermissionEngine(Settings(workspace=workspace))
    eng.add_session_rule("allow", "Bash(npm test:*)")
    bash = create_default_registry().get("Bash")
    assert eng.check(bash, {"command": "npm test &> out.log"}, "default").outcome == "allow"
