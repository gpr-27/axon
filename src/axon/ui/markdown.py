"""
Markdown renderer for the Axon terminal.

* ``MarkdownStream`` renders markdown incrementally (fed by streaming deltas): block state such as
  fenced code, tables and math survives across chunks, complete lines are emitted exactly once
  (no redraw, no flicker) and long paragraph lines are emitted row by row as they wrap.
* ``format_markdown`` renders a whole document with the same engine.
* Headings, emphasis, strikethrough, inline code, links (OSC 8 when supported), nested / numbered /
  task lists, block quotes and callouts, width-aware tables, fenced code with lightweight syntax
  highlighting, horizontal rules and LaTeX math (converted to Unicode).
* Width calculations are ANSI-, wide-char- and emoji-aware; untrusted control characters are
  stripped; malformed input never raises.
"""
from __future__ import annotations

import re
from pathlib import Path

from axon.ui.theme import (
    ACCENT,
    BOLD,
    BORDER,
    CODE_BG,
    CODE_FG,
    CYAN,
    DIFF_ADD,
    DIFF_DEL,
    ERROR,
    INFO,
    ITALIC,
    LINK,
    MUTED,
    PRIMARY,
    RST,
    STRIKE,
    SUBTLE,
    SUCCESS,
    TEXT,
    UNDER,
    WARNING,
    char_width,
    display_width,
    glyph,
    hard_wrap_ansi,
    hyperlink,
    pad,
    sanitize,
    wrap_ansi,
)

# Backwards-compatible alias: width of a string ignoring escape sequences.
str_width = display_width

__all__ = [
    "MarkdownStream",
    "char_width",
    "file_uri",
    "format_markdown",
    "highlight",
    "latex_to_unicode",
    "make_clickable",
    "render_table",
    "str_width",
]

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
            if j == i + 1 and j < len(s):
                j += 1  # single-character command such as \, \; \% \#
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
                    k = s.find("]", j)
                    if k < 0:
                        k = len(s)
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




# ── Links ──────────────────────────────────────────────────────────────────────

def file_uri(path_str: str, workspace: Path | None = None) -> str:
    """Return a valid ``file://`` URI for *path_str* (relative paths resolve against *workspace*)."""
    try:
        p = Path(str(path_str).strip().strip("'\"")).expanduser()
        if not p.is_absolute():
            p = (workspace or Path.cwd()) / p
        return p.resolve().as_uri()
    except (ValueError, OSError):
        return ""


def link_target(url_or_path: str, workspace: Path | None = None) -> str:
    clean = url_or_path.strip().strip("<>").strip("'\"")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", clean) or clean.startswith("mailto:"):
        return clean
    return file_uri(clean.split("#", 1)[0], workspace)


def make_clickable(label: str, url_or_path: str, workspace: Path | None = None) -> str:
    """Styled hyperlink (OSC 8 when the terminal supports it)."""
    return hyperlink(link_target(url_or_path, workspace), f"{UNDER}{LINK}{label}{RST}")


# ── Syntax highlighting ────────────────────────────────────────────────────────

_LANG_ALIASES = {
    "py": "python", "python3": "python", "py3": "python", "ipython": "python", "pyi": "python", "pyw": "python",
    "js": "js", "javascript": "js", "jsx": "js", "ts": "js", "typescript": "js", "tsx": "js",
    "mjs": "js", "cjs": "js", "java": "js", "c": "js", "cpp": "js", "c++": "js", "cc": "js", "h": "js",
    "hpp": "js", "cs": "js", "csharp": "js", "kotlin": "js", "kt": "js", "swift": "js", "php": "js",
    "scala": "js", "dart": "js", "zig": "js",
    "sh": "bash", "bash": "bash", "zsh": "bash", "shell": "bash", "console": "bash", "fish": "bash",
    "ps": "powershell", "ps1": "powershell", "powershell": "powershell", "pwsh": "powershell",
    "bat": "powershell", "cmd": "powershell",
    "json": "json", "jsonc": "json", "json5": "json", "jsonl": "json", "ndjson": "json", "ipynb": "json",
    "diff": "diff", "patch": "diff", "udiff": "diff",
    "go": "go", "golang": "go",
    "rs": "rust", "rust": "rust",
    "sql": "sql", "postgres": "sql", "postgresql": "sql", "mysql": "sql", "sqlite": "sql", "psql": "sql",
    "yaml": "yaml", "yml": "yaml", "ini": "yaml", "cfg": "yaml", "conf": "yaml", "properties": "yaml",
    "env": "yaml", "dotenv": "yaml",
    "toml": "toml", "lock": "toml",
    "html": "html", "htm": "html", "xml": "html", "svg": "html", "vue": "html", "svelte": "html",
    "xhtml": "html", "plist": "html",
    "css": "css", "scss": "css", "sass": "css", "less": "css",
    "md": "markdown", "markdown": "markdown", "mdx": "markdown",
    "rb": "ruby", "ruby": "ruby", "lua": "lua", "r": "python",
    "dockerfile": "dockerfile", "containerfile": "dockerfile", "make": "bash", "makefile": "bash", "mk": "bash",
}

