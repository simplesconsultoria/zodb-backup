# Working conventions for zodb-backup

## Evidence over reasoning

This tool exists to protect other people's data, so claims about it need
evidence, not plausibility.

- When a test fails, run it, capture the traceback, and diagnose from the output.
  Never explain a failure from design reasoning alone.
- Never write "should work". Write what was observed.
- Distinguish what has been **verified** from what is merely **believed**, and be
  explicit about which one you are relying on. Do not quietly promote a belief
  into a fact.
- Call bugs bugs.
- "This approach does not work" is a valid, useful result. Record it and change
  course rather than pushing through.

## Code

- `cli.py` parses and dispatches only. Every operation is a plain function taking
  a `Settings` instance, so it is testable without Typer.
- Precedence is CLI flag > environment variable > default. `Settings.from_env()`
  is the only place environment variables are read.
- All subprocess calls (`rsync`, `tar`, hooks) go through one helper that logs
  the exact command line and raises on a non-zero exit.
- Log to stdout/stderr only. Exit codes are a contract: `0` ok, `1` operational
  failure, `2` configuration error — monitoring depends on them.
- A stub must never exit 0. Cron would record a successful backup that never
  happened.
- Type hints everywhere; docstrings in reStructuredText.

## Tooling

- `uv` for everything: `uv sync`, `uv run pytest`, `uvx ruff`. Never pip or pipx.
- `make check && make test` before declaring any task done.
- pytest style only, no `unittest.TestCase`. Every bug found later gets a
  regression test first.
- A towncrier fragment in `news/` per pull request.

## Provenance

This project is derived from `collective.recipe.backup` (GPL-2.0-only). Read the
actual recipe source before reimplementing one of its modules, and record every
adopted or diverged behaviour in `docs/provenance.md`.
