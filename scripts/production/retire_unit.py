#!/usr/bin/env python3
"""Stop and retire one unit while retaining its staff identity.

Usage: retire_unit.py <slug> <unit> [--dry-run] [--wait-minutes N]

Staff supplies the unit folder and session id. Run ownership uses the director's session id.
Claude transcripts remain in place. The parked configuration retains the plan, branch, session id,
retirement date, and formatting settings. Origin's refs/retired/<staff id> retains unmerged commits.

Uncommitted work refuses retirement. Unmerged commits remain under a confirmed retirement ref.
Retirement stops the unit's sessions, workers, build claims, and timers before it removes the worktree.
A live process or an unreadable liveness check refuses removal. A preview changes no file.

Exit 0 on success, 1 on a refusal or failure, and 2 on invalid arguments.
"""
from __future__ import annotations


import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import session_staff, staff, staff_sources, unit_config
from scripts.shutdown.remote import local_machine
from scripts.lib import process_group, conf
from scripts.issue_score.selection import UseConfiguredValue
from scripts.agents import worker_lifecycle

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
import berth_board  # noqa: E402
import progress_history  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
import unit_names  # noqa: E402
import unit_runs  # noqa: E402
import pane_record  # noqa: E402
from scripts.production.production_doc import Production, Refusal, read_production  # noqa: E402

if TYPE_CHECKING:
    from scripts.message import sessions
    from scripts.message.sessions import SessionRecord
    from scripts.shutdown import stop_resume
else:
    from scripts.shutdown import stop_resume
    import sessions
    from sessions import SessionRecord

SCRIPTS = Path(__file__).resolve().parent.parent
MESH = SCRIPTS / "agents" / "codex_mesh.py"
BUILD_HOLD = SCRIPTS / "build_hold" / "build_hold.py"
MAC_CLAIMS = Path.home() / ".local/state/mac-test/claims"
WAIT_MINUTES = 20.0
# A run's folder of detached-run records (codex_mesh.DETACHED_RUNS_DIR).
DETACHED_RUNS = "mesh_runs"


class Refused(Exception):
    pass


@dataclass(frozen=True)
class SavedBranch:
    ref: str
    tip: str


@dataclass(frozen=True)
class NoSavedBranch:
    pass


NO_SAVED_BRANCH = NoSavedBranch()


@dataclass(frozen=True)
class DirectorSession:
    session_id: str
    resume_folder: Path


@dataclass(frozen=True)
class NoDirectorSession:
    pass


@dataclass(frozen=True)
class UnresolvedStaffMember:
    """A caller has not supplied the unit's retained identity."""


@dataclass(frozen=True)
class Unit:
    production: Production
    name: str
    worktree: Path
    branch: str
    retired: bool
    staff_member: staff.StaffRow | UnresolvedStaffMember = UnresolvedStaffMember()


class MeshRun(NamedTuple):
    run: Path
    pid: int
    seats: tuple[str, ...]


class MacClaim(NamedTuple):
    path: Path
    what: str
    pid: int
    proc_start: str


class Found(NamedTuple):
    claude: tuple[SessionRecord, ...]
    runs: tuple[MeshRun, ...]
    holds: tuple[str, ...]
    reservations: tuple[str, ...]
    claims: tuple[MacClaim, ...]
    processes: tuple[int, ...]



def run(argv: list[str], cwd: Path | None = None, *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> subprocess.CompletedProcess[str]:
    """One command, without this session's id: shutdown then finds the account that holds the unit."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    limit = settings.number("RETIRE_COMMAND_TIMEOUT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    remaining = operation_limit
    if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE:
        remaining = operation_limit - time.monotonic()
        if remaining <= 0:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    return subprocess.run(argv, pass_fds=staff.operation_fds(), cwd=cwd, env=environment, capture_output=True, text=True, check=False,
                          timeout=min(limit, remaining))


def failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"


def production_of(slug: str) -> Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}")
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise Refused(f"{slug} has no readable update timer: {checked.reason}")
    return read_production(checked.path)


def find_unit(slug: str, given: str, *,
              record: staff.StaffSnapshot | session_staff.ReadCurrentStaff = session_staff.ReadCurrentStaff.CURRENT) -> Unit:
    record = staff.snapshot() if isinstance(record, session_staff.ReadCurrentStaff) else record
    production = production_of(slug)
    name = unit_names.unit_id(given)
    selected = staff_sources.member(staff_sources.UnitMember(production.slug, name), record)
    if isinstance(selected, staff_sources.NoSuchMember):
        raise Refused(f"{production.slug}/{name}: no matching staff member")
    location = unit_lookup.local_worktree(selected)
    retired = isinstance(location, staff.RemovedWorktree)
    if retired and location.path.exists():
        raise Refused(f"{name}: removed staff worktree still exists: {location.path}")
    return Unit(production, name, location.path, unit_branch(selected.row, location.path), retired, selected.row)


def unit_branch(row: staff.StaffRow, worktree: Path) -> str:
    """The parked branch once retirement starts; before it, the worktree's branch."""
    parked = unit_config.read_parked(row.id, cleanup=False)
    if isinstance(parked, unit_config.UnitConfig) and isinstance(parked.retired, unit_config.RetiredSession):
        return parked.retired.branch.value
    result = run(["git", "-C", str(worktree), "branch", "--show-current"])
    if result.returncode or not result.stdout.strip():
        raise Refused(f"{row.name}: cannot read the branch of {worktree}: {failure(result)}")
    return result.stdout.strip()


def _git(unit: Unit, *arguments: str, settings: conf.ConfigRead) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(unit.production.checkout), *arguments], settings=settings)


