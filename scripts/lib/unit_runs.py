#!/usr/bin/env python3
"""Where unit runs keep their folders, under the new name and the former one.

A /unit:direct run keeps its files in `/tmp/claude/unit/<run id>/` and marks its Claude session
in `/tmp/claude/unit/active/<session id>`.
Runs started before 2026-10-15 used `delegate` in both places and keep their folders
until they end, so every reader takes both names. The former name is read until a removal
step, on or after 2026-10-22, drops it; that step runs only while `unit_runs.py removal-check`
passes, since a run started before the rename may stay open past that date.

A test points a reader at its own folder with an override, which replaces both names.

Staff supplies each run's current owner. Run records keep task history.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, NamedTuple, cast

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
ACTIVE_MARKER_FILE = "active_marker"
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
    """Resolve a fixed owner first; older runs can still name their director."""
    from scripts.production import staff

    reference = fields.get(OWNER_FIELD)
    if isinstance(reference, str) and reference:
        row = record.by_id(staff.StaffId(reference))
        if row is None or row.kind not in ("unit", "showrunner"):
            raise staff.StaffUnreadable(f"run owner {reference}: unit staff row is unavailable")
        return row
    identity = fields.get("main_agent")
    agent = cast(dict[str, object], identity) if isinstance(identity, dict) else {}
    reference = agent.get("session_id") or fields.get("owner_session") or fields.get("director_session")
    row = record.owner_of(reference) if isinstance(reference, str) and reference else None
    return row if row is not None and row.kind in ("unit", "showrunner") else RunOwnerUnrecorded("run has no unit staff owner")


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


def run_temp_path(run: Path) -> Path:
    """Name the temporary folder that shares the run's lifetime."""
    return run / RUN_TEMP_NAME










class RunStaffAtUse(Enum):
    LOCAL = "read the local staff record"




@contextmanager
def marker_lock(path: Path) -> Generator[None]:
    """Serialize marker publication and removal outside the marker folder."""
    directory = path.parent.resolve()
    lock_path = directory.with_name(directory.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def activate(run: Path, session_id: str, active_override: str | Path | None = None) -> None:
    """Write the current marker and remove the former marker."""
    from scripts.lib.atomic_file import write_text_atomic  # The Mac usage reader ships this module without atomic_file.

    current, *former = active_dirs(active_override)
    current.mkdir(parents=True, exist_ok=True)
    path = current / session_id
    with marker_lock(path):
        write_text_atomic(run / ACTIVE_MARKER_FILE, f"{path.resolve()}\n")
        write_text_atomic(path, f"{run}\n")
    for directory in former:
        if directory.is_dir():
            with marker_lock(directory / session_id):
                (directory / session_id).unlink(missing_ok=True)


def end(run: Path) -> None:
    """Remove only the recorded marker whose first line still names this run."""
    try:
        recorded = (run / ACTIVE_MARKER_FILE).read_text(encoding="utf-8").splitlines()
        if not recorded:
            return
        path = Path(recorded[0])
        with marker_lock(path):
            target = path.read_text(encoding="utf-8").splitlines()
            if target and Path(target[0]).resolve() == run.resolve():
                path.unlink(missing_ok=True)
    except FileNotFoundError:
        return


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


def active_dirs(override: str | Path | None = None) -> tuple[Path, ...]:
    """The folders that hold run markers, the new name first."""
    return (Path(override),) if override else tuple(root / "active" for root in run_roots())


def marker(session_id: str, override: str | Path | None = None) -> Path:
    """The session's run marker: the first that exists, else where a new run writes it."""
    candidates = [directory / session_id for directory in active_dirs(override)]
    return next((path for path in candidates if path.is_file()), candidates[0])


def marker_must_stay(path: Path) -> bool:
    """Keep another session's marker when its owner is live or liveness is unknown.

    The marker name is the session id; its age and target's existence say nothing
    about the owning process. Reuse the registry's process-identity check.
    """
    from scripts.message import sessions
    return not isinstance(sessions.session_liveness(path.name), sessions.SessionNotLive)


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
    """List unique run folders in root and name order, without marker folders."""
    folders: dict[Path, Path] = {}
    for root in run_roots(root_override):
        try:
            candidates = sorted(root.iterdir())
        except OSError:
            continue
        for folder in candidates:
            if folder != root / "active" and folder.is_dir():
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
                    record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL) -> dict[str, list[str]]:
    """Read each run's director once, in root and name order."""
    found: dict[str, list[str]] = {}
    for folder in run_folders(root_override):
        if (folder / STATE_FILE).is_file():
            found.setdefault(director(folder, record=record), []).append(str(folder))
    return found


