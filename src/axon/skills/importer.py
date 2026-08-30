"""
Safe Skill Importer (adapted from Odysseus).
Imports SKILL.md bundles directly from GitHub repositories or raw URLs with strict path jail and size safety.
"""
from __future__ import annotations
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_SKILL_BYTES = 500_000

@dataclass
class ImportedSkill:
    name: str
    description: str
    instructions: str
    target_path: Path

def parse_github_skill_url(url: str) -> tuple[str, str, str, str]:
    """
    Parses GitHub URL into (owner, repo, ref, subpath).
    Supports formats:
      - https://github.com/owner/repo/tree/main/skills/my-skill
      - https://github.com/owner/repo/blob/main/SKILL.md
      - owner/repo
    """
    raw = url.strip()
    if not raw.startswith("http://") and not raw.startswith("https://"):
        parts = raw.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1], "main", "/".join(parts[2:])
        raise ValueError(f"Invalid repository shorthand: {url}")

    parsed = urlparse(raw)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = path_parts[0]
    repo = path_parts[1]
    ref = "main"
    subpath = ""

    if len(path_parts) >= 4 and path_parts[2] in ("tree", "blob", "raw"):
        ref = path_parts[3]
        subpath = "/".join(path_parts[4:])
    elif len(path_parts) > 2:
        subpath = "/".join(path_parts[2:])

    return owner, repo, ref, subpath

def import_skill_from_url(url_or_repo: str, workspace: Path) -> ImportedSkill:
    """
    Safely fetches a remote SKILL.md and installs it in .axon/skills/<name>/SKILL.md.
    """
    owner, repo, ref, subpath = parse_github_skill_url(url_or_repo)
    
    # Target raw URL candidate
    if subpath.endswith(".md"):
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{subpath}"
        skill_name = Path(subpath).parent.name if Path(subpath).parent.name not in ("", ".") else Path(subpath).stem
    elif subpath:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{subpath}/SKILL.md"
        skill_name = Path(subpath).name
    else:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/SKILL.md"
        skill_name = repo.lower().replace("_", "-")

    if skill_name.lower() in ("skills", "src", "main", "master", "master-branch"):
        skill_name = f"{repo.lower()}-skill"

    # Fetch content with timeout and user-agent
    req = urllib.request.Request(
        raw_url,
        headers={"User-Agent": "Axon-Skill-Importer/2.0"},
    )
    
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            content = response.read(MAX_SKILL_BYTES).decode("utf-8")
    except Exception as e:
        # Fallback to direct raw URL if user provided raw.githubusercontent
        if "raw.githubusercontent.com" in url_or_repo:
            req_direct = urllib.request.Request(url_or_repo, headers={"User-Agent": "Axon-Skill-Importer/2.0"})
            with urllib.request.urlopen(req_direct, timeout=8) as resp:
                content = resp.read(MAX_SKILL_BYTES).decode("utf-8")
        else:
            raise ConnectionError(f"Could not download SKILL.md from {raw_url}: {e}")

    # Parse description and frontmatter
    desc = f"Imported from {owner}/{repo}"
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            for line in fm.splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("name:"):
                    skill_name = line.split(":", 1)[1].strip().strip('"').strip("'")

    # Install into workspace
    clean_name = re.sub(r"[^a-zA-Z0-9\-_]", "", skill_name).lower()
    target_dir = workspace / ".axon" / "skills" / clean_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "SKILL.md"
    target_file.write_text(content, encoding="utf-8")

    return ImportedSkill(
        name=clean_name,
        description=desc,
        instructions=content,
        target_path=target_file,
    )
