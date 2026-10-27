"""Loading and strict validation of scenario files.

Validation is deliberately picky: a typo such as "retires" instead of "retry" would otherwise
silently change what a test does, so every unknown key is an error with a "did you mean" hint.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from .durations import parse_duration, resolve_network
from .errors import ScenarioError
from .signing import ALGOS, FORMATS

PLAY_TOP = {"name", "description", "target", "vars", "timeout", "steps"}
STEP_COMMON = {"id", "name"}
STEP_KINDS = ("send", "wait", "parallel", "shuffle")
STEP_EXTRA = {
    "send": {"send", "expect", "retry", "repeat", "every"},
    "wait": {"wait"},
    "parallel": {"parallel"},
    "shuffle": {"shuffle", "seed"},
}
SEND_KEYS = {"method", "path", "target", "headers", "json", "body", "body_base64", "corrupt", "sign", "timeout"}
CORRUPT = {"truncate", "invalid_json", "empty", "garbage", "bad_signature"}
EXPECT_KEYS = {"status", "body_contains", "max_ms", "error"}
RETRY_KEYS = {"attempts", "delay", "until"}
SIGN_KEYS = {"secret", "header", "format", "algo"}

SERVE_TOP = {"name", "description", "host", "port", "upstream", "network", "routes"}
ROUTE_KEYS = {"name", "match", "responses", "after_last"}
MATCH_KEYS = {"method", "path"}
RESPONSE_KEYS = {
    "status", "headers", "json", "body", "delay", "network",
    "fault", "truncate_at", "hang_for", "proxy",
}  # fmt: skip
FAULTS = {"drop", "reset", "truncate", "garbage", "hang"}


def load(path: str | Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ScenarioError(f"cannot read {path}: {e.strerror or e}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScenarioError(f"{path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: the top level must be a JSON object")
    return data


def kind(scenario: dict[str, Any]) -> str:
    """'play' for a file that sends events, 'serve' for one that mocks an API."""
    if "steps" in scenario:
        return "play"
    if "routes" in scenario or "upstream" in scenario:
        return "serve"
    raise ScenarioError('cannot tell what this file is: add "steps" (to send events) or "routes" (to mock an API)')


def validate(scenario: dict[str, Any]) -> str:
    which = kind(scenario)
    (validate_play if which == "play" else validate_serve)(scenario)
    return which


# --- helpers -------------------------------------------------------------------------------


def _keys(obj: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ScenarioError(f"{where}: expected an object")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        close = difflib.get_close_matches(unknown[0], sorted(allowed), n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        raise ScenarioError(f"{where}: unknown key {unknown[0]!r}{hint}; allowed: {', '.join(sorted(allowed))}")
    return obj


def _duration(value: Any, where: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as e:
        raise ScenarioError(f"{where}: {e}") from None


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ScenarioError(f"{where}: must be a whole number >= 1")
    return value


# --- send scenarios ------------------------------------------------------------------------


def validate_play(scenario: dict[str, Any]) -> None:
    _keys(scenario, PLAY_TOP, "scenario")
    if "timeout" in scenario:
        _duration(scenario["timeout"], "timeout")
    if "vars" in scenario and not isinstance(scenario["vars"], dict):
        raise ScenarioError("vars: expected an object")
    _steps(scenario.get("steps"), "steps")


def _steps(steps: Any, where: str) -> None:
    if not isinstance(steps, list) or not steps:
        raise ScenarioError(f"{where}: expected a non-empty list of steps")
    for i, step in enumerate(steps):
        _step(step, f"{where}[{i}]")


def _step(step: Any, where: str) -> None:
    if not isinstance(step, dict):
        raise ScenarioError(f"{where}: expected an object")
    present = [k for k in STEP_KINDS if k in step]
    if len(present) != 1:
        raise ScenarioError(f"{where}: a step needs exactly one of {', '.join(STEP_KINDS)} (found {len(present)})")
    which = present[0]
    _keys(step, STEP_COMMON | STEP_EXTRA[which], where)
    if which == "wait":
        _duration(step["wait"], f"{where}.wait")
    elif which == "send":
        _send(step, where)
    elif which == "parallel":
        _steps(step["parallel"], f"{where}.parallel")
    else:
        _steps(step["shuffle"], f"{where}.shuffle")
        if "seed" in step and (isinstance(step["seed"], bool) or not isinstance(step["seed"], int)):
            raise ScenarioError(f"{where}.seed: must be a whole number")


def _send(step: dict[str, Any], where: str) -> None:
    spec = _keys(step["send"], SEND_KEYS, f"{where}.send")
    bodies = [k for k in ("json", "body", "body_base64") if k in spec]
    if len(bodies) > 1:
        raise ScenarioError(f"{where}.send: use only one of json, body, body_base64 (found {', '.join(bodies)})")
    if "method" in spec and not isinstance(spec["method"], str):
        raise ScenarioError(f"{where}.send.method: expected text such as \"POST\"")
    if "path" in spec and not isinstance(spec["path"], str):
        raise ScenarioError(f"{where}.send.path: expected text such as \"/webhooks\"")
    if "headers" in spec and not isinstance(spec["headers"], dict):
        raise ScenarioError(f"{where}.send.headers: expected an object")
    if "timeout" in spec:
        _duration(spec["timeout"], f"{where}.send.timeout")
    if "corrupt" in spec and spec["corrupt"] not in CORRUPT:
        raise ScenarioError(f"{where}.send.corrupt: must be one of {', '.join(sorted(CORRUPT))}")
    if spec.get("corrupt") == "bad_signature" and "sign" not in spec:
        raise ScenarioError(f"{where}.send.corrupt: bad_signature needs a \"sign\" block to break")
    if "sign" in spec:
        sign = _keys(spec["sign"], SIGN_KEYS, f"{where}.send.sign")
        if not isinstance(sign.get("secret"), str) or not sign["secret"]:
            raise ScenarioError(f"{where}.send.sign.secret: required")
        if sign.get("format", "hex") not in FORMATS:
            raise ScenarioError(f"{where}.send.sign.format: must be one of {', '.join(sorted(FORMATS))}")
        if sign.get("algo", "sha256") not in ALGOS:
            raise ScenarioError(f"{where}.send.sign.algo: must be one of {', '.join(sorted(ALGOS))}")
    if "expect" in step:
        _expect(step["expect"], f"{where}.expect")
    if "retry" in step:
        retry = _keys(step["retry"], RETRY_KEYS, f"{where}.retry")
        _positive_int(retry.get("attempts", 3), f"{where}.retry.attempts")
        if "delay" in retry:
            _duration(retry["delay"], f"{where}.retry.delay")
        if "until" in retry:
            _expect(retry["until"], f"{where}.retry.until")
    if "repeat" in step:
        _positive_int(step["repeat"], f"{where}.repeat")
    if "every" in step:
        _duration(step["every"], f"{where}.every")


def _expect(expect: Any, where: str) -> None:
    _keys(expect, EXPECT_KEYS, where)
    status = expect.get("status")
    if status is not None:
        options = status if isinstance(status, list) else [status]
        for option in options:
            ok_int = isinstance(option, int) and not isinstance(option, bool) and 100 <= option <= 599
            ok_class = isinstance(option, str) and len(option) == 3 and option[0] in "12345" and option[1:] == "xx"
            if not (ok_int or ok_class):
                raise ScenarioError(f"{where}.status: use a code like 200, a class like \"2xx\", or a list of those")
    if "max_ms" in expect and (isinstance(expect["max_ms"], bool) or not isinstance(expect["max_ms"], (int, float))):
        raise ScenarioError(f"{where}.max_ms: expected a number of milliseconds")
    if "error" in expect and not isinstance(expect["error"], str):
        raise ScenarioError(f"{where}.error: expected text such as \"timeout\" (or \"any\")")


# --- serve scenarios -----------------------------------------------------------------------


def validate_serve(config: dict[str, Any]) -> None:
    _keys(config, SERVE_TOP, "scenario")
    if "port" in config and (
        isinstance(config["port"], bool) or not isinstance(config["port"], int) or not 0 <= config["port"] <= 65535
    ):
        raise ScenarioError("port: expected a number from 0 to 65535")
    if "network" in config:
        _network(config["network"], "network")
    routes = config.get("routes", [])
    if not isinstance(routes, list):
        raise ScenarioError("routes: expected a list")
    if not routes and not config.get("upstream"):
        raise ScenarioError('routes: add at least one route (or set "upstream" to proxy everything)')
    for i, route in enumerate(routes):
        _route(route, f"routes[{i}]", has_upstream=bool(config.get("upstream")))


def _network(spec: Any, where: str) -> None:
    try:
        resolve_network(spec)
    except ValueError as e:
        raise ScenarioError(f"{where}: {e}") from None


def _route(route: Any, where: str, has_upstream: bool) -> None:
    _keys(route, ROUTE_KEYS, where)
    match = _keys(route.get("match"), MATCH_KEYS, f"{where}.match")
    if not isinstance(match.get("path"), str):
        raise ScenarioError(f'{where}.match.path: required, e.g. "/v1/charges", "/v1/users/*" or "regex:^/v1/.+"')
    method = match.get("method")
    if method is not None and not (
        isinstance(method, str) or (isinstance(method, list) and all(isinstance(m, str) for m in method))
    ):
        raise ScenarioError(f"{where}.match.method: expected \"POST\" or [\"GET\", \"POST\"]")
    if route.get("after_last", "repeat_last") not in ("repeat_last", "cycle"):
        raise ScenarioError(f"{where}.after_last: must be \"repeat_last\" or \"cycle\"")
    responses = route.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ScenarioError(f"{where}.responses: expected a non-empty list")
    for j, response in enumerate(responses):
        _response(response, f"{where}.responses[{j}]", has_upstream)


def _response(resp: Any, where: str, has_upstream: bool) -> None:
    _keys(resp, RESPONSE_KEYS, where)
    status = resp.get("status", 200)
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ScenarioError(f"{where}.status: expected an HTTP status code such as 200 or 503")
    if "json" in resp and "body" in resp:
        raise ScenarioError(f"{where}: use either json or body, not both")
    if "headers" in resp and not isinstance(resp["headers"], dict):
        raise ScenarioError(f"{where}.headers: expected an object")
    if "delay" in resp:
        _duration(resp["delay"], f"{where}.delay")
    if "hang_for" in resp:
        _duration(resp["hang_for"], f"{where}.hang_for")
    if "network" in resp:
        _network(resp["network"], f"{where}.network")
    if "fault" in resp and resp["fault"] not in FAULTS:
        raise ScenarioError(f"{where}.fault: must be one of {', '.join(sorted(FAULTS))}")
    if "truncate_at" in resp:
        at = resp["truncate_at"]
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at < 1:
            raise ScenarioError(f"{where}.truncate_at: expected a fraction from 0 up to (not including) 1")
    if resp.get("proxy") and not has_upstream:
        raise ScenarioError(f'{where}.proxy: needs a top-level "upstream" URL to forward to')
