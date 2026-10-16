#!/usr/bin/env python3
"""
Regenerate the reference parts of Axon's documentation from the code, so they never drift.

    python scripts/gen_docs.py            # rewrite generated regions + docs/assets/demo.svg
    python scripts/gen_docs.py --check    # exit 1 when anything is out of date (for CI)

Generated regions are delimited in Markdown files by

    <!-- BEGIN GENERATED: <name> -->
    ...
    <!-- END GENERATED: <name> -->

and everything between the markers is replaced. Sources of truth:

* slash commands   -> axon.commands.registry.visible_commands()
* tools            -> axon.tools.registry.create_default_registry().visible_tools()
* providers        -> axon.providers.catalog.PROVIDER_PRESETS
* settings         -> axon.config.Settings fields + the settings.json key map
* CLI flags        -> axon.cli parser (``axon --help``)
* terminal demo    -> a scripted turn rendered by axon.ui.render.Renderer (plain text for the
                      README, coloured ANSI converted to docs/assets/demo.svg)

Stdlib only (plus Axon itself, imported from ./src). Output is deterministic, so running the
script twice produces no diff.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEMO_WIDTH = 96
DEMO_PROMPT = "fix the failing test in tests/test_cart.py"


# ─── Scripted demo turn ────────────────────────────────────────────────────

def _demo_events(emit, advance=lambda s: None) -> None:  # noqa: ANN001
    """A realistic turn: read + grep in parallel, run the tests, edit with a hook, re-run, answer."""
    from axon.agent.trace import HookEvent, LLMCallEnd, PermissionDecisionEvent, ToolTiming
    from axon.providers.base import (LLMCallStart, TextDelta, ToolBatchStart, ToolExecutionResult,
                                     ToolExecutionStart, ToolUseBlock, TurnComplete, Usage)

    t0 = 1_760_000_000.0  # fixed clock: deterministic output
    model = "claude-sonnet-4-5"

    def tools(batch, results, *, perm=("allow", "read-only", "mode")) -> None:  # noqa: ANN001
        emit(ToolBatchStart(total_count=len(batch), tools=batch))
        for call, (content, lines, size, toks, secs) in zip(batch, results):
            emit(ToolExecutionStart(call.id, call.name, call.input))
            emit(PermissionDecisionEvent(id=call.id, tool=call.name, outcome=perm[0], reason=perm[1], source=perm[2]))
            advance(secs)
            emit(ToolExecutionResult(call.id, call.name, call.input, content))
            emit(ToolTiming(call.id, call.name, t0, t0 + secs, secs, lines, size, toks, False, len(batch) > 1))

    def llm(it: int, usage, cost: float, secs: float, stop: str, ctx: int, n_tools: int = 0) -> None:  # noqa: ANN001
        emit(TurnComplete(stop_reason=stop, usage=usage))
        emit(LLMCallEnd(iteration=it, model=model, usage=usage, cost=cost, duration_s=secs, stop_reason=stop,
                        started_at=t0, ended_at=t0 + secs, tool_use_count=n_tools,
                        context_used=ctx, context_window=200_000))

    emit(LLMCallStart(iteration=1, max_iterations=50, model=model, message_count=1, tokens_in=11_840,
                      user_prompt=DEMO_PROMPT, mode="acceptEdits"))
    emit(TextDelta("I'll look at the test and the code it exercises.\n"))
    llm(1, Usage(input=11_840, output=164, cache_read=10_912), 0.0061, 2.1, "tool_use", 12_004, 2)
    tools([ToolUseBlock("t1", "Read", {"file_path": "tests/test_cart.py"}),
           ToolUseBlock("t2", "Grep", {"pattern": "def total", "path": "src/", "output_mode": "content"})],
          [("\n".join(f"{i:>6}\u2192line {i}" for i in range(1, 49)), 48, 1_532, 410, 0.01),
           ("src/shop/cart.py:31:    def total(self) -> Decimal:", 1, 52, 14, 0.04)])

    emit(LLMCallStart(iteration=2, max_iterations=50, model=model, message_count=4, tokens_in=12_480))
    llm(2, Usage(input=12_480, output=96, cache_read=11_840), 0.0052, 1.4, "tool_use", 12_576, 1)
    tools([ToolUseBlock("t3", "Bash", {"command": "pytest -q tests/test_cart.py", "description": "Run the cart tests"})],
          [("F....\nFAILED tests/test_cart.py::test_discount_rounding - assert Decimal('9.99') == Decimal('10.00')\n"
            "1 failed, 4 passed in 0.21s", 3, 128, 36, 1.8)],
          perm=("allow", "Bash(pytest *)", "rule"))

    emit(LLMCallStart(iteration=3, max_iterations=50, model=model, message_count=6, tokens_in=12_660))
    emit(TextDelta("The discount is truncated instead of rounded. Fixing `total()`.\n"))
    llm(3, Usage(input=12_660, output=212, cache_read=12_480), 0.0068, 2.6, "tool_use", 12_872, 1)
    edit = ToolUseBlock("t4", "Edit", {"file_path": "src/shop/cart.py",
                                       "old_string": "        return (subtotal * rate).quantize(CENTS, ROUND_DOWN)\n",
                                       "new_string": "        return (subtotal * rate).quantize(CENTS, ROUND_HALF_UP)\n"})
    emit(ToolBatchStart(total_count=1, tools=[edit]))
    emit(ToolExecutionStart(edit.id, edit.name, edit.input))
    emit(PermissionDecisionEvent(id="t4", tool="Edit", outcome="allow", reason="acceptEdits", source="mode"))
    emit(HookEvent(event="PostToolUse", ran=1, tool_name="Edit", tool_use_id="t4"))
    advance(0.03)
    emit(ToolExecutionResult(edit.id, edit.name, edit.input, "Edited src/shop/cart.py (1 replacement)"))
    emit(ToolTiming("t4", "Edit", t0, t0 + 0.01, 0.01, 1, 40, 10, False, False))

    emit(LLMCallStart(iteration=4, max_iterations=50, model=model, message_count=8, tokens_in=12_930))
    llm(4, Usage(input=12_930, output=88, cache_read=12_660), 0.0049, 1.2, "tool_use", 13_018, 1)
    tools([ToolUseBlock("t5", "Bash", {"command": "pytest -q tests/test_cart.py", "description": "Re-run the cart tests"})],
          [(".....\n5 passed in 0.19s", 2, 26, 8, 1.7)], perm=("allow", "Bash(pytest *)", "rule"))

    emit(LLMCallStart(iteration=5, max_iterations=50, model=model, message_count=10, tokens_in=13_010))
    emit(TextDelta("Fixed. `Cart.total()` now rounds the discount with `ROUND_HALF_UP`, "
                   "so 9.995 becomes **10.00**. All 5 cart tests pass.\n"))
    llm(5, Usage(input=13_010, output=58, cache_read=12_930), 0.0045, 1.1, "end_turn", 13_068)


def _render_demo_here() -> str:
    """Render the scripted turn in this process (colour depends on the environment)."""
    import io

    from axon.providers.base import Usage
    from axon.ui import render
    from axon.ui.render import Renderer

    class _Out(io.StringIO):
        encoding = "utf-8"

        def isatty(self) -> bool:
            return False

    import time as _time

    clock = [1_760_000_000.0]

    class _Clock:  # deterministic wall clock for the renderer's own tool timings
        def __getattr__(self, name: str):  # noqa: ANN204
            return getattr(_time, name)

        @staticmethod
        def time() -> float:
            return clock[0]

    def advance(secs: float) -> None:
        clock[0] += secs

    render.term_width = lambda: DEMO_WIDTH  # type: ignore[assignment]
    render.time = _Clock()  # type: ignore[attr-defined]
    Renderer._tip_shown = True  # no random "Tip:" line
    buf = _Out()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = Renderer(trace_level="normal")
        _demo_events(r.on_event, advance)
        r.turn_footer(5, Usage(input=62_920, output=618), 0.0275, 10.6, llm_calls=5)
    finally:
        sys.stdout = real
    text = buf.getvalue()
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)  # drop OSC 8 hyperlinks
    text = re.sub(r"\r(?!\n)", "", text)
    return text.strip("\n") + "\n"


def render_demo(color: bool) -> str:
    """Render in a clean subprocess: Axon fixes its colour depth at import time."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXON_", "NO_COLOR", "FORCE_COLOR", "COLORTERM"))}
    home = tempfile.mkdtemp(prefix="axon-docs-")
    env.update({"PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8", "AXON_THEME": "dark",
                "AXON_NO_HYPERLINKS": "1", "AXON_NO_TIPS": "1", "AXON_HOME": home})
    if color:
        env.update({"AXON_FORCE_COLOR": "truecolor", "COLORTERM": "truecolor", "TERM": "xterm-256color"})
    else:
        env["NO_COLOR"] = "1"
    try:
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_render-demo"], env=env,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ─── ANSI -> SVG ───────────────────────────────────────────────────────────

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_XTERM16 = ["#1e2127", "#e06c75", "#98c379", "#d19a66", "#61afef", "#c678dd", "#56b6c2", "#abb2bf",
            "#5c6370", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#ffffff"]
_FG_DEFAULT = "#e4e6ec"
_BG = "#16181f"


def _xterm256(n: int) -> str:
    if n < 16:
        return _XTERM16[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def _parse_ansi_line(line: str) -> list[tuple[str, dict]]:
    """Split one line into (text, style) runs."""
    style: dict = {}
    runs: list[tuple[str, dict]] = []
    pos = 0
    for m in _SGR_RE.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], dict(style)))
        codes = [int(c) if c else 0 for c in m.group(1).split(";")] if m.group(1) else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                style = {}
            elif c == 1:
                style["bold"] = True
            elif c == 2:
                style["dim"] = True
            elif c == 3:
                style["italic"] = True
            elif c == 4:
                style["underline"] = True
            elif c == 7:
                style["reverse"] = True
            elif c == 22:
                style.pop("bold", None)
                style.pop("dim", None)
            elif c == 23:
                style.pop("italic", None)
            elif c == 24:
                style.pop("underline", None)
            elif c == 39:
                style.pop("fg", None)
            elif c == 49:
                style.pop("bg", None)
            elif 30 <= c <= 37:
                style["fg"] = _XTERM16[c - 30]
            elif 90 <= c <= 97:
                style["fg"] = _XTERM16[c - 90 + 8]
            elif 40 <= c <= 47:
                style["bg"] = _XTERM16[c - 40]
            elif c in (38, 48) and i + 1 < len(codes):
                key = "fg" if c == 38 else "bg"
                if codes[i + 1] == 2 and i + 4 < len(codes):
                    style[key] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    i += 4
                elif codes[i + 1] == 5 and i + 2 < len(codes):
                    style[key] = _xterm256(codes[i + 2])
                    i += 2
            i += 1
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], dict(style)))
    return [(t, s) for t, s in runs if t]


