"""File-change rendering: gap rows, notes, deleted / renamed rows, the approval preview."""
from axon.agent.filechanges import FileChange
from axon.ui import cards, diff_view
from axon.ui.theme import display_width, strip_ansi


def _plain(rows):
    return [strip_ansi(r) for r in rows]


def test_rows_never_exceed_width_and_keep_gutter():
    old = "x = 1\n" * 3 + "def f(): return 'héllo 世界'\n"
    new = "x = 1\n" * 3 + "def f(): return 'héllo 世界 and a very long tail that has to wrap " + "w" * 80 + "'\n"
    for w in (40, 60, 80, 120):
        rows = cards.diff_rows(old, new, 1, w, "python")
        assert rows and all(display_width(r) <= w for r in rows), w


def test_gap_row_between_hunks():
    old = "a\n" + "k\n" * 20 + "z\n"
    new = "A\n" + "k\n" * 20 + "Z\n"
    rows = _plain(cards.diff_rows(old, new, 1, 80))
    assert any("unchanged lines" in r for r in rows)
    assert sum("unchanged" in r for r in rows) == 1


def test_binary_whitespace_and_eof_notes():
    assert "binary file, 3 B" in _plain(cards.diff_rows("a\x00b", "a\x00c", 1, 80))[0]
    assert "whitespace-only change" in "\n".join(_plain(cards.diff_rows("a  b\n", "a b\n", 1, 80)))
    rows = _plain(cards.diff_rows("a\nb", "a\nc\n", 1, 80, eof_notes=True))
    assert any("No newline at end of file" in r for r in rows)
    only = _plain(cards.diff_rows("a\nb", "a\nb\n", 1, 80, eof_notes=True))
    assert only and "newline at end of file" in only[0]


def test_intra_line_highlight_spans():
    old_ch, new_ch = cards._pair_lines(["return a+b"], ["return a + b + c"])
    assert old_ch and new_ch


def test_changed_files_summary_deleted_grouped_and_renamed():
    ch = [FileChange(f"tmp/f{i}.txt", "deleted", None, 2, "ab\n", None) for i in range(4)]
    ch.append(FileChange("docs/hi.tht", "renamed", None, None, "x", "x", old_path="docs/hi.txt"))
    ch.append(FileChange("one.txt", "deleted", None, 3, "x\n" * 3, None))
    rows = _plain(cards.file_change_rows(ch, 80))
    assert rows[0].startswith("✎ 6 files changed")
    assert any("Deleted" in r and "tmp/" in r and "4 files" in r for r in rows)
    assert any("docs/hi.txt → docs/hi.tht" in r and "same content" in r for r in rows)
    assert any("one.txt" in r and "6 B" in r for r in rows)
    assert all(display_width(r) <= 40 for r in _plain(cards.file_change_rows(ch, 40)))


def test_single_file_summary_has_no_repeated_counts():
    rows = _plain(cards.file_change_rows([FileChange("a.py", "created", 3, None, None, "a\nb\nc\n")], 80))
    assert rows[0] == "✎ 1 file changed" and "+3" in rows[1]


def test_approval_preview_matches_card_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "m.py"
    f.write_text("def a():\n    return 1\n")
    rows = _plain(diff_view.approval_rows("Edit", {"path": "m.py", "old_string": "    return 1", "new_string": "    return 2"}, 80, indent=""))
    assert "Edit" in rows[0] and "+1" in rows[0]
    assert any("return 1" in r and "-" in r for r in rows) and any("return 2" in r and "+" in r for r in rows)
    new = _plain(diff_view.approval_rows("Write", {"path": "n.py", "content": "x\n" * 30}, 60, indent=""))
    assert "Create" in new[0] and len(new) <= 1 + diff_view.CREATE_PREVIEW_ROWS + 1
    assert diff_view.approval_rows("Bash", {"command": "ls"}, 80) == []
