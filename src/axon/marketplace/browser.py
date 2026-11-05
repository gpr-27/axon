"""
Keyboard browser for ``/marketplace`` in a terminal.

    ←→ / Tab      Connectors · Skills · Plugins
    y             Yours ⇄ Discover
    ↑↓ / j k      move            /  filter as you type
    Enter         open an item (details + the actions valid for it)
    Esc / q       back / quit

Category and "free only" are rows of the list, and so are the list-wide actions (add a custom
connector, new skill, new plugin, status, automation, export, import). Every action calls the same
:class:`axon.marketplace.cli.Marketplace` method the typed subcommand uses; this module only asks
the questions (guided steps) and passes the answers on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from axon.marketplace import catalog, control, control_cli, installer
from axon.marketplace.render import TAB_TITLES, info_lines
from axon.ui.menus import Action, ask_text, choose_action, choose_value, confirm, run_action, run_with_spinner
from axon.ui.picker import SelectItem, Selector

if TYPE_CHECKING:
    from axon.marketplace.cli import Args, Marketplace

FOOTER = "←→ tabs · ↑↓ move · Enter open · / filter · y yours/discover · Esc back · q quit"
SCOPES = [("user", "user", "every project on this machine"),
          ("project", "project", "this project, shared with the team"),
          ("local", "local", "this project, only you")]


@dataclass
class BrowserState:
    tab: str = "connectors"
    yours: bool = False
    category: str = ""
    free: bool = False
    index: int = 0


@dataclass
class Choice:
    """What a row stands for: an item of a tab, an option to change, or a list-wide action."""
    kind: str            # "entry" | "installed" | "option" | "action"
    tab: str = ""
    item: Any = None     # catalog entry, Row, option name or Action


def make_args(*words: str, **options: str) -> "Args":
    from axon.marketplace.cli import Args
    return Args(words=list(words), options={k: [v] for k, v in options.items() if v})


# ─── the list ────────────────────────────────────────────────────────────────

class Browser:
    def __init__(self, shop: "Marketplace", tab: str = "connectors") -> None:
        self.shop = shop
        self.state = BrowserState(tab=tab if tab in catalog.TABS else "connectors")

    def run(self) -> None:
        while True:
            choice = self.pick()
            if choice is None:
                return
            self.open(choice)

    def subtitle(self) -> str:
        view = "[Yours] Discover" if self.state.yours else "Yours [Discover]"
        filters = []
        if self.state.category:
            filters.append(self.state.category)
        if self.state.free:
            filters.append("free only")
        return view + (f" · {', '.join(filters)}" if filters else "")

    def pick(self) -> Choice | None:
        tabs = list(catalog.TABS)
        selector = Selector(title="Marketplace", subtitle=self.subtitle(), tabs=[TAB_TITLES[t] for t in tabs],
                            tab_index=tabs.index(self.state.tab), items_for_tab=self.items_for_tab,
                            slash_filter=True, footer=FOOTER, on_key=self.on_key, max_visible=12)
        selector.index = max(0, min(self.state.index, len(selector.items) - 1))
        chosen = selector.run()
        self.state.index = selector.index
        return chosen.value if isinstance(chosen, SelectItem) else None

    def on_key(self, selector: Selector, key: str) -> bool:
        """y toggles Yours / Discover; everything else is the selector's own keys."""
        if selector.filterable or key not in ("y", "Y"):
            return False
        self.state.yours = not self.state.yours
        selector.subtitle = self.subtitle()
        selector.set_items(self.items_for_tab(selector.tab_index), keep_index=False)
        return True

    def items_for_tab(self, tab_index: int) -> list[SelectItem]:
        self.state.tab = catalog.TABS[tab_index]
        items = self.yours_items() if self.state.yours else self.discover_items()
        return items + self.option_items() + self.action_items()

    def discover_items(self) -> list[SelectItem]:
        tab = self.state.tab
        entries = catalog.filter_entries(self.shop.entries(tab), "", self.state.category, self.state.free)
        items = []
        for row, entry in zip(self.shop.rows_for(tab, entries), entries):
            facts = [row.publisher, row.category, "free" if row.free else "paid"]
            items.append(SelectItem(label=row.name, value=Choice("entry", tab, entry), badge=mark(row.installed),
                                    hint=" · ".join(f for f in facts if f),
                                    search=f"{row.id} {row.description}"))
        if not items:
            items.append(SelectItem(label="Nothing matches the filters", value=None))
        return items

    def yours_items(self) -> list[SelectItem]:
        tab = self.state.tab
        items = []
        for row in self.shop.yours_rows(tab):
            hint = " · ".join(f for f in (row.status, row.type_label) if f)
            items.append(SelectItem(label=row.name, value=Choice("installed", tab, row), badge=mark(True),
                                    hint=hint, search=row.id))
        if not items:
            items.append(SelectItem(label=f"No {tab} installed yet (press y for Discover)", value=None))
        return items

    def option_items(self) -> list[SelectItem]:
        if self.state.yours:
            return []
        category = self.state.category or "all"
        free = "on" if self.state.free else "off"
        return [SelectItem(label=f"Category: {category}", value=Choice("option", item="category"), group="Filters"),
                SelectItem(label=f"Free only: {free}", value=Choice("option", item="free"), group="Filters")]

    def action_items(self) -> list[SelectItem]:
        return [SelectItem(label=a.label, value=Choice("action", item=a), hint=a.hint, group="Actions")
                for a in self.list_actions()]

    def list_actions(self) -> list[Action]:
        shop = self.shop
        tab = self.state.tab
        actions = []
        if tab == "connectors":
            actions.append(Action("Add custom connector", self.add_custom, "URL or command, step by step"))
        if tab == "skills":
            actions.append(Action("New skill", self.new_skill, "write your own, step by step"))
        if tab == "plugins":
            actions.append(Action("New plugin", self.new_plugin, "start an empty plugin"))
        actions += [
            Action("Status of everything installed", lambda: control_cli.cmd_status(shop, make_args(), [])),
            Action("Automation", self.automation, "on / off / status"),
            Action("Export setup", self.export, "one JSON file"),
            Action("Import setup", self.import_setup, "from a JSON file"),
        ]
        return actions

    # ── open a row ───────────────────────────────────────────────────────────
    def open(self, choice: Choice | None) -> None:
        if choice is None:
            return
        if choice.kind == "option":
            self.change_option(choice.item)
        elif choice.kind == "action":
            run_action(choice.item)
        elif choice.kind == "entry":
            self.entry_panel(choice.tab, choice.item)
        elif choice.kind == "installed":
            self.installed_panel(choice.tab, choice.item)

    def change_option(self, name: str) -> None:
        if name == "free":
            self.state.free = not self.state.free
            return
        names = catalog.categories(self.shop.entries(self.state.tab))
        options = [("All categories", "", "")] + [(n, n, "") for n in names]
        picked = choose_value("Category", options, current=self.state.category)
        if picked is not None:
            self.state.category = picked

    def entry_panel(self, tab: str, entry: Any) -> None:
        """Details of a catalog item, then the actions valid for it."""
        installed = entry.id in self.shop.installed_ids(tab)
        row = self.shop._row(tab, entry, installed)
        self.shop.say(info_lines(row, self.shop.extra_facts(tab, entry)))
        if installed:
            self.item_actions(tab, entry.id, entry)
            return
        actions = [Action("Install", lambda: self.install(tab, entry), "guided"), Action("Back", lambda: None)]
        run_action(choose_action(entry.name, actions, subtitle="not installed"))

    def installed_panel(self, tab: str, row: Any) -> None:
        self.shop.run(["info", row.id, "--tab", tab])
        entry = catalog.find_entry(self.shop.entries(tab), row.id)
        self.item_actions(tab, row.id, entry)

    def item_actions(self, tab: str, name: str, entry: Any) -> None:
        run_action(choose_action(name, self.installed_actions(tab, name, entry), subtitle="installed"))

    def installed_actions(self, tab: str, name: str, entry: Any) -> list[Action]:
        shop = self.shop
        enabled = self.is_enabled(tab, name)
        actions = [Action("Test", lambda: shop.run(["test", name, "--tab", tab]), "health check")]
        if tab == "connectors":
            actions.append(Action("Tools", lambda: shop.run(["test", name, "--tab", tab]), "connect and list them"))
            actions.append(Action("Permissions", lambda: self.tool_permissions(name), "allow / ask / deny per tool"))
            actions.append(Action("Keys", lambda: self.keys(name, entry), "set a key or variable (hidden)"))
        actions.append(Action("Scope", lambda: self.move_scope(tab, name), "user / project / local"))
        if enabled:
            actions.append(Action("Disable", lambda: shop.run(["disable", name, "--tab", tab])))
        else:
            actions.append(Action("Enable", lambda: shop.run(["enable", name, "--tab", tab])))
        actions.append(Action("Remove", lambda: shop.run(["remove", name, "--tab", tab]),
                              confirm=f"Remove '{name}'?"))
        actions.append(Action("Back", lambda: None))
        return actions

    def is_enabled(self, tab: str, name: str) -> bool:
        for row in self.shop.yours_rows(tab):
            if row.id == name:
                return not row.status.startswith("disabled")
        return True

    # ── guided install ───────────────────────────────────────────────────────
    def install(self, tab: str, entry: Any) -> None:
        if tab == "connectors":
            self.install_connector(entry)
            return
        if getattr(entry, "kind", "") == "builtin":
            self.new_skill() if tab == "skills" else self.new_plugin()
            return
        scope = choose_value("Install where?", SCOPES[:2], current="user")
        if scope is None:
            return
        if tab == "skills":
            self.shop.install_skill(entry, make_args(scope=scope))
        else:
            self.shop.install_plugin(entry, make_args(scope=scope))

    def install_connector(self, entry: Any, name: str = "") -> None:
        """1 scope · 2 keys · 3 connect and check · 4 sign in · 5 next steps."""
        scope = choose_value(f"Install {entry.name}: where?", SCOPES, current="user")
        if scope is None:
            return
        env = self.ask_keys(entry)
        if env is None:
            return
        result = run_with_spinner(f"Connecting to {entry.name} and listing its tools...",
                                  lambda: installer.install_connector(
                                      entry, self.shop.workspace, scope=scope, name=name, env=env, keep=True,
                                      health_check=self.shop.health_check))
        name = result.name or name or entry.id
        if not result.ok and result.path is not None:
            if not self.after_failed_check(name, scope, result.message):
                return
        else:
            self.shop.report_install(result)
            if not result.ok:
                return
        if result.needs_login:
            self.offer_sign_in(name)
        if not result.already_installed and name not in self.shop.outcome.installed:
            self.shop.outcome.installed.append(name)
        self.next_steps(name)

    def ask_keys(self, entry: Any) -> dict[str, str] | None:
        """Hidden input for each key that is not already known. None when cancelled."""
        values: dict[str, str] = {}
        for var in entry.env_vars:
            if installer.known_value(var, {}):
                self.shop.say(f"  {var.name}: already set")
                continue
            need = "required" if var.required else "optional, Enter to skip"
            label = f"{var.name} ({need})" + (f" - {var.help}" if var.help else "")
            value = ask_text(label, secret=True)
            if value is None:
                return None
            if value:
                values[var.name] = value
        return values

    def after_failed_check(self, name: str, scope: str, message: str) -> bool:
        """Retry / Keep anyway / Cancel after a failed health check. True when the server stays."""
        while True:
            self.shop.say(f"  Problem: {message}")
            what = choose_value("The health check failed", [
                ("Retry", "retry", "connect again"),
                ("Keep anyway", "keep", "fix it later, then reconnect"),
                ("Cancel", "cancel", "remove what was just saved")])
            if what == "retry":
                done, tools = run_with_spinner("Connecting again...", lambda: control.probe_server(
                    self.shop.workspace, name, health_check=self.shop.health_check))
                if done.ok:
                    self.shop.say(f"  OK: {done.message}")
                    return True
                message = done.message
                continue
            if what == "keep":
                self.shop.say(f"  Kept '{name}'. Fix the problem, then use Test or /mcp reconnect {name}.")
                return True
            self.shop.say("  " + installer.remove_connector(name, self.shop.workspace, scope).message)
            return False

    def offer_sign_in(self, name: str) -> None:
        what = choose_value(f"'{name}' needs you to sign in", [
            ("Sign in now", "now", "opens the browser"), ("Later", "later", f"/mcp login {name}")])
        if what != "now":
            return
        agent = getattr(self.shop, "agent", None)
        if agent is None:
            self.shop.say(f"  Run: axon mcp login {name}")
            return
        from axon.mcp.commands import handle_mcp_command
        handle_mcp_command(agent, f"login {name}")

    def next_steps(self, name: str) -> None:
        actions = [
            Action("View tools", lambda: self.shop.run(["test", name, "--tab", "connectors"])),
            Action("Set tool permissions", lambda: self.tool_permissions(name)),
            Action("Back to the list", lambda: None),
        ]
        run_action(choose_action(f"'{name}' is installed", actions))

    # ── connector settings ───────────────────────────────────────────────────
    def tool_permissions(self, name: str) -> None:
        control_cli.list_tool_policies(self.shop, name)
        tool = ask_text("Tool name (Enter = every tool of this connector)", default="*")
        if tool is None:
            return
        policy = choose_value("Permission", [("allow", "allow", "run without asking"),
                                             ("ask", "ask", "ask every time"),
                                             ("deny", "deny", "never run"),
                                             ("reset", "reset", "remove the rule")])
        if policy:
            self.shop.run(["tool", name, tool, policy])

    def keys(self, name: str, entry: Any) -> None:
        known = [var.name for var in getattr(entry, "env_vars", [])]
        spec = self.shop.connector_specs().get(name)
        known += [k for k in (spec.env if spec else {}) if k not in known]
        options = [(k, k, "") for k in known] + [("Other variable...", "", "type its name")]
        var = choose_value(f"{name}: which key or variable?", options)
        if var is None:
            return
        if not var:
            var = ask_text("Variable name") or ""
            if not var:
                return
        value = ask_text(f"{var} (hidden)", secret=True)
        if value:
            self.shop.run(["env", name, f"{var}={value}"])

    def move_scope(self, tab: str, name: str) -> None:
        scopes = SCOPES if tab == "connectors" else SCOPES[:2]
        target = choose_value(f"Move '{name}' to", scopes)
        if target:
            self.shop.run(["scope", name, target, "--tab", tab])

    # ── list-wide actions ────────────────────────────────────────────────────
    def add_custom(self) -> None:
        from axon.marketplace.cli import catalog_name_from
        target = ask_text("URL (https://...) or command (npx -y package)")
        if not target:
            return
        name = ask_text("Name", default=catalog_name_from(target))
        if not name:
            return
        transport = ""
        if target.lower().startswith(("http://", "https://")):
            transport = choose_value("Transport", [("HTTP (streamable)", "http", "most servers"),
                                                   ("SSE", "sse", "older servers")]) or ""
            if not transport:
                return
        try:
            entry = installer.custom_connector(name, target, transport)
        except ValueError as e:
            self.shop.fail(str(e))
            return
        variable = ask_text("Key or variable it needs, as NAME (Enter if none)")
        if variable:
            entry.env_vars.append(catalog.EnvVar(variable, required=True))
        self.install_connector(entry, name)

    def new_skill(self) -> None:
        from axon.marketplace import skill_tools
        skill = self.shop.skill_from_args(make_args())
        self.shop.say(["", "  Preview of SKILL.md:", ""] +
                      [f"    {line}" for line in skill_tools.render_skill_md(skill).splitlines()] + [""])
        scope = choose_value("Save the skill where?", SCOPES[:2], current="user")
        if scope and confirm(f"Create skill '{skill.name}'?"):
            self.shop.create_skill(skill, scope)

    def new_plugin(self) -> None:
        name = ask_text("Plugin name")
        if not name:
            return
        scope = choose_value("Save the plugin where?", SCOPES[:2], current="user")
        if scope and confirm(f"Create plugin '{name}' ({scope})?"):
            self.shop.run(["create-plugin", name, "--scope", scope])

    def automation(self) -> None:
        what = choose_value("Automation", [("Status", "status", ""), ("Turn on", "on", ""), ("Turn off", "off", "")])
        if what:
            self.shop.run(["auto", what])

    def export(self) -> None:
        path = ask_text("Export to", default=str(self.shop.workspace / "axon-setup.json"))
        if path:
            self.shop.run(["export", path])

    def import_setup(self) -> None:
        path = ask_text("Import from (path to the JSON file)")
        if path:
            self.shop.run(["import", path])


def mark(installed: bool) -> str:
    from axon.marketplace.render import add_mark, installed_mark
    return installed_mark() if installed else add_mark()


def run_browser(shop: "Marketplace", tab: str = "connectors") -> None:
    Browser(shop, tab).run()
