"""
Tools package exports and the default tool suite.

Model-facing tools (kept deliberately small, Claude-Code-like):
  Read, Write, Edit, MultiEdit, NotebookEdit, Patch   — files
  Glob, Grep, Ls, CodeSearch                          — navigation/search
  Bash (+ background-shell tools if the platform provides them)
  TodoWrite, Task, ExitPlanMode                       — planning / subagents
  Skill, AskUserQuestion                              — skills on demand / ask the user
  WebFetch, WebSearch                                 — web
  + TOOLS exported by axon.features.* modules (e.g. UsageStats)
  ToolSearch is added by build_agent when MCP schemas are deferred.
Doctor is registered hidden (used by /doctor, not advertised to the model).
"""
from axon.tools.base import Tool, ToolContext
from axon.tools.registry import ToolRegistry
from axon.tools.fs_read import ReadTool
from axon.tools.fs_write import WriteTool, EditTool, MultiEditTool
from axon.tools.notebook import NotebookEditTool
from axon.tools.patch import PatchTool
from axon.tools.search import GlobTool, GrepTool, LsTool
from axon.tools.code_graph import CodeSearchTool
from axon.tools.shell import BashTool
from axon.tools.todo import TodoWriteTool
from axon.tools.web import WebFetchTool
from axon.tools.web_search import WebSearchTool
from axon.tools.task import TaskTool, ExitPlanModeTool
from axon.tools.doctor import DoctorTool
from axon.tools.interactive import AskUserQuestionTool, SkillTool, ToolSearchTool

ApplyPatchTool = PatchTool


def _platform_extra_tools() -> list[Tool]:
    """Optional tools owned by the shell/process modules (e.g. background shells)."""
    extras: list[Tool] = []
    for mod_name, cls_names in (
        ("axon.tools.shell", ("BashOutputTool", "KillShellTool")),
        ("axon.tools.process_tool", ("BashOutputTool", "KillShellTool")),
    ):
        try:
            mod = __import__(mod_name, fromlist=list(cls_names))
        except ImportError:
            continue
        for cn in cls_names:
            cls = getattr(mod, cn, None)
            if isinstance(cls, type) and issubclass(cls, Tool) and not any(type(e) is cls for e in extras):
                try:
                    extras.append(cls())
                except Exception:
                    pass
    return extras


def feature_tools() -> list[Tool]:
    """Tools exported as ``TOOLS`` by the feature modules (axon.features.FEATURE_MODULES)."""
    out: list[Tool] = []
    try:
        import importlib
        from axon.features import FEATURE_MODULES
    except Exception:
        return out
    for mod_name in FEATURE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for t in getattr(mod, "TOOLS", ()) or ():
            if isinstance(t, Tool) and all(t.name != o.name for o in out):
                out.append(t)
    return out


# Names accepted by --tools besides the canonical tool names.
_TOOL_GROUPS = {"default": None, "all": None}


def restrict_tools(registry: ToolRegistry, spec: str | list[str] | None) -> ToolRegistry:
    """Apply ``--tools``: "" = no built-in tools, "default"/"all" = everything, otherwise a comma or
    space separated list of tool names. MCP tools (mcp__*) are never removed by this filter."""
    if spec is None:
        return registry
    if isinstance(spec, str):
        names = [n.strip() for n in spec.replace(" ", ",").split(",") if n.strip()]
    else:
        names = [str(n).strip() for n in spec if str(n).strip()]
    if any(n.lower() in _TOOL_GROUPS for n in names):
        return registry
    keep = []
    for t in registry.all_tools():
        if t.name.startswith("mcp__") or getattr(t, "hidden", False):
            keep.append(t.name)
    for n in names:
        if registry.has(n):
            canonical = registry.get(n).name
            if canonical not in keep:
                keep.append(canonical)
    return registry.subset(names=keep)


def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry loaded with the standard Axon tools."""
    tools: list[Tool] = [
        ReadTool(),
        WriteTool(),
        EditTool(),
        MultiEditTool(),
        NotebookEditTool(),
        PatchTool(),
        GlobTool(),
        GrepTool(),
        LsTool(),
        CodeSearchTool(),
        BashTool(),
        *_platform_extra_tools(),
        TodoWriteTool(),
        WebFetchTool(),
        WebSearchTool(),
        TaskTool(),
        ExitPlanModeTool(),
        SkillTool(),
        AskUserQuestionTool(),
        DoctorTool(),
    ]
    for extra in feature_tools():
        if all(extra.name != t.name for t in tools):
            tools.append(extra)
    return ToolRegistry(tools)


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "create_default_registry",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "MultiEditTool",
    "NotebookEditTool",
    "PatchTool",
    "ApplyPatchTool",
    "GlobTool",
    "GrepTool",
    "LsTool",
    "CodeSearchTool",
    "BashTool",
    "TodoWriteTool",
    "WebFetchTool",
    "WebSearchTool",
    "TaskTool",
    "ExitPlanModeTool",
    "DoctorTool",
    "SkillTool",
    "AskUserQuestionTool",
    "ToolSearchTool",
    "feature_tools",
    "restrict_tools",
]


def __getattr__(name: str):  # PEP 562: lazy access to optional/legacy names
    if name == "ProcessTool":
        from axon.tools.process_tool import ProcessTool
        return ProcessTool
    raise AttributeError(f"module 'axon.tools' has no attribute {name!r}")
