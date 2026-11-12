"""OpenTelemetry trace export (off by default), provider-agnostic.

It listens to the events the agent loop already emits (``Agent._deliver``) and wraps the turn entry point, so the
loop itself is untouched. One root span per user turn (``axon.turn``) has children for every model request
(``chat <model>``, CLIENT span), tool call (``execute_tool <name>``), subagent run (``invoke_agent <type>``, with its
own llm/tool steps below it), compaction, goal check, advisor consult and permission check. Attribute names follow the
OpenTelemetry generative-AI conventions (``gen_ai.*``); Axon's own are ``axon.*``. ``gen_ai.conversation.id`` is the
session id.

Export is OTLP/HTTP JSON written with httpx (no OpenTelemetry package needed): spans go on a bounded queue, a
background thread posts batches with short timeouts and backoff, overflow is dropped, nothing here ever raises into or
waits on a turn, and the queue is flushed at exit.

Privacy: metadata only by default (model, provider, token counts, cost, durations, tool NAMES, statuses). Prompt,
response and tool input/output text is sent only with ``captureContent`` on, redacted with the journal's secret
scrubber and capped per span.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import random
import threading
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

SERVICE_DEFAULT = "axon"
MAX_QUEUE = 2048
BATCH = 64
FLUSH_S = 2.0
HTTP_TIMEOUT_S = 3.0
RETRIES = 3
MAX_CONTENT_BYTES = 786_432          # 0.75 MB of captured text per span, as the Claude docs suggest
_KIND = {"internal": 1, "server": 2, "client": 3}
_VENDOR_HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com", "api.deepseek.com",
                 "openrouter.ai", "api.groq.com", "api.mistral.ai", "api.together.xyz", "api.fireworks.ai",
                 "api.cerebras.ai", "api.x.ai", "api.moonshot.ai", "api.cohere.com", "api.perplexity.ai")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ─── configuration ──────────────────────────────────────────────────────────

def _env_headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[unquote(k.strip())] = unquote(v.strip())
    return out


def resolve_config(settings: Any) -> dict[str, Any]:
    """Effective telemetry config: settings.json ``telemetry`` block (user level only: project files cannot set it),
    then the standard OTEL_EXPORTER_OTLP_* variables for anything it leaves out."""
    block = dict(getattr(settings, "telemetry", None) or {})
    env = os.environ
    endpoint = str(block.get("endpoint") or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    if not endpoint:
        base = str(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip().rstrip("/")
        endpoint = base + "/v1/traces" if base else ""
    elif block.get("endpoint") and not endpoint.rstrip("/").endswith("/v1/traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"
    headers = {**_env_headers(env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or env.get("OTEL_EXPORTER_OTLP_HEADERS") or ""),
               **{str(k): str(v) for k, v in (block.get("headers") or {}).items()}}
    res = _env_headers(env.get("OTEL_RESOURCE_ATTRIBUTES") or "")
    res.update({str(k): str(v) for k, v in (block.get("resourceAttributes") or {}).items()})
    enabled = block.get("enabled")
    enabled = _truthy(enabled) if enabled is not None else _truthy(env.get("AXON_ENABLE_TELEMETRY", ""))
    try:
        rate = max(0.0, min(1.0, float(block.get("sampleRate", 1.0))))
    except (TypeError, ValueError):
        rate = 1.0
    cap = block.get("captureContent")
    return {
        "enabled": bool(enabled) and bool(endpoint),
        "requested": bool(enabled),
        "endpoint": endpoint, "headers": headers, "sampleRate": rate,
        "captureContent": _truthy(cap) if cap is not None else _truthy(env.get("AXON_TELEMETRY_CAPTURE_CONTENT", "")),
        "resourceAttributes": res,
        "serviceName": str(block.get("serviceName") or env.get("OTEL_SERVICE_NAME") or SERVICE_DEFAULT),
        "propagate": _truthy(block.get("propagate", False)),
        "propagateHosts": [str(h).lower() for h in (block.get("propagateHosts") or [])],
    }


def endpoint_host(endpoint: str) -> str:
    try:
        u = urlsplit(endpoint)
        return u.netloc.rsplit("@", 1)[-1] or "?"
    except Exception:
        return "?"


# ─── OTLP/HTTP JSON encoding ────────────────────────────────────────────────

def _val(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_val(x) for x in v]}}
    return {"stringValue": str(v)}


def _attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": _val(v)} for k, v in d.items() if v is not None]


def encode_spans(spans: list[dict[str, Any]], resource: dict[str, Any], version: str = "") -> bytes:
    out = []
    for s in spans:
        o: dict[str, Any] = {
            "traceId": s["trace_id"], "spanId": s["span_id"], "name": s["name"], "kind": _KIND.get(s.get("kind", "internal"), 1),
            "startTimeUnixNano": str(int(s["start"] * 1e9)), "endTimeUnixNano": str(int(max(s["end"], s["start"]) * 1e9)),
            "attributes": _attrs(s.get("attrs") or {}),
            "status": {"code": 2 if s.get("error") else 1, **({"message": str(s["error"])[:200]} if s.get("error") else {})},
        }
        if s.get("parent"):
            o["parentSpanId"] = s["parent"]
        if s.get("links"):
            o["links"] = [{"traceId": t, "spanId": i} for t, i in s["links"]]
        out.append(o)
    doc = {"resourceSpans": [{"resource": {"attributes": _attrs(resource)},
                              "scopeSpans": [{"scope": {"name": "axon", "version": version}, "spans": out}]}]}
    return json.dumps(doc, separators=(",", ":"), default=str).encode("utf-8")


# ─── exporter ───────────────────────────────────────────────────────────────

class Exporter:
    """Bounded queue plus one daemon thread. ``submit`` never blocks and never raises."""

    def __init__(self, cfg: dict[str, Any], version: str = "") -> None:
        self.cfg = cfg
        self.version = version
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUE)
        self.dropped = 0
        self.exported = 0
        self.failed = 0
        self.last_error = ""
        self.last_status: int | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.resource = {"service.name": cfg.get("serviceName") or SERVICE_DEFAULT, "service.version": version or "0",
                         "telemetry.sdk.name": "axon", "telemetry.sdk.language": "python", **(cfg.get("resourceAttributes") or {})}

    def submit(self, span: dict[str, Any]) -> None:
        try:
            self.q.put_nowait(span)
            self._idle.clear()
            self._ensure_thread()
            if self.q.qsize() >= BATCH:
                self._wake.set()
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="axon-otel", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(FLUSH_S)
            self._wake.clear()
            try:
                self._drain()
            except Exception as e:  # pragma: no cover - defensive
                self.last_error = f"{type(e).__name__}: {e}"[:200]
        try:
            self._drain()
        except Exception:
            pass

    def _drain(self) -> None:
        while True:
            batch: list[dict[str, Any]] = []
            try:
                while len(batch) < BATCH:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                self._idle.set()
                return
            self._post(batch)

    def _post(self, batch: list[dict[str, Any]]) -> None:
        try:
            body = encode_spans(batch, self.resource, self.version)
        except Exception as e:
            self.failed += len(batch)
            self.last_error = f"encode: {type(e).__name__}"[:200]
            return
        ok, status, err = post_json(self.cfg["endpoint"], body, self.cfg.get("headers") or {}, retries=RETRIES, stop=self._stop)
        self.last_status = status
        if ok:
            self.exported += len(batch)
            self.last_error = ""
        else:
            self.failed += len(batch)
            self.last_error = err

    def flush(self, timeout: float = 3.0) -> bool:
        """Wait (bounded) until the queue is empty and posted. Used at exit and by tests."""
        if self.q.empty() and self._idle.is_set():
            return True
        self._ensure_thread()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._wake.set()
            if self.q.empty() and self._idle.wait(0.05):
                return True
            time.sleep(0.01)
        return False

    def close(self, timeout: float = 3.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._wake.set()


def post_json(endpoint: str, body: bytes, headers: dict[str, str], retries: int = 1, stop: threading.Event | None = None,
              timeout: float = HTTP_TIMEOUT_S) -> tuple[bool, int | None, str]:
    """POST once with up to ``retries`` backoff retries on connection errors, 429 and 5xx. Never raises."""
    try:
        import httpx
    except Exception:  # pragma: no cover
        return False, None, "httpx is not installed"
    hdrs = {"content-type": "application/json", "user-agent": "axon-otel", **headers}
    status: int | None = None
    err = ""
    for attempt in range(max(1, retries)):
        try:
            r = httpx.post(endpoint, content=body, headers=hdrs, timeout=httpx.Timeout(timeout, connect=min(2.0, timeout)))
            status = r.status_code
            if 200 <= status < 300:
                return True, status, ""
            err = f"HTTP {status}"
            if status not in (408, 429) and status < 500:
                return False, status, err
        except Exception as e:
            err = f"{type(e).__name__}"[:80]
        if attempt + 1 < retries:
            delay = 0.25 * (2 ** attempt) + random.random() * 0.1
            if stop is not None:
                if stop.wait(delay):
                    break
            else:
                time.sleep(delay)
    return False, status, err


# ─── content handling ───────────────────────────────────────────────────────

def _scrub(text: str) -> str:
    try:
        from axon.session.activity import redact_text
        return redact_text(text)
    except Exception:
        return "[redaction unavailable]"


class _Content:
    """Redacted, capped text for one span (``MAX_CONTENT_BYTES`` in total)."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.left = MAX_CONTENT_BYTES
        self.truncated = False

    def take(self, text: Any) -> str | None:
        if not self.enabled or text is None:
            return None
        s = _scrub(str(text))
        b = s.encode("utf-8", "replace")
        if len(b) > self.left:
            self.truncated = True
            s = b[: max(0, self.left)].decode("utf-8", "ignore")
            b = s.encode("utf-8", "replace")
        self.left -= len(b)
        return s

    def apply(self, attrs: dict[str, Any]) -> None:
        if self.truncated:
            attrs["axon.content.truncated"] = True


