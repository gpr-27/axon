"""disableClaudeSkills: Axon stops reading ~/.claude/skills but still reads its own ~/.axon/skills."""
import json
from pathlib import Path

from axon.skills.manager import SkillManager
from axon.storage import axon_home


def _skill(folder: Path, name: str) -> None:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test skill {name}\n---\nBody\n", encoding="utf-8")


def _names(workspace: Path) -> set[str]:
    return {s.name for s in SkillManager(workspace).skills.values() if s.scope == "personal"}


def test_claude_skills_are_read_by_default_and_skipped_when_disabled(workspace: Path):
    _skill(Path.home() / ".claude" / "skills" / "from-claude", "from-claude")
    _skill(axon_home() / "skills" / "from-axon", "from-axon")
    assert _names(workspace) == {"from-claude", "from-axon"}
    (axon_home() / "settings.json").write_text(json.dumps({"disableClaudeSkills": True}), encoding="utf-8")
    assert _names(workspace) == {"from-axon"}
    assert (Path.home() / ".claude" / "skills" / "from-claude" / "SKILL.md").is_file()   # nothing is deleted
