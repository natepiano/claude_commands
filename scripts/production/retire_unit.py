#!/usr/bin/env python3
"""Retire a staff unit without deleting uncommitted or unmerged work."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast


from scripts.production import staff, staff_sources, unit_config
from scripts.copies import copies
from scripts.shutdown.remote import local_machine
from scripts.shutdown import stop_resume
from scripts.message import sessions
from scripts.lib import process_group, conf
from scripts.herdr import worker_panes
from scripts.lib.conf import UseConfiguredValue
from scripts.agents import worker_lifecycle
from scripts.lib import berth_board
from scripts.direct import progress_history
from scripts.herdr import herdr
from scripts.production import unit_lookup
from scripts.production import unit_move
from scripts.lib import unit_runs
from scripts.production.production_doc import Production, Refusal, read_production

SCRIPTS = Path(__file__).resolve().parent.parent
DETACHED_RUNS = "mesh_runs"
BUILD_HOLD = SCRIPTS / "build_hold" / "build_hold.py"
MAC_CLAIMS = Path.home() / ".local/state/mac-test/claims"


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Unit:
    production: Production
    name: str
    worktree: Path
    branch: str
    staff_member: staff.StaffRow


class MacClaim(NamedTuple):
    path: Path
    what: str
    pid: int
    proc_start: str


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


def production_of(production_name: str, *, record: staff.StaffSnapshot) -> Production:
    found = staff_sources.member(staff_sources.ShowrunnerMember(production_name), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(found.row.doc, staff.DocUnfilled):
        raise ValueError(f"{production_name}: no showrunner staff document")
    if found.row.doc.machine != local_machine():
        raise ValueError(f"{production_name}: run retire on {found.row.doc.machine}")
    return read_production(found.row)


def _git(unit: Unit, *arguments: str, settings: conf.ConfigRead) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(unit.production.checkout), *arguments], settings=settings)


def recorder_line_only(worktree: Path, path: str, *, settings: conf.ConfigRead) -> bool:
    """Whether the file's whole change against HEAD is one added `Project started` line."""
    diff = run(["git", "-C", str(worktree), "diff", "--no-color", "--unified=0", "HEAD", "--", path], settings=settings)
    changed = [line for line in diff.stdout.splitlines()
               if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))]
    return (diff.returncode == 0 and len(changed) == 1 and changed[0].startswith("+")
            and progress_history.PROJECT_STARTED_PATTERN.fullmatch(changed[0][1:]) is not None)


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


