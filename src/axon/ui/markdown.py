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

# ── LaTeX to Unicode converter ──────────────────────────────────────────────────

# Unicode superscript and subscript digit maps
_SUPER_DIGITS = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
_SUB_DIGITS = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUPER_LETTERS = {
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
    "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}
_SUB_LETTERS = {
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Alpha": "Α", "Beta": "Β", "Gamma": "Γ", "Delta": "Δ", "Epsilon": "Ε",
    "Theta": "Θ", "Lambda": "Λ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
    "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ", "ell": "ℓ",
}

_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡",
    "propto": "∝", "sim": "∼", "ll": "≪", "gg": "≫",
    "to": "→", "rightarrow": "→", "Rightarrow": "⇒", "leftarrow": "←",
    "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "implies": "⟹", "iff": "⟺",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "forall": "∀", "exists": "∃",
    "quad": "  ", "qquad": "    ", ";": " ", ",": " ", ":": " ",
    "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "star": "⋆", "circ": "∘", "bullet": "•",
    "sum": "∑", "prod": "∏", "int": "∫",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋",
    "%": "%", " ": " ", "#": "#", "&": "&",
    "text": "",  # handled separately
    "mathrm": "", "mathbf": "", "mathit": "", "mathcal": "",
    "left": "", "right": "", "Big": "", "big": "", "bigg": "", "Bigg": "",
}


def _extract_brace_arg(s: str, pos: int) -> tuple[str, int]:
    """Extract content inside { } starting at pos, handling nested braces."""
    if pos >= len(s) or s[pos] != "{":
        # Single character arg (no braces)
        if pos < len(s):
            return s[pos], pos + 1
        return "", pos
    depth = 0
    start = pos + 1
    i = pos
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    return s[start:], len(s)


def _to_superscript(text: str) -> str:
    """Convert text to Unicode superscript characters."""
    result = []
    for ch in text:
        if ch in _SUPER_LETTERS:
            result.append(_SUPER_LETTERS[ch])
        else:
            translated = ch.translate(_SUPER_DIGITS)
            result.append(translated)
    return "".join(result)


def _to_subscript(text: str) -> str:
    """Convert text to Unicode subscript characters."""
    result = []
    for ch in text:
        if ch in _SUB_LETTERS:
            result.append(_SUB_LETTERS[ch])
        else:
            translated = ch.translate(_SUB_DIGITS)
            result.append(translated)
    return "".join(result)


