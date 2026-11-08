"""
Text rendering for the marketplace: Discover cards, the Yours table and the info page.

Every function returns a list of lines and never prints. Colors come from ``axon.ui.theme`` (which is
empty on a non-terminal), and layouts fall back to plain ASCII text on legacy consoles and to a
stacked layout on narrow terminals.
"""
from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass

from axon.ui.theme import BOLD, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE, term_width

NARROW_WIDTH = 72
TAB_TITLES = {"connectors": "Connectors", "skills": "Skills", "plugins": "Plugins"}


@dataclass
class Row:
    """One line item of a tab, already joined with its installed state."""
    id: str
    name: str
    publisher: str
    description: str
    category: str
    verified: bool = False
    free: bool = True
    installed: bool = False
    type_label: str = ""       # "Web" / "Local" for connectors
    status: str = ""           # shown in the Yours table
    note: str = ""
    update: bool = False       # installed, and the catalog pins a different version
    tokens: int | None = None  # prompt tokens per turn (skills: the listing line the model sees every turn)


def unicode_ok() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    return encoding in ("utf8", "utf_8")


def screen_width() -> int:
    try:
        return max(40, min(110, term_width()))
    except Exception:  # noqa: BLE001
        return 80


def is_narrow(width: int) -> bool:
    return width < NARROW_WIDTH


def installed_mark() -> str:
    return f"{MINT}✓{RST}" if unicode_ok() else f"{MINT}[x]{RST}"


def add_mark() -> str:
    return f"{TEAL}+{RST}" if unicode_ok() else f"{TEAL}[+]{RST}"


def verified_mark() -> str:
    return f"{TEAL}✔{RST}" if unicode_ok() else f"{TEAL}(verified){RST}"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    lines = textwrap.wrap(text, max(20, width - len(indent)))
    return [indent + line for line in lines]


def card_lines(row: Row, width: int) -> list[str]:
    """Three or four lines: name and mark, description, then a small facts line."""
    mark = installed_mark() if row.installed else add_mark()
    verified = f" {verified_mark()}" if row.verified else ""
    title = f"  {mark} {BOLD}{WHITE}{row.name}{RST}{verified} {DIM}by {row.publisher}{RST}"
    lines = [title]
    lines.extend(_wrap(row.description, width, "      "))
    facts = [row.id, row.category]
    if row.type_label:
        facts.append(row.type_label)
    facts.append("free" if row.free else "account or paid plan needed")
    if row.tokens is not None:
        facts.append(f"~{row.tokens} tokens/turn")
    if row.update:
        facts.append("update available")
    lines.append(f"      {SLATE}{' · '.join(facts)}{RST}")
    return lines


def discover_lines(tab: str, rows: list[Row], total: int, width: int | None = None,
                   hint: bool = True) -> list[str]:
    """The Discover view: a heading and one card per entry. ``hint`` adds the "Install with" line
    (left out in a terminal, where /marketplace opens the keyboard browser instead)."""
    width = width or screen_width()
    title = f"{TAB_TITLES.get(tab, tab)} · Discover"
    lines = ["", f"  {GOLD}{BOLD}{title}{RST}  {DIM}{len(rows)} of {total} shown{RST}", ""]
    if not rows:
        lines.append(f"  {SLATE}Nothing matches. Try a different search or clear the filters.{RST}")
        return lines + [""]
    for row in rows:
        lines.extend(card_lines(row, width))
        lines.append("")
    if hint:
        lines.append(f"  {DIM}Install with: /marketplace install <id>{RST}")
    return lines + [""]


def _status_text(status: str) -> str:
    if status == "connected" or status == "enabled":
        return f"{MINT}{status}{RST}"
    if status in ("disabled", "installed"):
        return f"{SLATE}{status}{RST}"
    return f"{ROSE}{status}{RST}"


def yours_lines(tab: str, rows: list[Row], width: int | None = None) -> list[str]:
    """The Yours view: a table on wide terminals, stacked lines on narrow ones."""
    width = width or screen_width()
    lines = ["", f"  {GOLD}{BOLD}{TAB_TITLES.get(tab, tab)} · Yours{RST}  {DIM}{len(rows)} installed{RST}", ""]
    if not rows:
        lines.append(f"  {SLATE}Nothing installed yet. Browse with: /marketplace list {tab} --discover{RST}")
        return lines + [""]
    if is_narrow(width):
        return lines + _yours_stacked(rows) + [""]
    return lines + _yours_table(tab, rows) + [""]


def _yours_stacked(rows: list[Row]) -> list[str]:
    lines = []
    for row in rows:
        kind = f" [{row.type_label}]" if row.type_label else ""
        lines.append(f"  {WHITE}{row.name}{RST}{kind}")
        lines.append(f"      {_status_text(row.status)}")
    return lines


def _yours_table(tab: str, rows: list[Row]) -> list[str]:
    first = TAB_TITLES.get(tab, tab).rstrip("s")
    name_width = max([len(first)] + [len(r.name) for r in rows]) + 2
    lines = [f"  {SLATE}{first:<{name_width}}{'Type':<8}Status{RST}"]
    lines.append(f"  {SLATE}{'-' * (name_width + 8 + 12)}{RST}")
    for row in rows:
        type_label = row.type_label or "-"
        lines.append(f"  {WHITE}{row.name:<{name_width}}{RST}{type_label:<8}{_status_text(row.status)}")
    return lines


def info_lines(row: Row, extra: list[tuple[str, str]], width: int | None = None) -> list[str]:
    """The detail page for one entry."""
    width = width or screen_width()
    verified = f" {verified_mark()}" if row.verified else ""
    lines = ["", f"  {GOLD}{BOLD}{row.name}{RST}{verified}  {DIM}by {row.publisher}{RST}", ""]
    lines.extend(_wrap(row.description, width, "  "))
    lines.append("")
    facts = [("id", row.id), ("category", row.category)]
    if row.type_label:
        facts.append(("type", row.type_label))
    facts.append(("installed", "yes" if row.installed else "no"))
    if row.update:
        facts.append(("update", "available: /marketplace update " + row.id))
    if row.tokens is not None:
        facts.append(("cost", f"~{row.tokens} tokens in every prompt while installed (more when the skill is loaded)"))
    facts.extend(extra)
    for label, value in facts:
        if value:
            lines.append(f"  {SLATE}{label:<12}{RST}{value}")
    return lines + [""]


def category_lines(tab: str, names: list[str]) -> list[str]:
    lines = ["", f"  {GOLD}{BOLD}{TAB_TITLES.get(tab, tab)} categories{RST}"]
    lines.extend(f"    {name}" for name in names)
    return lines + [""]
