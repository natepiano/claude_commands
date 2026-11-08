"""Read Linux directory events without losing watch replacement or overflow events."""
from __future__ import annotations

import ctypes
import errno
from dataclasses import dataclass
import os
from pathlib import Path
import struct
from typing import Callable, cast

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
DIRECTORY_CHANGES = (IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
                     | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)
WATCH_REMOVED = IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED


@dataclass(frozen=True)
class Event:
    watch: int
    mask: int
    name: str


def open_watch() -> int:
    library = ctypes.CDLL(None, use_errno=True)
    initialize = cast(Callable[[int], int], library.inotify_init1)
    descriptor = initialize(os.O_CLOEXEC | os.O_NONBLOCK)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "cannot open directory watch")
    return descriptor


def add_watch(descriptor: int, path: Path, mask: int = DIRECTORY_CHANGES) -> int:
    library = ctypes.CDLL(None, use_errno=True)
    add = cast(Callable[[int, bytes, int], int], library.inotify_add_watch)
    watch = add(descriptor, os.fsencode(path), mask)
    if watch < 0:
        raise OSError(ctypes.get_errno(), f"cannot watch {path}")
    return watch


def remove_watch(descriptor: int, watch: int) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    remove = cast(Callable[[int, int], int], library.inotify_rm_watch)
    if remove(descriptor, watch) < 0:
        error = ctypes.get_errno()
        # The kernel can remove a watch before reconciliation.
        if error != errno.EINVAL:
            raise OSError(error, "cannot remove directory watch")


def read_events(descriptor: int) -> tuple[Event, ...]:
    """Read one batch of directory events."""
    try:
        data = os.read(descriptor, 65536)
    except BlockingIOError:
        return ()
    result: list[Event] = []
    offset = 0
    while offset + 16 <= len(data):
        watch, mask, cookie, size = struct.unpack_from("iIII", data, offset)
        del cookie
        name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b"\0", 1)[0])
        result.append(Event(watch, mask, name))
        offset += 16 + size
    return tuple(result)


def events(descriptor: int) -> tuple[Event, ...]:
    result: list[Event] = []
    while batch := read_events(descriptor):
        result.extend(batch)
    return tuple(result)
