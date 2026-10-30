"""Publish a complete text file without exposing a partial write."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Flush a sibling temporary file, then replace the destination."""
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
    finally:
        temporary.unlink(missing_ok=True)
