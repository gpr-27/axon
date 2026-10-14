"""Security and invariant test: the agent loop enforces the per-turn token budget."""
from pathlib import Path
from axon.providers.base import AssistantTurn, ToolUseBlock, Usage
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


def test_sec_loop_turn_token_budget(workspace: Path):
    turns = [AssistantTurn(blocks=[ToolUseBlock(id=f"c{i}", name="Ls", input={"path": "."})], stop_reason="tool_use",
                           usage=Usage(input=400, output=100)) for i in range(20)]
    prov = FakeProvider(turns)
    agent = make_agent(prov, workspace, turn_token_budget=1200)
    res = agent.run_turn("spend tokens")
    assert res.stop_reason == "token_budget"
    assert len(prov.requests) == 3  # 500 + 500 + 500 >= 1200 after the third call
    assert res.usage.input + res.usage.output >= 1200
    agent.conversation.validate()
