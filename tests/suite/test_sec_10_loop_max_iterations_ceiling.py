"""Security and invariant test: the agent loop terminates at max_iterations with valid pairing."""
from pathlib import Path
from axon.providers.base import AssistantTurn, ToolUseBlock
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


def test_sec_loop_max_iterations_ceiling(workspace: Path):
    turns = [AssistantTurn(blocks=[ToolUseBlock(id=f"c{i}", name="Ls", input={"path": "."})], stop_reason="tool_use")
             for i in range(20)]
    prov = FakeProvider(turns)
    agent = make_agent(prov, workspace, max_iterations=4)
    res = agent.run_turn("never stop")
    assert res.stop_reason == "max_iterations"
    assert len(prov.requests) == 4
    agent.conversation.validate()  # every tool_use has its tool_result
