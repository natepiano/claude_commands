#!/usr/bin/env python3
"""Retire one unit: stop everything it owns, mark its Units row retired, remove its worktree and branch.

Usage: retire_unit.py <slug> <unit> [--dry-run] [--wait-minutes N]

A showrunner runs it through /showrunner:retire_unit. Uncommitted work refuses. Commits the merge
branch lacks are pushed to origin as refs/retired/<staff id> and confirmed before any removal.
The retired note keeps that ref and its full tip, alongside the director session and resume folder.
The `Project started` line the /unit:direct recorder adds to a plan, alone in its file's diff, is not
uncommitted work: no person wrote it. Then it stops what the unit owns. It finds each thing by its own record, never by a name pattern:

- the marked director and each session recorded by the worktree's runs, wherever it works, stop
  through `stop_resume.stop`, which ends its Claude process, and its
  pane launch is then forgotten, so pane restore does not bring it back;
- each run of the worktree that has an app-server stops it and ends its Codex workers
  (`codex_mesh.py stop`);
- the build holds of the unit's sessions release, its berth reservations release, and the
  process of each Mac claim on the worktree stops;
- the unit's exact pane shell does not block retirement; another process inside the worktree
  blocks unless its environment proves the recorded session owns it, that session is dead,
  and the group's worktree, ownership and start times still match before a checked signal.

When nothing is left running, the Units row's Plan cell gets "(retired <date>, worktree removed,
session <id>)", adding ", folder <path>" when its transcript resumes outside the worktree,
committed on the merge branch. Unmerged work adds ", ref refs/retired/<staff id>, tip <SHA>".
Only then do the worktree, local branch and origin branch go. The retired ref stays for resume.
Shutdown completion and forgetting its restore must succeed; a final liveness and record check
refuses marking or removal while a known session still runs or its liveness is unreadable.
--dry-run prints each refusal and everything it would stop and remove, and changes nothing.

Exit 0 when the unit is retired; 1 on a refusal or when something still runs (a rerun resumes);
2 on usage.
"""
from __future__ import annotations


import argparse
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import staff, staff_sources
from scripts.lib import process_group

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
import add_unit  # noqa: E402
import berth_board  # noqa: E402
import production_units  # noqa: E402
import progress_history  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
import unit_names  # noqa: E402
import unit_runs  # noqa: E402
import pane_record  # noqa: E402
from production_units import Production, read_production  # noqa: E402

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
POLL_SECONDS = 10.0
# How long a process has to exit after SIGTERM before it is reported as still running.
TERM_WAIT_SECONDS = 10.0
# A run's folder of detached-run records (codex_mesh.DETACHED_RUNS_DIR).
DETACHED_RUNS = "mesh_runs"
RETIRED_NOTE = "(retired {date}, worktree removed{session})"
# The note names the unit director's last session, which /showrunner:resume_unit resumes.
SESSION_NOTE = ", session {id}"


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


def saved_branch_in_note(plan: str) -> SavedBranch | NoSavedBranch:
    note = re.match(r"^\s*\(retired ([^)]*)\)", plan)
    if note is None:
        return NoSavedBranch()
    saved = re.search(r", ref (refs/retired/[^, ]+), tip ([0-9a-f]{40}(?:[0-9a-f]{24})?)(?=,|$)", note[1])
    if saved:
        return SavedBranch(saved[1], saved[2])
    if ", ref " in note[1] or ", tip " in note[1]:
        raise Refused("retired note has an invalid saved branch ref or tip")
    return NoSavedBranch()


@dataclass(frozen=True)
class DirectorSession:
    session_id: str
    resume_folder: Path


@dataclass(frozen=True)
class NoDirectorSession:
    pass


@dataclass(frozen=True)
class Unit:
    production: Production
    name: str
    worktree: Path
    branch: str
    retired: bool


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


