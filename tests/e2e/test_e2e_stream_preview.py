"""In the real REPL a sentence is visible while it is still arriving, and ends up in scrollback exactly once."""
import pytest

pytest.importorskip("pty")


def test_partial_line_is_visible_during_streaming(axon_env):
    axon_env.llm.enqueue([{"text": "The quick brown"}, {"delay": 1.5}, {"text": " fox.\n"}])
    with axon_env.pty() as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("hello\r")
        session.expect(r"The quick brown", 10)        # shown before the delay ends and the line is complete
        assert "brown fox" not in session.screen
        session.expect(r"The quick brown fox\.", 10)
        session.expect(r"Done in", 20)


def _stream_script(n: int = 50, delay: float = 0.05):
    words = "streaming words arrive steadily and the line keeps growing until it wraps around".split()
    steps = []
    for i in range(n):
        steps += [{"text": " ".join(words[(i * 2) % 8:(i * 2) % 8 + 2]) + " "}, {"delay": delay}]
    return steps + [{"text": "\nTHE-END-MARK\n"}]


def _read_timed(session, seconds: float, stop: bytes = b""):
    import os
    import select
    import time
    t0, out = time.monotonic(), []
    while time.monotonic() - t0 < seconds:
        r, _, _ = select.select([session._master], [], [], 0.05)
        if r:
            try:
                d = os.read(session._master, 65536)
            except OSError:
                break
            if not d:
                break
            out.append((time.monotonic() - t0, d))
            session._raw += d
            if stop and stop in b"".join(x[1] for x in out[-4:]):
                break
    return out


def test_live_row_refreshes_fast_and_scrollback_is_clean(axon_env):
    axon_env.llm.enqueue(_stream_script())
    with axon_env.pty() as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("hello\r")
        chunks = _read_timed(session, 15, b"Done in")
        data = b"".join(d for _, d in chunks)
        span = chunks[-1][0] - chunks[0][0]
        print(f"\nterminal stream: {len(chunks) / span:.0f} reads/s, {len(data)} bytes over {span:.1f}s")
        assert len(chunks) / span > 15                           # was ~10 redraws a second
        text = session.screen
        assert text.count("THE-END-MARK") == 1
        assert text.count("streaming words arrive") >= 1


def test_ctrl_c_and_resize_mid_stream_leave_the_screen_sane(axon_env):
    import fcntl
    import os
    import signal
    import struct
    import termios
    axon_env.llm.enqueue(_stream_script(80, 0.1))
    with axon_env.pty() as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("hello\r")
        session.expect(r"streaming words", 15)
        for cols in (80, 50, 120):                               # resize while the live row is on screen
            fcntl.ioctl(session._master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, cols, 0, 0))
            os.killpg(session.proc.pid, signal.SIGWINCH)
            _read_timed(session, 0.4)
        session.send("\x03")                                     # Ctrl+C in the middle of the reply
        _read_timed(session, 3)
        assert session.proc.poll() is None                       # interrupted the turn, not the program
        raw = session._raw.decode("utf-8", "replace")
        assert "Traceback" not in raw
        session.send("\x03")
        session.send("\x04")
        session.wait(5)
