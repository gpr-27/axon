"""
``/plugin``: installed plugins, marketplaces, install / update / uninstall, enable / disable with
dependencies and install scopes, ``userConfig`` options and ``validate``. Text based (no tabbed panel).

  /plugin                                   installed plugins + added marketplaces
  /plugin discover [marketplace]            plugins offered by the added marketplaces
  /plugin install <name>[@marketplace] [--scope user|project|local]
  /plugin install <name> --marketplace <source>   add the marketplace first, then install
  /plugin install <folder | https git URL> [--project]     (a plain plugin, no marketplace)
  /plugin uninstall|enable|disable <name>   enable / disable accept --scope
  /plugin update <name>                     /plugin info <name>      /plugin configure <name> [KEY=VALUE ...]
  /plugin marketplace add|list|update|remove [<source|name>]          (also: /plugin market ...)
  /plugin validate <path>                   check a plugin, marketplace or skills directory
  /plugin create <name> [--global]          /plugin reload
"""
from __future__ import annotations

from axon import platform
from pathlib import Path
from typing import Any, Callable

from axon.commands import marketplace as mk
from axon.commands import plugins as pl
from axon.commands.common import confirm, err, header, hint, info, is_tty, kv, ok, usage, typed_hint, typed_only
from axon.storage import axon_home as _axon_home
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, RED, RST, SLATE, WHITE

SCOPES = ("user", "project", "local")


def _split(arg: str) -> tuple[list[str], dict[str, str], set[str]]:
    """(positional words, --key value options, bare flags)."""
    try:
        words = platform.split_words(arg)
    except ValueError:
        words = arg.split()
    pos: list[str] = []
    opts: dict[str, str] = {}
    flags: set[str] = set()
    i = 0
    value_opts = {"--scope", "--marketplace"}
    while i < len(words):
        w = words[i]
        if w in value_opts and i + 1 < len(words):
            opts[w] = words[i + 1]
            i += 2
            continue
        if w.startswith("--"):
            if "=" in w:
                k, _, v = w.partition("=")
                opts[k] = v
            else:
                flags.add(w)
        else:
            pos.append(w)
        i += 1
    return pos, opts, flags


def _find(installed: list[pl.Plugin], ref: str) -> pl.Plugin | None:
    for p in installed:
        if ref in (p.name, p.id):
            return p
    return None


def _scope_of(p: pl.Plugin) -> str:
    return str(p.record.get("scope") or ("project" if p.scope == "project" else "user"))


def _warnings_for(p: pl.Plugin) -> list[str]:
    out = []
    for key, folder in (("commands", "commands"), ("agents", "agents"), ("outputStyles", "output-styles")):
        if key in p.manifest and (p.path / folder).is_dir():
            out.append(f'Default {folder}/ folder is ignored because the manifest sets "{key}"')
    return out


SHARED_WITH_MARKETPLACE = ("status", "test", "scope", "export", "import")


def _marketplace_control(ws: Path, sub: str, rest: list[str], refresh: Callable[[Any], None], agent: Any) -> None:
    """status / test / scope / export / import run the same code as /marketplace."""
    from axon.marketplace.cli import run_command
    run_command([sub, *rest, "--tab", "plugins"], ws, agent=agent)
    refresh(agent)


def run_plugin_command(agent: Any, arg: str, refresh: Callable[[Any], None], reload_cmd: Callable[[Any, str], Any]) -> None:
    from axon.commands.common import ws_of
    ws = ws_of(agent)
    pos, opts, flags = _split(arg)
    sub = pos[0].lower() if pos else ""
    rest = pos[1:]
    installed = pl.load_plugins(ws)

    try:
        if sub in ("marketplace", "market", "marketplaces"):
            _marketplace(agent, ws, rest, refresh)
        elif sub in ("create", "new"):
            if not rest:
                return usage("/plugin create <name> [--global]")
            root = (_axon_home() / "plugins") if "--global" in flags else (ws / ".axon" / "plugins")
            d = pl.create_plugin(rest[0], root)
            ok(f"Created plugin at {WHITE}{d}{RST}  {SLATE}(try /{d.name}:hello){RST}")
        elif sub in ("install", "add"):
            _install(agent, ws, rest, opts, flags, refresh)
        elif sub in ("uninstall", "remove", "rm"):
            _uninstall(agent, ws, installed, rest, refresh)
        elif sub in ("enable", "disable"):
            _toggle(agent, ws, installed, sub == "enable", rest, opts, refresh)
        elif sub == "update":
            _update(agent, ws, installed, rest, refresh)
        elif sub in ("info", "show", "details"):
            _info(ws, installed, rest)
        elif sub in ("configure", "config", "options"):
            _configure(ws, installed, rest, refresh, agent)
        elif sub == "validate":
            _validate(rest)
        elif sub in ("discover", "browse", "search"):
            _discover(rest)
        elif sub in SHARED_WITH_MARKETPLACE:
            _marketplace_control(ws, sub, rest, refresh, agent)
        elif sub == "reload":
            reload_cmd(agent, "")
        else:
            _list(installed)
    except (mk.MarketplaceError, ValueError, FileExistsError, RuntimeError, OSError) as e:
        err(str(e))