def held(session_id: str, root_override: str | Path | None = None,
         active_override: str | Path | None = None, *,
         scanned: dict[str, list[str]] | None = None,
         record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL) -> list[str]:
    """List directed folders with the verified marker first, then the shared scan's order."""
    if not session_id:
        return []
    folders = held_by_session(root_override, record=record) if scanned is None else scanned
    active = Path(root_override) / "active" if root_override is not None and active_override is None else active_override
    try:
        lines = marker(session_id, active).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    runs: list[str] = []
    if lines and (target := lines[0].strip()):
        resolved = Path(target).resolve()
        owner = next((session for session, paths in folders.items()
                      if any(Path(path).resolve() == resolved for path in paths)), None)
        if owner is None:
            owner = director(Path(target), record=record)
            if owner == session_id:
                # Cache an outside target only after its marker owner matches its director.
                folders.setdefault(owner, []).append(target)
        if owner == session_id:
            runs.append(target)
    seen = {Path(run).resolve() for run in runs}
    for folder in folders.get(session_id, ()):
        resolved = Path(folder).resolve()
        if resolved not in seen:
            seen.add(resolved)
            runs.append(folder)
    return runs


class FormerOwner(NamedTuple):
    session_id: str
    name: str
    run: str


def former_owners(running: Mapping[str, str]) -> list[FormerOwner]:
    """Each running session, by id to its name, that owns a marker under the former name."""
    try:
        markers = sorted((FORMER_RUN_ROOT / "active").iterdir())
    except OSError:
        return []
    owners: list[FormerOwner] = []
    for path in markers:
        if path.name not in running and not marker_must_stay(path):
            continue
        try:
            run = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            run = "an unreadable marker"
        owners.append(FormerOwner(path.name, running.get(path.name, "live or uncheckable session"), run))
    return owners


def removal_check(running: Mapping[str, str]) -> int:
    """Refuse the former name's removal while a running session owns a marker under it."""
    owners = former_owners(running)
    if not owners:
        print(f"no running session owns a marker under {FORMER_RUN_ROOT / 'active'}; the former name can go")
        return 0
    print(f"keep the former name: {len(owners)} running session(s) own a marker under {FORMER_RUN_ROOT / 'active'}")
    for owner in owners:
        print(f"  {owner.name or 'unnamed'} ({owner.session_id}): {owner.run}")
    return 1


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
    _ = parser.add_argument("--active", default="", help="one marker folder in place of both")
    args = parser.parse_args(argv)
    for run in held(cast(str, args.session_id), cast(str, args.root) or None, cast(str, args.active) or None):
        print(run)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "end":
        try:
            end(Path(argv[1]))
        except (OSError, UnicodeError) as error:
            print(f"unit_runs: marker not removed: {error}", file=sys.stderr)
            return 1
        return 0
    if len(argv) == 3 and argv[0] == "activate":
        try:
            activate(Path(argv[1]), argv[2])
        except OSError as error:
            print(f"unit_runs: marker not recorded: {error}", file=sys.stderr)
            return 1
        return 0
    if argv and argv[0] == "held":
        return _held_main(argv[1:])
    if argv == ["history-root"]:
        print(history_root())
        return 0
    if argv == ["move-history"]:
        return move_history(time.time())
    if argv != ["removal-check"]:
        print("usage: unit_runs.py removal-check | history-root | move-history | activate <run folder> <session id> | end <run folder> | held <session id>",
              file=sys.stderr)
        return 2
    from scripts.message.sessions import running_sessions

    return removal_check({record["sessionId"]: record["name"] for record in running_sessions()})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
