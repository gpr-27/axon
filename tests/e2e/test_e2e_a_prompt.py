"""
E2E (agent A): what ends up in the system prompt / request - memory files (AXON.md, @imports, CLAUDE.md,
AGENTS.md fallback, ~/.axon/AXON.md, rules), output styles, --append-system-prompt(-file),
--system-prompt(-file), --tools / --disallowedTools and @file mentions - plus "nothing auto-written".
"""
from __future__ import annotations

import json

import pytest

from harness import AxonEnv
from test_e2e_a_common import no_traceback, write_settings

pytestmark = pytest.mark.e2e


def _sys(env: AxonEnv, *args: str, prompt: str = "hi") -> str:
    env.llm.enqueue([{"text": "ok"}])
    r = env.run(["-p", *args, prompt])
    assert r.code == 0, str(r)
    no_traceback(r)
    return env.llm.completion_requests()[-1].system_text


def _ws_files(env: AxonEnv) -> list[str]:
    return sorted(str(p.relative_to(env.workspace)) for p in env.workspace.rglob("*"))


# ─── memory ─────────────────────────────────────────────────────────────────

def test_axon_md_loaded_into_system_prompt(axon_env: AxonEnv) -> None:
    """Project AXON.md content appears in the system prompt."""
    (axon_env.workspace / "AXON.md").write_text("Always use tabs. AXON-MEM-1\n", encoding="utf-8")
    assert "AXON-MEM-1" in _sys(axon_env)


def test_axon_md_import_followed(axon_env: AxonEnv) -> None:
    """@path imports inside AXON.md are inlined (nested up to the hop limit)."""
    ws = axon_env.workspace
    (ws / "docs").mkdir()
    (ws / "AXON.md").write_text("Root rules\n@docs/a.md\n", encoding="utf-8")
    (ws / "docs" / "a.md").write_text("IMPORTED-A\n@b.md\n", encoding="utf-8")
    (ws / "docs" / "b.md").write_text("IMPORTED-B-NESTED\n", encoding="utf-8")
    s = _sys(axon_env)
    assert "IMPORTED-A" in s and "IMPORTED-B-NESTED" in s


def test_import_inside_code_fence_is_ignored(axon_env: AxonEnv) -> None:
    """An @path inside a fenced code block is not treated as an import."""
    ws = axon_env.workspace
    (ws / "secret.md").write_text("FENCED-IMPORT-LEAK\n", encoding="utf-8")
    (ws / "AXON.md").write_text("Rules\n```\n@secret.md\n```\n", encoding="utf-8")
    assert "FENCED-IMPORT-LEAK" not in _sys(axon_env)


def test_claude_md_loaded_alongside_axon_md(axon_env: AxonEnv) -> None:
    """CLAUDE.md is loaded as well (Claude Code compatibility)."""
    (axon_env.workspace / "AXON.md").write_text("AXON-PART\n", encoding="utf-8")
    (axon_env.workspace / "CLAUDE.md").write_text("CLAUDE-PART\n", encoding="utf-8")
    s = _sys(axon_env)
    assert "AXON-PART" in s and "CLAUDE-PART" in s


def test_agents_md_is_fallback_only(axon_env: AxonEnv) -> None:
    """AGENTS.md is used when no AXON.md/CLAUDE.md exists, and ignored when one does."""
    (axon_env.workspace / "AGENTS.md").write_text("AGENTS-FALLBACK-MARK\n", encoding="utf-8")
    assert "AGENTS-FALLBACK-MARK" in _sys(axon_env)
    (axon_env.workspace / "AXON.md").write_text("PRIMARY\n", encoding="utf-8")
    s = _sys(axon_env)
    assert "PRIMARY" in s and "AGENTS-FALLBACK-MARK" not in s


def test_user_level_axon_md_loaded(axon_env: AxonEnv) -> None:
    """~/.axon/AXON.md (under AXON_HOME) is loaded for every project."""
    (axon_env.axon_home / "AXON.md").write_text("USER-GLOBAL-MEM\n", encoding="utf-8")
    assert "USER-GLOBAL-MEM" in _sys(axon_env)


def test_project_rules_dir_loaded(axon_env: AxonEnv) -> None:
    """Unconditional .axon/rules/*.md files are loaded."""
    d = axon_env.workspace / ".axon" / "rules"
    d.mkdir(parents=True)
    (d / "style.md").write_text("RULE-FILE-MARK\n", encoding="utf-8")
    assert "RULE-FILE-MARK" in _sys(axon_env)


