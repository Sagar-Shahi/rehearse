# Contributing to Rehearse

Rehearse is small on purpose: standard library only, one JSON file per scenario, no daemon, no GUI required.
Changes that keep it that way are the easiest to merge.

## Good first contributions

- A new `corrupt` mode or fault (for example a slow-drip response or a duplicated header).
- A new signature format (`sign.format`) for a provider you use. Add it to `rehearse/signing.py` and a test.
- HAR or curl import: turn captured traffic into a `steps` scenario.
- Better timeline output (colors per status class, a `--no-timeline` flag).
- Docs: a recipe for a provider or framework you know well.

## Workflow

1. Fork, branch, and make your change **with a test**. Tests talk to real local sockets (see `tests/support.py`).
2. Run the suite: `python -m unittest discover -s tests -t .` (about 6 seconds).
3. If you changed the file format, update `docs/scenario-format.md` and `rehearse/examples.py`.
4. Open a pull request describing the failure you wanted to be able to rehearse.

## Style

- Python 3.10+, type hints, no runtime dependencies.
- Validation errors name the exact path in the file (`steps[2].send.corrupt`) and suggest a fix.
- Comments explain *why*. Keep functions short enough to read in one screen.
