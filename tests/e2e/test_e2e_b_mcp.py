"""E2E-B: MCP end to end - a real stdio MCP server (tests/e2e/fake_mcp_server.py, newline-framed
JSON-RPC, paginated tools/list, tools/call, resources, prompts) and an in-process Streamable-HTTP
MCP server, exercised through `axon mcp add/list/remove`, project .mcp.json, ${VAR} expansion,
model tool calls (mcp__srv__tool) with results fed back, @srv:uri resource mentions, MCP prompts
as /mcp__srv__prompt, failing and disabled servers."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from fake_llm import Recorded
from fake_mcp_server import start_http_server
from harness import IS_WINDOWS, AxonEnv
from test_e2e_b_util import (
    has_tool_result, no_crash, repl_cmd, run_json, start_repl, text_in_request, write_file,
    parallel, pbatch,  # noqa: F401 (pbatch is a fixture)
)

pytestmark = pytest.mark.e2e

FAKE = str(Path(__file__).resolve().parent / "fake_mcp_server.py")
PY = sys.executable
ALL_TOOLS = {"mcp__srv__echo", "mcp__srv__add", "mcp__srv__env_secret", "mcp__srv__boom"}


def _log(env: AxonEnv, name: str = "srv") -> Path:
    return env.root / f"mcp-{name}.log"


def _methods(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [json.loads(ln).get("method", "") for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _stdio_spec(ae: AxonEnv, name: str = "srv", **extra) -> dict:
    return {"command": PY, "args": [FAKE, "--name", name, "--log", str(_log(ae, name))], **extra}


def _project_mcp(env: AxonEnv, servers: dict) -> None:
    write_file(env.workspace / ".mcp.json", json.dumps({"mcpServers": servers}))


def _tool_names(env: AxonEnv) -> set[str]:
    reqs = env.llm.completion_requests()
    assert reqs, "no LLM request"
    return set(reqs[0].tool_names)


def _p(env: AxonEnv, line: str, *extra: str, **kw):
    r = env.run(["-p", *extra, line], **kw)
    no_crash(r)
    assert r.code == 0, str(r)
    return r


# ─── `axon mcp` subcommand ──────────────────────────────────────────────────

@parallel
def test_cli_mcp_add_stdio_connects_and_paginates(axon_env: AxonEnv) -> None:
    """`axon mcp add srv <python> <script>` saves to AXON_HOME/mcp.json and connects with all 4 tools
    (tools/list is paginated over two pages)."""
    r = axon_env.run(["mcp", "add", "srv", PY, FAKE, "--name", "srv", "--log", str(_log(axon_env))])
    no_crash(r)
    assert r.code == 0, str(r)
    assert "connected" in r.output and "4 tool(s)" in r.output, str(r)
    data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["command"] == PY
    assert data["mcpServers"]["srv"]["args"][0] == FAKE
    assert _methods(_log(axon_env)).count("tools/list") >= 2
    assert not (axon_env.workspace / ".mcp.json").exists()


@parallel
def test_cli_mcp_list_and_remove(axon_env: AxonEnv) -> None:
    """`axon mcp list` shows a configured server; `axon mcp remove` deletes it from the config."""
    write_file(axon_env.axon_home / "mcp.json", json.dumps({"mcpServers": {"srv": _stdio_spec(axon_env)}}))
    r = axon_env.run(["mcp", "list"])
    no_crash(r)
    assert "srv" in r.output, str(r)
    r = axon_env.run(["mcp", "remove", "srv"])
    no_crash(r)
    assert "Removed" in r.output, str(r)
    data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
    assert "srv" not in data.get("mcpServers", {})


@parallel
def test_cli_mcp_remove_unknown(axon_env: AxonEnv) -> None:
    """`axon mcp remove nosuch` reports it is not configured."""
    r = axon_env.run(["mcp", "remove", "nosuch"])
    no_crash(r)
    assert "not found" in r.output


@parallel
def test_cli_mcp_add_project_scope_writes_dot_mcp_json(axon_env: AxonEnv) -> None:
    """`axon mcp add --scope project` writes <ws>/.mcp.json (Claude Code layout)."""
    r = axon_env.run(["mcp", "add", "--scope", "project", "srv", PY, FAKE])
    no_crash(r)
    data = json.loads((axon_env.workspace / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["args"] == [FAKE]


@parallel
def test_cli_mcp_add_with_env(axon_env: AxonEnv) -> None:
    """`axon mcp add --env K=V` stores the env var in the spec."""
    r = axon_env.run(["mcp", "add", "--env", "MCP_SECRET=abc", "srv", PY, FAKE])
    no_crash(r)
    data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["env"] == {"MCP_SECRET": "abc"}


@parallel
def test_cli_mcp_add_http(axon_env: AxonEnv) -> None:
    """`axon mcp add --transport http web <url>` saves an http spec and connects."""
    srv = start_http_server()
    try:
        r = axon_env.run(["mcp", "add", "--transport", "http", "web", srv.url])
        no_crash(r)
        assert "connected" in r.output and "4 tool(s)" in r.output, str(r)
        data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
        assert data["mcpServers"]["web"] == {"type": "http", "url": srv.url, "headers": {}} or \
            data["mcpServers"]["web"]["url"] == srv.url
    finally:
        srv.shutdown()


@parallel
def test_cli_mcp_add_json(axon_env: AxonEnv) -> None:
    """`axon mcp add-json srv '<json>'` saves the given spec verbatim."""
    spec = json.dumps({"command": PY, "args": [FAKE]})
    r = axon_env.run(["mcp", "add-json", "srv", spec])
    no_crash(r)
    data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["args"] == [FAKE], str(r)


@parallel
def test_cli_mcp_add_path_with_space(axon_env: AxonEnv) -> None:
    """An argument with a space (`axon mcp add srv python "/dir with space/s.py"`) is kept intact."""
    spaced = axon_env.root / "dir with space" / "s.py"
    write_file(spaced, Path(FAKE).read_text(encoding="utf-8"))
    axon_env.run(["mcp", "add", "srv", PY, str(spaced)])
    data = json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["args"] == [str(spaced)], data


# ─── discovery and tool calls ───────────────────────────────────────────────

@parallel
def test_project_mcp_json_tools_offered_to_model(axon_env: AxonEnv) -> None:
    """Servers in <ws>/.mcp.json connect at startup; all paginated tools are offered to the model."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hi")
    assert ALL_TOOLS <= _tool_names(axon_env), _tool_names(axon_env)
    m = _methods(_log(axon_env))
    assert m[0] == "initialize" and "notifications/initialized" in m


