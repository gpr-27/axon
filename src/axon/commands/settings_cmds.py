"""/settings: one hub for every user-facing option, grouped by purpose.

Each row shows the current value and a one-line explanation. On a terminal, Enter/Space toggles a
switch, ←/→ cycle a choice, Enter types a value, and rows backed by a command (/model, /keys,
/permissions, ...) open that command's own picker. Without a terminal it prints the same table;
``/settings <key> <value>`` changes one option from scripts. Changes apply to the running session
and persist (settings.json where the option has a JSON name, otherwise ~/.axon/config.toml).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from axon.commands.common import clip, err, header, hint, is_tty, ok, update_global_config, update_settings, usage, ws_of
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, ROSE, RST, SLATE, WHITE

CAT = "System"
CLEAR_WORDS = ("-", "none", "off", "clear", "unset", "")


@dataclass
class Item:
    key: str
    label: str
    explain: str
    section: str
    kind: str                                   # toggle | choice | text | command
    choices: Callable[[Any], list[str]] | None = None
    command: str = ""                           # slash command for kind == "command"
    value: Callable[[Any], str] | None = None   # display value (defaults to settings.<key>)
    apply: Callable[[Any, Any], str] | None = None   # custom setter -> confirmation text


# ─── value helpers ─────────────────────────────────────────────────────────

def _get(agent: Any, field: str, default: Any = "") -> Any:
    return getattr(getattr(agent, "settings", None), field, default)


def _show(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if v is None or v == "" or v == []:
        return "—"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _json_key(field: str) -> str | None:
    try:
        from axon.config import _JSON_KEY_MAP
    except ImportError:
        return None
    names = [k for k, v in _JSON_KEY_MAP.items() if v == field]
    camel = [k for k in names if k != field and any(c.isupper() for c in k)]
    return (camel or names or [None])[0]


def persist(agent: Any, field: str, value: Any, json_key: str | None = None) -> str:
    """Apply ``field = value`` to the session and save it; returns where it was saved."""
    from axon.commands.common import update_settings_file
    try:
        from axon.commands.system_cmds import _panel_keys, set_config_value
        if any(k == field for k, *_ in _panel_keys(agent)):
            set_config_value(agent, field, value)
            return "settings.json"
    except ImportError:
        pass
    update_settings(agent, **{field: value})
    jkey = json_key or _json_key(field)
    plain = float(value) if isinstance(value, Decimal) else value
    if jkey and update_settings_file({jkey: plain}) is not None:
        return "settings.json"
    if update_global_config({field: plain} if plain is not None else {}):
        return "config.toml"
    return "this session only"


def _coerce_number(raw: str, kind: type, minimum: float | None = None, maximum: float | None = None) -> Any:
    try:
        v = kind(str(raw).strip().rstrip("s").lstrip("$"))
    except (TypeError, ValueError):
        raise ValueError(f"expected a number, got {raw!r}") from None
    if minimum is not None and v < minimum:
        raise ValueError(f"must be at least {minimum:g}")
    if maximum is not None and v > maximum:
        raise ValueError(f"must be at most {maximum:g}")
    return v


def _optional_usd(field: str, json_key: str) -> Callable[[Any, Any], str]:
    def apply(agent: Any, raw: Any) -> str:
        text = str(raw).strip().lower()
        val = None if text in CLEAR_WORDS else _coerce_number(text, float, 0.0)
        from axon.commands.common import update_settings_file
        update_settings(agent, **{field: val})
        update_settings_file({json_key: val})
        return f"{field} = {_show(val)} (settings.json)"
    return apply


def _int_field(field: str, minimum: int = 0) -> Callable[[Any, Any], str]:
    def apply(agent: Any, raw: Any) -> str:
        val = _coerce_number(raw, int, minimum)
        where = persist(agent, field, val)
        return f"{field} = {val} ({where})"
    return apply


def _model_field(field: str) -> Callable[[Any, Any], str]:
    def apply(agent: Any, raw: Any) -> str:
        text = str(raw).strip()
        val = None if text.lower() in CLEAR_WORDS else text
        where = persist(agent, field, val)
        return f"{field} = {_show(val)} ({where})"
    return apply


def _compact(agent: Any, raw: Any) -> str:
    from axon.commands.system_cmds import parse_compact_threshold
    try:
        window = int(agent.context.get_effective_budget(agent.settings.model))
    except Exception:
        window = 200_000
    pct = parse_compact_threshold(str(raw), window)
    where = persist(agent, "compact_at", pct)
    return f"auto-compact at {int(pct * 100)}% ({where})"


def _ceiling(agent: Any, raw: Any) -> str:
    text = str(raw).strip().lower()
    val = Decimal("0") if text in CLEAR_WORDS else Decimal(str(_coerce_number(text, float, 0.0)))
    update_settings(agent, session_cost_ceiling=val)
    update_global_config({"session_cost_ceiling": float(val)})
    return f"session cost ceiling = {'off' if val == 0 else f'${val}'} (config.toml)"


def _notify(agent: Any, raw: Any) -> str:
    from axon.commands.common import update_settings_file
    val = str(raw).strip().lower()
    if val not in ("important", "all", "off"):
        raise ValueError("expected important, all or off")
    update_settings(agent, notifications=val)
    update_settings_file({"notifications": val})
    try:
        from axon.ui.notify import configure
        configure(agent.settings)
    except Exception:
        pass
    return f"notifications = {val} (settings.json)"


def _budget_action(agent: Any, raw: Any) -> str:
    from axon.commands.common import update_settings_file
    val = str(raw).strip().lower()
    if val not in ("warn", "stop", "downgrade"):
        raise ValueError("expected warn, stop or downgrade")
    update_settings(agent, budget_action=val)
    update_settings_file({"budget.action": val})
    return f"budget action = {val} (settings.json)"


def _provider_name(agent: Any) -> str:
    try:
        from axon.commands.keys_cmds import _active_preset
        p = _active_preset(agent)
        return p.name if p is not None else "custom endpoint"
    except Exception:
        return "—"


def _key_value(agent: Any) -> str:
    try:
        from axon.commands.keys_cmds import _active_preset
        from axon.providers import keystore as ks
        p = _active_preset(agent)
        if p is None:
            return "—"
        if not p.requires_key:
            return "not needed"
        st = ks.preset_status(p, ws_of(agent))
        if not st.is_set:
            return f"{st.var} not set"
        return f"{st.var} {st.masked} · {ks.check_state(st.var, st.value)}"
    except Exception:
        return "—"


def _rules_value(agent: Any) -> str:
    try:
        perms = agent.settings.permissions
        return f"{len(perms.allow)} allow · {len(perms.ask)} ask · {len(perms.deny)} deny"
    except Exception:
        return "—"


def _choices_from(name: str) -> Callable[[Any], list[str]]:
    def get(agent: Any) -> list[str]:
        try:
            from axon.commands.system_cmds import _panel_keys
            for k, _label, choices, _j in _panel_keys(agent):
                if k == name and isinstance(choices, list):
                    return list(choices)
        except Exception:
            pass
        return []
    return get


def _const(values: list[str]) -> Callable[[Any], list[str]]:
    return lambda _agent: list(values)


# ─── catalogue ─────────────────────────────────────────────────────────────

SECTIONS = ("Model & provider", "Permissions", "Display", "Context & cost", "Data", "Shell")


def items() -> list[Item]:
    M, P, D, C, DA, S = SECTIONS
    return [
        Item("model", "Model", "the model that answers (endpoint follows its provider)", M, "command", command="/model"),
        Item("provider", "Provider", "connect or switch provider (key + endpoint in one step)", M, "command",
             command="/provider", value=_provider_name),
        Item("endpoint", "Endpoint", "base URL / proxy redirection, custom OpenAI-compatible endpoints", M, "command",
             command="/endpoint", value=lambda a: str(_get(a, "base_url"))),
        Item("keys", "API keys", "add, replace, test or remove keys; choose user or project .env", M, "command",
             command="/keys", value=_key_value),
        Item("effort", "Reasoning effort", "reflex (fast) … quantum (deepest)", M, "choice", choices=_choices_from("effort")),
        Item("thinking", "Show thinking", "stream the model's reasoning while it works", M, "toggle"),
        Item("fallback_model", "Fallback model", "tried when the main model keeps failing (comma list; '-' clears)", M,
             "text", apply=_model_field("fallback_model")),
        Item("small_model", "Small model", "cheap model for compaction, /fast and side tasks ('-' = main model)", M,
             "text", apply=_model_field("small_model")),
        Item("mode", "Permission mode", "default asks · acceptEdits · plan (read-only) · bypass", P, "choice",
             choices=_choices_from("mode")),
        Item("permissions", "Allow / ask / deny rules", "per-tool rules and extra working directories", P, "command",
             command="/permissions", value=_rules_value),
        Item("trace_level", "Trace level", "how much of each turn is shown", D, "choice", choices=_choices_from("trace_level")),
        Item("theme", "Theme", "colours for dark / light / high-contrast terminals", D, "choice", choices=_choices_from("theme")),
        Item("output_style", "Output style", "how answers are written (explanatory, concise, …)", D, "choice",
             choices=_choices_from("output_style")),
        Item("vim_mode", "Vim mode", "vim key bindings in the prompt editor", D, "toggle"),
        Item("tips", "Usage tips", "short tips while the model works", D, "toggle"),
        Item("tips_llm", "Model-written tips", "tips tailored to your session (costs a few tokens)", D, "toggle"),
        Item("suggestions", "Prompt suggestions", "suggested next prompts after answers", D, "toggle"),
        Item("notifications", "Desktop notifications", "important · all · off", D, "choice",
             choices=_const(["important", "all", "off"]), apply=_notify),
        Item("screen_log", "Screen log", "keep a plain-text copy of everything shown (per project)", D, "toggle"),
        Item("statusline", "Status line", "custom command shown under the prompt", D, "command", command="/statusline",
             value=lambda a: "custom" if _get(a, "status_line", None) else "default"),
        Item("context_limit", "Context limit", "how much of the model's window to use: 128k · 256k · 1M · max", C,
             "command", command="/context limit",
             value=lambda a: (f"{int(_get(a, 'context_limit', 0)) // 1000}k" if int(_get(a, 'context_limit', 0) or 0) < 1_000_000
                              else f"{int(_get(a, 'context_limit', 0)) / 1_000_000:g}M") if int(_get(a, 'context_limit', 0) or 0) else "model max"),
        Item("compact_at", "Auto-compact at", "compress history at this share of the context (e.g. 85 or 500k)", C,
             "text", apply=_compact, value=lambda a: f"{int(float(_get(a, 'compact_at', 0.85)) * 100)}%"),
        Item("session_cost_ceiling", "Session cost ceiling", "hard stop for one session in USD ('off' disables)", C,
             "text", apply=_ceiling, value=lambda a: "off" if not _get(a, "session_cost_ceiling", 0) else f"${_get(a, 'session_cost_ceiling')}"),
        Item("budget_turn_usd", "Budget per turn", "USD; checked before each request ('-' clears)", C, "text",
             apply=_optional_usd("budget_turn_usd", "budget.turn_usd")),
        Item("budget_session_usd", "Budget per session", "USD; checked before each request ('-' clears)", C, "text",
             apply=_optional_usd("budget_session_usd", "budget.session_usd")),
        Item("budget_action", "When a budget is hit", "warn · stop · downgrade to a cheaper model", C, "choice",
             choices=_const(["warn", "stop", "downgrade"]), apply=_budget_action),
        Item("max_history_turns", "History window", "turns kept in the prompt (0 = unlimited)", C, "text",
             apply=_int_field("max_history_turns")),
        Item("auto_memory", "Auto memory", "let Axon save learnings about this project", C, "toggle"),
        Item("project", "Where things are stored", "this project's data folder, sizes and paths", DA, "command",
             command="/project", value=lambda a: "/project"),
        Item("activity", "Activity journal", "everything you did, searchable", DA, "command", command="/activity",
             value=lambda a: "/activity"),
        Item("cleanup_period_days", "Keep sessions for", "days before old sessions are pruned (0 = forever)", DA, "text",
             apply=_int_field("cleanup_period_days")),
        Item("purge", "Delete stored data…", "this project's sessions, caches, or everything (asks first)", DA,
             "command", command="", value=lambda a: ""),
        Item("respond_to_bash_commands", "Reply to !commands", "the model answers after `!cmd` output", S, "toggle"),
        Item("bash_timeout_s", "Bash timeout", "seconds before a shell command is stopped", S, "text",
             apply=_int_field("bash_timeout_s", 1), value=lambda a: f"{_get(a, 'bash_timeout_s', 180)}s"),
    ]


def _value(agent: Any, it: Item) -> str:
    try:
        return it.value(agent) if it.value else _show(_get(agent, it.key, None))
    except Exception:
        return "—"


def set_item(agent: Any, it: Item, raw: Any) -> str:
    """Apply a value to a toggle / choice / text item. Raises ValueError when invalid."""
    if it.kind == "toggle":
        if isinstance(raw, bool):
            val = raw
        else:
            low = str(raw).strip().lower()
            if low in ("on", "true", "1", "yes", "enable", "enabled"):
                val = True
            elif low in ("off", "false", "0", "no", "disable", "disabled"):
                val = False
            elif low in ("toggle", ""):
                val = not bool(_get(agent, it.key, False))
            else:
                raise ValueError("expected on or off")
        if it.apply:
            return it.apply(agent, val)
        where = persist(agent, it.key, val)
        return f"{it.label}: {'on' if val else 'off'} ({where})"
    if it.kind == "choice":
        opts = it.choices(agent) if it.choices else []
        low = {o.lower(): o for o in opts}
        val = low.get(str(raw).strip().lower())
        if val is None:
            raise ValueError(f"expected one of {', '.join(opts)}")
        if it.apply:
            return it.apply(agent, val)
        where = persist(agent, it.key, val)
        return f"{it.label}: {val} ({where})"
    if it.kind == "text":
        if it.apply is None:
            raise ValueError("not editable")
        return it.apply(agent, raw)
    raise ValueError(f"{it.label} opens {it.command or 'its own menu'}")


def _run_command(agent: Any, it: Item) -> None:
    if it.key == "purge":
        _purge_flow(agent)
        return
    from axon.commands.registry import dispatch_command
    if dispatch_command(it.command, agent) is None:
        err(f"{it.command} is not available in this build.")


def _purge_flow(agent: Any) -> None:
    from axon.ui.picker import choose
    from axon.commands.workspace_cmds import purge
    scope = choose([("This project's sessions, traces and checkpoints", "project"), ("Caches only", "caches"),
                    ("Everything Axon stored (keeps settings, keys and memories)", "all"), ("Cancel", None)],
                   title="Delete stored data")
    if not scope:
        return
    removed = purge(scope, ws_of(agent))
    ok(f"Deleted {len(removed)} location{'s' if len(removed) != 1 else ''}") if removed else hint("    Nothing deleted.")


# ─── rendering ─────────────────────────────────────────────────────────────

def _change_hint(it: Item) -> str:
    if it.kind == "command":
        return it.command or "/settings (menu)"
    if it.kind == "toggle":
        return f"/settings {it.key} on|off"
    if it.kind == "choice":
        return f"/settings {it.key} <choice>"
    return f"/settings {it.key} <value>"


def print_settings(agent: Any, section: str = "") -> int:
    rows = items()
    if section:
        want = section.lower()
        rows = [it for it in rows if it.section.lower().startswith(want) or want in it.section.lower()]
        if not rows:
            err(f"No section '{section}'. Sections: {', '.join(SECTIONS)}")
            return 2
    header("Settings", "every option, grouped · /settings <key> <value> changes one")
    last = ""
    for it in rows:
        if it.section != last:
            print(f"\n  {GOLD}{BOLD}{it.section}{RST}")
            last = it.section
        val = _value(agent, it)
        print(f"    {WHITE}{it.label:<24}{RST} {CYAN}{clip(val, 34):<34}{RST} {SLATE}{it.explain}{RST}")
        print(f"    {'':<24} {DARK_SLATE}{it.key:<34} {_change_hint(it)}{RST}")
    print()
    hint("    Also: /config (quick panel) · /keys · /endpoint · /permissions · /theme · /output-style")
    print()
    return 0


def _hub(agent: Any) -> CommandResult:
    from axon.ui.picker import SelectItem, Selector
    catalogue = items()
    index = 0

    def rows() -> list[SelectItem]:
        out = []
        for it in catalogue:
            val = _value(agent, it)
            kind = {"toggle": "↵ toggle", "choice": "←→ cycle", "text": "↵ edit", "command": "↵ open"}[it.kind]
            out.append(SelectItem(label=f"{it.label:<24} {clip(val, 30)}", value=it, hint=f"{it.explain} · {kind}",
                                  group=it.section, search=f"{it.key} {it.command}"))
        return out

    while True:
        def on_key(sel: Any, key: str) -> bool:
            cur = sel.current()
            if cur is None or key not in ("enter", " ", "right", "left"):
                return False
            it: Item = cur.value
            if it.kind == "command":
                if key == "enter":
                    sel.finish(cur)
                return True
            try:
                if it.kind == "toggle":
                    msg = set_item(agent, it, not bool(_get(agent, it.key, False)))
                elif it.kind == "choice":
                    opts = it.choices(agent) if it.choices else []
                    if not opts:
                        return True
                    cur_v = str(_get(agent, it.key, ""))
                    i = opts.index(cur_v) if cur_v in opts else -1
                    msg = set_item(agent, it, opts[(i + (-1 if key == "left" else 1)) % len(opts)])
                else:
                    if key != "enter":
                        return True
                    text = sel.prompt_line(f"{it.label}: ", default="", allow_empty=False)
                    if text is None:
                        return True
                    msg = set_item(agent, it, text)
                sel.flash(f"✓ {msg}")
            except (ValueError, ArithmeticError) as e:
                sel.flash(f"✗ {e}", ROSE)
            sel.set_items(rows())
            return True

        picked = Selector(rows(), title="Settings", subtitle="↵/space change · ←→ cycle · type to filter · esc close",
                          index=index, filterable=True, max_visible=18, on_key=on_key,
                          placeholder="type to find a setting",
                          footer="changes apply now and are saved").run()
        if not isinstance(picked, SelectItem):
            return CommandResult(handled=True)
        index = catalogue.index(picked.value) if picked.value in catalogue else 0
        _run_command(agent, picked.value)


USAGE = ("/settings                       every option, grouped (menu on a terminal)\n"
         "/settings <section>             one section: model, permissions, display, context, data, shell\n"
         "/settings <key> <value>         change one option (e.g. /settings tips off)")


@command("settings", category=CAT, description="All settings in one place: model, keys, endpoint, permissions, display, cost, data",
         aliases=("preferences", "prefs", "options"), usage=USAGE)
def handle_settings(agent: Any, arg: str) -> CommandResult:
    words = arg.split(maxsplit=1)
    if not words:
        if is_tty():
            return _hub(agent)
        print_settings(agent)
        return CommandResult(handled=True)
    first = words[0].lower()
    if first in ("help", "--help", "-h"):
        usage(USAGE)
        return CommandResult(handled=True)
    if "=" in first and len(words) == 1:
        first, _, val = words[0].partition("=")
        words = [first, val]
        first = first.lower()
    by_key = {it.key: it for it in items()}
    it = by_key.get(first) or next((i for i in by_key.values() if i.label.lower() == arg.strip().lower()), None)
    if it is None:
        code = print_settings(agent, arg.strip())
        return CommandResult(handled=True, success=code == 0)
    if it.kind == "command":
        if it.key == "purge":
            if not is_tty():
                hint("    Without a terminal use: axon purge [project|caches|all] --yes")
            else:
                _purge_flow(agent)  # it has no slash command of its own
            return CommandResult(handled=True)
        from axon.commands.registry import dispatch_command
        rest = words[1] if len(words) > 1 else ""
        dispatch_command(f"{it.command} {rest}".strip(), agent)
        return CommandResult(handled=True)
    if len(words) < 2:
        print(f"\n    {WHITE}{it.label}{RST}  {CYAN}{_value(agent, it)}{RST}  {SLATE}{it.explain}{RST}")
        if it.kind == "choice" and it.choices:
            print(f"    {SLATE}choices: {', '.join(it.choices(agent))}{RST}")
        print(f"    {DARK_SLATE}{_change_hint(it)}{RST}\n")
        return CommandResult(handled=True)
    try:
        msg = set_item(agent, it, words[1])
    except (ValueError, ArithmeticError) as e:
        err(f"{it.label}: {e}")
        return CommandResult(handled=True, success=False)
    ok(msg)
    return CommandResult(handled=True)


__all__ = ["Item", "SECTIONS", "handle_settings", "items", "persist", "print_settings", "set_item"]
