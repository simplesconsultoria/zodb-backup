"""Tests for filestorage backup and restore.

These use a real FileStorage built in a temporary directory — no ZEO, no Plone.
Restore fidelity is checked against an oracle: the dict committed at backup time
must come back out byte-for-byte equal.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from ZODB.scripts import repozo as zodb_repozo
from zodb_backup import repozo
from zodb_backup.errors import BackupError
from zodb_backup.errors import RestoreError
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import format_stamp
from zodb_backup.timestamps import next_stamp

import pytest


Commit = Callable[[Path, str, object], None]
ReadRoot = Callable[[Path], dict[str, object]]
Stamps = Callable[[int], datetime]


class TestBackup:
    def test_first_backup_is_full(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        result = repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        assert result.full is True
        assert result.changed is True
        assert result.path is not None
        assert result.path.name == f"{format_stamp(stamps(0))}.fsz"

    def test_second_backup_is_incremental(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        commit(datafs, "b", "second")
        result = repozo.backup(datafs=datafs, repository=repository, stamp=stamps(1))

        assert result.full is False
        assert result.path is not None
        assert result.path.name == f"{format_stamp(stamps(1))}.deltafsz"

    def test_full_can_be_forced(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        commit(datafs, "b", "second")
        result = repozo.backup(
            datafs=datafs, repository=repository, stamp=stamps(1), full=True
        )

        assert result.full is True

    def test_unchanged_filestorage_writes_nothing(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        """A no-op run must be reported honestly, not as a written backup."""
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        result = repozo.backup(datafs=datafs, repository=repository, stamp=stamps(1))

        assert result.changed is False
        assert result.path is None

    def test_uncompressed_backup_when_gzip_disabled(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        result = repozo.backup(
            datafs=datafs, repository=repository, stamp=stamps(0), gzip=False
        )

        assert result.path is not None
        assert result.path.suffix == ".fs"

    def test_repository_is_created_if_missing(
        self, datafs: Path, tmp_path: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        target = tmp_path / "nested" / "backups"

        repozo.backup(datafs=datafs, repository=target, stamp=stamps(0))

        assert target.is_dir()

    def test_stamp_controls_the_filename(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        """We name backups, not the wall clock; this is what makes them ordered."""
        commit(datafs, "a", "first")
        result = repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        assert result.path is not None
        assert result.path.name.startswith("2026-03-01-12-00-00")

    def test_missing_source_raises_backup_error(
        self, datafs: Path, repository: Path, stamps: Stamps
    ) -> None:
        with pytest.raises(BackupError):
            repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

    def test_corrupt_source_raises_backup_error(
        self, datafs: Path, repository: Path, stamps: Stamps
    ) -> None:
        """A damaged Data.fs is an operational failure, not a traceback.

        ZODB raises ``FileStorageFormatError`` here, which is neither a
        ``RepozoError`` nor an ``OSError``; without wrapping it the process would
        exit on an unhandled exception instead of the documented exit code.
        """
        datafs.write_bytes(b"this is not a filestorage")

        with pytest.raises(BackupError):
            repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))


class TestRestoreFidelity:
    def test_restores_state_from_a_full_backup(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        commit(datafs, "a", "first")
        oracle = read_root(datafs)
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        target = tmp_path / "restored" / "Data.fs"
        repozo.recover(repository=repository, output=target)

        assert read_root(target) == oracle

    def test_restores_state_across_incrementals(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))
        commit(datafs, "b", "second")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(1))
        commit(datafs, "c", "third")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(2))
        oracle = read_root(datafs)

        target = tmp_path / "restored" / "Data.fs"
        repozo.recover(repository=repository, output=target)

        assert read_root(target) == oracle
        assert oracle == {"a": "first", "b": "second", "c": "third"}

    def test_restores_an_intermediate_state_by_date(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))
        commit(datafs, "b", "second")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(1))
        oracle_midway = read_root(datafs)
        commit(datafs, "c", "third")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(2))

        target = tmp_path / "restored" / "Data.fs"
        repozo.recover(
            repository=repository, output=target, date=format_stamp(stamps(1))
        )

        assert read_root(target) == oracle_midway
        assert "c" not in read_root(target)

    def test_restore_survives_larger_object_graphs(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        """Several rounds of churn, each compared against its own oracle."""
        for round_number in range(6):
            commit(datafs, f"key-{round_number}", {"payload": "x" * 500})
            repozo.backup(
                datafs=datafs, repository=repository, stamp=stamps(round_number)
            )
        oracle = read_root(datafs)

        target = tmp_path / "restored" / "Data.fs"
        repozo.recover(repository=repository, output=target)

        assert read_root(target) == oracle
        assert len(oracle) == 6

    def test_restore_creates_the_output_directory(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        stamps: Stamps,
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        target = tmp_path / "deeply" / "nested" / "Data.fs"
        repozo.recover(repository=repository, output=target)

        assert target.exists()

    def test_empty_repository_raises_restore_error(
        self, repository: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(RestoreError):
            repozo.recover(repository=repository, output=tmp_path / "out" / "Data.fs")


class TestSameSecondCollision:
    """Regression tests for an upstream repozo defect.

    ``ZODB.scripts.repozo.find_files`` reverse-sorts the repository listing and
    stops at the first full backup it meets. When a full backup and an
    incremental share a timestamp, ``"<stamp>.fsz"`` sorts after
    ``"<stamp>.deltafsz"`` (``"f" > "d"``), so the full backup is seen first, the
    loop breaks, and the incremental is silently omitted from the restore — which
    then reports success while missing data.

    :func:`zodb_backup.timestamps.next_stamp` makes the collision impossible.
    """

    def test_upstream_repozo_loses_a_same_second_incremental(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        """Pins the upstream behaviour we are defending against.

        If this ever starts failing, ZODB has fixed the bug and our guard can be
        reconsidered.
        """
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))
        commit(datafs, "b", "second")
        # Deliberately reuse the stamp, which our own API would never do.
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        target = tmp_path / "restored" / "Data.fs"
        # Raw repozo, unlike our wrapper, does not create the output directory.
        target.parent.mkdir()
        options = zodb_repozo.parseargs(
            ["--recover", "--repository", str(repository), "--output", str(target)]
        )
        zodb_repozo.do_recover(options)

        assert read_root(target) == {"a": "first"}, (
            "upstream repozo appears to have been fixed; revisit the guard"
        )

    def test_next_stamp_prevents_the_collision(
        self,
        datafs: Path,
        repository: Path,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        stamps: Stamps,
    ) -> None:
        """The same sequence, with stamps allocated the way the tool does it."""
        commit(datafs, "a", "first")
        repozo.backup(
            datafs=datafs,
            repository=repository,
            stamp=next_stamp(repository, now=stamps(0)),
        )
        commit(datafs, "b", "second")
        repozo.backup(
            datafs=datafs,
            repository=repository,
            stamp=next_stamp(repository, now=stamps(0)),
        )
        oracle = read_root(datafs)

        target = tmp_path / "restored" / "Data.fs"
        repozo.recover(repository=repository, output=target)

        assert read_root(target) == oracle
        assert oracle == {"a": "first", "b": "second"}

    def test_allocated_stamps_never_repeat_under_a_frozen_clock(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        """Many backups in one clock second must still produce distinct files."""
        for index in range(4):
            commit(datafs, f"k{index}", index)
            repozo.backup(
                datafs=datafs,
                repository=repository,
                stamp=next_stamp(repository, now=stamps(0)),
            )

        names = [f.name for f in find_backup_files(repository)]
        assert len(names) == len(set(names)) == 4


class TestVerify:
    def test_verifies_a_healthy_repository(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        repozo.backup(datafs=datafs, repository=repository, stamp=stamps(0))

        repozo.verify(repository=repository)

    def test_detects_a_corrupted_backup(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        commit(datafs, "a", "first")
        result = repozo.backup(
            datafs=datafs, repository=repository, stamp=stamps(0), gzip=False
        )
        assert result.path is not None
        result.path.write_bytes(b"corrupted")

        with pytest.raises(BackupError):
            repozo.verify(repository=repository)


class TestVerbosityIsolation:
    def test_verbose_run_does_not_leak_into_later_runs(
        self, datafs: Path, repository: Path, commit: Commit, stamps: Stamps
    ) -> None:
        """repozo.VERBOSE is a sticky global; a debug run must not infect others."""
        before = zodb_repozo.VERBOSE
        commit(datafs, "a", "first")

        repozo.backup(
            datafs=datafs, repository=repository, stamp=stamps(0), verbose=True
        )

        assert before == zodb_repozo.VERBOSE

    def test_verbosity_is_restored_after_a_failure(
        self, datafs: Path, repository: Path, stamps: Stamps
    ) -> None:
        before = zodb_repozo.VERBOSE

        with pytest.raises(BackupError):
            repozo.backup(
                datafs=datafs, repository=repository, stamp=stamps(0), verbose=True
            )

        assert before == zodb_repozo.VERBOSE
