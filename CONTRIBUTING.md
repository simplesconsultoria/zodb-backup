# Contributing to zodb-backup

Thanks for wanting to help. This tool exists to protect other people's
databases, so the bar here is a little different from most projects: **claims
about behaviour need evidence, not plausibility.**

## The one rule that matters

When you say something works, say what you *observed*. Not what should happen
given the design.

- If a test fails, run it, capture the traceback, and diagnose from the output.
- Never write "should work" in a pull request description or a commit message.
- Keep what you have **verified** separate from what you **believe**, and be
  explicit about which one you are relying on.
- Call bugs bugs.
- "This approach does not work" is a useful result. Record it and change course
  rather than pushing through.

## Getting set up

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Never use `pip` or
`pipx` here — `uv` manages everything.

```sh
uv sync
make check     # ruff format + lint, pyroma, python version consistency, mypy
make test      # 208 unit tests
```

`make check && make test` must pass before you open a pull request.

The integration suite is opt-in because it needs Docker. It builds the image and
runs it against a live ZEO server that is being written to while the backup runs:

```sh
make test-integration
make stack-down          # tear down the stack and its volumes afterwards
```

## Code conventions

- `cli.py` parses and dispatches only. Every operation is a plain function
  taking a `Settings` instance, so it can be tested without going through Typer.
- Precedence is **CLI flag > environment variable > default**, and
  `Settings.from_env()` is the only place environment variables are read.
- All subprocess calls — `rsync`, `tar`, hooks — go through the single helper in
  `commands.py`, which logs the exact command line and raises on a non-zero exit.
- Log to stdout and stderr only.
- **Exit codes are a contract**: `0` success, `1` operational failure, `2`
  configuration error. Monitoring depends on them, so do not add new meanings
  casually.
- **A stub must never exit `0`.** Cron would record a successful backup that
  never happened.
- Type hints everywhere. Docstrings in reStructuredText — `:param:`,
  `:returns:`, `:raises:`.

## Tests

- pytest style only. No `unittest.TestCase`.
- Every bug found after the fact gets a **regression test first**, then the fix.
- Tests that need Docker belong in `tests/integration/` and stay deselected by
  default.

## Provenance

`zodb-backup` is derived from
[collective.recipe.backup](https://github.com/collective/collective.recipe.backup)
(GPL-2.0-only). Before reimplementing one of its behaviours, read the actual
recipe source — a copy is under `vendor-reference/` — and record what you adopted
or changed, and why, in [docs/provenance.md](docs/provenance.md).

This project is GPL-2.0-only for the same reason. Contributions are accepted
under that licence.

## Changelog

Every pull request needs a [towncrier](https://towncrier.readthedocs.io/)
fragment in `news/`, named `<issue-number>.<type>` when there is an issue and
`+<short-slug>.<type>` when there is not. Valid types:

| Type | Use for |
| --- | --- |
| `breaking` | changes that break existing configurations or the exit-code contract |
| `feature` | new user-visible behaviour |
| `bugfix` | user-visible fixes |
| `internal` | refactors, packaging, CI |

Write the entry in past tense, aimed at someone operating the tool, and end it
with your GitHub handle:

```
# news/+blob-tar-permissions.bugfix
Blob archives now preserve the original file mode instead of the umask of the
backup process. @yourhandle
```

Preview the result with `make changelog`.

CI checks for a fragment, so a missing one will fail the pull request.

## Pull requests

1. Branch off `main`.
2. Make the change, with tests.
3. Add the news fragment.
4. Run `make check && make test`.
5. Open the pull request and fill in the template — particularly the section
   asking what you actually verified.

Small, focused pull requests get reviewed faster than large ones.

## Reporting bugs

Use the issue templates. For anything involving a backup that cannot be
restored, or data that came back different, please include the exact commands,
the environment variables in play, and the full output — that class of bug is
the highest priority in this project.

**Security problems do not go in the issue tracker.** See
[SECURITY.md](SECURITY.md).
