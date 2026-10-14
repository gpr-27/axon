"""Tests for axon.features.repo_map (incremental index, ranking, deterministic map, commands)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.features import FeatureCommand
from axon.features import repo_map as rm


@pytest.fixture(autouse=True)
def axon_home(tmp_path, monkeypatch):
    home = tmp_path / "axhome"
    monkeypatch.setenv("AXON_HOME", str(home))
    rm._PROMPT_CACHE.clear()
    return home


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path) -> Path:
    ws = tmp_path / "proj"
    _write(ws / "src" / "core.py", "class Engine:\n    def start(self, speed):\n        pass\n\n"
                                   "def helper_compute(x):\n    return x\n")
    _write(ws / "src" / "cli.py", "from core import Engine, helper_compute\n\n"
                                  "def main(argv):\n    Engine().start(1)\n    helper_compute(2)\n")
    _write(ws / "src" / "util.py", "from core import Engine\nENGINE_COUNT = 3\n\ndef lonely():\n    Engine()\n")
    _write(ws / "web" / "app.ts", "export class WidgetStore {\n}\nexport function renderWidget(a: number) {\n}\n")
    _write(ws / "svc" / "Main.java", "public class Main {\n    public static void main(String[] args) {\n    }\n}\n")
    _write(ws / "lib" / "thing.rb", "module Things\n  class Thing\n    def run(x)\n    end\n  end\nend\n")
    _write(ws / "native" / "mathx.c", "struct vec {\n  int x;\n};\nint add_numbers(int a, int b) {\n  return a + b;\n}\n")
    _write(ws / "README.md", "# not source\n")
    return ws


def test_index_extracts_symbols_across_languages(repo):
    index, stats = rm.build_index(repo)
    files = index["files"]
    assert "README.md" not in files
    names = {rel: {s[0] for s in info["sym"]} for rel, info in files.items()}
    assert {"Engine", "start", "helper_compute"} <= names["src/core.py"]
    assert {"WidgetStore", "renderWidget"} <= names["web/app.ts"]
    assert {"Main", "main"} <= names["svc/Main.java"]
    assert {"Things", "Thing", "run"} <= names["lib/thing.rb"]
    assert {"vec", "add_numbers"} <= names["native/mathx.c"]
    assert stats.files == 7 and stats.updated == 7 and stats.reused == 0
    assert stats.languages["python"] == 3
    assert Path(stats.index_path).is_file()
    assert Path(stats.index_path).parent.parent.parent == rm.index_path(repo).parent.parent.parent


def test_index_is_incremental(repo):
    rm.build_index(repo)
    _i, stats = rm.build_index(repo)
    assert stats.updated == 0 and stats.reused == 7
    f = repo / "src" / "util.py"
    f.write_text(f.read_text(encoding="utf-8") + "\ndef added_later():\n    pass\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    (repo / "lib" / "thing.rb").unlink()
    index, stats = rm.build_index(repo)
    assert stats.updated == 1 and stats.removed == 1 and stats.reused == 5
    assert "added_later" in {s[0] for s in index["files"]["src/util.py"]["sym"]}
    _i, stats = rm.build_index(repo, rebuild=True)
    assert stats.updated == 6


def test_corrupt_index_is_ignored(repo):
    path = rm.index_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _i, stats = rm.build_index(repo)
    assert stats.updated == 7
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == rm.INDEX_VERSION


def test_ranking_prefers_referenced_files(repo):
    index, _ = rm.build_index(repo)
    ranked = rm.rank_files(index)
    order = [r.path for r in ranked]
    assert order.index("src/core.py") < order.index("src/util.py")
    core = next(r for r in ranked if r.path == "src/core.py")
    assert core.symbol_refs.get("Engine", 0) >= 2


def test_map_deterministic_and_budgeted(repo):
    a = rm.build_repo_map(repo, max_tokens=1500)
    b = rm.build_repo_map(repo, max_tokens=1500)
    assert a == b
    assert "src/" in a and " core.py" in a and "class Engine" in a
    assert "def start(speed)" in a  # self stripped, method under its class
    small = rm.build_repo_map(repo, max_tokens=64)
    assert rm.estimate_tokens(small) <= 64 + 20
    assert "more files not shown" in small


def test_map_subpath(repo):
    text = rm._map_from_index(repo, rm.build_index(repo)[0], 1500, "web")
    assert "app.ts" in text and "core.py" not in text


def test_repo_map_for_prompt_respects_setting(repo):
    assert rm.repo_map_for_prompt(repo) is None
    off = SimpleNamespace(workspace=repo, repo_map=False)
    assert rm.repo_map_for_prompt(off) is None
    on = SimpleNamespace(workspace=repo, repo_map=True)
    text = rm.repo_map_for_prompt(on)
    assert text and "Engine" in text
    assert rm.repo_map_for_prompt(repo, settings=on) == text


def test_regex_fallback_python_nesting():
    syms = rm._regex_symbols("class A:\n    def m(self):\n        pass\ndef f(x):\n    pass\n", "python")
    assert ["m", "method"] == syms[1][:2] and syms[1][4] == "A"
    assert syms[2][:2] == ["f", "function"] and syms[2][4] is None


def test_commands_and_output(repo, capsys):
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=repo))
    rm.handle_index(agent, "--rebuild")
    out = capsys.readouterr().out
    assert "files scanned" in out and "python" in out and "symbols" in out
    rm.handle_map(agent, "src --tokens 400")
    out = capsys.readouterr().out
    assert "core.py" in out and "app.ts" not in out and "ms" in out
    rm.handle_map(agent, "--tokens abc")
    assert "Usage" in capsys.readouterr().out
    rm.handle_map(SimpleNamespace(), "")  # no settings → cwd, must not crash
    names = {c.name for c in rm.COMMANDS}
    assert {"/map", "/index"} <= names and all(isinstance(c, FeatureCommand) for c in rm.COMMANDS)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_recency_and_ls_files(repo):
    def g(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    _write(repo / ".gitignore", "native/\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    index, stats = rm.build_index(repo)
    assert "native/mathx.c" not in index["files"]
    recent = rm._git_recent_files(repo)
    assert "src/core.py" in recent
