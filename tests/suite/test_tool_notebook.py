"""
Test suite for NotebookEditTool (.ipynb Jupyter notebook cell operations).
"""
import json
from pathlib import Path
import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.tools.base import ToolContext
from axon.tools.notebook import NotebookEditTool


@pytest.fixture
def sample_notebook(tmp_path: Path) -> Path:
    nb_path = tmp_path / "analysis.ipynb"
    nb_content = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Exploratory Data Analysis\n", "This notebook analyzes trends."],
            },
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "source": ["import numpy as np\n", "x = np.array([1, 2, 3])\n", "print(x)"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": ["[1 2 3]\n"],
                    }
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "metadata": {},
                "source": ["x.mean()"],
                "outputs": [
                    {
                        "output_type": "execute_result",
                        "execution_count": 2,
                        "data": {"text/plain": ["2.0"]},
                    }
                ],
            },
        ],
        "metadata": {
            "language_info": {"name": "python"},
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    nb_path.write_text(json.dumps(nb_content, indent=1), encoding="utf-8")
    return nb_path


def test_notebook_read_cells(sample_notebook: Path, tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    out = tool.run({"notebook_path": str(sample_notebook), "action": "read_cells"}, ctx)
    assert "analysis.ipynb (3 cells)" in out
    assert "[Cell 0] (markdown)" in out
    assert "# Exploratory Data Analysis" in out
    assert "[Cell 1] (code [Execution Count: 1])" in out
    assert "import numpy as np" in out
    assert "Outputs (1 item)" in out


def test_notebook_edit_cell(sample_notebook: Path, tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    new_code = "import pandas as pd\ndf = pd.DataFrame({'a': [10, 20]})"
    res = tool.run({
        "notebook_path": "analysis.ipynb",
        "action": "edit_cell",
        "cell_index": 1,
        "source": new_code,
    }, ctx)

    assert "Successfully updated Cell #1" in res

    # Verify JSON structure
    data = json.loads(sample_notebook.read_text(encoding="utf-8"))
    cell1 = data["cells"][1]
    assert cell1["source"] == ["import pandas as pd\n", "df = pd.DataFrame({'a': [10, 20]})"]
    assert cell1["outputs"] == []
    assert cell1["execution_count"] is None


def test_notebook_insert_cell(sample_notebook: Path, tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    res = tool.run({
        "notebook_path": "analysis.ipynb",
        "action": "insert_cell",
        "cell_index": 1,
        "cell_type": "markdown",
        "source": "## Data Cleaning Step",
    }, ctx)

    assert "Successfully inserted new Cell #1" in res

    data = json.loads(sample_notebook.read_text(encoding="utf-8"))
    assert len(data["cells"]) == 4
    assert data["cells"][1]["cell_type"] == "markdown"
    assert "## Data Cleaning Step" in data["cells"][1]["source"][0]


def test_notebook_delete_cell(sample_notebook: Path, tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    res = tool.run({
        "notebook_path": "analysis.ipynb",
        "action": "delete_cell",
        "cell_index": 0,
    }, ctx)

    assert "Successfully deleted Cell #0" in res

    data = json.loads(sample_notebook.read_text(encoding="utf-8"))
    assert len(data["cells"]) == 2
    assert data["cells"][0]["cell_type"] == "code"


def test_notebook_clear_outputs(sample_notebook: Path, tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    res = tool.run({
        "notebook_path": "analysis.ipynb",
        "action": "clear_outputs",
    }, ctx)

    assert "Cleared outputs across 2 code cells" in res

    data = json.loads(sample_notebook.read_text(encoding="utf-8"))
    for c in data["cells"]:
        if c.get("cell_type") == "code":
            assert c["outputs"] == []
            assert c["execution_count"] is None


def test_notebook_error_handling(tmp_path: Path):
    tool = NotebookEditTool()
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=Settings(workspace=tmp_path),
    )

    with pytest.raises(ToolError, match="Notebook file not found"):
        tool.run({"notebook_path": "nonexistent.ipynb", "action": "read_cells"}, ctx)
