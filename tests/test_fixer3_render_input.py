"""FIXER-3 regression tests: markdown wrap/no text loss, code soft-wrap, rail width, sanitizing,
paste heuristic, PYTHONIOENCODING, stream cancellation, approval grants."""
from __future__ import annotations

import io
import random
import re
import socket
import sys
import threading
import time
import types

import pytest

from axon.ui import theme
from axon.ui.markdown import MarkdownStream, format_markdown
from axon.ui.theme import display_width, strip_ansi


def _stream(md: str, width: int, step: int) -> str:
    s = MarkdownStream(width=width)
    out = "".join(s.feed(md[i:i + step]) for i in range(0, len(md), step))
    return out + s.flush()


_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda"]


def _paragraph(rng: random.Random) -> tuple[str, list[str]]:
    """Random markdown paragraph and the words its rendering must contain."""
    src, words = [], []
    for _ in range(rng.randint(5, 40)):
        w = rng.choice(_WORDS) + str(rng.randint(0, 99))
        kind = rng.randint(0, 7)
        words.append(w)
        if kind == 0:
            src.append(f"`{w}`")
        elif kind == 1:
            src.append(f"*{w}*")
        elif kind == 2:
            src.append(f"**{w}**")
        elif kind == 3:
            src.append(f"~~{w}~~")
        elif kind == 4:
            w2 = rng.choice(_WORDS)
            words.append(w2)
            src.append(f"[{w} {w2}](https://example.com/{w})")
        else:
            src.append(w)
    return " ".join(src) + ".", words


@pytest.mark.parametrize("seed", range(40))
def test_markdown_never_loses_words(seed: int) -> None:
    rng = random.Random(seed)
    para, words = _paragraph(rng)
    prefix = rng.choice(["", "- ", "1. ", "- [ ] "])
    width = rng.randint(20, 100)
    step = rng.choice([1, 2, 5, 13, 10_000])
    out = " ".join(strip_ansi(_stream(prefix + para + "\n", width, step)).split())
    for w in words:
        assert w in out, (w, out, para, width, step)
    assert "](" not in out and "**" not in out and "~~" not in out


def test_link_across_wrap_boundary() -> None:
    md = "Use `inline code` and *italic* and ~~strike~~ and [a link](https://example.com).\n"
    for step in (1, 3, 7, 1000):
        out = " ".join(strip_ansi(_stream(md, 58, step)).split())
        assert "a link." in out and "[a" not in out


def test_code_block_soft_wraps_with_marker() -> None:
    md = "```python\nx = 1  # " + "word " * 30 + "\n```\n"
    out = format_markdown(md, 40)
    rows = out.splitlines()
    assert all(display_width(r) <= 40 for r in rows), rows
    assert any(theme.glyph("wrap") in r for r in rows)
    # the card frame lives in the gutter ('│ ' rows between '╭── python' and '╰──')
    body = "".join(strip_ansi(r).strip().lstrip(theme.glyph("v")).strip().lstrip(theme.glyph("wrap")).strip()
                   for r in rows[1:-1])
    assert body.replace(" ", "") == ("x = 1  # " + "word " * 30).replace(" ", "")


def test_hard_wrap_ansi_reopens_styles() -> None:
    rows = theme.hard_wrap_ansi("\x1b[31m" + "a" * 25 + "\x1b[0m", 10)
    assert len(rows) == 3 and all(display_width(r) <= 10 for r in rows)
    assert rows[1].startswith("\x1b[31m")


def _renderer(monkeypatch, cols: int):
    from axon.ui import render
    monkeypatch.setattr(render, "term_width", lambda: cols)
    buf = io.StringIO()
    monkeypatch.setattr(render, "_write", buf.write)
    r = render.Renderer()
    r._turn_open = True
    return r, buf


@pytest.mark.parametrize("cols", [40, 44, 46, 50, 80])
def test_rail_rows_never_exceed_width(monkeypatch, cols: int) -> None:
    r, buf = _renderer(monkeypatch, cols)
    r._line("cache usage (caching unsupported or not reported) " * 6, "    ")
    r._line("x" * 300, "  ")
    for row in buf.getvalue().splitlines():
        assert display_width(row) <= cols, (row, display_width(row))


def test_atomic_line_wraps_only_at_separators(monkeypatch) -> None:
    from axon.ui import render
    r, buf = _renderer(monkeypatch, 40)
    dot = theme.glyph("dot")
    r._line(f"12.4k in {dot} 412 out {dot} 1.9s {dot} $0.0031 {dot} ctx 6% of 200k", "  ", atomic=True)
    rows = [strip_ansi(x) for x in buf.getvalue().splitlines()]
    assert any("ctx 6% of 200k" in x for x in rows), rows
    assert all(display_width(x) <= 40 for x in rows)
    assert render._NBSP not in buf.getvalue()


def test_railed_response_keeps_rail(monkeypatch) -> None:
    from axon.providers.base import TextDelta
    r, buf = _renderer(monkeypatch, 80)
    r.level  # noqa: B018 - property exists
    r._on_text(TextDelta(text="I'll look at the files.\n\nSecond paragraph.\n"))
    r._finish_markdown()
    rows = [strip_ansi(x) for x in buf.getvalue().splitlines() if x.strip()]
    body = [x for x in rows if "look at" in x or "Second" in x]
    assert body and all(x.startswith(theme.glyph("v")) for x in body), rows


