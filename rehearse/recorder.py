"""Capture real incoming requests into a scenario file you can replay and share.

Point a webhook provider (through a tunnel) or your own client at the recorder. It stores each
request, with the gap between them, as a "steps" scenario that `rehearse send` plays back.
"""

from __future__ import annotations

import base64
import datetime
import http.client
import http.server
import json
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .httpbase import Server, make_server, send_json, send_raw
from .server import HOP_BY_HOP, Upstream

SKIP_HEADERS = HOP_BY_HOP | {"accept-encoding", "expect"}


class Recorder:
    def __init__(
        self,
        out_path: str | Path,
        host: str = "127.0.0.1",
        port: int = 9000,
        forward: str | None = None,
        target: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.out_path = Path(out_path)
        self.host, self._requested_port = host, port
        self.upstream = Upstream(forward) if forward else None
        self.target = target or forward
        self.scenario: dict[str, Any] = {
            "name": "Recorded " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "steps": [],
        }
        if self.target:
            self.scenario["target"] = self.target
        self._last: float | None = None
        self._lock = threading.Lock()
        self._log = log
        self._httpd: Server | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle (same shape as FaultServer) ----------------------------------------------

    def bind(self) -> "Recorder":
        if self._httpd is None:
            self._httpd = make_server(self.host, self._requested_port, self._on_request)
        return self

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    def start(self) -> "Recorder":
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

    def __enter__(self) -> "Recorder":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- recording --------------------------------------------------------------------------

    def _on_request(self, h: http.server.BaseHTTPRequestHandler, body: bytes) -> None:
        self._record(h, body)
        if self.upstream:
            try:
                status, headers, payload = self.upstream.forward(h.command, h.path, dict(h.headers.items()), body)
                send_raw(h, status, headers, payload)
                return
            except (OSError, http.client.HTTPException) as e:
                send_json(h, 502, {"error": f"forward target unreachable: {e}"})
                return
        send_json(h, 200, {"ok": True, "recorded": True})

    def _record(self, h: http.server.BaseHTTPRequestHandler, body: bytes) -> None:
        send: dict[str, Any] = {"method": h.command, "path": h.path}
        headers = {k: v for k, v in h.headers.items() if k.lower() not in SKIP_HEADERS}
        if headers:
            send["headers"] = headers
        if body:
            text = _decode(body)
            if text is None:
                send["body_base64"] = base64.b64encode(body).decode("ascii")
            elif "json" in h.headers.get("Content-Type", "").lower() and _is_json(text):
                send["json"] = json.loads(text)
            else:
                send["body"] = text

        with self._lock:
            now = time.monotonic()
            if self._last is not None and now - self._last >= 0.001:
                self.scenario["steps"].append({"wait": f"{round((now - self._last) * 1000)}ms"})
            self._last = now
            self.scenario["steps"].append({"send": send})
            self.save()
        if self._log:
            self._log(f"recorded {h.command} {h.path} ({len(body)} bytes)")

    def save(self) -> None:
        """Write atomically so a Ctrl-C mid-save never leaves a half-written file."""
        tmp = self.out_path.with_suffix(self.out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.scenario, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.out_path)

    @property
    def recorded(self) -> int:
        return sum(1 for s in self.scenario["steps"] if "send" in s)


def _decode(body: bytes) -> str | None:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False
