"""The Settings page's Permission mode, saved before any chat exists, must be the mode the next chat starts in."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401


def test_saved_accept_edits_is_what_the_next_chat_starts_in(c, env, llm):
    st, _, body = c.post("/api/settings/set", {"key": "mode", "value": "acceptEdits"})
    assert st == 200 and b"next chat" in body
    panel = {r["key"]: r["value"] for r in json.loads(c.get("/api/settings/panel")[2])["settings"]}
    assert panel["mode"] == "acceptEdits"                       # the page shows what it just saved
    c.post("/api/chat/new", {})
    assert json.loads(c.get("/api/chat/state")[2])["mode"] == "acceptEdits"   # and the chat really starts there
