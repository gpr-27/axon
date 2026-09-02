"""
In-Flight Live Background Input Listener & Prompt Queuing Engine.
Captures user keystrokes silently in the background, queues follow-up prompts upon Enter,
and handles /btw side inquiries and /q queue drop/clear commands without streaming collisions.
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
    Allows the user to queue prompts (or execute /btw side queries, /q drop <id>, /q clear)
    without waiting for the current turn or tools to finish.
    Keystrokes are buffered silently in memory during streaming to prevent terminal output corruption.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_attr: Any = None
        self._fd: int | None = None
        self._buffer: list[str] = []
        self._cursor_pos: int = 0
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

        # Discard any unsubmitted partial buffer cleanly
        with self._lock:
            self._buffer = []
            self._cursor_pos = 0

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

                # Esc or Ctrl+U -> Clear in-flight buffer
                if raw in (b"\x1b", b"\x15"):
                    with self._lock:
                        self._buffer = []
                        self._cursor_pos = 0
                    continue

                # Backspace
                if raw in (b"\x7f", b"\x08"):
                    with self._lock:
                        if self._cursor_pos > 0 and self._buffer:
                            self._buffer.pop(self._cursor_pos - 1)
                            self._cursor_pos -= 1
                    continue

                # Left Arrow
                if any(raw.startswith(k) for k in (b"\x1b[D", b"\x1bOD", b"\x1b[1;2D", b"\x1b[1;3D", b"\x1b[1;5D", b"\x1b[1;9D")):
                    with self._lock:
                        if self._cursor_pos > 0:
                            self._cursor_pos -= 1
                    continue

                # Right Arrow
                if any(raw.startswith(k) for k in (b"\x1b[C", b"\x1bOC", b"\x1b[1;2C", b"\x1b[1;3C", b"\x1b[1;5C", b"\x1b[1;9C")):
                    with self._lock:
                        if self._cursor_pos < len(self._buffer):
                            self._cursor_pos += 1
                    continue

                # Up / Down Arrow -> Ignore during in-flight streaming
                if any(raw.startswith(k) for k in (b"\x1b[A", b"\x1bOA", b"\x1b[B", b"\x1bOB", b"\x1b[1;5A", b"\x1b[1;5B")):
                    continue

                # Home / End
                if raw in (b"\x1b[H", b"\x1b[1~", b"\x01", b"\x1bOH"):
                    with self._lock:
                        self._cursor_pos = 0
                    continue
                if raw in (b"\x1b[F", b"\x1b[4~", b"\x05", b"\x1bOF"):
                    with self._lock:
                        self._cursor_pos = len(self._buffer)
                    continue

                # Filter all other unhandled escape sequences completely
                if raw.startswith(b"\x1b"):
                    continue

                # Process text and newlines (supports single keystrokes, fast typing, and compound chunks)
                try:
                    text = raw.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
                    for ch in text:
                        if ch == "\n":
                            with self._lock:
                                line = "".join(self._buffer).strip()
                                self._buffer = []
                                self._cursor_pos = 0
                            if line:
                                self._submit_line(line)
                        elif ord(ch) >= 32 or ch == "\t":
                            with self._lock:
                                self._buffer.insert(self._cursor_pos, ch)
                                self._cursor_pos += 1
                except Exception:
                    pass

            except Exception:
                break

    def _submit_line(self, line: str) -> None:
        """Submit a completed line to /btw, /q, or agent message queue."""
        if not line:
            return
        if line.startswith(("/btw", "/ask", "/side")):
            question = line.split(" ", 1)[1].strip() if " " in line else ""
            if question:
                sys.stdout.write(f"\n  {GOLD}💬 [Side inquiry (/btw)]:{RST} {WHITE}{BOLD}{question}{RST}\n")
                sys.stdout.flush()
                threading.Thread(
                    target=self._run_async_btw,
                    args=(question,),
                    daemon=True,
                ).start()
        elif line.startswith(("/q ", "/q", "/queue")):
            self._handle_queue_command(line)
        elif hasattr(self.agent, "message_queue"):
            item = self.agent.message_queue.push(line)
            pending_count = len(self.agent.message_queue)
            sys.stdout.write(
                f"\n  {CYAN}📥 [Queued for next turn · #{item.id} ({pending_count} pending)]:{RST} {WHITE}{BOLD}{line}{RST}\n"
            )
            sys.stdout.flush()

    def _handle_queue_command(self, line: str) -> None:
        """Process /q commands (including drop, clear, list) during in-flight execution."""
        if not hasattr(self.agent, "message_queue"):
            return

        parts = line.split(maxsplit=2)
        if len(parts) == 1:
            # /q alone -> show summary
            q = self.agent.message_queue
            if len(q) == 0:
                sys.stdout.write(f"\n  {SLATE}📥 Message queue is empty. Type /q <prompt> to add.{RST}\n")
            else:
                sys.stdout.write(f"\n  {CYAN}📥 Queue ({len(q)} pending):{RST}\n")
                for idx, it in enumerate(q.items, 1):
                    badge = f"{CYAN}{BOLD}#{it.id}{RST}" if idx == 1 else f"{SLATE}#{it.id}{RST}"
                    nxt = " [Next]" if idx == 1 else ""
                    sys.stdout.write(f"    {badge}{nxt}: {WHITE}{it.text}{RST}\n")
            sys.stdout.flush()
            return

        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("drop", "rm", "remove", "delete") and len(parts) > 2:
            try:
                target_id = int(parts[2].lstrip("#"))
                if self.agent.message_queue.remove(target_id):
                    sys.stdout.write(f"\n  {MINT}✓ Removed message #{target_id} from queue.{RST}\n")
                else:
                    sys.stdout.write(f"\n  {ROSE}No message with #{target_id} found in queue.{RST}\n")
            except ValueError:
                sys.stdout.write(f"\n  {ROSE}Invalid id '{parts[2]}'. Usage: /q drop <id>{RST}\n")
            sys.stdout.flush()
        elif sub in ("clear", "empty", "reset"):
            count = len(self.agent.message_queue)
            self.agent.message_queue.clear()
            sys.stdout.write(f"\n  {MINT}✓ Cleared {count} messages from queue.{RST}\n")
            sys.stdout.flush()
        else:
            # /q <prompt text> -> enqueue
            prompt_text = line.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
            if prompt_text:
                item = self.agent.message_queue.push(prompt_text)
                pending = len(self.agent.message_queue)
                sys.stdout.write(
                    f"\n  {CYAN}📥 [Queued for next turn · #{item.id} ({pending} pending)]:{RST} {WHITE}{BOLD}{prompt_text}{RST}\n"
                )
                sys.stdout.flush()

    def _run_async_btw(self, question: str) -> None:
        """Executes a concurrent side question without blocking the main agent turn."""
        from axon.providers.registry import provider_for
        from axon.ui.render import render_side_question_box
        try:
            sys_blocks = [{"type": "text", "text": "You are a concise technical assistant. Answer the side question clearly and accurately in 2-4 sentences."}]
            scratch_messages = [{"role": "user", "content": question}]
            side_provider = provider_for(self.agent.settings.model, self.agent.settings)
            stream = side_provider.stream(
                model=self.agent.settings.model,
                system=sys_blocks,
                messages=scratch_messages,
                tools=[],
                max_tokens=600,
                effort="low",
                thinking=False,
            )
            for _ in stream:
                pass
            turn = side_provider.finalize()
            ans_text = turn.text or "Completed."
            sys.stdout.write(f"\n{render_side_question_box(question, ans_text)}\n\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"\n  ❌ Side inquiry failed: {e}\n")
            sys.stdout.flush()