# ─── list / info ────────────────────────────────────────────────────────────

def _list(installed: list[pl.Plugin]) -> None:
    header("Plugins", f"{len(installed)} installed")
    if not installed:
        print(f"    {SLATE}None. /plugin marketplace add <source> then /plugin install <name>@<marketplace>, "
              f"or put a plugin in ~/.axon/plugins/<name>/ or .axon/plugins/<name>/.{RST}")
    for p in installed:
        comps = ", ".join(f"{n} {k}" for k, n in p.components().items()) or "empty"
        state = f"{MINT}●{RST}" if p.enabled else f"{DARK_SLATE}○{RST}"
        print(f"    {state} {WHITE}{p.id:<28}{RST} {SLATE}{p.version:<8} {p.scope:<8} {comps}{RST}")
    markets = mk.list_marketplaces()
    if markets:
        print(f"\n    {SLATE}Marketplaces: {', '.join(n for n, _ in markets)}  (/plugin discover){RST}")
    typed_hint("\n    install <name>@<marketplace> · uninstall|enable|disable|update|info <name> · marketplace add|list|update|remove · validate <path>")
    print()


def _info(ws: Path, installed: list[pl.Plugin], rest: list[str]) -> None:
    p = _find(installed, rest[0]) if rest else None
    if p is None:
        return err(f"No installed plugin named '{rest[0] if rest else ''}'.")
    header(p.title, f"{p.version} · {p.scope} · {'enabled' if p.enabled else 'disabled'}")
    kv("Id", p.id)
    kv("Path", p.path)
    kv("Description", p.description or "-")
    if p.marketplace:
        kv("Marketplace", p.marketplace)
        kv("Install scope", _scope_of(p))
    for k, n in p.components().items():
        kv(k.capitalize(), n)
    deps = mk._normalize_deps(p.manifest.get("dependencies"), p.marketplace)
    if deps:
        kv("Dependencies", ", ".join(f"{n}@{m}" if m else n for n, m in deps))
    schema = pl.plugin_user_config_schema(p)
    if schema:
        vals = pl.plugin_user_config_values(p, ws, include_sensitive=False)
        for key, spec in schema.items():
            shown = "(set)" if spec.get("sensitive") and key in pl.plugin_user_config_values(p, ws) else vals.get(key, "-")
            kv(f"Option {key}", f"{shown}  {SLATE}{spec.get('description', '')}{RST}")
    for w in _warnings_for(p):
        print(f"    {GOLD}!{RST} {SLATE}{w}{RST}")
    print()


def _discover(rest: list[str]) -> None:
    markets = [m for m, _ in mk.list_marketplaces()]
    if rest:
        markets = [m for m in markets if m == rest[0]]
    if not markets:
        return info("No marketplaces added. /plugin marketplace add <owner/repo | git URL | path | https://…/marketplace.json>")
    for m in markets:
        entries = mk.marketplace_plugins(m)
        header(m, f"{len(entries)} plugins")
        for e in entries:
            print(f"    {WHITE}{str(e.get('name')):<24}{RST} {SLATE}{(e.get('description') or '')[:90]}{RST}")
        print(f"    {DARK_SLATE}/plugin install <name>@{m}{RST}\n")


# ─── install / update / uninstall ───────────────────────────────────────────

