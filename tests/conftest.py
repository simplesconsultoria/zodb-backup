"""Shared pytest fixtures."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
import transaction
import ZODB
import ZODB.FileStorage


#: Fixed point in time the ``stamps`` fixture counts from.
BASE_STAMP = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clean_env() -> dict[str, str]:
    """An empty environment mapping.

    Settings are always built from an explicit mapping in tests, never from
    :data:`os.environ`, so no test can be influenced by the developer's shell or
    by another test.

    :returns: a fresh, empty mapping to populate per test.
    """
    return {}


@pytest.fixture
def datafs(tmp_path: Path) -> Path:
    """Path to a filestorage inside a temporary directory.

    The file does not exist yet; committing to it creates it.

    :param tmp_path: pytest's per-test temporary directory.
    :returns: the path a ``Data.fs`` should be created at.
    """
    folder = tmp_path / "filestorage"
    folder.mkdir()
    return folder / "Data.fs"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """An empty repozo repository directory.

    :param tmp_path: pytest's per-test temporary directory.
    :returns: the directory backups should be written to.
    """
    folder = tmp_path / "backups"
    folder.mkdir()
    return folder


@pytest.fixture
def commit() -> Callable[[Path, str, object], None]:
    """Return a helper that commits one key/value pair to a filestorage.

    Each call opens and closes the database, so the file is left in a state a
    backup can read.

    :returns: a callable taking ``(datafs, key, value)``.
    """

    def _commit(target: Path, key: str, value: object) -> None:
        storage = ZODB.FileStorage.FileStorage(str(target))
        db = ZODB.DB(storage)
        connection = db.open()
        connection.root()[key] = value
        transaction.commit()
        db.close()

    return _commit


@pytest.fixture
def read_root() -> Callable[[Path], dict[str, object]]:
    """Return a helper reading a filestorage root as a plain dict.

    Used as the oracle in restore-fidelity tests: what comes back out must equal
    what was committed at backup time.

    :returns: a callable taking a ``Data.fs`` path and returning its root.
    """

    def _read(target: Path) -> dict[str, object]:
        storage = ZODB.FileStorage.FileStorage(str(target), read_only=True)
        db = ZODB.DB(storage)
        connection = db.open()
        contents = dict(connection.root())
        db.close()
        return contents

    return _read


@pytest.fixture
def blobstorage(tmp_path: Path) -> Path:
    """A small blobstorage tree, shaped the way ZODB lays blobs out.

    :param tmp_path: pytest's per-test temporary directory.
    :returns: the populated blobstorage directory.
    """
    root = tmp_path / "blobstorage"
    for oid in ("0x00", "0x01"):
        folder = root / oid / "0x0a"
        folder.mkdir(parents=True)
        (folder / f"{oid}.blob").write_bytes(f"blob for {oid}".encode())
    (root / ".layout").write_text("bushy")
    return root


@pytest.fixture
def snapshot_tree() -> Callable[[Path], dict[str, bytes]]:
    """Return a helper capturing a directory tree as an oracle.

    The mapping is relative path to file content, which is what restore fidelity
    is compared against: same structure, same bytes.

    :returns: a callable taking a directory and returning its contents.
    """

    def _snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    return _snapshot


@pytest.fixture
def stamps() -> Callable[[int], datetime]:
    """Return a helper producing fixed, well-separated UTC timestamps.

    Tests that care about ordering use these instead of the wall clock, so they
    are deterministic and need no sleeps.

    :returns: a callable mapping an offset in seconds to a datetime.
    """

    def _at(offset: int) -> datetime:
        return BASE_STAMP + timedelta(seconds=offset)

    return _at
