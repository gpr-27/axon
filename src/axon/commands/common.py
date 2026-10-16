"""
Shared helpers for slash commands: consistent output styling, token estimates,
config/.env persistence, subprocess helpers and editor launching.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from axon.storage import axon_home as _axon_home
from axon.ui.theme import BOLD, DARK_SLATE, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE, strip_ansi, term_width


# ─── Output ─────────────────────────────────────────────────────────────────

def width() -> int:
    return max(48, min(100, term_width() - 4))


def header(title: str, subtitle: str = "", color: str = GOLD) -> None:
    sub = f"  {SLATE}{subtitle}{RST}" if subtitle else ""
    print(f"\n  {color}{BOLD}{title}{RST}{sub}")
    print(f"  {DARK_SLATE}{'─' * min(width(), max(len(title) + len(subtitle) + 2, 40))}{RST}")


def section(title: str, color: str = TEAL) -> None:
    print(f"\n  {color}{BOLD}{title}{RST}")


def kv(label: str, value: Any, label_w: int = 18, color: str = WHITE) -> None:
    print(f"    {SLATE}{label:<{label_w}}{RST} {color}{value}{RST}")


def ok(msg: str) -> None:
    print(f"\n  {MINT}✓{RST} {msg}\n")


def info(msg: str) -> None:
    print(f"\n  {SLATE}{msg}{RST}\n")


def warn(msg: str) -> None:
    print(f"\n  {GOLD}!{RST} {msg}\n")


def err(msg: str) -> None:
    print(f"\n  {ROSE}✗{RST} {msg}\n")


def usage(text: str) -> None:
    # continuation lines of a multi-line usage are aligned under the first one (after "  Usage: ")
    body = text.replace("\n", "\n" + " " * 9)
    print(f"\n  {GOLD}Usage:{RST} {WHITE}{body}{RST}\n")


def hint(text: str) -> None:
    print(f"  {DIM}{text}{RST}")


def bar(fraction: float, cells: int = 24, color: str = TEAL) -> str:
    f = max(0.0, min(1.0, fraction))
    full = int(round(f * cells))
    return f"{color}{'█' * full}{DARK_SLATE}{'░' * (cells - full)}{RST}"


def table(rows: list[list[str]], headers: list[str] | None = None, indent: int = 4) -> None:
    """Render a plain aligned table (ANSI-aware widths)."""
    all_rows = ([headers] if headers else []) + rows
    if not all_rows:
        return
    ncol = max(len(r) for r in all_rows)
    widths = [0] * ncol
    for r in all_rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(strip_ansi(str(c))))
    pad = " " * indent
    def fmt(r: list[str]) -> str:
        cells = []
        for i in range(ncol):
            c = str(r[i]) if i < len(r) else ""
            cells.append(c + " " * (widths[i] - len(strip_ansi(c))))
        return pad + "  ".join(cells).rstrip()
    if headers:
        print(f"{SLATE}{fmt(headers)}{RST}")
        print(f"{pad}{DARK_SLATE}{'─' * (sum(widths) + 2 * (ncol - 1))}{RST}")
    for r in rows:
        print(fmt(r))


def fmt_tokens(n: int | float) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


def fmt_usd(v: Any, places: int = 4) -> str:
    try:
        return f"${float(v):.{places}f}"
    except (TypeError, ValueError):
        return "$0"


# ─── Tokens ─────────────────────────────────────────────────────────────────

def estimate_tokens(content: Any) -> int:
    """Single token estimator for commands (delegates to agent.state)."""
    try:
        from axon.agent.state import estimate_content_tokens
        return int(estimate_content_tokens(content))
    except Exception:
        import json
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        return max(0, len(text) // 4)


# ─── Agent helpers ──────────────────────────────────────────────────────────

def ws_of(agent: Any) -> Path:
    return Path(getattr(agent.settings, "workspace", Path.cwd()))


def update_settings(agent: Any, **changes: Any) -> None:
    """Replace the (frozen) settings object and propagate it to dependents. A mode change goes
    through ``agent.set_mode`` when available so it is recorded in the session."""
    set_mode = getattr(agent, "set_mode", None)
    if "mode" in changes and callable(set_mode):
        changes = dict(changes)
        set_mode(changes.pop("mode"))
    if changes:
        before = {k: getattr(agent.settings, k, None) for k in changes}
        agent.settings = agent.settings.model_copy(update=changes)
        _journal_settings(agent, before, changes)
    perms = getattr(agent, "permissions", None)
    if perms is not None and hasattr(perms, "settings"):
        perms.settings = agent.settings
    ctx = getattr(agent, "context", None)
    if ctx is not None and hasattr(ctx, "settings"):
        try:
            ctx.settings = agent.settings
        except Exception:
            pass
    if "thinking" in changes and getattr(agent, "renderer", None) is not None:
        try:
            agent.renderer.show_thinking = bool(changes["thinking"])
        except Exception:
            pass


def _journal_settings(agent: Any, before: dict[str, Any], changes: dict[str, Any]) -> None:
    """Record in-session settings changes (old -> new) in the activity journal; secrets are masked."""
    try:
        from axon.session import activity

        def plain(v: Any) -> Any:
            if hasattr(v, "get_secret_value"):
                return activity.REDACTED if v.get_secret_value() else ""
            return v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)

        diff = {k: {"from": plain(before.get(k)), "to": plain(v)} for k, v in changes.items()
                if plain(before.get(k)) != plain(v)}
        if diff:
            activity.record("settings", {"changes": diff}, agent=agent)
    except Exception:
        pass


def reprint_banner(agent: Any) -> None:
    try:
        import axon
        from axon.ui.render import Renderer
        r = getattr(agent, "renderer", None) or Renderer()
        r.print_banner(
            version=getattr(axon, "__version__", ""),
            model=agent.settings.model,
            effort=agent.settings.effort,
            workspace=str(agent.settings.workspace),
            mode=agent.settings.mode,
            base_url=agent.settings.base_url,
        )
    except Exception:
        pass


def is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def confirm(question: str, default: bool = False) -> bool:
    """Ask y/n on a TTY. Non-interactive sessions get ``default``."""
    if not is_tty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {GOLD}?{RST} {question} {SLATE}{suffix}{RST} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def ask_text(prompt: str) -> str:
    if not is_tty():
        return ""
    try:
        return input(f"  {GOLD}?{RST} {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ─── Config persistence ─────────────────────────────────────────────────────

def _toml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as f:
        return tomllib.load(f)


def update_global_config(updates: dict[str, Any] | None = None, add_custom_model: dict[str, str] | None = None) -> bool:
    """Load-modify-dump ~/.axon/config.toml atomically. Never wipes unrelated keys."""
    cfg_file = _axon_home() / "config.toml"
    try:
        data = _toml_load(cfg_file)
    except Exception:
        return False  # malformed: refuse to overwrite the user's file
    for k, v in (updates or {}).items():
        data[k] = v
    if add_custom_model:
        models = [m for m in data.get("custom_models", []) or [] if isinstance(m, dict)]
        if not any(m.get("model") == add_custom_model.get("model") and m.get("provider") == add_custom_model.get("provider") for m in models):
            models.append(dict(add_custom_model))
        data["custom_models"] = models
    try:
        import tomli_w
        from axon.session.store import atomic_write_text
        atomic_write_text(cfg_file, tomli_w.dumps(data))
        return True
    except Exception:
        return False


def settings_file(scope: str = "user", workspace: Path | None = None) -> Path:
    """settings.json for a scope: user (~/.axon), project (<ws>/.axon) or local (<ws>/.axon/settings.local.json)."""
    if scope == "user" or workspace is None:
        return _axon_home() / "settings.json"
    name = "settings.local.json" if scope == "local" else "settings.json"
    return Path(workspace) / ".axon" / name


def read_settings_file(path: Path) -> dict[str, Any] | None:
    """Parsed JSON object, {} when missing, None when malformed (callers must not overwrite it)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_settings_file(updates: dict[str, Any], scope: str = "user", workspace: Path | None = None) -> Path | None:
    """Load-modify-dump a settings.json. A value of None deletes the key; dotted keys
    ("permissions.additionalDirectories") address nested objects. Returns the path, or None on failure."""
    path = settings_file(scope, workspace)
    data = read_settings_file(path)
    if data is None:
        return None
    for key, value in updates.items():
        parts = key.split(".")
        node = data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    try:
        from axon.session.store import atomic_write_text
        atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        return None
    try:
        from axon.session import activity
        activity.record("settings_file", {"path": str(path), "scope": scope, "updates": updates},
                        workspace=workspace)
    except Exception:
        pass
    return path


