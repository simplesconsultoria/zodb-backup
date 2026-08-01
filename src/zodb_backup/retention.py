"""Backup rotation, and the coupling between filestorage and blob backups.

The retention model comes from ``collective.recipe.backup`` 4.0 and later:

* ``KEEP`` counts **full filestorage backups**, not files. Keeping 2 means
  keeping the two most recent full backups together with every incremental that
  belongs to them — deleting a full backup without its incrementals, or an
  incremental without its full backup, would leave a chain that cannot be
  restored.
* Blob backups are not counted separately. A blob backup is kept exactly as long
  as there is a filestorage backup it can be restored alongside; once it is older
  than the oldest surviving full backup it is an orphan and is removed.
* ``KEEP=0`` means keep everything.

One deliberate difference from the recipe: it rotates by file modification time,
which a copy, an rsync or a restore from tape can rewrite. We order by the
timestamp embedded in the filename instead. Those timestamps are allocated
monotonically (see :mod:`zodb_backup.timestamps`), so they are a more reliable
record of the order things actually happened in.
"""

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from zodb_backup.blobs import LATEST_LINK
from zodb_backup.blobs import find_backups as find_blob_backups
from zodb_backup.blobs import update_latest_symlink
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import parse_stamp

import logging
import re
import shutil


logger = logging.getLogger("zodb_backup.retention")

#: Any file repozo names after a timestamp: data files, plus .dat and .index.
STAMPED_FILE_PATTERN = re.compile(r"^(\d{4}(?:-\d\d){5})\.")


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What a rotation removed."""

    filestorage: list[Path] = field(default_factory=list)
    blobs: list[Path] = field(default_factory=list)

    @property
    def removed_any(self) -> bool:
        """Whether anything was deleted."""
        return bool(self.filestorage or self.blobs)


def oldest_retained_full(repository: Path, keep: int) -> datetime | None:
    """Find the timestamp of the oldest full backup that rotation would keep.

    This is the cutoff every other decision is derived from: filestorage files
    older than it are obsolete, and blob backups older than it are orphans.

    :param repository: directory holding the filestorage backups.
    :param keep: number of full backups to retain; ``0`` keeps everything.
    :returns: the cutoff timestamp, or ``None`` when nothing would be removed,
        either because ``keep`` is 0 or because there are not that many full
        backups yet.
    """
    fulls = [backup for backup in find_backup_files(repository) if backup.full]
    if keep <= 0 or len(fulls) <= keep:
        return None
    return fulls[-keep].stamp


def rotate_filestorage(repository: Path, keep: int) -> list[Path]:
    """Delete filestorage backups that fall outside the retention window.

    Everything named after a timestamp older than the oldest retained full
    backup goes, including the ``.dat`` and ``.index`` companions repozo writes
    beside each backup. Incrementals belonging to a retained full backup are
    newer than the cutoff, so they survive with it.

    :param repository: directory holding the filestorage backups.
    :param keep: number of full backups to retain; ``0`` keeps everything.
    :returns: the paths that were removed.
    """
    cutoff = oldest_retained_full(repository, keep)
    if cutoff is None:
        logger.debug("nothing to rotate in %s", repository)
        return []

    removed = []
    for entry in sorted(repository.iterdir()):
        match = STAMPED_FILE_PATTERN.match(entry.name)
        if match is None or not entry.is_file():
            continue
        if parse_stamp(match.group(1)) >= cutoff:
            continue
        entry.unlink()
        removed.append(entry)
        logger.info("removed old filestorage backup file %s", entry.name)
    return removed


def remove_orphaned_blobs(blob_location: Path, repository: Path) -> list[Path]:
    """Delete blob backups that no filestorage backup can be restored with.

    A blob backup older than the oldest surviving full filestorage backup can
    never be paired with anything, so keeping it only consumes disk.

    Nothing is removed when the filestorage repository holds no full backup at
    all: that means rotation has not established a cutoff, and deleting blob
    backups on a guess would destroy the only copies we have.

    :param blob_location: directory holding the blob backups.
    :param repository: directory holding the filestorage backups.
    :returns: the paths that were removed.
    """
    fulls = [backup for backup in find_backup_files(repository) if backup.full]
    if not fulls:
        logger.debug("no full filestorage backup; leaving blob backups alone")
        return []
    cutoff = fulls[0].stamp

    removed = []
    for backup in find_blob_backups(blob_location):
        if backup.stamp >= cutoff:
            continue
        if backup.archive:
            backup.path.unlink()
        else:
            shutil.rmtree(backup.path)
        removed.append(backup.path)
        logger.info("removed orphaned blob backup %s", backup.name)

    if removed:
        _repoint_latest(blob_location)
    return removed


def _repoint_latest(blob_location: Path) -> None:
    """Make the ``latest`` symlink point at a backup that still exists.

    Called after deletions, so the link never dangles.

    :param blob_location: directory holding the blob backups.
    """
    trees = [
        backup for backup in find_blob_backups(blob_location) if not backup.archive
    ]
    update_latest_symlink(blob_location, trees[-1].path if trees else None)


def apply(
    *,
    repository: Path,
    keep: int,
    blob_location: Path | None = None,
) -> RetentionResult:
    """Rotate filestorage backups and drop the blob backups they orphan.

    Order matters: filestorage rotation runs first so that blob removal is
    decided against the backups that actually survived.

    :param repository: directory holding the filestorage backups.
    :param keep: number of full backups to retain; ``0`` keeps everything.
    :param blob_location: directory holding the blob backups, if blobs are in
        use.
    :returns: what was removed.
    """
    filestorage = rotate_filestorage(repository, keep)
    blobs: list[Path] = []
    if blob_location is not None:
        blobs = remove_orphaned_blobs(blob_location, repository)
    return RetentionResult(filestorage=filestorage, blobs=blobs)


def verify_coupling(repository: Path, blob_location: Path) -> list[str]:
    """Check that every surviving blob backup can still be restored.

    Used by tests as an executable statement of invariant I3, and cheap enough
    to call after any rotation.

    :param repository: directory holding the filestorage backups.
    :param blob_location: directory holding the blob backups.
    :returns: a list of human-readable problems; empty when the invariant holds.
    """
    problems = []
    fulls = [backup for backup in find_backup_files(repository) if backup.full]
    if not fulls:
        if find_blob_backups(blob_location):
            problems.append("blob backups exist but no full filestorage backup does")
        return problems

    cutoff = fulls[0].stamp
    for backup in find_blob_backups(blob_location):
        if backup.stamp < cutoff:
            problems.append(
                f"blob backup {backup.name} predates the oldest full "
                f"filestorage backup and can never be restored"
            )

    link = blob_location / LATEST_LINK
    if link.is_symlink() and not link.exists():
        problems.append("the 'latest' symlink is dangling")
    return problems
