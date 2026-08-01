# Summary

<!-- What changes, and why. Link the issue if there is one. -->

Closes #

## What was verified

<!--
The important section. Describe what you *observed*, not what should happen.
Paste the commands you ran and their results. If something is believed but not
verified, say so explicitly here rather than leaving it implicit.
-->

## Checklist

- [ ] `make check` passes
- [ ] `make test` passes
- [ ] Integration tests run (`make test-integration`), or not applicable
- [ ] Tests added — a regression test first, if this fixes a bug
- [ ] A towncrier fragment is in `news/`
- [ ] Docstrings are reStructuredText and type hints are present
- [ ] `docs/provenance.md` updated, if this adopts or diverges from
      `collective.recipe.backup`

## Risk

<!--
Does this touch the restore path, retention, or the exit-code contract? Those
are the places where a mistake silently costs someone their data. If it does,
say what you did to convince yourself it is correct.
-->

- [ ] Touches the restore path
- [ ] Touches retention or deletion
- [ ] Changes exit codes, environment variables, or defaults (needs `breaking`)
