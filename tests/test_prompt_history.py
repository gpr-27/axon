"""One prompt history per project folder: a counter while browsing, and pasted text recalled as chips."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from axon import storage
from axon.ui import input as ui_input
from axon.ui.input import EditorState, PromptChrome, add_history, build_frame, history_entries, load_history

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(lines):
    return [ANSI.sub("", line) for line in lines]


@pytest.fixture(autouse=True)
def _clean_session_history():
    ui_input._SESSION_HISTORY.clear()
    ui_input._SESSION_PASTES.clear()
    yield
    ui_input._SESSION_HISTORY.clear()
    ui_input._SESSION_PASTES.clear()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_counter_runs_oldest_first_and_shows_in_the_top_line():
    st = EditorState(history=[f"prompt {i}" for i in range(1, 101)])
    for _ in range(11):
        st.history_step(-1)
    assert st.buf == "prompt 90" and st.history_position() == (90, 100)
    top = _plain(build_frame(st, PromptChrome())[0])[0]
    assert "History 90/100" in top and top.strip().startswith("──")


def test_no_counter_when_not_browsing():
    st = EditorState(history=["a", "b"])
    assert st.history_position() is None
    assert "History" not in _plain(build_frame(st, PromptChrome())[0])[0]
    st.history_step(-1)
    st.history_step(1)                       # back to the draft
    assert st.history_position() is None


def test_one_history_across_chats_and_subfolders(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    add_history("from chat one", "session_1")
    monkeypatch.chdir(root / "pkg")
    add_history("from chat two, started in a subfolder", "session_2")
    ui_input._SESSION_HISTORY.clear()        # a fresh process: only the file remains
    for cwd in (root, root / "pkg"):
        assert load_history(cwd=str(cwd)) == ["from chat one", "from chat two, started in a subfolder"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert load_history(cwd=str(other)) == []


def test_recalled_prompt_brings_its_paste_chip_back(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    big = "".join(f"line {i}\n" for i in range(300))
    st = EditorState()
    st.insert_paste(big)
    chip = st.buf
    assert chip.startswith("[Pasted text #1 +")
    add_history(chip + " why is this slow?", "s1", {1: big})
    record = json.loads(storage.user_file("history.jsonl").read_text().splitlines()[-1])
    assert record["display"].startswith("[Pasted text #1") and big not in json.dumps(record)   # stored once, elsewhere
    ui_input._SESSION_HISTORY.clear()
    ui_input._SESSION_PASTES.clear()
    entries, pastes = history_entries(cwd=str(root))
    fresh = EditorState(history=entries, history_pastes=pastes)
    fresh.history_step(-1)
    assert fresh.buf == chip + " why is this slow?"
    assert fresh.expanded() == big + " why is this slow?"          # the chip expands to the full text again


def test_going_back_to_the_draft_restores_the_drafts_own_pastes():
    big = "x\n" * 200
    st = EditorState()
    st.insert_paste(big)
    draft, draft_pastes = st.buf, dict(st.pastes)
    st.history = [draft + " older question"]          # Up only finds entries that start with what is typed
    st.history_step(-1)
    assert st.buf == draft + " older question" and st.pastes == {}    # that entry stored no pastes
    st.history_step(1)
    assert st.buf == draft and st.pastes == draft_pastes


def test_a_git_repo_in_the_home_folder_is_not_one_big_project(monkeypatch):
    """~ is a git repo here: the folders below it must still keep separate histories."""
    home = Path.home()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    own_repo = home / "Documents" / "axon"
    own_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(own_repo)], check=True)
    plain = home / "Documents" / "Important projects"
    plain.mkdir(parents=True)
    monkeypatch.chdir(own_repo)
    add_history("typed in the axon repo", "s1")
    monkeypatch.chdir(plain)
    add_history("typed in the plain folder", "s2")
    ui_input._SESSION_HISTORY.clear()
    assert load_history(cwd=str(own_repo)) == ["typed in the axon repo"]
    assert load_history(cwd=str(plain)) == ["typed in the plain folder"]
    assert load_history(cwd=str(plain / "deeper")) == []          # a new folder starts empty, it does not inherit ~
    assert storage.history_project(plain) == plain
    assert storage.history_project(own_repo / "src") == own_repo


def test_old_records_written_with_a_subfolder_are_matched_to_their_repo(tmp_path: Path):
    root = _project(tmp_path)
    storage.append_jsonl(storage.user_file("history.jsonl"),
                         {"ts": 1, "project": str(root / "pkg"), "session": "old", "display": "from an old subfolder run"})
    assert load_history(cwd=str(root)) == ["from an old subfolder run"]
    other = tmp_path / "other"
    other.mkdir()
    assert load_history(cwd=str(other)) == []
