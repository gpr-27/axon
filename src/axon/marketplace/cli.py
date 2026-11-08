"""
The ``/marketplace`` command (also ``axon marketplace``): browse, install and create connectors,
skills and plugins.

    marketplace list [connectors|skills|plugins] [--installed|--discover] [--free] [--category C] [--json]
    marketplace search <words> [--tab T] [--free] [--category C] [--sort name|category|installed|tokens|updates]
    marketplace info <id>            marketplace categories [tab]
    marketplace install <id> [--scope user|project|local] [--env NAME=VALUE] [--keep] [--no-check] [--timeout S]
    marketplace remove <id>          marketplace enable <id>      marketplace disable <id>
    marketplace add <url|command> --name N [--transport http|sse]    (custom connector)
    marketplace add <git url|owner/repo|path>     add your own marketplace: plugins and/or skill folders
    marketplace sources | refresh [name] | remove-source <name>    the marketplaces you added
    marketplace create-skill [--name N --description D --trigger T --instructions TEXT]
    marketplace create-plugin <name> [--scope user|project]

  Control what is installed (connectors, skills and plugins):
    marketplace status [--check] [--all] [--json]       everything: enabled, connected, tools, last error, scope
    marketplace test <id> [--timeout S]         connect once and list the tools (health check)
    marketplace reconnect <id|all>              reconnect in the running session
    marketplace logs <id> [lines]               the connector's log
    marketplace scope <id> user|project|local   move a connector to another scope
    marketplace env <id> NAME=VALUE ... [--unset NAME] [--literal]    keys and variables
    marketplace timeout <id> <seconds>          time limit for the connector
    marketplace tool <id> [<tool>|*] allow|ask|deny|reset [--global]   permission per tool
    marketplace export [file] [--include-secrets]      the whole setup as one JSON file
    marketplace import <file> [--dry-run] [--overwrite]
    marketplace auto on|off|status|set <name> <value>  automatic behaviour (off by default)
    marketplace suggest [--reset]               connectors that fit this project
    marketplace updates | update <id>           look for newer skills, plugins and catalog entries

With no arguments, a terminal gets an interactive browser and anything else gets the connector list.
"""
from __future__ import annotations

import dataclasses
import getpass
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.marketplace import catalog, control, control_cli, installer, plugin_tools, skill_tools
from axon.marketplace.catalog import Connector, EnvVar, PluginEntry, SkillEntry
from axon.marketplace.render import (
    Row, category_lines, discover_lines, info_lines, yours_lines,
)

VALUE_OPTIONS = {
    "scope", "tab", "category", "env", "timeout", "name", "transport", "description",
    "trigger", "instructions", "instructions-file", "script", "unset", "from", "sort",
}
SORTS = ("name", "category", "installed", "tokens", "updates")
TAB_ALIASES = {
    "connector": "connectors", "connectors": "connectors", "mcp": "connectors",
    "skill": "skills", "skills": "skills",
    "plugin": "plugins", "plugins": "plugins",
}
HELP_TEXT = __doc__ or ""


@dataclass
class Args:
    """Command line split into plain words, flags and --option values."""
    words: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    options: dict[str, list[str]] = field(default_factory=dict)

    def option(self, name: str, default: str = "") -> str:
        values = self.options.get(name)
        return values[-1] if values else default

    def option_list(self, name: str) -> list[str]:
        return list(self.options.get(name, []))


def parse_args(tokens: list[str]) -> Args:
    args = Args()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "-y":
            args.flags.add("yes")
        elif not token.startswith("--"):
            args.words.append(token)
        elif "=" in token:
            name, value = token[2:].split("=", 1)
            args.options.setdefault(name, []).append(value)
        elif token[2:] in VALUE_OPTIONS and index < len(tokens):
            args.options.setdefault(token[2:], []).append(tokens[index])
            index += 1
        else:
            args.flags.add(token[2:])
    return args


@dataclass
class Outcome:
    """Exit code plus the connector names that changed (so a live session can follow)."""
    code: int = 0
    installed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reconnect: list[str] = field(default_factory=list)


def parse_timeout(text: str) -> float:
    """Seconds from --timeout, or the default when it is missing or not a positive number."""
    try:
        value = float(text)
    except ValueError:
        return installer.DEFAULT_CHECK_TIMEOUT
    return value if value > 0 else installer.DEFAULT_CHECK_TIMEOUT


