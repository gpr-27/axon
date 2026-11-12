"""The provider-independent PDF and image reading chain, with fakes and tiny generated PDFs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axon.agent.state import Conversation, FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.tools import create_default_registry, pdf_render
from axon.tools.base import ToolContext
from tests.test_tools_read_pdf import make_pdf


class _Prov:
    def __init__(self, pdf: bool = False, images: bool = False, official: bool = True) -> None:
        self.f = {"tool_pdf": pdf, "tool_images": images}
        self._official = official

    def supports(self, feature: str) -> bool:
        return self.f.get(feature, False)


class _Agent:
    def __init__(self, prov: _Prov, model: str, mode: str = "auto") -> None:
        self.provider = prov
        self.settings = Settings(model=model, pdf_mode=mode)


def _ctx(ws: Path, prov: _Prov, model: str = "some-model", mode: str = "auto") -> ToolContext:
    return ToolContext(workspace=ws, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=ws),
                       agent=_Agent(prov, model, mode))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    pdf_render._shown.clear()
    pdf_render._pending_note.clear()


def _fake_renderer(monkeypatch, calls: list[tuple[int, int]]) -> None:
    def render(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
        calls.append((first, last))
        for n in range(first, last + 1):
            (outdir / f"p-{n}.jpg").write_bytes(b"\xff\xd8fake")
    monkeypatch.setattr(pdf_render, "_find_renderer", lambda: "fake")
    monkeypatch.setitem(pdf_render._RENDER, "fake", render)


def test_chain_selection(workspace: Path) -> None:
    pick = lambda prov, model="m", mode="auto": pdf_render.choose(_ctx(workspace, prov, model, mode))  # noqa: E731
    assert pick(_Prov(pdf=True, images=True)) == "native"
    assert pick(_Prov(pdf=True, images=True), "gpt-5") == "native"          # no Claude-name gate any more
    assert pick(_Prov(pdf=True, images=True, official=False), "my-llm") == "images"
    assert pick(_Prov(images=True)) == "images"
    assert pick(_Prov()) == "ocr"
    assert pick(_Prov(pdf=True, images=True), mode="text") == "text"
    assert pick(_Prov(pdf=True, images=True), mode="images") == "images"
    assert pick(_Prov(images=True), mode="native") == "images"              # provider cannot: falls back


def test_live_caps_decide(workspace: Path, monkeypatch) -> None:
    from axon.providers import model_caps
    model_caps.clear()
    model_caps.record([{"id": "no-pdf", "capabilities": {"pdf_input": {"supported": False}, "image_input": {"supported": True}}},
                       {"id": "blind", "capabilities": {"image_input": {"supported": False}}}])
    try:
        assert pdf_render.choose(_ctx(workspace, _Prov(pdf=True, images=True), "no-pdf")) == "images"
        assert pdf_render.choose(_ctx(workspace, _Prov(images=True), "blind")) == "ocr"
    finally:
        model_caps.clear()


def test_openrouter_style_modalities() -> None:
    from axon.providers import model_caps
    c = model_caps.parse_model_info({"id": "x/vl", "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text", "image"]}})
    assert c is not None and c.features["image_input"] is True


def test_images_mode_range_limits_cache_and_card(workspace: Path, monkeypatch) -> None:
    make_pdf(workspace / "d.pdf", [f"text of page {i}" for i in range(1, 31)])
    calls: list[tuple[int, int]] = []
    _fake_renderer(monkeypatch, calls)
    read = create_default_registry().get("Read")
    ctx = _ctx(workspace, _Prov(images=True))
    out = read.run({"path": "d.pdf", "pages": "2-4"}, ctx)
    assert len(ctx.images) == 3 and all(i.media_type == "image/jpeg" for i in ctx.images)
    assert "text of page 3" in out
    assert read.summarize({}, out) == "PDF · pages 2-4 of 30 · rendered as images"
    ctx2 = _ctx(workspace, _Prov(images=True))
    out2 = read.run({"path": "d.pdf"}, ctx2)                  # default: first 10 pages, and says how to continue
    assert len(ctx2.images) == 10 and 'pages="11-20"' in out2
    assert read.summarize({}, out2) == "PDF · pages 1-10 of 30 · rendered as images"
    before = len(calls)
    read.run({"path": "d.pdf", "pages": "2-4"}, _ctx(workspace, _Prov(images=True)))
    assert len(calls) == before                                # cached pages are not rendered again
    with pytest.raises(ToolError, match=r'pages="3-12"'):
        read.run({"path": "d.pdf", "pages": "3-20"}, _ctx(workspace, _Prov(images=True)))
    assert "beyond the end" in read.run({"path": "d.pdf", "pages": "40"}, _ctx(workspace, _Prov(images=True)))


def test_whole_short_pdf_card(workspace: Path, monkeypatch) -> None:
    make_pdf(workspace / "s.pdf", ["a", "b", "c"])
    _fake_renderer(monkeypatch, [])
    read = create_default_registry().get("Read")
    out = read.run({"path": "s.pdf"}, _ctx(workspace, _Prov(images=True)))
    assert read.summarize({}, out) == "PDF · 3 pages · rendered as images"


def test_no_renderer_falls_back_to_text_and_tells_once(workspace: Path, monkeypatch) -> None:
    make_pdf(workspace / "s.pdf", ["hello world text"])
    monkeypatch.setattr(pdf_render, "_find_renderer", lambda: None)
    read = create_default_registry().get("Read")
    first = read.run({"path": "s.pdf"}, _ctx(workspace, _Prov(images=True)))
    second = read.run({"path": "s.pdf"}, _ctx(workspace, _Prov(images=True)))
    assert "hello world text" in first and "install poppler" in first.lower().replace("install poppler", "install poppler")
    assert "brew install poppler" in first and "brew install poppler" not in second
    assert read.summarize({}, second) == "PDF · 1 page · text extracted (images not included)"


def test_ocr_for_scanned_pages_of_a_text_only_model(workspace: Path, monkeypatch) -> None:
    make_pdf(workspace / "scan.pdf", ["", "typed page two text"])
    _fake_renderer(monkeypatch, [])
    monkeypatch.setattr(pdf_render, "_ocr", lambda p: "scanned words")
    monkeypatch.setattr(pdf_render, "_page_text", lambda path, n: "" if n == 1 else "typed page two text")
    read = create_default_registry().get("Read")
    ctx = _ctx(workspace, _Prov())
    out = read.run({"path": "scan.pdf"}, ctx)
    assert not ctx.images
    assert "[OCR text — may contain errors]\nscanned words" in out and "typed page two text" in out
    assert read.summarize({}, out) == "PDF · 2 pages · OCR text"


def test_text_only_model_with_text_layer_stays_plain_text(workspace: Path, monkeypatch) -> None:
    make_pdf(workspace / "t.pdf", ["plain text page"])
    _fake_renderer(monkeypatch, [])
    read = create_default_registry().get("Read")
    out = read.run({"path": "t.pdf"}, _ctx(workspace, _Prov()))
    assert "plain text page" in out and "OCR" not in out
    assert read.summarize({}, out).endswith("text extracted (images not included)")


def test_native_gate_has_no_claude_check(workspace: Path) -> None:
    make_pdf(workspace / "d.pdf", ["x"])
    ctx = _ctx(workspace, _Prov(pdf=True), model="gpt-5")
    out = create_default_registry().get("Read").run({"path": "d.pdf"}, ctx)
    assert [i.media_type for i in ctx.images] == ["application/pdf"] and "natively" in out


class _Att:
    label = "[Image #1]"
    original_path = "/x/shot.png"
    mime_type = "image/png"
    width = height = 10

    def read_base64(self) -> str:
        return "QUJD"

    def get_ocr_text(self) -> str:
        return "text in the screenshot"


def _attach(monkeypatch, vision: bool) -> Any:
    monkeypatch.setattr("axon.agent.images.compact_image_paths", lambda t: ("what is this?", [_Att()]))
    conv = Conversation()
    conv.append_user("look /x/shot.png", vision=vision)
    return conv.messages[-1]["content"]


def test_user_image_goes_as_block_for_a_vision_model(monkeypatch) -> None:
    content = _attach(monkeypatch, True)
    assert content[0]["type"] == "image" and "text in the screenshot" in content[-1]["text"]


def test_user_image_becomes_ocr_text_for_a_text_only_model(monkeypatch) -> None:
    content = _attach(monkeypatch, False)
    assert isinstance(content, str) and "text in the screenshot" in content
    assert "cannot see images" in content and "what is this?" in content


def test_agent_vision_per_provider(workspace: Path) -> None:
    assert pdf_render.agent_vision(_Agent(_Prov(images=True), "claude-x"))
    assert not pdf_render.agent_vision(_Agent(_Prov(), "gpt-4o"))
    assert pdf_render.name_looks_visual("meta/llama-4-scout") and not pdf_render.name_looks_visual("deepseek-v4-flash")
