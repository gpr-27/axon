"""A huge prompt never ends the turn: the full text is saved, the model gets the ends and the path."""
from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.agent import large_input
from axon.agent.trace import LargeInputEvent
from axon.errors import ProviderError
from axon.providers.base import AssistantTurn, TextBlock, Usage


def _huge(lines: int = 100_000) -> str:
    return "\n".join(f"log line {i} with some ordinary content" for i in range(lines))


def _sent_text(provider: FakeProvider, index: int = -1) -> str:
    return str(provider.requests[index]["messages"])


class Rejecting(FakeProvider):
    """Rejects any request larger than *limit* characters, the way a provider does when over its window."""

    def __init__(self, turns, limit: int, error: Exception | None = None) -> None:
        super().__init__(turns)
        self.limit = limit
        self.rejected = 0
        self.error = error or ProviderError("HTTP 400: the conversation exceeds the model's context window", status=400)

    def stream(self, **kw):
        if len(str(kw["messages"])) > self.limit:
            self.rejected += 1
            raise self.error
        return super().stream(**kw)


def test_small_input_is_untouched(workspace: Path):
    agent = make_agent(FakeProvider(scripted("ok")), workspace)
    agent.run_turn("please fix the bug")
    assert "please fix the bug" in _sent_text(agent.provider)
    assert "too large to send whole" not in _sent_text(agent.provider)


def test_100k_lines_finishes_the_turn(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace)
    result = agent.run_turn(_huge())
    assert result.stop_reason == "end_turn" and result.final_text == "done"


def test_request_stays_small_and_names_the_saved_file(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace)
    text = _huge()
    agent.run_turn(text)
    sent = _sent_text(agent.provider)
    assert len(sent) < 200_000
    saved = next(p for p in Path(agent.session.active_file).parent.joinpath("pastes").glob("*.txt"))
    assert saved.read_text() == text and str(saved) in sent


def test_both_ends_of_the_input_reach_the_model(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace)
    agent.run_turn("summarise this log\n" + _huge() + "\nand list every error")
    sent = _sent_text(agent.provider)
    assert "summarise this log" in sent and "and list every error" in sent


def test_model_is_told_the_real_size_and_how_to_read_the_rest(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace)
    agent.run_turn(_huge())
    sent = _sent_text(agent.provider)
    assert "100,000 lines" in sent and "Read" in sent and "Grep" in sent


def test_user_sees_what_happened_and_the_cost(workspace: Path):
    events: list = []
    agent = make_agent(FakeProvider(scripted("done")), workspace, events=events)
    agent.run_turn(_huge())
    event = next(e for e in events if isinstance(e, LargeInputEvent))
    assert event.spilled and event.lines == 100_000
    assert event.tokens_sent < event.tokens_full and Path(event.path).exists()


def test_transcript_keeps_the_excerpt_so_resume_stays_cheap(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace)
    agent.run_turn(_huge())
    assert Path(agent.session.active_file).stat().st_size < 500_000


def test_follow_up_turns_do_not_resend_the_whole_input(workspace: Path):
    agent = make_agent(FakeProvider(scripted("one") + scripted("two")), workspace)
    agent.run_turn(_huge())
    agent.run_turn("what next?")
    assert len(_sent_text(agent.provider)) < 200_000


def test_setting_can_turn_it_off(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace, large_input_tokens=-1)
    text = _huge(20_000)
    agent.run_turn(text)
    assert "log line 10000 " in _sent_text(agent.provider)


def test_setting_can_lower_the_limit(workspace: Path):
    agent = make_agent(FakeProvider(scripted("done")), workspace, large_input_tokens=100)
    agent.run_turn("word " * 1000)
    assert "too large to send whole" in _sent_text(agent.provider)


def test_oversized_message_from_an_old_session_is_shrunk(workspace: Path):
    agent = make_agent(FakeProvider(scripted("ok")), workspace)
    agent.conversation.messages.append({"role": "user", "content": _huge()})
    agent.conversation.messages.append({"role": "assistant", "content": "seen"})
    agent.run_turn("continue")
    assert len(_sent_text(agent.provider)) < 200_000


