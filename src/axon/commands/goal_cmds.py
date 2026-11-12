"""/goal: a done-when goal that an independent grader checks after every turn (see axon.agent.goal).

    /goal                    show the goal, its round limit and the last verdict
    /goal <criteria>         set it (free text or a short checklist)
    /goal budget N           max grader rounds per turn (default 3, max 10)
    /goal clear              remove it

The goal applies to the main agent only; subagents are never graded.
"""
from __future__ import annotations

from typing import Any

from axon.agent import goal as G
from axon.commands.common import err, hint, info, kv, ok, update_settings
from axon.commands.registry import CommandResult
from axon.ui.theme import RST, WHITE

USAGE = ("/goal                  show the goal and its last verdict\n"
         "/goal <done-when>      set it: free text or a short checklist; a grader checks it after each turn\n"
         "/goal budget N         max grader rounds per turn (default 3, max 10)\n"
         "/goal clear            remove it")


def apply_goal_arg(agent: Any, arg: str) -> tuple[str, str]:
    """Run one /goal argument; returns (kind, message) with kind in show|set|clear|budget|error. No printing."""
    action, value = G.parse_goal_arg(arg)
    if action == "show":
        return "show", "\n".join(G.describe(agent))
    if action == "clear":
        had = bool(G.goal_text(agent))
        G.clear_goal(agent)
        update_settings(agent, goal=None)
        return "clear", "Goal cleared" if had else "No goal was set"
    if action == "budget":
        if value is None or value < 1:
            return "error", f"Give a number from 1 to {G.MAX_ROUNDS}: /goal budget 3"
        n = G.set_rounds(agent, min(value, G.MAX_ROUNDS))
        cap = f" (capped at {G.MAX_ROUNDS})" if value > G.MAX_ROUNDS else ""
        return "budget", f"Goal rounds per turn: {n}{cap}"
    G.set_goal(agent, value)
    update_settings(agent, goal=agent.goal)
    return "set", f"Goal set: {agent.goal}"


def handle_goal_command(agent: Any, arg: str) -> CommandResult:
    kind, msg = apply_goal_arg(agent, arg)
    if kind == "error":
        err(msg)
        return CommandResult(handled=True, success=False)
    if kind == "show":
        if not G.goal_text(agent):
            info("No goal set. /goal <done-when criteria> sets one.")
        else:
            lines = msg.split("\n")
            kv("Goal", f"{WHITE}{G.goal_text(agent)}{RST}")
            for ln in lines[1:]:
                info(ln)
        return CommandResult(handled=True)
    if kind == "set":
        ok(msg)
        hint(f"    When the agent finishes a turn that did work, a separate grader checks this (up to {G.goal_rounds(agent)} rounds) "
             "and sends the gaps back until it is met.")
    else:
        ok(msg)
    return CommandResult(handled=True)
