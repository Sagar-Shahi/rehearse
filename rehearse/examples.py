"""Example scenarios written by `rehearse init`. Both are checked by the test-suite."""

from __future__ import annotations

import json

_EVENT = {
    "id": "evt_{{uuid}}",
    "type": "payment.created",
    "order": "{{order}}",
    "amount": "{{amount}}",
    "created": "{{now}}",
}


def _event(kind: str, **extra: object) -> dict:
    return {**_EVENT, "type": kind, **extra}


def _post(body: dict, **extra: object) -> dict:
    return {
        "path": "/webhooks/payments",
        "json": body,
        "sign": {"secret": "{{secret}}", "format": "stripe"},
        **extra,
    }


WEBHOOK = {
    "name": "Payment webhook edge cases",
    "description": "Run against `rehearse demo-app`, or point target at your own app and adjust the paths.",
    "target": "http://127.0.0.1:3000",
    "vars": {"secret": "whsec_test_123", "order": "ord_1001", "amount": 4200},
    "steps": [
        {"name": "happy path", "send": _post(_event("payment.created")), "expect": {"status": "2xx"}},
        {"wait": "1s"},
        {
            "name": "body is not valid JSON",
            "send": _post(_event("payment.created"), corrupt="invalid_json"),
            "expect": {"status": [400, 422]},
        },
        {
            "name": "signature does not match",
            "send": _post(_event("payment.created"), corrupt="bad_signature"),
            "expect": {"status": [400, 401, 403]},
        },
        {
            "name": "same event delivered 3 times",
            "repeat": 3,
            "every": "100ms",
            "send": _post({**_event("payment.created"), "id": "evt_duplicate_1"}),
            "expect": {"status": "2xx"},
        },
        {
            "name": "events arrive out of order",
            "shuffle": [
                {"id": "succeeded", "send": _post(_event("payment.succeeded")), "expect": {"status": [200, 409]}},
                {"id": "refunded", "send": _post(_event("payment.refunded")), "expect": {"status": [200, 409]}},
                {"id": "created", "send": _post(_event("payment.created")), "expect": {"status": [200, 409]}},
            ],
            "seed": 7,
        },
        {
            "name": "receiver recovers after an outage",
            "send": {
                "path": "/webhooks/flaky",
                "json": {"id": "evt_flaky_{{uuid}}", "type": "payment.created"},
            },
            "retry": {"attempts": 5, "delay": "200ms", "until": {"status": 200}},
        },
    ],
}

API = {
    "name": "Flaky payments API",
    "description": "Set your app's payments base URL to http://localhost:8080 and watch how it copes.",
    "port": 8080,
    "routes": [
        {
            "name": "charge: retry storm",
            "match": {"method": "POST", "path": "/v1/charges"},
            "responses": [
                {"status": 500, "json": {"error": "internal_error"}},
                {"status": 503, "headers": {"Retry-After": "1"}, "json": {"error": "unavailable"}},
                {"status": 200, "delay": "300ms", "json": {"id": "ch_{{uuid}}", "status": "succeeded"}},
            ],
        },
        {
            "name": "customers on slow 3G",
            "match": {"method": "GET", "path": "/v1/customers/*"},
            "responses": [{"status": 200, "network": "slow-3g", "json": {"id": "cus_1", "email": "asha@example.com"}}],
        },
        {
            "name": "refunds drop the connection",
            "match": {"path": "/v1/refunds"},
            "responses": [{"fault": "drop"}, {"status": 200, "json": {"id": "re_1"}}],
        },
        {
            "name": "event list cut off mid-body",
            "match": {"path": "/v1/events"},
            "responses": [{"status": 200, "json": {"data": [{"id": "evt_1"}, {"id": "evt_2"}]}, "fault": "truncate"}],
        },
        {
            "name": "echo the request",
            "match": {"method": "POST", "path": "/v1/echo"},
            "responses": [{"json": {"you_sent": "{{request.json.name}}", "call": "{{seq}}"}}],
        },
    ],
}

FILES = {"webhook.json": WEBHOOK, "api.json": API}


def dump(scenario: dict) -> str:
    return json.dumps(scenario, indent=2) + "\n"
