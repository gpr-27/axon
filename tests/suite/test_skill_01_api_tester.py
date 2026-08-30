"""Unit test for /api-tester skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_api_tester_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "api-tester" in mgr.skills:
        skill = mgr.skills["api-tester"]
        assert skill.name == "api-tester"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("api-tester")
        assert len(prompt) > 0
