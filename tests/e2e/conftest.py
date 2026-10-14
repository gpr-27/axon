"""Fixtures for the end-to-end suite (real `axon` subprocess against a local fake LLM server)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fake_llm import FakeLLMServer  # noqa: E402
from harness import AxonEnv, create_axon_env  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: end-to-end test running the real axon CLI against a fake LLM server")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def fake_llm() -> FakeLLMServer:
    server = FakeLLMServer(model_ids=("gpt-4o", "claude-sonnet-4-6", "fake-model")).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def axon_env(tmp_path: Path, fake_llm: FakeLLMServer) -> AxonEnv:
    return create_axon_env(tmp_path, fake_llm)


@pytest.fixture
def axon_git_env(tmp_path: Path, fake_llm: FakeLLMServer) -> AxonEnv:
    return create_axon_env(tmp_path, fake_llm, git=True)