def run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """One command, without this session's id: shutdown then finds the account that holds the unit."""
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    return subprocess.run(argv, pass_fds=staff.operation_fds(), cwd=cwd, env=environment, capture_output=True, text=True, check=False,
                          timeout=900)


def failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"


def production_of(slug: str) -> Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}")
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise Refused(f"{slug} has no readable update timer: {checked.reason}")
    return read_production(checked.path)


def find_unit(slug: str, given: str) -> Unit:
    production = production_of(slug)
    rows = production_units.unit_table(production.doc.read_text(encoding="utf-8").splitlines())
    ids = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    name = given if given in ids else unit_names.unit_id(given)
    matching = [row for row, unit in zip(rows, ids, strict=True) if unit == name]
    if len(matching) != 1:
        raise Refused(f"{name}: expected one Units row, found {len(matching)}")
    row = matching[0]
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser()
    return Unit(production, name, worktree, production_units.cell_value(row.get("Branch", "")),
                production_units.plan_cell_is_retired(row.get("Plan", "")))


def _git(unit: Unit, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(unit.production.checkout), *arguments])


def branch_refs(unit: Unit) -> list[str]:
    """The unit's branch here and on origin, each that exists; origin's is fetched to read it."""
    refs: list[str] = []
    if _git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}").returncode == 0:
        refs.append(f"refs/heads/{unit.branch}")
    remote = _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch)
    if remote.returncode == 0:
        tracking = f"refs/remotes/origin/{unit.branch}"
        fetched = _git(unit, "fetch", "-q", "origin", f"+refs/heads/{unit.branch}:{tracking}")
        if fetched.returncode:
            raise Refused(f"cannot read origin {unit.branch}: {failure(fetched)}")
        refs.append(tracking)
    elif remote.returncode != 2:
        raise Refused(f"cannot read origin {unit.branch}: {failure(remote)}")
    return refs


def unit_plan(unit: Unit) -> str:
    for row in production_units.unit_table(unit.production.doc.read_text(encoding="utf-8").splitlines()):
        if production_units.cell_value(row.get("Unit", "")) == unit.name:
            return row.get("Plan", "")
    raise Refused(f"{unit.name}: Units row is missing")


def confirm_saved_branch(unit: Unit, saved: SavedBranch) -> None:
    remote = _git(unit, "ls-remote", "--exit-code", "origin", saved.ref)
    if remote.returncode or remote.stdout.splitlines() != [f"{saved.tip}\t{saved.ref}"]:
        raise Refused(f"cannot confirm {saved.ref} at {saved.tip}: {failure(remote)}")


