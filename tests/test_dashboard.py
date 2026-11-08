"""`axon dash`: security (loopback, token/cookie, Host, Origin, CSP, escaping, secrets, traversal), data
endpoints, pagination of huge messages, sharing/gist confirmation, replies (SSE, approvals, live queue)."""
from __future__ import annotations

import http.client
import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dashboard as dash  # noqa: E402
from test_dashboard_sample import BEARER, SECRET_KEY, XSS_TOOL, XSS_USER, build_sample  # noqa: E402

API_KEY = "sk-test-dashboard-key-0123456789abcdef"


class Client:
    def __init__(self, srv: dash.DashServer) -> None:
        self.srv = srv
        self.cookie: str | None = None

    def raw(self, method: str, path: str, body: Any = None, *, host: str | None = None, origin: str | None = "same",
            cookie: bool = True, headers: dict[str, str] | None = None, ctype: str = "application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{self.srv.port}")
        if cookie and self.cookie:
            conn.putheader("Cookie", f"{dash.COOKIE_NAME}={self.cookie}")
        if origin == "same":
            origin = f"http://127.0.0.1:{self.srv.port}"
        if origin and method == "POST":
            conn.putheader("Origin", origin)
        data = None
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
            conn.putheader("Content-Type", ctype)
            conn.putheader("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders(data)
        return conn, conn.getresponse()

    def req(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, dict[str, str], bytes]:
        conn, r = self.raw(method, path, body, **kw)
        try:
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> tuple[int, dict[str, str], bytes]:
        return self.req("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> tuple[int, dict[str, str], bytes]:
        return self.req("POST", path, body if body is not None else {}, **kw)

    def json(self, path: str, **kw: Any) -> Any:
        st, _, b = self.get(path, **kw)
        assert st == 200, (st, b[:300])
        return json.loads(b)

    def login(self) -> None:
        st, h, _ = self.get(f"/?t={self.srv.token}", cookie=False)
        assert st == 302
        self.cookie = re.search(rf"{dash.COOKIE_NAME}=([^;]+)", h["set-cookie"]).group(1)  # type: ignore[union-attr]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.setenv("AXON_HOME", str(home))
    monkeypatch.setenv("AXON_API_KEY", API_KEY)
    ws = tmp_path / "proj"
    ids = build_sample(home, ws, tmp_path / "other")
    return SimpleNamespace(home=home, ws=ws, ids=ids, tmp=tmp_path)


@pytest.fixture
def server(env):
    srv = dash.make_server(0, env.ws)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown_runs()
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def c(server) -> Client:
    cl = Client(server)
    cl.login()
    return cl


# ─── loopback only ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "::", "localhost", ""])
def test_refuses_non_loopback_bind(env, host):
    with pytest.raises(ValueError):
        dash.make_server(0, env.ws, host=host)


def test_binds_loopback_and_cli_has_no_remote_option(server):
    assert server.server_address[0] == "127.0.0.1"
    import argparse
    seen: list[str] = []
    orig = argparse.ArgumentParser.add_argument

    def spy(self, *a, **k):
        seen.extend(a)
        return orig(self, *a, **k)
    argparse.ArgumentParser.add_argument = spy  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            dash.dash_main(["--help"])
    finally:
        argparse.ArgumentParser.add_argument = orig  # type: ignore[method-assign]
    assert {"--port", "--no-open", "--workspace"} <= set(seen)
    assert not {"--host", "--allow-remote", "--bind"} & set(seen)


# ─── token -> cookie ─────────────────────────────────────────────────────────

def test_token_exchange_cookie_and_redirect(server):
    cl = Client(server)
    assert cl.get("/", cookie=False)[0] == 401
    assert cl.get("/?t=wrong", cookie=False)[0] == 403
    st, h, _ = cl.get(f"/?t={server.token}", cookie=False)
    assert st == 302 and h["location"] == "/" and "t=" not in h["location"]
    sc = h["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=Strict" in sc and server.token not in sc
    cl.cookie = re.search(rf"{dash.COOKIE_NAME}=([^;]+)", sc).group(1)  # type: ignore[union-attr]
    st, h, body = cl.get("/")
    assert st == 200 and b"/static/app.js" in body
    assert cl.get("/api/info", cookie=False)[0] == 401
    assert cl.get("/api/info")[0] == 200
    assert cl.get("/api/info", headers={"Cookie": f"{dash.COOKIE_NAME}=forged"}, cookie=False)[0] == 401


def test_open_url_carries_token_and_is_loopback(server):
    assert server.open_url == f"http://127.0.0.1:{server.port}/?t={server.token}"
    assert len(server.token) >= 24 and server.token != server.cookie


# ─── Host / Origin ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:80", "127.0.0.1.evil.com", "10.0.0.5",
                                  "localhost:1", "127.0.0.1:1", ""])
def test_bad_host_rejected_even_with_cookie(c, host):
    st, _, body = c.get("/api/info", host=host)
    assert st == 403, body
    assert c.get("/", host=host)[0] == 403
    assert c.get(f"/?t={c.srv.token}", host=host, cookie=False)[0] == 403


def test_localhost_names_allowed(c):
    for h in (f"localhost:{c.srv.port}", f"127.0.0.1:{c.srv.port}", f"[::1]:{c.srv.port}"):
        assert c.get("/api/info", host=h)[0] == 200, h


def test_post_requires_same_origin(c, env):
    body = {"kind": "overview"}
    assert c.post("/api/share/preview", body)[0] == 200
    assert c.post("/api/share/preview", body, origin=None)[0] == 403
    assert c.post("/api/share/preview", body, origin="http://evil.example.com")[0] == 403
    assert c.post("/api/share/preview", body, origin=f"http://127.0.0.1:{c.srv.port + 1}")[0] == 403
    assert c.post("/api/share/preview", body, origin="null")[0] == 403
    assert c.post("/api/share/preview", body, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert c.post("/api/share/preview", body, cookie=False)[0] == 401
    assert c.post("/api/share/preview", body, ctype="text/plain")[0] == 415
    assert c.post("/api/share/preview", b"not json")[0] == 400
    assert c.req("OPTIONS", "/api/share/preview")[0] == 405
    for path in (f"/api/session/{env.ids['short']}/reply", "/api/approval", "/api/cancel", "/api/share/gist"):
        assert c.post(path, {"text": "x"}, origin="http://evil.example.com")[0] == 403, path


# ─── CSP and static assets ───────────────────────────────────────────────────

def test_strict_csp_and_headers(c):
    for path in ("/", "/api/info", "/static/app.js", "/static/app.css"):
        st, h, _ = c.get(path)
        assert st == 200, path
        csp = h["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp
        assert "unsafe-eval" not in csp
        assert not re.search(r"script-src[^;]*unsafe-inline", csp)
        assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "object-src 'none'" in csp
        assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
        assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"


def test_page_has_no_inline_script_and_no_remote_loads(c):
    page = c.get("/")[2].decode()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)
    for asset in ("/", "/static/app.js", "/static/app.css"):
        text = c.get(asset)[2].decode()
        assert not re.search(r"""(src|href)=["']https?://""", text)
        assert "@import" not in text and "url(http" not in text
        assert "https://" not in text or asset == "/", asset
    assert "eval(" not in c.get("/static/app.js")[2].decode()


# ─── escaping ────────────────────────────────────────────────────────────────

def _all_html(c: Client, sid: str) -> str:
    page = c.json(f"/api/session/{sid}?limit=200")
    return "".join(e["html"] for e in page["entries"])


def test_transcript_text_is_escaped(c, env):
    html = _all_html(c, env.ids["xss"])
    assert "<script" not in html.lower().replace("&lt;script", "")
    assert "&lt;script&gt;alert(&#x27;xss-user&#x27;)&lt;/script&gt;" in html
    assert "&lt;/pre&gt;&lt;script&gt;alert(&#x27;xss-tool&#x27;)" in html
    assert not re.search(r"<img\b", html) and "&lt;img" in html
    assert not re.search(r"<img[^>]*onerror", html)
    assert 'href="javascript:' not in html
    assert 'href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer"' in html


def test_markdown_escapes_and_formats():
    out = dash.render_markdown("# T <b>x</b>\n\n**bold** `a<b` [x](javascript:alert(1)) [y](https://e.com)\n\n```py\n<script>1</script>\n```\n\n- a\n- b\n\n1. one\n\n| h1 | h2 |\n|---|---|\n| <i> | 2 |\n")
    assert "<script" not in out and "<b>" not in out and "<i>" not in out
    assert "<strong>bold</strong>" in out and "<code>a&lt;b</code>" in out
    assert 'href="javascript' not in out and "<a href=\"javascript" not in out
    assert 'href="https://e.com"' in out and "<table>" in out and "<ol>" in out and "<ul>" in out
    assert '<pre class="code"><code class="lang-py">&lt;script&gt;1&lt;/script&gt;' in out
    assert 'class="x" onclick' not in dash.render_markdown('```" onclick="x\nhi\n```')
    assert "\x00" not in dash.render_markdown("a\x00b `c\x001`")


def test_diff_rendering_classes():
    html = dash.render_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old <b>\n+new")
    assert 'class="del">-old &lt;b&gt;' in html and 'class="add">+new' in html and 'class="hunk"' in html


def test_extra_redaction_patterns():
    text = f"Authorization: Basic abc123def456\nX-API-Key: {SECRET_KEY}\nFOO_PASSWORD=hunter2hunter2\nA=b\nDB_URL=postgres://u:pw12345@h/db\nSECRET_TOKEN=abcdefghijkl\n"
    out = dash.redact_text(text)
    for leak in ("abc123def456", SECRET_KEY, "hunter2hunter2", "pw12345", "abcdefghijkl"):
        assert leak not in out
    env_block = "NODE_ENV=production\nSTRIPE_LIVE=zzzzzzzzzzzz\nDEBUG=true\n"
    out = dash.redact_text(env_block)
    assert "zzzzzzzzzzzz" not in out and "DEBUG=true" in out
    doc = dash.redact_html_text("<p>KEY_TOKEN=abcdefghijk</p><p class=\"x\">ok</p>")
    assert doc.endswith('<p class="x">ok</p>') and "abcdefghijk" not in doc


# ─── secrets never appear ────────────────────────────────────────────────────

def test_no_secret_in_any_response(c, env):
    (env.ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"gh": {
        "command": "npx", "args": ["server"], "env": {"GITHUB_TOKEN": "ghp_" + "A" * 36},
        "headers": {"Authorization": "Bearer zzzzzzzzzzzzzzzzzzzzzzzz"}}}}), encoding="utf-8")
    secrets_ = [API_KEY, SECRET_KEY, BEARER.split()[1], "ghp_" + "A" * 36, "zzzzzzzzzzzzzzzzzzzzzzzz", "sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    paths = ["/api/info", "/api/overview?period=all&scope=all", "/api/sessions?scope=all", "/api/extensions",
             "/", "/static/app.js", "/static/app.css"]
    for sid in env.ids.values():
        paths += [f"/api/session/{sid}?limit=200", f"/api/session/{sid}/text?entry=1&field=text"]
    paths += [f"/api/session/{env.ids['xss']}/text?entry=1&field=t0.out"]
    for p in paths:
        st, _, body = c.get(p)
        assert st in (200, 404), (p, st)
        text = body.decode("utf-8", "replace")
        for s in secrets_:
            assert s not in text, (p, s)
    ext = c.json("/api/extensions")
    assert ext["settings"]["api_key"].startswith("••••")
    gh = next(m for m in ext["mcp"] if m["name"] == "gh")
    assert "env" not in gh and "headers" not in gh
    for kind, extra in (("session", {"id": env.ids["xss"]}), ("overview", {})):
        st, _, body = c.post("/api/share/preview", {"kind": kind, **extra})
        assert st == 200
        for s in secrets_:
            assert s not in body.decode()


