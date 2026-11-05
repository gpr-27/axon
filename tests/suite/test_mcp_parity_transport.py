"""WebSocket transport, headersHelper, OAuth and 401 handling for remote MCP servers."""
from __future__ import annotations

import json
import os
import shlex
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from axon.mcp.bridge import MCPServerConnection
from axon.mcp.client import MCPClient
from axon.mcp.config import ServerSpec
from axon.mcp.headers import DynamicHeaders, HeadersHelperError, run_headers_helper, scrubbed_env
from axon.mcp.oauth import OAuthError, OAuthManager, TokenStore, parse_www_authenticate
from axon.mcp.transports import StreamableHTTPTransport, TransportError
from axon.mcp.websocket import OP_PING, OP_TEXT, WebSocketTransport, accept_key, encode_frame

INIT = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "ws", "version": "1"}}
TOOLS = [{"name": "ping", "description": "p", "inputSchema": {"type": "object"}}]


# ─── WebSocket ──────────────────────────────────────────────────────────────

class WSServer:
    """A one-connection WebSocket MCP server on 127.0.0.1."""

    def __init__(self, need_header: tuple[str, str] | None = None) -> None:
        self.need_header = need_header
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.request_headers: dict[str, str] = {}
        self.got_ping_reply = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/socket"

    def _read(self, conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += conn.recv(4096)
        lines = raw.decode().split("\r\n")
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k:
                self.request_headers[k.strip().lower()] = v.strip()
        if self.need_header and self.request_headers.get(self.need_header[0].lower()) != self.need_header[1]:
            conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Bearer\r\nContent-Length: 0\r\n\r\n")
            conn.close()
            return
        key = self.request_headers["sec-websocket-key"]
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(key)}\r\nSec-WebSocket-Protocol: mcp\r\n\r\n").encode())
        conn.sendall(encode_frame(OP_PING, b"hi", mask=False))
        try:
            while True:
                b1, b2 = self._read(conn, 2)
                opcode, n = b1 & 0x0F, b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(conn, 2))[0]
                mask = self._read(conn, 4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(conn, n)))
                if opcode == 0xA:
                    self.got_ping_reply.set()
                elif opcode == 0x8:
                    return
                elif opcode == OP_TEXT:
                    msg = json.loads(payload)
                    if "id" not in msg:
                        continue
                    result = {"initialize": INIT, "tools/list": {"tools": TOOLS},
                              "tools/call": {"content": [{"type": "text", "text": "pong"}]}}.get(msg["method"], {})
                    reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                    # split into two fragments to exercise reassembly
                    half = len(reply) // 2
                    conn.sendall(bytes([OP_TEXT, half]) + reply[:half])
                    conn.sendall(bytes([0x80, len(reply) - half]) + reply[half:])
        except (EOFError, OSError):
            return
        finally:
            conn.close()


def test_websocket_transport_handshake_fragments_and_headers():
    srv = WSServer(need_header=("x-token", "abc"))
    t = WebSocketTransport(srv.url, {"X-Token": "abc"}, name="ws")
    client = MCPClient("ws", t, timeout=5)
    try:
        client.connect()
        assert client.server_info["name"] == "ws"
        assert [x["name"] for x in client.list_tools()] == ["ping"]
        assert client.call_tool("ping", {})["content"][0]["text"] == "pong"
        assert srv.got_ping_reply.wait(2)                   # pings are answered
        assert srv.request_headers["sec-websocket-protocol"] == "mcp"
    finally:
        client.close()


def test_websocket_rejection_carries_the_status():
    srv = WSServer(need_header=("x-token", "abc"))
    t = WebSocketTransport(srv.url, {"X-Token": "wrong"}, name="ws")
    with pytest.raises(TransportError) as ei:
        t.start(lambda m: None, lambda r: None)
    assert ei.value.status == 401


def test_websocket_connection_through_the_bridge_uses_header_provider():
    srv = WSServer(need_header=("x-dyn", "1"))
    spec = ServerSpec.from_dict("ws", {"type": "ws", "url": srv.url, "headersHelper": "echo"})
    conn = MCPServerConnection("ws", spec=spec, auto_reconnect=False)
    # a helper that prints a header the server demands
    helper = f"{shlex.quote(sys.executable)} -c \"import json;print(json.dumps({{'X-Dyn':'1'}}))\""
    conn.spec.headers_helper = helper
    try:
        assert conn.start(), conn.error
        assert conn.transport_kind == "ws"
    finally:
        conn.stop()


# ─── headersHelper ──────────────────────────────────────────────────────────

