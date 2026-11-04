"""Move a unit folder, or share stopped-unit move steps with rename commands.

Folder moves stop, move, publish the location, and resume the unit.
Rename callers control their own step order and progress records.
"""
from __future__ import annotations

import fcntl
import argparse
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from subprocess import Popen
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
import progress_history  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import herdr  # noqa: E402
import pane_record  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from rename_state import RenameRefused, read_json, write_json  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.herdr.pane_launch import agent_kind  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.shutdown import remote, stop_resume  # noqa: E402

BERTH_KEY = "cargoBerthTarget"


@contextmanager
def production_lock() -> Generator[None]:
    """The one lock every production and unit rename holds; it lives outside each renamed folder."""
    tidy_wrapped.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (tidy_wrapped.STATE_DIR / "rename.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class PathMove(NamedTuple):
    source: Path
    destination: Path


def git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise RenameRefused(f"git {' '.join(arguments)}: {failure_detail(result)}")
    return result.stdout.rstrip("\n")


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def move(path_move: PathMove) -> None:
    if not exists(path_move.source):
        return
    if exists(path_move.destination):
        raise RenameRefused(f"destination already exists: {path_move.destination}")
    path_move.destination.parent.mkdir(parents=True, exist_ok=True)
    _ = path_move.source.rename(path_move.destination)


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RenameRefused("invalid rename journal: expected an object")
    return cast(dict[str, object], value)


def plan_paths(cell: str, checkout: Path) -> tuple[Path, ...]:
    """The tracked plan files under docs/plans that a unit's plan value names."""
    names = [match[1] for match in re.finditer(r"`([^`]+)`", cell)]
    if not names:
        if re.search(r"\s", cell.strip()):
            return ()
        names = [cell.strip()]
    paths: list[Path] = []
    for name in names:
        path = Path(name).expanduser()
        path = (path if path.is_absolute() else checkout / path).resolve()
        try:
            relative = path.relative_to(checkout)
        except ValueError:
            continue
        if relative.parts[:2] != ("docs", "plans") or path.suffix != ".md" or not path.is_file():
            continue
        if git(checkout, "ls-files", "-z", "--", f":(literal){relative}"):
            paths.append(path)
    return tuple(paths)


def branch_tip(checkout: Path, branch: str) -> str:
    return git(checkout, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")


def remote_tip(checkout: Path, branch: str) -> str:
    listed = git(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return listed.split("\t", 1)[0] if listed else ""


def delete_remote_branch(checkout: Path, branch: str, tip: str) -> None:
    remote = remote_tip(checkout, branch)
    if remote:
        if remote != tip:
            try:
                _ = git(checkout, "merge-base", "--is-ancestor", remote, tip)
            except RenameRefused as error:
                raise RenameRefused(f"origin branch {branch} has history outside the preserved tip") from error
        # The lease deletes only the tip just checked: a push that lands meanwhile keeps the branch.
        _ = git(checkout, "push", f"--force-with-lease=refs/heads/{branch}:{remote}", "origin", "--delete", branch)


def delete_local_branch(checkout: Path, branch: str, tip: str) -> None:
    local = branch_tip(checkout, branch)
    if local:
        if local != tip:
            raise RenameRefused(f"branch {branch} moved during the rename")
        _ = git(checkout, "update-ref", "-d", f"refs/heads/{branch}", tip)
        _ = subprocess.run(["git", "-C", str(checkout), "config", "--remove-section", f"branch.{branch}"],
                           capture_output=True, check=False)


def delete_branch(checkout: Path, branch: str, tip: str) -> None:
    delete_remote_branch(checkout, branch, tip)
    delete_local_branch(checkout, branch, tip)


def check_tip(checkout: Path, branch: str, tip: str) -> None:
    current = branch_tip(checkout, branch)
    if current and current != tip:
        raise RenameRefused(f"branch {branch} moved during the rename")


def push_branch(checkout: Path, old_branch: str, new_branch: str, tip: str,
                retarget: tuple[str, str] = ("", "")) -> None:
    """Create the new branch at the old tip, carry its cargo-berth target, and push it.

    A production rename passes (old merge branch, new merge branch) as `retarget`, so a target
    on the production's merge branch follows it; a target set to any other branch is kept.
    """
    check_tip(checkout, old_branch, tip)
    local, remote = branch_tip(checkout, new_branch), remote_tip(checkout, new_branch)
    if (local and local != tip) or (remote and remote != tip):
        raise RenameRefused(f"branch {new_branch} moved during the rename")
    if not local:
        _ = git(checkout, "branch", new_branch, tip)
    berth = subprocess.run(["git", "-C", str(checkout), "config", "--get", f"branch.{old_branch}.{BERTH_KEY}"],
                           capture_output=True, text=True, check=False).stdout.strip()
    if berth:
        target = retarget[1] if retarget[0] and berth == retarget[0] else berth
        _ = git(checkout, "config", f"branch.{new_branch}.{BERTH_KEY}", target)
    _ = git(checkout, "push", "--set-upstream", "origin", f"{new_branch}:refs/heads/{new_branch}")


def move_worktree(checkout: Path, old_branch: str, new_branch: str, old: Path, new: Path, tip: str) -> None:
    check_tip(checkout, old_branch, tip)
    if old.exists():
        if exists(new):
            raise RenameRefused(f"destination already exists: {new}")
        _ = git(checkout, "worktree", "move", str(old), str(new))
    if not (new / ".git").is_file():
        raise RenameRefused(f"unit worktree missing: {new}")
    _ = git(checkout, "worktree", "repair", str(new))
    current = git(new, "branch", "--show-current")
    if current not in (old_branch, new_branch):
        raise RenameRefused(f"{new} switched to {current}")
    check_tip(checkout, old_branch, tip)
    _ = git(new, "switch", new_branch)



def copy_trust(path: Path, old: str, new: str) -> None:
    config = json_object(read_json(path))
    projects = json_object(config.get("projects", {}))
    if old not in projects:
        return
    trust = projects[old]
    for attempt in range(3):
        if attempt:
            config = json_object(read_json(path))
            projects = json_object(config.get("projects", {}))
        _ = projects.setdefault(old, trust)
        projects[new] = trust
        config["projects"] = projects
        write_json(path, config)
        time.sleep(2)
        verified = json_object(read_json(path))
        if new in json_object(verified.get("projects", {})):
            return
    raise RenameRefused(f"{path}: new worktree trust entry did not persist; next launch needs a trust prompt")


def project_folder(worktree: str) -> str:
    """The folder name Claude keeps a worktree's transcripts under."""
    return re.sub(r"[^A-Za-z0-9]", "-", worktree)


def move_configs(old: str, new: str) -> None:
    """Move the transcript folders and copy the trust entry in every Claude config dir."""
    old_project, new_project = project_folder(old), project_folder(new)
    for directory in sessions.config_dirs():
        move(PathMove(directory / "projects" / old_project, directory / "projects" / new_project))
        config = (Path.home() / ".claude.json" if directory == Path.home() / ".claude"
                  else directory / ".claude.json")
        if config.exists():
            copy_trust(config, old, new)


def _events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = cast(object, json.loads(line))
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return events


def newest_run(worktree: Path) -> Path | None:
    """The newest run record matching the worktree's exact recorded spelling, finished or not."""
    run = unit_lookup.read_run_states().records.get(str(worktree))
    return run.path if run is not None else None


def open_run(worktree: Path) -> Path | None:
    """The record of the worktree's newest /unit:direct run when that run has not finished."""
    run = unit_lookup.read_run_states().records.get(str(worktree))
    return run.path if run is not None and run.state is unit_lookup.UnitState.RUNNING else None


def open_phase(path: Path) -> str:
    """The id of a phase the run started and has not finished, or nothing."""
    started: dict[str, str] = {}
    for event in _events(path):
        instance = str(event.get("phase_instance_id", ""))
        if event.get("event_type") == "phase_started":
            started[instance] = str(event.get("phase_id", "?"))
        elif event.get("event_type") == "phase_finished":
            _ = started.pop(instance, None)
    return next(iter(started.values()), "")


def move_run(path: Path, old: Path, new: Path, new_branch: str) -> None:
    """Point a run, its record and its recorder state, at the moved worktree and new branch."""
    _ = progress_history.move_run(path, old, new, new_branch)


def merge_into(worktree: Path, ref: str, message: str) -> str:
    """Merge `ref` into the worktree's branch while its unit is stopped; a conflict leaves it untouched."""
    if subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", ref, "HEAD"],
                      capture_output=True, check=False).returncode == 0:
        return git(worktree, "rev-parse", "HEAD")
    merged = subprocess.run(["git", "-C", str(worktree), "merge", "--no-ff", "-m", message, ref],
                            capture_output=True, text=True, check=False)
    if merged.returncode:
        _ = subprocess.run(["git", "-C", str(worktree), "merge", "--abort"], capture_output=True, check=False)
        raise RenameRefused(f"merging {ref} into {worktree} conflicts; resolve it there, then rerun")
    return git(worktree, "rev-parse", "HEAD")


def processes_inside(worktree: Path) -> list[int]:
    """Pids on this machine whose working directory is the worktree or inside it."""
    if sys.platform == "darwin":
        return _darwin_processes_inside(worktree)
    found: list[int] = []
    root = str(worktree)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # A working directory that was removed reads "<path> (deleted)".
            cwd = os.readlink(entry / "cwd").removesuffix(" (deleted)")
        except OSError:
            continue
        if cwd == root or cwd.startswith(f"{root}/"):
            found.append(int(entry.name))
    return sorted(found)


class UnitConversation(NamedTuple):
    """The conversation and account saved at the unit's Claude launch."""
    session_id: str
    account: str


class NoUnitConversation(NamedTuple):
    """No launch entry identifies this unit's conversation."""


def unit_conversation(found: unit_lookup.MarkedUnit | None, *, session_id: str = "") -> UnitConversation | NoUnitConversation:
    """Use the live director's id, then its pane launch; retained launches survive a stop."""
    from scripts.accounts import accounts

    if found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        session_id = found.claude.session_id
    if not session_id:
        return NoUnitConversation()
    if found is not None:
        entry = pane_record.read(found.place.session).get(found.place.pane)
        if entry is not None and entry["session_id"] == session_id and entry["config_dir"]:
            return UnitConversation(session_id, accounts.claude_config_label(Path(entry["config_dir"])))
    try:
        entry = pane_record.launch_for_session(session_id)
    except ValueError as error:
        raise RenameRefused(str(error)) from error
    return (UnitConversation(session_id, accounts.claude_config_label(Path(entry["config_dir"])))
            if entry is not None and entry["config_dir"] else NoUnitConversation())



class UnitPane(NamedTuple):
    unit: str
    place: herdr.HerdrPlace
    shell: int


def unit_pane(found: unit_lookup.MarkedUnit | None, _production: str) -> UnitPane | None:
    """Find the unit's exact pane; an exec-launched Claude leaves no shell to close."""
    if found is None:
        return None
    try:
        shell = pane_record.pane_shell_pid(found.place)
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        raise RenameRefused(f"{found.unit}: cannot find its pane shell: {error}") from error
    if isinstance(found.claude, unit_lookup.LiveClaude) and shell == found.claude.pid:
        return None
    return UnitPane(found.unit, found.place, shell)


def close_unit_pane(pane: UnitPane | None) -> None:
    """Close the unit's exact session-qualified pane after the caller stops the unit."""
    if pane is None:
        return
    try:
        herdr.Herdr(pane.place.session).close_retained_pane(pane.place.pane)
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        raise RenameRefused(f"{pane.unit}: pane close failed: {error}") from error


def _darwin_processes_inside(worktree: Path) -> list[int]:
    """The Mac has no /proc: lsof names every process's working directory, resolved, so /tmp reads as /private/tmp."""
    listed = subprocess.run(["lsof", "-w", "-d", "cwd", "-Fpn"], capture_output=True, text=True, check=False)
    if listed.returncode and not listed.stdout:
        raise RenameRefused(f"cannot list working directories: {listed.stderr.strip() or 'lsof failed'}")
    root = str(worktree.resolve())
    found: set[int] = set()
    pid = 0
    for field in listed.stdout.splitlines():
        if field.startswith("p"):
            pid = int(field[1:])
        elif field.startswith("n") and (field[1:] == root or field[1:].startswith(f"{root}/")):
            found.add(pid)
    return sorted(found)


@dataclass(frozen=True)
class FolderLaunch:
    """The retained launch used to stop and resume the unit."""

    session: str
    entry: pane_record.PaneLaunch


@dataclass(frozen=True)
class NoFolderLaunch:
    """No retained launch identifies the unit."""


def folder_launch(row: staff.StaffRow) -> FolderLaunch | NoFolderLaunch:
    matches = [FolderLaunch(path.stem, entry)
               for path in sorted(pane_record.record_directory().glob("*.json"))
               for entry in pane_record.read_strict(path.stem).values()
               if entry["session_id"] in (row.id, row.current_session)]
    if not matches:
        print(f"unit_move: {row.id}: no retained launch", file=sys.stderr)
        return NoFolderLaunch()
    current = [launch for launch in matches if launch.entry["session_id"] == row.current_session]
    return next((launch for launch in reversed(current or matches) if not launch.entry.get("ended")),
                (current or matches)[-1])


def _folder_resume_notice(row: staff.StaffRow, launch: FolderLaunch) -> str:
    entry = launch.entry.copy()
    entry["session_id"] = row.current_session
    return shlex.join([agent_kind(entry), *pane_record.resume_arguments(entry)])


def stop_folder_session(row: staff.StaffRow, launch: FolderLaunch) -> bool:
    """Signal only the registered session process in the retained pane."""
    client = herdr.Herdr(launch.session)
    pane = launch.entry["pane_id"]
    live = sessions.session_liveness(row.current_session)
    if isinstance(live, sessions.SessionNotLive):
        return False
    if agent_kind(launch.entry) != "claude":
        raise ValueError(f"{row.id}: target process is not proved; resume with {_folder_resume_notice(row, launch)}")
    if not isinstance(live, sessions.LiveSessionProcess):
        raise ValueError(f"{row.id}: target process is not proved; resume with {_folder_resume_notice(row, launch)}")
    if not any(current["pane_id"] == pane for current in client.panes()):
        raise ValueError(f"{row.id}: target pane is absent; no signal sent")
    info = herdr.json_object(client.result("pane", "process-info", "--pane", pane).get("process_info"),
                             "folder move process info")
    if not any(herdr.json_object(raw, "foreground process").get("pid") == live.pid
               for raw in herdr.json_items(info.get("foreground_processes"), "foreground processes")):
        raise ValueError(f"{row.id}: target process is outside the retained pane; no signal sent")
    pane_record.end(launch.session, pane, row.current_session, record=staff.StaffSnapshot((row,)))
    os.kill(live.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while not isinstance(sessions.session_liveness(row.current_session), sessions.SessionNotLive):
        if time.monotonic() >= deadline:
            raise ValueError(f"{row.id}: session stop is incomplete; resume with {_folder_resume_notice(row, launch)}")
        time.sleep(0.05)
    return True


def resume_folder_session(row: staff.StaffRow, launch: FolderLaunch,
                          old: Path, new: Path, runs: tuple[Path, ...]) -> None:
    """Resume the unit with the commands for its existing workers."""
    from scripts.lib.resume_message import resume_message

    entry = launch.entry
    projected = entry.copy()
    projected["session_id"] = row.current_session
    message = resume_message("the unit folder moved", moved_from=str(old), moved_to=str(new))
    environment = {key: value for key, value in entry["environment"].items()
                   if key not in {"STAFF_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "HERDR_TAB_ID"}}
    command = ["env", "-u", "STAFF_ID", "-u", "CODEX_THREAD_ID", "-u", "CLAUDE_CODE_SESSION_ID", "-u", "HERDR_TAB_ID",
               *(f"{key}={value}" for key, value in environment.items()),
               "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", str(new),
               sys.executable, str(Path(__file__).resolve().parents[1] / "accounts/accounts.py"),
               "exec", Path(entry["config_dir"]).name.removeprefix("."),
               *pane_record.resume_arguments(projected), message]
    client = herdr.Herdr(launch.session)
    if (agent_kind(entry) != "claude"
            or not any(pane["pane_id"] == entry["pane_id"] for pane in client.panes())
            or not isinstance(sessions.session_liveness(row.current_session), sessions.SessionNotLive)
            or not isinstance(client.pane_activity(entry["pane_id"]), herdr.PaneAtShell)):
        print(f"unit_move: {row.id}: {_folder_resume_notice(row, launch)}: resume is not safe; run {shlex.join(command)}", file=sys.stderr)
        return
    resume_folder_workers(row, runs, old, new)
    client.run_in_pane(entry["pane_id"], command)
    with pane_record.locked(launch.session):
        entries = pane_record.read_strict(launch.session)
        current = entries.get(entry["pane_id"])
        if current is not None:
            current["ended"] = False
            pane_record.save(launch.session, entries)


def folder_runs(row: staff.StaffRow) -> tuple[Path, ...]:
    """Find run folders by the unit's session identity."""
    return tuple(sorted({path.parent for root in progress_history.SESSION_ROOTS
                         for path in root.glob(f"*/{unit_runs.STATE_FILE}")
                         if unit_runs.director(path.parent) == row.current_session}))


def pause_folder_workers(runs: tuple[Path, ...]) -> None:
    """Pause each recorded mesh and keep its queued messages."""
    from scripts.agents import codex_mesh

    for run in runs:
        if any(run.glob("mesh_*.json")):
            try:
                if codex_mesh.command_stop(argparse.Namespace(session_dir=str(run), pause=True)):
                    print(f"unit_move: cannot pause run {run}", file=sys.stderr)
            except (SystemExit, OSError, ValueError) as error:
                print(f"unit_move: cannot pause run {run}: {error}", file=sys.stderr)


def resume_folder_workers(row: staff.StaffRow, runs: tuple[Path, ...], old: Path, new: Path) -> None:
    """Start the existing worker threads through their launchers."""
    from scripts.lib.resume_message import resume_worker_command
    from scripts.shutdown.inventory import codex_seats

    environment = dict(os.environ)
    for key in ("CODEX_THREAD_ID", "STAFF_ID"):
        _ = environment.pop(key, None)
    environment["CLAUDE_CODE_SESSION_ID"] = row.current_session
    for seat in codex_seats(list(runs)):
        prompt = Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md"
        text = prompt.read_text(encoding="utf-8") if prompt.exists() else ""
        note = f"Your unit folder moved from {old} to {new}. Use {new} for commands. Continue your current task."
        if note not in text:
            _ = prompt.write_text(f"{text}{note}\n", encoding="utf-8")
        command = shlex.split(resume_worker_command(seat, str(new)))
        with prompt.with_suffix(".log").open("a") as log:
            _ = Popen(command, cwd=new, env=environment, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)


@contextmanager
def folder_move_lock(unit: staff.StaffId) -> Generator[None]:
    """Keep one move owner while worker launchers use the staff operation lock."""
    path = staff.folder_move_path(unit).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RenameRefused(f"{unit}: another folder move is running") from error
        yield


def _start_folder_row(unit: staff.StaffId) -> None:
    start_row = cast(Callable[[staff.StaffRow, staff.StaffSnapshot, herdr.Herdr], None],
                     import_module("scripts.shutdown.restart").start_row)
    record = staff.snapshot()
    row = record.by_id(unit)
    if row is None:
        raise ValueError(f"{unit}: no staff row")
    start_row(row, record, herdr.Herdr(herdr.shared_session()))


def move_folder(unit: staff.StaffId, destination: Path) -> None:
    """Stop, move, publish the location, and resume the unit."""
    destination = destination.expanduser().resolve()
    with folder_move_lock(unit):
        record = staff.snapshot()
        row = record.by_id(unit)
        if row is None or row.kind != "unit" or not isinstance(row.worktree, staff.PresentWorktree):
            print(f"{unit}: move-folder requires a unit with a present worktree", file=sys.stderr)
            return
        if row.worktree.machine != remote.local_machine():
            print(f"{unit}: run move-folder on {row.worktree.machine}", file=sys.stderr)
        old = row.worktree.path
        if old == destination:
            resumed = False
            launch = folder_launch(row)
            if (isinstance(launch, FolderLaunch)
                    and isinstance(sessions.session_liveness(row.current_session), sessions.SessionNotLive)
                    and isinstance(herdr.Herdr(launch.session).pane_activity(launch.entry["pane_id"]), herdr.PaneAtShell)):
                resume_folder_session(row, launch, old, destination, ())
                resumed = True
            print(f"resumed {unit} in {destination}" if resumed else "unchanged")
            return
        moved = False
        if not old.exists():
            owner = record.parent_of(row)
            if (owner is not None and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
                    and owner.doc.machine == remote.local_machine()):
                try:
                    moved = (git(owner.doc.path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
                             == git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir"))
                except RenameRefused:
                    pass
        runs = folder_runs(row)
        was_live = isinstance(sessions.session_liveness(row.current_session), sessions.LiveSessionProcess)
        launch: FolderLaunch | NoFolderLaunch = NoFolderLaunch()
        try:
            launch = folder_launch(row)
            if isinstance(launch, FolderLaunch):
                _ = stop_folder_session(row, launch)
            else:
                stopped = stop_resume.stop(row.current_session)
                if isinstance(stopped, stop_resume.StopFailed):
                    print(f"unit_move: {stopped.reason}", file=sys.stderr)
                elif isinstance(stopped, stop_resume.SessionNotFound):
                    print(f"unit_move: session not found: {stopped.name}", file=sys.stderr)
        except (OSError, ValueError, herdr.HerdrFailure, RenameRefused) as error:
            print(f"unit_move: {error}", file=sys.stderr)
        pause_folder_workers(runs)
        if not moved:
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                _ = git(old, "worktree", "move", str(old), str(destination))
            except RenameRefused:
                if was_live:
                    try:
                        if isinstance(launch, FolderLaunch):
                            resume_folder_session(row, launch, old, old, runs)
                        else:
                            _start_folder_row(unit)
                    except (OSError, ValueError, herdr.HerdrFailure, RenameRefused) as error:
                        print(f"unit_move: {error}", file=sys.stderr)
                raise
        histories = {Path(history) for run in runs
                     if isinstance(history := json_object(read_json(run / unit_runs.STATE_FILE)).get("history_file"), str)
                     and history}
        for history in sorted(histories):
            try:
                move_run(history, old, destination, git(destination, "branch", "--show-current"))
            except (OSError, ValueError, RenameRefused) as error:
                print(f"unit_move: {history}: {error}", file=sys.stderr)
        _ = staff.set_worktree(unit, staff.PresentWorktree(row.worktree.machine, destination), expected=row.worktree)
        if isinstance(launch, FolderLaunch):
            try:
                resume_folder_session(row, launch, old, destination, runs)
            except (OSError, ValueError, herdr.HerdrFailure, RenameRefused) as error:
                print(f"unit_move: {error}", file=sys.stderr)
        else:
            resume_folder_workers(row, runs, old, destination)
            if was_live:
                try:
                    _start_folder_row(unit)
                except (OSError, ValueError, herdr.HerdrFailure, RenameRefused) as error:
                    print(f"unit_move: {error}", file=sys.stderr)
        print(f"moved {unit}: {old} -> {destination}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move a unit folder without a rename.")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("move-folder")
    _ = command.add_argument("--unit", required=True)
    _ = command.add_argument("--to", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        move_folder(staff.native_id(cast(str, arguments.unit)), cast(Path, arguments.to))
        return 0
    except (RenameRefused, staff.StaffRefused, staff.StaffNotFound, OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"unit_move: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
