"""State bugs from QA round 2: credential saves, empty session ids, resumed ledgers, mode and folder consistency."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from test_dashboard import c, env, llm, server  # noqa: E402,F401


@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.json"])
def test_server_refuses_saving_a_credential_file_with_a_correct_sha(c, env, name):
    p = env.ws / name
    original = b"API_KEY=abc\nOTHER=short\n"
    p.write_bytes(original)
    sha = hashlib.sha256(original).hexdigest()
    st, _, body = c.post("/api/file/save", {"path": name, "content": "API_KEY=changed\n", "sha": sha})
    assert st in (403, 409) and b"credential" in body.lower(), (st, body)
    assert p.read_bytes() == original


def test_review_with_an_empty_session_is_an_empty_result_not_an_error(c, env):
    st, _, body = c.get("/api/insights/review?session=")
    assert st == 200 and b"hunks" in body
    st, _, body = c.get("/api/insights/review")
    assert st == 200


def test_a_chat_just_opened_has_an_empty_page_not_a_404(c, env):
    """The chat view asks for the history of a brand-new session before any file exists; a 404 shows up as a
    console error on every new chat."""
    sid = json.loads(c.post("/api/chat/new", {})[2])["session"]
    st, _, body = c.get(f"/api/session/{sid}?limit=30")
    assert st == 200 and json.loads(body)["entries"] == []
    assert c.get("/api/session/session_20200101_000000_proj_000000?limit=30")[0] == 404


def test_progress_cost_of_a_resumed_session_is_its_recorded_cost(c, env):
    """After a restart the ledger starts empty; the panel must show what the transcript recorded, not $0.00
    (unpriced models make a recomputed ledger zero)."""
    from test_dashboard_sample import _session
    from axon import storage
    sid = "session_20261106_120000_proj_ffff06"
    _session(storage.sessions_dir(env.ws), sid, env.ws,
             [{"user": "hi", "text": "yo", "model": "my-unpriced-custom-model", "cost": 0.0631}], age_h=1)
    st = c.json(f"/api/chat/state?session={sid}")
    assert st["cost"] == pytest.approx(0.0631), st["cost"]


# ─── page logic, run in node against the real page script ───────────────────

import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from axon.features.dashboard_web import APP_JS  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page script")


def js_fn(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^}\n", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def node(body: str) -> str:
    p = subprocess.run(["node", "-"], input=body, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@needs_node
def test_progress_follows_the_active_tab_not_the_last_tab_to_load():
    out = node(js_fn("claimRail") + """
    const UI = { chat: null, active: null };
    const A = { name: 'first' }, B = { name: 'active' }, ta = {}, tb = {};
    UI.active = tb;                       // the active tab is the second one
    claimRail(A, ta);                     // the first tab finishes loading first (it claims an empty panel)
    claimRail(B, tb);                     // then the active one
    const r1 = UI.chat.name;
    UI.chat = B; claimRail(A, ta);        // a background tab finishing later must not take it back
    console.log(r1 + ',' + UI.chat.name);
    """)
    assert out == "active,active"


FAKE_FEED = r"""
class El { constructor(c){this.c=c;this.kids=[];this.textContent='';this.removed=false;}
  querySelector(sel){ if(sel==='.turnbar .spin') return this.spin&&!this.spin.removed?this.spin:null; if(sel==='.turnbar .tl') return this; return null; } }
