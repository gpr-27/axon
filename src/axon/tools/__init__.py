"""
Tools package exports and default tool suite creation.
"""
from axon.tools.base import Tool, ToolContext
from axon.tools.registry import ToolRegistry
from axon.tools.fs_read import ReadTool
from axon.tools.fs_write import WriteTool, EditTool, MultiEditTool
from axon.tools.shell import BashTool
from axon.tools.search import GlobTool, GrepTool, LsTool
from axon.tools.todo import TodoWriteTool
from axon.tools.web import WebFetchTool
from axon.tools.task import TaskTool, ExitPlanModeTool
from axon.tools.doctor import DoctorTool
from axon.tools.web_search import WebSearchTool
from axon.tools.git import GitTool
from axon.tools.code_symbols import CodeSymbolsTool
from axon.tools.file_tree import FileTreeTool
from axon.tools.patch import PatchTool
from axon.tools.http_tool import HttpTool
from axon.tools.process_tool import ProcessTool
from axon.tools.env_tool import EnvTool
from axon.tools.diff_tool import DiffTool
from axon.tools.deep_research import DeepResearchTool
from axon.tools.table_search import TableSearchTool
from axon.tools.code_graph import GoToDefinitionTool, FindReferencesTool
from axon.tools.semantic_search import SemanticSearchTool
from axon.tools.ui_diff import UiPreviewTool
from axon.tools.notebook import NotebookEditTool

def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry loaded with all standard Axon tools."""
    return ToolRegistry([
        ReadTool(),
        WriteTool(),
        EditTool(),
        MultiEditTool(),
        NotebookEditTool(),
        BashTool(),
        GlobTool(),
        GrepTool(),
        LsTool(),
        TodoWriteTool(),
        WebFetchTool(),
        WebSearchTool(),
        GitTool(),
        CodeSymbolsTool(),
        FileTreeTool(),
        PatchTool(),
        HttpTool(),
        ProcessTool(),
        EnvTool(),
        DiffTool(),
        DeepResearchTool(),
        TableSearchTool(),
        GoToDefinitionTool(),
        FindReferencesTool(),
        SemanticSearchTool(),
        UiPreviewTool(),
        TaskTool(),
        ExitPlanModeTool(),
        DoctorTool(),
    ])

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "create_default_registry",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "MultiEditTool",
    "BashTool",
    "GlobTool",
    "GrepTool",
    "LsTool",
    "TodoWriteTool",
    "WebFetchTool",
    "WebSearchTool",
    "GitTool",
    "CodeSymbolsTool",
    "FileTreeTool",
    "PatchTool",
    "HttpTool",
    "ProcessTool",
    "EnvTool",
    "DiffTool",
    "TaskTool",
    "ExitPlanModeTool",
    "DoctorTool",
]
