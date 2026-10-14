"""
Axon top-level CLI: argument parsing, print mode, subcommands and the interactive REPL.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from axon.config import ConfigError, PermissionConfig, Settings
from axon.errors import AxonError

SUBCOMMANDS = ("mcp", "doctor", "purge")
_FAILED_STOPS = {"error", "blocked", "interrupted", "budget_exceeded", "token_budget", "max_iterations"}
_ESCAPE_FRAGMENTS = {"[A", "[B", "[C", "[D", "OA", "OB", "OC", "OD", "\x1b"}


# ─── console / stdin helpers ───────────────────────────────────────────────

def _setup_console() -> None:
    """Enable VT + UTF-8 output (Windows) before anything prints."""
    try:
        from axon.platform import setup_console
        setup_console()
        return
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import colorama  # type: ignore[import-not-found]
            colorama.just_fix_windows_console()
        except Exception:
            pass


def _eof_hint() -> str:
    try:
        from axon.platform import eof_hint
        return str(eof_hint())
    except Exception:
        return "Ctrl+Z Enter" if sys.platform == "win32" else "Ctrl+D"


def read_piped_stdin() -> str:
    """Read stdin only when it is a pipe or a redirected file (never block on a TTY/console)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        import stat
        mode = os.fstat(sys.stdin.fileno()).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
            return ""
        return sys.stdin.read().strip()
    except Exception:
        return ""


def _split_tool_list(values: list[str] | None) -> list[str]:
    """Accept `--allowedTools "Read,Grep" "Bash(git *)"` style lists (comma or space separated)."""
    out: list[str] = []
    for v in values or []:
        depth = 0
        buf = ""
        for ch in v:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            if ch in ", " and depth == 0:
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
                continue
            buf += ch
        if buf.strip():
            out.append(buf.strip())
    return out


# ─── parser ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    import axon
    parser = argparse.ArgumentParser(
        prog="axon",
        description="A terminal-native agentic coding assistant.",
        epilog="Subcommands: `axon mcp <list|add|remove|...>` MCP servers, `axon doctor` checks your setup, "
               "`axon setup` guided provider setup, `axon purge` deletes stored sessions/traces.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s v{getattr(axon, '__version__', '?')}")
    parser.add_argument("prompt", nargs="*", default=[], help="Prompt to run (starts the REPL when omitted)")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="Run one prompt non-interactively and exit")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text", help="Print-mode output format")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument("--provider", help="Provider preset id (e.g. anthropic, openai, openrouter, ollama)")
    parser.add_argument("--base-url", dest="base_url", help="Provider API base URL")
    parser.add_argument("--mode", "--permission-mode", dest="mode", type=_permission_mode, metavar="MODE",
                        help="Permission mode: default, acceptEdits, plan, bypass (also bypassPermissions, manual)")
    parser.add_argument("--effort", choices=["reflex", "balanced", "synapse", "quantum", "low", "medium", "high", "xhigh", "max"], help="Reasoning effort tier")
    parser.add_argument("--workspace", help="Workspace root directory")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true", help="Resume the latest session")
    parser.add_argument("-r", "--resume", dest="resume_id", nargs="?", const="", default=None,
                        help="Resume a session by ID or name (no value: open the session picker)")
    parser.add_argument("--fork-session", action="store_true", help="With --continue/--resume: copy the history to a new session")
    parser.add_argument("-n", "--name", dest="session_name", help="Name the session")
    parser.add_argument("--trace", choices=["compact", "normal", "detailed", "debug"], help="Glass-box trace detail level")
    parser.add_argument("--max-budget-usd", dest="max_budget_usd", type=float, help="Session budget guard in USD")
    parser.add_argument("--settings", dest="settings_file", help="Extra settings JSON file or inline JSON (highest file layer)")
    parser.add_argument("--max-turns", dest="max_turns", type=int, help="Maximum agent iterations per prompt")
    parser.add_argument("--allowedTools", "--allowed-tools", dest="allowed_tools", nargs="+", metavar="RULE",
                        help='Tools/rules to allow without asking, e.g. "Read" "Bash(git *)"')
    parser.add_argument("--disallowedTools", "--disallowed-tools", dest="disallowed_tools", nargs="+", metavar="RULE",
                        help='Tools to remove or rules to deny, e.g. "WebFetch" "Bash(rm *)"')
    parser.add_argument("--append-system-prompt", dest="append_system_prompt", help="Extra text appended to the system prompt")
    parser.add_argument("--verbose", action="store_true", help="Verbose output (hook warnings, full events)")
    parser.add_argument("--no-thinking", action="store_true", help="Disable extended reasoning")
    parser.add_argument("--dangerously-skip-permissions", action="store_true",
                        help="Run every tool without asking (forces bypass mode)")
    parser.add_argument("--add-dir", dest="add_dirs", metavar="DIR", action="append",
                        help="Extra directory the session may read and edit (repeat for more)")
    parser.add_argument("--system-prompt", dest="system_prompt", help="Replace Axon's core system prompt")
    parser.add_argument("--system-prompt-file", dest="system_prompt_file", help="Read the replacement system prompt from a file")
    parser.add_argument("--append-system-prompt-file", dest="append_system_prompt_file",
                        help="File whose text is appended to the system prompt (before --append-system-prompt)")
    parser.add_argument("--agent", dest="agent_name", help="Run the main session as this subagent definition")
    parser.add_argument("--agents", dest="agents_json", help="JSON object of session-scoped subagent definitions")
    parser.add_argument("--tools", dest="tools", help='Restrict built-in tools: "Bash,Edit,Read", "" for none, "default" for all')
    parser.add_argument("--mcp-config", dest="mcp_config", nargs="+", metavar="FILE_OR_JSON", action="extend",
                        help="Load MCP servers from JSON files or inline JSON strings")
    parser.add_argument("--strict-mcp-config", action="store_true", help="Only use servers from --mcp-config")
    parser.add_argument("--fallback-model", dest="fallback_model", help="Comma list of models to fail over to on 429/5xx/overload")
    parser.add_argument("--debug", dest="debug", nargs="?", const="all", default=None, metavar="FILTER",
                        help='Debug log to ~/.axon/debug/<session>.log; optional filter e.g. "api,hooks" or "!mcp"')
    parser.add_argument("--debug-file", dest="debug_file", help="Write the debug log to this file")
    parser.add_argument("-w", "--worktree", dest="worktree", nargs="?", const="", default=None, metavar="NAME",
                        help="Run the session in a git worktree (created or reused)")
    return parser


def _permission_mode(value: str) -> str:
    from axon.config import normalize_mode
    mode = normalize_mode(value)
    if mode is None:
        raise argparse.ArgumentTypeError(
            f"invalid permission mode '{value}' (choose default, acceptEdits, plan, bypass, bypassPermissions, manual)")
    return mode


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "provider": args.provider,
        "base_url": args.base_url,
        "mode": args.mode,
        "effort": args.effort,
        "workspace": Path(args.workspace).expanduser().resolve() if args.workspace else None,
        "thinking": False if args.no_thinking else None,
        "dangerously_skip_permissions": True if args.dangerously_skip_permissions else None,
        "max_iterations": args.max_turns if args.max_turns and args.max_turns > 0 else None,
        "append_system_prompt": args.append_system_prompt,
        "verbose": True if args.verbose else None,
        "trace_level": args.trace,
        "budget_session_usd": args.max_budget_usd if args.max_budget_usd and args.max_budget_usd > 0 else None,
        "settings_file": args.settings_file,
        "fallback_model": getattr(args, "fallback_model", None) or None,
    }


