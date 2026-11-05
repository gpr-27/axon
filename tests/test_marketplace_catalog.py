"""Marketplace catalog: schema checks on the shipped data, loader errors, search and filters."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.marketplace import catalog

REQUIRED_CONNECTOR_FIELDS = ("id", "name", "publisher", "description", "category", "transport",
                             "auth", "docs_url", "verified", "free", "free_note")


def raw_connectors() -> list[dict]:
    path = catalog.DATA_DIR / "connectors.json"
    return json.loads(path.read_text(encoding="utf-8"))["connectors"]


def test_every_connector_has_the_required_fields() -> None:
    for raw in raw_connectors():
        for name in REQUIRED_CONNECTOR_FIELDS:
            assert name in raw, f"{raw.get('id')} is missing {name}"
        assert catalog.connector_problems(raw) == [], raw["id"]


def test_connector_ids_are_unique() -> None:
    ids = [raw["id"] for raw in raw_connectors()]
    assert len(ids) == len(set(ids))


def test_http_and_sse_connectors_use_https() -> None:
    for raw in raw_connectors():
        if raw["transport"] in ("http", "sse"):
            assert raw["url"].startswith("https://"), raw["id"]
        else:
            assert raw["command"], raw["id"]


def test_api_key_connectors_name_their_environment_variable() -> None:
    for raw in raw_connectors():
        if raw["auth"] == "api_key":
            names = [v["name"] for v in raw["env_vars"]]
            assert names and all(n.isupper() for n in names), raw["id"]


def test_header_templates_only_use_declared_variables() -> None:
    for raw in raw_connectors():
        declared = {v["name"] for v in raw.get("env_vars", [])}
        for value in raw.get("headers", {}).values():
            for name in declared:
                value = value.replace("${" + name + "}", "")
            assert "${" not in value, raw["id"]


def test_all_three_tabs_load() -> None:
    assert len(catalog.load_connectors()) >= 30
    assert len(catalog.load_skills()) >= 5
    assert len(catalog.load_plugins()) >= 5


def test_skill_and_plugin_ids_are_unique() -> None:
    for tab in ("skills", "plugins"):
        ids = [e.id for e in catalog.load_tab(tab)]
        assert len(ids) == len(set(ids))


def test_no_made_up_totals_in_data_files() -> None:
    for name in ("connectors.json", "skills.json", "plugins.json"):
        data = json.loads((catalog.DATA_DIR / name).read_text(encoding="utf-8"))
        assert set(data) <= {"version", "connectors", "skills", "plugins"}


def write_connectors(folder: Path, items: list[dict]) -> Path:
    (folder / "connectors.json").write_text(json.dumps({"connectors": items}), encoding="utf-8")
    return folder


def good_item() -> dict:
    return dict(raw_connectors()[0])


def test_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    write_connectors(tmp_path, [good_item(), good_item()])
    with pytest.raises(catalog.CatalogError, match="duplicate id"):
        catalog.load_connectors(tmp_path)


def test_loader_rejects_plain_http_urls(tmp_path: Path) -> None:
    item = good_item()
    item["transport"], item["url"] = "http", "http://example.com/mcp"
    write_connectors(tmp_path, [item])
    with pytest.raises(catalog.CatalogError, match="https"):
        catalog.load_connectors(tmp_path)


def test_loader_rejects_missing_fields_and_bad_json(tmp_path: Path) -> None:
    item = good_item()
    del item["publisher"]
    write_connectors(tmp_path, [item])
    with pytest.raises(catalog.CatalogError, match="publisher"):
        catalog.load_connectors(tmp_path)
    (tmp_path / "connectors.json").write_text("not json", encoding="utf-8")
    with pytest.raises(catalog.CatalogError):
        catalog.load_connectors(tmp_path)


def test_search_matches_every_word() -> None:
    entries = catalog.load_connectors()
    hits = catalog.filter_entries(entries, "postgres")
    assert {"neon", "prisma"} <= {e.id for e in hits}
    assert catalog.filter_entries(entries, "github zzzz") == []


def test_filter_by_category_and_free() -> None:
    entries = catalog.load_connectors()
    databases = catalog.filter_entries(entries, category="databases")
    assert databases and all(e.category == "Databases" for e in databases)
    free = catalog.filter_entries(entries, free_only=True)
    assert free and all(e.free for e in free)
    assert len(free) < len(entries)


def test_filter_by_installed_state() -> None:
    entries = catalog.load_connectors()
    installed = {"github", "notion"}
    yours = catalog.filter_entries(entries, installed_ids=installed, installed=True)
    assert {e.id for e in yours} == installed
    others = catalog.filter_entries(entries, installed_ids=installed, installed=False)
    assert len(others) == len(entries) - 2


def test_find_entry_ignores_case_and_categories_are_sorted() -> None:
    entries = catalog.load_connectors()
    assert catalog.find_entry(entries, "GitHub").id == "github"
    assert catalog.find_entry(entries, "nope") is None
    names = catalog.categories(entries)
    assert names == sorted(names)
