"""Unit test for /benchmark skill."""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager

def test_skill_benchmark_registration(workspace: Path):
    mgr = SkillManager(workspace)
    assert mgr.skills is not None
    if "benchmark" in mgr.skills:
        skill = mgr.skills["benchmark"]
        assert skill.name == "benchmark"
        assert len(skill.instructions) > 0
        prompt = mgr.execute_skill("benchmark")
        assert len(prompt) > 0
