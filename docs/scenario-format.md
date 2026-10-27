# Scenario format reference

Rehearse reads one JSON file per scenario. There are two kinds, and Rehearse tells them apart by their top-level key:

| File has | Kind | Run with | What it does |
|----------|------|----------|--------------|
| `"steps"` | **send** scenario | `rehearse send` | Plays a timeline of requests *at* your app |
| `"routes"` or `"upstream"` | **serve** scenario | `rehearse serve` | Runs a local API your app calls, which misbehaves on purpose |

Validation is strict. Unknown keys are errors with a "did you mean" hint, and messages name the exact location
(`steps[2].send.corrupt`). Check a file without running it: `rehearse validate file.json`.

---

## Send scenarios

```json
{
  "name": "Payment webhook edge cases",
  "target": "http://localhost:3000",
  "timeout": "10s",
  "vars": { "secret": "whsec_test_123", "amount": 4200 },
  "steps": [ ... ]
}
```

| Key | Meaning |
|-----|---------|
| `name`, `description` | Free text shown in the report |
| `target` | Base URL requests are sent to. `--target` on the command line overrides it |
| `timeout` | Default per-request timeout (default `10s`) |
| `vars` | Values for `{{name}}` placeholders |
| `steps` | The timeline, run top to bottom |

### Step types

Every step has exactly one of these keys, plus optional `id` and `name` (used as the label in the report).

| Step | Example | Behaviour |
|------|---------|-----------|
| `send` | `{"send": {"path": "/hook", "json": {...}}}` | Send one request |
| `wait` | `{"wait": "2s"}` | Pause. `--speed 10` makes waits ten times shorter |
| `parallel` | `{"parallel": [step, step]}` | Run steps at the same time (race conditions) |
| `shuffle` | `{"shuffle": [step, step, step], "seed": 7}` | Run steps in a random order (out-of-order delivery). The seed is printed, so a failing order can be replayed |

Steps nest: a `shuffle` can sit inside a `parallel`, and so on.

### `send`

| Key | Default | Meaning |
|-----|---------|---------|
| `method` | `POST` | HTTP method |
| `path` | `/` | Appended to the target; may include a query string |
| `target` | scenario target | Override the base URL for this request |
| `headers` | none | Object of header names to values |
| `json` / `body` / `body_base64` | none | The body. Use at most one |
| `corrupt` | none | Break the request on purpose (see below) |
| `sign` | none | Add an HMAC signature header (see below) |
| `timeout` | scenario timeout | Per-request timeout |

Step-level keys next to `send`:

| Key | Meaning |
|-----|---------|
| `expect` | What a correct receiver should do. Failed expectations fail the run |
| `retry` | Re-send until a condition holds |
| `repeat` / `every` | Send the same request N times (duplicate delivery), optionally spaced by a duration |

### `corrupt`

| Value | Effect |
|-------|--------|
| `truncate` | Cut the body in half |
| `invalid_json` | Make the body syntactically invalid JSON |
| `empty` | Send an empty body |
| `garbage` | Send non-text bytes |
| `bad_signature` | Send the correct body with a wrong signature (requires `sign`) |

The signature is computed over the *final* body, so `truncate` and friends are validly signed. They exercise your JSON
parser, while `bad_signature` exercises your signature check. The two failures stay separate.

### `sign`

```json
"sign": { "secret": "{{secret}}", "format": "stripe" }
```

| `format` | Header (default) | Value |
|----------|------------------|-------|
| `hex` (default) | `X-Signature` | `hex(HMAC(body))` |
| `github` | `X-Hub-Signature-256` | `sha256=` + `hex(HMAC(body))` |
| `stripe` | `Stripe-Signature` | `t=<unix time>,v1=hex(HMAC("<unix time>." + body))` |

`header` overrides the header name. `algo` is `sha256` (default), `sha1` or `sha512`.

### `expect`

```json
"expect": { "status": [400, 422], "body_contains": "invalid", "max_ms": 500 }
```

| Key | Meaning |
|-----|---------|
| `status` | `200`, a class like `"2xx"`, or a list of either |
| `body_contains` | A string, or a list of strings that must all appear |
| `max_ms` | Fail if the response took longer |
| `error` | Expect the request to *fail*: `"timeout"`, `"connection refused"`, `"connection reset"`, `"connection closed"`, `"cut off"`, or `"any"` |

Without `expect`, a step passes as long as the app answered at all. A connection error fails unless you expected it.

### `retry`

```json
"retry": { "attempts": 5, "delay": "500ms", "until": { "status": 200 } }
```

Re-sends the **same payload** (same generated ids), the way a real provider retries, until `until` is met or `attempts`
(default 3) run out. Without `until`, the step's `expect` is used, then any `2xx`. The report shows how many attempts it took.

### Variables

`{{name}}` works inside any string value in a `send` (not in durations).

