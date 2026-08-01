"""Tests for environment variable parsing and settings validation."""

from pathlib import Path
from zodb_backup.config import DEFAULT_BACKUP_LOCATION
from zodb_backup.config import DEFAULT_DATAFS
from zodb_backup.config import Settings
from zodb_backup.config import parse_bool
from zodb_backup.config import parse_int
from zodb_backup.errors import ConfigurationError

import pytest


class TestParseBool:
    @pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "yes", "ON", " on "])
    def test_accepts_true_values(self, raw: str) -> None:
        assert parse_bool(raw, name="FLAG") is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "OFF", " off "])
    def test_accepts_false_values(self, raw: str) -> None:
        assert parse_bool(raw, name="FLAG") is False

    @pytest.mark.parametrize("raw", ["", "maybe", "tru", "2", "y", "n"])
    def test_rejects_anything_else(self, raw: str) -> None:
        """A typo must not silently become False."""
        with pytest.raises(ConfigurationError, match="FLAG"):
            parse_bool(raw, name="FLAG")


class TestParseInt:
    def test_parses_and_strips(self) -> None:
        assert parse_int(" 5 ", name="KEEP") == 5

    @pytest.mark.parametrize("raw", ["two", "", "1.5"])
    def test_rejects_non_integers(self, raw: str) -> None:
        with pytest.raises(ConfigurationError, match="KEEP"):
            parse_int(raw, name="KEEP")

    def test_rejects_below_minimum(self) -> None:
        with pytest.raises(ConfigurationError, match=">= 0"):
            parse_int("-1", name="KEEP")


class TestDefaults:
    def test_empty_environment_yields_documented_defaults(
        self, clean_env: dict[str, str]
    ) -> None:
        """Defaults must match the table documented in the README."""
        settings = Settings.from_env(clean_env)

        assert settings.datafs == Path(DEFAULT_DATAFS)
        assert settings.backup_location == Path(DEFAULT_BACKUP_LOCATION)
        assert settings.full is False
        assert settings.quick is True
        assert settings.gzip is True
        assert settings.keep == 2
        assert settings.backup_blobs is True
        assert settings.only_blobs is False
        assert settings.archive_blob is False
        assert settings.compress_blob is False
        assert settings.use_rsync is True
        assert settings.rsync_options == ()
        assert settings.pre_command is None
        assert settings.post_command is None
        assert settings.assume_yes is False
        assert settings.debug is False
        assert settings.quiet is False

    def test_settings_are_frozen(self, clean_env: dict[str, str]) -> None:
        settings = Settings.from_env(clean_env)
        with pytest.raises(AttributeError):
            settings.keep = 9  # type: ignore[misc]


class TestEnvironmentReading:
    def test_reads_paths_and_flags(self, clean_env: dict[str, str]) -> None:
        clean_env.update(
            {
                "DATAFS": "/srv/var/filestorage/Data.fs",
                "BACKUP_LOCATION": "/mnt/backups/fs",
                "FULL": "yes",
                "QUICK": "off",
                "KEEP": "7",
            }
        )
        settings = Settings.from_env(clean_env)

        assert settings.datafs == Path("/srv/var/filestorage/Data.fs")
        assert settings.backup_location == Path("/mnt/backups/fs")
        assert settings.full is True
        assert settings.quick is False
        assert settings.keep == 7

    def test_keep_zero_is_valid(self, clean_env: dict[str, str]) -> None:
        """``KEEP=0`` means 'keep everything', not 'keep nothing'."""
        clean_env["KEEP"] = "0"
        assert Settings.from_env(clean_env).keep == 0

    def test_rsync_options_are_shell_split(self, clean_env: dict[str, str]) -> None:
        clean_env["RSYNC_OPTIONS"] = "--exclude '*.tmp' --bwlimit=1000"
        settings = Settings.from_env(clean_env)
        assert settings.rsync_options == ("--exclude", "*.tmp", "--bwlimit=1000")

    def test_empty_blobstorage_means_filestorage_only(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env["BLOBSTORAGE"] = ""
        settings = Settings.from_env(clean_env)

        assert settings.blobstorage is None
        assert settings.blobs_enabled is False

    def test_blobs_enabled_requires_both_path_and_flag(
        self, clean_env: dict[str, str]
    ) -> None:
        assert Settings.from_env(clean_env).blobs_enabled is True

        clean_env["BACKUP_BLOBS"] = "false"
        assert Settings.from_env(clean_env).blobs_enabled is False

    def test_empty_hook_commands_become_none(self, clean_env: dict[str, str]) -> None:
        clean_env.update({"PRE_COMMAND": "", "POST_COMMAND": "echo done"})
        settings = Settings.from_env(clean_env)

        assert settings.pre_command is None
        assert settings.post_command == "echo done"

    def test_bad_boolean_is_a_configuration_error(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env["BACKUP_BLOBS"] = "maybe"
        with pytest.raises(ConfigurationError) as excinfo:
            Settings.from_env(clean_env)

        assert excinfo.value.exit_code == 2
        assert "BACKUP_BLOBS" in str(excinfo.value)


class TestPrecedence:
    def test_cli_override_beats_environment(self, clean_env: dict[str, str]) -> None:
        clean_env["KEEP"] = "3"
        settings = Settings.from_env(clean_env, overrides={"keep": 10})
        assert settings.keep == 10

    def test_none_override_falls_through_to_environment(
        self, clean_env: dict[str, str]
    ) -> None:
        """An unset CLI flag arrives as ``None`` and must not clobber the env."""
        clean_env["KEEP"] = "3"
        settings = Settings.from_env(clean_env, overrides={"keep": None})
        assert settings.keep == 3

    def test_none_override_falls_through_to_default(
        self, clean_env: dict[str, str]
    ) -> None:
        settings = Settings.from_env(clean_env, overrides={"full": None})
        assert settings.full is False

    def test_unknown_override_is_rejected(self, clean_env: dict[str, str]) -> None:
        with pytest.raises(ConfigurationError, match="unknown setting"):
            Settings.from_env(clean_env, overrides={"kepe": 1})


class TestValidation:
    def test_only_blobs_without_backup_blobs_is_rejected(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env.update({"ONLY_BLOBS": "true", "BACKUP_BLOBS": "false"})
        with pytest.raises(ConfigurationError, match="ONLY_BLOBS"):
            Settings.from_env(clean_env)

    def test_only_blobs_without_blobstorage_is_rejected(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env.update({"ONLY_BLOBS": "true", "BLOBSTORAGE": ""})
        with pytest.raises(ConfigurationError, match="BLOBSTORAGE"):
            Settings.from_env(clean_env)

    def test_compress_blob_requires_archive_blob(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env["COMPRESS_BLOB"] = "true"
        with pytest.raises(ConfigurationError, match="ARCHIVE_BLOB"):
            Settings.from_env(clean_env)

    def test_compress_blob_with_archive_blob_is_accepted(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env.update({"COMPRESS_BLOB": "true", "ARCHIVE_BLOB": "true"})
        settings = Settings.from_env(clean_env)

        assert settings.compress_blob is True
        assert settings.archive_blob is True

    def test_debug_and_quiet_are_mutually_exclusive(
        self, clean_env: dict[str, str]
    ) -> None:
        clean_env.update({"DEBUG": "true", "QUIET": "true"})
        with pytest.raises(ConfigurationError, match="mutually exclusive"):
            Settings.from_env(clean_env)