# ─── traversal ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sid", ["..", "%2e%2e", "..%2f..%2fetc%2fpasswd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", "a%00b",
                                 "session_x/../../etc/passwd", "....//....//x", "%252e%252e%252f", "nope",
                                 "x" * 300, "..%5c..%5cwindows", "/etc/passwd"])
def test_session_id_traversal_fails(c, sid):
    for suffix in ("", "/text?entry=0&field=text"):
        st, _, body = c.get(f"/api/session/{sid}{suffix}")
        assert st in (400, 404), (sid, st)
        assert b"root:" not in body
    st, _, _ = c.post(f"/api/session/{sid}/reply", {"text": "hi"})
    assert st in (400, 404)
    assert c.post("/api/share/preview", {"kind": "session", "id": sid.replace("%2f", "/")})[0] in (400, 404)


def test_static_traversal_fails(c):
    for p in ("/static/../../../etc/passwd", "/static/..%2f..%2fetc%2fpasswd", "/static/app.js/../../x", "/static/dashboard.py"):
        assert c.get(p)[0] in (404, 400), p


# ─── data endpoints ──────────────────────────────────────────────────────────

def test_overview_payload(c, env):
    d = c.json("/api/overview?period=30d&scope=project")
    k = d["kpis"]
    assert not d["empty"] and k["cost"] > 0 and k["calls"] > 0 and 0 < k["hit_pct"] <= 100 and k["savings"] > 0
    assert k["has_latency"] and k["api_s"] > 0 and "wall_s" in k
    assert d["charts"]["cost"].startswith("<svg") and d["charts"]["tokens"].startswith("<svg") and d["charts"]["donut"].startswith("<svg")
    row = d["by_model"][0]
    assert {"input", "output", "cache_read", "cache_write", "cost", "calls"} <= set(row)
    assert d["by_source"] and d["tools"] and d["turns"]
    allp = c.json("/api/overview?period=all&scope=all")
    assert allp["scope"] == "all"
    assert c.json("/api/overview?period=bogus")["period"] == "30d"


