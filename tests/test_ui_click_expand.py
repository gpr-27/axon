"""Click-to-expand in the scrolling output: row tags in the screen model, cursor position reports,
click geometry above the prompt frame, SGR click -> transcript entry, and the off switches."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.ui import screen_log
from axon.ui.input import ClickMap
from axon.ui.picker import LiveFrame
from axon.ui.render import TranscriptEntry as E
from axon.ui.screen_log import RowTags, ScreenLog, click_expand_enabled


@pytest.fixture(autouse=True)
def _reset_tag():
    screen_log.set_tag(None)
    ClickMap._misses = 0
    yield
    screen_log.set_tag(None)
    ClickMap._misses = 0


def _feed(tags: RowTags, text: str, tag: int | None = None) -> None:
    screen_log.set_tag(tag)
    tags.feed(text)


def _tags_of(tags: RowTags) -> list[int | None]:
    return [t for t, _w in tags.rows]


# ─── row tags ──────────────────────────────────────────────────────────────

def test_rows_carry_the_tag_that_was_current_when_they_were_written() -> None:
    t = RowTags()
    _feed(t, "banner\n\n")
    _feed(t, "● Bash  ls\n  ⎿ 3 files\n", tag=7)
    _feed(t, "answer\n", tag=8)
    _feed(t, "/cost output\n", tag=None)
    assert _tags_of(t) == [None, None, 7, 7, 8, None]


def test_erased_spinner_rows_leave_no_stale_tags() -> None:
    t = RowTags()
    _feed(t, "\r\x1b[2K⠋ thinking\n\x1b[2KTip: x", tag=99)          # spinner + hint row
    _feed(t, "\r\x1b[2K\x1b[A\r\x1b[2K", tag=None)                     # erased again
    _feed(t, "● Read  a.py\n", tag=5)
    assert _tags_of(t) == [5]


def test_cursor_up_redraw_replaces_rows_and_keeps_untouched_ones() -> None:
    t = RowTags()
    _feed(t, "one\ntwo\nthree\n", tag=3)
    _feed(t, "\x1b[2A\r\x1b[J", tag=None)                              # a frame redraw from two rows up
    _feed(t, "frame row\n")
    assert _tags_of(t) == [3, None]
    _feed(t, "\x1b[A\n", tag=None)                                       # moved over a row without writing
    assert _tags_of(t) == [3, None]


def test_full_clear_and_alternate_screen() -> None:
    t = RowTags()
    _feed(t, "a\nb\n", tag=1)
    _feed(t, "\x1b[?1049h\x1b[Hviewer\nrows\n\x1b[?1049l", tag=2)       # the alt screen is not the main screen
    assert _tags_of(t) == [1, 1]
    _feed(t, "\x1b[3J\x1b[H\x1b[2J", tag=None)
    assert _tags_of(t) == []


def test_tags_are_bounded() -> None:
    t = RowTags(keep=10)
    _feed(t, "x\n" * 25, tag=4)
    assert len(t.rows) == 10


def test_screen_log_feeds_tags_even_while_paused_and_without_a_file(tmp_path) -> None:
    real = io.StringIO()
    tags = RowTags()
    log = ScreenLog(real, None, tags=tags)
    screen_log.set_tag(6)
    log.write("tool row\n")
    log.pause()
    screen_log.set_tag(None)
    log.write("prompt frame\n")
    log.resume()
    log.close()
    assert _tags_of(tags) == [6, None] and real.getvalue() == "tool row\nprompt frame\n"
    with_file = ScreenLog(io.StringIO(), tmp_path / "s.log", tags=RowTags())
    with_file.write("kept\n")
    with_file.close()
    assert "kept" in (tmp_path / "s.log").read_text()


def test_wide_rows_count_as_several_screen_rows() -> None:
    t = RowTags()
    _feed(t, "x" * 250 + "\n", tag=1)
    _feed(t, "short\n", tag=2)
    assert t.tag_at(1, cols=100) == 2
    assert [t.tag_at(i, cols=100) for i in (2, 3, 4)] == [1, 1, 1]
    assert t.tag_at(5, cols=100) is None


def test_renderer_records_tag_rows_with_entry_uids() -> None:
    from axon.ui.render import _record, clear_transcript
    a, b = E("tool", 1), E("tool", 1)
    assert a.uid != b.uid and a.uid > 0
    try:
        _record(a)
        assert screen_log.current_tag == a.uid
    finally:
        clear_transcript()


# ─── cursor position report ────────────────────────────────────────────────

def _key(seq: str) -> str:
    it = iter(seq[1:])
    return plat._decode_sequence(seq[0], lambda _t: next(it, None))


def test_cpr_is_decoded_and_never_typed() -> None:
    assert _key("\x1b[12;1R") == "cpr:12:1"
    assert plat.parse_cpr("cpr:12:1") == (12, 1)
    assert plat.parse_cpr("up") is None and plat.parse_cpr("cpr:x") is None
    assert _key("\x1b[A") == "up" and _key("\x1b[<0;3;4M") == "mouse:0:3:4:M"


def test_query_cursor_position_keeps_type_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = iter(["a", "b", "cpr:9:3"])
    written: list[str] = []
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "_write_raw", written.append)
    monkeypatch.setattr(plat, "read_key", lambda timeout=None, report_resize=True: next(keys, None))
    plat._events.clear()
    assert plat.query_cursor_position(0.5) == (9, 3)
    assert written == ["\x1b[6n"] and list(plat._events) == ["a", "b"]
    plat._events.clear()


def test_query_cursor_position_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "_write_raw", lambda s: None)
    monkeypatch.setattr(plat, "read_key", lambda timeout=None, report_resize=True: None)
    assert plat.query_cursor_position(0.05) is None


# ─── geometry ──────────────────────────────────────────────────────────────

class _Screen:
    """A ScreenLog over a StringIO as sys.stdout, so LiveFrame's real output drives the row model."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, cols: int = 100, rows: int = 10) -> None:
        self.tags = RowTags()
        self.log = ScreenLog(io.StringIO(), None, tags=self.tags)
        self.size = (cols, rows)
        monkeypatch.setattr("sys.stdout", self.log)
        monkeypatch.setattr(plat, "term_size", lambda: self.size)
        self.mouse: list[bool] = []
        monkeypatch.setattr(plat, "set_mouse_reporting", lambda on: self.mouse.append(on))

    def print(self, text: str, tag: int | None = None) -> None:
        screen_log.set_tag(tag)
        self.log.write(text)
        screen_log.set_tag(None)


