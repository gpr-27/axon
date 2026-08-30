"""
Exhaustive security guards, path jail escapes, and sensitive credential protection test matrix.
"""
import pytest
from pathlib import Path
from axon.errors import PermissionDenied
from axon.permissions.paths import resolve_in_workspace, is_in_workspace
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import Rule
from axon.config import Settings
from axon.tools import create_default_registry

# ─── System Directories & Sensitive Files Matrix (25 tests) ─────────────────
@pytest.mark.parametrize("blocked_path", [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/hosts",
    "/System/Library",
    "/bin/sh",
    "/sbin/reboot",
    "/usr/bin/python3",
    "/usr/sbin/systeminfo",
    "/private/etc/sudoers",
    "/private/var/root/secret",
])
def test_system_prefix_blocked(workspace: Path, blocked_path: str):
    with pytest.raises(PermissionDenied, match="Access to system path"):
        resolve_in_workspace(workspace, blocked_path)
    assert is_in_workspace(workspace, blocked_path) is False

@pytest.mark.parametrize("sensitive_component", [
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".vault",
    "shadow",
])
def test_sensitive_credentials_component_blocked(workspace: Path, sensitive_component: str):
    bad_path = workspace / sensitive_component / "key"
    with pytest.raises(PermissionDenied, match="accesses protected component"):
        resolve_in_workspace(workspace, bad_path)

def test_dot_git_blocked_by_default(workspace: Path):
    git_path = workspace / ".git" / "config"
    with pytest.raises(PermissionDenied, match="accesses protected component '.git'"):
        resolve_in_workspace(workspace, git_path, allow_git_read=False)

def test_dot_git_allowed_with_explicit_flag(workspace: Path):
    git_path = workspace / ".git" / "HEAD"
    # When allow_git_read=True is passed, .git is accessible for reading metadata
    resolved = resolve_in_workspace(workspace, ".git/HEAD", allow_git_read=True)
    assert resolved == git_path.resolve()

# ─── Command Injection Invariants (15 tests) ────────────────────────────────
@pytest.mark.parametrize("dangerous_cmd", [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf / --no-preserve-root",
])
def test_root_filesystem_destruction_blocked_in_all_modes(workspace: Path, dangerous_cmd: str):
    settings = Settings(workspace=workspace)
    engine = PermissionEngine(settings)
    reg = create_default_registry()
    bash_tool = reg.get("Bash")

    # Invariant must hold in ALL modes, even bypass mode!
    for mode in ["default", "acceptEdits", "plan", "bypass"]:
        decision = engine.check(bash_tool, {"command": dangerous_cmd}, mode=mode)
        assert decision.outcome == "deny"
        assert "structural invariant" in decision.reason.lower()
