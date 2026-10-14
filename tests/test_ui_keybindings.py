"""~/.axon/keybindings.json: parsing, merging, chords, effective map and live remapping in read_input."""
from __future__ import annotations

import contextlib
import json
import os
import time
from collections import deque

import pytest

from axon import platform as plat
from axon.ui import input as ui_input
from axon.ui import keybindings as kb
from axon.ui.input import read_input


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    h = tmp_path / "axhome"
    h.mkdir()
    monkeypatch.setenv("AXON_HOME", str(h))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    kb._cache.update(map=None, mtime=None, path=None, errors=[])
    return h


def write(home, data) -> None:
    p = home / "keybindings.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))   # make the cache notice the change


@pytest.mark.parametrize("spec,expected", [
    ("ctrl+e", "ctrl-e"), ("Ctrl+E", "ctrl-e"), ("escape", "esc"), ("Return", "enter"),
    ("option+enter", "alt-enter"), ("meta+b", "alt-b"), ("shift+tab", "shift-tab"),
    ("ctrl+x ctrl+e", "ctrl-x ctrl-e"), ("a", "a"), ("shift+a", "A"), ("ctrl+_", "ctrl-_"),
    ("hyper+q", None), ("", None),
])
def test_normalize_key(spec, expected):
    assert kb.normalize_key(spec) == expected


def test_defaults_documented_and_effective_without_file():
    eff = kb.effective_keybindings()
    assert eff["chat:submit"] == ["enter"]
    assert "ctrl-x ctrl-e" in eff["chat:externalEditor"]
    assert set(eff) == set(kb.ACTIONS)
    assert all(kb.describe_action(a) for a in kb.ACTIONS)
    assert kb.keybinding_errors() == []


def test_action_to_keys_replaces_defaults_and_steals_key(home):
    write(home, {"bindings": {"chat:externalEditor": ["ctrl+e"], "edit:undo": None}})
    eff = kb.effective_keybindings()
    assert eff["chat:externalEditor"] == ["ctrl-e"]
    assert "ctrl-e" not in eff["cursor:lineEnd"] and "end" in eff["cursor:lineEnd"]
    assert eff["edit:undo"] == []


def test_claude_code_style_blocks_key_to_action_and_unbind(home):
    write(home, {"bindings": [{"context": "Chat", "bindings": {"ctrl+t": None, "alt+u": "edit:undo",
                                                              "ctrl+q": "nope:action"}}]})
    eff = kb.effective_keybindings()
    assert eff["app:todos"] == []
    assert "alt-u" in eff["edit:undo"] and "ctrl-_" in eff["edit:undo"]
    assert any("nope:action" in e for e in kb.keybinding_errors())


def test_invalid_file_falls_back_to_defaults(home):
    write(home, "{not json")
    assert kb.effective_keybindings() == kb.default_bindings()
    assert "invalid JSON" in kb.keybinding_errors()[0]
    write(home, "[1, 2]")
    assert kb.effective_keybindings() == kb.default_bindings()


def test_resolver_chords():
    r = kb.KeyResolver({"chat:externalEditor": ["ctrl-x ctrl-e"], "edit:undo": ["ctrl-_"]})
    assert r.feed("ctrl-x") == (None, [])
    assert r.feed("ctrl-e") == ("chat:externalEditor", ["ctrl-e"])
    assert r.feed("ctrl-x") == (None, [])
    assert r.feed("a") == (None, ["a"])            # broken chord: the key is handled on its own
    assert r.feed("ctrl-_") == ("edit:undo", ["ctrl-_"])
    assert r.feed("z") == (None, ["z"])


@pytest.fixture
def keys(monkeypatch):
    def install(seq):
        q = deque(seq)

        def fake_read_key(timeout=None, report_resize=True):
            if not q:
                raise AssertionError("ran out of scripted keys")
            return q.popleft()
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    return install


def test_remap_applies_in_read_input(home, keys):
    write(home, {"bindings": {"ctrl+k": "chat:submit", "ctrl+b": "cursor:lineStart"}})
    keys(list("bc") + ["ctrl-b", "a", "ctrl-k"])
    assert read_input()[0] == "abc"


def test_unbound_default_key_does_nothing(home, keys):
    write(home, {"bindings": {"edit:killToStart": None}})
    keys(list("keep") + ["ctrl-u", "enter"])
    assert read_input()[0] == "keep"


def test_ctrl_n_is_history_next_and_tab_without_popup_does_not_cycle_mode(keys):
    ui_input._SESSION_HISTORY.extend(["one", "two"])
    keys(["up", "up", "ctrl-n", "tab", "enter"])
    text, mode, _ = read_input(mode="default")
    assert text == "two" and mode is None


def test_alt_o_runs_fast_command(keys):
    keys(["alt-o"])
    assert read_input()[0] == "/fast"