def _read_text_arg(path: str, label: str) -> str:
    p = Path(os.path.expanduser(path))
    try:
        from axon.config import decode_config_bytes
        return decode_config_bytes(p.read_bytes()).strip()
    except OSError as e:
        raise ConfigError(f"{label}: cannot read {p}: {e}") from e


def _apply_prompt_flags(settings: Settings, args: argparse.Namespace) -> Settings:
    """--system-prompt(-file), --append-system-prompt-file and --add-dir onto the loaded settings."""
    updates: dict[str, Any] = {}
    override = getattr(args, "system_prompt", None)
    if getattr(args, "system_prompt_file", None):
        override = _read_text_arg(args.system_prompt_file, "--system-prompt-file")
    if override:
        updates["system_prompt_override"] = override
    if getattr(args, "append_system_prompt_file", None):
        file_text = _read_text_arg(args.append_system_prompt_file, "--append-system-prompt-file")
        tail = settings.append_system_prompt or ""
        updates["append_system_prompt"] = (file_text + ("\n\n" + tail if tail else "")).strip()
    if getattr(args, "add_dirs", None):
        dirs = list(settings.additional_dirs)
        for raw in args.add_dirs:
            d = Path(os.path.expanduser(raw)).resolve()
            if not d.is_dir():
                raise ConfigError(f"--add-dir: not a directory: {raw}")
            if d not in dirs:
                dirs.append(d)
        updates["additional_dirs"] = dirs
    return settings.model_copy(update=updates) if updates else settings


def _apply_agent_flag(settings: Settings, name: str) -> tuple[Settings, Any]:
    """--agent <name>: the main session takes that agent's prompt, model and tool list."""
    from axon.agent.subagent import get_agent_definition, load_agent_definitions
    d = get_agent_definition(settings.workspace, name)
    if d is None:
        names = ", ".join(sorted(x.name for x in load_agent_definitions(settings.workspace)))
        raise ConfigError(f"--agent: unknown agent '{name}'. Available: {names}")
    updates: dict[str, Any] = {}
    model = (d.model or "").strip()
    if model and model.lower() not in ("inherit", "default", "sonnet", "opus", "haiku"):
        updates["model"] = model
    if d.max_turns:
        updates["max_iterations"] = int(d.max_turns)
    return (settings.model_copy(update=updates) if updates else settings), d


def setup_debug_logging(filter_spec: str | None, file: str | None, session_id: str = "") -> Path | None:
    """--debug[=filter] / --debug-file: stdlib logging of axon.* loggers to a file.
    Categories: api (providers), hooks, mcp, permissions, agent, tools; "!cat" excludes."""
    import logging
    if filter_spec is None and not file:
        return None
    if file:
        path = Path(os.path.expanduser(file))
    else:
        from axon import storage
        path = storage.ensure_dir(storage.axon_home() / "debug") / f"{session_id or time.strftime('%Y%m%d-%H%M%S')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    cats = {"api": "axon.providers", "providers": "axon.providers", "hooks": "axon.hooks", "mcp": "axon.mcp",
            "permissions": "axon.permissions", "agent": "axon.agent", "tools": "axon.tools", "session": "axon.session"}
    spec = [c.strip().lower() for c in (filter_spec or "all").split(",") if c.strip()]
    include = [cats[c] for c in spec if c in cats]
    exclude = [cats[c[1:]] for c in spec if c.startswith("!") and c[1:] in cats]

    class _Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            name = record.name
            if any(name.startswith(x) for x in exclude):
                return False
            return not include or any(name.startswith(x) for x in include)

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_Filter())
    root = logging.getLogger("axon")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.debug("debug logging started (filter=%s)", filter_spec or "all")
    return path


def _mcp_specs_from_args(values: list[str], workspace: Path) -> dict[str, Any]:
    """--mcp-config values (files or inline JSON) -> {name: ServerSpec}."""
    from axon.mcp.config import ServerSpec, _servers_in
    specs: dict[str, Any] = {}
    for raw in values:
        text = raw.strip()
        source: Path | None = None
        if not text.startswith("{"):
            source = Path(os.path.expanduser(text))
            if not source.is_absolute():
                source = workspace / source
            text = _read_text_arg(str(source), "--mcp-config")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ConfigError(f"--mcp-config: invalid JSON ({raw[:60]}): {e}") from e
        servers = _servers_in(data) if isinstance(data, dict) else {}
        for name, spec in servers.items():
            try:
                specs[str(name)] = ServerSpec.from_dict(str(name), spec, "cli", source)
            except ValueError as e:
                raise ConfigError(f"--mcp-config: server '{name}': {e}") from e
    return specs


def _apply_tool_rules(settings: Settings, allowed: list[str], disallowed: list[str]) -> Settings:
    if not allowed and not disallowed:
        return settings
    deny_rules = [r for r in disallowed]
    perms = PermissionConfig(
        allow=list(dict.fromkeys(list(settings.permissions.allow) + allowed)),
        deny=list(dict.fromkeys(list(settings.permissions.deny) + deny_rules)),
    )
    return settings.model_copy(update={"permissions": perms})


# ─── agent construction ────────────────────────────────────────────────────

