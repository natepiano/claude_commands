"""Measure filesystem capacity and device identity without report dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import cgroup_capture


@dataclass(frozen=True)
class FilesystemUsage:
    used: int
    free: int
    total: int | Literal["unavailable"] = "unavailable"
    device: str = "unavailable"
    device_name: str = "unavailable"


def filesystem_usage(path: str) -> FilesystemUsage:
    stat = os.statvfs(path)
    identity = os.stat(path).st_dev
    device = f"{os.major(identity)}:{os.minor(identity)}"
    return FilesystemUsage(
        used=(stat.f_blocks - stat.f_bfree) * stat.f_frsize,
        free=stat.f_bavail * stat.f_frsize,
        total=stat.f_blocks * stat.f_frsize,
        device=device,
        device_name=cgroup_capture.device_name(device),
    )
