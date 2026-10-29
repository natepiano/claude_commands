"""Read worker state after converting names from earlier runs."""
from __future__ import annotations

import fcntl
import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, TypedDict, cast


class WorkerLaunchRecord(TypedDict, total=False):
    name: str
    slot: str
    family: str
    model: str
    worker_id: str
    worker: str
    pid: int
    turn_id: str
    seat_id: str
    seat: str
    role: str


def _move_old_name(previous: Path, current: Path) -> Path:
    if not current.exists() and previous.exists():
        try:
            current.hardlink_to(previous)
        except (FileNotFoundError, FileExistsError):
            # Another reader completed the same conversion.
            pass
        else:
            previous.unlink(missing_ok=True)
    return current


def _link_live_name(previous: Path, current: Path) -> Path:
    """Keep both paths on one inode for launchers running across the update."""
    try:
        current.hardlink_to(previous)
    except (FileNotFoundError, FileExistsError):
        pass
    return current


def worker_ledger(run: Path) -> Path:
    """Read the worker ledger while old launchers can still append to it."""
    return _link_live_name(run / "seats", run / "workers")


def _convert_record(path: Path) -> None:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return
    if not isinstance(value, dict):
        return
    record = cast(WorkerLaunchRecord, cast(object, value))
    changed = False
    if "seat_id" in record:
        if "worker_id" not in record:
            record["worker_id"] = record["seat_id"]
        del record["seat_id"]
        changed = True
    if "seat" in record:
        if "worker" not in record:
            record["worker"] = record["seat"]
        del record["seat"]
        changed = True
    if changed:
        scratch = path.with_suffix(".tmp")
        _ = scratch.write_text(json.dumps(record), encoding="utf-8")
        _ = scratch.replace(path)


def worker_record_path(run: Path, name: str, slot: str, *, role: str = "") -> Path:
    """Find a worker by its address; a later assignment never changes its identity path."""
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and cast(dict[str, object], value).get("name") == name:
            return path
    try:
        roster = cast(dict[str, object], json.loads((run / "mesh_roster.json").read_text(encoding="utf-8")))
        record = roster.get(name)
        if not role and isinstance(record, dict):
            stored_role = cast(dict[str, object], record).get("role")
            role = stored_role if isinstance(stored_role, str) else ""
    except (OSError, ValueError):
        pass
    key = "fix" if role == "fix" else slot
    candidate = run / f"impl_worker_{key}.json"
    if candidate.exists():
        # Slot paths are retained for earlier callers. An occupied slot belongs
        # to another address, so give the new worker a separate stable key.
        return run / f"impl_worker_name_{name.encode().hex()}.json"
    return candidate


def worker_slot(run: Path, name: str) -> str | None:
    """The team role the named worker's durable identity was recorded under; None when no record names it."""
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        record = cast(WorkerLaunchRecord, cast(object, value))
        slot = record.get("slot")
        if record.get("name") == name and isinstance(slot, str) and slot:
            return slot
    return None


def migrate_worker_records(run: Path) -> None:
    """Convert durable identity names and claim fields under the launch lock."""
    _ = worker_ledger(run)
    for previous in run.glob("impl_seat_*.json"):
        _ = _move_old_name(previous, previous.with_name(previous.name.replace("impl_seat_", "impl_worker_", 1)))
    for pattern in ("impl_worker_*.json", "impl_claim_*.json"):
        for path in run.glob(pattern):
            _convert_record(path)


@contextmanager
def worker_claim_lock(run: Path) -> Generator[TextIO, None, None]:
    """Keep the old lock path so an earlier launcher can reopen the same inode."""
    path = _link_live_name(run / "impl_seat_claim.lock", run / "impl_worker_claim.lock")
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        migrate_worker_records(run)
        yield lock


if __name__ == "__main__":
    with worker_claim_lock(Path(sys.argv[1])):
        pass
