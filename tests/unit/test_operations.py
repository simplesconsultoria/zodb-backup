"""End-to-end tests for the backup, snapshot and restore operations.

These exercise the real orchestration against a real FileStorage and a real
blobstorage in a temporary directory, so the ordering and coupling invariants are
checked as they actually behave rather than as they are meant to.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zodb_backup import operations
from zodb_backup import retention
from zodb_backup.blobs import LATEST_LINK
from zodb_backup.config import Settings
from zodb_backup.errors import BackupError
from zodb_backup.errors import CommandError
from zodb_backup.errors import RestoreError
from zodb_backup.timestamps import find_backup_files
from zodb_backup.timestamps import format_stamp

import logging
import pytest


Commit = Callable[[Path, str, object], None]
ReadRoot = Callable[[Path], dict[str, object]]
Snapshot = Callable[[Path], dict[str, bytes]]
Stamps = Callable[[int], datetime]


@pytest.fixture
def settings(tmp_path: Path, datafs: Path, blobstorage: Path) -> Settings:
    """Settings pointing entirely inside the test's temporary directory."""
    return Settings.from_env(
        {
            "DATAFS": str(datafs),
            "BLOBSTORAGE": str(blobstorage),
            "BACKUP_LOCATION": str(tmp_path / "backups" / "filestorage"),
            "BLOB_BACKUP_LOCATION": str(tmp_path / "backups" / "blobstorage"),
            "SNAPSHOT_LOCATION": str(tmp_path / "backups" / "snapshots"),
            "BLOB_SNAPSHOT_LOCATION": str(tmp_path / "backups" / "blobsnapshots"),
            "ASSUME_YES": "true",
        }
    )


