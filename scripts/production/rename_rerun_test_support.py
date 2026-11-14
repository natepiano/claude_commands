"""Compare interrupted renames with uninterrupted runs in the same private fixture."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile


@contextmanager
def saved_fixture(root: Path) -> Generator[Callable[[], None]]:
    """Restore the pre-command files at their original absolute paths."""
    with tempfile.TemporaryDirectory(prefix="rename-rerun-") as directory:
        snapshot = Path(directory) / "snapshot"
        _ = shutil.copytree(root, snapshot, symlinks=True)

        def restore() -> None:
            shutil.rmtree(root)
            _ = shutil.copytree(snapshot, root, symlinks=True)

        yield restore


def _files(root: Path, pruned: Callable[[str, str], bool],
           kept: Callable[[str], bool]) -> Iterator[tuple[str, os.DirEntry[str]]]:
    """Yield kept regular files below root by relative path, as Path.rglob finds them.

    A pruned entry and everything below it is left out. Symlinked folders are not followed;
    a symlink to a file is read through.
    """
    folders = [""]
    while folders:
        folder = folders.pop()
        with os.scandir(root / folder) as entries:
            for entry in entries:
                relative = f"{folder}/{entry.name}" if folder else entry.name
                if pruned(relative, entry.name):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders.append(relative)
                elif entry.is_file() and kept(entry.name):
                    yield relative, entry


def final_files(root: Path) -> dict[str, bytes]:
    """Business files, including journals and shutdown records; Git is checked separately.

    Git reflogs and command logs record attempts rather than the resulting state.
    Live session/socket records are supplied by the test, outside the command.
    """
    def pruned(relative: str, name: str) -> bool:
        return name in (".git", "origin.git", "proc", "__pycache__") or relative == "state/sessions"

    def kept(name: str) -> bool:
        return name not in ("herdr.jsonl", "shutdown-calls.jsonl") and not name.endswith(".lock")

    return {relative: Path(entry.path).read_bytes() for relative, entry in _files(root, pruned, kept)}


def file_versions(root: Path) -> dict[str, tuple[bytes, int]]:
    """Read each durable file and its modification time, including Git state."""
    def pruned(_relative: str, name: str) -> bool:
        return name == "__pycache__"

    def kept(name: str) -> bool:
        return not name.endswith(".lock")

    return {relative: (Path(entry.path).read_bytes(), entry.stat().st_mtime_ns)
            for relative, entry in _files(root, pruned, kept)}
