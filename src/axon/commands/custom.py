"""
Custom slash commands: markdown files that expand into a prompt.

Locations (later override earlier):
  plugins  <plugin>/commands/**.md         -> /<plugin>:<name>
  user     ~/.axon/commands/**.md, ~/.claude/commands/**.md
  project  <ws>/.claude/commands/**.md, <ws>/.axon/commands/**.md
Sub-directories become namespaces: ``frontend/component.md`` -> ``/frontend:component``.

Frontmatter: ``description``, ``argument-hint``, ``model``, ``allowed-tools``.
Body: ``$ARGUMENTS`` / ``$1``..``$9`` substitution, ``!`cmd``` shell output (only if
the command is permitted by ``allowed-tools`` or the permission engine / user), and
``@path`` file includes (workspace files only).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.ui.theme import BOLD, DIM, GOLD, RST, SLATE, TEAL, WHITE

MAX_INCLUDE_BYTES = 200_000
_INCLUDE_RE = re.compile(r"(?<![\w@`])@([A-Za-z0-9_./\\~-][^\s`'\"<>()]*)")


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: Path
    source: str
    argument_hint: str = ""
    model: str | None = None
    allowed_tools: list[str] = field(default_factory=list)


def command_dirs(workspace: Path) -> list[tuple[Path, str, str]]:
    """(directory, source label, namespace prefix) in increasing precedence."""
    dirs: list[tuple[Path, str, str]] = []
    try:
        from axon.commands.plugins import load_plugins
        for pl in load_plugins(workspace):
            if pl.enabled and (pl.path / "commands").is_dir():
                dirs.append((pl.path / "commands", f"plugin:{pl.name}", f"{pl.name}:"))
    except Exception:
        pass
    home = Path.home()
    ws = Path(workspace)
    dirs += [
        (home / ".claude" / "commands", "user", ""),
        (_axon_home() / "commands", "user", ""),
        (ws / ".claude" / "commands", "project", ""),
        (ws / ".axon" / "commands", "project", ""),
    ]
    return dirs


_cache: dict[str, tuple[float, tuple[tuple[str, int], ...], dict[str, CustomCommand]]] = {}
_CACHE_TTL = 2.0


def _scan_files(dirs: list[tuple[Path, str, str]]) -> list[tuple[Path, Path, str, str, int]]:
    found = []
    for d, source, prefix in dirs:
        if not d.is_dir():
            continue
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if not s.startswith(".")]
            for fn in files:
                if fn.lower().endswith(".md") and not fn.startswith("."):
                    p = Path(root) / fn
                    try:
                        found.append((d, p, source, prefix, p.stat().st_mtime_ns))
                    except OSError:
                        pass
    return found


def load_custom_commands(workspace: Path) -> dict[str, CustomCommand]:
    key = str(Path(workspace))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[2]
    files = _scan_files(command_dirs(Path(workspace)))
    sig = tuple((str(p), m) for _, p, _, _, m in files)
    if hit and hit[1] == sig:
        _cache[key] = (now, sig, hit[2])
        return hit[2]
    from axon.skills.manager import as_list, parse_frontmatter
    out: dict[str, CustomCommand] = {}
    for base, p, source, prefix, _ in files:
        rel = p.relative_to(base).with_suffix("")
        name = prefix + ":".join(re.sub(r"[^A-Za-z0-9_.-]+", "-", part) for part in rel.parts)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        desc = fm.get("description")
        if not isinstance(desc, str) or not desc:
            desc = next((ln.strip().lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")[:100]
        model = fm.get("model")
        out[name] = CustomCommand(
            name=name, description=str(desc), body=body, path=p, source=source,
            argument_hint=_raw_hint(fm.get("argument-hint")),
            model=model if isinstance(model, str) and model else None,
            allowed_tools=as_list(fm.get("allowed-tools") or fm.get("allowed_tools")),
        )
    _cache[key] = (now, sig, out)
    return out


def invalidate_cache() -> None:
    _cache.clear()


# ─── Rendering ──────────────────────────────────────────────────────────────

def expand_file_includes(text: str, workspace: Path) -> str:
    """Replace ``@path`` with the file's contents for files inside the workspace."""
    from axon.permissions.paths import is_protected_path, is_within

    def repl(m: re.Match[str]) -> str:
        raw = m.group(1).rstrip(".,:;")
        trail = m.group(1)[len(raw):]
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(workspace) / p
        try:
            if not p.is_file() or not is_within(workspace, p) or is_protected_path(p, workspace):
                return m.group(0)
            if p.stat().st_size > MAX_INCLUDE_BYTES:
                return f"{m.group(0)} [file too large to include]{trail}"
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return m.group(0)
        rel = os.path.relpath(p, workspace).replace("\\", "/")
        return f'\n<file path="{rel}">\n{content}\n</file>\n{trail}'
    return _INCLUDE_RE.sub(repl, text)


