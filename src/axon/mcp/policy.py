"""
MCP access policy: allowlists, denylists, managed servers and exclusive control.

Settings keys (any settings.json layer; ``allowManagedMcpServersOnly`` and ``managedMcpServers`` only from
the managed tier):

    allowedMcpServers   [{"serverUrl": "https://*.example.com/*"}, {"serverCommand": ["npx", "-y", "pkg"]},
                         {"serverName": "github"}]       unset = everything allowed, [] = nothing allowed
    deniedMcpServers    same entry shapes; always wins
    allowManagedMcpServersOnly   managed allowlist only (user/project allowlists are ignored)
    managedMcpServers   {"name": {"type": "http", "url": "https://..."}}  servers the organization provides
    enableAllProjectMcpServers / enabledMcpjsonServers / disabledMcpjsonServers
                        approval of servers defined in the project's .mcp.json

Managed tier files (the first one found per kind wins; ``AXON_MANAGED_DIR`` overrides the directory list):

    managed-settings.json   the settings above, deployed by an administrator
    managed-mcp.json        exclusive control: ONLY these servers (plus managedMcpServers) may load

Evaluation order for a server: denylist, then allowlist (servers the organization provides skip it).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME_OK = re.compile(r"^[A-Za-z0-9_-]+$")
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def managed_dirs() -> list[Path]:
    override = os.environ.get("AXON_MANAGED_DIR", "").strip()
    if override:
        return [Path(override)]
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/Axon"), Path("/Library/Application Support/ClaudeCode")]
    if os.name == "nt":
        base = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [Path(base) / "Axon", Path(base) / "ClaudeCode"]
    return [Path("/etc/axon"), Path("/etc/claude-code")]


def _first_file(name: str) -> Path | None:
    for d in managed_dirs():
        p = d / name
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _project_settings_files(workspace: Path) -> list[tuple[str, Path]]:
    ws = Path(workspace)
    return [(label, ws / rel) for label, rel in (
        ("project", ".claude/settings.json"), ("project", ".axon/settings.json"),
        ("local", ".claude/settings.local.json"), ("local", ".axon/settings.local.json"))
        if (ws / rel).is_file()]


def settings_layers(workspace: Path, *, include_project: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """(label, settings dict) lowest to highest: user, project (shared), project (local), --settings."""
    from axon.mcp import state
    from axon.storage import user_file
    layers: list[tuple[str, dict[str, Any]]] = [("user", _read_json(user_file("settings.json")))]
    if include_project:
        layers += [(label, _read_json(p)) for label, p in _project_settings_files(workspace)]
    if state.cli_settings():
        layers.append(("cli", state.cli_settings()))
    return layers


# ─── folder trust, as MCP needs it ──────────────────────────────────────────

def _home_project_entry(workspace: Path | str) -> dict[str, Any]:
    from axon.hooks import trust
    from axon.storage import user_file
    projects = _read_json(user_file("mcp.json")).get("projects")
    entry = projects.get(trust._key(workspace)) if isinstance(projects, dict) else None
    return entry if isinstance(entry, dict) else {}


def folder_trusted(workspace: Path | str) -> bool:
    """You trusted exactly this folder: accepted the trust dialog (``/trust``), or set
    ``projects["<path>"].hasTrustDialogAccepted`` in ~/.axon/mcp.json. A parent folder's trust does not count,
    and neither does the way a ``-p`` run loads project hooks without asking."""
    from axon.hooks import trust
    if _home_project_entry(workspace).get("hasTrustDialogAccepted") is True:
        return True
    try:
        return trust.status(workspace) == "trusted"
    except Exception:  # noqa: BLE001 - not knowing means not trusted
        return False


def set_folder_trusted(workspace: Path | str) -> None:
    """Record the answer to the trust question asked before a project's headersHelper first runs."""
    from axon.hooks import trust
    from axon.session.store import atomic_write_text
    from axon.storage import user_file
    path = user_file("mcp.json")
    data = _read_json(path)
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = data["projects"] = {}
    entry = projects.get(trust._key(workspace))
    if not isinstance(entry, dict):
        entry = projects[trust._key(workspace)] = {}
    entry["hasTrustDialogAccepted"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _approval_layers(workspace: Path) -> list[tuple[str, dict[str, Any]]]:
    """Settings that may approve a .mcp.json server, and their label: your user settings, your local settings
    and ``--settings`` approve ("yours"); the committed project settings can only reject."""
    layers: list[tuple[str, dict[str, Any]]] = []
    for label, data in settings_layers(workspace, include_project=True):
        layers.append(("reject-only" if label == "project" else "yours", data))
    return layers


def _expand(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            return m.group(2) if m.group(2) is not None else m.group(0)
        return val
    return _VAR_RE.sub(sub, text)


def _wild(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile("^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$", flags | re.DOTALL)


def url_matches(pattern: str, url: str) -> bool:
    """``*``-wildcard URL pattern match (scheme/host case-insensitive, default-port rules from the docs)."""
    pattern = _expand(pattern.strip())
    url = _expand(url.strip())
    if "://" not in pattern or "://" not in url:
        return False
    p_scheme, _, p_rest = pattern.partition("://")
    p_netloc, slash, p_path = p_rest.partition("/")
    p_path = slash + p_path
    u = urlsplit(url)
    u_scheme = (u.scheme or "").lower()
    if not _wild(p_scheme.lower()).match(u_scheme):
        return False
    p_host, colon, p_port = p_netloc.rpartition(":")
    if not colon or not (p_port.isdigit() or p_port == "*"):
        p_host, p_port = p_netloc, ""
    p_host = p_host.lower().rstrip(".")
    u_host = (u.hostname or "").lower().rstrip(".")
    if not _wild(p_host).match(u_host):
        return False
    u_port = u.port or _DEFAULT_PORTS.get(u_scheme)
    if p_port == "*":
        pass
    elif p_port:
        if int(p_port) != u_port:
            return False
    elif "*" not in p_host and u.port is not None and u.port != _DEFAULT_PORTS.get(u_scheme):
        return False  # a fully written host with no port means the scheme's default port only
    if p_path == "":
        return True
    return bool(_wild(p_path).match(u.path or "/"))


def _command_of(spec: Any) -> list[str]:
    return [spec.command, *spec.args]


def _entry_matches(entry: dict[str, Any], spec: Any) -> bool:
    if "serverName" in entry:
        return str(entry["serverName"]) == spec.name
    if "serverUrl" in entry:
        return bool(spec.url) and url_matches(str(entry["serverUrl"]), spec.url)
    cmd = entry.get("serverCommand")
    if isinstance(cmd, list) and spec.type == "stdio":
        want = [str(c) for c in cmd]
        have = _command_of(spec)
        return want == have or [_expand(c) for c in want] == [_expand(c) for c in have]
    return False


def _valid_entries(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    out = []
    for e in value:
        if isinstance(e, dict) and len(e) == 1 and next(iter(e)) in ("serverName", "serverUrl", "serverCommand"):
            out.append(e)
    return out


# ─── the policy object ──────────────────────────────────────────────────────

@dataclass
class McpPolicy:
    allowed: list[dict[str, Any]] | None = None
    denied: list[dict[str, Any]] = field(default_factory=list)
    managed_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    exclusive: dict[str, dict[str, Any]] | None = None     # managed-mcp.json servers; None = no such file
    notices: list[str] = field(default_factory=list)
    plugin_only: bool = False

    @property
    def has_rules(self) -> bool:
        return self.allowed is not None or bool(self.denied)

    def check(self, spec: Any) -> str | None:
        """None when the server may load; otherwise 'denied' or 'not-allowed'."""
        if any(_entry_matches(e, spec) for e in self.denied):
            return "denied"
        if self.allowed is None or (getattr(spec, "managed", False) and not _uses_expansion(spec)):
            return None
        remote = spec.type != "stdio"
        key = "serverUrl" if remote else "serverCommand"
        typed = [e for e in self.allowed if key in e]
        if typed:
            return None if any(_entry_matches(e, spec) for e in typed) else "not-allowed"
        named = [e for e in self.allowed if "serverName" in e]
        return None if any(_entry_matches(e, spec) for e in named) else "not-allowed"

    def reason(self, spec: Any) -> str | None:
        verdict = self.check(spec)
        if verdict == "denied":
            return f'Cannot add MCP server "{spec.name}": server is explicitly blocked by enterprise policy'
        if verdict == "not-allowed":
            return f'Cannot add MCP server "{spec.name}": not allowed by enterprise policy'
        return None


def _uses_expansion(spec: Any) -> bool:
    """managed-mcp.json servers that use ${VAR} outside env are still checked against the allowlist."""
    texts = [spec.command, spec.url, *spec.args, *spec.headers.values()]
    return any(_VAR_RE.search(t or "") for t in texts)


def _validate_managed_servers(raw: Any, notices: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        if raw is not None:
            notices.append("managedMcpServers must be an object keyed by server name; ignored")
        return out
    for name, entry in raw.items():
        problem = None
        if not isinstance(entry, dict):
            problem = "entry is not an object"
        elif not _NAME_OK.match(str(name)):
            problem = "name may contain only letters, numbers, hyphens and underscores"
        else:
            kind = str(entry.get("type") or "").lower()
            if kind == "streamable-http":
                kind = "http"
            url = str(entry.get("url") or "")
            if kind not in ("http", "sse"):
                problem = "type must be http or sse"
            elif not url.startswith("https://"):
                problem = "url must be an https:// URL"
            elif any(k in entry for k in ("command", "args", "env", "headersHelper")):
                problem = "command, args, env and headersHelper are not allowed"
            elif _VAR_RE.search(json.dumps(entry)):
                problem = "environment variable references are not expanded in managed entries"
        if problem:
            notices.append(f"managedMcpServers: dropped '{name}' ({problem})")
            continue
        out[str(name)] = dict(entry)
    return out


def load_policy(workspace: Path) -> McpPolicy:
    pol = McpPolicy()
    layers = settings_layers(workspace)
    managed_path = _first_file("managed-settings.json")
    managed = _read_json(managed_path) if managed_path else {}
    managed_only = bool(managed.get("allowManagedMcpServersOnly"))

    allowed: list[dict[str, Any]] | None = None
    sources = [("managed", managed)] + ([] if managed_only else layers)
    for _label, data in sources:
        entries = _valid_entries(data.get("allowedMcpServers"))
        if entries is not None:
            allowed = (allowed or []) + entries
    pol.allowed = allowed
    for _label, data in [*layers, ("managed", managed)]:
        pol.denied.extend(_valid_entries(data.get("deniedMcpServers")) or [])
    pol.managed_servers = _validate_managed_servers(managed.get("managedMcpServers"), pol.notices)
    strict = managed.get("strictPluginOnlyCustomization")
    pol.plugin_only = strict is True or (isinstance(strict, list) and "mcp" in strict)

    mcp_file = _first_file("managed-mcp.json")
    if mcp_file is not None:
        data = _read_json(mcp_file)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            pol.exclusive = {str(k): dict(v) for k, v in servers.items() if isinstance(v, dict)}
        else:
            pol.exclusive = {}
            pol.notices.append(f"MCP config diagnostics: {mcp_file} could not be parsed; no MCP servers will load")
    return pol


# ─── .mcp.json approvals from settings ──────────────────────────────────────

def settings_approval(workspace: Path, name: str) -> str | None:
    """'approved' / 'rejected' / None from settings keys. Only your own settings can approve a server
    (a cloned repo's committed settings cannot approve its own .mcp.json); any layer can reject."""
    approved = False
    for label, data in _approval_layers(workspace):
        if name in (data.get("disabledMcpjsonServers") or []):
            return "rejected"
        if label == "yours":
            if data.get("enableAllProjectMcpServers") or name in (data.get("enabledMcpjsonServers") or []):
                approved = True
    managed_path = _first_file("managed-settings.json")
    if managed_path:
        data = _read_json(managed_path)
        if name in (data.get("disabledMcpjsonServers") or []):
            return "rejected"
        if data.get("enableAllProjectMcpServers") or name in (data.get("enabledMcpjsonServers") or []):
            approved = True
    return "approved" if approved else None


def cli_conflict(workspace: Path, *, cli_servers: bool, strict: bool) -> str | None:
    """Startup error when ``--mcp-config`` / ``--strict-mcp-config`` meet an exclusive managed-mcp.json."""
    if not (cli_servers or strict):
        return None
    if load_policy(workspace).exclusive is None:
        return None
    if cli_servers:
        return "You cannot dynamically configure MCP servers when an enterprise MCP config is present"
    return "--strict-mcp-config cannot replace the servers of an enterprise MCP config (managed-mcp.json)"
