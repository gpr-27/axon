"""Read returns a real image: ToolResultBlock.images, provider encoding, fallback, and persistence by reference."""
from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

from axon.providers.anthropic import AnthropicProvider, to_anthropic_messages
from axon.providers.base import ImageRef, ToolResultBlock
from axon.providers.openai_compat import sanitize_openai_messages
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _png(path: Path) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")
    raw = b"\x00\xff\x00\x00"
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00")
            + chunk(b"IDAT", zlib.compress(raw + b"\x00\x00")) + chunk(b"IEND", b""))
    path.write_bytes(data)
    return data


class NoImages(FakeProvider):
    def supports(self, feature: str) -> bool:
        return feature != "tool_images"


def test_read_attaches_the_picture_when_the_provider_can_take_it(workspace: Path) -> None:
    img = workspace / "shot.png"
    _png(img)
    agent = make_agent(FakeProvider(scripted(("Read", {"path": str(img)}), "seen")), workspace)
    agent.run_turn("look")
    block = next(b for m in agent.conversation.messages if isinstance(m.get("content"), list)
                 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
    assert block["images"] == [{"path": str(img.resolve()), "media_type": "image/png"}]
    assert "attached" in block["content"] and "cannot carry" not in block["content"]


def test_read_falls_back_to_text_when_the_provider_cannot(workspace: Path) -> None:
    img = workspace / "shot.png"
    _png(img)
    agent = make_agent(NoImages(scripted(("Read", {"path": str(img)}), "ok")), workspace)
    agent.run_turn("look")
    blocks = [b for m in agent.conversation.messages if isinstance(m.get("content"), list)
              for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert "images" not in blocks[0] and "[Image file:" in blocks[0]["content"]


def test_anthropic_encodes_refs_and_materialises_base64_only_on_the_wire(workspace: Path) -> None:
    img = workspace / "a.png"
    raw = _png(img)
    res = ToolResultBlock("t1", "[Image file]", images=(ImageRef(str(img), "image/png"),))
    (msg,) = AnthropicProvider.encode_tool_results(object.__new__(AnthropicProvider), [res])
    assert msg["content"][0]["images"] == [{"path": str(img), "media_type": "image/png"}]
    assert "base64" not in json.dumps(msg)                              # history holds the reference
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}, msg]
    wire = to_anthropic_messages(history)
    content = wire[1]["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "[Image file]"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(raw).decode()}
    assert "images" not in wire[1]["content"][0]


def test_openai_wire_sends_the_image_in_a_following_user_message_or_drops_it(workspace: Path) -> None:
    img = workspace / "a.png"
    _png(img)
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[Image file]",
                                             "images": [{"path": str(img), "media_type": "image/png"}]}]}]
    with_img = sanitize_openai_messages(history, tool_images=True)
    assert [m["role"] for m in with_img] == ["assistant", "tool", "user"]
    assert "images" not in with_img[1]
    part = with_img[2]["content"][-1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
    without = sanitize_openai_messages(history)
    assert [m["role"] for m in without] == ["assistant", "tool"] and "images" not in without[1]


def test_missing_file_is_dropped_quietly(workspace: Path) -> None:
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "text",
                                             "images": [{"path": str(workspace / "gone.png"), "media_type": "image/png"}]}]}]
    wire = to_anthropic_messages(history)
    assert wire[1]["content"][0]["content"] == "text"


def test_session_records_the_reference_not_base64_and_resume_restores_it(workspace: Path) -> None:
    img = workspace / "shot.png"
    _png(img)
    agent = make_agent(FakeProvider(scripted(("Read", {"path": str(img)}), "seen")), workspace)
    agent.run_turn("look")
    text = agent.session.active_file.read_text(encoding="utf-8")
    assert str(img.resolve()) in text and "iVBOR" not in text            # PNG base64 always starts "iVBOR"
    conv = agent.session.read_conversation(agent.session.active_session_id)
    blocks = [b for m in conv.messages if isinstance(m.get("content"), list)
              for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert blocks[0]["images"] == [{"path": str(img.resolve()), "media_type": "image/png"}]
