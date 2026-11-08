"""Drive the real REPL in a pseudo-terminal through /usage and every tab (POSIX only)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
from fake_llm import FakeLLMServer  # noqa: E402
from harness import create_axon_env, strip_ansi  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty-driven REPL")

RIGHT, LEFT, ESC = "\x1b[C", "\x1b[D", "\x1b"


def _last_screen(text: str) -> str:
    """The most recent frame of the full-screen view (frames start with the title row)."""
    return text.rsplit("◆ Usage", 1)[-1]


@pytest.mark.parametrize("cols", [60, 120])
def test_usage_screen_tabs_and_close(tmp_path, cols) -> None:
    llm = FakeLLMServer(model_ids=("gpt-4o", "fake-model")).start()
    try:
        env = create_axon_env(tmp_path, llm)
        with env.pty([], rows=40, cols=cols) as s:
            s.expect(r"(?i)(axon|❯|>)", timeout=25)
            s.drain(0.8)
            s.sendline("/usage")
            s.expect(r"Footprint every turn", timeout=15)
            screen = s.drain(0.5)
            assert "Status" in screen and "Config" in screen and "Stats" in screen and "Context" in screen
            assert "no limit data from this provider" in _last_screen(screen) or "no limit data" in screen
            seen = {}
            for name, marker in (("stats", r"Axon usage"), ("context", r"Context usage"), ("status", r"Axon status"),
                                 ("config", r"Auto-compact at")):
                mark = s.mark()
                s.send(RIGHT)
                s.expect(marker, timeout=10, since=mark)
                seen[name] = True
            mark = s.mark()
            s.send(ESC)
            s.drain(0.6)
            s.sendline("/status")
            s.expect(r"Axon status", timeout=10, since=mark)
            s.sendline("/exit")
            s.wait(10)
        assert all(seen.values())
    finally:
        llm.stop()