def _msg(role: str, text: str) -> str:
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": text}]}], ensure_ascii=False)


# ─── the observer ───────────────────────────────────────────────────────────

def _hid(n: int) -> str:
    return uuid.uuid4().hex[: n]


_ACTIVE: list["Tracker"] = []     # the main agent's tracker, for traceparent injection


class Tracker:
    """Builds spans for one agent from its event stream. Every public method swallows its own errors."""

    def __init__(self, agent: Any, cfg: dict[str, Any], exporter: Exporter) -> None:
        self.agent = agent
        self.cfg = cfg
        self.exp = exporter
        self.enabled = True
        self.lock = threading.RLock()
        self.trace_id = ""
        self.root_id = ""
        self.root: dict[str, Any] | None = None
        self.sampled = False
        self.content = _Content(False)
        self.llm: dict[str, Any] | None = None
        self.tools: dict[str, dict[str, Any]] = {}
        self.subs: dict[str, dict[str, Any]] = {}
        self.pending_perm: dict[str, Any] = {}
        self.retries = 0
        self.last_ts = 0.0
        self.turn_stats = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "llm": 0, "tools": 0}
        self.spans_made = 0

    # -- helpers
    def _cid(self) -> str:
        return str(getattr(getattr(self.agent, "session", None), "active_session_id", "") or "")

    def _provider(self) -> str:
        s = self.agent.settings
        p = str(getattr(s, "provider", "") or "")
        if not p:
            p = str(getattr(getattr(self.agent, "provider", None), "name", "") or "")
        return p or "unknown"

    def _emit(self, name: str, start: float, end: float, attrs: dict[str, Any], kind: str = "internal", parent: str | None = None,
              error: str = "", span_id: str = "", links: list[tuple[str, str]] | None = None) -> str:
        sid = span_id or _hid(16)
        if not self.sampled:
            return sid
        attrs = {k: v for k, v in attrs.items() if v is not None and v != ""}
        attrs.setdefault("gen_ai.conversation.id", self._cid())
        self.exp.submit({"trace_id": self.trace_id, "span_id": sid, "parent": parent if parent is not None else self.root_id,
                         "name": name, "kind": kind, "start": start, "end": end, "attrs": attrs, "error": error,
                         "links": links or []})
        self.spans_made += 1
        return sid

    # -- turn
    def begin_turn(self, prompt: str) -> None:
        with self.lock:
            self.trace_id, self.root_id = _hid(32), _hid(16)
            self.sampled = self.enabled and random.random() < float(self.cfg.get("sampleRate", 1.0))
            self.content = _Content(bool(self.cfg.get("captureContent")))
            self.llm, self.tools, self.subs, self.pending_perm = None, {}, {}, {}
            self.turn_stats = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "llm": 0, "tools": 0}
            self.root = {"start": time.time(), "prompt": prompt}
            _ACTIVE[:] = [self]

    def end_turn(self, result: Any, error: BaseException | None) -> None:
        with self.lock:
            if self.root is None:
                return
            now = time.time()
            for tid in list(self.tools):
                self._finish_tool(tid, now)
            for tid in list(self.subs):
                self._end_sub(tid, now, status="cancelled")
            if self.llm is not None:
                self._finish_llm(None, now)
            u = getattr(result, "usage", None)
            stop = str(getattr(result, "stop_reason", "") or ("interrupted" if error is None or isinstance(error, KeyboardInterrupt) else "exception"))
            err = str(getattr(result, "error", "") or "") if result is not None else (f"{type(error).__name__}" if error is not None and not isinstance(error, KeyboardInterrupt) else "")
            ts = self.turn_stats
            a = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": str(getattr(self.agent, "agent_name", "") or "axon"),
                 "gen_ai.provider.name": self._provider(), "gen_ai.request.model": str(self.agent.settings.model),
                 "axon.turn.stop_reason": stop, "axon.turn.number": int(getattr(self.agent, "_turn_counter", 0) or 0),
                 "axon.turn.llm_calls": ts["llm"], "axon.turn.tool_calls": ts["tools"],
                 "axon.permission.mode": str(getattr(self.agent.settings, "mode", "") or ""),
                 "gen_ai.usage.input_tokens": int(getattr(u, "input", 0) or ts["input"]) if u is not None else ts["input"],
                 "gen_ai.usage.output_tokens": int(getattr(u, "output", 0) or ts["output"]) if u is not None else ts["output"],
                 "axon.cost.usd": round(ts["cost"], 6)}
            c = self.content.take(self.root["prompt"])
            if c is not None:
                a["gen_ai.input.messages"] = _msg("user", c)
            ft = self.content.take(getattr(result, "final_text", "") or "") if result is not None else None
            if ft:
                a["gen_ai.output.messages"] = _msg("assistant", ft)
            self.content.apply(a)
            self._emit("axon.turn", self.root["start"], now, a, "internal", parent="", error=err, span_id=self.root_id)
            self.root = None
            if _ACTIVE and _ACTIVE[0] is self:
                _ACTIVE.clear()

    # -- events
    def observe(self, ev: Any) -> None:
        if not self.enabled or self.root is None:
            return
        try:
            with self.lock:
                self._observe(ev)
        except Exception:
            pass

    def _observe(self, ev: Any) -> None:
        from axon.agent import trace as T
        from axon.providers import base as B
        now = time.time()
        if isinstance(ev, B.LLMCallStart):
            if self.llm is not None:
                self._finish_llm(None, now)
            self.retries = 0
            self.llm = {"start": now, "id": _hid(16), "iteration": ev.iteration, "model": ev.model, "ttfc": None, "text": [], "req": {}}
        elif isinstance(ev, (B.TextDelta, B.ThinkingDelta)):
            if self.llm is not None:
                if self.llm["ttfc"] is None:
                    self.llm["ttfc"] = now - self.llm["start"]
                if isinstance(ev, B.TextDelta) and self.content.enabled and len(self.llm["text"]) < 4096:
                    self.llm["text"].append(ev.text)
        elif isinstance(ev, T.LLMRequestInfo):
            if self.llm is not None:
                self.llm["req"] = {"gen_ai.request.max_tokens": ev.max_tokens, "axon.request.effort": ev.effort,
                                   "axon.request.thinking": ev.thinking, "axon.request.messages": ev.message_count,
                                   "axon.request.tools": ev.tool_count, "axon.request.cache_markers": ev.cache_markers,
                                   "gen_ai.provider.name": ev.provider or None}
        elif isinstance(ev, T.RetryEvent):
            self.retries += 1
            if self.llm is not None:
                self.llm.setdefault("retry_errors", []).append(_scrub(ev.error)[:160])
        elif isinstance(ev, T.LLMCallEnd):
            self._finish_llm(ev, now)
        elif isinstance(ev, B.ToolExecutionStart):
            self.tools[ev.id] = {"id": ev.id, "name": ev.name, "start": now, "input": ev.input, "server": getattr(ev, "server", ""),
                                 "span": _hid(16), "result": None, "timing": None}
        elif isinstance(ev, T.PermissionDecisionEvent):
            self.pending_perm[ev.id] = ev
        elif isinstance(ev, T.ToolTiming):
            t = self.tools.setdefault(ev.id, {"id": ev.id, "name": ev.name, "start": ev.started_at, "input": {}, "server": "",
                                              "span": _hid(16), "result": None, "timing": None})
            t["timing"] = ev
            if t["result"] is not None:
                self._finish_tool(ev.id, now)
        elif isinstance(ev, B.ToolExecutionResult):
            t = self.tools.setdefault(ev.id, {"id": ev.id, "name": ev.name, "start": now, "input": ev.input, "server": "",
                                              "span": _hid(16), "result": None, "timing": None})
            t["result"] = ev
            if t["timing"] is not None:
                self._finish_tool(ev.id, now)
        elif isinstance(ev, T.SubagentEvent):
            self._subagent(ev, now)
        elif isinstance(ev, T.SubagentStep):
            self._sub_step(ev, now)
        elif isinstance(ev, T.CompactionEvent):
            if not ev.preview and not ev.phase:
                a = {"gen_ai.operation.name": "compact", "axon.compaction.trigger": ev.trigger, "axon.compaction.messages_before": ev.messages_before,
                     "axon.compaction.messages_after": ev.messages_after, "axon.compaction.tokens_before": ev.tokens_before,
                     "axon.compaction.tokens_after": ev.tokens_after, "gen_ai.request.model": ev.model, "axon.cost.usd": round(float(ev.cost or 0), 6),
                     "axon.compaction.blocked_by_hook": ev.blocked_by_hook or None, **self._usage(ev.usage)}
                self.turn_stats["cost"] += float(ev.cost or 0)
                self._emit("axon.compaction", now, now, a)
        elif isinstance(ev, T.GoalCheckEvent):
            if ev.phase == "verdict":
                a = {"gen_ai.operation.name": "chat", "axon.goal.verdict": ev.verdict, "axon.goal.round": ev.round,
                     "gen_ai.request.model": ev.model, "axon.cost.usd": round(float(ev.cost or 0), 6), **self._usage(ev.usage)}
                self.turn_stats["cost"] += float(ev.cost or 0)
                self._emit("axon.goal_check", now - max(0.0, ev.duration_s), now, a, "client")
        elif isinstance(ev, T.AdvisorEvent):
            a = {"gen_ai.operation.name": "chat", "gen_ai.request.model": ev.model, "axon.cost.usd": round(float(ev.cost or 0), 6),
                 **self._usage(ev.usage)}
            c = self.content.take(ev.question)
            if c is not None:
                a["gen_ai.input.messages"] = _msg("user", c)
                r = self.content.take(ev.advice)
                if r:
                    a["gen_ai.output.messages"] = _msg("assistant", r)
            self.content.apply(a)
            self.turn_stats["cost"] += float(ev.cost or 0)
            self._emit("axon.advisor", now - max(0.0, ev.duration_s), now, a, "client", error=ev.error)
        elif isinstance(ev, T.AutoPermissionEvent):
            if ev.source == "classifier" or ev.usage is not None:
                a = {"gen_ai.operation.name": "chat", "gen_ai.request.model": ev.model, "axon.permission.decision": ev.outcome,
                     "axon.permission.source": ev.source, "gen_ai.tool.name": ev.tool, "axon.cost.usd": round(float(ev.cost or 0), 6),
                     **self._usage(ev.usage)}
                self.turn_stats["cost"] += float(ev.cost or 0)
                self._emit("axon.permission_check", now - max(0.0, ev.duration_s), now, a, "client",
                           error="classifier error" if ev.source == "error" else "")

    @staticmethod
    def _usage(u: Any) -> dict[str, Any]:
        if u is None:
            return {}
        return {"gen_ai.usage.input_tokens": int(getattr(u, "input", 0) or 0), "gen_ai.usage.output_tokens": int(getattr(u, "output", 0) or 0),
                "gen_ai.usage.cache_read.input_tokens": int(getattr(u, "cache_read", 0) or 0),
                "gen_ai.usage.cache_creation.input_tokens": int(getattr(u, "cache_write", 0) or 0)}

    def _finish_llm(self, end: Any, now: float) -> None:
        cur, self.llm = self.llm, None
        if cur is None:
            return
        u = getattr(end, "usage", None)
        stop = str(getattr(end, "stop_reason", "") or "")
        a: dict[str, Any] = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": self._provider(),
                             "gen_ai.request.model": str(getattr(end, "model", "") or cur["model"]),
                             "gen_ai.response.finish_reasons": [stop] if stop else None, "axon.iteration": cur["iteration"],
                             "axon.retries": self.retries or None, "gen_ai.response.time_to_first_chunk": round(cur["ttfc"], 4) if cur["ttfc"] is not None else None,
                             "axon.cost.usd": round(float(getattr(end, "cost", 0) or 0), 6) if end is not None else None,
                             "axon.outcome": getattr(end, "outcome", None), "axon.tool_use_count": getattr(end, "tool_use_count", None) or None,
                             "axon.cache.miss_cause": getattr(end, "cache_miss_cause", "") or None, **cur["req"], **self._usage(u)}
        if cur.get("retry_errors"):
            a["axon.retry.errors"] = cur["retry_errors"][:5]
        if self.content.enabled and cur["text"]:
            t = self.content.take("".join(cur["text"]))
            if t:
                a["gen_ai.output.messages"] = _msg("assistant", t)
        self.content.apply(a)
        ts = self.turn_stats
        ts["llm"] += 1
        if end is not None:
            ts["cost"] += float(end.cost or 0)
            ts["input"] += int(getattr(u, "input", 0) or 0)
            ts["output"] += int(getattr(u, "output", 0) or 0)
        err = ""
        if end is not None and getattr(end, "outcome", "ok") == "error":
            err = _scrub(str(end.error))[:200] or "error"
            a["error.type"] = "provider_error"
        start = float(getattr(end, "started_at", 0) or cur["start"])
        self._emit(f"chat {a['gen_ai.request.model']}", start, float(getattr(end, "ended_at", 0) or now), a, "client", error=err, span_id=cur["id"])

    def _finish_tool(self, tid: str, now: float) -> None:
        t = self.tools.pop(tid, None)
        if t is None:
            return
        tm, res, perm = t["timing"], t["result"], self.pending_perm.pop(tid, None)
        start = float(tm.started_at) if tm is not None else float(t["start"])
        end = float(tm.ended_at) if tm is not None else now
        is_err = bool(getattr(res, "is_error", False) or getattr(tm, "is_error", False))
        denied = perm is not None and perm.outcome == "deny"
        a: dict[str, Any] = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": t["name"], "gen_ai.tool.call.id": tid,
                             "gen_ai.tool.type": "function", "axon.tool.status": "denied" if denied else ("error" if is_err else ("ok" if res is not None else "unknown")),
                             "axon.tool.duration_ms": int((end - start) * 1000), "axon.mcp.server": t.get("server") or getattr(res, "server", "") or None}
        if perm is not None:
            a.update({"axon.permission.decision": perm.outcome, "axon.permission.source": perm.source, "axon.permission.rule": perm.rule or None,
                      "axon.permission.mode": perm.mode or None, "axon.permission.approval": perm.approval})
        if tm is not None:
            a.update({"axon.tool.result.lines": tm.lines, "axon.tool.result.bytes": tm.bytes, "axon.tool.parallel": tm.parallel or None})
        if self.content.enabled:
            inp = self.content.take(json.dumps(t["input"], default=str, ensure_ascii=False)) if t["input"] else None
            out = self.content.take(getattr(res, "content", "")) if res is not None else None
            if inp is not None:
                a["gen_ai.tool.call.arguments"] = inp
            if out is not None:
                a["gen_ai.tool.call.result"] = out
        self.content.apply(a)
        self.turn_stats["tools"] += 1
        self._emit(f"execute_tool {t['name']}", start, end, a, "internal", error="tool error" if is_err or denied else "",
                   span_id=t["span"])

    # -- subagents
    def _subagent(self, ev: Any, now: float) -> None:
        if ev.phase in ("queued", "start"):
            if ev.task_id not in self.subs:
                spawner = next((t["span"] for t in self.tools.values() if str(t["name"]).lower() in ("agent", "task", "subagent", "spawn_agent")), self.root_id)
                self.subs[ev.task_id] = {"id": _hid(16), "start": now, "parent": spawner, "ev": ev, "steps": 0}
        elif ev.phase == "end":
            if ev.task_id not in self.subs:
                self.subs[ev.task_id] = {"id": _hid(16), "start": now - float(ev.duration_s or 0), "parent": self.root_id, "ev": ev, "steps": 0}
            self._end_sub(ev.task_id, now, ev)

    def _end_sub(self, task_id: str, now: float, ev: Any = None, status: str = "") -> None:
        s = self.subs.pop(task_id, None)
        if s is None:
            return
        ev = ev or s["ev"]
        st = status or str(ev.status or "")
        a = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": ev.agent_type, "gen_ai.agent.id": task_id,
             "gen_ai.agent.description": ev.title if self.content.enabled else None,
             "gen_ai.request.model": ev.model, "gen_ai.usage.input_tokens": ev.input_tokens, "gen_ai.usage.output_tokens": ev.output_tokens,
             "axon.cost.usd": round(float(ev.cost or 0), 6), "axon.subagent.status": st, "axon.subagent.iterations": ev.iterations or None,
             "axon.subagent.steps": s["steps"]}
        self.turn_stats["cost"] += float(ev.cost or 0)
        bad = st in ("error", "failed", "cancelled")
        self._emit(f"invoke_agent {ev.agent_type}", float(s["start"]), now, a, "internal", parent=s["parent"],
                   error=(_scrub(ev.detail)[:200] or st) if bad else "", span_id=s["id"], links=[(self.trace_id, self.root_id)])

    def _sub_step(self, ev: Any, now: float) -> None:
        s = self.subs.get(ev.task_id)
        if s is None:
            s = self.subs[ev.task_id] = {"id": _hid(16), "start": now, "parent": self.root_id, "steps": 0,
                                         "ev": type("E", (), {"agent_type": ev.agent_type, "title": "", "model": "", "input_tokens": 0, "output_tokens": 0,
                                                              "cost": 0.0, "status": "running", "iterations": 0, "detail": "", "duration_s": 0.0})()}
        if ev.kind not in ("llm", "tool"):
            return
        s["steps"] += 1
        start = now - float(ev.duration_s or 0)
        if ev.kind == "llm":
            a = {"gen_ai.operation.name": "chat", "gen_ai.request.model": ev.name, "gen_ai.usage.input_tokens": ev.input_tokens,
                 "gen_ai.usage.output_tokens": ev.output_tokens, "axon.cost.usd": round(float(ev.cost or 0), 6),
                 "gen_ai.agent.name": ev.agent_type, "gen_ai.provider.name": self._provider()}
            self._emit(f"chat {ev.name}", start, now, a, "client", parent=s["id"], error="error" if ev.is_error else "")
        else:
            a = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": ev.name, "gen_ai.agent.name": ev.agent_type,
                 "axon.tool.status": "error" if ev.is_error else "ok"}
            c = self.content.take(ev.summary)
            if c is not None:
                a["gen_ai.tool.call.result"] = c
            self._emit(f"execute_tool {ev.name}", start, now, a, "internal", parent=s["id"], error="tool error" if ev.is_error else "")

    def status_line(self) -> str:
        e = self.exp
        return (f"telemetry: on -> {endpoint_host(self.cfg['endpoint'])} · {e.exported} sent, {e.dropped} dropped, "
                f"queue {e.q.qsize()}" + (f" · last error: {e.last_error}" if e.last_error else ""))


