"""A Stop that lands after a column's agent exists but before its turn thread starts must still end the column."""
from __future__ import annotations

import threading
import time

from axon.features.compare_models import Column, Compare


class _NotStartedYet:
    def __init__(self, misses: int) -> None:
        self.misses = misses
        self.calls = 0
        self.cancelled = threading.Event()

    def cancel_current_turn(self) -> bool:
        self.calls += 1
        if self.calls <= self.misses:
            return False
        self.cancelled.set()
        return True


def _compare(agent) -> tuple[Compare, Column]:
    cmp = Compare.__new__(Compare)
    cmp.lock = threading.RLock()
    col = Column(0, "gpt-4o")
    col.status, col.agent = "running", agent
    cmp.columns = [col]
    return cmp, col


def test_stop_retries_until_the_turn_thread_can_be_interrupted():
    agent = _NotStartedYet(misses=3)
    cmp, col = _compare(agent)
    assert cmp.stop(0)
    assert agent.cancelled.wait(5)
    assert col.stop_requested


def test_stop_gives_up_when_the_column_has_ended():
    agent = _NotStartedYet(misses=10**9)
    cmp, col = _compare(agent)
    assert cmp.stop(0)
    col.status = "stopped"
    time.sleep(0.2)
    seen = agent.calls
    time.sleep(0.2)
    assert agent.calls == seen


def test_a_cancel_that_raises_is_not_retried():
    class Boom:
        calls = 0

        def cancel_current_turn(self):
            Boom.calls += 1
            raise RuntimeError("no")
    cmp, _ = _compare(Boom())
    assert cmp.stop(0)
    time.sleep(0.2)
    assert Boom.calls == 1