def parse_env_options(items: list[str]) -> dict[str, str]:
    values = {}
    for item in items:
        if "=" in item:
            name, value = item.split("=", 1)
            values[name.strip()] = value
    return values


class Marketplace:
    """Runs marketplace commands for one workspace. Output and questions go through callbacks."""

    def __init__(self, workspace: Path, *, out: Callable[[str], None] = print,
                 ask_line: Callable[[str], str] | None = None,
                 ask_secret: Callable[[str], str] | None = None,
                 health_check: installer.HealthCheck = installer.check_server,
                 data_dir: Path = catalog.DATA_DIR, agent: Any = None) -> None:
        self.workspace = Path(workspace)
        self.agent = agent
        self.in_session = agent is not None
        self.permissions = getattr(agent, "permissions", None)
        self.out = out
        self.ask_line = ask_line
        self.ask_secret = ask_secret
        self.health_check = health_check
        self.data_dir = data_dir
        self.outcome = Outcome()

    # ── helpers ──────────────────────────────────────────────────────────────
    def say(self, lines: list[str] | str) -> None:
        if isinstance(lines, str):
            lines = [lines]
        for line in lines:
            self.out(line)

    def fail(self, message: str) -> Outcome:
        self.say(f"  Error: {message}")
        self.outcome.code = 1
        return self.outcome

    def entries(self, tab: str) -> list[Any]:
        """The built-in catalog plus the skills and plugins of marketplaces you added."""
        built_in = catalog.load_tab(tab, self.data_dir)
        if tab == "connectors":
            return built_in
        from axon.marketplace import sources
        return built_in + sources.user_entries(tab, {e.id for e in built_in})

    def installed_ids(self, tab: str) -> set[str]:
        if tab == "connectors":
            return set(self.connector_specs())
        if tab == "skills":
            return set(skill_tools.installed_skills(self.workspace))
        names = set(plugin_tools.installed_plugins(self.workspace))
        # a catalog plugin whose id would clash with a connector (github, slack...) is listed as "<id>-plugin"
        return names | {f"{n}-plugin" for n in names}

    def connector_specs(self) -> dict[str, Any]:
        specs = installer.installed_names(self.workspace)
        return {name: spec for name, spec in specs.items() if spec.scope != "plugin"}

    def can_ask(self) -> bool:
        return self.ask_line is not None

    # ── entry point ──────────────────────────────────────────────────────────
    def run(self, tokens: list[str]) -> Outcome:
        args = parse_args(tokens)
        if not args.words:
            return self.interactive(args) if self.can_ask() else self.cmd_list(args, [])
        verb, rest = args.words[0].lower(), args.words[1:]
        handlers: dict[str, Callable[[Args, list[str]], Outcome]] = {
            "list": self.cmd_list, "ls": self.cmd_list, "search": self.cmd_search, "find": self.cmd_search,
            "info": self.cmd_info, "show": self.cmd_info, "categories": self.cmd_categories,
            "install": self.cmd_install, "add-catalog": self.cmd_install,
            "remove": self.cmd_remove, "uninstall": self.cmd_remove, "rm": self.cmd_remove,
            "enable": self.cmd_enable, "disable": self.cmd_disable, "add": self.cmd_add_custom,
            "create-skill": self.cmd_create_skill, "create-plugin": self.cmd_create_plugin,
            "sources": self.cmd_sources, "remove-source": self.cmd_remove_source, "refresh": self.cmd_refresh_source,
            "help": self.cmd_help,
        }
        handler = handlers.get(verb)
        if handler is not None:
            return handler(args, rest)
        control_handler = control_cli.HANDLERS.get(verb)
        if control_handler is not None:
            return control_handler(self, args, rest)
        return self.fail(f"unknown subcommand '{verb}'. Try: /marketplace help")

    def cmd_help(self, _args: Args, _rest: list[str]) -> Outcome:
        self.say(HELP_TEXT.strip().splitlines())
        return self.outcome

    # ── rows ─────────────────────────────────────────────────────────────────
    def rows_for(self, tab: str, entries: list[Any]) -> list[Row]:
        installed = self.installed_ids(tab)
        rows = []
        for entry in entries:
            rows.append(self._row(tab, entry, entry.id in installed))
        return rows

    def _row(self, tab: str, entry: Any, installed: bool) -> Row:
        row = Row(id=entry.id, name=entry.name, publisher=entry.publisher, description=entry.description,
                  category=entry.category, verified=entry.verified, installed=installed,
                  free=getattr(entry, "free", True))
        if tab == "skills" and entry.kind == "github":
            row.tokens = self.skill_tokens().get(entry.id)
            if row.tokens is None:
                row.tokens = skill_tools.listing_tokens(entry.id, entry.description)
            row.update = installed and self.skill_has_update(entry)
        elif tab == "plugins" and installed:
            row.update = self.plugin_has_update(entry)
        if tab == "connectors":
            row.type_label = entry.type_label
            row.note = entry.free_note
        return row

    def skill_tokens(self) -> dict[str, int]:
        if not hasattr(self, "_skill_tokens"):
            self._skill_tokens = skill_tools.installed_listing_tokens(self.workspace)
        return self._skill_tokens

    def skill_has_update(self, entry: Any) -> bool:
        found = skill_tools.installed_skills(self.workspace).get(entry.id)
        if found is None or not entry.sha:
            return False
        commit = str(skill_tools.read_origin(found[1].parent).get("commit") or "")
        return bool(commit) and not commit.startswith(entry.sha[:12])

    def plugin_has_update(self, entry: Any) -> bool:
        plugin = plugin_tools.installed_plugins(self.workspace).get(entry.name)
        sha = getattr(entry, "pinned_sha", "")
        commit = str((plugin.record if plugin else {}).get("commit") or "")
        return bool(sha and commit and not commit.startswith(sha[:12]))

    def yours_rows(self, tab: str) -> list[Row]:
        if tab == "connectors":
            return self._yours_connectors()
        if tab == "skills":
            return self._yours_skills()
        return [Row(id=n, name=n, publisher="", description="", category="",
                    status="enabled" if p.enabled else "disabled")
                for n, p in plugin_tools.installed_plugins(self.workspace).items()]

    def _yours_skills(self) -> list[Row]:
        from axon.skills.manager import SkillManager
        off = set(SkillManager(self.workspace).disabled)
        rows = []
        for name, (scope, _path) in skill_tools.installed_skills(self.workspace).items():
            state = "disabled" if name in off else "enabled"
            rows.append(Row(id=name, name=name, publisher="", description="", category="",
                            status=f"{state} ({scope})"))
        return rows

    def _yours_connectors(self) -> list[Row]:
        known = {entry.id: entry for entry in self.entries("connectors")}
        rows = []
        for name, spec in sorted(self.connector_specs().items()):
            entry = known.get(name)
            type_label = "Local" if spec.type == "stdio" else "Web"
            title = entry.name if entry else name
            status = "disabled" if spec.disabled else "enabled"
            rows.append(Row(id=name, name=title, publisher="", description="", category="",
                            type_label=type_label, status=status))
        return rows

    # ── list / search / info ─────────────────────────────────────────────────
    def tab_from(self, args: Args, rest: list[str]) -> tuple[str, list[str]]:
        """Pick the tab from --tab or a leading word such as 'skills'. Returns (tab, other words)."""
        if args.option("tab"):
            return TAB_ALIASES.get(args.option("tab").lower(), "connectors"), rest
        if rest and rest[0].lower() in TAB_ALIASES:
            return TAB_ALIASES[rest[0].lower()], rest[1:]
        return "connectors", rest

    def cmd_list(self, args: Args, rest: list[str]) -> Outcome:
        tab, words = self.tab_from(args, rest)
        return self.show_tab(tab, args, " ".join(words))

    def cmd_search(self, args: Args, rest: list[str]) -> Outcome:
        tab, words = self.tab_from(args, rest)
        if not words:
            return self.fail("usage: /marketplace search <words> [--tab connectors|skills|plugins]")
        return self.show_tab(tab, args, " ".join(words))

    def show_tab(self, tab: str, args: Args, query: str) -> Outcome:
        entries = self.entries(tab)
        installed = self.installed_ids(tab)
        wants_yours = "installed" in args.flags or "yours" in args.flags
        wants_new = "discover" in args.flags
        state = True if wants_yours else (False if wants_new else None)
        shown = catalog.filter_entries(entries, query, args.option("category"), "free" in args.flags,
                                       installed, state)
        sort = args.option("sort", "name").lower()
        if sort not in SORTS:
            return self.fail(f"--sort must be one of: {', '.join(SORTS)}")
        shown = self.sorted_entries(tab, shown, sort, installed)
        if "json" in args.flags:
            self.say(json.dumps([dataclasses.asdict(e) for e in shown], indent=2))
            return self.outcome
        if wants_yours:
            self.say(yours_lines(tab, self._filter_yours(tab, shown)))
        else:
            self.say(discover_lines(tab, self.rows_for(tab, shown), len(entries), hint=not self.can_ask()))
        return self.outcome

    def sorted_entries(self, tab: str, entries: list[Any], sort: str, installed: set[str]) -> list[Any]:
        """name (default), category, installed first, cheapest tokens first, or updates first."""
        def name_key(e: Any) -> str:
            return e.name.lower()
        if sort == "category":
            return sorted(entries, key=lambda e: (e.category.lower(), name_key(e)))
        if sort == "installed":
            return sorted(entries, key=lambda e: (e.id not in installed, name_key(e)))
        if sort == "updates":
            flags = {e.id: self._row(tab, e, e.id in installed).update for e in entries}
            return sorted(entries, key=lambda e: (not flags[e.id], name_key(e)))
        if sort == "tokens":
            cost = {e.id: self._row(tab, e, e.id in installed).tokens for e in entries}
            return sorted(entries, key=lambda e: (cost[e.id] is None, cost[e.id] or 0, name_key(e)))
        return sorted(entries, key=name_key)

    def _filter_yours(self, tab: str, shown: list[Any]) -> list[Row]:
        wanted = {entry.id for entry in shown}
        rows = self.yours_rows(tab)
        return [row for row in rows if row.id in wanted or row.id not in self._catalog_ids(tab)]

    def _catalog_ids(self, tab: str) -> set[str]:
        return {entry.id for entry in self.entries(tab)}

    def cmd_categories(self, args: Args, rest: list[str]) -> Outcome:
        tab, _words = self.tab_from(args, rest)
        self.say(category_lines(tab, catalog.categories(self.entries(tab))))
        return self.outcome

    def find(self, args: Args, entry_id: str) -> tuple[str, Any] | None:
        """Find a catalog entry by id in the chosen tab, or in every tab when none is chosen."""
        tabs = [TAB_ALIASES[args.option("tab").lower()]] if args.option("tab") in TAB_ALIASES else list(catalog.TABS)
        hits = []
        for tab in tabs:
            entry = catalog.find_entry(self.entries(tab), entry_id)
            if entry is not None:
                hits.append((tab, entry))
        if len(hits) > 1:
            names = ", ".join(tab for tab, _ in hits)
            self.say(f"  '{entry_id}' exists in several tabs ({names}); add --tab to choose.")
            return None
        return hits[0] if hits else None

    def cmd_info(self, args: Args, rest: list[str]) -> Outcome:
        if not rest:
            return self.fail("usage: /marketplace info <id>")
        found = self.find(args, rest[0])
        if found is None:
            return self.info_for_installed(rest[0])
        tab, entry = found
        row = self._row(tab, entry, entry.id in self.installed_ids(tab))
        self.say(info_lines(row, self.extra_facts(tab, entry)))
        if tab == "connectors" and row.installed:
            self.say(self.installed_connector_lines(entry.id))
        return self.outcome

    def info_for_installed(self, name: str) -> Outcome:
        """Details of something installed that is not in the catalog (a custom connector)."""
        if name not in self.connector_specs():
            return self.fail(f"nothing called '{name}' in the catalog or installed.")
        self.say([f"  {name}  (custom connector)"] + self.installed_connector_lines(name))
        return self.outcome

    def installed_connector_lines(self, name: str) -> list[str]:
        """Scope, address, timeout, variables, permission rules and health of an installed connector."""
        from axon.mcp import health as mcp_health
        spec = self.connector_specs()[name]
        record = mcp_health.get(name)
        lines = ["", "  Installed:", f"    scope     {spec.scope}   ({spec.source or 'managed'})",
                 f"    target    {spec.describe_target()}", f"    enabled   {'no' if spec.disabled else 'yes'}",
                 f"    timeout   {spec.timeout:g}s"]
        if spec.env:
            lines.append("    env       " + ", ".join(sorted(spec.env)))
        for kind, rule, _source in control.tool_policies(self.workspace, name, self.permissions):
            lines.append(f"    {kind:<9} {rule}")
        if record.last_error:
            lines.append(f"    last error  {record.last_error}")
        if record.auto_disabled:
            lines.append(f"    switched off by Axon after repeated failures: /marketplace enable {name}")
        return lines

    def extra_facts(self, tab: str, entry: Any) -> list[tuple[str, str]]:
        facts = [("docs", entry.docs_url)]
        if tab == "connectors":
            facts += self._connector_facts(entry)
        if tab == "skills":
            facts += [("source", entry.source), ("license", entry.license), ("note", entry.note)]
        if tab == "plugins" and entry.kind == "git":
            facts.append(("source", f"{entry.source.get('url')} ({entry.source.get('path')})"))
        return facts

    def _connector_facts(self, entry: Connector) -> list[tuple[str, str]]:
        target = entry.url if entry.remote else " ".join([entry.command] + entry.args)
        facts = [("transport", entry.transport), ("target", target), ("auth", entry.auth),
                 ("cost", entry.free_note or ("free" if entry.free else ""))]
        for var in entry.env_vars:
            need = "required" if var.required else "optional"
            facts.append(("needs", f"{var.name} ({need}) {var.help}".strip()))
        return facts

    # ── install ──────────────────────────────────────────────────────────────
    def cmd_install(self, args: Args, rest: list[str]) -> Outcome:
        if not rest:
            return self.fail("usage: /marketplace install <id>")
        found = self.find(args, rest[0])
        if found is None:
            return self.fail(f"nothing called '{rest[0]}' in the catalog. Try: /marketplace search {rest[0]}")
        tab, entry = found
        if tab == "connectors":
            return self.install_connector(entry, args)
        if tab == "skills":
            return self.install_skill(entry, args)
        return self.install_plugin(entry, args)

    def secret_prompt(self, var: EnvVar) -> str:
        if self.ask_secret is None:
            return ""
        label = f"  {var.name}" + (f" ({var.help})" if var.help else "") + ": "
        return self.ask_secret(label)

    def install_connector(self, entry: Connector, args: Args, name: str = "") -> Outcome:
        timeout = parse_timeout(args.option("timeout"))
        self.say(f"  Installing {entry.name}...")
        result = installer.install_connector(
            entry, self.workspace, scope=args.option("scope", "user"), name=name or args.option("name"),
            env=parse_env_options(args.option_list("env")), prompt=self.secret_prompt,
            keep="keep" in args.flags, check="no-check" not in args.flags, timeout=timeout,
            health_check=self.health_check)
        return self.report_install(result)

    def report_install(self, result: installer.InstallResult) -> Outcome:
        if not result.ok:
            return self.fail(result.message)
        self.say(f"  OK: {result.message}")
        if result.tools:
            shown = ", ".join(result.tools[:8])
            more = f" and {len(result.tools) - 8} more" if len(result.tools) > 8 else ""
            self.say(f"      Tools: {shown}{more}")
        if result.saved_env:
            self.say(f"      Saved {', '.join(result.saved_env)} to ~/.axon/.env (owner-only).")
        if not result.already_installed:
            self.outcome.installed.append(result.name)
        return self.outcome

    def install_skill(self, entry: SkillEntry, args: Args) -> Outcome:
        if entry.kind == "builtin":
            return self.cmd_create_skill(args, [])
        try:
            bundle = skill_tools.fetch_skill(entry)
        except Exception as e:  # noqa: BLE001 - network and parse errors become one clear line
            return self.fail(f"could not download skill '{entry.id}': {e}")
        pin = bundle.commit[:10] if bundle.commit else "unpinned"
        self.say(f"  {entry.name} from {entry.source} ({pin}): {len(bundle.files)} file(s), "
                 f"{bundle.total_bytes() // 1000} KB")
        self.say(bundle.listing())
        scripts = bundle.executables()
        if scripts:
            self.say(f"  {len(scripts)} file(s) are executable scripts. Axon never runs them by itself; the skill's "
                     "instructions may ask the model to, and each run goes through your permission prompts.")
        if self.can_ask() and "yes" not in args.flags and not self.confirm("Install this skill?"):
            self.say("  Cancelled. Nothing was installed.")
            return self.outcome
        try:
            path = skill_tools.install_skill(entry, self.workspace, args.option("scope", "user"),
                                             "force" in args.flags, bundle=bundle)
        except FileExistsError as e:
            self.say(f"  Already installed: {e}")
            return self.outcome
        except Exception as e:  # noqa: BLE001
            return self.fail(f"could not install skill '{entry.id}': {e}")
        self.say(f"  OK: Installed skill '{entry.id}' at {path.parent} ({len(bundle.files)} files). {entry.note}".rstrip())
        return self.outcome

    def confirm(self, question: str) -> bool:
        if self.ask_line is None:
            return True
        return self.ask_line(f"  {question} [y/N]: ").strip().lower() in ("y", "yes")

    def install_plugin(self, entry: PluginEntry, args: Args) -> Outcome:
        if entry.kind == "builtin":
            return self.cmd_create_plugin(args, [])
        if entry.kind == "marketplace":
            return self.install_from_added_marketplace(entry, args)
        try:
            def review(lines: list[str]) -> bool:
                self.say(lines)
                return "yes" in args.flags or not self.can_ask() or self.confirm("Install and enable it?")
            path = plugin_tools.install_plugin(entry, self.workspace, args.option("scope", "user"), review)
        except plugin_tools.PluginInstallError as e:
            return self.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return self.fail(f"could not install plugin '{entry.id}': {e}")
        self.say(f"  OK: Installed plugin '{entry.id}' at {path}. Run /reload-plugins to load it.")
        return self.outcome

    def install_from_added_marketplace(self, entry: PluginEntry, args: Args) -> Outcome:
        from axon.commands import marketplace as plugin_market
        spec = f"{entry.name}@{entry.source.get('marketplace')}"

        def review(lines: list[str]) -> bool:
            self.say(lines)
            return "yes" in args.flags or not self.can_ask() or self.confirm("Install and enable it?")
        try:
            plugin, deps = plugin_market.install(spec, self.workspace, args.option("scope", "user"), review=review)
        except plugin_market.MarketplaceError as e:
            return self.fail(str(e))
        self.say(f"  OK: Installed plugin '{plugin.name}' at {plugin.path}. Run /reload-plugins to load it.")
        return self.outcome

    # ── marketplaces you add ─────────────────────────────────────────────────
    def cmd_add_source(self, args: Args, rest: list[str]) -> Outcome:
        """``/marketplace add <git url | owner/repo | path | marketplace.json URL>``: a plugin marketplace or a
        folder / repository of skills. Nothing is installed; its entries appear in list, search and install."""
        from axon.commands import marketplace as plugin_market
        from axon.marketplace import sources
        target = " ".join(rest)
        try:
            name, state = plugin_market.add_marketplace(target)
        except plugin_market.MarketplaceError as e:
            return self.fail(str(e))
        skills = len([e for e in sources.user_skill_entries() if e.publisher == name])
        plugins = 0 if state.get("skillsOnly") else len(plugin_market.marketplace_plugins(name))
        self.say(f"  OK: Added marketplace '{name}': {plugins} plugin(s), {skills} skill(s). "
                 f"It is not trusted like the built-in catalog (no verified mark): review what you install. "
                 f"Browse: /marketplace list skills --category Custom")
        return self.outcome

    def cmd_sources(self, args: Args, rest: list[str]) -> Outcome:
        from axon.commands import marketplace as plugin_market
        rows = plugin_market.list_marketplaces()
        if not rows:
            self.say("  No marketplaces added. /marketplace add <git url | owner/repo | path>")
        for name, state in rows:
            where = state.get("path") or state.get("url") or state.get("repo") or ""
            kind = "skills only" if state.get("skillsOnly") else "plugins"
            self.say(f"  {name}  ({state.get('kind')}, {kind})  {where}")
        return self.outcome

    def cmd_remove_source(self, args: Args, rest: list[str]) -> Outcome:
        from axon.commands import marketplace as plugin_market
        if not rest:
            return self.fail("usage: /marketplace remove-source <name>")
        try:
            removed = plugin_market.remove_marketplace(rest[0], self.workspace)
        except plugin_market.MarketplaceError as e:
            return self.fail(str(e))
        self.say(f"  OK: Removed marketplace '{rest[0]}'" + (f" and its plugins: {', '.join(removed)}" if removed else "."))
        return self.outcome

    def cmd_refresh_source(self, args: Args, rest: list[str]) -> Outcome:
        from axon.commands import marketplace as plugin_market
        names = rest or [n for n, _ in plugin_market.list_marketplaces()]
        for name in names:
            try:
                count = plugin_market.update_marketplace(name)
            except plugin_market.MarketplaceError as e:
                return self.fail(str(e))
            self.say(f"  OK: refreshed '{name}' ({count} plugin(s)).")
        return self.outcome

    # ── remove / enable / disable ────────────────────────────────────────────
    def cmd_remove(self, args: Args, rest: list[str]) -> Outcome:
        if not rest:
            return self.fail("usage: /marketplace remove <id>")
        name = rest[0]
        tab = self.installed_tab(args, name)
        if tab is None:
            return self.fail(f"'{name}' is not installed.")
        if tab == "connectors":
            result = installer.remove_connector(name, self.workspace, args.option("scope") or None)
            if result.ok:
                self.outcome.removed.append(name)
            return self.report_simple(result.ok, result.message)
        if tab == "skills":
            done = skill_tools.remove_skill(name, self.workspace, args.option("scope") or None)
            return self.report_simple(done, f"Removed skill '{name}'." if done else f"Skill '{name}' not found.")
        done = plugin_tools.remove_plugin(name, self.workspace)
        return self.report_simple(done, f"Removed plugin '{name}'." if done else f"Plugin '{name}' not found.")

    def installed_tab(self, args: Args, name: str) -> str | None:
        wanted = args.option("tab")
        tabs = [TAB_ALIASES[wanted]] if wanted in TAB_ALIASES else list(catalog.TABS)
        for tab in tabs:
            if name in self.installed_ids(tab):
                return tab
        return None

    def report_simple(self, ok: bool, message: str) -> Outcome:
        if not ok:
            return self.fail(message)
        self.say(f"  OK: {message}")
        return self.outcome

    def cmd_enable(self, args: Args, rest: list[str]) -> Outcome:
        return self.switch(args, rest, True)

    def cmd_disable(self, args: Args, rest: list[str]) -> Outcome:
        return self.switch(args, rest, False)

    def switch(self, args: Args, rest: list[str], enabled: bool) -> Outcome:
        if not rest:
            return self.fail("usage: /marketplace enable|disable <id>")
        name = rest[0]
        tab = self.installed_tab(args, name)
        if tab == "connectors":
            result = installer.set_connector_enabled(name, self.workspace, enabled)
            if result.ok:
                self.outcome.reconnect.append(name)
            return self.report_simple(result.ok, result.message)
        if tab == "plugins":
            if enabled and not self.review_plugin_enable(name, args):
                return self.outcome
            done = plugin_tools.set_plugin_enabled(name, self.workspace, enabled)
            return self.report_simple(done, f"{'Enabled' if enabled else 'Disabled'} plugin '{name}'.")
        if tab == "skills":
            scope = "project" if args.option("scope") == "project" else "user"
            result = control.set_skill_enabled(self.workspace, name, enabled, scope)
            return self.report_simple(result.ok, result.message)
        return self.fail(f"'{name}' is not installed.")

    def review_plugin_enable(self, name: str, args: Args) -> bool:
        """Before a plugin is switched on: list the hooks and MCP commands it runs."""
        from axon.commands import marketplace as plugin_market
        plugin = plugin_tools.installed_plugins(self.workspace).get(name)
        if plugin is None or plugin.enabled:
            return True
        self.say(plugin_market.install_review_lines(plugin.id, plugin_market.plugin_runs(plugin.path, self.workspace, name),
                                                    str(plugin.record.get("commit") or "")))
        if self.can_ask() and "yes" not in args.flags and not self.confirm("Enable it?"):
            self.say("  Not enabled.")
            return False
        return True

    # ── custom connector ─────────────────────────────────────────────────────
    def cmd_add_custom(self, args: Args, rest: list[str]) -> Outcome:
        if not rest:
            return self.fail("usage: /marketplace add <https://url | command args...> --name <name>  (a connector)\n"
                             "       /marketplace add <git url | owner/repo | path> [--marketplace]  (plugins and skills)")
        target = " ".join(rest)
        from axon.marketplace import sources
        if "marketplace" in args.flags or "source" in args.flags or (
                "connector" not in args.flags and sources.looks_like_source(target)):
            return self.cmd_add_source(args, rest)
        name = args.option("name") or catalog_name_from(target)
        try:
            entry = installer.custom_connector(name, target, args.option("transport"))
        except ValueError as e:
            return self.fail(str(e))
        return self.install_connector(entry, args, name)

    # ── creators ─────────────────────────────────────────────────────────────
    def prompt(self, question: str, default: str = "") -> str:
        if self.ask_line is None:
            return default
        answer = self.ask_line(f"  {question}" + (f" [{default}]" if default else "") + ": ").strip()
        return answer or default

    def prompt_block(self, question: str) -> str:
        """Read lines until an empty line (used for the skill instructions)."""
        if self.ask_line is None:
            return ""
        self.say(f"  {question} (finish with an empty line)")
        lines = []
        while True:
            line = self.ask_line("    > ")
            if not line.strip():
                break
            lines.append(line)
        return "\n".join(lines)

    def skill_from_args(self, args: Args) -> skill_tools.NewSkill:
        name = args.option("name") or skill_tools.slugify(self.prompt("Skill name (for example code-reviewer)"))
        description = args.option("description") or self.prompt("One-line description")
        triggers = args.option_list("trigger")
        if not triggers and self.can_ask():
            triggers = [t.strip() for t in self.prompt("Trigger phrases, comma separated (optional)").split(",") if t.strip()]
        instructions = args.option("instructions") or self._instructions_from_file(args) or self.prompt_block("Instructions")
        scripts = self._scripts_from(args)
        return skill_tools.NewSkill(name, description, instructions, triggers, scripts)

    def _instructions_from_file(self, args: Args) -> str:
        path = args.option("instructions-file")
        if not path:
            return ""
        return Path(path).expanduser().read_text(encoding="utf-8")

    def _scripts_from(self, args: Args) -> dict[str, str]:
        scripts = {}
        for item in args.option_list("script"):
            path = Path(item).expanduser()
            scripts[path.name] = path.read_text(encoding="utf-8")
        return scripts

    def cmd_create_skill(self, args: Args, _rest: list[str]) -> Outcome:
        try:
            skill = self.skill_from_args(args)
        except (ValueError, OSError) as e:
            return self.fail(str(e))
        return self.create_skill(skill, args.option("scope", "user"), "force" in args.flags)

    def create_skill(self, skill: skill_tools.NewSkill, scope: str, force: bool = False) -> Outcome:
        try:
            path = skill_tools.create_skill(skill, self.workspace, scope, force)
        except (ValueError, FileExistsError, OSError) as e:
            return self.fail(str(e))
        self.say(f"  OK: Created and validated skill '{skill.name}' at {path}")
        return self.outcome

    def cmd_create_plugin(self, args: Args, rest: list[str]) -> Outcome:
        name = rest[0] if rest else (args.option("name") or self.prompt("Plugin name"))
        if not name:
            return self.fail("usage: /marketplace create-plugin <name> [--scope user|project]")
        try:
            folder = plugin_tools.create_new_plugin(name, self.workspace, args.option("scope", "user"))
        except (ValueError, FileExistsError, OSError) as e:
            return self.fail(str(e))
        self.say(f"  OK: Created and validated plugin at {folder}")
        return self.outcome

    # ── interactive browser ──────────────────────────────────────────────────
    def interactive(self, args: Args | None = None) -> Outcome:
        """The arrow-key browser (axon.marketplace.browser) in a terminal; plain text otherwise."""
        from axon.ui.menus import menus_enabled
        args = args or Args()
        if not menus_enabled():
            return self.cmd_list(args, [])
        from axon.marketplace.browser import run_browser
        tab, _words = self.tab_from(args, [])
        run_browser(self, tab)
        return self.outcome


