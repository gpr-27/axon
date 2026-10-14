"""
Type-ahead while the agent works.

InFlightInputListener reads keys in a background thread during a turn (POSIX termios and
Windows consoles via axon.platform). Typed text is edited with the prompt's EditorState (readline
keys Ctrl+A/E/K/U/W/Y, Alt+B/F, Shift/Alt+Enter newline) and shown on the spinner line. Enter
queues it marked ``steer=True``: the agent loop hands steer messages to the model between tool
rounds ("[User added while you were working]: ..."), and whatever is left runs as the next prompt.
/btw runs a side question, /q edits the queue. Up on an empty draft pulls the last queued message
back for editing. Ctrl+X Ctrl+S (or Ctrl+Enter) queues the draft and interrupts the turn so the
queue runs now. Ctrl+X Ctrl+K stops all background subagents (press twice within 3 s). Esc on an
empty draft interrupts the running turn, like Ctrl+C.

`paused()` temporarily stops any active listener and restores normal terminal mode so that
approval prompts, pickers or editors can read stdin; the listener resumes afterwards.
"""
from __future__ import annotations

import _thread
import contextlib
import sys
import threading
from typing import TYPE_CHECKING, Any, Iterator

from axon import platform as plat
from axon.ui.theme import BOLD, CYAN, GOLD, MINT, ROSE, RST, SLATE, WHITE

if TYPE_CHECKING:
    from axon.agent.loop import Agent

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag

_active_lock = threading.Lock()
_active: list["InFlightInputListener"] = []


def _output_lock() -> Any:
    try:
        from axon.ui.theme import OUTPUT_LOCK
        return OUTPUT_LOCK
    except (ImportError, AttributeError):
        return _LOCAL_LOCK


_LOCAL_LOCK = threading.RLock()


def _say(text: str) -> None:
    """Write a status line from the listener thread without interleaving with the renderer."""
    with _output_lock():
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass


@contextlib.contextmanager
def paused() -> Iterator[None]:
    """Pause every active listener (terminal back in cooked mode) for the duration."""
    with _active_lock:
        listeners = list(_active)
    for listener in listeners:
        listener.pause()
    try:
        yield
    finally:
        for listener in listeners:
            listener.resume()


def push_queued(queue: Any, text: str, steer: bool = False) -> Any:
    """Push onto agent.message_queue, flagging ``steer`` (uses the queue's own flag when its
    ``push`` accepts one, else sets the attribute on the returned item)."""
    try:
        item = queue.push(text, steer=steer)
    except TypeError:
        item = queue.push(text)
    try:
        if getattr(item, "steer", None) != steer:
            setattr(item, "steer", steer)
    except (AttributeError, TypeError):
        pass
    return item


_type_ahead: list[str] = []
_type_ahead_lock = threading.Lock()


def _stash_type_ahead(text: str) -> None:
    with _type_ahead_lock:
        _type_ahead.append(text)


def take_type_ahead() -> str:
    """Unsent draft typed during the last turn (handed to the next prompt as its initial text)."""
    with _type_ahead_lock:
        text = "".join(_type_ahead)
        _type_ahead.clear()
    return text


