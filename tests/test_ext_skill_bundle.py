"""Marketplace skills install the whole folder (scripts, references), safely."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from axon.marketplace import catalog, skill_tools
from axon.skills import bundle as bundle_mod
from axon.skills import importer

SHA = "a" * 40
SKILL_MD = b"---\nname: demo\ndescription: Demo skill.\n---\n\nRun scripts/run.py\n"


def make_tar(entries: dict[str, bytes | tuple], top: str = "skills-main", comment: str = SHA) -> bytes:
    """entries: path -> bytes | ("symlink", target) | ("hardlink", target) | ("dev",)"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}) as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        for path, val in entries.items():
            ti = tarfile.TarInfo(f"{top}/{path}")
            if isinstance(val, tuple) and val[0] in ("symlink", "hardlink"):
                ti.type = tarfile.SYMTYPE if val[0] == "symlink" else tarfile.LNKTYPE
                ti.linkname = val[1]
                tf.addfile(ti)
            elif isinstance(val, tuple):
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(val)
                ti.mode = 0o755 if path.endswith(".sh") else 0o644
                tf.addfile(ti, io.BytesIO(val))
    return buf.getvalue()


GOOD = {
    "skills/demo/SKILL.md": SKILL_MD,
    "skills/demo/scripts/run.py": b"print('hi')\n",
    "skills/demo/scripts/go.sh": b"#!/bin/sh\necho hi\n",
    "skills/demo/references/guide.md": b"# guide\n",
    "skills/other/SKILL.md": b"other",
}


def entry(sha: str = SHA) -> catalog.SkillEntry:
    return catalog.SkillEntry(id="demo", name="demo", publisher="x", description="d", category="c", kind="github",
                              source="owner/skills/skills/demo", sha=sha)


def test_whole_folder_is_installed_and_other_skills_are_not(workspace: Path):
    seen: list[str] = []
    b = skill_tools.fetch_skill(entry(), fetch=lambda url: seen.append(url) or make_tar(GOOD))
    assert seen == [f"https://codeload.github.com/owner/skills/tar.gz/{SHA}"]       # pinned commit, not a branch
    assert sorted(b.files) == ["SKILL.md", "references/guide.md", "scripts/go.sh", "scripts/run.py"]
    path = skill_tools.install_skill(entry(), workspace, bundle=b)
    root = path.parent
    assert (root / "scripts" / "run.py").read_text() == "print('hi')\n"
    assert (root / "references" / "guide.md").is_file()
    assert (root / "scripts" / "go.sh").stat().st_mode & 0o111
    origin = skill_tools.read_origin(root)
    assert origin["commit"] == SHA and origin["content_hash"] == b.content_hash() and origin["files"] == 4


def test_listing_flags_executables():
    b = bundle_mod.read_tarball(make_tar(GOOD), "skills/demo", "src")
    text = "\n".join(b.listing())
    assert "scripts/run.py" in text and text.count("EXECUTABLE SCRIPT") == 2
    assert "references/guide.md  (8 bytes)" in text and "guide.md  (8 bytes)  [EXEC" not in text