def _install(agent: Any, ws: Path, rest: list[str], opts: dict[str, str], flags: set[str], refresh: Callable[[Any], None]) -> None:
    if not rest:
        return usage("/plugin install <name>[@marketplace] [--scope user|project|local] | <folder | https git URL> [--project]")
    spec = rest[0]
    scope = opts.get("--scope") or ("project" if "--project" in flags else "user")
    if scope not in SCOPES:
        return err("--scope must be user, project or local")
    if "--marketplace" in opts:
        src = opts["--marketplace"]
        existing = {n for n, _ in mk.list_marketplaces()}
        if not confirm(f"Add marketplace {src}? Its plugins can run code on your machine.", default=not is_tty()):
            return info("Cancelled.")
        name, _ = mk.add_marketplace(src)
        if name not in existing:
            ok(f"Added marketplace {BOLD}{name}{RST}")
        spec = f"{spec.split('@')[0]}@{name}"
    is_path = Path(spec).expanduser().is_dir() or spec.startswith(("https://", "http://", "./", "../", "/", "~"))
    if is_path:
        root = (ws / ".axon" / "plugins") if scope in ("project", "local") else (_axon_home() / "plugins")
        if not confirm(f"Install plugin from {spec}? Plugins can add commands, skills and hooks that run code.", default=not is_tty()):
            return info("Cancelled.")
        p = pl.install_plugin(spec, root)
        refresh(agent)
        return ok(f"Installed {BOLD}{p.name}{RST} {SLATE}{p.version}{RST} → {p.path}")
    name, market = mk.split_id(spec)
    market_name, entry = mk.find_plugin_entry(name, market)
    print(f"    {WHITE}{name}@{market_name}{RST} {SLATE}{entry.get('description', '')}{RST}")
    if not confirm(f"Install {name}@{market_name} ({scope} scope)? Plugins can add commands, skills, hooks and MCP servers that run code.",
                   default=not is_tty()):
        return info("Cancelled.")
    p, deps = mk.install(spec, ws, scope)
    refresh(agent)
    extra = f" (+ dependencies: {', '.join(deps)})" if deps else ""
    ok(f"Installed {BOLD}{p.id}{RST} {SLATE}{p.version}{RST} (scope: {scope}){extra}  Plugin is now active.")
    schema = pl.plugin_user_config_schema(p)
    if schema:
        typed_hint(f"    Options to set: /plugin configure {p.name} {' '.join(k + '=…' for k in schema)}")
    comps = ", ".join(f"{n} {k}" for k, n in p.components().items())
    if comps:
        hint(f"    Adds: {comps}")


def _uninstall(agent: Any, ws: Path, installed: list[pl.Plugin], rest: list[str], refresh: Callable[[Any], None]) -> None:
    p = _find(installed, rest[0]) if rest else None
    if p is None:
        return err(f"No installed plugin named '{rest[0] if rest else ''}'.")
    deps = mk.dependents_of(p, ws)
    if deps:
        return err(f"Enabled plugin(s) still depend on {p.name}: {', '.join(d.name for d in deps)}. Disable or uninstall those first.")
    if confirm(f"Remove plugin {p.name} ({p.path})?", default=not is_tty()):
        mk.uninstall(p, ws)
        refresh(agent)
        ok(f"Removed plugin {p.name}")


def _toggle(agent: Any, ws: Path, installed: list[pl.Plugin], enable: bool, rest: list[str], opts: dict[str, str],
            refresh: Callable[[Any], None]) -> None:
    p = _find(installed, rest[0]) if rest else None
    if p is None:
        return err(f"No installed plugin named '{rest[0] if rest else ''}'.")
    scope = opts.get("--scope")
    if scope and scope not in SCOPES:
        return err("--scope must be user, project or local")
    if not enable:
        deps = mk.dependents_of(p, ws)
        if deps:
            names = " ".join(d.name for d in deps)
            return err(f"{p.name} is needed by {', '.join(d.name for d in deps)}. Disable those first" + (typed_only(f": /plugin disable {names}") or "."))
    else:
        runs = mk.plugin_runs(Path(p.path), ws, p.name)
        if runs:
            typed_hint(f"    {p.name} will run: " + "; ".join(runs))
        for dn, _ in mk._normalize_deps(p.manifest.get("dependencies"), p.marketplace):
            dep = _find(installed, dn)
            if dep is None:
                return err(f"{p.name} depends on '{dn}', which is not installed. Install it first.")
            if not dep.enabled:
                _set(ws, dep, True, scope)
                ok(f"Enabled dependency {dep.name}")
    _set(ws, p, enable, scope)
    refresh(agent)
    ok(f"{'Enabled' if enable else 'Disabled'} plugin {p.name}")


def _set(ws: Path, p: pl.Plugin, enable: bool, scope: str | None) -> None:
    if p.marketplace or scope:
        where = scope or _scope_of(p)
        pl.set_enabled(p.id, enable, ws, where)
        if where == "user":    # the legacy list (~/.axon/plugins/disabled.json) is user-wide: never for project / local
            pl.set_enabled(p.name, enable)
    else:
        pl.set_enabled(p.name, enable)