def branch_refs(unit: Unit, *, read_only: bool = False, settings: conf.ConfigRead) -> list[str]:
    """The unit's branch here and on origin; a preview fetches objects without updating refs."""
    refs: list[str] = []
    if _git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}", settings=settings).returncode == 0:
        refs.append(f"refs/heads/{unit.branch}")
    remote = _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings)
    if remote.returncode == 0:
        tracking = f"refs/remotes/origin/{unit.branch}"
        fetched = _git(unit, "fetch", *(("--no-write-fetch-head", "--refmap=",) if read_only else ()),
                       "-q", "origin", f"refs/heads/{unit.branch}" if read_only
                       else f"+refs/heads/{unit.branch}:{tracking}", settings=settings)
        if fetched.returncode:
            raise Refused(f"cannot read origin {unit.branch}: {failure(fetched)}")
        if read_only:
            refs.append(remote.stdout.split()[0])
        else:
            refs.append(tracking)
    elif remote.returncode != 2:
        raise Refused(f"cannot read origin {unit.branch}: {failure(remote)}")
    return refs


def staff_id(unit: Unit) -> staff.StaffId:
    member = (staff_sources.MemberFound(unit.staff_member) if isinstance(unit.staff_member, staff.StaffRow) else
              staff_sources.member(staff_sources.UnitMember(unit.production.slug, unit.name), staff.snapshot()))
    if not isinstance(member, staff_sources.MemberFound):
        raise Refused(f"{unit.name}: no staff id for the retired ref")
    return member.row.id


def recorded_saved_branch(checkout: Path, member: staff.StaffId) -> SavedBranch | NoSavedBranch:
    """The retired ref that an earlier run pushed; origin holds its only record."""
    ref = f"refs/retired/{member}"
    remote = run(["git", "-C", str(checkout), "ls-remote", "--exit-code", "origin", ref])
    if remote.returncode == 2:
        return NoSavedBranch()
    if remote.returncode:
        raise Refused(f"cannot read {ref}: {failure(remote)}")
    return SavedBranch(ref, remote.stdout.split()[0])


def confirm_saved_branch(unit: Unit, saved: SavedBranch, *, settings: conf.ConfigRead) -> None:
    remote = _git(unit, "ls-remote", "--exit-code", "origin", saved.ref, settings=settings)
    if remote.returncode or remote.stdout.splitlines() != [f"{saved.tip}\t{saved.ref}"]:
        raise Refused(f"cannot confirm {saved.ref} at {saved.tip}: {failure(remote)}")


def plan_saved_branch(unit: Unit, saved: SavedBranch | NoSavedBranch = NO_SAVED_BRANCH,
                      *, read_only: bool = False, settings: conf.ConfigRead) -> SavedBranch | NoSavedBranch:
    """Choose one tip that retains the local, origin and previously saved work."""
    if isinstance(saved, NoSavedBranch):
        saved = recorded_saved_branch(unit.production.checkout, staff_id(unit))
    recorded_tip = saved.tip if isinstance(saved, SavedBranch) else ""
    tip = recorded_tip
    for ref in branch_refs(unit, read_only=read_only, settings=settings):
        result = _git(unit, "rev-parse", f"{ref}^{{commit}}", settings=settings)
        if result.returncode:
            raise Refused(f"cannot read {ref}: {failure(result)}")
        candidate = result.stdout.strip()
        if tip and _git(unit, "merge-base", "--is-ancestor", candidate, tip, settings=settings).returncode == 0:
            continue
        if tip and _git(unit, "merge-base", "--is-ancestor", tip, candidate, settings=settings).returncode:
            raise Refused(f"{unit.branch}: local, origin and saved tips diverge; keep all before retiring")
        tip = candidate
    if isinstance(saved, NoSavedBranch):
        if not tip or _git(unit, "merge-base", "--is-ancestor", tip, unit.production.merge_branch, settings=settings).returncode == 0:
            return NoSavedBranch()
        saved = SavedBranch(f"refs/retired/{staff_id(unit)}", tip)
    remote = _git(unit, "ls-remote", "--exit-code", "origin", saved.ref, settings=settings)
    if remote.returncode == 0:
        if remote.stdout.splitlines() == [f"{saved.tip}\t{saved.ref}"]:
            remote_tip = saved.tip
        else:
            fetched = _git(unit, "fetch", *(("--no-write-fetch-head", "--refmap=",) if read_only else ()),
                           "-q", "origin", saved.ref, settings=settings)
            if fetched.returncode:
                raise Refused(f"cannot read {saved.ref}: {failure(fetched)}")
            if read_only:
                remote_ref = remote.stdout.split()[0]
            else:
                remote_ref = "FETCH_HEAD"
            result = _git(unit, "rev-parse", "--verify", f"{remote_ref}^{{commit}}", settings=settings)
            if result.returncode:
                raise Refused(f"cannot read {saved.ref}: {failure(result)}")
            remote_tip = result.stdout.strip()
        if recorded_tip and _git(unit, "merge-base", "--is-ancestor", recorded_tip, remote_tip, settings=settings).returncode:
            raise Refused(f"{saved.ref} does not descend from saved tip {recorded_tip}")
        if _git(unit, "merge-base", "--is-ancestor", tip, remote_tip, settings=settings).returncode == 0:
            tip = remote_tip
        elif _git(unit, "merge-base", "--is-ancestor", remote_tip, tip, settings=settings).returncode:
            raise Refused(f"{unit.branch}: branch and retired ref diverge; keep both before retiring")
    elif remote.returncode != 2:
        raise Refused(f"cannot read {saved.ref}: {failure(remote)}")
    return SavedBranch(saved.ref, tip)