_KEYWORDS = {
    "python": set("""and as assert async await break class continue def del elif else except finally
        for from global if import in is lambda nonlocal not or pass raise return try while with yield
        match case True False None self cls""".split()),
    "js": set("""async await break case catch class const continue debugger default delete do else
        export extends finally for from function if import in instanceof let new of return static super
        switch this throw try typeof var void while with yield true false null undefined interface type
        enum implements public private protected readonly abstract as public int void char long double
        float bool struct namespace using include define package fun val override""".split()),
    "bash": set("""if then else elif fi for while until do done case esac in function return local
        export source alias unset readonly declare echo cd exit set shift trap eval exec""".split()),
    "powershell": set("""if else elseif foreach for while do until switch function param return
        begin process end try catch finally throw break continue trap filter in""".split()),
    "go": set("""break case chan const continue default defer else fallthrough for func go goto if
        import interface map package range return select struct switch type var nil true false""".split()),
    "rust": set("""as async await break const continue crate dyn else enum extern false fn for if impl
        in let loop match mod move mut pub ref return self Self static struct super trait true type
        unsafe use where while Some None Ok Err""".split()),
    "sql": set("""select from where and or not insert into values update set delete create table drop
        alter index join left right inner outer full on as group by order having limit offset union all
        distinct case when then else end null is in like between exists primary key foreign references
        default view with returning asc desc count sum avg min max begin commit rollback""".split()),
    "ruby": set("""alias and begin break case class def defined do else elsif end ensure false for if in
        module next nil not or redo rescue retry return self super then true undef unless until when while
        yield require attr_accessor attr_reader puts""".split()),
    "lua": set("""and break do else elseif end false for function goto if in local nil not or repeat return
        then true until while require""".split()),
    "json": {"true", "false", "null"},
    "yaml": {"true", "false", "null", "yes", "no", "on", "off", "True", "False", "None"},
    "toml": {"true", "false"},
    "dockerfile": set("""FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG ONBUILD
        STOPSIGNAL HEALTHCHECK SHELL AS""".split()),
}
_TYPES = {
    "python": set("int str float bool list dict set tuple bytes object type Any Optional Union List Dict "
                  "Set Tuple Path Iterator Callable ClassVar Exception Decimal".split()),
    "js": set("string number boolean any unknown never object Promise Array Map Set Record "
              "Partial Error".split()),
    "go": set("string int int64 int32 uint byte rune float64 float32 bool error any".split()),
    "rust": set("i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str String "
                "Vec Option Result Box Rc Arc HashMap".split()),
}
_DEF_KEYWORDS = {"def", "class", "function", "func", "fn", "struct", "enum", "trait", "interface", "type", "impl"}
_LINE_COMMENT = {
    "python": "#", "bash": "#", "powershell": "#", "yaml": "#", "toml": "#", "ruby": "#", "dockerfile": "#",
    "js": "//", "go": "//", "rust": "//", "sql": "--", "lua": "--",
}
# Block constructs that may span lines: (opener, closer, style name) per language.
_BLOCKS = {
    "python": (('"""', '"""', "string"), ("'''", "'''", "string")),
    "js": (("/*", "*/", "comment"), ("`", "`", "string")),
    "go": (("/*", "*/", "comment"), ("`", "`", "string")),
    "rust": (("/*", "*/", "comment"),),
    "css": (("/*", "*/", "comment"),),
    "sql": (("/*", "*/", "comment"),),
    "html": (("<!--", "-->", "comment"),),
    "markdown": (("<!--", "-->", "comment"),),
    "lua": (("--[[", "]]", "comment"),),
}

_TOKEN_RE = re.compile(
    r"(?P<STRING>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)"
    r"|(?P<VAR>\$\{?[A-Za-z_][\w]*\}?|\$\w)"
    r"|(?P<NUMBER>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)"
    r"|(?P<DECOR>@[A-Za-z_][\w.]*)"
    r"|(?P<WORD>[A-Za-z_][\w]*)"
    r"|(?P<SPACE>\s+)"
    r"|(?P<OTHER>.)"
)
_HTML_RE = re.compile(
    r"(?P<COMMENT><!--.*?(?:-->|$))|(?P<TAG></?[A-Za-z][\w:.-]*|/?>)|(?P<ATTR>[A-Za-z_:][\w:.-]*(?=\s*=))"
    r"|(?P<STRING>\"[^\"]*\"?|'[^']*'?)|(?P<ENTITY>&#?\w+;)|(?P<TEXT>[^<\"'&]+|.)"
)
_CSS_RE = re.compile(
    r"(?P<COMMENT>/\*.*?(?:\*/|$))|(?P<STRING>\"[^\"]*\"?|'[^']*'?)|(?P<AT>@[\w-]+)|(?P<HEX>#[0-9a-fA-F]{3,8}\b)"
    r"|(?P<NUMBER>-?\d*\.?\d+(?:px|em|rem|%|vh|vw|s|ms|deg|fr|ch|pt)?\b)|(?P<IMPORTANT>!important)"
    r"|(?P<WORD>-?[A-Za-z_][\w-]*)|(?P<OTHER>\s+|.)"
)


def normalize_lang(lang: str) -> str:
    key = (lang or "").strip().lower().split()[0] if (lang or "").strip() else ""
    key = key.lstrip("{.").rstrip("}")
    return _LANG_ALIASES.get(key, key)


def lang_for_path(path: str) -> str:
    """Highlighter language for a file path ('' when unknown)."""
    name = Path(str(path or "")).name.lower()
    if name in ("dockerfile", "containerfile") or name.startswith("dockerfile."):
        return "dockerfile"
    if name in ("makefile", "gnumakefile"):
        return "bash"
    if name.startswith(".env"):
        return "yaml"
    if name in (".bashrc", ".zshrc", ".profile", ".bash_profile"):
        return "bash"
    suffix = Path(name).suffix.lstrip(".")
    return normalize_lang(suffix) if suffix else ""


def _style(kind: str) -> str:
    """Colour for a token kind (looked up at call time: the theme can be switched live)."""
    return {
        "string": SUCCESS, "comment": f"{SUBTLE}{ITALIC}", "number": WARNING, "keyword": ACCENT,
        "type": PRIMARY, "call": INFO, "key": CYAN, "var": CYAN, "flag": WARNING, "text": TEXT,
        "tag": ACCENT, "attr": CYAN, "heading": f"{PRIMARY}{BOLD}", "code": CODE_FG, "link": LINK,
        "def": f"{INFO}{BOLD}", "class": f"{PRIMARY}{BOLD}", "decorator": INFO, "section": f"{ACCENT}{BOLD}",
        "add": DIFF_ADD, "del": DIFF_DEL, "hunk": CYAN, "meta": f"{BOLD}{TEXT}", "muted": MUTED,
    }.get(kind, "")


def _join_spans(spans: list[tuple[str, str]]) -> str:
    return "".join(f"{st}{tx}{RST}" if st and tx else tx for st, tx in spans)