def latex_to_unicode(latex: str) -> str:
    """Convert a LaTeX math expression to readable Unicode text for terminal display."""
    s = latex.strip()

    # Pass 1: \frac{a}{b} → (a)/(b) or a/b for simple args
    def _replace_frac(m: re.Match) -> str:
        rest = s[m.end():] if m.end() < len(s) else ""
        # We need to parse the braces from the original string
        return m.group(0)  # placeholder, handled below

    # Manual \frac parsing (regex can't handle nested braces)
    result = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            # Read command name
            j = i + 1
            if s[j] == "\\":  # \\  → newline
                result.append("\n")
                i = j + 1
                continue
            while j < len(s) and (s[j].isalpha() or s[j] == "@"):
                j += 1
            cmd = s[i + 1:j]

            if cmd == "frac":
                num, j = _extract_brace_arg(s, j)
                den, j = _extract_brace_arg(s, j)
                num_u = latex_to_unicode(num)
                den_u = latex_to_unicode(den)
                # Simple fractions don't need parens
                if len(num_u) <= 3 and not any(c in num_u for c in "+-×÷"):
                    frac_str = f"{num_u}/{den_u}"
                else:
                    frac_str = f"({num_u})/({den_u})"
                result.append(frac_str)
                i = j
            elif cmd == "sqrt":
                # Check for optional [n] root
                if j < len(s) and s[j] == "[":
                    k = s.index("]", j)
                    root_n = s[j + 1:k]
                    j = k + 1
                    arg, j = _extract_brace_arg(s, j)
                    arg_u = latex_to_unicode(arg)
                    root_sup = _to_superscript(root_n)
                    result.append(f"{root_sup}√({arg_u})")
                else:
                    arg, j = _extract_brace_arg(s, j)
                    arg_u = latex_to_unicode(arg)
                    if len(arg_u) <= 2:
                        result.append(f"√{arg_u}")
                    else:
                        result.append(f"√({arg_u})")
                i = j
            elif cmd in ("text", "mathrm", "mathbf", "mathit", "mathcal", "operatorname"):
                arg, j = _extract_brace_arg(s, j)
                result.append(arg)
                i = j
            elif cmd in ("hat", "bar", "vec", "dot", "ddot", "tilde", "overline"):
                arg, j = _extract_brace_arg(s, j)
                arg_u = latex_to_unicode(arg)
                accents = {"hat": "̂", "bar": "̄", "vec": "⃗", "dot": "̇", "ddot": "̈", "tilde": "̃", "overline": "̄"}
                result.append(f"{arg_u}{accents.get(cmd, '')}")
                i = j
            elif cmd in ("left", "right", "Big", "big", "bigg", "Bigg"):
                # Size modifiers — skip the command, keep the delimiter
                result.append("")  # ignore sizing
                i = j
            elif cmd in _GREEK:
                result.append(_GREEK[cmd])
                i = j
            elif cmd in _SYMBOLS:
                result.append(_SYMBOLS[cmd])
                i = j
            elif cmd == "log":
                result.append("log")
                i = j
            elif cmd == "ln":
                result.append("ln")
                i = j
            elif cmd == "sin":
                result.append("sin")
                i = j
            elif cmd == "cos":
                result.append("cos")
                i = j
            elif cmd == "tan":
                result.append("tan")
                i = j
            elif cmd == "lim":
                result.append("lim")
                i = j
            elif cmd == "max":
                result.append("max")
                i = j
            elif cmd == "min":
                result.append("min")
                i = j
            else:
                # Unknown command — render as-is without backslash
                result.append(cmd)
                i = j
        elif s[i] == "^":
            # Superscript
            arg, j = _extract_brace_arg(s, i + 1)
            arg_u = latex_to_unicode(arg)
            result.append(_to_superscript(arg_u))
            i = j
        elif s[i] == "_":
            # Subscript
            arg, j = _extract_brace_arg(s, i + 1)
            arg_u = latex_to_unicode(arg)
            result.append(_to_subscript(arg_u))
            i = j
        elif s[i] in ("{" , "}"):
            # Skip bare braces (grouping)
            i += 1
        elif s[i] == "~":
            result.append(" ")
            i += 1
        else:
            result.append(s[i])
            i += 1

    return "".join(result)


def _render_math_block(latex_lines: list[str]) -> list[str]:
    """Render a $$ math block as a styled Unicode formula."""
    joined = " ".join(l.strip() for l in latex_lines if l.strip())
    rendered = latex_to_unicode(joined)
    out = []
    out.append(f"  {DARK_SLATE}┌── {LBLUE}math{DARK_SLATE} {'─' * 40}{RST}")
    for line in rendered.split("\n"):
        out.append(f"  {DARK_SLATE}│{RST}  {GOLD}{line}{RST}")
    out.append(f"  {DARK_SLATE}└──{'─' * 44}{RST}")
    return out


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

def make_clickable(label: str, url_or_path: str, workspace: Path | None = None) -> str:
    """OSC 8 terminal hyperlink: \x1b]8;;URL\x1b\\LABEL\x1b]8;;\x1b\\"""
    clean_target = url_or_path.strip().strip("'\"")
    if clean_target.startswith(("http://", "https://", "file://")):
        target = clean_target
    else:
        ws = workspace or Path.cwd()
        p = Path(clean_target).expanduser()
        abs_p = (ws / p).resolve() if not p.is_absolute() else p.resolve()
        target = f"file://{abs_p}"
    return f"\x1b]8;;{target}\x1b\\{UNDER}{TEAL}{label}{RST}\x1b]8;;\x1b\\"