def preserve_branch(unit: Unit, saved: SavedBranch | NoSavedBranch = NO_SAVED_BRANCH, *, settings: conf.ConfigRead) -> SavedBranch | NoSavedBranch:
    """Refresh and confirm the saved tip before any removal."""
    updated = plan_saved_branch(unit, saved, settings=settings)
    if isinstance(updated, NoSavedBranch):
        return updated
    remote = _git(unit, "ls-remote", "--exit-code", "origin", updated.ref, settings=settings)
    if remote.returncode not in (0, 2):
        raise Refused(f"cannot read {updated.ref}: {failure(remote)}")
    if remote.returncode == 2 or remote.stdout.splitlines() != [f"{updated.tip}\t{updated.ref}"]:
        pushed = _git(unit, "push", "origin", f"{updated.tip}:{updated.ref}", settings=settings)
        if pushed.returncode:
            raise Refused(f"cannot preserve {updated.ref}: {failure(pushed)}")
        print(f"saved: {updated.ref} at {updated.tip}")
    confirm_saved_branch(unit, updated, settings=settings)
    return updated


def recorder_line_only(worktree: Path, path: str, *, settings: conf.ConfigRead) -> bool:
    """Whether the file's whole change against HEAD is one added `Project started` line."""
    diff = run(["git", "-C", str(worktree), "diff", "--no-color", "--unified=0", "HEAD", "--", path], settings=settings)
    changed = [line for line in diff.stdout.splitlines()
               if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))]
    return (diff.returncode == 0 and len(changed) == 1 and changed[0].startswith("+")
            and progress_history.PROJECT_STARTED_PATTERN.fullmatch(changed[0][1:]) is not None)


def refusals(unit: Unit, *, settings: conf.ConfigRead) -> list[str]:
    """What keeps the unit from retiring: uncommitted work, and commits the merge branch lacks.

    A merge of the merge branch into the unit carries nothing of the unit's own, so only commits
    that are not merges count."""
    for key in ("RETIRE_COMMAND_TIMEOUT_SECONDS", "RETIRE_TERM_WAIT_SECONDS", "RETIRE_POLL_SECONDS"):
        _ = settings.number(key)
    found: list[str] = []
    try:
        marked = unit_lookup.marked_unit(unit.production.slug, unit.name)
    except OSError as error:
        return [f"cannot read unit mark: {error}"]
    try:
        _ = unit_move.unit_pane(marked, unit.production.slug)
    except (OSError, ValueError) as error:
        return [f"{unit.name}: cannot check its pane shell: {error}"]
    if unit_move.exists(unit.worktree):
        status = run(["git", "-C", str(unit.worktree), "status", "--short"], settings=settings)
        if status.returncode:
            found.append(f"cannot read {unit.worktree}: {failure(status)}")
        else:
            # The /unit:direct recorder writes its `Project started` line into the plan and never
            # commits it; a unit that never checkpoints keeps it as its only change. No person wrote it.
            paths = [line[3:] for line in status.stdout.splitlines()
                     if not (line[:2] == " M" and recorder_line_only(unit.worktree, line[3:], settings=settings))]
            if paths:
                found.append(f"uncommitted work in {unit.worktree}: {', '.join(paths[:5])}"
                             + (f" and {len(paths) - 5} more" if len(paths) > 5 else ""))
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_start(pid: int) -> str:
    """The process's start time in clock ticks, as /proc/<pid>/stat field 22 gives it."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    return stat.rsplit(")", 1)[-1].split()[19]


def claude_sessions(_worktree: Path, ids: frozenset[str]) -> tuple[SessionRecord, ...]:
    """Only positively owned sessions, named by their run/director identity.

    A unit director can work outside its worktree, so the run's director is found by its id.
    """
    return tuple(item for item in sessions.live_sessions() if item["sessionId"] in ids)


def _json(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def runs_of(worktree: Path) -> list[Path]:
    """Read run ownership through the staff row's session identities."""
    record = staff.snapshot()
    owner = record.by_worktree(local_machine(), worktree.resolve())
    if owner is None:
        return []
    found: list[Path] = []
    for root in unit_runs.run_roots(os.environ.get("RETIRE_UNIT_RUN_ROOT") or None):
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            if folder.name != "active" and record.by_member_reference(unit_runs.director(folder)) == owner:
                found.append(folder)
    return found


