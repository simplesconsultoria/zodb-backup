"""Tests for rotation and the filestorage/blob coupling (invariant I3)."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zodb_backup import retention
from zodb_backup.blobs import LATEST_LINK
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import format_stamp

import pytest
import random


Stamps = Callable[[int], datetime]


def write_full(repository: Path, stamp: datetime) -> Path:
    """Create a full backup and the companions repozo writes beside it."""
    repository.mkdir(parents=True, exist_ok=True)
    base = format_stamp(stamp)
    (repository / f"{base}.fsz").write_bytes(b"full")
    (repository / f"{base}.dat").write_text("chain\n")
    (repository / f"{base}.index").write_bytes(b"index")
    return repository / f"{base}.fsz"


def write_incremental(repository: Path, stamp: datetime) -> Path:
    """Create an incremental backup file."""
    repository.mkdir(parents=True, exist_ok=True)
    base = format_stamp(stamp)
    (repository / f"{base}.deltafsz").write_bytes(b"delta")
    (repository / f"{base}.index").write_bytes(b"index")
    return repository / f"{base}.deltafsz"


def write_blob_tree(location: Path, stamp: datetime) -> Path:
    """Create a blob tree backup."""
    location.mkdir(parents=True, exist_ok=True)
    target = location / f"blobstorage.{format_stamp(stamp)}"
    (target / "blobstorage").mkdir(parents=True)
    (target / "blobstorage" / "a.blob").write_bytes(b"blob")
    return target


def names(repository: Path) -> set[str]:
    """Names of every file in a directory."""
    return {entry.name for entry in repository.iterdir()}


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    folder = tmp_path / "backups"
    folder.mkdir()
    return folder


@pytest.fixture
def blob_location(tmp_path: Path) -> Path:
    folder = tmp_path / "blobbackups"
    folder.mkdir()
    return folder


class TestRotateFilestorage:
    def test_keep_zero_removes_nothing(self, repository: Path, stamps: Stamps) -> None:
        """KEEP=0 means keep everything, not keep nothing."""
        for offset in range(4):
            write_full(repository, stamps(offset))
        before = names(repository)

        assert retention.rotate_filestorage(repository, 0) == []
        assert names(repository) == before

    def test_fewer_backups_than_keep_removes_nothing(
        self, repository: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(0))
        write_full(repository, stamps(1))

        assert retention.rotate_filestorage(repository, 5) == []

    def test_keeps_the_newest_full_backups(
        self, repository: Path, stamps: Stamps
    ) -> None:
        for offset in range(5):
            write_full(repository, stamps(offset))

        retention.rotate_filestorage(repository, 2)

        surviving = [b.stamp for b in find_backup_files(repository)]
        assert surviving == [stamps(3), stamps(4)]

    def test_incrementals_survive_with_their_full_backup(
        self, repository: Path, stamps: Stamps
    ) -> None:
        """Splitting a chain would leave a backup that cannot be restored."""
        write_full(repository, stamps(0))
        write_incremental(repository, stamps(1))
        write_full(repository, stamps(2))
        write_incremental(repository, stamps(3))
        write_incremental(repository, stamps(4))

        retention.rotate_filestorage(repository, 1)

        surviving = [b.stamp for b in find_backup_files(repository)]
        assert surviving == [stamps(2), stamps(3), stamps(4)]

    def test_removes_dat_and_index_companions(
        self, repository: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(0))
        write_full(repository, stamps(1))

        retention.rotate_filestorage(repository, 1)

        assert not any(
            name.startswith(format_stamp(stamps(0))) for name in names(repository)
        )

    def test_ignores_unrelated_files(self, repository: Path, stamps: Stamps) -> None:
        write_full(repository, stamps(0))
        write_full(repository, stamps(1))
        (repository / "README").write_text("do not delete me")

        retention.rotate_filestorage(repository, 1)

        assert "README" in names(repository)

    def test_ordering_uses_filenames_not_modification_times(
        self, repository: Path, stamps: Stamps
    ) -> None:
        """mtimes get rewritten by copies and restores; stamps do not."""
        newest = write_full(repository, stamps(4))
        oldest = write_full(repository, stamps(0))
        # Make the oldest backup look freshly written.
        import os

        os.utime(oldest, (2_000_000_000, 2_000_000_000))

        retention.rotate_filestorage(repository, 1)

        assert newest.exists()
        assert not oldest.exists()


class TestOrphanedBlobs:
    def test_removes_blobs_older_than_the_oldest_full_backup(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(5))
        orphan = write_blob_tree(blob_location, stamps(1))
        paired = write_blob_tree(blob_location, stamps(5))

        removed = retention.remove_orphaned_blobs(blob_location, repository)

        assert removed == [orphan]
        assert not orphan.exists()
        assert paired.exists()

    def test_keeps_blobs_newer_than_the_cutoff(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(0))
        newer = write_blob_tree(blob_location, stamps(9))

        assert retention.remove_orphaned_blobs(blob_location, repository) == []
        assert newer.exists()

    def test_removes_orphaned_archives_too(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(5))
        orphan = blob_location / f"blobstorage.{format_stamp(stamps(1))}.tar.gz"
        orphan.write_bytes(b"archive")

        removed = retention.remove_orphaned_blobs(blob_location, repository)

        assert removed == [orphan]
        assert not orphan.exists()

    def test_does_nothing_without_a_full_filestorage_backup(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """Never delete the only blob copies on a guess."""
        write_incremental(repository, stamps(1))
        blob = write_blob_tree(blob_location, stamps(0))

        assert retention.remove_orphaned_blobs(blob_location, repository) == []
        assert blob.exists()

    def test_latest_symlink_is_repointed_after_deletion(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """A dangling 'latest' would break every consumer that follows it."""
        write_full(repository, stamps(5))
        removed_tree = write_blob_tree(blob_location, stamps(1))
        surviving = write_blob_tree(blob_location, stamps(5))
        (blob_location / LATEST_LINK).symlink_to(removed_tree.name)

        retention.remove_orphaned_blobs(blob_location, repository)

        link = blob_location / LATEST_LINK
        assert link.is_symlink()
        assert link.resolve() == surviving.resolve()

    def test_latest_symlink_is_removed_when_nothing_survives(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(5))
        only = write_blob_tree(blob_location, stamps(1))
        (blob_location / LATEST_LINK).symlink_to(only.name)

        retention.remove_orphaned_blobs(blob_location, repository)

        assert not (blob_location / LATEST_LINK).is_symlink()


class TestCouplingInvariant:
    """Invariant I3, as an executable statement."""

    def test_reports_a_healthy_pair(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(0))
        write_blob_tree(blob_location, stamps(0))

        assert retention.verify_coupling(repository, blob_location) == []

    def test_detects_an_orphaned_blob_backup(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(5))
        write_blob_tree(blob_location, stamps(1))

        problems = retention.verify_coupling(repository, blob_location)

        assert problems
        assert "can never be restored" in problems[0]

    def test_detects_a_dangling_latest_symlink(
        self, repository: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        write_full(repository, stamps(0))
        write_blob_tree(blob_location, stamps(0))
        (blob_location / LATEST_LINK).symlink_to("blobstorage.2000-01-01-00-00-00")

        assert "dangling" in " ".join(
            retention.verify_coupling(repository, blob_location)
        )


class TestRandomisedRotation:
    """Long random sequences checked against an independent oracle."""

    @pytest.mark.parametrize("keep", [0, 1, 2, 5])
    def test_coupling_holds_after_every_rotation(
        self, repository: Path, blob_location: Path, stamps: Stamps, keep: int
    ) -> None:
        rng = random.Random(20260731 + keep)
        expected_fulls: list[datetime] = []

        for offset in range(40):
            stamp = stamps(offset)
            is_full = offset == 0 or rng.random() < 0.35
            if is_full:
                write_full(repository, stamp)
                expected_fulls.append(stamp)
            else:
                write_incremental(repository, stamp)
            write_blob_tree(blob_location, stamp)

            result = retention.apply(
                repository=repository, keep=keep, blob_location=blob_location
            )
            assert isinstance(result, retention.RetentionResult)

            # Oracle: which full backups should still be present?
            if keep > 0 and len(expected_fulls) > keep:
                expected_fulls = expected_fulls[-keep:]
            surviving_fulls = [b.stamp for b in find_backup_files(repository) if b.full]
            assert surviving_fulls == expected_fulls, f"after round {offset}"

            problems = retention.verify_coupling(repository, blob_location)
            assert problems == [], f"after round {offset}: {problems}"

    @pytest.mark.parametrize("keep", [1, 2, 5])
    def test_every_surviving_chain_is_complete(
        self, repository: Path, blob_location: Path, stamps: Stamps, keep: int
    ) -> None:
        """No incremental may outlive the full backup it depends on."""
        rng = random.Random(99 + keep)
        for offset in range(30):
            stamp = stamps(offset)
            if offset == 0 or rng.random() < 0.3:
                write_full(repository, stamp)
            else:
                write_incremental(repository, stamp)
            retention.apply(repository=repository, keep=keep)

            surviving = find_backup_files(repository)
            if surviving:
                assert surviving[0].full, (
                    f"after round {offset} the oldest surviving backup is an "
                    "incremental with no full backup to apply it to"
                )
