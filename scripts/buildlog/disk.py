"""Measure the root filesystem once for the build report's disk table."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
import sweep

import store


class DiskRow(TypedDict):
    label: str
    bytes: int


class DiskSnapshot(TypedDict):
    measured_at: str
    host: str
    rows: list[DiskRow]
    used: int
    free: int
    floor: int | None


FOLDERS: list[tuple[str, str]] = [
    ("~/rust", "~/rust"),
    ("/tmp", "/tmp"),
    ("CI runner 1", "/var/lib/hana-ci/hana-linux-1"),
    ("CI runner 2", "/var/lib/hana-ci/hana-linux-2"),
]


@dataclass(frozen=True)
class FilesystemUsage:
    used: int
    free: int


class InvalidFloor(ValueError):
    """A configured disk floor that cannot be measured in GiB."""


def filesystem_usage(path: str) -> FilesystemUsage:
    stat = os.statvfs(path)
    return FilesystemUsage(
        used=(stat.f_blocks - stat.f_bfree) * stat.f_frsize,
        free=stat.f_bavail * stat.f_frsize,
    )


def read_floor() -> int | None:
    config = os.path.expanduser(os.environ.get(sweep.CONFIG_ENV) or sweep.DEFAULT_CONFIG)
    floor, key = sweep.floor_bytes(sweep.config_values(config), socket.gethostname())
    if floor is None and key is not None:
        raise InvalidFloor(f"{key} in {config} must be a non-negative number of GiB")
    return floor


def measure(folders: list[tuple[str, str]], usage: Callable[[], FilesystemUsage], floor: int | None) -> DiskSnapshot:
    seen: set[sweep.InodeKey] = set()
    rows: list[DiskRow] = [
        {"label": label, "bytes": sweep.directory_blocks(os.path.expanduser(path), seen)}
        for label, path in folders
    ]
    current_usage = usage()
    measured_at = store.utc_iso(time.time())
    return {
        "measured_at": measured_at,
        "host": store.host_name(),
        "rows": rows,
        "used": current_usage.used,
        "free": current_usage.free,
        "floor": floor,
    }


def snapshot() -> DiskSnapshot:
    return measure(FOLDERS, lambda: filesystem_usage("/"), read_floor())


def write_snapshot(snapshot: DiskSnapshot) -> None:
    path = store.root() / store.DISK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(snapshot) + "\n")
    _ = partial.replace(path)


def read_snapshot() -> DiskSnapshot | None:
    try:
        return cast(DiskSnapshot, json.loads((store.root() / store.DISK_NAME).read_text()))
    except (OSError, ValueError):
        return None
