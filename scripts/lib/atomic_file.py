"""Publish complete file content and persist its directory entry."""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO, cast

TEMPORARY_NAME = re.compile(r"\.(?P<target>.+)\.(?P<pid>\d+)\.[^.]+\.tmp", re.DOTALL)


def temporary_target(name: str) -> str | None:
    """Return the destination name of an atomic temporary file."""
    match = TEMPORARY_NAME.fullmatch(name)
    return match.group("target") if match is not None else None


def sync_folder(folder: Path) -> None:
    """Persist the folder's directory entries, including a completed replacement."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_dead_temporary_files(path: Path) -> None:
    """Remove sibling temporary files whose writing process has ended."""
    for sibling in path.parent.iterdir():
        match = TEMPORARY_NAME.fullmatch(sibling.name)
        if match is None or match.group("target") != path.name:
            continue
        try:
            os.kill(int(match.group("pid")), 0)
        except ProcessLookupError:
            sibling.unlink(missing_ok=True)
        except PermissionError:
            continue


@contextmanager
def _flushed_temporary_file(path: Path, content: str | bytes, *, mode: int = 0o600) -> Generator[Path]:
    """Keep a complete, private sibling file until publication finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_dead_temporary_files(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with cast(TextIO | BinaryIO, os.fdopen(
                descriptor, "w" if isinstance(content, str) else "wb",
                encoding="utf-8" if isinstance(content, str) else None)) as stream:
            os.fchmod(stream.fileno(), mode)
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


@contextmanager
def prepared_replacement(path: Path, content: str | bytes, *, mode: int) -> Generator[Path]:
    """Prepare and fsync a sibling; the caller owns replace and folder sync.

    The caller supplies the source mode and checks it again before replacement.
    Keep this context outside publication locks: preparation scans stale files,
    writes and syncs content, and exiting removes an unused temporary file.
    """
    with _flushed_temporary_file(path, content, mode=mode) as temporary:
        yield temporary


def write_text_atomic(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
    """Publish complete text; preserve existing permissions or use the supplied mode for a new file."""
    _write_atomic(path, text, new_file_mode=new_file_mode)


def write_bytes_atomic(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Publish exact bytes with the same durability guarantees as text publication."""
    _write_atomic(path, data, mode=mode)


def _write_atomic(path: Path, content: str | bytes, *, mode: int | None = None,
                  new_file_mode: int | None = None) -> None:
    """Own the single replacement boundary for text and binary evidence."""
    replaced = False
    try:
        with _flushed_temporary_file(path, content, mode=mode if mode is not None else 0o600) as temporary:
            if mode is not None:
                temporary.chmod(mode)
            elif path.exists():
                temporary.chmod(path.stat().st_mode)
            elif new_file_mode is not None:
                temporary.chmod(new_file_mode)
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


def atomic_sample(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        _ = temporary.write_text(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
