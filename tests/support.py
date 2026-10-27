"""Test helpers: a scriptable stand-in for "your app" that records what it receives."""

from __future__ import annotations

import http.client
import socket
import threading
import time
from typing import Any, Callable

from rehearse.httpbase import make_server, send_raw

Handler = Callable[[dict[str, Any], int], tuple]


class Receiver:
    """HTTP server on a free port. ``handler(request, n)`` returns (status, payload[, delay[, headers]])."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or (lambda req, n: (200, b"ok"))
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __enter__(self) -> "Receiver":
        self._httpd = make_server("127.0.0.1", 0, self._on_request)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(2)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _on_request(self, h, body: bytes) -> None:
        request = {"method": h.command, "path": h.path, "headers": {k.lower(): v for k, v in h.headers.items()}, "body": body}
        with self._lock:
            self.requests.append(request)
            n = len(self.requests)
        result = self.handler(request, n)
        status, payload = result[0], result[1]
        if len(result) > 2 and result[2]:
            time.sleep(result[2])
        headers = list(result[3]) if len(result) > 3 else []
        send_raw(h, status, headers, payload if isinstance(payload, bytes) else payload.encode())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(base: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 3.0):
    """Return (status, response_headers, body). Network faults propagate as exceptions."""
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        conn.close()
