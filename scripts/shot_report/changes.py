"""The dated product and measurement changes used by the screenshot report."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class Change:
    repository: str
    commit: str
    summary: str
    effective_at: datetime
    host_coverage: tuple[str, ...]
    measurement_change: bool = False


# The rollout's effective time is from analysis.md; the later times are the
# documented commit times. Host scope stays local unless the change is Mac-only.
SEED_CHANGES = (
    Change("claude", "b01a299", "Introduce /hana_shot", datetime.fromisoformat("2026-10-06T16:35:00+00:00"), ("natedev",)),
    Change("bevy_brp", "dab07788", "Release extras 0.22.9 with rectangle crops", datetime.fromisoformat("2026-10-06T17:59:57+00:00"), ("natedev",)),
    Change("claude", "7d19a7b", "Sort stored view keys", datetime.fromisoformat("2026-10-06T18:13:34+00:00"), ("natedev",)),
    Change("claude", "ec6703e", "Capture a remote Hana", datetime.fromisoformat("2026-10-06T18:27:33+00:00"), ("natedev",)),
    Change("claude", "4f10e77", "Keep remote Mac Hana visible and awake", datetime.fromisoformat("2026-10-06T18:32:56+00:00"), ("mac",)),
    Change("hana_catalyst", "44cd7b3d4", "Crop screenshots in extras", datetime.fromisoformat("2026-10-06T19:09:22+00:00"), ("natedev",)),
    Change("claude", "efb0eab", "Use /hana_shot in live probes", datetime.fromisoformat("2026-10-06T20:11:55+00:00"), ("natedev",)),
    Change("claude", "e6c96fb", "Record failures and kept-shot evidence", datetime.fromisoformat("2026-10-07T02:10:51+00:00"), ("natedev",), True),
)


def _read(path: Path) -> list[Change]:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a list of changes")
    changes: list[Change] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: invalid change")
        row = cast(dict[str, object], item)
        if any(key not in row for key in ("repository", "commit", "summary", "effective_at", "host_coverage")):
            raise ValueError(f"{path}: invalid change")
        hosts = row.get("host_coverage")
        if not isinstance(hosts, list) or not all(isinstance(host, str) for host in cast(list[object], hosts)):
            raise ValueError(f"{path}: invalid host coverage")
        stamp = datetime.fromisoformat(str(row["effective_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError(f"{path}: effective time needs an offset")
        changes.append(Change(
            str(row["repository"]), str(row["commit"]), str(row["summary"]), stamp,
            tuple(cast(list[str], hosts)), bool(row.get("measurement_change", False)),
        ))
    return sorted(changes, key=lambda change: (change.effective_at, change.repository, change.commit))


def read_changes(path: Path) -> list[Change]:
    """Create the documented seed once; preserve subsequent approved entries."""
    if path.exists():
        return _read(path)
    write_changes(path, SEED_CHANGES)
    return list(SEED_CHANGES)


def write_changes(path: Path, changes: tuple[Change, ...] | list[Change]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            rows = [{**asdict(change), "effective_at": change.effective_at.isoformat()}
                    for change in changes]
            _ = target.write(json.dumps(rows, indent=2) + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def append_change(path: Path, change: Change) -> None:
    changes = read_changes(path)
    if any(old.repository == change.repository and old.commit == change.commit for old in changes):
        return
    changes.append(change)
    changes.sort(key=lambda item: (item.effective_at, item.repository, item.commit))
    write_changes(path, changes)
