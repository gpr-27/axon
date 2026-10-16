"""
`axon keys ...` and `axon endpoint ...`: the /keys and /endpoint engines outside the REPL.

    axon keys                         list keys (menu on a terminal)
    axon keys set <provider|VAR> [key] [--project] [--no-verify]
    axon keys test [provider|all]     axon keys remove <provider|VAR> [--project] [-y]
    axon keys where                   axon keys edit [--project]

    axon endpoint                     list endpoints (menu on a terminal)
    axon endpoint set <provider> <url>
    axon endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--use]
    axon endpoint reset <provider|name> · axon endpoint test [provider|url] · axon endpoint use <name>
"""
from __future__ import annotations


def keys_main(argv: list[str]) -> int:
    from axon.commands.keys_cmds import keys_main as _main
    return _main(argv)


def endpoint_main(argv: list[str]) -> int:
    from axon.commands.endpoint_cmds import endpoint_main as _main
    return _main(argv)


COMMANDS: list = []
CLI_SUBCOMMANDS = {"keys": keys_main, "key": keys_main, "endpoint": endpoint_main, "endpoints": endpoint_main}

__all__ = ["CLI_SUBCOMMANDS", "COMMANDS", "endpoint_main", "keys_main"]
