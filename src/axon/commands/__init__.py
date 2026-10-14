"""
Slash commands: built-ins (one registry), custom markdown commands, skills, plugins
and MCP prompts. ``dispatch_command(line, agent)`` is the single entry point and
``list_command_names()`` feeds the input autocomplete.
"""
from axon.commands.builtin import CommandResult, dispatch_command, list_command_names

__all__ = ["dispatch_command", "CommandResult", "list_command_names"]
