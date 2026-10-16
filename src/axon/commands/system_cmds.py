"""System commands: /help /status /doctor /config /env /keybindings /notify /bug /statusbar /recap /autocompact."""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.commands.common import (
    err, estimate_tokens, fmt_tokens, fmt_usd, git, header, hint, info, kv, ok, one_shot, update_global_config, update_settings, usage, ws_of,
)
from axon.commands.registry import CommandResult, command, render_help
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE, term_height, term_width

CAT = "System"


@command("help", category=CAT, description="List commands, or details for one command", usage="/help [command]")
def handle_help(agent: Any, arg: str) -> CommandResult:
    render_help(agent, arg)
    return CommandResult(handled=True)


def _version() -> str:
    try:
        import axon
        return str(getattr(axon, "__version__", "")) or "dev"
    except Exception:
        return "dev"


def _setting_sources(agent: Any) -> list[str]:
    ws = ws_of(agent)
    Path.home()
    cands = [
        ("user config", _axon_home() / "config.toml"),
        ("user settings", _axon_home() / "settings.json"),
        ("project config", ws / ".axon" / "config.toml"),
        ("project settings", ws / ".axon" / "settings.json"),
        ("local settings", ws / ".axon" / "settings.local.json"),
        (".claude settings", ws / ".claude" / "settings.json"),
    ]
    return [label for label, p in cands if p.is_file()]


@command("status", category=CAT, description="Version, model, mode, trace, session, cost, storage")
def handle_status(agent: Any, arg: str) -> CommandResult:
    from axon.commands.model_cmds import current_trace_level
    s = agent.settings
    header("Axon status", f"v{_version()}")
    kv("Model", f"{CYAN}{s.model}{RST} {SLATE}· effort {s.effort} · thinking {'on' if s.thinking else 'off'}{RST}")
    kv("Endpoint", s.base_url)
    kv("Mode", s.mode)
    kv("Trace level", current_trace_level(agent))
    style = getattr(s, "output_style", "default") or "default"
    if style != "default":
        kv("Output style", style)
    if getattr(agent, "fast_mode", False):
        kv("Fast mode", f"{GOLD}on{RST} {SLATE}(/fast off returns to {getattr(agent, '_fast_prev_model', '')}){RST}")
    goal = getattr(agent, "goal", None) or getattr(s, "goal", None)
    if goal:
        kv("Goal", f"{WHITE}{goal}{RST}")
    perms = getattr(agent, "permissions", None)
    dirs = perms.working_dirs() if hasattr(perms, "working_dirs") else []
    if dirs:
        kv("Extra dirs", ", ".join(str(d) for d in dirs))
    loops = [j for j in (getattr(agent, "loops", None) or []) if getattr(j, "active", False)]
    if loops:
        kv("Loops", f"{len(loops)} active (/loop)")
    sess = agent.session
    kv("Session", f"{sess.active_session_id}")
    msgs = len(agent.conversation.messages)
    kv("Context", f"{msgs} messages · ~{fmt_tokens(sum(estimate_tokens(m.get('content', '')) for m in agent.conversation.messages))} tokens")
    try:
        kv("Cost", f"{GOLD}{fmt_usd(agent.ledger.total(), 5)}{RST} {SLATE}({len(agent.ledger.entries)} calls){RST}")
    except Exception:
        pass
    q = getattr(agent, "message_queue", None)
    subs = getattr(agent, "subagents", None)
    todos = getattr(agent, "todos", None)
    extras = []
    if q is not None and len(q):
        extras.append(f"{len(q)} queued")
    if subs is not None and hasattr(subs, "all_tasks") and subs.all_tasks():
        extras.append(f"{len(subs.all_tasks())} subagent task(s)")
    if todos is not None and hasattr(todos, "progress"):
        try:
            done, total, _ = todos.progress()
            if total:
                extras.append(f"todos {done}/{total}")
        except Exception:
            pass
    if extras:
        kv("Activity", " · ".join(extras))
    ws = ws_of(agent)
    code, branch = git(ws, "rev-parse", "--abbrev-ref", "HEAD", timeout=3)
    kv("Workspace", f"{ws}" + (f" {SLATE}(git: {branch.strip()}){RST}" if code == 0 else ""))
    kv("Setting sources", ", ".join(_setting_sources(agent)) or "defaults only")
    try:
        from axon.agent.memory import discover_instruction_files
        files = discover_instruction_files(ws)
        big = [(f.path, estimate_tokens(f.content)) for f in files if estimate_tokens(f.content) > 5000]
        kv("Memory files", f"{len(files)} loaded")
        for p, t in big:
            print(f"    {GOLD}!{RST} {SLATE}{p} is ~{fmt_tokens(t)} tokens - consider trimming (sent every turn){RST}")
    except Exception:
        pass
    try:
        from axon import storage
        rows = storage.storage_summary()
        total = sum(int(r.get("bytes", 0)) for r in rows)
        kv("Storage", f"{storage.axon_home()} · {total / 1_048_576:.1f} MB")
        kv("Sessions dir", sess.session_dir)
    except Exception:
        pass
    print()
    return CommandResult(handled=True)


