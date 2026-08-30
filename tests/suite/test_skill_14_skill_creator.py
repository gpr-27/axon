"""Unit test for /skill-creator skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_skill_creator_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "skill-creator" in mgr.skills:
        skill = mgr.skills["skill-creator"]
        assert skill.name == "skill-creator"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("skill-creator")
        assert len(prompt) > 0