def _prompt(scr: _Screen, monkeypatch: pytest.MonkeyPatch, frame_lines: int, cursor_row: int, cpr_row: int):
    scr.log.write("\n")                                  # read_input's newline before the frame
    frame = LiveFrame()
    frame.render([f"frame {i}" for i in range(frame_lines)], cursor=(cursor_row, 2))
    monkeypatch.setattr(plat, "query_cursor_position", lambda timeout=0.15: (cpr_row, 3))
    cm = ClickMap(scr.tags)
    assert cm.measure(frame)
    return cm, frame


def test_geometry_on_a_screen_that_is_not_full(monkeypatch: pytest.MonkeyPatch) -> None:
    scr = _Screen(monkeypatch)
    scr.print("banner 1\nbanner 2\n")                   # rows 1-2
    scr.print("● Bash  ls\n  ⎿ out\n", tag=7)           # rows 3-4
    # row 5: blank (newline before the frame); frame rows 6-8, cursor on frame row 1 -> screen row 7
    cm, _frame = _prompt(scr, monkeypatch, frame_lines=3, cursor_row=1, cpr_row=7)
    assert cm.frame_top == 6 and scr.mouse[-1] is True
    assert [cm.entry_at(y) for y in range(1, 9)] == [None, None, 7, 7, None, None, None, None]


def test_geometry_when_the_output_has_scrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    scr = _Screen(monkeypatch, rows=10)
    scr.print("".join(f"old {i}\n" for i in range(30)))
    scr.print("● Read  a.py\n", tag=3)
    scr.print("answer line\n", tag=4)
    # 10-row window: frame of 3 at rows 8-10 (cursor on its last row), blank row 7, answer 6, Read 5
    cm, _frame = _prompt(scr, monkeypatch, frame_lines=3, cursor_row=2, cpr_row=10)
    assert cm.frame_top == 8
    assert [cm.entry_at(y) for y in (4, 5, 6, 7, 8)] == [None, 3, 4, None, None]


def test_a_taller_frame_near_the_bottom_shifts_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    scr = _Screen(monkeypatch, rows=10)
    scr.print("".join(f"old {i}\n" for i in range(30)))
    scr.print("● Bash  make\n", tag=9)
    cm, frame = _prompt(scr, monkeypatch, frame_lines=3, cursor_row=0, cpr_row=8)
    assert cm.frame_top == 8 and cm.entry_at(6) == 9
    frame.render([f"frame {i}" for i in range(6)], cursor=(0, 2))   # completion popup: 3 more rows
    cm.update(frame)
    assert cm.frame_top == 5 and cm.entry_at(3) == 9 and cm.entry_at(6) is None
    frame.render(["frame 0", "frame 1"], cursor=(0, 2))             # shrinking never moves rows back down
    cm.update(frame)
    assert cm.frame_top == 5 and cm.entry_at(3) == 9


def test_no_cursor_report_means_no_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
    scr = _Screen(monkeypatch)
    scr.print("● Bash  ls\n", tag=1)
    monkeypatch.setattr(plat, "query_cursor_position", lambda timeout=0.15: None)
    cm = ClickMap(scr.tags)
    frame = LiveFrame()
    frame.render(["frame"])
    assert not cm.measure(frame) and scr.mouse[-1] is False
    assert cm.entry_at(1) is None and cm.target("mouse:0:1:1:M") is None
    cm.measure(frame)
    asked: list[int] = []
    monkeypatch.setattr(plat, "query_cursor_position", lambda timeout=0.15: asked.append(1))
    cm.measure(frame)                                    # gave up after two misses: no more 150 ms waits
    assert asked == []


# ─── clicks ────────────────────────────────────────────────────────────────

