"""Publish a complete text file and persist its directory entry."""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


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


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Generator[None]:
    """Lock an inode that is never replaced or unlinked; the OS releases it on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


@contextmanager
def locked_text(path: Path, *, blocking: bool = True) -> Generator[TextIO]:
    """Open state only after acquiring its permanent sidecar lock."""
    with file_lock(path.with_name(path.name + ".lock"), blocking=blocking):
        with path.open("a+", encoding="utf-8") as handle:
            _ = handle.seek(0)
            yield handle


def rewrite_text(handle: TextIO, text: str) -> None:
    """Publish under the caller's sidecar lock and refresh its transaction's reader."""
    path = Path(handle.name)
    write_text_atomic(path, text)
    # Some inbox transactions read again after writing. Keep that reader on the
    # newly published inode, without changing the permanent lock's descriptor.
    with path.open("r+", encoding="utf-8") as current:
        _ = os.dup2(current.fileno(), handle.fileno())
    _ = handle.seek(0)
