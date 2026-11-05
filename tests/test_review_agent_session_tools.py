"""Regression tests from the code review of agent/, session/ and tools/."""
from __future__ import annotations

import glob
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from axon.agent.memory import MemoryLoader
from axon.agent.state import FileState
from axon.errors import StaleFileError
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.store import SessionStore
from axon.tools.base import ToolContext
from axon.tools.shell import BashTool
from axon.agent.state import TodoState
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


# ── session/store.py: a rolled-back prompt after a compaction is not replayed ────────────────────

def test_rolled_back_prompt_after_first_call_compaction_is_not_replayed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, session_dir=tmp_path / "sessions")
    store.open("s1")
    for i in range(3):
        store.append_user(f"p{i}")
        store.append_turn(AssistantTurn(blocks=[TextBlock(f"a{i}")], usage=Usage(input=1, output=1)))
    store.append_user("new prompt")
    # auto-compaction at the turn's first model call keeps the new prompt as the last message ...
    compacted = [{"role": "user", "content": "[Summary]"},
                 {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]},
                 {"role": "user", "content": "new prompt"}]
    store.append("compaction", {"messages": compacted, "trigger": "auto"})
    # ... and then the provider fails: the live conversation drops the prompt, so the replay must too.
    store.append("turn_failed", {"prompt": "new prompt", "resume": False, "rolled_back": True, "error": "x", "tool_calls": 0})
    msgs = store.read_conversation("s1").messages
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "new prompt" not in str(msgs)


# ── tools/shell.py: spooled output never stays behind in the temp dir ────────────────────────────

def _bash_ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=make_settings(workspace, tool_output_cap=2000))


def _spills(tmp: Path) -> list[str]:
    return glob.glob(str(tmp / "axon-bash-*.txt"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_bash_spill_file_is_not_leaked_when_a_background_child_holds_the_pipe(workspace: Path, tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    cmd = f"{shlex.quote(sys.executable)} -c \"print('x' * 6000)\"; (sleep 2 &); echo done"
    out = BashTool().run({"command": cmd, "description": "big output with a lingering child"}, _bash_ctx(workspace))
    assert "background process is still running" in out
    assert "(full output, " in out           # what was captured is saved with the session ...
    assert _spills(scratch) == []            # ... and nothing is left in the temp dir


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_bash_spill_file_is_removed_when_the_command_is_interrupted(workspace: Path, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from axon import interruptible
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def esc(proc: Any, timeout: float | None = None) -> int:
        time.sleep(1.0)            # the command has printed its output by now
        raise KeyboardInterrupt

    monkeypatch.setattr(interruptible, "wait_process", esc)
    cmd = f"{shlex.quote(sys.executable)} -c \"print('x' * 6000)\"; sleep 5"
    with pytest.raises(KeyboardInterrupt):
        BashTool().run({"command": cmd, "description": "interrupted"}, _bash_ctx(workspace))
    assert _spills(scratch) == []


# ── agent/state.py: FileState never loads a whole file into memory ───────────────────────────────

def test_file_state_hashes_in_chunks_and_still_detects_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_read_bytes(self: Path) -> bytes:
        raise AssertionError("the whole file was loaded")

    monkeypatch.setattr(Path, "read_bytes", no_read_bytes)
    f = tmp_path / "big.bin"
    f.write_bytes(b"a" * 5000)
    fs = FileState()
    fs.record_read(f)
    fs.check_writable(f)
    f.write_bytes(b"b" * 5000)                      # same size, different content
    os.utime(f, ns=(1, 2_000_000_000))              # and a different mtime
    with pytest.raises(StaleFileError):
        fs.check_writable(f)
    fs.record_read(f)
    f.write_bytes(b"b" * 5000)                      # touched, content unchanged
    os.utime(f, ns=(1, 3_000_000_000))
    fs.check_writable(f)


# ── agent/memory.py: @imports cannot pull credential stores into the prompt ──────────────────────

def test_memory_import_of_a_credential_file_is_skipped(workspace: Path, isolate_user_home: Path) -> None:
    key = isolate_user_home / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("PRIVATE-KEY-MATERIAL", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "ok.md").write_text("OK-IMPORT", encoding="utf-8")
    (workspace / "AXON.md").write_text(f"Main @{key} and @docs/ok.md", encoding="utf-8")
    loader = MemoryLoader(workspace)
    loader.load_startup(include_user=False, include_managed=False)
    text = loader.render()
    assert "OK-IMPORT" in text
    assert "PRIVATE-KEY-MATERIAL" not in text
    assert any("Skipped import" in w for w in loader.warnings)


# ── agent/images.py: only raster images are attached from "[Image: path]" ────────────────────────

def test_bracket_image_marker_does_not_attach_non_image_files(tmp_path: Path) -> None:
    from axon.agent.images import compact_image_paths, ingest_image_file
    secret = tmp_path / "credentials.txt"
    secret.write_text("aws_secret_access_key = abc", encoding="utf-8")
    text, atts = compact_image_paths(f"look at [Image: {secret}]")
    assert atts == [] and str(secret) in text
    assert ingest_image_file(str(secret)) is None


# ── agent/subagent.py: a subagent's edits are captured for /rewind ───────────────────────────────

def test_subagent_edits_are_captured_in_the_parents_checkpoints(workspace: Path) -> None:
    from axon.agent.subagent import run_subagent
    target = workspace / "made_by_sub.txt"
    prov = FakeProvider(scripted(("Write", {"path": "made_by_sub.txt", "content": "hello"}), "wrote it"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("create the file", parent)
    assert "wrote it" in out and target.read_text(encoding="utf-8") == "hello"
    assert parent.checkpoints.pending_count() == 1
    parent.checkpoints.rewind(1)
    assert not target.exists()                      # /rewind removes the file the subagent created


# ── tools/shell.py: the output budget holds however the pipe delivers the bytes ──────────────────

def test_capture_caps_a_single_oversized_chunk() -> None:
    from axon.tools.shell import _Capture
    cap = _Capture(2000)
    cap.add("é" * 5000)                 # one read() can return more than the whole budget
    text = cap.text()
    assert len(text) < 2200
    assert text.startswith("é" * 1000) and text.endswith("é" * 1000)
    assert "3000 characters elided by Axon" in text
    cap.close_spill()
    small = _Capture(2000)
    small.add("short")
    assert small.text() == "short"


# ── agent/loop.py: the saved-output path survives a ")" in a folder name ─────────────────────────

def test_full_output_path_with_parentheses_is_recorded_whole(workspace: Path) -> None:
    from axon.providers.base import ToolUseBlock
    agent = make_agent(FakeProvider([]), workspace)
    block = ToolUseBlock(id="t1", name="Bash", input={})
    path = "/Users/John (work)/.axon/projects/p/sessions/s/outputs/bash-ls.txt"
    agent._note_full_output(block, f"out\n(full output, 123,456 chars, saved to {path})")
    assert agent._full_outputs["t1"] == path


def test_message_queue_survives_concurrent_pop_and_drain():
    """pop/peek/remove/clear take the same lock as push and drain_steer, so threads cannot corrupt the list."""
    import threading
    from axon.agent.state import MessageQueue
    q = MessageQueue()
    errors: list[BaseException] = []

    def pusher():
        for i in range(300):
            q.push(f"m{i}", steer=i % 2 == 0)

    def popper():
        try:
            for _ in range(600):
                q.pop(); q.peek(); q.drain_steer()
        except BaseException as e:               # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=f) for f in (pusher, popper, popper)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    q.clear()
    assert len(q) == 0 and q.remove(1) is False and q.remove_by_index(1) is None
