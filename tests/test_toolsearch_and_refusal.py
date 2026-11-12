"""Server-side tool search, per-MCP-server tool controls and provider-neutral refusal handling (fakes only)."""
from __future__ import annotations

from types import SimpleNamespace

from axon.agent import refusal
from axon.mcp import toolctl
from axon.mcp.config import ServerSpec
from axon.providers import toolflow, toolsearch


def _tools():
    return [{"name": "Read", "description": "r", "input_schema": {"type": "object"}},
            {"name": "ToolSearch", "description": "client search", "input_schema": {"type": "object"}},
            {"name": "NotebookEdit", "description": "n", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}]


def test_server_search_rewrites_tools_on_official_host_only():
    toolflow.set_hints(deferred_defs=[{"name": "mcp__a__x", "description": "d", "input_schema": {"type": "object"}}])
    kw = {"tools": _tools(), "messages": []}
    assert toolsearch.apply(kw, "claude-sonnet-4-6", official=True, settings=SimpleNamespace(server_tool_search="auto"))
    names = [t["name"] for t in kw["tools"]]
    assert "ToolSearch" not in names and toolsearch.SERVER_TOOL_NAME in names
    deferred = {t["name"] for t in kw["tools"] if t.get("defer_loading")}
    assert deferred == {"mcp__a__x", "NotebookEdit"}
    assert not any("cache_control" in t for t in kw["tools"] if t.get("defer_loading"))
    assert "cache_control" in kw["tools"][0]                       # breakpoint moved to a non-deferred tool
    for official, model, mode in ((False, "claude-sonnet-4-6", "auto"), (True, "claude-opus-4-1", "auto"), (True, "claude-sonnet-4-6", "off")):
        kw = {"tools": _tools()}
        assert toolsearch.apply(kw, model, official=official, settings=SimpleNamespace(server_tool_search=mode)) == []
        assert [t["name"] for t in kw["tools"]] == ["Read", "ToolSearch", "NotebookEdit"]
    toolflow.set_hints()


def test_strip_retries_with_every_tool_loaded_and_drops_history_blocks():
    toolflow.set_hints(deferred_defs=[{"name": "mcp__a__x", "description": "d", "input_schema": {}}])
    msgs = [{"role": "assistant", "content": [{"type": "server_tool_use", "id": "srvtoolu_1"},
                                              {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_1"},
                                              {"type": "text", "text": "ok"}]}]
    kw = {"tools": _tools(), "messages": msgs}
    toolsearch.apply(kw, "claude-sonnet-5-5", official=True)
    assert not toolsearch.strip(kw, "some other 400")
    assert toolsearch.strip(kw, "tools.3.defer_loading: Extra inputs are not permitted")
    assert not any(t.get("defer_loading") or t.get("type") == toolsearch.SERVER_TOOL_TYPE for t in kw["tools"])
    assert kw["messages"][0]["content"] == [{"type": "text", "text": "ok"}]
    toolflow.set_hints()


def test_deferred_defs_and_reveal_all():
    from axon.tools.interactive import ToolSearchTool

    class T:
        name, description, schema = "mcp__a__x", "does x", {"type": "object", "properties": {}}

    class Reg:
        def __init__(self):
            self.tools = {"ToolSearch": ToolSearchTool([T()])}

        def has(self, n):
            return n in self.tools

        def register(self, t):
            self.tools[t.name] = t

    reg = Reg()
    assert toolsearch.deferred_defs(reg)[0]["name"] == "mcp__a__x"
    assert toolsearch.reveal_all(reg) == 1 and toolsearch.reveal_all(reg) == 0
    assert reg.tools["mcp__a__x"].hidden


def test_server_tool_controls():
    spec = ServerSpec.from_dict("s", {"command": "x", "allowedTools": ["get_*", "list"], "deniedTools": "get_secret", "pinTools": True})
    assert spec.allow_tools == ["get_*", "list"] and spec.deny_tools == ["get_secret"] and spec.pin_tools
    again = ServerSpec.from_dict("s", spec.to_dict())
    assert (again.allow_tools, again.deny_tools, again.pin_tools) == (spec.allow_tools, spec.deny_tools, True)
    tools = [{"name": n} for n in ("get_a", "get_secret", "list", "drop")]
    kept, dropped = toolctl.filter_specs(spec, tools)
    assert [t["name"] for t in kept] == ["get_a", "list"] and dropped == ["get_secret", "drop"]
    pins: dict = {}
    first = toolctl.pinned(pins, spec, "s", tools)
    assert toolctl.pinned(pins, spec, "s", tools[:1]) == first          # a changed list is ignored while pinned
    spec.pin_tools = False
    assert toolctl.pinned(pins, spec, "s", tools[:1]) == tools[:1] and "s" not in pins


def test_filter_error_detection_is_provider_neutral():
    class E(Exception):
        status = 400
    assert refusal.is_filter_error(E("The response was filtered due to the prompt triggering content_filter"))
    assert refusal.is_filter_error(E("ResponsibleAIPolicyViolation"))
    assert not refusal.is_filter_error(E("rate limit exceeded"))


def _agent(fallback="gpt-5", model="claude-sonnet-5-5"):
    conv = SimpleNamespace(messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "earlier"}]},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": [{"type": "text", "text": "no"}]}])
    return SimpleNamespace(settings=SimpleNamespace(model=model, refusal_fallback_model=fallback), conversation=conv)


def test_refusal_retries_once_on_fallback_and_keeps_history_clean(monkeypatch):
    switched = []
    monkeypatch.setattr(refusal, "switch", lambda a, m: switched.append(m) or True)
    a = _agent()
    turn = SimpleNamespace(stop_details={"category": "x", "fallback_credit_token": "tok", "fallback_has_prefill_claim": False})
    note = refusal.retry(a, turn)
    assert "retrying once on gpt-5" in note and switched == ["gpt-5"]
    assert [m["role"] for m in a.conversation.messages] == ["user", "assistant", "user"]       # refused reply dropped
    assert a.conversation.messages[1]["content"] == [{"type": "text", "text": "earlier"}]       # no foreign thinking
    assert refusal.take_credit() == "tok" and refusal.take_credit() is None


def test_refusal_without_fallback_or_with_claim_does_nothing_extra(monkeypatch):
    monkeypatch.setattr(refusal, "switch", lambda a, m: True)
    assert refusal.retry(_agent(fallback="")) == ""
    a = _agent()
    refusal.retry(a, SimpleNamespace(stop_details={"fallback_credit_token": "t", "fallback_has_prefill_claim": True}))
    assert refusal.take_credit() is None            # a prefill claim needs the refused content echoed: not supported
    assert "refusal_fallback_model" in refusal.next_step_line(SimpleNamespace(refusal_fallback_model=""))


def test_credit_beta_only_on_official_host_and_strippable():
    s = SimpleNamespace(refusal_fallback_model="claude-opus-5")
    kw: dict = {}
    assert not refusal.apply_credit(kw, s, official=False)
    assert refusal.apply_credit(kw, s, official=True, token="tok")
    assert kw["extra_body"]["fallback_credit_token"] == "tok" and refusal.BETA_FALLBACK_CREDIT in kw["extra_headers"]["anthropic-beta"]
    assert refusal.strip_credit(kw, "unknown field fallback_credit_token")
    assert "extra_body" not in kw and "extra_headers" not in kw
