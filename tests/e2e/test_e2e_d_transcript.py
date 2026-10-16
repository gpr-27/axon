"""
Agent-D e2e: the full-screen transcript viewer (Ctrl+F / ``/transcript``) driven through a real pty.

Runs a tool turn against the fake LLM, opens the viewer with the key, expands the tool call with
an SGR mouse click (and collapses it with Enter), closes it with ``q`` and checks the prompt is
back with mouse reporting switched off again.
"""
from __future__ import annotations

import re
import time

import pytest

from harness import AxonEnv, strip_ansi
from test_e2e_d_helpers import PROMPT_READY, TRACEBACK, open_repl, posix_only, tool_turn

pytestmark = [pytest.mark.e2e, posix_only]

CTRL_F = "\x06"
MOUSE_ON = ("\x1b[?1000h", "\x1b[?1006h")
MOUSE_OFF = ("\x1b[?1006l", "\x1b[?1000l")
_ROW_RE = re.compile(r"\x1b\[(\d+);1H(.*?)\x1b\[K", re.S)


def _last_frame(raw: str) -> dict[int, str]:
    """Row number -> plain text of the most recent full-screen frame in ``raw``."""
    start = raw.rfind("\x1b[1;1H")
    return {int(m.group(1)): strip_ansi(m.group(2)) for m in _ROW_RE.finditer(raw[start:] if start >= 0 else raw)}


def _wait_frame(s, offset: int, pred, timeout: float = 8.0) -> dict[int, str]:
    deadline = time.monotonic() + timeout
    frame: dict[int, str] = {}
    while time.monotonic() < deadline:
        s.drain(0.15)
        frame = _last_frame(s.raw_since(offset))
        if frame and pred(frame):
            return frame
    pytest.fail("viewer frame never matched:\n" + "\n".join(f"{k:>3} {v}" for k, v in sorted(frame.items())))


def test_ctrl_f_opens_transcript_click_expands_tool_output_q_restores_prompt(axon_env: AxonEnv) -> None:
    """Ctrl+F opens the transcript; a click expands the tool call to its full numbered output; Enter
    collapses it; q closes the alternate screen, turns mouse reporting off and the prompt works again."""
    cmd = "for i in $(seq 1 40); do echo row-$i; done"
    axon_env.llm.enqueue(*tool_turn(cmd, "FINAL-VIEW"))
    with open_repl(axon_env, ("--mode", "bypass")) as s:
        s.turn("go")
        raw0 = len(s._raw)
        s.send(CTRL_F)
        frame = _wait_frame(s, raw0, lambda f: any("Transcript" in v for v in f.values())
                            and any("FINAL-VIEW" in v for v in f.values()))
        raw = s.raw_since(raw0)
        assert "\x1b[?1049h" in raw and all(seq in raw for seq in MOUSE_ON)
        assert any("click/enter expand" in v for v in frame.values())
        tool_y = next(y for y, v in sorted(frame.items()) if "Bash" in v and "row-$i" in v)
        assert not any("row-25" in v for v in frame.values())

        # left click on the tool row -> expanded: input JSON + full numbered output
        raw1 = len(s._raw)
        s.send(f"\x1b[<0;6;{tool_y}M\x1b[<0;6;{tool_y}m")
        frame = _wait_frame(s, raw1, lambda f: any("row-25" in v for v in f.values()))
        text = "\n".join(frame.values())
        assert "Input" in text and "Output" in text and re.search(r"\b25 . row-25", text), text

        # Enter on the (now selected) tool row collapses it again
        raw2 = len(s._raw)
        s.send("\r")
        frame = _wait_frame(s, raw2, lambda f: not any("row-25" in v for v in f.values())
                            and any("Bash" in v for v in f.values()))

        # q closes: alternate screen left, mouse off after the last enable, prompt back
        raw3 = len(s._raw)
        m = s.mark()
        s.send("q")
        s.expect(PROMPT_READY, timeout=10, since=m)
        s.drain(0.5)
        tail = s.raw_since(raw3)
        assert "\x1b[?1049l" in tail and all(seq in tail for seq in MOUSE_OFF), repr(tail[:400])
        whole = s.raw_since(0)
        assert max(whole.rfind(seq) for seq in MOUSE_ON) < min(whole.rfind(seq) for seq in MOUSE_OFF)
        assert tail.rfind("\x1b[?1049l") < tail.rfind("Ask anything")  # prompt drawn after leaving the view

        # the editor still works and no mouse report leaked into it as text
        m = s.mark()
        s.type("hello-after-view")
        after = s.screen[m:]
        assert "hello-after-view" in after and "[<0;" not in after and "<0;6" not in after
        assert TRACEBACK not in s.screen