def test_html_comments_stripped_from_memory(axon_env: AxonEnv) -> None:
    """Block-level <!-- comments --> in AXON.md are not sent to the model."""
    (axon_env.workspace / "AXON.md").write_text("VISIBLE-RULE\n<!-- HIDDEN-COMMENT -->\n", encoding="utf-8")
    s = _sys(axon_env)
    assert "VISIBLE-RULE" in s and "HIDDEN-COMMENT" not in s


def test_memory_never_auto_written(axon_env: AxonEnv) -> None:
    """A normal run (with tools) creates nothing in the workspace beyond what tools wrote, and no auto memory
    files under AXON_HOME."""
    (axon_env.workspace / "AXON.md").write_text("rules\n", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "out.txt", "content": "x"}}], [{"text": "remember: I like tabs"}])
    r = axon_env.run(["-p", "remember that I prefer tabs"])
    assert r.code == 0, str(r)
    assert _ws_files(axon_env) == ["AXON.md", "out.txt"]
    assert (axon_env.workspace / "AXON.md").read_text(encoding="utf-8") == "rules\n"
    mem_files = [p for p in axon_env.axon_home.rglob("*") if p.is_file() and "memory" in p.parts]
    assert mem_files == [], mem_files


def test_auto_memory_file_is_loaded(axon_env: AxonEnv) -> None:
    """An existing auto-memory MEMORY.md under ~/.axon/projects/<key>/memory is loaded into the prompt."""
    mem = axon_env.sessions_dir().parent / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text("AUTO-MEMORY-MARK\n", encoding="utf-8")
    assert "AUTO-MEMORY-MARK" in _sys(axon_env)


# ─── output styles ──────────────────────────────────────────────────────────

def test_builtin_output_style_in_system_prompt(axon_env: AxonEnv) -> None:
    """outputStyle=explanatory adds the Explanatory style section to the system prompt."""
    s = _sys(axon_env, "--settings", json.dumps({"outputStyle": "explanatory"}))
    assert "Output style: explanatory" in s or "Output style: Explanatory" in s


def test_default_output_style_adds_nothing(axon_env: AxonEnv) -> None:
    """The default style adds no style section."""
    assert "Output style:" not in _sys(axon_env)


def test_custom_output_style_file(axon_env: AxonEnv) -> None:
    """A custom style in .axon/output-styles/<name>.md is selectable and its body is injected."""
    d = axon_env.workspace / ".axon" / "output-styles"
    d.mkdir(parents=True)
    (d / "pirate.md").write_text("---\nname: pirate\ndescription: Talk like a pirate\n---\nPIRATE-STYLE-BODY arr\n",
                                 encoding="utf-8")
    write_settings(axon_env, {"outputStyle": "pirate"})
    s = _sys(axon_env)
    assert "PIRATE-STYLE-BODY" in s and "You are Axon" in s


def test_custom_style_without_coding_instructions(axon_env: AxonEnv) -> None:
    """keep-coding-instructions: false drops the operating rules but keeps the identity and the style."""
    d = axon_env.workspace / ".axon" / "output-styles"
    d.mkdir(parents=True)
    (d / "bare.md").write_text("---\nname: bare\ndescription: x\nkeep-coding-instructions: false\n---\nBARE-STYLE\n",
                               encoding="utf-8")
    full = _sys(axon_env)
    write_settings(axon_env, {"outputStyle": "bare"})
    bare = _sys(axon_env)
    assert "BARE-STYLE" in bare and len(bare) < len(full)


# ─── prompt flags ───────────────────────────────────────────────────────────

def test_append_system_prompt_is_last_block(axon_env: AxonEnv) -> None:
    """--append-system-prompt text is appended after the built-in sections."""
    s = _sys(axon_env, "--append-system-prompt", "APPEND-MARK-77")
    assert "APPEND-MARK-77" in s and "You are Axon" in s
    assert s.rstrip().endswith("APPEND-MARK-77")


def test_append_system_prompt_file(axon_env: AxonEnv) -> None:
    """--append-system-prompt-file reads the extra text from a file."""
    f = axon_env.root / "extra.txt"
    f.write_text("FILE-APPEND-MARK\n", encoding="utf-8")
    assert "FILE-APPEND-MARK" in _sys(axon_env, "--append-system-prompt-file", str(f))


