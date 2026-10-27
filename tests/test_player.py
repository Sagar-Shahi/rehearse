import hashlib
import hmac
import json
import time
import unittest

from rehearse.errors import ScenarioError
from rehearse.player import Player, check_expect
from tests.support import Receiver, free_port


def play(scenario, target=None, speed=1.0):
    player = Player(scenario, target=target, speed=speed)
    player.run()
    return player


def one(send, target, **step):
    return {"target": target, "steps": [{"send": send, **step}]}


class SendTests(unittest.TestCase):
    def test_sends_json_with_vars_and_headers(self):
        with Receiver() as app:
            scenario = {
                "target": app.url,
                "vars": {"order": "ord_9", "amount": 4200},
                "steps": [
                    {
                        "send": {
                            "path": "/hooks/pay",
                            "headers": {"X-Source": "rehearse"},
                            "json": {"order": "{{order}}", "amount": "{{amount}}", "seq": "{{seq}}"},
                        },
                        "expect": {"status": 200},
                    }
                ],
            }
            player = play(scenario)
        request = app.requests[0]
        self.assertTrue(player.ok)
        self.assertEqual((request["method"], request["path"]), ("POST", "/hooks/pay"))
        self.assertEqual(request["headers"]["x-source"], "rehearse")
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(request["body"]), {"order": "ord_9", "amount": 4200, "seq": 1})

    def test_get_with_query_and_target_override(self):
        with Receiver() as app:
            play({"target": "http://wrong.invalid", "steps": [{"send": {"method": "GET", "path": "/x?a=1"}}]}, target=app.url)
        self.assertEqual((app.requests[0]["method"], app.requests[0]["path"]), ("GET", "/x?a=1"))

    def test_expectation_failures_are_reported(self):
        with Receiver(lambda req, n: (500, b"boom")) as app:
            player = play(one({"path": "/"}, app.url, expect={"status": 200}))
        result = player.results[0]
        self.assertFalse(result.ok)
        self.assertFalse(player.ok)
        self.assertIn("got 500", result.failures[0])

    def test_check_expect_variants(self):
        self.assertEqual(check_expect({"status": "2xx"}, 204, "", 0, None), [])
        self.assertEqual(check_expect({"status": [400, 422]}, 422, "", 0, None), [])
        self.assertTrue(check_expect({"status": [400, 422]}, 200, "", 0, None))
        self.assertTrue(check_expect({"body_contains": "ok"}, 200, "nope", 0, None))
        self.assertEqual(check_expect({"body_contains": ["a", "b"]}, 200, "a b", 0, None), [])
        self.assertTrue(check_expect({"max_ms": 100}, 200, "", 0.5, None))
        self.assertEqual(check_expect({"error": "timeout"}, None, "", 0, "timeout"), [])
        self.assertEqual(check_expect({"error": "any"}, None, "", 0, "connection reset"), [])
        self.assertTrue(check_expect({"error": "timeout"}, 200, "", 0, None))
        self.assertTrue(check_expect({}, None, "", 0, "connection reset"))  # unexpected errors always fail

    def test_corrupt_modes(self):
        expected = {}
        for mode in ("truncate", "invalid_json", "empty", "garbage"):
            with Receiver() as app:
                play(one({"path": "/", "json": {"id": "evt_1", "type": "x"}, "corrupt": mode}, app.url))
            expected[mode] = app.requests[0]["body"]
        full = json.dumps({"id": "evt_1", "type": "x"}, separators=(",", ":")).encode()
        self.assertEqual(expected["truncate"], full[: len(full) // 2])
        with self.assertRaises(ValueError):
            json.loads(expected["invalid_json"])
        self.assertEqual(expected["empty"], b"")
        self.assertTrue(expected["garbage"].startswith(b"\x00"))

    def test_signatures(self):
        body = {"id": "evt_1"}
        raw = json.dumps(body, separators=(",", ":")).encode()
        want = hmac.new(b"k", raw, hashlib.sha256).hexdigest()
        sent = {}
        for label, corrupt in (("good", None), ("bad", "bad_signature")):
            send = {"json": body, "sign": {"secret": "k", "header": "X-Sig"}}
            if corrupt:
                send["corrupt"] = corrupt
            with Receiver() as app:
                play(one(send, app.url))
            sent[label] = app.requests[0]["headers"]["x-sig"]
        self.assertEqual(sent["good"], want)
        self.assertNotEqual(sent["bad"], want)
        self.assertEqual(len(sent["bad"]), len(want))

    def test_signature_covers_corrupted_body(self):
        """A truncated body is still validly signed, so it exercises the JSON parser, not the signature check."""
        with Receiver() as app:
            play(one({"json": {"id": "evt_1", "type": "x"}, "corrupt": "truncate", "sign": {"secret": "k"}}, app.url))
        request = app.requests[0]
        self.assertEqual(request["headers"]["x-signature"], hmac.new(b"k", request["body"], hashlib.sha256).hexdigest())

    def test_body_base64(self):
        with Receiver() as app:
            play(one({"body_base64": "AP8="}, app.url))
        self.assertEqual(app.requests[0]["body"], b"\x00\xff")


class FlowTests(unittest.TestCase):
    def test_retry_resends_the_same_event_until_it_works(self):
        with Receiver(lambda req, n: (503, b"no") if n < 3 else (200, b"yes")) as app:
            player = play(
                one(
                    {"path": "/", "json": {"id": "evt_{{uuid}}"}},
                    app.url,
                    retry={"attempts": 5, "delay": "10ms", "until": {"status": 200}},
                )
            )
        result = player.results[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len({r["body"] for r in app.requests}), 1)  # identical payload every time

    def test_retry_gives_up(self):
        with Receiver(lambda req, n: (500, b"no")) as app:
            player = play(one({"path": "/"}, app.url, retry={"attempts": 3, "delay": "1ms", "until": {"status": 200}}))
        self.assertEqual((player.results[0].ok, player.results[0].attempts), (False, 3))
        self.assertEqual(len(app.requests), 3)

    def test_repeat_sends_duplicates(self):
        with Receiver() as app:
            player = play(one({"json": {"n": "{{repeat}}", "id": "evt_fixed"}}, app.url, repeat=3, every="5ms"))
        self.assertEqual(len(player.results), 3)
        self.assertEqual([json.loads(r["body"])["n"] for r in app.requests], [1, 2, 3])
        self.assertEqual([r.note for r in player.results], ["repeat 1/3", "repeat 2/3", "repeat 3/3"])

    def test_shuffle_is_reproducible_from_its_seed(self):
        def order(seed):
            with Receiver() as app:
                kids = [{"id": name, "send": {"path": f"/{name}"}} for name in "abcdef"]
                play({"target": app.url, "steps": [{"shuffle": kids, "seed": seed}]})
            return [r["path"] for r in app.requests]

        self.assertEqual(order(3), order(3))
        self.assertEqual(sorted(order(3)), sorted(f"/{c}" for c in "abcdef"))  # nothing lost or duplicated
        self.assertGreater(len({tuple(order(s)) for s in range(6)}), 1)  # different seeds, different orders

    def test_parallel_steps_overlap_in_time(self):
        with Receiver(lambda req, n: (200, b"ok", 0.25)) as app:
            started = time.monotonic()
            player = play({"target": app.url, "steps": [{"parallel": [{"send": {"path": "/a"}}, {"send": {"path": "/b"}}]}]})
            elapsed = time.monotonic() - started
        self.assertTrue(player.ok)
        self.assertLess(elapsed, 0.45)  # sequential would take at least 0.5s

    def test_wait_honours_speed(self):
        started = time.monotonic()
        player = play({"steps": [{"wait": "1s"}]}, speed=20)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(player.results[0].kind, "wait")

    def test_steps_run_in_order_with_waits(self):
        with Receiver() as app:
            player = play({"target": app.url, "steps": [{"send": {"path": "/1"}}, {"wait": "150ms"}, {"send": {"path": "/2"}}]})
        sends = [r for r in player.results if r.kind == "send"]
        self.assertGreaterEqual(sends[1].start - sends[0].start, 0.14)

    def test_seq_variable_increments_per_request(self):
        with Receiver() as app:
            play({"target": app.url, "steps": [{"send": {"json": {"s": "{{seq}}"}}}, {"send": {"json": {"s": "{{seq}}"}}}]})
        self.assertEqual([json.loads(r["body"])["s"] for r in app.requests], [1, 2])


class NetworkErrorTests(unittest.TestCase):
    def test_timeout_can_be_the_expected_outcome(self):
        with Receiver(lambda req, n: (200, b"slow", 0.6)) as app:
            expected = play(one({"timeout": "100ms"}, app.url, expect={"error": "timeout"}))
            unexpected = play(one({"timeout": "100ms"}, app.url))
        self.assertTrue(expected.ok)
        self.assertFalse(unexpected.ok)
        self.assertEqual(unexpected.results[0].error, "timeout")

    def test_connection_refused(self):
        target = f"http://127.0.0.1:{free_port()}"
        failing = play(one({"path": "/"}, target))
        self.assertFalse(failing.ok)
        self.assertIn("refused", failing.results[0].error)
        self.assertTrue(play(one({"path": "/"}, target, expect={"error": "refused"})).ok)

    def test_scenario_errors(self):
        with self.assertRaises(ScenarioError):
            play({"steps": [{"send": {"path": "/"}}]})  # no target anywhere
        with Receiver() as app, self.assertRaises(ScenarioError):
            play(one({"json": {"x": "{{nope}}"}}, app.url))
        with self.assertRaises(ScenarioError):
            Player({"steps": [{"wait": "1s"}]}, speed=0)


if __name__ == "__main__":
    unittest.main()
