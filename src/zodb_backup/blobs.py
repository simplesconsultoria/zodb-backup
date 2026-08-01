"""Blob backup and restore.

A blob backup is named after the filestorage backup made in the same run, so the
two can always be paired again at restore time. Given a blobstorage at
``/data/blobstorage`` and a backup location ``/backups/blobstorage``, one run
produces either

* ``/backups/blobstorage/blobstorage.2026-03-01-12-00-00/blobstorage/…`` — a
  directory tree, or
* ``/backups/blobstorage/blobstorage.2026-03-01-12-00-00.tar[.gz]`` — an archive.

The tree layout keeps the blobstorage's own directory name inside the timestamped
folder. That looks redundant but is what makes ``rsync --link-dest`` line up
between consecutive backups, so unchanged blobs become hard links instead of
copies (the Mike Rubel snapshot pattern). It is inherited from
``collective.recipe.backup``; see ``docs/provenance.md``.

A ``latest`` symlink in the backup location points at the newest tree backup.
Unlike the original recipe, which unlinks and re-creates it, we build a temporary
symlink and rename it over the old one, so a reader never observes a moment where
``latest`` is missing.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zodb_backup.commands import require
from zodb_backup.commands import run
from zodb_backup.errors import BackupError
from zodb_backup.errors import ConfigurationError
from zodb_backup.errors import RestoreError
from zodb_backup.timestamps import format_stamp
from zodb_backup.timestamps import parse_stamp

import logging
import os
import re
import shutil
import tarfile
import tempfile


logger = logging.getLogger("zodb_backup.blobs")

#: Name of the symlink pointing at the newest tree backup.
LATEST_LINK = "latest"

#: Suffixes that mark an archived blob backup, longest first so ``.tar.gz`` wins.
ARCHIVE_SUFFIXES = (".tar.gz", ".tar")

_STAMP = r"\d{4}(?:-\d\d){5}"
#: Matches both layouts, capturing the timestamp and the archive suffix.
BACKUP_PATTERN = re.compile(
    rf"^(?P<base>.+)\.(?P<stamp>{_STAMP})(?P<suffix>\.tar\.gz|\.tar)?$"
)


@dataclass(frozen=True, slots=True, order=True)
class BlobBackup:
    """One blob backup in a backup location.

    Ordering is by ``stamp`` first, so a sorted sequence is chronological.
    """

    stamp: datetime
    path: Path
    archive: bool

    @property
    def name(self) -> str:
        """The backup's base name."""
        return self.path.name


