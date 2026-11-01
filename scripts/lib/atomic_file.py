"""Publish complete file content and persist its directory entry."""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO, cast


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
def _flushed_temporary_file(path: Path, content: str | bytes) -> Generator[Path]:
    """Keep a complete, private sibling file until publication finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_dead_temporary_files(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with cast(TextIO | BinaryIO, os.fdopen(
                descriptor, "w" if isinstance(content, str) else "wb",
                encoding="utf-8" if isinstance(content, str) else None)) as stream:
            if isinstance(content, str):
                _ = cast(TextIO, stream).write(content)
            else:
                _ = cast(BinaryIO, stream).write(content)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


class AtomicPublicationFailed(OSError):
    """Replacement did not happen; the previous destination remains whole."""


class AtomicDurabilityUnconfirmed(OSError):
    """Complete new content is visible, but directory durability is unconfirmed."""


def write_text_atomic(path: Path, text: str) -> None:
    """Publish whole text; distinguish refusal from visible, unconfirmed replacement."""
    _write_atomic(path, text)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Publish exact bytes with the same durability guarantees as text publication."""
    _write_atomic(path, data)


def _write_atomic(path: Path, content: str | bytes) -> None:
    """Own the single replacement boundary for text and binary evidence."""
    replaced = False
    try:
        with _flushed_temporary_file(path, content) as temporary:
            if path.exists():
                temporary.chmod(path.stat().st_mode)
            else:
                mask = os.umask(0)
                _ = os.umask(mask)
                temporary.chmod(0o666 & ~mask)
            os.replace(temporary, path)
            replaced = True
            sync_folder(path.parent)
    except OSError as error:
        failure = AtomicDurabilityUnconfirmed if replaced else AtomicPublicationFailed
        state = "new content visible; durability unconfirmed" if replaced else "not replaced; previous content retained"
        raise failure(error.errno, f"{state}: {error}", str(path)) from error


def create_text_atomic(path: Path, text: str) -> bool:
    """Publish private text without replacing a file; return whether it was created."""
    with _flushed_temporary_file(path, text) as temporary:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        sync_folder(path.parent)
        return True
