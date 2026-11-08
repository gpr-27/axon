"""The shell guards must see through the ways a command can name a credential or hide a payload."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.permissions.engine import catastrophic_command, shell_credential_access
from axon.permissions.paths import credential_location, is_protected_path, resolve_in_workspace
from axon.tools.shell import _is_secret_env


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / ".axon").mkdir(parents=True)
    (h / ".axon" / ".env").write_text("BZN_API_KEY=x\n", encoding="utf-8")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("AXON_HOME", str(h / ".axon"))
    return h


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    w.mkdir()
    return w


@pytest.mark.parametrize("cmd", [
    "cat ~/.axon/.e*",
    "cat ~/.ax*/.env",
    "cat ~/.axon/.{env,foo}",
    "cd ~/.axon && cat .env",
    "cd ~/.axon; cat ./.env",
    "(cd ~/.axon && cat .env)",
    "cd ~ && cat .axon/.env",
    "cd ~/.axon && cat .e*",
    "grep -r KEY ~/.axon",
    "grep -rn KEY ~",
    "cd ~/.axon && grep -r KEY .",
    "cd ~/.axon && rg --hidden KEY",
    "rg --hidden KEY ~/.axon",
    "tar czf /tmp/x.tgz ~/.axon",
    "cp -r ~/.axon /tmp/x",
    "bash -c 'cat ~/.axon/.env'",
    "sh -lc 'cd ~/.axon && cat .env'",
    "eval 'cat ~/.axon/.env'",
    "D=~/.axon; cat $D/.env",
    "cat $(echo ~)/.axon/.env",
    "cat<~/.axon/.env",
    "cat ~nosuchuser/.ssh/id_rsa",
])
def test_credential_bypasses_are_caught(home: Path, ws: Path, cmd: str):
    assert shell_credential_access(cmd, ws), cmd


@pytest.mark.parametrize("cmd", [
    "cat .env",                       # a project's own .env is not the provider key store
    "grep -r TODO .",
    "grep -r TODO src",
    "rg TODO ~",                      # ripgrep skips hidden directories unless told otherwise
    "ls -la ~/.axon",
    "cat ~/.axon/*.md",
    "cd src && cat ./main.py",
    "cp -r src /tmp/x",
    "echo 'cat ~/.axon/.env is blocked'",
    "D=src; cat $D/main.py",
    "cat ~nosuchuser/notes.txt",
])
def test_ordinary_commands_are_not_blocked(home: Path, ws: Path, cmd: str):
    assert shell_credential_access(cmd, ws) is None, cmd


@pytest.mark.parametrize("cmd", [
    "bash -c 'rm -rf /'",
    "sh -c \"rm -rf ~\"",
    "eval 'rm -rf /'",
    "echo cm0gLXJmIC8K | base64 -d | sh",
    "echo cm0gLXJmIC8K | base64 --decode | sudo bash",
    'eval "$(echo cm0gLXJmIC8K | base64 -d)"',
    'bash -c "$(echo cm0gLXJmIC8K | base64 -d)"',
])
def test_hidden_destructive_commands_are_caught(cmd: str):
    assert catastrophic_command(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "bash -c 'rm -rf build'",
    "bash scripts/test.sh",
    "base64 -d blob.txt > out.bin",
    "echo aGk= | base64 -d",
    "eval \"$(ssh-agent -s)\"",
])
def test_ordinary_shell_use_is_not_catastrophic(cmd: str):
    assert catastrophic_command(cmd) is None, cmd


@pytest.mark.parametrize("raw", ["~nosuchuser/x", "~nosuchuser"])
def test_unknown_user_home_never_raises(tmp_path: Path, raw: str):
    credential_location(raw, tmp_path)
    is_protected_path(raw, tmp_path)
    resolve_in_workspace(tmp_path, raw)


@pytest.mark.parametrize("name", ["BZN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"])
def test_provider_keys_never_reach_shell_children(name: str):
    assert _is_secret_env(name)