def test_overview_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "h"))
    ws = tmp_path / "w"
    ws.mkdir()
    srv = dash.make_server(0, ws)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        cl = Client(srv)
        cl.login()
        d = cl.json("/api/overview?period=7d")
        assert d["empty"] is True and "charts" not in d
        assert cl.json("/api/sessions")["sessions"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_sessions_scopes(c, env):
    mine = c.json("/api/sessions?scope=project")["sessions"]
    allp = c.json("/api/sessions?scope=all")["sessions"]
    assert env.ids["refactor"] in {s["id"] for s in mine}
    assert "session_20261105_120000_other_eeee05" not in {s["id"] for s in mine}
    assert "session_20261105_120000_other_eeee05" in {s["id"] for s in allp}
    top = next(s for s in mine if s["id"] == env.ids["refactor"])
    assert top["starred"] and top["tags"] == ["auth"] and top["title"] == "Refactor the auth module" and top["cost"] > 0
    assert {"model", "messages", "updated", "project", "live"} <= set(top)


def test_transcript_entries_tools_diff_denied_and_costs(c, env):
    page = c.json(f"/api/session/{env.ids['refactor']}?limit=50")
    html = "".join(e["html"] for e in page["entries"])
    assert page["total"] == len(page["entries"]) and not page["has_before"]
    assert 'class="diff"' in html and 'class="add">+    if not user:' in html
    assert re.search(r'<details class="tool[^"]* denied"', html) and "needs approval in non-interactive mode" in html
    assert "$0.0123" in html and "in 12.0k" in html
    assert '<details class="tool"' in html  # collapsed by default (no open attribute)
    assert "<details class=\"tool\" open" not in html


def test_transcript_pagination_before_after(c, env):
    sid = env.ids["refactor"]
    last = c.json(f"/api/session/{sid}?limit=3")
    assert [e["i"] for e in last["entries"]] == [last["first"], last["first"] + 1, last["first"] + 2] and last["has_before"]
    prev = c.json(f"/api/session/{sid}?limit=3&before={last['first']}")
    assert prev["entries"][-1]["i"] == last["first"] - 1
    nxt = c.json(f"/api/session/{sid}?limit=50&after={last['entries'][0]['i']}")
    assert nxt["entries"][0]["i"] == last["entries"][0]["i"] + 1


def test_huge_message_is_collapsed_and_paged(c, env):
    sid = env.ids["huge"]
    t0 = time.time()
    st, _, body = c.get(f"/api/session/{sid}?limit=30")
    assert st == 200 and time.time() - t0 < 10
    assert len(body) < 400_000, len(body)           # 4 MB of text in the transcript, a few 10s of KB on the wire
    page = json.loads(body)
    html = "".join(e["html"] for e in page["entries"])
    assert "of 3,888,910 characters (100,001 lines)" in html and 'class="more"' in html
    assert html.count("line 0:") >= 1 and "line 99999:" not in html
    m = re.search(r'data-entry="(\d+)" data-field="(text)" data-offset="0"', html)
    assert m
    off, total, chunks = 0, None, 0
    while True:
        r = c.json(f"/api/session/{sid}/text?entry={m.group(1)}&field=text&offset={off}&limit=1000000")
        assert len(r["text"]) <= 1_000_000
        total = r["total"]
        off, chunks = r["next"], chunks + 1
        if r["done"]:
            break
    assert off == total and chunks >= 4 and "line 99999:" in r["text"]
    assert c.get(f"/api/session/{sid}/text?entry=0&field=bogus")[0] == 404
    assert c.get(f"/api/session/{sid}/text?entry=9999&field=text")[0] == 404


def test_extensions_payload(c):
    d = c.json("/api/extensions")
    for key in ("mcp", "skills", "plugins", "hooks", "agents", "commands", "settings"):
        assert key in d
    assert any(a["name"] for a in d["agents"]) and any(x["name"] == "/open" for x in d["commands"])


# ─── sharing and gists ───────────────────────────────────────────────────────

def test_session_share_is_self_contained_and_escaped(c, env):
    st, _, body = c.post("/api/share/preview", {"kind": "session", "id": env.ids["xss"]})
    p = json.loads(body)
    doc = p["content"]
    assert p["bytes"] == len(doc.encode()) and re.fullmatch(r"[0-9a-f]{64}", p["sha256"])
    assert "<script" not in doc.lower() and "onerror=" not in re.sub(r"&lt;[^&]*?&gt;", "", doc)
    assert "&lt;script&gt;alert(&#x27;xss-user&#x27;)" in doc
    assert not re.search(r"(src|href)=[\"']https?://(?!example\.com)", doc) and "<link" not in doc
    assert "<style>" in doc and "[REDACTED]" in doc and SECRET_KEY not in doc and "@media print" in doc
    st, h, file = c.get(f"/api/share/file?sha={p['sha256']}")
    assert st == 200 and file.decode() == doc and h["content-disposition"].startswith("attachment; filename=")
    assert c.get("/api/share/file?sha=" + "0" * 64)[0] == 404


def test_overview_share_has_no_script(c):
    st, _, body = c.post("/api/share/preview", {"kind": "overview", "period": "30d"})
    doc = json.loads(body)["content"]
    assert st == 200 and "<script" not in doc.lower() and "Axon usage report" in doc


def test_gist_needs_confirm_matching_preview_and_gh(c, env, monkeypatch):
    calls: list[Any] = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="https://gist.github.com/me/abc123\n", stderr="")
    monkeypatch.setattr(dash.subprocess, "run", fake_run)
    monkeypatch.setattr(dash.shutil, "which", lambda n: "/usr/bin/gh")
    p = json.loads(c.post("/api/share/preview", {"kind": "session", "id": env.ids["refactor"]})[2])
    # nothing is uploaded without the explicit confirm flag, with a wrong hash, or for unseen content
    assert c.post("/api/share/gist", {"sha256": p["sha256"]})[0] == 400
    assert c.post("/api/share/gist", {"sha256": p["sha256"], "confirm": "true"})[0] == 400
    assert c.post("/api/share/gist", {"sha256": "f" * 64, "confirm": True})[0] == 409
    assert c.post("/api/share/gist", {"confirm": True})[0] == 409
    assert calls == []
    st, _, body = c.post("/api/share/gist", {"sha256": p["sha256"], "confirm": True})
    assert st == 200 and json.loads(body)["url"] == "https://gist.github.com/me/abc123"
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv[1:3] == ["gist", "create"] and "--public" not in argv      # secret is gh's default
    assert kw["input"] == p["content"]                                      # exactly what the preview showed
    # a gist is never created by the preview step itself
    c.post("/api/share/preview", {"kind": "overview"})
    assert len(calls) == 1