class _BashProbe:
    name = "Bash"
    readonly = False
    default_permission = "ask"


def make_shell_gate(agent: Any, allowed_tools: list[str]) -> Any:
    """Approve ``!`cmd``` injections: deny rules and invariants always win; then the
    command's allowed-tools, then the permission engine, then an interactive y/n."""
    from axon.commands.common import confirm
    from axon.permissions.rules import parse_rule
    ws = Path(agent.settings.workspace)
    rules = [parse_rule(s) for s in allowed_tools if s.strip()]
    tool = None
    try:
        tool = agent.registry.get("Bash")
    except Exception:
        tool = None
    tool = tool or _BashProbe()

    def gate(cmd: str) -> bool:
        args = {"command": cmd}
        perms = getattr(agent, "permissions", None)
        decision = None
        if perms is not None:
            try:
                decision = perms.check(tool, args, agent.settings.mode)
            except Exception:
                decision = None
        if decision is not None and decision.outcome == "deny":
            print(f"  {GOLD}!{RST} {SLATE}shell context blocked: {cmd} ({decision.reason}){RST}")
            return False
        if any(r.matches("Bash", args, ws) for r in rules if r.tool in ("Bash", "*")):
            return True
        if decision is not None and decision.outcome == "allow":
            return True
        return confirm(f"Run shell context {WHITE}{cmd}{RST}?", default=False)

    return gate


def render_custom_command(agent: Any, cmd: CustomCommand, arguments: str) -> str:
    from axon.skills.manager import expand_shell_injections, substitute_arguments
    ws = Path(agent.settings.workspace)
    body = substitute_arguments(cmd.body, arguments)
    body = expand_shell_injections(body, ws, make_shell_gate(agent, cmd.allowed_tools))
    return expand_file_includes(body, ws)


def run_with_overrides(agent: Any, prompt: str, *, model: str | None = None, allowed_tools: list[str] | None = None) -> Any:
    """Run one agent turn with temporary allow rules and/or model."""
    added = []
    perms = getattr(agent, "permissions", None)
    for spec in allowed_tools or []:
        if perms is not None and hasattr(perms, "add_session_rule"):
            perms.add_session_rule("allow", spec)
            added.append(spec)
    old_settings = old_provider = None
    if model and model != agent.settings.model:
        try:
            from axon.providers.registry import provider_for
            old_settings, old_provider = agent.settings, agent.provider
            agent.settings = agent.settings.model_copy(update={"model": model})
            agent.provider = provider_for(model, agent.settings)
        except Exception as e:
            print(f"  {GOLD}!{RST} {SLATE}model override '{model}' unavailable ({e}); using {agent.settings.model}{RST}")
            if old_settings is not None:
                agent.settings, agent.provider = old_settings, old_provider
            old_settings = None
    try:
        return agent.run_turn(prompt)
    finally:
        for spec in added:
            perms.remove_session_rule("allow", spec)
        if old_settings is not None:
            agent.settings, agent.provider = old_settings, old_provider


def run_custom_command(agent: Any, cmd: CustomCommand, arguments: str) -> None:
    prompt = render_custom_command(agent, cmd, arguments)
    extra = f" {DIM}{arguments}{RST}" if arguments else ""
    print(f"\n  {TEAL}▸{RST} {BOLD}/{cmd.name}{RST}{extra}  {SLATE}{cmd.source}{RST}\n")
    result = run_with_overrides(agent, prompt, model=cmd.model, allowed_tools=cmd.allowed_tools)
    from axon.commands.agent_cmds import record_invocation
    record_invocation(agent, "command_run", cmd.name, arguments, result, kind="custom_command",
                      scope=cmd.source, path=str(cmd.path))


def _raw_hint(value: object) -> str:
    """``argument-hint: [env]`` is a hint string, not a YAML list: show it verbatim as "[env]"."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value or "")
