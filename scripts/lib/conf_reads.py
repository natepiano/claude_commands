"""Record config reads in one private log for a test module."""
from __future__ import annotations

import os

_open = os.open
_write = os.write
_close = os.close

CONF_READ_LOG = "CONF_READ_LOG"
_read_log = os.environ.get(CONF_READ_LOG, "")


def record(stem: str) -> None:
    if not _read_log:
        return
    descriptor = _open(_read_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _ = _write(descriptor, (stem + "\n").encode("utf-8"))
    finally:
        _close(descriptor)


def read_text(path: str | os.PathLike[str]) -> str:
    """Read raw config text and record its file stem."""
    source = path if isinstance(path, str) else path.__fspath__()
    record(os.path.basename(source).removesuffix(".conf"))
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ModuleReadLog:
    """Set the read log for one module and its child processes."""

    def __init__(self, destination: str | os.PathLike[str]) -> None:
        self.destination: str = os.fspath(destination)
        self.previous_log: str = ""

    def __enter__(self) -> None:
        global _read_log
        self.previous_log = _read_log
        _read_log = self.destination
        os.environ[CONF_READ_LOG] = _read_log

    def __exit__(self, *_exception: object) -> None:
        global _read_log
        _read_log = self.previous_log
        if _read_log:
            os.environ[CONF_READ_LOG] = _read_log
        else:
            _ = os.environ.pop(CONF_READ_LOG, None)


def record_reads(destination: str | os.PathLike[str]) -> ModuleReadLog:
    return ModuleReadLog(destination)