def ansi_to_svg(text: str, title: str) -> str:
    from axon.ui.theme import display_width

    lines = text.rstrip("\n").split("\n")
    cw, lh, pad_x, top = 8.4, 19, 18, 48
    cols = max(DEMO_WIDTH, max(display_width(_SGR_RE.sub("", ln)) for ln in lines))
    width = int(cols * cw + pad_x * 2)
    height = int(top + len(lines) * lh + 18)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f"<title>{html.escape(title)}</title>",
        "<style>text{font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace;"
        "font-size:14px;white-space:pre}.b{font-weight:700}.i{font-style:italic}.d{opacity:.62}"
        ".u{text-decoration:underline}</style>",
        f'<rect width="{width}" height="{height}" rx="10" fill="{_BG}"/>',
        f'<rect width="{width}" height="32" rx="10" fill="#22252e"/><rect y="22" width="{width}" height="10" fill="#22252e"/>',
        '<circle cx="20" cy="16" r="6" fill="#ff5f57"/><circle cx="40" cy="16" r="6" fill="#febc2e"/>'
        '<circle cx="60" cy="16" r="6" fill="#28c840"/>',
        f'<text x="{width / 2:.0f}" y="21" fill="#8c92a0" text-anchor="middle" style="font-size:12px">'
        f"{html.escape(title)}</text>",
    ]
    for n, line in enumerate(lines):
        y = top + n * lh + 14
        col = 0
        bgs: list[str] = []
        spans: list[str] = []
        for chunk, st in _parse_ansi_line(line):
            w = display_width(chunk)
            fg, bg = st.get("fg", _FG_DEFAULT), st.get("bg")
            if st.get("reverse"):
                fg, bg = bg or _BG, fg
            if bg:
                bgs.append(f'<rect x="{pad_x + col * cw:.1f}" y="{y - 14}" width="{w * cw:.1f}" height="{lh}" fill="{bg}"/>')
            cls = " ".join(k for k, flag in (("b", "bold"), ("i", "italic"), ("d", "dim"), ("u", "underline"))
                           if st.get(flag))
            attrs = f' x="{pad_x + col * cw:.1f}" fill="{fg}"' + (f' class="{cls}"' if cls else "")
            if chunk.strip():
                spans.append(f"<tspan{attrs}>{html.escape(chunk, quote=False)}</tspan>")
            col += w
        out.extend(bgs)
        if spans:
            out.append(f'<text y="{y}" xml:space="preserve">{"".join(spans)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ─── Reference tables ──────────────────────────────────────────────────────

def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _commands():
    from axon.commands.registry import CATEGORIES, visible_commands
    order = [c for c, _ in CATEGORIES]
    cmds = visible_commands()
    cats = order + sorted({c.category for c in cmds} - set(order))
    return [(cat, sorted((c for c in cmds if c.category == cat), key=lambda c: c.name)) for cat in cats
            if any(c.category == cat for c in cmds)]


def gen_commands_overview() -> str:
    rows = ["| Group | Commands |", "|---|---|"]
    total = 0
    for cat, cmds in _commands():
        total += len(cmds)
        rows.append(f"| **{cat}** | " + " ".join(f"`/{c.name}`" for c in cmds) + " |")
    return (f"{total} built-in commands. Run `/help` for the list or `/help <command>` for details; "
            "the full reference is in [docs/commands.md](docs/commands.md).\n\n" + "\n".join(rows))


def gen_commands_reference() -> str:
    from axon.commands.registry import resolve_builtin
    parts: list[str] = []
    groups = _commands()
    total = sum(len(c) for _, c in groups)
    parts.append(f"{total} built-in commands in {len(groups)} groups.\n")
    parts.append("Contents: " + " · ".join(f"[{cat}](#{re.sub(r'[^a-z0-9 -]', '', cat.lower()).replace(' ', '-')})"
                                            for cat, _ in groups) + "\n")
    for cat, cmds in groups:
        parts.append(f"## {cat}\n")
        parts.append("| Command | Aliases | Description |")
        parts.append("|---|---|---|")
        for c in cmds:
            live = [a for a in c.aliases if getattr(resolve_builtin(a), "name", None) == c.name]
            aliases = " ".join(f"`/{a}`" for a in live)
            parts.append(f"| `/{c.name}` | {aliases} | {_md(c.description)} |")
        parts.append("")
        for c in cmds:
            if c.usage and c.usage.strip() != f"/{c.name}":
                parts.append(f"<details><summary><code>/{c.name}</code> usage</summary>\n\n```text\n"
                             f"{c.usage.rstrip()}\n```\n\n</details>\n")
    return "\n".join(parts).rstrip()


def gen_tools() -> str:
    from axon.tools.registry import create_default_registry
    rows = ["| Tool | Default permission | What it does |", "|---|---|---|"]
    for t in create_default_registry().visible_tools():
        desc = (t.description or "").strip().split("\n")[0]
        first = re.split(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s+(?=\S)", desc, maxsplit=1)[0]
        rows.append(f"| `{t.name}` | {getattr(t, 'default_permission', 'ask')} | {_md(first)} |")
    return "\n".join(rows)


def gen_providers() -> str:
    from axon.providers.catalog import PROVIDER_PRESETS
    rows = ["| Preset id | Provider | Endpoint | API key variable(s) | Default model |", "|---|---|---|---|---|"]
    for p in PROVIDER_PRESETS:
        keys = ", ".join(f"`{v}`" for v in (p.env_vars or ())) or "none (local)"
        rows.append(f"| `{p.id}` | {_md(p.name)} | `{p.base_url}` | {keys} | `{p.default_model}` |")
    return "\n".join(rows)


def _fmt_default(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


_EXTRA_JSON_KEYS = {
    "mode": ["permissions.defaultMode"], "permissions": ["permissions"], "hooks": ["hooks"],
    "budget_turn_usd": ["budget.turn_usd"], "budget_session_usd": ["budget.session_usd"],
    "budget_action": ["budget.action"], "vim_mode": ["editorMode"],
}


def gen_settings() -> str:
    from axon.config import _JSON_KEY_MAP, Settings
    json_names: dict[str, set[str]] = {}
    for k, v in _JSON_KEY_MAP.items():
        json_names.setdefault(v, set()).add(k)
    for k, extra in _EXTRA_JSON_KEYS.items():
        json_names.setdefault(k, set()).update(extra)
    rows = ["| Setting (`config.toml`) | Env var | `settings.json` key | Default |", "|---|---|---|---|"]
    for name, field in Settings.model_fields.items():
        if name == "workspace":
            default = "current directory"
        elif name == "api_key":
            default = "none"
        elif name in ("permissions", "hooks"):
            default = "empty"
        else:
            default = _fmt_default(field.get_default(call_default_factory=True))
        names = json_names.get(name, set())
        camel = sorted(n for n in names if n != name) or sorted(names)
        jn = ", ".join(f"`{j}`" for j in camel) or "–"
        rows.append(f"| `{name}` | `AXON_{name.upper()}` | {jn} | `{_md(default)}` |")
    return "\n".join(rows)


def gen_cli_help() -> str:
    env = dict(os.environ, PYTHONPATH=str(SRC), COLUMNS="100", NO_COLOR="1")
    out = subprocess.run([sys.executable, "-m", "axon", "--help"], env=env, capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return "```text\n" + out.rstrip() + "\n```"


def gen_counts() -> str:
    from axon.tools.registry import create_default_registry
    from axon.providers.catalog import PROVIDER_PRESETS
    n_cmd = sum(len(c) for _, c in _commands())
    n_tools = len(create_default_registry().visible_tools())
    return f"{n_cmd} slash commands · {n_tools} built-in tools · {len(PROVIDER_PRESETS)} provider presets"


# ─── Writer ────────────────────────────────────────────────────────────────

def _replace_region(text: str, name: str, body: str, path: Path) -> str:
    pat = re.compile(rf"(<!-- BEGIN GENERATED: {re.escape(name)} -->\n)(.*?)(<!-- END GENERATED: {re.escape(name)} -->)", re.S)
    if not pat.search(text):
        raise SystemExit(f"{path.relative_to(ROOT)}: missing generated region '{name}'")
    return pat.sub(lambda m: m.group(1) + body.rstrip("\n") + "\n" + m.group(3), text)


def build() -> dict[Path, str]:
    plain = render_demo(color=False)
    colored = render_demo(color=True)
    demo_block = "```text\n> " + DEMO_PROMPT + "\n\n" + plain.rstrip() + "\n```"
    regions = {
        "demo": demo_block,
        "counts": gen_counts(),
        "commands-overview": gen_commands_overview(),
        "tools": gen_tools(),
        "providers": gen_providers(),
        "commands-reference": gen_commands_reference(),
        "settings": gen_settings(),
        "cli-help": gen_cli_help(),
    }
    files: dict[Path, str] = {}
    for md in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = md.read_text(encoding="utf-8")
        new = text
        for name in re.findall(r"<!-- BEGIN GENERATED: ([\w-]+) -->", text):
            if name not in regions:
                raise SystemExit(f"{md.relative_to(ROOT)}: unknown generated region '{name}'")
            new = _replace_region(new, name, regions[name], md)
        files[md] = new
    files[ROOT / "docs" / "assets" / "demo.svg"] = ansi_to_svg(colored, f"axon  ·  {DEMO_PROMPT}")
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if any generated file is out of date")
    ap.add_argument("--_render-demo", dest="render_demo", action="store_true", help=argparse.SUPPRESS)
    opts = ap.parse_args(argv)
    if opts.render_demo:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.write(_render_demo_here())
        return 0
    stale: list[str] = []
    for path, content in build().items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == content:
            continue
        stale.append(str(path.relative_to(ROOT)))
        if not opts.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if opts.check:
        if stale:
            print("Out of date: " + ", ".join(stale))
            return 1
        print("Docs are up to date.")
        return 0
    print("Updated: " + ", ".join(stale) if stale else "Docs already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
