"""
Skills for the marketplace: create a new SKILL.md, install one from GitHub, list and remove.

Skills live in ``~/.axon/skills/<name>/SKILL.md`` (user) or ``<project>/.axon/skills/<name>/SKILL.md``.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from axon.marketplace.catalog import SkillEntry
from axon.storage import axon_home

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESCRIPTION = 1024
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ORIGIN_FILE = ".axon-source.json"


@dataclass
class NewSkill:
    """Everything the skill creator asks for."""
    name: str
    description: str
    instructions: str
    triggers: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)   # file name -> file text


def skills_root(workspace: Path, scope: str) -> Path:
    """Folder that holds skills for 'user' or 'project' scope."""
    if scope == "project":
        return Path(workspace) / ".axon" / "skills"
    return axon_home() / "skills"


def slugify(text: str) -> str:
    """Turn 'My New Skill!' into 'my-new-skill'."""
    lowered = text.strip().lower()
    dashed = re.sub(r"[^a-z0-9]+", "-", lowered)
    return dashed.strip("-")[:64]


def skill_input_problems(skill: NewSkill) -> list[str]:
    """Check what the user typed before anything is written."""
    problems = []
    if not NAME_RE.match(skill.name):
        problems.append("name must be lowercase letters, digits and dashes (for example 'code-reviewer')")
    if not skill.description.strip():
        problems.append("description is required")
    if len(skill.description) > MAX_DESCRIPTION:
        problems.append(f"description is longer than {MAX_DESCRIPTION} characters")
    if not skill.instructions.strip():
        problems.append("instructions are required")
    for file_name in skill.scripts:
        if not SCRIPT_NAME_RE.match(file_name) or file_name in (".", ".."):
            problems.append(f"script name '{file_name}' is not a plain file name")
    return problems


def full_description(skill: NewSkill) -> str:
    """The description line the model reads, with the trigger phrases added."""
    text = " ".join(skill.description.split())
    if skill.triggers:
        phrases = ", ".join(f'"{t}"' for t in skill.triggers)
        text += f" Use when the user mentions: {phrases}."
    return text


def render_skill_md(skill: NewSkill) -> str:
    """Build the text of SKILL.md (frontmatter, then the instructions)."""
    description = full_description(skill).replace('"', "'")
    lines = ["---", f"name: {skill.name}", f'description: "{description}"', "---", ""]
    lines.append(f"# {skill.name.replace('-', ' ').title()}")
    lines.append("")
    lines.append(skill.instructions.strip())
    if skill.scripts:
        lines.append("")
        lines.append("## Bundled scripts")
        for file_name in sorted(skill.scripts):
            lines.append(f"- `scripts/{file_name}`")
    return "\n".join(lines) + "\n"


def validate_skill_file(path: Path) -> list[str]:
    """Check a written SKILL.md the same way Axon will read it. Returns a list of problems."""
    from axon.commands.plugins import validate_skill_file as axon_validate
    from axon.skills.manager import parse_frontmatter
    problems = list(axon_validate(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return problems + [str(e)]
    meta, body = parse_frontmatter(text)
    if not meta.get("name"):
        problems.append("frontmatter has no name")
    if not meta.get("description"):
        problems.append("frontmatter has no description")
    if not body.strip():
        problems.append("the skill body is empty")
    return problems


def create_skill(skill: NewSkill, workspace: Path, scope: str = "user", force: bool = False) -> Path:
    """Write the skill folder and return the SKILL.md path. Raises ValueError with every problem."""
    problems = skill_input_problems(skill)
    if problems:
        raise ValueError("; ".join(problems))
    folder = skills_root(workspace, scope) / skill.name
    skill_file = folder / "SKILL.md"
    if skill_file.exists() and not force:
        raise FileExistsError(f"{skill_file} already exists (use --force to replace it)")
    folder.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(render_skill_md(skill), encoding="utf-8")
    _write_scripts(folder, skill.scripts)
    problems = validate_skill_file(skill_file)
    if problems:
        raise ValueError("The skill was written but failed validation: " + "; ".join(problems))
    return skill_file


def _write_scripts(folder: Path, scripts: dict[str, str]) -> None:
    if not scripts:
        return
    script_dir = folder / "scripts"
    script_dir.mkdir(exist_ok=True)
    for file_name, text in scripts.items():
        (script_dir / file_name).write_text(text, encoding="utf-8")


def fetch_skill(entry: SkillEntry, fetch=None):
    """Download the whole skill folder of a catalog entry (pinned to its ``sha`` when it has one).
    Nothing is written; show ``bundle.listing()`` before :func:`install_skill`."""
    if entry.kind == "local":                      # a folder from a marketplace you added
        from axon.marketplace.sources import bundle_from_folder
        from axon.skills.bundle import BundleError
        try:
            return bundle_from_folder(Path(entry.source))
        except BundleError as e:
            raise ValueError(f"Skill rejected: {e}") from e
    from axon.skills.importer import fetch_skill_bundle
    return fetch_skill_bundle(entry.source, entry.sha, fetch)


def install_skill(entry: SkillEntry, workspace: Path, scope: str = "user", force: bool = False,
                  bundle=None) -> Path:
    """Install a catalog skill (SKILL.md plus its scripts, references and assets) from GitHub."""
    from axon.skills.importer import import_skill_bundle
    bundle = bundle or fetch_skill(entry)
    imported = import_skill_bundle(bundle, workspace, target_root=skills_root(workspace, scope),
                                   overwrite=force, fallback_name=entry.id)
    write_origin(imported.target_path.parent, entry.source, bundle.skill_md, bundle)
    return imported.target_path


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_origin(folder: Path, source: str, text: str, bundle=None) -> None:
    """Remember where a skill came from and what it looked like, so an update check can compare.
    With a bundle it also records the commit and a hash of every file (checked before an update)."""
    record = {"source": source, "sha256": text_hash(text), "installed_at": int(time.time())}
    if bundle is not None:
        record.update({"commit": bundle.commit, "content_hash": bundle.content_hash(), "files": len(bundle.files)})
    (folder / ORIGIN_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def read_origin(folder: Path) -> dict:
    try:
        data = json.loads((folder / ORIGIN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_skills(workspace: Path) -> dict[str, tuple[str, Path]]:
    """Skills found in the user and project folders: name -> (scope, SKILL.md path)."""
    found: dict[str, tuple[str, Path]] = {}
    for scope in ("user", "project"):
        root = skills_root(workspace, scope)
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*/SKILL.md")):
            found[skill_file.parent.name] = (scope, skill_file)
    return found


def remove_skill(name: str, workspace: Path, scope: str | None = None) -> bool:
    """Delete an installed skill folder. Returns False if it was not found."""
    scopes = [scope] if scope else ["project", "user"]
    for each in scopes:
        folder = skills_root(workspace, each) / name
        if (folder / "SKILL.md").is_file():
            shutil.rmtree(folder)
            return True
    return False


def listing_tokens(name: str, description: str, folder_skill=None) -> int:
    """Tokens the skill's listing line costs in every prompt (what /skill-doctor reports as "Listing")."""
    from axon.commands.common import estimate_tokens
    from axon.skills.manager import Skill, skills_prompt_section
    skill = folder_skill or Skill(name=name, description=description, instructions="", path=Path(name), scope="catalog")
    last = skills_prompt_section([skill]).splitlines()[-1]
    return estimate_tokens(last)


def installed_listing_tokens(workspace: Path) -> dict[str, int]:
    """Listing tokens of every skill Axon currently loads, by name (the numbers /skill-doctor shows)."""
    from axon.skills.manager import SkillManager
    try:
        manager = SkillManager(workspace)
        return {n: listing_tokens(n, "", s) for n, s in manager.skills.items()
                if not getattr(s, "disable_model_invocation", False)}
    except Exception:  # noqa: BLE001 - a broken skill must not break browsing
        return {}
