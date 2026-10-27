"""HMAC webhook signatures in the formats providers actually use."""

from __future__ import annotations

import hmac
import time

DEFAULT_HEADERS = {
    "hex": "X-Signature",  # hex(HMAC(body))
    "github": "X-Hub-Signature-256",  # "sha256=" + hex(HMAC(body))
    "stripe": "Stripe-Signature",  # "t=<unix>,v1=" + hex(HMAC("<unix>.<body>"))
}
FORMATS = frozenset(DEFAULT_HEADERS)
ALGOS = frozenset({"sha1", "sha256", "sha512"})


def sign_body(
    body: bytes,
    secret: str,
    header: str | None = None,
    format: str = "hex",
    algo: str = "sha256",
    now: float | None = None,
) -> tuple[str, str]:
    """Return (header_name, header_value) for ``body``."""
    key = secret.encode("utf-8")
    if format == "stripe":
        stamp = int(time.time() if now is None else now)
        mac = hmac.new(key, f"{stamp}.".encode() + body, algo).hexdigest()
        value = f"t={stamp},v1={mac}"
    else:
        mac = hmac.new(key, body, algo).hexdigest()
        value = mac if format == "hex" else f"{algo}={mac}"
    return header or DEFAULT_HEADERS[format], value