def mesh_runs(runs: list[Path]) -> tuple[MeshRun, ...]:
    found: list[MeshRun] = []
    for folder in runs:
        pid = _json(folder / "mesh_server.json").get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or not _alive(pid):
            continue
        roster = _json(folder / "mesh_roster.json")
        seats = tuple(name for name, entry in roster.items()
                      if isinstance(entry, dict) and cast(dict[str, object], entry).get("status") != "ended")
        found.append(MeshRun(folder, pid, seats))
    return tuple(found)


def command_line(pid: int, *, settings: conf.ConfigRead) -> list[str]:
    """The process's arguments; empty once it has gone. The Mac has no /proc: ps splits them at spaces."""
    if sys.platform == "darwin":
        return run(["ps", "-o", "args=", "-p", str(pid)], settings=settings).stdout.split()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _session_of(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return 0


def run_processes(worktree: Path, inside: list[int], *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[int, str]:
    """The processes inside the worktree that its runs leave behind once their sessions stop, each named.

    A run's detached command (mesh_runs.py supervise) is found by its record, which holds the
    supervisor's pid and is in the supervisor's arguments; an app-server by its run's
    mesh_server.json; a live server's idle-stop watcher by its mesh entry point and run folder
    arguments. A dead server's watcher needs no matching run history: its own kill -0 rule would
    end it at its next wake. Each leads its own session, so its children go with it.
    """
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    runs = runs_of(worktree)
    leaders = {item.pid: f"app-server {item.pid} of run {item.run.name}" for item in mesh_runs(runs)}
    for folder in runs:
        for path in sorted((folder / DETACHED_RUNS).glob("*.json")):
            record = _json(path)
            pid = record.get("pid")
            if (record.get("status") == "running" and isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and str(path) in command_line(pid, settings=settings)):
                leaders[pid] = f"run supervisor {pid} of {record.get('label', path.stem)} in run {folder.name}"
    folders = {str(folder.resolve()): folder.name for folder in runs}
    for pid in inside:
        words = command_line(pid, settings=settings)
        if (len(words) < 7 or Path(words[0]).name != "sh" or words[1] != "-c"
                or "idle-stop" not in words[2] or Path(words[3]).resolve() != MESH.resolve()):
            continue
        try:
            server = int(words[4])
        except ValueError:
            continue
        if server <= 0:
            continue
        if not _alive(server):
            leaders[pid] = f"app-server watcher {pid} of dead server {server}"
        elif (folder := str(Path(words[6]).resolve())) in folders:
            leaders[pid] = f"app-server watcher {pid} of run {folders[folder]}"
    return {pid: f"process {pid} ({_command_name(pid)}) of the {leaders[leader]}"
            for pid in inside if (leader := _session_of(pid)) in leaders}


def hold_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))


def holds(session_ids: set[str]) -> tuple[str, ...]:
    """The build holds kept under the unit's session ids: build_hold keys a hold by session id."""
    return tuple(sorted(key for key in session_ids if key and (hold_directory() / key).is_file()))


def reservations(worktree: Path) -> tuple[str, ...]:
    if not unit_move.exists(worktree):
        return ()
    board = berth_board.read_board(worktree)
    if isinstance(board, berth_board.BoardUnavailable):
        return ()
    return tuple(item.reservation_id for item in board.reservations()
                 if item.holder_worktree_root.resolve() == worktree.resolve())


def mac_claims(worktree: Path) -> tuple[MacClaim, ...]:
    found: list[MacClaim] = []
    for path in sorted(MAC_CLAIMS.glob("*.json")) if MAC_CLAIMS.is_dir() else ():
        claim = _json(path)
        pid = claim.get("pid")
        if claim.get("worktree") == str(worktree) and isinstance(pid, int) and not isinstance(pid, bool):
            found.append(MacClaim(path, str(claim.get("what", path.stem)), pid, str(claim.get("proc_start", ""))))
    return tuple(found)


