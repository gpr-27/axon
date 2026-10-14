"""SessionStore robustness: atomic writes, corrupt lines, encryption, scoping, checkpoints, skills importer."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.checkpoint import CheckpointManager
from axon.session.store import SessionStore, atomic_write_text


def test_atomic_write_replaces_file(tmp_path: Path):
    p = tmp_path / "d" / "f.json"
    atomic_write_text(p, "one")
    atomic_write_text(p, "two ✓")
    assert p.read_text(encoding="utf-8") == "two ✓"
    assert [x.name for x in p.parent.iterdir()] == ["f.json"]


def test_corrupt_lines_are_skipped(tmp_path: Path):
    store = SessionStore(tmp_path, session_dir=tmp_path / "s")
    store.open("s1")
    store.append_user("hello")
    with open(store.active_file, "a", encoding="utf-8") as f:
        f.write('{"type": "user_message", "data": {"content": "trunc\n')
        f.write("not json at all\n")
    store.append_turn(AssistantTurn(blocks=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(input=10, output=2)), model="claude-opus-5")
    conv = store.read_conversation("s1")
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]
    meta = store.list_recent()[0]
    assert meta.message_count == 2 and meta.total_tokens == 12 and meta.model == "claude-opus-5"


def test_encrypted_sessions_list_and_load(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXON_SESSION_PASSPHRASE", "pw")
    store = SessionStore(tmp_path, session_dir=tmp_path / "s")
    store.open("enc")
    store.append_user("secret prompt")
    assert "secret" not in store.active_file.read_text(encoding="utf-8")
    assert store.list_recent()[0].first_prompt == "secret prompt"
    assert store.read_conversation("enc").messages[0]["content"] == "secret prompt"


def test_sessions_are_scoped_per_workspace(tmp_path: Path):
    shared = tmp_path / "shared"
    a = SessionStore(tmp_path / "a", session_dir=shared)
    a.open("sa")
    a.append_user("from a")
    b = SessionStore(tmp_path / "b", session_dir=shared)
    b.open("sb")
    b.append_user("from b")
    assert [m.session_id for m in a.list_recent()] == ["sa"]
    assert b.latest() == "sb"
    assert b.delete_all_sessions() == 1
    assert (shared / "sa.jsonl").exists()


def test_default_dir_is_per_project_and_legacy_is_read(tmp_path: Path):
    ws = tmp_path / "proj"
    ws.mkdir()
    legacy = Path.home() / ".axon" / "sessions"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "old_one.jsonl").write_text('{"type": "user_message", "data": {"content": "legacy task"}}\n', encoding="utf-8")
    store = SessionStore(ws)
    assert "projects" in store.session_dir.parts
    assert "old_one" in [m.session_id for m in store.list_recent()]
    assert store.read_conversation("old_one").messages[0]["content"] == "legacy task"
    assert store.latest() is None  # unscoped legacy sessions are never auto-continued


def test_branch_keeps_structured_messages(tmp_path: Path):
    from types import SimpleNamespace
    from axon.agent.state import Conversation
    from axon.session.interactive import handle_branch
    store = SessionStore(tmp_path, session_dir=tmp_path / "s")
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
    agent = SimpleNamespace(session=store, conversation=Conversation(list(msgs)))
    handle_branch(agent, "try")
    assert store.read_conversation(store.active_session_id).messages == msgs


def test_checkpoints_are_binary_safe_and_persist(tmp_path: Path):
    ws = tmp_path / "w"
    ws.mkdir()
    f = ws / "data.bin"
    f.write_bytes(b"\xff\xfe\r\nbinary")
    cm = CheckpointManager(ws, session_id="sess1")
    cm.capture_before_edit(f)
    f.write_bytes(b"new")
    created = ws / "created.txt"
    cm.capture_before_edit(created)
    created.write_text("x", encoding="utf-8")
    cm.commit_turn_snapshot()
    cm2 = CheckpointManager(ws, session_id="sess1")  # e.g. after resume
    assert len(cm2.history) == 1
    reverted = cm2.rewind(1)
    assert f.read_bytes() == b"\xff\xfe\r\nbinary"
    assert not created.exists()
    assert len(reverted) == 2
    assert CheckpointManager(ws, session_id="sess1").history == []


@pytest.mark.parametrize("bad", ["https://evil.com/a/b", "owner/../x", "../../etc", "https://github.com/a/b/tree/main/../../x"])
def test_importer_rejects_unsafe_sources(bad: str):
    from axon.skills.importer import parse_github_skill_url
    with pytest.raises(ValueError):
        parse_github_skill_url(bad)


def test_skill_content_and_frontmatter(tmp_path: Path):
    from axon.skills.manager import skill_content, skills_prompt_section, SkillManager
    d = tmp_path / ".axon" / "skills" / "lint"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: lint\ndescription: >-\n  Lint the\n  code\nallowed-tools:\n  - Bash(ruff:*)\n---\nRun ruff on $1 then !`echo nope`\n", encoding="utf-8")
    text = skill_content("lint", tmp_path, "src")
    assert "Run ruff on src" in text and "not permitted" in text
    mgr = SkillManager(tmp_path)
    assert mgr.skills["lint"].description == "Lint the code"
    assert mgr.skills["lint"].allowed_tools == ["Bash(ruff:*)"]
    assert "- lint: Lint the code" in skills_prompt_section(list(mgr.skills.values()))
    assert "Unknown skill" in skill_content("nope", tmp_path)
