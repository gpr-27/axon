"""/memory: scopes, listing with paths, view/edit/remove/move/search, restore, reindex, doctor, why, export/import."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.agent.loop import Agent
from axon.agent.context import ContextManager
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.features import memory_files as mf
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from fakes import FakeProvider, scripted


@pytest.fixture(autouse=True)
def _home(isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AXON_HOME", str(isolate_user_home / ".axon"))
    monkeypatch.delenv("AXON_AUTO_MEMORY_DIR", raising=False)
    monkeypatch.delenv("AXON_DISABLE_AUTO_MEMORY", raising=False)
    return isolate_user_home / ".axon"


@pytest.fixture
def agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    return Agent(provider=FakeProvider(scripted([], "ok")), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)


def run(agent: Agent, line: str, capsys) -> str:
    from axon.ui.theme import strip_ansi
    dispatch_command(line, agent)
    return strip_ansi(capsys.readouterr().out)


def test_add_in_each_scope_writes_standard_files(agent, workspace, _home, capsys) -> None:
    agent.provider = None      # no LLM: the text is saved as written
    assert "[user]" in run(agent, "/memory add --global prefer tabs", capsys)
    assert "[private]" in run(agent, "/memory add --project deploy with make", capsys)
    assert "[project]" in run(agent, "/memory add use pytest", capsys)
    assert (_home / "memory" / "MEMORY.md").is_file()
    assert (workspace / ".axon" / "memory" / "MEMORY.md").is_file()
    assert (mf.scope_dir(workspace, "project") / "MEMORY.md").is_file()
    assert {m.scope for m in mf.list_memories(workspace)} == {"global", "project", "repo"}
    assert all(m.modern for m in mf.list_memories(workspace))


def test_listing_shows_scopes_types_and_every_path(agent, workspace, _home, capsys) -> None:
    mf.write_memory(workspace, "global", name="Tabs", body="Use tabs.", type_="user")
    mf.write_memory(workspace, "project", name="Deploy", body="make deploy", type_="project")
    out = run(agent, "/memory", capsys)
    assert "Global memory" in out and "Project memory (private)" in out and "Repo memory (shared)" in out
    assert str(_home / "memory" / "tabs.md").replace(str(Path.home()), "~") in out
    assert "user" in out and "Tabs" in out and "deploy.md" in out
    only = run(agent, "/memory --scope global", capsys)
    assert "Tabs" in only and "deploy.md" not in only
    assert "deploy.md" in run(agent, "/memory --type project", capsys) and "tabs.md" not in run(agent, "/memory --type project", capsys)
    assert "Unknown memory scope" in run(agent, "/memory --scope nope", capsys)


def test_view_edit_remove_move_search_and_restore(agent, workspace, capsys) -> None:
    mf.write_memory(workspace, "project", name="Linter", body="Run ruff before commit.", type_="feedback")
    out = run(agent, "/memory view project:linter", capsys)
    assert "Run ruff before commit." in out and "feedback" in out and "linter.md" in out
    run(agent, "/memory edit project:linter run mypy too", capsys)
    m = mf.find_memory(workspace, "project:linter")
    assert m.body == "run mypy too" and m.created and m.type == "feedback"
    assert "linter.md" in run(agent, "/memory search mypy", capsys)
    assert "moved" in run(agent, "/memory move project:linter global", capsys).lower() or mf.find_memory(workspace, "global:linter")
    assert mf.find_memory(workspace, "global:linter") and not mf.find_memory(workspace, "project:linter")
    out = run(agent, "/memory remove global:linter", capsys)       # non-interactive: deletes, but keeps a copy
    assert "Deleted" in out and not mf.find_memory(workspace, "global:linter")
    listing = run(agent, "/memory restore", capsys)
    assert "linter.md" in listing and "deleted" in listing
    assert "Restored" in run(agent, "/memory restore linter", capsys)
    assert "run mypy too" in mf.find_memory(workspace, "global:linter").body
    assert "No memory matching" in run(agent, "/memory view zzz", capsys)


def test_search_covers_instruction_files_too(agent, workspace, capsys) -> None:
    (workspace / "AXON.md").write_text("Always use uv for installs.\n", encoding="utf-8")
    mf.write_memory(workspace, "global", name="Pkg", body="uv is fast")
    out = run(agent, "/memory search uv", capsys)
    assert "pkg" in out.lower() and "AXON.md:1" in out


def test_add_redacts_secret_and_says_so(agent, workspace, capsys) -> None:
    agent.provider = None
    key = "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0"
    out = run(agent, f"/memory add --global the key is {key}", capsys)
    assert "Redacted" in out and key not in out
    for p in (mf.scope_dir(workspace, "global")).iterdir():
        assert key not in p.read_text(encoding="utf-8")


def test_reindex_and_doctor_commands(agent, workspace, capsys) -> None:
    d = mf.scope_dir(workspace, "project")
    (d / "orphan.md").write_text("---\nname: Orphan\ndescription: lonely\ntype: reference\n---\n\nSee `src/missing/thing.py`.\n", encoding="utf-8")
    out = run(agent, "/memory doctor", capsys)
    assert "Not in the index" in out and "Names a file that no longer exists" in out and "src/missing/thing.py" in out
    out = run(agent, "/memory reindex", capsys)
    assert "added 1 pointer" in out
    assert "orphan.md" in (d / "MEMORY.md").read_text(encoding="utf-8")
    assert "Not in the index" not in run(agent, "/memory doctor", capsys)


def test_why_lists_loaded_files_with_tokens(agent, workspace, capsys) -> None:
    (workspace / "AXON.md").write_text("Main rules.", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("agents file", encoding="utf-8")
    mf.write_memory(workspace, "global", name="G", body="global note")
    out = run(agent, "/memory why", capsys)
    assert "Loaded" in out and "AXON.md" in out and "tok" in out and "global memory index" in out
    assert "Skipped" in out and "AGENTS.md" in out


def test_export_then_import_needs_confirmation_and_scans(agent, workspace, tmp_path, capsys) -> None:
    mf.write_memory(workspace, "global", name="Tabs", body="Use tabs.", type_="user")
    dest = tmp_path / "bundle.json"
    assert "Exported 1 memory" in run(agent, f"/memory export global {dest}", capsys)
    payload = json.loads(dest.read_text(encoding="utf-8"))
    payload["memories"].append({"file": "evil.md", "content": "---\nname: Evil\ndescription: d\ntype: user\n---\n\nkey ghp_" + "C" * 36})
    dest.write_text(json.dumps(payload), encoding="utf-8")
    out = run(agent, f"/memory import {dest} --scope project", capsys)           # not a terminal: needs --yes
    assert "Import preview" in out and "redacted" in out and "Nothing imported" in out
    assert not mf.list_memories(workspace, ["project"])
    out = run(agent, f"/memory import {dest} --scope project --yes", capsys)
    assert "Imported 2 memories" in out
    imported = {m.file: m for m in mf.list_memories(workspace, ["project"])}
    assert "ghp_" not in imported["evil.md"].body and imported["evil.md"].meta.get("source") == "imported"
    assert "No such file" in run(agent, f"/memory import {tmp_path / 'missing.json'}", capsys)


def test_clear_requires_a_scope_default_and_keeps_copies(agent, workspace, capsys) -> None:
    mf.write_memory(workspace, "repo", name="A", body="a")
    mf.write_memory(workspace, "global", name="B", body="b")
    run(agent, "/memory clear", capsys)
    assert not mf.list_memories(workspace, ["repo"]) and mf.list_memories(workspace, ["global"])
    assert any(e.original == "a.md" for e in mf.list_trash(workspace))


def test_browser_entries_group_by_scope_and_offer_actions(agent, workspace) -> None:
    from axon.commands.memory_cmds import browser_entries
    mf.write_memory(workspace, "global", name="Tabs", body="Use tabs.", type_="user")
    entries = browser_entries(agent, mf.list_memories(workspace))
    groups = [e.group for e in entries]
    assert any(g.startswith("Global memory") for g in groups) and any(g.startswith("Project memory") for g in groups)
    mem_rows = [e for e in entries if e.value[0] == "mem"]
    assert len(mem_rows) == 1 and "user" in mem_rows[0].label and "tabs.md" in mem_rows[0].hint
    assert {e.value[1] for e in entries if e.value[0] == "cmd"} >= {"why", "doctor", "reindex", "restore"}


def test_picker_move_and_delete_actions(agent, workspace, monkeypatch) -> None:
    from axon.commands import memory_cmds
    mf.write_memory(workspace, "project", name="Tabs", body="Use tabs.", type_="user")
    m = mf.find_memory(workspace, "project:tabs")
    picks = iter(["move:global"])

    class FakeSelector:
        def __init__(self, items, **kw):
            self.items = items

        def run(self):
            want = next(picks)
            return next(i for i in self.items if i.value == want)

    monkeypatch.setattr("axon.ui.picker.Selector", FakeSelector)
    memory_cmds._memory_actions(agent, m)
    assert mf.find_memory(workspace, "global:tabs") and not mf.find_memory(workspace, "project:tabs")
    picks = iter(["delete"])
    memory_cmds._memory_actions(agent, mf.find_memory(workspace, "global:tabs"))
    assert not mf.find_memory(workspace, "global:tabs") and mf.list_trash(workspace)


def test_prompt_explains_saving_compactly(workspace, _home) -> None:
    from axon.agent.prompt import env_preamble
    s = Settings(workspace=workspace, mode="default")
    text = env_preamble(s)
    for needle in ("MEMORY.md", "description", "user|feedback|project|reference", "never store secrets", str(_home / "memory")):
        assert needle in text
    assert str(mf.scope_dir(workspace, "project")) in text
    off = env_preamble(Settings(workspace=workspace, mode="default", auto_memory=False))
    assert str(mf.scope_dir(workspace, "project")) not in off and str(_home / "memory") in off
    from axon.agent.state import estimate_content_tokens
    block = text[text.index("Memory:"):].replace(str(mf.scope_dir(workspace, "project")), "P").replace(str(_home / "memory"), "G")
    assert estimate_content_tokens(block) < 140     # the prompt text itself; the two folder paths add ~20-40 more
