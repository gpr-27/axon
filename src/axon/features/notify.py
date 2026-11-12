"""Notification events and their sinks: signed webhooks and open web pages.

Events (``type``): ``requires_action`` (a permission or a question is waiting), ``budget_reached``, ``idle`` (a run
finished), ``run_failed``, ``scheduled_task_finished``, ``subagent_finished`` and ``test``.

Settings: one ``notifications`` block in settings.json (the plain string form, ``"important"`` and so on, still means
only the desktop mode)::

    "notifications": {
      "mode": "important",            # desktop: important | all | off
      "enabled": true,                # false switches every sink here off (``/notify off``)
      "web": true,                    # push events to open dashboard / phone pages
      "allowPrivate": false,          # allow webhook targets on private / loopback addresses
      "webhooks": [{"url": "https://example.com/hook", "secret": "whsec_...", "events": ["requires_action", "idle"]}]
    }

A webhook receives ``{"id", "type", "time", "session", "summary"}`` as JSON, signed the way the Managed Agents webhooks
are (the Standard Webhooks scheme): headers ``webhook-id``, ``webhook-timestamp`` (Unix seconds, regenerated on every
attempt) and ``webhook-signature: v1,<base64 HMAC-SHA256 of "<id>.<timestamp>.<raw body>">``, with a ``whsec_`` secret
whose base64 part is the key. ``id`` is unique per event and identical on every retry: receivers dedupe on it and
reject a timestamp older than five minutes (see :func:`verify`). Delivery runs on a background thread, with a short
timeout and up to three attempts with jittered backoff; it never delays or fails a turn, and an event that cannot be
delivered is dropped (webhooks are not a log). The payload carries one short, redacted summary and never file contents.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import random
import secrets as _secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

EVENT_TYPES = ("requires_action", "budget_reached", "idle", "run_failed", "scheduled_task_finished",
               "subagent_finished", "test")
MODES = ("important", "all", "off")
ATTEMPTS = 3
BACKOFF_S = (1.0, 4.0)          # between attempts (jittered); the Managed Agents service uses 5-120s
TIMEOUT_S = 5.0
REPLAY_WINDOW_S = 300
MAX_SUMMARY = 200
_MAX_INFLIGHT = 8

_settings_ref: Any = None
_listeners: list[Callable[[dict[str, Any]], None]] = []
_results: list[dict[str, Any]] = []          # the last few deliveries, for /notify status
_inflight = threading.BoundedSemaphore(_MAX_INFLIGHT)
_lock = threading.Lock()


# ─── settings block ──────────────────────────────────────────────────────────

def split_block(block: dict[str, Any]) -> dict[str, Any]:
    """The Settings fields for a ``notifications`` object: the desktop mode stays in ``notifications``."""
    out: dict[str, Any] = {"notify_sinks": normalize_block(block)}
    mode = str(block.get("mode") or "").strip().lower()
    if mode in MODES:
        out["notifications"] = mode
    return out


def normalize_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {}
    hooks: list[dict[str, Any]] = []
    for w in block.get("webhooks") or []:
        if isinstance(w, str):
            w = {"url": w}
        if not isinstance(w, dict) or not str(w.get("url") or "").strip():
            continue
        evs = [e for e in (w.get("events") or []) if e in EVENT_TYPES]
        hooks.append({"url": str(w["url"]).strip(), "secret": str(w.get("secret") or ""), "events": evs,
                      "allowPrivate": bool(w.get("allowPrivate", block.get("allowPrivate", False)))})
    return {"enabled": block.get("enabled", True) is not False, "web": block.get("web", True) is not False,
            "allowPrivate": bool(block.get("allowPrivate", False)), "webhooks": hooks}


def sinks(settings: Any) -> dict[str, Any]:
    b = getattr(settings, "notify_sinks", None)
    return normalize_block(b) if isinstance(b, dict) and b else {"enabled": True, "web": True, "allowPrivate": False, "webhooks": []}


# ─── events ──────────────────────────────────────────────────────────────────

def clean_summary(text: Any) -> str:
    """One short line, control characters removed, secrets masked, never file contents (callers pass words)."""
    from axon.ui.notify import safe_notice_text
    try:
        from axon.features.memory_files import redact
        text = redact(str(text or ""))
    except Exception:
        pass
    return safe_notice_text(text, MAX_SUMMARY)


def make_event(kind: str, summary: str, session: str = "") -> dict[str, Any]:
    return {"id": "evt_" + uuid.uuid4().hex, "type": kind,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": str(session or "")[:120], "summary": clean_summary(summary)}


def body_bytes(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


# ─── signing (Standard Webhooks, as in the Managed Agents webhooks docs) ─────

def _key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[6:], validate=True)
        except Exception:
            pass
    return secret.encode("utf-8")


def sign(secret: str, msg_id: str, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(_key(secret), f"{msg_id}.{timestamp}.".encode("utf-8") + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode("ascii")


def _hook_secret(hook: dict[str, Any], url: str) -> str:
    """The signing secret: a ``vault:NAME`` reference is read from the vault and only released to its bound URL."""
    raw = str(hook.get("secret") or "")
    if raw.startswith("vault:"):
        from axon.features import vault
        return vault.resolve_ref(raw, url)
    return raw


def new_secret() -> str:
    return "whsec_" + base64.b64encode(_secrets.token_bytes(32)).decode("ascii")


def headers_for(event: dict[str, Any], body: bytes, secret: str, now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    h = {"Content-Type": "application/json", "User-Agent": "axon-notify/1", "webhook-id": event["id"],
         "webhook-timestamp": str(ts)}
    if secret:
        h["webhook-signature"] = sign(secret, event["id"], ts, body)
    return h


def verify(secret: str, headers: dict[str, str], body: bytes, tolerance: int = REPLAY_WINDOW_S,
           now: float | None = None) -> dict[str, Any]:
    """What a receiver does: check the timestamp window and the signature, then parse. Raises ValueError."""
    low = {str(k).lower(): str(v) for k, v in headers.items()}
    try:
        ts = int(low["webhook-timestamp"])
        msg_id = low["webhook-id"]
        sigs = low["webhook-signature"].split()
    except (KeyError, ValueError):
        raise ValueError("missing or malformed webhook headers") from None
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        raise ValueError("timestamp outside the replay window")
    want = sign(secret, msg_id, ts, body)
    if not any(hmac.compare_digest(want, s) for s in sigs):
        raise ValueError("invalid signature")
    return json.loads(body.decode("utf-8"))


# ─── targets (SSRF) ──────────────────────────────────────────────────────────

def _resolve(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def is_private_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified)


def check_target(url: str, allow_private: bool = False, resolve: Callable[[str], list[str]] | None = None
                 ) -> tuple[bool, list[str]]:
    """(allowed, notes). Only http and https; a name that resolves to a private, loopback or link-local address is
    refused unless allowed; plain http and a failed lookup only warn."""
    notes: list[str] = []
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return False, ["not a valid URL"]
    if parts.scheme not in ("http", "https"):
        return False, ["only http and https URLs are allowed"]
    if not host:
        return False, ["the URL has no host"]
    if parts.username or parts.password:
        return False, ["put the secret in the webhook's secret field, not in the URL"]
    if parts.scheme == "http":
        notes.append("plain http: the summary travels unencrypted")
    try:
        addrs = (resolve or _resolve)(host)
    except OSError:
        return True, notes + [f"could not resolve {host} just now"]
    private = [a for a in addrs if is_private_address(a)]
    if private:
        msg = f"{host} points at a private or local address ({private[0]})"
        if not allow_private:
            return False, [msg + "; set allowPrivate to send there anyway"] + notes
        notes.append(msg + " (allowed)")
    return True, notes


def shown_url(url: str) -> str:
    """Host and path only: a webhook URL can carry a token in its query string."""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname}{p.path[:40]}" + ("?…" if p.query else "")
    except ValueError:
        return "(invalid URL)"


# ─── delivery ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:     # redirects are never followed
        return None


def _send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
    """POST and return the HTTP status (a 3xx or error status is returned, not raised); network errors raise."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retryable(status: int) -> bool:
    return status in (408, 425, 429) or status >= 500


