"""A one-line dropped path that arrives as keys (no bracketed paste) is ingested like a drop; a path in a sentence is not."""
from __future__ import annotations

import os
from pathlib import Path

from axon import platform as plat
from axon.agent.attachments import looks_like_typed_drop


def test_a_whole_path_to_a_file_outside_the_workspace_is_a_drop(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "report.pdf"
    outside.write_text("x")
    assert looks_like_typed_drop(str(outside), ws)
    assert looks_like_typed_drop(f"'{outside}' ", ws)                       # shell-quoted, trailing space
    assert looks_like_typed_drop(str(outside).replace(" ", "\\ "), ws)


def test_not_a_drop(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inside = ws / "a.py"
    inside.write_text("x")
    outside = tmp_path / "b.txt"
    outside.write_text("x")
    assert not looks_like_typed_drop(str(inside), ws)                       # in the workspace: @-mention territory
    assert not looks_like_typed_drop(f"please read {outside}", ws)          # mentioned in a sentence
    assert not looks_like_typed_drop(str(tmp_path), ws)                     # a folder
    assert not looks_like_typed_drop(str(tmp_path / "missing.txt"), ws)
    assert not looks_like_typed_drop("\x1b[200~" + str(outside), ws)
    assert not looks_like_typed_drop(str(outside) + "\r", ws)               # carries its own Enter: normal submit


def test_posix_fill_turns_a_dropped_path_burst_into_a_paste_event(tmp_path: Path, monkeypatch) -> None:
    outside = tmp_path / "doc.txt"
    outside.write_text("hello")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    r, w = os.pipe()
    monkeypatch.setattr(plat, "_stdin_fd", lambda: r)
    plat._events.clear()
    plat._pending_chars.clear()
    os.write(w, str(outside).encode())
    assert plat._posix_fill(0.5)
    assert list(plat._events) == [plat.PASTE_PREFIX + str(outside)] and not plat._pending_chars
    plat._events.clear()
    os.write(w, b"look at " + str(outside).encode())
    assert plat._posix_fill(0.5)
    assert not plat._events and "".join(plat._pending_chars).startswith("look at ")
    plat._pending_chars.clear()
    os.close(r)
    os.close(w)
