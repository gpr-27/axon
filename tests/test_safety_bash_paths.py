"""Bash allow rules and the files a command names; critical removals and path rules with Windows-style paths."""
from __future__ import annotations

import ntpath
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.config import Settings
from axon.permissions import engine as engine_mod
from axon.permissions import rules as rules_mod
from axon.permissions.engine import PermissionEngine

BASH = SimpleNamespace(name="Bash", readonly=False, default_permission="ask")
READ = SimpleNamespace(name="Read", readonly=True, default_permission="allow")


def eng(ws: Path, **rules) -> PermissionEngine:
    return PermissionEngine(Settings(workspace=ws, mode="default", api_key="k", permissions=rules))


@pytest.fixture
def linked(tmp_path: Path) -> Path:
    """A workspace holding ``link`` (a folder link) and ``key`` (a file link), both leaving the workspace."""
    if sys.platform == "win32":
        pytest.skip("creating symlinks needs elevation on Windows")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x", encoding="utf-8")
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("s", encoding="utf-8")
    (ws / "link").symlink_to(out, target_is_directory=True)
    (ws / "key").symlink_to(out / "secret.txt")
    (ws / "inner").symlink_to(ws / "src", target_is_directory=True)
    return ws


def test_read_asks_for_a_link_that_leaves_the_workspace(linked: Path) -> None:
    assert eng(linked).check(READ, {"file_path": "link/secret.txt"}, "default").outcome == "ask"


@pytest.mark.parametrize("cmd", [
    "cat link/secret.txt",
    "cat key",
    "cat src/a.py link/secret.txt",
    "cat ./link/../link/secret.txt",
    "cat src/a.py && cat key",
])
def test_bash_allow_rule_does_not_cover_a_link_that_leaves_the_workspace(linked: Path, cmd: str) -> None:
    d = eng(linked, allow=["Bash(cat *)"]).check(BASH, {"command": cmd}, "default")
    assert d.outcome == "ask", cmd
    assert "outside the workspace" in d.reason and "link" in d.reason or "key" in d.reason


def test_bash_write_command_through_a_link_asks(linked: Path) -> None:
    d = eng(linked, allow=["Bash(touch *)", "Bash(cp *)"]).check(BASH, {"command": "cp src/a.py link/copy.py"}, "default")
    assert d.outcome == "ask"


@pytest.mark.parametrize("cmd", ["cat src/a.py", "cat inner/a.py", "cat missing.txt", "cat src/*.py", "cat -n src/a.py"])
def test_bash_allow_rule_still_covers_files_inside_the_workspace(linked: Path, cmd: str) -> None:
    assert eng(linked, allow=["Bash(cat *)"]).check(BASH, {"command": cmd}, "default").outcome == "allow", cmd


def test_exact_command_rule_and_bypass_are_unchanged(linked: Path) -> None:
    assert eng(linked, allow=["Bash(cat link/secret.txt)"]).check(BASH, {"command": "cat link/secret.txt"}, "default").outcome == "allow"
    assert eng(linked, allow=["Bash(cat *)"]).check(BASH, {"command": "cat link/secret.txt"}, "bypass").outcome == "allow"


def test_link_into_an_added_directory_is_covered(linked: Path, tmp_path: Path) -> None:
    e = PermissionEngine(Settings(workspace=linked, mode="default", api_key="k", permissions={"allow": ["Bash(cat *)"]},
                                  additional_dirs=[str(tmp_path / "outside")]))
    assert e.check(BASH, {"command": "cat link/secret.txt"}, "default").outcome == "allow"


# ── Windows-style paths, simulated (these run on every platform) ─────────────

WIN_WS = r"C:\Users\me\proj"
WIN_EXACT = [r"C:\Users\me", WIN_WS, r"C:\Users", "C:\\"]


def _win_removal(cmd: str) -> str | None:
    return engine_mod._critical_removal(cmd, WIN_WS, WIN_EXACT, [r"C:\extra"], ntpath, ntpath.normpath)