def catalog_name_from(target: str) -> str:
    """Guess a server name from a URL host or a command: 'https://mcp.example.com/x' -> 'example'."""
    text = target.strip()
    if text.lower().startswith(("http://", "https://")):
        host = text.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
        parts = [p for p in host.split(".") if p not in ("mcp", "www", "api")]
        return skill_tools.slugify(parts[-2] if len(parts) >= 2 else host) or "custom"
    first = text.split()[0] if text.split() else "custom"
    return skill_tools.slugify(Path(first).name) or "custom"


def stdin_is_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def run_command(tokens: list[str], workspace: Path, *, interactive: bool | None = None,
                out: Callable[[str], None] = print, agent: Any = None) -> Outcome:
    """Run one marketplace command line. Asks questions only when attached to a terminal.
    ``agent`` is the running session (None for ``axon marketplace ...`` on the command line)."""
    can_prompt = stdin_is_terminal() if interactive is None else interactive
    ask_line = input if can_prompt else None
    ask_secret = getpass.getpass if can_prompt else None
    shop = Marketplace(workspace, out=out, ask_line=ask_line, ask_secret=ask_secret, agent=agent)
    try:
        return shop.run(tokens)
    except (EOFError, KeyboardInterrupt):
        shop.say("")
        shop.outcome.code = 130
        return shop.outcome
    except catalog.CatalogError as e:
        return shop.fail(f"catalog problem: {e}")


def main(argv: list[str]) -> int:
    """``axon marketplace ...`` entry point."""
    return run_command(list(argv), Path.cwd()).code