def test_gist_without_gh_and_too_large(c, env, monkeypatch):
    monkeypatch.setattr(dash.shutil, "which", lambda n: None)
    p = json.loads(c.post("/api/share/preview", {"kind": "session", "id": env.ids["short"]})[2])
    assert p["gh"] is False
    st, _, body = c.post("/api/share/gist", {"sha256": p["sha256"], "confirm": True})
    assert st == 501 and b"gh" in body
    big = json.loads(c.post("/api/share/preview", {"kind": "session", "id": env.ids["huge"]})[2])
    assert big["gist_ok"] is (big["bytes"] <= dash.GIST_MAX_BYTES)
    if not big["gist_ok"]:
        monkeypatch.setattr(dash.shutil, "which", lambda n: "/usr/bin/gh")
        assert c.post("/api/share/gist", {"sha256": big["sha256"], "confirm": True})[0] == 413


# ─── replies ─────────────────────────────────────────────────────────────────

@pytest.fixture
def llm(monkeypatch):
    from fake_llm import FakeLLMServer
    srv = FakeLLMServer(model_ids=("gpt-4o", "fake-model")).start()
    for k, v in dict(AXON_BASE_URL=srv.url, AXON_API_KEY="sk-e2e-test-key", AXON_MODEL="gpt-4o", AXON_OFFLINE="1",
                     AXON_NO_UPDATE_CHECK="1", AXON_TITLE_LLM="0", AXON_TIPS_LLM="0", AXON_PROMPT_SUGGESTIONS="0",
                     AXON_NO_NOTIFY="1").items():
        monkeypatch.setenv(k, v)
    yield srv
    srv.stop()