def deliver(event: dict[str, Any], hook: dict[str, Any], *, attempts: int = ATTEMPTS) -> dict[str, Any]:
    """Send one event to one webhook with retries. Every attempt carries the same id and body; only the timestamp
    (and so the signature) is fresh. Returns {"url", "ok", "attempts", "status", "error"}."""
    url = hook["url"]
    res: dict[str, Any] = {"url": shown_url(url), "ok": False, "attempts": 0, "status": None, "error": ""}
    ok, notes = check_target(url, bool(hook.get("allowPrivate")))
    if not ok:
        res["error"] = notes[0]
        return res
    body = body_bytes(event)
    for n in range(1, max(1, attempts) + 1):
        res["attempts"] = n
        try:
            status = _send(url, headers_for(event, body, _hook_secret(hook, url)), body, TIMEOUT_S)
            res["status"] = status
            if 200 <= status < 300:
                res["ok"], res["error"] = True, ""
                return res
            res["error"] = f"HTTP {status}" + (" (redirects are not followed)" if 300 <= status < 400 else "")
            if not _retryable(status):
                return res
        except Exception as e:  # noqa: BLE001 - any transport failure is a failed attempt
            res["error"] = f"{type(e).__name__}"
        if n < attempts:
            base = BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)]
            _sleep(base * random.uniform(0.8, 1.2))
    return res


