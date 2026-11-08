"""Dropped files and images are saved under ~/.axon at once, so nothing depends on the original staying put."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from axon import platform as plat
from axon import storage
from axon.agent import attachments, images
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry
from axon.ui.input import EditorState

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / ".axon").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("AXON_HOME", str(h / ".axon"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    storage.set_active_session(ws, "sess_a")
    attachments.reset_for_tests()
    return h


@pytest.fixture
def report(tmp_path: Path) -> Path:
    d = tmp_path / "Downloads folder"
    d.mkdir()
    f = d / "Q3 report.pdf"
    f.write_bytes(b"%PDF-1.7 hello")
    return f


def _escaped(p: Path) -> str:
    # A backslash is a path separator on Windows, so a path with spaces arrives quoted there.
    return f'"{p}"' if os.name == "nt" else str(p).replace(" ", "\\ ")


@pytest.mark.parametrize("form", [_escaped, lambda p: f"'{p}'", lambda p: f'"{p}"', lambda p: p.as_uri()])
def test_every_drop_format_is_recognised(home: Path, report: Path, form):
    assert attachments.parse_dropped_paths(form(report)) == [report]


def test_several_files_in_one_drop(home: Path, report: Path, tmp_path: Path):
    other = tmp_path / "notes.txt"
    other.write_text("x")
    assert attachments.parse_dropped_paths(f"{_escaped(report)} {other}") == [report, other]


@pytest.mark.parametrize("text", ["please read /etc/hosts", "hello world", "/no/such/file.pdf", "", "a\nb\nc"])
def test_ordinary_paste_is_not_a_drop(home: Path, text: str):
    assert attachments.parse_dropped_paths(text) == []


def test_drop_stores_a_copy_and_inserts_a_chip(home: Path, report: Path):
    ed = EditorState()
    ed.insert_paste(_escaped(report))
    assert ed.buf.startswith("[File #1: Q3 report.pdf]")
    att = attachments.get(1)
    assert att.stored_path.read_bytes() == report.read_bytes()
    assert str(home / ".axon") in str(att.stored_path)
    assert att.stored_path.parent.name == "attachments"


def test_original_may_vanish_after_the_drop(home: Path, report: Path):
    ed = EditorState()
    ed.insert_paste(_escaped(report))
    report.unlink()
    expanded = attachments.expand_file_chips(ed.buf)
    stored = attachments.get(1).stored_path
    assert str(stored) in expanded and stored.exists()


def test_submitted_text_tells_the_model_where_the_file_is(home: Path, report: Path):
    ed = EditorState()
    ed.insert_paste(_escaped(report))
    ed.insert(" summarise this")
    out = attachments.expand_file_chips(ed.buf)
    assert "Q3 report.pdf" in out and "summarise this" in out and "[File #1:" not in out.replace("[File #1: Q3 report.pdf,", "")


def test_stored_file_is_readable_without_a_prompt(home: Path, report: Path, tmp_path: Path):
    att = attachments.ingest_file(report)
    engine = PermissionEngine(Settings(workspace=tmp_path / "ws"))
    decision = engine.check(create_default_registry().get("Read"), {"file_path": str(att.stored_path)})
    assert decision.outcome == "allow"


def test_image_drop_is_saved_at_once(home: Path, tmp_path: Path):
    shot = tmp_path / "Screenshot 1.png"
    shot.write_bytes(PNG)
    ed = EditorState()
    ed.insert_paste(_escaped(shot))
    assert "[Image #" in ed.buf
    label = ed.buf.strip()
    shot.unlink()
    text, atts = images.compact_image_paths(label)
    assert atts and atts[0].cached_path.exists()


def test_same_content_is_stored_once(home: Path, report: Path):
    a = attachments.ingest_file(report)
    b = attachments.ingest_file(report)
    assert a.stored_path == b.stored_path


@pytest.mark.parametrize("name", [".env", ".ssh/id_rsa", ".aws/credentials"])
def test_credentials_are_never_copied(home: Path, name: str, monkeypatch: pytest.MonkeyPatch):
    f = home / ".axon" / name if name == ".env" else home / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("SECRET=1")
    ed = EditorState()
    ed.insert_paste(str(f))
    assert "File #" not in ed.buf and "protected" in ed.notice.lower()
    assert not list((home / ".axon").rglob("attachments/*"))


def test_too_large_is_referenced_not_lost(home: Path, report: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(attachments, "MAX_STORED_BYTES", 4)
    att = attachments.ingest_file(report)
    assert att.stored_path == report and att.by_reference
    assert str(report) in attachments.expand_file_chips(att.chip)


def test_a_directory_drop_stays_a_path(home: Path, report: Path):
    ed = EditorState()
    ed.insert_paste(str(report.parent))
    assert ed.buf == str(report.parent)


def test_manifest_lists_what_was_kept(home: Path, report: Path):
    attachments.ingest_file(report)
    rows = attachments.list_session_attachments()
    assert rows[0]["name"] == "Q3 report.pdf" and rows[0]["original"] == str(report)
    assert rows[0]["sha256"] and Path(rows[0]["stored"]).exists()


def test_chip_resolves_after_a_restart(home: Path, report: Path):
    att = attachments.ingest_file(report)
    chip = att.chip
    attachments.reset_for_tests()
    assert str(att.stored_path) in attachments.expand_file_chips(chip)


def test_images_are_listed_in_the_manifest_too(home: Path, tmp_path: Path):
    shot = tmp_path / "a.png"
    shot.write_bytes(PNG)
    images.ingest_image_file(str(shot))
    assert any(r["kind"] == "image" and r["name"] == "a.png" for r in attachments.list_session_attachments())


def test_prompt_history_pastes_are_not_in_the_disposable_cache(home: Path):
    pid = storage.store_paste("large paste")
    assert (home / ".axon" / "pastes" / f"{pid}.txt").exists()
    assert not (home / ".axon" / "cache" / "pastes" / f"{pid}.txt").exists()
    assert storage.load_paste(pid) == "large paste"


def test_old_pastes_in_the_cache_still_load(home: Path):
    legacy = home / ".axon" / "cache" / "pastes"
    legacy.mkdir(parents=True)
    (legacy / "0123456789abcdef.txt").write_text("old")
    assert storage.load_paste("0123456789abcdef") == "old"


def test_split_paste_keeps_one_session_copy(home: Path, tmp_path: Path):
    ed = EditorState()
    ed.insert_paste("a\n" * 2000)
    ed.insert_paste("b\n" * 2000)
    kept = list(storage.active_session_subdir("pastes").glob("*.txt"))
    assert len(kept) == 1 and kept[0].read_text() == "a\n" * 2000 + "b\n" * 2000


# ── the paste reader ─────────────────────────────────────────────────────────

def _feed(text: str, clock: list[float] | None = None, step: float = 0.0):
    chars = iter(text)

    def getc(_timeout: float):
        if clock is not None:
            clock[0] += step
        return next(chars, None)
    return getc


def test_a_slow_paste_is_never_cut_off(monkeypatch: pytest.MonkeyPatch):
    clock = [0.0]
    monkeypatch.setattr(plat.time, "monotonic", lambda: clock[0])
    body = "line\n" * 100_000
    got = plat._read_bracketed_paste(_feed(body + plat.PASTE_END, clock, step=0.01))   # 5,000 seconds of data
    assert got == body


def test_a_terminal_that_goes_silent_ends_the_paste():
    assert plat._read_bracketed_paste(_feed("partial")) == "partial"


def test_the_end_marker_is_removed():
    assert plat._read_bracketed_paste(_feed("abc" + plat.PASTE_END + "next")) == "abc"


def test_paste_reader_handles_a_million_lines_quickly():
    import time
    body = "x\n" * 1_000_000
    start = time.perf_counter()
    assert plat._read_bracketed_paste(_feed(body + plat.PASTE_END)) == body
    assert time.perf_counter() - start < 5


def test_manifest_is_valid_json_lines(home: Path, report: Path):
    att = attachments.ingest_file(report)
    index = att.stored_path.parent / "index.jsonl"
    assert json.loads(index.read_text().splitlines()[0])["name"] == "Q3 report.pdf"


def test_files_command_lists_what_was_kept(home: Path, report: Path, capsys: pytest.CaptureFixture[str]):
    from axon.commands.session_cmds import handle_files
    attachments.ingest_file(report)
    handle_files(object(), "")
    out = capsys.readouterr().out
    assert "Q3 report.pdf" in out and "attachments" in out


def test_files_command_when_empty(home: Path, capsys: pytest.CaptureFixture[str]):
    from axon.commands.session_cmds import handle_files
    handle_files(object(), "")
    assert "Nothing attached yet" in capsys.readouterr().out
