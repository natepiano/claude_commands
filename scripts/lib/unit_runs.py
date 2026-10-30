#!/usr/bin/env python3
"""Where unit runs keep their folders, under the new name and the former one.

A /unit:direct run keeps its files in `/tmp/claude/unit/<run id>/` and marks its Claude session
in `/tmp/claude/unit/active/<session id>`.
Runs started before 2026-10-15 used `delegate` in both places and keep their folders
until they end, so every reader takes both names. The former name is read until a removal
step, on or after 2026-10-22, drops it; that step runs only while `unit_runs.py removal-check`
passes, since a run started before the rename may stay open past that date.

A test points a reader at its own folder with an override, which replaces both names.

A run's director is recorded in the run folder itself, not read off the markers: a director runs
independent phases side by side, each in its own run folder, and its one marker names only one of
them. implement.sh and review.sh call `record-director` on every dispatch, so no step depends on a
model remembering to write it. `held` lists every run a director holds, so its run end stops the
workers of each one, not only the run its marker names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

RUN_ROOT = Path("/tmp/claude/unit")
FORMER_RUN_ROOT = Path("/tmp/claude/delegate")
# Notifier instances a unit run once had; conversation_pause names them "status reports".
INSTANCE_PREFIXES = ("unit-", "delegate-")
# A run's shared liveness log, beaten every 60 s per worker while one runs.
HEARTBEAT = "heartbeat.log"
# A run whose log moved within this window is live. Ten beats of slack, because
# worker cleanup that reads a live run as dead removes its working workers.
LIVE_HEARTBEAT_SECS = 600
# The Claude session that directs the run, one line, rewritten by each dispatch.
DIRECTOR_FILE = "director"
# Names the run's history, whose first line is the `run_started` event `start-run` writes.
STATE_FILE = "progress_history_state.json"
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


def record_director(run: Path, session_id: str) -> None:
    """Name the run's director. A Codex worker that dispatches a peer inherits the director's
    session id, so it writes the same one; a Claude worker has its own, so a session the run lists
    as its Claude worker writes nothing. The last write wins, so a director that takes over the
    run under a new session id takes its edges with it."""
    if not session_id or not run.is_dir() or _claude_worker(run, session_id):
        return
    staged = run / f".{DIRECTOR_FILE}.{os.getpid()}"
    _ = staged.write_text(f"{session_id}\n", encoding="utf-8")
    _ = staged.replace(run / DIRECTOR_FILE)


def _claude_worker(run: Path, session_id: str) -> bool:
    """Whether the run launched this session as a Claude worker: a launch record names it, or a
    line of the `workers` ledger, whose short id prefixes its session id."""
    for path in run.glob("impl_worker_*.json"):
        try:
            record = _json_object(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if record.get("family") == "claude" and record.get("worker_id") == session_id:
            return True
    try:
        ledger = (run / "workers").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return any(short and session_id.startswith(short) for short in (line.partition("\t")[0].strip() for line in ledger))


def director(run: Path) -> str:
    """The run's director: its recorded one, else the session that started it, from the
    `run_started` event that opens its history, for a run nothing has dispatched in since
    dispatches began recording it. The state's own `main_agent` is not used: in runs started before
    2026-10-17 a pass opened inside a worker rewrote it with that worker's thread, and
    progress_history reads the director from here to repair it."""
    try:
        recorded = (run / DIRECTOR_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        recorded = ""
    return recorded or _started_by(run)


def held(session_id: str, root_override: str | Path | None = None,
         active_override: str | Path | None = None) -> list[str]:
    """Every run folder the session directs: its marker's run first, as the marker writes it, then
    each folder under the run roots that names it as director. A plain scan, since reading the
    director of every folder costs about 25 ms for the 224 live ones (2026-10-17)."""
    if not session_id:
        return []
    try:
        lines = marker(session_id, active_override).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    runs = [lines[0].strip()] if lines and lines[0].strip() else []
    seen = {Path(run).resolve() for run in runs}
    for root in run_roots(root_override):
        try:
            folders = sorted(root.iterdir())
        except OSError:
            continue
        for folder in folders:
            if folder.is_dir() and folder.resolve() not in seen and director(folder) == session_id:
                seen.add(folder.resolve())
                runs.append(str(folder))
    return runs


def _json_object(text: str) -> dict[str, object]:
    try:
        raw = cast(object, json.loads(text))
    except ValueError:
        return {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def _started_by(run: Path) -> str:
    try:
        history = _json_object((run / STATE_FILE).read_text(encoding="utf-8")).get("history_file")
        if not isinstance(history, str) or not history:
            return ""
        with Path(history).open(encoding="utf-8") as handle:
            event = _json_object(handle.readline())
    except (OSError, UnicodeError):
        return ""
    agent = event.get("main_agent") if event.get("event_type") == "run_started" else None
    session = cast(dict[str, object], agent).get("session_id") if isinstance(agent, dict) else None
    return session if isinstance(session, str) else ""


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


def live_run_folders(now: float) -> list[Path]:
    """Run folders under either name whose heartbeat moved in the live window."""
    live: list[Path] = []
    for root in run_roots():
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
    if argv and argv[0] == "held":
        return _held_main(argv[1:])
    if len(argv) == 2 and argv[0] == "record-director":
        try:
            record_director(Path(argv[1]), os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        except OSError as error:
            print(f"unit_runs: director not recorded in {argv[1]}: {error}", file=sys.stderr)
            return 1
        return 0
    if argv == ["history-root"]:
        print(history_root())
        return 0
    if argv == ["move-history"]:
        return move_history(time.time())
    if argv != ["removal-check"]:
        print("usage: unit_runs.py removal-check | history-root | move-history | record-director <run folder> | held <session id>",
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
