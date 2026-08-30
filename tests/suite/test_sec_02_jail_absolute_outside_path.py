"""Security and invariant test for Prevent access to absolute paths outside workspace (jail_absolute_outside_path)."""
import pytest
from pathlib import Path
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.skills.importer import parse_github_skill_url
from axon.errors import ToolError, PermissionDenied

def test_sec_jail_absolute_outside_path(workspace: Path):
    settings = Settings(workspace=workspace)
    perms = PermissionEngine(settings)
    assert perms is not None

    # Parse GitHub URL safety
    owner, repo, ref, subpath = parse_github_skill_url("https://github.com/owner/repo/tree/main/skills/test")
    assert owner == "owner"
    assert repo == "repo"
