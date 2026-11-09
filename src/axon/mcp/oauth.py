"""
OAuth 2.0 for remote MCP servers (HTTP and SSE): authorization code flow with PKCE.

* Discovery: RFC 9728 protected-resource metadata (``WWW-Authenticate: ... resource_metadata=``, then
  ``/.well-known/oauth-protected-resource``), then RFC 8414 authorization-server metadata, with a
  ``oauth.authServerMetadataUrl`` override (https only).
* Client: pre-registered ``oauth.clientId`` (+ a client secret stored once, at add time) or Dynamic Client
  Registration. ``oauth.callbackPort`` fixes the ``http://localhost:PORT/callback`` redirect URI.
* Scopes: ``oauth.scopes`` (space separated) pins them; otherwise only what the server's challenge asks for.
* Tokens (and registered clients) live in ``~/.axon/mcp-auth.json`` (mode 0600), per server endpoint, are
  refreshed automatically and deleted by ``logout`` / ``remove``.

Credentials are only ever sent to an https token endpoint (or localhost).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from axon import notices
from axon.mcp.config import ServerSpec, normalize_url
from axon.mcp.callback_page import failure_page, success_page

LOGIN_TIMEOUT = 300.0
EXPIRY_SKEW = 60.0
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class OAuthError(Exception):
    """Sign-in failed; the message is meant for the user. ``retryable`` marks a network or server (5xx)
    failure: the stored tokens may still be good, so they must not be thrown away."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ─── storage ────────────────────────────────────────────────────────────────

def auth_store_path() -> Path:
    from axon.storage import user_file
    return user_file("mcp-auth.json")