def _json(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def command_line(pid: int, *, settings: conf.ConfigRead) -> list[str]:
    """The process's arguments; empty once it has gone. The Mac has no /proc: ps splits them at spaces."""
    if sys.platform == "darwin":
        return run(["ps", "-o", "args=", "-p", str(pid)], settings=settings).stdout.split()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


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


def _session_of(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return 0


def run_processes(worktree: Path, inside: list[int], *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[int, str]:
    """Name the detached command processes that remain in the worktree."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    runs = runs_of(worktree)
    leaders: dict[int, str] = {}
    for folder in runs:
        for path in sorted((folder / DETACHED_RUNS).glob("*.json")):
            record = _json(path)
            pid = record.get("pid")
            if (record.get("status") == "running" and isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and str(path) in command_line(pid, settings=settings)):
                leaders[pid] = f"run supervisor {pid} of {record.get('label', path.stem)} in run {folder.name}"
    return {pid: f"process {pid} ({_command_name(pid)}) of the {leaders[leader]}"
            for pid in inside if (leader := _session_of(pid)) in leaders}


def _command_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def find_unit(production_name: str, given: str, *,
              record: staff.StaffSnapshot | None = None,
              settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Unit:
    record = staff.snapshot() if record is None else record
    production = production_of(production_name, record=record)
    member = record.by_id(staff.StaffId(given))
    if member is None or member.kind != "unit":
        selected = staff_sources.member(staff_sources.UnitMember(production.name, given), record)
        if isinstance(selected, staff_sources.NoSuchMember):
            raise ValueError(f"{production.name}/{given}: no matching staff member")
        member = selected.row
    location = unit_lookup.local_worktree(staff_sources.MemberFound(member))
    branch = ""
    if location.path.is_dir():
        result = run(["git", "-C", str(location.path), "branch", "--show-current"], settings=settings)
        branch = result.stdout.strip()
    return Unit(production, member.name, location.path, branch, member)


def last_session(unit: Unit) -> str:
    return unit.staff_member.current_session


def refusals(unit: Unit, *, settings: conf.ConfigRead) -> list[str]:
    """Keep uncommitted work and commits outside the merge branch."""
    if not unit.worktree.is_dir():
        return []
    status = run(["git", "-C", str(unit.worktree), "status", "--short"], settings=settings)
    if status.returncode:
        raise OSError(f"cannot read {unit.worktree}: {failure(status)}")
    paths = [line[3:] for line in status.stdout.splitlines()
             if not (line[:2] == " M" and recorder_line_only(unit.worktree, line[3:], settings=settings))]
    found = [f"uncommitted work in {unit.worktree}: {', '.join(paths)}"] if paths else []
    refs = [f"refs/heads/{unit.branch}"] if unit.branch else ["HEAD"]
    remote = _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings)
    if remote.returncode == 0:
        fetched = _git(unit, "fetch", "--no-write-fetch-head", "--refmap=", "-q", "origin", unit.branch, settings=settings)
        if fetched.returncode:
            raise OSError(failure(fetched))
        refs.append(remote.stdout.split()[0])
    for ref in refs:
        commits = _git(unit, "rev-list", "--no-merges", ref, "--not", unit.production.merge_branch, settings=settings)
        if commits.returncode:
            raise OSError(failure(commits))
        if commits.stdout.strip():
            found.append(f"unmerged commits on {unit.branch or ref}: {commits.stdout.splitlines()[0]}")
            break
    return found


def members(unit: Unit, record: staff.StaffSnapshot) -> list[staff.StaffRow]:
    return [unit.staff_member, *record.find(kind="worker", parent=unit.staff_member.id)]


def dry_run(unit: Unit, *, settings: conf.ConfigRead) -> int:
    print(f"retire {unit.name} of {unit.production.name}: dry run, nothing changes")
    refused = refusals(unit, settings=settings)
    for reason in refused:
        print(f"refused: {reason}")
    for row in members(unit, staff.snapshot()):
        print(f"would stop: session {row.current_session}")
    trees = copies.owned_trees(str(unit.staff_member.id))
    for tree in (*trees.standing, *(entry[0] for entry in trees.workspaces)):
        print(f"would remove: checkout {tree}")
    print(f"would remove: worktree {unit.worktree}")
    print(f"would delete: branch {unit.branch} here and on origin")
    return int(bool(refused))


def remove(unit: Unit, *, settings: conf.ConfigRead) -> list[str]:
    failed: list[str] = []
    if unit.worktree.exists():
        result = _git(unit, "worktree", "remove", "--force", str(unit.worktree), settings=settings)
        if result.returncode:
            return [failure(result)]
        print(f"removed: worktree {unit.worktree}")
    if unit.branch:
        if _git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}", settings=settings).returncode == 0:
            result = _git(unit, "branch", "-D", unit.branch, settings=settings)
            if result.returncode:
                failed.append(failure(result))
        if _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings).returncode == 0:
            result = _git(unit, "push", "-q", "origin", "--delete", unit.branch, settings=settings)
            if result.returncode:
                failed.append(failure(result))
    return failed


def release_reservations(unit: Unit, *, settings: conf.ConfigRead) -> None:
    for key in reservations(unit.worktree):
        result = run(["cargo-berth", "release", key, "--json"], cwd=unit.worktree, settings=settings)
        if result.returncode and not (result.returncode == 5 and "already has a final disposition" in result.stdout + result.stderr):
            print(f"anomaly: {failure(result)}")


def retire(unit: Unit, *, settings: conf.ConfigRead,
           record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    row = unit.staff_member
    if isinstance(row.worktree, staff.RemovedWorktree) and not unit.worktree.exists():
        try:
            copies.remove_trees(str(row.id))
        except copies.CopyError as error:
            print(f"anomaly: {error}".replace("\n", " "))
        print("unchanged")
        return 0
    refused = refusals(unit, settings=settings)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    config = unit_config.read(unit.worktree, cleanup=False)
    if isinstance(config, unit_config.UnitConfig):
        saved = unit_config.read_parked(row.id, cleanup=False)
        facts = (saved.retired if isinstance(saved, unit_config.UnitConfig)
                 and isinstance(saved.retired, unit_config.RetiredSession) else
                 unit_config.RetiredSession(datetime.now(unit.production.zone).date().isoformat(),
                                            row.current_session, unit_config.Branch(unit.branch)))
        _ = unit_config.write_parked(row.id, replace(config, retired=facts))
    owned = members(unit, record)
    pane_workers = worker_panes.observe(row.id, record)
    pane_ids = {item.row.id for item in pane_workers}
    for member in owned:
        if member.id in pane_ids or isinstance(record.backends.get(member.id), staff.CodexBackend):
            continue
        try:
            outcome = stop_resume.stop(member.current_session, deadline=worker_lifecycle.effective_deadline())
            if isinstance(outcome, stop_resume.StopFailed):
                print(f"anomaly: {member.name}: {outcome.reason}")
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: {member.name}: {' '.join(str(error).splitlines())}")
    stop_reasons = stop_resume.stop_unit_workers(row.id, record)
    for reason in stop_reasons:
        print(f"anomaly: {reason}")
    for key in holds({member.current_session for member in owned}):
        result = run([sys.executable, str(BUILD_HOLD), "release", "--holder", key], settings=settings)
        if result.returncode:
            print(f"anomaly: {failure(result)}")
    release_reservations(unit, settings=settings)
    claims = live_claims(mac_claims(unit.worktree))
    for reason in terminate([claim.pid for claim in claims], {claim.pid: claim.what for claim in claims}, settings=settings):
        print(f"anomaly: {reason}")
    inside = unit_move.processes_inside(unit.worktree) if unit.worktree.exists() else []
    classified = run_processes(unit.worktree, inside, settings=settings)
    for reason in terminate(list(classified), classified, settings=settings):
        print(f"anomaly: {reason}")
    for pid in inside:
        if pid != os.getpid() and pid not in classified:
            for reason in stop_orphan(unit, pid, row.current_session, settings=settings):
                print(f"anomaly: {reason}")
    # Recheck work after sessions stop, before removing the folder.
    refused = refusals(unit, settings=settings)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    remaining = worker_panes.observe(row.id, staff.snapshot()) if pane_workers else []
    if any(isinstance(item, (worker_panes.OpenWorker, worker_panes.UncertainWorker)) for item in remaining):
        return 1
    try:
        copies.remove_trees(str(row.id))
    except copies.RemoteMirrorKept as error:
        print(f"refused: {error}")
        return 1
    except copies.CopyError as error:
        print(f"anomaly: {error}".replace("\n", " "))
    failed = remove(unit, settings=settings)
    if failed:
        print("\n".join(f"anomaly: {reason}" for reason in failed))
        return 1
    _ = staff.set_worktree(row.id, staff.RemovedWorktree(local_machine(), unit.worktree), expected=row.worktree)
    print(f"{unit.name} retired")
    return 0


def _main(argv: list[str], *, settings: conf.ConfigRead) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("production_name")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        record = staff.snapshot()
        unit = find_unit(cast(str, options.production_name), cast(str, options.unit), record=record, settings=settings)
        if cast(bool, options.dry_run):
            return dry_run(unit, settings=settings)
        return retire(unit, settings=settings, record=record)
    except (Refused, Refusal, ValueError, OSError, copies.CopyError, conf.Unusable, unit_config.ConfigRefused, subprocess.SubprocessError) as error:
        print(f"retire: {error}")
        return 1


def main(argv: list[str]) -> int:
    with staff.operation():
        return staff.cli_result(lambda: _main(argv, settings=conf.read("production")))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
