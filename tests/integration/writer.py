"""Write objects and blobs to a ZEO server, continuously.

Runs inside the ``plone/plone-zeo`` image, which already ships ZEO and ZODB.
Its job is to keep the database changing while a backup runs, so the backup is
exercised against a moving target rather than a quiescent file.

Every committed object records its own payload, and every blob's content is
derived from its key, so a restore can be checked without the writer's
cooperation: whatever keys survive must have exactly the right payload and blob
bytes.

Usage: ``python writer.py <zeo-host:port> <seconds> <marker>``
"""

from persistent.mapping import PersistentMapping
from ZODB.blob import Blob

import sys
import time
import transaction
import ZEO


def payload(marker: str, index: int) -> str:
    """Content for the plain object at an index."""
    return f"{marker}-object-{index}"


def blob_bytes(marker: str, index: int) -> bytes:
    """Content for the blob at an index, derived only from its identity."""
    return (f"{marker}-blob-{index}-" + "x" * (index % 97)).encode()


def main() -> int:
    host, port = sys.argv[1].split(":")
    duration = float(sys.argv[2])
    marker = sys.argv[3]

    # A ZEO client can only write blobs if it knows where they live.
    # shared_blob_dir mirrors a real Plone deployment, where the backend and the
    # ZEO server mount the same blobstorage volume.
    connection = ZEO.connection(
        (host, int(port)),
        blob_dir="/data/blobstorage",
        shared_blob_dir=True,
    )
    root = connection.root()
    # PersistentMapping, not a plain dict: ZODB cannot detect mutation of a
    # non-persistent object, so with a plain dict only the initial empty value
    # would ever reach disk.
    root.objects = PersistentMapping()
    root.blobs = PersistentMapping()
    transaction.commit()

    deadline = time.time() + duration
    index = 0
    while time.time() < deadline:
        root.objects[index] = payload(marker, index)
        blob = Blob()
        with blob.open("w") as handle:
            handle.write(blob_bytes(marker, index))
        root.blobs[index] = blob
        transaction.commit()
        index += 1
        time.sleep(0.02)

    print(f"committed {index} transactions", flush=True)
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
