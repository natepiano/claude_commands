#!/usr/bin/env python3
"""Read run folders and the open run in each director's staff row.

Staff supplies each run's current owner. Run records keep task history.
Run folders can use the current unit name or the former delegate name.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cache, lru_cache
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from scripts.production import staff

RUN_TEMP_NAME = "tmp"
# SCRATCH_ROOT also selects the parent folder for temporary unit runs.
RUN_ROOT = Path(os.environ.get("SCRATCH_ROOT") or "/tmp/claude/scratch").parent / "unit"
FORMER_RUN_ROOT = RUN_ROOT.parent / "delegate"
ASTRA_RUN_ROOT = Path(os.environ.get("ASTRA_REVIEW_DIR") or "/tmp/claude/astra")
# Notifier instances a unit run once had; conversation_pause names them "status reports".
INSTANCE_PREFIXES = ("unit-", "delegate-")
# A run's shared liveness log, beaten every 60 s per worker while one runs.
HEARTBEAT = "heartbeat.log"
# A run whose log moved within this window is live. Ten beats of slack, because
# worker cleanup that reads a live run as dead removes its working workers.
LIVE_HEARTBEAT_SECS = 600
# Names the run's history, whose first line is the `run_started` event `start-run` writes.
STATE_FILE = "progress_history_state.json"
OWNER_FIELD = "owner_staff_id"
# Every run's durable history: runs/, review_turns/, archives. The former name is read until
# `unit_runs.py move-history` moves the folder; a test or a scratch run names its own with
# UNIT_HISTORY_DIR. Both sit under the state folder, read at each call, since a caller may change HOME.
HISTORY_NAME = "unit-history"
FORMER_HISTORY_NAME = "plan-delegate"
# Run folders the usage backfill keeps after their runs end, under the same state folder.
RUN_ARCHIVE_NAME = "usage-model/unit-archive"
FORMER_RUN_ARCHIVE_NAME = "usage-model/delegate-archive"
# The same choice for a script sent over ssh, which cannot import this module: it sets `root`
# from the argument, else the override, else the new name once it exists, else the former one.
REMOTE_HISTORY_ROOT = f"""state = Path.home() / '.local/state'
new, former = state / '{HISTORY_NAME}', state / '{FORMER_HISTORY_NAME}'
root = Path(sys.argv[1] or os.environ.get('UNIT_HISTORY_DIR') or os.environ.get('PLAN_DELEGATE_HISTORY_DIR')
            or (former if not new.exists() and former.exists() else new)).expanduser()
"""


@dataclass(frozen=True)
class RunOwnerUnrecorded:
    reason: str


def run_owner(fields: Mapping[str, object], record: staff.StaffSnapshot) -> staff.StaffRow | RunOwnerUnrecorded:
    """Resolve the run owner through its fixed staff reference."""
    from scripts.production import staff

    reference = fields.get(OWNER_FIELD)
    if isinstance(reference, str) and reference:
        row = record.by_id(staff.StaffId(reference))
        if row is None or row.kind not in ("unit", "showrunner"):
            raise staff.StaffUnreadable(f"run owner {reference}: unit staff row is unavailable")
        return row
    return RunOwnerUnrecorded("run has no unit staff owner")


def environ(name: str) -> str:
    """The run variable `UNIT_<name>`, else the name a run started before the rename exports.

    Such a run's launcher exports `PLAN_DELEGATE_<name>`, and the run folder as
    `PLAN_DELEGATE_BOARD_DIR`. Read while such a run can still be open.
    """
    former = "BOARD_DIR" if name == "RUN_DIR" else name
    return os.environ.get(f"UNIT_{name}") or os.environ.get(f"PLAN_DELEGATE_{former}", "")


def run_roots(override: str | Path | None = None) -> tuple[Path, ...]:
    """The folders that hold run folders, the new name first."""
    return (Path(override),) if override else (RUN_ROOT, FORMER_RUN_ROOT)


def is_unit_run(folder: Path) -> bool:
    """Test whether the folder is under a unit run root."""
    folder = folder.resolve()
    return any(folder.is_relative_to(root.resolve()) for root in run_roots())


def run_temp_path(run: Path) -> Path:
    """Name the temporary folder that shares the run's lifetime."""
    return run / RUN_TEMP_NAME










class RunStaffAtUse(Enum):
    LOCAL = "read the local staff record"


