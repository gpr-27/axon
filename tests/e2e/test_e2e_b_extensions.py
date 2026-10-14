"""E2E-B: user extensions through the real CLI - custom markdown commands (.axon/commands,
.claude/commands, user level, namespaces, $ARGUMENTS, frontmatter model), skills (SKILL.md
discovery, /name and /skill:name) and plugins (command + skill + agent + hook, enable/disable)."""
from __future__ import annotations

import json

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_b_util import (
    no_crash, run_json, write_file, parallel, pbatch,  # noqa: F401 (pbatch is a fixture)
)

pytestmark = pytest.mark.e2e


def _last_user(env: AxonEnv) -> str:
    reqs = env.llm.completion_requests()
    assert reqs, "no LLM request was made"
    return reqs[-1].last_user_text()


def _p(env: AxonEnv, line: str, *extra: str):
    r = env.run(["-p", *extra, line])
    no_crash(r)
    assert r.code == 0, str(r)
    return r


def _cmd_file(env: AxonEnv, rel: str, body: str, fm: str = "description: test command") -> None:
    write_file(env.workspace / rel, f"---\n{fm}\n---\n{body}\n")


# ─── custom commands ────────────────────────────────────────────────────────

@parallel
def test_custom_command_axon_dir_arguments(axon_env: AxonEnv) -> None:
    """.axon/commands/greet.md expands $ARGUMENTS and is sent to the model."""
    _cmd_file(axon_env, ".axon/commands/greet.md", "Say hello to $ARGUMENTS politely.")
    _p(axon_env, "/greet Bob Smith")
    assert "Say hello to Bob Smith politely." in _last_user(axon_env)


@parallel
def test_custom_command_claude_dir(axon_env: AxonEnv) -> None:
    """.claude/commands/*.md (Claude Code layout) is picked up too."""
    _cmd_file(axon_env, ".claude/commands/claudecmd.md", "CLAUDE-DIR-BODY $ARGUMENTS")
    _p(axon_env, "/claudecmd x1")
    assert "CLAUDE-DIR-BODY x1" in _last_user(axon_env)


@parallel
def test_custom_command_positional_args(axon_env: AxonEnv) -> None:
    """$1 and $2 are replaced by positional arguments."""
    _cmd_file(axon_env, ".axon/commands/pair.md", "first=$1 second=$2")
    _p(axon_env, "/pair alpha beta")
    assert "first=alpha second=beta" in _last_user(axon_env)


@parallel
def test_custom_command_namespace(axon_env: AxonEnv) -> None:
    """A sub-directory becomes a namespace: frontend/component.md -> /frontend:component."""
    _cmd_file(axon_env, ".axon/commands/frontend/component.md", "NAMESPACED-BODY $ARGUMENTS")
    _p(axon_env, "/frontend:component Button")
    assert "NAMESPACED-BODY Button" in _last_user(axon_env)


@parallel
def test_custom_command_frontmatter_model(axon_env: AxonEnv) -> None:
    """`model:` in frontmatter overrides the model for that command's request."""
    _cmd_file(axon_env, ".axon/commands/othermodel.md", "MODEL-OVERRIDE-BODY", "description: x\nmodel: fake-model")
    _p(axon_env, "/othermodel")
    reqs = axon_env.llm.completion_requests()
    assert reqs, "no request made (model override sent the request elsewhere?)"
    assert reqs[-1].body["model"] == "fake-model", reqs[-1].body.get("model")


@parallel
def test_custom_command_user_level(axon_env: AxonEnv) -> None:
    """~/.axon/commands/*.md (AXON_HOME) commands work in any project."""
    write_file(axon_env.axon_home / "commands" / "usercmd.md", "USER-LEVEL-BODY $ARGUMENTS")
    _p(axon_env, "/usercmd q")
    assert "USER-LEVEL-BODY q" in _last_user(axon_env)


@parallel
def test_project_command_overrides_user_command(axon_env: AxonEnv) -> None:
    """A project command wins over a user command with the same name."""
    write_file(axon_env.axon_home / "commands" / "dup.md", "USER-VERSION")
    _cmd_file(axon_env, ".axon/commands/dup.md", "PROJECT-VERSION")
    _p(axon_env, "/dup")
    assert "PROJECT-VERSION" in _last_user(axon_env)


@parallel
def test_custom_command_shadowed_by_builtin_reachable_with_prefix(axon_env: AxonEnv) -> None:
    """A custom /status does not replace the built-in; it stays reachable as /cmd:status."""
    _cmd_file(axon_env, ".axon/commands/status.md", "CUSTOM-STATUS-BODY")
    out = _p(axon_env, "/status").output
    assert "Axon status" in out and axon_env.llm.completion_requests() == []
    _p(axon_env, "/cmd:status")
    assert "CUSTOM-STATUS-BODY" in _last_user(axon_env)


