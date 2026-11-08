"""Typing @pic.png gives the model one real image block, like dragging the picture in; never a text description."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _blocks(req) -> list[dict]:
    out = []
    for m in req.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            out += [b for b in m["content"] if isinstance(b, dict)]
    return out


def _images(req) -> list[dict]:
    return [b for b in _blocks(req) if b.get("type") in ("image", "image_url")]


def _user_text(req) -> str:
    return "\n".join(str(b.get("text", "")) for b in _blocks(req) if b.get("type") == "text")


def test_mention_turns_into_an_image_reference_whatever_the_current_folder(tmp_path, monkeypatch):
    from axon.cli import expand_file_mentions
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pic.png").write_bytes(PNG)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)             # the app's server does not run inside the workspace
    out = expand_file_mentions("look at @pic.png please", ws)
    assert f"[Image: {(ws / 'pic.png').resolve()}]" in out
    assert "[File:" not in out


def test_one_block_one_attachment_not_two(tmp_path, monkeypatch):
    from axon.agent.state import Conversation
    from axon.cli import expand_file_mentions
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pic.png").write_bytes(PNG)
    monkeypatch.chdir(tmp_path)
    conv = Conversation()
    conv.append_user(expand_file_mentions("see @pic.png and again @pic.png", ws))
    content = conv.messages[0]["content"]
    assert [b["type"] for b in content].count("image") == 1


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_terminal_print_mode_sends_an_image_block(tmp_path, wire):
    from fake_llm import FakeLLMServer
    from harness import create_axon_env
    llm = FakeLLMServer(model_ids=("gpt-4o", "claude-sonnet-4-6")).start()
    try:
        axon_env = create_axon_env(tmp_path, llm)
        if wire == "anthropic":
            axon_env.use_anthropic_wire()
        (axon_env.workspace / "pic.png").write_bytes(PNG)
        llm.enqueue([{"text": "SAW"}])
        r = axon_env.run(["-p", "what is in @pic.png ?"], timeout=60)
        assert r.code == 0, r.stderr
        req = llm.completion_requests()[-1]
        assert req.wire == wire
        assert len(_images(req)) == 1
        text = _user_text(req)
        assert "[File:" not in text and "what is in" in text
    finally:
        llm.stop()


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_app_sends_an_image_block(c, env, llm, monkeypatch, wire):
    if wire == "anthropic":
        monkeypatch.setenv("AXON_PROVIDER", "anthropic")
        monkeypatch.setenv("AXON_MODEL", "claude-sonnet-4-6")
    (env.ws / "pic.png").write_bytes(PNG)
    llm.enqueue([{"text": "SAW"}])
    r = json.loads(c.post("/api/chat/send", {"text": "what is in @pic.png ?"})[2])
    rt = c.srv.runtime(r["session"], create=False)
    deadline = time.time() + 20
    while time.time() < deadline and (rt.busy or not llm.completion_requests()):
        time.sleep(0.05)
    req = llm.completion_requests()[-1]
    assert req.wire == wire
    assert len(_images(req)) == 1, req.body
    assert "[File:" not in _user_text(req)
