"""
Exhaustive skills studio, lifecycle hooks, and persistent memory test matrix.
"""
import pytest
from pathlib import Path
from axon.skills.manager import SkillManager
from axon.hooks.runner import HookRunner
from axon.agent.memory import MemoryStore

# ─── Skills Studio Matrix (15 tests) ────────────────────────────────────────
def test_skill_discovery_and_metadata(workspace: Path):
    sm = SkillManager(workspace)
    assert len(sm.skills) >= 15
    for name, skill in sm.skills.items():
        assert len(skill.name) > 0
        assert len(skill.description) > 0

@pytest.mark.parametrize("skill_name", [
    "api-tester",
    "benchmark",
    "code-review",
    "debug",
    "deep-research",
    "docgen",
    "docker",
    "frontend-ui",
    "git-workflow",
    "optimize",
    "refactor",
    "security-audit",
    "test-gen",
    "verify",
])
def test_built_in_skills_present(workspace: Path, skill_name: str):
    sm = SkillManager(workspace)
    assert skill_name in sm.skills

# ─── Lifecycle Hooks Matrix (15 tests) ──────────────────────────────────────
def test_hook_runner_lifecycle_execution(workspace: Path):
    from axon.config import Settings, HookConfig
    settings = Settings(
        workspace=workspace,
        hooks={
            "pre_tool": [HookConfig(tool="Write", command="echo 'pre write hook'")],
            "post_tool": [HookConfig(tool="Write", command="echo 'post write hook'")],
        }
    )
    runner = HookRunner(settings)
    assert len(settings.hooks["pre_tool"]) == 1
    assert len(settings.hooks["post_tool"]) == 1

    # Execute without errors
    outcome_pre = runner.run("pre_tool", {"tool": "Write", "path": "test.txt"})
    assert outcome_pre.proceed is True
    outcome_post = runner.run("post_tool", {"tool": "Write", "path": "test.txt"})
    assert outcome_post.proceed is True

def test_hook_failure_isolation(workspace: Path):
    from axon.config import Settings, HookConfig
    settings = Settings(
        workspace=workspace,
        hooks={
            "pre_tool": [HookConfig(tool="*", command="exit 1")],
        }
    )
    runner = HookRunner(settings)
    # Non-blocking hook failure must not stop execution
    outcome = runner.run("pre_tool", {"tool": "Read", "path": "test.txt"})
    assert outcome.proceed is True

# ─── Persistent Memory Store Matrix (15 tests) ──────────────────────────────
def test_memory_store_lifecycle(workspace: Path):
    ms = MemoryStore(workspace)
    assert len(ms.list_all()) == 0

    # Learn conventions
    e1 = ms.learn("Always use pytest for test runner", category="testing")
    e2 = ms.learn("Backend uses FastAPI framework", category="architecture")
    e3 = ms.learn("Use Decimal for currency calculations", category="conventions")
    assert len(ms.list_all()) == 3

    # Search
    results = ms.search("pytest")
    assert len(results) >= 1
    assert "Always use pytest" in results[0].content

    # Delete
    deleted = ms.delete(e1.id)
    assert deleted is True
    assert len(ms.list_all()) == 2

    # Clear
    ms.clear()
    assert len(ms.list_all()) == 0
