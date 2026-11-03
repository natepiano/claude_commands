"""Compare interrupted renames with uninterrupted runs in the same private fixture."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
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


def final_files(root: Path) -> dict[str, bytes]:
    """Business files, including journals and shutdown records; Git is checked separately.

    Git reflogs and command logs record attempts rather than the resulting state.
    Live session/socket records are supplied by the test, outside the command.
    """
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.is_socket()
            and not any(part in (".git", "origin.git", "proc", "__pycache__")
                        for part in path.relative_to(root).parts)
            and path.relative_to(root).parts[:2] != ("state", "sessions")
            and path.name not in ("herdr.jsonl", "shutdown-calls.jsonl")
            and not path.name.endswith(".lock")}
