"""
Markdown styling and terminal formatter for Axon.
Formats headings, tables, code blocks, lists, callouts, and inline elements with clean ANSI aesthetics.
"""
from __future__ import annotations
import re
import textwrap
import unicodedata
from pathlib import Path
from axon.ui.theme import (
    AMBER,
    BOLD,
    CYAN,
    DARK_SLATE,
    DIM,
    GOLD,
    ITALIC,
    LBLUE,
    MINT,
    PURPLE,
    ROSE,
    RST,
    SLATE,
    TEAL,
    UNDER,
    WHITE,
    term_width,
)

def char_width(ch: str) -> int:
    """Compute terminal cell width for a Unicode character."""
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    if ord(ch) >= 0x1F300 or unicodedata.category(ch) == "So":
        return 2
    return 1

def str_width(s: str) -> int:
    """Calculate visible terminal column width of a string (ignoring ANSI & OSC 8 sequences)."""
    # Strip OSC 8 hyperlinks \x1b]8;;...\x1b\ or \x07
    t = re.sub(r"\x1b\]8;;.*?(?:\x1b\\|\x07)", "", s)
    # Strip standard ANSI color sequences \x1b[...m
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    w = 0
    for ch in t:
        w += char_width(ch)
    return w

def make_clickable(label: str, url_or_path: str) -> str:
    """OSC 8 terminal hyperlink: \x1b]8;;URL\x1b\\LABEL\x1b]8;;\x1b\\"""
    if url_or_path.startswith(("http://", "https://", "file://")):
        target = url_or_path
    else:
        abs_p = Path(url_or_path).resolve()
        target = f"file://{abs_p}"
    return f"\x1b]8;;{target}\x1b\\{UNDER}{TEAL}{label}{RST}\x1b]8;;\x1b\\"

