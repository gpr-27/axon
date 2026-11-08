"""A staging endpoint says so when the user selects it (BizNeuro points at a stg- host by default)."""
from __future__ import annotations

from axon.providers.catalog import endpoint_notice, get_preset_by_id


def test_bizneuro_is_flagged_as_staging():
    note = endpoint_notice(get_preset_by_id("bizneuro"))
    assert note and "staging" in note and "prompts and keys" in note and "BizNeuro" in note


def test_production_presets_have_no_notice():
    for pid in ("anthropic", "openai", "ollama", "openrouter"):
        assert endpoint_notice(get_preset_by_id(pid)) is None


def test_custom_stg_host_is_flagged_by_url():
    assert endpoint_notice(None, "https://stg-foo.example.com/v1")
    assert endpoint_notice(None, "https://api.example.com/v1") is None


def test_picker_prints_the_notice_once(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path))
    from axon.ui.provider_picker import print_endpoint_notice
    print_endpoint_notice(get_preset_by_id("bizneuro"))
    out = capsys.readouterr().out
    assert out.count("staging") == 1
