"""Live model discovery (new models appear without an update) and deleting models from the picker."""
from __future__ import annotations

import pytest

from axon.providers import catalog, endpoints


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(catalog, "axon_config_file", lambda: path)
    monkeypatch.setattr(endpoints, "axon_config_file", lambda: path)
    monkeypatch.setattr(catalog, "_live_cache", {})
    return path


def _preset():
    return catalog.get_preset_by_id("groq")


def test_live_models_are_merged_after_curated(cfg, monkeypatch):
    p = _preset()
    monkeypatch.setattr(catalog, "find_api_key", lambda names: "k")
    monkeypatch.setattr(catalog, "fetch_provider_models", lambda *a, **k: ([*p.models, "brand/new-model-9"], 200))
    assert catalog.discover_models(p) == [*p.models, "brand/new-model-9"]
    got = catalog.get_models_for_provider(p)
    assert got[: len(p.models)] == p.models and got[-1] == "brand/new-model-9"


def test_discovery_without_key_or_failure_keeps_built_in_list(cfg, monkeypatch):
    p = _preset()
    monkeypatch.setattr(catalog, "find_api_key", lambda names: None)
    assert catalog.discover_models(p) == []
    assert catalog.get_models_for_provider(p) == p.models


def test_delete_custom_model_forgets_it(cfg):
    p = _preset()
    endpoints.add_models(p.id, ["my/custom"])
    assert "my/custom" in catalog.get_models_for_provider(p)
    assert endpoints.delete_model(p.id, "my/custom") == "removed"
    assert "my/custom" not in catalog.get_models_for_provider(p)
    assert endpoints.saved_models(p.id) == []


def test_delete_builtin_model_hides_it_and_unhide_restores(cfg):
    p = _preset()
    first = p.models[0]
    assert endpoints.delete_model(p.id, first) in ("hidden", "removed")
    assert first not in catalog.get_models_for_provider(p)
    assert endpoints.unhide_model(p.id, first) is True
    assert first in catalog.get_models_for_provider(p)
