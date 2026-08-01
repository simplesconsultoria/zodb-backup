"""Tests for the CLI surface.

These cover argument handling and dispatch. The behaviour behind each command is
tested against :mod:`zodb_backup.operations` directly, without Typer.
"""

from pathlib import Path
from typer.testing import CliRunner
from zodb_backup import __version__
from zodb_backup.cli import app

import pytest


runner = CliRunner()

#: Every command the tool documents, besides ``version``.
COMMANDS = [
    "backup",
    "snapshot",
    "restore",
    "snapshot-restore",
    "list",
    "verify",
]


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_version_command_reports_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_commands_are_wired_to_real_operations(command: str) -> None:
    """No command may be a stub.

    A stub that returned quietly would be recorded by cron as a successful
    backup that never happened, so reaching an unimplemented code path is a
    failure in its own right.
    """
    result = runner.invoke(app, [command])

    assert not isinstance(result.exception, NotImplementedError)


class TestDispatch:
    """The CLI resolves configuration and hands off; nothing more."""

    def test_backup_runs_against_configured_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datafs = tmp_path / "filestorage" / "Data.fs"
        datafs.parent.mkdir()
        datafs.write_bytes(b"not really a filestorage")
        monkeypatch.setenv("DATAFS", str(datafs))
        monkeypatch.setenv("BACKUP_LOCATION", str(tmp_path / "backups"))
        monkeypatch.setenv("BLOBSTORAGE", "")

        result = runner.invoke(app, ["backup"])

        # The paths were honoured: the backup got far enough to read the file
        # and reject its contents, rather than failing on configuration.
        assert result.exit_code != 0
        assert str(datafs) in str(result.exception)

    def test_list_reports_an_empty_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BACKUP_LOCATION", str(tmp_path / "backups"))
        monkeypatch.setenv("BLOBSTORAGE", "")

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "no backups found" in result.output

    def test_bad_date_is_a_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BACKUP_LOCATION", str(tmp_path / "backups"))
        monkeypatch.setenv("BLOBSTORAGE", "")
        monkeypatch.setenv("ASSUME_YES", "true")

        result = runner.invoke(app, ["restore", "not-a-date"])

        assert result.exit_code != 0
