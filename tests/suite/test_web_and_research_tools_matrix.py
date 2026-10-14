"""
Test matrix for WebFetch and WebSearch. No network: httpx.MockTransport via axon.tools._http._TRANSPORT.
"""
import httpx
import pytest
from pathlib import Path
from axon.errors import ToolError
from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.tools import _http
from axon.tools.base import ToolContext
from axon.tools import create_default_registry


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=Settings(workspace=workspace))


@pytest.fixture
def mock_net(monkeypatch):
    routes = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        fn = routes.get(key)
        if fn is None:
            raise httpx.ConnectError("no route", request=request)
        return fn(request)

    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    return routes


@pytest.mark.parametrize("invalid_url", ["ftp://example.com", "file:///etc/passwd", "just_a_string"])
def test_webfetch_url_validation_rejections(ctx, invalid_url):
    with pytest.raises(ToolError, match="Invalid URL"):
        create_default_registry().get("WebFetch").run({"url": invalid_url}, ctx)


def test_webfetch_requires_url(ctx):
    with pytest.raises(ToolError, match="requires 'url'"):
        create_default_registry().get("WebFetch").run({}, ctx)


def test_webfetch_html_to_markdown(ctx, mock_net):
    html = """<html><head><title>Docs Page</title><style>.x{}</style></head><body>
      <nav>menu junk</nav><main><h1>Install</h1><p>Run <code>pip install axon</code> &amp; enjoy.</p>
      <ul><li>first</li><li>second</li></ul><pre>print("hi")</pre>
      <a href="/next">Next page</a><script>alert(1)</script></main></body></html>"""
    mock_net["docs.example.com/guide"] = lambda r: httpx.Response(200, html=html)
    res = create_default_registry().get("WebFetch").run(
        {"url": "https://docs.example.com/guide", "prompt": "install steps"}, ctx)
    assert "Title: Docs Page" in res
    assert "# Install" in res
    assert "`pip install axon` & enjoy." in res
    assert "- first" in res and "- second" in res
    assert '```\nprint("hi")\n```' in res
    assert "[Next page](https://docs.example.com/next)" in res
    assert "alert(1)" not in res and "menu junk" not in res
    assert "Looking for: install steps" in res


def test_webfetch_json_pretty_and_paging(ctx, mock_net):
    mock_net["api.example.com/status"] = lambda r: httpx.Response(200, json={"status": "ok", "items": list(range(3000))})
    tool = create_default_registry().get("WebFetch")
    res = tool.run({"url": "https://api.example.com/status", "max_chars": 1000}, ctx)
    assert '"status": "ok"' in res
    assert "start_char=1000" in res


def test_webfetch_http_error(ctx, mock_net):
    mock_net["example.com/missing"] = lambda r: httpx.Response(404, text="Not Found")
    with pytest.raises(ToolError, match="HTTP 404"):
        create_default_registry().get("WebFetch").run({"url": "https://example.com/missing"}, ctx)


def test_webfetch_network_error_is_toolerror(ctx, mock_net):
    with pytest.raises(ToolError, match="Network error"):
        create_default_registry().get("WebFetch").run({"url": "https://unreachable.example/"}, ctx)


def test_webfetch_size_cap(ctx, mock_net, monkeypatch):
    monkeypatch.setattr(_http, "DEFAULT_MAX_BYTES", 1000)
    mock_net["big.example.com/file.txt"] = lambda r: httpx.Response(200, text="A" * 50_000,
                                                                     headers={"content-type": "text/plain"})
    res = _http.fetch("https://big.example.com/file.txt", max_bytes=1000)
    assert res.truncated and len(res.text) == 1000


DDG_HTML = """<div class="result results_links web-result"><div class="result__body">
<h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&rut=x">asyncio &mdash; Asynchronous I/O</a></h2>
<a class="result__snippet" href="#">asyncio is a library to write <b>concurrent</b> code.</a></div></div>
<div class="result result--ad"><a class="result__a" href="https://ads.example.com">Buy now</a></div>
<div class="result"><a class="result__a" href="https://realpython.com/async-io-python/">Async IO in Python</a>
<a class="result__snippet">A complete walkthrough.</a></div>"""


def test_websearch_ddg_parsing(ctx, mock_net):
    mock_net["html.duckduckgo.com/html/"] = lambda r: httpx.Response(200, html=DDG_HTML)
    res = create_default_registry().get("WebSearch").run({"query": "python asyncio"}, ctx)
    assert "https://docs.python.org/3/library/asyncio.html" in res
    assert "asyncio — Asynchronous I/O" in res
    assert "concurrent code" in res
    assert "ads.example.com" not in res
    assert "realpython.com" in res


def test_websearch_domain_filters(ctx, mock_net):
    mock_net["html.duckduckgo.com/html/"] = lambda r: httpx.Response(200, html=DDG_HTML)
    tool = create_default_registry().get("WebSearch")
    res = tool.run({"query": "asyncio", "blocked_domains": ["realpython.com"]}, ctx)
    assert "realpython.com" not in res and "docs.python.org" in res


def test_websearch_falls_back_to_lite(ctx, mock_net):
    mock_net["html.duckduckgo.com/html/"] = lambda r: httpx.Response(503, text="busy")
    lite = ('<table><tr><td><a class="result-link" href="https://example.org/page">Example Page</a></td></tr>'
            '<tr><td class="result-snippet">Snippet text</td></tr></table>')
    mock_net["lite.duckduckgo.com/lite/"] = lambda r: httpx.Response(200, html=lite)
    res = create_default_registry().get("WebSearch").run({"query": "example"}, ctx)
    assert "via DuckDuckGo Lite" in res and "https://example.org/page" in res and "Snippet text" in res


def test_websearch_brave_backend(ctx, mock_net, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    seen = {}

    def brave(r):
        seen["token"] = r.headers.get("X-Subscription-Token")
        return httpx.Response(200, json={"web": {"results": [{"title": "T", "url": "https://b.example/x", "description": "D"}]}})

    mock_net["api.search.brave.com/res/v1/web/search"] = brave
    res = create_default_registry().get("WebSearch").run({"query": "brave test"}, ctx)
    assert "via Brave" in res and "https://b.example/x" in res and seen["token"] == "k"


def test_websearch_offline_clear_error(ctx, mock_net):
    with pytest.raises(ToolError, match="WebSearch failed"):
        create_default_registry().get("WebSearch").run({"query": "anything"}, ctx)


def test_websearch_requires_query(ctx):
    with pytest.raises(ToolError, match="query"):
        create_default_registry().get("WebSearch").run({"query": " "}, ctx)


def test_web_tools_not_readonly():
    reg = create_default_registry()
    assert reg.get("WebFetch").readonly is False
    assert reg.get("WebSearch").readonly is False
