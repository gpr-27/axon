"""
Prompt status line: a user command (``settings.status_line``) or the built-in summary.

Custom status line (Claude-Code compatible)::

    "status_line": {"type": "command", "command": "~/.axon/statusline.sh", "padding": 0}

The command runs through the platform shell (bash/sh, PowerShell or cmd) with a JSON description
of the session on stdin (``model``, ``workspace``, ``cost``, ``context_window``, ``session_id``,
``transcript_path``, ``vim``, ...). Its stdout (multi-line, ANSI colours and OSC 8 links kept) is
shown under the prompt. Results are cached for 300 ms; a command that fails or exceeds the
timeout keeps the last good output (or falls back to the built-in line).

Built-in line (no command configured): ``model · ctx 42% · $0.1234 · trace normal · ⎇ main``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from axon import platform as plat

CACHE_TTL_S = 0.3
TIMEOUT_S = 1.0
MAX_LINES = 4

_lock = threading.Lock()
_cache: dict[str, tuple[float, str | None]] = {}
_last_good: dict[str, str] = {}
_git_cache: dict[str, tuple[float, str]] = {}


# ─── settings access ───────────────────────────────────────────────────────

def status_line_config(settings: Any) -> dict[str, Any] | None:
    cfg = getattr(settings, "status_line", None)
    if cfg is None:
        cfg = getattr(settings, "statusLine", None)
    if hasattr(cfg, "model_dump"):
        cfg = cfg.model_dump()
    if isinstance(cfg, str) and cfg.strip():
        cfg = {"type": "command", "command": cfg}
    if not isinstance(cfg, dict) or not str(cfg.get("command") or "").strip():
        return None
    if str(cfg.get("type") or "command") != "command":
        return None
    return cfg


# ─── git branch (no subprocess: read .git/HEAD) ───────────────────────────

def git_branch(workspace: Path | str | None) -> str:
    """Current branch (or short detached SHA) of the repo containing *workspace*, '' if none."""
    if not workspace:
        return ""
    try:
        here = Path(workspace).resolve()
    except OSError:
        return ""
    for d in (here, *here.parents):
        dot_git = d / ".git"
        try:
            if dot_git.is_dir():
                head = dot_git / "HEAD"
            elif dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return ""
                gd = Path(text[7:].strip())
                head = (gd if gd.is_absolute() else d / gd) / "HEAD"
            else:
                continue
            key = str(head)
            mtime = head.stat().st_mtime
            hit = _git_cache.get(key)
            if hit and hit[0] == mtime:
                return hit[1]
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            branch = ref[16:] if ref.startswith("ref: refs/heads/") else (ref.split("/")[-1] if ref.startswith("ref:") else ref[:7])
            _git_cache[key] = (mtime, branch)
            return branch
        except OSError:
            return ""
    return ""


# ─── session facts (duck-typed agent) ──────────────────────────────────────

def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def session_cost(agent: Any) -> float:
    for getter in ("get_combined_cost",):
        fn = getattr(agent, getter, None)
        if callable(fn):
            try:
                return _num(fn())
            except Exception:
                pass
    ledger = getattr(agent, "ledger", None)
    try:
        return _num(ledger.total()) if ledger is not None else 0.0
    except Exception:
        return 0.0


def context_usage(agent: Any) -> tuple[int, int]:
    """(tokens in context, context window) - last LLM call when known, else an estimate."""
    renderer = getattr(agent, "renderer", None)
    used, window = getattr(renderer, "last_context", (0, 0)) or (0, 0)
    settings = getattr(agent, "settings", None)
    if not window:
        try:
            from axon.providers.registry import get_context_window
            window = int(get_context_window(str(getattr(settings, "model", "") or "")) or 0)
        except Exception:
            window = 0
    if not used:
        conv = getattr(agent, "conversation", None)
        try:
            used = int(conv.token_estimate()) if conv is not None else 0
        except Exception:
            used = 0
    return int(used or 0), int(window or 200_000)


def _transcript_path(agent: Any) -> str:
    session = getattr(agent, "session", None)
    p = getattr(session, "active_file", None)
    return str(p) if p else ""


def build_payload(settings: Any, agent: Any = None) -> dict[str, Any]:
    """JSON document piped to the status-line command (Claude-Code schema subset + Axon extras)."""
    settings = settings if settings is not None else getattr(agent, "settings", None)
    model = str(getattr(settings, "model", "") or "")
    ws = getattr(settings, "workspace", None) or os.getcwd()
    used, window = context_usage(agent) if agent is not None else (0, 0)
    pct = round(100.0 * used / window, 1) if window else 0.0
    ledger = getattr(agent, "ledger", None)
    totals: dict[str, Any] = {}
    try:
        totals = dict(ledger.totals()) if ledger is not None else {}
    except Exception:
        totals = {}
    session = getattr(agent, "session", None)
    try:
        from axon.ui.input import vim_mode_label
        vim_mode = vim_mode_label()
    except Exception:
        vim_mode = ""
    try:
        import axon
        version = getattr(axon, "__version__", "")
    except Exception:
        version = ""
    renderer = getattr(agent, "renderer", None)
    started = _num(getattr(agent, "started_at", 0) or 0)
    payload: dict[str, Any] = {
        "hook_event_name": "Status",
        "session_id": str(getattr(session, "active_session_id", "") or ""),
        "session_name": str(getattr(session, "active_name", "") or getattr(session, "name", "") or ""),
        "transcript_path": _transcript_path(agent),
        "cwd": os.getcwd(),
        "version": version,
        "model": {"id": model, "display_name": model.split("/")[-1]},
        "workspace": {"current_dir": str(ws), "project_dir": str(ws),
                      "added_dirs": [str(d) for d in (getattr(settings, "additional_dirs", None) or [])]},
        "output_style": {"name": str(getattr(settings, "output_style", "default") or "default")},
        "cost": {
            "total_cost_usd": round(session_cost(agent), 6) if agent is not None else 0.0,
            "total_duration_ms": int((time.time() - started) * 1000) if started else 0,
            "total_input_tokens": int(totals.get("input", 0) or 0),
            "total_output_tokens": int(totals.get("output", 0) or 0),
        },
        "context_window": {
            "context_window_size": window,
            "total_input_tokens": int(totals.get("input", 0) or 0),
            "total_output_tokens": int(totals.get("output", 0) or 0),
            "current_usage": used,
            "used_percentage": pct,
            "remaining_percentage": round(max(0.0, 100.0 - pct), 1),
        },
        "effort": {"level": str(getattr(settings, "effort", "") or "")},
        "thinking": {"enabled": bool(getattr(settings, "thinking", False))},
        "permission_mode": str(getattr(settings, "mode", "") or ""),
        "trace_level": str(getattr(renderer, "trace_level", "") or ""),
        "vim": {"mode": vim_mode} if vim_mode else None,
        "git_branch": git_branch(ws),
    }
    return {k: v for k, v in payload.items() if v is not None}


# ─── running the command ───────────────────────────────────────────────────

def run_status_command(command: str, payload: dict[str, Any], timeout: float = TIMEOUT_S,
                       cwd: str | None = None) -> str | None:
    """Run *command* with *payload* as JSON on stdin; stdout (rstripped) or None on failure."""
    argv = plat.shell_command_argv(os.path.expanduser(command))
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=cwd if cwd and os.path.isdir(cwd) else None,
                                **plat.new_process_group_kwargs())
    except (OSError, ValueError):
        return None
    try:
        out, _ = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            plat.kill_process_tree(proc, grace=0.2)
        except Exception:
            proc.kill()
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    text = plat.decode_output(out).replace("\r\n", "\n").replace("\r", "").rstrip()
    return text


def render_custom_status(settings: Any, agent: Any = None) -> str | None:
    """Output of the configured status-line command (None when none is set or it failed)."""
    cfg = status_line_config(settings if settings is not None else getattr(agent, "settings", None))
    if cfg is None:
        return None
    command = str(cfg["command"])
    payload = build_payload(settings, agent)
    stable = {k: v for k, v in payload.items() if k != "cost"}
    key = hashlib.sha1((command + json.dumps(stable, sort_keys=True, default=str)).encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    ws = str(getattr(settings, "workspace", "") or "") or None
    text = run_status_command(command, payload, timeout=_num(cfg.get("timeout"), TIMEOUT_S) or TIMEOUT_S, cwd=ws)
    with _lock:
        if text is None:
            text = _last_good.get(command)
        elif text:
            _last_good[command] = text
        pad = int(_num(cfg.get("padding"), 0))
        if text:
            rows = [r for r in text.split("\n")][:MAX_LINES]
            text = "\n".join((" " * max(0, pad)) + r for r in rows)
        _cache[key] = (now, text or None)
    return text or None


def default_status_line(agent: Any, width: int | None = None, include_mode: bool = False) -> str:
    """Built-in line: model · [mode ·] ctx% · $session · trace level · git branch."""
    from axon.ui.theme import MUTED, RST, glyph, term_width, truncate
    settings = getattr(agent, "settings", None)
    used, window = context_usage(agent)
    pct = 100.0 * used / window if window else None
    try:
        from axon.ui.render import render_status_line
        line = render_status_line(str(getattr(settings, "model", "") or "(no model)"),
                                  mode=str(getattr(settings, "mode", "") or "") if include_mode else "",
                                  context_pct=pct, session_cost=session_cost(agent),
                                  trace_level=str(getattr(getattr(agent, "renderer", None), "trace_level", "") or ""),
                                  width=10_000)
    except Exception:
        line = f"  {getattr(settings, 'model', '')}"
    branch = git_branch(getattr(settings, "workspace", None) or os.getcwd())
    if branch:
        line += f" {MUTED}{glyph('dot')}{RST} {MUTED}{glyph('git')} {branch}{RST}"
    return truncate(line, (width or term_width()) - 1)


def prompt_status_rows(agent: Any, width: int | None = None) -> list[str]:
    """Rows shown under the prompt: the custom command output, else the built-in line."""
    if agent is None:
        return []
    settings = getattr(agent, "settings", None)
    try:
        custom = render_custom_status(settings, agent)
    except Exception:
        custom = None
    if custom:
        from axon.ui.theme import truncate, term_width
        w = (width or term_width()) - 1
        return [truncate("  " + r, w) for r in custom.split("\n")]
    try:
        return [default_status_line(agent, width)]
    except Exception:
        return []
