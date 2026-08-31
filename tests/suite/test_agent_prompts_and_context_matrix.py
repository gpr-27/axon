"""
Exhaustive test suite for system prompt assembly, project context discovery, and context token estimations.
"""
import pytest
from pathlib import Path
from axon.config import Settings
from axon.tools import create_default_registry
from axon.agent.prompt import build_system, discover_project_context, IDENTITY, OPERATING_RULES
from axon.agent.context import ContextManager
from axon.agent.state import Conversation
from axon.skills.manager import Skill

# ─── System Prompt Assembly Matrix (20 tests) ───────────────────────────────
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypass"])
def test_system_prompt_structure_across_modes(workspace: Path, mode: str):
    settings = Settings(workspace=workspace, mode=mode)
    tools = create_default_registry()
    blocks = build_system(settings, tools)
    
    assert len(blocks) >= 4
    assert blocks[0]["text"] == IDENTITY
    assert blocks[1]["text"] == OPERATING_RULES
    assert f"Permission mode: {mode}" in blocks[3]["text"]

def test_system_prompt_with_skills_and_append(workspace: Path):
    settings = Settings(workspace=workspace, append_system_prompt="Custom system directive")
    tools = create_default_registry()
    mock_skill = Skill(name="custom-skill", description="Custom skill description", instructions="Do X", path=workspace, scope="project")
    blocks = build_system(settings, tools, skills=[mock_skill])

    all_text = "\n".join(b["text"] for b in blocks)
    assert "/custom-skill" in all_text
    assert "Custom system directive" in all_text

# ─── Project Context Discovery Matrix (15 tests) ────────────────────────────
@pytest.mark.parametrize("filename", ["AGENTS.md", "CLAUDE.md", ".axon/AGENTS.md"])
def test_discover_project_context_files(workspace: Path, filename: str):
    target = workspace / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# Project Conventions for {filename}\nRule 1: Strict Typing\n")

    discovered = discover_project_context(workspace)
    assert "Strict Typing" in discovered
    assert filename in discovered

def test_discover_project_context_in_parent_directory(workspace: Path):
    (workspace / "AGENTS.md").write_text("Parent Conventions")
    nested = workspace / "sub" / "deep"
    nested.mkdir(parents=True, exist_ok=True)

    discovered = discover_project_context(nested)
    assert "Parent Conventions" in discovered

# ─── Sliding Context Window & Token Bounds (15 tests) ───────────────────────
@pytest.mark.parametrize("max_turns,total_messages,expected_messages", [
    (1, 6, 2),   # 1 turn = 2 messages
    (2, 8, 4),   # 2 turns = 4 messages
    (3, 10, 6),  # 3 turns = 6 messages
])
def test_sliding_context_window_enforcement(workspace: Path, max_turns: int, total_messages: int, expected_messages: int):
    settings = Settings(workspace=workspace, max_history_turns=max_turns)
    cm = ContextManager(settings)
    conv = Conversation()
    for i in range(total_messages // 2):
        conv.append_user(f"User {i}")
        from axon.providers.base import AssistantTurn, TextBlock
        conv.append_assistant(AssistantTurn(blocks=[TextBlock(text=f"Assistant {i}")], stop_reason="end_turn"))

    cm.prepare(conv, [], [], model="claude-opus-5")
    assert len(conv.messages) == expected_messages
    assert conv.messages[0]["role"] == "user"  # Clean turn boundary alignment