def build_agent(
    settings: Settings,
    *,
    on_event: Callable[[Any], None] | None = None,
    on_approval: Callable[[str, dict[str, Any], Any], str] | None = None,
    connect_mcp: bool = True,
    disallowed_tools: list[str] | None = None,
    announce: Callable[[str], None] | None = None,
    provider: Any = None,
    tools_spec: str | None = None,
    mcp_specs: dict[str, Any] | None = None,
    strict_mcp: bool = False,
    agent_def: Any = None,
) -> Any:
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent
    from axon.permissions.engine import PermissionEngine
    from axon.providers.registry import provider_for
    from axon.session.ledger import Ledger
    from axon.session.store import SessionStore
    from axon.tools import create_default_registry, restrict_tools

    provider_given = provider is not None
    if provider is None:
        provider = provider_for(settings.model, settings)
    tools = create_default_registry()
    if tools_spec is not None:
        tools = restrict_tools(tools, tools_spec)
    if agent_def is not None and getattr(agent_def, "tools", None):
        tools = restrict_tools(tools, list(agent_def.tools) + ["TodoWrite"])

    if connect_mcp and (mcp_specs or not strict_mcp):
        try:
            from axon.mcp.bridge import get_bridge, initialize_mcp_bridge, shutdown_mcp_bridge
            import atexit
            atexit.register(shutdown_mcp_bridge)
            # With registry=, servers that finish connecting later register their tools live.
            mcp_tools = [] if strict_mcp else initialize_mcp_bridge(settings.workspace, registry=tools)
            if mcp_specs:
                bridge = get_bridge(settings.workspace)
                bridge.attach_registry(tools)
                mcp_tools = list(mcp_tools) + list(bridge.connect_all(mcp_specs) or [])
            for mcp_tool in mcp_tools:
                if mcp_tool.name not in tools.tools:
                    tools.register(mcp_tool)
            if announce:
                try:
                    rows = get_bridge().status_rows() if hasattr(get_bridge(), "status_rows") else []
                    failed = any(r.get("status") in ("failed", "needs-config", "disconnected") for r in rows)
                    summary = get_bridge().startup_summary()
                    if summary and (failed or settings.verbose):
                        announce(summary)
                    elif mcp_tools:
                        announce(f"MCP: {len(mcp_tools)} tool(s) loaded from configured servers")
                except Exception:
                    pass
        except Exception as e:
            if settings.verbose and announce:
                announce(f"MCP bridge unavailable: {e}")

    bare = {d for d in (disallowed_tools or []) if "(" not in d}
    if agent_def is not None:
        bare |= {str(t) for t in getattr(agent_def, "disallowed_tools", []) or []}
    if bare:
        keep = [t.name for t in tools.all_tools() if t.name not in bare]
        tools = tools.subset(names=keep)

    context = ContextManager(settings)
    try:
        from axon.tools.interactive import defer_mcp_tools
        search = defer_mcp_tools(tools, context.context_window(settings.model))
        if search is not None and announce and settings.verbose:
            announce(f"MCP: {len(search.deferred)} tool schema(s) deferred behind ToolSearch")
    except Exception:
        pass

    agent = Agent(
        provider=provider,
        tools=tools,
        permissions=PermissionEngine(settings),
        context=context,
        session=SessionStore(workspace=settings.workspace),
        ledger=Ledger(),
        settings=settings,
        on_event=on_event,
        on_approval=on_approval,
        extra_system=str(getattr(agent_def, "system_prompt", "") or "") if agent_def is not None else "",
    )
    if agent_def is not None:
        agent.main_agent_name = agent_def.name
    if settings.small_model and getattr(settings, "auto_title", True) and not provider_given:
        try:
            from axon.agent.loop import make_provider
            agent.title_provider = make_provider(settings.small_model, settings.model_copy(update={"model": settings.small_model}),
                                                 role="title")
        except Exception:
            agent.title_provider = None
    return agent


def find_session(agent: Any, query: str) -> tuple[str | None, list[str]]:
    """Resolve a session by exact ID, unique ID prefix, exact name, or unique name substring.
    Returns (session_id or None, candidates when ambiguous)."""
    q = query.strip()
    if q.endswith(".jsonl"):
        q = Path(q).stem
    try:
        metas = agent.session.list_recent()
    except Exception:
        metas = []
    ids = [m.session_id for m in metas]
    if q in ids:
        return q, []
    if (agent.session.session_dir / f"{q}.jsonl").is_file():
        return q, []
    pref = [i for i in ids if i.startswith(q)]
    if len(pref) == 1:
        return pref[0], []
    names = [(m.session_id, (m.first_prompt or "").strip()) for m in metas]
    exact = [sid for sid, name in names if name.lower() == q.lower()]
    if len(exact) == 1:
        return exact[0], []
    sub = [sid for sid, name in names if q.lower() in name.lower()]
    if len(sub) == 1:
        return sub[0], []
    return None, (exact or sub or pref)


def fork_session(agent: Any, say: Callable[[str], None]) -> str:
    """Copy the active session's transcript to a new session ID and switch to it."""
    import shutil
    old_id = agent.session.active_session_id
    old_file = agent.session.session_dir / f"{old_id}.jsonl"
    new_id = agent.session._generate_session_id() if hasattr(agent.session, "_generate_session_id") else None
    new_id = agent.session.open(new_id)
    try:
        if old_file.is_file():
            shutil.copyfile(old_file, agent.session.session_dir / f"{new_id}.jsonl")
        from axon.agent.trace import trace_file
        old_traces = trace_file(agent.settings.workspace, old_id)
        if old_traces.is_file():
            shutil.copyfile(old_traces, trace_file(agent.settings.workspace, new_id))
    except OSError as e:
        say(f"Could not copy transcript: {e}")
    say(f"Forked session {old_id} -> {new_id}")
    return new_id


def session_state(agent: Any, session_id: str) -> dict[str, Any]:
    """Scan a transcript for state to restore: last permission mode, custom title, turn count,
    last user prompt and last assistant text."""
    state: dict[str, Any] = {"mode": None, "title": None, "turns": 0, "last_prompt": "", "last_answer": ""}
    try:
        from axon.session.store import iter_records
        path = agent.session._file(session_id) if hasattr(agent.session, "_file") else agent.session.session_dir / f"{session_id}.jsonl"
        for rec in iter_records(path):
            kind, data = rec.get("type"), rec.get("data")
            if kind == "permission_mode" and isinstance(data, dict):
                state["mode"] = data.get("mode")
            elif kind == "session_rename" and isinstance(data, dict):
                state["title"] = data.get("title")
            elif kind == "user_message" and isinstance(data, dict) and isinstance(data.get("content"), str):
                text = data["content"]
                if not text.startswith("[") or not text.endswith("hook feedback]"):
                    state["turns"] += 1
                    state["last_prompt"] = text
            elif kind == "assistant_turn" and isinstance(data, dict) and str(data.get("text") or "").strip():
                state["last_answer"] = str(data["text"])
    except Exception:
        pass
    return state


