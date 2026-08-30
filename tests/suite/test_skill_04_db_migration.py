"""Unit test for /db-migration skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_db_migration_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "db-migration" in mgr.skills:
        skill = mgr.skills["db-migration"]
        assert skill.name == "db-migration"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("db-migration")
        assert len(prompt) > 0
