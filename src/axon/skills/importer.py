"""
Safe skill importer: installs a SKILL.md from a GitHub repository.

Safety checks:
- only github.com / raw.githubusercontent.com sources (also after redirects);
- owner / repo / ref / subpath are validated (no traversal, no odd characters);
- download size capped at MAX_SKILL_BYTES, must decode as UTF-8 text;
- the installed folder name is sanitised and never empty; existing skills are not
  overwritten unless ``overwrite=True``.
Imported skills are untrusted: their ``!`cmd``` lines only run through the
permission engine (see SkillManager.execute_skill).
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

MAX_SKILL_BYTES = 500_000
ALLOWED_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")


@dataclass
class ImportedSkill:
    name: str
    description: str
    instructions: str
    target_path: Path


def _check_part(value: str, what: str, rx: re.Pattern[str] = _NAME_RE) -> str:
    if not rx.match(value) or value in (".", "..") or ".." in value.split("/"):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def parse_github_skill_url(url: str) -> tuple[str, str, str, str]:
    """
    Parse a GitHub location into (owner, repo, ref, subpath). Accepts
      https://github.com/owner/repo[.git]
      https://github.com/owner/repo/tree/<ref>/skills/my-skill
      https://github.com/owner/repo/blob/<ref>/path/SKILL.md
      https://raw.githubusercontent.com/owner/repo/<ref>/path/SKILL.md
      owner/repo[/subpath]
    """
    raw = url.strip()
    if not raw:
        raise ValueError("Empty skill source")
    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw[len("git@github.com:"):]
    if not raw.startswith(("http://", "https://")):
        if ":" in raw or raw.startswith(("/", "\\", ".")):
            raise ValueError(f"Invalid repository shorthand: {url}")
        parts = [p for p in raw.split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"Invalid repository shorthand: {url}")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        sub = "/".join(parts[2:])
        _check_part(owner, "owner"), _check_part(repo, "repository")
        if sub:
            _check_part(sub, "path", _REF_RE)
        return owner, repo, "main", sub

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Only GitHub sources are supported (got host {host or '?'})")
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")
    owner, repo = path_parts[0], path_parts[1].removesuffix(".git")
    ref, subpath = "main", ""
    if host == "raw.githubusercontent.com":
        if len(path_parts) >= 3:
            ref = path_parts[2]
            subpath = "/".join(path_parts[3:])
    elif len(path_parts) >= 4 and path_parts[2] in ("tree", "blob", "raw"):
        ref = path_parts[3]
        subpath = "/".join(path_parts[4:])
    elif len(path_parts) > 2:
        subpath = "/".join(path_parts[2:])
    _check_part(owner, "owner")
    _check_part(repo, "repository")
    _check_part(ref, "ref", _REF_RE)
    if subpath:
        _check_part(subpath, "path", _REF_RE)
    return owner, repo, ref, subpath


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Axon-Skill-Importer/3.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - host validated
        final_host = (urlparse(resp.geturl()).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Redirected to untrusted host {final_host}")
        data = resp.read(MAX_SKILL_BYTES + 1)
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError(f"SKILL.md larger than {MAX_SKILL_BYTES // 1000} KB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("SKILL.md is not UTF-8 text") from e
    if "\x00" in text:
        raise ValueError("SKILL.md looks binary")
    return text


def sanitize_skill_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-_").lower()
    return clean[:64]


def import_skill_from_url(url_or_repo: str, workspace: Path, *, target_root: Path | None = None,
                          overwrite: bool = False) -> ImportedSkill:
    """Fetch a SKILL.md and install it into ``target_root`` (default <ws>/.axon/skills)."""
    owner, repo, ref, subpath = parse_github_skill_url(url_or_repo)
    if subpath.lower().endswith(".md"):
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{subpath}"
        parent = Path(subpath).parent.name
        skill_name = parent if parent not in ("", ".") else Path(subpath).stem
    elif subpath:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{subpath}/SKILL.md"
        skill_name = Path(subpath).name
    else:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/SKILL.md"
        skill_name = repo
    if skill_name.lower() in ("skills", "src", "main", "master"):
        skill_name = f"{repo}-skill"

    try:
        content = _download(raw_url)
    except ValueError:
        raise
    except Exception as e:
        if ref == "main" and "tree" not in url_or_repo and "blob" not in url_or_repo:
            try:
                content = _download(raw_url.replace("/main/", "/master/", 1))
            except Exception:
                raise ConnectionError(f"Could not download SKILL.md from {raw_url}: {e}") from e
        else:
            raise ConnectionError(f"Could not download SKILL.md from {raw_url}: {e}") from e

    from axon.skills.manager import parse_frontmatter
    fm, body = parse_frontmatter(content)
    if not body.strip() and not fm:
        raise ValueError("Downloaded SKILL.md is empty")
    desc = fm.get("description") if isinstance(fm.get("description"), str) else ""
    if isinstance(fm.get("name"), str) and fm["name"].strip():
        skill_name = str(fm["name"])

    clean_name = sanitize_skill_name(skill_name) or sanitize_skill_name(f"{repo}-skill") or "imported-skill"
    root = target_root or (Path(workspace) / ".axon" / "skills")
    target_dir = root / clean_name
    target_file = target_dir / "SKILL.md"
    if target_file.exists() and not overwrite:
        raise FileExistsError(f"Skill '{clean_name}' already exists at {target_file} (use --force to replace)")
    target_dir.mkdir(parents=True, exist_ok=True)
    from axon.session.store import atomic_write_text
    atomic_write_text(target_file, content)
    return ImportedSkill(
        name=clean_name,
        description=str(desc or f"Imported from {owner}/{repo}"),
        instructions=content,
        target_path=target_file,
    )
