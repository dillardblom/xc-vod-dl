# Contributing

Thanks for considering a contribution.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a PR

```bash
ruff check .
pytest
```

Both must pass; CI runs the same checks. Tests marked `integration` spin up a
local `http.server` (or a fake Xtream API + media server) on `127.0.0.1` — no
real network access or real Xtream server is needed to run the suite.

## Scope

- Bug fixes and test coverage are always welcome.
- For anything touching the concurrency/overload-detection heuristics
  (`download/concurrency.py`) or the resume/verify sequencing
  (`download/engine.py`), please include a test that exercises the new
  behavior via the hermetic local-server fixtures in `tests/conftest.py`
  rather than only against a real server.
- Keep new dependencies to a minimum — this project intentionally favors the
  standard library (`sqlite3`, `http.server` for tests, `subprocess` for
  ffprobe/ffmpeg) over adding packages where the stdlib is sufficient.

## Commit style

Small, focused commits with a message that explains *why*, not just *what*.
