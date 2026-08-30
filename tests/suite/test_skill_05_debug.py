"""Unit test for /debug skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_debug_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "debug" in mgr.skills:
        skill = mgr.skills["debug"]
        assert skill.name == "debug"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("debug")
        assert len(prompt) > 0
