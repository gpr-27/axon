"""
Custom slash commands: markdown files that expand into a prompt.

Locations (later override earlier):
  plugins  <plugin>/commands/**.md (or the manifest ``commands`` paths / object map) -> /<plugin>:<name>
  user     ~/.axon/commands/**.md, ~/.claude/commands/**.md
  project  <ws>/.claude/commands/**.md, <ws>/.axon/commands/**.md
Sub-directories become namespaces: ``frontend/component.md`` -> ``/frontend:component``.
A skill with the same name wins over a command file (see ``registry.dispatch_command``).

Frontmatter: the same fields as a skill except ``name`` and ``paths`` - ``description``,
``argument-hint``, ``arguments``, ``model``, ``effort``, ``allowed-tools``, ``disallowed-tools``,
``context: fork`` + ``agent`` + ``background``, ``shell``, ``user-invocable``.
Body: ``$ARGUMENTS`` / ``$ARGUMENTS[N]`` / ``$0``.. / named-argument substitution, ``${CLAUDE_*}``
variables, ``!`cmd``` and fenced ```` ```! ```` shell output (only if the command is permitted by
``allowed-tools`` or the permission engine / user; a failing or refused command aborts the command),
and ``@path`` file includes (workspace files only).
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
    path: Path | None
    source: str
    argument_hint: str = ""
    model: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)
    effort: str | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    context: str = ""
    agent: str | None = None
    background: bool = True
    shell: str = ""
    plugin: str = ""
    plugin_root: Path | None = None


def command_dirs(workspace: Path) -> list[tuple[Path, str, str]]:
    """(directory, source label, namespace prefix) in increasing precedence (plugin command folders
    are listed by :func:`plugin_command_sources`, which also honours the manifest ``commands`` paths)."""
    home = Path.home()
    ws = Path(workspace)
    return [
        (home / ".claude" / "commands", "user", ""),
        (_axon_home() / "commands", "user", ""),
        (ws / ".claude" / "commands", "project", ""),
        (ws / ".axon" / "commands", "project", ""),
    ]


def plugin_command_sources(workspace: Path) -> list[tuple[Path, Path, str, str, Any]]:
    """(base dir, file, source label, prefix, plugin) for every command file of the enabled plugins."""
    out: list[tuple[Path, Path, str, str, Any]] = []
    try:
        from axon.commands.plugins import enabled_plugins, plugin_command_files
        for pl in enabled_plugins(workspace):
            for base, f in plugin_command_files(pl):
                out.append((base, f, f"plugin:{pl.name}", f"{pl.name}:", pl))
    except Exception:
        pass
    return out


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


def _command_name(prefix: str, rel: Path) -> str:
    return prefix + ":".join(re.sub(r"[^A-Za-z0-9_.-]+", "-", part) for part in rel.with_suffix("").parts)


def _build(name: str, text: str, path: Path | None, source: str, plugin: Any = None) -> CustomCommand | None:
    from axon.skills.manager import as_bool, as_list, parse_frontmatter
    fm, body = parse_frontmatter(text)
    if not as_bool(fm.get("user-invocable", fm.get("user_invocable")), True):
        return None
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc:
        desc = next((ln.strip().lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")[:100]
    model = fm.get("model")

    def s(key: str) -> str:
        v = fm.get(key, fm.get(key.replace("_", "-"), ""))
        return v if isinstance(v, str) else (" ".join(str(x) for x in v) if isinstance(v, list) else "")

    shell = s("shell").strip().lower()
    return CustomCommand(
        name=name, description=str(desc), body=body, path=path, source=source,
        argument_hint=_raw_hint(fm.get("argument-hint")),
        model=model if isinstance(model, str) and model and model != "inherit" else None,
        allowed_tools=as_list(fm.get("allowed-tools") or fm.get("allowed_tools")),
        arguments=as_list(fm.get("arguments")),
        effort=s("effort") or None,
        disallowed_tools=as_list(fm.get("disallowed-tools") or fm.get("disallowed_tools")),
        context=s("context").lower(), agent=s("agent") or None,
        background=as_bool(fm.get("background"), True),
        shell=shell if shell in ("bash", "powershell") else "",
        plugin=plugin.name if plugin is not None else "",
        plugin_root=Path(plugin.path) if plugin is not None else None,
    )


def load_custom_commands(workspace: Path) -> dict[str, CustomCommand]:
    key = str(Path(workspace))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[2]
    plugin_files = plugin_command_sources(Path(workspace))
    files = [(b, p, src, pre, _mtime(p)) for b, p, src, pre, _pl in plugin_files]
    plugin_by_file = {p: pl for _, p, _, _, pl in plugin_files}
    files += _scan_files(command_dirs(Path(workspace)))
    inline = _inline_plugin_commands(Path(workspace))
    sig = tuple((str(p), m) for _, p, _, _, m in files) + tuple((n, hash(c.body)) for n, c in inline.items())
    if hit and hit[1] == sig:
        _cache[key] = (now, sig, hit[2])
        return hit[2]
    out: dict[str, CustomCommand] = {}
    for name, c in inline.items():
        out[name] = c
    for base, p, source, prefix, _ in files:
        rel = p.relative_to(base)
        name = _command_name(prefix, rel)
        from axon.skills.manager import name_refused
        if name_refused(name):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cmd = _build(name, text, p, source, plugin_by_file.get(p))
        if cmd is not None:
            out[name] = cmd
    _cache[key] = (now, sig, out)
    return out


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def _inline_plugin_commands(workspace: Path) -> dict[str, CustomCommand]:
    """Commands declared in a manifest ``commands`` object map (``source`` file or inline ``content``)."""
    out: dict[str, CustomCommand] = {}
    try:
        from axon.commands.plugins import enabled_plugins, plugin_inline_commands
        for pl in enabled_plugins(workspace):
            for key, spec in plugin_inline_commands(pl).items():
                cmd = _build(f"{pl.name}:{key}", spec["body"], spec["path"], f"plugin:{pl.name}", pl)
                if cmd is None:
                    continue
                if spec["description"]:
                    cmd.description = spec["description"]
                if spec["argument_hint"]:
                    cmd.argument_hint = spec["argument_hint"]
                if spec["model"]:
                    cmd.model = spec["model"]
                if spec["allowed_tools"]:
                    cmd.allowed_tools = spec["allowed_tools"]
                if not cmd.description:
                    cmd.description = f"{key} (from {pl.name})"
                out[cmd.name] = cmd
    except Exception:
        pass
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


def session_id_of(agent: Any) -> str:
    try:
        return str(getattr(getattr(agent, "session", None), "active_session_id", "") or "")
    except Exception:
        return ""


def command_variables(agent: Any, cmd: CustomCommand) -> dict[str, str]:
    v = {
        "PROJECT_DIR": str(agent.settings.workspace),
        "SESSION_ID": session_id_of(agent),
        "EFFORT": cmd.effort or str(getattr(agent.settings, "effort", "") or ""),
    }
    if cmd.path is not None:
        v["SKILL_DIR"] = str(cmd.path.parent)
    if cmd.plugin_root is not None:
        v["PLUGIN_ROOT"] = str(cmd.plugin_root)
        try:
            from axon.commands.plugins import Plugin, plugin_data_dir
            v["PLUGIN_DATA"] = str(plugin_data_dir(Plugin(name=cmd.plugin, path=cmd.plugin_root, scope="user")))
        except Exception:
            pass
    return v


def render_custom_command(agent: Any, cmd: CustomCommand, arguments: str) -> str:
    from axon.skills.manager import render_body
    from axon.skills.settings import get_bool_setting
    ws = Path(agent.settings.workspace)
    user_config: dict[str, str] = {}
    if cmd.plugin and cmd.plugin_root is not None:
        try:
            from axon.commands.plugins import Plugin, plugin_user_config_values
            user_config = plugin_user_config_values(Plugin(name=cmd.plugin, path=cmd.plugin_root, scope="user",
                                                           manifest=_plugin_manifest(cmd.plugin_root)), ws, include_sensitive=False)
        except Exception:
            user_config = {}
    body = render_body(
        cmd.body, arguments, names=cmd.arguments, variables=command_variables(agent, cmd), user_config=user_config,
        cwd=ws, allow_shell=make_shell_gate(agent, cmd.allowed_tools), shell=cmd.shell or None,
        shell_disabled=get_bool_setting(ws, "disableSkillShellExecution", False),
    )
    return expand_file_includes(body, ws)


def _plugin_manifest(root: Path) -> dict[str, Any]:
    from axon.commands.plugins import _manifest_path, _read_json_dict
    mp = _manifest_path(root)
    return (_read_json_dict(mp) if mp else None) or {}


def _effort_value(effort: str) -> str:
    return {"low": "reflex", "medium": "balanced", "high": "synapse", "xhigh": "quantum", "max": "quantum"}.get(
        effort.strip().lower(), effort.strip().lower())


def run_with_overrides(agent: Any, prompt: str, *, model: str | None = None, allowed_tools: list[str] | None = None,
                       disallowed_tools: list[str] | None = None, effort: str | None = None) -> Any:
    """Run one agent turn with temporary allow / deny rules, model and effort."""
    added: list[tuple[str, str]] = []
    perms = getattr(agent, "permissions", None)
    for kind, specs in (("allow", allowed_tools), ("deny", disallowed_tools)):
        for spec in specs or []:
            if perms is not None and hasattr(perms, "add_session_rule"):
                try:
                    perms.add_session_rule(kind, spec)
                    added.append((kind, spec))
                except Exception:
                    pass
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
    old_effort = None
    if effort:
        new_eff = _effort_value(effort)
        if new_eff != getattr(agent.settings, "effort", None):
            old_effort = agent.settings
            try:
                agent.settings = agent.settings.model_copy(update={"effort": new_eff})
            except Exception:
                old_effort = None
    try:
        return agent.run_turn(prompt)
    finally:
        if old_effort is not None:
            agent.settings = old_effort
        for kind, spec in added:
            try:
                perms.remove_session_rule(kind, spec)
            except Exception:
                pass
        if old_settings is not None:
            agent.settings, agent.provider = old_settings, old_provider


def run_forked(agent: Any, prompt: str, *, label: str, agent_type: str | None = None, background: bool = True) -> str | None:
    """``context: fork``: run ``prompt`` as a fresh subagent (``agent_type``, default general-purpose).
    In the background the report arrives as a steering message when it finishes; with
    ``background=False`` the report is printed and returned."""
    from axon.agent.subagent import run_subagent
    kind = agent_type or "general-purpose"

    def work() -> str:
        return str(run_subagent(prompt, agent, subagent_type=kind, description=label[:60]))

    if not background:
        report = work()
        print(f"\n{report}\n")
        return report
    import threading
    from axon.ui.in_flight import push_queued

    def runner() -> None:
        try:
            note = f"[Skill {label} finished]\n{work()}"
        except Exception as e:  # noqa: BLE001
            note = f"[Skill {label} failed] {type(e).__name__}: {e}"
        queue = getattr(agent, "message_queue", None)
        if queue is not None:
            push_queued(queue, note, steer=True)

    threading.Thread(target=runner, daemon=True, name=f"fork-{label}").start()
    print(f"  {SLATE}{label} is running in a subagent ({kind}) in the background; its result will arrive here (/list-agents, /stop).{RST}")
    return None


def run_custom_command(agent: Any, cmd: CustomCommand, arguments: str) -> None:
    from axon.skills.manager import SkillShellError
    try:
        prompt = render_custom_command(agent, cmd, arguments)
    except SkillShellError as e:
        print(f"\n  {GOLD}!{RST} /{cmd.name} not run: {e}\n")
        return
    extra = f" {DIM}{arguments}{RST}" if arguments else ""
    print(f"\n  {TEAL}▸{RST} {BOLD}/{cmd.name}{RST}{extra}  {SLATE}{cmd.source}{RST}\n")
    if cmd.context == "fork":
        result = None
        run_forked(agent, prompt, label=f"/{cmd.name}", agent_type=cmd.agent, background=cmd.background)
    else:
        result = run_with_overrides(agent, prompt, model=cmd.model, allowed_tools=cmd.allowed_tools,
                                    disallowed_tools=cmd.disallowed_tools, effort=cmd.effort)
    from axon.commands.agent_cmds import record_invocation
    record_invocation(agent, "command_run", cmd.name, arguments, result, kind="custom_command",
                      scope=cmd.source, path=str(cmd.path or ""))


def _raw_hint(value: object) -> str:
    """``argument-hint: [env]`` is a hint string, not a YAML list: show it verbatim as "[env]"."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value or "")
