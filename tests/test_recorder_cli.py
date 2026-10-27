import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from rehearse.cli import main
from rehearse.demo_app import DemoApp
from rehearse.player import Player
from rehearse.recorder import Recorder
from rehearse.scenario import kind, load, validate
from tests.support import Receiver, fetch


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RecorderTests(unittest.TestCase):
    def test_record_then_replay_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "captured.json"
            with Recorder(out, port=0) as rec:
                base = f"http://127.0.0.1:{rec.port}"
                status, _, body = fetch(base, "POST", "/hooks/a?x=1", b'{"n":1}', {"Content-Type": "application/json", "X-Sig": "abc"})
                self.assertEqual((status, json.loads(body)["recorded"]), (200, True))
                time.sleep(0.12)
                fetch(base, "POST", "/hooks/b", b"plain text", {"Content-Type": "text/plain"})
                time.sleep(0.05)
                fetch(base, "PUT", "/hooks/c", b"\x00\xff\xfe", {"Content-Type": "application/octet-stream"})
                self.assertEqual(rec.recorded, 3)

            scenario = load(out)
            self.assertEqual(validate(scenario), "play")
            sends = [s["send"] for s in scenario["steps"] if "send" in s]
            self.assertEqual(sends[0]["json"], {"n": 1})  # JSON bodies are stored structured, not as escaped text
            self.assertEqual(sends[0]["headers"]["X-Sig"], "abc")
            self.assertEqual(sends[1]["body"], "plain text")
            self.assertIn("body_base64", sends[2])
            waits = [int(s["wait"][:-2]) for s in scenario["steps"] if "wait" in s]
            self.assertEqual(len(waits), 2)
            self.assertGreaterEqual(waits[0], 110)  # the real gap between requests is preserved

            with Receiver() as app:
                player = Player(scenario, target=app.url)
                player.run()
            self.assertTrue(player.ok)
            self.assertEqual([(r["method"], r["path"]) for r in app.requests], [("POST", "/hooks/a?x=1"), ("POST", "/hooks/b"), ("PUT", "/hooks/c")])
            self.assertEqual(json.loads(app.requests[0]["body"]), {"n": 1})
            self.assertEqual(app.requests[0]["headers"]["x-sig"], "abc")
            self.assertEqual(app.requests[2]["body"], b"\x00\xff\xfe")

    def test_forwarding_relays_the_real_answer(self):
        with Receiver(lambda req, n: (202, b"accepted")) as real, tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "captured.json"
            with Recorder(out, port=0, forward=real.url) as rec:
                status, _, body = fetch(f"http://127.0.0.1:{rec.port}", "POST", "/hook", b'{"a":1}')
            recorded = load(out)
        self.assertEqual((status, body), (202, b"accepted"))
        self.assertEqual(real.requests[0]["body"], b'{"a":1}')
        self.assertEqual(recorded["target"], real.url)  # the replay target defaults to the forward URL


class CliTests(unittest.TestCase):
    def test_init_writes_valid_examples_and_respects_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "ex"
            code, out, _ = run_cli("init", str(folder))
            self.assertEqual(code, 0)
            self.assertEqual({p.name for p in folder.iterdir()}, {"webhook.json", "api.json"})
            self.assertEqual(kind(load(folder / "webhook.json")), "play")
            self.assertEqual(kind(load(folder / "api.json")), "serve")
            (folder / "api.json").write_text("{}")
            self.assertIn("skip", run_cli("init", str(folder))[1])
            self.assertEqual((folder / "api.json").read_text(), "{}")
            run_cli("init", str(folder), "--force")
            self.assertEqual(kind(load(folder / "api.json")), "serve")

    def test_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp) / "good.json", Path(tmp) / "bad.json"
            run_cli("init", tmp)
            bad.write_text('{"steps": [{"send": {"path": "/"}, "retires": {}}]}')
            self.assertEqual(run_cli("validate", str(Path(tmp) / "webhook.json"))[0], 0)
            code, _, err = run_cli("validate", str(bad))
            self.assertEqual(code, 2)
            self.assertIn("did you mean 'retry'", err)

    def test_the_bundled_example_passes_against_the_demo_app(self):
        with tempfile.TemporaryDirectory() as tmp, DemoApp(port=0) as app:
            run_cli("init", tmp)
            code, out, _ = run_cli("send", str(Path(tmp) / "webhook.json"), "--target", f"http://127.0.0.1:{app.port}", "--speed", "50")
        self.assertEqual(code, 0, out)
        self.assertIn("all 10 requests passed", out)
        self.assertIn("Timeline", out)

    def test_send_exit_code_and_json_output_for_ci(self):
        with Receiver(lambda req, n: (500, b"boom")) as app, tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"target": app.url, "steps": [{"send": {"path": "/"}, "expect": {"status": 200}}]}))
            code, out, _ = run_cli("send", str(path))
            self.assertEqual(code, 1)
            self.assertIn("FAIL", out)
            code, out, _ = run_cli("send", str(path), "--json")
        report = json.loads(out)
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertEqual(report["results"][0]["status"], 500)

    def test_wrong_file_kind_and_missing_file_are_usage_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli("init", tmp)
            code, _, err = run_cli("send", str(Path(tmp) / "api.json"))
            self.assertEqual(code, 2)
            self.assertIn("rehearse serve", err)
            code, _, err = run_cli("serve", str(Path(tmp) / "webhook.json"))
            self.assertEqual(code, 2)
            self.assertIn("rehearse send", err)
            self.assertEqual(run_cli("send", str(Path(tmp) / "nope.json"))[0], 2)

    def test_version(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()):
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)


class DemoAppTests(unittest.TestCase):
    def test_behaves_like_a_robust_receiver(self):
        from rehearse.signing import sign_body

        def post(app, payload, tamper=False):
            body = json.dumps(payload).encode()
            _, sig = sign_body(body, "whsec_test_123", format="stripe")
            if tamper:
                sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
            return fetch(f"http://127.0.0.1:{app.port}", "POST", "/webhooks/payments", body, {"Stripe-Signature": sig})[0]

        with DemoApp(port=0) as app:
            created = {"id": "e1", "type": "payment.created", "order": "o1"}
            self.assertEqual(post(app, created), 200)
            self.assertEqual(post(app, created), 200)  # duplicate is acknowledged, not reprocessed
            self.assertEqual(len(app.seen), 1)
            self.assertEqual(post(app, created, tamper=True), 401)
            refund = {"id": "e2", "type": "payment.refunded", "order": "o1"}
            self.assertEqual(post(app, refund), 409)  # out of order: refund before payment.succeeded
            self.assertEqual(post(app, {"id": "e3", "type": "payment.succeeded", "order": "o1"}), 200)
            self.assertEqual(post(app, refund), 200)


if __name__ == "__main__":
    unittest.main()