class TestBackupOrdering:
    """Invariant I1: filestorage first, blobs second."""

    def test_filestorage_is_backed_up_before_blobs(
        self,
        settings: Settings,
        commit: Commit,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit(settings.datafs, "a", "first")
        order: list[str] = []

        import zodb_backup.blobs as blob_module
        import zodb_backup.repozo as repozo_module

        real_fs = repozo_module.backup
        real_blobs = blob_module.backup

        def spy_fs(**kwargs: object) -> object:
            order.append("filestorage")
            return real_fs(**kwargs)  # type: ignore[arg-type]

        def spy_blobs(**kwargs: object) -> object:
            order.append("blobs")
            return real_blobs(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(operations.repozo, "backup", spy_fs)
        monkeypatch.setattr(operations.blob_module, "backup", spy_blobs)

        operations.backup(settings)

        assert order == ["filestorage", "blobs"]

    def test_a_failed_filestorage_backup_leaves_blobs_untouched(
        self, settings: Settings
    ) -> None:
        """A blob backup with no filestorage backup beside it is useless."""
        # datafs was never created, so repozo fails.
        with pytest.raises(Exception, match="repozo"):
            operations.backup(settings)

        assert not settings.blob_backup_location.exists()


class TestTimestampCoupling:
    """Invariant I2: both halves of a run share one timestamp."""

    def test_blob_backup_carries_the_filestorage_stamp(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")

        result = operations.backup(settings)

        assert result.blobs is not None
        assert result.blobs.stamp == result.filestorage.stamp
        assert format_stamp(result.filestorage.stamp) in result.blobs.path.name

    def test_repeated_runs_produce_distinct_paired_stamps(
        self, settings: Settings, commit: Commit
    ) -> None:
        """Back-to-back runs must not collide, even inside one clock second."""
        seen = []
        for index in range(4):
            commit(settings.datafs, f"k{index}", index)
            result = operations.backup(settings)
            assert result.blobs is not None
            assert result.blobs.stamp == result.filestorage.stamp
            seen.append(result.filestorage.stamp)

        assert len(set(seen)) == 4
        assert seen == sorted(seen)


class TestBackupBehaviour:
    def test_first_run_is_full_then_incremental(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        first = operations.backup(settings)

        commit(settings.datafs, "b", "second")
        second = operations.backup(settings)

        assert first.filestorage.full is True
        assert second.filestorage.full is False

    def test_snapshot_writes_to_the_snapshot_locations(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")

        result = operations.snapshot(settings)

        assert result.filestorage.path is not None
        assert settings.snapshot_location in result.filestorage.path.parents
        assert result.blobs is not None
        assert settings.blob_snapshot_location in result.blobs.path.parents
        assert not settings.backup_location.exists()

    def test_snapshot_is_always_full(self, settings: Settings, commit: Commit) -> None:
        commit(settings.datafs, "a", "first")
        operations.snapshot(settings)
        commit(settings.datafs, "b", "second")

        second = operations.snapshot(settings)

        assert second.filestorage.full is True

    def test_filestorage_only_when_blobstorage_is_unset(
        self, settings: Settings, commit: Commit
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, blobstorage=None)
        commit(settings.datafs, "a", "first")

        result = operations.backup(settings)

        assert result.blobs is None
        assert not settings.blob_backup_location.exists()

    def test_latest_symlink_follows_the_newest_run(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        commit(settings.datafs, "b", "second")
        newest = operations.backup(settings)

        assert newest.blobs is not None
        link = settings.blob_backup_location / LATEST_LINK
        assert link.resolve() == newest.blobs.path.resolve()


class TestUnchangedRuns:
    """An idle run must not manufacture work."""

    def test_no_new_blob_backup_when_the_filestorage_is_unchanged(
        self, settings: Settings, commit: Commit
    ) -> None:
        """Otherwise every idle run re-copies, or re-tars, the whole blobstorage."""
        commit(settings.datafs, "a", "first")
        first = operations.backup(settings)

        second = operations.backup(settings)

        assert second.filestorage.changed is False
        assert second.blobs is not None
        assert first.blobs is not None
        assert second.blobs.path == first.blobs.path
        assert len(list(settings.blob_backup_location.glob("blobstorage.*"))) == 1

    def test_idle_runs_leave_the_pairing_intact(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        for _ in range(3):
            operations.backup(settings)

        problems = retention.verify_coupling(
            settings.backup_location, settings.blob_backup_location
        )
        assert problems == []

    def test_a_missing_blob_backup_is_repaired_on_the_next_run(
        self, settings: Settings, commit: Commit
    ) -> None:
        """Simulates a run that died between the filestorage and blob steps."""
        commit(settings.datafs, "a", "first")
        first = operations.backup(settings)
        assert first.blobs is not None

        import shutil

        shutil.rmtree(first.blobs.path)
        (settings.blob_backup_location / LATEST_LINK).unlink(missing_ok=True)

        second = operations.backup(settings)

        assert second.filestorage.changed is False
        assert second.blobs is not None
        assert second.blobs.stamp == first.filestorage.stamp
        assert second.blobs.path.exists()

    def test_an_idle_run_still_refreshes_the_latest_symlink(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        first = operations.backup(settings)
        assert first.blobs is not None
        (settings.blob_backup_location / LATEST_LINK).unlink()

        operations.backup(settings)

        link = settings.blob_backup_location / LATEST_LINK
        assert link.is_symlink()
        assert link.resolve() == first.blobs.path.resolve()


class TestRetentionIsApplied:
    def test_rotation_runs_after_a_successful_backup(
        self, settings: Settings, commit: Commit
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, keep=1, full=True)
        for index in range(4):
            commit(settings.datafs, f"k{index}", index)
            operations.backup(settings)

        fulls = [b for b in find_backup_files(settings.backup_location) if b.full]
        assert len(fulls) == 1

    def test_coupling_holds_after_every_run(
        self, settings: Settings, commit: Commit
    ) -> None:
        """Invariant I3, checked end to end rather than on synthetic files."""
        from dataclasses import replace

        settings = replace(settings, keep=2, full=True)
        for index in range(6):
            commit(settings.datafs, f"k{index}", index)
            operations.backup(settings)

            problems = retention.verify_coupling(
                settings.backup_location, settings.blob_backup_location
            )
            assert problems == [], f"after run {index}: {problems}"


class TestHooks:
    def test_pre_command_failure_aborts_before_anything_is_written(
        self, settings: Settings, commit: Commit
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, pre_command="exit 3")
        commit(settings.datafs, "a", "first")

        with pytest.raises(CommandError, match="PRE_COMMAND"):
            operations.backup(settings)

        assert not settings.backup_location.exists()
        assert not settings.blob_backup_location.exists()

    def test_pre_command_runs_and_its_output_is_captured(
        self, settings: Settings, commit: Commit
    ) -> None:
        marker = settings.datafs.parent / "pre-ran"
        from dataclasses import replace

        settings = replace(settings, pre_command=f"touch {marker}")
        commit(settings.datafs, "a", "first")

        operations.backup(settings)

        assert marker.exists()

    def test_post_command_failure_keeps_the_completed_backup(
        self, settings: Settings, commit: Commit
    ) -> None:
        """An off-site push that failed must not destroy the local backup."""
        from dataclasses import replace

        settings = replace(settings, post_command="exit 4")
        commit(settings.datafs, "a", "first")

        with pytest.raises(CommandError, match="POST_COMMAND"):
            operations.backup(settings)

        assert [b for b in find_backup_files(settings.backup_location) if b.full]
        assert list(settings.blob_backup_location.iterdir())

    def test_post_command_runs_after_the_backup_exists(
        self, settings: Settings, commit: Commit
    ) -> None:
        listing = settings.datafs.parent / "listing.txt"
        from dataclasses import replace

        settings = replace(
            settings,
            post_command=f"ls {settings.backup_location} > {listing}",
        )
        commit(settings.datafs, "a", "first")

        operations.backup(settings)

        assert ".fsz" in listing.read_text()


class TestRestore:
    def test_round_trip_restores_filestorage_and_blobs(
        self,
        settings: Settings,
        tmp_path: Path,
        commit: Commit,
        read_root: ReadRoot,
        snapshot_tree: Snapshot,
    ) -> None:
        commit(settings.datafs, "a", "first")
        (settings.blobstorage / "0x00" / "0x0a" / "0x00.blob").write_bytes(b"v1")
        operations.backup(settings)
        fs_oracle = read_root(settings.datafs)
        blob_oracle = snapshot_tree(settings.blobstorage)

        # Churn after the backup; the restore must undo it.
        commit(settings.datafs, "b", "second")
        (settings.blobstorage / "0x00" / "0x0a" / "0x00.blob").write_bytes(b"v2-longer")

        operations.restore(settings)

        assert read_root(settings.datafs) == fs_oracle
        assert snapshot_tree(settings.blobstorage) == blob_oracle

    def test_restore_pairs_blobs_with_the_requested_date(
        self,
        settings: Settings,
        commit: Commit,
        read_root: ReadRoot,
        snapshot_tree: Snapshot,
    ) -> None:
        commit(settings.datafs, "a", "first")
        (settings.blobstorage / "round-1.blob").write_bytes(b"one")
        first = operations.backup(settings)
        fs_oracle = read_root(settings.datafs)
        blob_oracle = snapshot_tree(settings.blobstorage)

        commit(settings.datafs, "b", "second")
        (settings.blobstorage / "round-2.blob").write_bytes(b"two")
        operations.backup(settings)

        operations.restore(settings, format_stamp(first.filestorage.stamp))

        assert read_root(settings.datafs) == fs_oracle
        assert snapshot_tree(settings.blobstorage) == blob_oracle
        assert not (settings.blobstorage / "round-2.blob").exists()

    def test_restore_from_snapshot_locations(
        self, settings: Settings, commit: Commit, read_root: ReadRoot
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.snapshot(settings)
        oracle = read_root(settings.datafs)
        commit(settings.datafs, "b", "second")

        operations.restore(settings, snapshot=True)

        assert read_root(settings.datafs) == oracle

    def test_empty_repository_raises(self, settings: Settings) -> None:
        with pytest.raises(RestoreError):
            operations.restore(settings)

    def test_bad_date_is_rejected(self, settings: Settings, commit: Commit) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)

        with pytest.raises(Exception, match="not a valid date"):
            operations.restore(settings, "the-day-before-yesterday")


class TestRestoreConfirmation:
    """Invariant I6: no confirmation, no destruction."""

    def test_refuses_without_a_terminal_and_without_yes(
        self,
        settings: Settings,
        commit: Commit,
        read_root: ReadRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace

        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        commit(settings.datafs, "b", "second")
        untouched = read_root(settings.datafs)

        settings = replace(settings, assume_yes=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(RestoreError, match="--yes"):
            operations.restore(settings)

        assert read_root(settings.datafs) == untouched, "the filestorage was modified"

    def test_declining_at_the_prompt_changes_nothing(
        self,
        settings: Settings,
        commit: Commit,
        read_root: ReadRoot,
        snapshot_tree: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace

        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        commit(settings.datafs, "b", "second")
        fs_before = read_root(settings.datafs)
        blobs_before = snapshot_tree(settings.blobstorage)

        settings = replace(settings, assume_yes=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "no")

        with pytest.raises(RestoreError, match="cancelled"):
            operations.restore(settings)

        assert read_root(settings.datafs) == fs_before
        assert snapshot_tree(settings.blobstorage) == blobs_before

    def test_typing_yes_proceeds(
        self,
        settings: Settings,
        commit: Commit,
        read_root: ReadRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace

        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        oracle = read_root(settings.datafs)
        commit(settings.datafs, "b", "second")

        settings = replace(settings, assume_yes=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "yes")

        operations.restore(settings)

        assert read_root(settings.datafs) == oracle


class TestListing:
    def test_lists_filestorage_and_blob_backups(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)

        lines = operations.list_backups(settings)

        assert any("full" in line for line in lines)
        assert any("blobs" in line for line in lines)

    def test_empty_repository_lists_nothing(self, settings: Settings) -> None:
        assert operations.list_backups(settings) == []


class TestVerify:
    def test_verifies_a_healthy_repository(
        self, settings: Settings, commit: Commit
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)

        operations.verify(settings)


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the clock runs are timed with by a :class:`FakeClock`."""
    fake = FakeClock()
    monkeypatch.setattr(operations, "_clock", fake)
    return fake


@pytest.fixture
def durations(caplog: pytest.LogCaptureFixture) -> Callable[[], list[tuple[int, str]]]:
    """Return a helper listing the duration lines logged so far.

    :returns: a callable giving ``(level, message)`` for every record that
        reports how long a run took.
    """
    caplog.set_level(logging.INFO, logger="zodb_backup.operations")

    def _lines() -> list[tuple[int, str]]:
        return [
            (record.levelno, record.getMessage())
            for record in caplog.records
            if record.name == "zodb_backup.operations"
            and (
                " finished in " in record.getMessage()
                or " failed after " in record.getMessage()
            )
        ]

    return _lines


def _slow(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    clock: FakeClock,
    seconds: float,
    error: Exception | None = None,
) -> None:
    """Make ``module.name`` take ``seconds`` on the fake clock, then run or fail.

    :param monkeypatch: pytest's monkeypatch fixture.
    :param module: the module whose function is replaced.
    :param name: the function's name.
    :param clock: the fake clock to advance.
    :param seconds: how long the call appears to take.
    :param error: raised after the delay instead of calling the real function.
    """
    real = getattr(module, name)

    def wrapper(**kwargs: object) -> object:
        clock.advance(seconds)
        if error is not None:
            raise error
        return real(**kwargs)

    monkeypatch.setattr(module, name, wrapper)


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0.0s"),
            (12.34, "12.3s"),
            (59.94, "59.9s"),
            (59.96, "1m00s"),
            (61, "1m01s"),
            (3599.4, "59m59s"),
            (3599.6, "1h00m00s"),
            (3723, "1h02m03s"),
            (90061, "25h01m01s"),
        ],
    )
    def test_renders(self, seconds: float, expected: str) -> None:
        assert operations.format_duration(seconds) == expected


class TestRunDuration:
    """Every run logs its duration, in total and per phase (issue #3)."""

    def test_a_backup_logs_the_total_and_each_phase(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit(settings.datafs, "a", "first")
        _slow(monkeypatch, operations.repozo, "backup", clock, 60)
        _slow(monkeypatch, operations.blob_module, "backup", clock, 600)

        operations.backup(settings)

        assert durations() == [
            (
                logging.INFO,
                "backup finished in 11m00s "
                "(filestorage 1m00s, blobs 10m00s, retention 0.0s)",
            )
        ]

    def test_a_snapshot_is_labelled_as_such(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
    ) -> None:
        commit(settings.datafs, "a", "first")

        operations.snapshot(settings)

        ((_, message),) = durations()
        assert message.startswith("snapshot finished in ")

    def test_a_failed_run_reports_how_long_it_took_to_give_up(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit(settings.datafs, "a", "first")
        _slow(monkeypatch, operations.repozo, "backup", clock, 60)
        _slow(
            monkeypatch,
            operations.blob_module,
            "backup",
            clock,
            30,
            error=BackupError("rsync died"),
        )

        with pytest.raises(BackupError, match="rsync died"):
            operations.backup(settings)

        assert durations() == [
            (
                logging.ERROR,
                "backup failed after 1m30s in blobs (filestorage 1m00s, blobs 30.0s)",
            )
        ]

    def test_a_filestorage_failure_is_attributed_to_the_filestorage(
        self,
        settings: Settings,
        durations: Callable[[], list[tuple[int, str]]],
    ) -> None:
        """Real clock, real repozo: datafs was never created, so repozo fails."""
        with pytest.raises(BackupError):
            operations.backup(settings)

        ((level, message),) = durations()
        assert level == logging.ERROR
        assert message.startswith("backup failed after ")
        assert " in filestorage (filestorage " in message
        assert "blobs" not in message

    def test_hooks_are_timed_when_configured(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, pre_command="true", post_command="true")
        commit(settings.datafs, "a", "first")

        operations.backup(settings)

        ((_, message),) = durations()
        assert message.endswith(
            "(pre-command 0.0s, filestorage 0.0s, blobs 0.0s, "
            "retention 0.0s, post-command 0.0s)"
        )

    def test_a_failing_post_command_is_reported_as_the_failed_phase(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, post_command="exit 4")
        commit(settings.datafs, "a", "first")

        with pytest.raises(CommandError):
            operations.backup(settings)

        ((level, message),) = durations()
        assert level == logging.ERROR
        assert " in post-command (" in message

    def test_only_blobs_has_no_filestorage_phase(
        self,
        settings: Settings,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
    ) -> None:
        from dataclasses import replace

        settings = replace(settings, only_blobs=True)

        operations.backup(settings)

        ((_, message),) = durations()
        assert message == "backup finished in 0.0s (blobs 0.0s, retention 0.0s)"

    def test_a_restore_logs_the_total_and_each_phase(
        self,
        settings: Settings,
        commit: Commit,
        clock: FakeClock,
        durations: Callable[[], list[tuple[int, str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        _slow(monkeypatch, operations.repozo, "recover", clock, 5)
        _slow(monkeypatch, operations.blob_module, "restore", clock, 125)

        operations.restore(settings)

        assert durations()[-1] == (
            logging.INFO,
            "restore finished in 2m10s (filestorage 5.0s, blobs 2m05s)",
        )

    def test_a_declined_restore_is_not_timed(
        self,
        settings: Settings,
        commit: Commit,
        durations: Callable[[], list[tuple[int, str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing ran, so there is no run to report on."""
        from dataclasses import replace

        commit(settings.datafs, "a", "first")
        operations.backup(settings)
        settings = replace(settings, assume_yes=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(RestoreError):
            operations.restore(settings)

        assert [message for _, message in durations() if "restore" in message] == []
