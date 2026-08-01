"""End-to-end operations: what each CLI command actually does.

Every function here takes a :class:`~zodb_backup.config.Settings` and can be
called directly from a test without going through Typer.

The order inside a backup is not arbitrary and must not be rearranged:

1. ``PRE_COMMAND`` — before anything is written, so a failure leaves no trace.
2. **Filestorage**, then **blobs**. ZODB writes a blob file to disk before the
   transaction that references it is committed to the filestorage, so every blob
   the filestorage backup can refer to is already on disk by the time the blob
   copy starts. Backing blobs up first would invert that and allow a filestorage
   record with no corresponding blob. Extra blobs newer than the filestorage
   backup are harmless. The invariant breaks if the database is packed during a
   backup, which is why the README says never to pack in a backup window.
3. **Retention**, only after both succeeded, so a failed run never deletes an
   older backup that is still the newest good one.
4. ``POST_COMMAND``.

A failure in the filestorage step abandons the run before blobs are touched: a
blob backup with no filestorage backup beside it is useless.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zodb_backup import blobs as blob_module
from zodb_backup import repozo
from zodb_backup import retention
from zodb_backup.blobs import BlobBackup
from zodb_backup.config import Settings
from zodb_backup.errors import ConfigurationError
from zodb_backup.errors import RestoreError
from zodb_backup.hooks import run_post_command
from zodb_backup.hooks import run_pre_command
from zodb_backup.repozo import BackupResult
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import format_stamp
from zodb_backup.timestamps import latest_stamp
from zodb_backup.timestamps import next_stamp
from zodb_backup.timestamps import parse_stamp

import logging
import sys


logger = logging.getLogger("zodb_backup.operations")


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one backup or snapshot run produced."""

    filestorage: BackupResult
    blobs: BlobBackup | None
    removed: retention.RetentionResult


def _locations(settings: Settings, *, snapshot: bool) -> tuple[Path, Path]:
    """Pick the filestorage and blob locations for a run.

    :param settings: resolved settings for this run.
    :param snapshot: whether this is a snapshot rather than a regular backup.
    :returns: ``(filestorage location, blob location)``.
    """
    if snapshot:
        return settings.snapshot_location, settings.blob_snapshot_location
    return settings.backup_location, settings.blob_backup_location


def _backup_blobs(
    settings: Settings,
    blob_location: Path,
    repository: Path,
    stamp: datetime,
    filestorage_result: BackupResult,
) -> BlobBackup:
    """Back up the blobstorage, reusing an existing backup when nothing changed.

    A blob file cannot appear without a transaction committing to the
    filestorage, so an unchanged filestorage means unchanged blobs. Rather than
    writing an identical copy under a fresh timestamp — which for
    ``ARCHIVE_BLOB`` means re-tarring the whole blobstorage on every idle run —
    the run is pinned to the newest filestorage backup that exists.

    If no blob backup exists for that timestamp the copy is made anyway. That is
    what repairs a previous run which died between the filestorage and blob
    steps, leaving a filestorage backup with no blobs beside it.

    :param settings: resolved settings for this run.
    :param blob_location: directory to write the blob backup into.
    :param repository: directory holding the filestorage backups.
    :param stamp: the timestamp allocated for this run.
    :param filestorage_result: what the filestorage step produced.
    :returns: the blob backup now paired with the filestorage.
    """
    assert settings.blobstorage is not None
    if filestorage_result.changed:
        blob_stamp = stamp
    else:
        blob_stamp = latest_stamp(repository) or stamp

    existing = next(
        (
            candidate
            for candidate in blob_module.find_backups(blob_location)
            if candidate.stamp == blob_stamp
        ),
        None,
    )
    if existing is not None:
        logger.info(
            "blobs unchanged since %s; keeping %s",
            format_stamp(blob_stamp),
            existing.name,
        )
        if not existing.archive:
            blob_module.update_latest_symlink(blob_location, existing.path)
        return existing

    return blob_module.backup(
        source=settings.blobstorage,
        location=blob_location,
        stamp=blob_stamp,
        archive=settings.archive_blob,
        compress=settings.compress_blob,
        use_rsync=settings.use_rsync,
        rsync_options=settings.rsync_options,
    )


def backup(settings: Settings, *, snapshot: bool = False) -> RunResult:
    """Back up the filestorage and, when configured, the blobstorage.

    :param settings: resolved settings for this run.
    :param snapshot: write a full backup into the snapshot locations instead of
        an incremental one into the regular locations.
    :returns: what the run produced.
    :raises BackupError: if the filestorage or blob backup fails.
    :raises CommandError: if a hook fails.
    """
    repository, blob_location = _locations(settings, snapshot=snapshot)
    run_pre_command(settings)

    stamp = next_stamp(repository)
    filestorage_result = BackupResult(stamp=stamp, path=None, full=False)

    if not settings.only_blobs:
        filestorage_result = repozo.backup(
            datafs=settings.datafs,
            repository=repository,
            stamp=stamp,
            full=settings.full or snapshot,
            quick=settings.quick,
            gzip=settings.gzip,
            verbose=settings.debug,
        )

    blob_result = None
    if settings.blobs_enabled:
        assert settings.blobstorage is not None  # guaranteed by blobs_enabled
        blob_result = _backup_blobs(
            settings, blob_location, repository, stamp, filestorage_result
        )

    removed = retention.apply(
        repository=repository,
        keep=settings.keep,
        blob_location=blob_location if settings.blobs_enabled else None,
    )

    run_post_command(settings)
    return RunResult(filestorage=filestorage_result, blobs=blob_result, removed=removed)