| Variable | Value |
|----------|-------|
| `{{uuid}}` | A fresh UUID each time it appears |
| `{{now}}` | ISO-8601 UTC time with milliseconds |
| `{{timestamp}}` | Unix seconds |
| `{{seq}}` | 1, 2, 3... for each request sent in this run |
| `{{repeat}}` | 1, 2, 3... inside a `repeat` step |
| anything in `vars` | Your own values. A value that is exactly one placeholder keeps its type, so `"{{amount}}"` becomes the number `4200` |

Durations are `"500ms"`, `"2s"`, `"1.5m"`; a bare number means milliseconds.

---

## Serve scenarios

```json
{
  "name": "Flaky payments API",
  "port": 8080,
  "routes": [
    {
      "name": "charge: retry storm",
      "match": { "method": "POST", "path": "/v1/charges" },
      "responses": [
        { "status": 500 },
        { "status": 503, "headers": { "Retry-After": "1" } },
        { "status": 200, "delay": "300ms", "json": { "id": "ch_{{uuid}}" } }
      ]
    }
  ]
}
```

| Key | Meaning |
|-----|---------|
| `host`, `port` | Where to listen (default `127.0.0.1:8080`; `--host`/`--port` override) |
| `upstream` | A real base URL. Requests that match no route are proxied there |
| `network` | Default [network profile](#network-profiles) for every response |
| `routes` | Checked top to bottom; the first match wins |

### Routes

| Key | Meaning |
|-----|---------|
| `match.path` | Exact (`/v1/charges`), glob (`/v1/users/*`) or regex (`regex:/v2/\\d+`). Query strings are ignored when matching |
| `match.method` | `"POST"` or `["GET", "HEAD"]`. Omit to match any method |
| `responses` | A **sequence**: the 1st call gets the 1st response, the 2nd gets the 2nd... |
| `after_last` | `repeat_last` (default) keeps returning the final response; `cycle` starts over |

### Responses

| Key | Default | Meaning |
|-----|---------|---------|
| `status` | `200` | HTTP status |
| `headers` | none | Response headers (templated) |
| `json` / `body` | empty | Response body (templated) |
| `delay` | none | Wait this long before responding |
| `network` | server default | [Profile](#network-profiles) for latency and bandwidth |
| `fault` | none | A transport-level failure (below) |
| `truncate_at` | `0.5` | With `fault: truncate`, the fraction of the body to send |
| `hang_for` | `30s` | With `fault: hang`, how long to stay silent |
| `proxy` | `false` | Forward to `upstream` instead of answering, with `delay`/`network` applied to the real response |

### Faults

| `fault` | What your client sees |
|---------|-----------------------|
| `drop` | The connection closes with no response |
| `reset` | A TCP reset (`ECONNRESET`) |
| `truncate` | Headers promise N bytes, only part arrives, then the connection closes |
| `garbage` | Bytes that are not HTTP |
| `hang` | Silence until your own timeout fires |

### Network profiles

Either a name or an object: `"network": "3g"` or `"network": {"latency": "300ms", "jitter": "100ms", "bandwidth": 50000}`
(bandwidth in bytes per second).

| Profile | Latency (± jitter) | Bandwidth |
|---------|--------------------|-----------|
| `4g` | 50 ms (± 20) | about 1.5 MB/s |
| `3g` | 150 ms (± 50) | about 200 KB/s |
| `slow-3g` | 400 ms (± 100) | about 50 KB/s |
| `2g` | 800 ms (± 300) | about 6 KB/s |

These approximate common browser throttling presets. Use `--seed` for reproducible jitter.

### Templates in responses

| Variable | Value |
|----------|-------|
| `{{seq}}` | Which response in the sequence this is (1, 2, 3...) |
| `{{request.method}}`, `{{request.path}}` | The incoming request |
| `{{request.query.name}}` | A query parameter |
| `{{request.json.field}}` | A field of the JSON body (nested: `request.json.user.id`) |
| `{{request.headers.x-api-key}}` | A request header (lowercase name) |
| `{{request.body}}` | The raw body text |
| `{{uuid}}`, `{{now}}`, `{{timestamp}}` | As in send scenarios |

Missing values render as an empty string rather than failing the request.

### Inspecting a running server

| Request | Result |
|---------|--------|
| `GET /__rehearse/calls` | Every request received so far (method, path, headers, body, which response it got) |
| `POST /__rehearse/reset` | Clear the log and restart every sequence |
| `GET /__rehearse/health` | `{"ok": true}` |

This lets your own test code assert, for example, that your client retried exactly twice.

---

## Command line

| Command | Purpose |
|---------|---------|
| `rehearse send FILE [--target URL] [--speed N] [--json]` | Play a send scenario |
| `rehearse serve FILE [--host H] [--port N] [--log FILE] [--seed N]` | Run a serve scenario |
| `rehearse record [--out FILE] [--port N] [--forward URL] [--target URL]` | Capture requests into a send scenario |
| `rehearse validate FILE...` | Check files without running them |
| `rehearse init [DIR] [--force]` | Write example scenarios |
| `rehearse demo-app [--port N]` | Run a sample webhook receiver |

Exit codes: `0` every expectation held, `1` at least one failed, `2` the scenario file or command line was wrong.
