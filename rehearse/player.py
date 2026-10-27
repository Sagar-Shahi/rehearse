"""Plays a "steps" scenario against your app: signed webhooks, delays, retries, chaos."""

from __future__ import annotations

import base64
import http.client
import json
import random
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .durations import parse_duration
from .errors import ScenarioError
from .scenario import validate_play
from .signing import sign_body
from .template import Context, render

DEFAULT_TIMEOUT = 10.0
MAX_BODY = 1_000_000


@dataclass
class StepResult:
    label: str
    kind: str  # "send", "wait" or "info"
    start: float  # seconds since the run began
    duration: float
    ok: bool = True
    status: int | None = None
    error: str | None = None
    attempts: int = 1
    failures: list[str] = field(default_factory=list)
    depth: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "kind": self.kind, "start": round(self.start, 4),
            "duration": round(self.duration, 4), "ok": self.ok, "status": self.status,
            "error": self.error, "attempts": self.attempts, "failures": self.failures, "note": self.note,
        }  # fmt: skip


def _status_matches(want: Any, status: int | None) -> bool:
    if status is None:
        return False
    options = want if isinstance(want, list) else [want]
    for option in options:
        if isinstance(option, int) and status == option:
            return True
        if isinstance(option, str) and str(status).startswith(option[0]):
            return True
    return False


def check_expect(expect: dict[str, Any] | None, status: int | None, body: str, elapsed: float, error: str | None) -> list[str]:
    """Return a list of human-readable failures (empty means the expectation holds)."""
    expect = expect or {}
    if "error" in expect:
        want = expect["error"]
        if error is None:
            return [f"expected the request to fail with {want!r} but got status {status}"]
        if want != "any" and want not in error:
            return [f"expected error {want!r}, got {error!r}"]
        return []
    if error:
        return [f"request failed: {error}"]
    failures: list[str] = []
    if "status" in expect and not _status_matches(expect["status"], status):
        failures.append(f"expected status {expect['status']}, got {status}")
    needles = expect.get("body_contains")
    if needles is not None:
        for needle in needles if isinstance(needles, list) else [needles]:
            if needle not in body:
                failures.append(f"response body does not contain {needle!r}")
    if "max_ms" in expect and elapsed * 1000 > expect["max_ms"]:
        failures.append(f"took {elapsed * 1000:.0f}ms, limit is {expect['max_ms']}ms")
    return failures


