"""@dir / @server:resource completion, registry autocomplete, per-directory history, in-flight steering
editor, spinner draft, rewind menu, non-TTY provider picker and clipboard image fallback."""
from __future__ import annotations

import contextlib
import io
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.agent.state import MessageQueue
from axon.ui import input as ui_input
from axon.ui.input import EditorState, read_input


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    ui_input._res_cache.update(t=0.0, items=[])
    ui_input._dyn_cache.clear()
    yield
    ui_input._res_cache.update(t=0.0, items=[])


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
        monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
    return install


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("x = 1\n")
    (root / "src" / "main.py").write_text("print(1)\n")
    (root / "README.md").write_text("# hi\n")
    from axon.ui import fuzzy_picker
    fuzzy_picker._cache.clear()
    monkeypatch.chdir(root)
    return root


# ── @ completion ────────────────────────────────────────────────────────────

def test_match_files_includes_directories(ws):
    items = ui_input.match_files("pkg", ws)
    assert "src/pkg/" in items and "src/pkg/mod.py" in items
    assert ui_input.match_files("src/", ws) == ["src/pkg/", "src/main.py"]   # direct children, folders first


def test_tab_on_directory_drills_in_enter_mentions_it(ws, keys, monkeypatch):
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    keys(list("see @src/") + ["tab", "tab", "enter"])   # tab: open src/pkg/, tab: pick mod.py, enter: send
    assert read_input(cwd=str(ws))[0] == "see @src/pkg/mod.py"
    keys(list("see @src/") + ["enter", "enter"])          # enter on a folder mentions the folder itself
    assert read_input(cwd=str(ws))[0] == "see @src/pkg/"
    st = EditorState(cwd=ws)
    st.set_text("@pk")
    pop = st.popup()
    st.popup_index = pop.items.index("src/pkg/")
    pop = st.popup()
    st.accept_completion(pop)
    assert st.buf == "@src/pkg/" and st.popup() is not None      # still completing inside the folder
    pop = st.popup()
    st.popup_index = pop.items.index("src/pkg/mod.py")
    st.accept_completion(st.popup())
    assert st.buf == "@src/pkg/mod.py "


class _FakeClient:
    closed = False

    def has(self, cap):
        return cap == "resources"

    def list_resources(self, refresh=False):
        return [{"uri": "docs://guide/intro", "name": "intro"}, {"uri": "docs://api", "name": "api"}]


def test_mcp_resource_mentions_and_completion(ws, monkeypatch):
    from axon.mcp import bridge
    conn = SimpleNamespace(connected=True, client=_FakeClient())
    monkeypatch.setattr(bridge, "_bridge", SimpleNamespace(connections={"docs": conn, "off": SimpleNamespace(connected=False, client=None)}))
    assert ui_input.mcp_resource_mentions() == ["docs:docs://guide/intro", "docs:docs://api"]
    items = ui_input.match_files("docs:api", ws)
    assert items[0] == "docs:docs://api" and all(i.startswith("docs:") for i in items)
    st = EditorState(cwd=ws)
    st.set_text("read @docs:intro")
    pop = st.popup()
    assert pop is not None and ui_input.is_resource_mention(pop.items[0])
    st.accept_completion(pop)
    assert st.buf == "read @docs:docs://guide/intro "
    lines = ui_input.render_popup(EditorState(cwd=ws).popup() or pop, 100, 8)
    assert any("mcp:docs" in l for l in lines)


def test_no_bridge_means_no_resources(monkeypatch):
    from axon.mcp import bridge
    monkeypatch.setattr(bridge, "_bridge", None)
    assert ui_input.mcp_resource_mentions() == []


def test_slash_autocomplete_from_command_registry(monkeypatch, tmp_path):
    import axon.commands as commands
    monkeypatch.setattr(commands, "list_command_names",
                        lambda ws=None: [("/model", "dup"), ("/deploy", "Ship it"), ("/mcp__gh__pr", "MCP prompt")])
    names = {c[0]: c for c in ui_input.registered_slash_commands(tmp_path)}
    assert "/deploy" in names and "/model" not in names
    assert names["/mcp__gh__pr"][1] == "mcp"
    assert ui_input.match_slash_commands("depl", tmp_path)[0][0] == "/deploy"