def _helper(tmp_path: Path, body: str) -> str:
    script = tmp_path / "helper.py"
    script.write_text(body, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


@pytest.mark.skipif(os.name == "nt", reason="shell quoting differs on Windows")
def test_headers_helper_runs_with_server_env_and_scrubs_project_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_REGISTRY_TOKEN", "supersecretvalue")
    monkeypatch.setenv("PLAIN", "visible")
    cmd = _helper(tmp_path, "import json,os\nprint(json.dumps({'X-Name': os.environ['CLAUDE_CODE_MCP_SERVER_NAME'],"
                  "'X-Url': os.environ['CLAUDE_CODE_MCP_SERVER_URL'],"
                  "'X-Tok': os.environ.get('MY_REGISTRY_TOKEN','gone'), 'X-Plain': os.environ.get('PLAIN','')}))")
    proj = ServerSpec.from_dict("s", {"type": "http", "url": "https://h/mcp?k=supersecretvalue", "headersHelper": cmd},
                                scope="project")
    out = run_headers_helper(proj, tmp_path)
    assert out["X-Name"] == "s" and out["X-Tok"] == "gone" and out["X-Plain"] == "visible"
    assert "supersecretvalue" not in out["X-Url"] and "REDACTED" in out["X-Url"]
    user = ServerSpec.from_dict("s", {"type": "http", "url": "https://h/mcp", "headersHelper": cmd}, scope="user")
    assert run_headers_helper(user, tmp_path)["X-Tok"] == "supersecretvalue"


@pytest.mark.skipif(os.name == "nt", reason="shell quoting differs on Windows")
def test_headers_helper_errors(tmp_path: Path):
    for body, needle in (("print('not json')", "JSON object"), ("import sys;sys.exit(3)", "code 3"),
                         ("import json;print(json.dumps({'a': 1}))", "string key-value")):
        spec = ServerSpec.from_dict("s", {"type": "http", "url": "https://h", "headersHelper": _helper(tmp_path, body)})
        with pytest.raises(HeadersHelperError, match=needle):
            run_headers_helper(spec, tmp_path)


def test_scrubbed_env_keeps_git_key_variables():
    env = scrubbed_env({"ANTHROPIC_API_KEY": "x", "MY_TOKEN": "y", "GIT_CONFIG_KEY_0": "k", "PATH": "/bin",
                        "ANTHROPIC_CUSTOM_HEADERS": "h", "Author": "a"})
    assert set(env) == {"GIT_CONFIG_KEY_0", "PATH"}


# ─── 401 handling ───────────────────────────────────────────────────────────

class AuthServer:
    def __init__(self, good: str) -> None:
        self.good = good
        self.seen: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {self.good}":
            return httpx.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://x/.well-known/oauth-protected-resource", '
                                    'scope="read write"'})
        if request.method != "POST":
            return httpx.Response(405)
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        res = {"initialize": INIT, "tools/list": {"tools": TOOLS}}.get(msg["method"], {})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": res})


def test_http_401_calls_on_unauthorized_and_retries_once():
    srv = AuthServer("fresh")
    token = {"v": "stale"}

    def provider() -> dict[str, str]:
        return {"Authorization": f"Bearer {token['v']}"}

    def refresh() -> bool:
        token["v"] = "fresh"
        return True

    http = httpx.Client(transport=httpx.MockTransport(srv))
    t = StreamableHTTPTransport("https://x/mcp", client=http, header_provider=provider, on_unauthorized=refresh)
    c = MCPClient("x", t, timeout=5)
    c.connect()
    assert srv.seen[:2] == ["Bearer stale", "Bearer fresh"]
    assert c.list_tools()[0]["name"] == "ping"


def test_parse_www_authenticate():
    p = parse_www_authenticate('Bearer error="insufficient_scope", scope="a b", resource_metadata="https://r/x"')
    assert p == {"error": "insufficient_scope", "scope": "a b", "resource_metadata": "https://r/x"}


def test_connection_needs_auth_then_connects_with_a_stored_token():
    srv = AuthServer("tok123")
    spec = ServerSpec.from_dict("web", {"type": "http", "url": "https://x/mcp"})
    http = httpx.Client(transport=httpx.MockTransport(srv))
    conn = MCPServerConnection("web", spec=spec, http_client=http, auto_reconnect=False)
    assert conn.start() is False
    assert conn.status == "needs-auth" and "authenticat" in (conn.error or "")
    assert conn.auth_challenge["scope"] == "read write"
    TokenStore().update(OAuthManager(spec).key, name="web", tokens={"access_token": "tok123", "token_type": "Bearer"})
    # (a transport closes the httpx client it was given, so the retry gets its own)
    conn2 = MCPServerConnection("web", spec=spec, http_client=httpx.Client(transport=httpx.MockTransport(srv)),
                                auto_reconnect=False)
    assert conn2.start() is True and conn2.status == "connected"
    conn2.stop()


