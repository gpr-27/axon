"""/tools must still show deferred MCP tools (they are behind ToolSearch) and full text for one tool."""
from typing import Any, ClassVar

from fakes import FakeProvider
from test_core_helpers import make_agent

from axon.commands import dispatch_command
from axon.tools.base import Tool
from axon.tools.interactive import defer_mcp_tools


class _Mcp(Tool):
    name: ClassVar[str] = "mcp__demo__do_thing"
    description: ClassVar[str] = "Does a demo thing. " * 400
    schema: ClassVar[dict[str, Any]] = {"type": "object", "required": ["q"], "properties": {
        "q": {"type": "string", "description": "L" * 200}}}

    def run(self, args, ctx):  # pragma: no cover
        return ""


def test_deferred_tool_is_listed_and_shown_in_full(workspace, capsys):
    agent = make_agent(FakeProvider([]), workspace)
    agent.registry.register(_Mcp())
    assert defer_mcp_tools(agent.registry, 1_000_000, force=True) is not None
    assert "mcp__demo__do_thing" not in agent.registry.visible_names()
    dispatch_command("/tools", agent)
    assert "mcp__demo__do_thing" in capsys.readouterr().out
    dispatch_command("/tools mcp__demo__do_thing", agent)
    out = capsys.readouterr().out
    assert "deferred" in out and "L" * 200 in out          # the whole parameter text, not cut at 80 characters