@pytest.mark.parametrize("entries,msg", [
    ({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/evil": ("symlink", "../../../../etc/passwd")}, "outside"),
    ({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/evil": ("symlink", "/etc/passwd")}, "outside"),
    ({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/evil": ("hardlink", "skills-main/skills/other/SKILL.md")}, "outside"),
    ({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/dev": ("dev",)}, "regular"),
    ({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/big": b"x" * (bundle_mod.MAX_FILE_BYTES + 1)}, "larger"),
    ({"skills/demo/scripts/x.py": b"1"}, "SKILL.md"),
])
def test_unsafe_archives_are_rejected(entries, msg):
    with pytest.raises(bundle_mod.BundleError, match=msg):
        bundle_mod.read_tarball(make_tar(entries), "skills/demo")


def test_inner_symlink_is_skipped_not_created(workspace: Path):
    b = bundle_mod.read_tarball(make_tar({"skills/demo/SKILL.md": SKILL_MD, "skills/demo/alias.md": ("symlink", "SKILL.md")}),
                                "skills/demo")
    assert b.skipped == ["alias.md"] and "alias.md" not in b.files


def test_file_count_cap():
    entries = {"skills/demo/SKILL.md": SKILL_MD}
    entries.update({f"skills/demo/f{i}.txt": b"x" for i in range(bundle_mod.MAX_FILES + 1)})
    with pytest.raises(bundle_mod.BundleError, match="more than"):
        bundle_mod.read_tarball(make_tar(entries), "skills/demo")


def test_dotdot_member_is_rejected():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("skills-main/skills/demo/SKILL.md", "skills-main/skills/demo/../../x"):
            ti = tarfile.TarInfo(name)
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(bundle_mod.BundleError):
        bundle_mod.read_tarball(buf.getvalue(), "skills/demo")


def test_pin_mismatch_is_rejected():
    with pytest.raises(ValueError, match="expected"):
        skill_tools.fetch_skill(entry(), fetch=lambda url: make_tar(GOOD, comment="b" * 40))


def test_force_replaces_and_existing_is_refused(workspace: Path):
    b = bundle_mod.read_tarball(make_tar(GOOD), "skills/demo", "owner/skills/skills/demo")
    skill_tools.install_skill(entry(), workspace, bundle=b)
    with pytest.raises(FileExistsError):
        skill_tools.install_skill(entry(), workspace, bundle=b)
    skill_tools.install_skill(entry(), workspace, force=True, bundle=b)


def test_catalog_has_no_skill_md_only_note():
    for e in catalog.load_skills():
        assert "Only SKILL.md" not in e.note


def test_cli_shows_files_and_asks(workspace: Path, monkeypatch):
    from axon.marketplace.cli import Marketplace
    monkeypatch.setattr(importer, "_download_bytes", lambda url, limit: make_tar(GOOD))
    out: list[str] = []
    answers = iter(["n", "y"])
    shop = Marketplace(workspace, out=out.append, ask_line=lambda q: next(answers))
    shop.install_skill(entry(), __import__("axon.marketplace.cli", fromlist=["Args"]).Args())
    text = "\n".join(out)
    assert "scripts/run.py" in text and "EXECUTABLE SCRIPT" in text and "Cancelled" in text
    assert not skill_tools.installed_skills(workspace)
    shop.install_skill(entry(), __import__("axon.marketplace.cli", fromlist=["Args"]).Args())
    assert (skill_tools.skills_root(workspace, "user") / "demo" / "scripts" / "run.py").is_file()


def _install_v1(workspace: Path) -> Path:
    b = bundle_mod.read_tarball(make_tar(GOOD), "skills/demo", "owner/skills/skills/demo")
    return skill_tools.install_skill(entry(), workspace, bundle=b).parent


def test_update_shows_diff_before_applying_and_verifies_content(workspace: Path):
    from axon.marketplace import updates
    folder = _install_v1(workspace)
    v2 = dict(GOOD)
    v2["skills/demo/scripts/run.py"] = b"print('changed')\n"
    v2["skills/demo/SKILL.md"] = SKILL_MD + b"New line\n"
    pending = updates.preview_skill_update(workspace, "demo", fetch_bytes=lambda u: make_tar(v2, comment="c" * 40))
    text = "\n".join(pending.lines)
    assert "changed  scripts/run.py  [EXECUTABLE SCRIPT]" in text and "+New line" in text
    assert pending.scripts_changed and not pending.local_edits
    assert "print('hi')" in (folder / "scripts" / "run.py").read_text()          # nothing written by the preview
    with pytest.raises(ValueError, match="review it"):
        updates.apply_skill_update(workspace, "demo", auto=True)                  # auto-update refuses script changes
    updates.preview_skill_update(workspace, "demo", fetch_bytes=lambda u: make_tar(v2, comment="c" * 40))
    assert "Updated skill" in updates.apply_skill_update(workspace, "demo")
    assert "changed" in (folder / "scripts" / "run.py").read_text()
    assert skill_tools.read_origin(folder)["commit"] == "c" * 40


def test_update_warns_about_local_edits(workspace: Path):
    from axon.marketplace import updates
    folder = _install_v1(workspace)
    (folder / "references" / "guide.md").write_text("my notes")
    pending = updates.preview_skill_update(workspace, "demo", fetch_bytes=lambda u: make_tar(GOOD, comment="c" * 40))
    assert pending.local_edits and any("edited after install" in l for l in pending.lines)


def test_pinned_catalog_entry_reports_update_without_network(workspace: Path, tmp_path: Path):
    import json
    from axon.marketplace import updates
    _install_v1(workspace)
    data = tmp_path / "data"
    data.mkdir()
    (data / "skills.json").write_text(json.dumps({"skills": [{
        "id": "demo", "name": "demo", "publisher": "x", "description": "d", "category": "c", "kind": "github",
        "source": "owner/skills/skills/demo", "sha": "d" * 40}]}))
    found = updates.skill_updates(workspace, lambda s: pytest.fail("no network expected"), data)
    assert [u.name for u in found] == ["demo"] and "pins dddddddddd" in found[0].message
