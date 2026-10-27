# Rehearse

**Rehearse the worst day of your integrations.** Script webhook timelines, flaky APIs and network faults in one JSON
file, run them against `localhost`, and share the file with your team.

[![CI](https://github.com/Sagar-Shahi/rehearse/actions/workflows/ci.yml/badge.svg)](https://github.com/Sagar-Shahi/rehearse/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Your app talks to payment gateways, auth providers and other people's webhooks. The happy path is easy to test. The
questions that cause incidents are not:

- What if `payment.refunded` arrives **before** `payment.succeeded`?
- What if the provider returns a 500 on the **second** retry?
- What if the connection drops halfway through a response? Or the same event is delivered **three times**?

Today that means temporary mock code you forget to delete, or clicking "resend" in a dashboard. Rehearse turns each
of those situations into a file you can run, repeat, and commit.

## 60 seconds

```bash
pip install -e .                  # or just use: python -m rehearse ...
rehearse init examples            # writes example scenarios
rehearse demo-app &               # a small webhook receiver on :3000
rehearse send examples/webhook.json
```

```text
Payment webhook edge cases
[ ok ] happy path  (200)
[ ok ] wait 1s  (1.00s)
[ ok ] body is not valid JSON  (400)
[ ok ] signature does not match  (401)
[ ok ] same event delivered 3 times  (200, repeat 1/3)
[ ok ] same event delivered 3 times  (200, repeat 2/3)
[ ok ] same event delivered 3 times  (200, repeat 3/3)
[info] shuffle seed=7  (created > succeeded > refunded)
[ ok ]   created  (200)
[ ok ]   succeeded  (200)
[ ok ]   refunded  (200)
[ ok ] receiver recovers after an outage  (200, 3 attempts)

Timeline (total 1.62s)
  0.00s |#                                   | happy path
  0.00s |----------------------              | wait 1s
  1.00s |                      #             | body is not valid JSON
  1.01s |                      #             | signature does not match
  1.01s |                      #             | same event delivered 3 times
  1.11s |                        #           | same event delivered 3 times
  1.21s |                          #         | same event delivered 3 times
  1.21s |                          #         |   created
  1.22s |                          #         |   succeeded
  1.22s |                          #         |   refunded
  1.22s |                           #########| receiver recovers after an outage

all 10 requests passed in 1.62s
```

Point `target` at your own app and edit the paths to rehearse it for real.

## Three tools in one file format

### 1. `rehearse send`: push awkward events at your app

A timeline of signed, corrupted, repeated, delayed and reordered requests, each with an expectation.

```json
{
  "target": "http://localhost:3000",
  "vars": { "secret": "whsec_test_123" },
  "steps": [
    { "name": "happy path",
      "send": { "path": "/webhooks/payments", "json": { "id": "evt_{{uuid}}", "type": "payment.created" },
                "sign": { "secret": "{{secret}}", "format": "stripe" } },
      "expect": { "status": "2xx" } },

    { "wait": "2s" },

    { "name": "broken JSON arrives next",
      "send": { "path": "/webhooks/payments", "json": { "id": "evt_1", "type": "x" },
                "corrupt": "invalid_json", "sign": { "secret": "{{secret}}", "format": "stripe" } },
      "expect": { "status": [400, 422] } },

    { "name": "then the provider retries until you recover",
      "send": { "path": "/webhooks/flaky", "json": { "id": "evt_{{uuid}}" } },
      "retry": { "attempts": 5, "delay": "500ms", "until": { "status": 200 } } }
  ]
}
```

Built in: HMAC signing (Stripe, GitHub or plain hex), `corrupt` (truncate, invalid JSON, empty, garbage, bad
signature), `repeat` for duplicate delivery, `parallel` for races, `shuffle` for out-of-order events (the seed is printed so
a failing order can be replayed), `retry` that resends the *same* payload like a real provider, and `expect` on status,
body, latency or a specific network error.

### 2. `rehearse serve`: a third-party API that misbehaves on purpose

Change your app's base URL to `http://localhost:8080`. No changes to your source code.

```json
{
  "port": 8080,
  "routes": [
    { "match": { "method": "POST", "path": "/v1/charges" },
      "responses": [
        { "status": 500 },
        { "status": 503, "headers": { "Retry-After": "1" } },
        { "status": 200, "delay": "300ms", "json": { "id": "ch_{{uuid}}" } } ] },

    { "match": { "path": "/v1/customers/*" },
      "responses": [ { "network": "slow-3g", "json": { "id": "cus_1" } } ] },

    { "match": { "path": "/v1/refunds" }, "responses": [ { "fault": "drop" }, { "json": { "id": "re_1" } } ] },
    { "match": { "path": "/v1/events" },  "responses": [ { "json": { "data": [] }, "fault": "truncate" } ] }
  ]
}
```

Each route plays a **sequence** (500, then 503, then 200), and each response can add latency, a network profile
(`4g`, `3g`, `slow-3g`, `2g`, or your own latency, jitter and bandwidth) or a transport fault: `drop`, `reset`, `truncate`
(body cut off mid-stream), `garbage` (not HTTP) and `hang` (silence until your timeout fires). Set `upstream` to
proxy everything else to the real API, so you can add faults to real traffic. Your test code can read what your client
actually did from `GET /__rehearse/calls`.

### 3. `rehearse record`: capture once, replay anywhere

```bash
rehearse record --port 9000 --out bug-4812.json          # point a webhook tunnel at :9000, trigger the bug
rehearse send bug-4812.json --target http://localhost:3000   # replay the exact sequence, timing included
```

The scenario file keeps bodies, headers and the gaps between requests. Commit it, attach it to the ticket, and everyone
reproduces the same sequence locally. Add `--forward URL` to relay requests to a real service while recording.

## Use it in CI

```yaml
- run: rehearse send scenarios/webhooks.json --target http://localhost:3000 --speed 10
```

Exit code `0` means every expectation held, `1` means one failed, `2` means the file was wrong. Add `--json` for
machine-readable results. `rehearse validate scenarios/*.json` catches typos before they run; unknown keys are
errors with a "did you mean" hint.

## How it compares

Rehearse does not replace the tools below. It fills the gap between them.

| Tool | Great at | Where Rehearse differs |
|------|----------|------------------------|
| WireMock, Mountebank, MockServer | Rich stubbing, response sequences, fault injection | These need a JVM or Node runtime and their own stub formats, and they only mock *responses*. They do not send signed webhooks at your app |
| Toxiproxy | TCP-level network conditions for any protocol | It does not understand HTTP, webhooks or scripted payloads |
| Provider CLIs (Stripe CLI and similar) | Triggering one vendor's real test events | Rehearse is vendor-neutral and scripts *sequences* of corrupted, duplicated and reordered events |
| Postman, curl, Insomnia | Hand-testing endpoints | No timelines, no assertions about retry behaviour, no fault injection |
| ngrok, Hookdeck | Reaching localhost from the internet and replaying inbound requests | Complementary: tunnel into `rehearse record`, then replay offline |

What Rehearse adds is one dependency-free file format that drives **both** directions (events *to* your app, failures
*from* the APIs it calls) plus record and replay.

## Known limits

Rehearse is an early release (0.1) built for local development and CI, not load testing.

- HTTP/1.x only. The mock server speaks plain HTTP (no TLS), one request per connection. The sender supports `https://` targets.
- Faults are injected at the HTTP-response level, not by a real network proxy. For packet-level conditions use Toxiproxy alongside it.
- Scenario files are JSON (no comments). Everything is configured in the file; there is no GUI yet.

## Roadmap

- [ ] A local web UI: a visual timeline editor on top of the same scenario files
- [ ] Import from HAR files and `curl` commands
- [ ] TLS for the mock server
- [ ] More signature formats (Slack, Shopify, Twilio) and a plugin hook for custom ones
- [ ] Probabilistic faults ("1 in 10 requests fails") for soak tests
- [ ] OpenAPI-aware mocking

## Documentation

- [Scenario format reference](docs/scenario-format.md): every key, fault, profile and variable
- [CONTRIBUTING.md](CONTRIBUTING.md)

## Development

```bash
python -m unittest discover -s tests -t .     # 74 tests, about 6 seconds, real local sockets
python -m rehearse --help
```

Standard library only. Python 3.10+.

## License

[MIT](LICENSE)
