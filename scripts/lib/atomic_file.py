"""Publish a complete text file and persist its directory entry."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def sync_folder(folder: Path) -> None:
    """Persist the folder's directory entries, including a completed replacement."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_dead_temporary_files(path: Path) -> None:
    name = re.compile(rf"\.{re.escape(path.name)}\.(\d+)\.[^.]+\.tmp")
    for sibling in path.parent.iterdir():
        match = name.fullmatch(sibling.name)
        if match is None:
            continue
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            sibling.unlink(missing_ok=True)
        except PermissionError:
            continue


def write_text_atomic(path: Path, text: str) -> None:
    """Flush a sibling temporary file, replace the destination, and sync its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _remove_dead_temporary_files(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
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
