"""
Pre- and post-tool shell hooks runner.
"""
from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass
from typing import Any
from axon.config import Settings

@dataclass(frozen=True)
class HookOutcome:
    proceed: bool
    override_output: str | None = None

class HookRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, event: str, payload: dict[str, Any]) -> HookOutcome:
        hooks = self.settings.hooks.get(event, [])
        if not hooks:
            return HookOutcome(proceed=True)

        for h in hooks:
            try:
                proc = subprocess.run(
                    h.command,
                    shell=True,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 2:
                    # Exit code 2 blocks execution and replaces result with stdout
                    return HookOutcome(proceed=False, override_output=proc.stdout.strip())
            except Exception:
                pass

        return HookOutcome(proceed=True)
