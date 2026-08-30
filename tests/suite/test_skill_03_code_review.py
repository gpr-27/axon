"""Unit test for /code-review skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_code_review_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "code-review" in mgr.skills:
        skill = mgr.skills["code-review"]
        assert skill.name == "code-review"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("code-review")
        assert len(prompt) > 0
