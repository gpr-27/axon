"""Prompt keys through a real pseudo-terminal: paste chips, terminals without bracketed paste, drops, shortcuts."""
from __future__ import annotations

import pytest

from harness import AxonEnv, IS_WINDOWS

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")]

BIG = "".join(f"row {i} some ordinary content\r" for i in range(40))


def _ready(s) -> None:
    s.expect(r"(?i)(axon|❯|>)", timeout=15)
    s.drain(0.5)


def test_bracketed_paste_chip_shows_notice_and_alt_e_expands(axon_env: AxonEnv) -> None:
    with axon_env.pty() as s:
        _ready(s)
        m = s.mark()
        s.send("\x1b[200~" + BIG + "\x1b[201~")
        s.expect(r"Pasted text #1 \+4\d lines", timeout=10, since=m)
        s.expect(r"tokens", timeout=5, since=m)
        assert not axon_env.llm.completion_requests()
        m = s.mark()
        s.send("\x1be")                                   # alt+e: expand in place
        s.expect(r"row 39 some ordinary content", timeout=10, since=m)
        s.send("\x03")                                    # ctrl+c clears the draft
        s.drain(0.3)
        s.sendline("/exit")
        assert s.wait(10) == 0


def test_multiline_paste_without_bracketed_paste_does_not_submit(axon_env: AxonEnv) -> None:
    with axon_env.pty() as s:
        _ready(s)
        m = s.mark()
        s.send("first pasted line\rsecond pasted line\rthird pasted line\r")   # one chunk, no markers
        s.expect(r"third pasted line", timeout=10, since=m)
        s.drain(0.5)
        assert not axon_env.llm.completion_requests(), "a paste without bracket markers must not submit"
        s.send("\x03")
        s.drain(0.3)
        s.sendline("/exit")
        assert s.wait(10) == 0


def test_ctrl_s_stashes_and_restores_the_draft(axon_env: AxonEnv) -> None:
    with axon_env.pty() as s:
        _ready(s)
        s.send("my half written thought")
        s.expect("my half written thought", timeout=5)
        m = s.mark()
        s.send("\x13")
        s.expect(r"stashed", timeout=5, since=m)
        m = s.mark()
        s.send("\x13")
        s.expect("my half written thought", timeout=5, since=m)
        s.send("\x15")
        s.sendline("/exit")
        assert s.wait(10) == 0


def test_typed_image_path_without_bracketed_paste_becomes_a_chip(axon_env: AxonEnv) -> None:
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    img = axon_env.workspace / "shot.png"
    img.write_bytes(png)
    axon_env.llm.enqueue([{"text": "SAW-IMAGE"}])
    with axon_env.pty() as s:
        _ready(s)
        m = s.mark()
        s.send(f"describe {img}\r")
        s.expect("SAW-IMAGE", timeout=20, since=m)
        s.sendline("/exit")
        assert s.wait(10) == 0
    req = axon_env.llm.completion_requests()[-1]
    assert "image" in str(req.body).lower()