def _one_line(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _resume(agent: Any, session_id: str, say: Callable[[str], None], restore_mode: bool = True) -> bool:
    try:
        agent.session.open(session_id)
        agent.conversation = agent.session.load(session_id)
        agent.conversation.repair()
        agent.ledger = agent.session.load_ledger(session_id, agent.settings.model)
    except Exception as e:
        say(f"Could not resume session {session_id}: {e}")
        return False
    bind = getattr(agent, "_bind_checkpoints", None)
    if callable(bind):
        bind()
    state = session_state(agent, session_id)
    restored_mode = ""
    from axon.config import normalize_mode
    mode = normalize_mode(state.get("mode"))
    if restore_mode and mode and mode != agent.settings.mode and not getattr(agent.settings, "dangerously_skip_permissions", False):
        agent.settings = agent.settings.model_copy(update={"mode": mode})
        try:
            agent.permissions.settings = agent.settings
        except Exception:
            pass
        restored_mode = mode
    if state.get("title") and hasattr(agent, "mark_titled"):
        agent.mark_titled(state["title"])
    tot_tok = agent.ledger.total_input_tokens + agent.ledger.total_output_tokens
    name = f" \"{state['title']}\"" if state.get("title") else ""
    say(f"Resumed session {session_id}{name} ({len(agent.conversation.messages)} msgs · {tot_tok:,} tokens · ${agent.ledger.total():.5f})")
    if state.get("turns"):
        say(f"  Recap: {state['turns']} prompt(s). Last: \"{_one_line(state['last_prompt'], 90)}\"")
        if state.get("last_answer"):
            say(f"  Last answer: {_one_line(state['last_answer'], 110)}")
    if restored_mode:
        say(f"  Permission mode restored: {restored_mode}")
    return True


# ─── shell shortcut ────────────────────────────────────────────────────────

def run_shell_shortcut(cmd_shell: str, sink: list[str] | None = None) -> int:
    """Run a `!command` line in the platform shell (PowerShell/cmd on Windows, bash/sh elsewhere).

    Output streams to the terminal; with ``sink`` it is also captured (stdout+stderr merged)
    so the caller can add it to the conversation."""
    from axon.ui.theme import GOLD, MINT, ROSE, RST, WHITE
    cmd_shell = cmd_shell.strip()
    if not cmd_shell:
        return 0
    print(f"\n  {GOLD}Shell:{RST} {WHITE}{cmd_shell}{RST}\n")
    if cmd_shell == "cd" or cmd_shell.startswith("cd "):
        target_dir = cmd_shell[2:].strip().strip('"').strip("'") or str(Path.home())
        target_path = Path(os.path.expandvars(target_dir)).expanduser()
        if not target_path.is_absolute():
            target_path = Path.cwd() / target_path
        if target_path.is_dir():
            os.chdir(target_path)
            msg = f"Changed directory to {target_path.resolve()}"
            print(f"  {MINT}{msg}{RST}\n")
            if sink is not None:
                sink.append(msg)
            return 0
        print(f"  {ROSE}Directory not found: {target_dir}{RST}\n")
        if sink is not None:
            sink.append(f"Directory not found: {target_dir}")
        return 1
    from axon.hooks.manager import platform_shell_argv
    try:
        proc = subprocess.Popen(
            platform_shell_argv(cmd_shell), cwd=os.getcwd(),
            stdout=subprocess.PIPE if sink is not None else None,
            stderr=subprocess.STDOUT if sink is not None else None,
            stdin=subprocess.DEVNULL if sink is not None else None,
        )
    except Exception as e:
        print(f"  {ROSE}Command failed to start: {e}{RST}\n")
        if sink is not None:
            sink.append(f"Command failed to start: {e}")
        return 1
    try:
        if sink is not None and proc.stdout is not None:
            chunks: list[str] = []
            total = 0
            while True:
                data = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                try:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                except Exception:
                    pass
                if total < 400_000:
                    chunks.append(text)
                    total += len(text)
            sink.append("".join(chunks))
        code = proc.wait()
    except KeyboardInterrupt:
        try:
            from axon.platform import kill_process_tree
            kill_process_tree(proc)
        except Exception:
            proc.kill()
        print(f"\n  {ROSE}Interrupted.{RST}\n")
        if sink is not None:
            sink.append("[interrupted by user]")
        return 130
    if code:
        print(f"\n  {ROSE}exit code {code}{RST}")
    print()
    return code


def _shell_into_context(agent: Any, renderer: Any, command: str) -> None:
    """`!cmd`: run it, then hand command + output to the model as context for the next prompt
    (and run a turn now when settings.respond_to_bash_commands)."""
    from axon.ui.theme import RST, SLATE
    sink: list[str] = []
    cwd_before = os.getcwd()
    code = run_shell_shortcut(command, sink=sink)
    output = "".join(sink)
    if os.getcwd() != cwd_before:
        hook = getattr(agent, "_hook", None)
        if callable(hook):
            hook("CwdChanged", {"old_cwd": cwd_before, "new_cwd": os.getcwd()})
    add = getattr(agent, "add_shell_context", None)
    if not callable(add):
        return
    add(command, output, code)
    if getattr(agent.settings, "respond_to_bash_commands", False):
        _run_agent_turn(agent, renderer, "", "Shell follow-up")
    else:
        print(f"  {SLATE}(output added to context for your next message){RST}\n")


# ─── prompt preprocessing ──────────────────────────────────────────────────

_MENTION_RE = re.compile(r"(?<![\w@])@([\w\-./\\~]+[\w/\\])")
_RESOURCE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_\-]+):(\S+)")
MENTION_MAX_BYTES = 256 * 1024
_DIR_LIST_MAX = 200


def _dir_listing(p: Path, limit: int = _DIR_LIST_MAX) -> str:
    entries: list[str] = []
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError:
        return ""
    for x in items:
        if x.name in (".git", "__pycache__", "node_modules", ".venv"):
            continue
        entries.append(x.name + ("/" if x.is_dir() else ""))
        if len(entries) >= limit:
            entries.append(f"... ({len(items) - limit} more)")
            break
    return "\n".join(entries) or "(empty directory)"


def _expand_resources(line: str, limit: int) -> str:
    """`@server:uri` -> the MCP resource's content (connected servers only)."""
    try:
        from axon.mcp.bridge import get_bridge
        bridge = get_bridge()
    except Exception:
        return line
    for server, uri in sorted(set(_RESOURCE_RE.findall(line)), key=lambda t: -len(t[0]) - len(t[1])):
        try:
            conn = bridge.get_connection(server)
            text = conn.read_resource(uri)
        except Exception:
            continue
        note = ""
        if len(text) > limit:
            text, note = text[:limit], f"\n[... truncated at {limit:,} characters ...]"
        line = line.replace(f"@{server}:{uri}", f"\n\n[Resource: {server}:{uri}]\n```\n{text}{note}\n```\n")
    return line


def expand_file_mentions(line: str, workspace: Path, limit: int = 20_000) -> str:
    """Inline `@path` mentions: files as fenced context, directories as a listing, and
    `@server:uri` MCP resources. Files over 256 KB are skipped; AXON_DISABLE_FILE_MENTIONS=1 disables it."""
    if "@" not in line or line.startswith("/"):
        return line
    if os.environ.get("AXON_DISABLE_FILE_MENTIONS", "").strip().lower() in ("1", "true", "yes"):
        return line
    if ":" in line:
        line = _expand_resources(line, limit)
    for m in sorted(set(_MENTION_RE.findall(line)), key=len, reverse=True):
        p = Path(m).expanduser()
        if not p.is_absolute():
            p = workspace / m
        try:
            if p.is_dir():
                line = line.replace(f"@{m}", f"\n\n[Directory: {m}]\n```\n{_dir_listing(p)}\n```\n")
                continue
            if not p.is_file() or p.stat().st_size > MENTION_MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        note = ""
        if len(text) > limit:
            text, note = text[:limit], f"\n[... truncated at {limit:,} characters ...]"
        line = line.replace(f"@{m}", f"\n\n[File: {m}]\n```\n{text}{note}\n```\n")
    return line


# ─── print mode ────────────────────────────────────────────────────────────

