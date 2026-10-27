"""Tiny shared HTTP plumbing for the mock server, the recorder and the demo app."""

from __future__ import annotations

import http
import http.server
import json
from typing import Callable

RequestHandler = Callable[[http.server.BaseHTTPRequestHandler, bytes], None]


def reason(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Status"


def read_body(h: http.server.BaseHTTPRequestHandler) -> bytes:
    """Read a request body, supporting both Content-Length and chunked encoding."""
    if "chunked" in h.headers.get("Transfer-Encoding", "").lower():
        parts: list[bytes] = []
        while True:
            size = int(h.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while h.rfile.readline().strip():  # skip trailers up to the blank line
                    pass
                return b"".join(parts)
            parts.append(h.rfile.read(size))
            h.rfile.readline()  # the CRLF after each chunk
    length = int(h.headers.get("Content-Length") or 0)
    return h.rfile.read(length) if length else b""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one request per connection keeps fault injection simple

    def log_message(self, *args: object) -> None:  # silence the default stderr access log
        pass

    def _dispatch(self) -> None:
        try:
            body = read_body(self)
            self.server.on_request(self, body)  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away (often on purpose, e.g. a timeout test)
        except Exception as e:  # never leak a traceback into the user's terminal
            try:
                send_json(self, 500, {"error": f"rehearse internal error: {e}"})
            except OSError:
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    on_request: RequestHandler


def make_server(host: str, port: int, on_request: RequestHandler) -> Server:
    server = Server((host, port), _Handler)
    server.on_request = on_request
    return server


def send_raw(h: http.server.BaseHTTPRequestHandler, status: int, headers: list[tuple[str, str]], payload: bytes) -> None:
    lines = [f"HTTP/1.1 {status} {reason(status)}"]
    seen = {k.lower() for k, _ in headers}
    lines += [f"{k}: {v}" for k, v in headers]
    if "content-length" not in seen:
        lines.append(f"Content-Length: {len(payload)}")
    lines.append("Connection: close")
    h.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    h.wfile.flush()


def send_json(h: http.server.BaseHTTPRequestHandler, status: int, obj: object) -> None:
    send_raw(h, status, [("Content-Type", "application/json")], json.dumps(obj).encode("utf-8"))