def _stream(c: Client, sid: str, text: str, mode: str = "default", on_event=None) -> list[tuple[str, dict]]:
    conn, r = c.raw("POST", f"/api/session/{sid}/reply", {"text": text, "mode": mode})
    assert r.status == 200, r.read()
    assert "text/event-stream" in r.getheader("Content-Type")
    events: list[tuple[str, dict]] = []
    ev, data = "message", ""
    while True:
        line = r.fp.readline()
        if not line:
            break
        line = line.decode().rstrip("\n")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
        elif line == "" and data:
            d = json.loads(data)
            events.append((ev, d))
            if on_event:
                on_event(ev, d)
            ev, data = "message", ""
    conn.close()
    return events


def test_reply_streams_and_persists(c, env, llm):
    llm.enqueue([{"text": "Hello from the fake model"}])
    events = _stream(c, env.ids["short"], "say hi")
    kinds = [k for k, _ in events]
    assert kinds[0] == "run" and "text_delta" in kinds and kinds[-1] == "result"
    assert "".join(d["text"] for k, d in events if k == "text_delta") == "Hello from the fake model"
    res = events[-1][1]
    assert res["is_error"] is False and res["session_id"] == env.ids["short"]
    page = c.json(f"/api/session/{env.ids['short']}?limit=50")
    assert "Hello from the fake model" in "".join(e["html"] for e in page["entries"])
    assert page["meta"]["running"] is False
    assert not any(API_KEY in json.dumps(d) for _, d in events)