@parallel
def test_custom_command_listed_in_help(axon_env: AxonEnv) -> None:
    """/help shows custom commands with their description and (project) source."""
    _cmd_file(axon_env, ".axon/commands/deploy.md", "deploy it", "description: Deploy the app\nargument-hint: <env>")
    out = _p(axon_env, "/help").output
    assert "Custom commands" in out and "/deploy" in out and "Deploy the app" in out and "(project)" in out
    assert "<env>" in out


@parallel
def test_custom_command_bracket_argument_hint(axon_env: AxonEnv) -> None:
    """`argument-hint: [env]` is displayed verbatim as [env] in /help."""
    _cmd_file(axon_env, ".axon/commands/deploy.md", "deploy it", "description: Deploy the app\nargument-hint: [env]")
    out = _p(axon_env, "/help").output
    assert "[env]" in out and "['env']" not in out, out


@parallel
def test_custom_command_file_include(axon_env: AxonEnv) -> None:
    """`@path` in a command body inlines the workspace file's content."""
    write_file(axon_env.workspace / "notes.txt", "INCLUDED-FILE-CONTENT")
    _cmd_file(axon_env, ".axon/commands/inc.md", "Look at @notes.txt please")
    _p(axon_env, "/inc")
    assert "INCLUDED-FILE-CONTENT" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_custom_command_without_frontmatter(axon_env: AxonEnv) -> None:
    """A plain markdown file without frontmatter is still a command."""
    write_file(axon_env.workspace / ".axon" / "commands" / "plain.md", "PLAIN-BODY $ARGUMENTS")
    _p(axon_env, "/plain z")
    assert "PLAIN-BODY z" in _last_user(axon_env)


@parallel
def test_help_topic_for_custom_command(axon_env: AxonEnv) -> None:
    """/help <custom> shows its source and path."""
    _cmd_file(axon_env, ".axon/commands/topic.md", "x", "description: Topic cmd")
    out = _p(axon_env, "/help topic").output
    assert "Topic cmd" in out and "project" in out


# ─── skills ─────────────────────────────────────────────────────────────────

def _skill(root, name: str, body: str, extra: str = "") -> None:
    write_file(root / name / "SKILL.md", f"---\nname: {name}\ndescription: The {name} skill\n{extra}---\n{body}\n")


@parallel
def test_project_skill_runs_by_name(axon_env: AxonEnv) -> None:
    """.axon/skills/<n>/SKILL.md is runnable as /<n> and its body reaches the model."""
    _skill(axon_env.workspace / ".axon" / "skills", "greeter", "GREETER-SKILL-BODY")
    _p(axon_env, "/greeter world")
    text = axon_env.llm.completion_requests()[-1].all_text()
    assert "GREETER-SKILL-BODY" in text and "world" in text


@parallel
def test_skill_prefix_form(axon_env: AxonEnv) -> None:
    """/skill:<name> runs the skill explicitly."""
    _skill(axon_env.workspace / ".axon" / "skills", "prefixed", "PREFIXED-SKILL-BODY")
    _p(axon_env, "/skill:prefixed")
    assert "PREFIXED-SKILL-BODY" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_claude_dir_skill_discovered(axon_env: AxonEnv) -> None:
    """.claude/skills/<n>/SKILL.md is discovered and listed by /skills."""
    _skill(axon_env.workspace / ".claude" / "skills", "claudeskill", "CLAUDE-SKILL-BODY")
    assert "claudeskill" in _p(axon_env, "/skills").output
    _p(axon_env, "/claudeskill")
    assert "CLAUDE-SKILL-BODY" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_personal_skill_in_axon_home(axon_env: AxonEnv) -> None:
    """AXON_HOME/skills/<n>/SKILL.md is a personal skill available in every project."""
    _skill(axon_env.axon_home / "skills", "personal1", "PERSONAL-SKILL-BODY")
    _p(axon_env, "/skill:personal1")
    assert "PERSONAL-SKILL-BODY" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_skill_shadowed_by_builtin_uses_prefix(axon_env: AxonEnv) -> None:
    """A skill named like a built-in (status) runs only via /skill:status."""
    _skill(axon_env.workspace / ".axon" / "skills", "status", "STATUS-SKILL-BODY")
    assert "Axon status" in _p(axon_env, "/status").output
    _p(axon_env, "/skill:status")
    assert "STATUS-SKILL-BODY" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_non_user_invocable_skill_hidden(axon_env: AxonEnv) -> None:
    """A skill with `user-invocable: false` is not runnable as a slash command."""
    _skill(axon_env.workspace / ".axon" / "skills", "modelonly", "MODEL-ONLY-BODY", "user-invocable: false\n")
    out = _p(axon_env, "/modelonly").output
    assert "Unknown command" in out
    assert not any("MODEL-ONLY-BODY" in q.all_text() and "modelonly" in q.last_user_text()
                   for q in axon_env.llm.completion_requests())


@parallel
def test_skills_advertised_to_model(axon_env: AxonEnv) -> None:
    """Discovered skills are advertised to the model (name + description in the request)."""
    _skill(axon_env.workspace / ".axon" / "skills", "advertised", "body")
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hi")
    assert "The advertised skill" in axon_env.llm.completion_requests()[0].all_text()


# ─── plugins ────────────────────────────────────────────────────────────────

