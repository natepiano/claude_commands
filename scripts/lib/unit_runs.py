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

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
SERVER_FILE = "mesh_server.json"
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
        if row is None or row.kind != "unit":
            raise staff.StaffUnreadable(f"run owner {reference}: unit staff row is unavailable")
        return row
    identity = fields.get("main_agent")
    agent = cast(dict[str, object], identity) if isinstance(identity, dict) else {}
    reference = agent.get("session_id") or fields.get("owner_session") or fields.get("director_session")
    row = record.by_member_reference(reference) if isinstance(reference, str) and reference else None
    if row is not None and row.kind == "worker":
        row = record.parent_of(row)
    return row if row is not None and row.kind == "unit" else RunOwnerUnrecorded("run has no unit staff owner")


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


def worker_transport(run: Path) -> Literal["mesh", "herdr"]:
    """Pin the configured worker transport for this run."""
    from scripts.lib import conf
    from scripts.lib.atomic_file import write_text_atomic

    run.mkdir(parents=True, exist_ok=True)
    pin = run / "worker_transport"
    with (run / "worker_transport.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            selected = pin.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            selected = conf.read("direct").text("WORKER_TRANSPORT")
            selected = _transport(selected, run)
            write_text_atomic(pin, selected + "\n")
        return _transport(selected, pin)


def _transport(selected: str, source: str | Path) -> Literal["mesh", "herdr"]:
    if selected not in ("mesh", "herdr"):
        raise ValueError(f"{source}: invalid worker transport: {selected}")
    return selected


class NoRunPin(Enum):
    ABSENT = "the run has no transport pin"


def run_pin(run: Path) -> Literal["mesh", "herdr"] | NoRunPin:
    """Read a run's transport pin without writing it."""
    pin = run / "worker_transport"
    try:
        return _transport(pin.read_text(encoding="utf-8").strip(), pin)
    except FileNotFoundError:
        return NoRunPin.ABSENT


class RunStaffAtUse(Enum):
    LOCAL = "read the local staff record"


def parent_transport(session_id: str, *, scanned: dict[str, list[str]] | None = None,
                     record: staff.StaffSnapshot | RunStaffAtUse = RunStaffAtUse.LOCAL
                     ) -> Literal["mesh", "herdr"]:
    """Read the first held run's pin, or current configuration, without writing."""
    from scripts.lib import conf

    runs = held(session_id, os.environ.get("ROSTER_RUN_ROOT"), scanned=scanned, record=record)
    if runs:
        selected = run_pin(Path(runs[0]))
        if not isinstance(selected, NoRunPin):
            return selected
    return _transport(conf.read("direct").text("WORKER_TRANSPORT"), "config/direct.conf")


def activate(run: Path, session_id: str) -> None:
    """Write the current marker and remove the former marker."""
    current, *former = active_dirs()
    current.mkdir(parents=True, exist_ok=True)
    _ = (current / session_id).write_text(f"{run}\n", encoding="utf-8")
    for directory in former:
        (directory / session_id).unlink(missing_ok=True)


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
    if TYPE_CHECKING:
        from ..message import sessions
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
        import sessions
    return not isinstance(sessions.session_liveness(path.name), sessions.SessionNotLive)


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
            print(f"anomaly: {run}: {error}", file=sys.stderr)
        return ""
    except (OSError, ValueError) as error:
        print(f"anomaly: {run}: {error}", file=sys.stderr)
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
            return _json_object(path.read_text())
        except (OSError, UnicodeError):
            return {}

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
    roster = read_record(run / "mesh_roster.json")
    if any(
        isinstance(entry, dict)
        and (cast(dict[str, object], entry).get("status") in tuple(worker_state.ROSTER_LIVE)
             or cast(dict[str, object], entry).get("paused_turn") is True)
        for entry in roster.values()
    ):
        return True
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
        if any((folder / name).is_file() for name in (STATE_FILE, "mesh_roster.json")):
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
    try:
        lines = marker(session_id, active_override).read_text(encoding="utf-8").splitlines()
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


def _json_object(text: str) -> dict[str, object]:
    try:
        raw = cast(object, json.loads(text))
    except ValueError:
        return {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def migrate_owners(root: Path, *, dry_run: bool = False) -> int:
    """Replace active run names with proved staff ids; keep history files unchanged."""
    from scripts.lib import atomic_file
    from scripts.production import staff

    unavailable = False
    for path in sorted(root.glob(f"*/{STATE_FILE}")):
        try:
            with (path.parent / ".progress_history.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                fields = _json_object(path.read_text(encoding="utf-8"))
                if not fields:
                    raise ValueError(f"{path}: run state is unreadable")
                if fields.get("status") != "active":
                    continue
                record = staff.snapshot()
                owner = run_owner(fields, record)
                if isinstance(owner, RunOwnerUnrecorded):
                    production, unit = fields.get("production"), fields.get("unit")
                    parents = {row.id for row in record.find(kind="showrunner") if row.name == production}
                    matches = [row for row in record.find(kind="unit") if isinstance(row.parent, staff.ReportsTo) and row.parent.id in parents
                               and isinstance(unit, str) and row.name == unit]
                    if len(matches) != 1:
                        raise staff.StaffUnreadable(f"{path}: run owner is unavailable")
                    owner = matches[0]
                changed = dict(fields)
                changed[OWNER_FIELD] = owner.id
                folder = fields.get("working_dir")
                for key in ("plan_doc", "project_plan_doc"):
                    value = fields.get(key)
                    if isinstance(folder, str) and isinstance(value, str) and value.startswith(folder + "/"):
                        changed[key] = str(Path(value).relative_to(folder))
                for key in ("production", "unit", "working_dir", "worktree", "branch"):
                    _ = changed.pop(key, None)
                if changed != fields:
                    if not dry_run:
                        atomic_file.write_text_atomic(path, json.dumps(changed, indent=2, sort_keys=True) + "\n")
                    print(f"{'would migrate' if dry_run else 'migrated'} {path}: {owner.id}")
        except (OSError, ValueError) as error:
            unavailable = True
            print(f"run owner unavailable: {error}", file=sys.stderr)
    return int(unavailable)


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
    if len(argv) == 2 and argv[0] == "worker-transport":
        try:
            print(worker_transport(Path(argv[1])))
        except (OSError, ValueError) as error:
            print(f"unit_runs: {error}", file=sys.stderr)
            return 2
        return 0
    if argv and argv[0] == "migrate-owners":
        parser = argparse.ArgumentParser(description="Migrate active run owners to staff ids.")
        _ = parser.add_argument("--root", type=Path, required=True)
        _ = parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(argv[1:])
        return migrate_owners(cast(Path, args.root), dry_run=cast(bool, args.dry_run))
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
        print("usage: unit_runs.py removal-check | history-root | move-history | activate <run folder> <session id> | held <session id> | worker-transport <run folder>",
              file=sys.stderr)
        return 2
    if TYPE_CHECKING:
        from ..message.sessions import running_sessions
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
        from sessions import running_sessions

    return removal_check({record["sessionId"]: record["name"] for record in running_sessions()})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