@parallel
def test_model_calls_mcp_tool_and_gets_result(axon_env: AxonEnv) -> None:
    """The model calls mcp__srv__echo; the server's result is fed back in the next request."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"tool": "mcp__srv__echo", "args": {"text": "round-trip-77"}}],
                         {"steps": [{"text": "MCP-DONE"}], "expect": [has_tool_result("ECHO:round-trip-77")]})
    data = run_json(axon_env, "use echo")
    assert data["result"].strip() == "MCP-DONE" and data["tool_calls"] == 1
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert "tools/call" in _methods(_log(axon_env))


@parallel
def test_mcp_tool_with_integer_args_anthropic_wire(axon_env: AxonEnv) -> None:
    """mcp__srv__add works over the Anthropic wire too (tool_use -> tool_result)."""
    axon_env.use_anthropic_wire()
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"tool": "mcp__srv__add", "args": {"a": 2, "b": 3}}],
                         {"steps": [{"text": "sum ok"}], "expect": [has_tool_result("SUM=5")]})
    run_json(axon_env, "add")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_mcp_tool_error_is_reported_to_model(axon_env: AxonEnv) -> None:
    """A tool result with isError=true reaches the model as an error result."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"tool": "mcp__srv__boom", "args": {}}],
                         {"steps": [{"text": "saw error"}], "expect": [has_tool_result("BOOM-FAILED")]})
    data = run_json(axon_env, "boom")
    assert not axon_env.llm.failures, axon_env.llm.failures
    recs = [json.loads(ln) for ln in (axon_env.sessions_dir() / f"{data['session_id']}.jsonl").read_text(
        encoding="utf-8").splitlines()]
    res = [r for r in recs if r["type"] == "tool_results"]
    assert res and res[0]["data"][0]["is_error"] is True


