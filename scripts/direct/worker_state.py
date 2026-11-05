"""Read task settings and lock worker claims."""
from __future__ import annotations

import fcntl
import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents import worker_lifecycle


def worker_record_path(run: Path, name: str, slot: str) -> Path:
    """Find task settings through the fixed staff reference."""
    reference = _worker_reference(run, name)
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if reference and isinstance(value, dict) and cast(dict[str, object], value).get("staff_id") == reference:
            return path
    return run / f"impl_worker_{slot}.json"


def _worker_reference(run: Path, name: str) -> str:
    record = worker_lifecycle.read_worker_state(run / "mesh_roster.json")
    entry = record.get(name)
    return str(cast(dict[str, object], entry).get("staff_id", "")) if isinstance(entry, dict) else ""


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