# ── history per directory ───────────────────────────────────────────────────

def test_read_input_uses_history_of_the_workspace(keys, monkeypatch, tmp_path):
    seen = {}

    def fake_load(limit=1000, cwd=None):
        seen["cwd"] = cwd
        return ["from this project"]
    monkeypatch.setattr(ui_input, "load_history", fake_load)
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    keys(["up", "enter"])
    assert read_input(cwd=str(tmp_path))[0] == "from this project"
    assert seen["cwd"] == str(tmp_path)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path / "agent_ws"))
    keys(["x", "enter"])
    read_input(agent=agent)
    assert seen["cwd"] == str(tmp_path / "agent_ws")


def test_load_history_filters_by_project(tmp_path, monkeypatch):
    from axon import storage
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    storage.record_history(a, "in a", "s")
    storage.record_history(b, "in b", "s")
    assert ui_input.load_history(cwd=str(a)) == ["in a"]
    assert ui_input.load_history(cwd=str(b)) == ["in b"]


# ── in-flight editor & steering ─────────────────────────────────────────────

def _listener(**agent_attrs):
    from axon.ui.in_flight import InFlightInputListener
    agent = SimpleNamespace(message_queue=MessageQueue(), **agent_attrs)
    return agent, InFlightInputListener(agent)


def test_typed_mid_turn_is_marked_steer():
    agent, li = _listener()
    for k in list("also run lint") + ["enter"]:
        li.handle_key(k)
    item = agent.message_queue.items[0]
    assert item.text == "also run lint" and getattr(item, "steer") is True


def test_in_flight_readline_keys_and_multiline():
    agent, li = _listener()
    for k in list("one two three") + ["ctrl-w", "ctrl-w", "ctrl-y", "home", "alt-f", "ctrl-k"]:
        li.handle_key(k)
    assert li.draft() == "one"
    for k in ["shift-enter"] + list("x") + ["enter"]:
        li.handle_key(k)
    assert agent.message_queue.items[0].text == "one\nx"


def test_up_on_empty_draft_pulls_back_the_last_queued_message():
    agent, li = _listener()
    agent.message_queue.push("first")
    agent.message_queue.push("second")
    li.handle_key("up")
    assert li.draft() == "second" and [i.text for i in agent.message_queue.items] == ["first"]


def test_ctrl_x_ctrl_s_queues_and_interrupts():
    called = []
    agent, li = _listener(cancel_current_turn=lambda: called.append(1))
    for k in list("now please") + ["ctrl-x", "ctrl-s"]:
        li.handle_key(k)
    assert called == [1]
    item = agent.message_queue.items[0]
    assert item.text == "now please" and item.steer is False


def test_ctrl_x_ctrl_k_needs_confirmation():
    stopped = []
    agent, li = _listener(subagents=SimpleNamespace(cancel_all=lambda: stopped.append(1) or 2))
    li.handle_key("ctrl-x"); li.handle_key("ctrl-k")
    assert stopped == []
    li.handle_key("ctrl-x"); li.handle_key("ctrl-k")
    assert stopped == [1]


def test_push_queued_with_flag_aware_queue():
    from axon.ui.in_flight import push_queued

    class Q:
        def __init__(self):
            self.got = None

        def push(self, text, steer=False):
            self.got = (text, steer)
            return SimpleNamespace(id=1, text=text, steer=steer)
    q = Q()
    assert push_queued(q, "hi", steer=True).steer is True and q.got == ("hi", True)


def test_spinner_shows_the_draft(monkeypatch):
    from axon.ui.render import Spinner
    from axon.ui.theme import strip_ansi
    monkeypatch.setattr(Spinner, "draft_provider", staticmethod(lambda: "check the logs"))
    line = strip_ansi(Spinner().render_line())
    assert "check the logs" in line
    monkeypatch.setattr(Spinner, "draft_provider", None)
    assert "check the logs" not in strip_ansi(Spinner().render_line())


# ── pickers ─────────────────────────────────────────────────────────────────

