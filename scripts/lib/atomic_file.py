"""Publish a complete text file and persist its directory entry."""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def sync_folder(folder: Path) -> None:
    """Persist the folder's directory entries, including a completed replacement."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_dead_temporary_files(path: Path) -> None:
    """Remove sibling temporary files whose writing process has ended."""
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


@contextmanager
def _flushed_temporary_text(path: Path, text: str) -> Generator[Path]:
    """Keep a complete, private sibling file until publication finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_dead_temporary_files(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _ = stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    """Flush a sibling temporary file, replace the destination, and sync its folder."""
    with _flushed_temporary_text(path, text) as temporary:
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        else:
            mask = os.umask(0)
            _ = os.umask(mask)
            temporary.chmod(0o666 & ~mask)
        os.replace(temporary, path)
        sync_folder(path.parent)


def create_text_atomic(path: Path, text: str) -> bool:
    """Publish private text without replacing a file; return whether it was created."""
    with _flushed_temporary_text(path, text) as temporary:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        sync_folder(path.parent)
        return True
