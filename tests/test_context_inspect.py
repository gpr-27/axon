"""The context inspector: every section of the Context panel returns its full contents in one shape."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from test_dashboard import c, env, llm, server  # noqa: E402,F401
from axon.features import context_inspect as ci  # noqa: E402

FAKE_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
ITEM_KEYS = {"id", "title", "subtitle", "group", "tokens", "kind", "lang", "body", "body_truncated", "size_chars", "meta", "badges", "links", "explain"}
ORDER = ["messages", "system_prompt", "system_tools", "mcp_tools", "mcp_instructions", "skills", "custom_agents", "memory_files",
         "autocompact", "free", "permissions", "hooks"]


def _sid(c) -> str:
    return c.json("/api/chat/state?create=1")["session"]


def _inspect(c, sid: str, section: str) -> dict:
    st, _, body = c.post("/api/insights/ctxinspect", {"session": sid, "section": section})
    assert st == 200, (section, st, body[:300])
    return json.loads(body)


def _agent(c, sid: str):
    return c.srv.runtime(sid, create=False).agent


def _talk(agent, big: str = "R" * 5000) -> None:
    agent.conversation.messages.extend([
        {"role": "user", "content": "please read app.py"},
        {"role": "assistant", "content": [{"type": "text", "text": "Reading it."},
                                          {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "app.py", "limit": 50}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": big}]},
    ])


def test_every_section_returns_the_contract_shape(c):
    sid = _sid(c)
    _talk(_agent(c, sid))
    for key in ["overview"] + ORDER:
        d = _inspect(c, sid, key)
        assert d["section"] == key and d["title"] and len(d["intro"]) > 20 and d["estimate"] is True and d["window"] > 0
        assert [s["key"] for s in d["sections"]] == ORDER
        assert all(set(s) >= {"key", "label", "tokens", "pct", "count"} for s in d["sections"])
        assert {s["key"]: s["tokens"] for s in d["sections"]}["permissions"] is None
        assert {s["key"]: s["tokens"] for s in d["sections"]}["hooks"] is None
        assert d["items"] or key in ("mcp_tools", "mcp_instructions", "skills", "memory_files"), key
        assert d["items"] or d["note"].startswith("Nothing in"), key
        for it in d["items"]:
            assert ITEM_KEYS <= set(it), (key, it["id"])
            assert isinstance(it["body"], str) and it["size_chars"] >= len(it["body"]) and isinstance(it["meta"], list)
        assert len({it["id"] for it in d["items"]}) == len(d["items"]), key


def test_overview_has_one_item_per_section_with_numbers(c):
    d = _inspect(c, _sid(c), "overview")
    assert [i["id"] for i in d["items"]] == ["sec:" + k for k in ORDER]
    assert d["tokens"] > 0


def test_messages_include_the_full_tool_result_and_tool_call_json(c):
    sid = _sid(c)
    big = "line of output\n" * 3000                       # well over what a preview would hold
    _talk(_agent(c, sid), big)
    d = _inspect(c, sid, "messages")
    by_id = {i["id"]: i for i in d["items"]}
    res = by_id["toolu_1"]
    assert res["kind"] == "tool_result" and res["body"] == big and res["body_truncated"] is False
    assert {a["label"] for a in res["actions"]} >= {"Pin", "Drop"}
    drop = next(a for a in res["actions"] if a["label"] == "Drop")
    assert drop["api"] == "/api/insights/ctxmap_drop" and drop["payload"]["id"] == "toolu_1"
    call = next(i for i in d["items"] if i["kind"] == "message" and i.get("tool_calls"))
    assert call["tool_calls"][0]["name"] == "Read" and call["tool_calls"][0]["input"] == {"file_path": "app.py", "limit": 50}
    assert '"file_path": "app.py"' in call["body"] and "Reading it." in call["body"]
    assert any(i["body"] == "please read app.py" for i in d["items"])


def test_messages_handle_openai_style_tool_calls_and_pin_state(c):
    sid = _sid(c)
    agent = _agent(c, sid)
    agent.conversation.messages.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": "a.py\nb.py"},
    ])
    d = _inspect(c, sid, "messages")
    call = next(i for i in d["items"] if i.get("tool_calls"))
    assert call["tool_calls"][0]["input"] == {"command": "ls"}
    assert next(i for i in d["items"] if i["id"] == "call_9")["body"] == "a.py\nb.py"
    st, _, _ = c.post("/api/insights/pin", {"session": sid, "target": "call_9"})
    assert st == 200
    pinned = next(i for i in _inspect(c, sid, "messages")["items"] if i["id"] == "call_9")
    assert "pinned" in pinned["badges"] and {a["label"] for a in pinned["actions"]} == {"Unpin"}


def test_a_huge_body_is_capped_and_marked(c):
    sid = _sid(c)
    _talk(_agent(c, sid), "x" * (ci.BODY_CAP + 5000))
    res = next(i for i in _inspect(c, sid, "messages")["items"] if i["id"] == "toolu_1")
    assert len(res["body"]) == ci.BODY_CAP and res["body_truncated"] is True and res["size_chars"] == ci.BODY_CAP + 5000


def test_system_prompt_and_tools_show_exact_text_and_schemas(c):
    sid = _sid(c)
    sp = _inspect(c, sid, "system_prompt")
    assert sp["items"] and all(i["kind"] == "block" and i["body"] and i["tokens"] > 0 for i in sp["items"])
    tools = _inspect(c, sid, "system_tools")
    names = {i["title"] for i in tools["items"]}
    assert {"Read", "Bash", "Edit"} <= names
    read = next(i for i in tools["items"] if i["title"] == "Read")
    schema = json.loads(read["body"])
    assert schema["type"] == "object" and "path" in schema["properties"]
    assert any(k == "Description" and v for k, v in read["meta"])


def test_memory_file_bodies_are_complete_and_secrets_never_appear(c, env):
    text = "# Project rules\n" + "\n".join(f"- rule number {i}" for i in range(400)) + f"\nAPI key: {FAKE_KEY}\nANTHROPIC_API_KEY={FAKE_KEY}\n"
    Path(env.ws, "AXON.md").write_text(text, encoding="utf-8")
    sid = _sid(c)
    d = _inspect(c, sid, "memory_files")
    f = next(i for i in d["items"] if i["title"] == "AXON.md")
    assert "- rule number 399" in f["body"] and "- rule number 0\n" in f["body"] and f["tokens"] > 1000
    assert f["lang"] == "markdown" and any(k == "Scope" for k, _ in f["meta"]) and f["links"]
    assert "scrubbed" in d["note"] or "masked" in d["note"]
    for section in ["overview"] + ORDER:
        assert FAKE_KEY not in json.dumps(_inspect(c, sid, section)), section


def test_secrets_in_messages_and_tool_results_are_masked(c):
    sid = _sid(c)
    _talk(_agent(c, sid), f"config dump\nOPENAI_API_KEY={FAKE_KEY}\n")
    raw = json.dumps(_inspect(c, sid, "messages"))
    assert FAKE_KEY not in raw


def test_skills_agents_and_mcp_sections_list_their_sources(c, env):
    sid = _sid(c)
    skill_dir = Path(env.ws, ".axon", "skills", "demo-skill")
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Does a demo thing\n---\nStep one: do the thing.\n", encoding="utf-8")
    agents_dir = Path(env.ws, ".axon", "agents")
    agents_dir.mkdir(parents=True)
    (agents_dir / "scout.md").write_text("---\nname: scout\ndescription: Looks around\ntools: Read, Grep\n---\nYou are a careful scout.\n", encoding="utf-8")
    agent = _agent(c, sid)
    try:
        agent.skills.load()
    except Exception:
        pass
    agents = _inspect(c, sid, "custom_agents")
    scout = next(i for i in agents["items"] if i["title"] == "scout")
    assert "You are a careful scout." in scout["body"] and any(k == "Tools" and "Read" in v for k, v in scout["meta"])
    assert any(k == "Source" and "scout.md" in v for k, v in scout["meta"])
    skills = _inspect(c, sid, "skills")
    assert isinstance(skills["items"], list)
    for it in skills["items"]:
        assert any(k == "File" for k, _ in it["meta"]) and it["extra"][0]["label"]
    mcp = _inspect(c, sid, "mcp_tools")
    assert isinstance(mcp["items"], list) and _inspect(c, sid, "mcp_instructions")["note"]


def test_mcp_tools_include_loaded_and_deferred_with_schemas(c):
    from types import SimpleNamespace
    sid = _sid(c)
    agent = _agent(c, sid)
    deferred = SimpleNamespace(name="mcp__demo__lookup", description="short", full_description="[MCP:demo] Look a thing up",
                               schema={"type": "object", "properties": {"q": {"type": "string"}}})
    agent.registry.tools["ToolSearch"] = SimpleNamespace(deferred={deferred.name: deferred}, name="ToolSearch")
    d = _inspect(c, sid, "mcp_tools")
    it = next(i for i in d["items"] if i["id"] == "mcp:mcp__demo__lookup")
    assert it["group"] == "demo" and "deferred" in it["badges"] and json.loads(it["body"])["properties"]["q"]["type"] == "string"
    assert any(k == "Description" and "Look a thing up" in v for k, v in it["meta"])


def test_permissions_list_mode_rules_with_their_source_and_meaning(c, env):
    sid = _sid(c)
    cfg = Path(env.ws, ".axon")
    cfg.mkdir(exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git status)"], "deny": ["Read(./secrets/**)"], "ask": ["Bash(rm *)"]}}), encoding="utf-8")
    agent = _agent(c, sid)
    agent.permissions.reload()
    d = _inspect(c, sid, "permissions")
    ids = {i["id"] for i in d["items"]}
    assert {"perm:mode", "perm:dirs", "perm:adddirs", "perm:protected", "perm:creds", "perm:sandbox"} <= ids
    rules = {i["title"]: i for i in d["items"] if i["kind"] == "rule" and i["id"].startswith(("perm:allow", "perm:ask", "perm:deny"))}
    assert {"Bash(git status)", "Read(./secrets/**)", "Bash(rm *)"} <= set(rules)
    allow = rules["Bash(git status)"]
    meta = dict(allow["meta"])
    assert meta["Decision"] == "allow" and meta["Source"] and "git status" in meta["Applies to"] and "allow" in allow["badges"]
    assert "settings.json" in meta["File"]
    assert dict(rules["Read(./secrets/**)"]["meta"])["Decision"] == "deny"
    protected = next(i for i in d["items"] if i["id"] == "perm:protected")
    assert ".git" in protected["body"] and ".zshrc" in protected["body"]


def test_hooks_list_every_configured_hook_and_trust(c, env):
    sid = _sid(c)
    cfg = Path(env.ws, ".axon")
    cfg.mkdir(exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo checking"}]}]}}), encoding="utf-8")
    _agent(c, sid).hooks.reload()
    d = _inspect(c, sid, "hooks")
    assert d["items"][0]["id"] == "hook:trust"
    hook = next(i for i in d["items"] if i["id"].startswith("hook:") and i["id"] != "hook:trust" and not i["id"].startswith("hook:err"))
    meta = dict(hook["meta"])
    assert meta["Event"] == "PreToolUse" and meta["Matcher"] == "Bash" and meta["Type"] == "command" and hook["body"] == "echo checking"


def test_autocompact_and_free_explain_with_numbers(c):
    sid = _sid(c)
    ac = _inspect(c, sid, "autocompact")["items"][0]
    assert "/autocompact" in ac["body"] and any(k == "Window" for k, _ in ac["meta"])
    fr = _inspect(c, sid, "free")["items"][0]
    assert "Free space" in fr["body"] and fr["tokens"] >= 0


def test_a_missing_session_gives_an_empty_result_not_an_error(c):
    st, _, body = c.post("/api/insights/ctxinspect", {"session": "no-such-session-id", "section": "messages"})
    assert st == 200
    d = json.loads(body)
    assert d["section"] == "messages" and d["items"] == [] and d["note"] and [s["key"] for s in d["sections"]] == ORDER
    assert d["sections"][-1]["tokens"] is None


def test_an_unknown_section_is_rejected_and_the_route_is_guarded(c):
    sid = _sid(c)
    st, _, _ = c.post("/api/insights/ctxinspect", {"session": sid, "section": "nope"})
    assert st == 400
    st, _, _ = c.post("/api/insights/ctxinspect", {"session": sid, "section": "messages"}, origin="https://evil.example")
    assert st in (400, 401, 403)
    st, _, _ = c.post("/api/insights/ctxinspect", {"session": sid, "section": "messages"}, cookie=False)
    assert st in (400, 401, 403)


def test_inspect_section_rejects_unknown_keys():
    with pytest.raises(ValueError):
        ci.inspect_section(object(), "bogus")
