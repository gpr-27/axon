"""
Permission prompt shown before mutating tools and shell commands run.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Literal

from axon import platform as plat
from axon.ui import decision_help, live_status
from axon.ui.picker import SelectItem, Selector
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE, sanitize, strip_ansi


def _clean(text: Any) -> str:
    return sanitize(strip_ansi(str(text)))

# "once": this call only; "always": every call of this tool for the rest of the session (in memory);
# "rule": persist the narrow rule shown in the option label (<ws>/.axon/settings.local.json).
ApprovalResult = Literal["once", "always", "rule", "deny"]

# Tools whose narrow rule does not depend on the workspace path (the label can show it exactly).
_RULE_TOOLS = ("Bash", "Git", "WebFetch", "Http")

# Letter shortcuts accepted by the picker (and the plain-text fallback).
_KEYS = {"y": "once", "a": "always", "n": "deny"}

# Answers that can carry a comment for the model, and the prompts that do not offer the field.
_COMMENT_ANSWERS = {"once": "Yes", "deny": "No"}
_NO_COMMENT_TOOLS = frozenset({"WebFetch", "Http"})
_COMMENT_MAX = 4000


class Answer(str):
    """An approval answer with the note the person typed. It compares as the plain answer, so every caller and
    approver in between keeps working; the agent loop reads ``comment``."""
    comment: str

    def __new__(cls, value: str, comment: str = "") -> "Answer":
        self = super().__new__(cls, value)
        self.comment = comment
        return self


class _CommentField:
    """The comment field of the permission prompt. Tab on Yes or No opens it; Enter answers that option with the
    comment; Tab or Shift+Tab closes it and keeps the text for when that option is answered. Open, it takes every key,
    so nothing typed into it can answer the prompt by accident; Esc declines, as it always does here."""

    def __init__(self, footer: str) -> None:
        self.footer = footer
        self.open_on: str | None = None
        self.notes: dict[str, str] = {}

    def _show(self, sel: Selector) -> None:
        if self.open_on is None:
            sel._prompt = None
            sel.footer = self.footer
            return
        room = max(10, plat.term_size()[0] - 30)
        sel._prompt = (f"{GOLD}Comment on {_COMMENT_ANSWERS[self.open_on]}:{RST} ", self.notes.get(self.open_on, "")[-room:])
        sel.footer = "enter answer with the comment · tab close, keep the text · esc deny"

    def extra_lines(self, sel: Selector) -> list[str]:
        return [f"    {SLATE}Comment on {_COMMENT_ANSWERS[k]}: {plat.truncate_ansi(_clean(v), max(20, plat.term_size()[0] - 24))}{RST}"
                for k, v in self.notes.items() if v.strip() and k != self.open_on]

    def on_key(self, sel: Selector, key: str) -> bool:
        """True when the key belongs to the field (the Tab that opens it included)."""
        if self.open_on is None:
            current = sel.current()
            if key != "tab":
                return False
            if current is not None and current.value in _COMMENT_ANSWERS:
                self.open_on = current.value
                self._show(sel)
            return True
        if key in ("esc", "ctrl-c"):
            self.notes.clear()
            sel._prompt = None
            sel.finish(None)
        elif key == "enter":
            sel._prompt = None
            sel.finish(sel.current())
        elif key in ("tab", "shift-tab"):
            self.open_on = None
        elif key == "backspace":
            self.notes[self.open_on] = self.notes.get(self.open_on, "")[:-1]
        elif key == "ctrl-u":
            self.notes[self.open_on] = ""
        else:
            text = sanitize(key[len(plat.PASTE_PREFIX):].replace("\n", " ")) if key.startswith(plat.PASTE_PREFIX) else key
            if key.startswith(plat.PASTE_PREFIX) or (len(key) == 1 and key >= " "):
                self.notes[self.open_on] = (self.notes.get(self.open_on, "") + text)[:_COMMENT_MAX]
        self._show(sel)
        return True


def narrow_rule_label(tool_name: str, args: dict[str, Any]) -> str:
    """The exact rule a "rule" approval would save, e.g. ``Bash(git status:*)`` ('' when unavailable)."""
    if tool_name not in _RULE_TOOLS:
        return ""
    try:
        from pathlib import Path

        from axon.permissions.engine import PermissionEngine
        return str(PermissionEngine.narrow_rule_for(tool_name, args if isinstance(args, dict) else {}, Path.cwd()))
    except Exception:
        return ""


def _detail_lines(tool_name: str, args: dict[str, Any], reason: str) -> list[str]:
    cols = plat.term_size()[0]
    width = max(20, cols - 8)
    lines: list[str] = []
    if tool_name == "Bash":
        cmd = str(args.get("command", ""))
        for i, part in enumerate(cmd.splitlines()[:12] or [""]):
            lines.append(f"    {GOLD}{'$' if i == 0 else ' '}{RST} {WHITE}{BOLD}{plat.truncate_ansi(_clean(part), width)}{RST}")
        if args.get("description"):
            lines.append(f"    {SLATE}{plat.truncate_ansi(_clean(args['description']), width)}{RST}")
        lines.append(f"    {DARK_SLATE}runs in {plat.shell_name()}{RST}")
    elif "path" in args or "file_path" in args:
        lines.append(f"    {WHITE}{BOLD}{plat.truncate_ansi(_clean(args.get('path') or args.get('file_path')), width)}{RST}")
    else:
        try:
            text = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(args)
        lines.append(f"    {SLATE}{plat.truncate_ansi(_clean(text), width)}{RST}")
    if reason:
        lines.append(f"    {DARK_SLATE}{plat.truncate_ansi(_clean(reason), width)}{RST}")
    return lines


_SETTLE_SECONDS = 0.35


def ask_approval(tool_name: str, args: dict[str, Any], reason: str = "") -> ApprovalResult:
    """Ask the user to allow a tool call. Non-interactive sessions are denied (safe default)."""
    try:
        interactive_in = sys.stdin.isatty()
    except Exception:
        interactive_in = False
    if not interactive_in:
        return "deny"

    try:
        from axon.ui.in_flight import paused
    except ImportError:  # pragma: no cover - same package
        import contextlib
        paused = contextlib.nullcontext  # type: ignore[assignment]

    try:
        from axon.ui.render import pause_spinners
    except (ImportError, AttributeError):
        import contextlib
        pause_spinners = contextlib.nullcontext  # type: ignore[assignment]

    comment = ""
    tool_name_s = _clean(tool_name)
    header = [f"  {GOLD}{BOLD}⚠ Allow {tool_name_s}?{RST}", *_detail_lines(tool_name, args, reason), ""]
    rule = narrow_rule_label(tool_name, args)
    explain = decision_help.approval_lines(tool_name, args, reason, rule)   # what is asked, why, what each answer does
    with pause_spinners(), paused(), live_status.waiting("user", what=f"approve {tool_name_s}"):
        if plat.is_interactive():
            items = [
                SelectItem("Yes", value="once", hint="this call only · y"),
                SelectItem("Yes, don't ask again this session", value="always",
                           hint=f"every {tool_name_s} call until you exit · a"),
                SelectItem("No", value="deny", hint="n · esc"),
            ]
            if rule:
                items.append(SelectItem(f"Yes, always allow {_clean(rule)}", value="rule",
                                        hint="saved for this project (.axon/settings.local.json)"))

            comments = None if tool_name in _NO_COMMENT_TOOLS else _CommentField(
                f"↑↓ move · enter choose · tab comment · 1-{len(items)} quick pick · y yes · a session · n/esc deny")
            footer = comments.footer if comments is not None else (
                f"↑↓ move · enter choose · 1-{len(items)} quick pick · y yes · a session · n/esc deny")

            def on_key(sel: Selector, key: str) -> bool:
                if comments is not None and comments.on_key(sel, key):
                    return True
                value = _KEYS.get(key.lower()) if len(key) == 1 else None
                if value is None:
                    return False
                sel.finish(next(it for it in sel.items if it.value == value))
                return True

            sys.stdout.write("\n" + "\n".join(header) + "\n")
            sys.stdout.flush()
            # Keys typed while the model was working (the next message) must never answer a permission prompt:
            # a "y" or "1" in that text would allow the call. Anything that arrives before a person could have read
            # the question is dropped.
            time.sleep(_SETTLE_SECONDS)
            plat.flush_input()
            with decision_help.shown(explain):
                selector = Selector(items, title="Permission", subtitle=tool_name_s, on_key=on_key, footer=footer,
                                    extra_lines=comments.extra_lines if comments is not None else None)
                selector.key_context = "Confirmation"
                chosen = selector.run()
            result: ApprovalResult = chosen.value if isinstance(chosen, SelectItem) else "deny"
            if comments is not None and isinstance(chosen, SelectItem):
                comment = comments.notes.get(result, "").strip()
        else:
            print("\n" + "\n".join(header))
            decision_help.show(explain)
            print(f"  {MINT}[y]{RST} yes (this call)   {GOLD}[a]{RST} every {tool_name_s} call this session   {ROSE}[n]{RST} no")
            if tool_name not in _NO_COMMENT_TOOLS:
                print(f"  {SLATE}to add a note for the model: y: use staging   or   n: not on main{RST}")
            try:
                typed = input(f"  {BOLD}› {RST}").strip()
            except (KeyboardInterrupt, EOFError):
                typed = "n"
            answer, _, note = typed.partition(":")
            answer = answer.strip().lower()
            result = "once" if answer in ("y", "yes") else ("always" if answer in ("a", "always") else "deny")
            if result != "always" and tool_name not in _NO_COMMENT_TOOLS:    # the same options take a note as in the picker
                comment = " ".join(note.split())
    label = {"once": f"{MINT}✓ Allowed once",
             "always": f"{GOLD}✓ Allowed for this session (all {tool_name_s} calls)",
             "rule": f"{GOLD}✓ Allowed {_clean(rule)} for this project",
             "deny": f"{ROSE}✗ Denied"}[result]
    print(f"  {label}{RST}" + (f"{SLATE} · comment sent to the model: {_clean(comment)}{RST}" if comment else "") + "\n")
    return Answer(result, comment) if comment else result
