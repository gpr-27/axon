"""Unit test for /test-gen skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_test_gen_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "test-gen" in mgr.skills:
        skill = mgr.skills["test-gen"]
        assert skill.name == "test-gen"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("test-gen")
        assert len(prompt) > 0
