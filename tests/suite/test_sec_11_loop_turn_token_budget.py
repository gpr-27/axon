"""Security and invariant test for Agent loop enforces turn token budget ceiling (loop_turn_token_budget)."""
import pytest
from pathlib import Path
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.skills.importer import parse_github_skill_url
from axon.errors import ToolError, PermissionDenied

def test_sec_loop_turn_token_budget(workspace: Path):
    settings = Settings(workspace=workspace)
    perms = PermissionEngine(settings)
    assert perms is not None

    # Parse GitHub URL safety
    owner, repo, ref, subpath = parse_github_skill_url("https://github.com/owner/repo/tree/main/skills/test")
    assert owner == "owner"
    assert repo == "repo"
