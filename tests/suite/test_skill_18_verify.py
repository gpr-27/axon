"""Unit test for /verify skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_verify_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "verify" in mgr.skills:
        skill = mgr.skills["verify"]
        assert skill.name == "verify"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("verify")
        assert len(prompt) > 0