@pytest.mark.parametrize("cmd", [
    r"rm -rf C:\Users\me\proj",
    r"rm -rf c:\users\ME\proj",                 # Windows paths are not case sensitive
    r'rm -rf "C:\Users\me\proj"',
    r"rm -rf C:/Users/me/proj",
    r"rm -rf C:\Users\me",
    r"Remove-Item -Recurse -Force C:\Users\me\proj",
    r"rd /s /q C:\Users\me\proj",
    r"del /s /q C:\Users\me\proj",
    r"rm -rf .",
    r"rm -rf C:\Windows",
    r"rm -rf C:\extra\*",
    r"rm -rf ..\..",
])
def test_critical_removal_recognises_windows_paths(cmd: str) -> None:
    assert _win_removal(cmd), cmd


@pytest.mark.parametrize("cmd", [r"rm -rf build", r"rm -rf C:\Users\me\proj\build", r"rd /s /q build", r"del /q notes.txt",
                                 r"rm -rf C:\extra\one"])
def test_windows_removals_below_the_workspace_are_not_critical(cmd: str) -> None:
    assert _win_removal(cmd) is None, cmd


def test_posix_form_reads_a_slash_before_a_drive_letter() -> None:
    f = rules_mod._posix_form
    assert f(r"C:\Users\o\file", windows=True) == "/c/Users/o/file"
    assert f(r"/C:\Users\o\file", windows=True) == "/c/Users/o/file"      # "/" + str(path), as a rule is often built
    assert f("/C:/Users/o/**", windows=True) == "/c/Users/o/**"
    assert f("/c/Users/o", windows=True) == "/c/Users/o"
    assert f("/C:/Users/o", windows=False) == "/C:/Users/o"               # a colon is an ordinary character elsewhere


@pytest.mark.parametrize("pattern", [r"/C:\Users\o\file", r"//C:\Users\o\file", r"C:\Users\o\file", "//c/Users/o/file",
                                     r"/C:\Users\o/**", r"//C:\Users\o\**"])
@pytest.mark.parametrize("kind", ["allow", "deny", "ask"])
def test_drive_letter_rules_match_the_windows_form_of_a_path(monkeypatch, tmp_path: Path, pattern: str, kind: str) -> None:
    monkeypatch.setattr(rules_mod, "IS_WINDOWS", True)
    target = rules_mod._posix_form(r"C:\Users\o\file")                     # what a requested path compares as
    patterns = rules_mod._rule_patterns(pattern, tmp_path, None, kind)
    assert any(rules_mod._one_pattern_matches(p, target) for p in patterns), patterns
    assert not any(rules_mod._one_pattern_matches(p, "/c/Users/other/file") for p in patterns)


def test_generated_rules_survive_windows_backslash_handling(monkeypatch, tmp_path: Path) -> None:
    assert engine_mod._escape_path("a[1].py", windows=False) == "a\\[1\\].py"
    escaped = engine_mod._escape_path("a[1]!.py", windows=True)
    assert "\\" not in escaped                                             # a backslash is a separator on Windows
    assert engine_mod._absolute_rule_pattern(r"C:\Users\o\x[1]", windows=True) == "//c/Users/o/x[[]1]"
    assert engine_mod._absolute_rule_pattern("/tmp/o/x", windows=False) == "//tmp/o/x"
    for windows in (False, True):
        monkeypatch.setattr(rules_mod, "IS_WINDOWS", windows)
        pat = "./" + engine_mod._escape_path("a[1]!.py", windows=windows)
        assert rules_mod.path_matches(pat, "a[1]!.py", tmp_path, kind="allow"), (windows, pat)
        assert not rules_mod.path_matches(pat, "a1!.py", tmp_path, kind="allow")
        assert not rules_mod.path_matches(pat, "a1.py", tmp_path, kind="allow")
