# Changelog

<!-- towncrier release notes start -->

## 1.0.0a1 (2026-08-01)

### New features

- Added a container image and an opt-in integration suite that runs it against a live ZEO server. The image runs as a non-root user, ships rsync, and expects the data layout of the official `plone/plone-zeo` image. The tests take a backup while a writer is committing objects and blobs, restore into a separate volume, and verify every restored object and blob against content derived independently of the backup. @ericof
- Added blob backup and restore in four modes: an rsync tree that hard-links unchanged blobs against the previous backup, a plain directory copy for systems without rsync, and tar or tar.gz archives. Blob backups are named after the filestorage backup made in the same run, and a `latest` symlink is updated atomically. @ericof
- Added filestorage backup, restore and verify, driving ZODB's `repozo` in process. Backup timestamps are allocated to be strictly monotonic, which avoids an upstream `repozo` defect where an incremental sharing a clock second with its full backup is silently dropped from later restores. @ericof
- Wired every command to real work: `backup`, `snapshot`, `restore`, `snapshot-restore`, `list` and `verify`. A run applies the pre-command hook, backs up the filestorage before the blobs, rotates old backups only once both succeeded, and then runs the post-command hook. Retention keeps a number of full backups together with their incrementals and removes blob backups no surviving filestorage backup can be restored with. Restore requires an explicit confirmation and refuses to touch anything without one. @ericof

### Bug fixes

- A backup location that cannot be created or written to now reports a configuration error naming the directory and the user id, and exits with status 2, instead of failing with a traceback. This is what a non-root container hits against a freshly created Docker volume. @ericof

### Internal

- Added the project scaffold: GPL-2.0 licensing, hatchling packaging, ruff and strict mypy, pytest, towncrier, a GitHub Actions workflow, and the `zodb-backup` command surface with environment-variable configuration. @ericof
- Fixed the container image workflow's job summary step, which aborted with a shell syntax error because the newline-separated tag list was spliced directly into the script text. The image itself built and pushed correctly, but the failed step turned the whole run red. Values now reach the script through the environment. @ericof
