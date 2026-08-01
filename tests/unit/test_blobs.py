"""Tests for blob backup and restore.

Fidelity is checked against an oracle: a snapshot of the blobstorage taken at
backup time, compared path-by-path and byte-by-byte against what comes back out.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zodb_backup import blobs
from zodb_backup.blobs import LATEST_LINK
from zodb_backup.blobs import BlobBackup
from zodb_backup.errors import BackupError
from zodb_backup.errors import RestoreError
from zodb_backup.timestamps import format_stamp

import os
import pytest
import random
import shutil
import stat


Snapshot = Callable[[Path], dict[str, bytes]]
Stamps = Callable[[int], datetime]

#: Every storage mode a backup can be written in, as keyword arguments.
MODES = {
    "rsync": {"archive": False, "use_rsync": True},
    "copytree": {"archive": False, "use_rsync": False},
    "tar": {"archive": True, "use_rsync": False},
    "tar.gz": {"archive": True, "compress": True, "use_rsync": False},
}

needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync is not installed"
)


@pytest.fixture
def blob_location(tmp_path: Path) -> Path:
    """Directory blob backups are written to."""
    return tmp_path / "blobbackups"


def inode(path: Path) -> int:
    """Return a file's inode number, used to prove hard links."""
    return path.stat().st_ino


class TestFindBackups:
    def test_missing_location_yields_nothing(self, tmp_path: Path) -> None:
        assert blobs.find_backups(tmp_path / "absent") == []

    def test_recognises_trees_and_archives(self, blob_location: Path) -> None:
        blob_location.mkdir()
        (blob_location / "blobstorage.2026-03-01-12-00-00").mkdir()
        (blob_location / "blobstorage.2026-03-01-12-00-01.tar").touch()
        (blob_location / "blobstorage.2026-03-01-12-00-02.tar.gz").touch()

        found = blobs.find_backups(blob_location)

        assert [b.name for b in found] == [
            "blobstorage.2026-03-01-12-00-00",
            "blobstorage.2026-03-01-12-00-01.tar",
            "blobstorage.2026-03-01-12-00-02.tar.gz",
        ]
        assert [b.archive for b in found] == [False, True, True]

    def test_ignores_unrelated_entries(self, blob_location: Path) -> None:
        blob_location.mkdir()
        (blob_location / "blobstorage.2026-03-01-12-00-00").mkdir()
        (blob_location / "notes.txt").touch()
        (blob_location / "blobstorage.not-a-stamp").mkdir()

        assert len(blobs.find_backups(blob_location)) == 1

    def test_ignores_the_latest_symlink(self, blob_location: Path) -> None:
        """Counting the alias as a backup would corrupt retention."""
        blob_location.mkdir()
        target = blob_location / "blobstorage.2026-03-01-12-00-00"
        target.mkdir()
        (blob_location / LATEST_LINK).symlink_to(target.name)

        assert [b.name for b in blobs.find_backups(blob_location)] == [target.name]


class TestFindBackupAtOrBefore:
    @pytest.fixture
    def populated(self, blob_location: Path) -> Path:
        blob_location.mkdir()
        for second in (0, 5, 9):
            (blob_location / f"blobstorage.2026-03-01-12-00-0{second}").mkdir()
        return blob_location

    def test_exact_match(self, populated: Path, stamps: Stamps) -> None:
        found = blobs.find_backup_at_or_before(populated, stamps(5))
        assert found is not None
        assert found.stamp == stamps(5)

    def test_picks_the_newest_not_newer_than_the_moment(
        self, populated: Path, stamps: Stamps
    ) -> None:
        found = blobs.find_backup_at_or_before(populated, stamps(7))
        assert found is not None
        assert found.stamp == stamps(5)

    def test_none_when_every_backup_is_newer(
        self, populated: Path, stamps: Stamps
    ) -> None:
        assert blobs.find_backup_at_or_before(populated, stamps(-1)) is None


