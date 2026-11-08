"""
Marketplaces you added yourself (``/marketplace add <git url | owner/repo | path>``) shown next to the built-in
catalog: their plugins and their skills.

A source may be a Claude-Code-style plugin marketplace (``.claude-plugin/marketplace.json``) or just a folder or
repository of skills (``SKILL.md`` folders, no marketplace file). Entries from here are never ``verified``: that
mark is reserved for the official Anthropic marketplace and skills repository (see docs/marketplace.md).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from axon.commands import marketplace as mk
from axon.marketplace.catalog import PluginEntry, SkillEntry
from axon.skills import bundle as bundle_mod


def looks_like_source(target: str) -> bool:
    """Does ``/marketplace add <target>`` mean a plugin / skill marketplace rather than an MCP connector?"""
    t = target.strip()
    if not t or " " in t:
        return False
    if t.startswith(("git@", "ssh://")) or re.match(r"^[\w.-]+@[\w.-]+:", t):
        return True
    if t.lower().startswith(("http://", "https://")):
        base = t.split("?")[0].rstrip("/")
        if base.lower().endswith((".json", ".git")):
            return True
        m = re.match(r"https?://(?:www\.)?(?:github|gitlab)\.com/[^/]+/[^/]+$", base, re.I)
        return bool(m)
    p = Path(t).expanduser()
    if t.startswith(("./", "../", "/", "~")) or p.exists():
        return p.exists()
    return bool(re.fullmatch(r"[\w.-]+/[\w.-]+", t))


def _desc_of(skill_dir: Path) -> str:
    from axon.skills.manager import parse_frontmatter
    try:
        fm, _ = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        return ""
    d = " ".join(str(fm.get("description") or "").split())
    return d[:240]


def user_skill_entries(taken: set[str] = frozenset()) -> list[SkillEntry]:
    out: list[SkillEntry] = []
    for name, state in mk.list_marketplaces():
        root = mk.marketplace_dir(name, state)
        for d in mk.find_skill_dirs(root):
            sid = d.name if d != root else name
            if sid in taken or any(e.id == sid for e in out):
                sid = f"{sid}@{name}"
            out.append(SkillEntry(id=sid, name=d.name, publisher=name, description=_desc_of(d) or "(no description)",
                                  category="Custom", kind="local", source=str(d), verified=False,
                                  note=f"from the marketplace you added: {name}"))
    return out


def user_plugin_entries(taken: set[str] = frozenset()) -> list[PluginEntry]:
    out: list[PluginEntry] = []
    for name, state in mk.list_marketplaces():
        if state.get("skillsOnly"):
            continue
        try:
            entries = mk.marketplace_plugins(name)
        except mk.MarketplaceError:
            continue
        for e in entries:
            pname = str(e.get("name") or "")
            if not pname:
                continue
            pid = f"{pname}@{name}" if pname in taken or any(x.id == pname for x in out) else pname
            out.append(PluginEntry(id=pid, name=pname, publisher=name,
                                   description=" ".join(str(e.get("description") or "").split())[:300] or "(no description)",
                                   category=str(e.get("category") or "Custom").capitalize(), kind="marketplace",
                                   source={"marketplace": name}, docs_url=str(e.get("homepage") or ""), verified=False))
    return out


def user_entries(tab: str, taken: set[str] = frozenset()) -> list:
    if tab == "skills":
        return user_skill_entries(taken)
    if tab == "plugins":
        return user_plugin_entries(taken)
    return []


def bundle_from_folder(folder: Path, source: str = ""):
    """A skill folder on disk (a marketplace you added) as a bundle, with the same checks as a download:
    no symlinks that leave the folder, the file-count and size caps, a SKILL.md at the top."""
    folder = Path(folder).resolve()
    b = bundle_mod.SkillBundle(source=source or str(folder))
    total = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            p = Path(root) / n
            rel = p.relative_to(folder).as_posix()
            if p.is_symlink():
                target = p.resolve()
                if folder not in target.parents and target != folder:
                    raise bundle_mod.BundleError(f"symlink {rel!r} points outside the skill folder")
                b.skipped.append(rel)
                continue
            size = p.stat().st_size
            total += size
            if size > bundle_mod.MAX_FILE_BYTES or total > bundle_mod.MAX_TOTAL_BYTES or len(b.files) >= bundle_mod.MAX_FILES:
                raise bundle_mod.BundleError("skill folder is over the size or file-count limit")
            b.files[rel] = p.read_bytes()
            b.modes[rel] = p.stat().st_mode & 0o777
    if "SKILL.md" not in b.files:
        raise bundle_mod.BundleError("no SKILL.md at the top of that folder")
    try:
        b.commit = mk.head_sha(next(p for p in [folder, *folder.parents] if (p / ".git").exists()))
    except StopIteration:
        b.commit = ""
    return b