def test_configured_authorization_rejected_is_a_failure_not_needs_auth():
    srv = AuthServer("good")
    spec = ServerSpec.from_dict("web", {"type": "http", "url": "https://x/mcp",
                                        "headers": {"Authorization": "Bearer bad"}})
    conn = MCPServerConnection("web", spec=spec, http_client=httpx.Client(transport=httpx.MockTransport(srv)),
                               auto_reconnect=False)
    assert conn.start() is False and conn.status == "failed"
    assert "Bearer bad" not in (conn.error or "")


def test_insufficient_scope_message():
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="admin"'})
    spec = ServerSpec.from_dict("web", {"type": "http", "url": "https://x/mcp"})
    conn = MCPServerConnection("web", spec=spec, http_client=httpx.Client(transport=httpx.MockTransport(server)),
                               auto_reconnect=False)
    conn.start()
    assert conn.status == "needs-auth" and "additional permissions" in conn.error and "admin" in conn.error


def test_dynamic_headers_prefers_configured_authorization(tmp_path: Path):
    class FakeOAuth:
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer oauth"}

    configured = ServerSpec.from_dict("s", {"type": "http", "url": "https://h", "headers": {"Authorization": "x"}})
    assert DynamicHeaders(configured, tmp_path, FakeOAuth())() == {}
    bare = ServerSpec.from_dict("s", {"type": "http", "url": "https://h"})
    assert DynamicHeaders(bare, tmp_path, FakeOAuth())() == {"Authorization": "Bearer oauth"}


# ─── OAuth ──────────────────────────────────────────────────────────────────

class OAuthWorld:
    """Fake MCP server + authorization server behind one httpx.MockTransport."""

    def __init__(self, *, dcr: bool = True, token_endpoint: str = "https://auth.example/token",
                 offline: bool = False) -> None:
        self.dcr, self.token_endpoint, self.offline = dcr, token_endpoint, offline
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.revoked = False
        self.issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} \
            if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded") else {}
        self.requests.append((request.method, url, form))
        if url.endswith("/.well-known/oauth-protected-resource") or "oauth-protected-resource/" in url:
            return httpx.Response(200, json={"resource": "https://mcp.example/mcp",
                                             "authorization_servers": ["https://auth.example"]})
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            meta = {"issuer": "https://auth.example", "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": self.token_endpoint, "revocation_endpoint": "https://auth.example/revoke",
                    "scopes_supported": ["read", "write"] + (["offline_access"] if self.offline else [])}
            if self.dcr:
                meta["registration_endpoint"] = "https://auth.example/register"
            return httpx.Response(200, json=meta)
        if url == "https://auth.example/register":
            body = json.loads(request.content)
            assert body["token_endpoint_auth_method"] == "none" and body["redirect_uris"][0].startswith("http://localhost:")
            return httpx.Response(201, json={"client_id": "dyn-client"})
        if url.endswith("/token"):
            self.issued += 1
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") != "r-1":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"acc-{self.issued}", "refresh_token": "r-1",
                                             "expires_in": 3600, "token_type": "Bearer"})
        if url.endswith("/revoke"):
            self.revoked = True
            return httpx.Response(200)
        return httpx.Response(404)


def _browser(captured: dict):
    """An 'opener' that plays the browser: follows the redirect back to the local callback server."""
    def open_url(url: str) -> bool:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        captured.update(q)
        captured["url"] = url

        def go() -> None:
            httpx.get(q["redirect_uri"], params={"code": "the-code", "state": captured.get("force_state", q["state"])})
        threading.Thread(target=go, daemon=True).start()
        return True
    return open_url


def _spec(**oauth) -> ServerSpec:
    raw = {"type": "http", "url": "https://mcp.example/mcp"}
    if oauth:
        raw["oauth"] = oauth
    return ServerSpec.from_dict("srv", raw)


def test_oauth_login_dcr_pkce_refresh_and_logout(tmp_path: Path):
    world = OAuthWorld(offline=True)
    store = TokenStore(tmp_path / "auth.json")
    mgr = OAuthManager(_spec(scopes="read"), store=store, http=httpx.Client(transport=httpx.MockTransport(world)))
    seen: dict = {}
    msg = mgr.login(opener=_browser(seen), timeout=10)
    assert "Signed in" in msg
    assert seen["code_challenge_method"] == "S256" and seen["client_id"] == "dyn-client"
    assert seen["scope"] == "read offline_access"               # pinned scopes + offline_access
    assert seen["resource"] == "https://mcp.example/mcp"
    assert seen["redirect_uri"].startswith("http://localhost:") and seen["redirect_uri"].endswith("/callback")
    token_call = next(f for m, u, f in world.requests if u.endswith("/token"))
    assert token_call["grant_type"] == "authorization_code" and len(token_call["code_verifier"]) >= 43
    assert mgr.status() == "authenticated" and mgr.auth_headers() == {"Authorization": "Bearer acc-1"}
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    # an expiring token is refreshed transparently
    rec = store.get(mgr.key)
    rec["tokens"]["expires_at"] = time.time() + 5
    store.update(mgr.key, tokens=rec["tokens"])
    assert mgr.auth_headers() == {"Authorization": "Bearer acc-2"}
    assert mgr.on_unauthorized() is True and mgr.auth_headers() == {"Authorization": "Bearer acc-3"}
    assert mgr.logout() is True and world.revoked
    assert mgr.status() == "not authenticated" and store.get(mgr.key) == {}


