"""Esc must stop a request even while the server is silent (gateways that send nothing until done)."""
import http.server
import threading
import time

import pytest

from axon.providers.base import StreamCanceller


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        time.sleep(5)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"late")
        except OSError:
            pass

    def log_message(self, *args):
        pass


class _QuickHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_cancel_while_waiting_for_first_bytes():
    import httpx
    server, url = _serve(_SlowHandler)
    try:
        canceller = StreamCanceller()
        canceller.begin()
        threading.Timer(0.4, canceller.cancel).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with canceller.open_stream(lambda: httpx.stream("POST", url, timeout=30)):
                pass
        assert time.monotonic() - started < 2.0
    finally:
        server.shutdown()


def test_open_stream_returns_the_response_when_not_cancelled():
    import httpx
    server, url = _serve(_QuickHandler)
    try:
        canceller = StreamCanceller()
        canceller.begin()
        with canceller.open_stream(lambda: httpx.stream("POST", url, timeout=10)) as response:
            assert response.status_code == 200
            assert response.read() == b"hello"
    finally:
        server.shutdown()
