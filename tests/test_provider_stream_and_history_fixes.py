"""
Regressions from the provider review:

* an Anthropic `event: error` inside a 200 stream (overloaded_error) is retried and reported with the status
  its type stands for, not as "HTTP 200",
* tool_result blocks whose tool_use is not in the turn just before them are sent as text (the Messages API
  rejects them), and a duplicate result for one call is dropped,
* dotenv values with a trailing `# comment` load without the comment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.config import _parse_dotenv, dotenv_value
from axon.errors import ProviderError
from axon.providers import transport
from axon.providers.anthropic import stream_error_status, to_anthropic_messages
from axon.providers.base import TextDelta
from test_providers_layer import ANTHROPIC_STREAM, _anthropic_httpx, _anthropic_sse, anthropic_provider


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_sleep", lambda s: None)


OVERLOADED = [
    ANTHROPIC_STREAM[0],
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
]
TEXT_ONLY = [
    ANTHROPIC_STREAM[0],
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
]


def _streams(*bodies: list[dict]):
    hx = _anthropic_httpx()
    queue = list(bodies)
    calls: list = []

    def handler(request):
        calls.append(request)
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(queue.pop(0)))
    return handler, calls


def _run(prov):
    events = list(prov.stream(model="claude-opus-5-5", system=[], messages=[{"role": "user", "content": "hi"}],
                              tools=[], max_tokens=1000, effort=None, thinking=False))
    return events, prov.finalize()


def test_stream_error_status_maps_anthropic_error_types() -> None:
    assert stream_error_status({"type": "error", "error": {"type": "overloaded_error"}}) == 529
    assert stream_error_status({"error": {"type": "rate_limit_error"}}) == 429
    assert stream_error_status({"error": {"type": "api_error"}}) == 500
    assert stream_error_status({"error": {"type": "something_new"}}) is None


def test_overloaded_event_inside_a_200_stream_is_retried() -> None:
    handler, calls = _streams(OVERLOADED, TEXT_ONLY)
    events, turn = _run(anthropic_provider(handler))
    retries = [e for e in events if type(e).__name__ in ("RetryEvent", "ProviderRetry")]
    assert len(calls) == 2 and len(retries) == 1 and "529" in retries[0].error
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hello"] and turn.text == "hello"


def test_overloaded_event_reports_overload_not_http_200() -> None:
    handler, calls = _streams(*[OVERLOADED] * 4)
    with pytest.raises(ProviderError) as exc:
        _run(anthropic_provider(handler))
    assert len(calls) == 4  # 1 + max_retries
    assert exc.value.status == 529 and "HTTP 200" not in str(exc.value)
    assert "temporarily unavailable" in str(exc.value)


def test_orphan_tool_result_is_sent_as_text() -> None:
    out = to_anthropic_messages([
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "old output"}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ])
    first = out[0]["content"]
    assert all(b["type"] != "tool_result" for b in first) and "old output" in first[0]["text"]


def test_tool_result_for_a_call_two_turns_back_is_not_a_tool_result() -> None:
    out = to_anthropic_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "X", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "r"}]},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "again"}]},
    ])
    assert out[2]["content"] == [{"type": "tool_result", "tool_use_id": "a", "content": "r"}]
    assert [b["type"] for b in out[4]["content"]] == ["text"]


def test_duplicate_tool_result_is_dropped_and_missing_one_stubbed_first() -> None:
    out = to_anthropic_messages([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "X", "input": {}},
                                          {"type": "tool_use", "id": "b", "name": "X", "input": {}}]},
        {"role": "user", "content": [{"type": "text", "text": "note"},
                                     {"type": "tool_result", "tool_use_id": "b", "content": "rb"},
                                     {"type": "tool_result", "tool_use_id": "b", "content": "dup"}]},
    ])
    content = out[1]["content"]
    assert [b.get("tool_use_id") for b in content[:2]] == ["a", "b"]  # results first, in call order
    assert content[0]["is_error"] and content[1]["content"] == "rb"
    assert content[2] == {"type": "text", "text": "note"} and len(content) == 3


@pytest.mark.parametrize("raw,want", [
    ('sk-1', 'sk-1'), ('"sk-1"  # work key', 'sk-1'), ('sk-1   # work key', 'sk-1'),
    ("'a#b'", 'a#b'), ('a#b', 'a#b'), ('"a"b"', 'a"b'), ('', ''),
])
def test_dotenv_values_drop_trailing_comments(raw: str, want: str) -> None:
    assert dotenv_value(raw) == want


def test_dotenv_file_with_inline_comment(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('AXON_API_KEY=sk-real  # personal\nexport AXON_MODEL="deepseek-v4-flash" # default\n', encoding="utf-8")
    assert _parse_dotenv(env) == {"AXON_API_KEY": "sk-real", "AXON_MODEL": "deepseek-v4-flash"}
