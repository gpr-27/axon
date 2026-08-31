"""Security and invariant test for Switching provider model endpoints (config_model_switching)."""
import pytest
from pathlib import Path
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.skills.importer import parse_github_skill_url
from axon.errors import ToolError, PermissionDenied

def test_sec_config_model_switching(workspace: Path):
    settings = Settings(workspace=workspace)
    perms = PermissionEngine(settings)
    assert perms is not None
    # Default model is deepseek-v4-flash
    assert settings.model == "deepseek-v4-flash"

    # Verify that model configuration allows switching to any chosen model
    gpt_settings_1 = Settings(workspace=workspace, model="gpt-4o")
    assert gpt_settings_1.model == "gpt-4o"

    gpt_settings_2 = Settings(workspace=workspace, model="gpt-5.6-sol")
    assert gpt_settings_2.model == "gpt-5.6-sol"

    gpt_settings_3 = Settings(workspace=workspace, model="o3-mini")
    assert gpt_settings_3.model == "o3-mini"

    claude_settings = Settings(workspace=workspace, model="claude-opus-5")
    assert claude_settings.model == "claude-opus-5"

    # Empty model defaults to deepseek-v4-flash
    empty_settings = Settings(workspace=workspace, model="")
    assert empty_settings.model == "deepseek-v4-flash"

    # Parse GitHub URL safety
    owner, repo, ref, subpath = parse_github_skill_url("https://github.com/owner/repo/tree/main/skills/test")
    assert owner == "owner"
    assert repo == "repo"
