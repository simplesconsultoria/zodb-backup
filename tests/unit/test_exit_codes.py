"""Tests for the process exit-code contract.

Cron and swarm monitoring key off these codes, so they are pinned by tests:
``0`` ok, ``1`` operational failure, ``2`` configuration error.
"""

from collections.abc import Iterator
from zodb_backup import cli
from zodb_backup.errors import BackupError
from zodb_backup.errors import CommandError
from zodb_backup.errors import ConfigurationError
from zodb_backup.errors import RestoreError
from zodb_backup.errors import ZODBBackupError

import pytest


@pytest.fixture
def no_argv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run ``main()`` with a bare argv so Typer does not see pytest's arguments."""
    monkeypatch.setattr("sys.argv", ["zodb-backup"])
    yield


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ZODBBackupError, 1),
        (BackupError, 1),
        (RestoreError, 1),
        (CommandError, 1),
        (ConfigurationError, 2),
    ],
)
def test_error_classes_carry_their_exit_code(
    error: type[ZODBBackupError], expected: int
) -> None:
    assert error.exit_code == expected


def test_configuration_errors_exit_two(
    monkeypatch: pytest.MonkeyPatch, no_argv: None
) -> None:
    def boom() -> None:
        raise ConfigurationError("KEEP='x' is not an integer")

    monkeypatch.setattr(cli, "app", boom)
    assert cli.main() == 2


def test_operational_errors_exit_one(
    monkeypatch: pytest.MonkeyPatch, no_argv: None
) -> None:
    def boom() -> None:
        raise BackupError("repozo failed")

    monkeypatch.setattr(cli, "app", boom)
    assert cli.main() == 1


def test_success_exits_zero(monkeypatch: pytest.MonkeyPatch, no_argv: None) -> None:
    monkeypatch.setattr(cli, "app", lambda: None)
    assert cli.main() == 0


def test_bad_environment_variable_exits_two(
    monkeypatch: pytest.MonkeyPatch, no_argv: None
) -> None:
    """End-to-end: a typo'd boolean in the environment must exit 2, not 1."""
    monkeypatch.setenv("BACKUP_BLOBS", "maybe")
    monkeypatch.setattr("sys.argv", ["zodb-backup", "backup"])

    assert cli.main() == 2
