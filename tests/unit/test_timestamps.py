"""Tests for timestamp parsing, discovery and collision-free allocation."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from zodb_backup.timestamps import BackupFile
from zodb_backup.timestamps import as_repozo_date
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import format_stamp
from zodb_backup.timestamps import latest_stamp
from zodb_backup.timestamps import next_stamp
from zodb_backup.timestamps import parse_stamp

import pytest


MOMENT = datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)
STAMP = "2026-03-01-12-30-45"


def touch(repository: Path, *names: str) -> None:
    """Create empty files in a repository.

    :param repository: directory to create the files in.
    :param names: file names to create.
    """
    for name in names:
        (repository / name).touch()


class TestFormatting:
    def test_format_matches_repozo_naming(self) -> None:
        assert format_stamp(MOMENT) == STAMP

    def test_parse_round_trips(self) -> None:
        assert parse_stamp(STAMP) == MOMENT

    def test_parsed_stamps_are_timezone_aware(self) -> None:
        assert parse_stamp(STAMP).tzinfo is UTC

    def test_non_utc_input_is_converted(self) -> None:
        """repozo names files in UTC, so a local-time input must be converted."""
        in_sao_paulo = MOMENT.astimezone(timezone(timedelta(hours=-3)))

        assert format_stamp(in_sao_paulo) == STAMP
        assert as_repozo_date(in_sao_paulo) == (2026, 3, 1, 12, 30, 45)

    def test_as_repozo_date_returns_the_six_tuple(self) -> None:
        assert as_repozo_date(MOMENT) == (2026, 3, 1, 12, 30, 45)

    @pytest.mark.parametrize(
        "bad", ["", "2026-03-01", "not-a-stamp", "2026-13-01-00-00-00"]
    )
    def test_invalid_stamps_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_stamp(bad)


class TestFindBackupFiles:
    def test_missing_repository_yields_nothing(self, tmp_path: Path) -> None:
        assert find_backup_files(tmp_path / "absent") == []

    def test_empty_repository_yields_nothing(self, repository: Path) -> None:
        assert find_backup_files(repository) == []

    def test_recognises_all_four_data_extensions(self, repository: Path) -> None:
        touch(
            repository,
            "2026-03-01-12-00-00.fs",
            "2026-03-01-12-00-01.fsz",
            "2026-03-01-12-00-02.deltafs",
            "2026-03-01-12-00-03.deltafsz",
        )
        found = find_backup_files(repository)

        assert [f.name for f in found] == [
            "2026-03-01-12-00-00.fs",
            "2026-03-01-12-00-01.fsz",
            "2026-03-01-12-00-02.deltafs",
            "2026-03-01-12-00-03.deltafsz",
        ]
        assert [f.full for f in found] == [True, True, False, False]

    def test_ignores_non_data_files(self, repository: Path) -> None:
        """.dat, .index and blob archives share the directory but are not backups."""
        touch(
            repository,
            "2026-03-01-12-00-00.fsz",
            "2026-03-01-12-00-00.dat",
            "2026-03-01-12-00-00.index",
            "2026-03-01-12-00-00.tar.gz",
            "notes.txt",
        )
        assert [f.name for f in find_backup_files(repository)] == [
            "2026-03-01-12-00-00.fsz"
        ]

    def test_results_are_chronological_regardless_of_listing_order(
        self, repository: Path
    ) -> None:
        touch(
            repository,
            "2026-03-01-12-00-05.deltafsz",
            "2026-03-01-11-00-00.fsz",
            "2026-03-01-12-00-01.deltafsz",
        )
        stamps = [f.stamp for f in find_backup_files(repository)]

        assert stamps == sorted(stamps)

    def test_backup_file_ordering_is_by_stamp(self) -> None:
        older = BackupFile(parse_stamp("2026-03-01-12-00-00"), True, Path("a"))
        newer = BackupFile(parse_stamp("2026-03-01-12-00-01"), False, Path("b"))

        assert older < newer


class TestLatestStamp:
    def test_none_when_empty(self, repository: Path) -> None:
        assert latest_stamp(repository) is None

    def test_returns_the_newest(self, repository: Path) -> None:
        touch(
            repository,
            "2026-03-01-12-00-00.fsz",
            "2026-03-01-12-00-09.deltafsz",
            "2026-03-01-12-00-04.deltafsz",
        )
        assert latest_stamp(repository) == parse_stamp("2026-03-01-12-00-09")


class TestNextStamp:
    def test_uses_the_clock_for_an_empty_repository(self, repository: Path) -> None:
        assert next_stamp(repository, now=MOMENT) == MOMENT

    def test_truncates_sub_second_precision(self, repository: Path) -> None:
        """repozo filenames have one-second resolution; keep our stamps aligned."""
        assert next_stamp(repository, now=MOMENT.replace(microsecond=999_999)) == MOMENT

    def test_uses_the_clock_when_it_is_ahead(self, repository: Path) -> None:
        touch(repository, "2026-03-01-11-00-00.fsz")
        assert next_stamp(repository, now=MOMENT) == MOMENT

    def test_steps_past_a_collision(self, repository: Path) -> None:
        """The core guard: never reuse the stamp of an existing backup."""
        touch(repository, format_stamp(MOMENT) + ".fsz")

        assert next_stamp(repository, now=MOMENT) == MOMENT + timedelta(seconds=1)

    def test_steps_past_a_future_stamp(self, repository: Path) -> None:
        """A clock that jumped backwards must not produce a stamp already used."""
        touch(repository, "2026-03-01-13-00-00.fsz")

        assert next_stamp(repository, now=MOMENT) == parse_stamp("2026-03-01-13-00-01")

    def test_repeated_allocation_is_strictly_increasing(self, repository: Path) -> None:
        """Simulates many backups within the same clock second."""
        seen = []
        for _ in range(5):
            stamp = next_stamp(repository, now=MOMENT)
            touch(repository, format_stamp(stamp) + ".deltafsz")
            seen.append(stamp)

        assert seen == sorted(set(seen))
        assert len(seen) == 5
