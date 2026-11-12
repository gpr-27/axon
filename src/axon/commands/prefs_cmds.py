"""Preference commands: /output-style /statusline /theme /vim /suggestions /color /fast /goal /terminal-setup /release-notes.

User-level preferences are saved to ``~/.axon/settings.json`` with the Claude-Code key names
(``outputStyle``, ``statusLine``, ``theme``, ``editorMode``, ``promptColor``) via a load-modify-dump
that never touches unrelated keys.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from axon.commands.common import (
    confirm, err, header, hint, info, is_tty, kv, ok, section, update_settings, update_settings_file, usage, warn,
    ws_of,
    typed_hint,
    typed_only,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, RST, SLATE, TEAL, WHITE

CAT_SYS = "System"
CAT_MODEL = "Model & Modes"


def _done() -> CommandResult:
    return CommandResult(handled=True)


def _saved_note(path: Path | None) -> str:
    return f"{SLATE}(saved to {path}){RST}" if path else f"{GOLD}(could not save settings.json){RST}"


def _choose(title: str, options: list[tuple[str, str]], current: str) -> str | None:
    """Pick one of (value, description) with the shared Selector; None on cancel / non-TTY."""
    if not is_tty():
        return None
    try:
        from axon.ui.picker import SelectItem, Selector
    except Exception:
        return None
    items = [SelectItem(label=v, value=v, hint=d, badge=(f"{MINT}●{RST}" if v == current else " ")) for v, d in options]
    idx = next((i for i, (v, _) in enumerate(options) if v == current), 0)
    chosen = Selector(items, title=title, index=idx).run()
    return chosen.value if chosen is not None and hasattr(chosen, "value") else None


# ─── /output-style ─────────────────────────────────────────────────────────

OUTPUT_STYLE_USAGE = """/output-style                  list styles (picker on a terminal)
/output-style <name>           switch style (saved to ~/.axon/settings.json)
/output-style new <name> [description]   create ~/.axon/output-styles/<name>.md (--project for .axon/)"""


@command("output-style", category=CAT_SYS, description="Choose how answers are written (default, explanatory, learning, concise, custom)",
         aliases=("output-styles", "style"), usage=OUTPUT_STYLE_USAGE)
def handle_output_style(agent: Any, arg: str) -> CommandResult:
    from axon.commands.output_styles import active_style_name, create_style, get_style, list_styles
    ws = ws_of(agent)
    words = arg.split()
    current = active_style_name(agent.settings)
    if words and words[0].lower() in ("new", "create", "add"):
        project = "--project" in words
        rest = [w for w in words[1:] if w != "--project"]
        if not rest:
            usage("/output-style new <name> [description]")
            return _done()
        name, desc = rest[0], " ".join(rest[1:])
        try:
            path = create_style(name, desc or f"Custom style {name}", "", ws, scope="project" if project else "user")
        except FileExistsError as e:
            err(f"{e} already exists.")
            return _done()
        except ValueError as e:
            err(str(e))
            return _done()
        ok(f"Created output style {BOLD}{name}{RST} at {WHITE}{path}{RST}")
        if is_tty() and confirm("Open it in your editor now?", default=True):
            from axon.commands.common import open_in_editor
            open_in_editor(path)
        typed_hint(f"    Activate it with /output-style {path.stem}")
        print()
        return _done()

    styles = list_styles(ws)
    target = words[0] if words else None
    if target is None:
        target = _choose("Output style", [(s.name, s.description[:60]) for s in styles], current)
        if target is None:
            header("Output styles", f"active: {current}")
            for s in styles:
                mark = f"{MINT}●{RST}" if s.name == current else f"{DARK_SLATE}○{RST}"
                print(f"    {mark} {WHITE}{s.name:<14}{RST} {SLATE}{s.description[:64]}{RST} {DARK_SLATE}{s.source}{RST}")
            print()
            typed_hint("    /output-style <name> · /output-style new <name>")
            print()
            return _done()
    style = get_style(target, ws)
    if style is None:
        err(f"No output style '{target}'. Available: {', '.join(s.name for s in styles)}")
        return _done()
    update_settings(agent, output_style=style.name)
    path = update_settings_file({"outputStyle": style.name})
    ok(f"Output style {BOLD}{style.name}{RST} {_saved_note(path)}")
    hint("    Takes effect from the next message.")
    print()
    return _done()


# ─── /statusline ───────────────────────────────────────────────────────────

_STATUSLINE_SCRIPT = r'''#!/usr/bin/env python3
"""Axon status line: reads the session JSON on stdin and prints one line."""
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
model = (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id") or "?"
ws = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or os.getcwd()
cost = (d.get("cost") or {}).get("total_cost_usd")
ctx = d.get("context_window") or {}
pct = ctx.get("used_percentage")
parts = [model, os.path.basename(str(ws).rstrip("/\\")) or str(ws)]
if pct is not None:
    parts.append(f"ctx {float(pct):.0f}%")
if cost is not None:
    parts.append(f"${float(cost):.4f}")
print(" | ".join(parts))
'''


STATUSLINE_USAGE = """/statusline                 show the current status line setting
/statusline <command>       run <command> for the status line (session JSON on stdin)
/statusline default         install a ready-made script (~/.axon/statusline.py)
/statusline off             remove the custom status line"""


def default_statusline_command() -> tuple[Path, str]:
    """Write the bundled status-line script under the Axon home; returns (path, command)."""
    from axon.storage import user_file
    from axon.session.store import atomic_write_text
    path = user_file("statusline.py")
    atomic_write_text(path, _STATUSLINE_SCRIPT)
    exe = sys.executable or "python"
    q = (lambda s: f'"{s}"') if os.name == "nt" else (lambda s: "'" + s.replace("'", "'\\''") + "'")
    return path, f"{q(exe)} {q(str(path))}"


@command("statusline", category=CAT_SYS, description="Custom status line command under the prompt",
         usage=STATUSLINE_USAGE)
def handle_statusline(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    current = getattr(agent.settings, "status_line", None)
    if not a:
        header("Status line")
        if isinstance(current, dict) and current.get("command"):
            kv("Command", current.get("command"))
            kv("Padding", current.get("padding", 0))
        else:
            kv("Command", "none (built-in status bar)")
        print()
        typed_hint("    " + STATUSLINE_USAGE.replace("\n", "\n    "))
        print()
        return _done()
    if a.lower() in ("off", "remove", "clear", "none", "reset"):
        update_settings(agent, status_line=None)
        path = update_settings_file({"statusLine": None})
        ok(f"Custom status line removed {_saved_note(path)}")
        return _done()
    if a.lower() in ("default", "preset", "builtin"):
        try:
            script, cmd = default_statusline_command()
        except OSError as e:
            err(f"Could not write the status line script: {e}")
            return _done()
        info(f"Wrote {script}")
    else:
        cmd = a
    value = {"type": "command", "command": cmd, "padding": 0}
    update_settings(agent, status_line=value)
    path = update_settings_file({"statusLine": value})
    ok(f"Status line command set to {WHITE}{cmd}{RST} {_saved_note(path)}")
    return _done()


# ─── /theme ────────────────────────────────────────────────────────────────

THEMES: list[tuple[str, str]] = [
    ("auto", "Axon theme, following the terminal background"),
    ("axon", "Axon theme for dark terminals"),
    ("axon-light", "Axon theme for light terminals"),
    ("dark", "classic palette for dark terminals"),
    ("light", "classic palette for light terminals"),
    ("high-contrast", "maximum contrast, colour-blind friendly"),
    ("mono", "no colours"),
]


def apply_theme(name: str) -> bool:
    """Ask the UI layer to switch palettes (guarded: older UIs only persist the choice)."""
    try:
        from axon.ui.theme import set_theme  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return False
    try:
        set_theme(name)
        return True
    except Exception:
        return False


@command("theme", category=CAT_SYS, description="Colour theme: auto | axon | axon-light | dark | light | high-contrast | mono",
         usage="/theme [auto|axon|axon-light|dark|light|high-contrast|mono]")
def handle_theme(agent: Any, arg: str) -> CommandResult:
    names = [n for n, _ in THEMES]
    current = str(getattr(agent.settings, "theme", "auto") or "auto")
    name = arg.strip().lower() or _choose("Theme", THEMES, current)
    if not name:
        header("Theme", f"current: {current}")
        for n, d in THEMES:
            print(f"    {MINT + '●' if n == current else DARK_SLATE + '○'}{RST} {WHITE}{n:<14}{RST} {SLATE}{d}{RST}")
        print()
        return _done()
    if name == "colorblind":
        name = "high-contrast"
    if name not in names:
        err(f"Unknown theme '{name}'. Choose: {', '.join(names)}")
        return _done()
    live = apply_theme(name)
    update_settings(agent, theme=name)
    path = update_settings_file({"theme": name})
    ok(f"Theme {BOLD}{name}{RST} {_saved_note(path)}" + ("" if live else f" {SLATE}· applies on next start{RST}"))
    return _done()


# ─── /vim ──────────────────────────────────────────────────────────────────

def apply_vim_mode(on: bool) -> bool:
    try:
        from axon.ui.input import set_vim_mode  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return False
    try:
        set_vim_mode(on)
        return True
    except Exception:
        return False


@command("vim", category=CAT_SYS, description="Toggle vim key bindings in the prompt editor", usage="/vim [on|off]")
def handle_vim(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    cur = bool(getattr(agent.settings, "vim_mode", False))
    if a in ("on", "true", "1", "vim"):
        new = True
    elif a in ("off", "false", "0", "normal"):
        new = False
    elif not a or a == "toggle":
        new = not cur
    else:
        usage("/vim [on|off]")
        return _done()
    live = apply_vim_mode(new)
    update_settings(agent, vim_mode=new)
    path = update_settings_file({"editorMode": "vim" if new else "normal"})
    ok(f"Editor mode {BOLD}{'vim' if new else 'normal'}{RST} {_saved_note(path)}"
       + ("" if live else f" {SLATE}· applies on next start{RST}"))
    if new:
        hint("    Esc → normal mode (h j k l w b 0 $ x dd i a A I), i/a → insert mode")
        print()
    return _done()


# ─── /suggestions ──────────────────────────────────────────────────────────

@command("suggestions", category=CAT_SYS, usage="/suggestions [on|off]",
         description="Show a guess of your next message as ghost text in the empty prompt (Tab accepts it)")
def handle_suggestions(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    cur = bool(getattr(agent.settings, "prompt_suggestions", True))
    if a in ("on", "true", "1", "enable"):
        new = True
    elif a in ("off", "false", "0", "disable"):
        new = False
    elif not a or a == "toggle":
        new = not cur
    else:
        usage("/suggestions [on|off]")
        return _done()
    update_settings(agent, prompt_suggestions=new)
    if not new and hasattr(agent, "suggestions"):
        agent.suggestions.clear()            # drop the one on screen or on its way
    path = update_settings_file({"promptSuggestions": new})
    ok(f"Next-prompt suggestions {BOLD}{'on' if new else 'off'}{RST} {_saved_note(path)}")
    if new:
        hint("    After an answer the likely next message shows as dim text in the empty prompt: Tab accepts it, any key dismisses it.")
        hint("    It costs a few hundred input tokens and ~15 output tokens per turn on the small model.")
        print()
    return _done()


# ─── /color ────────────────────────────────────────────────────────────────

def prompt_colors() -> dict[str, str]:
    """name -> SGR code for the active theme (the light theme gets darker ones: 221 and 215 are faint on white)."""
    from axon.ui.theme import prompt_color_sgr
    return {"default": "", **{n: prompt_color_sgr(n) for n in PROMPT_COLOR_NAMES}}


PROMPT_COLOR_NAMES = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink", "gray")


@command("color", category=CAT_SYS, description="Colour of the prompt bar for this session",
         aliases=("colour",), usage="/color [default|red|orange|yellow|green|cyan|blue|purple|pink|gray]")
def handle_color(agent: Any, arg: str) -> CommandResult:
    name = arg.strip().lower().replace("grey", "gray")
    PROMPT_COLORS = prompt_colors()
    current = getattr(agent, "prompt_color", "default") or "default"
    if not name:
        name = _choose("Prompt colour", [(n, "") for n in PROMPT_COLORS], current) or ""
    if not name:
        swatches = "  ".join(f"{code}■{RST} {n}" if code else f"□ {n}" for n, code in PROMPT_COLORS.items())
        header("Prompt colour", f"current: {current}")
        print(f"    {swatches}\n")
        return _done()
    if name not in PROMPT_COLORS:
        err(f"Unknown colour '{name}'. Choose: {', '.join(PROMPT_COLORS)}")
        return _done()
    agent.prompt_color = name
    try:
        from axon.ui.input import set_prompt_color  # type: ignore[attr-defined]
        set_prompt_color(name)
    except Exception:
        pass
    path = update_settings_file({"promptColor": None if name == "default" else name})
    ok(f"Prompt colour {PROMPT_COLORS[name]}{BOLD}{name}{RST} {_saved_note(path)}")
    return _done()


# ─── /fast ─────────────────────────────────────────────────────────────────

def _connection_of(settings: Any) -> dict[str, Any]:
    """The settings that decide which endpoint a model is sent to."""
    return {"provider": settings.provider, "base_url": settings.base_url, "api_key": settings.api_key}


def _restore_connection(agent: Any, model: str) -> None:
    """Go back to *model* on exactly the provider, URL and key that were active before /fast."""
    from axon.commands.common import update_settings
    from axon.commands.model_cmds import switch_model
    from axon.providers.registry import provider_for

    saved = getattr(agent, "_fast_prev_connection", None)
    if not saved:
        switch_model(agent, model, persist=False)
        return
    update_settings(agent, model=model, **saved)
    agent.provider = provider_for(model, agent.settings)
    try:
        agent.session.model = model
    except Exception:
        pass


@command("fast", category=CAT_MODEL, description="Fast mode: the API's fast speed where the model has it, else the provider's fast model",
         usage="/fast [on|off|status]\n/fast model <id>   the model /fast switches to on providers without native fast mode\n"
               "/fast on model     use the fast model even where API fast mode exists")
def handle_fast(agent: Any, arg: str) -> CommandResult:
    from axon.agent.speed import run_fast
    run_fast(agent, arg)
    return _done()


# ─── /focus ────────────────────────────────────────────────────────────────

@command("focus", category=CAT_SYS, description="Focus view: only prompts, one-line tool summaries and answers",
         usage="/focus [on|off]   (switches the trace level to compact and back)")
def handle_focus(agent: Any, arg: str) -> CommandResult:
    import contextlib
    import io
    from axon.commands.model_cmds import current_trace_level, handle_trace
    a = arg.strip().lower()
    on_now = getattr(agent, "_focus_prev_trace", None) is not None
    want = True if a == "on" else False if a == "off" else not on_now
    if want == on_now:
        info(f"Focus view is already {'on' if on_now else 'off'}.")
        return _done()
    with contextlib.redirect_stdout(io.StringIO()):
        if want:
            agent._focus_prev_trace = current_trace_level(agent)
            handle_trace(agent, "compact")
        else:
            handle_trace(agent, agent._focus_prev_trace or "normal")
            agent._focus_prev_trace = None
    ok(f"Focus view {'on: compact trace' if want else 'off: trace ' + current_trace_level(agent)}")
    return _done()


# ─── /goal ─────────────────────────────────────────────────────────────────

@command("goal", category=CAT_MODEL, description="Set done-when criteria; a separate grader checks them after each turn and sends back the gaps",
         usage="/goal                  show the goal and its last verdict\n/goal <done-when>      set it (free text or a short checklist)\n"
               "/goal budget N         max grader rounds per turn (default 3, max 10)\n/goal clear            remove it")
def handle_goal(agent: Any, arg: str) -> CommandResult:
    from axon.commands.goal_cmds import handle_goal_command      # the done-when logic lives in goal_cmds / agent.goal
    return handle_goal_command(agent, arg)


def goal_prompt_section(agent_or_settings: Any) -> str | None:
    """System-prompt text for the active session goal (None when unset)."""
    g = getattr(agent_or_settings, "goal", None)
    if not g and hasattr(agent_or_settings, "settings"):
        g = getattr(agent_or_settings.settings, "goal", None)
    if not g:
        return None
    return (f"# Session goal\nThe user set this done-when goal for the session: {g}\n"
            "Keep working toward it. When you finish a turn, an independent grader checks it against the evidence "
            "(files changed, command output); show your evidence, and expect gaps to be sent back to you.")


# ─── /terminal-setup ───────────────────────────────────────────────────────

_VSCODE_BINDING = {"key": "shift+enter", "command": "workbench.action.terminal.sendSequence",
                   "args": {"text": "\u001b\r"}, "when": "terminalFocus"}
_WT_ACTION = {"command": {"action": "sendInput", "input": "\u001b\r"}, "keys": "shift+enter"}


def detect_terminal(env: dict[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    prog = (e.get("TERM_PROGRAM") or "").lower()
    if "vscode" in prog:
        if "cursor" in (e.get("CURSOR_TRACE_ID", "") + e.get("VSCODE_GIT_ASKPASS_MAIN", "")).lower():
            return "cursor"
        return "vscode"
    if e.get("WT_SESSION"):
        return "windows-terminal"
    for key, name in (("iterm", "iterm2"), ("apple_terminal", "apple-terminal"), ("wezterm", "wezterm"),
                      ("ghostty", "ghostty"), ("alacritty", "alacritty"), ("kitty", "kitty")):
        if key in prog:
            return name
    if e.get("KITTY_WINDOW_ID"):
        return "kitty"
    if e.get("ALACRITTY_SOCKET") or e.get("ALACRITTY_LOG"):
        return "alacritty"
    if sys.platform == "win32":
        return "windows-console"
    return "unknown"


def vscode_keybindings_path(app: str = "Code") -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / app / "User" / "keybindings.json"


def install_vscode_binding(path: Path) -> str:
    """Add the Shift+Enter binding to a VS Code keybindings.json. Returns 'added', 'present' or 'unparseable'."""
    from axon.session.store import atomic_write_text
    data: list[Any] = []
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
        except (OSError, ValueError):
            return "unparseable"   # JSONC with comments: never rewrite the user's file
        if not isinstance(data, list):
            return "unparseable"
    if any(isinstance(b, dict) and b.get("key") == "shift+enter" and b.get("command") == _VSCODE_BINDING["command"] for b in data):
        return "present"
    data.append(dict(_VSCODE_BINDING))
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return "added"


def terminal_instructions(term: str) -> list[str]:
    snippets = {
        "iterm2": ["iTerm2 → Settings → Profiles → Keys → Key Mappings → +",
                   "Shortcut: Shift+Return · Action: Send Hex Codes · Value: 0x1b 0x0d"],
        "apple-terminal": ["Terminal → Settings → Profiles → Keyboard → enable \"Use Option as Meta key\".",
                           "Then Option+Enter inserts a newline."],
        "wezterm": ["Add to ~/.wezterm.lua:",
                    "  config.keys = { { key = 'Enter', mods = 'SHIFT', action = wezterm.action.SendString '\\x1b\\r' } }"],
        "ghostty": ["Add to ~/.config/ghostty/config:", "  keybind = shift+enter=text:\\x1b\\r"],
        "alacritty": ["Add to alacritty.toml:", "  [[keyboard.bindings]]", '  key = "Return"', '  mods = "Shift"',
                      '  chars = "\\u001b\\r"'],
        "kitty": ["Add to kitty.conf:", "  map shift+enter send_text all \\x1b\\r"],
        "windows-terminal": ["Windows Terminal → Settings → Open JSON file, then add to \"actions\":",
                             "  " + json.dumps(_WT_ACTION),
                             "(Axon already reads Shift+Enter natively from the Windows console API; use this only if it does not work.)"],
        "windows-console": ["conhost / PowerShell / cmd: Shift+Enter is read natively from the console API. Nothing to set up."],
        "vscode": ["Add to VS Code keybindings.json (Ctrl/Cmd+Shift+P → \"Preferences: Open Keyboard Shortcuts (JSON)\"):",
                   "  " + json.dumps(_VSCODE_BINDING)],
    }
    snippets["cursor"] = snippets["vscode"]
    return snippets.get(term, ["Unknown terminal. Configure Shift+Enter to send ESC + CR (\\x1b\\r)."])


@command("terminal-setup", category=CAT_SYS, description="Set up Shift+Enter for newlines in your terminal",
         usage="/terminal-setup [vscode|cursor|iterm2|windows-terminal|wezterm|ghostty|alacritty|kitty|apple-terminal]")
def handle_terminal_setup(agent: Any, arg: str) -> CommandResult:
    term = arg.strip().lower() or detect_terminal()
    header("Terminal setup", f"detected: {term}")
    print(f"    {SLATE}Alt+Enter and Ctrl+J always insert a newline; Shift+Enter needs terminal support.{RST}\n")
    for line in terminal_instructions(term):
        print(f"    {WHITE}{line}{RST}")
    print()
    if term in ("vscode", "cursor"):
        path = vscode_keybindings_path("Cursor" if term == "cursor" else "Code")
        if is_tty() and confirm(f"Add the Shift+Enter binding to {path}?", default=False):
            try:
                res = install_vscode_binding(path)
            except OSError as e:
                err(f"Could not write {path}: {e}")
                return _done()
            if res == "added":
                ok(f"Added Shift+Enter binding to {path}")
            elif res == "present":
                info("The Shift+Enter binding is already installed.")
            else:
                warn(f"{path} has comments or is not a JSON list; add the snippet above by hand.")
    return _done()


# ─── /release-notes ────────────────────────────────────────────────────────

def changelog_path() -> Path:
    return Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def parse_changelog(text: str) -> list[tuple[str, str]]:
    """[(version, body)] newest first from '## <version>' headings."""
    out: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+\[?v?([0-9][^\]\s]*)\]?", line)
        if m:
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf).strip()))
    return out


def _print_notes(version: str, body: str) -> None:
    section(f"Axon {version}", TEAL)
    for ln in body.splitlines():
        if not ln.strip():
            print()
        elif ln.startswith("- ") or ln.startswith("  "):
            print(f"    {SLATE}{ln}{RST}")
        else:
            print(f"    {GOLD}{BOLD}{ln}{RST}")


@command("release-notes", category=CAT_SYS, description="What's new in this and earlier Axon versions",
         aliases=("changelog", "whats-new"), usage="/release-notes [version|all]")
def handle_release_notes(agent: Any, arg: str) -> CommandResult:
    try:
        entries = parse_changelog(changelog_path().read_text(encoding="utf-8"))
    except OSError:
        entries = []
    if not entries:
        info("No release notes are bundled with this installation.")
        return _done()
    a = arg.strip().lower().lstrip("v")
    if a == "all":
        chosen = entries
    elif a:
        chosen = [e for e in entries if e[0] == a or e[0].startswith(a)]
        if not chosen:
            more = f" … ({len(entries) - 12} older, /release-notes all)" if len(entries) > 12 else ""
            err(f"No release notes for {a}. Recent versions: {', '.join(v for v, _ in entries[:12])}{more}")
            return _done()
    else:
        pick = _choose("Release notes", [(v, (b.splitlines() or [''])[0][:50]) for v, b in entries], entries[0][0]) if len(entries) > 1 else None
        chosen = [e for e in entries if e[0] == pick] or entries[:1]
    for v, body in chosen:
        _print_notes(v, body)
    print()
    return _done()