def _points():
    import time
    return [SimpleNamespace(index=1, prompt="add tests", ts=time.time() - 120, files_changed=2),
            SimpleNamespace(index=2, prompt="explain", ts=time.time() - 30, files_changed=0)]


def test_rewind_menu_flow(keys):
    from axon.ui.picker import run_rewind_menu
    pts = _points()
    keys(["up", "enter", "enter"])                     # last point preselected; up -> #1; first option
    assert run_rewind_menu(pts) == (pts[0], "code_and_conversation")
    keys(["enter", "enter"])                           # #2 changed no files: code options hidden
    assert run_rewind_menu(pts) == (pts[1], "conversation")
    keys(["enter", "esc"])
    assert run_rewind_menu(pts) is None


def test_rewind_menu_non_interactive(monkeypatch):
    from axon.ui.picker import rewind_point_items, run_rewind_menu
    monkeypatch.setattr(plat, "is_interactive", lambda: False)
    assert run_rewind_menu(_points()) is None
    items = rewind_point_items(_points())
    assert items[0].hint.endswith("2 files changed") and "no file changes" in items[1].hint


def test_choose_helper(keys):
    from axon.ui.picker import choose
    keys(["down", "enter"])
    assert choose([("A", "a"), ("B", "b", "hint")], title="T") == "b"
    assert choose([], title="T") is None


def test_provider_picker_never_blocks_without_tty(monkeypatch, capsys):
    from axon.ui import provider_picker as pp
    monkeypatch.setattr(plat, "is_interactive", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(pp.getpass, "getpass", lambda *a, **k: (_ for _ in ()).throw(AssertionError("blocked")))
    agent = SimpleNamespace(settings=SimpleNamespace(base_url="", model="m"))
    assert pp.run_provider_picker(agent) is False
    preset = next(p for p in pp.PROVIDER_PRESETS if p.requires_key)
    monkeypatch.setattr(pp, "find_key", lambda p: None)
    assert pp._configure_and_apply_preset(agent, preset) is False
    assert "No terminal" in capsys.readouterr().out
    assert pp.choose_model_option(preset, "✏️ custom") is None


def test_provider_files_follow_axon_home(tmp_path, monkeypatch):
    from axon.ui import provider_picker as pp
    home = tmp_path / "custom_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    preset = pp.PROVIDER_PRESETS[0]
    path = pp.save_provider_choice(preset, preset.default_model)
    assert path == home / "config.toml" and path.exists()


# ── clipboard image fallback (Linux tools) ──────────────────────────────────

def test_linux_clipboard_image(monkeypatch, tmp_path):
    from axon.ui import clipboard
    monkeypatch.setattr(clipboard.plat, "IS_WINDOWS", False)
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda n: "/usr/bin/" + n if n == "wl-paste" else None)
    calls = []

    def fake_run(argv, data=None, timeout=3.0):
        calls.append(argv)
        if "--list-types" in argv:
            return SimpleNamespace(returncode=0, stdout=b"text/plain\nimage/png\n")
        return SimpleNamespace(returncode=0, stdout=b"\x89PNG fake")
    monkeypatch.setattr(clipboard, "_run", fake_run)
    dest = tmp_path / "clip.png"
    assert clipboard.paste_image_to(dest) is True
    assert dest.read_bytes() == b"\x89PNG fake"
    assert calls[1][-1] == "image/png"
    monkeypatch.setattr(clipboard, "_run", lambda argv, data=None, timeout=3.0: SimpleNamespace(returncode=0, stdout=b"text/plain"))
    assert clipboard.paste_image_to(tmp_path / "none.png") is False


def test_invisible_characters_warning(monkeypatch):
    from axon.ui.input import PromptChrome, build_frame
    from axon.ui.theme import strip_ansi
    monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    st = EditorState()
    st.set_text("rm ‮exe.txt")
    lines, _ = build_frame(st, PromptChrome())
    assert any("invisible / bidi" in strip_ansi(l) for l in lines)


def test_prompt_color(monkeypatch):
    ui_input.set_prompt_color("default")
    with pytest.raises(ValueError):
        ui_input.set_prompt_color("chartreuse")
    assert ui_input.set_prompt_color("Blue") == "blue"
    ui_input.set_prompt_color("default")