def test_reply_default_mode_denies_unapproved_tools(c, env, llm):
    llm.enqueue([{"tool": "Write", "args": {"path": "out.txt", "content": "x"}}], [{"text": "denied, ok"}])
    approvals: list[dict] = []

    def on_event(kind, d):
        if kind == "approval_request":
            approvals.append(d)
            run = next(iter(c.srv.runs))
            st, _, _ = c.post("/api/approval", {"run": run, "id": d["id"], "allow": False})
            assert st == 200
    events = _stream(c, env.ids["short"], "write out.txt", on_event=on_event)
    assert len(approvals) == 1 and approvals[0]["tool"] == "Write"          # surfaced, never auto-run
    assert not (env.ws / "out.txt").exists()
    results = [d for k, d in events if k == "tool_result"]
    assert results and results[0]["is_error"] is True
    assert events[-1][1]["result"] == "denied, ok"


def test_reply_approve_once_runs_the_tool(c, env, llm):
    llm.enqueue([{"tool": "Write", "args": {"path": "ok.txt", "content": "hello"}}], [{"text": "written"}])

    def on_event(kind, d):
        if kind == "approval_request":
            assert not (env.ws / "ok.txt").exists()
            c.post("/api/approval", {"run": next(iter(c.srv.runs)), "id": d["id"], "allow": True})
    _stream(c, env.ids["short"], "write ok.txt", on_event=on_event)
    assert (env.ws / "ok.txt").read_text() == "hello"


