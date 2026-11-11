"""Keep Git index bytes and their timestamp together."""

import os
from pathlib import Path
import shutil


def private_copy(source: Path, destination: Path) -> None:
    """Copy a Git index with the timestamp of its bytes to retain Git's racy-entry check."""
    with open(source, "rb") as original, open(destination, "wb") as copied:
        metadata = os.fstat(original.fileno())
        shutil.copyfileobj(original, copied)
    os.utime(destination, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