def test_model_text_is_sanitized_in_trace_rows(monkeypatch) -> None:
    from axon.ui import render
    evil = "ok \x1b[31mRED\x1b[0m \x1b]0;pwned\x07 done"
    assert "\x1b" not in render._one_line(evil) and "\x07" not in render._one_line(evil)
    assert "\x1b" not in render.tool_target("Bash", {"command": evil})
    assert "\x1b" not in render.tool_target("Task", {"description": evil})
    card = render.render_subagent_result_card(f"[Subagent #1 Result ({evil})]: body")
    assert "pwned" not in card or "\x1b]0;" not in card


# ── input ────────────────────────────────────────────────────────────────

def test_paste_heuristic() -> None:
    from axon import platform as plat
    assert not plat._looks_like_paste("/exit\r")
    assert not plat._looks_like_paste("/exit\n")
    assert not plat._looks_like_paste("x")
    assert plat._looks_like_paste("line one\nline two\n")
    assert plat._looks_like_paste("a" * 300 + "\n")
    assert not plat._looks_like_paste("\x1b[A\r")


def test_explicit_pythonioencoding_is_respected(monkeypatch) -> None:
    from axon import platform as plat
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    plat._reconfigure_streams()
    assert stream.encoding == "cp1252" and stream.errors == "replace"
    monkeypatch.delenv("PYTHONIOENCODING")
    plat._reconfigure_streams()
    assert stream.encoding.lower().replace("-", "") == "utf8"


def test_type_ahead_is_handed_to_next_prompt() -> None:
    from axon.ui import in_flight
    in_flight.take_type_ahead()
    in_flight._stash_type_ahead("fo")
    assert in_flight.take_type_ahead() == "fo" and in_flight.take_type_ahead() == ""


def test_ctrl_c_key_interrupts_like_esc() -> None:
    from axon.ui.in_flight import InFlightInputListener
    calls = []
    agent = types.SimpleNamespace(cancel_current_turn=lambda: calls.append(1))
    lst = InFlightInputListener(agent)  # type: ignore[arg-type]
    lst.handle_key("ctrl-c")
    assert calls == [1]


# ── stream cancellation ──────────────────────────────────────────────────

def test_stream_canceller_wakes_blocked_socket_read() -> None:
    from axon.providers.base import StreamCanceller
    a, b = socket.socketpair()
    resp = types.SimpleNamespace(extensions={"network_stream": types.SimpleNamespace(get_extra_info=lambda k: a)},
                                 close=lambda: None)
    c = StreamCanceller()
    result: dict[str, object] = {}

    def reader() -> None:
        c.begin()
        try:
            with c.track(resp):
                a.recv(10)  # blocks until cancel() shuts the socket down
            c.check()
            result["out"] = "finished"
        except KeyboardInterrupt:
            result["out"] = "interrupted"

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert c.cancel(t.ident)
    t.join(3)
    assert result.get("out") == "interrupted" and time.monotonic() - t0 < 2
    a.close()
    b.close()


def test_stream_cancel_does_not_close_socket_under_the_reader() -> None:
    """Regression: cancel() shut the socket down AND closed the response from the cancelling thread.
    A reader that reached its next recv() after the close polled fd -1 and slept for the whole read
    timeout - Esc then left the REPL stuck on "responding" (~4% of interrupts on macOS)."""
    from axon.providers.base import StreamCanceller
    a, b = socket.socketpair()
    a.settimeout(5)
    resp = types.SimpleNamespace(extensions={"network_stream": types.SimpleNamespace(get_extra_info=lambda k: a)},
                                 close=a.close)  # like httpx: closing the response closes the socket
    c = StreamCanceller()
    cancelled = threading.Event()
    result: dict[str, object] = {}

    def reader() -> None:
        c.begin()
        try:
            with c.track(resp):
                cancelled.wait(3)       # the reader is between two reads when Esc arrives
                result["data"] = a.recv(10)
            c.check()
            result["out"] = "finished"
        except KeyboardInterrupt:
            result["out"] = "interrupted"
        except OSError as e:
            result["out"] = f"oserror {e}"

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    assert c.cancel(t.ident)
    cancelled.set()
    t.join(4)
    assert result.get("out") == "interrupted", result
    assert time.monotonic() - t0 < 2
    a.close()
    b.close()


def test_failover_provider_cancel_delegates() -> None:
    from axon.providers.failover import FailoverProvider
    hits = []
    primary = types.SimpleNamespace(cancel=lambda ident=None: hits.append(ident) or True)
    fp = FailoverProvider(primary, model="m", settings=None, chain=[])
    assert fp.cancel(7) and hits == [7]


# ── approvals ────────────────────────────────────────────────────────────

def test_session_grant_allows_whole_tool() -> None:
    from axon.agent.loop import Agent
    added = []
    fake = types.SimpleNamespace(permissions=types.SimpleNamespace(add_session_rule=lambda k, s: added.append((k, s))))
    Agent._grant_session(fake, "Bash")  # type: ignore[arg-type]
    assert added == [("allow", "Bash")]


def test_narrow_rule_label_for_bash() -> None:
    from axon.ui.approve import narrow_rule_label
    assert narrow_rule_label("Bash", {"command": "git status"}) == "Bash(git status:*)"
    assert narrow_rule_label("Write", {"path": "x"}) == ""
