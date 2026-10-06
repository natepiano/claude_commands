"""Where the build log lives and how a record reaches it.

The permanent record is JSON lines, one file per host per month, appended and
never edited: ~/.local/state/buildlog/<host>/<YYYY-MM>.jsonl, with the logs of
failed steps beside it under <host>/logs/<YYYY-MM>/. CI timings sit in ci/ the
same way. index.py derives the SQLite index from these lines and can rebuild it
from nothing. BUILDLOG_DIR moves the root, for tests.

Many processes append at once (every lint step on the machine), so a record is
one write() of one line to a file opened O_APPEND, under an exclusive flock.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import traceback
from datetime import UTC, datetime
from pathlib import Path

INDEX_NAME = "index.sqlite"
CI_DIR = "ci"
LOGS_DIR = "logs"
ERRORS_NAME = "errors.log"
SYNC_STATUS_NAME = "sync.json"
SYNC_PAUSED_NAME = "sync_paused.json"
CI_POLLED_NAME = "polled.json"
DISK_NAME = "disk.json"


def root() -> Path:
    override = os.environ.get("BUILDLOG_DIR")
    return Path(override) if override else Path.home() / ".local/state/buildlog"


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


def append_line(path: Path, record: object) -> None:
    data = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def note_error(context: str) -> None:
    """A recorder never fails its caller; what went wrong goes here instead."""
    try:
        path = root() / ERRORS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            _ = handle.write(f"{utc_iso(datetime.now(UTC).timestamp())} {context}\n{traceback.format_exc()}\n")
    except OSError:
        pass
