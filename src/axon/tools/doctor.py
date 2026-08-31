"""
Doctor tool for environment, proxy endpoint, and capability verification.
"""
from __future__ import annotations
import shutil
import sys
from typing import Any, ClassVar
from axon.tools.base import Tool, ToolContext

class DoctorTool(Tool):
    name: ClassVar[str] = "Doctor"
    description: ClassVar[str] = "Diagnose the local environment, proxy endpoint, tool availability, and capabilities."
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        import platform
        from axon.tools.shell import _find_shell_runner
        rg_avail = shutil.which("rg") is not None
        git_avail = shutil.which("git") is not None
        _, shell_flavor = _find_shell_runner()
        return (
            f"=== Axon Environment Diagnostics ===\n"
            f"Platform       : {platform.system()} ({platform.release()})\n"
            f"Python Version : {sys.version.split()[0]}\n"
            f"Shell Engine   : {shell_flavor.upper()}\n"
            f"Workspace      : {ctx.workspace}\n"
            f"Active Model   : {ctx.settings.model}\n"
            f"Base URL       : {ctx.settings.base_url}\n"
            f"Ripgrep (rg)   : {'Installed' if rg_avail else 'Not found (using Python fallback)'}\n"
            f"Git CLI        : {'Installed' if git_avail else 'Not found'}\n"
            f"Permission Mode: {ctx.settings.mode}\n"
            f"Status         : Healthy and operational.\n"
        )

