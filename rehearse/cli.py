"""Command-line interface: rehearse send | serve | record | validate | init | demo-app."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .demo_app import DemoApp
from .errors import ScenarioError
from .examples import FILES, dump
from .player import Player
from .recorder import Recorder
from .report import format_line, paint, render_timeline, summary
from .scenario import kind, load, validate
from .server import FaultServer


def _color(stream) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def cmd_send(args: argparse.Namespace) -> int:
    scenario = load(args.file)
    if kind(scenario) != "play":
        raise ScenarioError(f"{args.file} defines routes, so run it with: rehearse serve {args.file}")
    color = _color(sys.stdout) and not args.json
    live = None if args.json else (lambda r: print(format_line(r, color), flush=True))
    if not args.json:
        print(paint(scenario.get("name", args.file), "bold", color))
    player = Player(scenario, target=args.target, speed=args.speed, on_result=live)
    results = player.run()
    if args.json:
        print(json.dumps({"ok": player.ok, "results": [r.to_dict() for r in results]}, indent=2))
    else:
        print()
        print(render_timeline(results))
        print()
        print(summary(results, color))
    return 0 if player.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    config = load(args.file)
    if kind(config) != "serve":
        raise ScenarioError(f"{args.file} defines steps, so run it with: rehearse send {args.file}")
    log_file = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(line: str) -> None:
        _stderr(line)
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()

    server = FaultServer(config, host=args.host, port=args.port, log=log, seed=args.seed).bind()
    print(f"{config.get('name', 'Mock API')}: listening on {server.url}")
    for route in server.routes:
        print(f"  {route.name}: {len(route.responses)} response(s) in sequence")
    if server.upstream:
        print(f"  everything else is proxied to {config['upstream']}")
    print("Inspect calls at /__rehearse/calls, reset at POST /__rehearse/reset. Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if log_file:
            log_file.close()
    print(f"\n{len(server.calls)} request(s) handled.")
    for route in server.routes:
        print(f"  {route.name}: {route.count} call(s)")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    recorder = Recorder(args.out, host=args.host, port=args.port, forward=args.forward, target=args.target, log=_stderr)
    recorder.bind()
    print(f"Recording on http://{args.host}:{recorder.port}  ->  {args.out}")
    print("Forwarding to " + args.forward if args.forward else "Answering 200 to everything.")
    print("Press Ctrl-C to finish. Replay with: rehearse send " + str(args.out) + " --target URL\n")
    try:
        recorder.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        recorder.save()
        recorder.stop()
    print(f"\nSaved {recorder.recorded} request(s) to {args.out}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    code = 0
    for path in args.files:
        try:
            which = validate(load(path))
            print(f"ok     {path} ({'sends events' if which == 'play' else 'mock API'})")
        except ScenarioError as e:
            print(f"error  {path}: {e}", file=sys.stderr)
            code = 2
    return code


def cmd_init(args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    for name, scenario in FILES.items():
        path = folder / name
        if path.exists() and not args.force:
            print(f"skip   {path} (exists, use --force to overwrite)")
            continue
        path.write_text(dump(scenario), encoding="utf-8")
        print(f"wrote  {path}")
    print("\nTry it:\n  rehearse demo-app            # in one terminal\n  rehearse send " + str(folder / "webhook.json"))
    return 0


def cmd_demo_app(args: argparse.Namespace) -> int:
    app = DemoApp(host=args.host, port=args.port, log=_stderr).bind()
    print(f"Demo webhook receiver on http://{args.host}:{app.port}  (Ctrl-C to stop)")
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rehearse",
        description="Rehearse the worst day of your integrations: webhook timelines, flaky APIs, network faults.",
    )
    parser.add_argument("--version", action="version", version=f"rehearse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("send", help="play a scenario of events against your app")
    p.add_argument("file")
    p.add_argument("--target", help="base URL, overrides the scenario's target")
    p.add_argument("--speed", type=float, default=1.0, help="time multiplier for waits (10 = ten times faster)")
    p.add_argument("--json", action="store_true", help="print machine-readable results (for CI)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("serve", help="run a local API that returns scripted responses and faults")
    p.add_argument("file")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--log", help="also append request lines to this file")
    p.add_argument("--seed", type=int, default=None, help="seed the latency jitter for reproducible runs")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("record", help="capture incoming requests into a replayable scenario")
    p.add_argument("--out", default="recorded.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--forward", help="also forward each request to this real URL and relay its answer")
    p.add_argument("--target", help="target URL to store in the recorded scenario")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("validate", help="check scenario files without running them")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("init", help="write example scenarios")
    p.add_argument("dir", nargs="?", default="rehearse-examples")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("demo-app", help="run a small webhook receiver to try the examples against")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3000)
    p.set_defaults(func=cmd_demo_app)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ScenarioError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