class TestBackup:
    @pytest.mark.parametrize("mode", list(MODES))
    def test_backup_is_named_after_the_stamp(
        self,
        mode: str,
        blobstorage: Path,
        blob_location: Path,
        stamps: Stamps,
    ) -> None:
        """Invariant I2: the blob backup carries the filestorage stamp."""
        result = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            **MODES[mode],
        )

        assert result.stamp == stamps(0)
        assert format_stamp(stamps(0)) in result.path.name
        assert result.path.exists()

    def test_tree_layout_nests_the_blobstorage_name(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """The nesting is what makes --link-dest line up between backups."""
        result = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            use_rsync=False,
        )

        assert (result.path / "blobstorage" / ".layout").exists()

    def test_compressed_archive_is_smaller_and_suffixed(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        plain = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(0), archive=True
        )
        compressed = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(1),
            archive=True,
            compress=True,
        )

        assert plain.path.name.endswith(".tar")
        assert compressed.path.name.endswith(".tar.gz")

    def test_location_is_created_if_missing(
        self, blobstorage: Path, tmp_path: Path, stamps: Stamps
    ) -> None:
        target = tmp_path / "nested" / "blobbackups"
        blobs.backup(source=blobstorage, location=target, stamp=stamps(0))
        assert target.is_dir()

    def test_missing_source_raises(
        self, tmp_path: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        with pytest.raises(BackupError, match="does not exist"):
            blobs.backup(
                source=tmp_path / "absent", location=blob_location, stamp=stamps(0)
            )

    def test_reusing_a_stamp_raises(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """Refuse to silently overwrite an existing backup."""
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))

        with pytest.raises(BackupError, match="already exists"):
            blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))


class TestHardLinks:
    """Invariant behind the whole rsync mode: unchanged blobs are not copied."""

    @needs_rsync
    def test_unchanged_files_are_hard_links_between_backups(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        first = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(0)
        )
        second = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        unchanged = Path("blobstorage") / "0x00" / "0x0a" / "0x00.blob"
        assert inode(first.path / unchanged) == inode(second.path / unchanged)

    @needs_rsync
    def test_changed_files_are_not_hard_links(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        first = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(0)
        )
        changed = blobstorage / "0x00" / "0x0a" / "0x00.blob"
        changed.write_bytes(b"rewritten content")
        second = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        relative = changed.relative_to(blobstorage.parent)
        assert inode(first.path / relative) != inode(second.path / relative)
        assert (second.path / relative).read_bytes() == b"rewritten content"

    @needs_rsync
    def test_deleted_files_disappear_from_the_next_backup(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """--delete must apply, or backups would accumulate deleted blobs."""
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))
        (blobstorage / "0x01" / "0x0a" / "0x01.blob").unlink()
        second = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        assert not (
            second.path / "blobstorage" / "0x01" / "0x0a" / "0x01.blob"
        ).exists()

    def test_copytree_mode_does_not_hard_link(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        first = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            use_rsync=False,
        )
        second = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(1),
            use_rsync=False,
        )

        unchanged = Path("blobstorage") / "0x00" / "0x0a" / "0x00.blob"
        assert inode(first.path / unchanged) != inode(second.path / unchanged)


class TestLatestSymlink:
    """Invariant I4."""

    def test_points_at_the_newest_tree_backup(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))
        newest = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        link = blob_location / LATEST_LINK
        assert link.is_symlink()
        assert link.resolve() == newest.path.resolve()

    def test_is_relative_so_the_location_can_be_moved(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """An absolute link would break the moment the volume is mounted elsewhere."""
        result = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(0)
        )

        assert (blob_location / LATEST_LINK).readlink() == Path(result.path.name)

    def test_update_is_atomic_never_leaving_the_link_absent(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        """Replacing must not go through an unlinked state."""
        first = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(0)
        )
        link = blob_location / LATEST_LINK
        assert link.is_symlink()

        second = blob_location / "blobstorage.2026-03-01-12-00-09"
        second.mkdir()
        blobs.update_latest_symlink(blob_location, second)

        assert link.is_symlink()
        assert link.readlink() == Path(second.name)
        assert first.path.exists()

    def test_can_be_removed(self, blob_location: Path) -> None:
        blob_location.mkdir()
        target = blob_location / "blobstorage.2026-03-01-12-00-00"
        target.mkdir()
        blobs.update_latest_symlink(blob_location, target)

        blobs.update_latest_symlink(blob_location, None)

        assert not (blob_location / LATEST_LINK).is_symlink()

    def test_archives_do_not_get_a_symlink(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            archive=True,
        )

        assert not (blob_location / LATEST_LINK).exists()