class _StreamJsonEmitter:
    """--output-format stream-json: one JSON object per line."""

    def __init__(self, out: Any = None) -> None:
        self.out = out or sys.stdout

    def emit(self, obj: dict[str, Any]) -> None:
        try:
            self.out.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
            self.out.flush()
        except Exception:
            pass

    def on_event(self, e: Any) -> None:
        try:
            from axon.features.sdk import event_to_dict  # type: ignore[import-not-found]
            d = event_to_dict(e)
            if d:
                self.emit(d)
            return
        except (ImportError, AttributeError):
            pass
        except Exception:
            return
        from axon.providers.base import LLMCallStart, TextDelta, ThinkingDelta, ToolExecutionResult, ToolExecutionStart
        if isinstance(e, TextDelta):
            self.emit({"type": "text_delta", "text": e.text})
        elif isinstance(e, ThinkingDelta):
            self.emit({"type": "thinking_delta", "text": e.text})
        elif isinstance(e, ToolExecutionStart):
            self.emit({"type": "tool_use", "id": e.id, "name": e.name, "input": e.input})
        elif isinstance(e, ToolExecutionResult):
            self.emit({"type": "tool_result", "tool_use_id": e.id, "name": e.name, "content": e.content, "is_error": e.is_error})
        elif isinstance(e, LLMCallStart):
            self.emit({"type": "llm_call", "iteration": e.iteration, "model": e.model, "tokens_in": e.tokens_in})


def _result_payload(agent: Any, result: Any, elapsed: float) -> dict[str, Any]:
    return {
        "type": "result",
        "result": result.final_text,
        "is_error": result.stop_reason in _FAILED_STOPS,
        "stop_reason": result.stop_reason,
        "session_id": getattr(agent.session, "active_session_id", ""),
        "cost_usd": float(agent.ledger.total()),
        "iterations": result.iterations,
        "tool_calls": result.tool_calls_count,
        "usage": {
            "input_tokens": result.usage.input,
            "output_tokens": result.usage.output,
            "cache_read_tokens": result.usage.cache_read,
            "cache_write_tokens": result.usage.cache_write,
        },
        "elapsed_seconds": round(elapsed, 3),
    }


def run_print_mode(agent: Any, prompt: str, fmt: str, renderer: Any = None) -> int:
    """Headless single-prompt execution. Slash commands and '#' quick memories are dispatched like
    in the REPL; prompts they queue (e.g. /loop 2x) run before exit, within --max-turns and the budget."""
    if not prompt.strip():
        sys.stderr.write("axon: no prompt given (pass it as an argument or pipe it on stdin)\n")
        return 2
    if prompt.startswith(("/", "#")):
        from axon.commands.builtin import dispatch_command
        res = dispatch_command(prompt, agent)
        if res is not None:
            code = 0 if getattr(res, "success", True) else 1
            try:
                return _drain_print_queue(agent, fmt, renderer, code)
            finally:
                _stop_loops(agent)
    return _print_turn(agent, prompt, fmt, renderer)


def _drain_print_queue(agent: Any, fmt: str, renderer: Any, code: int) -> int:
    queue = getattr(agent, "message_queue", None)
    limit = max(1, int(getattr(agent.settings, "max_iterations", 50) or 50))
    ran = 0
    while queue is not None and len(queue) and ran < limit:
        item = queue.pop()
        if item is None:
            break
        ran += 1
        code = _print_turn(agent, str(getattr(item, "text", item)), fmt, renderer)
        if code == 130 or getattr(agent, "_last_print_stop", "") in ("budget_exceeded", "blocked"):
            break
    return code


def _stop_loops(agent: Any) -> None:
    for job in list(getattr(agent, "loops", None) or []):
        try:
            job.stop.set()
        except Exception:
            pass


def _print_turn(agent: Any, prompt: str, fmt: str, renderer: Any = None) -> int:
    prompt = expand_file_mentions(prompt, agent.settings.workspace)
    t0 = time.time()
    try:
        result = agent.run_turn(prompt)
    except KeyboardInterrupt:
        sys.stderr.write("\naxon: interrupted\n")
        return 130
    elapsed = time.time() - t0
    try:
        agent._last_print_stop = result.stop_reason
    except Exception:
        pass

    if fmt in ("json", "stream-json"):
        payload = _result_payload(agent, result, elapsed)
        if fmt == "json":
            print(json.dumps(payload, indent=2, default=str))
        else:
            _StreamJsonEmitter().emit(payload)
    elif renderer is not None:
        shown = render_turn_footer(renderer, agent, result, elapsed)
        if result.stop_reason in _FAILED_STOPS and result.final_text and not (shown and sys.stderr.isatty()):
            sys.stderr.write(f"\n{result.final_text}\n")
    else:
        print(f"\n{result.final_text}\n")
    return 1 if result.stop_reason in _FAILED_STOPS else 0


# ─── REPL ──────────────────────────────────────────────────────────────────

def _print_banner(agent: Any, renderer: Any, clear: bool = False) -> None:
    import axon
    if clear and sys.stdin.isatty() and sys.stdout.isatty():
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()
    renderer.print_banner(
        version=getattr(axon, "__version__", ""),
        model=agent.settings.model,
        effort=agent.settings.effort,
        workspace=str(agent.settings.workspace),
        mode=agent.settings.mode,
        base_url=agent.settings.base_url,
    )


def _open_session_switcher(agent: Any, renderer: Any) -> str | None:
    """Run the session dashboard. Returns a prompt to run in a new session, if any."""
    from axon.ui.switcher import run_session_dashboard
    from axon.ui.theme import MINT, ROSE, RST
    selected = run_session_dashboard(agent)
    _print_banner(agent, renderer, clear=True)
    if not selected:
        if agent.conversation.messages:
            from axon.session.interactive import render_restored_conversation
            render_restored_conversation(agent.conversation, agent.session.active_session_id, ledger=agent.ledger)
        return None
    if selected.startswith("__NEW_SESSION__:"):
        prompt_buf = selected.split(":", 1)[1].strip()
        new_id = agent.reset_for_new_session()
        if not prompt_buf:
            print(f"  {MINT}Started fresh session ({new_id}){RST}\n")
        return prompt_buf or None
    try:
        agent.session.open(selected)
        agent._titled, agent._title = None, None
        if callable(getattr(agent, "_bind_checkpoints", None)):
            agent._bind_checkpoints()
        agent.conversation = agent.session.load(selected)
        agent.conversation.repair()
        agent.ledger = agent.session.load_ledger(selected, agent.settings.model)
        from axon.agent.subagent import sync_subagents_for_session
        sync_subagents_for_session(agent)
        from axon.session.interactive import render_restored_conversation
        render_restored_conversation(agent.conversation, selected, ledger=agent.ledger)
    except Exception as e:
        print(f"\n  {ROSE}Failed to switch session: {e}{RST}\n")
    return None


def _quick_memory(agent: Any, line: str) -> None:
    """`# note` saves a project memory; `#~ note` (or `#user note`) saves a user memory."""
    from axon.agent.memory import MemoryStore
    from axon.ui.theme import MINT, ROSE, RST, SLATE
    body = line[1:]
    scope = "project"
    if body.startswith("~"):
        scope, body = "global", body[1:]
    elif body.lower().startswith(("user ", "global ")):
        scope, body = "global", body.split(" ", 1)[1]
    try:
        item = MemoryStore(agent.settings.workspace).add(body, scope=scope)
    except Exception as e:
        print(f"  {ROSE}Could not save memory: {e}{RST}\n")
        return
    where = "~/.axon/memory" if scope == "global" else ".axon/memory"
    print(f"  {MINT}Saved {'user' if scope == 'global' else 'project'} memory:{RST} {item.title} {SLATE}({where}/{item.id}.md){RST}\n")