def test_system_prompt_replaces_core_identity(axon_env: AxonEnv) -> None:
    """--system-prompt replaces Axon's identity/operating rules but keeps memory and tool policy layers."""
    (axon_env.workspace / "AXON.md").write_text("MEM-STILL-THERE\n", encoding="utf-8")
    s = _sys(axon_env, "--system-prompt", "You are TestBot. CUSTOM-CORE")
    assert "CUSTOM-CORE" in s and "You are Axon" not in s
    assert "MEM-STILL-THERE" in s


def test_system_prompt_file(axon_env: AxonEnv) -> None:
    """--system-prompt-file reads the replacement prompt from disk."""
    f = axon_env.root / "sp.txt"
    f.write_text("SP-FROM-FILE\n", encoding="utf-8")
    s = _sys(axon_env, "--system-prompt-file", str(f))
    assert "SP-FROM-FILE" in s and "You are Axon" not in s


def test_system_prompt_file_missing_is_clean_error(axon_env: AxonEnv) -> None:
    """A missing --system-prompt-file is a config error with no traceback and no LLM call."""
    r = axon_env.run(["-p", "--system-prompt-file", str(axon_env.root / "nope.txt"), "hi"])
    assert r.code != 0
    no_traceback(r)
    assert "system-prompt-file" in r.output and axon_env.llm.completion_requests() == []


def test_settings_append_system_prompt(axon_env: AxonEnv) -> None:
    """appendSystemPrompt in settings.json is honoured."""
    write_settings(axon_env, {"appendSystemPrompt": "SETTINGS-APPEND-MARK"})
    assert "SETTINGS-APPEND-MARK" in _sys(axon_env)


def test_tools_flag_restricts_schema(axon_env: AxonEnv) -> None:
    """--tools 'Read,Grep' advertises exactly those tools."""
    _sys(axon_env, "--tools", "Read,Grep")
    assert sorted(axon_env.llm.completion_requests()[-1].tool_names) == ["Grep", "Read"]


def test_tools_empty_disables_all(axon_env: AxonEnv) -> None:
    """--tools '' sends no tools at all."""
    _sys(axon_env, "--tools", "")
    assert axon_env.llm.completion_requests()[-1].tool_names == []


def test_tools_restricted_call_to_removed_tool_fails(axon_env: AxonEnv) -> None:
    """With --tools Read, a hallucinated Write call is rejected as unknown and nothing is written."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "x.txt", "content": "x"}}], [{"text": "ok"}])
    r = axon_env.run(["-p", "--tools", "Read", "--output-format", "stream-json", "go"])
    assert r.code == 0, str(r)
    res = [e for e in r.json_lines() if e["type"] == "tool_result"][0]
    assert res["is_error"] and not (axon_env.workspace / "x.txt").exists()


def test_disallowed_tools_removes_from_schema(axon_env: AxonEnv) -> None:
    """--disallowedTools Bash Write removes both tools from the request."""
    _sys(axon_env, "--disallowedTools", "Bash", "Write", "--")
    names = axon_env.llm.completion_requests()[-1].tool_names
    assert "Bash" not in names and "Write" not in names and "Edit" in names


def test_system_prompt_mentions_only_available_tools(axon_env: AxonEnv) -> None:
    """With --tools Read the subagents section (Task) is not advertised."""
    s = _sys(axon_env, "--tools", "Read")
    assert "general-purpose" not in s


def test_file_mention_inlined_in_prompt(axon_env: AxonEnv) -> None:
    """@file in the prompt inlines that file's content into the user message."""
    (axon_env.workspace / "notes.txt").write_text("MENTIONED-CONTENT-42\n", encoding="utf-8")
    _sys(axon_env, prompt="summarize @notes.txt please")
    u = axon_env.llm.completion_requests()[-1].last_user_text()
    assert "MENTIONED-CONTENT-42" in u


def test_environment_preamble_has_workspace(axon_env: AxonEnv) -> None:
    """The system prompt's environment section names the workspace directory."""
    s = _sys(axon_env)
    assert str(axon_env.workspace.name) in s


def test_system_prompt_stable_across_runs(axon_env: AxonEnv) -> None:
    """Two identical runs produce byte-identical system prompts and tool lists (prompt-cache friendly)."""
    a = _sys(axon_env)
    t1 = axon_env.llm.completion_requests()[-1].body["tools"]
    b = _sys(axon_env)
    t2 = axon_env.llm.completion_requests()[-1].body["tools"]
    assert a == b and t1 == t2
