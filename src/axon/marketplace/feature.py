"""Feature hook: exposes ``axon marketplace ...`` on the command line (see axon.features)."""
from __future__ import annotations

from axon.marketplace.cli import main

CLI_SUBCOMMANDS = {"marketplace": main}
