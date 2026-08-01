<div align="center">

<h1 align="center">zodb-backup</h1>

</div>

<div align="center">

[![PyPI](https://img.shields.io/pypi/v/zodb-backup)](https://pypi.org/project/zodb-backup/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/zodb-backup)](https://pypi.org/project/zodb-backup/)


[![GitHub contributors](https://img.shields.io/github/contributors/simplesconsultoria/zodb-backup)](https://github.com/simplesconsultoria/zodb-backup)
[![GitHub Repo stars](https://img.shields.io/github/stars/simplesconsultoria/zodb-backup?style=social)](https://github.com/simplesconsultoria/zodb-backup)

[![CI](https://github.com/simplesconsultoria/zodb-backup/actions/workflows/main.yml/badge.svg)](https://github.com/simplesconsultoria/zodb-backup/actions/workflows/main.yml)

</div>

Container-native backup and restore for ZODB FileStorage and blobstorage, aimed
at Plone/Zope deployments running under Docker and Docker Swarm.

The container runs once and exits, so scheduling belongs to your orchestrator
(swarm-cronjob, a systemd timer, a Kubernetes CronJob). Everything is configured
through environment variables.

> **Status: alpha.** Backup, restore, retention and hooks work, and the container
> is tested against a live ZEO server while it is being written to. What is not
> proven yet: no release has been published, hard-linked blob backups are
> untested on NFS and Gluster, and nobody has yet restored a real production
> database with it. Rehearse your restore before relying on it.

## Credits

`zodb-backup` is directly inspired by, and largely modelled on,
**[collective.recipe.backup](https://github.com/collective/collective.recipe.backup)**
by Reinout van Rees, Maurits van Rees and contributors — in its own words,
"sensible defaults around `bin/repozo`". That recipe carries about two decades of
hard-won operational knowledge about backing up ZODB. This project ports that
behaviour to an environment-variable-driven CLI for one-shot containers; the
buildout plumbing is left behind.

See [docs/provenance.md](docs/provenance.md) for exactly which behaviours were
adopted, which were changed, and why.

## Commands

```sh
zodb-backup backup                  # incremental (full with --full / FULL=true)
zodb-backup snapshot                # full backup into the snapshot locations
zodb-backup restore [DATE]          # restore latest, or the state at DATE
zodb-backup snapshot-restore [DATE]
zodb-backup list                    # available backups, timestamps, sizes
zodb-backup verify                  # repozo verify pass
```

`DATE` uses repozo semantics: UTC, `yyyy-mm-dd[-hh[-mm[-ss]]]`.

Restore asks for a literal `yes`. Containers are usually non-interactive, so
pass `--yes` or set `ASSUME_YES=true` — without a TTY and without either, the
command fails with a clear message rather than hanging.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATAFS` | `/data/filestorage/Data.fs` | mount read-only for backup, read-write for restore |
| `BLOBSTORAGE` | `/data/blobstorage` | empty means filestorage-only |
| `BACKUP_LOCATION` | `/backups/filestorage` | |
| `BLOB_BACKUP_LOCATION` | `/backups/blobstorage` | |
| `SNAPSHOT_LOCATION` | `/backups/snapshots` | |
| `BLOB_SNAPSHOT_LOCATION` | `/backups/blobstoragesnapshots` | |
| `FULL` | `false` | force a full filestorage backup |
| `QUICK` | `true` | repozo `--quick`; disabling it reads 3–4× the database size per run |
| `GZIP` | `true` | repozo `--gzip` |
| `KEEP` | `2` | full backups to keep; `0` keeps everything |
| `BACKUP_BLOBS` | `true` | |
| `ONLY_BLOBS` | `false` | |
| `ARCHIVE_BLOB` | `false` | tar archive instead of an rsync tree |
| `COMPRESS_BLOB` | `false` | only valid with `ARCHIVE_BLOB=true` |
| `USE_RSYNC` | `true` | `false` falls back to a plain directory copy |
| `RSYNC_OPTIONS` | (empty) | extra rsync arguments, shell-quoted |
| `PRE_COMMAND` | (empty) | failure aborts the backup |
| `POST_COMMAND` | (empty) | e.g. push to off-site storage |
| `ASSUME_YES` | `false` | non-interactive restore |
| `DEBUG` / `QUIET` | `false` | mutually exclusive |

Booleans accept `true/1/yes/on` and `false/0/no/off`, case-insensitively.
Anything else is a configuration error — a typo will never be read as "false".

Exit codes: `0` success, `1` operational failure, `2` configuration error.

## Running the container

The image expects the ZODB data at `/data` and writes backups to `/backups`,
matching the layout of the official `plone/plone-zeo` image.

```sh
docker run --rm \
  -v zeo-data:/data:ro \
  -v zeo-backups:/backups \
  -e KEEP=7 \
  ghcr.io/simplesconsultoria/zodb-backup:latest backup
```

Mount the data **read-only**. The tool never writes there, and the mount makes
that a guarantee rather than a promise.

Two things to get right:

- **The container must run as the uid that owns the data.** It runs as uid `500`
  by default, which is what the official Plone images use, so alongside
  `plone/plone-zeo` there is nothing to configure. If your data is owned by
  someone else, pass `--user`.
- **The backups volume must be writable by that uid.** Docker creates named
  volumes owned by `root`, so a fresh one is not writable by a non-root
  container. Fix it once when you create it:

  ```sh
  docker run --rm -v zeo-backups:/backups busybox chown -R 500:500 /backups
  ```

  If you skip this the tool exits `2` and tells you exactly this.

### Scheduling with swarm-cronjob

The container runs once and exits, so scheduling belongs to your orchestrator:

```yaml
services:
  backup:
    image: ghcr.io/simplesconsultoria/zodb-backup:latest
    command: ["backup"]
    volumes:
      - zeo-data:/data:ro
      - zeo-backups:/backups
    environment:
      KEEP: "7"
    deploy:
      mode: replicated
      replicas: 0
      restart_policy:
        condition: none
      labels:
        - "swarm.cronjob.enable=true"
        - "swarm.cronjob.schedule=0 30 3 * * *"
        - "swarm.cronjob.skip-running=true"
```

With **local** volumes, pin the backup job and the ZEO service to the same node
(`deploy.placement.constraints`) — otherwise the job may be scheduled somewhere
the data does not exist.

## Restore runbook

Restoring replaces live data, so take the database offline first. This procedure
is executed by the integration tests, not just documented.

1. **Stop the writers.** Scale the backend and ZEO to zero, so nothing is
   holding the storage open or committing during the restore.

   ```sh
   docker service scale myproject_backend=0 myproject_zeo=0
   ```

2. **Restore.** Containers have no terminal, so `ASSUME_YES=true` is required;
   without it the command refuses rather than hanging on a prompt.

   ```sh
   docker run --rm \
     -v zeo-data:/data \
     -v zeo-backups:/backups:ro \
     -e ASSUME_YES=true \
     ghcr.io/simplesconsultoria/zodb-backup:latest restore
   ```

   Note `/data` is mounted **read-write** here, unlike during a backup, and
   `/backups` read-only. To restore an earlier state, pass a date:
   `restore 2026-03-01-02-00-00`.

3. **Bring the stack back up.**

   ```sh
   docker service scale myproject_zeo=1 myproject_backend=2
   ```

To rehearse without risk, restore into a *different* empty volume and point a
throwaway ZEO server at it. That is exactly what the integration tests do.

## Retention

`KEEP` counts **full filestorage backups**, not files. `KEEP=2` keeps the two
most recent full backups together with every incremental belonging to them —
an incremental is worthless without the full backup it applies to. Blob backups
are not counted separately: one is kept for as long as there is a filestorage
backup it can be restored alongside, and removed once it is older than the
oldest surviving full backup. `KEEP=0` keeps everything.

Rotation happens only after a backup has succeeded, so a failed run never
deletes the backup that is still your newest good one.

## Hooks

`PRE_COMMAND` runs before anything is written; if it fails, the run is abandoned
with nothing on disk. `POST_COMMAND` runs after the backup is complete and is
where off-site replication belongs:

```sh
POST_COMMAND="rclone sync /backups remote:zodb-backups"
```

A failing `POST_COMMAND` makes the process exit non-zero — an off-site copy that
did not happen is worth alerting on — but the backup already written is left
untouched. Both hooks run through a shell, so pipes and `&&` work as expected.

## What is NOT backed up

Being honest about scope, in the spirit of the original recipe:

- **RelStorage / SQL backends** — out of scope; use `pg_dump` or equivalent.
- **The rest of your deployment** — configuration, secrets, uploaded files
  outside the blobstorage, your container images.
- **Off-site copies** — use `POST_COMMAND` to invoke rclone, restic or borg.
- **Encryption** — backups are written unencrypted.

## Warnings

- **Never pack the database during a backup window.** Correct backups rely on
  FileStorage being append-only for the duration of the run; a concurrent
  `zeopack` breaks that assumption.
- Hard-link-based blob backups need a filesystem that supports them. Behaviour
  on NFS and Gluster volumes is untested — prefer `ARCHIVE_BLOB=true` there
  until it is.
- rsync decides what to copy from file size and modification time, so a blob
  rewritten to the **same length within the same second** would be missed and the
  stale copy hard-linked instead. ZODB never does this — blob files are immutable
  and a new revision becomes a new file — but if something outside ZODB writes
  into your blobstorage, set `RSYNC_OPTIONS=--checksum`. That reads every byte on
  every run, which is what the hard-link design exists to avoid, so do not enable
  it by reflex.

## Development

```sh
uv sync
make test      # unit tests
make lint      # ruff + mypy
make format
```

Requires Python 3.14+ and `uv`.

## License

GPL-2.0-only, matching `collective.recipe.backup`. See [LICENSE](LICENSE).
