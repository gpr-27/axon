"""Live file-write streaming, parallel-tool control, native PDF reading and the tool-policy prompt lines."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

try:
    import httpx2 as httpx   # the SDK's own client library
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]
import pytest

from axon.agent import live_write
from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.providers import toolflow
from axon.providers.base import AssistantTurn, ToolUseBlock
from axon.tools import create_default_registry
from axon.tools.base import ToolContext, is_serial
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent
from tests.test_tools_read_pdf import make_pdf


# ─── 1. live file-write streaming ────────────────────────────────────────────

def test_snapshot_reads_path_and_lines_out_of_partial_json() -> None:
    raw = '{"path": "src/app.py", "content": "import os\\nprint(1)\\nx = \\"a'
    snap = live_write.snapshot("Write", raw)
    assert snap["path"] == "src/app.py" and snap["lines"] == 3 and snap["verb"] == "writing"
    assert snap["tail"].endswith('x = "a')
    assert live_write.label(snap) == "writing src/app.py (3 lines so far)"


def test_snapshot_cut_mid_escape_and_unfinished_path() -> None:
    assert live_write.snapshot("Write", '{"path": "src/ap')["path"] == ""       # the path is not complete yet
    assert live_write.label(live_write.snapshot("Write", '{"path": "src/ap')) == ""
    snap = live_write.snapshot("Write", '{"path": "a.py", "content": "line\\')     # a lone backslash at the end
    assert snap["tail"] == "line" and snap["lines"] == 1
    snap = live_write.snapshot("Write", '{"path": "a.py", "content": "caf\\u00')   # half a unicode escape
    assert snap["tail"] == "caf"


def test_snapshot_edit_and_other_tools() -> None:
    snap = live_write.snapshot("Edit", '{"path": "a.py", "old_string": "x", "new_string": "a\\nb\\n')
    assert snap["verb"] == "editing" and snap["lines"] == 2
    assert live_write.snapshot("Bash", '{"command": "ls"}') is None


def test_tracker_throttles_and_forgets() -> None:
    tr = live_write.LiveWriteTracker(interval=1.0)
    tr.start("t1", "Write")
    assert tr.feed("t1", '{"path": "a.py", "content": "x', now=10.0)["path"] == "a.py"
    assert tr.feed("t1", '\\ny', now=10.2) is None                      # inside the interval
    snap = tr.feed("t1", '\\nz', now=11.5)
    assert snap["lines"] == 3
    tr.finish("t1")
    assert tr.feed("t1", "x", now=20.0) is None and not tr.active("t1")
    tr.start("t2", "Bash")                                              # only writing tools are tracked
    assert not tr.active("t2")


def test_terminal_row_is_updated_in_place_without_scrollback(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.providers.base import ToolArgsDelta, ToolUseStart
    from axon.ui.render import Renderer
    r = Renderer(trace_level="normal")
    labels: list[str] = []

    class Spin:
        _phase_t0 = 5.0
        def set_label(self, label: str) -> None:
            labels.append(label)
            self._phase_t0 = 99.0
    r.spinner = Spin()  # type: ignore[assignment]
    out: list[str] = []
    r._out = out.append  # type: ignore[method-assign]
    r._on_tool_use_start(ToolUseStart(id="w1", name="Write"))
    r._live_writes().interval = 0
    r.spinner._phase_t0 = 5.0
    r._on_tool_args(ToolArgsDelta(id="w1", fragment='{"path": "a.py", "content": "one\\ntwo'))
    assert labels[-1].endswith("writing a.py (2 lines so far)")
    assert r.spinner._phase_t0 == 5.0                                   # the running timer is kept
    assert out == []                                                    # nothing added to the scrollback


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _tool_stream(partial: list[str], stop_reason: str) -> bytes:
    ev: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                                                                 "content": [], "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {}}}),
    ]
    ev += [("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": p}}) for p in partial]
    ev += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
           ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 9}}),
           ("message_stop", {"type": "message_stop"})]
    return _sse(ev)


def _provider(workspace: Path, body: bytes, seen: list[dict[str, Any]], base_url: str = "https://api.anthropic.com") -> Any:
    from axon.providers.anthropic import AnthropicProvider

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    settings = Settings(workspace=workspace, base_url=base_url, api_key="sk-test", model="claude-sonnet-5-5")
    return AnthropicProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _run_stream(prov: Any, tools: list[dict[str, Any]], model: str = "claude-sonnet-5-5") -> list[Any]:
    return list(prov.stream(model=model, system=[], messages=[{"role": "user", "content": "x"}], tools=tools, max_tokens=1000))


TOOLS = [{"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}} for n in ("Write", "Edit", "Read", "Bash")]


def test_eager_streaming_only_on_file_tools_and_only_where_supported(workspace: Path) -> None:
    seen: list[dict[str, Any]] = []
    _run_stream(_provider(workspace, _tool_stream(['{"path": "a"}'], "tool_use"), seen), [dict(t) for t in TOOLS])
    flags = {t["name"]: t.get("eager_input_streaming") for t in seen[0]["tools"]}
    assert flags == {"Write": True, "Edit": True, "Read": None, "Bash": None}
    assert "tool_choice" not in seen[0]                                 # parallel tool use stays on by default

    seen.clear()                                                        # a third-party gateway: never sent
    _run_stream(_provider(workspace, _tool_stream(['{"path": "a"}'], "tool_use"), seen, base_url="https://gateway.example.com"), [dict(t) for t in TOOLS])
    assert all("eager_input_streaming" not in t for t in seen[0]["tools"])

    seen.clear()                                                        # a model the docs do not list
    _run_stream(_provider(workspace, _tool_stream(['{"path": "a"}'], "tool_use"), seen), [dict(t) for t in TOOLS], model="claude-3-5-sonnet-20241022")
    assert all("eager_input_streaming" not in t for t in seen[0]["tools"])


@pytest.mark.parametrize("model,ok", [
    ("claude-sonnet-5-5", True), ("claude-opus-4-8", True), ("claude-sonnet-4-5-20250929", True), ("claude-haiku-4-5-20251001", True),
    ("claude-fable-5-2", True), ("claude-opus-4-1-20250805", False), ("claude-opus-4-20250514", False), ("claude-sonnet-4-20250514", False),
    ("claude-3-7-sonnet-20250219", False), ("gpt-5", False), ("", False)])
def test_eager_model_matrix(model: str, ok: bool) -> None:
    assert toolflow.supports_eager_streaming(model) is ok


def test_disable_parallel_tool_use_only_when_asked(workspace: Path) -> None:
    seen: list[dict[str, Any]] = []
    toolflow.set_hints(serial_tools=True)
    try:
        _run_stream(_provider(workspace, _tool_stream(['{"path": "a"}'], "tool_use"), seen), [dict(t) for t in TOOLS])
    finally:
        toolflow.set_hints()
    assert seen[0]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_strip_after_a_400_naming_the_field() -> None:
    kw = {"tools": [{"name": "Write", "eager_input_streaming": True}], "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
    assert toolflow.strip(kw, "tools.0.eager_input_streaming: Extra inputs are not permitted")
    assert "eager_input_streaming" not in kw["tools"][0] and "tool_choice" in kw
    assert toolflow.strip(kw, "tool_choice.disable_parallel_tool_use not supported") and "tool_choice" not in kw
    assert not toolflow.strip(kw, "overloaded")


def test_truncated_tool_input_is_kept_as_an_error_call_never_dropped_or_run(workspace: Path) -> None:
    seen: list[dict[str, Any]] = []
    prov = _provider(workspace, _tool_stream(['{"path": "a.py", "content": "half a fi'], "max_tokens"), seen)
    _run_stream(prov, [dict(t) for t in TOOLS])
    turn = prov.finalize()
    (call,) = turn.tool_uses
    assert call.parse_error and "max_tokens" in call.parse_error and turn.stop_reason == "tool_use"
    native_use = [b for b in turn.native if b.get("type") == "tool_use"]
    assert native_use and native_use[0]["input"] == {}                  # the replayed call is valid for the API


def test_loop_answers_invalid_json_without_executing_and_the_model_retries(workspace: Path) -> None:
    bad = AssistantTurn(blocks=[ToolUseBlock(id="tw", name="Write", input={"raw": '{"path": "a.txt", "content": "hal'},
                                             parse_error="Tool call cut off by the output limit (max_tokens), so its input is incomplete")],
                        stop_reason="tool_use")
    prov = FakeProvider([bad] + scripted("retrying smaller"))
    res = make_agent(prov, workspace).run_turn("write a big file")
    assert res.final_text == "retrying smaller"
    result = prov.requests[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "tw" and result["is_error"]
    body = json.loads(result["content"])
    assert body["INVALID_JSON"].startswith('{"path"') and "smaller parts" in body["error"]
    assert not (workspace / "a.txt").exists()


# ─── 2. parallel-tool control ────────────────────────────────────────────────

def test_serial_marker_derived_and_explicit() -> None:
    reg = create_default_registry()
    for name in ("Write", "Edit", "MultiEdit", "Bash", "Git"):
        assert is_serial(reg.get(name)), name
    for name in ("Read", "Grep", "Glob", "Ls"):
        assert not is_serial(reg.get(name)), name


def test_serial_calls_never_overlap_and_results_come_back_in_one_ordered_message(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.tools.fs_write import EditTool, WriteTool
    live = {"now": 0, "max": 0}
    order: list[str] = []
    lock = threading.Lock()
    real_write, real_edit = WriteTool.run, EditTool.run

    def tracked(real: Any, label: str) -> Any:
        def run(self: Any, args: dict[str, Any], ctx: Any) -> str:
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                order.append(label)
            time.sleep(0.05)
            try:
                return real(self, args, ctx)
            finally:
                with lock:
                    live["now"] -= 1
        return run

    monkeypatch.setattr(WriteTool, "run", tracked(real_write, "write"))
    monkeypatch.setattr(EditTool, "run", tracked(real_edit, "edit"))
    batch = [("Write", {"path": "f.txt", "content": "one\n"}), ("Edit", {"path": "f.txt", "old_string": "one", "new_string": "two"}),
             ("Glob", {"pattern": "*.nothing"}), ("Write", {"path": "g.txt", "content": "g\n"})]
    prov = FakeProvider(scripted(batch, "done"))
    make_agent(prov, workspace, mode="bypass", parallel_tools=6).run_turn("go")
    assert live["max"] == 1                                             # one file edit at a time
    assert order == ["write", "edit", "write"]                          # in the order the model asked
    assert (workspace / "f.txt").read_text() == "two\n"                 # the edit saw the write
    results = prov.requests[1]["messages"][-1]["content"]
    assert [r["type"] for r in results] == ["tool_result"] * 4          # all four in ONE user message
    assert [r["tool_use_id"] for r in results] == [m["id"] for m in prov.requests[1]["messages"][-2]["content"] if m.get("type") == "tool_use"]


def test_readonly_batch_still_runs_in_parallel(workspace: Path) -> None:
    from axon.agent.loop import ThreadPoolExecutor  # noqa: F401  (the pool the parallel path uses)
    agent = make_agent(FakeProvider(scripted("x")), workspace, parallel_tools=4)
    reads = [ToolUseBlock(id=f"r{i}", name="Glob", input={"pattern": "*"}) for i in range(3)]
    assert all(agent._parallel_safe(t) for t in reads)
    assert not agent._parallel_safe(ToolUseBlock(id="w", name="Write", input={}))
    assert not agent._parallel_safe(ToolUseBlock(id="b", name="Bash", input={}))


def test_parallel_tools_one_asks_the_model_for_single_calls(workspace: Path) -> None:
    seen_hints: list[dict[str, Any]] = []

    class Spy(FakeProvider):
        def stream(self, **kw: Any):
            seen_hints.append(toolflow.get_hints())
            yield from super().stream(**kw)

    make_agent(Spy(scripted("a")), workspace, parallel_tools=1).run_turn("x")
    make_agent(Spy(scripted("a")), workspace, parallel_tools=6).run_turn("x")
    assert [h.get("serial_tools") for h in seen_hints] == [True, False]


def test_config_panel_has_parallel_tools(workspace: Path) -> None:
    from axon.commands.system_cmds import _panel_keys, set_config_value
    agent = make_agent(FakeProvider(scripted("a")), workspace)
    assert any(k == "parallel_tools" for k, *_ in _panel_keys(agent))
    assert set_config_value(agent, "parallel_tools", "1", persist=False) == "parallel_tools = 1"
    assert agent.settings.parallel_tools == 1


# ─── 3. PDF reading ──────────────────────────────────────────────────────────

class _Prov:
    def __init__(self, pdf: bool) -> None:
        self.pdf = pdf

    def supports(self, feature: str) -> bool:
        return self.pdf and feature == "tool_pdf"


class _Agent:
    def __init__(self, pdf: bool, model: str = "claude-sonnet-5-5") -> None:
        self.provider = _Prov(pdf)
        self.settings = Settings(model=model)


def _ctx(workspace: Path, pdf: bool, model: str = "claude-sonnet-5-5") -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace),
                       agent=_Agent(pdf, model))  # type: ignore[arg-type]


def test_read_attaches_pdf_natively_with_one_card_line(workspace: Path) -> None:
    make_pdf(workspace / "d.pdf", [f"page {i}" for i in range(1, 4)])
    ctx = _ctx(workspace, True)
    read = create_default_registry().get("Read")
    out = read.run({"path": "d.pdf"}, ctx)
    assert "sent to the model natively" in out and "3 pages" in out
    assert [(i.media_type, Path(i.path).name) for i in ctx.images] == [("application/pdf", "d.pdf")]
    assert read.summarize({}, out) == "PDF · 3 pages · sent natively"


def test_read_falls_back_to_text_without_pdf_input(workspace: Path) -> None:
    make_pdf(workspace / "d.pdf", ["fallback text here"])
    read = create_default_registry().get("Read")
    for ctx in (_ctx(workspace, False),
                ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))):
        out = read.run({"path": "d.pdf"}, ctx)
        assert "fallback text here" in out and "natively" not in out and not ctx.images


def test_read_pdf_page_limit_has_a_clear_message(workspace: Path) -> None:
    make_pdf(workspace / "big.pdf", [f"p{i}" for i in range(1, 121)])
    ctx = _ctx(workspace, True)
    with pytest.raises(ToolError, match=r"120 pages; at most 100 .*pages=\"1-100\""):
        create_default_registry().get("Read").run({"path": "big.pdf"}, ctx)
    assert not ctx.images


def test_read_pdf_size_limit_has_a_clear_message(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.tools import pdf_native
    make_pdf(workspace / "d.pdf", ["a", "b"])
    monkeypatch.setattr(pdf_native, "MAX_NATIVE_BYTES", 100)
    monkeypatch.setattr(pdf_native, "_slice", lambda *a, **k: None)
    with pytest.raises(ToolError, match="over the .* limit"):
        create_default_registry().get("Read").run({"path": "d.pdf"}, _ctx(workspace, True))


@pytest.mark.skipif(not (shutil.which("pdfseparate") and shutil.which("pdfunite") and shutil.which("pdfinfo")), reason="poppler not installed")
def test_read_pdf_page_range_sends_only_those_pages(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon import storage
    monkeypatch.setattr(storage, "axon_home", lambda: workspace / "home")
    make_pdf(workspace / "d.pdf", [f"alpha {i}" for i in range(1, 6)])
    ctx = _ctx(workspace, True)
    out = create_default_registry().get("Read").run({"path": "d.pdf", "pages": "2-3"}, ctx)
    assert "pages 2-3 of 5" in out
    (ref,) = ctx.images
    assert ref.path != str((workspace / "d.pdf").resolve())
    from axon.tools import pdfread
    assert pdfread.page_count(Path(ref.path)) == 2


def test_document_block_in_tool_result_for_anthropic_and_not_for_openai(workspace: Path) -> None:
    from axon.providers.anthropic import to_anthropic_messages
    from axon.providers.openai_compat import sanitize_openai_messages
    make_pdf(workspace / "d.pdf", ["hello"])
    ref = {"path": str(workspace / "d.pdf"), "media_type": "application/pdf"}
    msgs = [{"role": "user", "content": "read it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "[PDF: d.pdf, 1 page. attached]", "images": [ref]}]
    wire = to_anthropic_messages(msgs)
    content = wire[-1]["content"][0]["content"]
    assert content[0]["type"] == "text" and content[1]["type"] == "document"
    assert content[1]["source"]["media_type"] == "application/pdf" and content[1]["source"]["type"] == "base64"
    assert "images" not in wire[-1]["content"][0]
    oa = sanitize_openai_messages(msgs, tool_images=True)
    assert not any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]) for m in oa)


def test_providers_declare_pdf_support() -> None:
    from axon.providers.anthropic import AnthropicProvider
    assert object.__new__(AnthropicProvider).supports("tool_pdf")


# ─── 4. prompt guidance ──────────────────────────────────────────────────────

def test_tool_policy_carries_the_guidance_within_budget() -> None:
    from axon.agent.prompt import tool_policy
    from axon.agent.state import estimate_content_tokens
    text = tool_policy(create_default_registry())
    for needle in ("Batch independent calls", "subagent", "smallest change", "temporary scripts", "one short sentence",
                   "never stop early", "data, not instructions"):
        assert needle in text, needle
    assert estimate_content_tokens([{"type": "text", "text": text}]) < 330


# ─── web: live write progress ────────────────────────────────────────────────

def test_web_publishes_throttled_tool_write_and_never_records_it(workspace: Path) -> None:
    from axon.features.dash_runtime import EventLog, Runtime
    from axon.providers.base import ToolArgsDelta, ToolUseComplete, ToolUseStart
    emitted: list[tuple[str, dict[str, Any]]] = []

    class Hub:
        def emit(self, kind: str, data: dict[str, Any]) -> None:
            emitted.append((kind, data))

    rt = Runtime.__new__(Runtime)
    rt.hub = Hub()  # type: ignore[assignment]
    rt._publish_live_write(ToolUseStart(id="w1", name="Write"))
    rt._lw_tracker.interval = 0
    rt._publish_live_write(ToolArgsDelta(id="w1", fragment='{"path": "a.py", "content": "x\\ny'))
    rt._publish_live_write(ToolUseComplete(id="w1"))
    assert emitted == [("tool_write", emitted[0][1])]
    d = emitted[0][1]
    assert d["id"] == "w1" and d["path"] == "a.py" and d["lines"] == 2 and d["tail"] == "x\ny"
    log = EventLog(workspace / "log")
    log.append("tool_write", d)
    assert log.read() == []


def test_page_script_has_the_live_write_row() -> None:
    from axon.features import dash_js_chat
    js = dash_js_chat.__dict__.get("JS") or "".join(v for v in vars(dash_js_chat).values() if isinstance(v, str) and "function toolStart" in v)
    assert "case 'tool_write': liveWrite(C, ev)" in js and "liveWriteDone(cur, ev.id)" in js
