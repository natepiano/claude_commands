"""Recorder health, evaluated on its owning machine and exchanged with its host folder.

Only the producer can decide whether an error's pending path still exists.
Snapshots retain those unresolved error times so readers can apply their own
report day's midnight without inspecting another machine's private sources.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

import record
import store

SNAPSHOT_NAME = "recorder-health.json"
SNAPSHOT_MAX_AGE_SECONDS = 2 * 60 * 60
SNAPSHOT_CLOCK_SKEW_SECONDS = 5 * 60


class Machine(StrEnum):
    NATEDEV = "natedev"
    MAC = "Mac"


@dataclass(frozen=True)
class RecorderHealthSnapshot:
    machine: Machine
    generated_at: float
    unfinished_records: int
    error_timestamps: tuple[float, ...]

    def errors_since(self, since: float) -> int:
        return sum(timestamp >= since for timestamp in self.error_timestamps)


@dataclass(frozen=True)
class FreshRecorderHealth:
    snapshot: RecorderHealthSnapshot


@dataclass(frozen=True)
class StaleRecorderHealth:
    snapshot: RecorderHealthSnapshot


@dataclass(frozen=True)
class RecorderHealthUnavailable:
    machine: Machine


type RecorderHealth = FreshRecorderHealth | StaleRecorderHealth | RecorderHealthUnavailable


def machine_for_host(host: str) -> Machine:
    """The historical Mac folder names in the sync allow-list name one machine."""
    if host == Machine.NATEDEV:
        return Machine.NATEDEV
    if host in record.SYNC_HOSTS:
        return Machine.MAC
    raise ValueError(f"unknown build-log host: {host}")


def snapshot_path(host: str) -> Path:
    return store.root() / host / SNAPSHOT_NAME


def calculate_snapshot() -> RecorderHealthSnapshot:
    machine = machine_for_host(store.host_name())
    pending = 0
    for root in (record.pending_launches_root(), record.pending_steps_root()):
        try:
            paths = list(root.iterdir())
        except FileNotFoundError:
            continue
        pending += sum(path.suffix == ".json" and path.is_file() for path in paths)
    errors: list[float] = []
    try:
        with (store.root() / store.ERRORS_NAME).open() as handle:
            for line in handle:
                timestamp, separator, context = line.rstrip("\n").partition(" ")
                if not separator:
                    continue
                try:
                    instant = datetime.fromisoformat(timestamp)
                except ValueError:
                    continue
                if instant.tzinfo is None:
                    continue
                if context.startswith("pending record ") and not Path(context.removeprefix("pending record ")).is_file():
                    continue
                errors.append(instant.timestamp())
    except FileNotFoundError:
        pass
    return RecorderHealthSnapshot(machine, time.time(), pending, tuple(errors))


def write_snapshot() -> bool:
    """Keep the previous snapshot if calculation or atomic publication fails."""
    try:
        root = store.root()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "recorder-health.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            snapshot = calculate_snapshot()
            path = snapshot_path(store.host_name())
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "v": 1,
                "machine": snapshot.machine.value,
                "generated_at": snapshot.generated_at,
                "unfinished_records": snapshot.unfinished_records,
                "error_timestamps": snapshot.error_timestamps,
            }
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=SNAPSHOT_NAME + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    _ = handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 -- a failed refresh must preserve the last snapshot and be visible
        store.note_error("recorder health snapshot")
        return False


def _read_snapshot(host: str) -> RecorderHealthSnapshot:
    value = cast(object, json.loads(snapshot_path(host).read_text()))
    if not isinstance(value, dict):
        raise ValueError("recorder health snapshot is not an object")
    fields = cast(dict[str, object], value)
    machine = machine_for_host(host)
    generated = fields.get("generated_at")
    pending = fields.get("unfinished_records")
    errors = fields.get("error_timestamps")
    if (fields.get("v") != 1 or fields.get("machine") != machine.value
            or not isinstance(generated, int | float) or isinstance(generated, bool)
            or not math.isfinite(generated) or generated < 0
            or not isinstance(pending, int) or isinstance(pending, bool) or pending < 0
            or not isinstance(errors, list)):
        raise ValueError("invalid recorder health snapshot fields")
    timestamps: list[float] = []
    for timestamp in cast(list[object], errors):
        if (not isinstance(timestamp, int | float) or isinstance(timestamp, bool)
                or not math.isfinite(timestamp) or timestamp < 0):
            raise ValueError("invalid recorder error timestamp")
        timestamps.append(float(timestamp))
    return RecorderHealthSnapshot(machine, float(generated), pending, tuple(timestamps))


def read_snapshots() -> tuple[RecorderHealth, ...]:
    """Read one state per machine; the newest usable Mac alias wins."""
    now = time.time()
    latest: dict[Machine, RecorderHealthSnapshot] = {}
    for host in record.SYNC_HOSTS:
        try:
            snapshot = _read_snapshot(host)
        except (OSError, ValueError):
            continue
        if snapshot.generated_at > now + SNAPSHOT_CLOCK_SKEW_SECONDS:
            continue
        previous = latest.get(snapshot.machine)
        if previous is None or snapshot.generated_at > previous.generated_at:
            latest[snapshot.machine] = snapshot
    states: list[RecorderHealth] = []
    for machine in Machine:
        snapshot = latest.get(machine)
        if snapshot is None:
            states.append(RecorderHealthUnavailable(machine))
        elif now - snapshot.generated_at > SNAPSHOT_MAX_AGE_SECONDS:
            states.append(StaleRecorderHealth(snapshot))
        else:
            states.append(FreshRecorderHealth(snapshot))
    return tuple(states)


if __name__ == "__main__":
    raise SystemExit(0 if write_snapshot() else 1)