def _ensure_writable(directory: Path) -> None:
    """Create a backup directory, reporting permission problems clearly.

    :param directory: the directory to create.
    :raises ConfigurationError: if it cannot be created or written to.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ConfigurationError(
            f"cannot write to {directory}: {exc.strerror}. Make sure the volume "
            f"is writable by the user this runs as (uid {os.getuid()})."
        ) from exc
    except OSError as exc:
        raise ConfigurationError(f"cannot create {directory}: {exc}") from exc
    if not os.access(directory, os.W_OK):
        raise ConfigurationError(
            f"{directory} is not writable by the user this runs as (uid {os.getuid()})"
        )


def _classify(entry: Path) -> BlobBackup | None:
    """Interpret a directory entry as a blob backup, if it is one.

    :param entry: path to inspect.
    :returns: the backup it represents, or ``None`` if the name does not match.
    """
    match = BACKUP_PATTERN.match(entry.name)
    if match is None:
        return None
    suffix = match.group("suffix")
    if suffix is None and not entry.is_dir():
        return None
    if suffix is not None and not entry.is_file():
        return None
    return BlobBackup(
        stamp=parse_stamp(match.group("stamp")),
        path=entry,
        archive=suffix is not None,
    )


def find_backups(location: Path) -> list[BlobBackup]:
    """List the blob backups in a backup location, oldest first.

    The ``latest`` symlink is skipped: it is an alias for a backup that is
    already in the list, and counting it twice would corrupt retention.

    :param location: directory holding the blob backups.
    :returns: the backups sorted chronologically; empty if the directory is
        absent.
    """
    if not location.is_dir():
        return []
    found = []
    for entry in location.iterdir():
        if entry.name == LATEST_LINK or entry.is_symlink():
            continue
        backup = _classify(entry)
        if backup is not None:
            found.append(backup)
    return sorted(found)


def find_backup_at_or_before(location: Path, stamp: datetime) -> BlobBackup | None:
    """Find the newest blob backup that is not newer than a moment.

    This is the pairing rule used at restore time: the blobs restored alongside a
    filestorage state must not be newer than that state.

    :param location: directory holding the blob backups.
    :param stamp: the moment to match against.
    :returns: the matching backup, or ``None`` if every backup is newer.
    """
    candidates = [b for b in find_backups(location) if b.stamp <= stamp]
    return candidates[-1] if candidates else None


def update_latest_symlink(location: Path, target: Path | None) -> None:
    """Point the ``latest`` symlink at a backup, atomically.

    The link is created under a temporary name and renamed into place, so a
    concurrent reader sees either the old target or the new one, never a missing
    link.

    :param location: directory holding the blob backups.
    :param target: backup to point at; ``None`` removes the link.
    """
    link = location / LATEST_LINK
    if target is None:
        if link.is_symlink():
            link.unlink()
            logger.debug("removed %s", link)
        return

    temporary = location / f".{LATEST_LINK}.{os.getpid()}.tmp"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(target.name)
    # Renaming is atomic and acts on the symlink itself, not on its target.
    temporary.replace(link)
    logger.debug("%s -> %s", link, target.name)


def _rsync_tree(
    *,
    source: Path,
    destination: Path,
    previous: Path | None,
    options: tuple[str, ...],
) -> None:
    """Copy a blob tree with rsync, hard-linking against the previous backup.

    :param source: the blobstorage to copy. Passed without a trailing slash so
        rsync recreates the directory itself inside ``destination``.
    :param destination: the timestamped backup directory.
    :param previous: the preceding backup directory to hard-link against, if any.
    :param options: extra rsync arguments.
    :raises CommandError: if rsync fails.
    """
    rsync = require("rsync")
    argv: list[str | Path] = [rsync, "-a", *options]
    if previous is not None:
        # --link-dest is resolved relative to the destination directory.
        argv += ["--delete", f"--link-dest={os.path.relpath(previous, destination)}"]
    argv += [source, destination]
    run(argv)


def _copy_tree(*, source: Path, destination: Path) -> None:
    """Copy a blob tree with :func:`shutil.copytree`.

    The fallback for systems without rsync. It cannot hard-link, so every backup
    is a full copy.

    :param source: the blobstorage to copy.
    :param destination: the timestamped backup directory.
    """
    shutil.copytree(source, destination / source.name, symlinks=True)


def _write_archive(*, source: Path, target: Path, compress: bool) -> None:
    """Write a blob tree into a tar archive.

    Python's :mod:`tarfile` is used rather than the ``tar`` binary: there is no
    quoting to get wrong, and the GNU-specific features the original recipe
    needed (``--listed-incremental``) belong to the incremental-blob mode this
    project does not implement.

    :param source: the blobstorage to archive.
    :param target: archive path to write.
    :param compress: whether to gzip the archive.
    """
    mode = "w:gz" if compress else "w"
    with tarfile.open(target, mode) as archive:  # type: ignore[call-overload]
        archive.add(source, arcname=source.name)


def backup(
    *,
    source: Path,
    location: Path,
    stamp: datetime,
    archive: bool = False,
    compress: bool = False,
    use_rsync: bool = True,
    rsync_options: tuple[str, ...] = (),
) -> BlobBackup:
    """Back up a blobstorage.

    The source is only ever read, so it may be mounted read-only.

    :param source: the blobstorage directory to back up.
    :param location: directory to write the backup into; created if missing.
    :param stamp: timestamp to name the backup after. Must be the stamp of the
        filestorage backup made in the same run, so the two stay paired.
    :param archive: write a tar archive instead of a directory tree.
    :param compress: gzip the archive; only meaningful with ``archive``.
    :param use_rsync: use rsync, which hard-links unchanged blobs against the
        previous backup. Without it every backup is a full copy.
    :param rsync_options: extra arguments passed through to rsync.
    :returns: the backup that was created.
    :raises BackupError: if the source is missing or a backup already exists at
        this timestamp.
    """
    if not source.is_dir():
        raise BackupError(f"blobstorage {source} does not exist or is not a directory")
    _ensure_writable(location)

    base = f"{source.name}.{format_stamp(stamp)}"
    if archive:
        target = location / f"{base}.tar.gz" if compress else location / f"{base}.tar"
    else:
        target = location / base

    if target.exists():
        raise BackupError(f"a blob backup already exists at {target}")

    if archive:
        _write_archive(source=source, target=target, compress=compress)
        logger.info("wrote blob archive %s", target.name)
        return BlobBackup(stamp=stamp, path=target, archive=True)

    previous = find_backups(location)
    previous_tree = next(
        (b.path for b in reversed(previous) if not b.archive),
        None,
    )
    target.mkdir()
    if use_rsync:
        _rsync_tree(
            source=source,
            destination=target,
            previous=previous_tree,
            options=rsync_options,
        )
    else:
        _copy_tree(source=source, destination=target)

    update_latest_symlink(location, target)
    logger.info("wrote blob backup %s", target.name)
    return BlobBackup(stamp=stamp, path=target, archive=False)


def _replace_tree(*, staged: Path, destination: Path) -> None:
    """Swap a freshly built tree into place, replacing what is there.

    :param staged: the tree to install.
    :param destination: where it should end up.
    """
    if destination.exists():
        shutil.rmtree(destination)
    shutil.move(str(staged), str(destination))


def _restore_archive(*, backup: BlobBackup, destination: Path) -> None:
    """Restore a blobstorage from a tar archive.

    The archive is unpacked into a temporary directory beside the destination
    first, so a failure part-way through cannot leave a half-restored
    blobstorage behind.

    :param backup: the archive to restore.
    :param destination: the blobstorage directory to write.
    :raises RestoreError: if the archive does not hold exactly one tree.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        holding = Path(scratch)
        with tarfile.open(backup.path) as archive:
            archive.extractall(holding, filter="data")
        entries = list(holding.iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            raise RestoreError(
                f"{backup.path} does not contain a single blobstorage directory"
            )
        _replace_tree(staged=entries[0], destination=destination)


def _restore_tree(
    *,
    backup: BlobBackup,
    destination: Path,
    use_rsync: bool,
    rsync_options: tuple[str, ...],
) -> None:
    """Restore a blobstorage from a directory backup.

    :param backup: the tree backup to restore.
    :param destination: the blobstorage directory to write.
    :param use_rsync: use rsync rather than a plain copy.
    :param rsync_options: extra arguments passed through to rsync.
    :raises RestoreError: if the backup does not hold exactly one tree.
    """
    entries = [entry for entry in backup.path.iterdir() if entry.is_dir()]
    if len(entries) != 1:
        raise RestoreError(
            f"{backup.path} does not contain a single blobstorage directory"
        )
    stored = entries[0]

    if use_rsync:
        rsync = require("rsync")
        destination.mkdir(parents=True, exist_ok=True)
        # Trailing separators make rsync copy the *contents* of the tree.
        run(
            [
                rsync,
                "-a",
                "--delete",
                *rsync_options,
                f"{stored}{os.sep}",
                f"{destination}{os.sep}",
            ]
        )
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        staged = Path(scratch) / destination.name
        shutil.copytree(stored, staged, symlinks=True)
        _replace_tree(staged=staged, destination=destination)


def restore(
    *,
    backup: BlobBackup,
    destination: Path,
    use_rsync: bool = True,
    rsync_options: tuple[str, ...] = (),
) -> None:
    """Restore a blobstorage from a blob backup.

    The destination is replaced wholesale: blobs present now but absent from the
    backup are removed, so the result matches the backup exactly rather than
    being a merge of the two.

    :param backup: the backup to restore, from :func:`find_backup_at_or_before`.
    :param destination: the blobstorage directory to write.
    :param use_rsync: use rsync rather than a plain copy for tree backups.
    :param rsync_options: extra arguments passed through to rsync.
    :raises RestoreError: if the backup is missing or malformed.
    """
    if not backup.path.exists():
        raise RestoreError(f"blob backup {backup.path} does not exist")
    if backup.archive:
        _restore_archive(backup=backup, destination=destination)
    else:
        _restore_tree(
            backup=backup,
            destination=destination,
            use_rsync=use_rsync,
            rsync_options=rsync_options,
        )
    logger.info("restored blobs from %s to %s", backup.name, destination)