"""


@needs_node
def test_a_replayed_turn_that_never_ended_does_not_spin_forever():
    out = node(FAKE_FEED + js_fn("settleSpinners") + """
    const art = new El(); art.spin = { removed: false, remove() { this.removed = true; } }; art.textContent = 'working';
    const C = { cur: null, lastEv: 0, feed: { querySelectorAll: () => [art] } };
    settleSpinners(C, false);             // the header says idle
    console.log(art.spin.removed + ',' + art.textContent);
    const live = new El(); live.spin = { removed: false, remove() { this.removed = true; } };
    const C2 = { cur: { art: live }, lastEv: Date.now(), feed: { querySelectorAll: () => [live] } };
    settleSpinners(C2, true);             // a genuinely running turn keeps its spinner
    console.log(live.spin.removed);
    """)
    assert out.splitlines() == ["true,ended", "false"]


def test_chat_view_uses_both_helpers():
    assert "claimRail(C, tab)" in APP_JS and "settleSpinners(C, !!st.busy)" in APP_JS


def _apply_custom(c, llm, model="fake-model"):
    st, _, body = c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": model, "key": "sk-test-custom-key-123456"})
    assert st == 200, body
    return json.loads(body)


def test_a_custom_endpoint_survives_change_folder(c, env, llm, monkeypatch):
    import json as _j
    for k in ("AXON_MODEL", "AXON_BASE_URL"):         # a saved choice, not an environment override, is under test
        monkeypatch.delenv(k, raising=False)
    c.post("/api/chat/new", {})
    _apply_custom(c, llm)
    assert c.post("/api/ws/set", {"path": str(env.tmp / "other")})[0] == 200
    c.post("/api/chat/new", {})
    assert _j.loads(c.get("/api/chat/state")[2])["model"] == "fake-model"
    assert c.post("/api/ws/set", {"path": str(env.ws)})[0] == 200
    c.post("/api/chat/new", {})
    assert _j.loads(c.get("/api/chat/state")[2])["model"] == "fake-model"


def test_a_custom_endpoint_survives_change_folder_even_with_a_stale_model_variable(c, env, llm):
    import json as _j
    c.post("/api/chat/new", {})
    _apply_custom(c, llm)
    c.post("/api/ws/set", {"path": str(env.tmp / "other")})
    c.post("/api/chat/new", {})
    assert _j.loads(c.get("/api/chat/state")[2])["model"] == "fake-model"


def test_the_status_bar_and_the_chat_mode_agree(c, env):
    """settings.json says bypass; the web app never starts a chat in bypass. Every place must say the same thing."""
    from axon.commands.common import update_settings_file
    update_settings_file({"mode": "bypass"})
    c.srv._settings_cache.clear()
    before = json.loads(c.get("/api/info")[2])["mode"]
    st = json.loads(c.post("/api/chat/new", {})[2])
    after = json.loads(c.get("/api/info")[2])["mode"]
    chat = json.loads(c.get("/api/chat/state")[2])["mode"]
    assert before == after == chat == "default", (before, after, chat, st.get("mode"))
    panel = {r["key"]: r["value"] for r in json.loads(c.get("/api/settings/panel")[2])["settings"]}
    assert panel.get("mode") in (None, "default"), panel.get("mode")


def test_shell_folder_resets_to_the_project_after_change_folder(c, env):
    import time
    ws = env.ws.resolve()
    c.srv.shell.cwds[str(ws)] = ws / "src"            # an earlier `cd src` in the dock
    assert json.loads(c.get("/api/shell/history")[2])["cwd"].endswith("src")
    other = (env.tmp / "other").resolve()
    assert c.post("/api/ws/set", {"path": str(other)})[0] == 200
    assert json.loads(c.get("/api/shell/history")[2])["cwd"] == str(other)
    assert c.post("/api/ws/set", {"path": str(ws)})[0] == 200
    assert json.loads(c.get("/api/shell/history")[2])["cwd"] == str(ws)      # not the stale src folder


@needs_node
def test_shell_dock_header_follows_the_folder():
    assert "function shellFolderChanged(" in APP_JS
    out = node(js_fn("shellFolderChanged") + """
    const els = { shcwd: { textContent: '' } }; const input = { placeholder: '' };
    globalThis.document = { getElementById: (i) => i === 'shinput' ? input : (els[i] || null) };
    const state = { ws: { name: 'proj', workspace: '/w/proj' } };
    shellFolderChanged();
    console.log(els.shcwd.textContent + '|' + input.placeholder);
    """)
    assert out == "in /w/proj|Run a command in proj  (Enter)"


def test_model_command_says_why_it_opens_providers(c, env):
    r = json.loads(c.post("/api/commands/run", {"line": "/model", "session": ""})[2])
    assert r["open"] == "#/settings/providers" and "/model" in r["message"] and "\n" not in r["message"].strip()


@needs_node
def test_providers_banner_names_the_command_and_the_dock_origin_is_parsed():
    out = node(js_fn("providersOrigin") + """
    console.log([providersOrigin('providers?from=%2Fmodel'), providersOrigin('providers'), providersOrigin('providers?from=%3Cscript%3E')].join('|'));
    """)
    assert out == "/model||"
    assert "setTimeout(() => { location.hash = dest; }, 700)" in APP_JS and "id: 'from-cmd'" in APP_JS


def test_model_dropdown_lists_only_the_current_provider():
    pick = js_fn("modelPicker")
    assert "pv.providers.map" not in pick and "slice(0, 40)" not in pick and "Set up another provider..." in pick
