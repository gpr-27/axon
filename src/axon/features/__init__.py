"""
Pluggable Axon feature modules.

Each module listed in FEATURE_MODULES may export:
  COMMANDS: list[FeatureCommand]            -> slash commands (auto-registered)
  CLI_SUBCOMMANDS: dict[str, Callable]      -> `axon <name> ...` subcommands (fn(argv) -> int)
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

FEATURE_MODULES: tuple[str, ...] = (
    "axon.features.analytics",
    "axon.features.repo_map",
    "axon.features.gitops",
    "axon.features.sdk",
    "axon.features.server",
    "axon.features.solver",
    "axon.features.planner",
    "axon.features.onboarding",
    "axon.features.suggest",
    "axon.features.tips",
    "axon.features.scaffold",
    "axon.features.journal",
    "axon.features.credentials",
)


@dataclass(frozen=True)
class FeatureCommand:
    name: str
    handler: Callable[[Any, str], Any]
    description: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    category: str = "Features"
    usage: str = ""


def _load(mod_name: str) -> Any | None:
    try:
        return importlib.import_module(mod_name)
    except Exception:
        return None


def feature_commands() -> list[FeatureCommand]:
    out: list[FeatureCommand] = []
    for name in FEATURE_MODULES:
        mod = _load(name)
        for cmd in getattr(mod, "COMMANDS", ()) or ():
            if isinstance(cmd, FeatureCommand):
                out.append(cmd)
    return out


def cli_subcommands() -> dict[str, Callable[[list[str]], int]]:
    out: dict[str, Callable[[list[str]], int]] = {}
    for name in FEATURE_MODULES:
        mod = _load(name)
        out.update(getattr(mod, "CLI_SUBCOMMANDS", {}) or {})
    return out


__all__ = ["FEATURE_MODULES", "FeatureCommand", "feature_commands", "cli_subcommands"]