def test_reply_rejects_bad_mode_empty_and_concurrent(c, env, llm):
    sid = env.ids["short"]
    assert c.post(f"/api/session/{sid}/reply", {"text": "x", "mode": "bypass"})[0] == 400
    assert c.post(f"/api/session/{sid}/reply", {"text": "   "})[0] == 400
    assert c.post("/api/approval", {"run": "nope", "id": "nope", "allow": True})[0] == 404
    llm.enqueue([{"delay": 1.5}, {"text": "slow"}])
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(_stream(c, sid, "first")))
    t.start()
    deadline = time.time() + 10
    while sid not in c.srv.busy and time.time() < deadline:
        time.sleep(0.02)
    st, _, body = c.post(f"/api/session/{sid}/reply", {"text": "second"})
    assert st == 409 and b"already running" in body
    t.join(20)
    assert out and out[0][-1][0] == "result"


def test_reply_queues_onto_live_repl_agent(env, monkeypatch):
    from axon.agent.state import MessageQueue
    agent = SimpleNamespace(session=SimpleNamespace(active_session_id=env.ids["refactor"]), message_queue=MessageQueue(),
                            busy=False, settings=SimpleNamespace(api_key=SimpleNamespace(get_secret_value=lambda: "")))
    import axon.features.sdk as sdk
    monkeypatch.setattr(sdk, "AxonSession", lambda *a, **k: pytest.fail("a second writer was opened"))
    srv = dash.make_server(0, env.ws, agent=agent)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        cl = Client(srv)
        cl.login()
        assert cl.json("/api/info")["live_session"] == env.ids["refactor"]
        live = [s for s in cl.json("/api/sessions")["sessions"] if s["live"]]
        assert [s["id"] for s in live] == [env.ids["refactor"]]
        st, _, body = cl.post(f"/api/session/{env.ids['refactor']}/reply", {"text": "do the thing"})
        r = json.loads(body)
        assert st == 200 and r["queued"] and r["live"] and "terminal" in r["message"]
        assert [i.text for i in agent.message_queue.items] == ["do the thing"] and not agent.message_queue.items[0].steer
        agent.busy = True
        r = json.loads(cl.post(f"/api/session/{env.ids['refactor']}/reply", {"text": "steer me"})[2])
        assert r["busy"] and agent.message_queue.items[-1].steer is True
        assert cl.json(f"/api/session/{env.ids['refactor']}?limit=1")["meta"]["live"] is True
    finally:
        srv.shutdown()
        srv.server_close()


