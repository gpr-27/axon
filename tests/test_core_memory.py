"""Hierarchical memory: load order, imports, rules, on-demand loading, auto memory, MemoryStore."""
from __future__ import annotations
import json
from pathlib import Path

from axon.agent.memory import (
    AUTO_MEMORY_MAX_LINES, AutoMemory, MemoryLoader, MemoryStore, auto_memory_dir, check_memory_index,
    compact_instructions, expand_braces, path_matches, render_memory_context, strip_block_comments,
)
from axon.agent.prompt import build_system, discover_project_context
from axon.tools import create_default_registry
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_load_order_user_then_project_chain_then_local(workspace: Path, isolate_user_home: Path) -> None:
    _w(isolate_user_home / ".axon" / "AXON.md", "USER-RULE")
    _w(isolate_user_home / ".axon" / "rules" / "style.md", "USER-STYLE")
    (workspace / ".git").mkdir()
    _w(workspace / "AXON.md", "ROOT-AXON")
    _w(workspace / "CLAUDE.md", "ROOT-CLAUDE")
    _w(workspace / "AXON.local.md", "ROOT-LOCAL")
    _w(workspace / ".axon" / "rules" / "testing.md", "PROJECT-RULE")
    sub = workspace / "pkg"
    _w(sub / "AXON.md", "PKG-AXON")
    loader = MemoryLoader(sub)
    loader.load_startup()
    text = loader.render()
    order = [text.index(t) for t in ("USER-RULE", "USER-STYLE", "ROOT-AXON", "ROOT-CLAUDE", "PROJECT-RULE", "ROOT-LOCAL", "PKG-AXON")]
    assert order == sorted(order)


def test_agents_md_only_without_axon_or_claude(workspace: Path) -> None:
    _w(workspace / "AGENTS.md", "AGENTS-ONLY")
    assert "AGENTS-ONLY" in discover_project_context(workspace)
    _w(workspace / "AXON.md", "AXON-PRESENT")
    ctx = discover_project_context(workspace)
    assert "AXON-PRESENT" in ctx and "AGENTS-ONLY" not in ctx
    loader = MemoryLoader(workspace, instruction_files="axon-md-and-agents-md")
    loader.load_startup(include_user=False, include_managed=False)
    assert "AGENTS-ONLY" in loader.render()


def test_imports_depth_cycles_code_and_quotes(workspace: Path) -> None:
    _w(workspace / "AXON.md", "Main @docs/a.md\n`@ignored.md`\n```\n@fenced.md\n```\n\"@quoted.md\"\nmail me@example.com")
    _w(workspace / "docs" / "a.md", "A-CONTENT @b.md")
    _w(workspace / "docs" / "b.md", "B-CONTENT @a.md @c.md")  # cycle back to a
    _w(workspace / "docs" / "c.md", "C-CONTENT @d.md")
    _w(workspace / "docs" / "d.md", "D-CONTENT @e.md")
    _w(workspace / "docs" / "e.md", "E-CONTENT")
    for name in ("ignored.md", "fenced.md", "quoted.md", "example.com"):
        _w(workspace / name, f"SHOULD-NOT-LOAD {name}")
    ctx = discover_project_context(workspace)
    for t in ("A-CONTENT", "B-CONTENT", "C-CONTENT", "D-CONTENT"):
        assert t in ctx
    assert "E-CONTENT" not in ctx  # max 4 hops
    assert "SHOULD-NOT-LOAD" not in ctx
    assert ctx.count("A-CONTENT") == 1


def test_block_comments_stripped_outside_code() -> None:
    text = "keep <!-- hidden --> me\n```\n<!-- code comment -->\n```\n<!--\nmulti\n-->end"
    out = strip_block_comments(text)
    assert "hidden" not in out and "multi" not in out and "<!-- code comment -->" in out and "end" in out


def test_globs_and_braces() -> None:
    assert expand_braces("src/{a,b}/*.py") == ["src/a/*.py", "src/b/*.py"]
    assert path_matches("src/api/v1/routes.py", ["src/**/*.py"])
    assert path_matches("src/x.ts", ["src/*.{ts,tsx}"])
    assert not path_matches("lib/x.ts", ["src/**"])
    assert not path_matches("src/x.py", ["src/[abc.py"])  # invalid [ matches nothing