# ─── attaching to an agent ──────────────────────────────────────────────────

_EXPORTERS: dict[tuple[str, str], Exporter] = {}
_xlock = threading.Lock()


def _version() -> str:
    try:
        from axon import __version__
        return str(__version__)
    except Exception:
        return ""


def get_exporter(cfg: dict[str, Any]) -> Exporter:
    key = (cfg["endpoint"], json.dumps(cfg.get("headers") or {}, sort_keys=True) + str(cfg.get("serviceName")) + json.dumps(cfg.get("resourceAttributes") or {}, sort_keys=True))
    with _xlock:
        e = _EXPORTERS.get(key)
        if e is None:
            e = _EXPORTERS[key] = Exporter(cfg, _version())
    return e


def flush_all(timeout: float = 3.0) -> None:
    for e in list(_EXPORTERS.values()):
        try:
            e.close(timeout)
        except Exception:
            pass


atexit.register(flush_all)


def attach(agent: Any) -> Tracker | None:
    """Start tracing *agent* when telemetry is enabled. Safe to call twice; never raises. Subagent agents are skipped
    (their work reaches the parent as SubagentEvent/SubagentStep)."""
    try:
        if getattr(agent, "is_subagent", False):
            return None
        existing = getattr(agent, "_otel", None)
        cfg = resolve_config(agent.settings)
        if existing is not None:
            existing.cfg = cfg
            existing.enabled = cfg["enabled"]
            if cfg["enabled"]:
                existing.exp = get_exporter(cfg)
            return existing
        if not cfg["enabled"]:
            return None
        tr = Tracker(agent, cfg, get_exporter(cfg))
        orig_deliver, orig_turn = agent._deliver, agent._traced_turn

        def deliver(event: Any, *a: Any, **kw: Any) -> Any:
            tr.observe(event)
            return orig_deliver(event, *a, **kw)

        def turn(user_input: str) -> Any:
            if not tr.enabled:
                return orig_turn(user_input)
            try:
                tr.begin_turn(user_input)
            except Exception:
                pass
            result, error = None, None
            try:
                result = orig_turn(user_input)
                return result
            except BaseException as e:
                error = e
                raise
            finally:
                try:
                    tr.end_turn(result, error)
                except Exception:
                    pass

        agent._deliver = deliver
        agent._traced_turn = turn
        agent._otel = tr
        return tr
    except Exception:
        return None


