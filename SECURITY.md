# Security Policy

## Supported versions

`zodb-backup` is in alpha and has not had a stable release. Only the most recent
release receives fixes; there are no maintenance branches for older versions.

| Version | Supported |
| --- | --- |
| Latest release | ✅ |
| Anything older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/simplesconsultoria/zodb-backup/security/advisories/new),
or by email to <contato@simplesconsultoria.com.br>.

Please include:

- what an attacker can do, and what access they need to do it;
- the version or image tag affected;
- the configuration involved — particularly `PRE_COMMAND` / `POST_COMMAND`,
  `RSYNC_OPTIONS`, and the uid the container runs as;
- a reproduction, if you have one.

We will acknowledge your report within five working days and keep you updated as
we work on it. If the report is accepted we will agree a disclosure date with
you, credit you in the advisory unless you prefer otherwise, and publish a GitHub
Security Advisory alongside the fix.

## Scope

This tool reads a ZODB database and writes backups of it. The things worth
reporting are, in rough order of severity:

- **Silent data loss or corruption** — a backup that appears to succeed but
  cannot be restored, a restore that produces a database differing from the one
  backed up, or retention deleting a backup that is still needed. This is the
  most serious class of bug in a backup tool, whether or not an attacker is
  involved, and we treat it as a security issue.
- **Command injection** through configuration values, especially the hook
  variables and `RSYNC_OPTIONS`, which are deliberately passed to a shell.
- **Path traversal or privilege issues** in the backup and restore paths, or
  anything that lets the tool write outside its configured locations.
- **Secret disclosure** — credentials or database contents leaking into logs,
  error messages, or backup filenames.

Known and documented limitations are **not** vulnerabilities. These are described
in the [README](README.md) and are working as designed:

- backups are written **unencrypted**;
- hook commands run through a shell by design, so anyone who can set
  `PRE_COMMAND` or `POST_COMMAND` can already run arbitrary commands as the
  container user — treat those variables as equivalent to shell access;
- rsync's size+mtime quick check can miss a same-length rewrite within one
  second (not reachable through ZODB, whose blobs are immutable).

## Upstream

`zodb-backup` drives ZODB's `repozo` in process. A vulnerability in ZODB itself
should be reported to the [ZODB project](https://github.com/zopefoundation/ZODB).
If you are unsure which side a problem belongs to, report it to us and we will
route it.
