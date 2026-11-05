"""
Keyboard menus for slash commands: pick a row, pick an action, run it, come back to the list.

Every command follows one rule:

    no arguments + interactive terminal  ->  menu (this module)
    arguments                             ->  the typed subcommand, unchanged (scriptable)
    no terminal (pipes, axon -p, tests)   ->  the plain text output, unchanged

Menus only choose; the work is done by the same functions the typed subcommands call, so there is
one implementation per action. All drawing goes through axon.ui.picker.Selector, which restores the
terminal on Enter, Esc and Ctrl+C.
"""
from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from axon import platform as plat
from axon.ui.detail import BodyLine, show_detail
from axon.ui import picker
from axon.ui.picker import SelectItem, Selector

FOOTER = "↑↓ move · Enter select · Esc back · / filter"
FOOTER_SHORT = "↑↓ move · Enter select · Esc back"
LONG_LIST = 8   # lists longer than this get "/ filter"


@dataclass
class Action:
    """One entry of an action menu. ``confirm`` is a question asked before destructive actions."""
    label: str
    run: Callable[[], Any]
    hint: str = ""
    confirm: str = ""


_confirmed_in_menu = False


def already_confirmed() -> bool:
    """True while an action runs that the user already confirmed in the menu, so the command's own
    y/N question is not asked a second time (axon.commands.common.confirm checks this)."""
    return _confirmed_in_menu


def menus_enabled() -> bool:
    """True when a raw-key menu can be shown (stdin and stdout are terminals)."""
    return plat.is_interactive()


_shown = 0


def menus_started() -> int:
    """How many menus have been shown so far (a caller compares before and after a command)."""
    return _shown + picker.menus_run()


def _show(items: Sequence[SelectItem], title: str, subtitle: str = "", index: int = 0,
          body: Sequence[BodyLine] | None = None) -> SelectItem | None:
    """Every menu of this module goes through here: count it, then ``_select`` (which tests patch)."""
    global _shown
    _shown += 1
    return _select(items, title, subtitle, index, body)


def _select(items: Sequence[SelectItem], title: str, subtitle: str = "", index: int = 0,
            body: Sequence[BodyLine] | None = None) -> SelectItem | None:
    """Run one Selector and return the chosen item (None on Esc / Ctrl+C). Tests patch this.
    With ``body`` the items are shown under a block of text (a detail screen)."""
    if not items:
        return None
    if body:
        chosen = show_detail(items, title, subtitle, body, index)
        return chosen if isinstance(chosen, SelectItem) else None
    long_list = len(items) > LONG_LIST
    chosen = Selector(list(items), title=title, subtitle=subtitle, index=index,
                      slash_filter=long_list, footer=FOOTER if long_list else FOOTER_SHORT).run()
    return chosen if isinstance(chosen, SelectItem) else None


def choose_value(title: str, options: Sequence[tuple[str, Any, str]], subtitle: str = "",
                 current: Any = None) -> Any:
    """Pick from (label, value, hint) options; returns the value or None. ``current`` is preselected
    and marked with a dot."""
    items = []
    index = 0
    for position, (label, value, hint) in enumerate(options):
        is_current = current is not None and value == current
        if is_current:
            index = position
        items.append(SelectItem(label=label, value=value, hint=hint, badge="●" if is_current else ""))
    chosen = _show(items, title, subtitle, index)
    return chosen.value if chosen is not None else None


def choose_action(title: str, actions: Sequence[Action], subtitle: str = "",
                  body: Sequence[BodyLine] | None = None) -> Action | None:
    """Show an action menu; returns the chosen Action (not yet run) or None. ``body`` is text
    shown above the actions (scrollable when long)."""
    items = [SelectItem(label=a.label, value=a, hint=a.hint) for a in actions]
    chosen = _show(items, title, subtitle, 0, body)
    return chosen.value if chosen is not None else None


def choose_item(items: Sequence[SelectItem], title: str, subtitle: str = "", index: int = 0) -> SelectItem | None:
    """Pick one of ready-made rows (groups, custom rendering); None on Esc."""
    return _show(items, title, subtitle, index)


def confirm(question: str) -> bool:
    """Yes/No menu with No preselected, so a stray Enter never deletes anything."""
    answer = choose_value(question, [("No", False, "keep it"), ("Yes", True, "")])
    return answer is True


def run_action(action: Action | None) -> bool:
    """Run an action after its confirmation (if any). Returns False when nothing ran."""
    if action is None:
        return False
    global _confirmed_in_menu
    if action.confirm and not confirm(action.confirm):
        print("  Cancelled.")
        return False
    _confirmed_in_menu = bool(action.confirm)
    try:
        action.run()
    finally:
        _confirmed_in_menu = False
    return True


def pick_and_run(title: str, actions: Sequence[Action], subtitle: str = "") -> bool:
    """Show an action menu once and run the choice."""
    return run_action(choose_action(title, actions, subtitle))


def ask_text(label: str, default: str = "", secret: bool = False) -> str | None:
    """One line of text in cooked mode (hidden for secrets). None when cancelled."""
    shown = f"  {label}" + (f" [{default}]" if default and not secret else "") + ": "
    try:
        answer = getpass.getpass(shown) if secret else input(shown)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    answer = answer.strip()
    return answer or default


def run_with_spinner(label: str, work: Callable[[], Any]) -> Any:
    """Run ``work`` while a one-line spinner turns (terminal only); returns its result."""
    if not menus_enabled():
        print(f"  {label}")
        return work()
    import sys
    import threading
    from axon.ui.theme import OUTPUT_LOCK, glyph
    frames = glyph("spinner")           # braille frames, or | / - \ when the console cannot draw them
    stop = threading.Event()

    def spin() -> None:
        position = 0
        while not stop.wait(0.1):
            try:
                with OUTPUT_LOCK:       # never in the middle of a line another thread is printing
                    sys.stdout.write(f"\r  {frames[position % len(frames)]} {label}")
                    sys.stdout.flush()
            except (OSError, ValueError, UnicodeError):
                return                  # stdout closed or cannot encode the frame: stop quietly, work() goes on
            position += 1

    spinner = threading.Thread(target=spin, daemon=True)
    spinner.start()
    try:
        return work()
    finally:
        stop.set()
        spinner.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def browse(title: str, rows: Callable[[], Sequence[SelectItem]],
           actions_for: Callable[[Any], Sequence[Action]],
           extra_actions: Callable[[], Sequence[Action]] | None = None,
           subtitle: str = "") -> None:
    """List -> pick a row -> pick an action -> run it -> back to the list, until Esc.

    ``rows`` is called again after every action so the list shows fresh state. ``extra_actions``
    are list-wide entries (add, reconnect all, ...) shown under the rows."""
    index = 0
    while True:
        items = list(rows())
        tail = list(extra_actions()) if extra_actions else []
        for action in tail:
            items.append(SelectItem(label=action.label, value=action, hint=action.hint, group="Actions"))
        if not items:
            return
        chosen = _show(items, title, subtitle, min(index, len(items) - 1))
        if chosen is None:
            return
        index = items.index(chosen) if chosen in items else 0
        if isinstance(chosen.value, Action):
            run_action(chosen.value)
            continue
        actions = list(actions_for(chosen.value))
        if not actions:
            continue
        run_action(choose_action(chosen.label, actions))
