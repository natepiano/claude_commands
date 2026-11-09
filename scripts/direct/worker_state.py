"""Read task settings and lock worker claims."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol, TextIO, cast

from scripts.agents import worker_lifecycle
from scripts.lib import process_start
from scripts.production import staff


class WorkerMembership(Protocol):
    """Read the worker that owns a retained task record."""

    def member(self, run: Path, name: str) -> staff.StaffRow | None: ...


@dataclass(frozen=True)
class HeldWorkerClaim:
    """The launcher still holds the claim, or its start is unreadable."""
    pid: int


@dataclass(frozen=True)
class EndedWorkerClaim:
    """The recorded launcher no longer holds the claim."""


@dataclass(frozen=True)
class NoWorkerClaim:
    """No launcher is recorded."""


def publish_worker_claim(path: Path, worker: str) -> None:
    """Publish the launcher identity with its start when available."""
    record: dict[str, object] = {"pid": os.getpid(), "worker": worker}
    started = process_start.process_start(os.getpid())
    if isinstance(started, process_start.Started):
        record["started"] = started.start
    else:
        print(f"anomaly: {path}: launcher start is unavailable", file=sys.stderr)
    worker_lifecycle.WorkerStateFile(path).publish(json.dumps(record))


def read_worker_claim(path: Path) -> HeldWorkerClaim | EndedWorkerClaim | NoWorkerClaim:
    """Compare the recorded launcher start with the live process."""
    try:
        record = worker_lifecycle.read_worker_state(path)
    except worker_lifecycle.WorkerStateUnreadable:
        return NoWorkerClaim()
    pid, started = record.get("pid"), record.get("started")
    if not isinstance(pid, int) or pid <= 0:
        return NoWorkerClaim()
    if isinstance(started, str) and started:
        current = process_start.process_start(pid)
        if isinstance(current, process_start.Unreadable) or (
                isinstance(current, process_start.Started) and current.start == started):
            return HeldWorkerClaim(pid)
    return EndedWorkerClaim()


def worker_status_paths(run: Path, pattern: str = "*_status*") -> list[Path]:
    """Find status files only in the run and its phase folders."""
    return [*run.glob(pattern), *run.glob(f"phases/*/{pattern}")]


def worker_status_path(run: Path, slot: str) -> Path:
    """Select the newest status for a slot, or its unwritten root path."""
    return max(worker_status_paths(run, f"impl_status_{slot}"),
               key=lambda path: path.stat().st_mtime, default=run / f"impl_status_{slot}")


def worker_record_path(run: Path, name: str, slot: str) -> Path:
    """Find task settings through the fixed staff reference."""
    admission = cast(WorkerMembership, cast(object, import_module("scripts.production.worker_admission")))
    try:
        member = admission.member(run, name)
    except ValueError:
        member = None
    reference = str(member.id) if isinstance(member, staff.StaffRow) else ""
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if reference and isinstance(value, dict) and cast(dict[str, object], value).get("staff_id") == reference:
            return path
    return run / f"impl_worker_{slot}.json"


def worker_slot(run: Path, name: str) -> str | None:
    path = worker_record_path(run, name, name)
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if isinstance(value, dict):
        slot = cast(dict[str, object], value).get("slot")
        return slot if isinstance(slot, str) and slot else None
    return None


@contextmanager
def worker_claim_lock(run: Path) -> Generator[TextIO, None, None]:
    with (run / "impl_worker_claim.lock").open("a+") as lock:
        worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_EX)
        yield lock


if __name__ == "__main__":
    with worker_claim_lock(Path(sys.argv[1])):
        pass