def _update(agent: Any, ws: Path, installed: list[pl.Plugin], rest: list[str], refresh: Callable[[Any], None]) -> None:
    if not rest:
        return usage("/plugin update <name>   (/plugin marketplace update <market> refreshes a listing first)")
    p = _find(installed, rest[0])
    if p is None:
        return err(f"No installed plugin named '{rest[0]}'.")
    if mk.update_plugin(p, ws):
        refresh(agent)
        newp = _find(pl.load_plugins(ws), p.name)
        ok(f"Updated {p.id} {SLATE}{p.version or '?'} → {newp.version if newp else '?'}{RST}  " + typed_only("Run /reload-plugins if anything looks stale."))
    else:
        info(f"{p.id} is already up to date ({p.version or 'no version'}).")


def _configure(ws: Path, installed: list[pl.Plugin], rest: list[str], refresh: Callable[[Any], None], agent: Any) -> None:
    p = _find(installed, rest[0]) if rest else None
    if p is None:
        return err(f"No installed plugin named '{rest[0] if rest else ''}'.")
    schema = pl.plugin_user_config_schema(p)
    if not schema:
        return info(f"{p.name} declares no options.")
    sets = dict(w.split("=", 1) for w in rest[1:] if "=" in w)
    if not sets:
        vals = pl.plugin_user_config_values(p, ws)
        header(f"{p.name} options")
        for k, spec in schema.items():
            shown = "••••" if spec.get("sensitive") and k in vals else vals.get(k, "-")
            req = " (required)" if spec.get("required") else ""
            kv(k, f"{shown}  {SLATE}{spec.get('title', '')}{req}: {spec.get('description', '')}{RST}")
        return typed_hint("    /plugin configure <name> KEY=VALUE ...")
    problems = pl.save_user_config(p, ws, sets)
    for pr in problems:
        err(pr)
    saved = [k for k in sets if not any(pr.startswith(k) for pr in problems)]
    if saved:
        refresh(agent)
        ok(f"Saved {', '.join(saved)} for {p.name}")


# ─── marketplace ────────────────────────────────────────────────────────────

def _marketplace(agent: Any, ws: Path, rest: list[str], refresh: Callable[[Any], None]) -> None:
    action = rest[0].lower() if rest else "list"
    arg = rest[1] if len(rest) > 1 else ""
    if action == "add":
        if not arg:
            return usage("/plugin marketplace add <owner/repo[#ref] | git URL | ./path | https://…/marketplace.json>")
        if not confirm(f"Add marketplace {arg}? Plugins from it can run code once installed.", default=not is_tty()):
            return info("Cancelled.")
        name, entry = mk.add_marketplace(arg)
        ok(f"Successfully added marketplace: {BOLD}{name}{RST} {SLATE}({entry.get('kind')}){RST}")
    elif action in ("list", "ls"):
        markets = mk.list_marketplaces()
        header("Marketplaces", f"{len(markets)} added")
        for n, e in markets:
            src = e.get("path") or e.get("url") or e.get("repo") or ""
            try:
                cnt = len(mk.marketplace_plugins(n))
            except mk.MarketplaceError:
                cnt = -1
            print(f"    {WHITE}{n:<26}{RST} {SLATE}{e.get('kind'):<7} {src}  ({cnt if cnt >= 0 else RED + 'unreadable' + SLATE} plugins){RST}")
        print()
    elif action in ("update", "refresh"):
        names = [arg] if arg else [n for n, _ in mk.list_marketplaces()]
        for n in names:
            count = mk.update_marketplace(n)
            ok(f"Updated {n}: {count} plugins")
    elif action in ("remove", "rm"):
        if not arg:
            return usage("/plugin marketplace remove <name>")
        affected = [p.id for p in pl.load_plugins(ws) if p.marketplace == arg]
        note = f" This also uninstalls: {', '.join(affected)}." if affected else ""
        if not confirm(f"Remove marketplace {arg}?{note}", default=not is_tty()):
            return info("Cancelled.")
        removed = mk.remove_marketplace(arg, ws)
        refresh(agent)
        ok(f"Removed marketplace {arg}" + (f" and uninstalled {', '.join(removed)}" if removed else ""))
    else:
        usage("/plugin marketplace add|list|update|remove [<source|name>]")


# ─── validate ───────────────────────────────────────────────────────────────

def _validate(rest: list[str]) -> None:
    if not rest:
        return usage("/plugin validate <path to a plugin, marketplace or skills directory>")
    kind, errors, warnings = pl.validate_path(Path(rest[0]))
    header(f"Validate {kind}", rest[0])
    for w in warnings:
        print(f"    {GOLD}!{RST} {SLATE}{w}{RST}")
    for e in errors:
        print(f"    {RED}✗{RST} {e}")
    if errors:
        print(f"\n    {RED}Validation failed{RST}\n")
    elif warnings:
        print(f"\n    {MINT}Validation passed with warnings{RST}\n")
    else:
        print(f"\n    {MINT}✔ Validation passed{RST}\n")