def _plugin(env: AxonEnv, name: str = "myplug") -> None:
    root = env.workspace / ".axon" / "plugins" / name
    write_file(root / "plugin.json", json.dumps({"name": name, "version": "1.2.3", "description": "Test plugin"}))
    write_file(root / "commands" / "hello.md", "---\ndescription: plugin hello\n---\nPLUGIN-HELLO $ARGUMENTS\n")
    write_file(root / "skills" / "plugskill" / "SKILL.md",
               "---\nname: plugskill\ndescription: plugin skill\n---\nPLUGIN-SKILL-BODY\n")
    write_file(root / "agents" / "plugagent.md", "---\nname: plugagent\ndescription: Plugin agent\n---\nYou help.\n")
    write_file(root / "hooks" / "hooks.json", json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "echo plugin-hook > plugin_hook.out"}]}]}}))


@parallel
def test_plugin_create_and_run_command(axon_env: AxonEnv) -> None:
    """/plugin create NAME scaffolds a plugin whose /NAME:hello command expands $ARGUMENTS."""
    _p(axon_env, "/plugin create demo")
    assert (axon_env.workspace / ".axon" / "plugins" / "demo" / "plugin.json").is_file()
    _p(axon_env, "/demo:hello Alice")
    assert "Greet Alice" in _last_user(axon_env)


@parallel
def test_plugin_list_and_info_show_components(axon_env: AxonEnv) -> None:
    """/plugin lists the plugin with its components; /plugin info shows version and counts."""
    _plugin(axon_env)
    out = _p(axon_env, "/plugin").output
    assert "myplug" in out and "1 commands" in out and "1 skills" in out and "1 agents" in out, out
    info = _p(axon_env, "/plugin info myplug").output
    assert "1.2.3" in info and "enabled" in info


@parallel
def test_plugin_command_and_skill(axon_env: AxonEnv) -> None:
    """A plugin's command runs as /myplug:hello and its skill as /plugskill."""
    _plugin(axon_env)
    _p(axon_env, "/myplug:hello there")
    assert "PLUGIN-HELLO there" in _last_user(axon_env)
    _p(axon_env, "/plugskill")
    assert "PLUGIN-SKILL-BODY" in axon_env.llm.completion_requests()[-1].all_text()


@parallel
def test_plugin_disable_then_enable(axon_env: AxonEnv) -> None:
    """/plugin disable hides the plugin's command (disabled.json); /plugin enable restores it."""
    _plugin(axon_env)
    _p(axon_env, "/plugin disable myplug")
    assert "myplug" in (axon_env.axon_home / "plugins" / "disabled.json").read_text(encoding="utf-8")
    assert "Unknown command" in _p(axon_env, "/myplug:hello").output
    assert "disabled" in _p(axon_env, "/plugin info myplug").output
    _p(axon_env, "/plugin enable myplug")
    _p(axon_env, "/myplug:hello back")
    assert "PLUGIN-HELLO back" in _last_user(axon_env)


@parallel
def test_plugin_disabled_via_enabled_plugins_setting(axon_env: AxonEnv) -> None:
    """`enabledPlugins: {"myplug": false}` in .axon/settings.json disables the plugin."""
    _plugin(axon_env)
    write_file(axon_env.workspace / ".axon" / "settings.json", json.dumps({"enabledPlugins": {"myplug": False}}))
    assert "Unknown command" in _p(axon_env, "/myplug:hello").output


@parallel
def test_plugin_install_from_folder(axon_env: AxonEnv) -> None:
    """/plugin install <folder> copies a local plugin into AXON_HOME/plugins."""
    src = axon_env.root / "srcplug"
    write_file(src / "plugin.json", json.dumps({"name": "srcplug", "version": "0.1"}))
    write_file(src / "commands" / "go.md", "SRCPLUG-GO")
    _p(axon_env, f"/plugin install {src}")
    assert (axon_env.axon_home / "plugins" / "srcplug" / "plugin.json").is_file()
    _p(axon_env, "/srcplug:go")
    assert "SRCPLUG-GO" in _last_user(axon_env)


@parallel
def test_plugin_agent_listed(axon_env: AxonEnv) -> None:
    """A plugin's agents/plugagent.md shows up in /agents."""
    _plugin(axon_env)
    assert "plugagent" in _p(axon_env, "/agents").output


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell redirection")
@parallel
def test_plugin_hook_runs(axon_env: AxonEnv) -> None:
    """A plugin's UserPromptSubmit hook runs when a prompt is submitted."""
    _plugin(axon_env)
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hello")
    assert (axon_env.workspace / "plugin_hook.out").is_file()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell redirection")
@parallel
def test_settings_user_prompt_submit_hook_runs(axon_env: AxonEnv) -> None:
    """Control: the same UserPromptSubmit hook configured in .axon/settings.json does run."""
    write_file(axon_env.workspace / ".axon" / "settings.json", json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "echo settings-hook > settings_hook.out"}]}]}}))
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hello")
    assert (axon_env.workspace / "settings_hook.out").is_file()
