"""Claude Code parity for skills, custom commands, plugins, marketplaces and output styles."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.skills.manager import (
    SkillManager, SkillShellError, expand_shell_injections, parse_frontmatter, render_body, skill_content,
    skills_prompt_section, substitute_arguments,
)
from axon.tools import create_default_registry
from fakes import FakeProvider


def _turn(text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input=10, output=5))


@pytest.fixture
def agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    return Agent(provider=FakeProvider([_turn(f"answer {i}") for i in range(10)]), tools=create_default_registry(),
                 permissions=PermissionEngine(s), context=ContextManager(s), session=SessionStore(workspace),
                 ledger=Ledger(), settings=s)


def _last_user(agent: Agent) -> str:
    return next(m["content"] for m in reversed(agent.conversation.messages) if m["role"] == "user" and isinstance(m["content"], str))


def _skill(root: Path, name: str, text: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


def _plugin(root: Path, name: str, manifest: dict | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if manifest is not None:
        (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


# ─── argument substitution & variables ──────────────────────────────────────

def test_arguments_zero_based_indexed_and_escaped() -> None:
    assert substitute_arguments("Migrate $0 from $1 to $2", "SearchBar JS TS") == "Migrate SearchBar from JS to TS"
    assert substitute_arguments("A=$ARGUMENTS[0] B=$ARGUMENTS[1]", 'x "hello world"') == "A=x B=hello world"
    assert substitute_arguments("cost \\$1.00 for $0", "foo") == "cost $1.00 for foo"
    assert substitute_arguments("double \\\\$0", "foo") == "double \\\\foo"      # \\ keeps both backslashes


def test_unresolved_indexed_placeholder_stays_and_arguments_are_appended() -> None:
    out = substitute_arguments("Use $1 here", "only")
    assert "Use $1 here" in out and out.endswith("ARGUMENTS: only")        # $1 had no argument: not "used"
    assert substitute_arguments("Hi $name", "", ["name"]) == "Hi "           # a named placeholder expands to empty
    assert substitute_arguments("$issue on $branch", "12 main", ["issue", "branch"]) == "12 on main"


def test_variables_expand_after_arguments(workspace: Path) -> None:
    out = render_body("dir=${CLAUDE_SKILL_DIR} proj=${CLAUDE_PROJECT_DIR} sid=${AXON_SESSION_ID} $ARGUMENTS",
                      "${CLAUDE_EFFORT}", variables={"SKILL_DIR": "/s", "PROJECT_DIR": "/p", "SESSION_ID": "abc", "EFFORT": "high"})
    assert out == "dir=/s proj=/p sid=abc high"
    assert render_body("${CLAUDE_UNKNOWN} ${user_config.x}", "", variables={}, user_config={"x": "1"}) == "${CLAUDE_UNKNOWN} 1"


def test_allowed_tools_expand_skill_dir(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "render-chart",
           "---\nname: render-chart\nallowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/render.sh *) Read\n---\nRun it\n")
    mgr = SkillManager(workspace)
    sk = mgr.skills["render-chart"]
    assert sk.allowed_tools == ["Bash(${CLAUDE_SKILL_DIR}/scripts/render.sh *)", "Read"]
    expanded = mgr.allowed_tools_for(sk, "sid")
    assert expanded[0] == f"Bash({sk.path.parent}/scripts/render.sh *)" and expanded[1] == "Read"


# ─── shell injection ────────────────────────────────────────────────────────

def test_shell_inline_only_at_line_start_or_after_whitespace(workspace: Path) -> None:
    ok = lambda cmd: True  # noqa: E731
    out = expand_shell_injections("A: !`echo one`\n!`echo two`\nKEY=!`echo three`", workspace, ok)
    assert "A: one" in out and "\ntwo\n" in out and "KEY=!`echo three`" in out


def test_shell_fenced_block_and_output_not_rescanned(workspace: Path) -> None:
    out = expand_shell_injections("```!\necho a\necho '!`echo nested`'\n```", workspace, lambda c: True)
    assert "a" in out and "!`echo nested`" in out and "nested\n" not in out.replace("!`echo nested`", "")


def test_shell_failure_aborts_but_search_exit_one_is_a_result(workspace: Path) -> None:
    with pytest.raises(SkillShellError, match="Shell command failed"):
        expand_shell_injections("!`exit 3`", workspace, lambda c: True)
    if os.name != "nt":                                                    # grep and /dev/null are POSIX
        assert expand_shell_injections("x !`grep -q zzz /dev/null`", workspace, lambda c: True) == "x "   # grep exit 1
    out = expand_shell_injections("x !`echo out; echo err 1>&2`", workspace, lambda c: True)
    assert "out" in out and "err" in out                                   # stderr merged into stdout


def test_shell_refused_raises_and_missing_gate_notices(workspace: Path) -> None:
    with pytest.raises(SkillShellError, match="permission check failed"):
        expand_shell_injections("!`echo hi`", workspace, lambda c: False)
    assert "not permitted" in expand_shell_injections("!`echo hi`", workspace, None)


def test_argument_text_is_never_executed(workspace: Path) -> None:
    ran: list[str] = []

    def gate(cmd: str) -> bool:
        ran.append(cmd)
        return True
    out = render_body("Body $ARGUMENTS", "!`echo pwned`", cwd=workspace, allow_shell=gate)
    assert out == "Body !`echo pwned`" and ran == []
    out = render_body("Run !`echo $ARGUMENTS`", "hello", cwd=workspace, allow_shell=gate)
    assert out == "Run hello" and ran == ["echo hello"]


def test_disable_skill_shell_execution_setting(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "sh", "---\nname: sh\n---\nv: !`echo hi`\n")
    (workspace / ".claude").mkdir(exist_ok=True)
    (workspace / ".claude" / "settings.json").write_text('{"disableSkillShellExecution": true}', encoding="utf-8")
    mgr = SkillManager(workspace)
    assert "[shell command execution disabled by policy]" in mgr.execute_skill("sh", "", allow_shell=lambda c: True)


# ─── frontmatter ────────────────────────────────────────────────────────────

def test_frontmatter_fields(workspace: Path) -> None:
    text = (
        "---\nname: full\ndescription: Does things\nwhen_to_use: when asked\nargument-hint: [a] [b]\narguments: issue branch\n"
        "disable-model-invocation: yes\nuser-invocable: off\nallowed-tools: Bash(git add *) Bash(git commit *), Read\n"
        "disallowed-tools:\n  - AskUserQuestion\nmodel: inherit\neffort: high\ncontext: fork\nagent: Explore\nbackground: no\n"
        "shell: powershell\nlicense: MIT\ncompatibility: any\npaths: src/**/*.py, tests/**\n"
        "hooks:\n  PreToolUse:\n    - matcher: \"Bash\"\n      hooks:\n        - type: command\n          command: \"./check.sh\"\n"
        "metadata:\n  team: core\n---\nBody\n")
    p = _skill(workspace / ".claude" / "skills", "full", text)
    sk = SkillManager(workspace).skills["full"]
    assert sk.disable_model_invocation is True and sk.user_invocable is False and sk.background is False
    assert sk.allowed_tools == ["Bash(git add *)", "Bash(git commit *)", "Read"]
    assert sk.disallowed_tools == ["AskUserQuestion"] and sk.arguments == ["issue", "branch"]
    assert sk.model is None and sk.effort == "high" and sk.context == "fork" and sk.agent == "Explore"
    assert sk.shell == "powershell" and sk.license == "MIT" and sk.paths == ["src/**/*.py", "tests/**"]
    assert sk.hooks == {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./check.sh"}]}]}
    assert sk.metadata == {"team": "core"} and sk.argument_hint == "[a] [b]" and p.exists()


def test_description_falls_back_to_first_line_and_bad_name_is_ignored(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "plain", "# Heading line\nbody\n")
    _skill(workspace / ".claude" / "skills", "weird", "---\nname: bad​name\n---\nbody\n")
    mgr = SkillManager(workspace)
    assert mgr.skills["plain"].description == "Heading line"
    assert "weird" in mgr.skills and not any("​" in k for k in mgr.skills)
    assert any("not allowed" in p for p in mgr.problems)


def test_reserved_folder_names_do_not_load(workspace: Path) -> None:
    root = workspace / ".claude" / "skills"
    _skill(root, "synced", "---\nname: synced\n---\nx\n")
    _skill(root, "anthropic-skills", "---\nname: anthropic-skills\n---\nx\n")
    _skill(root, "ok", "---\nname: ok\n---\nx\n")
    mgr = SkillManager(workspace)
    assert "ok" in mgr.skills and "synced" not in mgr.skills and "anthropic-skills" not in mgr.skills


def test_frontmatter_must_open_on_line_one(workspace: Path) -> None:
    fm, body = parse_frontmatter("\n---\nname: x\n---\nbody")
    assert fm == {} and "name: x" in body


# ─── discovery, precedence, reload ──────────────────────────────────────────

def test_personal_skill_beats_project_skill(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "deploy", "---\nname: deploy\ndescription: project\n---\nP\n")
    _skill(Path.home() / ".claude" / "skills", "deploy", "---\nname: deploy\ndescription: personal\n---\nU\n")
    assert SkillManager(workspace).skills["deploy"].description == "personal"


def test_parent_directories_up_to_the_repo_root_load(workspace: Path) -> None:
    (workspace / ".git").mkdir()
    sub = workspace / "packages" / "web"
    sub.mkdir(parents=True)
    _skill(workspace / ".claude" / "skills", "root-skill", "---\nname: root-skill\n---\nr\n")
    _skill(sub / ".claude" / "skills", "near", "---\nname: near\n---\nn\n")
    mgr = SkillManager(sub)
    assert {"root-skill", "near"} <= set(mgr.skills)


def test_nested_skills_load_when_a_file_is_touched(workspace: Path) -> None:
    web = workspace / "apps" / "web"
    _skill(workspace / ".claude" / "skills", "deploy", "---\nname: deploy\ndescription: root\n---\nroot\n")
    _skill(web / ".claude" / "skills", "deploy", "---\nname: deploy\ndescription: web\n---\nweb\n")
    _skill(web / ".claude" / "skills", "only-web", "---\nname: only-web\n---\nw\n")
    (web / "src").mkdir(parents=True)
    mgr = SkillManager(workspace)
    assert "only-web" not in mgr.skills
    added = mgr.load_nested_for(web / "src" / "a.py")
    assert {s.name for s in added} == {"apps/web:deploy", "only-web"}
    assert mgr.skills["deploy"].description == "root"
    mgr.discover()                                              # a rescan keeps what was loaded on demand
    assert "only-web" in mgr.skills


def test_added_directory_skill_gets_prefix_on_clash(workspace: Path, tmp_path: Path) -> None:
    other = tmp_path / "cli"
    _skill(workspace / ".claude" / "skills", "verify", "---\nname: verify\n---\nmine\n")
    _skill(other / ".claude" / "skills", "verify", "---\nname: verify\n---\ntheirs\n")
    _skill(other / ".claude" / "skills", "extra", "---\nname: extra\n---\nx\n")
    mgr = SkillManager(workspace)
    mgr.add_directory(other)
    assert mgr.skills["verify"].instructions == "mine" and mgr.skills["cli:verify"].instructions == "theirs"
    assert "extra" in mgr.skills


def test_live_reload_picks_up_new_edited_and_removed_skills(workspace: Path) -> None:
    root = workspace / ".claude" / "skills"
    root.mkdir(parents=True)
    mgr = SkillManager(workspace)
    assert "late" not in mgr.skills
    p = _skill(root, "late", "---\nname: late\ndescription: v1\n---\nb\n")
    mgr._checked_at = 0
    assert mgr.skills["late"].description == "v1"
    p.write_text("---\nname: late\ndescription: v2\n---\nb\n", encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    assert mgr.refresh_if_changed() and mgr.skills["late"].description == "v2"
    p.unlink()
    p.parent.rmdir()
    assert mgr.refresh_if_changed() and "late" not in mgr.skills


def test_symlinked_skill_folder_loads(workspace: Path, tmp_path: Path) -> None:
    real = tmp_path / "shared" / "linked"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: linked\n---\nL\n", encoding="utf-8")
    (workspace / ".claude" / "skills").mkdir(parents=True)
    try:
        (workspace / ".claude" / "skills" / "linked").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert "linked" in SkillManager(workspace).skills


def test_skill_overrides_and_disable_bundled(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "legacy", "---\nname: legacy\ndescription: old stuff\n---\nL\n")
    _skill(workspace / ".claude" / "skills", "hidden", "---\nname: hidden\n---\nH\n")
    _skill(workspace / ".claude" / "skills", "useronly", "---\nname: useronly\ndescription: manual\n---\nU\n")
    (workspace / ".claude" / "settings.json").write_text(json.dumps({
        "skillOverrides": {"legacy": "name-only", "hidden": "off", "useronly": "user-invocable-only"}}), encoding="utf-8")
    mgr = SkillManager(workspace)
    assert "hidden" not in mgr.skills and mgr.skills["useronly"].disable_model_invocation
    listing = skills_prompt_section(list(mgr.skills.values()))
    assert "- legacy\n" in listing + "\n" and "old stuff" not in listing and "useronly" not in listing
    (workspace / ".claude" / "settings.json").write_text('{"disableBundledSkills": true}', encoding="utf-8")
    mgr = SkillManager(workspace)
    assert "debug" not in mgr.skills and "legacy" in mgr.skills


def test_listing_caps_description_and_when_to_use(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "long", "---\nname: long\ndescription: " + "d" * 2000 + "\nwhen_to_use: later\n---\nL\n")
    sk = SkillManager(workspace).skills["long"]
    line = [ln for ln in skills_prompt_section([sk]).splitlines() if ln.startswith("- long")][0]
    assert len(line) < 1600 and line.endswith("…")
    (workspace / ".claude" / "settings.json").write_text('{"skillListingMaxDescChars": 50}', encoding="utf-8")
    line = [ln for ln in skills_prompt_section([sk], workspace).splitlines() if ln.startswith("- long")][0]
    assert len(line) < 70


def test_new_bundled_skills_exist(workspace: Path) -> None:
    mgr = SkillManager(workspace)
    for name in ("batch", "update-config", "fewer-permission-prompts", "run", "run-skill-generator"):
        assert mgr.skills[name].scope == "bundled"
    assert mgr.skills["batch"].disable_model_invocation


# ─── plugin skills & components ─────────────────────────────────────────────

def test_plugin_skills_are_namespaced_and_bare_name_resolves(workspace: Path) -> None:
    pd = _plugin(workspace / ".axon" / "plugins", "demo", {"name": "demo"})
    _skill(pd / "skills", "review", "---\nname: fancy\ndescription: plugin skill\n---\nvars ${CLAUDE_PLUGIN_ROOT}|${CLAUDE_SKILL_DIR}\n")
    mgr = SkillManager(workspace)
    assert "demo:fancy" in mgr.skills and "fancy" not in mgr.skills
    assert mgr.get("fancy") is mgr.skills["demo:fancy"]
    out = mgr.execute_skill("demo:fancy")
    assert str(pd) in out and str(pd / "skills" / "review") in out


def test_plugin_root_skill_and_manifest_skill_paths(workspace: Path) -> None:
    single = _plugin(workspace / ".axon" / "plugins", "single", {"name": "single"})
    (single / "SKILL.md").write_text("---\nname: only\n---\nO\n", encoding="utf-8")
    multi = _plugin(workspace / ".axon" / "plugins", "multi", {"name": "multi", "skills": ["./extra-skills/"]})
    _skill(multi / "skills", "a", "---\nname: a\n---\nA\n")
    _skill(multi / "extra-skills", "b", "---\nname: b\n---\nB\n")
    mgr = SkillManager(workspace)
    assert {"single:only", "multi:a", "multi:b"} <= set(mgr.skills)


def test_plugin_without_manifest_uses_standard_layout(workspace: Path) -> None:
    from axon.commands.plugins import load_plugins
    d = workspace / ".axon" / "plugins" / "bare"
    (d / "commands").mkdir(parents=True)
    (d / "commands" / "hi.md").write_text("Say hi\n", encoding="utf-8")
    plugins = load_plugins(workspace)
    assert [p.name for p in plugins] == ["bare"] and plugins[0].components()["commands"] == 1


def test_manifest_commands_object_map_and_replacement(workspace: Path) -> None:
    from axon.commands.custom import load_custom_commands
    d = _plugin(workspace / ".axon" / "plugins", "tools", {
        "name": "tools", "commands": {
            "status": {"source": "./extras/status.md", "argumentHint": "[env]", "allowedTools": ["Bash(git status *)"]},
            "about": {"content": "Explain ${CLAUDE_PLUGIN_ROOT}", "description": "Describe this plugin"}}})
    (d / "extras").mkdir()
    (d / "extras" / "status.md").write_text("---\ndescription: Status\n---\nStatus of $0\n", encoding="utf-8")
    (d / "commands").mkdir()
    (d / "commands" / "ignored.md").write_text("never\n", encoding="utf-8")   # replaced by the manifest key
    cmds = load_custom_commands(workspace)
    assert set(cmds) == {"tools:status", "tools:about"}
    assert cmds["tools:status"].argument_hint == "[env]" and cmds["tools:status"].allowed_tools == ["Bash(git status *)"]
    assert cmds["tools:about"].description == "Describe this plugin"


def test_commands_accept_skill_fields_and_user_invocable_false(workspace: Path) -> None:
    from axon.commands.custom import load_custom_commands
    d = workspace / ".axon" / "commands"
    d.mkdir(parents=True)
    (d / "fx.md").write_text("---\narguments: [issue]\neffort: low\ncontext: fork\nagent: Explore\nbackground: false\n"
                             "disallowed-tools: WebFetch\n---\nFix $issue\n", encoding="utf-8")
    (d / "hid.md").write_text("---\nuser-invocable: false\n---\nx\n", encoding="utf-8")
    cmds = load_custom_commands(workspace)
    c = cmds["fx"]
    assert (c.arguments, c.effort, c.context, c.agent, c.background, c.disallowed_tools) == (["issue"], "low", "fork", "Explore", False, ["WebFetch"])
    assert "hid" not in cmds


def test_plugin_agents_outputstyles_hooks_and_mcp_forms(workspace: Path) -> None:
    from axon.commands.plugins import (load_plugins, plugin_agent_files, plugin_hooks, plugin_mcp_servers,
                                       plugin_output_style_files)
    d = _plugin(workspace / ".axon" / "plugins", "multi", {
        "name": "multi", "agents": ["./custom/reviewer.md"], "outputStyles": "./styles/",
        "hooks": ["./cfg/extra.json", {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/fmt.sh"}]}]}],
        "mcpServers": {"inline": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/s.js"]}}})
    (d / "agents").mkdir()
    (d / "agents" / "default.md").write_text("---\nname: default\n---\nx\n", encoding="utf-8")   # replaced
    (d / "custom").mkdir()
    (d / "custom" / "reviewer.md").write_text("---\nname: reviewer\n---\nx\n", encoding="utf-8")
    (d / "styles").mkdir()
    (d / "styles" / "terse.md").write_text("Be terse.\n", encoding="utf-8")
    (d / "cfg").mkdir()
    (d / "cfg" / "extra.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}), encoding="utf-8")
    (d / "hooks").mkdir()
    (d / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": []}]}}), encoding="utf-8")
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"file": {"command": "x"}, "inline": {"command": "old"}}}), encoding="utf-8")
    p = load_plugins(workspace)[0]
    assert [f.name for f in plugin_agent_files(p)] == ["reviewer.md"]
    assert [f.name for f in plugin_output_style_files(p)] == ["terse.md"]
    hooks = plugin_hooks(p)
    assert set(hooks) == {"PostToolUse", "PreToolUse"} and len(hooks["PostToolUse"]) == 2
    assert hooks["PostToolUse"][1]["hooks"][0]["command"] == f"{d}/fmt.sh"
    servers = plugin_mcp_servers(p)
    assert servers["file"]["command"] == "x" and servers["inline"]["command"] == "node"   # later declaration wins


def test_default_enabled_false_and_enabled_plugins_setting(workspace: Path) -> None:
    from axon.commands.plugins import load_plugins
    _plugin(workspace / ".axon" / "plugins", "optional", {"name": "optional", "defaultEnabled": False})
    assert load_plugins(workspace)[0].enabled is False
    (workspace / ".axon").mkdir(exist_ok=True)
    (workspace / ".axon" / "settings.json").write_text(json.dumps({"enabledPlugins": {"optional": True}}), encoding="utf-8")
    assert load_plugins(workspace)[0].enabled is True


def test_user_config_values_placeholders_and_expansion(workspace: Path) -> None:
    from axon.commands.plugins import load_plugins, plugin_mcp_servers, plugin_user_config_values, save_user_config
    d = _plugin(workspace / ".axon" / "plugins", "cfg", {
        "name": "cfg",
        "mcpServers": {"srv": {"command": "x", "env": {"TOKEN": "${user_config.api_token}", "URL": "${user_config.endpoint}"}}},
        "userConfig": {
            "endpoint": {"type": "string", "title": "Endpoint", "description": "d", "default": "https://a"},
            "api_token": {"type": "string", "title": "Token", "description": "d", "sensitive": True},
            "tone": {"type": "string", "title": "Tone", "description": "d", "options": ["warm", "formal"]}}})
    p = load_plugins(workspace)[0]
    assert save_user_config(p, workspace, {"api_token": "s3cret", "endpoint": "https://b", "tone": "loud"}) == ["tone: must be one of warm, formal"]
    vals = plugin_user_config_values(p, workspace)
    assert vals["api_token"] == "s3cret" and vals["endpoint"] == "https://b"
    safe = plugin_user_config_values(p, workspace, include_sensitive=False)
    assert "s3cret" not in safe["api_token"]
    env = plugin_mcp_servers(p, workspace)["srv"]["env"]
    assert env == {"TOKEN": "s3cret", "URL": "https://b"}
    secrets = Path.home() / ".axon" / "plugins" / "secrets.json"
    assert secrets.exists() and d.exists()


def test_plugin_data_dir_variable(workspace: Path) -> None:
    from axon.commands.plugins import expand_plugin_root, load_plugins
    _plugin(workspace / ".axon" / "plugins", "dat", {"name": "dat"})
    p = load_plugins(workspace)[0]
    out = expand_plugin_root("${CLAUDE_PLUGIN_DATA}/n ${CLAUDE_PROJECT_DIR}", p.path, plugin=p, workspace=workspace)
    assert out.startswith(str(Path.home() / ".axon" / "plugins" / "data" / "dat")) and out.endswith(str(workspace))


# ─── validate ───────────────────────────────────────────────────────────────

def test_validate_plugin_reports_problems(tmp_path: Path) -> None:
    from axon.commands.plugins import validate_path
    d = tmp_path / "bad"
    d.mkdir()
    (d / "plugin.json").write_text(json.dumps({
        "name": "claude-helper", "bogus": 1, "skills": ["./nope/"], "agents": ["agents/a.md"],
        "commands": {"x": {"source": "./a.md", "content": "both"}}, "hooks": ["./h.json"],
        "userConfig": {"k": {"type": "string", "title": "t", "description": "d", "weird": 1}}}), encoding="utf-8")
    (d / "h.json").write_text(json.dumps({"PreToolUse": []}), encoding="utf-8")
    kind, errors, warnings = validate_path(d)
    text = "\n".join(errors)
    assert kind == "plugin"
    assert "reserved" in text and "skills path not found" in text and 'must start with "./"' in text
    assert "exactly one of source or content" in text and 'top-level "hooks" key' in text and "unknown key 'weird'" in text
    assert any("Unknown field 'bogus'" in w for w in warnings)


def test_validate_good_plugin_and_skills_dir(tmp_path: Path) -> None:
    from axon.commands.plugins import validate_path
    d = tmp_path / "good"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "good", "version": "1.0.0", "description": "d", "author": {"name": "me"}}), encoding="utf-8")
    _skill(d / "skills", "s", "---\nname: s\n---\nok\n")
    assert validate_path(d)[1:] == ([], [])
    sk = tmp_path / "skills"
    _skill(sk, "late-frontmatter", "intro\n---\nname: late-frontmatter\ndescription: x\n---\nbody\n")
    kind, errors, _ = validate_path(sk)
    assert kind == "skills" and "first line" in errors[0]


# ─── marketplaces ───────────────────────────────────────────────────────────

def _marketplace(root: Path, plugins: list[dict], name: str = "my-market") -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": name, "owner": {"name": "Me"}, "plugins": plugins}), encoding="utf-8")
    return root


def _market_plugin(root: Path, name: str, version: str = "1.0.0", deps: list | None = None) -> Path:
    d = root / "plugins" / name
    (d / "commands").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": version, "description": f"{name} plugin"}
    if deps:
        manifest["dependencies"] = deps
    (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "commands" / "hello.md").write_text("Hello $ARGUMENTS\n", encoding="utf-8")
    return d


def test_parse_source_kinds(tmp_path: Path) -> None:
    from axon.commands.marketplace import MarketplaceError, parse_source
    assert parse_source("anthropics/claude-code#v1") == {"kind": "github", "repo": "anthropics/claude-code", "ref": "v1"}
    assert parse_source("https://gitlab.example.com/g/m.git#v1.0.0") == {"kind": "git", "url": "https://gitlab.example.com/g/m.git", "ref": "v1.0.0"}
    assert parse_source("https://example.com/marketplace.json") == {"kind": "url", "url": "https://example.com/marketplace.json"}
    assert parse_source("git@github.com:o/r.git")["kind"] == "git"
    assert parse_source(str(tmp_path))["kind"] == "path"
    with pytest.raises(MarketplaceError):
        parse_source("http://example.com/marketplace.json")
    with pytest.raises(MarketplaceError):
        parse_source("not a source")


def test_marketplace_install_enable_update_uninstall(workspace: Path, tmp_path: Path) -> None:
    from axon.commands import marketplace as mk
    from axon.commands.custom import invalidate_cache, load_custom_commands
    from axon.commands.plugins import load_plugins
    root = tmp_path / "market"
    _market_plugin(root, "first")
    _marketplace(root, [{"name": "first", "source": "./plugins/first", "description": "the first"}])
    name, _ = mk.add_marketplace(str(root))
    assert name == "my-market" and [n for n, _ in mk.list_marketplaces()] == ["my-market"]
    plugin, deps = mk.install("first@my-market", workspace, "project")
    assert deps == [] and plugin.id == "first@my-market" and plugin.path == workspace / ".axon" / "plugins" / "first"
    settings = json.loads((workspace / ".axon" / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"] == {"first@my-market": True}
    invalidate_cache()
    assert "first:hello" in load_custom_commands(workspace)
    with pytest.raises(mk.MarketplaceError, match="already installed"):
        mk.install("first@my-market", workspace, "project")
    # update picks up a new version of the source
    _market_plugin(root, "first", "2.0.0")
    assert mk.update_plugin(load_plugins(workspace)[0], workspace) is True
    assert load_plugins(workspace)[0].version == "2.0.0"
    assert mk.update_plugin(load_plugins(workspace)[0], workspace) is False
    # disable through settings
    from axon.commands.plugins import set_enabled
    set_enabled("first@my-market", False, workspace, "project")
    assert load_plugins(workspace)[0].enabled is False
    mk.uninstall(load_plugins(workspace)[0], workspace)
    assert load_plugins(workspace) == []
    assert "enabledPlugins" not in json.loads((workspace / ".axon" / "settings.json").read_text(encoding="utf-8"))


def test_marketplace_remove_uninstalls_its_plugins_and_dependencies_install(workspace: Path, tmp_path: Path) -> None:
    from axon.commands import marketplace as mk
    from axon.commands.plugins import load_plugins
    root = tmp_path / "market"
    _market_plugin(root, "base")
    _market_plugin(root, "top", deps=["base"])
    _marketplace(root, [{"name": "top", "source": "./plugins/top"}, {"name": "base", "source": "./plugins/base"}])
    mk.add_marketplace(str(root))
    plugin, deps = mk.install("top", workspace, "user")
    assert deps == ["base@my-market"] and {p.name for p in load_plugins(workspace)} == {"top", "base"}
    assert [p.name for p in mk.dependents_of(next(p for p in load_plugins(workspace) if p.name == "base"), workspace)] == ["top"]
    removed = mk.remove_marketplace("my-market", workspace)
    assert sorted(removed) == ["base@my-market", "top@my-market"] and load_plugins(workspace) == []
    assert mk.list_marketplaces() == []


def test_marketplace_rejects_bad_entries(workspace: Path, tmp_path: Path) -> None:
    from axon.commands import marketplace as mk
    root = tmp_path / "evil"
    _marketplace(root, [{"name": "esc", "source": "./../outside"}, {"name": "nosrc"}])
    errors, _ = mk.validate_marketplace(root)
    assert any('Path contains ".."' in e for e in errors) and any("source: required" in e for e in errors)
    with pytest.raises(mk.MarketplaceError):
        mk.add_marketplace(str(root))
    official = _marketplace(tmp_path / "official", [], name="claude-plugins-official")
    with pytest.raises(mk.MarketplaceError, match="reserved for official"):
        mk.add_marketplace(str(official))
    with pytest.raises(mk.MarketplaceError, match="is not added"):
        mk.install("ghost@nowhere", workspace, "user")


def test_marketplace_unsupported_source_types_are_reported(workspace: Path, tmp_path: Path) -> None:
    from axon.commands import marketplace as mk
    root = _marketplace(tmp_path / "m", [{"name": "n", "source": {"source": "npm", "package": "x"}}], name="m")
    mk.add_marketplace(str(root))
    with pytest.raises(mk.MarketplaceError, match="not supported"):
        mk.install("n@m", workspace, "user")


def test_entry_merges_into_manifest_without_plugin_json(workspace: Path, tmp_path: Path) -> None:
    from axon.commands import marketplace as mk
    from axon.commands.plugins import load_plugins
    root = tmp_path / "market"
    (root / "plugins" / "bare" / "skills" / "x").mkdir(parents=True)
    (root / "plugins" / "bare" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\nX\n", encoding="utf-8")
    _marketplace(root, [{"name": "bare", "source": "./plugins/bare", "displayName": "Bare One", "description": "from entry"}])
    mk.add_marketplace(str(root))
    p, _ = mk.install("bare", workspace, "user")
    assert p.display_name == "Bare One" and p.description == "from entry" and load_plugins(workspace)[0].components()["skills"] == 1


# ─── /plugin command end to end ─────────────────────────────────────────────

def test_plugin_command_flow(agent: Agent, workspace: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("axon.commands.plugin_cmds.confirm", lambda *a, **k: True)
    root = tmp_path / "market"
    _market_plugin(root, "greet")
    _marketplace(root, [{"name": "greet", "source": "./plugins/greet", "description": "greets"}])
    dispatch_command(f"/plugin marketplace add {root}", agent)
    assert "Successfully added marketplace" in capsys.readouterr().out
    dispatch_command("/plugin discover", agent)
    assert "greet" in capsys.readouterr().out
    dispatch_command("/plugin install greet@my-market --scope local", agent)
    out = capsys.readouterr().out
    assert "Installed" in out and "local" in out
    assert json.loads((workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))["enabledPlugins"] == {"greet@my-market": True}
    dispatch_command("/greet:hello Ada", agent)
    assert "Hello Ada" in _last_user(agent)
    dispatch_command("/plugin disable greet", agent)
    assert "Disabled" in capsys.readouterr().out
    dispatch_command("/plugin info greet@my-market", agent)
    assert "my-market" in capsys.readouterr().out
    dispatch_command(f"/plugin validate {root}", agent)
    assert "Validation passed" in capsys.readouterr().out
    dispatch_command("/plugin uninstall greet", agent)
    assert "Removed plugin greet" in capsys.readouterr().out
    dispatch_command("/plugin marketplace remove my-market", agent)
    assert "Removed marketplace" in capsys.readouterr().out


def test_plugin_install_with_marketplace_flag_adds_it_first(agent: Agent, workspace: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("axon.commands.plugin_cmds.confirm", lambda *a, **k: True)
    root = tmp_path / "market"
    _market_plugin(root, "solo")
    _marketplace(root, [{"name": "solo", "source": "./plugins/solo"}])
    dispatch_command(f"/plugin install solo --marketplace {root}", agent)
    out = capsys.readouterr().out
    assert "Added marketplace" in out and "Installed" in out


def test_plugin_configure_command(agent: Agent, workspace: Path, capsys) -> None:
    _plugin(workspace / ".axon" / "plugins", "opt", {"name": "opt", "userConfig": {
        "region": {"type": "string", "title": "Region", "description": "r", "default": "eu"}}})
    dispatch_command("/plugin configure opt region=us", agent)
    assert "Saved region" in capsys.readouterr().out
    dispatch_command("/plugin configure opt", agent)
    assert "us" in capsys.readouterr().out


# ─── run_skill: shell abort, fork, stacking, tool grants ────────────────────

def test_skill_with_failing_shell_is_not_run(agent: Agent, workspace: Path, capsys) -> None:
    _skill(workspace / ".claude" / "skills", "boom", "---\nname: boom\nallowed-tools: Bash(exit:*)\n---\nbefore !`exit 4` after\n")
    agent.skills.discover()
    before = len(agent.conversation.messages)
    dispatch_command("/boom", agent)
    assert len(agent.conversation.messages) == before and "not run" in capsys.readouterr().out


def test_stacked_skills_share_one_turn_and_trailing_arguments(agent: Agent, workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "write-tests", "---\nname: write-tests\n---\nTESTS for $0\n")
    _skill(workspace / ".claude" / "skills", "fix-issue", "---\nname: fix-issue\n---\nFIX $0\n")
    agent.skills.discover()
    dispatch_command("/write-tests /fix-issue 123", agent)
    sent = _last_user(agent)
    assert "TESTS for 123" in sent and "FIX 123" in sent


def test_user_invocable_false_skill_cannot_be_run(agent: Agent, workspace: Path, capsys) -> None:
    _skill(workspace / ".claude" / "skills", "bg", "---\nname: bg\nuser-invocable: false\ndescription: background\n---\nk\n")
    agent.skills.discover()
    before = len(agent.conversation.messages)
    dispatch_command("/bg", agent)
    assert len(agent.conversation.messages) == before


def test_fork_skill_runs_in_subagent(agent: Agent, workspace: Path, monkeypatch, capsys) -> None:
    _skill(workspace / ".claude" / "skills", "research", "---\nname: research\ncontext: fork\nagent: Explore\nbackground: false\n---\nResearch $ARGUMENTS\n")
    agent.skills.discover()
    calls: list[tuple] = []

    def fake_run_subagent(prompt, parent, max_iterations=None, subagent_type=None, description="", call_id=""):
        calls.append((prompt, subagent_type))
        return "SUBAGENT REPORT"
    monkeypatch.setattr("axon.agent.subagent.run_subagent", fake_run_subagent)
    before = len(agent.conversation.messages)
    dispatch_command("/research caching", agent)
    assert calls and "Research caching" in calls[0][0] and calls[0][1] == "Explore"
    assert "SUBAGENT REPORT" in capsys.readouterr().out and len(agent.conversation.messages) == before


def test_skill_effort_and_disallowed_tools_apply_for_the_turn(agent: Agent, workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "careful", "---\nname: careful\neffort: low\ndisallowed-tools: WebFetch\n---\ngo\n")
    agent.skills.discover()
    seen: dict = {}
    orig = agent.run_turn

    def spy(prompt):
        seen["effort"] = agent.settings.effort
        seen["deny"] = [str(e.rule) for e in agent.permissions.session_rules if e.kind == "deny"]
        return orig(prompt)
    agent.run_turn = spy  # type: ignore[method-assign]
    start_effort = agent.settings.effort
    dispatch_command("/careful", agent)
    assert seen["effort"] == "reflex" and any("WebFetch" in r for r in seen["deny"])
    assert agent.settings.effort == start_effort
    assert not [e for e in agent.permissions.session_rules if e.kind == "deny"]


def test_skill_tool_content_keeps_shell_unrun(workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "ctx", "---\nname: ctx\n---\nnow !`date`\n")
    assert "not permitted" in skill_content("ctx", workspace)


def test_skill_beats_command_of_same_name(agent: Agent, workspace: Path) -> None:
    (workspace / ".claude" / "commands").mkdir(parents=True)
    (workspace / ".claude" / "commands" / "twin.md").write_text("COMMAND body\n", encoding="utf-8")
    _skill(workspace / ".claude" / "skills", "twin", "---\nname: twin\n---\nSKILL body\n")
    agent.skills.discover()
    dispatch_command("/twin", agent)
    assert "SKILL body" in _last_user(agent)
    dispatch_command("/cmd:twin", agent)
    assert "COMMAND body" in _last_user(agent)


# ─── output styles ──────────────────────────────────────────────────────────

def test_output_style_builtins_and_keep_coding_default(workspace: Path) -> None:
    from axon.commands.output_styles import get_style, list_styles
    names = [s.name for s in list_styles(workspace)]
    assert names[:5] == ["default", "explanatory", "learning", "proactive", "concise"] or {"proactive", "concise"} <= set(names)
    assert get_style("Proactive", workspace).keep_coding_instructions is True
    d = workspace / ".claude" / "output-styles"
    d.mkdir(parents=True)
    (d / "diagrams.md").write_text("---\nname: Diagrams first\ndescription: Lead with a diagram\n---\nDraw first.\n", encoding="utf-8")
    (d / "kept.md").write_text("---\nkeep-coding-instructions: true\n---\nKeep.\n", encoding="utf-8")
    assert get_style("diagrams-first", workspace).keep_coding_instructions is False
    assert get_style("kept", workspace).keep_coding_instructions is True


def test_output_style_directories_nearest_wins_and_user_claude_dir(workspace: Path) -> None:
    from axon.commands.output_styles import get_style
    (workspace / ".git").mkdir()
    sub = workspace / "pkg"
    (sub / ".claude" / "output-styles").mkdir(parents=True)
    (workspace / ".claude" / "output-styles").mkdir(parents=True)
    (workspace / ".claude" / "output-styles" / "s.md").write_text("ROOT\n", encoding="utf-8")
    (sub / ".claude" / "output-styles" / "s.md").write_text("NEAR\n", encoding="utf-8")
    (Path.home() / ".claude" / "output-styles").mkdir(parents=True)
    (Path.home() / ".claude" / "output-styles" / "u.md").write_text("USER\n", encoding="utf-8")
    assert get_style("s", sub).prompt == "NEAR" and get_style("s", workspace).prompt == "ROOT"
    assert get_style("u", sub).source == "user"


def test_plugin_output_styles_and_force_for_plugin(workspace: Path) -> None:
    from axon.agent.prompt import output_style_block
    from axon.commands.output_styles import active_style_name, get_style
    d = _plugin(workspace / ".axon" / "plugins", "styled", {"name": "styled", "outputStyles": "./looks/"})
    (d / "looks").mkdir()
    (d / "looks" / "forced.md").write_text("---\nforce-for-plugin: true\n---\nALWAYS THIS\n", encoding="utf-8")
    (d / "looks" / "plain.md").write_text("PLAIN\n", encoding="utf-8")
    assert get_style("plain", workspace).source == "plugin:styled"
    s = Settings(workspace=workspace, output_style="concise")
    assert active_style_name(s) == "forced"
    text, keep = output_style_block(s)
    assert "ALWAYS THIS" in text and keep is False


# ─── memory ─────────────────────────────────────────────────────────────────

def test_user_claude_md_and_rules_are_loaded(workspace: Path) -> None:
    from axon.agent.memory import MemoryLoader
    (Path.home() / ".claude" / "rules").mkdir(parents=True)
    (Path.home() / ".claude" / "CLAUDE.md").write_text("USER-CLAUDE-MD\n", encoding="utf-8")
    (Path.home() / ".claude" / "rules" / "style.md").write_text("USER-RULE\n", encoding="utf-8")
    loader = MemoryLoader(workspace, auto_memory=False)
    text = "\n".join(n.content for n in loader.load_startup())
    assert "USER-CLAUDE-MD" in text and "USER-RULE" in text


def test_skill_hooks_frontmatter_registers_scoped_hooks(agent: Agent, workspace: Path) -> None:
    _skill(workspace / ".claude" / "skills", "guarded",
           "---\nname: guarded\nhooks:\n  PreToolUse:\n    - matcher: \"Bash\"\n      hooks:\n        - type: command\n"
           "          command: \"echo guard\"\n---\nGo\n")
    agent.skills.discover()
    dispatch_command("/guarded", agent)
    assert any(h["source"] == "scoped:skill:guarded" for h in agent.hooks.describe())
    dispatch_command("/guarded", agent)   # invoking again replaces, never duplicates
    assert len([h for h in agent.hooks.describe() if h["source"] == "scoped:skill:guarded"]) == 1
