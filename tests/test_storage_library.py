"""Tests for the library: every skill/agent/memory/subagent/hook/plan event is saved and shown."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

from axon import storage
from axon.session import activity, library


def _ws(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "proj"
    ws.mkdir()
    return ws


def test_classify_library_paths(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    home = tmp_path / "home"
    c = library.classify
    assert c(ws / ".axon/skills/demo/SKILL.md", ws) == {"type": "skill", "name": "demo", "scope": "project"}
    assert c(ws / ".claude/agents/rev.md", ws)["type"] == "agent"
    assert c(ws / ".axon/commands/git/ship.md", ws)["name"] == "git:ship"
    assert c(home / "memory/x.md", ws) == {"type": "memory", "name": "x", "scope": "global"}
    assert c(storage.project_dir(ws) / "memory/MEMORY.md", ws)["scope"] == "auto"
    assert c(ws / "AXON.md", ws)["type"] == "instructions"
    assert c(ws / ".axon/prompts/p.md", ws)["type"] == "prompt"
    assert c(ws / "src/main.py", ws) is None
    assert c(ws / ".axon/agents/notes.txt", ws) is None


def test_record_saved_keeps_one_copy_per_version(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    f = ws / ".axon/skills/demo/SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("v1")
    r1 = library.record_saved(f, workspace=ws)
    r2 = library.record_saved(f, workspace=ws)
    f.write_text("v2")
    r3 = library.record_saved(f, action="edited", workspace=ws)
    assert r1["copy"] == r2["copy"] != r3["copy"]
    assert Path(r3["copy"]).read_text() == "v2"
    assert library.record_saved(ws / "README.md", workspace=ws) is None
    kinds = [e["kind"] for e in activity.read(ws)]
    assert kinds.count("saved") == 3


def test_sync_all_finds_files_made_outside_axon(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    (ws / ".axon/agents").mkdir(parents=True)
    (ws / ".axon/agents/rev.md").write_text("---\nname: rev\n---\n")
    (ws / "AXON.md").write_text("rules")
    assert library.sync_all(ws) == 2
    assert library.sync_all(ws) == 0
    (ws / "AXON.md").write_text("rules v2")
    assert library.sync_all(ws) == 1


def test_memory_delete_keeps_last_version(tmp_path, monkeypatch):
    from axon.agent.memory import MemoryStore
    ws = _ws(tmp_path, monkeypatch)
    m = MemoryStore(ws)
    item = m.add("always use uv")
    assert m.delete(item.id)
    saved = [e["data"] for e in activity.read(ws, kinds=["saved"])]
    assert [s["action"] for s in saved] == ["created", "deleted"]
    assert "always use uv" in Path(saved[-1]["copy"]).read_text()


class _Store:
    active_session_id = "s1"

    def __init__(self):
        self.records = []

    def append(self, t, d):
        self.records.append((t, d))


def test_record_run_writes_session_and_journal(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    journal = []
    agent = SimpleNamespace(session=_Store(), settings=SimpleNamespace(workspace=ws),
                            journal=lambda k, d: journal.append((k, d)))
    library.record_run("subagent_result", {"agent_type": "Explore", "result": "ok", "api_key": "sk-ant-1234567890abcdefXYZ"},
                       agent=agent)
    assert agent.session.records[0][0] == "subagent_result"
    assert journal[0][0] == "subagent"
    assert "sk-ant" not in json.dumps(journal)


def test_collect_and_render_show_everything(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    library.record_run("subagent_result", {"agent_type": "Explore", "title": "find x", "status": "completed",
                                           "cost": 0.5, "elapsed_s": 2, "result": "found x"}, workspace=ws)
    library.record_run("skill_run", {"name": "demo", "args": "a", "source": "model"}, workspace=ws)
    library.record_run("hook_run", {"event": "PreToolUse", "blocked": True, "message": "nope"}, workspace=ws)
    inv = library.collect(ws)
    assert inv["subagents"]["runs"][0]["result"] == "found x"
    assert inv["skills"]["runs"][0]["name"] == "demo"
    assert inv["hooks"]["runs"][0]["blocked"] is True
    text = "\n".join(library.render(inv))
    assert "Explore: find x" in text and "/demo" in text and "BLOCKED" in text
    full = "\n".join(library.render(inv, "subagents"))
    assert "found x" in full
    out = io.StringIO()
    assert library.show(ws, "sub --json", out) == 0
    assert json.loads(out.getvalue())["subagents"]["runs"]
    assert library.show(ws, "nope", io.StringIO()) == 1


def test_history_shows_library_records(tmp_path, monkeypatch):
    from axon.session.store import iter_history
    from axon.ui.history_view import format_session_history
    from axon.ui.theme import strip_ansi
    ws = _ws(tmp_path, monkeypatch)
    p = tmp_path / "s.jsonl"
    rows = [{"type": "subagent_result", "timestamp": 1, "data": {"agent_type": "Explore", "title": "scan",
                                                                 "status": "completed", "cost": 0.1}},
            {"type": "skill_run", "timestamp": 2, "data": {"name": "demo", "source": "user"}},
            {"type": "saved", "timestamp": 3, "data": {"action": "created", "type": "skill", "name": "demo",
                                                       "scope": "project", "copy": "/x/copy.md"}}]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    texts = [h["text"] for h in iter_history(p)]
    assert any("subagent Explore" in t for t in texts) and any("skill /demo" in t for t in texts)
    shown = strip_ansi("\n".join(format_session_history(p, width=120)))
    assert "subagent Explore" in shown and "skill /demo" in shown and "/x/copy.md" in shown
