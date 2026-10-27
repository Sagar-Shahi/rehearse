import json
import random
import tempfile
import unittest
import uuid
from pathlib import Path

from rehearse.demo_app import verify_stripe
from rehearse.durations import Network, parse_duration, resolve_network, sample_delay
from rehearse.errors import ScenarioError
from rehearse.examples import API, WEBHOOK
from rehearse.player import StepResult
from rehearse.report import format_line, render_timeline, summary
from rehearse.scenario import kind, load, validate, validate_play, validate_serve
from rehearse.signing import sign_body
from rehearse.template import Context, render


class DurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("500ms"), 0.5)
        self.assertEqual(parse_duration("2s"), 2.0)
        self.assertEqual(parse_duration("1.5m"), 90.0)
        self.assertEqual(parse_duration(250), 0.25)  # bare numbers are milliseconds
        self.assertEqual(parse_duration(None), 0.0)

    def test_invalid(self):
        for bad in ("fast", "-1s", "2 hours", "1.5.2s", True, -5, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_duration(bad)

    def test_network_profiles(self):
        self.assertGreater(resolve_network("slow-3g").latency, resolve_network("4g").latency)
        self.assertLess(resolve_network("slow-3g").bandwidth, resolve_network("4g").bandwidth)
        custom = resolve_network({"latency": "200ms", "bandwidth": 1000})
        self.assertEqual((custom.latency, custom.bandwidth), (0.2, 1000))
        self.assertEqual(resolve_network(None), Network())
        for bad in ("5g", {"lag": 1}, {"bandwidth": 0}, 12):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_network(bad)

    def test_jitter_never_goes_negative(self):
        rng = random.Random(1)
        self.assertTrue(all(sample_delay(Network(0.01, 0.5), rng) >= 0 for _ in range(500)))


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context({"name": "Asha", "amount": 4200, "nested": {"a": {"b": 7}}})

    def test_substitution_and_types(self):
        self.assertEqual(render("hi {{name}}", self.ctx), "hi Asha")
        self.assertEqual(render("{{ name }}", self.ctx), "Asha")
        self.assertEqual(render("{{amount}}", self.ctx), 4200)  # a lone placeholder keeps its type
        self.assertEqual(render("total={{amount}}", self.ctx), "total=4200")
        self.assertEqual(render("{{nested.a.b}}", self.ctx), 7)

    def test_recurses_into_structures(self):
        rendered = render({"a": ["{{name}}", {"b": "{{amount}}"}], "n": 1}, self.ctx)
        self.assertEqual(rendered, {"a": ["Asha", {"b": 4200}], "n": 1})

    def test_builtins(self):
        a, b = render("{{uuid}}", self.ctx), render("{{uuid}}", self.ctx)
        self.assertNotEqual(a, b)
        uuid.UUID(a)
        self.assertTrue(render("{{now}}", self.ctx).endswith("Z"))
        self.assertIsInstance(render("{{timestamp}}", self.ctx), int)

    def test_unknown_variable(self):
        with self.assertRaises(ScenarioError) as caught:
            render("{{missing}}", self.ctx)
        self.assertIn("{{missing}}", str(caught.exception))
        self.assertEqual(render("[{{missing}}]", Context({}, strict=False)), "[]")


class SigningTests(unittest.TestCase):
    BODY = b'{"id":"evt_1"}'

    def test_hex(self):
        import hashlib, hmac

        header, value = sign_body(self.BODY, "s3cret")
        self.assertEqual(header, "X-Signature")
        self.assertEqual(value, hmac.new(b"s3cret", self.BODY, hashlib.sha256).hexdigest())

    def test_github_format(self):
        header, value = sign_body(self.BODY, "s3cret", format="github")
        self.assertEqual(header, "X-Hub-Signature-256")
        self.assertTrue(value.startswith("sha256="))

    def test_stripe_format_verifies(self):
        header, value = sign_body(self.BODY, "whsec_test_123", format="stripe")
        self.assertEqual(header, "Stripe-Signature")
        self.assertTrue(verify_stripe(value, self.BODY))
        self.assertFalse(verify_stripe(value, self.BODY + b" "))  # tampered body
        old = sign_body(self.BODY, "whsec_test_123", format="stripe", now=1_000_000)[1]
        self.assertFalse(verify_stripe(old, self.BODY))  # replayed timestamp
        self.assertFalse(verify_stripe("garbage", self.BODY))

    def test_custom_header(self):
        self.assertEqual(sign_body(self.BODY, "k", header="X-My-Sig")[0], "X-My-Sig")


class ValidationTests(unittest.TestCase):
    def play(self, step):
        return {"steps": [step]}

    def assertInvalid(self, scenario, fragment, validator=validate_play):
        with self.assertRaises(ScenarioError) as caught:
            validator(scenario)
        self.assertIn(fragment, str(caught.exception))

    def test_examples_are_valid(self):
        self.assertEqual(validate(WEBHOOK), "play")
        self.assertEqual(validate(API), "serve")

    def test_kind_detection(self):
        self.assertEqual(kind({"steps": []}), "play")
        self.assertEqual(kind({"routes": []}), "serve")
        with self.assertRaises(ScenarioError):
            kind({"hello": 1})

    def test_typos_get_a_hint(self):
        self.assertInvalid(self.play({"send": {"path": "/"}, "retires": {}}), "did you mean 'retry'")
        self.assertInvalid(self.play({"send": {"pth": "/"}}), "did you mean 'path'")

    def test_step_shape(self):
        self.assertInvalid({"steps": []}, "non-empty")
        self.assertInvalid(self.play({}), "exactly one of")
        self.assertInvalid(self.play({"send": {}, "wait": "1s"}), "exactly one of")
        self.assertInvalid(self.play({"wait": "soon"}), "invalid duration")

    def test_send_rules(self):
        self.assertInvalid(self.play({"send": {"json": {}, "body": "x"}}), "only one of")
        self.assertInvalid(self.play({"send": {"corrupt": "explode"}}), "corrupt")
        self.assertInvalid(self.play({"send": {"corrupt": "bad_signature"}}), "needs a \"sign\" block")
        self.assertInvalid(self.play({"send": {"sign": {}}}), "secret")
        self.assertInvalid(self.play({"send": {"sign": {"secret": "x", "format": "md5"}}}), "format")
        self.assertInvalid(self.play({"send": {}, "repeat": 0}), "repeat")
        self.assertInvalid(self.play({"send": {}, "expect": {"status": "200"}}), "status")
        self.assertInvalid(self.play({"send": {}, "retry": {"attempts": 0}}), "attempts")

    def test_nested_paths_in_errors(self):
        scenario = self.play({"shuffle": [{"send": {}}, {"wait": "nope"}]})
        self.assertInvalid(scenario, "steps[0].shuffle[1].wait")

    def test_serve_rules(self):
        route = lambda **resp: {"routes": [{"match": {"path": "/x"}, "responses": [resp]}]}
        self.assertInvalid(route(fault="explode"), "fault", validate_serve)
        self.assertInvalid(route(status=99), "status", validate_serve)
        self.assertInvalid(route(json={}, body="x"), "not both", validate_serve)
        self.assertInvalid(route(proxy=True), "upstream", validate_serve)
        self.assertInvalid(route(network="5g"), "network", validate_serve)
        self.assertInvalid(route(truncate_at=1.5), "truncate_at", validate_serve)
        self.assertInvalid({"routes": []}, "at least one route", validate_serve)
        self.assertInvalid({"routes": [{"match": {}, "responses": [{}]}]}, "match.path", validate_serve)

    def test_load_reports_json_errors_with_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{\n  "steps": [\n    {"wait": }\n  ]\n}', encoding="utf-8")
            with self.assertRaises(ScenarioError) as caught:
                load(path)
            self.assertIn("line 3", str(caught.exception))
            with self.assertRaises(ScenarioError):
                load(Path(tmp) / "missing.json")


class ReportTests(unittest.TestCase):
    def results(self):
        return [
            StepResult("POST /a", "send", 0.0, 0.10, True, 200),
            StepResult("wait 1s", "wait", 0.10, 1.00),
            StepResult("POST /b", "send", 1.10, 0.20, False, 500, failures=["expected status 200, got 500"]),
        ]

    def test_lines(self):
        ok, wait, bad = (format_line(r) for r in self.results())
        self.assertIn("ok", ok.splitlines()[0])
        self.assertIn("1.00s", wait)
        self.assertIn("FAIL", bad)
        self.assertIn("expected status 200, got 500", bad)

    def test_timeline_bars_have_fixed_width(self):
        lines = render_timeline(self.results(), width=30).splitlines()
        self.assertIn("total 1.30s", lines[0])
        for line in lines[1:]:
            bar = line.split("|")[1]
            self.assertEqual(len(bar), 30)
        self.assertIn("!", lines[3])  # failed steps are drawn with '!'

    def test_summary(self):
        self.assertIn("1 of 2 requests failed", summary(self.results()))
        self.assertIn("all 1 requests passed", summary(self.results()[:1]))


if __name__ == "__main__":
    unittest.main()