def preserve_branch(unit: Unit, saved: SavedBranch | NoSavedBranch = NO_SAVED_BRANCH) -> SavedBranch | NoSavedBranch:
    """Refresh and confirm the saved tip before the note or any removal."""
    if isinstance(saved, NoSavedBranch):
        saved = saved_branch_in_note(unit_plan(unit))
    recorded_tip = saved.tip if isinstance(saved, SavedBranch) else ""
    tip = recorded_tip
    for ref in branch_refs(unit):
        result = _git(unit, "rev-parse", f"{ref}^{{commit}}")
        if result.returncode:
            raise Refused(f"cannot read {ref}: {failure(result)}")
        candidate = result.stdout.strip()
        if tip and _git(unit, "merge-base", "--is-ancestor", candidate, tip).returncode == 0:
            continue
        if tip and _git(unit, "merge-base", "--is-ancestor", tip, candidate).returncode:
            raise Refused(f"{unit.branch}: local, origin and saved tips diverge; keep all before retiring")
        tip = candidate
    if isinstance(saved, NoSavedBranch):
        if not tip or _git(unit, "merge-base", "--is-ancestor", tip, unit.production.merge_branch).returncode == 0:
            return NoSavedBranch()
        member = staff_sources.member(staff_sources.UnitMember(unit.production.slug, unit.name), staff.initial_snapshot())
        if not isinstance(member, staff_sources.MemberFound):
            raise Refused(f"{unit.name}: no staff id for the retired ref")
        saved = SavedBranch(f"refs/retired/{member.row.id}", tip)
    remote = _git(unit, "ls-remote", "--exit-code", "origin", saved.ref)
    if remote.returncode == 0:
        # A previous push may have finished before the note was updated.
        if remote.stdout.splitlines() == [f"{saved.tip}\t{saved.ref}"]:
            remote_tip = saved.tip
        else:
            fetched = _git(unit, "fetch", "-q", "origin", saved.ref)
            if fetched.returncode:
                raise Refused(f"cannot read {saved.ref}: {failure(fetched)}")
            result = _git(unit, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
            if result.returncode:
                raise Refused(f"cannot read {saved.ref}: {failure(result)}")
            remote_tip = result.stdout.strip()
        if recorded_tip and _git(unit, "merge-base", "--is-ancestor", recorded_tip, remote_tip).returncode:
            raise Refused(f"{saved.ref} does not descend from saved tip {recorded_tip}")
        if _git(unit, "merge-base", "--is-ancestor", tip, remote_tip).returncode == 0:
            tip = remote_tip
        elif _git(unit, "merge-base", "--is-ancestor", remote_tip, tip).returncode:
            raise Refused(f"{unit.branch}: branch and retired ref diverge; keep both before retiring")
    elif remote.returncode != 2:
        raise Refused(f"cannot read {saved.ref}: {failure(remote)}")
    updated = SavedBranch(saved.ref, tip)
    if remote.returncode == 2 or remote.stdout.splitlines() != [f"{tip}\t{saved.ref}"]:
        pushed = _git(unit, "push", "origin", f"{tip}:{saved.ref}")
        if pushed.returncode:
            raise Refused(f"cannot preserve {saved.ref}: {failure(pushed)}")
    confirm_saved_branch(unit, updated)
    print(f"kept: {updated.ref} at {updated.tip}")
    return updated


def recorder_line_only(worktree: Path, path: str) -> bool:
    """Whether the file's whole change against HEAD is one added `Project started` line."""
    diff = run(["git", "-C", str(worktree), "diff", "--no-color", "--unified=0", "HEAD", "--", path])
    changed = [line for line in diff.stdout.splitlines()
               if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))]
    return (diff.returncode == 0 and len(changed) == 1 and changed[0].startswith("+")
            and progress_history.PROJECT_STARTED_PATTERN.fullmatch(changed[0][1:]) is not None)


def refusals(unit: Unit) -> list[str]:
    """Refuse unreadable pane ownership and uncommitted work before any stop."""
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
        status = run(["git", "-C", str(unit.worktree), "status", "--short"])
        if status.returncode:
            found.append(f"cannot read {unit.worktree}: {failure(status)}")
        else:
            # The /unit:direct recorder writes its `Project started` line into the plan and never
            # commits it; a unit that never checkpoints keeps it as its only change. No person wrote it.
            paths = [line[3:] for line in status.stdout.splitlines()
                     if not (line[:2] == " M" and recorder_line_only(unit.worktree, line[3:]))]
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
    """Every /unit:direct run folder whose history names the worktree."""
    found: list[Path] = []
    for root in unit_runs.run_roots(os.environ.get("RETIRE_UNIT_RUN_ROOT") or None):
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            if folder.name != "active" and _json(folder / unit_runs.STATE_FILE).get("working_dir") == str(worktree):
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