def _record(res: dict[str, Any], event: dict[str, Any]) -> None:
    with _lock:
        _results.append({**res, "type": event["type"], "id": event["id"], "at": time.time()})
        del _results[:-8]


def _deliver_async(event: dict[str, Any], hook: dict[str, Any]) -> None:
    if not _inflight.acquire(blocking=False):
        return                                  # too many deliveries running: drop rather than pile up

    def work() -> None:
        try:
            _record(deliver(event, hook), event)
        except Exception:
            pass
        finally:
            _inflight.release()

    try:
        threading.Thread(target=work, name="axon-webhook", daemon=True).start()
    except Exception:
        _inflight.release()


# ─── emitting ────────────────────────────────────────────────────────────────

def add_listener(fn: Callable[[dict[str, Any]], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn: Callable[[dict[str, Any]], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def remember(settings: Any) -> None:
    global _settings_ref
    if settings is not None:
        _settings_ref = settings


def emit(kind: str, summary: str, *, session: str = "", settings: Any = None) -> dict[str, Any] | None:
    """Create an event and hand it to every sink that is on. Never raises and never waits for the network."""
    try:
        if settings is not None:
            remember(settings)
        cfg = sinks(settings if settings is not None else _settings_ref)
        if not cfg["enabled"]:
            return None
        event = make_event(kind, summary, session)
        if cfg["web"]:
            for fn in list(_listeners):
                try:
                    fn(event)
                except Exception:
                    pass
        for hook in cfg["webhooks"]:
            if hook["events"] and kind not in hook["events"]:
                continue
            _deliver_async(event, {**hook, "allowPrivate": hook.get("allowPrivate") or cfg["allowPrivate"]})
        return event
    except Exception:
        return None


def emit_for_agent(agent: Any, kind: str, summary: str) -> dict[str, Any] | None:
    sid = ""
    try:
        sid = str(getattr(agent.session, "active_session_id", "") or "")
    except Exception:
        pass
    return emit(kind, summary, session=sid, settings=getattr(agent, "settings", None))


_WORDS = {"error": "stopped with an error", "max_iterations": "reached its step limit"}


def turn_finished(agent: Any, result: Any) -> None:
    """A run ended (called once per main-agent turn): finished, failed, or stopped by a spending limit."""
    if getattr(agent, "is_subagent", False):
        return
    stop = str(getattr(result, "stop_reason", "") or "")
    if stop in ("blocked", "interrupted", "declined"):
        return
    if stop == "budget_exceeded":
        if getattr(agent, "budget_pause", None) is None:       # a daily or monthly limit (a pause already announced itself)
            emit_for_agent(agent, "budget_reached", "A spending limit stopped the run")
        return
    if stop in ("error", "max_iterations"):
        emit_for_agent(agent, "run_failed", f"Axon {_WORDS[stop]}")
        return
    emit_for_agent(agent, "idle", "Axon finished and is waiting for you")


def from_agent_notification(agent: Any, notification_type: str, message: str) -> None:
    if notification_type in ("permission_prompt", "question"):
        emit_for_agent(agent, "requires_action", message)


def scheduled_finished(rec: dict[str, Any]) -> None:
    try:
        from axon.features import scheduler as sch
        name = sch.notice_name(rec.get("name"))
    except Exception:
        name = "A scheduled task"
    try:
        from axon.features.schedule_ext import outcome
        word = outcome(rec.get("status")) or "finished"
    except Exception:
        word = rec.get("status") or "finished"
    emit("scheduled_task_finished", f"{name}: {word}", session="")


def install_scheduler() -> None:
    try:
        from axon.features.scheduler import get_scheduler
        get_scheduler().add_listener(scheduled_finished)
    except Exception:
        pass


# ─── /notify ─────────────────────────────────────────────────────────────────

def _persist(agent: Any, block: dict[str, Any], mode: str | None = None) -> None:
    from axon.commands.common import update_settings, update_settings_file, ws_of
    updates: dict[str, Any] = {"notify_sinks": block}
    if mode:
        updates["notifications"] = mode
    update_settings(agent, **updates)
    current = str(getattr(agent.settings, "notifications", "important") or "important")
    update_settings_file({"notifications": {"mode": mode or current, **{k: v for k, v in block.items()}}},
                         workspace=ws_of(agent))


def persist_mode(agent: Any, mode: str) -> None:
    """``/notify important|all|off`` keeps the sinks block that is already there."""
    from axon.commands.common import update_settings, update_settings_file, ws_of
    block = dict(sinks(agent.settings))
    update_settings(agent, notifications=mode)
    if block["webhooks"] or not block["enabled"] or not block["web"]:
        update_settings_file({"notifications": {"mode": mode, **block}}, workspace=ws_of(agent))
    else:
        update_settings_file({"notifications": mode}, workspace=ws_of(agent))


def status_lines(agent: Any) -> list[str]:
    s = agent.settings
    cfg = sinks(s)
    lines = [f"Desktop: {getattr(s, 'notifications', 'important')}   Terminal signal: {getattr(s, 'terminal_notify', 'off')}",
             f"Web pages and phone: {'on' if cfg['enabled'] and cfg['web'] else 'off'} "
             f"({len(_listeners)} open now; browser alerts need the tab open, iOS web push needs an installed app)"]
    if not cfg["enabled"]:
        lines.append("All sinks here are off (/notify important turns them back on).")
    if not cfg["webhooks"]:
        lines.append("Webhooks: none. /notify webhook <url> adds one.")
    for h in cfg["webhooks"]:
        ok, notes = check_target(h["url"], bool(h.get("allowPrivate") or cfg["allowPrivate"]))
        what = ", ".join(h["events"]) if h["events"] else "every event"
        lines.append(f"Webhook {shown_url(h['url'])}  {'signed' if h['secret'] else 'unsigned'}  ({what})"
                     + ("" if ok else "  BLOCKED: " + notes[0]))
        for n in notes if ok else notes[1:]:
            lines.append("  note: " + n)
    for r in _results[-3:]:
        lines.append(f"Last delivery: {r['type']} to {r['url']}: " + ("ok" if r["ok"] else f"failed ({r['error']})")
                     + f" after {r['attempts']} attempt(s)")
    return lines


def handle_command(agent: Any, arg: str) -> bool:
    """The new ``/notify`` words (status, test, webhook, off, on). False when the old handler should take it."""
    from axon.commands.common import err, info, ok, usage
    words = arg.split()
    sub = words[0].lower() if words else ""
    if sub == "status":
        for ln in status_lines(agent):
            info(ln)
        return True
    if sub == "off":
        block = {**sinks(agent.settings), "enabled": False}
        _persist(agent, block, "off")
        ok("Notifications are off: desktop, web pages and webhooks. /notify important turns them back on.")
        return True
    if sub in ("important", "all"):
        block = sinks(agent.settings)
        if not block["enabled"]:
            _persist(agent, {**block, "enabled": True}, sub)
            ok(f"Notifications: {sub} (webhooks and web pages are on again)")
            return True
        return False
    if sub == "test":
        for line, good in run_test(agent, " ".join(words[1:])):
            (ok if good else err)(line)
        return True
    if sub == "webhook":
        arg2 = words[1:] if len(words) > 1 else []
        block = sinks(agent.settings)
        if not arg2:
            usage("/notify webhook <https-url> [--allow-private] | /notify webhook off")
            return True
        if arg2[0].lower() in ("off", "none", "clear", "remove"):
            _persist(agent, {**block, "webhooks": []})
            ok("Webhooks removed.")
            return True
        url = arg2[0]
        allow = "--allow-private" in arg2[1:]
        good, notes = check_target(url, allow)
        if not good:
            err(f"Not added: {notes[0]}.")
            return True
        secret = new_secret()
        stored = secret
        try:                                    # with an active vault the secret lives there; settings keep a reference
            from axon.features import vault
            if vault.active():
                name = "webhook:" + vault.origin_of(url).split("://", 1)[-1]
                vault.set_secret(name, secret, type="webhook_secret", bound=url)
                stored = "vault:" + name
        except Exception:
            stored = secret
        hooks = [h for h in block["webhooks"] if h["url"] != url] + [{"url": url, "secret": stored, "events": [],
                                                                      "allowPrivate": allow}]
        _persist(agent, {**block, "webhooks": hooks})
        ok(f"Webhook added: {shown_url(url)}")
        info(f"Signing secret (shown once, keep it): {secret}")
        for n in notes:
            info("note: " + n)
        info("Each POST has webhook-id, webhook-timestamp and webhook-signature (v1,HMAC-SHA256 of id.timestamp.body). /notify test sends one.")
        return True
    if sub == "on":
        block = {**sinks(agent.settings), "enabled": True}
        _persist(agent, block, "important")
        ok("Notifications are on (important).")
        return True
    return False


def run_test(agent: Any, message: str = "") -> list[tuple[str, bool]]:
    """Send a test event to every sink and say how each one went (one attempt per webhook)."""
    out: list[tuple[str, bool]] = []
    msg = message.strip() or "Axon notifications work."
    cfg = sinks(agent.settings)
    try:
        from axon.ui.notify import emit_terminal_notice, send_desktop_notification
        out.append(("Desktop: " + ("sent" if send_desktop_notification("Axon", msg) else "not available here"), True))
        if str(getattr(agent.settings, "terminal_notify", "off")) != "off":
            out.append(("Terminal signal: " + ("sent" if emit_terminal_notice("Axon", msg) else "no terminal to signal"), True))
    except Exception as e:  # noqa: BLE001
        out.append((f"Desktop: failed ({type(e).__name__})", False))
    event = make_event("test", msg, str(getattr(getattr(agent, "session", None), "active_session_id", "") or ""))
    if cfg["web"] and cfg["enabled"]:
        n = 0
        for fn in list(_listeners):
            try:
                fn(event)
                n += 1
            except Exception:
                pass
        out.append((f"Web pages: {n} open page(s) told", True))
    for h in cfg["webhooks"] if cfg["enabled"] else []:
        r = deliver(event, {**h, "allowPrivate": h.get("allowPrivate") or cfg["allowPrivate"]}, attempts=1)
        _record(r, event)
        out.append((f"Webhook {r['url']}: " + (f"ok (HTTP {r['status']})" if r["ok"] else f"failed ({r['error']})"), r["ok"]))
    if not cfg["webhooks"]:
        out.append(("Webhooks: none set", True))
    return out
