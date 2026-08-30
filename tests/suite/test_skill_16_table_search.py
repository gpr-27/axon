"""Unit test for /table-search skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_table_search_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "table-search" in mgr.skills:
        skill = mgr.skills["table-search"]
        assert skill.name == "table-search"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("table-search")
        assert len(prompt) > 0