@dataclass(frozen=True)
class LauncherRunDirectories:
    """Share worker resolution and parent scans within one display."""

    by_worker: dict[staff.StaffId, tuple[Path, ...]]
    by_session: dict[str, list[str]]




def open_run(session_id: str, *,
             record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL) -> Path | None:
    """Read the open run from the row for the current session."""
    from scripts.production import staff

    row = (staff.local_by_current_session(session_id) if isinstance(record, RunStaffAtUse)
           else record.by_current_session(session_id))
    return row.run.path if row is not None and isinstance(row.run, staff.OpenRun) else None


def run_is_open(folder: Path, *, record: staff.StaffSnapshot | None = None) -> bool:
    """Read a unit's staff pointer or a helper's unfinished state."""
    from scripts.production import staff

    folder = folder.resolve()
    try:
        raw = cast(object, json.loads((folder / STATE_FILE).read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    fields = cast(dict[str, object], raw)
    if not is_unit_run(folder):
        return "status" not in fields
    record = staff.local_snapshot() if record is None else record
    reference = fields.get(OWNER_FIELD)
    owner = (record.by_id(staff.StaffId(reference)) if isinstance(reference, str) else
             next((row for row in record.rows if isinstance(row.run, staff.OpenRun)
                   and row.run.path.resolve() == folder), None))
    return (isinstance(owner, staff.StaffRow) and isinstance(owner.run, staff.OpenRun)
            and owner.run.path.resolve() == folder)


def history_root() -> Path:
    """The history folder: the override, else the new name once it exists, else the former one."""
    configured = environ("HISTORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return history_root_in(state_dir())


def state_dir() -> Path:
    return Path.home() / ".local/state"


def history_root_in(state: Path) -> Path:
    """The history folder inside one state folder, as history_root picks it, for a reader given that folder."""
    return moved_folder(state / HISTORY_NAME, state / FORMER_HISTORY_NAME)


def run_archive() -> Path:
    """The usage backfill's run archive under its new name once it exists, else the former one."""
    return moved_folder(state_dir() / RUN_ARCHIVE_NAME, state_dir() / FORMER_RUN_ARCHIVE_NAME)


@cache
def report_anomaly(message: str) -> None:
    """Print each run anomaly once per process."""
    print(message, file=sys.stderr)


def director(run: Path, *, dependencies: Callable[[Path], None] | None = None,
             record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL) -> str:
    """Read the run owner's current session from staff."""
    from scripts.production import staff

    if dependencies is not None:
        dependencies(staff.record_path())
        dependencies(run / STATE_FILE)
    admission = import_module("scripts.production.worker_admission")
    missing_owner = cast(type[ValueError], admission.RunOwnerUnavailable)
    try:
        find_parent = cast(Callable[..., staff.StaffRow], admission.parent)
        return find_parent(staff.local_snapshot() if isinstance(record, RunStaffAtUse) else record, run=run).current_session
    except missing_owner as error:
        if _has_resumable_workers(run, record=record):
            report_anomaly(f"anomaly: {run}: {error}")
        return ""
    except (OSError, ValueError) as error:
        report_anomaly(f"anomaly: {run}: {error}")
        return ""


def run_folders(root_override: str | Path | None = None) -> list[Path]:
    """List unique run folders in root and name order."""
    folders: dict[Path, Path] = {}
    for root in run_roots(root_override):
        try:
            candidates = sorted(root.iterdir())
        except OSError:
            continue
        for folder in candidates:
            if folder.is_dir():
                _ = folders.setdefault(folder.resolve(), folder)
    return list(folders.values())


def _has_resumable_workers(run: Path, *, record: staff.StaffSnapshot | RunStaffAtUse) -> bool:
    from scripts.direct import worker_state
    from scripts.production import staff

    def read_record(path: Path) -> dict[str, object]:
        try:
            raw = cast(object, json.loads(path.read_text()))
        except (OSError, UnicodeError, ValueError):
            return {}
        return cast(dict[str, object], raw) if isinstance(raw, dict) else {}

    state = read_record(run / STATE_FILE)
    if state.get("status") == "ended":
        return False
    names: set[str] = set()
    for path in worker_state.worker_status_paths(run):
        names.add(path.name.split("_status", 1)[0])
        try:
            if path.read_text().strip() in ("implementing", "running"):
                return True
        except OSError:
            continue
    if not names:
        return False
    snapshot = staff.local_snapshot() if isinstance(record, RunStaffAtUse) else record
    recorder = import_module("scripts.direct.progress_history")
    read_dispatch = cast(Callable[[staff.StaffId, Path], object], recorder.active_dispatch)
    active_dispatch_type = cast(type[object], recorder.ActiveDispatch)
    return any(isinstance(read_dispatch(row.id, run), active_dispatch_type)
               for row in snapshot.find(kind="worker") if row.name in names)


def held_by_session(root_override: str | Path | None = None, *,
                    record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL,
                    additional_roots: Sequence[Path] = ()) -> dict[str, list[str]]:
    """Read each run's director once, in root and name order."""
    found: dict[str, list[str]] = {}
    folders: dict[Path, Path] = {}
    for root in (root_override, *additional_roots):
        for folder in run_folders(root):
            _ = folders.setdefault(folder.resolve(), folder)
    for folder in folders.values():
        if (folder / STATE_FILE).is_file():
            found.setdefault(director(folder, record=record), []).append(str(folder))
    return found


@lru_cache(maxsize=4096)
def _real_path(folder: str) -> Path:
    """Resolve a scanned run folder once per process; one board read compares each folder many times."""
    return Path(folder).resolve()


def held(session_id: str, root_override: str | Path | None = None,
         *,
         scanned: dict[str, list[str]] | None = None,
         record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL) -> list[str]:
    """List directed folders with the verified open run first."""
    if not session_id:
        return []
    folders = held_by_session(root_override, record=record) if scanned is None else scanned
    target = open_run(session_id, record=record)
    runs: list[str] = []
    if target is not None:
        resolved = target.resolve()
        owner = next((session for session, paths in folders.items()
                      if any(_real_path(path) == resolved for path in paths)), None)
        if owner is None:
            owner = director(target, record=record)
            if owner == session_id:
                folders.setdefault(owner, []).append(str(target))
        if owner == session_id:
            runs.append(str(target))
    seen = {_real_path(run) for run in runs}
    for folder in folders.get(session_id, ()):
        resolved = _real_path(folder)
        if resolved not in seen:
            seen.add(resolved)
            runs.append(folder)
    return runs


def live_run_folders(now: float, roots: Sequence[Path] | None = None) -> list[Path]:
    """Run folders under either name whose heartbeat moved in the live window."""
    live: list[Path] = []
    for root in run_roots() if roots is None else roots:
        for beat in sorted(root.glob(f"*/{HEARTBEAT}")):
            try:
                if now - beat.stat().st_mtime < LIVE_HEARTBEAT_SECS:
                    live.append(beat.parent)
            except OSError:
                continue
    return live


def moved_folder(new: Path, former: Path) -> Path:
    """The new name once it exists, else the former one while only it exists."""
    return former if not new.exists() and former.exists() else new


def move_history(now: float, state: Path | None = None) -> int:
    """Move the history folder and the run archive to their new names once, while no unit run writes.

    The usage collector reads the moved files again from the start under their new paths; every
    history row it writes is keyed by its event, so the second read adds nothing.
    """
    state = state or state_dir()
    names = ((FORMER_HISTORY_NAME, HISTORY_NAME), (FORMER_RUN_ARCHIVE_NAME, RUN_ARCHIVE_NAME))
    moves = [(state / former, state / new) for former, new in names if (state / former).exists() and not (state / new).exists()]
    if not moves:
        print("nothing to move")
        return 0
    live = live_run_folders(now)
    if live:
        print(f"not moved: {len(live)} unit run(s) are live and write history", file=sys.stderr)
        for run in live:
            print(f"  {run}", file=sys.stderr)
        return 1
    for former, new in moves:
        _ = former.rename(new)
        print(f"moved {former} to {new}")
    return 0


def _held_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unit_runs.py held", description="Print each run folder a session directs.")
    _ = parser.add_argument("session_id")
    _ = parser.add_argument("--root", default="", help="one run root in place of both run-folder names")
    args = parser.parse_args(argv)
    for run in held(cast(str, args.session_id), cast(str, args.root) or None):
        print(run)
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "held":
        return _held_main(argv[1:])
    if argv == ["history-root"]:
        print(history_root())
        return 0
    if argv == ["move-history"]:
        return move_history(time.time())
    print("usage: unit_runs.py history-root | move-history | held <session id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
