"""Nothing is lost: activity journal, redaction, retention, full-output sidecars, trash/restore,
rewind archives and crash-safe appends."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from axon import storage
from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage
from axon.session import activity
from axon.session.ledger import Ledger
from axon.session.store import SessionStore, iter_history, iter_records
from axon.tools import create_default_registry
from fakes import FakeProvider


@pytest.fixture(autouse=True)
def _unbind():
    activity.unbind()
    yield
    activity.unbind()


def _text(t: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=t)], stop_reason="end_turn", usage=Usage(input=10, output=5))


def _tool(tid: str, name: str, args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(blocks=[ToolUseBlock(id=tid, name=name, input=args)], stop_reason="tool_use",
                         usage=Usage(input=10, output=5))


def _agent(ws: Path, turns: list[AssistantTurn] | None = None, provider: Any = None, **kw: Any) -> Agent:
    s = Settings(workspace=ws, mode=kw.pop("mode", "default"), **kw)
    a = Agent(provider=provider or FakeProvider(turns or [_text(f"answer {i}") for i in range(10)]),
              tools=create_default_registry(), permissions=PermissionEngine(s), context=ContextManager(s),
              session=SessionStore(ws), ledger=Ledger(), settings=s)
    a.start_session("startup")
    return a


def _kinds(ws: Path) -> list[str]:
    return [e["kind"] for e in activity.read(ws)]


# ─── journal ────────────────────────────────────────────────────────────────

def test_journal_location_and_format(workspace: Path, isolate_user_home: Path) -> None:
    a = _agent(workspace)
    a.run_turn("hello there")
    path = storage.activity_path(workspace)
    assert path == isolate_user_home / ".axon" / "projects" / storage.project_key(workspace) / "activity.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert set(first) >= {"ts", "t", "session_id", "kind", "data", "pid"}
    kinds = _kinds(workspace)
    assert kinds[:3] == ["session_start", "turn_start", "turn_end"]
    end = activity.read(workspace, kinds=["turn_end"])[-1]["data"]
    assert end["stop_reason"] == "end_turn" and end["iterations"] == 1 and "cost_usd" in end
    assert activity.read(workspace, kinds=["turn_start"])[-1]["session_id"] == a.session.active_session_id


def test_journal_records_every_kind_of_action(workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    (workspace / "f.txt").write_text("x\n", encoding="utf-8")
    turns = [_tool("t1", "Read", {"path": "f.txt"}), _tool("t2", "Write", {"path": "g.txt", "content": "y"}),
             _text("done"), _text("side answer")] + [_text(f"a{i}") for i in range(8)]
    a = _agent(workspace, turns)
    a.run_turn("read and write")                       # turn_start, tool, permission (no approver), turn_end
    from axon.cli import _quick_memory, _resume, _shell_into_context, process_line
    process_line(a, None, "plain prompt")              # prompt
    dispatch_command("/rename My work", a)            # command + rename
    _quick_memory(a, "# remember the parser")          # memory
    a.settings = a.settings.model_copy(update={"respond_to_bash_commands": False})
    _shell_into_context(a, None, "echo journal-shell") # shell
    from axon.commands.common import update_settings
    update_settings(a, effort="high")                  # settings
    a.set_mode("plan")                                 # mode
    item = a.message_queue.push("later please")        # queue push
    a.message_queue.remove(item.id)                    # queue remove
    monkeypatch.setattr("axon.commands.code_cmds.one_shot", lambda *a_, **k: "42")
    dispatch_command("/btw what is the answer", a)     # side_question
    from axon.commands.session_cmds import apply_rewind
    apply_rewind(a, 1, "conversation")                 # rewind
    sid = a.session.active_session_id
    a.message_queue.push("unsent at clear")
    dispatch_command("/clear", a)                      # clear + queue clear
    _resume(a, sid, lambda m: None, restore_mode=False)  # resume
    from axon.features.tips import Tip, TipEngine
    eng = TipEngine()
    eng._workspace = workspace
    eng.set_tips([Tip(text="Use /activity")], {"origin": "llm"})  # tips
    a.message_queue.push("never sent")
    activity.on_exit(a, "exit")                        # session_end with unsent queue
    kinds = set(_kinds(workspace))
    for k in ("session_start", "turn_start", "tool", "permission", "turn_end", "prompt", "command", "rename",
              "memory", "shell", "settings", "mode", "queue", "side_question", "rewind", "clear", "resume",
              "tips", "session_end"):
        assert k in kinds, k
    shell = activity.read(workspace, kinds=["shell"])[-1]["data"]
    assert shell["command"] == "echo journal-shell" and shell["exit_code"] == 0 and "journal-shell" in shell["output"]
    side = activity.read(workspace, kinds=["side_question"])[-1]["data"]
    assert side["question"] == "what is the answer" and side["answer"] == "42"
    end = activity.read(workspace, kinds=["session_end"])[-1]["data"]
    assert end["unsent_queue"][0]["text"] == "never sent"
    settings = activity.read(workspace, kinds=["settings"])[-1]["data"]["changes"]
    assert settings["effort"]["to"] == "high"
    q = [e["data"]["action"] for e in activity.read(workspace, kinds=["queue"])]
    assert {"push", "remove", "clear"} <= set(q)
    perm = activity.read(workspace, kinds=["permission"])[-1]["data"]
    assert perm["tool"] == "Write" and perm["outcome"] == "deny"
    # /btw answers are kept in the transcript too (not replayed to the model)
    assert any(r.get("type") == "side_question" for r in iter_records(a.session._file(sid)))


class _Flaky:
    name = "flaky"

    def __init__(self, fails: int) -> None:
        self.fails = fails
        self.inner = FakeProvider([_text("recovered")])

    def stream(self, **kw: Any):
        if self.fails > 0:
            self.fails -= 1
            raise ConnectionError("connection reset")
        return self.inner.stream(**kw)

    def finalize(self) -> AssistantTurn:
        return self.inner.finalize()


def test_journal_records_retries_and_errors(workspace: Path) -> None:
    a = _agent(workspace, provider=_Flaky(1))
    a.retry_backoff_s = 0.0
    a.run_turn("try again")
    retry = activity.read(workspace, kinds=["retry"])
    assert retry and "connection reset" in retry[-1]["data"]["error"]
    b = _agent(workspace, provider=_Flaky(99))
    b.retry_backoff_s = 0.0
    res = b.run_turn("will fail")
    assert res.stop_reason == "error"
    err = activity.read(workspace, kinds=["error"])
    assert err and "ConnectionError" in err[-1]["data"]["error"]
    assert activity.read(workspace, kinds=["turn_end"])[-1]["data"]["stop_reason"] == "error"
    # the failed prompt is still recorded even though it was rolled back from the history
    assert any(e["data"].get("prompt") == "will fail" for e in activity.read(workspace, kinds=["turn_start"]))


def test_unknown_and_failing_commands_are_journaled(workspace: Path) -> None:
    a = _agent(workspace)
    dispatch_command("/definitely-not-a-command", a)
    rows = activity.read(workspace, kinds=["command"])
    assert rows[-1]["data"]["status"] == "unknown"


def test_secrets_are_redacted(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SERVICE_TOKEN", "zz-very-secret-value-123456")
    data = {
        "text": "use sk-ant-api03-abcdefghijklmnopqrstuvwx and ghp_" + "a" * 36 + " please",
        "cmd": "export OPENAI_API_KEY=sk-proj-1234567890abcdefghij && curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'",
        "env": "token is zz-very-secret-value-123456",
        "api_key": "plain-key-value",
        "url": "https://user:hunter2pass@example.com/x",
        "tokens": 1234,
    }
    e = activity.record("prompt", data, workspace=workspace)
    assert e is not None
    raw = storage.activity_path(workspace).read_text(encoding="utf-8")
    for secret in ("sk-ant-api03-abcdefghijklmnopqrstuvwx", "ghp_aaaa", "sk-proj-1234567890abcdefghij",
                   "abcdefghijklmnopqrstuvwxyz", "zz-very-secret-value-123456", "plain-key-value", "hunter2pass"):
        assert secret not in raw, secret
    assert activity.REDACTED in raw and '"tokens": 1234' in raw
    assert activity.redact_text("nothing secret here") == "nothing secret here"


def test_journal_tolerates_torn_last_line_and_concurrent_appends(workspace: Path) -> None:
    activity.record("prompt", {"text": "one"}, workspace=workspace)
    path = storage.activity_path(workspace)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026", "kind": "pro')   # crash mid-write
    activity.record("prompt", {"text": "two"}, workspace=workspace)
    texts = [e["data"]["text"] for e in activity.read(workspace)]
    assert texts == ["one", "two"]
    import threading
    threads = [threading.Thread(target=lambda i=i: activity.record("queue", {"i": i}, workspace=workspace))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(activity.read(workspace, kinds=["queue"])) == 20


def test_activity_filters_and_export(workspace: Path, tmp_path: Path) -> None:
    activity.record("prompt", {"text": "alpha"}, workspace=workspace, session_id="s1")
    activity.record("command", {"name": "/status"}, workspace=workspace, session_id="s2")
    activity.record("prompt", {"text": "beta"}, workspace=workspace, session_id="s2")
    assert [e["data"]["text"] for e in activity.read(workspace, kinds=["prompt"])] == ["alpha", "beta"]
    assert len(activity.read(workspace, session="s2")) == 2
    assert [e["kind"] for e in activity.read(workspace, text="status")] == ["command"]
    assert len(activity.read(workspace, limit=1)) == 1
    other = tmp_path / "other"
    other.mkdir()
    activity.record("prompt", {"text": "elsewhere"}, workspace=other)
    every = activity.read(all_projects=True)
    assert {e["project"] for e in every} == {storage.project_key(workspace), storage.project_key(other)}
    md = activity.export(activity.read(workspace), tmp_path / "a.md", "md")
    assert "alpha" in md.read_text(encoding="utf-8") and "| Kind |" in md.read_text(encoding="utf-8")
    jl = activity.export(activity.read(workspace), tmp_path / "a.jsonl", "jsonl")
    assert len(jl.read_text(encoding="utf-8").splitlines()) == 3


def test_activity_command_and_cli(workspace: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    from axon.features.journal import activity_main, parse_activity_args
    q = parse_activity_args("50 --kind prompt,shell --session ab --since 2h --all-projects --export x.jsonl")
    assert q.limit == 50 and q.kinds == ["prompt", "shell"] and q.session == "ab" and q.all_projects
    assert q.fmt == "jsonl" and q.since and q.since < time.time()
    assert parse_activity_args("fix parser").text == "fix parser"
    assert parse_activity_args("--export x.md").limit is None
    a = _agent(workspace)
    a.run_turn("show me")
    capsys.readouterr()
    dispatch_command("/activity 10", a)
    out = capsys.readouterr().out
    assert "turn_start" in out and "show me" in out
    monkeypatch.chdir(workspace)
    assert activity_main(["--kind", "turn_start", "--export", str(tmp_path / "out.md")]) == 0
    assert "show me" in (tmp_path / "out.md").read_text(encoding="utf-8")


# ─── retention / purge ──────────────────────────────────────────────────────

def test_retention_default_keeps_everything(workspace: Path) -> None:
    assert Settings(workspace=workspace).cleanup_period_days == 0
    old = storage.sessions_dir(workspace) / "ancient.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    then = time.time() - 400 * 86400
    os.utime(old, (then, then))
    assert storage.prune_on_startup(Settings(workspace=workspace).cleanup_period_days) == 0
    assert storage.prune_old(0) == 0
    assert old.exists()
    assert storage.prune_old(30) == 1 and not old.exists()  # explicit opt-in still works


def test_purge_still_deletes_on_request(workspace: Path) -> None:
    from axon.commands.workspace_cmds import purge
    activity.record("prompt", {"text": "x"}, workspace=workspace)
    pdir = storage.project_dir(workspace)
    assert purge("project", workspace, assume_yes=True) == [pdir]
    assert not pdir.exists()


# ─── full outputs ───────────────────────────────────────────────────────────

def test_truncated_tool_output_is_saved_in_full(workspace: Path) -> None:
    big = "".join(f"line {i:06d} {'x' * 40}\n" for i in range(3000))
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    a = _agent(workspace, [_tool("t1", "Bash", {"command": "cat big.txt"}), _text("ok")],
               mode="bypass", tool_output_cap=5000)
    a.run_turn("dump it")
    recs = [r for r in iter_records(a.session.active_file) if r.get("type") == "tool_results"]
    res = recs[-1]["data"][0]
    assert len(res["content"]) < 12000
    full_path = Path(res["full_output"])
    assert full_path.is_file() and full_path.parent.parent.name == "outputs"
    full = full_path.read_text(encoding="utf-8")
    assert "line 000000" in full and "line 002999" in full and "line 001500" in full
    tool = activity.read(workspace, kinds=["tool"])[-1]["data"]
    assert tool["name"] == "Bash"


def test_shell_capture_spills_everything(workspace: Path) -> None:
    from axon.tools.shell import _Capture
    cap = _Capture(1000)
    chunks = [f"{i:05d}" + "y" * 95 for i in range(100)]
    for c in chunks:
        cap.add(c)
    assert "elided by Axon" in cap.text()
    path = cap.close_spill()
    assert path and Path(path).read_text(encoding="utf-8") == "".join(chunks)
    os.unlink(path)
    small = _Capture(1000)
    small.add("short")
    assert small.close_spill() == "" and small.text() == "short"


def test_shell_shortcut_long_output_saved(workspace: Path) -> None:
    a = _agent(workspace)
    big = "z" * 50_000
    a.add_shell_context("cat big", big, 0)
    rec = [r for r in iter_records(a.session.active_file) if r.get("type") == "shell_command"][-1]["data"]
    assert len(rec["output"]) < len(big)
    assert Path(rec["full_output"]).read_text(encoding="utf-8") == big


# ─── trash / restore ────────────────────────────────────────────────────────

def test_delete_moves_to_trash_and_restore_brings_back(workspace: Path) -> None:
    store = SessionStore(workspace)
    sid = store.active_session_id
    store.append_user("keep me")
    sub = store.session_dir / f"{sid}_sub_1.jsonl"
    sub.write_text('{"type": "user_message", "data": {"content": "sub"}}\n', encoding="utf-8")
    trace = storage.traces_dir(workspace) / f"{sid}.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    cp = storage.checkpoints_dir(workspace, sid) / "1"
    cp.mkdir(parents=True)
    (cp / "manifest.json").write_text("{}", encoding="utf-8")
    transcript = store.active_file
    original = transcript.read_bytes()

    assert store.delete_session(sid)
    assert not transcript.exists() and not sub.exists() and not trace.exists() and not cp.exists()
    trash = store.list_trash()
    assert trash and trash[0]["id"] == sid and trash[0]["files"] >= 4
    assert sid not in [m.session_id for m in store.list_recent()]
    assert any(e["kind"] == "delete" for e in activity.read(workspace))

    assert store.restore_session(sid[:20]) == sid
    assert transcript.read_bytes() == original and sub.exists() and trace.exists() and (cp / "manifest.json").exists()
    assert store.list_trash() == []
    assert sid in [m.session_id for m in store.list_recent()]
    assert any(e["kind"] == "restore" for e in activity.read(workspace))


def test_restore_command_lists_and_restores(workspace: Path, capsys) -> None:
    a = _agent(workspace)
    a.run_turn("first")
    sid = a.session.active_session_id
    dispatch_command(f"/delete {sid}", a)
    capsys.readouterr()
    dispatch_command("/restore list", a)
    assert sid in capsys.readouterr().out
    dispatch_command(f"/restore {sid}", a)
    assert "Restored" in capsys.readouterr().out
    assert a.session.exists(sid)


# ─── rewind keeps everything ────────────────────────────────────────────────

def test_rewind_keeps_removed_messages_and_file_versions(workspace: Path) -> None:
    f = workspace / "a.txt"
    turns = [_tool("w1", "Write", {"path": "a.txt", "content": "v2"}), _text("wrote"), _text("second")]
    a = _agent(workspace, turns, mode="bypass")
    a.run_turn("edit the file")
    a.run_turn("second prompt")
    assert f.read_text(encoding="utf-8") == "v2"
    from axon.commands.session_cmds import apply_rewind
    apply_rewind(a, 1, "code_and_conversation")
    assert not f.exists()  # created by the rewound turn
    assert a.conversation.messages == []
    # conversation: the transcript still has every rewound message
    texts = [r["text"] for r in iter_history(a.session.active_file)]
    assert any("edit the file" in t for t in texts) and any("second prompt" in t for t in texts)
    # code: the replaced version (v2) is archived, not destroyed
    archived = a.checkpoints.load_archived()
    assert any(snap.files.get(f.resolve()) == b"v2" for _, snap in archived)
    assert {"rewind", "rewind_code"} <= set(_kinds(workspace))


# ─── crash safety ───────────────────────────────────────────────────────────

def test_transcript_survives_torn_line(workspace: Path) -> None:
    store = SessionStore(workspace)
    store.append_user("before crash")
    with open(store.active_file, "a", encoding="utf-8") as f:
        f.write('{"type": "assistant_turn", "data": {"text": "half')  # killed mid-write
    store.append_user("after restart")
    conv = store.read_conversation(store.active_session_id)
    assert [m["content"] for m in conv.messages] == ["before crash", "after restart"]


def test_interrupted_turn_keeps_prompt_and_partial_text(workspace: Path) -> None:
    class _Interrupting(FakeProvider):
        def stream(self, **kw: Any):
            from axon.providers.base import TextDelta
            yield TextDelta("partial answ")
            raise KeyboardInterrupt

        def finalize(self) -> AssistantTurn:
            raise RuntimeError("no final turn")

    a = _agent(workspace, provider=_Interrupting([]))
    res = a.run_turn("long task")
    assert res.stop_reason == "interrupted"
    recs = list(iter_records(a.session.active_file))
    assert any(r["type"] == "user_message" and r["data"]["content"] == "long task" for r in recs)
    assert any(r["type"] == "assistant_turn" and "partial answ" in r["data"]["text"] for r in recs)
    assert activity.read(workspace, kinds=["turn_end"])[-1]["data"]["stop_reason"] == "interrupted"


def test_on_exit_saves_unsent_queue_and_draft(workspace: Path, monkeypatch) -> None:
    a = _agent(workspace)
    a.message_queue.push("queued but never run")
    import axon.ui.in_flight as inflight
    inflight._stash_type_ahead("half typed")
    activity.on_exit(a, "terminated")
    end = activity.read(workspace, kinds=["session_end"])[-1]["data"]
    assert end["reason"] == "terminated" and end["unsent_draft"] == "half typed"
    rec = [r for r in iter_records(a.session.active_file) if r.get("type") == "unsent_messages"][-1]["data"]
    assert rec["queue"][0]["text"] == "queued but never run"