# ─── commands, browser opening ───────────────────────────────────────────────

def test_registered_as_cli_and_slash_commands():
    from axon.features import cli_subcommands, feature_commands
    assert cli_subcommands()["open"] is dash.dash_main and cli_subcommands()["dash"] is dash.dash_main
    cmd = next(x for x in feature_commands() if x.name == "/open")
    assert {"/dash", "/web"} <= set(cmd.aliases)


@pytest.mark.parametrize("env_vars,platform,expected", [
    ({"SSH_CONNECTION": "1 2 3 4"}, "darwin", False), ({"SSH_TTY": "/dev/pts/0"}, "linux", False),
    ({}, "linux", False), ({"DISPLAY": ":0"}, "linux", True), ({"WAYLAND_DISPLAY": "w0"}, "linux", True),
    ({}, "darwin", True), ({}, "win32", True), ({"CI": "1"}, "darwin", False), ({"AXON_NO_BROWSER": "1"}, "darwin", False),
    ({"DISPLAY": ":0", "SSH_CONNECTION": "x"}, "linux", False),
])
def test_has_display(env_vars, platform, expected):
    assert dash.has_display(env_vars, platform) is expected


def test_slash_command_background_reuse_and_headless(env, monkeypatch, capsys):
    opened: list[str] = []
    monkeypatch.setattr(dash, "open_browser", lambda url: opened.append(url) or True)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=env.ws,
                            api_key=SimpleNamespace(get_secret_value=lambda: "")), message_queue=None)
    try:
        t0 = time.time()
        dash._dash_command(agent, "0")
        assert time.time() - t0 < 5                       # returns at once: the server runs on a thread
        srv = dash._LIVE["server"]
        out1 = capsys.readouterr().out
        assert srv.open_url in out1 and opened == [srv.open_url]
        cl = Client(srv)
        cl.login()
        assert cl.get("/api/info")[0] == 200               # actually serving in the background
        dash._dash_command(agent, "")
        out2 = capsys.readouterr().out
        assert dash._LIVE["server"] is srv and "reusing" in out2 and srv.open_url in out2
        assert len(opened) == 2
        dash._dash_command(agent, "--no-open")
        assert len(opened) == 2 and srv.open_url in capsys.readouterr().out
        monkeypatch.undo()                                  # headless: print the URL, never call a browser
        monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
        called: list[str] = []
        import webbrowser
        monkeypatch.setattr(webbrowser, "open", lambda *a, **k: called.append("x") or True)
        dash._dash_command(agent, "")
        assert called == [] and srv.open_url in capsys.readouterr().out
    finally:
        dash._dash_command(agent, "stop")
    assert dash._LIVE["server"] is None


def test_dash_main_no_open_prints_url(env, monkeypatch, capsys):
    started: dict[str, Any] = {}

    def fake_serve(self, poll_interval=0.5):
        started["url"] = self.open_url
        raise KeyboardInterrupt
    monkeypatch.setattr(dash.DashServer, "serve_forever", fake_serve)
    monkeypatch.setattr(dash, "open_browser", lambda url: pytest.fail("browser opened despite --no-open"))
    assert dash.dash_main(["--no-open", "--port", "0", "--workspace", str(env.ws)]) == 0
    out = capsys.readouterr().out
    assert started["url"] in out and "127.0.0.1" in out
    assert dash.dash_main(["--workspace", str(env.tmp / "missing")]) == 2
