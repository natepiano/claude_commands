"""Publish a complete text file and persist its directory entry."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def sync_folder(folder: Path) -> None:
    """Persist the folder's directory entries, including a completed replacement."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_text_atomic(path: Path, text: str) -> None:
    """Flush a sibling temporary file, replace the destination, and sync its folder."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _ = stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        else:
            mask = os.umask(0)
            _ = os.umask(mask)
            temporary.chmod(0o666 & ~mask)
        os.replace(temporary, path)
        sync_folder(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