def gather(unit: Unit) -> Found:
    runs = runs_of(unit.worktree)
    directors = frozenset(unit_runs.director(folder) for folder in runs) - {""}
    claude = claude_sessions(unit.worktree, directors)
    marked = marked_director(unit)
    if isinstance(marked, unit_lookup.LiveClaude) and not any(
            item["sessionId"] == marked.session_id and item["pid"] == marked.pid for item in claude):
        claude += (SessionRecord(pid=marked.pid, sessionId=marked.session_id, name=marked.name,
                                 messagingSocketPath=marked.socket, updatedAt=0, tmux="", formerNames=[]),)
    claims = mac_claims(unit.worktree)
    ids = {item["sessionId"] for item in claude} | directors
    known = {item["pid"] for item in claude} | {claim.pid for claim in claims}
    inside = unit_move.processes_inside(unit.worktree) if unit_move.exists(unit.worktree) else []
    return Found(claude, mesh_runs(runs), holds(ids), reservations(unit.worktree), claims,
                 tuple(pid for pid in inside if pid not in known and pid != os.getpid()))


def marked_director(unit: Unit) -> unit_lookup.LiveClaude | unit_lookup.ClaudeNotRunning:
    try:
        found = unit_lookup.marked_unit(unit.production.slug, unit.name)
    except OSError as error:
        raise Refused(f"{unit.name}: cannot read unit mark: {error}") from error
    if found is None:
        return unit_lookup.ClaudeNotRunning()
    if isinstance(found.claude, unit_lookup.ClaudeUnknown):
        raise Refused(f"{unit.name}: cannot read unit mark: {found.claude.reason}")
    return found.claude


def transcript_locations(session_id: str) -> list[Path]:
    """Distinct project folders holding this transcript, including shared account symlinks once."""
    return sorted({path.resolve() for directory in unit_move.config_dirs()
                   for path in (directory / "projects").glob(f"*/{session_id}.jsonl")})


def director_resume_folder(session_id: str, *, dependencies: Callable[[Path], None] | None = None) -> Path:
    """Read the resume folder from the session's staff row."""
    record = staff.snapshot()
    if dependencies is not None:
        for path in record.inputs:
            dependencies(Path(path))
    row = record.by_member_reference(session_id)
    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
        raise Refused(f"{session_id}: the staff worktree is unavailable")
    return row.worktree.path


def last_session(unit: Unit) -> str:
    """Read the director's conversation from the unit's staff row."""
    member = unit_lookup.member_sources().member(unit_lookup.UnitMember(unit.production.slug, unit.name), staff.snapshot())
    if isinstance(member, unit_lookup.NoSuchMember):
        raise Refused(f"{unit.name}: no staff row")
    return member.row.current_session


def director_session(unit: Unit) -> DirectorSession | NoDirectorSession:
    session_id = last_session(unit)
    if not session_id:
        return NoDirectorSession()
    marked = marked_director(unit)
    if not transcript_locations(session_id) and not (
            isinstance(marked, unit_lookup.LiveClaude) and marked.session_id == session_id):
        return NoDirectorSession()
    return DirectorSession(session_id, director_resume_folder(session_id))


def describe_session(item: SessionRecord) -> str:
    return f"Claude session {item['name'] or item['sessionId']} ({item['sessionId']}, pid {item['pid']})"


def describe_run(item: MeshRun) -> str:
    seats = f"; workers {', '.join(item.seats)}" if item.seats else "; no open workers"
    return f"app-server pid {item.pid} of run {item.run.name}{seats}"


def _command_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def blocking_processes(pids: tuple[int, ...], pane: unit_move.UnitPane | None) -> tuple[int, ...]:
    """Only the caller and the unit's proven pane shell are exempt from cwd blockers."""
    return tuple(pid for pid in pids if pid != os.getpid() and (pane is None or pid != pane.shell))


def dry_run(unit: Unit, *, settings: conf.ConfigRead) -> int:
    print(f"retire {unit.name} of {unit.production.slug}: dry run, nothing changes")
    refused = refusals(unit, settings=settings)
    for reason in refused:
        print(f"refused: {reason}")
    found = gather(unit)
    pane = unit_move.unit_pane(unit_lookup.marked_unit(unit.production.slug, unit.name), unit.production.slug)
    lines = ([f"would stop: {describe_session(item)}, through stop_resume.stop" for item in found.claude]
             + [f"would stop: {describe_run(item)}" for item in found.runs]
             + [f"would release: build hold of {key}" for key in found.holds]
             + [f"would release: berth reservation {key}" for key in found.reservations]
             + [f"would stop: Mac claim {item.what} (pid {item.pid})" for item in found.claims]
             + ([f"would close: pane {pane.place.pane} (shell {pane.shell})"] if pane is not None else [])
             + [f"blocks retirement: unowned process {pid}" for pid in blocking_processes(found.processes, pane)])
    print("\n".join(lines or ["nothing to stop"]))
    saved = plan_saved_branch(unit, read_only=True, settings=settings)
    if isinstance(saved, SavedBranch):
        print(f"would save: {saved.tip} to origin {saved.ref}")
    if unit_move.exists(unit.worktree):
        print(f"would remove: worktree {unit.worktree}")
    print(f"would delete: branch {unit.branch} here and on origin")
    return 1 if refused else 0