def _style_inline(s: str) -> str:
    """Apply inline bold, code, emphasis, clickable links, and inline math."""
    # 1. Inline math: $...$ -> Unicode formula
    s = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", lambda m: f"{GOLD}{latex_to_unicode(m.group(1))}{RST}", s)

    # 2. Result badges: PASS, FAIL, WARN
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:✓\s*)?PASS(?:\b|(?=[\s|`]))", rf"{MINT}{BOLD}✓ PASS{RST}", s)
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:✗\s*)?FAIL(?:\b|(?=[\s|`]))", rf"{ROSE}{BOLD}✗ FAIL{RST}", s)
    s = re.sub(r"(?:\b|(?<=[\s|`]))(?:⚠\s*)?WARN(?:ING)?(?:\b|(?=[\s|`]))", rf"{GOLD}{BOLD}⚠ WARN{RST}", s)

    # 3. Bold + Italic ***text*** -> BOLD ITALIC WHITE
    s = re.sub(r"\*\*\*([^\n*]+?)\*\*\*", rf"{BOLD}{ITALIC}{WHITE}\1{RST}", s)

    # 4. Bold **text** or __text__ -> BOLD WHITE
    s = re.sub(r"\*\*([^\n*]+?)\*\*", rf"{BOLD}{WHITE}\1{RST}", s)
    s = re.sub(r"(?<!\w)__([^\n_]+?)__(?!\w)", rf"{BOLD}{WHITE}\1{RST}", s)

    # 5. Italic *text* or _text_ (strictly outside word/path boundaries so filename underscores are never touched)
    s = re.sub(r"(?<!\*)\*([^\n*]+?)\*(?!\*)", rf"{ITALIC}\1{RST}", s)
    s = re.sub(r"(?<![\w/\\\.])_([^\n_]+?)_(?![\w/\\\.])", rf"{ITALIC}\1{RST}", s)

    # 6. Markdown links: [label](url_or_path) -> OSC 8 clickable link
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: make_clickable(m.group(1), m.group(2)), s)

    # 7. Inline code `code` -> check if file path (runs LAST so OSC 8 escapes are never corrupted)
    def _code_repl(m: re.Match) -> str:
        code_str = m.group(1)
        if re.match(r"^[\w\-./\\]+\.(py|js|ts|json|md|toml|env|html|css|txt|sh|rs|go|cpp|c|h|csv|yaml|yml)$", code_str) or "/" in code_str:
            return make_clickable(code_str, code_str)
        return f"{TEAL}{code_str}{RST}"

    s = re.sub(r"`([^`]+)`", _code_repl, s)
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
            styled = f"{BOLD}{TEAL}{_style_inline(c)}{RST}"
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
                if col_i == 0 and "\033[" not in styled and styled.strip():
                    styled = f"{CYAN}{BOLD}{styled}{RST}"
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
    in_math_block = False
    math_buffer: list[str] = []
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

        # Display math block: $$ ... $$
        if stripped == "$$" and not in_code_block:
            if not in_math_block:
                in_math_block = True
                math_buffer = []
            else:
                in_math_block = False
                out.extend(_render_math_block(math_buffer))
                math_buffer = []
            continue

        if in_math_block:
            math_buffer.append(stripped)
            continue

        # Code block fence
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            lang = stripped.lstrip("`").strip()
            if in_code_block:
                if lang:
                    border_w = max(20, min(max_width, 80) - len(lang) - 8)
                    out.append(f"  {DARK_SLATE}┌── {LBLUE}{lang}{DARK_SLATE} {'─' * border_w}{RST}")
                else:
                    border_w = max(26, min(max_width, 80) - 4)
                    out.append(f"  {DARK_SLATE}┌──{'─' * border_w}{RST}")
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
