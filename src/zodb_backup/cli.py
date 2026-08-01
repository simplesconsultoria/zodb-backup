"""Typer entry point.

This module only parses arguments, resolves :class:`~zodb_backup.config.Settings`
and dispatches. All logic lives in the operation modules, which take a
``Settings`` instance and are testable without Typer.

Current status: the command surface and configuration resolution are real; the
backup and restore operations are not implemented yet and raise
:class:`NotImplementedError` rather than exiting successfully.
"""

from typing import Annotated
from typing import Any
from zodb_backup import __version__
from zodb_backup import operations
from zodb_backup.config import Settings
from zodb_backup.errors import ZODBBackupError

import logging
import sys
import typer


logger = logging.getLogger("zodb_backup")

app = typer.Typer(
    name="zodb-backup",
    help="Backup and restore ZODB FileStorage and blobstorage, for containers.",
    no_args_is_help=True,
    add_completion=False,
)

FullOption = Annotated[
    bool | None, typer.Option("--full/--no-full", help="Force a full backup.")
]
QuickOption = Annotated[
    bool | None,
    typer.Option("--quick/--no-quick", help="Use repozo --quick (default: on)."),
]
KeepOption = Annotated[
    int | None,
    typer.Option("--keep", help="Number of full backups to keep; 0 keeps all."),
]
YesOption = Annotated[
    bool | None,
    typer.Option("--yes", help="Assume 'yes' for the restore confirmation."),
]


def configure_logging(settings: Settings) -> None:
    """Set up stdout logging according to ``DEBUG`` / ``QUIET``.

    Logging goes to the standard streams only; there are no log files.

    :param settings: resolved settings for this run.
    """
    if settings.quiet:
        level = logging.WARNING
    elif settings.debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def build_settings(**overrides: Any) -> Settings:
    """Resolve settings for a command and configure logging.

    :param overrides: CLI flag values keyed by :class:`Settings` field name;
        ``None`` entries fall through to the environment.
    :returns: the resolved settings.
    """
    settings = Settings.from_env(overrides=overrides)
    configure_logging(settings)
    return settings


def _report(result: operations.RunResult) -> None:
    """Print a one-line summary of a completed run.

    :param result: what the run produced.
    """
    if result.filestorage.changed and result.filestorage.path is not None:
        kind = "full" if result.filestorage.full else "incremental"
        typer.echo(f"filestorage: {kind} backup {result.filestorage.path.name}")
    else:
        typer.echo("filestorage: unchanged, nothing written")
    if result.blobs is not None:
        typer.echo(f"blobs:       {result.blobs.name}")
    removed = len(result.removed.filestorage) + len(result.removed.blobs)
    if removed:
        typer.echo(f"retention:   removed {removed} obsolete file(s)")


@app.command()
def backup(
    full: FullOption = None, quick: QuickOption = None, keep: KeepOption = None
) -> None:
    """Make an incremental backup, or a full one with --full / FULL=true."""
    settings = build_settings(full=full, quick=quick, keep=keep)
    _report(operations.backup(settings))


@app.command()
def snapshot(quick: QuickOption = None, keep: KeepOption = None) -> None:
    """Make a full backup into the snapshot locations."""
    settings = build_settings(quick=quick, keep=keep)
    _report(operations.snapshot(settings))


@app.command()
def restore(date: str | None = None, yes: YesOption = None) -> None:
    """Restore the latest backup, or the state at DATE (UTC, repozo semantics).

    :param date: target state as yyyy-mm-dd[-hh[-mm[-ss]]]; latest when omitted.
    :param yes: skip the interactive confirmation.
    """
    settings = build_settings(assume_yes=yes)
    operations.restore(settings, date)
    typer.echo("restore complete")


@app.command(name="snapshot-restore")
def snapshot_restore(date: str | None = None, yes: YesOption = None) -> None:
    """Restore from the snapshot locations.

    :param date: target state as yyyy-mm-dd[-hh[-mm[-ss]]]; latest when omitted.
    :param yes: skip the interactive confirmation.
    """
    settings = build_settings(assume_yes=yes)
    operations.restore(settings, date, snapshot=True)
    typer.echo("restore complete")


@app.command(name="list")
def list_backups() -> None:
    """List available backups with their timestamps and sizes."""
    settings = build_settings()
    lines = operations.list_backups(settings)
    if not lines:
        typer.echo("no backups found")
        return
    for line in lines:
        typer.echo(line)


@app.command()
def verify() -> None:
    """Run a repozo verify pass against the backup repository."""
    settings = build_settings()
    operations.verify(settings)
    typer.echo("repository verified")


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def main() -> int:
    """Console-script wrapper translating exceptions into exit codes.

    :returns: ``0`` on success, ``1`` on operational failure, ``2`` on
        configuration error.
    """
    try:
        app()
    except ZODBBackupError as exc:
        logger.error("%s", exc)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
