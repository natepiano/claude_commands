"""Replace a text file atomically and sync its contents and directory entry."""

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


def write_text(path: Path, content: str) -> None:
    """Replace path with synced UTF-8 text, cleaning up the temporary on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    output = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(output.name)
    try:
        with output:
            _ = output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_folder(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