class Player:
    def __init__(
        self,
        scenario: dict[str, Any],
        target: str | None = None,
        speed: float = 1.0,
        on_result: Callable[[StepResult], None] | None = None,
    ) -> None:
        validate_play(scenario)
        if speed <= 0:
            raise ScenarioError("speed must be greater than zero")
        self.scenario = scenario
        self.target = target or scenario.get("target")
        self.speed = speed
        self.on_result = on_result
        self.results: list[StepResult] = []
        self.default_timeout = parse_duration(scenario["timeout"]) if "timeout" in scenario else DEFAULT_TIMEOUT
        self._ctx = Context(dict(scenario.get("vars", {})))
        self._lock = threading.Lock()
        self._seq = 0
        self._t0 = 0.0

    # --- public -----------------------------------------------------------------------------

    def run(self) -> list[StepResult]:
        self._t0 = time.monotonic()
        self._run_steps(self.scenario["steps"], depth=0)
        return self.results

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    # --- plumbing ---------------------------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _emit(self, result: StepResult) -> None:
        with self._lock:
            self.results.append(result)
            if self.on_result:
                self.on_result(result)

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds / self.speed)

    def _run_steps(self, steps: list[dict[str, Any]], depth: int) -> None:
        for step in steps:
            self._run_step(step, depth)

    def _run_step(self, step: dict[str, Any], depth: int) -> None:
        if "wait" in step:
            start = self._now()
            self._pause(parse_duration(step["wait"]))
            label = step.get("name") or f"wait {step['wait']}"
            self._emit(StepResult(label, "wait", start, self._now() - start, depth=depth))
        elif "send" in step:
            self._run_send(step, depth)
        elif "parallel" in step:
            self._run_parallel(step["parallel"], depth)
        else:
            self._run_shuffle(step, depth)

    def _run_parallel(self, children: list[dict[str, Any]], depth: int) -> None:
        errors: list[BaseException] = []

        def work(child: dict[str, Any]) -> None:
            try:
                self._run_step(child, depth + 1)
            except BaseException as e:  # re-raised on the main thread below
                errors.append(e)

        self._emit(StepResult(f"parallel x{len(children)}", "info", self._now(), 0.0, depth=depth))
        threads = [threading.Thread(target=work, args=(child,)) for child in children]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]

    def _run_shuffle(self, step: dict[str, Any], depth: int) -> None:
        children = list(step["shuffle"])
        seed = step["seed"] if "seed" in step else random.SystemRandom().randrange(2**32)
        random.Random(seed).shuffle(children)
        names = [c.get("id") or c.get("name") or f"#{i}" for i, c in enumerate(children)]
        order = " > ".join(str(n) for n in names)
        self._emit(StepResult(f"shuffle seed={seed}", "info", self._now(), 0.0, depth=depth, note=order))
        for child in children:
            self._run_step(child, depth + 1)

    # --- sending ----------------------------------------------------------------------------

    def _run_send(self, step: dict[str, Any], depth: int) -> None:
        times = step.get("repeat", 1)
        gap = parse_duration(step.get("every"))
        for i in range(1, times + 1):
            if i > 1:
                self._pause(gap)
            self._run_one(step, depth, i if times > 1 else None, times)

    def _run_one(self, step: dict[str, Any], depth: int, iteration: int | None, times: int) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        ctx = self._ctx.child(seq=seq, repeat=iteration or 1)
        request = self._build_request(step["send"], ctx)  # rendered once: retries resend the same event
        label = step.get("name") or step.get("id") or f"{request['method']} {request['path']}"
        expect = step.get("expect")
        retry = step.get("retry")
        until = retry.get("until") if retry else None
        stop_when = until or expect or {"status": "2xx"}
        attempts = retry.get("attempts", 3) if retry else 1
        delay = parse_duration(retry.get("delay")) if retry else 0.0

        start = self._now()
        used = 0
        status, body, error, elapsed = None, "", None, 0.0
        for used in range(1, attempts + 1):
            status, body, error, elapsed = self._send_once(request)
            if not check_expect(stop_when, status, body, elapsed, error):
                break
            if used < attempts:
                self._pause(delay)
        judge = expect if expect else (until if retry else {})
        failures = check_expect(judge, status, body, elapsed, error)
        note = f"repeat {iteration}/{times}" if iteration else ""
        self._emit(
            StepResult(label, "send", start, self._now() - start, not failures, status, error, used, failures, depth, note)
        )

    def _build_request(self, spec: dict[str, Any], ctx: Context) -> dict[str, Any]:
        spec = render(spec, ctx)
        target = spec.get("target") or self.target
        if not target:
            raise ScenarioError('no target URL: set "target" in the scenario or pass --target')
        path = spec.get("path", "/")
        if not path.startswith("/"):
            path = "/" + path
        headers = {str(k): str(v) for k, v in spec.get("headers", {}).items()}

        if "json" in spec:
            body = json.dumps(spec["json"], separators=(",", ":")).encode("utf-8")
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"
        elif "body_base64" in spec:
            body = base64.b64decode(spec["body_base64"])
        elif "body" in spec:
            body = str(spec["body"]).encode("utf-8")
        else:
            body = b""

        corrupt = spec.get("corrupt")
        if corrupt == "truncate":
            body = body[: len(body) // 2]
        elif corrupt == "invalid_json":
            body = body[:-1] + b",," if body else b"{"
        elif corrupt == "empty":
            body = b""
        elif corrupt == "garbage":
            body = b"\x00\xffnot-json\xfe"

        if "sign" in spec:  # signed over the final body, so only "bad_signature" fails verification
            name, value = sign_body(body, **spec["sign"])
            if corrupt == "bad_signature":
                value = value[:-1] + ("0" if value[-1] != "0" else "1")
            headers[name] = value

        timeout = parse_duration(spec["timeout"]) if "timeout" in spec else self.default_timeout
        return {
            "method": spec.get("method", "POST").upper(),
            "url": target.rstrip("/") + path,
            "path": path,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        }

    @staticmethod
    def _send_once(req: dict[str, Any]) -> tuple[int | None, str, str | None, float]:
        parts = urllib.parse.urlsplit(req["url"])
        conn_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_class(parts.hostname, parts.port, timeout=req["timeout"])
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        began = time.monotonic()
        status: int | None = None
        try:
            conn.request(req["method"], target, body=req["body"] or None, headers=req["headers"])
            response = conn.getresponse()
            status = response.status
            data = response.read(MAX_BODY)
            return status, data.decode("utf-8", "replace"), None, time.monotonic() - began
        except TimeoutError:
            error = "timeout"
        except ConnectionRefusedError:
            error = "connection refused"
        except http.client.RemoteDisconnected:
            error = "connection closed without a response"
        except ConnectionResetError:
            error = "connection reset"
        except http.client.IncompleteRead:
            error = "response cut off mid-body"
        except http.client.HTTPException as e:
            error = f"invalid response ({type(e).__name__})"
        except OSError as e:
            error = f"network error: {e}"
        finally:
            conn.close()
        return status, "", error, time.monotonic() - began
