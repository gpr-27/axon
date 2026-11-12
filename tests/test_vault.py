"""The secret vault: backends (fake CLIs), metadata-only listing, host binding, migration, redaction, web routes."""
from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from axon.features import vault


class FakeKeychain:
    """Stands in for the `security` CLI. Records every argv and stdin."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, input_text=None, timeout=10.0):
        self.calls.append((list(argv), input_text))
        if argv[:2] == ["security", "-i"]:
            m = re.match(r"add-generic-password -U -s (\S+) -a (\S+) -l \"[^\"]*\" -X ([0-9a-f]+)", input_text or "")
            assert m, input_text
            self.store[(m.group(1), m.group(2))] = bytes.fromhex(m.group(3)).decode()
            return 0, ""
        if argv[1] == "find-generic-password":
            v = self.store.get((argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]))
            return (0, v + "\n") if v is not None else (44, "")
        if argv[1] == "delete-generic-password":
            self.store.pop((argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]), None)
            return 0, ""
        return 1, ""


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "axonhome"
    h.mkdir()
    monkeypatch.setenv("AXON_HOME", str(h))
    monkeypatch.delenv("AXON_VAULT_PASSPHRASE", raising=False)
    monkeypatch.setattr(vault, "_passphrase", None)
    return h


@pytest.fixture()
def keychain(home, monkeypatch) -> FakeKeychain:
    fk = FakeKeychain()
    monkeypatch.setattr(vault, "platform_name", lambda: "darwin")
    monkeypatch.setattr(vault, "_which", lambda c: "/usr/bin/" + c if c == "security" else None)
    monkeypatch.setattr(vault, "_run", fk)
    return fk


SECRET = "sk-test-0123456789abcdef-SECRET"


def test_backend_selection_order(home, monkeypatch):
    monkeypatch.setattr(vault, "platform_name", lambda: "win32")
    monkeypatch.setattr(vault, "_which", lambda c: None)
    assert vault.backend().id == "plain-file" and "NOT encrypted" in vault.backend().warning()
    fk = FakeKeychain()
    monkeypatch.setattr(vault, "platform_name", lambda: "darwin")
    monkeypatch.setattr(vault, "_which", lambda c: "/x/" + c)
    monkeypatch.setattr(vault, "_run", fk)
    assert vault.backend().id == "keychain"
    monkeypatch.setattr(vault, "platform_name", lambda: "linux")
    monkeypatch.setattr(vault, "_run", lambda argv, input_text=None, timeout=10.0: (1, ""))
    assert vault.backend().id == "secret-service"
    monkeypatch.setattr(vault, "_run", lambda argv, input_text=None, timeout=10.0: (127, ""))
    assert vault.backend().id == "plain-file"


def test_set_get_list_metadata_only_and_no_secret_in_argv(keychain):
    m = vault.set_secret("env:OPENAI_API_KEY", SECRET, type="api_key")
    assert m["backend"] == "keychain" and SECRET not in json.dumps(m)
    assert vault.get_secret("env:OPENAI_API_KEY") == SECRET
    listing = json.dumps(vault.list_items())
    assert SECRET not in listing and "env:OPENAI_API_KEY" in listing
    assert SECRET not in (vault.index_path().read_text())
    for argv, stdin in keychain.calls:
        assert not any(SECRET in a or SECRET.encode().hex() in a for a in argv)     # never in argv
    put = [c for c in keychain.calls if c[0][:2] == ["security", "-i"]]
    assert put and SECRET.encode().hex() not in put[0][0] and put[0][1]              # secret travels on stdin only


def test_plain_file_backend_is_0600(home, monkeypatch):
    monkeypatch.setattr(vault, "platform_name", lambda: "win32")
    vault.set_secret("a", SECRET)
    p = home / "vault.json"
    assert p.exists() and (p.stat().st_mode & 0o777) == 0o600
    assert (home / "vault-index.json").stat().st_mode & 0o777 == 0o600


def test_encrypted_backend_roundtrip(home, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setattr(vault, "platform_name", lambda: "win32")
    vault.unlock("correct horse")
    vault.set_secret("a", SECRET)
    assert vault.move_backend("encrypted") == 1
    assert SECRET not in (home / "vault.enc").read_text()
    assert vault.get_secret("a") == SECRET
    vault.unlock("wrong")
    with pytest.raises(vault.VaultError):
        vault.get_secret("a")


def test_host_binding_refuses_other_destinations(keychain):
    vault.set_secret("mcp-token", SECRET, type="mcp_bearer", bound="https://API.Example.com:443/mcp")
    assert vault.get_secret("mcp-token", "https://api.example.com/other") == SECRET     # normalized host+scheme+port
    for bad in ("https://evil.example.com/mcp", "http://api.example.com/mcp", "https://api.example.com:8443/mcp", None, ""):
        with pytest.raises(vault.VaultError):
            vault.get_secret("mcp-token", bad)
    assert vault.resolve_ref("vault:mcp-token", "https://evil.example.com") == ""
    assert vault.resolve_ref("vault:mcp-token", "https://api.example.com") == SECRET
    with pytest.raises(vault.VaultError):
        vault.set_secret("unbound", SECRET, type="mcp_bearer")                          # MCP creds must be bound


def test_archive_rotate_delete(keychain):
    vault.set_secret("k", "one-one-one-one", type="api_key")
    vault.archive("k")
    assert vault.get_secret("k") is None and vault.list_items()[0]["archived"] is True
    with pytest.raises(vault.VaultError):
        vault.rotate("k", "two-two-two-two")
    vault.archive("k", False)
    vault.rotate("k", "two-two-two-two")
    assert vault.get_secret("k") == "two-two-two-two"
    assert vault.delete("k") and vault.get_secret("k") is None and vault.list_items() == []


def test_redaction_registered(keychain, monkeypatch):
    from axon.session import activity
    monkeypatch.setattr(activity, "_SECRETS", set())
    vault.set_secret("k", SECRET)
    assert SECRET in activity._SECRETS
    assert SECRET not in activity.redact_text(f"token is {SECRET} ok")


def test_migration_dry_run_apply_backup_marker_and_load_order(keychain, home, monkeypatch):
    from axon.providers.catalog import find_api_key
    env = home / ".env"
    env.write_text('OPENAI_API_KEY="' + SECRET + '"\n# keep\nOTHER=1\n')
    auth = home / "mcp-auth.json"
    ep = "https://mcp.example.com/sse"
    auth.write_text(json.dumps({ep: {"name": "x", "tokens": {"access_token": "AT-0123456789", "refresh_token": "RT-0123456789"}}}))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    plan = vault.plan_migration()
    assert {p["kind"] for p in plan} == {"api_key", "mcp_oauth"} and SECRET not in json.dumps(plan)
    assert vault.list_items() == [] and "OPENAI_API_KEY" in env.read_text()            # dry run changed nothing
    assert not vault.active()

    res = vault.apply_migration(plan)
    assert res["moved"] == 2 and not res["failed"]
    assert SECRET not in env.read_text() and "OTHER=1" in env.read_text()
    assert "tokens" not in json.loads(auth.read_text())[ep]
    backup = Path(res["backup"])
    assert (backup / ".env").read_text().count(SECRET) == 1 and ((backup / ".env").stat().st_mode & 0o777) == 0o600
    assert vault.active() and vault.status()["migrated"] and vault.status()["plaintext"] == []

    assert find_api_key(["OPENAI_API_KEY"]) == SECRET                                  # vault first
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell-env-1234")
    assert find_api_key(["OPENAI_API_KEY"]) == SECRET
    vault.delete("env:OPENAI_API_KEY")
    assert find_api_key(["OPENAI_API_KEY"]) == "sk-from-the-shell-env-1234"            # then env, then the legacy file

    from axon.mcp.oauth import TokenStore
    assert TokenStore().get(ep)["tokens"]["refresh_token"] == "RT-0123456789"           # loaders read tokens from the vault
    assert vault.purge_backups() == 1 and vault.backup_dirs() == []


def test_oauth_tokens_stay_in_vault_after_migration(keychain, home):
    from axon.mcp.oauth import TokenStore
    vault.set_secret("seed", "seed-seed-seed")                                         # activates the vault
    st = TokenStore()
    st.update("https://m.example.com/mcp", name="m", tokens={"access_token": "AT-aaaaaaaaaa", "refresh_token": "RT-bbbbbbbbbb"})
    assert "AT-aaaaaaaaaa" not in (home / "mcp-auth.json").read_text() if (home / "mcp-auth.json").exists() else True
    assert st.get("https://m.example.com/mcp")["tokens"]["access_token"] == "AT-aaaaaaaaaa"
    r = vault.validate_oauth(vault.oauth_name("https://m.example.com/mcp"), spec_for=lambda e: object(),
                             manager_for=lambda s: type("M", (), {"refresh": lambda self: True})())
    assert r == {"ok": True, "result": "refreshed"}


def test_webhook_secret_reference(keychain):
    from axon.features import notify
    vault.set_secret("webhook:h.example.com", "whsec_" + "QUJD" * 8, type="webhook_secret", bound="https://h.example.com/hook")
    hook = {"url": "https://h.example.com/hook", "secret": "vault:webhook:h.example.com"}
    assert notify._hook_secret(hook, hook["url"]).startswith("whsec_")
    assert notify._hook_secret(hook, "https://evil.example.com/hook") == ""


def test_project_settings_cannot_pick_the_backend(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / ".axon").mkdir()
    (ws / ".axon" / "settings.json").write_text(json.dumps({"vault": {"backend": "plain-file"}}))
    assert vault.backend().id in vault.BACKENDS and not (home / "vault-index.json").exists()


# ─── web ───────────────────────────────────────────────────────────────────

def _req(port, method, path, host, cookie="", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host, "Origin": f"http://{host}"}
    if cookie:
        h["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, r.read().decode(), r.getheader("Set-Cookie") or ""


def test_web_routes_auth_metadata_only_and_blocked_on_phone(keychain, home, tmp_path):
    from axon.features.dashboard import make_server
    s = make_server(0, tmp_path)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    try:
        host = f"127.0.0.1:{s.port}"
        assert _req(s.port, "GET", "/api/vault", host)[0] == 401                       # no cookie
        cookie = _req(s.port, "GET", f"/?t={s.token}", host)[2].split(";")[0]
        st, out, _ = _req(s.port, "POST", "/api/vault/set", host, cookie, {"name": "env:X_API_KEY", "value": SECRET, "type": "api_key"})
        assert st == 200 and SECRET not in out
        st, out, _ = _req(s.port, "GET", "/api/vault", host, cookie)
        assert st == 200 and "env:X_API_KEY" in out and SECRET not in out
        st, out, _ = _req(s.port, "POST", "/api/vault/set", host, cookie, {"name": "bad name", "value": SECRET})
        assert st == 400 and SECRET not in out
        assert _req(s.port, "POST", "/api/vault/archive", host, cookie, {"name": "env:X_API_KEY"})[0] == 200
        assert vault.get_secret("env:X_API_KEY") is None

        base = s.phone.start("127.0.0.1")
        pport, phost = s.phone.port, f"127.0.0.1:{s.phone.port}"
        pc = _req(pport, "GET", "/?t=" + s.phone.pairing_url().split("?t=")[1], phost)[2].split(";")[0]
        for method, path in (("GET", "/api/vault"), ("POST", "/api/vault/set"), ("POST", "/api/vault/delete")):
            st, out, _ = _req(pport, method, path, phost, pc, {} if method == "POST" else None)
            assert st == 403 and "not available from a phone" in out
    finally:
        s.phone.stop()
        s.shutdown()
        s.server_close()
