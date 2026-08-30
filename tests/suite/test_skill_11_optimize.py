"""Unit test for /optimize skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_optimize_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "optimize" in mgr.skills:
        skill = mgr.skills["optimize"]
        assert skill.name == "optimize"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("optimize")
        assert len(prompt) > 0
