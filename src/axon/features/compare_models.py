"""Compare models: one prompt, two to four models at once, read-only.

Each column is its own short-lived agent (own provider, own conversation, own ledger, own throwaway transcript), built
the way a sub-agent is, but with only the read-only tools: it can look at files and search, and it cannot change a file,
run a command or reach the network. Events of one column only ever touch that column.

Every model call is written to the usage log with source ``compare``, so the cost shows up in Usage and in the budgets.

The core (``Compare``, ``start_compare``, ``parse_command``) has no web or terminal imports; ``cmd_compare`` at the end is
the thin terminal front, ``dash_js_compare`` the web one.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from axon.features import FeatureCommand

MIN_COLUMNS, MAX_COLUMNS = 2, 4
MAX_STEPS = 6
MAX_TEXT = 200_000
READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")
NOTE = ("Read-only: each model can read files and search this project, but cannot change files, run commands or use the "
        "network. Nothing in your project changes.")
CONTEXT_NOTE = "Each model also gets a plain-text summary of this chat so far, so they answer in the same situation."
ROLE = ("You are one of several models answering the same question so that a person can compare the answers. Answer directly "
        "and concisely. You have read-only tools (read files, search) and nothing you do can change the project. Do not offer "
        "to edit files or run commands.")
_KEEP = 12


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Column:
    """One model's run. Written by its own thread only; read under the compare's lock."""

    def __init__(self, idx: int, model: str) -> None:
        self.idx = idx
        self.model = model
        self.status = "queued"          # queued | running | done | error | stopped
        self.text = ""
        self.tools: list[dict[str, str]] = []
        self.error = ""
        self.started = 0.0
        self.first_token: float | None = None
        self.ended = 0.0
        self.input = self.output = self.cache_read = 0
        self.cost = 0.0
        self.priced = True
        self.calls = 0
        self.stop_requested = False
        self.agent: Any = None
        self.thread: threading.Thread | None = None

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return max(0.0, (self.ended or time.time()) - self.started)

    def snapshot(self, offset: int = 0) -> dict[str, Any]:
        off = max(0, min(int(offset or 0), len(self.text)))
        return {"idx": self.idx, "model": self.model, "status": self.status, "delta": self.text[off:], "length": len(self.text),
                "tools": list(self.tools), "error": self.error, "seconds": round(self.elapsed(), 2),
                "first_token_s": None if self.first_token is None else round(self.first_token - self.started, 2),
                "input": self.input, "output": self.output, "cache_read": self.cache_read, "calls": self.calls,
                "cost": self.cost if self.priced else None, "priced": self.priced, "running": self.status in ("queued", "running")}


class Compare:
    def __init__(self, parent: Any, prompt: str, models: list[str], with_context: bool, session: str = "") -> None:
        self.id = uuid.uuid4().hex[:12]
        self.parent = parent
        self.prompt = prompt
        self.session = session
        self.with_context = with_context
        self.columns = [Column(i, m) for i, m in enumerate(models)]
        self.lock = threading.RLock()
        self.created = time.time()
        self.base = list(getattr(getattr(parent, "conversation", None), "messages", None) or [])      # the chat as it was when this started
        self.workdir = Path(tempfile.mkdtemp(prefix="axon-compare-"))
        self._sid = ""
        try:
            self._sid = str(parent.session.active_session_id).rsplit("_sub_", 1)[0]
        except Exception:
            pass

    # ── state ──
    @property
    def done(self) -> bool:
        with self.lock:
            return all(c.status in ("done", "error", "stopped") for c in self.columns)

    def snapshot(self, offsets: list[int] | None = None) -> dict[str, Any]:
        with self.lock:
            offs = list(offsets or [])
            cols = [c.snapshot(offs[i] if i < len(offs) else 0) for i, c in enumerate(self.columns)]
            priced = [c["cost"] for c in cols if c["cost"] is not None]
            return {"id": self.id, "prompt": self.prompt, "columns": cols, "done": self.done, "read_only": True, "note": NOTE,
                    "with_context": self.with_context, "total_cost": sum(priced), "unpriced": sum(1 for c in cols if not c["priced"])}

    # ── control ──
    def stop(self, idx: int) -> bool:
        with self.lock:
            if not 0 <= idx < len(self.columns):
                return False
            col = self.columns[idx]
            if col.status not in ("queued", "running"):
                return False
            col.stop_requested = True
            if col.status == "queued":
                col.status, col.ended = "stopped", time.time()
            agent = col.agent
        if agent is not None and not self._cancel(agent):
            # The agent exists but its turn thread has not started, so there was nothing to interrupt yet.
            threading.Thread(target=self._cancel_once_started, args=(col, agent), name=f"axon-compare-stop-{idx}", daemon=True).start()
        return True

    @staticmethod
    def _cancel(agent: Any) -> bool:
        try:
            return bool(agent.cancel_current_turn())
        except Exception:
            return True                  # retrying a cancel that raises would only raise again

    def _cancel_once_started(self, col: Column, agent: Any) -> None:
        while col.status == "running":
            if self._cancel(agent):
                return
            time.sleep(0.05)

    def stop_all(self) -> None:
        for i in range(len(self.columns)):
            self.stop(i)

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.time() + timeout
        for c in self.columns:
            t = c.thread
            if t is not None:
                t.join(None if end is None else max(0.0, end - time.time()))
        return self.done

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ── one column ──
    def _prompt_for_model(self) -> str:
        if not self.with_context:
            return self.prompt
        try:
            from axon.commands.code_cmds import session_context
            text, n = session_context(self.parent)
        except Exception:
            text, n = "", 0
        if not n:
            return self.prompt
        return (f"<conversation_so_far>\n{text}\n</conversation_so_far>\n\nThis is the conversation so far. "
                f"Answer the next message from the person, as the assistant in it:\n{self.prompt}")

    def _build_agent(self, col: Column) -> Any:
        from axon.agent.loop import Agent
        from axon.agent.subagent import _make_provider, _shared_checkpoints, _short_args  # noqa: F401  (the sub-agent code is reused)
        from axon.providers.registry import settings_for_model
        from axon.session.ledger import Ledger
        from axon.session.store import SessionStore
        parent = self.parent
        ws = Path(parent.settings.workspace)
        settings = settings_for_model(col.model, parent.settings).model_copy(update={
            "model": col.model, "max_iterations": MAX_STEPS, "workspace": ws, "mode": "default"})
        reg = parent.registry
        names = [t.name for t in reg.all_tools() if t.name in READ_ONLY_TOOLS]
        registry = reg.subset(names=names, readonly_only=True)        # the allow-list and the tools' own read-only flag
        store = SessionStore(ws, session_dir=self.workdir / f"c{col.idx}")
        store.open(f"{self._sid or 'compare'}_sub_cmp{col.idx + 1}_{self.id[:6]}")

        def on_event(ev: Any) -> None:
            self._on_event(col, ev)
        agent = Agent(provider=_make_provider(parent, col.model, settings), tools=registry, permissions=parent.permissions,
                      context=parent.context, checkpoints=_shared_checkpoints(parent, ws), session=store, ledger=Ledger(),
                      settings=settings, skills=getattr(parent, "skills", None), on_event=on_event, on_approval=None,
                      hooks=None, agent_name="compare", extra_system=ROLE, git_status=getattr(parent, "_git_snapshot", None))
        return agent

    def _on_event(self, col: Column, ev: Any) -> None:
        from axon.agent.subagent import _short_args
        from axon.providers.base import LLMCallStart, TextDelta, ToolExecutionStart
        with self.lock:
            if isinstance(ev, TextDelta):
                if col.first_token is None and ev.text:
                    col.first_token = time.time()
                if len(col.text) < MAX_TEXT:
                    col.text += ev.text
            elif isinstance(ev, LLMCallStart):
                col.calls += 1
                if col.text and not col.text.endswith("\n\n"):
                    col.text += "\n\n"
            elif isinstance(ev, ToolExecutionStart):
                col.tools.append({"name": str(ev.name), "summary": _short_args(ev.input, 100)})
                del col.tools[:-_KEEP]

    def _run(self, col: Column, prompt: str) -> None:
        with self.lock:
            if col.stop_requested:
                return
            col.status, col.started = "running", time.time()
        result = None
        try:
            agent = self._build_agent(col)
            with self.lock:
                col.agent = agent
                if col.stop_requested:
                    raise KeyboardInterrupt()
            result = agent.run_turn_threaded(prompt)
        except KeyboardInterrupt:
            pass
        except Exception as e:                               # a bad model name, a missing key, a network error
            with self.lock:
                col.error = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            self._finish(col, result)

    def _finish(self, col: Column, result: Any) -> None:
        ledger = getattr(col.agent, "ledger", None)
        with self.lock:
            col.ended = time.time()
            if ledger is not None:
                col.input = int(getattr(ledger, "total_input_tokens", 0) or 0)
                col.output = int(getattr(ledger, "total_output_tokens", 0) or 0)
                col.cache_read = int(getattr(ledger, "total_cache_read_tokens", 0) or 0)
                try:
                    col.cost = float(ledger.total())
                    from axon.providers.pricing import is_priced
                    col.priced = bool(is_priced(col.model, getattr(getattr(col.agent, "settings", None), "provider", None)))
                except Exception:
                    col.cost = 0.0
            if result is not None:
                if getattr(result, "stop_reason", "") == "error":
                    if not col.error:
                        col.error = str(getattr(result, "error", "") or getattr(result, "final_text", "") or "The model call failed.")[:400]
                elif not col.text.strip():
                    col.text = str(getattr(result, "final_text", "") or "")[:MAX_TEXT]
            if col.stop_requested:
                col.status = "stopped"
            elif col.error and not col.text.strip():
                col.status = "error"
            else:
                col.status = "done"
            if col.status == "error" and col.input == 0 and col.output == 0:
                col.priced = True       # nothing was sent, so there is nothing unpriced

    def start(self) -> "Compare":
        prompt = self._prompt_for_model()
        for col in self.columns:
            col.thread = threading.Thread(target=self._run, args=(col, prompt), name=f"axon-compare-{col.idx}", daemon=True)
        for col in self.columns:
            col.thread.start()      # type: ignore[union-attr]
        return self


_LOCK = threading.Lock()
_RUNS: dict[str, Compare] = {}
_KEEP_RUNS = 8


def validate_models(models: Any) -> list[str]:
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ValueError("models must be a list of model names")
    out: list[str] = []
    for m in models:
        m = m.strip()
        if not m or len(m) > 200 or any(c.isspace() or ord(c) < 32 for c in m):
            raise ValueError(f"'{m[:40]}' is not a model name")
        if m not in out:
            out.append(m)
    if len(out) < MIN_COLUMNS:
        raise ValueError(f"Pick at least {MIN_COLUMNS} different models.")
    if len(out) > MAX_COLUMNS:
        raise ValueError(f"Compare up to {MAX_COLUMNS} models at a time.")
    return out


def start_compare(parent: Any, prompt: str, models: Any, with_context: bool = True, session: str = "") -> Compare:
    """Start the columns and return at once; poll ``Compare.snapshot`` or call ``wait``."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Type the prompt to send to every model.")
    if len(prompt) > 50_000:
        raise ValueError("That prompt is too long to send to several models (most is 50,000 characters).")
    names = validate_models(models)
    has_chat = bool(getattr(getattr(parent, "conversation", None), "messages", None))
    cmp = Compare(parent, prompt, names, with_context and has_chat, session)
    with _LOCK:
        while len(_RUNS) >= _KEEP_RUNS:
            old = _RUNS.pop(next(iter(_RUNS)))
            old.stop_all()
            old.cleanup()
        _RUNS[cmp.id] = cmp
    return cmp.start()


