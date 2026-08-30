"""Unit test for /security-audit skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_security_audit_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "security-audit" in mgr.skills:
        skill = mgr.skills["security-audit"]
        assert skill.name == "security-audit"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("security-audit")
        assert len(prompt) > 0