class TokenStore:
    """JSON file of {endpoint: {name, client, tokens}} readable only by you."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or auth_store_path()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, key: str) -> dict[str, Any]:
        with self._lock:
            rec = self._read().get(key)
            return dict(rec) if isinstance(rec, dict) else {}

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            data = self._read()
            rec = data.get(key) if isinstance(data.get(key), dict) else {}
            rec.update({k: v for k, v in fields.items() if v is not None})
            for k in [k for k, v in fields.items() if v is None]:
                rec.pop(k, None)
            data[key] = rec
            self._write(data)

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True


def endpoint_key(spec: ServerSpec) -> str:
    return normalize_url(spec.url)


# ─── helpers ────────────────────────────────────────────────────────────────

def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """Parameters of a ``Bearer`` challenge: resource_metadata, scope, error, error_description."""
    out: dict[str, str] = {}
    for m in re.finditer(r'([A-Za-z_]+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', header or ""):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _is_secure_endpoint(url: str) -> bool:
    u = urlsplit(url)
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in _LOCAL_HOSTS)



class _CallbackServer:
    """One-shot localhost server that captures the browser redirect."""

    def __init__(self, port: int, server_name: str = "") -> None:
        self.result: dict[str, str] = {}
        self.done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                outer.result = query
                ok = "code" in query and "error" not in query
                if ok:
                    body = success_page(server_name)
                else:
                    body = failure_page(query.get("error", "no authorization code"),
                                        query.get("error_description", ""), server_name)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                outer.done.set()

            def log_message(self, *_a: Any) -> None:  # silence
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            raise OAuthError(f"cannot listen on callback port {port}: {e}") from e
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="mcp-oauth-callback", daemon=True).start()

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass


# ─── the manager ────────────────────────────────────────────────────────────

class OAuthManager:
    """Everything OAuth for one remote server."""

    def __init__(self, spec: ServerSpec, *, store: TokenStore | None = None,
                 http: httpx.Client | None = None) -> None:
        self.spec = spec
        self.store = store or TokenStore()
        self._http = http
        self._lock = threading.RLock()
        self.key = endpoint_key(spec)
        self.challenge: dict[str, str] = {}      # last WWW-Authenticate parameters seen
        self.helper_headers: dict[str, str] = {} # what the server's headersHelper last returned

    # ── state ───────────────────────────────────────────────────────────
    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True)

    def _own_headers(self, url: str) -> dict[str, str]:
        """Configured headers (except Authorization) go to the server's own origin (gateways)."""
        if _origin(url) != _origin(self.spec.url):
            return {}
        merged = {**self.spec.headers, **self.helper_headers}
        return {k: v for k, v in merged.items() if k.lower() != "authorization"}

    def tokens(self) -> dict[str, Any]:
        t = self.store.get(self.key).get("tokens")
        return dict(t) if isinstance(t, dict) else {}

    def status(self) -> str:
        t = self.tokens()
        if not t.get("access_token"):
            return "not authenticated"
        if t.get("expires_at") and t["expires_at"] < time.time() and not t.get("refresh_token"):
            return "expired"
        return "authenticated"

    def is_configured(self) -> bool:
        return bool(self.spec.oauth)

    # ── request headers ─────────────────────────────────────────────────
    def auth_headers(self) -> dict[str, str]:
        """``Authorization: Bearer ...`` when signed in (refreshing an expiring token first)."""
        with self._lock:
            t = self.tokens()
            if not t.get("access_token"):
                return {}
            if t.get("expires_at") and t["expires_at"] - EXPIRY_SKEW < time.time() and t.get("refresh_token"):
                self.refresh()
                t = self.tokens()
            token = t.get("access_token")
            return {"Authorization": f"{t.get('token_type') or 'Bearer'} {token}"} if token else {}

    def on_unauthorized(self) -> bool:
        """401 from the server: refresh the token once. A login with no refresh token connects once without
        the token (the rejected token is deleted): if the server answers, its tools work signed out.
        True = worth retrying the request."""
        with self._lock:
            t = self.tokens()
            if not t.get("access_token"):
                return False
            if not t.get("refresh_token"):
                self.forget_tokens()
                return True
            return self.refresh()

    # ── discovery ───────────────────────────────────────────────────────
    def _get_json(self, url: str) -> dict[str, Any] | None:
        try:
            r = self._client().get(url, headers={"Accept": "application/json", **self._own_headers(url)})
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def discover(self) -> dict[str, Any]:
        override = str(self.spec.oauth.get("authServerMetadataUrl") or "")
        if override:
            if not override.startswith("https://"):
                raise OAuthError("oauth.authServerMetadataUrl must use https://")
            meta = self._get_json(override)
            if not meta:
                raise OAuthError(f"could not read authorization server metadata from {override}")
            meta["_scopes_from_metadata"] = True
            return meta
        issuer = _origin(self.spec.url)
        resource_meta = None
        rm_url = self.challenge.get("resource_metadata")
        candidates = [rm_url] if rm_url else []
        u = urlsplit(self.spec.url)
        base = f"{u.scheme}://{u.netloc}"
        if u.path not in ("", "/"):
            candidates.append(f"{base}/.well-known/oauth-protected-resource{u.path}")
        candidates.append(f"{base}/.well-known/oauth-protected-resource")
        for c in candidates:
            if c:
                resource_meta = self._get_json(c)
                if resource_meta:
                    break
        if resource_meta and isinstance(resource_meta.get("authorization_servers"), list) \
                and resource_meta["authorization_servers"]:
            issuer = str(resource_meta["authorization_servers"][0])
        iu = urlsplit(issuer)
        ibase = f"{iu.scheme}://{iu.netloc}"
        ipath = iu.path.rstrip("/")
        meta = None
        for url in ([f"{ibase}/.well-known/oauth-authorization-server{ipath}"] if ipath else []) + [
                f"{ibase}/.well-known/oauth-authorization-server",
                f"{ibase}/.well-known/openid-configuration"]:
            meta = self._get_json(url)
            if meta:
                break
        if not meta:  # servers without metadata use the conventional endpoints
            meta = {"authorization_endpoint": f"{ibase}/authorize", "token_endpoint": f"{ibase}/token",
                    "registration_endpoint": f"{ibase}/register", "_fallback": True}
        meta["_resource"] = (resource_meta or {}).get("resource") or self.spec.url
        return meta

    # ── client registration ─────────────────────────────────────────────
    def _client_for(self, meta: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
        rec = self.store.get(self.key)
        stored = rec.get("client") if isinstance(rec.get("client"), dict) else {}
        configured = str(self.spec.oauth.get("clientId") or "")
        if configured:
            client = {"client_id": configured}
            if stored.get("client_id") == configured and stored.get("client_secret"):
                client["client_secret"] = stored["client_secret"]
            elif stored.get("client_secret") and stored.get("preconfigured"):
                client["client_secret"] = stored["client_secret"]
            return client
        if stored.get("client_id") and redirect_uri in (stored.get("redirect_uris") or []):
            return stored
        endpoint = meta.get("registration_endpoint")
        if not endpoint:
            raise OAuthError("Incompatible auth server: does not support dynamic client registration. "
                             "Register an OAuth app with the server and pass its client ID "
                             "(axon mcp add --client-id ... --client-secret ...).")
        body = {"client_name": "Axon", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        try:
            r = self._client().post(endpoint, json=body, headers=self._own_headers(endpoint))
        except httpx.HTTPError as e:
            raise OAuthError(f"client registration failed: {e}") from e
        if r.status_code >= 400:
            raise OAuthError(f"client registration failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            reg = r.json()
        except ValueError as e:
            raise OAuthError("client registration returned invalid JSON") from e
        if not reg.get("client_id"):
            raise OAuthError("client registration returned no client_id")
        client = {"client_id": reg["client_id"], "client_secret": reg.get("client_secret"),
                  "redirect_uris": [redirect_uri]}
        self.store.update(self.key, name=self.spec.name, client={k: v for k, v in client.items() if v})
        return client

    # ── token endpoint ──────────────────────────────────────────────────
    def _token_request(self, meta: dict[str, Any], form: dict[str, str], client: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(meta.get("token_endpoint") or "")
        if not endpoint or not _is_secure_endpoint(endpoint):
            raise OAuthError("Refusing to send credentials to non-https token endpoint "
                             f"({endpoint or 'none advertised'})")
        form = dict(form)
        form["client_id"] = client["client_id"]
        headers = {"Accept": "application/json", **self._own_headers(endpoint)}
        auth = None
        if client.get("client_secret"):
            methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_post"]
            if "client_secret_post" in methods or "client_secret_basic" not in methods:
                form["client_secret"] = client["client_secret"]
            else:
                auth = httpx.BasicAuth(client["client_id"], client["client_secret"])
        try:
            r = self._client().post(endpoint, data=form, headers=headers, auth=auth)
        except httpx.HTTPError as e:
            raise OAuthError(f"token request failed: {e}", retryable=True) from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if r.status_code >= 400 or "access_token" not in data:
            err = data.get("error_description") or data.get("error") or r.text[:200]
            raise OAuthError(f"token request failed: HTTP {r.status_code} {err}", retryable=r.status_code >= 500)
        return data

    def _store_tokens(self, data: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
        tokens = {"access_token": data["access_token"], "token_type": data.get("token_type") or "Bearer",
                  "scope": data.get("scope"),
                  "refresh_token": data.get("refresh_token") or (previous or {}).get("refresh_token")}
        if data.get("expires_in"):
            try:
                tokens["expires_at"] = time.time() + float(data["expires_in"])
            except (TypeError, ValueError):
                pass
        self.store.update(self.key, name=self.spec.name, tokens={k: v for k, v in tokens.items() if v})

    def refresh(self) -> bool:
        with self._lock:
            t = self.tokens()
            refresh_token = t.get("refresh_token")
            rec = self.store.get(self.key)
            client = rec.get("client") if isinstance(rec.get("client"), dict) else {}
            configured = str(self.spec.oauth.get("clientId") or "")
            if configured and not client.get("client_id"):
                client = {"client_id": configured}
            if not refresh_token or not client.get("client_id"):
                return False
            try:
                meta = self.discover()
                data = self._token_request(meta, {"grant_type": "refresh_token", "refresh_token": refresh_token},
                                           client)
            except OAuthError as e:
                if not e.retryable:  # a rejected refresh token is dead; an offline laptop is not
                    self.store.update(self.key, tokens=None)
                    notices.post(f'MCP server "{self.spec.name}" rejected the stored sign-in. '
                                 "Open /mcp and select Sign in to sign in again.")
                return False
            self._store_tokens(data, t)
            return True

    # ── interactive sign-in ─────────────────────────────────────────────
    def build_authorization_url(self, meta: dict[str, Any], client: dict[str, Any], redirect_uri: str,
                                state: str, challenge: str) -> str:
        endpoint = str(meta.get("authorization_endpoint") or "")
        if not endpoint:
            raise OAuthError("the authorization server advertises no authorization_endpoint")
        if not _is_secure_endpoint(endpoint):  # the URL is handed to the browser / OS URL handler
            raise OAuthError(f"Refusing to open a non-https authorization endpoint ({endpoint[:80]})")
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        pinned = str(self.spec.oauth.get("scopes") or "").strip()
        scopes = pinned or self.challenge.get("scope", "")
        if not scopes and meta.get("_scopes_from_metadata") and meta.get("scopes_supported"):
            scopes = " ".join(str(s) for s in meta["scopes_supported"])
        supported = [str(s) for s in (meta.get("scopes_supported") or [])]
        if scopes and "offline_access" in supported and "offline_access" not in scopes.split():
            scopes += " offline_access"
        if scopes:
            params["scope"] = scopes
        if meta.get("_resource"):
            params["resource"] = str(meta["_resource"])
        u = urlsplit(endpoint)
        query = (u.query + "&" if u.query else "") + urlencode(params)
        return urlunsplit((u.scheme, u.netloc, u.path, query, ""))

    def login(self, *, open_browser: bool = True, ask_paste: Callable[[], str] | None = None,
              opener: Callable[[str], Any] | None = None, notify: Callable[[str], None] | None = None,
              timeout: float = LOGIN_TIMEOUT) -> str:
        """Run the browser flow. Returns a short success message; raises OAuthError."""
        say = notify or (lambda _m: None)
        if not self.spec.remote or self.spec.type == "ws":
            raise OAuthError("OAuth works with HTTP and SSE servers only")
        try:
            meta = self.discover()
        except httpx.HTTPError as e:
            raise OAuthError(f"discovery failed: {e}") from e
        port = int(self.spec.oauth.get("callbackPort") or os.environ.get("MCP_OAUTH_CALLBACK_PORT") or 0)
        cb = _CallbackServer(port, getattr(self.spec, "name", "") or "")
        try:
            redirect_uri = f"http://localhost:{cb.port}/callback"
            client = self._client_for(meta, redirect_uri)
            verifier, challenge = _pkce()
            state = secrets.token_urlsafe(24)
            url = self.build_authorization_url(meta, client, redirect_uri, state, challenge)
            opened = False
            if open_browser:
                try:
                    opened = bool((opener or webbrowser.open)(url))
                except Exception:
                    opened = False
            if not opened:
                say(f"Open this URL in your browser to sign in to '{self.spec.name}':\n  {url}")
            result = self._await_redirect(cb, ask_paste if not opened else None, timeout)
        finally:
            cb.close()
        if result.get("error"):
            raise OAuthError(f"authorization failed: {result.get('error_description') or result['error']}")
        if result.get("state") != state:
            raise OAuthError("authorization failed: state mismatch")
        issuer = str(meta.get("issuer") or "").rstrip("/")
        if result.get("iss") and issuer and result["iss"].rstrip("/") != issuer:
            raise OAuthError("authorization failed: the response names an unexpected issuer "
                             f"({result['iss'][:80]})")
        code = result.get("code")
        if not code:
            raise OAuthError("authorization failed: no code returned")
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier}
        if meta.get("_resource"):
            form["resource"] = str(meta["_resource"])
        data = self._token_request(meta, form, client)
        self._store_tokens(data)
        return f"Signed in to '{self.spec.name}'."

    @staticmethod
    def _await_redirect(cb: _CallbackServer, ask_paste: Callable[[], str] | None, timeout: float) -> dict[str, str]:
        pasted: dict[str, str] = {}
        if ask_paste is not None:
            def _paste() -> None:
                try:
                    text = (ask_paste() or "").strip()
                except Exception:
                    return
                if text:
                    q = parse_qs(urlsplit(text).query) if "?" in text else parse_qs(text)
                    pasted.update({k: v[0] for k, v in q.items()})
                    cb.done.set()
            threading.Thread(target=_paste, name="mcp-oauth-paste", daemon=True).start()
        if not cb.done.wait(timeout):
            raise OAuthError("sign-in timed out waiting for the browser redirect")
        return cb.result or pasted

    # ── sign out ────────────────────────────────────────────────────────
    def logout(self) -> bool:
        """Forget tokens and the registered client; revoke the token when the server supports it. A client
        secret you set when adding the server stays: it can only be given at add time."""
        rec = self.store.get(self.key)
        t = rec.get("tokens") if isinstance(rec.get("tokens"), dict) else {}
        if t.get("access_token"):
            try:
                meta = self.discover()
                endpoint = meta.get("revocation_endpoint")
                client = rec.get("client") if isinstance(rec.get("client"), dict) else {}
                if endpoint and _is_secure_endpoint(str(endpoint)) and client.get("client_id"):
                    token = t.get("refresh_token") or t["access_token"]
                    self._client().post(str(endpoint), data={"token": token, "client_id": client["client_id"]},
                                        headers=self._own_headers(str(endpoint)))
            except (OAuthError, httpx.HTTPError):
                pass
        client = rec.get("client") if isinstance(rec.get("client"), dict) else {}
        if client.get("preconfigured"):
            self.store.update(self.key, tokens=None)
            return bool(t)
        return self.store.delete(self.key)

    def forget_tokens(self) -> None:
        self.store.update(self.key, tokens=None)


# ─── conveniences for commands ──────────────────────────────────────────────

def store_client_secret(spec: ServerSpec, secret: str, store: TokenStore | None = None) -> None:
    (store or TokenStore()).update(endpoint_key(spec), name=spec.name, client={
        "client_id": str(spec.oauth.get("clientId") or ""), "client_secret": secret, "preconfigured": True})


def forget(spec: ServerSpec, store: TokenStore | None = None) -> bool:
    """Delete everything stored for this server's endpoint (used by remove / logout)."""
    return (store or TokenStore()).delete(endpoint_key(spec))
