"""Markdown renderer: streaming equivalence, inline rules, tables, code, lists and robustness."""
from __future__ import annotations

import random

import pytest

from axon.ui.markdown import MarkdownStream, format_markdown, highlight, render_table
from axon.ui.theme import display_width, strip_ansi

DOC = """# Title
Intro with **bold**, *italic*, ~~gone~~, `__init__.py`, $5 and $10, math $x^2$ and [a link](https://example.com).

## Lists
- one
  - nested
    1. deep numbered
- [x] done task
- [ ] open task

> [!WARNING] careful
> quoted line

| Name | Status | Notes |
| :--- | :---: | ---: |
| `foo.py` | ✅ Ready | a long note that has to wrap inside its narrow column for sure |

```python
# not a heading
def f(x):
    return "| not a table |"
```

---
#### small heading
"""


def _plain(s: str) -> str:
    return strip_ansi(s)


def test_stream_matches_whole_document_for_any_chunking():
    whole = format_markdown(DOC, max_width=60)
    rng = random.Random(7)
    for _ in range(5):
        stream = MarkdownStream(width=60)
        out = ""
        i = 0
        while i < len(DOC):
            n = rng.randint(1, 12)
            out += stream.feed(DOC[i:i + n])
            i += n
        out += stream.flush()
        assert out.rstrip("\n") == whole


def test_code_fence_state_survives_streaming():
    stream = MarkdownStream(width=80)
    out = ""
    for line in DOC.splitlines(keepends=True):
        out += stream.feed(line)
    out += stream.flush()
    plain = _plain(out)
    assert "# not a heading" in plain          # comment inside code is not rendered as a heading
    assert '"| not a table |"' in plain        # pipes inside code are not a table
    assert "── python" in plain


def test_inline_rules():
    plain = _plain(format_markdown(DOC, max_width=100))
    assert "__init__.py" in plain              # code span content is literal
    assert "$5 and $10" in plain               # money is not math
    assert "x²" in plain                       # real inline math
    assert "gone" in plain and "~~" not in plain
    assert "a link" in plain and "[a link]" not in plain
    assert "small heading" in plain and "####" not in plain


def test_lists_and_callouts():
    plain = _plain(format_markdown(DOC, max_width=100))
    assert "• one" in plain
    assert "◦ nested" in plain
    assert "1. deep numbered" in plain
    assert "☑ done task" in plain and "☐ open task" in plain
    assert "⚠ Warning: careful" in plain


def test_paragraph_and_bullet_wrapping_respects_width():
    text = "word " * 60 + "\n- " + "item " * 40
    for width in (24, 40, 80):
        out = format_markdown(text, max_width=width)
        assert all(display_width(l) <= width for l in out.splitlines()), width
    rows = _plain(format_markdown("- " + "item " * 40, max_width=30)).splitlines()
    assert rows[1].startswith("    item")    # hanging indent under the bullet


@pytest.mark.parametrize("width", [30, 50, 100])
def test_table_width_aware(width: int):
    lines = render_table([
        "| Name | Status | Description |",
        "| :--- | :---: | ---: |",
        "| `foo.py` | ✅ Ready | Python file with a very long description that will wrap |",
        "| 中文名 | ⏳ Pending | C++ |",
    ], max_total_width=width)
    assert lines
    assert all(display_width(l) <= width for l in lines)
    joined = _plain("\n".join(lines))
    assert "Name" in joined and "Ready" in joined


def test_table_edge_cases():
    assert render_table([]) == []
    assert render_table(["| a |"])
    ragged = render_table(["| a | b | c |", "|---|---|---|", "| 1 |", "| 1 | 2 | 3 | 4 |"], max_total_width=60)
    assert ragged and "1" in _plain("\n".join(ragged))
    escaped = _plain("\n".join(render_table(["| a | b |", "|---|---|", r"| x \| y | `p|q` |"], max_total_width=60)))
    assert "x | y" in escaped and "p|q" in escaped
    many = render_table(["|" + "|".join(f"c{i}" for i in range(30)) + "|"], max_total_width=40)
    assert many and all(display_width(l) <= 40 for l in many)


@pytest.mark.parametrize("lang,line,needle", [
    ("python", "def f(x):  # c", "\x1b"), ("js", "const a = 1; // c", "const"), ("json", '{"a": true}', "true"),
    ("bash", "echo $HOME --flag", "$HOME"), ("powershell", "Get-Item $env:PATH", "Get"), ("diff", "+added", "+added"),
    ("go", "func main() {}", "func"), ("rust", "fn main() -> u8 {}", "fn"), ("sql", "SELECT * FROM t", "SELECT"),
    ("yaml", "key: value # c", "key"), ("unknown", "plain text", "plain"),
])
def test_highlight_never_loses_text(lang: str, line: str, needle: str):
    out = highlight(line, lang)
    assert strip_ansi(out) == line
    assert needle in out or needle == "\x1b"


@pytest.mark.parametrize("text", [
    "", "```", "```\nunclosed", "$$\nx^2", "|", "| |", "**", "*", "`", "[]()", "> ", ">>>> deep",
    "\x1b[2J\x07control", "#", "#######", "- [x]", "1.", "\t\tindented", "~~~\ncode\n~~~", "a" * 5000,
    "$$x$$", "\\*not italic\\*", "<https://x.org>",
])
def test_never_crashes_on_odd_input(text: str):
    out = format_markdown(text, max_width=40)
    assert isinstance(out, str)
    assert "\x07" not in out and "\x1b[2J" not in out


def test_long_paragraph_rows_stream_early():
    stream = MarkdownStream(width=30)
    emitted = stream.feed("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron")
    assert emitted  # complete wrapped rows appear before the newline arrives
    rest = stream.feed(" pi\n") + stream.flush()
    full = format_markdown("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi", max_width=30)
    assert (emitted + rest).rstrip("\n") == full
