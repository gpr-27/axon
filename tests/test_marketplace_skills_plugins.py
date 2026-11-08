"""Skill creator, skill install, plugin scaffolder and plugin install (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.marketplace import catalog, plugin_tools, skill_tools
from axon.marketplace.skill_tools import NewSkill


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    return home


def sample_skill(**changes) -> NewSkill:
    values = dict(name="code-reviewer", description="Reviews code for bugs.",
                  instructions="1. Read the diff.\n2. List problems.", triggers=["review", "pull request"])
    values.update(changes)
    return NewSkill(**values)


def test_create_skill_writes_valid_skill_md(private_home: Path, workspace: Path) -> None:
    path = skill_tools.create_skill(sample_skill(), workspace)
    assert path == private_home / ".axon" / "skills" / "code-reviewer" / "SKILL.md"
    assert skill_tools.validate_skill_file(path) == []
    from axon.skills.manager import parse_frontmatter
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["name"] == "code-reviewer"
    assert "review" in meta["description"] and "pull request" in meta["description"]
    assert "Read the diff" in body


def test_create_skill_is_found_by_the_skill_manager(workspace: Path) -> None:
    skill_tools.create_skill(sample_skill(), workspace, scope="project")
    from axon.skills.manager import SkillManager
    manager = SkillManager(workspace)
    manager.discover()
    assert manager.get("code-reviewer") is not None


def test_create_skill_with_scripts_in_project_scope(workspace: Path) -> None:
    skill = sample_skill(scripts={"check.sh": "echo ok\n"})
    path = skill_tools.create_skill(skill, workspace, scope="project")
    assert path.parent == workspace / ".axon" / "skills" / "code-reviewer"
    assert (path.parent / "scripts" / "check.sh").read_text(encoding="utf-8") == "echo ok\n"
    assert "scripts/check.sh" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("changes, word", [
    ({"name": "Bad Name"}, "name"),
    ({"description": "  "}, "description"),
    ({"instructions": ""}, "instructions"),
    ({"scripts": {"../evil.sh": "x"}}, "script"),
])
def test_create_skill_rejects_bad_input(workspace: Path, changes: dict, word: str) -> None:
    with pytest.raises(ValueError, match=word):
        skill_tools.create_skill(sample_skill(**changes), workspace)


def test_create_skill_does_not_overwrite_without_force(workspace: Path) -> None:
    skill_tools.create_skill(sample_skill(), workspace)
    with pytest.raises(FileExistsError):
        skill_tools.create_skill(sample_skill(), workspace)
    skill_tools.create_skill(sample_skill(description="New text."), workspace, force=True)


def test_description_quotes_cannot_break_frontmatter(workspace: Path) -> None:
    path = skill_tools.create_skill(sample_skill(description='Say "hi": always'), workspace)
    assert skill_tools.validate_skill_file(path) == []


def test_slugify() -> None:
    assert skill_tools.slugify("  My New Skill! ") == "my-new-skill"


def test_install_catalog_skill_with_mocked_download(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    text = "---\nname: frontend-design\ndescription: Design things.\n---\n\nBody text.\n"
    from test_ext_skill_bundle import make_tar
    tar = make_tar({"skills/frontend-design/SKILL.md": text.encode(), "skills/frontend-design/scripts/a.py": b"1"},
                   comment=catalog.find_entry(catalog.load_skills(), "frontend-design").sha)
    monkeypatch.setattr("axon.skills.importer._download_bytes", lambda url, limit: tar)
    entry = catalog.find_entry(catalog.load_skills(), "frontend-design")
    path = skill_tools.install_skill(entry, workspace)
    assert path.read_text(encoding="utf-8") == text
    assert (path.parent / "scripts" / "a.py").is_file()
    assert "frontend-design" in skill_tools.installed_skills(workspace)
    assert skill_tools.remove_skill("frontend-design", workspace)
    assert not skill_tools.remove_skill("frontend-design", workspace)


def test_create_plugin_scaffold_is_valid(private_home: Path, workspace: Path) -> None:
    folder = plugin_tools.create_new_plugin("My Tools", workspace)
    assert folder == private_home / ".axon" / "plugins" / "my-tools"
    manifest = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "my-tools"
    assert "my-tools" in plugin_tools.installed_plugins(workspace)
    with pytest.raises(FileExistsError):
        plugin_tools.create_new_plugin("my-tools", workspace)


def test_install_catalog_plugin_with_mocked_fetch(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    def fake_materialize(marketplace: str, entry: dict, work: Path) -> Path:
        source = work / "plugin"
        (source / "commands").mkdir(parents=True)
        (source / "commands" / "hi.md").write_text("---\ndescription: hi\n---\nSay hi.\n", encoding="utf-8")
        (source / "plugin.json").write_text(json.dumps({"name": entry["name"]}), encoding="utf-8")
        return source
    monkeypatch.setattr(plugin_tools.plugin_market, "materialize", fake_materialize)
    entry = [e for e in catalog.load_plugins() if e.kind == "git"][0]
    folder = plugin_tools.install_plugin(entry, workspace)
    assert (folder / "commands" / "hi.md").is_file()
    assert (folder / ".axon-install.json").is_file()
    assert entry.name in plugin_tools.installed_plugins(workspace)
    with pytest.raises(plugin_tools.PluginInstallError):
        plugin_tools.install_plugin(entry, workspace)
    assert plugin_tools.set_plugin_enabled(entry.name, workspace, False)
    assert plugin_tools.remove_plugin(entry.name, workspace)
