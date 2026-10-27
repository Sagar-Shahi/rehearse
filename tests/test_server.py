import http.client
import json
import threading
import time
import unittest

from rehearse.errors import ScenarioError
from rehearse.server import FaultServer
from tests.support import Receiver, fetch, free_port


def config(*routes, **extra):
    return {"routes": [dict(match={"path": "/x"}, **r) if "match" not in r else r for r in routes], **extra}


def serve(cfg, **kw):
    return FaultServer(cfg, host="127.0.0.1", port=0, **kw)


class SequenceTests(unittest.TestCase):
    def test_plays_responses_in_order_then_repeats_the_last(self):
        cfg = config({"responses": [{"status": 500}, {"status": 503}, {"status": 200, "json": {"ok": True}}]})
        with serve(cfg) as s:
            statuses = [fetch(s.url, "GET", "/x")[0] for _ in range(5)]
        self.assertEqual(statuses, [500, 503, 200, 200, 200])

    def test_cycle(self):
        cfg = config({"responses": [{"status": 201}, {"status": 202}], "after_last": "cycle"})
        with serve(cfg) as s:
            statuses = [fetch(s.url, "GET", "/x")[0] for _ in range(5)]
        self.assertEqual(statuses, [201, 202, 201, 202, 201])

    def test_routing_by_method_glob_and_regex(self):
        cfg = {
            "routes": [
                {"match": {"method": "POST", "path": "/v1/charges"}, "responses": [{"status": 201}]},
                {"match": {"method": ["GET", "HEAD"], "path": "/v1/users/*"}, "responses": [{"status": 202}]},
                {"match": {"path": "regex:/v2/\\d+"}, "responses": [{"status": 203}]},
                {"match": {"path": "/v1/*"}, "responses": [{"status": 204}]},
            ]
        }
        with serve(cfg) as s:
            got = {
                "post charges": fetch(s.url, "POST", "/v1/charges", b"{}")[0],
                "get charges": fetch(s.url, "GET", "/v1/charges")[0],  # method mismatch falls to the catch-all
                "user": fetch(s.url, "GET", "/v1/users/42?x=1")[0],  # query string ignored when matching
                "regex": fetch(s.url, "GET", "/v2/123")[0],
                "regex miss": fetch(s.url, "GET", "/v2/abc")[0],
            }
        self.assertEqual(got, {"post charges": 201, "get charges": 204, "user": 202, "regex": 203, "regex miss": 404})

    def test_unmatched_request_is_a_404_that_explains_itself(self):
        with serve(config({"responses": [{}]})) as s:
            status, _, body = fetch(s.url, "GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["path"], "/nope")

    def test_call_log_admin_endpoints(self):
        cfg = config({"responses": [{"status": 500}, {"status": 200}]})
        with serve(cfg) as s:
            fetch(s.url, "POST", "/x?a=1", b'{"k":1}', {"Content-Type": "application/json"})
            fetch(s.url, "GET", "/x")
            calls = json.loads(fetch(s.url, "GET", "/__rehearse/calls")[2])["calls"]
            self.assertEqual([c["status"] for c in calls], [500, 200])
            self.assertEqual((calls[0]["method"], calls[0]["query"], calls[0]["body"]), ("POST", "a=1", '{"k":1}'))
            self.assertEqual(fetch(s.url, "POST", "/__rehearse/reset")[0], 200)
            self.assertEqual(json.loads(fetch(s.url, "GET", "/__rehearse/calls")[2])["calls"], [])
            self.assertEqual(fetch(s.url, "GET", "/x")[0], 500)  # the sequence starts over after a reset

    def test_concurrent_requests_are_counted_exactly(self):
        cfg = config({"responses": [{"status": 201}, {"status": 202}], "after_last": "cycle"})
        statuses, lock = [], threading.Lock()
        with serve(cfg) as s:
            def hit():
                status = fetch(s.url, "GET", "/x")[0]
                with lock:
                    statuses.append(status)

            threads = [threading.Thread(target=hit) for _ in range(20)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(s.routes[0].count, 20)
        self.assertEqual((statuses.count(201), statuses.count(202)), (10, 10))

    def test_bad_config_fails_fast(self):
        with self.assertRaises(ScenarioError):
            FaultServer({"routes": [{"match": {"path": "/x"}, "responses": [{"fault": "explode"}]}]})


class LatencyTests(unittest.TestCase):
    def timed(self, response, path="/x"):
        with serve(config({"responses": [response]})) as s:
            started = time.monotonic()
            fetch(s.url, "GET", path)
            return time.monotonic() - started

    def test_delay(self):
        self.assertGreaterEqual(self.timed({"delay": "250ms"}), 0.24)

    def test_network_latency(self):
        self.assertGreaterEqual(self.timed({"network": {"latency": "200ms"}}), 0.19)

    def test_bandwidth_throttle(self):
        # ~4.1 KB at 20 KB/s is about 0.2s; unthrottled it is instant.
        slow = self.timed({"body": "x" * 4000, "network": {"bandwidth": 20000}})
        fast = self.timed({"body": "x" * 4000})
        self.assertGreaterEqual(slow, 0.15)
        self.assertLess(fast, 0.1)

    def test_server_wide_default_network(self):
        cfg = config({"responses": [{}]}, network={"latency": "200ms"})
        with serve(cfg) as s:
            started = time.monotonic()
            fetch(s.url, "GET", "/x")
        self.assertGreaterEqual(time.monotonic() - started, 0.19)


class FaultTests(unittest.TestCase):
    def fault(self, response, **kw):
        with serve(config({"responses": [response]})) as s:
            return s, fetch(s.url, "GET", "/x", **kw)

    def test_drop(self):
        with serve(config({"responses": [{"fault": "drop"}]})) as s:
            with self.assertRaises(http.client.RemoteDisconnected):
                fetch(s.url, "GET", "/x")
            self.assertEqual(s.calls[0]["fault"], "drop")

    def test_reset(self):
        with serve(config({"responses": [{"fault": "reset"}]})) as s:
            with self.assertRaises(ConnectionResetError):  # RemoteDisconnected is a subclass, so either is accepted
                fetch(s.url, "GET", "/x")

    def test_truncate_cuts_the_body_short(self):
        with serve(config({"responses": [{"json": {"data": list(range(50))}, "fault": "truncate"}]})) as s:
            with self.assertRaises(http.client.IncompleteRead) as caught:
                fetch(s.url, "GET", "/x")
        self.assertGreater(len(caught.exception.partial), 0)

    def test_garbage_is_not_http(self):
        with serve(config({"responses": [{"fault": "garbage"}]})) as s:
            with self.assertRaises(http.client.HTTPException):
                fetch(s.url, "GET", "/x")

    def test_hang_triggers_the_clients_timeout(self):
        with serve(config({"responses": [{"fault": "hang", "hang_for": "2s"}]})) as s:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                fetch(s.url, "GET", "/x", timeout=0.2)
            self.assertLess(time.monotonic() - started, 1.0)


class ResponseBuildingTests(unittest.TestCase):
    def test_templates_see_the_request(self):
        resp = {
            "status": 201,
            "headers": {"X-Seen": "{{request.headers.x-api-key}}"},
            "json": {"name": "{{request.json.name}}", "call": "{{seq}}", "q": "{{request.query.q}}", "path": "{{request.path}}"},
        }
        with serve(config({"responses": [resp]})) as s:
            status, headers, body = fetch(s.url, "POST", "/x?q=hello", b'{"name":"Asha"}', {"X-Api-Key": "k1"})
            _, _, body2 = fetch(s.url, "POST", "/x?q=hello", b"{}")
        self.assertEqual(status, 201)
        self.assertEqual(headers["X-Seen"], "k1")
        self.assertEqual(json.loads(body), {"name": "Asha", "call": 1, "q": "hello", "path": "/x"})
        self.assertEqual(json.loads(body2)["name"], "")  # missing fields render empty instead of crashing

    def test_text_body_and_content_types(self):
        with serve(config({"responses": [{"body": "plain"}]})) as s:
            _, headers, body = fetch(s.url, "GET", "/x")
        self.assertEqual((body, headers["Content-Type"]), (b"plain", "text/plain; charset=utf-8"))

    def test_chunked_request_bodies_are_decoded(self):
        cfg = config({"responses": [{"json": {"got": "{{request.body}}"}}]})
        with serve(cfg) as s:
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=3)
            conn.request("POST", "/x", body=iter([b"hello ", b"chunked ", b"world"]), encode_chunked=True)
            body = conn.getresponse().read()
            conn.close()
        self.assertEqual(json.loads(body)["got"], "hello chunked world")


class ProxyTests(unittest.TestCase):
    def test_unmatched_requests_pass_through_to_the_upstream(self):
        with Receiver(lambda req, n: (201, b"from-upstream", 0, [("X-Upstream", "yes")])) as real:
            cfg = {"upstream": real.url, "routes": [{"match": {"path": "/mine"}, "responses": [{"status": 418}]}]}
            with serve(cfg) as s:
                mine = fetch(s.url, "GET", "/mine")
                status, headers, body = fetch(s.url, "PUT", "/other?x=1", b"payload", {"X-Trace": "t1"})
        self.assertEqual(mine[0], 418)
        self.assertEqual((status, body), (201, b"from-upstream"))
        self.assertEqual((headers["X-Upstream"], headers["X-Rehearse-Proxied"]), ("yes", "1"))
        seen = real.requests[0]
        self.assertEqual((seen["method"], seen["path"], seen["body"]), ("PUT", "/other?x=1", b"payload"))
        self.assertEqual(seen["headers"]["x-trace"], "t1")
        self.assertEqual(len(real.requests), 1)  # the /mine route never reached the upstream

    def test_faults_can_wrap_real_traffic(self):
        with Receiver(lambda req, n: (200, b"real")) as real:
            cfg = {"upstream": real.url, "routes": [{"match": {"path": "/slow"}, "responses": [{"proxy": True, "delay": "250ms"}]}]}
            with serve(cfg) as s:
                started = time.monotonic()
                status, _, body = fetch(s.url, "GET", "/slow")
                elapsed = time.monotonic() - started
        self.assertEqual((status, body), (200, b"real"))
        self.assertGreaterEqual(elapsed, 0.24)

    def test_dead_upstream_becomes_a_502(self):
        cfg = {"upstream": f"http://127.0.0.1:{free_port()}", "routes": []}
        with serve(cfg) as s:
            status, _, body = fetch(s.url, "GET", "/anything")
        self.assertEqual(status, 502)
        self.assertIn("upstream unreachable", json.loads(body)["error"])

    def test_upstream_must_be_http(self):
        with self.assertRaises(ScenarioError):
            FaultServer({"upstream": "ftp://example.com"})


if __name__ == "__main__":
    unittest.main()