def render_turn_footer(renderer: Any, agent: Any, res: Any, elapsed: float) -> bool:
    """Turn footer that never claims success for a failed turn. The renderer shows the ✗ line and
    the error (stop_reason/error keywords); a duck-typed renderer without them gets a ✗ line here.
    Returns True when the failure detail was shown."""
    import inspect
    if renderer is None:
        return False
    kwargs: dict[str, Any] = dict(tool_count=res.tool_calls_count, usage=res.usage, cost=float(agent.get_combined_cost()),
                                  elapsed=elapsed, llm_calls=res.iterations)
    if res.stop_reason not in _FAILED_STOPS:
        renderer.turn_footer(**kwargs)
        return False
    error = str(res.error or res.final_text or "")
    try:
        params = inspect.signature(renderer.turn_footer).parameters
    except (TypeError, ValueError, AttributeError):
        params = {}
    if "stop_reason" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        renderer.turn_footer(**kwargs, stop_reason=res.stop_reason, error=error)
        return True
    try:
        spinner = getattr(renderer, "spinner", None)
        if spinner is not None:
            spinner.stop()
    except Exception:
        pass
    from axon.ui.theme import ROSE, RST, SLATE
    detail = " ".join(error.split())[:160]
    print(f"  {ROSE}✗ Failed{RST} {SLATE}· {elapsed:.1f}s · {res.iterations} LLM call(s) · "
          f"{res.tool_calls_count} tool call(s){(' · ' + detail) if detail else ''}{RST}\n")
    return True


def show_suggestions(agent: Any, res: Any) -> str:
    """After the footer: one dim 'Next: ...' line from features.suggest (settings.suggestions)."""
    if not getattr(agent.settings, "suggestions", True):
        return ""
    try:
        from axon.features.suggest import render_suggestions, suggest_next
        line = render_suggestions(suggest_next(agent, res))
    except Exception:
        return ""
    if line:
        print(line)
    return line or ""


def _use_turn_thread(agent: Any) -> bool:
    if not callable(getattr(agent, "run_turn_threaded", None)):
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _run_agent_turn(agent: Any, renderer: Any, turn_input: str, label: str) -> None:
    from axon.ui.theme import CYAN, ROSE, RST, SLATE, TEAL
    try:
        from axon.ui.in_flight import InFlightInputListener
        listener: Any = InFlightInputListener(agent)
    except Exception:
        import contextlib
        listener = contextlib.nullcontext()
    t0 = time.time()
    try:
        with listener:
            if _use_turn_thread(agent):
                res = agent.run_turn_threaded(turn_input)
            else:
                res = agent.run_turn(turn_input)
    except KeyboardInterrupt:
        print(f"\n\n  {TEAL}Stopped by user.{RST}\n")
        return
    except AxonError as e:
        print(f"\n  {ROSE}Error: {e}{RST}\n")
        return
    except Exception as e:
        print(f"\n  {ROSE}Error during execution: {type(e).__name__}: {e}{RST}\n")
        return
    if res is None:  # moved to the background with Ctrl+B
        bg = getattr(agent, "_active_bg", None)
        tag = getattr(bg, "id", "background")
        print(f"\n  {CYAN}⇢ Turn moved to the background ({tag}).{RST} {SLATE}Prompts you send now are queued until it "
              f"finishes; /tasks shows progress.{RST}\n")
        return
    elapsed = time.time() - t0
    tot_cost = float(agent.get_combined_cost())
    if not render_turn_footer(renderer, agent, res, elapsed) and res.stop_reason == "error" and res.final_text:
        print(f"\n  {ROSE}{res.final_text}{RST}\n")
    show_suggestions(agent, res)
    try:
        from axon.ui.notify import notify_if_unfocused
        notify_if_unfocused(title="Axon", message=f"{label} finished ({elapsed:.1f}s · ${tot_cost:.4f})")
    except Exception:
        pass
    agent.notify(f"{label} finished", "turn_complete")


def report_background(agent: Any, renderer: Any) -> int:
    """Print the outcome of background turns that finished since the last prompt."""
    from axon.ui.theme import CYAN, ROSE, RST, SLATE
    shown = 0
    for rec in list(getattr(agent, "background_tasks", {}).values()):
        if not getattr(rec, "done", False) or getattr(rec, "reported", True):
            continue
        rec.reported = True
        shown += 1
        res = rec.result
        color = CYAN if rec.status == "completed" else ROSE
        print(f"\n  {color}⇠ Background turn {rec.id} {rec.status}{RST} {SLATE}({rec.elapsed_s:.1f}s) · "
              f"{_one_line(rec.prompt, 70)}{RST}")
        if res is not None and getattr(res, "final_text", ""):
            try:
                from axon.ui.markdown import format_markdown  # type: ignore[attr-defined]
                print(format_markdown(res.final_text))
            except Exception:
                print(res.final_text)
            render_turn_footer(renderer, agent, res, rec.elapsed_s)
        elif rec.error:
            print(f"  {ROSE}{rec.error}{RST}\n")
    return shown


_BUSY_BLOCKED = ("/clear", "/new", "/reset", "/compact", "/resume", "/rewind", "/branch", "/fork", "/model", "/exit", "/quit")


def process_line(agent: Any, renderer: Any, line: str, from_queue: bool = False) -> bool:
    """Handle one REPL input line. Returns False when the session should end."""
    from axon.commands.builtin import dispatch_command
    from axon.ui.theme import BOLD, CYAN, GOLD, MINT, RST, SLATE, TEAL, WHITE

    line = line.strip()
    if not line or line in _ESCAPE_FRAGMENTS:
        return True

    if line == "__TRACE_CYCLE__":
        cycle = getattr(renderer, "cycle_trace_level", None)
        if callable(cycle):
            level = cycle()
            try:
                agent.settings = agent.settings.model_copy(update={"trace_level": level})
            except Exception:
                pass
            print(f"  {SLATE}trace level: {level}{RST}")
        return True
    if line == "__REWIND__":
        line = "/rewind"

    if not from_queue and (line == "__SWITCH_SESSION__" or line.lower() in ("/sessions", "/dashboard", "/switcher", "/chats")):
        new_prompt = _open_session_switcher(agent, renderer)
        if not new_prompt:
            return True
        line = new_prompt

    if line.startswith("!"):
        _shell_into_context(agent, renderer, line[1:])
        return True

    if line.startswith("#") and "\n" not in line and len(line) > 1 and not line.startswith("#!"):
        _quick_memory(agent, line)
        return True

    if getattr(agent, "busy", False) and not from_queue:
        head = line.split(maxsplit=1)[0].lower() if line.startswith("/") else ""
        if not line.startswith("/") or head in _BUSY_BLOCKED:
            if head in ("/exit", "/quit"):
                print(f"  {SLATE}A background turn is still running; press Ctrl+C twice to quit anyway.{RST}\n")
                return True
            item = agent.message_queue.push(line)
            print(f"  {CYAN}Queued #{item.id}: it runs when the background turn finishes.{RST}\n")
            return True

    if line.startswith("/"):
        cmd_res = dispatch_command(line, agent)
        if cmd_res is not None:
            if cmd_res.should_exit:
                print(f"\n  {TEAL}Goodbye! Total cost: {GOLD}${agent.get_combined_cost():.5f}{RST}\n")
                return False
            return True

    line = expand_file_mentions(line, agent.settings.workspace)

    if not from_queue and ";;" in line and not line.startswith("/"):
        parts = [p.strip() for p in line.split(";;") if p.strip()]
        if len(parts) > 1:
            line = parts[0]
            for extra in parts[1:]:
                agent.message_queue.push(extra)
            n = len(parts) - 1
            print(f"\n  {MINT}Queued {n} follow-up question{'s' if n > 1 else ''} (in queue: {len(agent.message_queue)}){RST}\n")

    try:
        from axon.agent.images import compact_image_paths
        line, attachments = compact_image_paths(line)
    except Exception:
        attachments = []
    if attachments:
        print(f"  {CYAN}Attached image{'s' if len(attachments) != 1 else ''} ({len(attachments)}):{RST}")
        for att in attachments:
            dim_str = f" · {att.width}x{att.height}" if att.width and att.height else ""
            print(f"    {GOLD}{att.label}{RST} {WHITE}{BOLD}{Path(att.original_path).name}{RST} {SLATE}({att.size_bytes / 1024:.1f} KB{dim_str}){RST}")
        print()

    _run_agent_turn(agent, renderer, line, "Queued prompt" if from_queue else "Turn")
    return True


