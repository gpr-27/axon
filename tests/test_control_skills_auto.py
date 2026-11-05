"""Auto-loading skills by trigger, and the built-in prompt handler that delivers notices."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon import notices
from axon.hooks.manager import HookManager
from axon.marketplace import skill_tools
from axon.skills import auto
from axon.skills.manager import SkillManager


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    auto._file_cache.clear()
    auto._seen_in_session.clear()
    notices.drain()
    yield home
    notices.drain()


def make_skill(workspace: Path, name: str, description: str, body: str = "Do the thing.", extra: str = "") -> None:
    folder = workspace / ".axon" / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n",
                                     encoding="utf-8")


def turn_on(home: Path, **changes) -> None:
    path = home / ".axon" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"automation": {"enabled": True, **changes}}))


def names(skills) -> list[str]:
    return [skill.name for skill in skills]


# ─── what counts as relevant ─────────────────────────────────────────────────

def test_a_trigger_phrase_in_the_prompt_selects_the_skill(workspace: Path) -> None:
    make_skill(workspace, "invoice-maker", 'Builds invoices. Use when the user mentions: "make an invoice", "billing pdf".')
    manager = SkillManager(workspace)
    assert names(auto.relevant_skills(manager, "Please make an invoice for ACME")) == ["invoice-maker"]
    assert auto.relevant_skills(manager, "Please fix the login page") == []


def test_skill_creator_triggers_are_picked_up(workspace: Path) -> None:
    skill = skill_tools.NewSkill("poster-maker", "Makes posters", "Design it.", ["poster layout"])
    skill_tools.create_skill(skill, workspace, "project")
    manager = SkillManager(workspace)
    assert names(auto.relevant_skills(manager, "I need a poster layout for the fair")) == ["poster-maker"]


def test_triggers_field_in_frontmatter(workspace: Path) -> None:
    make_skill(workspace, "release-notes", "Writes notes", extra="triggers: [changelog entry, release notes]\n")
    manager = SkillManager(workspace)
    assert names(auto.relevant_skills(manager, "draft the release notes please")) == ["release-notes"]


def test_trigger_must_match_whole_words(workspace: Path) -> None:
    make_skill(workspace, "cat-lover", 'Cats. Use when the user mentions: "cat".')
    manager = SkillManager(workspace)
    assert auto.relevant_skills(manager, "the category list is broken") == []
    assert names(auto.relevant_skills(manager, "my cat is sleeping")) == ["cat-lover"]


def test_enough_description_words_select_a_skill(workspace: Path) -> None:
    make_skill(workspace, "spreadsheet-cleaner", "Cleans spreadsheet columns, normalizes dates and removes duplicate rows")
    manager = SkillManager(workspace)
    assert names(auto.relevant_skills(manager, "normalizes dates and removes duplicate rows in this sheet")) == \
        ["spreadsheet-cleaner"]
    assert auto.relevant_skills(manager, "it removes something") == []


def test_workspace_files_matching_paths_add_weight(workspace: Path) -> None:
    make_skill(workspace, "terraform-helper", "Reviews terraform modules and plans", extra="paths: ['*.tf']\n")
    manager = SkillManager(workspace)
    prompt = "check the terraform plans"
    assert auto.relevant_skills(manager, prompt) == []
    (workspace / "main.tf").write_text("resource {}")
    auto._file_cache.clear()
    assert names(auto.relevant_skills(manager, prompt)) == ["terraform-helper"]


def test_existing_intent_matching_is_reused(workspace: Path) -> None:
    manager = SkillManager(workspace)
    assert "debug" in names(auto.relevant_skills(manager, "I get a traceback when I run it"))


def test_user_only_and_disabled_skills_are_never_chosen(workspace: Path, private_home: Path) -> None:
    make_skill(workspace, "secret-op", 'Only for me. Use when the user mentions: "launch codes".',
               extra="disable-model-invocation: true\n")
    make_skill(workspace, "off-op", 'Switched off. Use when the user mentions: "launch codes".')
    from axon.skills import settings
    settings.disable_skill(workspace, "off-op")
    assert auto.relevant_skills(SkillManager(workspace), "give me the launch codes") == []


def test_strongest_match_comes_first(workspace: Path) -> None:
    make_skill(workspace, "weak-one", "Handles pdf merging and splitting")
    make_skill(workspace, "strong-one", 'Anything. Use when the user mentions: "merge the pdfs".')
    manager = SkillManager(workspace)
    assert names(auto.relevant_skills(manager, "merge the pdfs and handle pdf merging splitting"))[0] == "strong-one"


# ─── injecting into the prompt ───────────────────────────────────────────────

def test_context_has_the_skill_body_and_names(workspace: Path) -> None:
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".', body="STEP ONE: totals.")
    text, picked = auto.context_for(SkillManager(workspace), "make an invoice", "s1", limit=2)
    assert picked == ["invoice-maker"] and "STEP ONE: totals." in text and "<auto-loaded-skills>" in text


def test_only_the_limit_is_added_and_each_skill_once_per_session(workspace: Path) -> None:
    for name in ("one-skill", "two-skill", "three-skill"):
        make_skill(workspace, name, f'Skill. Use when the user mentions: "do everything", "{name} please".')
    manager = SkillManager(workspace)
    _, first = auto.context_for(manager, "do everything", "s1", limit=2)
    _, second = auto.context_for(manager, "do everything", "s1", limit=2)
    _, other_session = auto.context_for(manager, "do everything", "s2", limit=2)
    assert len(first) == 2 and second == [n for n in ("one-skill", "three-skill", "two-skill") if n not in first]
    assert len(other_session) == 2
    assert auto.context_for(manager, "do everything", "s1", limit=2) == ("", [])


def test_long_bodies_are_cut(workspace: Path) -> None:
    make_skill(workspace, "wordy-one", 'Long. Use when the user mentions: "be wordy".', body="word " * 5000)
    text, _ = auto.context_for(SkillManager(workspace), "be wordy", "s1", limit=1)
    assert len(text) < auto.MAX_BODY_CHARS + 600 and "skill cut here" in text


# ─── the prompt hook ─────────────────────────────────────────────────────────

def prompt_result(workspace: Path, prompt: str, session: str = "sess"):
    manager = HookManager(None, workspace)
    assert manager.has("UserPromptSubmit")
    return manager.run("UserPromptSubmit", {"prompt": prompt, "session_id": session})


def test_nothing_is_added_while_automation_is_off(workspace: Path) -> None:
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".')
    result = prompt_result(workspace, "make an invoice")
    assert result.additional_context == "" and result.system_message == ""


def test_the_hook_adds_matching_skills_and_says_so(workspace: Path, private_home: Path) -> None:
    turn_on(private_home)
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".', body="STEP ONE")
    result = prompt_result(workspace, "please make an invoice")
    assert "STEP ONE" in result.additional_context
    assert "Auto-loaded skills: invoice-maker" in result.system_message


def test_auto_skills_can_be_turned_off_alone(workspace: Path, private_home: Path) -> None:
    turn_on(private_home, auto_skills=False)
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".')
    assert prompt_result(workspace, "make an invoice").additional_context == ""


def test_max_auto_skills_is_respected(workspace: Path, private_home: Path) -> None:
    turn_on(private_home, max_auto_skills=1)
    for name in ("one-skill", "two-skill"):
        make_skill(workspace, name, 'Skill. Use when the user mentions: "do everything".')
    result = prompt_result(workspace, "do everything")
    assert result.additional_context.count("## Skill: /") == 1


def test_waiting_notices_are_shown_once_at_the_next_prompt(workspace: Path) -> None:
    notices.post("MCP server 'x' was switched off.")
    first = prompt_result(workspace, "hello")
    assert "switched off" in first.system_message
    assert prompt_result(workspace, "hello again").system_message == ""


def test_a_failing_handler_never_breaks_the_prompt(workspace: Path) -> None:
    manager = HookManager(None, workspace)

    def broken(event, payload):
        raise RuntimeError("boom")

    manager.internal_handlers.insert(0, broken)
    manager.run("UserPromptSubmit", {"prompt": "hi"})          # must not raise


def test_configured_hooks_still_run_next_to_the_built_in_one(workspace: Path, private_home: Path) -> None:
    turn_on(private_home)
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".', body="BODY")
    settings = workspace / ".axon" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "echo from-config-hook"}]}]}}))
    result = prompt_result(workspace, "make an invoice")
    assert "from-config-hook" in result.additional_context and "BODY" in result.additional_context


def test_disabling_all_hooks_also_stops_the_built_in_handler(workspace: Path, private_home: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    turn_on(private_home)
    make_skill(workspace, "invoice-maker", 'Invoices. Use when the user mentions: "make an invoice".')
    monkeypatch.setenv("AXON_DISABLE_ALL_HOOKS", "1")
    manager = HookManager(None, workspace)
    assert not manager.has("UserPromptSubmit")
    assert manager.run("UserPromptSubmit", {"prompt": "make an invoice"}).additional_context == ""