def stop_sessions(unit: Unit, found: Found, wait_minutes: float, known_ids: frozenset[str], *, forget: bool = True) -> list[str]:
    """End each unit Claude through the single stop API, then forget its pane launch."""
    del wait_minutes
    identities = list(dict.fromkeys([item["sessionId"] for item in found.claude] + sorted(known_ids)))
    failures: list[str] = []
    for identity in identities:
        if isinstance(sessions.session_liveness(identity), sessions.SessionNotLive):
            continue
        outcome = stop_resume.stop(identity)
        if isinstance(outcome, stop_resume.StopFailed):
            failures.append(f"{identity}: stop refused: {outcome.reason}")
            continue
        if isinstance(outcome, stop_resume.SessionNotFound):
            if not isinstance(sessions.session_liveness(identity), sessions.SessionNotLive):
                failures.append(f"{identity}: no local tab; stop refused")
                continue
        if isinstance(outcome, stop_resume.Stopped):
            print(f"stopped: {identity}")
    if forget and not failures:
        try:
            _ = pane_record.forget_unit(unit.name)
        except (OSError, ValueError) as error:
            failures.append(f"{unit.name}: pane launch stays: {error}")
    return failures


def terminate(pids: list[int], what: dict[int, str], wait: float | None = None, *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
    """SIGTERM each pid, observing the configured wait and poll limits."""
    if not pids:
        return []
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    duration = settings.number("RETIRE_TERM_WAIT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    started = time.monotonic()
    if started >= operation_limit:
        raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    poll = settings.number("RETIRE_POLL_SECONDS")
    if wait is not None:
        duration = min(duration, max(0, wait))
    deadline = min(started + duration, operation_limit)
    signalled: set[int] = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.add(pid)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            what[pid] += f": {error}"
    while any(_alive(pid) for pid in pids):
        now = time.monotonic()
        if now >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    left: list[str] = []
    for pid in pids:
        if _alive(pid):
            left.append(f"{what[pid]}: still runs after SIGTERM")
        elif pid in signalled:
            print(f"stopped: {what[pid]}")
    return left


def process_description(pid: int, *, settings: conf.ConfigRead) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        parent = fields[1]
    except (OSError, IndexError):
        parent = "unknown"
    return (f"unowned process {pid}, parent {parent}, command {' '.join(command_line(pid, settings=settings)).strip() or 'unknown'}, "
            f"start {proc_start(pid) or 'unknown'}")


def process_session(pid: int) -> str:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for field in raw.split(b"\0"):
        key, _, value = field.partition(b"=")
        if key == b"CLAUDE_CODE_SESSION_ID":
            return value.decode("utf-8")
    return ""


def stop_orphan(unit: Unit, pid: int, recorded_session_id: str = "", *, settings: conf.ConfigRead) -> list[str]:
    """Only a proven, isolated group of this stopped session can be ended."""
    duration = settings.number("RETIRE_TERM_WAIT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
        raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    poll = settings.number("RETIRE_POLL_SECONDS")
    description = process_description(pid, settings=settings)
    try:
        started = proc_start(pid)
        session_id = recorded_session_id or last_session(unit)
        if not started or not session_id or process_session(pid) != session_id:
            raise Refused("no recorded session ownership")
        observed = sessions.session_processes(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown) or observed.pids:
            raise Refused("the recorded session is live or unreadable")
        # A group send must not reach a visitor or the caller. Only a leader whose
        # whole group is positively owned and inside this worktree is eligible.
        if os.getpgid(pid) != pid:
            raise Refused("process does not lead an isolated group")
        _ = process_group.check_process_group(pid)
        members: list[tuple[int, str]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            member = int(entry.name)
            try:
                if os.getpgid(member) != pid:
                    continue
                cwd = (entry / "cwd").resolve(strict=True)
                if not cwd.is_relative_to(unit.worktree.resolve()) or process_session(member) != session_id:
                    raise Refused(f"group member {member} has no worktree/session ownership")
                birth = proc_start(member)
                if not birth:
                    raise Refused(f"group member {member} has no start time")
                members.append((member, birth))
            except ProcessLookupError:
                continue
        if not members or not any(member == pid for member, _ in members):
            raise Refused("process group identity is unreadable")
        observed = sessions.session_processes(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown) or observed.pids:
            raise Refused("the recorded session became live or unreadable")
        if any(proc_start(member) != birth or os.getpgid(member) != pid
               or process_session(member) != session_id
               or not Path(f"/proc/{member}/cwd").resolve(strict=True).is_relative_to(unit.worktree.resolve())
               for member, birth in members) or proc_start(pid) != started:
            raise Refused("process identity changed before signal")
        if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        process_group.send_process_group(pid, signal.SIGTERM)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except ProcessLookupError:
        return []
    except (OSError, ValueError, Refused) as error:
        return [f"{description} works inside {unit.worktree}; stop it first: {error}"]
    deadline = min(time.monotonic() + duration, operation_limit)
    def still_running() -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state != "Z" and proc_start(pid) == started and _alive(pid)
    while still_running():
        now = time.monotonic()
        if now >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    if still_running():
        return [f"{description}: still runs after SIGTERM"]
    print(f"stopped: {description} of session {session_id}")
    return []


def live_claims(claims: tuple[MacClaim, ...]) -> tuple[MacClaim, ...]:
    return tuple(claim for claim in claims if _alive(claim.pid) and proc_start(claim.pid) == claim.proc_start)


def stop_rest(unit: Unit, found: Found, recorded_session_id: str = "", *, settings: conf.ConfigRead) -> list[str]:
    """Stop owned resources; remaining unowned cwd processes come back as blockers."""
    from scripts.direct import workers
    left: list[str] = []
    workers.end_unit(unit.production.slug, unit.name)
    for item in found.runs:
        result = run([sys.executable, str(MESH), "stop", "--session-dir", str(item.run)], settings=settings)
        if result.returncode:
            left.append(f"{describe_run(item)}: {failure(result)}")
        else:
            print(f"stopped: {describe_run(item)}")
    for key in found.holds:
        result = run([sys.executable, str(BUILD_HOLD), "release", "--holder", key], settings=settings)
        if result.returncode:
            left.append(f"build hold of {key}: {failure(result)}")
        else:
            print(f"released: build hold of {key}")
    for key in found.reservations:
        result = run(["cargo-berth", "release", key, "--json"], cwd=unit.worktree, settings=settings)
        if result.returncode and not (result.returncode == 5
                and "already has a final disposition" in result.stdout + result.stderr):
            left.append(f"berth reservation {key}: {failure(result)}")
        elif result.returncode == 0:
            print(f"released: berth reservation {key}")
    claims = live_claims(found.claims)
    what = {claim.pid: f"Mac claim {claim.what} (pid {claim.pid})" for claim in claims}
    left += terminate([claim.pid for claim in claims], what, settings=settings) if claims else terminate([], {})
    # Cwd alone never gives signal authority; the session and identity must agree.
    inside: set[int] = set(unit_move.processes_inside(unit.worktree)) if unit_move.exists(unit.worktree) else set()
    for pid in found.processes:
        if pid in inside:
            left.extend(stop_orphan(unit, pid, recorded_session_id, settings=settings))
    return left


def remove(unit: Unit, *, settings: conf.ConfigRead) -> list[str]:
    """Remove the worktree, the local branch and the origin branch; a failure comes back."""
    _ = preserve_branch(unit, settings=settings)
    failed: list[str] = []
    if unit_move.exists(unit.worktree):
        result = _git(unit, "worktree", "remove", str(unit.worktree), settings=settings)
        if result.returncode:
            return [f"worktree {unit.worktree}: {failure(result)}"]
        print(f"removed: worktree {unit.worktree}")
    if _git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}", settings=settings).returncode == 0:
        # Unmerged commits were confirmed on origin under the retired ref before marking.
        result = _git(unit, "branch", "-D", unit.branch, settings=settings)
        if result.returncode:
            failed.append(f"branch {unit.branch}: {failure(result)}")
        else:
            print(f"deleted: branch {unit.branch}")
    if _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings).returncode == 0:
        result = _git(unit, "push", "-q", "origin", "--delete", unit.branch, settings=settings)
        if result.returncode:
            failed.append(f"origin {unit.branch}: {failure(result)}")
        else:
            print(f"deleted: origin {unit.branch}")
    _ = _git(unit, "update-ref", "-d", f"refs/remotes/origin/{unit.branch}", settings=settings)
    return failed


def final_stop_check(unit: Unit, ids: frozenset[str], *, settings: conf.ConfigRead) -> list[str]:
    left: list[str] = []
    marked = marked_director(unit)
    if isinstance(marked, unit_lookup.LiveClaude):
        ids |= {marked.session_id}
        left.append(f"marked session {marked.session_id}, pid {marked.pid}")
    for session_id in sorted(ids):
        observed = sessions.session_processes(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown):
            left.append(f"session {session_id}: {observed.reason}")
        else:
            left.extend(f"session {session_id}, pid {pid}" for pid in observed.pids)
    if unit_move.exists(unit.worktree):
        pane = unit_move.unit_pane(unit_lookup.marked_unit(unit.production.slug, unit.name), unit.production.slug)
        left.extend(f"{process_description(pid, settings=settings)} works inside {unit.worktree}; stop it first"
                    for pid in blocking_processes(tuple(unit_move.processes_inside(unit.worktree)), pane))
    return left


def prepare_retirement(unit: Unit, row: staff.StaffRow, *, settings: conf.ConfigRead) -> unit_config.RetiredSession:
    """Validate ownership and retain the original facts before the first destructive step."""
    location = row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise Refused(f"{unit.name}: staff worktree is unfilled; run staff_sources.py fill")
    if location.machine != local_machine():
        raise Refused(f"{unit.name}: staff worktree conflicts with {unit.worktree} on {local_machine()}")
    if unit.worktree.exists():
        actual = run(["git", "-C", str(unit.worktree), "branch", "--show-current"], settings=settings)
        if actual.returncode or actual.stdout.strip() != unit.branch:
            raise Refused(f"{unit.name}: parked branch {unit.branch} conflicts with worktree branch {actual.stdout.strip()}")
    saved = unit_config.read_parked(row.id, cleanup=False)
    if isinstance(saved, unit_config.UnitConfig):
        if not isinstance(saved.retired, unit_config.RetiredSession):
            raise Refused(f"{unit.name}: parked retirement facts are incomplete")
        return saved.retired
    director = director_session(unit)
    if not isinstance(director, DirectorSession):
        raise Refused(f"{unit.name}: no recorded director session for retirement")
    return unit_config.RetiredSession(datetime.now(unit.production.zone).strftime("%Y-%m-%d"),
        director.session_id, unit_config.Branch(unit.branch))


def retire(unit: Unit, wait_minutes: float, *, settings: conf.ConfigRead,
           record: staff.StaffSnapshot | session_staff.ReadCurrentStaff = session_staff.ReadCurrentStaff.CURRENT) -> int:
    record = staff.snapshot() if isinstance(record, session_staff.ReadCurrentStaff) else record
    member = staff_sources.member(staff_sources.UnitMember(unit.production.slug, unit.name), record)
    if isinstance(member, staff_sources.NoSuchMember):
        raise Refused(f"{unit.name}: no staff row; run staff_sources.py fill")
    location = unit_lookup.local_worktree(member)
    facts = prepare_retirement(unit, member.row, settings=settings)
    already_retired = isinstance(member.row.worktree, staff.RemovedWorktree)
    refused = refusals(unit, settings=settings)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    _ = unit_config.park(member.row.id, location.path, facts)
    saved = preserve_branch(unit, settings=settings)
    pane = unit_move.unit_pane(unit_lookup.marked_unit(unit.production.slug, unit.name), unit.production.slug)
    session_id = facts.session_id
    found = gather(unit)
    ids = {item["sessionId"] for item in found.claude}
    if session_id:
        ids.add(session_id)
    left = stop_sessions(unit, found, wait_minutes, frozenset(ids), forget=pane is None)
    if not left:
        remaining = gather(unit)
        ids.update(item["sessionId"] for item in remaining.claude)
        # Only the guarded close can remove the proven shell; all other cwd PIDs block.
        cleanup = remaining._replace(processes=blocking_processes(remaining.processes, pane))
        left = stop_rest(unit, cleanup, session_id, settings=settings)
        if not left and pane is not None:
            try:
                unit_move.close_unit_pane(pane)
            except ValueError as error:
                left.append(str(error))
            else:
                left = stop_sessions(unit, remaining, 0, frozenset(ids))
    if left:
        print("\n".join(f"left running: {reason}" for reason in left))
        print(f"{unit.name} is not retired: rerun once these stop")
        return 1
    from scripts.astra import design_roles
    try:
        design_roles.end_holder(unit.production.slug, unit.name)
    except (OSError, ValueError, SystemExit) as error:
        print(f"{unit.name} is not retired: held role cleanup: {error}")
        return 1
    try:
        left = final_stop_check(unit, frozenset(ids), settings=settings)
    except (Refused, OSError) as error:
        left = [str(error)]
    if left:
        print("\n".join(f"left running: {reason}" for reason in left))
        print(f"{unit.name} is not retired: rerun once these stop")
        return 1
    # All writers are stopped; include commits made during shutdown.
    _ = preserve_branch(unit, saved, settings=settings)
    failed = remove(unit, settings=settings)
    if failed:
        print("\n".join(f"not removed: {reason}" for reason in failed))
    workers = staff.find(kind="worker", parent=member.row.id)
    for worker in workers:
        print(f"kept worker {worker.id} {worker.name} under {member.row.name}")
    if workers:
        return 1
    if not failed:
        _ = staff.set_worktree(member.row.id, staff.RemovedWorktree(local_machine(), location.path),
                               expected=member.row.worktree)
        print(f"{unit.name} already retired" if already_retired else f"{unit.name} retired")
    return 1 if failed else 0


def _main(argv: list[str], *, settings: conf.ConfigRead) -> int:
    from scripts.direct.workers import WorkerUnavailable
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--wait-minutes", type=float, default=WAIT_MINUTES)
    options = parser.parse_args(argv)
    try:
        with worker_lifecycle.operation_deadline(timeout=settings.number("RETIRE_COMMAND_TIMEOUT_SECONDS")):
            record = staff.snapshot()
            unit = find_unit(cast(str, options.slug), cast(str, options.unit), record=record)
            if cast(bool, options.dry_run):
                return dry_run(unit, settings=settings)
            return retire(unit, cast(float, options.wait_minutes), settings=settings, record=record)
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except (Refused, Refusal, unit_names.BadName, WorkerUnavailable, OSError,
            conf.Unusable, unit_config.ConfigRefused, subprocess.SubprocessError) as error:
        print(f"refused: {error}")
        return 1


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv, settings=conf.read("production"))
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