def drain_queue(agent: Any, renderer: Any) -> bool:
    """Process queued prompts in order. Returns False if a queued command ended the session."""
    from axon.ui.theme import CYAN, RST
    while len(agent.message_queue) > 0 and not getattr(agent, "busy", False):
        nxt = agent.message_queue.pop()
        if not nxt or not nxt.text.strip() or nxt.text.strip() in _ESCAPE_FRAGMENTS:
            continue
        remaining = len(agent.message_queue)
        rem_str = f" · {remaining} remaining" if remaining else ""
        try:
            from axon.ui.input import format_user_prompt_banner
            banner = format_user_prompt_banner(nxt.text)
        except Exception:
            banner = nxt.text
        print(f"\n  {CYAN}[Queued #{nxt.id}{rem_str}]{RST}\n{banner}\n")
        try:
            if not process_line(agent, renderer, nxt.text, from_queue=True):
                return False
        except KeyboardInterrupt:
            print("\n  Stopped queued prompt.\n")
            return True
    return True


def run_repl(agent: Any, renderer: Any) -> int:
    """Interactive terminal REPL."""
    from axon.ui.input import read_input
    from axon.ui.theme import GOLD, RST, SLATE, TEAL

    if sys.stdin.isatty() and sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H\n")
        sys.stdout.flush()
    _print_banner(agent, renderer)
    try:
        from axon.agent.subagent import sync_subagents_for_session
        sync_subagents_for_session(agent)
    except Exception:
        pass

    last_interrupt = 0.0
    while True:
        if report_background(agent, renderer) and not getattr(agent, "busy", False):
            if not drain_queue(agent, renderer):
                return 0
        try:
            plan_summary = None
            if agent.todos.items:
                comp, tot, pct = agent.todos.progress()
                if tot:
                    plan_summary = f"Plan: {comp}/{tot} ({pct}%)"
            queue_summary = f"Queue: {len(agent.message_queue)} pending" if len(agent.message_queue) else None
            curr_id = agent.session.active_session_id
            subagent_label = f"Subagent #{curr_id.split('_sub_')[-1]}" if "_sub_" in curr_id else None
            prefill = getattr(agent, "pending_input", None) or ""
            if prefill:
                agent.pending_input = None
            res_input = read_input(
                initial=prefill,
                mode=agent.settings.mode,
                effort=agent.settings.effort,
                plan_summary=plan_summary,
                queue_summary=queue_summary,
                subagent_label=subagent_label,
                session_id=curr_id,
            )
            line, toggled_mode = res_input[0], res_input[1]
            if toggled_mode:
                if callable(getattr(agent, "set_mode", None)):
                    agent.set_mode(toggled_mode)
                else:
                    agent.settings = agent.settings.model_copy(update={"mode": toggled_mode})
                    agent.permissions.settings = agent.settings
        except KeyboardInterrupt:
            now = time.time()
            if now - last_interrupt < 1.5:
                print(f"\n  {TEAL}Goodbye! Total cost: {GOLD}${agent.get_combined_cost():.5f}{RST}\n")
                return 0
            last_interrupt = now
            print(f"\n  {SLATE}(Input cleared · press Ctrl+C again, {_eof_hint()} or /exit to quit){RST}\n")
            continue
        except EOFError:
            print(f"\n\n  {TEAL}Session closed. Total cost: {GOLD}${agent.get_combined_cost():.5f}{RST}\n")
            return 0

        line = (line or "").strip()
        if not line:
            if getattr(agent, "busy", False):
                continue
            nxt = agent.message_queue.pop() if len(agent.message_queue) else None
            if nxt is None:
                continue
            line = nxt.text

        try:
            if not process_line(agent, renderer, line):
                return 0
            if not drain_queue(agent, renderer):
                return 0
        except KeyboardInterrupt:
            print(f"\n\n  {TEAL}Stopped by user.{RST}\n")
        except Exception as e:
            print(f"\n  Turn error: {type(e).__name__}: {e}\n")


# ─── subcommands ───────────────────────────────────────────────────────────

def _load_settings_lenient(workspace: str | None = None) -> Settings:
    overrides: dict[str, Any] = {"workspace": Path(workspace).resolve() if workspace else None}
    return Settings.load(overrides, require_api_key=False)


