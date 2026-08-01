# Provenance

`zodb-backup` is derived from **[collective.recipe.backup](https://github.com/collective/collective.recipe.backup)**
by Reinout van Rees, Maurits van Rees and contributors — a buildout recipe
described by its own authors as "sensible defaults around `bin/repozo`". That
recipe carries roughly two decades of accumulated operational wisdom about
backing up ZODB in production. This project ports that *behaviour* to an
environment-variable-driven CLI for one-shot containers.

## License

`collective.recipe.backup` declares `license = "GPL-2.0-only"` in its
`pyproject.toml`. `zodb-backup` is licensed **GPL-2.0-only** as well, which keeps
adoption of recipe code legally straightforward and matches the license
expectations of the Plone ecosystem this project may eventually be donated to.

A read-only study clone lives in `vendor-reference/` (git-ignored):

```sh
git clone --depth 1 https://github.com/collective/collective.recipe.backup.git vendor-reference
```

## Behaviour confirmed against the recipe source

Read at commit of the shallow clone taken 2026-07-31.

| Behaviour | Recipe source | Status in `zodb-backup` |
|---|---|---|
| Filestorage is backed up **before** blobs | `main.py` `backup_main()` — `repozorunner.backup_main()` runs first, `copyblobs.backup_blobs()` second | Adopted as invariant I1 |
| A failed filestorage backup aborts before touching blobs | `main.py`: logs "Halting execution due to error; not backing up blobs." and returns | Adopted |
| Blob backup name reuses the timestamp of the matching repozo backup | `copyblobs.py` `get_latest_filestorage_timestamp()` — newest file matching `\d{4}(?:-\d\d){5}\.(?:delta)?fsz?$`, itself adapted from `ZODB/scripts/repozo.py` `find_files` | Adopted as invariant I2 |
| `--quick` is the repozo default | `repozorunner.py` `backup_arguments()` | Adopted (`QUICK=true` default) |
| Restore requires an explicit `yes` | `main.py` `restore_check()` | Adopted, plus `--yes` / `ASSUME_YES` for non-TTY containers |
| Blob tree backed up as `<location>/<base>.<stamp>/<base>/…`, hard-linked against the previous backup with a *relative* `--link-dest` | `copyblobs.py` `backup_blobs()` | Adopted verbatim — the apparently redundant nesting is what makes `--link-dest` line up between runs |
| `--delete` passed to rsync when a previous backup exists | `copyblobs.py` | Adopted, so blobs deleted at source do not linger in later backups |
| An unchanged filestorage means the existing blob backup is reused rather than copied again | `copyblobs.py` `backup_blobs()` | Adopted — it also repairs a run that died between the filestorage and blob steps |
| Retention keeps the newest N **full** backups plus their incrementals; blob backups older than the oldest surviving full backup are orphans and removed | `repozorunner.py` `cleanup()`, `copyblobs.py` `remove_orphaned_blob_backups()` | Adopted |

## Deliberate divergences

| Recipe behaviour | What `zodb-backup` does instead | Why |
|---|---|---|
| Invokes `bin/repozo` via `os.system(quote_command(...))` (`repozorunner.py`) | Calls `ZODB.scripts.repozo` in process — `parseargs()` plus `do_backup` / `do_recover` / `do_verify`, never `main()` | A container has no buildout `bin/` directory. `os.system` also loses stderr and shifts the exit status, and `repozo.main()` converts typed errors into `sys.exit(str(e))`; the layer beneath it raises `NoFiles` / `WouldOverwriteFiles` / `VerificationFail` |
| Lets repozo stamp each backup with the wall clock, then reads the timestamp back off the filesystem | Allocates the timestamp itself and injects it via `options.test_now`, guaranteeing it is strictly newer than every existing backup | Avoids an upstream data-loss bug (below) and lets the blob backup reuse the identical timestamp without a second directory scan |
| Buildout options, part-name-based naming, script generation | Environment variables into a frozen `Settings` dataclass | No buildout in a container image |
| `zipbackup` / `ziprestore` commands | Dropped — `snapshot` plus `ARCHIVE_BLOB=true` covers the use case | Fewer commands, same capability |
| Multiple filestorages | v1 supports one `Data.fs` and one blobstorage | Deferred (D5) |
| `alternative_restore_source` | Documented as "mount a different backup volume" | Volume mounts already express this |
| Windows support | Dropped | Target is Linux containers |

## An upstream repozo bug we work around

While validating the filestorage layer we found a defect in ZODB's own
`repozo`, which `collective.recipe.backup` inherits.

`ZODB.scripts.repozo.find_files()` reverse-sorts the repository listing and stops
at the first full backup it encounters. When a full backup and an incremental
carry the same timestamp, `"<stamp>.fsz"` sorts *after* `"<stamp>.deltafsz"`
because `"f" > "d"`, so the full backup is seen first, the loop stops, and the
incremental is left out of the restore. **The restore then reports success while
silently missing the data in that incremental.**

The window is narrow — both backups must land in the same clock second, which
scheduled backups rarely do — and two incrementals in one second are safe,
failing loudly with `WouldOverwriteFiles`. Only full-plus-incremental is
dangerous.

`zodb_backup.timestamps.next_stamp()` closes the hole by allocating each
timestamp to be strictly newer than every backup already in the repository, so
the collision cannot occur. Both the upstream behaviour and our guard are pinned
by tests in `tests/unit/test_repozo.py`, so we will notice if ZODB fixes it.

## A limitation inherited from rsync

rsync decides whether a file needs copying from its size and modification time.
A blob rewritten to the *same length* within the same clock second therefore
looks unchanged, and `--link-dest` hard-links the previous, stale content into
the new backup.

ZODB does not produce this situation: blob files are immutable, and a new object
revision is written as a new file rather than an edit of an existing one. The
default is left alone because `--checksum` would re-read the entire blobstorage
on every run, which is exactly what the hard-link design exists to avoid. Set
`RSYNC_OPTIONS=--checksum` if something outside ZODB writes into your
blobstorage. `collective.recipe.backup` has the same property, for the same
reason.

Both the limitation and the `--checksum` escape hatch are covered by tests in
`tests/unit/test_blobs.py`.

## Not yet studied

These recipe areas still need a source read before the corresponding module is
written; update this file in the same PR that implements them.

- `utils.py` helpers worth adopting wholesale

One difference worth calling out: the recipe rotates backups by file
modification time. `zodb-backup` orders by the timestamp embedded in the
filename instead, because mtimes are rewritten by copies, rsync transfers and
restores from other media, while the filename records when the backup was
actually taken.
