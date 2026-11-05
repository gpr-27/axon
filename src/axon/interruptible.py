"""
Waits that Esc can cut short.

Esc stops a turn by raising KeyboardInterrupt in the turn's thread (Agent.cancel_current_turn).
Python delivers that exception only between bytecodes, so a thread blocked inside one long C call
(time.sleep, Future.result, a socket read, Popen.communicate) does not see it until the call
returns, which can be many seconds later. The helpers here wait in short slices instead, so the
interrupt lands within POLL_SECONDS.
"""
from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

POLL_SECONDS = 0.1


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + max(0.0, timeout)


def _slice(deadline: float | None) -> float:
    """How long the next short wait may last."""
    if deadline is None:
        return POLL_SECONDS
    return max(0.0, min(POLL_SECONDS, deadline - time.monotonic()))


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def sleep(seconds: float) -> None:
    """time.sleep(seconds), in short slices."""
    deadline = _deadline(seconds)
    while not _expired(deadline):
        time.sleep(_slice(deadline))


def wait_future(future: Future[Any], timeout: float | None = None) -> Any:
    """future.result(timeout), in short slices. Raises TimeoutError when the timeout passes."""
    deadline = _deadline(timeout)
    while True:
        try:
            return future.result(timeout=_slice(deadline))
        except FutureTimeout:
            if _expired(deadline):
                raise


def wait_process(proc: subprocess.Popen[Any], timeout: float | None = None) -> int:
    """proc.wait(timeout), in short slices. Raises subprocess.TimeoutExpired when the timeout passes."""
    deadline = _deadline(timeout)
    while True:
        try:
            return proc.wait(timeout=_slice(deadline))
        except subprocess.TimeoutExpired:
            if _expired(deadline):
                raise subprocess.TimeoutExpired(proc.args, timeout or 0.0) from None


def communicate(proc: subprocess.Popen[Any], input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
    """proc.communicate(input, timeout), in short slices. Raises subprocess.TimeoutExpired when the
    timeout passes. The input is handed over on the first call only (Popen keeps sending the rest)."""
    deadline = _deadline(timeout)
    pending_input = input
    while True:
        try:
            return proc.communicate(pending_input, timeout=_slice(deadline))
        except subprocess.TimeoutExpired:
            pending_input = None
            if _expired(deadline):
                raise subprocess.TimeoutExpired(proc.args, timeout or 0.0) from None


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run fn(*args, **kwargs) on a helper thread and wait for it in short slices.

    For blocking calls that cannot be sliced themselves (an HTTP request). After an interrupt the
    helper thread is left to finish on its own; it is a daemon, and the call should have a timeout."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 - handed to the waiting thread
            box["error"] = error
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="axon-interruptible").start()
    while not done.wait(POLL_SECONDS):
        pass
    if "error" in box:
        raise box["error"]
    return box.get("value")