def run_mcp_subcommand(args: list[str]) -> int:
    try:
        from axon.mcp.commands import handle_mcp_command
    except (ImportError, AttributeError) as e:
        sys.stderr.write(f"axon: MCP support is unavailable: {e}\n")
        return 1
    try:
        settings = _load_settings_lenient()
        agent = build_agent(settings, connect_mcp=False)
    except ConfigError as e:
        sys.stderr.write(f"axon: {e}\n")
        return 2
    try:
        handle_mcp_command(agent, list(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        sys.stderr.write(f"axon mcp: {type(e).__name__}: {e}\n")
        return 1
    return 0


def run_purge_subcommand(args: list[str]) -> int:
    """`axon purge [project|caches|all] [--yes]` (implemented by commands.workspace_cmds)."""
    try:
        from axon.commands.workspace_cmds import purge_main
    except (ImportError, AttributeError) as e:
        sys.stderr.write(f"axon purge is unavailable: {e}\n")
        return 1
    return int(purge_main(args) or 0)


def run_doctor_subcommand(args: list[str]) -> int:
    """Environment diagnostics that work even without an API key: ONE report (the same one /doctor
    shows), printed as aligned plain rows so it is safe to pipe and on cp1252 consoles."""
    import axon
    print(f"\nAxon doctor (v{getattr(axon, '__version__', '?')})\n")
    try:
        settings = _load_settings_lenient()
    except ConfigError as e:
        print(f"  [!!] Configuration      {e}\n")
        return 1
    try:
        from axon.tools.doctor import WARN, run_diagnostics
        report = run_diagnostics(settings, settings.workspace)
    except Exception as e:
        print(f"  [!!] Diagnostics failed: {type(e).__name__}: {e}\n")
        return 1
    for line in report.splitlines():
        if line.startswith("==="):
            continue
        print(f"  {line}" if line else "")
    print()
    return 1 if any(WARN in ln for ln in report.splitlines() if not ln.startswith("Status")) else 0


# ─── entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and not argv[0].startswith("-") and argv[0] not in SUBCOMMANDS:
        try:
            from axon.features import cli_subcommands
            feature_subs = cli_subcommands()
        except Exception:
            feature_subs = {}
        if argv[0] in feature_subs:
            try:
                return int(feature_subs[argv[0]](argv[1:]) or 0)
            except KeyboardInterrupt:
                return 130
            except Exception as e:
                sys.stderr.write(f"axon {argv[0]}: {type(e).__name__}: {e}\n")
                return 1

    if argv and argv[0] in SUBCOMMANDS:
        try:
            if argv[0] == "mcp":
                return run_mcp_subcommand(argv[1:])
            if argv[0] == "purge":
                return run_purge_subcommand(argv[1:])
            return run_doctor_subcommand(argv[1:])
        except KeyboardInterrupt:
            return 130

    parser = build_parser()
    args = parser.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    stdin_content = read_piped_stdin()
    if stdin_content:
        prompt = f"{stdin_content}\n\n{prompt}".strip() if prompt else stdin_content

    worktree_note = ""
    if args.worktree is not None:
        from axon.agent.worktree import create_session_worktree
        base = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
        info = create_session_worktree(base, args.worktree or None)
        if info is None:
            sys.stderr.write("axon: --worktree needs a git repository with at least one commit\n")
            return 2
        args.workspace = str(info.worktree_path)
        try:
            os.chdir(info.worktree_path)
        except OSError:
            pass
        worktree_note = f"Worktree: {info.worktree_path} (branch {info.branch_name}{', new' if info.created else ''})"

    if args.agents_json:
        try:
            from axon.agent.subagent import parse_agents_json, register_session_agents
            register_session_agents(parse_agents_json(args.agents_json))
        except ValueError as e:
            sys.stderr.write(f"axon: --agents: {e}\n")
            return 2

    try:
        settings = Settings.load(_overrides_from_args(args))
        settings = _apply_prompt_flags(settings, args)
        agent_def = None
        if args.agent_name:
            settings, agent_def = _apply_agent_flag(settings, args.agent_name)
        mcp_specs = _mcp_specs_from_args(args.mcp_config, settings.workspace) if args.mcp_config else None
    except ConfigError as e:
        sys.stderr.write(f"axon: {e}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130

    try:
        from axon import storage
        storage.prune_on_startup(settings.cleanup_period_days)
    except Exception:
        pass

    allowed = _split_tool_list(args.allowed_tools)
    disallowed = _split_tool_list(args.disallowed_tools)
    settings = _apply_tool_rules(settings, allowed, [d for d in disallowed if "(" in d])

    headless = bool(args.print_mode or prompt)
    fmt = args.output_format
    renderer: Any = None
    on_event: Callable[[Any], None] | None
    if headless and fmt == "stream-json":
        on_event = _StreamJsonEmitter().on_event
    elif headless and fmt == "json":
        on_event = None
    else:
        from axon.ui.render import Renderer
        renderer = Renderer(show_thinking=settings.thinking)
        on_event = renderer.on_event

    interactive = sys.stdin.isatty() if sys.stdin else False
    on_approval = None
    if interactive:
        from axon.ui.approve import ask_approval
        on_approval = lambda tool, targs, dec: ask_approval(tool, targs, dec.reason)  # noqa: E731

    def announce(msg: str) -> None:
        if fmt == "text":
            print(f"  {msg}")

    try:
        agent = build_agent(
            settings,
            on_event=on_event,
            on_approval=on_approval,
            disallowed_tools=disallowed,
            announce=announce,
            tools_spec=args.tools,
            mcp_specs=mcp_specs,
            strict_mcp=bool(args.strict_mcp_config),
            agent_def=agent_def,
        )
    except AxonError as e:
        sys.stderr.write(f"axon: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"axon: failed to start: {e}\n")
        return 2
    agent.renderer = renderer
    if renderer is not None and hasattr(renderer, "attach"):
        try:
            renderer.attach(agent)
        except Exception:
            pass
    if worktree_note:
        announce(worktree_note)
    if settings.additional_dirs and fmt == "text" and settings.verbose:
        announce("Additional directories: " + ", ".join(str(d) for d in settings.additional_dirs))

    source = "startup"
    if args.continue_session:
        latest = agent.session.latest()
        if not latest:
            sys.stderr.write("No conversation found to continue\n")
            return 1
        if not _resume(agent, latest, announce, restore_mode=args.mode is None):
            return 1
        source = "resume"
    elif args.resume_id is not None:
        target: str | None = None
        if args.resume_id == "":
            if not interactive:
                sys.stderr.write("axon: --resume without an ID needs an interactive terminal\n")
                return 1
            from axon.ui.switcher import run_session_dashboard
            picked = run_session_dashboard(agent)
            target = picked if picked and not picked.startswith("__NEW_SESSION__") else None
        else:
            target, candidates = find_session(agent, args.resume_id)
            if target is None:
                if candidates:
                    sys.stderr.write(f"Ambiguous session name '{args.resume_id}': {', '.join(candidates[:8])}\n")
                else:
                    sys.stderr.write(f"No conversation found with session ID: {args.resume_id}\n")
                return 1
        if target:
            if not _resume(agent, target, announce, restore_mode=args.mode is None):
                return 1
            source = "resume"
    if args.fork_session and source == "resume":
        fork_session(agent, announce)
    if args.session_name:
        try:
            agent.session.rename_session(args.session_name)
            if hasattr(agent, "mark_titled"):
                agent.mark_titled(args.session_name)
            from axon.agent.loop import set_terminal_title
            set_terminal_title(args.session_name)
        except Exception:
            pass
    debug_path = setup_debug_logging(args.debug, args.debug_file, getattr(agent.session, "active_session_id", ""))
    if debug_path is not None and fmt == "text":
        announce(f"Debug log: {debug_path}")
    try:
        from axon.agent.trace import load_traces
        agent.traces = load_traces(agent.settings.workspace, agent.session.active_session_id,
                                   legacy_session_dir=agent.session.session_dir) if source == "resume" else []
    except Exception:
        pass

    agent.start_session(source)
    if settings.additional_dirs:
        for d in settings.additional_dirs:
            agent._hook("DirectoryAdded", {"directory": str(d), "source": "cli"})
    exit_reason = "exit"
    try:
        if headless:
            return run_print_mode(agent, prompt, fmt, renderer)
        return run_repl(agent, renderer)
    except KeyboardInterrupt:
        exit_reason = "interrupt"
        return 130
    finally:
        try:
            agent.end_session(exit_reason)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
