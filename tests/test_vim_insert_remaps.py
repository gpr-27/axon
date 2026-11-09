"""vimInsertModeRemaps: a two-key sequence typed in vim INSERT mode acts as Esc, and only you can set it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.config import Settings, map_json_settings
from axon.ui import vim as vim_mod
from axon.ui.input import EditorState, PromptEditor
from axon.ui.vim import INSERT, NORMAL, VimState, set_insert_remaps


@pytest.fixture(autouse=True)
def _clear():
    set_insert_remaps(None)
    yield
    set_insert_remaps(None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(vim_mod.time, "monotonic", lambda: now[0])
    return now


def _editor(text: str = "") -> tuple[EditorState, PromptEditor]:
    st = EditorState()
    st.vim = VimState()
    if text:
        st.set_text(text)
    return st, PromptEditor(st)


def _type(ed: PromptEditor, keys: str) -> None:
    for k in keys:
        assert ed.handle(k) is None


@pytest.mark.parametrize("mapping, expected", [
    ({"jk": "<Esc>"}, {"jk"}), ({"jj": "<Esc>", "kj": "<Esc>"}, {"jj", "kj"}),
    ({"j": "<Esc>", "jkl": "<Esc>", "": "<Esc>"}, set()),      # exactly two characters
    ({"jk": "<CR>", "kk": "esc", "jj": None}, set()),          # <Esc> is the only target
    ({"j\t": "<Esc>"}, set()),                                 # printable only
    (None, set()), ("jk", set()), (["jk"], set()),
])
def test_only_valid_entries_count(mapping, expected) -> None:
    assert set(set_insert_remaps(mapping)) == expected


def test_sequence_leaves_insert_and_removes_both_keys(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor()
    _type(ed, "hello j")
    assert st.vim.mode == INSERT and st.buf == "hello j"        # the first key goes in as usual
    clock[0] += 0.3
    _type(ed, "k")
    assert st.vim.mode == NORMAL and st.buf == "hello " and st.pos == 5
    _type(ed, "x")                                              # really NORMAL: x deletes under the cursor
    assert st.buf == "hello"


def test_same_key_twice(clock) -> None:
    set_insert_remaps({"jj": "<Esc>"})
    st, ed = _editor()
    _type(ed, "ajj")
    assert st.vim.mode == NORMAL and st.buf == "a"


def test_too_slow_stays_literal(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor()
    _type(ed, "j")
    clock[0] += 1.01
    _type(ed, "k")
    assert st.vim.mode == INSERT and st.buf == "jk"


def test_exactly_one_second_still_counts(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor()
    _type(ed, "j")
    clock[0] += 1.0
    _type(ed, "k")
    assert st.vim.mode == NORMAL and st.buf == ""


def test_another_key_between_keeps_all(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor()
    _type(ed, "jxk")
    assert st.vim.mode == INSERT and st.buf == "jxk"
    _type(ed, "jjk")                                            # the second j starts over
    assert st.vim.mode == NORMAL and st.buf == "jxkj"


def test_moving_or_pasting_between_keeps_all(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor()
    _type(ed, "j")
    ed.handle("left")
    _type(ed, "k")
    assert st.vim.mode == INSERT and st.buf == "kj"


def test_normal_mode_keys_are_untouched(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor("one\ntwo")
    ed.handle("esc")
    st.pos = 0
    _type(ed, "jk")                                             # motions, not a sequence
    assert st.vim.mode == NORMAL and st.buf == "one\ntwo"


def test_undo_after_the_sequence(clock) -> None:
    set_insert_remaps({"jk": "<Esc>"})
    st, ed = _editor("base")
    st.vim.to_normal(st)
    _type(ed, "A")                                              # a new insert session
    _type(ed, "xjk")
    assert st.vim.mode == NORMAL and st.buf == "basex"
    _type(ed, "u")
    assert st.buf == "base"


def test_without_a_setting_nothing_changes(clock) -> None:
    st, ed = _editor()
    _type(ed, "jk")
    assert st.vim.mode == INSERT and st.buf == "jk"


# ─── where the setting may come from ───────────────────────────────────────

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _load(ws: Path, **kw) -> Settings:
    return Settings.load({"workspace": ws, **kw}, require_api_key=False)


def test_read_from_user_settings(tmp_path: Path) -> None:
    from axon.storage import axon_home
    _write(axon_home() / "settings.json", {"vimInsertModeRemaps": {"jk": "<Esc>"}})
    assert _load(tmp_path).vim_insert_mode_remaps == {"jk": "<Esc>"}


def test_read_from_the_settings_flag(tmp_path: Path) -> None:
    assert _load(tmp_path, settings_file='{"vimInsertModeRemaps": {"kj": "<Esc>"}}').vim_insert_mode_remaps == {"kj": "<Esc>"}


@pytest.mark.parametrize("rel", [".axon/settings.json", ".axon/settings.local.json", ".claude/settings.json", ".claude/settings.local.json"])
def test_a_repository_cannot_set_it_in_json(tmp_path: Path, rel: str, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.storage import axon_home
    _write(axon_home() / "settings.json", {"vimInsertModeRemaps": {"jk": "<Esc>"}})
    _write(tmp_path / rel, {"vimInsertModeRemaps": {"ab": "<Esc>"}})
    assert _load(tmp_path).vim_insert_mode_remaps == {"jk": "<Esc>"}
    assert "vim_insert_mode_remaps" in capsys.readouterr().err


def test_a_repository_cannot_set_it_in_toml_or_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".axon").mkdir()
    (tmp_path / ".axon" / "config.toml").write_text('[vim_insert_mode_remaps]\nab = "<Esc>"\n', encoding="utf-8")
    (tmp_path / ".env").write_text('AXON_VIM_INSERT_MODE_REMAPS={"cd": "<Esc>"}\n', encoding="utf-8")
    monkeypatch.delenv("AXON_VIM_INSERT_MODE_REMAPS", raising=False)
    assert _load(tmp_path).vim_insert_mode_remaps is None


def test_a_bad_value_is_ignored_not_fatal() -> None:
    assert "vim_insert_mode_remaps" not in map_json_settings({"vimInsertModeRemaps": "jk"})