def test_sgr_click_selects_the_entry_and_wheel_up_opens_history(monkeypatch: pytest.MonkeyPatch) -> None:
    scr = _Screen(monkeypatch)
    scr.print("● Bash  ls\n", tag=11)                   # row 1
    cm, _frame = _prompt(scr, monkeypatch, frame_lines=2, cursor_row=0, cpr_row=3)
    assert cm.target("mouse:0:4:1:M") == 11
    assert cm.target("mouse:0:4:1:m") is None            # release
    assert cm.target("mouse:2:4:1:M") is None            # right button
    assert cm.target("mouse:0:4:3:M") is None            # the prompt frame itself
    assert cm.target("mouse:64:4:1:M") == ClickMap.WHEEL_UP
    assert cm.target("mouse:65:4:1:M") is None           # wheel down at the prompt: nothing
    assert cm.target("cpr:3:1") is None                  # a late cursor report is ignored


def test_viewer_focus_selects_and_expands_the_clicked_entry() -> None:
    from axon.ui.transcript_view import TranscriptViewer
    es = [E("turn", 1, title="Turn 1"), E("step", 1, 1, title="Step 1"),
          E("tool", 1, 1, title="Bash  ls", body="\n".join(f"l{i}" for i in range(50)), detail="{}"),
          E("text", 1, 1, title="Response", body="done")]
    v = TranscriptViewer(es, width=80, height=12, clock=lambda: 0.0)
    v.toggle_turns()                                     # even inside a folded turn
    assert v.focus(es[2].uid, expand=True)
    assert v.sel == id(es[2]) and id(es[2]) in v.expanded and v.top == v.header_index(id(es[2]))
    v.handle_key(f"mouse:0:3:{v.header_index(id(es[2])) - v.top + 2}:M")  # click again: collapse
    assert id(es[2]) not in v.expanded
    assert not v.focus(123456789)


# ─── off switches ──────────────────────────────────────────────────────────

def test_setting_and_env_turn_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXON_NO_MOUSE", raising=False)
    on = SimpleNamespace(mouse_click_expand=True)
    off = SimpleNamespace(mouse_click_expand=False)
    if plat.IS_WINDOWS:
        assert not click_expand_enabled(on)
        return
    assert click_expand_enabled(on)
    assert not click_expand_enabled(off) and not click_expand_enabled(None)   # opt-in: the wheel scrolls by default
    monkeypatch.setenv("AXON_MOUSE", "1")
    assert click_expand_enabled(None) and click_expand_enabled(off)
    monkeypatch.delenv("AXON_MOUSE")
    monkeypatch.setenv("AXON_NO_MOUSE", "1")
    assert not click_expand_enabled(on)


def test_no_click_map_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout", ScreenLog(io.StringIO(), None, tags=RowTags()))
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.delenv("AXON_NO_MOUSE", raising=False)
    agent_off = SimpleNamespace(settings=SimpleNamespace(mouse_click_expand=False))
    assert ClickMap.for_prompt(agent_off) is None
    monkeypatch.setenv("AXON_NO_MOUSE", "1")
    assert ClickMap.for_prompt(SimpleNamespace(settings=SimpleNamespace(mouse_click_expand=True))) is None
    monkeypatch.setattr("sys.stdout", io.StringIO())     # no screen model: nothing to map
    monkeypatch.delenv("AXON_NO_MOUSE", raising=False)
    assert ClickMap.for_prompt(None) is None


def test_mouse_reporting_is_written_once_and_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []
    monkeypatch.setattr(plat, "_write_raw", written.append)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "_mouse_on", False)
    plat.set_mouse_reporting(True)
    plat.set_mouse_reporting(True)
    assert plat.mouse_reporting() and written == [plat.ENABLE_MOUSE]
    plat.set_mouse_reporting(False)
    assert written == [plat.ENABLE_MOUSE, plat.DISABLE_MOUSE] and not plat.mouse_reporting()
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    plat.set_mouse_reporting(True)                       # Windows: keyboard only
    assert not plat.mouse_reporting()


def test_config_setting_exists_and_maps_from_settings_json() -> None:
    from axon.config import _JSON_KEY_MAP, Settings
    assert Settings.model_fields["mouse_click_expand"].default is False   # native scrolling / selection first
    assert _JSON_KEY_MAP["mouseClickExpand"] == "mouse_click_expand"


def test_redrawn_history_rows_keep_their_transcript_tags():
    """After a resize redraw, tool rows still map to their transcript entry (clickable)."""
    from axon.ui import render
    render.clear_transcript()
    entry = render._record(render.TranscriptEntry("tool", 1, 1, title="Bash  ls", id="t1"))
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a\nb"}]}]
    tags: list = []
    rows = render.format_conversation_replay(msgs, width=80, tag="", row_tags=tags)
    assert len(tags) == len(rows)
    bash_rows = [i for i, r in enumerate(rows) if "Bash" in render.strip_ansi(r)]
    assert bash_rows and all(tags[i] == entry.uid for i in bash_rows)
    assert tags[0] is None                       # the prompt banner is not a tool block
    render.clear_transcript()
