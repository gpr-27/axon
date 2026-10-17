"""
MCP server configuration: discovery, merging, variable expansion and persistence.

Config files (later entries override earlier ones, per server name):
  0. .mcp.json of every enabled plugin  scope "plugin" (lowest; ${AXON_PLUGIN_ROOT} /
     ${CLAUDE_PLUGIN_ROOT} expand to the plugin directory)
  1. ~/.axon/mcp.json             scope "user"    (alias "global")
  2. <workspace>/.mcp.json        scope "project" (Claude Code compatible)
  3. <workspace>/.axon/mcp.json   scope "local"

Every file uses the Claude Code layout: {"mcpServers": {"<name>": {...spec...}}}.
A spec is {"type": "stdio"|"http"|"sse", "command", "args", "env", "cwd", "url",
"headers", "timeout", "disabled"}. ${VAR} and ${VAR:-default} are expanded in
command/args/env/cwd/url/headers at connect time (the file keeps the reference).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.mcp.catalog import get_mcp_preset

SCOPES = ("user", "project", "local")
_SCOPE_ALIASES = {"global": "user", "user": "user", "project": "project", "local": "local",
                  "workspace": "local"}
DEFAULT_TIMEOUT = 60.0

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PLACEHOLDER_RE = re.compile(r"^(your_.*|xoxb-\.\.\.|T\.\.\.|<.*>|\.\.\.)$", re.IGNORECASE)


def normalize_scope(scope: str | None) -> str:
    s = _SCOPE_ALIASES.get((scope or "user").strip().lower())
    if s is None:
        raise ValueError(f"Unknown scope '{scope}'. Use one of: user, project, local.")
    return s


def expand_vars(value: str, env: dict[str, str] | None = None) -> str:
    """Expand ${VAR} and ${VAR:-default}. Unknown variables without a default become ''."""
    source = os.environ if env is None else env

    def _sub(m: re.Match[str]) -> str:
        val = source.get(m.group(1))
        if val is None or (val == "" and m.group(2) is not None):
            return m.group(2) if m.group(2) is not None else ""
        return val

    return _VAR_RE.sub(_sub, value)


def missing_vars(value: str, env: dict[str, str] | None = None) -> list[str]:
    """Names of ${VAR} references (without a default) that are not set."""
    source = os.environ if env is None else env
    return [m.group(1) for m in _VAR_RE.finditer(value)
            if m.group(2) is None and not source.get(m.group(1))]


def is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(str(value).strip()))


def infer_type(raw: dict[str, Any]) -> str:
    t = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    if t in ("http", "streamable-http", "streamable_http", "streamablehttp"):
        return "http"
    if t == "sse":
        return "sse"
    if t == "stdio":
        return "stdio"
    if raw.get("url") or raw.get("serverUrl"):
        return "http"
    return "stdio"


@dataclass
class ServerSpec:
    name: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    disabled: bool = False
    scope: str = "user"
    source: Path | None = None

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any], scope: str = "user",
                  source: Path | None = None) -> "ServerSpec":
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server '{name}' spec must be a JSON object")
        timeout = raw.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        if timeout > 1000:  # Claude-Code style milliseconds
            timeout = timeout / 1000.0
        args = raw.get("args") or []
        if isinstance(args, str):
            args = [args]
        elif not isinstance(args, list):
            raise ValueError(f"MCP server '{name}': 'args' must be a list")
        for key in ("env", "headers"):
            if raw.get(key) is not None and not isinstance(raw.get(key), dict):
                raise ValueError(f"MCP server '{name}': '{key}' must be an object")
        return cls(
            name=name,
            type=infer_type(raw),
            command=str(raw.get("command") or ""),
            args=[str(a) for a in args],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            url=str(raw.get("url") or raw.get("serverUrl") or ""),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            timeout=timeout if timeout > 0 else DEFAULT_TIMEOUT,
            disabled=bool(raw.get("disabled", False)),
            scope=scope,
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.type == "stdio":
            out["command"] = self.command
            out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
            if self.cwd:
                out["cwd"] = self.cwd
        else:
            out["type"] = self.type
            out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
        if self.timeout != DEFAULT_TIMEOUT:
            out["timeout"] = self.timeout
        if self.disabled:
            out["disabled"] = True
        return out

    def validate(self) -> str | None:
        """Return a human-readable problem, or None when the spec is usable."""
        if self.type == "stdio" and not self.command:
            return "stdio server has no 'command'"
        if self.type in ("http", "sse") and not self.url:
            return f"{self.type} server has no 'url'"
        return None

    def resolved(self) -> "ServerSpec":
        """Copy with ${VAR} references expanded."""
        env = {k: expand_vars(v) for k, v in self.env.items()}
        lookup = dict(os.environ)
        lookup.update(env)
        return ServerSpec(
            name=self.name, type=self.type,
            command=expand_vars(self.command, lookup),
            args=[expand_vars(a, lookup) for a in self.args],
            env=env,
            cwd=expand_vars(self.cwd, lookup) if self.cwd else None,
            url=expand_vars(self.url, lookup),
            headers={k: expand_vars(v, lookup) for k, v in self.headers.items()},
            timeout=self.timeout, disabled=self.disabled, scope=self.scope, source=self.source,
        )

    def unresolved_problems(self) -> list[str]:
        """Missing ${VAR}s and leftover catalog placeholders that would make the server fail."""
        problems: list[str] = []
        for k, v in list(self.env.items()) + list(self.headers.items()):
            if is_placeholder(v):
                problems.append(f"{k} is a placeholder ('{v}')")
        for v in self.env.values():
            for var in missing_vars(v):
                problems.append(f"${{{var}}} is not set")
        # command/args/url/headers may reference the server's own env (resolved() expands them that way)
        lookup = dict(os.environ)
        lookup.update({k: expand_vars(v) for k, v in self.env.items()})
        for text in [self.command, self.url, *self.args, *(self.headers.values())]:
            for var in missing_vars(text, lookup):
                problems.append(f"${{{var}}} is not set")
        return sorted(set(problems))

    def describe_target(self) -> str:
        if self.type == "stdio":
            return " ".join([self.command, *self.args]).strip()
        return self.url


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _servers_in(data: dict[str, Any]) -> dict[str, Any]:
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = data.get("servers")  # VS Code style
    return servers if isinstance(servers, dict) else {}


class MCPConfig:
    """Reads and writes MCP server definitions across user/project/local scopes."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        from axon.storage import user_file
        self.global_config_file = user_file("mcp.json")
        self.project_config_file = self.workspace / ".mcp.json"
        self.local_config_file = self.workspace / ".axon" / "mcp.json"

    # ── Paths ────────────────────────────────────────────────────────────
    def path_for(self, scope: str) -> Path:
        s = normalize_scope(scope)
        return {"user": self.global_config_file, "project": self.project_config_file,
                "local": self.local_config_file}[s]

    def config_files(self) -> list[tuple[str, Path]]:
        return [("user", self.global_config_file), ("project", self.project_config_file),
                ("local", self.local_config_file)]

    # ── Loading ──────────────────────────────────────────────────────────
    def plugin_servers(self) -> list[tuple[Path, str, dict[str, Any]]]:
        """(source path, server name, raw spec) contributed by enabled plugins."""
        try:
            from axon.commands.plugins import enabled_plugins, plugin_mcp_servers
            plugins = enabled_plugins(self.workspace)
        except Exception:  # noqa: BLE001 - plugins are optional
            return []
        out: list[tuple[Path, str, dict[str, Any]]] = []
        for plug in plugins:
            for name, raw in plugin_mcp_servers(plug).items():
                out.append((plug.path / ".mcp.json", name, raw))
        return out

    def load_specs(self, include_disabled: bool = True) -> dict[str, ServerSpec]:
        merged: dict[str, ServerSpec] = {}
        for path, name, raw in self.plugin_servers():
            try:
                merged[name] = ServerSpec.from_dict(name, raw, "plugin", path)
            except ValueError:
                continue
        for scope, path in self.config_files():
            if not path.is_file():
                continue
            for name, raw in _servers_in(_read_json(path)).items():
                try:
                    merged[str(name)] = ServerSpec.from_dict(str(name), raw, scope, path)
                except ValueError:
                    continue
        if not include_disabled:
            merged = {k: v for k, v in merged.items() if not v.disabled}
        return merged

    def get_spec(self, name: str) -> ServerSpec | None:
        return self.load_specs().get(name)

    def get_all_servers(self) -> dict[str, dict[str, Any]]:
        """Merged raw specs (backward compatible dict view)."""
        out: dict[str, dict[str, Any]] = {name: dict(raw) for _p, name, raw in self.plugin_servers()}
        for scope, path in self.config_files():
            if path.is_file():
                for name, raw in _servers_in(_read_json(path)).items():
                    if isinstance(raw, dict):
                        out[str(name)] = dict(raw)
        return out

    # ── Writing ──────────────────────────────────────────────────────────
    def _write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def save_spec(self, spec: ServerSpec, scope: str = "user") -> Path:
        problem = spec.validate()
        if problem:
            raise ValueError(problem)
        target = self.path_for(scope)
        data = _read_json(target) if target.is_file() else {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[spec.name] = spec.to_dict()
        data["mcpServers"] = servers
        self._write(target, data)
        return target

    def add_server(self, name: str, command: str, args: list[str] | None = None,
                   env: dict[str, str] | None = None, scope: str = "global") -> Path:
        spec = ServerSpec(name=name, type="stdio", command=command, args=list(args or []),
                          env=dict(env or {}))
        return self.save_spec(spec, scope)

    def add_raw(self, name: str, raw: dict[str, Any], scope: str = "user") -> Path:
        return self.save_spec(ServerSpec.from_dict(name, raw), scope)

    def remove_server(self, name: str, scope: str | None = None) -> bool:
        removed = False
        files = [self.path_for(scope)] if scope else [p for _, p in self.config_files()]
        for cfg in files:
            if not cfg.is_file():
                continue
            data = _read_json(cfg)
            servers = data.get("mcpServers")
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                self._write(cfg, data)
                removed = True
        return removed

    def set_disabled(self, name: str, disabled: bool) -> Path | None:
        """Toggle 'disabled' in the file whose definition wins the merge."""
        spec = self.get_spec(name)
        if spec is None or spec.source is None or spec.scope == "plugin":
            return None
        data = _read_json(spec.source)
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or not isinstance(servers.get(name), dict):
            return None
        if disabled:
            servers[name]["disabled"] = True
        else:
            servers[name].pop("disabled", None)
        self._write(spec.source, data)
        return spec.source

    def add_preset(self, preset_id: str, custom_env: dict[str, str] | None = None,
                   scope: str = "global") -> bool:
        """Install a catalog preset. Env vars the user did not supply are stored as
        ${VAR} references (never as catalog placeholder text)."""
        preset = get_mcp_preset(preset_id)
        if not preset:
            return False
        env: dict[str, str] = {}
        for key in preset.env_vars:
            val = (custom_env or {}).get(key, "").strip()
            env[key] = val if val and not is_placeholder(val) else "${" + key + "}"
        self.add_server(preset.id, preset.command, list(preset.args), env or None, scope)
        return True

    # ── External config discovery / import ──────────────────────────────
    def find_claude_desktop_config(self) -> Path | None:
        if sys.platform == "darwin":
            candidate: Path | None = (Path.home() / "Library" / "Application Support" / "Claude"
                                      / "claude_desktop_config.json")
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            candidate = Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
        else:
            candidate = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        return candidate if candidate and candidate.is_file() else None

    def find_all_external_mcp_configs(self) -> list[tuple[str, Path]]:
        """Known MCP configs of other tools. Each label ends with '(Global)' or
        '(Workspace)' so imports land in the matching Axon scope."""
        found: list[tuple[str, Path]] = []
        claude = self.find_claude_desktop_config()
        if claude:
            found.append(("Claude Desktop (Global)", claude))
        home = Path.home()
        for label, p in (("Claude Code (Global)", home / ".claude.json"),
                         ("Cursor (Global)", home / ".cursor" / "mcp.json"),
                         ("Windsurf (Global)", home / ".codeium" / "windsurf" / "mcp_config.json"),
                         ("Cursor (Workspace)", self.workspace / ".cursor" / "mcp.json"),
                         ("VS Code (Workspace)", self.workspace / ".vscode" / "mcp.json"),
                         ("Project mcp.json (Workspace)", self.workspace / "mcp.json")):
            if p.is_file() and _servers_in(_read_json(p)):
                found.append((label, p))
        if sys.platform == "darwin":
            storage: Path | None = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            storage = Path(appdata) / "Code" / "User" / "globalStorage" if appdata else None
        else:
            storage = home / ".config" / "Code" / "User" / "globalStorage"
        if storage and storage.is_dir():
            for label, ext in (("VS Code Cline (Global)", "saoudrizwan.claude-dev"),
                               ("VS Code Roo Code (Global)", "rooveterinaryinc.roo-cline")):
                p = storage / ext / "settings" / "cline_mcp_settings.json"
                if p.is_file():
                    found.append((label, p))
        return found

    def import_from_all_discovered(self) -> int:
        """Import external servers. Global sources -> user scope; workspace sources ->
        local scope (so project servers never leak into every other project).
        Already-defined names are not overwritten. Returns number imported."""
        existing = set(self.load_specs())
        imported = 0
        for label, path in self.find_all_external_mcp_configs():
            scope = "local" if label.endswith("(Workspace)") else "user"
            for name, raw in _servers_in(_read_json(path)).items():
                if name in existing or not isinstance(raw, dict):
                    continue
                try:
                    spec = ServerSpec.from_dict(str(name), raw)
                    if spec.validate():
                        continue
                    self.save_spec(spec, scope)
                except (ValueError, OSError):
                    continue
                existing.add(name)
                imported += 1
        return imported

    def detect_workspace_recommended_presets(self) -> list[str]:
        recommended: list[str] = []
        try:
            if any(self.workspace.glob("*.sqlite")) or any(self.workspace.glob("*.db")):
                recommended.append("sqlite")
            git_cfg = self.workspace / ".git" / "config"
            if git_cfg.is_file() and "github.com" in git_cfg.read_text(encoding="utf-8", errors="ignore"):
                recommended.append("github")
            for dc_name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
                dc = self.workspace / dc_name
                if dc.is_file() and "postgres" in dc.read_text(encoding="utf-8", errors="ignore").lower():
                    recommended.append("postgres")
                    break
        except OSError:
            pass
        return recommended
