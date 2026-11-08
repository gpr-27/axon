"""A stream that dies part-way was still processed by the provider: the tokens it reported are counted and priced."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeProvider  # noqa: E402
from test_core_helpers import make_agent  # noqa: E402

from axon.providers.base import AssistantTurn, TextBlock, TextDelta, Usage  # noqa: E402


class CutProvider(FakeProvider):
    """Sends some text, then the connection drops. finalize() holds what the provider reported so far."""

    def __init__(self, usage: Usage) -> None:
        super().__init__([])
        self.usage = usage

    def stream(self, **kw: Any) -> Iterator[Any]:
        self.requests.append({"model": kw["model"]})
        yield TextDelta("partial answ")
        raise ConnectionError("stream cut")

    def finalize(self) -> AssistantTurn:
        return AssistantTurn(blocks=[TextBlock(text="partial answ")], stop_reason="end_turn", usage=self.usage)


def test_a_cut_stream_costs_what_the_provider_reported(workspace):
    events: list = []
    agent = make_agent(CutProvider(Usage(input=1200, output=34)), workspace, events=events, model="claude-sonnet-4-6")
    agent.max_retries = 0
    result = agent.run_turn("hello")
    assert result.stop_reason == "error"
    assert agent.ledger.total_input_tokens == 1200 and agent.ledger.total_output_tokens == 34
    assert agent.ledger.total() > 0
    end = next(e for e in events if type(e).__name__ == "LLMCallEnd")
    assert end.usage.input == 1200 and end.cost > 0


def test_no_usage_reported_means_nothing_is_invented(workspace):
    agent = make_agent(CutProvider(Usage()), workspace, model="claude-sonnet-4-6")
    agent.max_retries = 0
    assert agent.run_turn("hello").stop_reason == "error"
    assert agent.ledger.total_tokens() == 0 and agent.ledger.total() == 0