class TestRestoreFidelity:
    @pytest.mark.parametrize("mode", list(MODES))
    def test_restore_reproduces_the_oracle_exactly(
        self,
        mode: str,
        blobstorage: Path,
        blob_location: Path,
        tmp_path: Path,
        snapshot_tree: Snapshot,
        stamps: Stamps,
    ) -> None:
        oracle = snapshot_tree(blobstorage)
        backup = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            **MODES[mode],
        )

        target = tmp_path / "restored-blobstorage"
        blobs.restore(
            backup=backup,
            destination=target,
            use_rsync=MODES[mode].get("use_rsync", False),
        )

        assert snapshot_tree(target) == oracle

    @pytest.mark.parametrize("mode", list(MODES))
    def test_restore_removes_files_absent_from_the_backup(
        self,
        mode: str,
        blobstorage: Path,
        blob_location: Path,
        tmp_path: Path,
        snapshot_tree: Snapshot,
        stamps: Stamps,
    ) -> None:
        """A restore is a replacement, not a merge."""
        oracle = snapshot_tree(blobstorage)
        backup = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            **MODES[mode],
        )

        target = tmp_path / "restored-blobstorage"
        target.mkdir()
        (target / "stale.blob").write_bytes(b"should not survive")

        blobs.restore(
            backup=backup,
            destination=target,
            use_rsync=MODES[mode].get("use_rsync", False),
        )

        assert not (target / "stale.blob").exists()
        assert snapshot_tree(target) == oracle

    def test_restore_into_a_missing_parent_directory(
        self,
        blobstorage: Path,
        blob_location: Path,
        tmp_path: Path,
        snapshot_tree: Snapshot,
        stamps: Stamps,
    ) -> None:
        oracle = snapshot_tree(blobstorage)
        backup = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(0),
            use_rsync=False,
        )

        target = tmp_path / "deeply" / "nested" / "blobstorage"
        blobs.restore(backup=backup, destination=target, use_rsync=False)

        assert snapshot_tree(target) == oracle

    def test_missing_backup_raises(self, tmp_path: Path, stamps: Stamps) -> None:
        phantom = BlobBackup(stamp=stamps(0), path=tmp_path / "absent", archive=False)
        with pytest.raises(RestoreError, match="does not exist"):
            blobs.restore(backup=phantom, destination=tmp_path / "out")

    def test_malformed_tree_backup_raises(
        self, blob_location: Path, tmp_path: Path, stamps: Stamps
    ) -> None:
        blob_location.mkdir()
        empty = blob_location / "blobstorage.2026-03-01-12-00-00"
        empty.mkdir()
        backup = BlobBackup(stamp=stamps(0), path=empty, archive=False)

        with pytest.raises(RestoreError, match="single blobstorage directory"):
            blobs.restore(backup=backup, destination=tmp_path / "out")


class TestChurn:
    """Randomised mutation rounds, each compared to its own oracle."""

    @pytest.mark.parametrize("mode", list(MODES))
    def test_every_round_restores_to_its_own_snapshot(
        self,
        mode: str,
        blobstorage: Path,
        blob_location: Path,
        tmp_path: Path,
        snapshot_tree: Snapshot,
        stamps: Stamps,
    ) -> None:
        rng = random.Random(20260731)
        oracles: dict[int, dict[str, bytes]] = {}
        backups: dict[int, BlobBackup] = {}

        for round_number in range(6):
            self._mutate(blobstorage, rng, round_number)
            oracles[round_number] = snapshot_tree(blobstorage)
            backups[round_number] = blobs.backup(
                source=blobstorage,
                location=blob_location,
                stamp=stamps(round_number),
                **MODES[mode],
            )

        for round_number, backup in backups.items():
            target = tmp_path / f"restored-{round_number}"
            blobs.restore(
                backup=backup,
                destination=target,
                use_rsync=MODES[mode].get("use_rsync", False),
            )
            assert snapshot_tree(target) == oracles[round_number], (
                f"round {round_number} did not restore to its own state"
            )

    @staticmethod
    def _mutate(root: Path, rng: random.Random, round_number: int) -> None:
        """Add, rewrite and delete blobs at random.

        Rewrites always change the file's length. A same-length rewrite within
        one clock second is invisible to rsync's default quick check — see
        :class:`TestRsyncQuickCheck` — and ZODB never produces one, because blob
        files are immutable and a new revision is written as a new file.
        """
        folder = root / f"0x{round_number:02x}" / "0x0a"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"added-{round_number}.blob").write_bytes(
            rng.randbytes(rng.randint(1, 200))
        )

        existing = sorted(root.rglob("*.blob"))
        if len(existing) > 2:
            victim = rng.choice(existing)
            grown = victim.stat().st_size + rng.randint(1, 50)
            victim.write_bytes(rng.randbytes(grown))
        if len(existing) > 3:
            rng.choice(existing).unlink()


