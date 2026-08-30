"""Unit test for /git-workflow skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_git_workflow_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "git-workflow" in mgr.skills:
        skill = mgr.skills["git-workflow"]
        assert skill.name == "git-workflow"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("git-workflow")
        assert len(prompt) > 0
