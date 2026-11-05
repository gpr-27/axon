"""
The marketplace catalog: three curated lists (connectors, skills, plugins) stored as JSON files.

Each list is loaded into small dataclasses and checked against a simple schema, so a typo in the
data files is caught by the tests instead of by a user. Nothing here touches the network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DATA_DIR = Path(__file__).parent / "data"
TABS = ("connectors", "skills", "plugins")
TRANSPORTS = ("http", "sse", "stdio")
AUTH_KINDS = ("none", "oauth", "api_key")


class CatalogError(ValueError):
    """Raised when a catalog file is missing or malformed."""


@dataclass
class EnvVar:
    """One environment variable a connector needs (for example an API key)."""
    name: str
    help: str = ""
    required: bool = True


@dataclass
class Connector:
    """An MCP server a user can install."""
    id: str
    name: str
    publisher: str
    description: str
    category: str
    transport: str
    auth: str
    docs_url: str
    url: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    env_vars: list[EnvVar] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    verified: bool = False
    free: bool = True
    free_note: str = ""

    @property
    def remote(self) -> bool:
        return self.transport != "stdio"

    @property
    def type_label(self) -> str:
        """'Web' for remote servers, 'Local' for servers started on this machine."""
        return "Web" if self.remote else "Local"


@dataclass
class SkillEntry:
    """A skill a user can install (or the built-in skill creator)."""
    id: str
    name: str
    publisher: str
    description: str
    category: str
    kind: str                 # "github" or "builtin"
    source: str = ""
    docs_url: str = ""
    license: str = ""
    verified: bool = False
    note: str = ""


@dataclass
class PluginEntry:
    """A plugin a user can install (or the built-in plugin creator)."""
    id: str
    name: str
    publisher: str
    description: str
    category: str
    kind: str                 # "git" or "builtin"
    source: dict[str, Any] = field(default_factory=dict)
    docs_url: str = ""
    verified: bool = False


# ─── Validation ──────────────────────────────────────────────────────────────

def _missing_text_fields(raw: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    problems = []
    for name in names:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing or empty '{name}'")
    return problems


def _url_problem(url: str, label: str, must_be_https: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"{label} is not a valid URL: {url!r}"
    if must_be_https and parsed.scheme != "https":
        return f"{label} must use https: {url!r}"
    return ""


def _env_var_problems(raw: dict[str, Any]) -> list[str]:
    problems = []
    for item in raw.get("env_vars", []):
        if not isinstance(item, dict) or not item.get("name"):
            problems.append("every env_vars item needs a 'name'")
    return problems


def connector_problems(raw: dict[str, Any]) -> list[str]:
    """List what is wrong with one connector entry (empty list means it is valid)."""
    problems = _missing_text_fields(
        raw, ("id", "name", "publisher", "description", "category", "transport", "auth", "docs_url"))
    transport = raw.get("transport")
    if transport not in TRANSPORTS:
        problems.append(f"transport must be one of {TRANSPORTS}")
    if raw.get("auth") not in AUTH_KINDS:
        problems.append(f"auth must be one of {AUTH_KINDS}")
    if raw.get("docs_url"):
        problems.append(_url_problem(raw["docs_url"], "docs_url", must_be_https=True))
    if transport in ("http", "sse"):
        problems.append(_url_problem(str(raw.get("url", "")), "url", must_be_https=True))
    if transport == "stdio" and not raw.get("command"):
        problems.append("stdio connector needs a 'command'")
    if raw.get("auth") == "api_key" and not raw.get("env_vars"):
        problems.append("api_key auth needs at least one env_vars entry")
    if not isinstance(raw.get("free"), bool):
        problems.append("'free' must be true or false")
    if not isinstance(raw.get("verified"), bool):
        problems.append("'verified' must be true or false")
    problems.extend(_env_var_problems(raw))
    return [p for p in problems if p]


def skill_problems(raw: dict[str, Any]) -> list[str]:
    problems = _missing_text_fields(raw, ("id", "name", "publisher", "description", "category", "kind"))
    if raw.get("kind") not in ("github", "builtin"):
        problems.append("kind must be 'github' or 'builtin'")
    if raw.get("kind") == "github" and not raw.get("source"):
        problems.append("github skill needs a 'source'")
    if raw.get("docs_url"):
        problems.append(_url_problem(raw["docs_url"], "docs_url", must_be_https=True))
    return [p for p in problems if p]


def plugin_problems(raw: dict[str, Any]) -> list[str]:
    problems = _missing_text_fields(raw, ("id", "name", "publisher", "description", "category", "kind"))
    if raw.get("kind") not in ("git", "builtin"):
        problems.append("kind must be 'git' or 'builtin'")
    if raw.get("kind") == "git":
        source = raw.get("source")
        if not isinstance(source, dict) or not source.get("url") or not source.get("path"):
            problems.append("git plugin needs source.url and source.path")
        else:
            problems.append(_url_problem(source["url"], "source.url", must_be_https=True))
    if raw.get("docs_url"):
        problems.append(_url_problem(raw["docs_url"], "docs_url", must_be_https=True))
    return [p for p in problems if p]


# ─── Loading ─────────────────────────────────────────────────────────────────

def _read_list(file_name: str, key: str, data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / file_name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CatalogError(f"cannot read {path}: {e}") from e
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise CatalogError(f"{path} must contain a '{key}' list")
    return items


def _check_all(items: list[dict[str, Any]], checker: Any, label: str) -> None:
    """Raise one CatalogError listing every problem, including duplicate ids."""
    problems: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        item_id = str(raw.get("id") or f"#{index}")
        for problem in checker(raw):
            problems.append(f"{label} {item_id}: {problem}")
        if item_id in seen:
            problems.append(f"{label} {item_id}: duplicate id")
        seen.add(item_id)
    if problems:
        raise CatalogError("; ".join(problems))


def _connector_from_dict(raw: dict[str, Any]) -> Connector:
    env_vars = [EnvVar(v["name"], v.get("help", ""), v.get("required", True)) for v in raw.get("env_vars", [])]
    return Connector(
        id=raw["id"], name=raw["name"], publisher=raw["publisher"], description=raw["description"],
        category=raw["category"], transport=raw["transport"], auth=raw["auth"], docs_url=raw["docs_url"],
        url=raw.get("url", ""), command=raw.get("command", ""), args=list(raw.get("args", [])),
        env_vars=env_vars, headers=dict(raw.get("headers", {})), verified=raw["verified"],
        free=raw["free"], free_note=raw.get("free_note", ""))


def load_connectors(data_dir: Path = DATA_DIR) -> list[Connector]:
    items = _read_list("connectors.json", "connectors", data_dir)
    _check_all(items, connector_problems, "connector")
    return [_connector_from_dict(raw) for raw in items]


def load_skills(data_dir: Path = DATA_DIR) -> list[SkillEntry]:
    items = _read_list("skills.json", "skills", data_dir)
    _check_all(items, skill_problems, "skill")
    return [SkillEntry(**{k: v for k, v in raw.items() if k in SkillEntry.__dataclass_fields__}) for raw in items]


def load_plugins(data_dir: Path = DATA_DIR) -> list[PluginEntry]:
    items = _read_list("plugins.json", "plugins", data_dir)
    _check_all(items, plugin_problems, "plugin")
    return [PluginEntry(**{k: v for k, v in raw.items() if k in PluginEntry.__dataclass_fields__}) for raw in items]


def load_tab(tab: str, data_dir: Path = DATA_DIR) -> list[Any]:
    """Load one tab's entries by name: 'connectors', 'skills' or 'plugins'."""
    if tab == "connectors":
        return load_connectors(data_dir)
    if tab == "skills":
        return load_skills(data_dir)
    if tab == "plugins":
        return load_plugins(data_dir)
    raise CatalogError(f"unknown tab {tab!r}; use one of {TABS}")