def highlight_spans(line: str, lang: str = "") -> list[tuple[str, str]]:
    """Tokenised *line* as (style, text) spans; joining the texts gives back *line* exactly."""
    if not line:
        return []
    lang = normalize_lang(lang)
    if lang == "diff":
        if line.startswith("+") and not line.startswith("+++"):
            return [(_style("add"), line)]
        if line.startswith("-") and not line.startswith("---"):
            return [(_style("del"), line)]
        if line.startswith("@@"):
            return [(_style("hunk"), line)]
        if line.startswith(("+++", "---", "diff ", "index ")):
            return [(_style("meta"), line)]
        return [(_style("muted"), line)]
    if lang == "html":
        return _html_spans(line)
    if lang == "css":
        return _css_spans(line)
    if lang == "markdown":
        return _markdown_spans(line)
    if lang == "toml" and re.match(r"^\s*\[\[?[^\]]*\]\]?\s*(#.*)?$", line):
        head, _, tail = line.partition("#")
        return [(_style("section"), head)] + ([(_style("comment"), "#" + tail)] if _ else [])

    comment = _LINE_COMMENT.get(lang)
    code, tail = line, ""
    if comment is None:
        s = line.lstrip()
        if s.startswith(("#", "//", "--", "/*", "* ")):
            return [(_style("comment"), line)]
    else:
        idx = _find_comment(line, comment)
        if lang == "js" and idx < 0:
            s = line.lstrip()
            if s.startswith(("/*", "* ", "*/")):
                return [(_style("comment"), line)]
        if idx >= 0:
            code, tail = line[:idx], line[idx:]

    keywords = _KEYWORDS.get(lang, _KEYWORDS["python"] | _KEYWORDS["js"])
    types = _TYPES.get(lang, set())
    out: list[tuple[str, str]] = []
    tokens = list(_TOKEN_RE.finditer(code))
    first_word = _first_word_index(tokens)
    prev_word = ""
    for n, mo in enumerate(tokens):
        kind = mo.lastgroup
        val = mo.group()
        if kind == "STRING":
            nxt = code[mo.end():].lstrip()[:1]
            out.append((_style("key") if lang in ("json", "yaml", "toml") and nxt in (":", "=") else _style("string"), val))
        elif kind == "VAR":
            out.append((_style("var") if lang in ("bash", "powershell", "dockerfile") else "", val))
        elif kind == "NUMBER":
            out.append((_style("number"), val))
        elif kind == "DECOR":
            out.append((_style("decorator") if lang in ("python", "js") else _style("text"), val))
        elif kind == "WORD":
            lookup = val.lower() if lang in ("sql", "powershell") else val
            nxt = code[mo.end():mo.end() + 1]
            if lang in ("yaml", "toml") and n == first_word and code[mo.end():].lstrip().startswith((":", "=")):
                out.append((_style("key"), val))
            elif lang == "dockerfile" and n == first_word and val.upper() in keywords:
                out.append((_style("keyword"), val))
            elif prev_word in _DEF_KEYWORDS and lookup not in keywords:
                out.append((_style("class" if prev_word in ("class", "struct", "enum", "trait", "interface", "type")
                                   else "def"), val))
            elif lookup in keywords:
                out.append((_style("keyword"), val))
            elif val in types:
                out.append((_style("type"), val))
            elif nxt == "(":
                out.append((_style("call"), val))
            elif lang in ("bash", "powershell") and mo.start() > 0 and code[mo.start() - 1] == "-":
                out.append((_style("flag"), val))
            else:
                out.append((_style("text"), val))
            prev_word = lookup if lookup in _DEF_KEYWORDS else ""
            continue
        else:
            out.append(("", val))
        if kind != "SPACE":
            prev_word = ""
    if tail:
        out.append((_style("comment"), tail))
    return out


