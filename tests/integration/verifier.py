"""Check a restored filestorage and blobstorage against what the writer promised.

Runs inside the ``plone/plone-zeo`` image against the restored data directly,
with no ZEO server involved, so it verifies the files on disk rather than a
server's cache.

The writer derives every payload and every blob's bytes from the object's key,
so this can reconstruct the expected content independently. That is the point:
the check does not trust anything the backup wrote about itself.

Usage: ``python verifier.py <data-dir> <marker>``
"""

from ZODB.blob import BlobStorage

import sys
import ZODB
import ZODB.FileStorage


def payload(marker: str, index: int) -> str:
    """Content the writer would have stored for a plain object."""
    return f"{marker}-object-{index}"


def blob_bytes(marker: str, index: int) -> bytes:
    """Content the writer would have stored in a blob."""
    return (f"{marker}-blob-{index}-" + "x" * (index % 97)).encode()


def main() -> int:
    data_dir = sys.argv[1]
    marker = sys.argv[2]

    storage = ZODB.FileStorage.FileStorage(
        f"{data_dir}/filestorage/Data.fs", read_only=True
    )
    storage = BlobStorage(f"{data_dir}/blobstorage", storage)
    db = ZODB.DB(storage)
    connection = db.open()
    root = connection.root()

    objects = dict(root.objects)
    blobs = dict(root.blobs)

    problems = []
    if not objects:
        problems.append("no objects were restored at all")

    for index, stored in objects.items():
        if stored != payload(marker, index):
            problems.append(f"object {index}: {stored!r}")

    missing_blobs = set(objects) - set(blobs)
    if missing_blobs:
        problems.append(f"objects without blobs: {sorted(missing_blobs)[:5]}")

    for index, blob in blobs.items():
        try:
            with blob.open("r") as handle:
                content = handle.read()
        except OSError as exc:
            problems.append(f"blob {index} unreadable: {exc}")
            continue
        if content != blob_bytes(marker, index):
            problems.append(f"blob {index} has wrong content")

    db.close()

    print(f"objects={len(objects)} blobs={len(blobs)}", flush=True)
    if problems:
        for problem in problems[:20]:
            print(f"PROBLEM: {problem}", flush=True)
        return 1
    print("OK: every restored object and blob matches", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
