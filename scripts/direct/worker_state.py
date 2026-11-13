"""Read task settings and lock worker claims."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import os
import socket
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol, TextIO, cast

from scripts.agents import worker_lifecycle
from scripts.lib import process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.boot_identity import boot_identity
from scripts.production import staff


class WorkerMembership(Protocol):
    """Read the worker that owns a retained task record."""

    def member(self, run: Path, name: str) -> staff.StaffRow | None: ...


@dataclass(frozen=True)
class HeldWorkerClaim:
    """The launcher holds the claim, or its identity cannot prove absence."""
    pid: int


@dataclass(frozen=True)
class EndedWorkerClaim:
    """The recorded launcher no longer holds the claim."""


@dataclass(frozen=True)
class NoWorkerClaim:
    """No launcher is recorded."""


def _claim_scope() -> str:
    """Identify the host, boot, and PID namespace; return empty when unknown."""
    try:
        if sys.platform == "linux":
            boot = boot_identity()
            namespace = os.readlink("/proc/self/ns/pid")
        elif sys.platform == "darwin":
            boot = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], check=True,
                capture_output=True, text=True, timeout=5).stdout.strip()
            namespace = "host"
        else:
            return ""
        return json.dumps([socket.gethostname(), boot, namespace]) if boot and namespace else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def publish_worker_claim(path: Path, worker: str) -> None:
    """Publish the launcher identity with its start when available."""
    record: dict[str, object] = {"pid": os.getpid(), "worker": worker, "scope": _claim_scope()}
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
    scope = _claim_scope()
    if not scope or record.get("scope") != scope or not isinstance(started, str) or not started:
        return HeldWorkerClaim(pid)
    current = process_start.process_start(pid)
    if isinstance(current, process_start.Unreadable) or (
            isinstance(current, process_start.Started) and current.start == started):
        return HeldWorkerClaim(pid)
    return EndedWorkerClaim()


def worker_status_paths(run: Path, pattern: str = "*_status*") -> list[Path]:
    """Recover dead launchers before returning the run's status files."""
    if any(run.glob("impl_claim_*.json")):
        with worker_claim_lock(run):
            for claim in run.glob("impl_claim_*.json"):
                if not isinstance(read_worker_claim(claim), EndedWorkerClaim):
                    continue
                slot = claim.name.removeprefix("impl_claim_").removesuffix(".json")
                paths = [*run.glob(f"impl_status_{slot}"), *run.glob(f"phases/*/impl_status_{slot}")]
                status = max(paths, key=lambda path: path.stat().st_mtime, default=None)
                if status is not None and status.read_text().strip() == "implementing":
                    _ = subprocess.run(["bash", str(Path(__file__).with_name("board.sh")), "post",
                        str(run), slot, "blocked", "launcher: launcher ended; this worker is down"], check=True)
                    write_text_atomic(status, "error\n")
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
    _ = worker_status_paths(Path(sys.argv[1]))
    with worker_claim_lock(Path(sys.argv[1])):
        pass
