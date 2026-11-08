"""OS sandbox for the Bash tool.

The text-based shell guard reads the command line, so ``python3 -c "open(...)"`` slips past it. The sandbox is
enforced by the operating system instead. Policy: read anywhere except credential stores; write only inside
the workspace, the additional directories and the temp dir; no network unless ``sandbox.allowNetwork``.

macOS uses ``sandbox-exec`` with a generated profile, Linux uses ``bwrap`` (bubblewrap) when it is on PATH.
When the sandbox is on and no backend works, commands are refused rather than run unsandboxed.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.errors import ToolError

SANDBOX_TAG = "[sandbox]"
_CRED_NAMES = (".ssh", ".aws", ".gnupg", ".netrc", ".vault", ".git-credentials", ".pgpass", ".azure", ".kube")
_DENIED_OUTPUT = re.compile(r"Operation not permitted|Permission denied|Read-only file system|"
                            r"Network is unreachable|PermissionError|nodename nor servname", re.IGNORECASE)


@dataclass(frozen=True)
class Policy:
    workspace: Path
    extra_dirs: tuple[Path, ...] = ()
    allow_network: bool = False
    home: Path | None = None
    axon_home: Path | None = None
    tmp: tuple[Path, ...] = field(default_factory=tuple)


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "sandbox_enabled", False))


def backend() -> tuple[str | None, str]:
    """(backend name, "") when this machine can sandbox, else (None, the reason)."""
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec"):
            return "sandbox-exec", ""
        return None, "sandbox-exec was not found on PATH"
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap"):
            return "bwrap", ""
        return None, "bwrap (bubblewrap) is not installed; install it, for example `apt install bubblewrap`"
    return None, f"no sandbox backend exists for {sys.platform}"


def _real(p: Path | str) -> Path:
    try:
        return Path(os.path.realpath(os.path.expanduser(str(p))))
    except OSError:
        return Path(p)


def policy_for(settings: Any) -> Policy:
    from axon.storage import axon_home
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    tmp = {_real(tempfile.gettempdir())}
    if sys.platform == "darwin":
        tmp.add(_real("/tmp"))
    else:
        tmp.add(Path("/tmp"))
    return Policy(
        workspace=_real(getattr(settings, "workspace", Path.cwd())),
        extra_dirs=tuple(_real(d) for d in (getattr(settings, "additional_dirs", None) or [])),
        allow_network=bool(getattr(settings, "sandbox_allow_network", False)),
        home=home, axon_home=axon_home(), tmp=tuple(sorted(tmp)))


def write_roots(policy: Policy) -> list[Path]:
    return [policy.workspace, *policy.extra_dirs, *policy.tmp]


def credential_paths(policy: Policy) -> list[Path]:
    """Credential stores to hide, whether or not they exist yet."""
    out: list[Path] = []
    if policy.home is not None:
        out += [policy.home / n for n in _CRED_NAMES]
    if policy.axon_home is not None:
        out.append(policy.axon_home / ".env")
    try:
        from axon.permissions.paths import credential_stores
        out += credential_stores()
    except Exception:
        pass
    seen, uniq = set(), []
    for p in out:
        for q in (p, _real(p)):
            if q not in seen:
                seen.add(q)
                uniq.append(q)
    return uniq


def _q(p: Path) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_profile(policy: Policy) -> str:
    """The sandbox-exec profile. Later rules win, so the denials come after the allowances."""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    lines.append('(allow file-write* (regex #"^/dev/(null|zero|tty|dtracehelper|fd/[0-9]+|stdout|stderr)$"))')
    for root in write_roots(policy):
        lines.append(f"(allow file-write* (subpath {_q(root)}))")
    for p in credential_paths(policy):
        lines.append(f"(deny file-read* file-write* (subpath {_q(p)}))")
    if not policy.allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def _bwrap_argv(argv: list[str], policy: Policy) -> list[str]:
    out = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for root in write_roots(policy):
        if root.exists():
            out += ["--bind", str(root), str(root)]
    for p in credential_paths(policy):
        if p.is_dir():
            out += ["--tmpfs", str(p)]
        elif p.exists():
            out += ["--ro-bind", "/dev/null", str(p)]
    if not policy.allow_network:
        out.append("--unshare-net")
    return out + ["--"] + argv


def wrap_argv(argv: list[str], settings: Any) -> list[str]:
    """*argv* wrapped to run sandboxed. Raises ToolError when the sandbox is on but cannot be used."""
    if not enabled(settings):
        return argv
    name, why = backend()
    if name is None:
        raise ToolError(f"{SANDBOX_TAG} the Bash sandbox is on but unavailable: {why}. Refusing to run the command "
                        f"unsandboxed. Install the missing tool, or turn the sandbox off with /sandbox off.")
    policy = policy_for(settings)
    if name == "sandbox-exec":
        return ["sandbox-exec", "-p", build_profile(policy)] + argv
    return _bwrap_argv(argv, policy)


def start_failure(output: str, code: int | None) -> str | None:
    """The sandbox tool's own error when it could not start (as opposed to the command failing)."""
    if code in (None, 0):
        return None
    head = output.strip().splitlines()[0] if output.strip() else ""
    if head.startswith(("sandbox-exec:", "bwrap:")):
        return head
    return None


def denial_note(output: str, code: int | None) -> str:
    """A note for a failed command whose output looks like an OS denial, so it is not mistaken for a plain failure."""
    if code in (None, 0) or not _DENIED_OUTPUT.search(output or ""):
        return ""
    return (f"{SANDBOX_TAG} this failure may come from the sandbox, not the command: writes outside the workspace, "
            f"additional directories and temp dir, reads of credential stores, and network access are blocked. "
            f"/sandbox status shows the policy; /sandbox off turns it off.")


def status_text(settings: Any) -> str:
    name, why = backend()
    on = enabled(settings)
    p = policy_for(settings)
    net = "allowed (sandbox.allowNetwork)" if p.allow_network else "blocked"
    lines = [f"Bash sandbox: {'on' if on else 'off'}"]
    lines.append(f"Backend: {name}" if name else f"Backend: unavailable ({why})")
    if on and not name:
        lines.append("Bash commands are refused while the sandbox is on and unavailable.")
    lines += [
        "Enforced by the OS, for every Bash command and the processes it starts:",
        "  Read: anywhere, except credential stores (~/.ssh, ~/.aws, ~/.gnupg, ~/.netrc, ~/.kube, ~/.azure, "
        "~/.git-credentials, ~/.pgpass, ~/.vault, ~/.axon/.env)",
        "  Write: only inside the workspace, additional directories and the temp dir:",
        *[f"    {r}" for r in write_roots(p)],
        f"  Network: {net}",
        "Not enforced:",
        "  The Read, Write and Edit tools and MCP servers run in Axon itself, outside the sandbox (the "
        "permission rules and protected-path checks cover them).",
        "  Hooks and web tools are not sandboxed. Reads of files outside the credential list are not restricted.",
    ]
    if name == "sandbox-exec":
        lines.append("  macOS sandbox-exec is deprecated by Apple but still enforced by the kernel.")
    return "\n".join(lines)
