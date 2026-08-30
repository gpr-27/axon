"""Unit test for /subagent-fanout skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_subagent_fanout_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "subagent-fanout" in mgr.skills:
        skill = mgr.skills["subagent-fanout"]
        assert skill.name == "subagent-fanout"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("subagent-fanout")
        assert len(prompt) > 0