@pytest.mark.parametrize("err", [
    ProviderError("HTTP 400: the conversation exceeds the model's context window", status=400),
    ProviderError("prompt is too long: 900000 tokens > 200000 maximum", status=400),
    ProviderError("This model's maximum context length is 8192 tokens", status=400),
    ProviderError("HTTP 413: request too large", status=413),
])
def test_provider_rejection_is_recovered_within_the_turn(workspace: Path, err: Exception):
    text = "start\n" + _huge(8000) + "\nend"
    provider = Rejecting(scripted("recovered"), limit=30_000, error=err)
    agent = make_agent(provider, workspace, large_input_tokens=-1)    # the proactive limit is off, so the provider rejects
    agent.conversation.messages.append({"role": "user", "content": text})
    agent.conversation.messages.append({"role": "assistant", "content": "seen"})
    result = agent.run_turn("go on")
    assert result.final_text == "recovered" and provider.rejected >= 1


def test_unrelated_400_is_not_retried(workspace: Path):
    provider = Rejecting([], limit=0, error=ProviderError("HTTP 400: bad tool schema", status=400))
    agent = make_agent(provider, workspace)
    result = agent.run_turn("hello")
    assert result.stop_reason == "error" and provider.rejected == 1


def test_recovery_gives_up_with_an_honest_error(workspace: Path):
    provider = Rejecting([], limit=0)
    agent = make_agent(provider, workspace)
    result = agent.run_turn("hello")
    assert result.stop_reason == "error" and provider.rejected <= 3


def test_auto_compaction_with_nothing_to_compact_makes_no_paid_call(workspace: Path):
    provider = FakeProvider(scripted("ok"))
    agent = make_agent(provider, workspace)
    agent.conversation.messages.extend([
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
    ])
    assert agent.compact(trigger="auto") is False
    assert provider.requests == []


def test_context_full_stop_reason_is_handled(workspace: Path):
    full = AssistantTurn(blocks=[TextBlock(text="partial")], stop_reason="model_context_window_exceeded", usage=Usage())
    agent = make_agent(FakeProvider([full] + scripted("finished")), workspace)
    result = agent.run_turn("do it")
    assert result.final_text == "finished"


def test_context_full_twice_stops_with_the_partial_answer(workspace: Path):
    full = AssistantTurn(blocks=[TextBlock(text="partial")], stop_reason="model_context_window_exceeded", usage=Usage())
    agent = make_agent(FakeProvider([full, full, full]), workspace)
    result = agent.run_turn("do it")
    assert result.final_text == "partial" and result.stop_reason == "model_context_window_exceeded"


def test_estimate_counts_non_ascii_text_higher():
    assert large_input.estimate_tokens("日本語" * 1000) > large_input.estimate_tokens("abc" * 1000)


@pytest.mark.parametrize("window,expected", [(128_000, 32_000), (200_000, 50_000), (1_000_000, 100_000)])
def test_automatic_limit_follows_the_window(window: int, expected: int):
    assert large_input.inline_limit(window, 0) == expected


def test_single_enormous_line_is_cut_too():
    text = "x" * 5_000_000
    fitted = large_input.excerpt(text, 1000, Path("/tmp/x.txt"))
    assert len(fitted) < 10_000


def test_journal_does_not_keep_full_prompts(workspace: Path):
    from axon.session.activity import bounded
    data = bounded({"text": "x" * 1_000_000, "n": 1})
    assert len(data["text"]) < 20_000 and data["n"] == 1


def test_image_scan_skips_huge_text(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    from axon.agent import images
    calls = []
    monkeypatch.setattr(images, "resolve_fuzzy_image_path", lambda s: calls.append(s))
    images.compact_image_paths("see a.png\n" * 100_000)
    assert calls == []
