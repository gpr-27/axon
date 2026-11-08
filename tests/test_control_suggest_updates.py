"""Project suggestions (once per project, never installed unasked) and the update check (reports only)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from axon import notices
from axon.marketplace import cli, skill_tools, startup, suggest, updates
from axon.marketplace.catalog import load_connectors
from axon.marketplace.installer import HealthResult
from axon.mcp.config import MCPConfig, ServerSpec


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "RENDER_API_KEY"):
        monkeypatch.setenv(name, "")
    notices.drain()
    yield home
    notices.drain()


def ids(workspace: Path) -> list[str]:
    return [s.connector_id for s in suggest.detect_signals(workspace)]


def run(tokens: list[str], workspace: Path):
    lines: list[str] = []
    shop = cli.Marketplace(workspace, out=lines.append,
                           health_check=lambda s, w, t: HealthResult("connected", tools=["t"]))
    return shop.run(tokens), "\n".join(lines)


# ─── signals ─────────────────────────────────────────────────────────────────

def test_github_remote_is_detected(workspace: Path) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:me/app.git\n')
    assert ids(workspace) == ["github"]


def test_a_gitlab_remote_is_not_github(workspace: Path) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text('[remote "origin"]\n\turl = git@gitlab.com:me/app.git\n')
    assert ids(workspace) == []


def test_package_json_dependencies(workspace: Path) -> None:
    deps = {"dependencies": {"@supabase/supabase-js": "^2", "stripe": "^14", "@clerk/nextjs": "^5"},
            "devDependencies": {"prisma": "^5", "@sentry/nextjs": "^8", "mongoose": "^8"}}
    (workspace / "package.json").write_text(json.dumps(deps))
    assert set(ids(workspace)) == {"supabase", "stripe", "clerk", "prisma", "sentry", "mongodb"}


def test_config_files(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    (workspace / "render.yaml").write_text("services: []")
    (workspace / "wrangler.toml").write_text("name = 'w'")
    (workspace / "sentry.properties").write_text("defaults.org=x")
    assert set(ids(workspace)) == {"vercel", "render", "cloudflare-api", "sentry"}


def test_mongodb_uri_in_env_file_without_reading_the_secret(workspace: Path) -> None:
    (workspace / ".env.example").write_text("DATABASE_URL=mongodb+srv://user:pw@cluster.mongodb.net/db\n")
    signals = suggest.detect_signals(workspace)
    assert [s.connector_id for s in signals] == ["mongodb"]
    assert "pw" not in signals[0].reason and ".env.example" in signals[0].reason


def test_python_requirements(workspace: Path) -> None:
    (workspace / "requirements.txt").write_text("stripe==9.0\nsentry-sdk>=2\nrequests\n")
    assert set(ids(workspace)) == {"stripe", "sentry"}
    (workspace / "requirements.txt").write_text("stripe-tax-helper\n")
    assert ids(workspace) == []


def test_broken_package_json_and_empty_workspace(workspace: Path) -> None:
    (workspace / "package.json").write_text("{not json")
    assert ids(workspace) == []


def test_only_catalog_connectors_that_are_not_installed_are_suggested(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    (workspace / "render.yaml").write_text("x")
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("vercel", {"type": "http", "url": "https://mcp.vercel.com"}), "user")
    assert [c.id for c, _why in suggest.fitting_connectors(workspace)] == ["render"]


def test_every_signal_id_exists_in_the_catalog() -> None:
    known = {c.id for c in load_connectors()}
    wanted = set(suggest.NPM_PACKAGES) | set(suggest.PYTHON_PACKAGES) | set(suggest.FILE_SIGNALS) | {"github", "mongodb"}
    assert wanted <= known


# ─── once per project, never installed unasked ───────────────────────────────

def test_suggestions_are_posted_once_per_project(workspace: Path, tmp_path: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    first = suggest.run_once(workspace, False, notices.post)
    assert first == ["vercel"]
    (note,) = notices.drain()
    assert "vercel" in note and "/marketplace install" in note
    assert suggest.run_once(workspace, False, notices.post) == [] and notices.drain() == []
    other = tmp_path / "other"
    other.mkdir()
    (other / "vercel.json").write_text("{}")
    assert suggest.run_once(other, False, notices.post) == ["vercel"]


def test_new_signals_in_a_known_project_are_announced_later(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    suggest.run_once(workspace, False, notices.post)
    notices.drain()
    (workspace / "render.yaml").write_text("x")
    assert suggest.run_once(workspace, False, notices.post) == ["render"]


def test_nothing_is_installed_without_auto_install(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    suggest.run_once(workspace, False, notices.post)
    assert "vercel" not in MCPConfig(workspace).load_specs()


def test_auto_install_installs_only_what_needs_nothing_more(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / "vercel.json").write_text("{}")                  # oauth, no key needed
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("url = https://github.com/a/b\n")   # needs a token
    health = lambda spec, ws, timeout: HealthResult("connected", tools=["t"])      # noqa: E731
    suggest.run_once(workspace, True, notices.post, health)
    specs = MCPConfig(workspace).load_specs()
    assert "vercel" in specs and specs["vercel"].scope == "local"
    assert "github" not in specs
    text = " | ".join(notices.drain())
    assert "Installed for this project: vercel" in text and "github" in text


def test_auto_install_uses_a_key_that_is_already_set(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("url = https://github.com/a/b\n")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test")
    health = lambda spec, ws, timeout: HealthResult("connected", tools=["t"])      # noqa: E731
    suggest.run_once(workspace, True, notices.post, health)
    assert "github" in MCPConfig(workspace).load_specs()


def test_marketplace_suggest_lists_and_resets(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    _, text = run(["suggest"], workspace)
    assert "vercel" in text and "/marketplace install vercel" in text
    suggest.run_once(workspace, False, notices.post)
    notices.drain()
    run(["suggest", "--reset"], workspace)
    assert suggest.run_once(workspace, False, notices.post) == ["vercel"]


def test_marketplace_suggest_with_nothing_to_say(workspace: Path) -> None:
    assert "No connector suggestions" in run(["suggest"], workspace)[1]


def test_startup_begin_does_nothing_while_automation_is_off(workspace: Path) -> None:
    (workspace / "vercel.json").write_text("{}")
    auto = startup.begin(workspace)
    time.sleep(0.1)
    assert auto.enabled is False and notices.drain() == []


def test_startup_background_tasks_post_notices_and_swallow_errors(workspace: Path, private_home: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.marketplace.automation import Automation
    (workspace / "vercel.json").write_text("{}")
    monkeypatch.setattr(updates, "run_if_due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    startup.run_background_tasks(workspace, Automation(enabled=True))
    assert any("vercel" in note for note in notices.drain())


# ─── the update check ────────────────────────────────────────────────────────

def install_skill_from(workspace: Path, text: str = "---\nname: remote-skill\ndescription: Remote\n---\n\nv1\n") -> Path:
    folder = skill_tools.skills_root(workspace, "user") / "remote-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    skill_tools.write_origin(folder, "owner/repo/skills/remote-skill", text)
    return folder / "SKILL.md"


def test_unchanged_skill_has_no_update(workspace: Path) -> None:
    text = "---\nname: remote-skill\ndescription: Remote\n---\n\nv1\n"
    install_skill_from(workspace, text)
    assert updates.skill_updates(workspace, lambda source: text) == []


def test_changed_skill_is_reported_and_not_changed(workspace: Path) -> None:
    path = install_skill_from(workspace)
    found = updates.skill_updates(workspace, lambda source: "different")
    assert [(u.kind, u.name) for u in found] == [("skill", "remote-skill")]
    assert "v1" in path.read_text()


def test_offline_fetch_says_nothing(workspace: Path) -> None:
    install_skill_from(workspace)

    def offline(source: str) -> str:
        raise OSError("no network")

    assert updates.skill_updates(workspace, offline) == []


def test_skills_without_an_origin_are_skipped(workspace: Path) -> None:
    skill_tools.create_skill(skill_tools.NewSkill("home-made", "Mine", "Body."), workspace, "user")
    assert updates.skill_updates(workspace, lambda s: "x") == []


def test_catalog_endpoint_change_is_reported_for_connectors(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("vercel", {"type": "http", "url": "https://old.example.com/mcp"}), "user")
    found = updates.connector_updates(workspace)
    assert [u.name for u in found] == ["vercel"] and "old.example.com" in found[0].message
    MCPConfig(workspace).save_spec(ServerSpec.from_dict(
        "vercel", {"type": "http", "url": next(c.url for c in load_connectors() if c.id == "vercel")}), "user")
    assert updates.connector_updates(workspace) == []


def test_periodic_check_runs_when_due_and_only_reports(workspace: Path) -> None:
    path = install_skill_from(workspace)
    posted: list[str] = []
    found = updates.run_if_due(workspace, 7, False, posted.append, lambda s: "newer text")
    assert len(found) == 1 and "update(s) available" in posted[0]
    assert "v1" in path.read_text()                                 # nothing was changed
    assert updates.run_if_due(workspace, 7, False, posted.append, lambda s: "newer text") == []   # not due again
    assert len(posted) == 1


def test_check_is_due_again_after_the_interval(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    updates.mark_checked([])
    assert not updates.is_due(7)
    assert updates.is_due(7, now=time.time() + 8 * 86400)
    assert updates.is_due(0)


def test_auto_update_applies_skills_but_never_connectors(workspace: Path) -> None:
    path = install_skill_from(workspace)
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("vercel", {"type": "http", "url": "https://old.example.com/mcp"}), "user")
    posted: list[str] = []
    updates.run_if_due(workspace, 7, True, posted.append, lambda s: "---\nname: remote-skill\ndescription: R\n---\n\nv2\n")
    assert "v2" in path.read_text()
    assert MCPConfig(workspace).get_spec("vercel").url == "https://old.example.com/mcp"
    assert any("Updated skill" in line for line in posted) and any("connector vercel" in line for line in posted)


def test_marketplace_updates_and_update_commands(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = install_skill_from(workspace)
    monkeypatch.setattr(updates, "default_fetcher", lambda source: "---\nname: remote-skill\ndescription: R\n---\n\nv9\n")
    monkeypatch.setattr(updates, "check_updates", lambda ws, fetch=None, data_dir=None: updates.skill_updates(ws, updates.default_fetcher))
    outcome, text = run(["updates"], workspace)
    assert outcome.code == 0 and "remote-skill" in text and "/marketplace update remote-skill" in text
    monkeypatch.setattr(updates, "apply_skill_update", lambda ws, name, fetch=updates.default_fetcher: "Updated skill 'remote-skill'.")
    assert "Updated skill" in run(["update", "remote-skill"], workspace)[1]
    assert run(["update", "ghost"], workspace)[0].code == 1


def test_updating_a_connector_by_command_is_refused(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("mine", {"command": "x"}), "user")
    outcome, text = run(["update", "mine"], workspace)
    assert outcome.code == 1 and "never updated automatically" in text


def test_marketplace_installed_skill_records_its_origin(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.skills import importer
    text = "---\nname: frontend-design\ndescription: Design\n---\n\nBody\n"
    from test_ext_skill_bundle import make_tar
    from axon.marketplace import catalog
    pin = catalog.find_entry(catalog.load_skills(), "frontend-design").sha
    tar = make_tar({"skills/frontend-design/SKILL.md": text.encode()}, comment=pin)
    monkeypatch.setattr(importer, "_download_bytes", lambda url, limit: tar)
    outcome, _ = run(["install", "frontend-design", "--tab", "skills"], workspace)
    assert outcome.code == 0
    origin = skill_tools.read_origin(skill_tools.skills_root(workspace, "user") / "frontend-design")
    assert origin["source"] and origin["sha256"] == skill_tools.text_hash(text) and origin["content_hash"]
