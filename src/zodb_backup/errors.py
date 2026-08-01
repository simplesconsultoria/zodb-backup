"""Exception hierarchy and process exit codes.

Exit codes are part of the tool's contract with cron/swarm monitoring:
``0`` ok, ``1`` operational failure, ``2`` configuration error.
"""


class ZODBBackupError(Exception):
    """Base class for every error raised by :mod:`zodb_backup`.

    :cvar exit_code: process exit status used when this error reaches the CLI.
    """

    exit_code: int = 1


class ConfigurationError(ZODBBackupError):
    """Invalid or contradictory configuration (bad env var, impossible combination)."""

    exit_code: int = 2


class BackupError(ZODBBackupError):
    """A backup operation failed."""


class RestoreError(ZODBBackupError):
    """A restore operation failed."""


class CommandError(ZODBBackupError):
    """An external command (``rsync``, ``tar``, a hook) exited non-zero."""
