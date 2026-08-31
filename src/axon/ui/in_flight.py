"""
In-Flight Live Background Input Listener & Prompt Queuing Engine.
Captures user keystrokes, queues follow-up prompts, and handles /btw side inquiries
concurrently while the main agent is executing tools or streaming responses.
"""
from __future__ import annotations
import os
import select
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axon.agent.loop import Agent

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ModuleNotFoundError:
    termios = None  # type: ignore
    tty = None      # type: ignore
    _HAS_TERMIOS = False

from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE


class InFlightInputListener:
    """
    Context manager that listens on stdin in the background during agent turn execution.
    Allows the user to queue prompts (or execute /btw side queries) without waiting
    for the current turn or tools to finish.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_attr: Any = None
        self._fd: int | None = None
        self._buffer: list[str] = []
        self._lock = threading.Lock()

    def __enter__(self) -> InFlightInputListener:
        if not sys.stdin.isatty() or not _HAS_TERMIOS:
            return self

        try:
            self._fd = sys.stdin.fileno()
            self._old_attr = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._fd = None
            return self

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="InFlightInputListener")
        self._thread.start()
        sys.stdout.write(f"\n  {DARK_SLATE}📥 In-flight queuing active · Type follow-ups or /btw anytime · Enter to queue{RST}\n")
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.25)
        if self._fd is not None and self._old_attr is not None and termios is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attr)
            except Exception:
                pass

        # If any remaining text in buffer when turn finishes, auto-queue it
        with self._lock:
            remaining = "".join(self._buffer).strip()
            if remaining and hasattr(self.agent, "message_queue"):
                item = self.agent.message_queue.push(remaining)
                sys.stdout.write(f"\n  {MINT}📥 [Queued for next turn · #{item.id}]:{RST} {WHITE}{BOLD}{remaining}{RST}\n")
                sys.stdout.flush()
                self._buffer = []

    def _listen_loop(self) -> None:
        """Background loop reading non-blocking keystrokes while turn runs."""
        while not self._stop_event.is_set():
            if self._fd is None:
                break
            try:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if not r:
                    continue

                raw = os.read(self._fd, 1024)
                if not raw:
                    break

                # Ctrl+C -> Signal abort
                if raw == b"\x03":
                    if hasattr(self.agent, "cancel_current_turn"):
                        self.agent.cancel_current_turn()
                    sys.stdout.write(f"\n  {ROSE}⏹ Cancel signal received (Ctrl+C){RST}\n")
                    sys.stdout.flush()
                    continue

                # Enter -> Submit queued message or /btw side question
                if raw in (b"\r", b"\n", b"\r\n"):
                    with self._lock:
                        line = "".join(self._buffer).strip()
                        self._buffer = []

                    if line:
                        if line.startswith(("/btw", "/ask", "/side")):
                            question = line.split(" ", 1)[1].strip() if " " in line else ""
                            if question:
                                sys.stdout.write(f"\r\033[K  {GOLD}💬 [Side inquiry (/btw)]:{RST} {WHITE}{BOLD}{question}{RST}\n")
                                sys.stdout.flush()
                                threading.Thread(
                                    target=self._run_async_btw,
                                    args=(question,),
                                    daemon=True,
                                ).start()
                        elif hasattr(self.agent, "message_queue"):
                            item = self.agent.message_queue.push(line)
                            pending_count = len(self.agent.message_queue)
                            sys.stdout.write(
                                f"\r\033[K  {CYAN}📥 [Queued for next turn · #{item.id} ({pending_count} pending)]:{RST} {WHITE}{BOLD}{line}{RST}\n"
                            )
                            sys.stdout.flush()
                    else:
                        sys.stdout.write("\r\033[K")
                        sys.stdout.flush()
                    continue

                # Backspace
                if raw in (b"\x7f", b"\x08"):
                    with self._lock:
                        if self._buffer:
                            self._buffer.pop()
                        curr = "".join(self._buffer)
                    if curr:
                        sys.stdout.write(f"\r\033[K  {GOLD}› [Queue / /btw]{RST} {WHITE}{BOLD}{curr}{RST}")
                    else:
                        sys.stdout.write("\r\033[K")
                    sys.stdout.flush()
                    continue

                # Ctrl+U (Clear line)
                if raw == b"\x15":
                    with self._lock:
                        self._buffer = []
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()
                    continue

                # Normal printable text
                try:
                    text = raw.decode("utf-8", errors="ignore")
                    with self._lock:
                        for ch in text:
                            if ord(ch) >= 32 or ch == "\t":
                                self._buffer.append(ch)
                        curr = "".join(self._buffer)
                    if curr:
                        sys.stdout.write(f"\r\033[K  {GOLD}› [Queue / /btw]{RST} {WHITE}{BOLD}{curr}{RST}")
                        sys.stdout.flush()
                except Exception:
                    pass

            except Exception:
                break

    def _run_async_btw(self, question: str) -> None:
        """Executes a concurrent side question without blocking the main agent turn."""
        from axon.commands.builtin import handle_btw
        try:
            handle_btw(self.agent, question)
        except Exception as e:
            sys.stdout.write(f"\n  ❌ Side inquiry failed: {e}\n")
            sys.stdout.flush()
