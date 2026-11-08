"""The shipped key check has no shortcut for keys that look like test data (a real key could start the same way)."""
from unittest.mock import MagicMock, patch

import pytest

import httpx

from axon.providers import verifier
from axon.providers.verifier import verify_api_key


@pytest.mark.parametrize("key", ["sk-test-abc123", "test-abc123", "mock-abc123", "AIzaSy_test_abc", "abc-test"])
def test_look_alike_keys_are_really_checked(key, monkeypatch):
    monkeypatch.setattr(verifier, "httpx", httpx)  # drop the suite-wide fixture-key stand-in from conftest
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"error": {"message": "bad key"}}
    with patch("httpx.post", return_value=resp) as post:
        ok, msg = verify_api_key("openai", key)
    assert post.called, "a request must be sent; no key is accepted without one"
    assert ok is False and "401" in msg