def test_path_scoped_rule_and_nested_axon_md_load_on_demand(workspace: Path) -> None:
    _w(workspace / ".axon" / "rules" / "api.md", "---\npaths:\n  - \"src/api/**/*.py\"\n---\nAPI-RULE: validate input")
    _w(workspace / "src" / "api" / "AXON.md", "NESTED-API-GUIDE")
    _w(workspace / "src" / "api" / "v1.py", "x = 1\n")
    loader = MemoryLoader(workspace)
    loader.load_startup(include_user=False, include_managed=False)
    assert "API-RULE" not in loader.render() and "NESTED-API-GUIDE" not in loader.render()
    new = loader.on_file_access(workspace / "src" / "api" / "v1.py")
    reasons = {n.reason for n in new}
    assert reasons == {"on-demand", "path-rule"}
    assert "frontmatter" not in loader.render() and "paths:" not in loader.render(new)
    assert loader.on_file_access(workspace / "src" / "api" / "v1.py") == []  # once only


def test_on_demand_memory_injected_into_tool_result(workspace: Path) -> None:
    _w(workspace / ".axon" / "rules" / "py.md", "---\npaths: \"**/*.py\"\n---\nPY-RULE: use type hints")
    _w(workspace / "m.py", "print(1)\n")
    prov = FakeProvider(scripted(("Read", {"path": "m.py"}), "ok"))
    make_agent(prov, workspace).run_turn("read it")
    assert "PY-RULE: use type hints" in prov.requests[1]["messages"][-1]["content"][0]["content"]
    assert "PY-RULE" not in json.dumps(prov.requests[0]["system"])


def test_memory_read_once_and_kept_stable(workspace: Path) -> None:
    _w(workspace / "AXON.md", "VERSION-ONE")
    prov = FakeProvider(scripted("a", "b"))
    agent = make_agent(prov, workspace)
    agent.run_turn("1")
    _w(workspace / "AXON.md", "VERSION-TWO")
    agent.run_turn("2")
    assert prov.requests[0]["system"] == prov.requests[1]["system"]  # cache-stable within the session
    agent.reload_memory()
    assert "VERSION-TWO" in agent.memory_text()


def test_auto_memory_index_caps(workspace: Path) -> None:
    am = AutoMemory(workspace)
    topic, warn = am.save("Deploy process", "Use make deploy; never deploy on Fridays.", "project")
    assert topic.is_file() and warn is None
    assert "type: project" in topic.read_text(encoding="utf-8")
    idx = auto_memory_dir(workspace) / "MEMORY.md"
    assert "Deploy process" in idx.read_text(encoding="utf-8")
    assert str(auto_memory_dir(workspace)).startswith(str(Path.home()))
    loader = MemoryLoader(workspace)
    loader.load_startup()
    assert "Deploy process" in loader.render()
    idx.write_text("\n".join(f"- line {i}" for i in range(AUTO_MEMORY_MAX_LINES + 50)), encoding="utf-8")
    assert check_memory_index(idx).startswith("Error")
    loader = MemoryLoader(workspace)
    loader.load_startup()
    assert "line 199" in loader.render() and "line 200" not in loader.render()


def test_memory_store_add_scopes_and_escaping(workspace: Path) -> None:
    store = MemoryStore(workspace)
    item = store.add('# Prefer "pytest -q" for tests', scope="project")
    assert (workspace / ".axon" / "memory" / f"{item.id}.md").is_file()
    g = store.add("I like tabs", scope="user")
    assert g.scope == "global" and not (workspace / ".axon" / "memory" / f"{g.id}.md").exists()
    loaded = {i.id: i for i in store.list_all()}
    assert loaded[item.id].title == 'Prefer "pytest -q" for tests'
    assert store.delete("../../etc/passwd") is False
    assert store.delete(item.id) is True


def test_memory_store_does_not_create_dirs_on_read(workspace: Path) -> None:
    MemoryStore(workspace).list_all()
    assert not (workspace / ".axon").exists()


def test_system_prompt_contains_memory_and_saved_items(workspace: Path) -> None:
    _w(workspace / "AXON.md", "Always use uv.")
    MemoryStore(workspace).add("Database is Postgres 16")
    text = "\n".join(b["text"] for b in build_system(make_settings(workspace), create_default_registry()))
    assert "Always use uv." in text and "Database is Postgres 16" in text
    assert "Saved memories" in render_memory_context(workspace)


def test_compact_instructions_section() -> None:
    text = "# Guide\n## Compact Instructions\nKeep test names.\n## Other\nx"
    assert compact_instructions(text) == "Keep test names."
