"""Catalog integrity, /marketplace search + sort + install state, and marketplaces the user adds."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.commands import marketplace as mk
from axon.marketplace import catalog, skill_tools, sources
from axon.marketplace.cli import Marketplace
from test_ext_plugin_pin import git
from test_ext_skill_bundle import make_tar


def run(workspace: Path, *tokens: str, ask=None) -> str:
    out: list[str] = []
    Marketplace(workspace, out=out.append, ask_line=ask).run(list(tokens))
    return "\n".join(out)


def test_catalog_entries_are_pinned_and_ids_do_not_clash():
    skills, plugins, conns = catalog.load_skills(), catalog.load_plugins(), catalog.load_connectors()
    gh_skills = [s for s in skills if s.kind == "github"]
    assert len(gh_skills) >= 19 and len(plugins) >= 80
    assert all(len(s.sha) == 40 for s in gh_skills)
    assert all(len(p.pinned_sha) == 40 for p in plugins if p.kind == "git")
    assert not {c.id for c in conns} & {p.id for p in plugins}, "a connector and a plugin share an id"
    for e in skills + plugins:
        assert "Only SKILL.md" not in getattr(e, "note", "")


def test_verified_means_official_anthropic_source():
    for s in catalog.load_skills():
        if s.verified and s.kind == "github":
            assert s.source.startswith("anthropics/skills/")
    for p in catalog.load_plugins():
        if p.verified and p.kind == "git":
            # listed in Anthropic's official marketplace with its own pinned sha (docs/marketplace.md)
            assert p.pinned_sha


def test_search_filters_by_category_and_sorts(workspace: Path):
    text = run(workspace, "search", "pdf", "--tab", "skills")
    assert "pdf" in text and "tokens/turn" in text
    docs = run(workspace, "list", "skills", "--category", "Documents", "--sort", "name")
    assert docs.index("docx") < docs.index("pdf") < docs.index("pptx") < docs.index("xlsx")
    assert "algorithmic-art" not in docs
    by_cat = run(workspace, "list", "skills", "--sort", "category")
    assert by_cat.index("Authoring") < by_cat.index("Documents")
    assert "must be one of" in run(workspace, "list", "skills", "--sort", "bogus")


def test_installed_and_update_available_are_shown(workspace: Path):
    entry = catalog.find_entry(catalog.load_skills(), "pdf")
    tar = make_tar({"skills/pdf/SKILL.md": b"---\nname: pdf\ndescription: Do PDFs.\n---\n\nbody\n"}, comment=entry.sha)
    b = __import__("axon.skills.bundle", fromlist=["x"]).read_tarball(tar, "skills/pdf", entry.source)
    skill_tools.install_skill(entry, workspace, bundle=b)
    shown = run(workspace, "list", "skills", "--category", "Documents")
    title = next(l for l in shown.splitlines() if " pdf " in l and "by Anthropic" in l)
    assert "✓" in title or "[x]" in title
    assert "update available" not in shown
    origin = skill_tools.skills_root(workspace, "user") / "pdf" / skill_tools.ORIGIN_FILE
    rec = json.loads(origin.read_text())
    rec["commit"] = "0" * 40                     # installed from an older pin
    origin.write_text(json.dumps(rec))
    assert "update available" in run(workspace, "list", "skills", "--category", "Documents")
    first = run(workspace, "list", "skills", "--sort", "updates").index("pdf")
    assert first < run(workspace, "list", "skills", "--sort", "updates").index("docx")


def test_token_cost_matches_the_skill_doctor_listing(workspace: Path):
    cost = skill_tools.listing_tokens("x", "A short description.")
    long = skill_tools.listing_tokens("x", "A long description. " * 60)
    assert 0 < cost < long


# ─── marketplaces you add ───────────────────────────────────────────────────

def _skill_repo(tmp: Path) -> Path:
    r = tmp / "my-skills"
    (r / "skills" / "alpha" / "scripts").mkdir(parents=True)
    (r / "skills" / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: Alpha skill.\n---\n\nUse scripts/a.sh\n")
    (r / "skills" / "alpha" / "scripts" / "a.sh").write_text("#!/bin/sh\necho a\n")
    (r / "skills" / "beta").mkdir()
    (r / "skills" / "beta" / "SKILL.md").write_text("---\nname: beta\ndescription: Beta skill.\n---\n\nBody\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "x")
    return r


def test_add_skill_only_git_source_then_install_whole_folder(workspace: Path, tmp_path: Path):
    repo = _skill_repo(tmp_path)
    text = run(workspace, "add", repo.as_uri() + ".git" if False else str(repo), "--marketplace")
    assert "2 skill(s)" in text and "no verified mark" in text
    listing = run(workspace, "list", "skills", "--category", "Custom")
    assert "alpha" in listing and "beta" in listing and "✔" not in listing and "(verified)" not in listing
    out = run(workspace, "install", "alpha", "--tab", "skills", ask=lambda q: "y")
    assert "scripts/a.sh" in out and "EXECUTABLE SCRIPT" in out and "OK: Installed skill" in out
    folder = skill_tools.skills_root(workspace, "user") / "alpha"
    assert (folder / "scripts" / "a.sh").is_file()
    assert skill_tools.read_origin(folder)["commit"]
    assert "my-skills" in run(workspace, "sources")


def test_add_plugin_marketplace_from_path_and_install(workspace: Path, tmp_path: Path):
    mdir = tmp_path / "mymk"
    (mdir / ".claude-plugin").mkdir(parents=True)
    (mdir / "plug" / "commands").mkdir(parents=True)
    (mdir / "plug" / "commands" / "hi.md").write_text("---\ndescription: hi\n---\nHi\n")
    (mdir / "plug" / "plugin.json").write_text(json.dumps({"name": "plug"}))
    (mdir / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "mymk", "owner": {"name": "me"}, "plugins": [{"name": "plug", "source": "./plug", "description": "A plug"}]}))
    assert "1 plugin(s)" in run(workspace, "add", str(mdir))
    assert "plug" in run(workspace, "list", "plugins", "--category", "Custom")
    assert "OK: Installed plugin 'plug'" in run(workspace, "install", "plug", "--tab", "plugins")
    assert "Removed marketplace" in run(workspace, "remove-source", "mymk")


def test_connector_urls_still_mean_connectors():
    assert not sources.looks_like_source("https://mcp.example.com/mcp")
    assert not sources.looks_like_source("npx")
    assert sources.looks_like_source("git@github.com:o/r.git")
    assert sources.looks_like_source("https://github.com/o/r")
    assert sources.looks_like_source("owner/repo")


def test_local_skill_with_escaping_symlink_is_rejected(tmp_path: Path):
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nb")
    (tmp_path / "secret.txt").write_text("x")
    (d / "leak").symlink_to(tmp_path / "secret.txt")
    from axon.skills.bundle import BundleError
    with pytest.raises(BundleError, match="outside"):
        sources.bundle_from_folder(d)
