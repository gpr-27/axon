"""
Project-based suggestions: look at the workspace and suggest catalog connectors that fit it.

Signals: a github.com git remote, dependencies in package.json / requirements.txt / pyproject.toml,
config files (vercel.json, render.yaml, wrangler.toml, prisma/schema.prisma, Sentry config ...) and
MongoDB connection strings in .env files. Only file names and dependency names are looked at; no
value of any .env file is read into a message.

Each project is told once (``~/.axon/state/connector-suggestions.json``). Nothing is installed unless
``automation.auto_install`` is on, and then only connectors that need nothing more from you.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from axon.marketplace import catalog, installer
from axon.mcp.config import MCPConfig
from axon.storage import project_key, read_state, write_state

STATE_NAME = "connector-suggestions"
ENV_FILES = (".env", ".env.example", ".env.local", ".env.development", ".env.sample")
MONGO_RE = re.compile(r"mongodb(\+srv)?://", re.IGNORECASE)
MAX_READ_BYTES = 200_000

NPM_PACKAGES = {
    "supabase": ("@supabase/supabase-js", "@supabase/ssr", "supabase"),
    "stripe": ("stripe", "@stripe/stripe-js"),
    "clerk": ("@clerk/nextjs", "@clerk/clerk-react", "@clerk/backend", "@clerk/express"),
    "prisma": ("prisma", "@prisma/client"),
    "sentry": ("@sentry/node", "@sentry/nextjs", "@sentry/react", "@sentry/browser"),
    "mongodb": ("mongodb", "mongoose"),
    "neon": ("@neondatabase/serverless",),
    "upstash": ("@upstash/redis", "@upstash/ratelimit"),
    "vercel": ("@vercel/analytics", "@vercel/blob", "@vercel/kv", "vercel"),
    "cloudflare-api": ("wrangler", "@cloudflare/workers-types"),
    "playwright": ("@playwright/test", "playwright"),
    "linear": ("@linear/sdk",),
    "notion": ("@notionhq/client",),
}
PYTHON_PACKAGES = {
    "supabase": ("supabase",),
    "stripe": ("stripe",),
    "sentry": ("sentry-sdk", "sentry_sdk"),
    "mongodb": ("pymongo", "motor"),
    "playwright": ("playwright",),
}
FILE_SIGNALS = {
    "vercel": ("vercel.json", ".vercel"),
    "render": ("render.yaml",),
    "cloudflare-api": ("wrangler.toml", "wrangler.json", "wrangler.jsonc"),
    "sentry": ("sentry.properties", ".sentryclirc", "sentry.client.config.ts", "sentry.server.config.ts",
               "sentry.client.config.js", "sentry.server.config.js"),
    "prisma": ("prisma/schema.prisma",),
    "supabase": ("supabase/config.toml",),
    "playwright": ("playwright.config.ts", "playwright.config.js"),
}


@dataclass
class Suggestion:
    connector_id: str
    reason: str


def read_text(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def has_github_remote(workspace: Path) -> bool:
    config = workspace / ".git" / "config"
    if not config.is_file():
        return False
    return "github.com" in read_text(config)


def npm_dependencies(workspace: Path) -> set[str]:
    try:
        data = json.loads(read_text(workspace / "package.json"))
    except ValueError:
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        section = data.get(key) if isinstance(data, dict) else None
        if isinstance(section, dict):
            names.update(str(name) for name in section)
    return names


def python_dependency_text(workspace: Path) -> str:
    return (read_text(workspace / "requirements.txt") + "\n" + read_text(workspace / "pyproject.toml")).lower()


def python_has(text: str, package: str) -> bool:
    return re.search(rf"(?<![a-z0-9_-]){re.escape(package)}(?![a-z0-9_-])", text) is not None


def mongo_uri_in_env(workspace: Path) -> str:
    for name in ENV_FILES:
        if MONGO_RE.search(read_text(workspace / name)):
            return name
    return ""


def detect_signals(workspace: Path) -> list[Suggestion]:
    """Every fitting connector id with the reason, in a stable order, without duplicates."""
    workspace = Path(workspace)
    found: dict[str, str] = {}

    def add(connector_id: str, reason: str) -> None:
        found.setdefault(connector_id, reason)

    if has_github_remote(workspace):
        add("github", "the git remote is on github.com")
    npm = npm_dependencies(workspace)
    for connector_id, packages in NPM_PACKAGES.items():
        hit = next((p for p in packages if p in npm), "")
        if hit:
            add(connector_id, f"package.json depends on {hit}")
    python_text = python_dependency_text(workspace)
    for connector_id, packages in PYTHON_PACKAGES.items():
        hit = next((p for p in packages if python_has(python_text, p)), "")
        if hit:
            add(connector_id, f"the Python requirements mention {hit}")
    for connector_id, names in FILE_SIGNALS.items():
        hit = next((n for n in names if (workspace / n).exists()), "")
        if hit:
            add(connector_id, f"found {hit}")
    env_file = mongo_uri_in_env(workspace)
    if env_file:
        add("mongodb", f"{env_file} has a MongoDB connection string")
    return [Suggestion(connector_id, reason) for connector_id, reason in found.items()]


def fitting_connectors(workspace: Path, data_dir: Path = catalog.DATA_DIR) -> list[tuple[catalog.Connector, str]]:
    """Catalog connectors that fit the workspace and are not installed yet."""
    known = {c.id: c for c in catalog.load_connectors(data_dir)}
    installed = set(MCPConfig(workspace).load_specs())
    return [(known[s.connector_id], s.reason) for s in detect_signals(workspace)
            if s.connector_id in known and s.connector_id not in installed]


# ─── once per project ────────────────────────────────────────────────────────

def already_told(workspace: Path) -> set[str]:
    state = read_state(STATE_NAME, {})
    entry = state.get(project_key(workspace)) if isinstance(state, dict) else None
    return set(entry.get("suggested", [])) if isinstance(entry, dict) else set()


def remember(workspace: Path, ids: list[str]) -> None:
    state = read_state(STATE_NAME, {})
    state = state if isinstance(state, dict) else {}
    key = project_key(workspace)
    before = state.get(key, {}).get("suggested", []) if isinstance(state.get(key), dict) else []
    state[key] = {"suggested": sorted(set(before) | set(ids)), "at": int(time.time())}
    write_state(STATE_NAME, state)


def forget(workspace: Path) -> None:
    state = read_state(STATE_NAME, {})
    if isinstance(state, dict) and state.pop(project_key(workspace), None) is not None:
        write_state(STATE_NAME, state)


def can_install_unattended(connector: catalog.Connector) -> bool:
    """True when every required key is already available, so no question needs to be asked."""
    return all(installer.known_value(var, {}) for var in connector.env_vars if var.required)


def auto_install(workspace: Path, connectors: list[catalog.Connector],
                 health_check: installer.HealthCheck = installer.check_server) -> list[str]:
    """Install the connectors that can be installed unattended (local scope). Returns their ids."""
    done = []
    for connector in connectors:
        if not can_install_unattended(connector):
            continue
        result = installer.install_connector(connector, workspace, scope="local", health_check=health_check)
        if result.ok and not result.already_installed:
            done.append(connector.id)
    return done


def suggestion_notice(connectors: list[catalog.Connector]) -> str:
    names = ", ".join(c.id for c in connectors)
    return f"This project could use: {names}. See /marketplace suggest, then /marketplace install <id>."


def run_once(workspace: Path, auto_install_on: bool, post: Callable[[str], None],
             health_check: installer.HealthCheck = installer.check_server) -> list[str]:
    """Tell the user about new fitting connectors (once per project). Returns the ids it announced."""
    told = already_told(workspace)
    fresh = [(c, why) for c, why in fitting_connectors(workspace) if c.id not in told]
    if not fresh:
        return []
    connectors = [c for c, _why in fresh]
    remember(workspace, [c.id for c in connectors])
    installed = auto_install(workspace, connectors, health_check) if auto_install_on else []
    if installed:
        post(f"Installed for this project: {', '.join(installed)} (automation.auto_install). "
             "Undo with /marketplace remove <id>.")
    left = [c for c in connectors if c.id not in installed]
    if left:
        post(suggestion_notice(left))
    return [c.id for c in connectors]


def suggestion_lines(workspace: Path) -> list[str]:
    """For ``/marketplace suggest``: what fits this project, with the reason for each."""
    fitting = fitting_connectors(workspace)
    if not fitting:
        return ["  No connector suggestions for this project (or the fitting ones are already installed)."]
    lines = ["  Connectors that fit this project:"]
    for connector, reason in fitting:
        lines.append(f"    {connector.id:<16}{connector.name} - {reason}")
        lines.append(f"    {'':<16}install: /marketplace install {connector.id}")
    return lines