@command("doctor", category=CAT, description="Diagnose installation, keys, tools and environment",
         usage="/doctor                 installation, keys, tools, terminal\n/doctor prompt-audit    conflicting, duplicated or stale instructions across AXON.md, rules, skills, agents")
def handle_doctor(agent: Any, arg: str) -> CommandResult:
    if arg.strip().lower().startswith(("prompt-audit", "prompts", "audit")):
        from axon.commands.insight_cmds import render_prompt_audit
        render_prompt_audit(agent)
        return CommandResult(handled=True)
    try:
        from axon.tools.doctor import run_diagnostics
        report = run_diagnostics(agent.settings, ws_of(agent))
    except (ImportError, AttributeError):
        tool = agent.registry.get("Doctor")
        from axon.tools.base import ToolContext
        report = tool.run({}, ToolContext(workspace=ws_of(agent), file_state=agent.file_state, todos=agent.todos,
                                          settings=agent.settings, ledger=agent.ledger, agent=agent))
    try:
        from axon.ui.render import render_doctor_box
        print(f"\n{render_doctor_box(report)}\n")
    except Exception:
        print(f"\n{report}\n")
    return CommandResult(handled=True)


_CONFIG_KEYS: dict[str, str] = {
    "model": "active model (/model)",
    "effort": "reasoning effort (/effort)",
    "mode": "permission mode (/mode)",
    "thinking": "stream reasoning (true/false)",
    "max_tokens": "max output tokens per call",
    "max_iterations": "max LLM calls per turn",
    "compact_at": "auto-compact threshold (0-1 of the window)",
    "max_history_turns": "sliding window in turns (0 = unlimited)",
    "parallel_tools": "max parallel read-only tools",
    "bash_timeout_s": "Bash timeout in seconds",
    "tool_output_cap": "max chars of tool output fed back",
    "base_url": "provider endpoint",
    "small_model": "cheap model for compaction, /fast and side tasks",
    "tips_refresh_turns": "min turns between model-written tip refreshes",
    "tips_refresh_minutes": "min minutes between model-written tip refreshes",
}


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        if raw.lower() in ("on", "true", "1", "yes"):
            return True
        if raw.lower() in ("off", "false", "0", "no"):
            return False
        raise ValueError("expected true/false")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


# Interactive /config panel: key -> (label, choices | None for free text | "bool", settings.json key)
def _panel_keys(agent: Any) -> list[tuple[str, str, Any, str]]:
    from axon.commands.model_cmds import TRACE_LEVELS, _EFFORTS, _MODES
    from axon.commands.prefs_cmds import THEMES
    try:
        from axon.commands.output_styles import list_styles
        styles = [st.name for st in list_styles(ws_of(agent))]
    except Exception:
        styles = ["default"]
    return [
        ("model", "Model", None, "model"),
        ("mode", "Permission mode", list(_MODES), "permissions.defaultMode"),
        ("effort", "Reasoning effort", list(_EFFORTS), "effortLevel"),
        ("thinking", "Show thinking", "bool", "alwaysThinkingEnabled"),
        ("trace_level", "Trace level", list(TRACE_LEVELS), "traceLevel"),
        ("output_style", "Output style", styles, "outputStyle"),
        ("theme", "Theme", [n for n, _ in THEMES], "theme"),
        ("vim_mode", "Vim mode", "bool", "editorMode"),
        ("suggestions", "Prompt suggestions", "bool", "promptSuggestionEnabled"),
        ("tips", "Usage tips", "bool", "tips"),
        ("tips_llm", "Model-written tips", "bool", "tipsLlm"),
    ]