@parallel
def test_env_var_expansion_in_env_and_args(axon_env: AxonEnv) -> None:
    """${VAR} in `env` and `args` is expanded from Axon's environment at connect time."""
    spec = {"command": PY, "args": ["${FAKE_MCP_PATH}", "--log", str(_log(axon_env))],
            "env": {"MCP_SECRET": "${E2E_SECRET}"}}
    _project_mcp(axon_env, {"srv": spec})
    axon_env.llm.enqueue([{"tool": "mcp__srv__env_secret", "args": {}}],
                         {"steps": [{"text": "k"}], "expect": [has_tool_result("SECRET=hunter2")]})
    run_json(axon_env, "secret", env={"E2E_SECRET": "hunter2", "FAKE_MCP_PATH": FAKE})
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert "${E2E_SECRET}" in (axon_env.workspace / ".mcp.json").read_text(encoding="utf-8")


@parallel
def test_env_var_default_expansion(axon_env: AxonEnv) -> None:
    """${VAR:-default} falls back to the default when VAR is unset."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env, env={"MCP_SECRET": "${UNSET_E2E_VAR:-fallback-val}"})})
    axon_env.llm.enqueue([{"tool": "mcp__srv__env_secret", "args": {}}],
                         {"steps": [{"text": "k"}], "expect": [has_tool_result("SECRET=fallback-val")]})
    run_json(axon_env, "secret")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_http_streamable_server_tool_call_json(axon_env: AxonEnv) -> None:
    """A Streamable-HTTP server (JSON responses, Mcp-Session-Id) serves a tool call end to end."""
    srv = start_http_server()
    try:
        _project_mcp(axon_env, {"web": {"type": "http", "url": srv.url}})
        axon_env.llm.enqueue([{"tool": "mcp__web__add", "args": {"a": 40, "b": 2}}],
                             {"steps": [{"text": "http ok"}], "expect": [has_tool_result("SUM=42")]})
        run_json(axon_env, "add over http")
        assert not axon_env.llm.failures, axon_env.llm.failures
        later = [h for h in srv.headers_seen[1:]]
        assert later and all(h.get("mcp-session-id") for h in later)
        assert any(m.get("method") == "tools/call" for m in srv.received if isinstance(m, dict))
    finally:
        srv.shutdown()


@parallel
def test_http_streamable_server_sse_responses(axon_env: AxonEnv) -> None:
    """The same flow works when the server answers POSTs with text/event-stream."""
    srv = start_http_server(sse=True)
    try:
        _project_mcp(axon_env, {"web": {"type": "http", "url": srv.url}})
        axon_env.llm.enqueue([{"tool": "mcp__web__echo", "args": {"text": "sse!"}}],
                             {"steps": [{"text": "sse ok"}], "expect": [has_tool_result("ECHO:sse!")]})
        run_json(axon_env, "echo over sse")
        assert not axon_env.llm.failures, axon_env.llm.failures
    finally:
        srv.shutdown()


@parallel
def test_http_header_with_env_expansion(axon_env: AxonEnv) -> None:
    """`headers` values support ${VAR}; a server requiring the header accepts the connection."""
    srv = start_http_server(required_header=("Authorization", "Bearer tok-123"))
    try:
        _project_mcp(axon_env, {"web": {"type": "http", "url": srv.url,
                                        "headers": {"Authorization": "Bearer ${E2E_TOKEN}"}}})
        axon_env.llm.enqueue([{"text": "ok"}])
        run_json(axon_env, "hi", env={"E2E_TOKEN": "tok-123"})
        assert "mcp__web__echo" in _tool_names(axon_env)
    finally:
        srv.shutdown()


@parallel
def test_mcp_config_flag_and_strict(axon_env: AxonEnv) -> None:
    """--mcp-config FILE adds servers; --strict-mcp-config ignores .mcp.json."""
    _project_mcp(axon_env, {"proj": _stdio_spec(axon_env, "proj")})
    cfg = write_file(axon_env.root / "extra-mcp.json", json.dumps({"mcpServers": {"srv": _stdio_spec(axon_env)}}))
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "--mcp-config", str(cfg), "--strict-mcp-config", "--", "hi")
    names = _tool_names(axon_env)
    assert "mcp__srv__echo" in names and not any(n.startswith("mcp__proj__") for n in names), names


@parallel
def test_local_scope_overrides_project_scope(axon_env: AxonEnv) -> None:
    """<ws>/.axon/mcp.json (local) overrides a same-named server from .mcp.json (project)."""
    _project_mcp(axon_env, {"srv": {"command": PY, "args": [FAKE, "--fail"]}})
    write_file(axon_env.workspace / ".axon" / "mcp.json", json.dumps({"mcpServers": {"srv": _stdio_spec(axon_env)}}))
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hi")
    assert "mcp__srv__echo" in _tool_names(axon_env)


# ─── failing / disabled servers ─────────────────────────────────────────────

@parallel
def test_failing_server_does_not_block_startup(axon_env: AxonEnv) -> None:
    """A server that exits at startup does not stop Axon: the prompt still runs, other servers work."""
    _project_mcp(axon_env, {"bad": {"command": PY, "args": [FAKE, "--fail"]}, "srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"text": "still works"}])
    t0 = time.monotonic()
    data = run_json(axon_env, "hi", timeout=30)
    assert data["result"].strip() == "still works"
    assert time.monotonic() - t0 < 20
    names = _tool_names(axon_env)
    assert "mcp__srv__echo" in names and not any(n.startswith("mcp__bad__") for n in names)


@parallel
def test_missing_command_server_does_not_block_startup(axon_env: AxonEnv) -> None:
    """A server whose command does not exist is reported, not fatal."""
    _project_mcp(axon_env, {"ghost": {"command": "definitely-not-a-real-binary-xyz"}})
    axon_env.llm.enqueue([{"text": "fine"}])
    assert run_json(axon_env, "hi", timeout=30)["result"].strip() == "fine"


@parallel
def test_failing_server_status_shown(axon_env: AxonEnv) -> None:
    """/mcp lists the failed server with an error state."""
    _project_mcp(axon_env, {"bad": {"command": PY, "args": [FAKE, "--fail"]}})
    out = _p(axon_env, "/mcp").output
    assert "bad" in out and ("fail" in out.lower() or "error" in out.lower() or "closed" in out.lower()), out


@parallel
def test_disabled_server_is_skipped(axon_env: AxonEnv) -> None:
    """A server with "disabled": true is never launched and offers no tools."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env, disabled=True)})
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hi")
    assert not any(n.startswith("mcp__srv__") for n in _tool_names(axon_env))
    assert not _log(axon_env).exists()


