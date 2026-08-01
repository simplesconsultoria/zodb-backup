"""Filestorage backup and restore, driving ZODB's ``repozo`` in process.

``repozo`` ships both a console script and an importable module. We use the
module, but deliberately **not** its :func:`~ZODB.scripts.repozo.main`: that
function catches ``RepozoError`` and ``OSError`` and re-raises them as
``sys.exit(str(e))``, which would cost us the exception type and turn every
failure into an indistinguishable exit status. The layer beneath it —
``parseargs`` plus ``do_backup`` / ``do_recover`` / ``do_verify`` — raises typed
exceptions and is what this module calls.

Two pieces of repozo state need care:

* ``repozo.VERBOSE`` is a module global that ``parseargs`` only ever sets to
  ``True``. A single verbose run would otherwise leave every later run in the
  same process verbose, so :func:`_repozo_session` pins and restores it.
* ``options.test_now`` overrides the timestamp repozo puts in the filename. We
  always set it, from :mod:`zodb_backup.timestamps`, so backup names are
  strictly monotonic; see that module for why this matters.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from ZODB.POSException import POSError
from ZODB.scripts import repozo
from zodb_backup.errors import BackupError
from zodb_backup.errors import ConfigurationError
from zodb_backup.errors import RestoreError
from zodb_backup.timestamps import as_repozo_date
from zodb_backup.timestamps import format_stamp

import logging
import os


logger = logging.getLogger("zodb_backup.repozo")

#: Failures that mean "the operation did not work", as opposed to a bug in this
#: package. ``POSError`` covers storage-level problems such as a truncated or
#: corrupt ``Data.fs``, which repozo lets through unwrapped.
_FAILURES = (repozo.RepozoError, POSError, OSError)

#: Extensions repozo may give the data file produced by a single backup run.
_RESULT_EXTENSIONS = (".fs", ".fsz", ".deltafs", ".deltafsz")


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Outcome of one filestorage backup run.

    :ivar stamp: the timestamp the backup was named after. A blob backup made in
        the same run must reuse this exact value.
    :ivar path: the data file repozo wrote, or ``None`` when the filestorage was
        unchanged and repozo had nothing to do.
    :ivar full: whether the run produced a full backup. Repozo may promote a
        requested incremental to a full backup, so this reports what actually
        happened, not what was asked for.
    """

    stamp: datetime
    path: Path | None
    full: bool

    @property
    def changed(self) -> bool:
        """Whether this run actually wrote a backup file."""
        return self.path is not None


@contextmanager
def _repozo_session(verbose: bool) -> Iterator[None]:
    """Run repozo with a pinned verbosity, restoring the global afterwards.

    :param verbose: whether repozo should log its progress.
    """
    previous = repozo.VERBOSE
    try:
        repozo.VERBOSE = verbose
        yield
    finally:
        repozo.VERBOSE = previous


def _parse(argv: list[str], verbose: bool) -> Any:
    """Build a repozo options object from an argument list.

    :param argv: repozo arguments, without the program name.
    :param verbose: desired verbosity, applied after parsing because
        ``parseargs`` sets the global itself.
    :returns: repozo's options object.
    """
    logger.debug("repozo %s", " ".join(argv))
    options = repozo.parseargs(argv)
    repozo.VERBOSE = verbose
    return options


def _ensure_writable(directory: Path) -> None:
    """Create a backup directory, reporting permission problems clearly.

    A container running as a non-root user against a freshly created Docker
    volume, which the daemon makes root-owned, fails here. That is a
    configuration mistake with a known fix, so it deserves a message saying so
    rather than a traceback.

    :param directory: the directory to create.
    :raises ConfigurationError: if the directory cannot be created or written to.
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


def _result_path(repository: Path, stamp: datetime) -> Path | None:
    """Find the data file a run produced, identified by its timestamp.

    Because we allocate the stamp ourselves, exactly one data file can carry it.

    :param repository: directory holding the backups.
    :param stamp: the timestamp used for this run.
    :returns: the data file, or ``None`` if the run wrote nothing.
    """
    base = format_stamp(stamp)
    for extension in _RESULT_EXTENSIONS:
        candidate = repository / f"{base}{extension}"
        if candidate.exists():
            return candidate
    return None


def backup(
    *,
    datafs: Path,
    repository: Path,
    stamp: datetime,
    full: bool = False,
    quick: bool = True,
    gzip: bool = True,
    verbose: bool = False,
) -> BackupResult:
    """Back up a filestorage into a repozo repository.

    :param datafs: the ``Data.fs`` to read. Opened read-only by repozo, so the
        source may be mounted read-only.
    :param repository: directory to write the backup into; created if missing.
    :param stamp: timestamp to name the backup after. Allocate it with
        :func:`zodb_backup.timestamps.next_stamp` so it cannot collide.
    :param full: force a full backup instead of an incremental one.
    :param quick: use repozo's ``--quick`` mode, which trusts the recorded
        checksums instead of re-reading the whole database.
    :param gzip: compress the backup.
    :param verbose: let repozo log its progress.
    :returns: what the run produced.
    :raises BackupError: if repozo fails.
    """
    _ensure_writable(repository)
    argv = ["--backup", "--file", str(datafs), "--repository", str(repository)]
    if full:
        argv.append("--full")
    if quick:
        argv.append("--quick")
    if gzip:
        argv.append("--gzip")

    with _repozo_session(verbose):
        options = _parse(argv, verbose)
        options.test_now = as_repozo_date(stamp)
        try:
            repozo.do_backup(options)
        except _FAILURES as exc:
            raise BackupError(f"repozo backup failed: {exc}") from exc

    path = _result_path(repository, stamp)
    if path is None:
        logger.info("filestorage unchanged; no backup file written")
        return BackupResult(stamp=stamp, path=None, full=False)
    logger.info("wrote %s", path.name)
    return BackupResult(stamp=stamp, path=path, full=path.suffix in (".fs", ".fsz"))


def recover(
    *,
    repository: Path,
    output: Path,
    date: str | None = None,
    with_verify: bool = False,
    verbose: bool = False,
) -> None:
    """Restore a filestorage from a repozo repository.

    :param repository: directory holding the backups.
    :param output: path to write the recovered ``Data.fs`` to. Repozo deletes an
        existing file at this path before writing.
    :param date: restore the state as of this UTC stamp
        (``yyyy-mm-dd[-hh[-mm[-ss]]]``); the latest state when ``None``.
    :param with_verify: check each file against the recorded checksums while
        restoring.
    :param verbose: let repozo log its progress.
    :raises RestoreError: if repozo fails or the repository has no usable backup.
    """
    _ensure_writable(output.parent)
    argv = ["--recover", "--repository", str(repository), "--output", str(output)]
    if date:
        argv.extend(["--date", date])
    if with_verify:
        argv.append("--with-verify")

    with _repozo_session(verbose):
        options = _parse(argv, verbose)
        try:
            repozo.do_recover(options)
        except _FAILURES as exc:
            raise RestoreError(f"repozo recover failed: {exc}") from exc
    logger.info("restored %s", output)


def verify(*, repository: Path, verbose: bool = False) -> None:
    """Check a repozo repository against its recorded checksums.

    :param repository: directory holding the backups.
    :param verbose: let repozo log its progress.
    :raises BackupError: if any file fails verification.
    """
    with _repozo_session(verbose):
        options = _parse(["--verify", "--repository", str(repository)], verbose)
        try:
            repozo.do_verify(options)
        except _FAILURES as exc:
            raise BackupError(f"repozo verify failed: {exc}") from exc
    logger.info("repository %s verified", repository)