def _json_value(key: str, value: Any) -> Any:
    if key == "mode":
        return "bypassPermissions" if value == "bypass" else value
    if key == "vim_mode":
        return "vim" if value else "normal"
    return value


def set_config_value(agent: Any, key: str, raw: Any, persist: bool = True) -> str:
    """Apply one panel setting quietly (all side effects of its command) and save it to ~/.axon/settings.json.
    Returns a short confirmation; raises ValueError for invalid values."""
    import contextlib
    import io
    spec = {k: (label, choices, jkey) for k, label, choices, jkey in _panel_keys(agent)}
    if key not in spec:
        raise ValueError(f"not a panel setting: {key}")
    _label, choices, jkey = spec[key]
    if choices == "bool":
        value: Any = raw if isinstance(raw, bool) else _coerce(False, str(raw))
    else:
        value = str(raw).strip()
        if isinstance(choices, list):
            low = {c.lower(): c for c in choices}
            if value.lower() not in low:
                raise ValueError(f"expected one of {', '.join(choices)}")
            value = low[value.lower()]
        elif not value:
            raise ValueError("value required")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if key == "model":
            from axon.commands.model_cmds import switch_model
            if switch_model(agent, value) is False:
                raise ValueError(f"could not switch to {value}")
        elif key == "mode":
            from axon.commands.model_cmds import set_mode
            set_mode(agent, value)
        elif key == "trace_level":
            from axon.commands.model_cmds import handle_trace
            handle_trace(agent, value)
        elif key == "theme":
            from axon.commands.prefs_cmds import handle_theme
            handle_theme(agent, value)
        elif key == "vim_mode":
            from axon.commands.prefs_cmds import handle_vim
            handle_vim(agent, "on" if value else "off")
        elif key == "output_style":
            from axon.commands.prefs_cmds import handle_output_style
            handle_output_style(agent, value)
        else:
            update_settings(agent, **{key: value})
    if persist:
        from axon.commands.common import update_settings_file
        update_settings_file({jkey: _json_value(key, value)})
    return f"{key} = {value}"


def _config_panel(agent: Any) -> CommandResult:
    from axon.ui.picker import SelectItem, Selector
    keys = _panel_keys(agent)

    def items() -> list[SelectItem]:
        out = []
        for k, label, choices, _ in keys:
            val = getattr(agent.settings, k, "")
            shown = ("on" if val else "off") if choices == "bool" else str(val)
            kind = "toggle" if choices == "bool" else ("type a value" if choices is None else "cycle")
            out.append(SelectItem(label=f"{label:<20} {shown}", value=k, hint=kind))
        return out

    def on_key(sel: Any, key: str) -> bool:
        cur = sel.current()
        if cur is None or key not in ("enter", " ", "right", "left"):
            return False
        k = cur.value
        _, _label, choices, _ = next(x for x in keys if x[0] == k)
        val = getattr(agent.settings, k, "")
        try:
            if choices == "bool":
                msg = set_config_value(agent, k, not bool(val))
            elif choices is None:
                if key != "enter":
                    return True
                text = sel.prompt_line(f"{k}: ", default=str(val))
                if not text:
                    return True
                msg = set_config_value(agent, k, text)
            else:
                i = choices.index(val) if val in choices else -1
                step = -1 if key == "left" else 1
                msg = set_config_value(agent, k, choices[(i + step) % len(choices)])
            sel.flash(f"✓ {msg} (saved to ~/.axon/settings.json)")
        except ValueError as e:
            sel.flash(f"✗ {e}", ROSE)
        sel.set_items(items())
        return True

    Selector(items(), title="Settings", subtitle="Enter/Space change · ←/→ cycle · Esc close",
             on_key=on_key, footer="changes apply now and are saved to ~/.axon/settings.json").run()
    return CommandResult(handled=True)


@command("config", category=CAT, description="Settings panel, or show/change one setting",
         aliases=("set",), usage="/config                          interactive panel (list without a terminal)\n/config <key> <value> [--save]\n/config <key>=<value>")