class TestRsyncQuickCheck:
    """Documents a fidelity limit inherited from rsync itself.

    rsync decides whether a file changed from its size and modification time. A
    file rewritten to the *same length* within the same clock second therefore
    looks unchanged, and ``--link-dest`` hard-links the previous, stale content
    into the new backup.

    This is not reachable through ZODB: blob files are immutable, and a new
    object revision is written as a new file rather than an edit of an existing
    one. It is pinned here so the limitation is a known, tested property rather
    than a surprise, and so the documented ``RSYNC_OPTIONS=--checksum`` escape
    hatch is verified to work.

    The modification time is pinned explicitly rather than relying on both writes
    landing in the same wall-clock second, which is a coin flip and made an
    earlier version of these tests flaky.
    """

    @staticmethod
    def _rewrite_keeping_mtime(target: Path, content: bytes) -> None:
        """Replace a file's contents without changing its size or mtime.

        :param target: file to rewrite.
        :param content: replacement bytes, which must match the current length.
        """
        stat_before = target.stat()
        assert len(content) == stat_before.st_size, "the rewrite must keep the size"
        target.write_bytes(content)
        os.utime(target, (stat_before.st_atime, stat_before.st_mtime))

    @needs_rsync
    def test_same_length_rewrite_in_the_same_second_is_missed(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        target = blobstorage / "0x00" / "0x0a" / "0x00.blob"
        original = target.read_bytes()
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))

        self._rewrite_keeping_mtime(target, b"X" * len(original))
        second = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        backed_up = (second.path / target.relative_to(blobstorage.parent)).read_bytes()
        assert backed_up == original, "rsync unexpectedly caught the rewrite"

    @needs_rsync
    def test_checksum_option_catches_it(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        target = blobstorage / "0x00" / "0x0a" / "0x00.blob"
        original = target.read_bytes()
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))

        rewritten = b"X" * len(original)
        self._rewrite_keeping_mtime(target, rewritten)
        second = blobs.backup(
            source=blobstorage,
            location=blob_location,
            stamp=stamps(1),
            rsync_options=("--checksum",),
        )

        backed_up = (second.path / target.relative_to(blobstorage.parent)).read_bytes()
        assert backed_up == rewritten

    @needs_rsync
    def test_a_length_change_is_always_caught(
        self, blobstorage: Path, blob_location: Path, stamps: Stamps
    ) -> None:
        target = blobstorage / "0x00" / "0x0a" / "0x00.blob"
        blobs.backup(source=blobstorage, location=blob_location, stamp=stamps(0))

        rewritten = b"X" * (target.stat().st_size + 10)
        target.write_bytes(rewritten)
        second = blobs.backup(
            source=blobstorage, location=blob_location, stamp=stamps(1)
        )

        backed_up = (second.path / target.relative_to(blobstorage.parent)).read_bytes()
        assert backed_up == rewritten


class TestReadOnlySource:
    """Invariant I7: backups must work from a read-only mount."""

    @pytest.fixture
    def read_only(self, blobstorage: Path) -> Path:
        original = stat.S_IMODE(blobstorage.stat().st_mode)
        for path in sorted(blobstorage.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        blobstorage.chmod(0o555)
        yield blobstorage
        blobstorage.chmod(original)
        for path in blobstorage.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    @pytest.mark.parametrize("mode", list(MODES))
    def test_backup_succeeds_from_a_read_only_source(
        self,
        mode: str,
        read_only: Path,
        blob_location: Path,
        snapshot_tree: Snapshot,
        stamps: Stamps,
    ) -> None:
        oracle = snapshot_tree(read_only)

        result = blobs.backup(
            source=read_only,
            location=blob_location,
            stamp=stamps(0),
            **MODES[mode],
        )

        assert result.path.exists()
        assert snapshot_tree(read_only) == oracle, "the source was modified"
