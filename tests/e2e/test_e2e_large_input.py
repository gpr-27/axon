"""A huge paste into the real REPL finishes the turn and sends only a small request."""
import pytest

pytest.importorskip("pty")


def test_huge_paste_is_answered(axon_env):
    axon_env.llm.enqueue([{"text": "DONE-OK"}])
    body = "\n".join(f"line {i}: the quick brown fox" for i in range(30_000))
    with axon_env.pty() as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("\x1b[200~" + body + "\x1b[201~")
        session.expect(r"Pasted text #1 \+30000 lines", 30)
        session.send("\r")
        session.expect(r"DONE-OK", 60)
    requests = axon_env.llm.completion_requests()
    assert len(requests) == 1 and len(str(requests[0].body)) < 200_000
