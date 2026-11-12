"""Publish cloud gate state beside the module counter."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal, cast

from scripts.build_queue import snapshot_codec as codec
from scripts.lib.atomic_file import atomic_sample

__all__ = ["CloudState", "CloudProgress", "NoCloudProgress", "publish", "read"]

CloudState = Literal["requesting", "setup", "running", "uploading"]


@dataclass(frozen=True)
class CloudProgress:
    state: CloudState
    size_vcpus: int
    failed_modules: int
    updated_at: float
    stage: str = ""
    region: str = ""
    zone: str = ""


@dataclass(frozen=True)
class NoCloudProgress:
    reason: Literal["missing", "unreadable"] = "missing"


def publish(sidecar: Path, progress: CloudProgress) -> None:
    atomic_sample(Path(str(sidecar) + ".cloud.json"),
        json.dumps(codec.snapshot_value(progress), separators=(",", ":"), allow_nan=False))


def read(sidecar: Path) -> CloudProgress | NoCloudProgress:
    try:
        return cast(CloudProgress, codec.parse_snapshot_value(
            cast(object, json.loads(Path(str(sidecar) + ".cloud.json").read_text())), CloudProgress))
    except FileNotFoundError:
        return NoCloudProgress("missing")
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return NoCloudProgress("unreadable")