def handle_config(agent: Any, arg: str) -> CommandResult:
    from axon.commands.common import is_tty
    words = arg.split()
    save = "--save" in words
    words = [w for w in words if w != "--save"]
    if len(words) == 1 and "=" in words[0]:
        k, _, v = words[0].partition("=")
        words = [k] + ([v] if v else [])
    if not words and is_tty():
        return _config_panel(agent)
    panel = {k: label for k, label, _, _ in _panel_keys(agent)}
    if not words:
        header("Configuration", "/config <key> <value> · panel settings are saved to ~/.axon/settings.json")
        shown = set()
        for k, label in panel.items():
            print(f"    {WHITE}{k:<18}{RST} {CYAN}{str(getattr(agent.settings, k, '')):<28}{RST} {DARK_SLATE}{label}{RST}")
            shown.add(k)
        for k, desc in _CONFIG_KEYS.items():
            if k not in shown:
                print(f"    {WHITE}{k:<18}{RST} {CYAN}{str(getattr(agent.settings, k, '')):<28}{RST} {DARK_SLATE}{desc}{RST}")
        print()
        return CommandResult(handled=True)
    key = words[0].lower()
    value = " ".join(words[1:])
    if key in panel and key not in ("model", "mode", "effort"):
        if not value:
            kv(key, getattr(agent.settings, key, ""))
            print()
            return CommandResult(handled=True)
        try:
            msg = set_config_value(agent, key, value)
        except ValueError as e:
            err(f"Invalid value for {key}: {e}")
            return CommandResult(handled=True)
        ok(f"{msg} {SLATE}(saved to ~/.axon/settings.json){RST}")
        return CommandResult(handled=True)
    if key not in _CONFIG_KEYS:
        err(f"Unknown key '{key}'. Keys: {', '.join(list(panel) + [k for k in _CONFIG_KEYS if k not in panel])}")
        return CommandResult(handled=True)
    if not value:
        kv(key, getattr(agent.settings, key, ""))
        print()
        return CommandResult(handled=True)
    if key == "model":
        from axon.commands.model_cmds import handle_model
        return handle_model(agent, value)
    if key == "effort":
        from axon.commands.model_cmds import handle_effort
        return handle_effort(agent, value)
    if key == "mode":
        from axon.commands.model_cmds import handle_mode
        return handle_mode(agent, value)
    try:
        new_val = _coerce(getattr(agent.settings, key), value)
        if key == "compact_at" and not 0.1 <= float(new_val) <= 0.99:
            raise ValueError("must be between 0.1 and 0.99")
        if isinstance(new_val, int) and not isinstance(new_val, bool) and new_val < 0:
            raise ValueError("must be >= 0")
    except ValueError as e:
        err(f"Invalid value for {key}: {e}")
        return CommandResult(handled=True)
    update_settings(agent, **{key: new_val})
    if key == "base_url":
        try:
            from axon.providers.registry import provider_for
            agent.provider = provider_for(agent.settings.model, agent.settings)
        except Exception:
            pass
    saved = ""
    if save:
        saved = " (saved to ~/.axon/config.toml)" if update_global_config({key: new_val}) else " (could not save)"
    ok(f"{key} = {BOLD}{new_val}{RST}{SLATE}{saved}{RST}")
    return CommandResult(handled=True)


def parse_compact_threshold(raw: str, window: int) -> float:
    """'85' / '85%' / '0.85' -> fraction; '500k' / '1.2m' / '150000' tokens -> fraction of ``window``;
    'auto' -> the default. Raises ValueError outside 10-99%."""
    a = raw.strip().lower().rstrip("%")
    if a in ("auto", "default", "reset"):
        return float(type_default("compact_at"))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km])", a)
    if m:
        tokens = float(m.group(1)) * (1000 if m.group(2) == "k" else 1_000_000)
        pct = tokens / window if window else 0.0
    else:
        pct = float(a)
        if pct > 1000:  # a raw token count
            pct = pct / window if window else 0.0
        elif pct > 1:
            pct /= 100
    if not 0.1 <= pct <= 0.99:
        raise ValueError("threshold must be between 10% and 99% of the context window")
    return round(pct, 3)


def type_default(field: str) -> Any:
    from axon.config import Settings
    return Settings.model_fields[field].default


@command("autocompact", category="Context & Cost", description="Set the auto-compact threshold (percent or tokens)",
         usage="/autocompact <percent 10-99>\n/autocompact <tokens>   e.g. 500k (fraction of the model's window)\n/autocompact auto       back to the default")
