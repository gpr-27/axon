"""
Environment diagnostics for /doctor (and the hidden Doctor tool).

Reports Python/OS, shell, git, ripgrep, node/npx/uvx (MCP servers), provider API-key
presence (never values), config files found, workspace state and terminal
capabilities (TTY, encoding/UTF-8, VT/ANSI support). Output is plain ASCII so it
prints safely on cp1252 consoles.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from axon.tools.base import Tool, ToolContext

OK, WARN, MISS = "[ok]", "[!!]", "[--]"


def _version(exe: str, args: list[str] | None = None) -> str:
    try:
        res = subprocess.run([exe, *(args or ["--version"])], capture_output=True, timeout=6,
                             stdin=subprocess.DEVNULL)
        out = (res.stdout or res.stderr).decode("utf-8", errors="replace").strip().splitlines()
        return out[0][:80] if out else "unknown version"
    except (OSError, subprocess.SubprocessError):
        return "found (version check failed)"


def _row(label: str, value: str) -> str:
    return f"{label:<15}: {value}"


def _shell_info() -> str:
    try:
        from axon import platform as ax_platform  # owned by PLATFORM agent
        fn = getattr(ax_platform, "default_shell", None)
        if callable(fn):
            argv = fn()
            if argv:
                return " ".join(str(a) for a in argv)
    except Exception:
        pass
    try:
        from axon.tools.shell import _find_shell_runner  # legacy helper
        argv, flavor = _find_shell_runner()
        return f"{flavor} ({' '.join(argv)})"
    except Exception:
        pass
    if os.name == "nt":
        for cand in ("pwsh", "powershell", "cmd"):
            if shutil.which(cand):
                return cand
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "sh"


def _windows_vt_enabled() -> bool | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return bool(mode.value & 0x0004)
    except Exception:
        return None


def _terminal_lines() -> list[str]:
    out = []
    tty = False
    try:
        tty = sys.stdout.isatty()
    except Exception:
        pass
    enc = (getattr(sys.stdout, "encoding", "") or "unknown")
    utf8 = enc.lower().replace("-", "").startswith("utf8")
    out.append(_row("Terminal TTY", f"{OK if tty else MISS} {'interactive' if tty else 'not a TTY (piped/redirected)'}"))
    out.append(_row("Output Enc.", f"{OK if utf8 else WARN} {enc}{'' if utf8 else ' (set PYTHONUTF8=1 or use Windows Terminal for full Unicode)'}"))
    term = os.environ.get("TERM_PROGRAM") or ("Windows Terminal" if os.environ.get("WT_SESSION") else "") or os.environ.get("TERM", "")
    if term:
        out.append(_row("Terminal", term))
    vt = _windows_vt_enabled()
    if os.name == "nt":
        if vt is None:
            out.append(_row("ANSI/VT", f"{WARN} unknown (no console attached)"))
        else:
            out.append(_row("ANSI/VT", f"{OK if vt else WARN} {'enabled' if vt else 'disabled - colors may print as escape codes'}"))
    else:
        out.append(_row("ANSI/VT", f"{OK} supported"))
    try:
        cols, rows = shutil.get_terminal_size((0, 0))
        if cols:
            out.append(_row("Terminal Size", f"{cols}x{rows}"))
    except Exception:
        pass
    return out


_KEY_VARS = [
    ("AXON_API_KEY", "Axon / AgentRouter"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("GEMINI_API_KEY", "Gemini"),
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("BRAVE_API_KEY", "Brave Search (WebSearch)"),
]


def _key_lines(settings: Any) -> list[str]:
    names = dict(_KEY_VARS)
    try:
        from axon.providers.catalog import PROVIDER_PRESETS
        for p in PROVIDER_PRESETS:
            if getattr(p, "env_var", None) and p.env_var not in names:
                names[p.env_var] = p.name
    except Exception:
        pass
    out = [_row("Provider keys", "(environment; values never shown)")]
    for var, label in names.items():
        present = bool(os.environ.get(var, "").strip())
        out.append(f"  {var:<22} {OK + ' set    ' if present else MISS + ' not set'} ({label})")
    return out


def _axon_home() -> Path:
    from axon.storage import axon_home
    return axon_home()


def _config_lines(workspace: Path) -> list[str]:
    home = Path.home()
    ah = _axon_home()
    cands = [
        ah / "config.toml",
        ah / ".env",
        ah / "settings.json",
        ah / "mcp.json",
        workspace / ".axon" / "config.toml",
        workspace / ".axon" / "settings.json",
        workspace / ".mcp.json",
        workspace / ".env",
        workspace / "AXON.md",
        workspace / "axon.md",
        workspace / "CLAUDE.md",
        workspace / "AGENTS.md",
    ]
    found = []
    seen: set[tuple[int, int]] = set()
    for c in cands:
        try:
            if c.is_file():
                st = c.stat()
                key = (st.st_dev, st.st_ino) if st.st_ino else (0, hash(str(c.resolve()).lower()))
                if key in seen:
                    continue
                seen.add(key)
                found.append(c)
        except OSError:
            continue
    if not found:
        return [_row("Config Files", f"{MISS} none found (defaults in use)")]
    lines = [_row("Config Files", f"{OK} {len(found)} found")]
    for f in found:
        try:
            shown = "~/" + f.relative_to(home).as_posix()
        except ValueError:
            shown = f.as_posix()
        lines.append(f"{'':<17}{shown}")
    return lines


def axon_lines(settings: Any, workspace: Path) -> list[str]:
    """Axon-specific setup: config locations, key, hooks, instruction files, agents, skills, storage."""
    lines: list[str] = []
    ah = _axon_home()
    for label, path in (("User config", ah / "config.toml"), ("Project config", workspace / ".axon" / "config.toml")):
        lines.append(_row(label, f"{path} ({'found' if path.is_file() else 'none'})"))
    try:
        key = bool(settings.api_key.get_secret_value())
    except Exception:
        key = False
    if key:
        lines.append(_row("API key", f"{OK} set"))
    else:
        hint = "set AXON_API_KEY or ~/.axon/.env"
        try:
            from axon.features.onboarding import needs_setup
            if needs_setup(settings):
                hint = "run `axon setup` to configure a provider"
        except Exception:
            pass
        lines.append(_row("API key", f"{WARN} missing ({hint})"))
    try:
        from axon.hooks import HookManager
        hm = HookManager(settings, workspace)
        n = len(hm.describe())
        lines.append(_row("Hooks", f"{WARN if hm.load_errors else OK} {n} configured"))
        for err in hm.load_errors:
            lines.append(_row("  hook config", f"{WARN} {err}"))
    except Exception as e:
        lines.append(_row("Hooks", f"{WARN} error: {e}"))
    try:
        from axon.agent.memory import discover_instruction_files
        files = discover_instruction_files(workspace)
        lines.append(_row("Instructions", ", ".join(str(f.path) for f in files) or "none"))
    except Exception:
        pass
    try:
        from axon.agent.subagent import load_agent_definitions
        lines.append(_row("Subagents", ", ".join(sorted(d.name for d in load_agent_definitions(workspace)))))
    except Exception:
        pass
    try:
        from axon.skills.manager import SkillManager
        mgr = SkillManager(workspace)
        names = sorted(getattr(mgr, "skills", {}) or {})
        lines.append(_row("Skills", f"{len(names)} available" + (f" ({', '.join(names[:8])}{', ...' if len(names) > 8 else ''})" if names else "")))
    except Exception:
        pass
    try:
        from axon import storage
        total = sum(int(r.get("bytes", 0)) for r in storage.storage_summary())
        lines.append(_row("Storage", f"{ah} ({total / 1_048_576:.1f} MB, cleanup after "
                                     f"{int(getattr(settings, 'cleanup_period_days', 30) or 30)} days)"))
    except Exception:
        pass
    return lines


def run_diagnostics(settings: Any, workspace: Path) -> str:
    """Collect environment and Axon-setup diagnostics as one printable ASCII report
    (used by both `axon doctor` and /doctor)."""
    lines = ["=== Axon Environment Diagnostics ==="]
    lines.append(_row("Platform", f"{platform.system()} {platform.release()} ({platform.machine()})"))
    py_ok = sys.version_info >= (3, 10)
    lines.append(_row("Python Version", f"{OK if py_ok else WARN} {sys.version.split()[0]} ({sys.executable})"))
    lines.append(_row("Shell Engine", _shell_info()))

    tools = [
        ("git", "Git CLI", ["--version"], "needed for diffs, worktrees and /review"),
        ("rg", "Ripgrep (rg)", ["--version"], "Grep/Glob use a slower Python fallback"),
        ("node", "Node.js", ["--version"], "needed for npx-based MCP servers"),
        ("npx", "npx", ["--version"], "needed for npx-based MCP servers"),
        ("uvx", "uvx", ["--version"], "needed for Python (uvx) MCP servers"),
    ]
    for exe, label, vargs, why in tools:
        path = shutil.which(exe)
        if exe == "rg" and not path:
            try:
                from axon.tools.search import find_rg
                path = find_rg()
            except Exception:
                path = None
        if path:
            lines.append(_row(label, f"{OK} {_version(path, vargs)}"))
        else:
            lines.append(_row(label, f"{MISS} not found - {why}"))

    lines.append("")
    lines.append(_row("Workspace", str(workspace)))
    ws_ok = workspace.is_dir() and os.access(workspace, os.W_OK)
    lines.append(_row("Writable", f"{OK} yes" if ws_ok else f"{WARN} no"))
    in_git = any((p / ".git").exists() for p in [workspace, *workspace.parents])
    lines.append(_row("Git Repository", f"{OK} yes" if in_git else f"{MISS} no"))
    lines.append(_row("Active Model", str(getattr(settings, "model", "?"))))
    lines.append(_row("Base URL", str(getattr(settings, "base_url", "?"))))
    lines.append(_row("Permission Mode", str(getattr(settings, "mode", "?"))))

    lines.append("")
    lines.extend(axon_lines(settings, workspace))
    lines.append("")
    lines.extend(_key_lines(settings))
    lines.append("")
    lines.extend(_config_lines(workspace))
    lines.append("")
    lines.extend(_terminal_lines())

    problems = sum(1 for ln in lines if WARN in ln)
    lines.append("")
    lines.append(_row("Status", "Healthy" if not problems else f"{problems} warning(s) - see [!!] lines above"))
    return "\n".join(lines)


class DoctorTool(Tool):
    name: ClassVar[str] = "Doctor"
    description: ClassVar[str] = (
        "Diagnose the local environment: Python, OS, shell, git, ripgrep, node/npx, API key presence, "
        "config files and terminal capabilities."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"
    hidden: ClassVar[bool] = True  # used by /doctor; not advertised to the model

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return run_diagnostics(ctx.settings, Path(ctx.workspace))