@parallel
def test_cli_mcp_disable_then_enable(axon_env: AxonEnv) -> None:
    """`axon mcp disable srv` marks it disabled (no tools); `axon mcp enable srv` brings it back."""
    write_file(axon_env.axon_home / "mcp.json", json.dumps({"mcpServers": {"srv": _stdio_spec(axon_env)}}))
    no_crash(axon_env.run(["mcp", "disable", "srv"]))
    assert json.loads((axon_env.axon_home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["srv"].get("disabled")
    axon_env.llm.enqueue([{"text": "a"}])
    run_json(axon_env, "hi")
    assert not any(n.startswith("mcp__srv__") for n in _tool_names(axon_env))
    no_crash(axon_env.run(["mcp", "enable", "srv"]))
    axon_env.llm.reset()
    axon_env.llm.enqueue([{"text": "b"}])
    run_json(axon_env, "hi")
    assert "mcp__srv__echo" in _tool_names(axon_env)


# ─── resources and prompts ──────────────────────────────────────────────────

@parallel
def test_mcp_resources_listed(axon_env: AxonEnv) -> None:
    """/mcp resources lists mem://notes; /mcp read srv mem://notes prints its text."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    assert "mem://notes" in _p(axon_env, "/mcp resources").output
    assert "RESOURCE-NOTES-CONTENT" in _p(axon_env, "/mcp read srv mem://notes").output


@parallel
def test_model_reads_resource_via_generic_tool(axon_env: AxonEnv) -> None:
    """The model can read a resource with mcp__read_resource; the content is fed back."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue([{"tool": "mcp__read_resource", "args": {"server": "srv", "uri": "mem://notes"}}],
                         {"steps": [{"text": "read"}], "expect": [has_tool_result("RESOURCE-NOTES-CONTENT")]})
    run_json(axon_env, "read notes")
    assert not axon_env.llm.failures, axon_env.llm.failures


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_resource_mention_is_inlined(axon_env: AxonEnv) -> None:
    """In the REPL, `@srv:mem://notes` in a prompt is replaced by the resource content."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    axon_env.llm.enqueue({"steps": [{"text": "RES-SEEN"}], "expect": [text_in_request("RESOURCE-NOTES-CONTENT")]})
    with start_repl(axon_env) as s:
        repl_cmd(s, "summarize @srv:mem://notes please", "RES-SEEN", 25)
        s.sendline("/exit")
        s.wait(10)
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_print_mode_resource_and_file_mentions(axon_env: AxonEnv) -> None:
    """`axon -p "... @srv:mem://notes @notes.txt"` inlines both like the REPL does."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    write_file(axon_env.workspace / "notes.txt", "FILE-MENTION-CONTENT")
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "look at @srv:mem://notes and @notes.txt")
    text = axon_env.llm.completion_requests()[0].last_user_text()
    assert "RESOURCE-NOTES-CONTENT" in text and "FILE-MENTION-CONTENT" in text, text