def command_line(pid: int) -> list[str]:
    """The process's arguments; empty once it has gone. The Mac has no /proc: ps splits them at spaces."""
    if sys.platform == "darwin":
        return run(["ps", "-o", "args=", "-p", str(pid)]).stdout.split()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _session_of(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return 0


def run_processes(worktree: Path, inside: list[int]) -> dict[int, str]:
    """The processes inside the worktree that its runs leave behind once their sessions stop, each named.

    A run's detached command (mesh_runs.py supervise) is found by its record, which holds the
    supervisor's pid and is in the supervisor's arguments; an app-server by its run's
    mesh_server.json; a live server's idle-stop watcher by its mesh entry point and run folder
    arguments. A dead server's watcher needs no matching run history: its own kill -0 rule would
    end it at its next wake. Each leads its own session, so its children go with it.
    """
    runs = runs_of(worktree)
    leaders = {item.pid: f"app-server {item.pid} of run {item.run.name}" for item in mesh_runs(runs)}
    for folder in runs:
        for path in sorted((folder / DETACHED_RUNS).glob("*.json")):
            record = _json(path)
            pid = record.get("pid")
            if (record.get("status") == "running" and isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and str(path) in command_line(pid)):
                leaders[pid] = f"run supervisor {pid} of {record.get('label', path.stem)} in run {folder.name}"
    folders = {str(folder.resolve()): folder.name for folder in runs}
    for pid in inside:
        words = command_line(pid)
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


def _interactive(transcript: Path) -> bool:
    """Whether a transcript is an interactive session's; a Claude worker runs with --print."""
    try:
        with transcript.open(encoding="utf-8") as lines:
            for line in itertools.islice(lines, 50):
                try:
                    entrypoint = cast(dict[str, object], json.loads(line)).get("entrypoint")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if entrypoint is not None:
                    return entrypoint == "cli"
    except OSError:
        return False
    return False


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


def transcript_resume_folder(transcript: Path) -> Path:
    """The first recorded cwd whose project folder still holds this transcript after renames."""
    with transcript.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(value, dict):
                cwd = cast(dict[str, object], value).get("cwd")
                if isinstance(cwd, str) and cwd:
                    folder = Path(cwd).expanduser()
                    if unit_move.project_folder(str(folder)) == transcript.parent.name:
                        return folder
    raise Refused(f"transcript {transcript} has no cwd matching its project folder {transcript.parent.name}")


def director_resume_folder(session_id: str) -> Path:
    transcripts = transcript_locations(session_id)
    if len(transcripts) != 1:
        places = ", ".join(str(path.parent) for path in transcripts) or "none"
        raise Refused(f"no account has the transcript of session {session_id} in exactly one project folder; found: {places}")
    return transcript_resume_folder(transcripts[0])


def last_session(unit: Unit) -> str:
    """The marked director, an existing retirement's session, or the newest interactive transcript
    under the worktree's project folder; empty when none can be found."""
    marked = marked_director(unit)
    if isinstance(marked, unit_lookup.LiveClaude):
        return marked.session_id
    if unit.retired:
        for row in production_units.unit_table(unit.production.doc.read_text(encoding="utf-8").splitlines()):
            if production_units.cell_value(row.get("Unit", "")) == unit.name:
                saved = re.match(r"^\s*\(retired [^)]*, session ([0-9A-Za-z-]+)(?=,|\))", row.get("Plan", ""))
                if saved:
                    return saved[1]
    launches = [entry for entry in pane_record.launches()
                if entry["environment"].get("SHOWRUNNER_UNIT_ID") == unit.name and entry["session_id"]]
    if launches:
        return max(launches, key=lambda entry: entry["written_at"])["session_id"]
    folder = unit_move.project_folder(str(unit.worktree))
    transcripts = [path for directory in unit_move.config_dirs()
                   for path in (directory / "projects" / folder).glob("*.jsonl") if _interactive(path)]
    return max(transcripts, key=lambda path: path.stat().st_mtime).stem if transcripts else ""


def director_session(unit: Unit) -> DirectorSession | NoDirectorSession:
    session_id = last_session(unit)
    return DirectorSession(session_id, director_resume_folder(session_id)) if session_id else NoDirectorSession()


def retired_note(unit: Unit, director: DirectorSession | NoDirectorSession,
                 saved: SavedBranch | NoSavedBranch = NO_SAVED_BRANCH) -> str:
    date = datetime.now(unit.production.zone).strftime("%Y-%m-%d")
    session = ""
    if isinstance(director, DirectorSession):
        session = SESSION_NOTE.format(id=director.session_id)
        if director.resume_folder.resolve() != unit.worktree.resolve():
            session += f", folder {director.resume_folder}"
    if isinstance(saved, SavedBranch):
        session += f", ref {saved.ref}, tip {saved.tip}"
    return RETIRED_NOTE.format(date=date, session=session)


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


def dry_run(unit: Unit) -> int:
    print(f"retire {unit.name} of {unit.production.slug}: dry run, nothing changes")
    refused = refusals(unit)
    for reason in refused:
        print(f"refused: {reason}")
    found = gather(unit)
    lines = ([f"would stop: {describe_session(item)}, through stop_resume.stop" for item in found.claude]
             + [f"would stop: {describe_run(item)}" for item in found.runs]
             + [f"would release: build hold of {key}" for key in found.holds]
             + [f"would release: berth reservation {key}" for key in found.reservations]
             + [f"would stop: Mac claim {item.what} (pid {item.pid})" for item in found.claims]
             + [f"blocks retirement: unowned process {pid}" for pid in found.processes])
    print("\n".join(lines or ["nothing to stop"]))
    if not unit.retired:
        print(f"would mark: the Units row {retired_note(unit, director_session(unit))}, committed on {unit.production.merge_branch}")
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
        outcome = stop_resume.stop(identity)
        if isinstance(outcome, stop_resume.StopFailed):
            failures.append(f"{identity}: stop refused: {outcome.reason}")
            continue
        if isinstance(outcome, stop_resume.SessionNotFound):
            if not isinstance(sessions.session_liveness(identity), sessions.SessionNotLive):
                failures.append(f"{identity}: no local tab; stop refused")
                continue
        print(f"stopped: {identity}")
    if forget and not failures:
        try:
            _ = pane_record.forget_unit(unit.name)
        except (OSError, ValueError) as error:
            failures.append(f"{unit.name}: pane launch stays: {error}")
    return failures


def terminate(pids: list[int], what: dict[int, str], wait: float | None = None) -> list[str]:
    """SIGTERM each pid; one alive after `wait` seconds (TERM_WAIT_SECONDS) comes back as still running."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            what[pid] += f": {error}"
    deadline = time.monotonic() + (TERM_WAIT_SECONDS if wait is None else wait)
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    left: list[str] = []
    for pid in pids:
        if _alive(pid):
            left.append(f"{what[pid]}: still runs after SIGTERM")
        else:
            print(f"stopped: {what[pid]}")
    return left


def process_description(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        parent = fields[1]
    except (OSError, IndexError):
        parent = "unknown"
    return (f"unowned process {pid}, parent {parent}, command {' '.join(command_line(pid)).strip() or 'unknown'}, "
            f"start {proc_start(pid) or 'unknown'}")


def process_session(pid: int) -> str:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for field in raw.split(b"\0"):
        key, _, value = field.partition(b"=")
        if key == b"CLAUDE_CODE_SESSION_ID":
            return value.decode("utf-8")
    return ""


def stop_orphan(unit: Unit, pid: int, recorded_session_id: str = "") -> list[str]:
    """Only a proven, isolated group of this stopped session can be ended."""
    description = process_description(pid)
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
        process_group.send_process_group(pid, signal.SIGTERM)
    except ProcessLookupError:
        return []
    except (OSError, ValueError, Refused) as error:
        return [f"{description} works inside {unit.worktree}; stop it first: {error}"]
    deadline = time.monotonic() + TERM_WAIT_SECONDS
    def still_running() -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state != "Z" and proc_start(pid) == started and _alive(pid)
    while still_running() and time.monotonic() < deadline:
        time.sleep(0.2)
    if still_running():
        return [f"{description}: still runs after SIGTERM"]
    print(f"stopped: {description} of session {session_id}")
    return []


def stop_rest(unit: Unit, found: Found, recorded_session_id: str = "") -> list[str]:
    """Stop owned resources; remaining unowned cwd processes come back as blockers."""
    from scripts.direct import workers
    left: list[str] = []
    workers.end_unit(unit.production.slug, unit.name)
    for item in found.runs:
        result = run([sys.executable, str(MESH), "stop", "--session-dir", str(item.run)])
        if result.returncode:
            left.append(f"{describe_run(item)}: {failure(result)}")
        else:
            print(f"stopped: {describe_run(item)}")
    for key in found.holds:
        result = run([sys.executable, str(BUILD_HOLD), "release", "--holder", key])
        if result.returncode:
            left.append(f"build hold of {key}: {failure(result)}")
        else:
            print(f"released: build hold of {key}")
    for key in found.reservations:
        result = run(["cargo-berth", "release", key, "--json"], cwd=unit.worktree)
        if result.returncode and not (result.returncode == 5
                and "already has a final disposition" in result.stdout + result.stderr):
            left.append(f"berth reservation {key}: {failure(result)}")
        else:
            print(f"released: berth reservation {key}")
    claims = [claim for claim in found.claims if _alive(claim.pid) and proc_start(claim.pid) == claim.proc_start]
    what = {claim.pid: f"Mac claim {claim.what} (pid {claim.pid})" for claim in claims}
    left += terminate([claim.pid for claim in claims], what)
    # Cwd alone never gives signal authority; the session and identity must agree.
    inside: set[int] = set(unit_move.processes_inside(unit.worktree)) if unit_move.exists(unit.worktree) else set()
    for pid in found.processes:
        if pid in inside:
            left.extend(stop_orphan(unit, pid, recorded_session_id))
    return left


def mark_retired(unit: Unit, director: DirectorSession | NoDirectorSession,
                 saved: SavedBranch | NoSavedBranch = NO_SAVED_BRANCH) -> None:
    """Put the retired note in front of the Units row's Plan cell and commit the doc on the merge branch."""
    doc = unit.production.doc
    lines = doc.read_text(encoding="utf-8").split("\n")
    headings = production_units.unit_headings(lines)
    _, rows = production_units.unit_rows(lines)
    plan_at = headings.index("Plan") + 1
    for index, line in enumerate(lines):
        parts = line.split("|")
        if line in rows and production_units.cell_value(parts[headings.index("Unit") + 1].strip()) == unit.name:
            plan = parts[plan_at].strip()
            if production_units.plan_cell_is_retired(plan):
                if isinstance(saved, SavedBranch):
                    previous = saved_branch_in_note(plan)
                    if isinstance(previous, SavedBranch):
                        plan = plan.replace(f", ref {previous.ref}, tip {previous.tip}",
                                            f", ref {saved.ref}, tip {saved.tip}", 1)
                    else:
                        plan = plan.replace(")", f", ref {saved.ref}, tip {saved.tip})", 1)
            else:
                plan = f"{retired_note(unit, director, saved)} {plan}"
            if plan != parts[plan_at].strip():
                parts[plan_at] = f" {plan} "
                lines[index] = "|".join(parts)
                _ = doc.write_text("\n".join(lines), encoding="utf-8")
            break
    add_unit.commit_paths(unit.production, [doc], f"production({unit.production.slug}): retire {unit.name}")
    print(f"marked: the retired Units row, committed on {unit.production.merge_branch}")


def remove(unit: Unit) -> list[str]:
    """Remove the worktree, the local branch and the origin branch; a failure comes back."""
    saved = preserve_branch(unit)
    if saved != saved_branch_in_note(unit_plan(unit)):
        mark_retired(unit, NoDirectorSession(), saved)
    failed: list[str] = []
    if unit_move.exists(unit.worktree):
        result = _git(unit, "worktree", "remove", str(unit.worktree))
        if result.returncode:
            return [f"worktree {unit.worktree}: {failure(result)}"]
        print(f"removed: worktree {unit.worktree}")
    if _git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}").returncode == 0:
        # Unmerged commits were confirmed on origin under the retired ref before marking.
        result = _git(unit, "branch", "-D", unit.branch)
        if result.returncode:
            failed.append(f"branch {unit.branch}: {failure(result)}")
        else:
            print(f"deleted: branch {unit.branch}")
    if _git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch).returncode == 0:
        result = _git(unit, "push", "-q", "origin", "--delete", unit.branch)
        if result.returncode:
            failed.append(f"origin {unit.branch}: {failure(result)}")
        else:
            print(f"deleted: origin {unit.branch}")
    _ = _git(unit, "update-ref", "-d", f"refs/remotes/origin/{unit.branch}")
    return failed


