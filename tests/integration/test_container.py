"""Integration tests: the container image against a live ZEO server.

Opt-in, and excluded from the default test run. ``make test-integration`` runs
them; they need a working Docker daemon and pull ``plone/plone-zeo``.

What makes these worth their runtime is that they are the only tests where a
backup is taken from a database that is being written to at that moment, which is
the situation the tool exists for. The restored data is checked by
``verifier.py``, which reconstructs every expected value from the object's key
rather than trusting anything the backup recorded about itself.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import shutil
import subprocess
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.test.yml"
MARKER = "run1"

#: Long enough for the writer to still be committing when the backup starts.
WRITER_SECONDS = 20
#: How far into the writer's run the backup is taken.
BACKUP_DELAY = 7


def docker_available() -> bool:
    """Whether a usable Docker daemon is present."""
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a docker compose command against the test stack.

    :param args: arguments following ``docker compose -f <file>``.
    :param check: raise if the command fails.
    :returns: the completed process, with output captured.
    """
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=check,
        timeout=600,
    )


def wait_for_healthy(container: str, attempts: int = 45) -> None:
    """Block until a container reports healthy.

    :param container: container name.
    :param attempts: how many two-second polls to make.
    :raises AssertionError: if it never becomes healthy.
    """
    for _ in range(attempts):
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip() == "healthy":
            return
        time.sleep(2)
    raise AssertionError(f"{container} never became healthy")


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """Bring up a clean stack for the module, and tear it down afterwards."""
    compose("down", "-v", check=False)
    compose("build", "backup")
    compose("up", "-d", "zeo")
    wait_for_healthy("zodb-backup-test-zeo-1")
    # Docker creates named volumes owned by root; the containers run as uid 500.
    compose("run", "--rm", "init")
    yield
    compose("down", "-v", check=False)


class TestLiveBackup:
    """The core of this suite: a backup of a database being written to."""

    def test_backup_during_writes_restores_intact(self, stack: None) -> None:
        """Backup mid-write, restore elsewhere, verify every object and blob.

        This is the empirical check that backing up a live FileStorage is safe,
        and that copying the filestorage before the blobs never leaves an object
        whose blob is missing.
        """
        compose("run", "--rm", "-d", "writer", "zeo:8100", str(WRITER_SECONDS), MARKER)
        time.sleep(BACKUP_DELAY)

        backup = compose("run", "--rm", "backup", "backup")
        assert "filestorage:" in backup.stdout

        # Let the writer finish so the restore is compared against a settled DB.
        time.sleep(WRITER_SECONDS - BACKUP_DELAY + 3)

        compose("run", "--rm", "restore")
        verified = compose("run", "--rm", "verifier")

        assert "OK: every restored object and blob matches" in verified.stdout
        assert "objects=0" not in verified.stdout, "nothing was restored"

    def test_restored_database_has_no_blobless_objects(self, stack: None) -> None:
        """Every restored object must have its blob on disk.

        The verifier reports this as a problem; this asserts on it separately
        because it is the specific failure that backing blobs up *before* the
        filestorage would cause.
        """
        verified = compose("run", "--rm", "verifier", check=False)

        assert "objects without blobs" not in verified.stdout


class TestImageContract:
    def test_runs_as_a_non_root_user(self, stack: None) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "id",
                "ghcr.io/simplesconsultoria/zodb-backup:dev",
                "-u",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() != "0"

    def test_backup_works_from_a_read_only_data_mount(self, stack: None) -> None:
        """Invariant I7, at the container level.

        The compose file mounts the data volume read-only into the backup
        service, so a successful run proves the source is never written to.
        """
        result = compose("run", "--rm", "backup", "backup")

        assert result.returncode == 0

    def test_ships_rsync(self, stack: None) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "rsync",
                "ghcr.io/simplesconsultoria/zodb-backup:dev",
                "--version",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert "rsync" in result.stdout

    def test_reports_its_version(self, stack: None) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "ghcr.io/simplesconsultoria/zodb-backup:dev",
                "version",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip()


class TestRestoreRunbook:
    """The documented recovery procedure, executed."""

    def test_stop_restore_start(self, stack: None) -> None:
        """Stop the server, restore in place, bring it back, read the data.

        This is what an operator actually does in an incident, so it is worth
        running rather than only documenting.
        """
        compose("run", "--rm", "-d", "writer", "zeo:8100", "6", MARKER)
        time.sleep(9)
        compose("run", "--rm", "backup", "backup")

        # Take the database offline, as the runbook instructs.
        compose("stop", "zeo")

        restored = compose("run", "--rm", "restore")
        assert restored.returncode == 0

        compose("start", "zeo")
        wait_for_healthy("zodb-backup-test-zeo-1")

        verified = compose("run", "--rm", "verifier")
        assert "OK: every restored object and blob matches" in verified.stdout


class TestRetentionInTheContainer:
    def test_keep_removes_older_full_backups(self, stack: None) -> None:
        """KEEP is honoured by the packaged tool, not just the library."""
        for _ in range(4):
            compose("run", "--rm", "-e", "FULL=true", "backup", "backup")

        listed = compose("run", "--rm", "backup", "list")
        fulls = [line for line in listed.stdout.splitlines() if " full " in line]

        assert len(fulls) <= 2, listed.stdout