def get_run(run_id: str) -> Compare | None:
    with _LOCK:
        return _RUNS.get(run_id)


def drop_all() -> None:
    with _LOCK:
        runs = list(_RUNS.values())
        _RUNS.clear()
    for r in runs:
        r.stop_all()
        r.cleanup()


def continuation(cmp: Compare, idx: int) -> tuple[str, str, str]:
    """(model, prompt, reply) of one finished column, for "Continue with this one"."""
    with cmp.lock:
        if not 0 <= idx < len(cmp.columns):
            raise ValueError("No such column.")
        col = cmp.columns[idx]
        if col.status not in ("done", "stopped") or not col.text.strip():
            raise ValueError("That model has no answer to continue from yet.")
        return col.model, cmp.prompt, col.text


# ─── the terminal ────────────────────────────────────────────────────────────

def looks_like_model(token: str, known: set[str]) -> bool:
    return token in known or any(c in token for c in "-/:.") or any(c.isdigit() for c in token)


def parse_command(arg: str, known: set[str] | None = None) -> tuple[list[str], str]:
    """``<model> <model> [<model>...] <prompt>`` or ``<model> <model> -- <prompt>`` into (models, prompt)."""
    words = (arg or "").split()
    known = known or set()
    if "--" in words:
        i = words.index("--")
        models, rest = words[:i], " ".join(words[i + 1:])
    else:
        i = 0
        while i < len(words) and i < MAX_COLUMNS and looks_like_model(words[i], known):
            i += 1
        models, rest = words[:i], " ".join(words[i:])
    return models, rest.strip()


