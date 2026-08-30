"""Unit test for /custom-workflow skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_custom_workflow_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "custom-workflow" in mgr.skills:
        skill = mgr.skills["custom-workflow"]
        assert skill.name == "custom-workflow"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("custom-workflow")
        assert len(prompt) > 0