def final_stop_check(unit: Unit, ids: frozenset[str]) -> list[str]:
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
        left.extend(f"{process_description(pid)} works inside {unit.worktree}; stop it first"
                    for pid in unit_move.processes_inside(unit.worktree)
                    if pid != os.getpid() and (pane is None or pid != pane.shell))
    return left


def retire(unit: Unit, wait_minutes: float) -> int:
    refused = refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    member = staff_sources.member(staff_sources.UnitMember(unit.production.slug, unit.name), staff.initial_snapshot())
    saved = preserve_branch(unit)
    pane = unit_move.unit_pane(unit_lookup.marked_unit(unit.production.slug, unit.name), unit.production.slug)
    # Read before the stop: once stopped, the unit's mark is gone.
    director = director_session(unit) if not unit.retired else NoDirectorSession()
    session_id = director.session_id if isinstance(director, DirectorSession) else last_session(unit)
    found = gather(unit)
    ids = {item["sessionId"] for item in found.claude}
    if session_id:
        ids.add(session_id)
    left = stop_sessions(unit, found, wait_minutes, frozenset(ids), forget=pane is None)
    if not left:
        remaining = gather(unit)
        ids.update(item["sessionId"] for item in remaining.claude)
        # Only the guarded close can remove the proven shell; all other cwd PIDs block.
        cleanup = remaining._replace(processes=tuple(pid for pid in remaining.processes
            if pane is None or pid != pane.shell))
        left = stop_rest(unit, cleanup, session_id)
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
        left = final_stop_check(unit, frozenset(ids))
    except (Refused, OSError) as error:
        left = [str(error)]
    if left:
        print("\n".join(f"left running: {reason}" for reason in left))
        print(f"{unit.name} is not retired: rerun once these stop")
        return 1
    # All writers are stopped; include commits made during shutdown.
    saved = preserve_branch(unit, saved)
    # The note may have reached disk before its commit; finish it before removal.
    mark_retired(unit, director, saved)
    failed = remove(unit)
    print("\n".join(f"not removed: {reason}" for reason in failed) if failed else f"{unit.name} retired")
    if isinstance(member, staff_sources.MemberFound):
        workers = staff.find(kind="worker", parent=member.row.id)
        for worker in workers:
            print(f"kept worker {worker.id} {worker.name} under {member.row.name}")
        if workers:
            return 1
        if not failed:
            _ = staff.retire(member.row.id)
    return 1 if failed else 0


def _main(argv: list[str]) -> int:
    from scripts.direct.workers import WorkerUnavailable
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--wait-minutes", type=float, default=WAIT_MINUTES)
    options = parser.parse_args(argv)
    try:
        unit = find_unit(cast(str, options.slug), cast(str, options.unit))
        if cast(bool, options.dry_run):
            return dry_run(unit)
        return retire(unit, cast(float, options.wait_minutes))
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except (Refused, production_units.Refusal, unit_names.BadName, WorkerUnavailable, OSError) as error:
        print(f"refused: {error}")
        return 1


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
