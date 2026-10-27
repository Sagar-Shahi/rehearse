"""Durations ("500ms", "2s", "1.5m") and network profiles ("3g", "slow-3g", ...)."""

from __future__ import annotations

import random
import re
from typing import Any, NamedTuple

_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0}
_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)\s*")


def parse_duration(value: Any) -> float:
    """Return seconds. A bare number means milliseconds; None means zero."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"invalid duration {value!r}; use e.g. 500ms, 2s, 1.5m")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("durations cannot be negative")
        return value / 1000.0
    if isinstance(value, str):
        match = _DURATION.fullmatch(value)
        if match:
            return float(match.group(1)) * _UNITS[match.group(2)]
    raise ValueError(f"invalid duration {value!r}; use e.g. 500ms, 2s, 1.5m")


class Network(NamedTuple):
    latency: float = 0.0  # seconds added before the response starts
    jitter: float = 0.0  # +/- seconds of random variation on the latency
    bandwidth: float | None = None  # bytes per second, None = unlimited


# Rough numbers modelled on common browser throttling presets.
PROFILES: dict[str, Network] = {
    "4g": Network(0.05, 0.02, 1_500_000),
    "3g": Network(0.15, 0.05, 200_000),
    "slow-3g": Network(0.40, 0.10, 50_000),
    "2g": Network(0.80, 0.30, 6_000),
}

_NETWORK_KEYS = {"latency", "jitter", "bandwidth"}


def resolve_network(spec: Any) -> Network:
    """Accept None, a profile name, or {"latency": "300ms", "jitter": "50ms", "bandwidth": 50000}."""
    if spec is None:
        return Network()
    if isinstance(spec, str):
        if spec not in PROFILES:
            raise ValueError(f"unknown network profile {spec!r}; choose from {', '.join(PROFILES)}")
        return PROFILES[spec]
    if isinstance(spec, dict):
        unknown = set(spec) - _NETWORK_KEYS
        if unknown:
            raise ValueError(f"unknown network key {sorted(unknown)[0]!r}; allowed: latency, jitter, bandwidth")
        bandwidth = spec.get("bandwidth")
        if bandwidth is not None and (
            isinstance(bandwidth, bool) or not isinstance(bandwidth, (int, float)) or bandwidth <= 0
        ):
            raise ValueError("bandwidth must be a positive number of bytes per second")
        return Network(parse_duration(spec.get("latency")), parse_duration(spec.get("jitter")), bandwidth)
    raise ValueError("network must be a profile name or an object")


def sample_delay(net: Network, rng: random.Random) -> float:
    """Latency for one response, with jitter, never negative."""
    if net.latency == 0 and net.jitter == 0:
        return 0.0
    return max(0.0, net.latency + rng.uniform(-net.jitter, net.jitter))