def test_oauth_fixed_callback_port_and_preconfigured_client(tmp_path: Path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    world = OAuthWorld(dcr=False)
    store = TokenStore(tmp_path / "auth.json")
    spec = _spec(clientId="my-client", callbackPort=port)
    from axon.mcp.oauth import store_client_secret
    store_client_secret(spec, "s3cret", store)
    mgr = OAuthManager(spec, store=store, http=httpx.Client(transport=httpx.MockTransport(world)))
    seen: dict = {}
    mgr.login(opener=_browser(seen), timeout=10)
    assert seen["redirect_uri"] == f"http://localhost:{port}/callback" and seen["client_id"] == "my-client"
    token_call = next(f for m, u, f in world.requests if u.endswith("/token"))
    assert token_call["client_secret"] == "s3cret" and token_call["client_id"] == "my-client"
    assert not any(u.endswith("/register") for _m, u, _f in world.requests)


def test_oauth_without_dcr_or_client_id_explains_what_to_do(tmp_path: Path):
    world = OAuthWorld(dcr=False)
    mgr = OAuthManager(_spec(), store=TokenStore(tmp_path / "a.json"), http=httpx.Client(transport=httpx.MockTransport(world)))
    with pytest.raises(OAuthError, match="dynamic client registration"):
        mgr.login(opener=lambda u: True, timeout=2)


def test_oauth_state_mismatch_and_insecure_token_endpoint(tmp_path: Path):
    mgr = OAuthManager(_spec(), store=TokenStore(tmp_path / "a.json"),
                       http=httpx.Client(transport=httpx.MockTransport(OAuthWorld())))
    with pytest.raises(OAuthError, match="state mismatch"):
        mgr.login(opener=_browser({"force_state": "evil"}), timeout=10)
    insecure = OAuthManager(_spec(), store=TokenStore(tmp_path / "b.json"),
                            http=httpx.Client(transport=httpx.MockTransport(
                                OAuthWorld(token_endpoint="http://auth.example/token"))))
    with pytest.raises(OAuthError, match="non-https token endpoint"):
        insecure.login(opener=_browser({}), timeout=10)


def test_oauth_manual_paste_when_no_browser(tmp_path: Path):
    world = OAuthWorld()
    mgr = OAuthManager(_spec(), store=TokenStore(tmp_path / "a.json"), http=httpx.Client(transport=httpx.MockTransport(world)))
    notes: list[str] = []
    holder: dict = {}

    def ask() -> str:
        # the user opens the URL elsewhere and pastes the address bar contents back
        deadline = time.time() + 5
        while "url" not in holder and time.time() < deadline:
            time.sleep(0.01)
        q = {k: v[0] for k, v in parse_qs(urlsplit(holder["url"]).query).items()}
        return f"{q['redirect_uri']}?code=pasted&state={q['state']}"

    def notify(text: str) -> None:
        notes.append(text)
        holder["url"] = text.split("\n  ", 1)[1]

    mgr.login(open_browser=False, ask_paste=ask, notify=notify, timeout=10)
    assert mgr.status() == "authenticated"
    assert next(f for m, u, f in world.requests if u.endswith("/token"))["code"] == "pasted"


def test_oauth_metadata_url_override_must_be_https(tmp_path: Path):
    mgr = OAuthManager(_spec(authServerMetadataUrl="http://auth/x"), store=TokenStore(tmp_path / "a.json"))
    with pytest.raises(OAuthError, match="https"):
        mgr.discover()


def test_oauth_gateway_headers_go_to_the_servers_origin_only(tmp_path: Path):
    world = OAuthWorld()
    spec = ServerSpec.from_dict("srv", {"type": "http", "url": "https://mcp.example/mcp",
                                        "headers": {"X-Api-Key": "gw", "Authorization": "ignored"}})
    mgr = OAuthManager(spec, store=TokenStore(tmp_path / "a.json"), http=httpx.Client(transport=httpx.MockTransport(world)))
    assert mgr._own_headers("https://mcp.example/.well-known/x") == {"X-Api-Key": "gw"}
    assert mgr._own_headers("https://auth.example/token") == {}