def _html_spans(line: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    in_tag = False
    for mo in _HTML_RE.finditer(line):
        kind, val = mo.lastgroup, mo.group()
        if kind == "COMMENT":
            out.append((_style("comment"), val))
        elif kind == "TAG":
            in_tag = not val.endswith(">")
            out.append((_style("tag"), val))
        elif kind == "ATTR" and in_tag:
            out.append((_style("attr"), val))
        elif kind == "STRING" and in_tag:
            out.append((_style("string"), val))
        elif kind == "ENTITY":
            out.append((_style("number"), val))
        else:
            out.append((_style("text") if val.strip() else "", val))
    return out


def _css_spans(line: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    declaration = ":" in line and not line.rstrip().endswith(("{", ","))
    seen_colon = False
    for mo in _CSS_RE.finditer(line):
        kind, val = mo.lastgroup, mo.group()
        if kind == "COMMENT":
            out.append((_style("comment"), val))
        elif kind == "STRING":
            out.append((_style("string"), val))
        elif kind == "AT" or kind == "IMPORTANT":
            out.append((_style("keyword"), val))
        elif kind in ("HEX", "NUMBER"):
            out.append((_style("number"), val))
        elif kind == "WORD":
            if declaration and not seen_colon:
                out.append((_style("key"), val))
            elif declaration:
                out.append((_style("call") if line[mo.end():mo.end() + 1] == "(" else _style("text"), val))
            else:
                out.append((_style("type"), val))
        else:
            if val == ":":
                seen_colon = True
            out.append(("", val))
    return out


def _markdown_spans(line: str) -> list[tuple[str, str]]:
    s = line.lstrip()
    if re.match(r"#{1,6}\s", s):
        return [(_style("heading"), line)]
    if s.startswith(">"):
        return [(_style("comment"), line)]
    out: list[tuple[str, str]] = []
    m = re.match(r"^(\s*)([-*+]|\d+[.)])(\s+)", line)
    pos = 0
    if m:
        out += [("", m.group(1)), (_style("keyword"), m.group(2)), ("", m.group(3))]
        pos = m.end()
    for mo in re.finditer(r"`[^`]*`|\[[^\]]*\]\([^)]*\)|\*\*[^*]+\*\*|[^`\[*]+|.", line[pos:]):
        val = mo.group()
        if val.startswith("`") and len(val) > 1:
            out.append((_style("code"), val))
        elif val.startswith("[") and "](" in val:
            out.append((_style("link"), val))
        elif val.startswith("**") and len(val) > 4:
            out.append((BOLD, val))
        else:
            out.append((_style("text"), val))
    return out


def highlight(line: str, lang: str = "") -> str:
    """Lightweight single-line syntax highlighter; output always ends with a reset."""
    if not line:
        return ""
    return _join_spans(highlight_spans(line, lang))


def _block_opener(code: str, lang: str) -> tuple[int, str, str] | None:
    """(index, closer, style kind) of a block construct opened but not closed on *code*, else None."""
    blocks = _BLOCKS.get(lang)
    if not blocks:
        return None
    comment = _LINE_COMMENT.get(lang)
    limit = len(code)
    if comment:
        ci = _find_comment(code, comment)
        if ci >= 0:
            limit = ci
    best: tuple[int, str, str] | None = None
    for opener, closer, kind in blocks:
        if opener == closer:
            # same delimiter (python triple quotes, JS template strings): odd count leaves one open
            if opener == "`" and lang == "js" and code.count("```"):
                continue
            pos, i, n = [], 0, len(opener)
            while True:
                i = code.find(opener, i)
                if i < 0 or i >= limit:
                    break
                if opener == "`" and i > 0 and code[i - 1] == "\\":
                    i += 1
                    continue
                pos.append(i)
                i += n
            if len(pos) % 2 == 1 and (best is None or pos[-1] < best[0]):
                best = (pos[-1], closer, kind)
        else:
            i = code.rfind(opener, 0, limit)
            if i >= 0 and code.find(closer, i + len(opener)) < 0 and (best is None or i < best[0]):
                best = (i, closer, kind)
    return best


def highlight_stateful(line: str, lang: str, state: tuple[str, str] | None = None) -> tuple[str, tuple[str, str] | None]:
    """Highlight one line of a multi-line source, tracking block comments / multi-line strings.

    *state* is ``None`` or ``(closer, style kind)`` of a construct still open from earlier lines;
    returns the highlighted line and the state for the next line."""
    lang = normalize_lang(lang)
    head = ""
    if state is not None:
        closer, kind = state
        idx = line.find(closer)
        if idx < 0:
            return (f"{_style(kind)}{line}{RST}" if line and _style(kind) else line), state
        cut = idx + len(closer)
        head = f"{_style(kind)}{line[:cut]}{RST}" if _style(kind) else line[:cut]
        line = line[cut:]
    if not line:
        return head, None
    op = _block_opener(line, lang)
    if op is None:
        return head + highlight(line, lang), None
    i, closer, kind = op
    rest = line[i:]
    return head + highlight(line[:i], lang) + (f"{_style(kind)}{rest}{RST}" if _style(kind) else rest), (closer, kind)


def highlight_block(lines: list[str], lang: str = "") -> list[str]:
    """Highlight consecutive source lines (multi-line strings and block comments keep their colour)."""
    out: list[str] = []
    state: tuple[str, str] | None = None
    for ln in lines:
        hl, state = highlight_stateful(ln, lang, state)
        out.append(hl)
    return out


def _first_word_index(tokens: list) -> int:
    for i, t in enumerate(tokens):
        if t.lastgroup in ("WORD", "STRING"):
            return i
    return -1


def _find_comment(line: str, marker: str) -> int:
    """Index of a line comment *marker* outside string literals, else -1."""
    quote = ""
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"', "`"):
            quote = ch
        elif line.startswith(marker, i):
            if marker == "#" and i > 0 and line[i - 1] not in " \t" and line[i - 1] != "(":
                i += 1  # e.g. "${#arr}" or "a#b" - not a comment
                continue
            return i
        i += 1
    return -1


# ── Inline styling ─────────────────────────────────────────────────────────────

_PH_OPEN, _PH_CLOSE = "\ue000", "\ue001"
_PH_RE = re.compile(_PH_OPEN + r"(\d+)" + _PH_CLOSE)
_PATHLIKE_RE = re.compile(
    r"^[\w\-.~/\\]+\.(py|pyi|js|jsx|ts|tsx|json|md|toml|env|html|css|txt|sh|ps1|rs|go|cpp|c|h|hpp|"
    r"java|kt|rb|csv|yaml|yml|ini|cfg|lock|sql)(:\d+(-\d+)?)?$"
)


def _style_inline(s: str) -> str:
    """Apply inline markdown: code spans, links, math, emphasis, strikethrough and result badges."""
    if not s:
        return s
    slots: list[str] = []

    def stash(text: str) -> str:
        slots.append(text)
        return f"{_PH_OPEN}{len(slots) - 1}{_PH_CLOSE}"

    # 1. Code spans first: their content is literal.
    def _code(m: re.Match) -> str:
        code = m.group(2).strip() if m.group(2).strip() else m.group(2)
        if _PATHLIKE_RE.match(code) or ("/" in code and " " not in code and not code.startswith(("http", "-"))):
            path = re.sub(r":\d+(-\d+)?$", "", code)
            return stash(hyperlink(link_target(path), f"{CODE_FG}{code}{RST}"))
        return stash(f"{CODE_BG}{CODE_FG}{code}{RST}" if CODE_FG else f"`{code}`")

    s = re.sub(r"(`+)(.+?)\1", _code, s)
    # 2. Backslash escapes.
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~$<>])", lambda m: stash(m.group(1)), s)
    # 3. Links: [label](target), <autolink>, bare URLs.
    s = re.sub(
        r"\[([^\]\n]+)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
        lambda m: stash(make_clickable(_emphasis(m.group(1)), m.group(2))),
        s,
    )
    s = re.sub(r"<((?:https?|file)://[^>\s]+)>", lambda m: stash(make_clickable(m.group(1), m.group(1))), s)
    s = re.sub(
        r"(?<![\w(\"'=/])(https?://[^\s<>()\"']+[^\s<>()\"'.,;:!?])",
        lambda m: stash(make_clickable(m.group(1), m.group(1))),
        s,
    )
    # 4. Inline math $...$ (must not start with a space or digit: "$5 and $10" is money).
    s = re.sub(
        r"(?<![\$\w])\$(?=[^\s\d$])([^$\n]+?)(?<=\S)\$(?![\$\w])",
        lambda m: stash(f"{WARNING}{latex_to_unicode(m.group(1))}{RST}"),
        s,
    )
    s = _emphasis(s)
    # 5. Result badges (colour only; the text is never rewritten).
    s = re.sub(r"(?<![\w-])(PASS(?:ED)?|OK)(?![\w-])", lambda m: f"{SUCCESS}{BOLD}{m.group(1)}{RST}", s)
    s = re.sub(r"(?<![\w-])(FAIL(?:ED|URE)?|ERROR)(?![\w-])", lambda m: f"{ERROR}{BOLD}{m.group(1)}{RST}", s)
    s = re.sub(r"(?<![\w-])(WARN(?:ING)?)(?![\w-])", lambda m: f"{WARNING}{BOLD}{m.group(1)}{RST}", s)

    # Restore placeholders (they may nest, e.g. a link label containing code).
    for _ in range(4):
        if _PH_OPEN not in s:
            break
        s = _PH_RE.sub(lambda m: slots[int(m.group(1))] if int(m.group(1)) < len(slots) else "", s)
    return s


