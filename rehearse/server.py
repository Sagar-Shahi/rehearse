"""A local API that misbehaves on purpose.

Point your app's third-party base URL at this server. Each route plays a *sequence* of
responses (500, then 503, then 200...), and every response can add latency, a network profile,
or a transport-level fault such as a dropped connection or a body cut off mid-stream.
"""

from __future__ import annotations

import fnmatch
import http.client
import http.server
import json
import random
import re
import socket
import struct
import threading
import time
import urllib.parse
from typing import Any, Callable

from .durations import parse_duration, resolve_network, sample_delay
from .errors import ScenarioError
from .httpbase import Server, make_server, reason, send_json, send_raw
from .scenario import validate_serve
from .template import Context, render

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}  # fmt: skip
THROTTLE_TICK = 0.05  # seconds between bandwidth-limited chunks
ADMIN_PREFIX = "/__rehearse/"


class Upstream:
    """Forwards requests to a real server (used for pass-through and fault-on-real-traffic)."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.parts = urllib.parse.urlsplit(base_url)
        if self.parts.scheme not in ("http", "https") or not self.parts.hostname:
            raise ScenarioError(f"upstream: {base_url!r} is not an http(s) URL")
        self.timeout = timeout

    def forward(self, method: str, path_and_query: str, headers: dict[str, str], body: bytes):
        conn_class = http.client.HTTPSConnection if self.parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_class(self.parts.hostname, self.parts.port, timeout=self.timeout)
        sent = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "accept-encoding"}
        try:
            conn.request(method, self.parts.path.rstrip("/") + path_and_query, body=body or None, headers=sent)
            response = conn.getresponse()
            data = response.read()
            kept = [(k, v) for k, v in response.getheaders() if k.lower() not in HOP_BY_HOP]
            return response.status, kept, data
        finally:
            conn.close()


class _Route:
    def __init__(self, index: int, spec: dict[str, Any]) -> None:
        self.index = index
        self.name = spec.get("name") or f"route {index}"
        match = spec["match"]
        methods = match.get("method")
        self.methods = {m.upper() for m in ([methods] if isinstance(methods, str) else methods)} if methods else None
        pattern = match["path"]
        self.regex = re.compile(pattern[6:]) if pattern.startswith("regex:") else None
        self.glob = pattern
        self.responses: list[dict[str, Any]] = spec["responses"]
        self.cycle = spec.get("after_last", "repeat_last") == "cycle"
        self.count = 0

    def matches(self, method: str, path: str) -> bool:
        if self.methods and method.upper() not in self.methods:
            return False
        if self.regex:
            return self.regex.fullmatch(path) is not None
        return fnmatch.fnmatchcase(path, self.glob)

    def take(self) -> int:
        """Index of the response for this call, then advance the counter."""
        n = self.count
        self.count += 1
        return n % len(self.responses) if self.cycle else min(n, len(self.responses) - 1)


class FaultServer:
    def __init__(
        self,
        config: dict[str, Any],
        host: str | None = None,
        port: int | None = None,
        log: Callable[[str], None] | None = None,
        seed: int | None = None,
    ) -> None:
        validate_serve(config)
        self.config = config
        self.host = host or config.get("host", "127.0.0.1")
        self._requested_port = config.get("port", 8080) if port is None else port
        self.routes = [_Route(i, spec) for i, spec in enumerate(config.get("routes", []))]
        self.upstream = Upstream(config["upstream"]) if config.get("upstream") else None
        self.default_network = config.get("network")
        self.calls: list[dict[str, Any]] = []
        self._log = log
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._httpd: Server | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle --------------------------------------------------------------------------

    def bind(self) -> "FaultServer":
        if self._httpd is None:
            self._httpd = make_server(self.host, self._requested_port, self._on_request)
        return self

    @property
    def port(self) -> int:
        assert self._httpd is not None, "call bind() or start() first"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FaultServer":
        """Serve in a background thread (handy in tests)."""
        self.bind()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
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

    def __enter__(self) -> "FaultServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            for route in self.routes:
                route.count = 0

    # --- request handling -------------------------------------------------------------------

    def _on_request(self, h: http.server.BaseHTTPRequestHandler, body: bytes) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        if parsed.path.startswith(ADMIN_PREFIX):
            return self._admin(h, parsed)

        method = h.command
        with self._lock:
            route = next((r for r in self.routes if r.matches(method, parsed.path)), None)
            index = route.take() if route else None
        call: dict[str, Any] = {
            "time": time.time(), "method": method, "path": parsed.path, "query": parsed.query,
            "headers": dict(h.headers.items()), "body": body[:65536].decode("utf-8", "replace"),
            "route": route.index if route else None, "response": index, "status": None, "fault": None,
        }  # fmt: skip
        with self._lock:
            self.calls.append(call)

        if route is not None:
            response = route.responses[index]
        elif self.upstream:
            response = {"proxy": True}
        else:
            call["status"] = 404
            send_json(h, 404, {"error": "no matching route", "method": method, "path": parsed.path})
            return self._announce(call, route)
        self._respond(h, response, call, parsed, body, index or 0)
        self._announce(call, route)

    def _admin(self, h: http.server.BaseHTTPRequestHandler, parsed: urllib.parse.SplitResult) -> None:
        action = parsed.path[len(ADMIN_PREFIX):]
        if action == "calls" and h.command == "GET":
            with self._lock:
                send_json(h, 200, {"calls": list(self.calls)})
        elif action == "reset" and h.command == "POST":
            self.reset()
            send_json(h, 200, {"ok": True})
        elif action == "health":
            send_json(h, 200, {"ok": True})
        else:
            send_json(h, 404, {"error": "unknown admin endpoint", "available": ["GET calls", "POST reset", "GET health"]})

    def _respond(
        self,
        h: http.server.BaseHTTPRequestHandler,
        resp: dict[str, Any],
        call: dict[str, Any],
        parsed: urllib.parse.SplitResult,
        body: bytes,
        index: int,
    ) -> None:
        net = resolve_network(resp.get("network", self.default_network))
        delay = parse_duration(resp.get("delay")) + sample_delay(net, self._rng)
        if delay:
            time.sleep(delay)

        fault = resp.get("fault")
        call["fault"] = fault
        if fault == "drop":  # accept the request, then close without answering
            return
        if fault == "hang":  # never answer, so the client's own timeout is what fires
            time.sleep(parse_duration(resp.get("hang_for", "30s")))
            return
        if fault == "reset":  # abortive close: the client sees ECONNRESET
            try:
                h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                h.connection.close()
            except OSError:
                pass
            return
        if fault == "garbage":
            self._write(h, b"\x17\x03\x01 this is not HTTP\r\n\r\n", net.bandwidth)
            return

        if resp.get("proxy"):
            try:
                status, headers, payload = self.upstream.forward(
                    h.command, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""),
                    dict(h.headers.items()), body,
                )  # fmt: skip
                headers.append(("X-Rehearse-Proxied", "1"))
            except (OSError, http.client.HTTPException) as e:
                status, headers, payload = 502, [("Content-Type", "application/json")], json.dumps(
                    {"error": f"upstream unreachable: {e}"}
                ).encode()
        else:
            status, headers, payload = self._build(resp, h, parsed, body, index)

        call["status"] = status  # recorded before the bytes leave, so tests never race the client
        head = [f"HTTP/1.1 {status} {reason(status)}"]
        head += [f"{k}: {v}" for k, v in headers if k.lower() != "content-length"]
        head += [f"Content-Length: {len(payload)}", "Connection: close"]
        raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
        if fault == "truncate":  # promise the full length, deliver only part, then close
            cut = int(len(payload) * resp.get("truncate_at", 0.5))
            self._write(h, raw + payload[:cut], net.bandwidth)
            return
        self._write(h, raw + payload, net.bandwidth)

    def _build(self, resp, h, parsed, body, index):
        try:
            parsed_json = json.loads(body) if body else {}
        except ValueError:
            parsed_json = {}
        ctx = Context(
            {
                "seq": index + 1,
                "request": {
                    "method": h.command,
                    "path": parsed.path,
                    "query": dict(urllib.parse.parse_qsl(parsed.query)),
                    "body": body.decode("utf-8", "replace"),
                    "json": parsed_json if isinstance(parsed_json, dict) else {},
                    "headers": {k.lower(): v for k, v in h.headers.items()},
                },
            },
            strict=False,
        )
        headers = [(str(k), str(render(v, ctx))) for k, v in resp.get("headers", {}).items()]
        names = {k.lower() for k, _ in headers}
        if "json" in resp:
            payload = json.dumps(render(resp["json"], ctx)).encode("utf-8")
            if "content-type" not in names:
                headers.append(("Content-Type", "application/json"))
        elif "body" in resp:
            payload = str(render(resp["body"], ctx)).encode("utf-8")
            if "content-type" not in names:
                headers.append(("Content-Type", "text/plain; charset=utf-8"))
        else:
            payload = b""
        return resp.get("status", 200), headers, payload

    @staticmethod
    def _write(h: http.server.BaseHTTPRequestHandler, data: bytes, bandwidth: float | None) -> None:
        try:
            if not bandwidth:
                h.wfile.write(data)
                h.wfile.flush()
                return
            chunk = max(1, int(bandwidth * THROTTLE_TICK))
            for i in range(0, len(data), chunk):
                h.wfile.write(data[i : i + chunk])
                h.wfile.flush()
                if i + chunk < len(data):
                    time.sleep(THROTTLE_TICK)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _announce(self, call: dict[str, Any], route: _Route | None) -> None:
        if not self._log:
            return
        where = f"{route.name}, response #{call['response'] + 1}" if route else "no route"
        outcome = f"fault:{call['fault']}" if call["fault"] else str(call["status"])
        stamp = time.strftime("%H:%M:%S", time.localtime(call["time"]))
        self._log(f"{stamp}  {call['method']:<6} {call['path']}  ->  {outcome}  ({where})")
