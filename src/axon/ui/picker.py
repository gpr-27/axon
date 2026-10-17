"""
Reusable interactive list selector for every Axon picker (models, providers, sessions, files).

One engine handles key input (via axon.platform, so it works on Windows consoles, macOS and
Linux), scrolling, optional type-to-filter, optional tabs, group headers, inline or alternate-
screen rendering, inline text prompts, and graceful non-TTY fallbacks. Specific pickers only
supply items and, when needed, an `on_key` hook.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from axon import platform as plat
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, MINT, ROSE, RST, SLATE, WHITE

# Back-compat feature flags (tests and older callers inspect these).
_HAS_TERMIOS = plat.HAS_TERMIOS
_HAS_MSVCRT = plat.HAS_MSVCRT

POINTER = "❯"


class LiveFrame:
    """A block of lines redrawn in place below the cursor (or at the top of the alternate
    screen). Tracks where the cursor sits so the next frame or clear() starts at the top, and
    compensates for terminals that reflow lines when the window shrinks."""

    def __init__(self, fullscreen: bool = False) -> None:
        self.fullscreen = fullscreen
        self._cursor_row = 0
        self._cursor_col = 0
        self._widths: list[int] = []
        self._cols = 0

    def _rows_to_top(self, cols: int) -> int:
        if not self._widths:
            return 0
        if not self._cols or cols >= self._cols:
            return self._cursor_row
        rows = sum(max(1, -(-w // cols)) for w in self._widths[:self._cursor_row])
        return rows + self._cursor_col // cols

    def render(self, lines: list[str], cursor: tuple[int, int] | None = None) -> None:
        if plat.IS_WINDOWS:
            plat.enable_vt_mode()
        cols, _rows = plat.term_size()
        lines = [plat.truncate_ansi(line, cols - 1) for line in lines] or [""]
        out = [plat.HIDE_CURSOR]
        if self.fullscreen:
            out.append("\033[H")
        else:
            up = self._rows_to_top(cols)
            if up > 0:
                out.append(f"\033[{up}A")
        out.append("\r\033[J")
        out.append("\n".join(lines))
        self._widths = [plat.str_width(line) for line in lines]
        self._cols = cols
        if cursor is not None:
            row, col = max(0, min(cursor[0], len(lines) - 1)), max(0, min(cursor[1], cols - 1))
            up = len(lines) - 1 - row
            if up > 0:
                out.append(f"\033[{up}A")
            out.append(f"\r\033[{col}C" if col > 0 else "\r")
            out.append(plat.SHOW_CURSOR)
            self._cursor_row, self._cursor_col = row, col
        else:
            self._cursor_row, self._cursor_col = len(lines) - 1, self._widths[-1]
        _write(out)

    def clear(self) -> None:
        """Erase the frame and leave the cursor where the frame started."""
        if self.fullscreen:
            _write(["\033[H\033[J"])
        else:
            up = self._rows_to_top(plat.term_size()[0])
            _write([f"\033[{up}A" if up > 0 else "", "\r\033[J", plat.SHOW_CURSOR])
        self._widths = []
        self._cursor_row = self._cursor_col = 0


def _write(parts: list[str]) -> None:
    try:
        lock = _output_lock()
        with lock:
            sys.stdout.write("".join(parts))
            sys.stdout.flush()
    except Exception:
        pass


def _output_lock() -> Any:
    try:
        from axon.ui.theme import OUTPUT_LOCK  # shared with the streaming renderer
        return OUTPUT_LOCK
    except (ImportError, AttributeError):
        return _FALLBACK_LOCK


_FALLBACK_LOCK = threading.RLock()


@dataclass
class SelectItem:
    label: str
    value: Any = None
    hint: str = ""          # dim right-hand text
    group: str = ""         # section header shown when it changes
    badge: str = ""         # short coloured marker before the label (e.g. "●")
    search: str = ""        # extra text matched by the filter
    render: Callable[[bool, int], str] | None = None  # custom row renderer(selected, width)

    def matches(self, query: str) -> bool:
        q = query.lower()
        return all(tok in f"{self.label} {self.hint} {self.group} {self.search}".lower() for tok in q.split())


class Selector:
    """Interactive selector. `run()` returns the chosen SelectItem, a value passed to
    `finish()`, or None when cancelled (Esc / Ctrl+C / q)."""

    def __init__(
        self,
        items: Sequence[SelectItem] | None = None,
        *,
        title: str = "Select",
        subtitle: str = "",
        index: int = 0,
        filterable: bool = False,
        filter_fn: Callable[[str], list[SelectItem]] | None = None,
        placeholder: str = "type to filter",
        tabs: list[str] | None = None,
        tab_index: int = 0,
        items_for_tab: Callable[[int], list[SelectItem]] | None = None,
        max_visible: int = 10,
        fullscreen: bool = False,
        footer: str | None = None,
        on_key: Callable[["Selector", str], bool] | None = None,
        extra_lines: Callable[["Selector"], list[str]] | None = None,
    ) -> None:
        self.title = title
        self.subtitle = subtitle
        self.filterable = filterable
        self.filter_fn = filter_fn
        self.placeholder = placeholder
        self.tabs = tabs or []
        self.tab_index = tab_index if self.tabs else 0
        self.items_for_tab = items_for_tab
        self.max_visible = max_visible
        self.fullscreen = fullscreen and plat.supports_alt_screen()
        self.footer = footer
        self.on_key = on_key
        self.extra_lines = extra_lines
        self.query = ""
        self.message = ""
        self.message_color = MINT
        self._base: list[SelectItem] = list(items or [])
        self.items: list[SelectItem] = []
        self.index = 0
        self._done = False
        self._result: Any = None
        self._live = LiveFrame(fullscreen=self.fullscreen)
        self._prompt: tuple[str, str] | None = None
        self._load_tab()
        if self.items:
            self.index = max(0, min(index, len(self.items) - 1))

    # ── state helpers ──────────────────────────────────────────────────────
    def _load_tab(self) -> None:
        if self.items_for_tab is not None:
            self._base = list(self.items_for_tab(self.tab_index))
        self.refresh_items()

    def set_items(self, items: Sequence[SelectItem], keep_index: bool = True) -> None:
        self._base = list(items)
        self.refresh_items(keep_index=keep_index)

    def refresh_items(self, keep_index: bool = False) -> None:
        if self.filter_fn is not None:
            self.items = list(self.filter_fn(self.query))
        elif self.query:
            self.items = [it for it in self._base if it.matches(self.query)]
        else:
            self.items = list(self._base)
        if not keep_index:
            self.index = 0
        self.index = max(0, min(self.index, len(self.items) - 1)) if self.items else 0

    def current(self) -> SelectItem | None:
        return self.items[self.index] if self.items else None

    def finish(self, result: Any) -> None:
        self._done = True
        self._result = result

    def flash(self, message: str, color: str = MINT) -> None:
        self.message = message
        self.message_color = color

    def move(self, delta: int) -> None:
        if self.items:
            self.index = (self.index + delta) % len(self.items)

    def switch_tab(self, delta: int) -> None:
        if self.tabs:
            self.tab_index = (self.tab_index + delta) % len(self.tabs)
            self.query = ""
            self._load_tab()

    # ── rendering ──────────────────────────────────────────────────────────
    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, rows = plat.term_size()
        width = max(20, cols - 4)
        lines: list[str] = []
        count = f"{self.index + 1}/{len(self.items)}" if self.items else "0/0"
        head = f"  {GOLD}◆{RST} {BOLD}{WHITE}{self.title}{RST}"
        if self.subtitle:
            head += f"  {SLATE}{self.subtitle}{RST}"
        pad = width - plat.str_width(head) - len(count)
        lines.append(head + (" " * max(2, pad)) + f"{DARK_SLATE}{count}{RST}")
        if self.tabs:
            chips = []
            for i, name in enumerate(self.tabs):
                chips.append(f"{GRAY_BG}{BOLD}{WHITE} {name} {RST}" if i == self.tab_index else f"{SLATE} {name} {RST}")
            lines.append("  " + f"{DARK_SLATE}‹{RST}" + "".join(chips) + f"{DARK_SLATE}›{RST}")
        cursor_row: int | None = None
        cursor_col = 0
        if self.filterable:
            if self.query:
                lines.append(f"  {CYAN}⌕{RST} {WHITE}{self.query}{RST}")
            else:
                lines.append(f"  {CYAN}⌕{RST} {DARK_SLATE}{self.placeholder}{RST}")
            cursor_row = len(lines) - 1
            cursor_col = 4 + plat.str_width(self.query)
        lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")

        reserved = len(lines) + 4 + (len(self.extra_lines(self)) if self.extra_lines else 0)
        visible = max(3, min(self.max_visible, rows - reserved - 2)) if not self.fullscreen else max(3, rows - reserved - 2)
        n = len(self.items)
        start = max(0, min(self.index - visible // 2, n - visible))
        end = min(n, start + visible)
        if not self.items:
            lines.append(f"    {SLATE}No matches{(' for “' + self.query + '”') if self.query else ''}.{RST}")
        last_group = None
        if start > 0:
            lines.append(f"    {DARK_SLATE}↑ {start} more{RST}")
        for i in range(start, end):
            it = self.items[i]
            if it.group and it.group != last_group:
                lines.append(f"  {GOLD}{BOLD}{it.group}{RST}")
            last_group = it.group
            lines.append(self._row(it, i == self.index, width))
        if end < n:
            lines.append(f"    {DARK_SLATE}↓ {n - end} more{RST}")
        if self.extra_lines:
            lines.extend(self.extra_lines(self))
        lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        if self._prompt is not None:
            label, buf = self._prompt
            lines.append(f"  {label}{WHITE}{buf}{RST}")
            cursor_row = len(lines) - 1
            cursor_col = 2 + plat.str_width(label) + plat.str_width(buf)
        elif self.message:
            lines.append(f"  {self.message_color}{self.message}{RST}")
        hint = self.footer
        if hint is None:
            parts = ["↑↓ move", "enter select"]
            if self.tabs:
                parts.append("tab/←→ switch")
            parts.append("esc cancel")
            hint = " · ".join(parts)
        lines.append(f"  {DARK_SLATE}{hint}{RST}")
        return lines, cursor_row, cursor_col

    def _row(self, it: SelectItem, selected: bool, width: int) -> str:
        if it.render is not None:
            body = it.render(selected, width - 4)
        else:
            label = f"{BOLD}{WHITE}{it.label}{RST}" if selected else f"{SLATE}{it.label}{RST}"
            badge = f"{it.badge} " if it.badge else ""
            body = badge + label
            if it.hint:
                gap = max(2, min(34, width - 6) - plat.str_width(badge + it.label))
                body += " " * gap + f"{DARK_SLATE if not selected else SLATE}{it.hint}{RST}"
        mark = f"{MINT}{BOLD}{POINTER}{RST}" if selected else " "
        return f"  {mark} {body}"

    def draw(self) -> None:
        lines, cur_row, cur_col = self._frame()
        self._live.render(lines, (cur_row, cur_col) if cur_row is not None else None)

    def _clear(self) -> None:
        if not self.fullscreen:
            self._live.clear()

    def redraw(self) -> None:
        self.draw()

    # ── inline prompts (used by on_key hooks) ─────────────────────────────
    def prompt_line(self, label: str, default: str = "", allow_empty: bool = False, secret: bool = False) -> str | None:
        """Read a line of text inside the frame. Esc cancels (None)."""
        buf = default
        while True:
            self._prompt = (label, "•" * len(buf) if secret else buf)
            self.redraw()
            key = plat.read_key()
            if key in ("esc", "ctrl-c"):
                self._prompt = None
                return None
            if key == "enter":
                self._prompt = None
                val = buf.strip()
                if not val and not allow_empty:
                    return None
                return val
            if key == "backspace":
                buf = buf[:-1]
            elif key == "ctrl-u":
                buf = ""
            elif key and key.startswith(plat.PASTE_PREFIX):
                buf += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
            elif key and len(key) == 1 and key >= " ":
                buf += key

    def confirm(self, question: str) -> bool:
        """y confirms; anything else (n, Esc, Enter) declines."""
        self._prompt = (f"{ROSE}{BOLD}{question}{RST} {SLATE}(y/N){RST} ", "")
        self.redraw()
        key = plat.read_key()
        while key == "resize":
            self.redraw()
            key = plat.read_key()
        self._prompt = None
        return key in ("y", "Y")

    # ── main loop ──────────────────────────────────────────────────────────
    def run(self) -> Any:
        if not plat.is_interactive():
            return self._fallback()
        plat.setup_console()
        if self.fullscreen:
            sys.stdout.write(plat.ENTER_ALT_SCREEN)
        try:
            with plat.raw_terminal(isig=False):
                self.draw()
                while not self._done:
                    key = plat.read_key()
                    if key is None:
                        continue
                    self.message = ""
                    if self.on_key is not None and self.on_key(self, key):
                        if not self._done:
                            self.redraw()
                        continue
                    self._default_key(key)
                    if not self._done:
                        self.redraw()
        finally:
            if self.fullscreen:
                sys.stdout.write(plat.EXIT_ALT_SCREEN)
            else:
                self._clear()
            sys.stdout.write(plat.SHOW_CURSOR)
            sys.stdout.flush()
        return self._result

    def _default_key(self, key: str) -> None:
        n = len(self.items)
        if key in ("esc", "ctrl-c"):
            if self.filterable and self.query and key == "esc":
                self.query = ""
                self.refresh_items()
                return
            self.finish(None)
        elif key == "enter":
            self.finish(self.current())
        elif key in ("up", "ctrl-p") or (key == "k" and not self.filterable):
            self.move(-1)
        elif key in ("down", "ctrl-n") or (key == "j" and not self.filterable):
            self.move(1)
        elif key == "pageup":
            self.index = max(0, self.index - self.max_visible)
        elif key == "pagedown":
            self.index = min(max(0, n - 1), self.index + self.max_visible)
        elif key == "home" and not self.query:
            self.index = 0
        elif key == "end" and not self.query:
            self.index = max(0, n - 1)
        elif self.tabs and key in ("tab", "right"):
            self.switch_tab(1)
        elif self.tabs and key in ("shift-tab", "left"):
            self.switch_tab(-1)
        elif self.filterable and key == "backspace":
            if self.query:
                self.query = self.query[:-1]
                self.refresh_items()
        elif self.filterable and key == "ctrl-u":
            self.query = ""
            self.refresh_items()
        elif self.filterable and key.startswith(plat.PASTE_PREFIX):
            self.query += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
            self.refresh_items()
        elif self.filterable and len(key) == 1 and key >= " ":
            self.query += key
            self.refresh_items()
        elif not self.filterable and key in ("q", "Q"):
            self.finish(None)
        elif not self.filterable and len(key) == 1 and key in "123456789" and int(key) <= n:
            self.index = int(key) - 1
            self.finish(self.current())

    def _fallback(self) -> Any:
        """Non-interactive: numbered menu when stdin is a terminal-less pipe is pointless, so
        return the preselected item (keeps scripts and tests deterministic)."""
        return self.current()


def pick(options: list[str], title: str = "Select Option", current: str | None = None) -> str | None:
    """Choose one string from options. Non-TTY: returns `current` (or the first option)."""
    if not options:
        return None
    idx = options.index(current) if (current and current in options) else 0
    if not plat.is_interactive():
        try:
            interactive_in = sys.stdin.isatty()
        except Exception:
            interactive_in = False
        if not interactive_in:
            return current or options[0]
        return _numbered_pick(options, title, idx)
    items = [SelectItem(label=" ".join(o.split()), value=o, badge=(f"{MINT}●{RST}" if o == current else "")) for o in options]
    chosen = Selector(items, title=title, index=idx).run()
    return chosen.value if isinstance(chosen, SelectItem) else None


def _numbered_pick(options: list[str], title: str, idx: int) -> str | None:
    """Line-mode menu for terminals where stdin is interactive but stdout is redirected."""
    print(f"\n  {GOLD}{BOLD}{title}{RST}")
    for i, opt in enumerate(options, 1):
        marker = f"{MINT}{POINTER}{RST}" if i - 1 == idx else " "
        print(f"  {marker} {DIM}{i:>2}.{RST} {opt}")
    try:
        choice = input(f"\n  Select [1-{len(options)}] (Enter = {idx + 1}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice:
        return options[idx]
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1]
    return None


# ── reusable flows ──────────────────────────────────────────────────────────

def choose(options: Sequence[tuple[Any, ...]], title: str = "Select", *, subtitle: str = "",
           index: int = 0, footer: str | None = None, filterable: bool = False) -> Any:
    """Pick from (label, value[, hint]) tuples; returns the value, or None on cancel / non-TTY
    (unless ``index`` points at a default: the non-interactive fallback returns that value)."""
    items = [SelectItem(label=str(o[0]), value=o[1], hint=str(o[2]) if len(o) > 2 and o[2] else "") for o in options]
    if not items:
        return None
    chosen = Selector(items, title=title, subtitle=subtitle, index=index, footer=footer,
                      filterable=filterable).run()
    return chosen.value if isinstance(chosen, SelectItem) else None


# Rewind (Esc Esc / /rewind): P-CMD supplies the points and applies the chosen mode.
REWIND_MODES: list[tuple[str, str, str]] = [
    ("Restore code and conversation", "code_and_conversation", "files and chat return to before this prompt"),
    ("Restore conversation", "conversation", "keep the current files"),
    ("Restore code", "code", "keep the conversation"),
    ("Summarize from here", "summarize_from_here", "compress the later messages into a summary"),
    ("Never mind", "", ""),
]


def _ago(ts: Any) -> str:
    import time
    try:
        t = float(ts)
    except (TypeError, ValueError):
        if hasattr(ts, "timestamp"):
            t = float(ts.timestamp())
        else:
            return ""
    d = max(0, int(time.time() - t))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def rewind_point_items(points: Sequence[Any]) -> list[SelectItem]:
    """Rows for rewind points (duck-typed: .index, .prompt, .ts, .files_changed)."""
    items: list[SelectItem] = []
    for p in points:
        idx = getattr(p, "index", None)
        prompt = " ".join(str(getattr(p, "prompt", "") or "(empty prompt)").split())
        files = getattr(p, "files_changed", 0)
        n_files = len(files) if isinstance(files, (list, tuple, set)) else int(files or 0)
        hint_bits = [b for b in (_ago(getattr(p, "ts", None)),
                                 f"{n_files} file{'s' if n_files != 1 else ''} changed" if n_files else "no file changes") if b]
        items.append(SelectItem(label=(prompt[:70] + ("…" if len(prompt) > 70 else "")), value=p,
                                hint=" · ".join(hint_bits), badge=f"{DARK_SLATE}#{idx}{RST}" if idx is not None else ""))
    return items


def run_rewind_menu(points: Sequence[Any]) -> tuple[Any, str] | None:
    """Two-step rewind menu: pick a prompt, then what to restore. Returns (point, mode) or None.
    Non-interactive terminals return None (callers print the text listing instead)."""
    if not points or not plat.is_interactive():
        return None
    items = rewind_point_items(points)
    chosen = Selector(items, title="Rewind", subtitle="restore to the point before a prompt",
                      index=len(items) - 1, filterable=len(items) > 8,
                      footer="↑↓ move · enter choose · esc cancel").run()
    if not isinstance(chosen, SelectItem):
        return None
    point = chosen.value
    files = getattr(point, "files_changed", 0)
    has_files = bool(len(files) if isinstance(files, (list, tuple, set)) else files)
    modes = [m for m in REWIND_MODES if has_files or m[1] not in ("code_and_conversation", "code")]
    mode = choose([(label, value, hint) for label, value, hint in modes],
                  title=f"Rewind to #{getattr(point, 'index', '?')}",
                  subtitle=" ".join(str(getattr(point, "prompt", "") or "").split())[:50])
    if not mode:
        return None
    return point, str(mode)