def _style_inline(s: str) -> str:
    """Apply inline bold, code, emphasis, and clickable file/url links."""
    # Markdown links: [label](url_or_path) -> OSC 8 clickable link
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: make_clickable(m.group(1), m.group(2)), s)

    # Bold + Italic ***text*** -> BOLD ITALIC WHITE
    s = re.sub(r"\*\*\*([^\n*]+?)\*\*\*", rf"{BOLD}{ITALIC}{WHITE}\1{RST}", s)

    # Bold **text** or __text__ -> BOLD WHITE
    s = re.sub(r"\*\*([^\n*]+?)\*\*", rf"{BOLD}{WHITE}\1{RST}", s)
    s = re.sub(r"__([^\n_]+?)__", rf"{BOLD}{WHITE}\1{RST}", s)

    # Inline code `code` -> check if file path
    def _code_repl(m: re.Match) -> str:
        code_str = m.group(1)
        if re.match(r"^[\w\-./\\]+\.(py|js|ts|json|md|toml|env|html|css|txt|sh|rs|go|cpp|c|h)$", code_str) or "/" in code_str:
            return make_clickable(code_str, code_str)
        return f"{TEAL}{code_str}{RST}"

    s = re.sub(r"`([^`]+)`", _code_repl, s)

    # Italic *text* or _text_ -> ITALIC
    s = re.sub(r"(?<!\*)\*([^\n*]+?)\*(?!\*)", rf"{ITALIC}\1{RST}", s)
    s = re.sub(r"(?<!_)_([^\n_]+?)_(?!_)", rf"{ITALIC}\1{RST}", s)

    # Result badges: PASS, FAIL, WARN (with or without checkmarks)
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:✓\s*)?PASS(?:\b|(?=[\s|`]))", rf"{MINT}{BOLD}✓ PASS{RST}", s)
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:✗\s*)?FAIL(?:\b|(?=[\s|`]))", rf"{ROSE}{BOLD}✗ FAIL{RST}", s)
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:⚠\s*)?WARN(?:ING)?(?:\b|(?=[\s|`]))", rf"{GOLD}{BOLD}⚠ WARN{RST}", s)
    return s

def render_table(table_lines: list[str], max_total_width: int | None = None) -> list[str]:
    """Parse markdown table lines and render as aligned Unicode box-drawing table."""
    if max_total_width is None:
        max_total_width = min(term_width() - 4, 100)

    raw_rows = []
    for line in table_lines:
        line_clean = line.strip()
        if line_clean.startswith("|"):
            line_clean = line_clean[1:]
        if line_clean.endswith("|"):
            line_clean = line_clean[:-1]
        cells = [c.strip() for c in line_clean.split("|")]
        raw_rows.append(cells)

    if not raw_rows:
        return []

    # Check for delimiter row (e.g. |---|---|)
    has_header = False
    delimiter_idx = -1
    for idx, row in enumerate(raw_rows):
        if any(re.match(r"^\s*:?-+:?\s*$", c) for c in row if c):
            has_header = True
            delimiter_idx = idx
            break

    header = raw_rows[0] if has_header and delimiter_idx > 0 else None
    data_rows = raw_rows[delimiter_idx + 1:] if has_header else raw_rows

    rows_to_check = [r for idx, r in enumerate(raw_rows) if idx != delimiter_idx]
    num_cols = max(len(r) for r in rows_to_check) if rows_to_check else 0
    if num_cols == 0:
        return []

    # Normalize rows to consistent column count
    if header:
        while len(header) < num_cols:
            header.append("")
    norm_data = []
    for r in data_rows:
        row_copy = list(r)
        while len(row_copy) < num_cols:
            row_copy.append("")
        norm_data.append(row_copy[:num_cols])

    # Measure max natural column widths using str_width
    natural_widths = [0] * num_cols
    if header:
        for i, c in enumerate(header):
            natural_widths[i] = max(natural_widths[i], str_width(c))
    for r in norm_data:
        for i, c in enumerate(r):
            natural_widths[i] = max(natural_widths[i], str_width(c))

    # Available width after border overhead (3 per col separator + 2 ends + 2 space indent)
    avail_w = max_total_width - (3 * (num_cols - 1) + 4 + 2)

    col_widths = list(natural_widths)
    if sum(col_widths) > avail_w:
        if num_cols == 2:
            # 2-column format: col 0 is label/path, col 1 is description
            col_widths[0] = min(natural_widths[0], 28)
            col_widths[1] = max(20, avail_w - col_widths[0])
        else:
            # Give short/fixed columns their natural width (up to 24 chars)
            # and compress only the wider elastic columns
            fixed_w = 0
            elastic_cols = []
            for i, w in enumerate(col_widths):
                if w <= 22:
                    fixed_w += w
                else:
                    elastic_cols.append(i)

            if elastic_cols and (avail_w - fixed_w) >= (12 * len(elastic_cols)):
                rem_for_elastic = avail_w - fixed_w
                elastic_nat = sum(col_widths[i] for i in elastic_cols) or 1
                for i in elastic_cols:
                    col_widths[i] = max(12, int(col_widths[i] * rem_for_elastic / elastic_nat))
            else:
                tot_nat = sum(natural_widths) or 1
                col_widths = [max(8, int(w * avail_w / tot_nat)) for w in natural_widths]

    out_lines = []

    # Top border
    top_b = f"  {DARK_SLATE}┌" + "┬".join("─" * (w + 2) for w in col_widths) + f"┐{RST}"
    out_lines.append(top_b)

    # Header row
    if header:
        h_styled_cells = []
        for i, c in enumerate(header):
            w = col_widths[i]
            styled = f"{BOLD}{WHITE}{_style_inline(c)}{RST}"
            sw = str_width(styled)
            pad = " " * max(0, w - sw)
            h_styled_cells.append(f" {styled}{pad} ")
        out_lines.append(f"  {DARK_SLATE}│{RST}" + f"{DARK_SLATE}│{RST}".join(h_styled_cells) + f"{DARK_SLATE}│{RST}")

        mid_b = f"  {DARK_SLATE}├" + "┼".join("─" * (w + 2) for w in col_widths) + f"┤{RST}"
        out_lines.append(mid_b)

    # Data rows with word wrapping
    for row_idx, r in enumerate(norm_data):
        wrapped_cols = []
        for i, c in enumerate(r):
            w = col_widths[i]
            lines = textwrap.wrap(c, width=w) if c else [""]
            wrapped_cols.append(lines or [""])

        max_sublines = max(len(wc) for wc in wrapped_cols)
        for sub_i in range(max_sublines):
            row_cells = []
            for col_i in range(num_cols):
                w = col_widths[col_i]
                sub_text = wrapped_cols[col_i][sub_i] if sub_i < len(wrapped_cols[col_i]) else ""
                styled = _style_inline(sub_text)
                sw = str_width(styled)
                pad = " " * max(0, w - sw)
                row_cells.append(f" {styled}{pad} ")
            out_lines.append(f"  {DARK_SLATE}│{RST}" + f"{DARK_SLATE}│{RST}".join(row_cells) + f"{DARK_SLATE}│{RST}")

    # Bottom border
    bot_b = f"  {DARK_SLATE}└" + "┴".join("─" * (w + 2) for w in col_widths) + f"┘{RST}"
    out_lines.append(bot_b)
    return out_lines

def format_markdown(text: str, max_width: int | None = None) -> str:
    """Format markdown text with terminal ANSI aesthetics, tables, code fences, and lists."""
    if max_width is None:
        max_width = min(term_width() - 4, 100)

    lines = text.splitlines()
    out = []
    in_code_block = False
    table_buffer: list[str] = []

    def flush_table():
        nonlocal table_buffer
        if table_buffer:
            out.extend(render_table(table_buffer, max_total_width=max_width))
            table_buffer = []

    for line in lines:
        stripped = line.strip()

        # Markdown table detection
        if not in_code_block and stripped.startswith("|") and ("|" in stripped[1:]):
            table_buffer.append(stripped)
            continue
        else:
            flush_table()

        # Code block fence
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            lang = stripped.lstrip("`").strip()
            if in_code_block:
                border_w = max(20, min(max_width, 80) - len(lang) - 8)
                out.append(f"  {DARK_SLATE}┌── {LBLUE}{lang or 'code'}{DARK_SLATE} {'─' * border_w}{RST}")
            else:
                border_w = max(26, min(max_width, 80) - 4)
                out.append(f"  {DARK_SLATE}└──{'─' * border_w}{RST}")
            continue

        if in_code_block:
            out.append(f"  {DARK_SLATE}│{RST} {MINT}{line}{RST}")
            continue

        # Blank line
        if not stripped:
            out.append("")
            continue

        # Horizontal rule
        if re.match(r"^(\-{3,}|\*{3,}|_{3,})$", stripped):
            hr_w = min(max_width - 4, 80)
            out.append(f"  {DARK_SLATE}{'─' * hr_w}{RST}")
            continue

        # Headings
        if stripped.startswith("# "):
            title_text = _style_inline(stripped[2:].strip())
            out.append(f"\n  {GOLD}{BOLD}■ {title_text}{RST}")
        elif stripped.startswith("## "):
            title_text = _style_inline(stripped[3:].strip())
            out.append(f"\n  {CYAN}{BOLD}▸ {title_text}{RST}")
        elif stripped.startswith("### "):
            title_text = _style_inline(stripped[4:].strip())
            out.append(f"\n  {TEAL}{BOLD}▫ {title_text}{RST}")
        # Task lists / Checklists
        elif re.match(r"^[-*]\s+\[([ xX])\]", stripped):
            m = re.match(r"^[-*]\s+\[([ xX])\]\s*(.*)", stripped)
            is_checked = m.group(1).lower() == "x" if m else False
            content = m.group(2) if m else ""
            indent = len(line) - len(line.lstrip())
            box = f"{MINT}☑{RST}" if is_checked else f"{SLATE}☐{RST}"
            out.append(f"  {' ' * indent}{box} {_style_inline(content)}")
        # Bullet points
        elif stripped.startswith(("- ", "* ", "• ")):
            indent = len(line) - len(line.lstrip())
            content = stripped[2:]
            out.append(f"  {' ' * indent}{TEAL}•{RST} {_style_inline(content)}")
        # Numbered list
        elif re.match(r"^\d+\.\s+", stripped):
            m = re.match(r"^(\d+)\.\s+(.*)", stripped)
            indent = len(line) - len(line.lstrip())
            num, content = (m.group(1), m.group(2)) if m else ("", stripped)
            out.append(f"  {' ' * indent}{TEAL}{num}.{RST} {_style_inline(content)}")
        # Callouts / Blockquotes
        elif stripped.startswith(">"):
            content = stripped.lstrip("> ").strip()
            if content.startswith("[!NOTE]"):
                out.append(f"  {LBLUE}{BOLD}ℹ Note:{RST} {_style_inline(content[7:].strip())}")
            elif content.startswith("[!TIP]"):
                out.append(f"  {MINT}{BOLD}💡 Tip:{RST} {_style_inline(content[6:].strip())}")
            elif content.startswith("[!IMPORTANT]"):
                out.append(f"  {AMBER}{BOLD}⚡ Important:{RST} {_style_inline(content[12:].strip())}")
            elif content.startswith("[!WARNING]"):
                out.append(f"  {ROSE}{BOLD}⚠️ Warning:{RST} {_style_inline(content[10:].strip())}")
            else:
                out.append(f"  {PURPLE}│{RST} {ITALIC}{_style_inline(content)}{RST}")
        else:
            out.append(f"  {_style_inline(line)}")

    flush_table()
    return "\n".join(out)
