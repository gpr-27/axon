"""
Turn execution runner for Axon.
Executes agent turns cleanly and sequentially without any hardware cursor desynchronization.
"""
from __future__ import annotations
from axon.agent.loop import Agent, TurnResult
from axon.ui.render import Renderer

def run_interactive_turn(agent: Agent, renderer: Renderer, turn_input: str) -> TurnResult | None:
    """Execute agent turn cleanly."""
    return agent.run_turn(turn_input)
