"""The full-history view is drawn from the transcript file, so earlier turns never lose their thinking,
tool calls or outputs, even after the model's context was compacted or old outputs were evicted."""
from __future__ import annotations

from pathlib import Path

from axon.session.store import SessionStore
from axon.ui.history_view import format_session_history
from axon.ui.theme import strip_ansi


def _session(tmp_path: Path) -> Path:
    store = SessionStore(tmp_path, session_dir=tmp_path / "sessions")
    sid = store.open(None)
    store.append("user_message", {"role": "user", "content": "find the handler"})
    store.append("assistant_turn", {"stop_reason": "tool_use", "text": "Let me search.", "thinking": "The handler is probably in app.py.",
                                    "tool_uses": [{"id": "t1", "name": "Grep", "input": {"pattern": "def handle"}}],
                                    "model": "deepseek-v4-flash", "cost": 0.01, "duration_s": 1.5,
                                    "usage": {"input": 9000, "output": 40}})
    store.append("tool_results", [{"tool_use_id": "t1", "content": "src/app.py:12:def handle(x):", "is_error": False,
                                   "duration_s": 0.02}])
    store.append("assistant_turn", {"stop_reason": "end_turn", "text": "It is in **app.py**.", "thinking": "",
                                    "tool_uses": [], "model": "deepseek-v4-flash", "cost": 0.005, "duration_s": 0.8,
                                    "usage": {"input": 9100, "output": 12}})
    # the model's context is compacted: the earlier turn survives only as a summary for the model
    store.append("compaction", {"messages": [{"role": "user", "content": "Summary of earlier conversation"}]})
    store.append("user_message", {"role": "user", "content": "now run the tests"})
    store.append("assistant_turn", {"stop_reason": "end_turn", "text": "All 5 tests pass.", "thinking": "",
                                    "tool_uses": [], "model": "deepseek-v4-flash", "cost": 0.004, "duration_s": 0.5,
                                    "usage": {"input": 300, "output": 8}})
    return Path(store._file(sid))


def test_earlier_turns_keep_thinking_tool_calls_and_outputs_after_compaction(tmp_path: Path) -> None:
    out = strip_ansi("\n".join(format_session_history(_session(tmp_path), width=90)))
    assert "Turn 1" in out and "Turn 2" in out
    assert "The handler is probably in app.py." in out          # thinking of an old turn
    assert "● Grep" in out and "src/app.py" in out and "def handle(x)" in out   # its tool call and output
    assert "▎ It is in app.py." in out and "▎ All 5 tests pass." in out
    assert "context compacted" in out                            # the compaction is shown, nothing hidden
    assert "replied in 1.5s" in out and "deepseek-v4-flash" in out


def test_row_tags_follow_tool_blocks(tmp_path: Path) -> None:
    tags: list = []
    rows = format_session_history(_session(tmp_path), width=90, row_tags=tags, tool_tags={"t1": 42})
    assert len(tags) == len(rows)
    grep_rows = [i for i, r in enumerate(rows) if "● Grep" in strip_ansi(r)]
    assert grep_rows and tags[grep_rows[0]] == 42