@parallel
def test_mcp_prompt_as_slash_command(axon_env: AxonEnv) -> None:
    """`/mcp__srv__review topic=security` fetches the prompt and sends it as the user turn."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    _p(axon_env, "/mcp__srv__review topic=security")
    reqs = axon_env.llm.completion_requests()
    assert reqs and "PROMPT-REVIEW-BODY about security" in reqs[-1].last_user_text()
    assert "prompts/get" in _methods(_log(axon_env))


@parallel
def test_mcp_prompt_listed_in_help(axon_env: AxonEnv) -> None:
    """/help lists connected MCP prompts under 'MCP prompts'."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    out = _p(axon_env, "/help").output
    assert "MCP prompts" in out and "/mcp__srv__review" in out


@parallel
def test_mcp_tools_command(axon_env: AxonEnv) -> None:
    """/mcp tools srv lists all four tools of the paginated server."""
    _project_mcp(axon_env, {"srv": _stdio_spec(axon_env)})
    out = _p(axon_env, "/mcp tools srv").output
    for t in ("echo", "add", "env_secret", "boom"):
        assert t in out, out


@parallel
def test_plugin_mcp_server_loaded(axon_env: AxonEnv) -> None:
    """A plugin bundling .mcp.json contributes its server's tools."""
    root = axon_env.workspace / ".axon" / "plugins" / "mcpplug"
    write_file(root / "plugin.json", json.dumps({"name": "mcpplug"}))
    write_file(root / ".mcp.json", json.dumps({"mcpServers": {"srv": _stdio_spec(axon_env)}}))
    axon_env.llm.enqueue([{"text": "ok"}])
    run_json(axon_env, "hi")
    assert any(n.startswith("mcp__srv__") for n in _tool_names(axon_env))


def _unused(_: Recorded) -> None:  # keep the Recorded import meaningful for type hints
    pass
