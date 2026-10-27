"""{{variable}} substitution used inside scenario files.

Built-ins: {{uuid}} (fresh each time), {{now}} (ISO-8601 UTC), {{timestamp}} (epoch seconds).
Everything else comes from the scenario's "vars", the step ({{seq}}, {{repeat}}) or, on the
mock server, the incoming request ({{request.json.name}}, {{request.headers.x-api-key}}).
"""

from __future__ import annotations

import datetime
import json
import re
import time
import uuid
from typing import Any

from .errors import ScenarioError

_VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")


class Context:
    def __init__(self, values: dict[str, Any] | None = None, strict: bool = True) -> None:
        self.values = dict(values or {})
        self.strict = strict  # strict: unknown variable is an error; lenient: renders as ""

    def child(self, **extra: Any) -> "Context":
        child = Context(self.values, self.strict)
        child.values.update(extra)
        return child

    def lookup(self, name: str) -> Any:
        if name == "uuid":
            return str(uuid.uuid4())
        if name == "now":
            return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if name == "timestamp":
            return int(time.time())
        current: Any = self.values
        for part in name.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif self.strict:
                raise ScenarioError(f"unknown variable {{{{{name}}}}}")
            else:
                return ""
        return current


def _to_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def render(value: Any, ctx: Context) -> Any:
    """Substitute variables in strings, recursively. A value that is exactly one
    placeholder keeps its type, so "{{amount}}" can become the number 4200."""
    if isinstance(value, str):
        whole = _VAR.fullmatch(value)
        if whole:
            return ctx.lookup(whole.group(1))
        return _VAR.sub(lambda m: _to_text(ctx.lookup(m.group(1))), value)
    if isinstance(value, list):
        return [render(item, ctx) for item in value]
    if isinstance(value, dict):
        return {key: render(item, ctx) for key, item in value.items()}
    return value