def snapshot(settings: Settings) -> RunResult:
    """Make a full backup into the snapshot locations.

    :param settings: resolved settings for this run.
    :returns: what the run produced.
    """
    return backup(settings, snapshot=True)


def _confirm(settings: Settings, targets: list[Path]) -> None:
    """Require an explicit confirmation before overwriting live data.

    On a terminal the operator must type ``yes``. Containers usually have no
    terminal, so without ``--yes`` / ``ASSUME_YES`` the command fails with an
    explanation instead of hanging forever on a prompt nobody can answer.

    :param settings: resolved settings for this run.
    :param targets: the paths that are about to be replaced.
    :raises RestoreError: if the restore is not confirmed.
    """
    if settings.assume_yes:
        return
    if not sys.stdin.isatty():
        raise RestoreError(
            "refusing to restore without confirmation: no terminal is attached, "
            "so pass --yes or set ASSUME_YES=true"
        )
    print("This will irreversibly replace:")
    for target in targets:
        print(f"  {target}")
    if input("Type 'yes' to continue: ").strip() != "yes":
        raise RestoreError("restore cancelled; nothing was changed")


def restore(
    settings: Settings, date: str | None = None, *, snapshot: bool = False
) -> None:
    """Restore the filestorage and blobstorage.

    The blob backup chosen is the newest one that is not newer than the restored
    filestorage state, so the two always describe the same moment.

    :param settings: resolved settings for this run.
    :param date: restore the state as of this UTC stamp
        (``yyyy-mm-dd[-hh[-mm[-ss]]]``); the latest state when ``None``.
    :param snapshot: restore from the snapshot locations.
    :raises ConfigurationError: if ``date`` is not a valid timestamp.
    :raises RestoreError: if the restore is not confirmed or cannot be done.
    """
    repository, blob_location = _locations(settings, snapshot=snapshot)

    targets = []
    if not settings.only_blobs:
        targets.append(settings.datafs)
    if settings.blobs_enabled:
        assert settings.blobstorage is not None
        targets.append(settings.blobstorage)
    _confirm(settings, targets)

    if not settings.only_blobs:
        repozo.recover(
            repository=repository,
            output=settings.datafs,
            date=date,
            verbose=settings.debug,
        )

    if not settings.blobs_enabled:
        return
    assert settings.blobstorage is not None

    moment = _restored_moment(repository, date)
    chosen = blob_module.find_backup_at_or_before(blob_location, moment)
    if chosen is None:
        raise RestoreError(
            f"no blob backup at or before {format_stamp(moment)} in {blob_location}"
        )
    blob_module.restore(
        backup=chosen,
        destination=settings.blobstorage,
        use_rsync=settings.use_rsync,
        rsync_options=settings.rsync_options,
    )


def _restored_moment(repository: Path, date: str | None) -> datetime:
    """Work out which point in time a restore reproduces.

    :param repository: directory holding the filestorage backups.
    :param date: the requested date, or ``None`` for the latest state.
    :returns: the moment the restored filestorage corresponds to.
    :raises ConfigurationError: if ``date`` cannot be parsed.
    :raises RestoreError: if the repository holds no backups.
    """
    backups = find_backup_files(repository)
    if not backups:
        raise RestoreError(f"no filestorage backups found in {repository}")
    if date is None:
        return backups[-1].stamp
    try:
        return parse_stamp(_pad_date(date))
    except ValueError as exc:
        raise ConfigurationError(
            f"{date!r} is not a valid date; use yyyy-mm-dd[-hh[-mm[-ss]]]"
        ) from exc


def _pad_date(date: str) -> str:
    """Expand a partial repozo date into a full timestamp.

    ``2026-03-01`` means the end of that day, matching repozo's "state as of"
    semantics rather than the start of the day.

    :param date: a partial or complete date string.
    :returns: a complete ``yyyy-mm-dd-hh-mm-ss`` string.
    :raises ConfigurationError: if the date has too many components.
    """
    parts = date.split("-")
    if len(parts) < 3 or len(parts) > 6:
        raise ConfigurationError(
            f"{date!r} is not a valid date; use yyyy-mm-dd[-hh[-mm[-ss]]]"
        )
    ceilings = ["23", "59", "59"]
    parts += ceilings[len(parts) - 3 :]
    return "-".join(parts)


def list_backups(settings: Settings, *, snapshot: bool = False) -> list[str]:
    """Describe the available backups, newest last.

    :param settings: resolved settings for this run.
    :param snapshot: list the snapshot locations instead.
    :returns: one human-readable line per backup.
    """
    repository, blob_location = _locations(settings, snapshot=snapshot)
    lines = []
    for entry in find_backup_files(repository):
        kind = "full" if entry.full else "incremental"
        size = entry.path.stat().st_size
        lines.append(
            f"{format_stamp(entry.stamp)}  {kind:<12} {_human(size):>10}  {entry.name}"
        )
    if settings.blobs_enabled:
        for blob in blob_module.find_backups(blob_location):
            kind = "blobs (tar)" if blob.archive else "blobs"
            lines.append(
                f"{format_stamp(blob.stamp)}  {kind:<12} {'':>10}  {blob.name}"
            )
    return sorted(lines)


def _human(size: int) -> str:
    """Render a byte count compactly.

    :param size: number of bytes.
    :returns: a short string such as ``1.4 MB``.
    """
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def verify(settings: Settings, *, snapshot: bool = False) -> None:
    """Check the backup repository against its recorded checksums.

    :param settings: resolved settings for this run.
    :param snapshot: verify the snapshot locations instead.
    :raises BackupError: if verification fails.
    """
    repository, _ = _locations(settings, snapshot=snapshot)
    repozo.verify(repository=repository, verbose=settings.debug)
