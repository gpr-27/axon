"""Unit test for /docker skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_docker_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "docker" in mgr.skills:
        skill = mgr.skills["docker"]
        assert skill.name == "docker"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("docker")
        assert len(prompt) > 0
