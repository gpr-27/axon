"""The /marketplace command: subcommands, scripted use, plain text output and narrow terminals."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from axon.marketplace import cli, installer
from axon.marketplace.installer import HealthResult
from axon.marketplace.render import Row, discover_lines, yours_lines

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "RENDER_API_KEY"):
        monkeypatch.setenv(name, "")  # empty counts as unset; restored after the test
    return home


def run(tokens: list[str], workspace: Path, health=None, ask_line=None, ask_secret=None):
    lines: list[str] = []
    shop = cli.Marketplace(workspace, out=lines.append, ask_line=ask_line, ask_secret=ask_secret,
                           health_check=health or (lambda s, w, t: HealthResult("connected", tools=["t1"])))
    outcome = shop.run(tokens)
    return outcome, "\n".join(lines)


def test_list_shows_cards_and_install_hint(workspace: Path) -> None:
    outcome, text = run(["list"], workspace)
    assert outcome.code == 0
    assert "Connectors" in text and "GitHub" in text and "by GitHub" in text
    assert "/marketplace install" in text


def test_list_other_tabs(workspace: Path) -> None:
    assert "frontend-design" in run(["list", "skills"], workspace)[1]
    assert "create-plugin" in run(["list", "plugins"], workspace)[1]


def test_list_json_is_parseable(workspace: Path) -> None:
    _, text = run(["list", "--json", "--free"], workspace)
    items = json.loads(text)
    assert items and all(item["free"] for item in items)


def test_search_and_category_filter(workspace: Path) -> None:
    _, text = run(["search", "browser"], workspace)
    assert "Playwright" in text and "GitHub" not in text
    _, text = run(["list", "--category", "Payments"], workspace)
    assert "Stripe" in text and "Playwright" not in text
    assert run(["search"], workspace)[0].code == 1


def test_info_shows_env_vars_and_docs(workspace: Path) -> None:
    _, text = run(["info", "github"], workspace)
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in text and "docs.github.com" in text
    assert run(["info", "nope"], workspace)[0].code == 1


def test_install_then_yours_then_remove(workspace: Path) -> None:
    outcome, text = run(["install", "memory"], workspace)
    assert outcome.code == 0 and outcome.installed == ["memory"] and "Tools: t1" in text
    _, yours = run(["list", "--installed"], workspace)
    assert "Memory" in yours and "Local" in yours and "enabled" in yours
    assert "Fetch" not in yours
    outcome, _ = run(["disable", "memory"], workspace)
    assert outcome.code == 0
    assert "disabled" in run(["list", "--installed"], workspace)[1]
    outcome, text = run(["remove", "memory"], workspace)
    assert outcome.code == 0 and outcome.removed == ["memory"]
    assert run(["remove", "memory"], workspace)[0].code == 1


def test_install_failure_sets_exit_code_and_rolls_back(workspace: Path) -> None:
    def bad(s, w, t):
        return HealthResult("failed", "connection refused")
    outcome, text = run(["install", "linear"], workspace, health=bad)
    assert outcome.code == 1 and "rolled back" in text
    assert run(["install", "linear", "--keep"], workspace, health=bad)[0].code == 1
    assert "linear" in installer.installed_names(workspace)


def test_install_with_env_option_and_prompt(workspace: Path) -> None:
    outcome, text = run(["install", "github", "--env", "GITHUB_PERSONAL_ACCESS_TOKEN=abc"], workspace)
    assert outcome.code == 0 and "GITHUB_PERSONAL_ACCESS_TOKEN" in text and "abc" not in text
    outcome, _ = run(["install", "render"], workspace, ask_secret=lambda label: "key-1")
    assert outcome.code == 0


def test_install_without_key_in_script_mode_explains(workspace: Path) -> None:
    outcome, text = run(["install", "render"], workspace)
    assert outcome.code == 1 and "--env" in text


def test_add_custom_connector(workspace: Path) -> None:
    outcome, _ = run(["add", "https://mcp.example.com/mcp", "--name", "example"], workspace)
    assert outcome.code == 0 and "example" in installer.installed_names(workspace)
    outcome, _ = run(["add", "python", "-m", "srv"], workspace)
    assert "python" in installer.installed_names(workspace)


def test_create_skill_and_plugin_from_the_command_line(workspace: Path, private_home: Path) -> None:
    tokens = ["create-skill", "--name", "notes-helper", "--description", "Helps with notes.",
              "--instructions", "Summarise the notes.", "--trigger", "notes"]
    outcome, text = run(tokens, workspace)
    assert outcome.code == 0 and "notes-helper" in text
    assert (private_home / ".axon" / "skills" / "notes-helper" / "SKILL.md").is_file()
    outcome, _ = run(["create-plugin", "my-plug"], workspace)
    assert outcome.code == 0
    assert run(["create-skill", "--name", "x"], workspace)[0].code == 1


def test_interactive_skill_creator_asks_questions(workspace: Path, private_home: Path) -> None:
    answers = iter(["Tidy Notes", "Tidies notes.", "tidy, notes", "Step one.", "Step two.", ""])
    outcome, _ = run(["create-skill"], workspace, ask_line=lambda prompt: next(answers))
    assert outcome.code == 0
    assert (private_home / ".axon" / "skills" / "tidy-notes" / "SKILL.md").is_file()


def test_no_arguments_without_raw_keys_prints_the_list(workspace: Path) -> None:
    # The keyboard browser needs a real terminal (tests/test_keyboard_menus.py drives it);
    # without one, /marketplace prints the connector list as before.
    outcome, text = run([], workspace, ask_line=lambda prompt: "q")
    assert outcome.code == 0
    assert "Connectors" in text and "GitHub" in text


def test_unknown_subcommand(workspace: Path) -> None:
    outcome, text = run(["frobnicate"], workspace)
    assert outcome.code == 1 and "unknown subcommand" in text


def test_non_tty_output_is_plain_text(workspace: Path) -> None:
    lines: list[str] = []
    outcome = cli.run_command(["list"], workspace, interactive=False, out=lines.append)
    text = "\n".join(lines)
    assert outcome.code == 0 and "\x1b" not in text
    text.encode("ascii", "ignore")


def test_narrow_terminal_wraps_and_stacks() -> None:
    rows = [Row(id="x", name="Example", publisher="Acme", description="word " * 40, category="Dev",
                type_label="Web", status="enabled")]
    for line in discover_lines("connectors", rows, 1, width=48):
        assert len(ANSI.sub("", line)) <= 48
    narrow = yours_lines("connectors", rows, width=48)
    wide = yours_lines("connectors", rows, width=100)
    assert any("Type" in line for line in wide)
    assert not any("Type" in line for line in narrow)


def test_slash_command_and_cli_hook_are_registered() -> None:
    from axon.commands.registry import resolve_builtin
    for name in ("marketplace", "connectors", "store"):
        assert resolve_builtin(name).name == "marketplace"
    from axon.features import cli_subcommands
    assert "marketplace" in cli_subcommands()