def _known_models(agent: Any) -> set[str]:
    out: set[str] = {str(getattr(agent.settings, "model", ""))}
    try:
        from axon.providers.registry import known_models
        out |= set(known_models())
    except Exception:
        pass
    return out


def render_column(col: dict[str, Any], width: int = 100) -> list[str]:
    cost = f"${col['cost']:.4f}" if col["cost"] is not None else "no price known"
    head = (f"{col['model']}  ·  {col['seconds']:.1f}s  ·  {col['input']:,} in / {col['output']:,} out tokens  ·  {cost}"
            f"  ·  {col['status']}")
    lines = [head, "-" * min(width, max(20, len(head)))]
    if col["tools"]:
        lines.append("looked at: " + ", ".join(f"{t['name']} {t['summary']}".strip() for t in col["tools"][-6:]))
    if col["error"]:
        lines.append("error: " + col["error"])
    body = col.get("delta") or ""
    lines += (body.rstrip().splitlines() or ["(no answer)"])
    return lines


def cmd_compare(agent: Any, arg: str) -> None:
    from axon.features import analytics
    known = _known_models(agent)
    models, prompt = parse_command(arg, known)
    usage = analytics.typed_only("Usage: /compare <model> <model> [<model> ...] <prompt>   (up to 4 models; use -- before the prompt if a word looks like a model)")
    if not models or not prompt:
        analytics._emit(["  Name two to four models, then the prompt to send them." + (" " + usage if usage else ""), "  " + NOTE])
        return
    try:
        cmp = start_compare(agent, prompt, models, with_context=True)
    except ValueError as e:
        analytics._emit([f"  {e}" + (" " + usage if usage else "")])
        return
    analytics._emit([f"  Asking {', '.join(models)} at once. {NOTE}", "  Ctrl+C stops them; answers print as each finishes."])
    printed: set[int] = set()
    try:
        while True:
            for col in cmp.columns:
                with cmp.lock:
                    fin = col.status in ("done", "error", "stopped")
                if fin and col.idx not in printed:
                    printed.add(col.idx)
                    with cmp.lock:
                        snap = col.snapshot(0)
                    analytics._emit([""] + ["  " + ln for ln in render_column(snap)])
            if len(printed) == len(cmp.columns):
                break
            time.sleep(0.15)
    except KeyboardInterrupt:
        cmp.stop_all()
        cmp.wait(5)
        analytics._emit(["  Stopped."])
    snap = cmp.snapshot()
    tail = f"  Cost of this comparison: ${snap['total_cost']:.4f}, counted in Usage as compare."
    if snap["unpriced"]:
        tail += f" {snap['unpriced']} model(s) have no known price and are not counted."
    analytics._emit(["", tail, ""])
    cmp.cleanup()


COMMANDS = [
    FeatureCommand(name="/compare", handler=cmd_compare, category="Insight",
                   description="Ask 2 to 4 models the same question at once, read-only, with time, tokens and cost for each",
                   usage="/compare <model> <model> [<model> ...] <prompt>"),
]