def send_test(settings: Any) -> tuple[bool, str]:
    """One span to the configured endpoint; reports the HTTP result. Works even when telemetry is switched off."""
    cfg = resolve_config(settings)
    if not cfg["endpoint"]:
        return False, "no endpoint: set telemetry.endpoint or OTEL_EXPORTER_OTLP_ENDPOINT"
    now = time.time()
    span = {"trace_id": _hid(32), "span_id": _hid(16), "parent": "", "name": "axon.telemetry_test", "kind": "internal",
            "start": now, "end": now, "attrs": {"axon.test": True}, "error": ""}
    res = {"service.name": cfg["serviceName"], "service.version": _version(), **cfg["resourceAttributes"]}
    ok, status, err = post_json(cfg["endpoint"], encode_spans([span], res, _version()), cfg["headers"], retries=1)
    host = endpoint_host(cfg["endpoint"])
    return ok, f"{host}: HTTP {status}" if status is not None else f"{host}: {err or 'failed'}"


def status_lines(agent: Any) -> list[str]:
    cfg = resolve_config(agent.settings)
    tr = getattr(agent, "_otel", None)
    lines = []
    if tr is not None and tr.enabled:
        e = tr.exp
        lines.append(f"Telemetry: on  endpoint {endpoint_host(cfg['endpoint'])}  service {cfg['serviceName']}")
        lines.append(f"  sample rate {cfg['sampleRate']:g}  content {'captured (redacted, capped)' if cfg['captureContent'] else 'not captured (metadata only)'}"
                     f"  traceparent {'on' if cfg['propagate'] else 'off'}")
        lines.append(f"  queue {e.q.qsize()}/{MAX_QUEUE}  sent {e.exported}  failed {e.failed}  dropped {e.dropped}"
                     f"  last HTTP {e.last_status if e.last_status is not None else '-'}")
        lines.append(f"  last error: {e.last_error or 'none'}")
    elif cfg["requested"] and not cfg["endpoint"]:
        lines.append("Telemetry: on but no endpoint (set telemetry.endpoint or OTEL_EXPORTER_OTLP_ENDPOINT)")
    else:
        lines.append("Telemetry: off" + (f"  (endpoint {endpoint_host(cfg['endpoint'])} configured)" if cfg["endpoint"] else ""))
    return lines


def usage_line(agent: Any) -> str:
    """One line for /usage; empty when telemetry is off."""
    tr = getattr(agent, "_otel", None)
    if tr is None or not tr.enabled:
        return ""
    return tr.status_line()


# ─── traceparent for gateways ───────────────────────────────────────────────

def propagation_headers(settings: Any) -> dict[str, str]:
    """``traceparent`` for the model request in flight, only when ``propagate`` is on, a turn is being traced, and the
    request goes to a base URL the user configured (never a known public vendor host unless listed in propagateHosts)."""
    try:
        if not _ACTIVE:
            return {}
        tr = _ACTIVE[0]
        if not (tr.enabled and tr.sampled and tr.cfg.get("propagate")) or tr.root is None:
            return {}
        host = (urlsplit(str(getattr(settings, "base_url", "") or "")).hostname or "").lower()
        if not host:
            return {}
        allowed = tr.cfg.get("propagateHosts") or []
        if host not in allowed and (host in _VENDOR_HOSTS or any(host.endswith("." + v) for v in _VENDOR_HOSTS)):
            return {}
        span = (tr.llm or {}).get("id") or tr.root_id
        return {"traceparent": f"00-{tr.trace_id}-{span}-01"}
    except Exception:
        return {}
