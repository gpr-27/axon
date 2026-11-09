"""A project config that writes one permission rule as a string, not a list, must still get that rule."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import Settings


@pytest.mark.parametrize("key", ["allow", "deny", "ask"])
def test_a_single_rule_written_as_a_string_is_one_rule(key: str, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (workspace / ".axon").mkdir()
    (workspace / ".axon" / "config.toml").write_text(f'[permissions]\n{key} = "Bash(rm *)"\n', encoding="utf-8")
    perms = Settings.load({"workspace": workspace, "api_key": "k"}).permissions
    rules = perms[key] if isinstance(perms, dict) else getattr(perms, key)
    assert list(rules) == ["Bash(rm *)"]
