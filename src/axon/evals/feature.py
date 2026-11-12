"""Feature registration: ``axon eval`` (CLI) and ``/eval`` (in session)."""
from __future__ import annotations

from axon.evals.cli import USAGE, eval_main, handle_eval

try:
    from axon.features import FeatureCommand
    COMMANDS = [FeatureCommand(name="/eval", handler=handle_eval, category="Features", usage=USAGE.replace("axon eval", "/eval"),
                               description="Run the golden eval suite (or your task files) against the current model")]
except Exception:                          # pragma: no cover
    COMMANDS = []

CLI_SUBCOMMANDS = {"eval": eval_main}
__all__ = ["COMMANDS", "CLI_SUBCOMMANDS"]
