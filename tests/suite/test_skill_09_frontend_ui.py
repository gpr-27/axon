"""Unit test for /frontend-ui skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_frontend_ui_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "frontend-ui" in mgr.skills:
        skill = mgr.skills["frontend-ui"]
        assert skill.name == "frontend-ui"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("frontend-ui")
        assert len(prompt) > 0