def handle_autocompact(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    try:
        window = int(agent.context.context_window(agent.settings.model))
    except Exception:
        window = 200_000
    if not a:
        pct_now = float(agent.settings.compact_at)
        kv("Auto-compact at", f"{int(pct_now * 100)}% of the context limit (~{fmt_tokens(int(pct_now * window))} of {fmt_tokens(window)} tokens; /context limit changes the size)")
        print()
        return CommandResult(handled=True)
    try:
        pct = parse_compact_threshold(a, window)
    except ValueError as e:
        usage(f"/autocompact <10-99 | 500k | auto>   ({e})")
        return CommandResult(handled=True)
    update_settings(agent, compact_at=pct)
    ok(f"Auto-compact at {BOLD}{int(pct * 100)}%{RST} of the context limit (~{fmt_tokens(int(pct * window))} of {fmt_tokens(window)} tokens)")
    return CommandResult(handled=True)


@command("env", category=CAT, description="OS, Python, shell, paths and toolchains",
         aliases=("environment",))
def handle_env(agent: Any, arg: str) -> CommandResult:
    header("Environment")
    kv("OS", f"{platform.system()} {platform.release()} ({platform.machine()})")
    kv("Python", f"{sys.version.split()[0]} · {sys.executable}")
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
    try:
        from axon.platform import default_shell  # type: ignore[import-not-found]
        shell = " ".join(default_shell()) or shell
    except Exception:
        pass
    kv("Shell", shell)
    kv("Terminal", f"{term_width()}×{term_height()} · tty {sys.stdin.isatty()} · encoding {getattr(sys.stdout, 'encoding', '?')}")
    kv("Workspace", ws_of(agent))
    try:
        from axon import storage
        kv("Axon home", storage.axon_home())
    except Exception:
        kv("Axon home", _axon_home())
    tools = []
    for t in ("git", "python", "python3", "node", "npm", "pytest", "rg", "docker", "gh", "uv"):
        tools.append(f"{MINT}✓ {t}{RST}" if shutil.which(t) else f"{DARK_SLATE}○ {t}{RST}")
    kv("Toolchains", "  ".join(tools))
    print()
    return CommandResult(handled=True)


@command("keybindings", category=CAT, description="Keyboard shortcuts in effect (remap in ~/.axon/keybindings.json)",
         aliases=("shortcuts", "kb"))
def handle_keybindings(agent: Any, arg: str) -> CommandResult:
    header("Keyboard shortcuts", "defaults merged with ~/.axon/keybindings.json")
    try:
        from axon.ui.keybindings import describe_action, effective_keybindings, keybinding_errors
        bindings = effective_keybindings()
        errors = keybinding_errors()
    except (ImportError, AttributeError):
        from axon.ui.render import render_shortcuts_footer
        print(render_shortcuts_footer())
        print()
        return CommandResult(handled=True)
    for action, keys in sorted(bindings.items()):
        shown = ", ".join(keys) if keys else f"{DARK_SLATE}(unbound){RST}"
        print(f"    {CYAN}{shown:<28}{RST} {WHITE}{action:<22}{RST} {SLATE}{describe_action(action)}{RST}")
    for e in errors:
        print(f"    {ROSE}✗{RST} {SLATE}{e}{RST}")
    print()
    return CommandResult(handled=True)


@command("notify", category=CAT, description="Desktop notifications: when they fire (important/all/off), or send a test",
         usage="/notify [important|all|off|after <seconds>|test [message]]")
def handle_notify(agent: Any, arg: str) -> CommandResult:
    from axon.commands.common import update_settings, update_settings_file
    from axon.ui.notify import NOTIFY_MODES, configure, send_desktop_notification
    words = arg.split()
    sub = words[0].lower() if words else ""
    s = agent.settings
    if sub in NOTIFY_MODES:
        update_settings(agent, notifications=sub)
        update_settings_file({"notifications": sub}, workspace=ws_of(agent))
        configure(agent.settings)
        ok(f"Desktop notifications: {sub}")
        return CommandResult(handled=True)
    if sub == "after" and len(words) > 1:
        try:
            secs = max(0.0, float(words[1].rstrip("s")))
        except ValueError:
            err("Usage: /notify after <seconds>")
            return CommandResult(handled=True)
        update_settings(agent, notify_after_seconds=secs)
        update_settings_file({"notifyAfterSeconds": secs}, workspace=ws_of(agent))
        configure(agent.settings)
        ok(f"Finished turns notify only after running {secs:g}s or longer")
        return CommandResult(handled=True)
    if sub == "test" or (sub and sub not in NOTIFY_MODES):
        msg = " ".join(words[1:] if sub == "test" else words).strip() or "Axon notifications work."
        if send_desktop_notification("Axon", msg):
            ok(f"Notification sent: {msg}")
        else:
            info("Desktop notifications are not available here.")
        return CommandResult(handled=True)
    mode = getattr(s, "notifications", "important")
    after = float(getattr(s, "notify_after_seconds", 30.0))
    header("Desktop notifications", f"mode: {mode}")
    rows = {
        "important": ["Axon needs your permission for a tool (default / acceptEdits / plan mode)",
                      "Axon asked you a question and is waiting", "a turn failed",
                      f"a turn finished after running {after:g}s or longer", "a background turn finished"],
        "all": ["after every turn, plus everything in 'important'"],
        "off": ["never"],
    }.get(mode, [])
    for r in rows:
        print(f"    {SLATE}·{RST} {WHITE}{r}{RST}")
    print(f"\n  {SLATE}Only while you are in another app. /notify important|all|off · /notify after <seconds> · /notify test{RST}\n")
    return CommandResult(handled=True)


@command("statusbar", category=CAT, description="Show the live status bar, or toggle it", usage="/statusbar [toggle]")
def handle_statusbar(agent: Any, arg: str) -> CommandResult:
    from axon.ui.statusbar import StatusBar
    if arg.strip().lower() in ("toggle", "on", "off"):
        ok(f"Status bar {'enabled' if StatusBar.toggle() else 'disabled'}")
    else:
        StatusBar.print_live_status(agent)
    return CommandResult(handled=True)


@command("bug", category=CAT, description="Save a bug report with diagnostics", aliases=("feedback",),
         usage="/bug <what went wrong>")
def handle_bug(agent: Any, arg: str) -> CommandResult:
    desc = arg.strip()
    if not desc:
        usage("/bug <what went wrong>")
        return CommandResult(handled=True)
    s = agent.settings
    lines = [
        "# Axon bug report", "", desc, "",
        "## Environment",
        f"- Axon: {_version()}",
        f"- OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- Python: {sys.version.split()[0]}",
        f"- Terminal: {term_width()}x{term_height()}, encoding {getattr(sys.stdout, 'encoding', '?')}",
        f"- Model: {s.model} · mode {s.mode} · effort {s.effort}",
        f"- Endpoint host: {s.base_url.split('/')[2] if '://' in s.base_url else s.base_url}",
        f"- Session: {agent.session.active_session_id}",
        "",
    ]
    try:
        from axon.commands.agent_cmds import _traces, explain_trace
        traces = _traces(agent)
        if traces:
            lines += ["## Last turn", "", explain_trace(traces[-1]), ""]
            errs = [st for st in traces[-1].steps if st.data.get("is_error") or st.data.get("error")]
            for st in errs[:5]:
                lines.append(f"- {st.kind} {st.data.get('name', '')}: {str(st.data.get('error') or st.data.get('result_preview', ''))[:300]}")
    except Exception:
        pass
    root = _axon_home() / "bug-reports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"bug-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"Bug report saved to {WHITE}{path}{RST}")
    hint("    It contains no API keys or conversation text; attach it to an issue for the project.")
    print()
    return CommandResult(handled=True)


@command("recap", category="Context & Cost", description="Short summary of the session so far (one LLM call)")
def handle_recap(agent: Any, arg: str) -> CommandResult:
    msgs = agent.conversation.messages
    if not msgs:
        info("Nothing to recap yet.")
        return CommandResult(handled=True)
    try:
        from axon.agent.context import _render_for_summary
        transcript = _render_for_summary(msgs, limit_chars=60_000)
    except Exception:
        transcript = "\n".join(f"{m.get('role')}: {str(m.get('content'))[:500]}" for m in msgs[-30:])
    try:
        text = one_shot(agent, "Summarise coding sessions in at most 400 characters: goal, what was done, what is pending.",
                        transcript, max_tokens=300, source="recap")
    except Exception as e:
        err(f"Recap failed: {e}")
        return CommandResult(handled=True)
    header("Recap")
    print(f"    {WHITE}{text[:400]}{RST}\n")
    return CommandResult(handled=True)