def read_env_file() -> dict[str, str]:
    env_f = _axon_home() / ".env"
    out: dict[str, str] = {}
    if not env_f.exists():
        return out
    try:
        for line in env_f.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def lookup_key(env_var: str) -> str:
    """API key from the process env or ~/.axon/.env (exact variable-name match)."""
    val = os.environ.get(env_var, "").strip()
    if val and not val.startswith("/"):
        return val
    val = read_env_file().get(env_var, "")
    return val if val and not val.startswith("/") else ""


def is_cancel_word(s: str) -> bool:
    return not s or s.startswith("/") or s.lower() in ("cancel", "exit", "skip", "q", "none", "null")


def save_env_key(env_var: str, key_val: str) -> bool:
    """Write/replace ``env_var`` in ~/.axon/.env (exact name match) and os.environ."""
    if not env_var or not key_val:
        return False
    clean = key_val.strip().strip('"').strip("'")
    if is_cancel_word(clean):
        return False
    try:
        from axon.providers.catalog import save_api_key
        save_api_key(env_var, clean)
    except (ImportError, AttributeError):
        from axon.session.store import atomic_write_text
        env_f = _axon_home() / ".env"
        lines = [ln for ln in (env_f.read_text(encoding="utf-8").splitlines() if env_f.exists() else [])
                 if ln.split("=", 1)[0].strip().removeprefix("export ").strip() != env_var]
        lines.append(f'{env_var}="{clean}"')
        atomic_write_text(env_f, "\n".join(lines).strip() + "\n")
    except ValueError:
        return False
    os.environ[env_var] = clean
    return True


