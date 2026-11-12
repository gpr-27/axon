"""Phone access: a second, opt-in listener so a phone on the same network can open the app and chat.

The dashboard itself stays on loopback. ``/phone`` adds one more listening socket, bound to this machine's LAN
address only, that serves the *same* server (one session, shown live in the terminal, the browser and the phone).
It is guarded separately from loopback:

  * off until asked for, and closed again by ``/phone stop``;
  * a pairing link/QR carries a one-time token that expires after ``PAIR_TTL`` seconds; scanning it signs that
    phone in with its own cookie (never the loopback cookie, which is useless on the LAN listener and vice versa);
  * only private-network peers (home/office LAN, link-local, Tailscale/CGNAT) are accepted, and the ``Host`` header
    must be this machine's own address, so a web page cannot reach it by DNS rebinding;
  * the raw terminal (``/api/term``) and shell runner are not served on it; chat, files and commands are.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import socket
import threading
import time
from typing import Any

PAIR_TTL = 600.0
PREFERRED_PORT_OFFSET = 1
_CGNAT = ipaddress.ip_network("100.64.0.0/10")     # Tailscale and carrier-grade NAT
BLOCKED_PREFIXES = ("/api/term", "/api/shell", "/api/vault")


def lan_address() -> str | None:
    """This machine's address on the local network (the one a phone would reach), or None when offline."""
    for target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))              # UDP connect sends nothing; it only picks the outgoing interface
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
        except OSError:
            continue
    return None


def is_private_peer(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_link_local or (ip.version == 4 and ip in _CGNAT)) and not ip.is_unspecified


class PhoneAccess:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.active = False
        self.ip = ""
        self.port = 0
        self.cookie = ""
        self.devices = 0
        self._token = ""
        self._expires = 0.0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── lifecycle ──
    def start(self, ip: str | None = None) -> str:
        """Open the LAN listener (idempotent) and return its base URL."""
        with self._lock:
            if self.active:
                return self.url
            ip = ip or lan_address()
            if not ip:
                raise OSError("no network address found: connect to Wi-Fi or a LAN first")
            try:
                if not is_private_peer(ip):
                    raise OSError(f"{ip} is not a private network address; phone access is for your own network")
            except ValueError as e:
                raise OSError(str(e)) from None
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                try:
                    sock.bind((ip, int(self.server.port) + PREFERRED_PORT_OFFSET))
                except OSError:
                    sock.bind((ip, 0))
                sock.listen(64)
            except OSError:
                sock.close()
                raise
            sock.settimeout(0.5)
            self._sock, self.ip, self.port = sock, ip, sock.getsockname()[1]
            self.cookie = secrets.token_urlsafe(32)
            self.devices = 0
            self.active = True
            self._thread = threading.Thread(target=self._serve, name="axon-phone", daemon=True)
            self._thread.start()
            return self.url

    def stop(self) -> bool:
        with self._lock:
            if not self.active:
                return False
            self.active = False
            self.cookie = ""        # every phone that was signed in is signed out
            self._token = ""
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return True

    def _serve(self) -> None:
        while self.active and self._sock is not None:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not is_private_peer(addr[0]):
                conn.close()
                continue
            try:
                self.server.process_request(conn, addr)
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass

    # ── addresses and pairing ──
    @property
    def url(self) -> str:
        return f"http://{self.ip}:{self.port}" if self.active else ""

    def pairing_url(self) -> str:
        """A fresh one-time sign-in link (the QR code). Older unused links stop working."""
        self._token = secrets.token_urlsafe(24)
        self._expires = time.monotonic() + PAIR_TTL
        return f"{self.url}/?t={self._token}"

    def consume(self, token: str) -> bool:
        ok = bool(self._token) and time.monotonic() < self._expires and hmac.compare_digest(token.encode(), self._token.encode())
        if ok:
            self._token = ""            # single use
            self.devices += 1
        return ok

    # ── per-request checks ──
    def serves(self, local_port: int) -> bool:
        return self.active and local_port == self.port

    def host_ok(self, host: str, port: int | None) -> bool:
        return host == self.ip and (port is None or port == self.port)

    def origins(self) -> set[str]:
        return {self.url} if self.active else set()

    def cookie_ok(self, value: str) -> bool:
        return bool(self.cookie) and hmac.compare_digest(value.encode(), self.cookie.encode())

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "url": self.url, "devices": self.devices, "ip": self.ip, "port": self.port}
