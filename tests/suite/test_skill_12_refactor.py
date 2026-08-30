"""Unit test for /refactor skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_refactor_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "refactor" in mgr.skills:
        skill = mgr.skills["refactor"]
        assert skill.name == "refactor"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("refactor")
        assert len(prompt) > 0
