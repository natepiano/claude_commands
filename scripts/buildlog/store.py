"""Where the build log lives and how a record reaches it.

The permanent record is JSON lines, one file per host per month, appended and
never edited: ~/.local/state/buildlog/<host>/<YYYY-MM>.jsonl, with the logs of
failed steps beside it under <host>/logs/<YYYY-MM>/. CI timings sit in ci/ the
same way. index.py derives the SQLite index from these lines and can rebuild it
from nothing. BUILDLOG_DIR moves the root, for tests.

Many processes append at once (every lint step on the machine). Each append
cuts an unfinished tail back to the last newline, writes its line and fsyncs
under an exclusive flock. Even a complete JSON object needs its final newline.
Row-end captures take a shared flock; tail mappings cannot race with a cut.
"""

from __future__ import annotations

import fcntl
import json
import mmap
import os
import socket
import traceback
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

INDEX_NAME = "index.sqlite"
UNKNOWN_IDENTITY = "unknown"
CI_DIR = "ci"
LOGS_DIR = "logs"
ERRORS_NAME = "errors.log"
SYNC_STATUS_NAME = "sync.json"
SYNC_PAUSED_NAME = "sync_paused.json"
CI_POLLED_NAME = "polled.json"
DISK_NAME = "disk.json"
DEFAULT_ROOT_DISPLAY = "~/.local/state/buildlog"
ERRORS_DISPLAY_PATH = f"{DEFAULT_ROOT_DISPLAY}/{ERRORS_NAME}"


def root() -> Path:
    override = os.environ.get("BUILDLOG_DIR")
    return Path(override) if override else Path(DEFAULT_ROOT_DISPLAY).expanduser()


def host_name() -> str:
    """The short host name, as verify.sh's ledger and send.py name machines."""
    return socket.gethostname().split(".")[0]


def utc_iso(epoch: float) -> str:
    """Milliseconds, UTC, with the Z suffix SQLite's date functions accept."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def month(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m")


def host_file(host: str, epoch: float) -> Path:
    return root() / host / f"{month(epoch)}.jsonl"


def sample_file(host: str, epoch: float) -> Path:
    return root() / host / f"samples-{month(epoch)}.jsonl"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("build-log append made no progress")
        view = view[written:]


def last_whole_row_end(handle: BinaryIO) -> int:
    """Find the byte after the last newline, without reading an ordinary row.

    The caller must hold this file's shared or exclusive flock so a tail cut
    cannot shrink the read-only mapping. Only bytes after this boundary can
    be cut. The handle's position changes; its contents do not.
    """
    fd = handle.fileno()
    end = os.lseek(fd, 0, os.SEEK_END)
    if not end:
        return 0
    _ = os.lseek(fd, end - 1, os.SEEK_SET)
    if os.read(fd, 1) == b"\n":
        return end
    with mmap.mmap(fd, end, access=mmap.ACCESS_READ) as contents:
        return contents.rfind(b"\n") + 1


def whole_rows(handle: BinaryIO, start: int) -> Iterator[dict[str, object]]:
    """Read complete object rows from an already locked binary file.

    A start inside a row skips that row. Invalid rows and the unfinished tail
    do not count as records. The caller keeps its shared or exclusive flock.
    """
    end = last_whole_row_end(handle)
    if start >= end:
        return
    if start > 0:
        _ = handle.seek(start - 1)
        if handle.read(1) != b"\n":
            _ = handle.readline(end - start)
    else:
        _ = handle.seek(0)
    while handle.tell() < end:
        line = handle.readline(end - handle.tell())
        if not line:
            break
        try:
            decoded = cast(object, json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(decoded, dict):
            yield cast(dict[str, object], decoded)


def read_last_whole_row_end(path: Path) -> int:
    """Capture a recovery boundary under a shared flock; an absent file is 0."""
    try:
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            return last_whole_row_end(handle)
    except FileNotFoundError:
        return 0


@contextmanager
def locked_append(path: Path) -> Generator[BinaryIO]:
    """Keep tail repair, any recovery scan and publication under one file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        fcntl.flock(fd, fcntl.LOCK_EX)
        end = last_whole_row_end(handle)
        if end != os.fstat(fd).st_size:
            os.ftruncate(fd, end)
        _ = handle.seek(end)
        yield handle
        os.fsync(handle.fileno())


def append_locked(handle: BinaryIO, record: object) -> None:
    """Append one row while the caller owns locked_append's file lock."""
    data = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    _write_all(handle.fileno(), data)


def append_line(path: Path, record: object) -> None:
    with locked_append(path) as handle:
        append_locked(handle, record)


def note_error(context: str) -> None:
    """A recorder never fails its caller; what went wrong goes here instead."""
    try:
        path = root() / ERRORS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            _ = handle.write(f"{utc_iso(datetime.now(UTC).timestamp())} {context}\n{traceback.format_exc()}\n")
    except OSError:
        pass