# ─── Search and filter ───────────────────────────────────────────────────────

def _search_text(entry: Any) -> str:
    parts = [entry.id, entry.name, entry.publisher, entry.description, entry.category]
    return " ".join(parts).lower()


def matches_query(entry: Any, query: str) -> bool:
    """True when every word of the query appears somewhere in the entry's text."""
    text = _search_text(entry)
    for word in query.lower().split():
        if word not in text:
            return False
    return True


def filter_entries(entries: list[Any], query: str = "", category: str = "",
                   free_only: bool = False, installed_ids: set[str] | None = None,
                   installed: bool | None = None) -> list[Any]:
    """Apply the search box and the filters. ``installed`` True keeps only installed entries,
    False keeps only the ones not yet installed, None ignores installation state."""
    result = []
    for entry in entries:
        if query and not matches_query(entry, query):
            continue
        if category and entry.category.lower() != category.lower():
            continue
        if free_only and not getattr(entry, "free", True):
            continue
        if installed is not None and installed_ids is not None:
            is_installed = entry.id in installed_ids
            if is_installed != installed:
                continue
        result.append(entry)
    return result


def categories(entries: list[Any]) -> list[str]:
    return sorted({entry.category for entry in entries})


def find_entry(entries: list[Any], entry_id: str) -> Any | None:
    for entry in entries:
        if entry.id.lower() == entry_id.lower():
            return entry
    return None