def _inline_closed(src: str) -> bool:
    """True when *src* has no inline construct left open (safe to render on its own)."""
    if src.count("`") % 2 or src.count("$") % 2:
        return False
    if src.count("[") != src.count("]") or src.count("(") != src.count(")") or src.count("<") != src.count(">"):
        return False
    if src.count("**") % 2 or src.count("~~") % 2 or src.count("__") % 2:
        return False
    if (src.count("*") - 2 * src.count("**")) % 2:
        return False
    if src.endswith("\\"):
        return False
    return True


def _emphasis(s: str) -> str:
    s = re.sub(r"\*\*\*(?=\S)([^\n*]+?)(?<=\S)\*\*\*", rf"{BOLD}{ITALIC}\1{RST}", s)
    s = re.sub(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*", rf"{BOLD}\1{RST}", s)
    s = re.sub(r"(?<![\w_])__(?=\S)([^\n]+?)(?<=\S)__(?![\w_])", rf"{BOLD}\1{RST}", s)
    s = re.sub(r"~~(?=\S)([^\n~]+?)(?<=\S)~~", rf"{STRIKE}{MUTED}\1{RST}", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])([^\n*]+?)(?<=[^\s*])\*(?![\w*])", rf"{ITALIC}\1{RST}", s)
    s = re.sub(r"(?<![\w/\\.])_(?=[^\s_])([^\n_]+?)(?<=[^\s_])_(?![\w/\\.])", rf"{ITALIC}\1{RST}", s)
    return s


# ── Tables ─────────────────────────────────────────────────────────────────────

_DELIM_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")
_NUMERIC_CELL_RE = re.compile(r"^[*_`]*[-+~≈]?[$€£¥]?\s?[-+]?(?:\d[\d,_]*)?(?:\.\d+)?\s?(?:%|[kKmMbBx]|ms|s|KB|MB|GB)?[*_`]*$")


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes that are outside code spans."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    cur: list[str] = []
    in_code = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _is_delimiter_row(cells: list[str]) -> bool:
    non_empty = [c for c in cells if c]
    return bool(non_empty) and all(_DELIM_CELL_RE.match(c) for c in non_empty)


def render_table(table_lines: list[str], max_total_width: int | None = None) -> list[str]:
    """Render markdown table rows as an aligned, width-aware box table (cells wrap to fit)."""
    if max_total_width is None:
        from axon.ui.theme import layout_width
        max_total_width = layout_width()
    rows = [_split_row(l) for l in table_lines if l.strip()]
    if not rows:
        return []
    header: list[str] | None = None
    aligns: list[str] = []
    explicit: list[bool] = []
    body = rows
    if len(rows) >= 2 and _is_delimiter_row(rows[1]):
        header, delim, body = rows[0], rows[1], rows[2:]
        for c in delim:
            c = c.strip()
            aligns.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left")
            explicit.append(c.startswith(":") or c.endswith(":"))
    else:
        body = [r for r in rows if not _is_delimiter_row(r)]
    ncols = max([len(r) for r in body] + ([len(header)] if header else []) + [0])
    if ncols == 0:
        return []
    aligns += ["left"] * (ncols - len(aligns))
    explicit += [False] * (ncols - len(explicit))
    for i in range(ncols):     # numbers line up on the right unless the table says otherwise
        cells = [r[i].strip() for r in body if i < len(r) and r[i].strip()]
        if not explicit[i] and cells and all(_NUMERIC_CELL_RE.match(c) and re.search(r"\d", c) for c in cells):
            aligns[i] = "right"

    def norm(r: list[str]) -> list[str]:
        return (r + [""] * ncols)[:ncols]

    styled_header = [f"{BOLD}{PRIMARY}{_style_inline(c)}{RST}" if c else "" for c in norm(header)] if header else None
    styled_body = [[_style_inline(c) for c in norm(r)] for r in body]

    natural = [0] * ncols
    for r in ([styled_header] if styled_header else []) + styled_body:
        for i, c in enumerate(r):
            natural[i] = max(natural[i], display_width(c), 1)

    indent = 2
    overhead = indent + 3 * ncols + 1
    avail = max_total_width - overhead
    if avail < 3 * ncols:
        return _render_table_vertical(styled_header, styled_body, max_total_width)
    widths = list(natural)
    while sum(widths) > avail:
        i = max(range(ncols), key=lambda k: widths[k])
        if widths[i] <= 3:
            break
        widths[i] -= 1

    v = glyph("v")
    h = glyph("h")
    sp = " " * indent
    out = [f"{sp}{BORDER}{glyph('sq_tl')}" + glyph("t_down").join(h * (w + 2) for w in widths) + f"{glyph('sq_tr')}{RST}"]

    def emit(cells: list[str], is_header: bool = False) -> None:
        wrapped = [wrap_ansi(c, widths[i]) if c else [""] for i, c in enumerate(cells)]
        height = max(len(w) for w in wrapped)
        for k in range(height):
            parts = []
            for i in range(ncols):
                text = wrapped[i][k] if k < len(wrapped[i]) else ""
                parts.append(" " + pad(text, widths[i], "center" if is_header else aligns[i]) + " ")
            out.append(f"{sp}{BORDER}{v}{RST}" + f"{BORDER}{v}{RST}".join(parts) + f"{BORDER}{v}{RST}")

    if styled_header:
        emit(styled_header, is_header=True)
        out.append(f"{sp}{BORDER}{glyph('t_right')}" + glyph("cross").join(h * (w + 2) for w in widths) + f"{glyph('t_left')}{RST}")
    for r in styled_body:
        emit(r)
    out.append(f"{sp}{BORDER}{glyph('sq_bl')}" + glyph("t_up").join(h * (w + 2) for w in widths) + f"{glyph('sq_br')}{RST}")
    return out


