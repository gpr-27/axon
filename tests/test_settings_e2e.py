"""Settings, providers and usability fixes found by driving the web app: local endpoint tests, the model list,
plain-JSON replies, the saved key staying with its own host, shortcut help, and the code theme ground."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features.dash_appearance import APPEARANCE_CSS  # noqa: E402
from axon.features.dash_js_chat import JS_CHAT  # noqa: E402
from axon.features.dash_js_settings import JS_SETTINGS  # noqa: E402
from axon.features.dash_js_tools import JS_TOOLS  # noqa: E402
from axon.features.dash_js_workbench import JS_WORKBENCH  # noqa: E402
from axon.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from test_dashboard import Client, c, env, llm, server  # noqa: E402,F401


class _Keyed(BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.headers.get("authorization", ""))
        if self.headers.get("authorization") != "Bearer good-key-0123456789":
            body, code = b'{"error":{"message":"Incorrect API key provided"}}', 401
        else:
            body, code = b'{"data":[{"id":"keyed-a"},{"id":"keyed-b"}]}', 200
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _keyed_server():
    _Keyed.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Keyed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def _test(c: Client, **body) -> dict:
    return json.loads(c.post("/api/provider/test", body)[2])


def test_local_server_that_is_not_running_is_not_reported_as_working(c):
    r = _test(c, preset="ollama", key="", base_url="http://127.0.0.1:9/v1", model="llama3")
    assert r["ok"] is False and "connect" in r["message"].lower()


def test_local_server_with_a_wrong_or_missing_key_says_so_and_a_right_key_passes(c):
    srv, url = _keyed_server()
    try:
        none = _test(c, preset="lmstudio", key="", base_url=url, model="x")
        bad = _test(c, preset="lmstudio", key="wrong-key-abcdef", base_url=url, model="x")
        good = _test(c, preset="lmstudio", key="good-key-0123456789", base_url=url, model="x")
    finally:
        srv.shutdown()
    assert none["ok"] is False and "needs a key" in none["message"]
    assert bad["ok"] is False and "refused" in bad["message"] and "wrong-key-abcdef" not in json.dumps(bad)
    assert good["ok"] is True


def test_the_saved_key_is_never_sent_to_a_host_the_person_typed(c, env):
    c.post("/api/provider/key", {"preset": "agentrouter", "key": "good-key-0123456789"})
    srv, url = _keyed_server()
    try:
        r = _test(c, preset="agentrouter", key="", base_url=url, model="x")
    finally:
        srv.shutdown()
    assert r["ok"] is False and "Bearer good-key-0123456789" not in _Keyed.seen


def test_custom_endpoint_test_works_without_a_key_when_it_is_local(c, llm):
    assert _test(c, preset="agentrouter", key="", base_url=llm.url + "/v1", model="gpt-4o")["ok"] is True


def test_model_list_is_the_endpoints_own_not_every_provider(c, llm):
    r = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "fake-model"})
    assert r[0] == 200
    d = c.json("/api/models")
    assert d["live"] is True and d["current"] == "fake-model"
    assert set(d["models"]) == set(llm.model_ids) | {"fake-model"}
    assert "claude-opus-5-5" not in d["models"]


class _Resp:
    """A 200 answer that is one JSON body instead of an event stream."""

    def __init__(self, text: str):
        self._t = text

    def iter_lines(self):
        return iter(self._t.split("\n"))


def test_a_gateway_that_ignores_stream_still_gets_its_answer_shown():
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "plain reply"}, "finish_reason": "stop"}]})
    chunks = [ch for e, ch in OpenAICompatProvider._sse(_Resp(body)) if e != "__done__"]
    assert chunks and chunks[0]["choices"][0]["message"]["content"] == "plain reply"
    stream = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n'
    assert len([1 for e, _c in OpenAICompatProvider._sse(_Resp(stream)) if e != "__done__"]) == 1          # real event streams are untouched


def test_shortcut_keys_work_with_the_footer_buttons_and_are_listed():
    assert "C.foot.model.click()" in JS_TOOLS and "WB_MODES" in JS_TOOLS            # Ctrl+P and Shift+Tab have no select now
    for want in ("Ctrl+P", "Shift+Tab", "Terminal", "Insights"):
        assert want in JS_TOOLS
    assert "never grows wider than the room" in JS_SETTINGS and "More menu" in JS_SETTINGS


def test_chat_title_reaches_the_tab_when_the_first_turn_ends():
    assert "setTabTitle(C.tab, C.meta.title" in JS_CHAT and "wbTitleSync" in JS_CHAT
    assert "the tab carries the chat's name" in JS_WORKBENCH


def test_a_chosen_code_theme_also_paints_the_file_viewer():
    assert "[data-code-theme] .code" in APPEARANCE_CSS
