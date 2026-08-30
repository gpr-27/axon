"""Unit test for /deep-research skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_deep_research_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "deep-research" in mgr.skills:
        skill = mgr.skills["deep-research"]
        assert skill.name == "deep-research"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("deep-research")
        assert len(prompt) > 0