def _render_table_vertical(header: list[str] | None, body: list[list[str]], width: int) -> list[str]:
    """Fallback for tables too wide for the terminal: one 'key: value' block per row."""
    out: list[str] = []
    for n, row in enumerate(body):
        if n:
            out.append(f"  {BORDER}{glyph('h') * max(4, min(width - 4, 20))}{RST}")
        for i, cell in enumerate(row):
            key = header[i] if header and i < len(header) else f"{i + 1}"
            for line in wrap_ansi(f"{key}{RST}: {cell}", max(10, width - 4)):
                out.append(f"  {line}")
    return out


# ── Block rendering ────────────────────────────────────────────────────────────

CODE_NUMBER_AFTER = 5          # code blocks longer than this get line numbers
CHART_LANGS = {"chart", "bar", "bars", "barchart", "bar-chart", "hbar", "sparkline", "spark", "sparklines"}
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}((?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_TASK_RE = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_NUM_RE = re.compile(r"^(\s*)(\d{1,9})([.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*((?:>\s?)+)(.*)$")
_CALLOUTS = {
    "NOTE": ("info", INFO, "Note"),
    "TIP": ("ok", SUCCESS, "Tip"),
    "IMPORTANT": ("diamond", ACCENT, "Important"),
    "WARNING": ("warn", WARNING, "Warning"),
    "CAUTION": ("warn", ERROR, "Caution"),
}


class MarkdownStream:
    """Incremental markdown renderer.

    ``feed(chunk)`` returns the rendered rows that became final (joined with ``\\n``, with a trailing
    newline when non-empty); ``flush()`` renders whatever is still pending.  Output is never revised.
    """

    def __init__(self, width: int | None = None, indent: int = 2) -> None:
        from axon.ui.theme import layout_width
        self.width = max(20, width if width is not None else layout_width())
        self.indent = indent
        self._partial = ""
        self._para: tuple[str, str, int, int] | None = None  # early-emitted paragraph state
        self._para_rows = 0
        self._fence: tuple[str, int] | None = None  # (char, length)
        self._lang = ""
        self._code_buf: list[str] | None = None   # first lines of a code block (numbered once it has > 5)
        self._code_no = 0                          # lines of the open code block emitted so far (numbered)
        self._hl_state: tuple[str, str] | None = None
        self._chart: list[str] | None = None       # body of an open ```chart block (drawn when it closes)
        self._callout = ""                         # colour of the callout a quote block opened
        self._table: list[str] = []
        self._math: list[str] | None = None
        self._last_blank = True
        self._emitted_any = False

    # public API ---------------------------------------------------------------
    def feed(self, text: str) -> str:
        if not text:
            return ""
        text = sanitize(text).replace("\r", "")
        rows: list[str] = []
        data = self._partial + text
        lines = data.split("\n")
        self._partial = lines.pop()
        for line in lines:
            if self._para is not None:
                rows.extend(self._finish_para(line))
            else:
                rows.extend(self._render_line(line))
        rows.extend(self._early_rows())
        return self._join(rows)

    def flush(self) -> str:
        rows: list[str] = []
        if self._partial and not self._partial.strip() and self._fence is None:
            self._partial = ""
        if self._para is not None:
            rows.extend(self._finish_para(self._partial))
            self._partial = ""
        elif self._partial:
            rows.extend(self._render_line(self._partial))
            self._partial = ""
        rows.extend(self._flush_table())
        if self._math is not None:
            rows.extend(self._render_math(self._math))
            self._math = None
        if self._fence is not None:
            rows.extend(self._close_fence())
        return self._join(rows)

    @property
    def in_code_block(self) -> bool:
        return self._fence is not None

    # internals ----------------------------------------------------------------
    @staticmethod
    def _join(rows: list[str]) -> str:
        return ("\n".join(rows) + "\n") if rows else ""

    def _early_rows(self) -> list[str]:
        """Emit complete rows of a long, still-growing paragraph line.

        Rows are cut at the *source* level: a row is committed only when its markdown source is
        self-contained (no open code span, emphasis, link or math), so the final render of the rest
        of the line can never disagree with what was already printed (no text is ever lost).
        """
        p = self._partial
        if not p or self._fence is not None or self._math is not None or self._table:
            return []
        if self._para is None:
            s = p.lstrip()
            if s.startswith(("|", "```", "~~~", "$$", "#", ">")) or display_width(p) <= self.width - self.indent:
                return []
            if _HR_RE.match(p):
                return []
            self._para = self._para_layout(p)
        first_prefix, cont_prefix, avail, offset = self._para
        out: list[str] = []
        while True:
            rest = p[offset:]
            tokens = list(re.finditer(r"\S+", rest))
            if len(tokens) < 2:
                break
            taken = 0
            for i in range(1, len(tokens)):
                candidate = rest[tokens[0].start():tokens[i - 1].end()]
                if display_width(_style_inline(candidate)) > avail:
                    break
                taken = i
            else:
                break  # everything left still fits on one row: the row is not complete yet
            if taken == 0:
                break  # a single over-long token: let the final render hard-break it
            row_src = rest[tokens[0].start():tokens[taken - 1].end()]
            if not _inline_closed(row_src):
                break
            prefix = first_prefix if not self._para_rows else cont_prefix
            out.append(prefix + _style_inline(row_src))
            self._para_rows += 1
            offset += tokens[taken].start()
            self._para = (first_prefix, cont_prefix, avail, offset)
        if out:
            self._last_blank = False
            self._emitted_any = True
        return out

    def _para_layout(self, line: str) -> tuple[str, str, int, int]:
        """(first-row prefix, continuation prefix, body width, body source offset) for *line*."""
        base = " " * self.indent
        tm = _TASK_RE.match(line)
        bm = None if tm else _BULLET_RE.match(line)
        nm = None if (tm or bm) else _NUM_RE.match(line)
        if tm or bm or nm:
            if tm:
                done = tm.group(2).lower() == "x"
                marker = f"{SUCCESS}{glyph('task_done')}{RST}" if done else f"{MUTED}{glyph('task_todo')}{RST}"
                depth, start = self._depth(tm.group(1)), tm.start(3)
            elif bm:
                depth, start = self._depth(bm.group(1)), bm.start(2)
                marker = f"{PRIMARY}{glyph(('bullet', 'bullet2', 'bullet3')[depth % 3])}{RST}"
            else:
                depth, start = self._depth(nm.group(1)), nm.start(4)
                marker = f"{PRIMARY}{nm.group(2)}{nm.group(3)}{RST}"
            lead = " " * (self.indent + 2 * depth)
            mw = display_width(marker) + 1
            return f"{lead}{marker} ", lead + " " * mw, max(10, self.width - len(lead) - mw), start
        start = len(line) - len(line.lstrip())
        return base, base, self.width - self.indent, start

    def _finish_para(self, line: str) -> list[str]:
        """Render the not-yet-emitted remainder of a paragraph whose first rows went out early."""
        assert self._para is not None
        first_prefix, cont_prefix, avail, offset = self._para
        emitted = self._para_rows
        self._para = None
        self._para_rows = 0
        if not emitted:
            return self._render_line(line)
        tm = _TASK_RE.match(line)
        task_done = bool(tm) and tm.group(2).lower() == "x"
        rest = line[offset:].strip()
        self._last_blank = False
        self._emitted_any = True
        if not rest:
            return []
        styled = _style_inline(rest)
        if task_done:
            styled = f"{MUTED}{styled}{RST}"
        return [cont_prefix + r for r in wrap_ansi(styled, avail)]

    def _flush_table(self) -> list[str]:
        if not self._table:
            return []
        rows = self._spacer() + render_table(self._table, max_total_width=self.width)
        self._table = []
        self._last_blank = False
        return rows

    def _spacer(self) -> list[str]:
        if self._emitted_any and not self._last_blank:
            self._last_blank = True
            return [""]
        return []

    def _render_line(self, line: str) -> list[str]:
        # Inside a fenced code block: everything is literal until the closing fence.
        if self._fence is not None:
            ch, n = self._fence
            st = line.strip()
            if st and set(st) == {ch} and len(st) >= n:
                return self._close_fence()
            if self._chart is not None:
                self._chart.append(line)
                return []
            if self._code_buf is not None:
                self._code_buf.append(line)
                if len(self._code_buf) <= CODE_NUMBER_AFTER:
                    return []
                buf, self._code_buf = self._code_buf, None      # long block: numbered from line 1
                return [r for ln in buf for r in self._code_row(ln, numbered=True)]
            return self._code_row(line, numbered=True)

        if self._math is not None:
            if line.strip() == "$$":
                rows = self._render_math(self._math)
                self._math = None
                return rows
            self._math.append(line)
            return []

        st = line.strip()
        if st.startswith("|") and "|" in st[1:]:
            self._table.append(st)
            return []
        rows = self._flush_table()

        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(2)
            self._fence = (fence[0], len(fence))
            self._lang = m.group(3)
            self._hl_state = None
            self._code_no = 0
            rows += self._spacer()
            if self._lang.strip().lower() in CHART_LANGS:
                self._chart = []          # drawn as a whole when the fence closes
            else:
                self._code_buf = []
                rows.append(self._code_header(self._lang))
            self._last_blank = False
            self._emitted_any = True
            return rows

        if st == "$$" or (st.startswith("$$") and st.endswith("$$") and len(st) > 4):
            if st == "$$":
                self._math = []
                return rows
            return rows + self._render_math([st[2:-2]])

        return rows + self._render_block(line)

    def _code_header(self, lang: str) -> str:
        """'╭── python' - the top of a code card (the frame lives in the gutter only, so selecting
        the code copies clean text)."""
        label = lang.strip() if lang.strip() else ""
        return (f"{' ' * self.indent}{BORDER}{glyph('tl')}{glyph('h') * 2}{RST}"
                + (f" {MUTED}{label}{RST}" if label else ""))

    def _close_fence(self) -> list[str]:
        rows: list[str] = []
        if self._chart is not None:
            body, self._chart = "\n".join(self._chart), None
            try:
                from axon.ui.charts import render_chart
                chart = render_chart(self._lang, body, self.width, self.indent)
            except Exception:
                chart = None
            if chart is not None:
                rows = chart
            else:                      # not chart data: show it as code
                rows = [self._code_header(self._lang)]
                for ln in body.split("\n") if body else []:
                    rows += self._code_row(ln, numbered=False)
                rows.append(f"{' ' * self.indent}{BORDER}{glyph('bl')}{glyph('h') * 2}{RST}")
        else:
            if self._code_buf is not None:
                buf, self._code_buf = self._code_buf, None
                for ln in buf:
                    rows += self._code_row(ln, numbered=False)
            rows.append(f"{' ' * self.indent}{BORDER}{glyph('bl')}{glyph('h') * 2}{RST}")
        self._fence = None
        self._lang = ""
        self._hl_state = None
        self._code_no = 0
        self._last_blank = False
        self._emitted_any = True
        return rows

    def _code_row(self, line: str, numbered: bool = False) -> list[str]:
        """Render one code line behind the card gutter ('│  12  code'); lines wider than the card are
        soft-wrapped with a ↪ marker under the same gutter."""
        code = line.expandtabs(4)
        hl, self._hl_state = highlight_stateful(code, self._lang, self._hl_state)
        bar = f"{' ' * self.indent}{BORDER}{glyph('v')}{RST} "
        num = ""
        if numbered:
            self._code_no += 1
            num = f"{SUBTLE}{self._code_no:>3}{RST}  "
        avail = max(8, self.width - self.indent - 2 - display_width(num))
        mark = glyph("wrap") + " "
        first, *rest = hard_wrap_ansi(hl, avail - display_width(mark), first_width=avail) if hl else [""]
        parts = [first] + [f"{MUTED}{mark}{RST}{r}" for r in rest]
        out = []
        for k, part in enumerate(parts):
            lead = bar + (num if k == 0 else " " * display_width(num))
            if CODE_BG:
                fill = max(0, avail - display_width(part))
                body = CODE_BG + part.replace(RST, RST + CODE_BG) if RST else part
                out.append(f"{lead}{body}{CODE_BG}{' ' * fill}{RST}")
            else:
                out.append(f"{lead}{part}".rstrip(" ") if not part else f"{lead}{part}")
        return out

    def _render_math(self, latex_lines: list[str]) -> list[str]:
        joined = " ".join(l.strip() for l in latex_lines if l.strip())
        if not joined:
            return []
        rows = self._spacer()
        for line in latex_to_unicode(joined).split("\n"):
            rows.append(f"{' ' * (self.indent + 2)}{WARNING}{line}{RST}")
        self._last_blank = False
        self._emitted_any = True
        return rows

    def _render_block(self, line: str) -> list[str]:
        st = line.strip()
        base = " " * self.indent
        if self._callout and not _QUOTE_RE.match(line):
            self._callout = ""
        if not st:
            if self._last_blank:
                return []
            self._last_blank = True
            return [""]

        rows: list[str] = []
        hm = _HEADING_RE.match(line)
        if hm:
            level = len(hm.group(1))
            title = _style_inline(hm.group(2))
            if self._emitted_any and not self._last_blank:
                rows.append("")
            if level == 1:
                styled = f"{PRIMARY}{BOLD}{title}{RST}"
                rows += [base + r for r in wrap_ansi(styled, self.width - self.indent)]
                rows.append(f"{base}{BORDER}{glyph('hh') * min(self.width - self.indent, max(3, display_width(title)))}{RST}")
            elif level == 2:
                rows += [base + r for r in wrap_ansi(f"{ACCENT}{BOLD}{title}{RST}", self.width - self.indent)]
            elif level == 3:
                rows += [base + r for r in wrap_ansi(f"{BOLD}{TEXT}{title}{RST}", self.width - self.indent)]
            elif level == 4:
                rows += [base + r for r in wrap_ansi(f"{BOLD}{MUTED}{title}{RST}", self.width - self.indent)]
            else:
                rows += [base + r for r in wrap_ansi(f"{MUTED}{ITALIC}{title}{RST}", self.width - self.indent)]
            self._last_blank = False
            self._emitted_any = True
            return rows

        if _HR_RE.match(line):
            self._last_blank = False
            self._emitted_any = True
            return [f"{base}{BORDER}{glyph('h') * max(3, min(self.width - self.indent, 80))}{RST}"]

        tm = _TASK_RE.match(line)
        bm = None if tm else _BULLET_RE.match(line)
        nm = None if (tm or bm) else _NUM_RE.match(line)
        qm = None if (tm or bm or nm) else _QUOTE_RE.match(line)
        if tm:
            depth = self._depth(tm.group(1))
            done = tm.group(2).lower() == "x"
            marker = f"{SUCCESS}{glyph('task_done')}{RST}" if done else f"{MUTED}{glyph('task_todo')}{RST}"
            body = _style_inline(tm.group(3))
            if done:
                body = f"{MUTED}{body}{RST}"
            rows = self._hanging(marker, body, depth)
        elif bm:
            depth = self._depth(bm.group(1))
            bullet = glyph(("bullet", "bullet2", "bullet3")[depth % 3])
            rows = self._hanging(f"{PRIMARY}{bullet}{RST}", _style_inline(bm.group(2)), depth)
        elif nm:
            depth = self._depth(nm.group(1))
            rows = self._hanging(f"{PRIMARY}{nm.group(2)}{nm.group(3)}{RST}", _style_inline(nm.group(4)), depth)
        elif qm:
            level = qm.group(1).count(">")
            content = qm.group(2).strip()
            bar = f"{BORDER}{glyph('quote')}{RST} " * level
            cm = re.match(r"^\[!(\w+)\]\s*(.*)$", content)
            if cm and cm.group(1).upper() in _CALLOUTS:
                icon, color, label = _CALLOUTS[cm.group(1).upper()]
                styled = f"{color}{BOLD}{glyph(icon)} {label}:{RST} {_style_inline(cm.group(2))}"
                self._callout = color
            elif self._callout:
                styled = f"{TEXT}{_style_inline(content)}{RST}" if content else ""
            else:
                styled = f"{MUTED}{ITALIC}{_style_inline(content)}{RST}" if content else ""
            if self._callout:
                bar = f"{self._callout}{glyph('quote')}{RST} " + f"{BORDER}{glyph('quote')}{RST} " * (level - 1)
            avail = self.width - self.indent - 2 * level
            rows = [base + bar + r for r in wrap_ansi(styled, max(10, avail))] if styled else [base + bar.rstrip()]
        else:
            rows = [base + r for r in wrap_ansi(_style_inline(st), self.width - self.indent)]
        self._last_blank = False
        self._emitted_any = True
        return rows

    @staticmethod
    def _depth(ws: str) -> int:
        return min(6, len(ws.expandtabs(4)) // 2)

    def _hanging(self, marker: str, body: str, depth: int) -> list[str]:
        lead = " " * (self.indent + 2 * depth)
        mw = display_width(marker) + 1
        avail = max(10, self.width - len(lead) - mw)
        wrapped = wrap_ansi(body, avail) if body else [""]
        rows = [f"{lead}{marker} {wrapped[0]}"]
        rows += [f"{lead}{' ' * mw}{r}" for r in wrapped[1:]]
        return rows


def format_markdown(text: str, max_width: int | None = None) -> str:
    """Render a complete markdown document for the terminal."""
    if not text:
        return ""
    try:
        stream = MarkdownStream(width=max_width)
        out = stream.feed(text) + stream.flush()
    except Exception:  # never let odd input crash the UI
        return sanitize(text)
    return out.rstrip("\n")
