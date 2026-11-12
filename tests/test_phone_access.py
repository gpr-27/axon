"""/phone: a second listener for devices on the LAN, guarded separately from the loopback dashboard."""
from __future__ import annotations

import http.client
import threading
from pathlib import Path

import pytest

from axon.features import dash_phone
from axon.features.dashboard import make_server


@pytest.fixture()
def srv(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    s = make_server(0, tmp_path)
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield s
    s.phone.stop()
    s.shutdown()
    s.server_close()


def _get(port: int, path: str, host: str, cookie: str = "") -> http.client.HTTPResponse:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host}
    if cookie:
        h["Cookie"] = cookie
    c.request("GET", path, headers=h)
    return c.getresponse()


def _cookie(resp: http.client.HTTPResponse) -> str:
    return (resp.getheader("Set-Cookie") or "").split(";")[0]


def test_phone_is_off_by_default(srv):
    assert srv.phone.active is False and srv.phone.url == ""


def test_pairing_signs_a_phone_in_once_and_loopback_cookie_is_useless_there(srv):
    base = srv.phone.start("127.0.0.1")
    port = srv.phone.port
    assert port != srv.port and base == f"http://127.0.0.1:{port}"
    link = srv.phone.pairing_url()
    token = link.split("?t=")[1]
    host = f"127.0.0.1:{port}"
    assert _get(port, "/", host).status == 401                       # no cookie, no page
    r = _get(port, f"/?t={token}", host)
    assert r.status == 302
    phone_cookie = _cookie(r)
    assert phone_cookie.startswith("axon_dash=")
    assert _get(port, "/", host, phone_cookie).status == 200         # signed in
    assert _get(port, f"/?t={token}", host).status == 403            # the link works once
    loop = _get(srv.port, f"/?t={srv.token}", f"127.0.0.1:{srv.port}")
    assert _get(port, "/", host, _cookie(loop)).status == 401        # the loopback cookie does not work on the phone listener
    assert _get(srv.port, "/", f"127.0.0.1:{srv.port}", phone_cookie).status == 401   # nor the phone cookie on loopback


def test_phone_listener_rejects_foreign_host_and_hides_terminal(srv):
    srv.phone.start("127.0.0.1")
    port = srv.phone.port
    cookie = _cookie(_get(port, f"/?t={srv.phone.pairing_url().split('?t=')[1]}", f"127.0.0.1:{port}"))
    assert _get(port, "/", "evil.example.com", cookie).status == 403          # DNS rebinding
    assert _get(port, "/api/term/stream", f"127.0.0.1:{port}", cookie).status == 403
    assert _get(port, "/api/phone", f"127.0.0.1:{port}", cookie).status == 403


def test_expired_token_and_stop_revokes(srv, monkeypatch):
    srv.phone.start("127.0.0.1")
    port = srv.phone.port
    token = srv.phone.pairing_url().split("?t=")[1]
    monkeypatch.setattr(dash_phone.time, "monotonic", lambda: 10**9)
    assert _get(port, f"/?t={token}", f"127.0.0.1:{port}").status == 403
    monkeypatch.undo()
    cookie = _cookie(_get(port, f"/?t={srv.phone.pairing_url().split('?t=')[1]}", f"127.0.0.1:{port}"))
    srv.phone.stop()
    with pytest.raises(OSError):
        _get(port, "/", f"127.0.0.1:{port}", cookie)
    assert not srv.phone.cookie_ok(cookie.split("=", 1)[1])


def test_private_peer_rules():
    assert dash_phone.is_private_peer("192.168.1.20") and dash_phone.is_private_peer("10.0.0.5")
    assert dash_phone.is_private_peer("100.100.1.1") and dash_phone.is_private_peer("::ffff:192.168.0.9")
    assert not dash_phone.is_private_peer("8.8.8.8") and not dash_phone.is_private_peer("not-an-ip")


def test_refuses_public_address(srv):
    with pytest.raises(OSError):
        srv.phone.start("8.8.8.8")
