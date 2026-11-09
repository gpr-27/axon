"""
Dynamic request headers for remote MCP servers.

``headersHelper`` is a shell command that prints a JSON object of header names to values (Kerberos, short
lived tokens, internal SSO). It runs on every connection and again after a 401/403. When a server has no
configured ``Authorization`` header, a stored OAuth token supplies one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from axon.mcp.config import ServerSpec

HELPER_TIMEOUT = 10.0
_CREDENTIAL_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|AUTH)", re.IGNORECASE)
_GIT_KEY = re.compile(r"^GIT_CONFIG_KEY_\d+$")
_ALWAYS_SCRUB = frozenset({"ANTHROPIC_CUSTOM_HEADERS"})


_warned: set[str] = set()


def helper_allowed(spec: ServerSpec, workspace: Path) -> bool:
    """A helper is a shell command. For a project ``.mcp.json`` or local-scope server it runs only after you
    trusted exactly this folder; ``-p`` runs get no automatic trust for this."""
    if spec.scope not in ("project", "local"):
        return True
    from axon.mcp.policy import folder_trusted
    return folder_trusted(workspace)


def _note_not_run(spec: ServerSpec, workspace: Path) -> None:
    from axon.mcp import state
    if state.can_prompt() or spec.name in _warned:
        return
    _warned.add(spec.name)
    try:
        key = str(Path(workspace).resolve())
        sys.stderr.write(f'MCP server "{spec.name}": headersHelper not run because this folder is not trusted. '
                         f'Run axon here and accept the trust question, or set projects["{key}"]'
                         f".hasTrustDialogAccepted to true in {_home_config()}.\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _home_config() -> Path:
    from axon.storage import user_file
    return user_file("mcp.json")


class HeadersHelperError(Exception):
    """headersHelper failed (not found, timed out, bad output)."""


def scrubbed_env(env: dict[str, str]) -> dict[str, str]:
    """Environment without credential-looking variables (for helpers a repository or plugin supplies)."""
    return {k: v for k, v in env.items()
            if k not in _ALWAYS_SCRUB and (_GIT_KEY.match(k) or not _CREDENTIAL_NAME.search(k))}


def helper_cwd(spec: ServerSpec, workspace: Path) -> Path:
    """Where a relative path in the helper command resolves, by where the server was configured."""
    if spec.scope == "plugin" and spec.source is not None:
        return Path(spec.source).parent
    if spec.scope in ("user", "managed"):
        from axon.storage import axon_home
        home = axon_home()
        return home if home.is_dir() else Path(workspace)
    return Path(workspace)


def run_headers_helper(spec: ServerSpec, workspace: Path, timeout: float = HELPER_TIMEOUT) -> dict[str, str]:
    """Run ``spec.headers_helper`` and return its headers."""
    env = dict(os.environ)
    redacted_url = spec.url
    if spec.scope in ("project", "plugin"):
        clean = scrubbed_env(env)
        for k, v in env.items():
            if k not in clean and len(v) >= 6:
                redacted_url = redacted_url.replace(v, "REDACTED")
        env = clean
    for prefix in ("CLAUDE_CODE_MCP", "AXON_MCP"):
        env[f"{prefix}_SERVER_NAME"] = spec.name
        env[f"{prefix}_SERVER_URL"] = redacted_url
    if spec.scope == "plugin" and spec.source is not None:
        env["AXON_PLUGIN_ROOT"] = env["CLAUDE_PLUGIN_ROOT"] = str(Path(spec.source).parent)
    try:
        proc = subprocess.run(spec.headers_helper, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, cwd=str(helper_cwd(spec, workspace)))
    except subprocess.TimeoutExpired:
        raise HeadersHelperError(f"headersHelper timed out after {timeout:g}s") from None
    except OSError as e:
        raise HeadersHelperError(f"headersHelper could not run: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise HeadersHelperError(f"headersHelper exited with code {proc.returncode}"
                                 + (f": {detail[-1][:200]}" if detail else ""))
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise HeadersHelperError("headersHelper must print a JSON object of string key-value pairs") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise HeadersHelperError("headersHelper must print a JSON object of string key-value pairs")
    return data


class DynamicHeaders:
    """Callable header provider for the HTTP/SSE/WebSocket transports."""

    def __init__(self, spec: ServerSpec, workspace: Path, oauth: Any = None) -> None:
        self.spec = spec
        self.workspace = Path(workspace)
        self.oauth = oauth
        self._helper: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return bool(self.spec.headers_helper) or self.oauth is not None

    def _may_run(self) -> bool:
        if not self.spec.headers_helper:
            return False
        if helper_allowed(self.spec, self.workspace):
            return True
        _note_not_run(self.spec, self.workspace)
        return False

    def prime(self) -> None:
        """Run the helper once (at connect), when the trust rule lets it. Raises HeadersHelperError."""
        if self._may_run():
            with self._lock:
                self._helper = run_headers_helper(self.spec, self.workspace)
            if self.oauth is not None:
                self.oauth.helper_headers = dict(self._helper)

    @property
    def helper_supplies_authorization(self) -> bool:
        return any(k.lower() == "authorization" for k in self._helper)

    def __call__(self) -> dict[str, str]:
        out = dict(self._helper)
        configured = self.spec.has_authorization_header or self.helper_supplies_authorization
        if self.oauth is not None and not configured:
            out.update(self.oauth.auth_headers())
        return out

    def on_unauthorized(self) -> bool:
        """A 401/403 came back: re-run the helper (new credential) or refresh the OAuth token."""
        if self._may_run():
            try:
                fresh = run_headers_helper(self.spec, self.workspace)
            except HeadersHelperError:
                return False
            with self._lock:
                self._helper = fresh
            if self.oauth is None or self.helper_supplies_authorization or self.spec.has_authorization_header:
                return True
        if self.oauth is not None and not self.spec.has_authorization_header:
            return bool(self.oauth.on_unauthorized())
        return False