# ─── Processes ──────────────────────────────────────────────────────────────

def run_cmd(argv: list[str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run an argv (resolved through PATH, so npm.cmd etc. work on Windows)."""
    exe = shutil.which(argv[0]) or argv[0]
    return subprocess.run(
        [exe] + argv[1:], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
    )


def git(cwd: Path, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    if not shutil.which("git"):
        return 127, "git is not installed"
    try:
        p = run_cmd(["git", *args], cwd, timeout)
        return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout))
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    except OSError as e:
        return 127, str(e)


def editor_argv() -> list[str]:
    """The user's editor ($VISUAL/$EDITOR, else a platform default) - one implementation in axon.platform."""
    try:
        from axon.platform import editor_command
        argv = list(editor_command())
        if argv:
            return argv
    except Exception:
        pass
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if ed:
        try:
            return shlex.split(ed, posix=(os.name != "nt"))
        except ValueError:
            return [ed]
    return ["notepad"] if os.name == "nt" else ["vi"]


def open_in_editor(path: Path) -> bool:
    argv = editor_argv()
    try:
        subprocess.call(argv + [str(path)])
        return True
    except (OSError, subprocess.SubprocessError) as e:
        err(f"Could not start editor {' '.join(argv)}: {e}. Set $EDITOR.")
        return False


def clip(text: str, limit: int) -> str:
    """Single-line text shortened to ``limit`` chars with an ellipsis."""
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[: max(1, limit - 1)].rstrip() + "…"


def first_line(text: str, limit: int = 80) -> str:
    for line in str(text).splitlines():
        if line.strip():
            s = line.strip()
            return s if len(s) <= limit else s[: limit - 1] + "…"
    return ""


def join_nonempty(parts: Iterable[str], sep: str = " · ") -> str:
    return sep.join(p for p in parts if p)


def record_usage(ledger: Any, model: str, usage: Any, source: str) -> None:
    """Record an auxiliary LLM call (side question, compaction, ...) in the ledger."""
    if ledger is None or usage is None:
        return
    try:
        from axon.agent.state import record_usage as _rec
        _rec(ledger, model, usage, source=source)
        return
    except Exception:
        pass
    try:
        ledger.record(model, usage, source=source)
    except TypeError:
        ledger.record(model, usage, tag=source)


def one_shot(agent: Any, system: str, prompt: str, *, max_tokens: int = 800, source: str = "aux") -> str:
    """Run a single tool-less LLM call outside the conversation; usage goes to the ledger."""
    model = agent.settings.model
    provider = agent.provider
    if provider is None:
        raise RuntimeError("no provider configured (use /provider or /model)")
    stream = provider.stream(
        model=model, system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": prompt}], tools=[], max_tokens=max_tokens,
        effort="low", thinking=False,
    )
    for _ in stream:
        pass
    turn = provider.finalize()
    record_usage(getattr(agent, "ledger", None), model, getattr(turn, "usage", None), source)
    return (getattr(turn, "text", "") or "").strip()
