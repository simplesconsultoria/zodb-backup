"""Backup timestamps: parsing, formatting and collision-free allocation.

``repozo`` names every backup file after the UTC second in which it ran, e.g.
``2026-03-01-12-00-00.fsz`` for a full backup and ``….deltafsz`` for an
incremental. Restores rely on those names sorting chronologically.

That scheme has a sharp edge. :func:`ZODB.scripts.repozo.find_files` reverse-sorts
the repository listing and stops at the first full backup it meets. When a full
backup and an incremental share a second, ``"….fsz"`` sorts *after*
``"….deltafsz"`` (because ``"f" > "d"``), so the full backup is seen first, the
loop stops, and **the incremental is silently dropped from every later restore**.
The restore then reports success while missing data.

This module removes that failure mode at the source: instead of letting repozo
stamp a backup with the wall clock, we allocate the stamp ourselves via
:func:`next_stamp`, guaranteeing it is strictly newer than every backup already
in the repository. A full backup and its incrementals can then never collide, and
the blob backup made in the same run can reuse the identical stamp without having
to scan the repository afterwards.
"""

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import re


#: Filename pattern repozo uses for data files, capturing the timestamp.
DATA_FILE_PATTERN = re.compile(r"^(\d{4}(?:-\d\d){5})\.(delta)?fsz?$")

#: ``strftime`` format matching repozo's filenames and its ``--date`` argument.
STAMP_FORMAT = "%Y-%m-%d-%H-%M-%S"

#: Extensions repozo gives to full backups, in both plain and gzipped form.
FULL_EXTENSIONS = frozenset({".fs", ".fsz"})


def format_stamp(moment: datetime) -> str:
    """Render a moment the way repozo names its files.

    :param moment: an aware datetime; converted to UTC first.
    :returns: a string such as ``2026-03-01-12-00-00``.
    """
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


def parse_stamp(text: str) -> datetime:
    """Parse a repozo timestamp string into an aware UTC datetime.

    :param text: a stamp such as ``2026-03-01-12-00-00``.
    :returns: the corresponding timezone-aware datetime in UTC.
    :raises ValueError: if the text is not a valid stamp.
    """
    return datetime.strptime(text, STAMP_FORMAT).replace(tzinfo=UTC)


def as_repozo_date(moment: datetime) -> tuple[int, int, int, int, int, int]:
    """Convert a moment to the 6-tuple repozo uses to build a filename.

    :param moment: an aware datetime; converted to UTC first.
    :returns: ``(year, month, day, hour, minute, second)`` in UTC.
    """
    utc = moment.astimezone(UTC)
    return (utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second)


@dataclass(frozen=True, slots=True, order=True)
class BackupFile:
    """One data file in a repozo repository.

    Ordering is by ``stamp`` first, so a sorted sequence is chronological.
    """

    stamp: datetime
    full: bool
    path: Path

    @property
    def name(self) -> str:
        """The file's base name."""
        return self.path.name


def find_backup_files(repository: Path) -> list[BackupFile]:
    """List the repozo data files in a repository, oldest first.

    Non-data files (``.dat``, ``.index``, blob archives, stray files) are
    ignored, matching repozo's own filter.

    :param repository: directory holding the backups.
    :returns: the data files sorted chronologically; empty if the directory does
        not exist.
    """
    if not repository.is_dir():
        return []
    found = []
    for entry in repository.iterdir():
        match = DATA_FILE_PATTERN.match(entry.name)
        if match is None:
            continue
        found.append(
            BackupFile(
                stamp=parse_stamp(match.group(1)),
                full=entry.suffix in FULL_EXTENSIONS,
                path=entry,
            )
        )
    return sorted(found)


def latest_stamp(repository: Path) -> datetime | None:
    """Return the newest backup timestamp in a repository.

    :param repository: directory holding the backups.
    :returns: the newest stamp, or ``None`` when the repository has no backups.
    """
    files = find_backup_files(repository)
    return files[-1].stamp if files else None


def next_stamp(repository: Path, now: datetime | None = None) -> datetime:
    """Allocate a timestamp that cannot collide with an existing backup.

    The result is the current UTC second, or one second past the newest existing
    backup when that would not already be in the past. Strict monotonicity is
    what makes the silent-incremental-loss failure described in the module
    docstring impossible.

    :param repository: directory holding the backups.
    :param now: current time, for tests; defaults to the real UTC clock.
    :returns: an aware UTC datetime truncated to the second.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    newest = latest_stamp(repository)
    if newest is not None and moment <= newest:
        return newest + timedelta(seconds=1)
    return moment
