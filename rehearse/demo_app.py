"""A deliberately well-behaved webhook receiver, so `rehearse send` has something to talk to.

    rehearse demo-app            # listens on http://127.0.0.1:3000
    rehearse send examples/webhook.json

It also documents what a *robust* receiver does, and the test-suite uses it as the reference:

* /webhooks/payments  verifies a Stripe-style signature (401), rejects broken JSON (400),
                      ignores duplicate event ids (200), and asks for a retry (409) when a
                      refund shows up before the payment it refunds.
* /webhooks/flaky     answers 503 twice per event id, then 200, to exercise retry logic.
"""

from __future__ import annotations

import hmac
import http.server
import json
import threading
import time
import urllib.parse
from typing import Any, Callable

from .httpbase import Server, make_server, send_json, send_raw

SECRET = "whsec_test_123"
TOLERANCE = 300  # seconds a signature timestamp may differ from now


def verify_stripe(header: str, body: bytes, secret: str = SECRET, now: float | None = None) -> bool:
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        stamp, claimed = int(fields["t"]), fields["v1"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - stamp) > TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, "sha256").hexdigest()
    return hmac.compare_digest(expected, claimed)


class DemoApp:
    def __init__(self, host: str = "127.0.0.1", port: int = 3000, log: Callable[[str], None] | None = None) -> None:
        self.host, self._requested_port, self._log = host, port, log
        self.seen: set[str] = set()
        self.orders: dict[str, set[str]] = {}
        self.attempts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._httpd: Server | None = None
        self._thread: threading.Thread | None = None

    def bind(self) -> "DemoApp":
        if self._httpd is None:
            self._httpd = make_server(self.host, self._requested_port, self._on_request)
        return self

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    def start(self) -> "DemoApp":
        self.bind()
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.bind()
        self._httpd.serve_forever(poll_interval=0.1)

    def stop(self) -> None:
        if self._httpd is None:
            return
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=2)
        self._httpd.server_close()
        self._httpd = None

    def __enter__(self) -> "DemoApp":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- the app ----------------------------------------------------------------------------

    def _on_request(self, h: http.server.BaseHTTPRequestHandler, body: bytes) -> None:
        path = urllib.parse.urlsplit(h.path).path
        if h.command == "GET" and path == "/":
            return send_json(h, 200, {"app": "rehearse demo", "webhooks": ["/webhooks/payments", "/webhooks/flaky"]})
        if h.command != "POST":
            return send_json(h, 405, {"error": "method not allowed"})
        if path == "/webhooks/payments":
            status, payload = self._payments(h.headers.get("Stripe-Signature", ""), body)
        elif path == "/webhooks/flaky":
            status, payload = self._flaky(body)
        else:
            status, payload = 404, {"error": "not found"}
        if self._log:
            self._log(f"{h.command} {path} -> {status}")
        extra = [("Retry-After", "1")] if status == 503 else []
        send_raw(h, status, [("Content-Type", "application/json"), *extra], json.dumps(payload).encode())

    def _payments(self, signature: str, body: bytes) -> tuple[int, dict[str, Any]]:
        if not verify_stripe(signature, body):
            return 401, {"error": "invalid signature"}
        try:
            event = json.loads(body)
            event_id, event_type = event["id"], event["type"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "body is not a valid event"}
        with self._lock:
            if event_id in self.seen:
                return 200, {"status": "duplicate"}
            done = self.orders.setdefault(str(event.get("order")), set())
            if event_type == "payment.refunded" and "payment.succeeded" not in done:
                return 409, {"error": "refund arrived before the payment succeeded", "retry": True}
            self.seen.add(event_id)
            done.add(event_type)
        return 200, {"status": "processed"}

    def _flaky(self, body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            key = str(json.loads(body).get("id", "?"))
        except (ValueError, AttributeError):
            key = "?"
        with self._lock:
            self.attempts[key] = self.attempts.get(key, 0) + 1
            attempt = self.attempts[key]
        if attempt < 3:
            return 503, {"error": "temporarily unavailable", "attempt": attempt}
        return 200, {"status": "processed", "attempt": attempt}