class InFlightInputListener:
    """Context manager running the background key listener for one agent turn."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._stop_event = threading.Event()
        self._pause_count = 0
        self._pause_lock = threading.Lock()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._idle_event = threading.Event()
        self._thread: threading.Thread | None = None
        from axon.ui.input import EditorState
        self._ed = EditorState()
        self._lock = threading.Lock()
        self._pending_ctrl_x = False
        self._kill_armed = 0.0

    # Draft accessors (kept for callers/tests that used the old list buffer).
    @property
    def _buffer(self) -> list[str]:
        return list(self._ed.buf)

    @_buffer.setter
    def _buffer(self, value: list[str]) -> None:
        self._ed.set_text("".join(value))

    @property
    def _cursor_pos(self) -> int:
        return self._ed.pos

    @_cursor_pos.setter
    def _cursor_pos(self, value: int) -> None:
        self._ed.pos = max(0, min(int(value), len(self._ed.buf)))

    def draft(self) -> str:
        """Current type-ahead draft (shown on the spinner line)."""
        return self._ed.buf

    # ── lifecycle ──────────────────────────────────────────────────────────
    def __enter__(self) -> InFlightInputListener:
        self._stop_event.clear()
        if not plat.is_interactive():
            return self
        with _active_lock:
            _active.append(self)
        try:
            from axon.ui.render import Renderer, Spinner
            Renderer.interrupt_hint = "esc to interrupt"
            Spinner.draft_provider = self.draft
        except (ImportError, AttributeError):
            pass
        self._thread = threading.Thread(target=self._run, daemon=True, name="InFlightInputListener")
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        was_listening = self._thread is not None
        self._stop_event.set()
        self._resume_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        with _active_lock:
            if self in _active:
                _active.remove(self)
        self._thread = None
        try:
            from axon.ui.render import Spinner
            if getattr(Spinner, "draft_provider", None) == self.draft:
                Spinner.draft_provider = None
        except (ImportError, AttributeError):
            pass
        with self._lock:
            leftover = self._ed.buf
            self._ed.set_text("")
        if leftover.strip() and was_listening:
            _stash_type_ahead(leftover)

    def pause(self, timeout: float = 1.0) -> None:
        """Stop reading and leave raw mode (blocks until the thread is idle)."""
        with self._pause_lock:
            self._pause_count += 1
            self._resume_event.clear()
        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._idle_event.wait(timeout)

    def resume(self) -> None:
        with self._pause_lock:
            self._pause_count = max(0, self._pause_count - 1)
            if self._pause_count == 0:
                self._idle_event.clear()
                self._resume_event.set()

    @property
    def is_paused(self) -> bool:
        return self._pause_count > 0

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.is_paused:
                self._idle_event.set()
                self._resume_event.wait(0.2)
                continue
            self._idle_event.clear()
            try:
                with plat.raw_terminal(isig=False):
                    while not self._stop_event.is_set() and not self.is_paused:
                        key = plat.read_key(timeout=0.05, report_resize=False)
                        if key:
                            self.handle_key(key)
            except EOFError:
                break
            except Exception:
                break
        self._idle_event.set()

    # ── key handling (public for tests) ────────────────────────────────────
    def handle_key(self, key: str) -> None:
        if self._pending_ctrl_x:
            self._pending_ctrl_x = False
            if key == "ctrl-s":
                self._send_now()
                return
            if key == "ctrl-k":
                self._stop_background_agents()
                return
        elif key == "ctrl-x":
            self._pending_ctrl_x = True
            return
        if key == "ctrl-enter":
            self._send_now()
            return
        if key == "esc":
            with self._lock:
                had_text = bool(self._ed.buf)
                self._ed.set_text("")
            if not had_text:
                self._interrupt_turn()
            return
        if key == "ctrl-c":
            self._interrupt_turn()
            return
        if key == "ctrl-b":
            background = getattr(self.agent, "background_current_turn", None)
            if callable(background):
                try:
                    background()
                    _say(f"\n  {CYAN}⇢ Moved the running task to the background.{RST}\n")
                except Exception as e:
                    _say(f"\n  {ROSE}Could not background the task: {e}{RST}\n")
            else:
                _say(f"\n  {SLATE}Backgrounding is not available for this turn.{RST}\n")
            return
        if key.startswith(plat.PASTE_PREFIX):
            with self._lock:
                self._ed.insert(plat.normalize_newlines(key[len(plat.PASTE_PREFIX):]))
            return
        line = None
        with self._lock:
            ed = self._ed
            if key in ("enter", "ctrl-j") and not (key == "enter" and ed.buf.endswith("\\")):
                line = ed.buf.strip()
                ed.set_text("")
            elif key == "enter":           # trailing backslash: newline
                ed.buf, ed.pos = ed.buf[:-1], max(0, ed.pos - 1)
                ed.insert("\n")
            elif key in ("shift-enter", "alt-enter"):
                ed.insert("\n")
            elif key == "backspace":
                ed.backspace()
            elif key == "delete":
                ed.delete()
            elif key == "left":
                ed.pos = max(0, ed.pos - 1)
            elif key == "right":
                ed.pos = min(len(ed.buf), ed.pos + 1)
            elif key in ("home", "ctrl-a"):
                ed.pos = 0
            elif key in ("end", "ctrl-e"):
                ed.pos = len(ed.buf)
            elif key in ("ctrl-left", "alt-b"):
                ed.pos = ed.word_left()
            elif key in ("ctrl-right", "alt-f"):
                ed.pos = ed.word_right()
            elif key == "ctrl-u":
                ed.set_text("")
            elif key == "ctrl-k":
                ed.kill(ed.pos, len(ed.buf))
            elif key in ("ctrl-w", "alt-backspace"):
                ed.kill(ed.word_left(), ed.pos)
            elif key == "alt-d":
                ed.kill(ed.pos, ed.word_right())
            elif key == "ctrl-y":
                ed.yank()
            elif key == "ctrl-_":
                ed.undo_last()
            elif key == "up" and not ed.buf:
                self._pull_back_queued()
            elif len(key) == 1 and (key >= " " or key == "\t"):
                ed.insert(key)
        if line:
            self._submit_line(line)

    def _pull_back_queued(self) -> None:
        """Up on an empty draft: take the most recently queued message back into the editor."""
        queue = getattr(self.agent, "message_queue", None)
        items = getattr(queue, "items", None)
        if not items:
            return
        item = items.pop()
        self._ed.set_text(getattr(item, "text", "") or "")

    def _send_now(self) -> None:
        """Queue the draft (if any) and interrupt the turn so the queue runs immediately."""
        with self._lock:
            line = self._ed.buf.strip()
            self._ed.set_text("")
        if line:
            self._submit_line(line, steer=False)
        queue = getattr(self.agent, "message_queue", None)
        if queue is not None and len(queue):
            _say(f"\n  {CYAN}⏩ Sending the queue now…{RST}\n")
            self._interrupt_turn()

    def _stop_background_agents(self) -> None:
        """Ctrl+X Ctrl+K twice within 3 s: cancel every running background subagent."""
        import time as _time
        now = _time.monotonic()
        if now - self._kill_armed > 3.0:
            self._kill_armed = now
            _say(f"\n  {GOLD}Press ctrl+x ctrl+k again to stop all background agents.{RST}\n")
            return
        self._kill_armed = 0.0
        manager = getattr(self.agent, "subagents", None)
        for name in ("cancel_all", "stop_all", "kill_all"):
            fn = getattr(manager, name, None) if manager is not None else None
            if callable(fn):
                try:
                    n = fn()
                    _say(f"\n  {MINT}⏹ Stopped background agents{f' ({n})' if isinstance(n, int) else ''}.{RST}\n")
                except Exception as e:
                    _say(f"\n  {ROSE}Could not stop background agents: {e}{RST}\n")
                return
        _say(f"\n  {SLATE}No background agents to stop.{RST}\n")

    def _interrupt_turn(self) -> None:
        cancel = getattr(self.agent, "cancel_current_turn", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        else:
            _thread.interrupt_main()
        _say(f"\n  {ROSE}⏹ Interrupting…{RST}\n")

    # ── submissions ────────────────────────────────────────────────────────
    def _submit_line(self, line: str, steer: bool = True) -> None:
        """Route a completed line: side question, queue command, or a queued prompt that the
        agent loop delivers to the model between tool rounds (``steer=True``)."""
        if not line:
            return
        head = line.split(maxsplit=1)[0].lower()
        if head in ("/btw", "/ask", "/side", "/ptw"):
            question = line.split(maxsplit=1)[1].strip() if " " in line else ""
            if question:
                _say(f"\n  {GOLD}💬 Side question:{RST} {WHITE}{BOLD}{question}{RST}\n")
                threading.Thread(target=self._run_async_btw, args=(question,), daemon=True).start()
            return
        if head in ("/q", "/queue"):
            self._handle_queue_command(line)
            return
        queue = getattr(self.agent, "message_queue", None)
        if queue is not None:
            item = push_queued(queue, line, steer=steer)
            how = "will be shown to the model at the next step" if steer else "runs next"
            _say(f"\n  {CYAN}📥 Queued #{item.id} ({len(queue)} pending, {how}):{RST} {WHITE}{line}{RST}\n")

    def _handle_queue_command(self, line: str) -> None:
        queue = getattr(self.agent, "message_queue", None)
        if queue is None:
            return
        parts = line.split(maxsplit=2)
        if len(parts) == 1:
            if len(queue) == 0:
                _say(f"\n  {SLATE}📥 Queue is empty. Type /q <prompt> to add.{RST}\n")
            else:
                rows = [f"\n  {CYAN}📥 Queue ({len(queue)} pending):{RST}\n"]
                for idx, it in enumerate(queue.items, 1):
                    badge = f"{CYAN}{BOLD}#{it.id}{RST}" if idx == 1 else f"{SLATE}#{it.id}{RST}"
                    rows.append(f"    {badge}{' [next]' if idx == 1 else ''}: {WHITE}{it.text}{RST}\n")
                _say("".join(rows))
            return
        sub = parts[1].lower()
        if sub in ("drop", "rm", "remove", "delete") and len(parts) > 2:
            try:
                target = int(parts[2].lstrip("#"))
            except ValueError:
                _say(f"\n  {ROSE}Invalid id '{parts[2]}'. Usage: /q drop <id>{RST}\n")
                return
            if queue.remove(target):
                _say(f"\n  {MINT}✓ Removed #{target} from the queue.{RST}\n")
            else:
                _say(f"\n  {ROSE}No queued message #{target}.{RST}\n")
        elif sub in ("clear", "empty", "reset") and len(parts) == 2:
            count = len(queue)
            queue.clear()
            _say(f"\n  {MINT}✓ Cleared {count} queued messages.{RST}\n")
        else:
            text = line.split(maxsplit=1)[1].strip()
            item = queue.push(text)
            _say(f"\n  {CYAN}📥 Queued #{item.id} ({len(queue)} pending):{RST} {WHITE}{text}{RST}\n")

    def _run_async_btw(self, question: str) -> None:
        """Answer a side question with a separate, context-free model call."""
        try:
            from axon.providers import provider_for
            from axon.ui.render import render_side_question_box
            settings = self.agent.settings
            side = provider_for(settings.model, settings)
            system = [{"type": "text", "text": (
                "You are a concise technical assistant. Answer the user's side question directly and "
                "factually in 2-4 sentences or a short snippet. Do not speculate.")}]
            for _ in side.stream(model=settings.model, system=system, messages=[{"role": "user", "content": question}],
                                 tools=[], max_tokens=600, effort="low", thinking=False):
                pass
            turn = side.finalize()
            ledger = getattr(self.agent, "ledger", None)
            if ledger is not None and getattr(turn, "usage", None):
                try:
                    ledger.record(settings.model, turn.usage, tag="side_question")
                except TypeError:
                    try:
                        ledger.record(settings.model, turn.usage, source="side_question")
                    except Exception:
                        pass
                except Exception:
                    pass
            _say(f"\n{render_side_question_box(question, turn.text or '(no answer)')}\n\n")
        except Exception as e:
            _say(f"\n  {ROSE}✗ Side question failed: {e}{RST}\n")
