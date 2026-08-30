"""Unit test for /docgen skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_docgen_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "docgen" in mgr.skills:
        skill = mgr.skills["docgen"]
        assert skill.name == "docgen"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("docgen")
        assert len(prompt) > 0
