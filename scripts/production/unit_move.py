"""Move a unit folder, or share stopped-unit move steps with rename commands.

Folder moves stop, move, publish the location, and resume the unit.
Rename callers control their own step order and progress records.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import NamedTuple, cast

from scripts.direct import progress_history  # noqa: E402
from scripts.production import tidy_wrapped  # noqa: E402
from scripts.production import unit_lookup  # noqa: E402
from scripts.herdr import pane_launch
from scripts.herdr import herdr  # noqa: E402
from scripts.shutdown import pane_record  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from scripts.production.rename_state import RenameRefused, read_json, write_json  # noqa: E402
from scripts.lib import conf  # noqa: E402
from scripts.agents import worker_lifecycle  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.shutdown import remote  # noqa: E402

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
                            text=True, check=False, timeout=_move_timeout("RENAME_COMMAND_TIMEOUT_SECONDS"))
    _check_move_deadline()
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


def unit_conversation(found: unit_lookup.LabeledSession | None, *, session_id: str = "") -> UnitConversation | NoUnitConversation:
    """Use the live director's id, then its pane launch; retained launches survive a stop."""
    from scripts.accounts import accounts

    if found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        session_id = found.claude.session_id
    if not session_id:
        return NoUnitConversation()
    if found is not None:
        entry = pane_record.read(found.place.session).get(found.place.pane)
        if entry is not None and entry["session_id"] == session_id:
            home = pane_launch.config_home(entry)
            if home:
                return UnitConversation(session_id, accounts.claude_config_label(Path(home)))
    try:
        entry = pane_record.launch_for_session(session_id)
    except ValueError as error:
        raise RenameRefused(str(error)) from error
    home = pane_launch.config_home(entry) if entry is not None else ""
    return UnitConversation(session_id, accounts.claude_config_label(Path(home))) if home else NoUnitConversation()



class UnitPane(NamedTuple):
    unit: str
    place: herdr.HerdrPlace
    shell: int


def unit_pane(found: unit_lookup.LabeledSession | None, _production: str) -> UnitPane | None:
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
        _ = herdr.Herdr(pane.place.session).close_retained_pane(pane.place.pane)
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


class KeepBranch(Enum):
    """Keep the branch that Git reports for the worktree."""

    CURRENT = "keep current branch"


def _check_move_deadline() -> None:
    """Start no operation after the shared deadline."""
    if time.monotonic() >= worker_lifecycle.effective_deadline():
        raise worker_lifecycle.OperationDeadlineExpired("folder move deadline expired")


def _move_timeout(key: str) -> float:
    """Use the configured limit within the current operation deadline."""
    limit = conf.read("production").number(key)
    remaining = worker_lifecycle.effective_deadline() - time.monotonic()
    if remaining <= 0:
        raise worker_lifecycle.OperationDeadlineExpired("folder move deadline expired")
    return min(limit, remaining)


def _wait_folder_rows(observe: Callable[[], set[staff.StaffId]], wanted: set[staff.StaffId], *, running: bool) -> bool:
    """Accept observations only within the configured operation limit."""
    key = "RENAME_LIVE_WAIT_SECONDS" if running else "RENAME_RUN_STOP_WAIT_SECONDS"
    try:
        deadline = min(worker_lifecycle.effective_deadline(), time.monotonic() + _move_timeout(key))
    except worker_lifecycle.OperationDeadlineExpired:
        return False
    with worker_lifecycle.operation_deadline(deadline=deadline):
        while True:
            if time.monotonic() >= deadline:
                return False
            if not wanted:
                return True
            live = observe()
            if time.monotonic() >= deadline:
                return False
            settled = wanted <= live if running else wanted.isdisjoint(live)
            if settled:
                return True
            poll = _move_timeout("RENAME_POLL_SECONDS")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))


class NoPendingMove(Enum):
    CURRENT = "no pending folder move"


class FolderMove(NamedTuple):
    destination: Path
    branch: str | KeepBranch


class PendingRename(NamedTuple):
    destination: Path
    branch: str | KeepBranch
    name: str


def pending_move(unit: staff.StaffId) -> FolderMove | PendingRename | NoPendingMove:
    path = staff.folder_move_path(unit)
    if not path.exists():
        return NoPendingMove.CURRENT
    progress = json_object(read_json(path))
    branch = str(progress.get("branch", ""))
    destination = Path(str(progress["destination"]))
    name = str(progress.get("name", ""))
    if progress.get("kind") == "rename" or ("kind" not in progress and name):
        return PendingRename(destination, branch or KeepBranch.CURRENT, name)
    return FolderMove(destination, branch or KeepBranch.CURRENT)


def prepare_move(row: staff.StaffRow, destination: Path, branch: str | KeepBranch,
                 name: str = "") -> None:
    """Save the move before the first staff write or session stop."""
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{row.id}: no unit worktree")
    source = row.worktree.path.expanduser().resolve()
    folder = source if source.exists() else destination
    admin_path = ""
    if (folder / ".git").is_file():
        admin = Path((folder / ".git").read_text().strip().removeprefix("gitdir: "))
        admin_path = str((admin if admin.is_absolute() else folder / admin).resolve())
    write_json(staff.folder_move_path(row.id), {
        "source": str(source), "destination": str(destination), "admin": admin_path,
        "branch": branch if isinstance(branch, str) else "",
        "kind": "rename" if name else "folder", **({"name": name} if name else {})})


def _recoverable_folder(record: staff.StaffSnapshot, row: staff.StaffRow, old: Path,
                        destination: Path, branch: str | KeepBranch) -> bool:
    """Require the journal identity and the unit's registered branch."""
    owner = record.parent_of(row)
    if (owner is None or not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
            or owner.doc.machine != remote.local_machine() or not (destination / ".git").is_file()):
        return False
    try:
        repository = Path(git(owner.doc.path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        target_repository = Path(git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if (target_repository != repository
                or Path(git(destination, "rev-parse", "--show-toplevel")).resolve() != destination):
            return False
        allowed = {"refs/heads/" + branch} if isinstance(branch, str) else set[str]()
        # Git keeps the admin path when it moves a worktree.
        admin = Path((destination / ".git").read_text().strip().removeprefix("gitdir: "))
        if not admin.is_absolute():
            admin = destination / admin
        admin = admin.resolve()
        if admin.parent != repository / "worktrees":
            return False
        progress_path = staff.folder_move_path(row.id)
        raw: object = read_json(progress_path) if progress_path.exists() else dict[str, object]()
        progress = cast(dict[str, object], raw) if isinstance(raw, dict) else dict[str, object]()
        if (progress.get("source") != str(old) or progress.get("destination") != str(destination)
                or progress.get("admin") != str(admin)):
            return False
        _check_move_deadline()
        head = (admin / "HEAD").read_text().strip()
        if head.startswith("ref: refs/heads/"):
            allowed.add(head.removeprefix("ref: "))
        registered = git(destination, "worktree", "list", "--porcelain", "-z")
        for entry in registered.split("\0\0"):
            fields = entry.split("\0")
            if (f"worktree {destination}" in fields
                    and any(f"branch {name}" in fields for name in allowed)):
                return True
    except RenameRefused:
        return False
    return False


def move_folder(unit: staff.StaffId, destination: Path, *, branch: str | KeepBranch = KeepBranch.CURRENT) -> bool:
    """Move a unit through the existing command API."""
    return _move_folder(unit, destination, branch=branch)


def _push_moved_branch(checkout: Path, branch: str) -> None:
    _ = git(checkout, "push", "--set-upstream", "origin", f"{branch}:refs/heads/{branch}")


def _move_folder(unit: staff.StaffId, destination: Path, *, branch: str | KeepBranch = KeepBranch.CURRENT) -> bool:
    """Stop live rows, move the worktree and branch, and resume those rows."""
    destination = destination.expanduser().resolve()
    with (worker_lifecycle.operation_deadline(timeout=_move_timeout("RENAME_COMMAND_TIMEOUT_SECONDS")),
          staff.row_operation(str(unit)), ExitStack() as locks):
        _check_move_deadline()
        record = staff.snapshot()
        row = record.by_id(unit)
        if row is None or row.kind != "unit" or isinstance(row.worktree, staff.WorktreeUnfilled):
            raise ValueError(f"{unit}: move-folder requires a unit with a worktree")
        location = row.worktree
        pending = pending_move(unit)
        if location.machine != remote.local_machine():
            raise ValueError(f"{unit}: run move-folder on {location.machine}")
        old = location.path.expanduser().resolve()
        if old != destination and exists(old) and exists(destination):
            raise RenameRefused(f"destination already exists: {destination}")
        folder = old if old.exists() else destination
        if not folder.exists():
            changed = isinstance(location, staff.RemovedWorktree) and location.path != destination
            if changed:
                _check_move_deadline()
                _ = staff.set_worktree(unit, replace(location, path=destination), expected=location)
                _check_move_deadline()
            missing = destination if isinstance(location, staff.RemovedWorktree) else old
            print(f"anomaly: folder is absent: {missing}", file=sys.stderr)
            staff.folder_move_path(unit).unlink(missing_ok=True)
            return changed or not isinstance(pending, NoPendingMove)
        recovering = folder == destination
        if recovering and old != destination and not _recoverable_folder(record, row, old, destination, branch):
            raise RenameRefused(f"destination already exists: {destination}")
        current_branch = git(folder, "branch", "--show-current")
        move_complete = old == destination and (isinstance(branch, KeepBranch) or current_branch == branch)
        if move_complete and isinstance(pending, NoPendingMove):
            return False
        members = [row, *record.find(kind="worker", parent=row.id)]
        for member in members[1:]:
            _check_move_deadline()
            _ = locks.enter_context(staff.row_operation(str(member.id)))
        # The delayed import avoids the restart module's dependency on unit_move.
        restart = import_module("scripts.shutdown.restart")
        live_members = cast(Callable[[Iterable[staff.StaffRow], staff.StaffSnapshot], list[staff.StaffRow]], restart.live_members)
        stop_row = cast(Callable[[staff.StaffRow, herdr.Herdr], bool], restart.stop_row)
        start_row = cast(Callable[[staff.StaffRow, staff.StaffSnapshot, herdr.Herdr], str], restart.start_row)
        client = herdr.Herdr(herdr.shared_session())
        observed_live = set[staff.StaffId]()

        def observe() -> set[staff.StaffId]:
            nonlocal observed_live
            _check_move_deadline()
            found = {member.id for member in live_members(members, record)}
            _check_move_deadline()
            observed_live = found
            return found

        live = observe().copy()
        for member in members:
            if member.id not in live:
                print(f"info: {member.name}: stopped; agents restart --only {member.id}")
        if move_complete:
            if isinstance(branch, str):
                _push_moved_branch(folder, branch)
            staff.folder_move_path(unit).unlink(missing_ok=True)
            return True
        if isinstance(pending, NoPendingMove):
            prepare_move(row, destination, branch)
        changed = False
        reported = set[staff.StaffId]()

        def restart_notice(member: staff.StaffRow, error: object) -> None:
            if member.id not in reported:
                reason = str(error).splitlines()
                detail = reason[0] if reason else "restart is incomplete"
                print(f"unit_move: anomaly: {member.name}: {detail}; agents restart --only {member.id}", file=sys.stderr)
                reported.add(member.id)

        panes = client.panes() if live else []
        retained = {member.id: (pane["pane_id"], pane.get("terminal_id", ""))
                    for member in members if member.id in live
                    for pane in panes if pane["session_id"] == member.current_session}
        try:
            stop_deadline = time.monotonic() + _move_timeout("RENAME_RUN_STOP_WAIT_SECONDS")
            with worker_lifecycle.operation_deadline(deadline=stop_deadline):
                for member in members:
                    if member.id in live:
                        _check_move_deadline()
                        observed_live.discard(member.id)
                        stopped = stop_row(member, client)
                        _check_move_deadline()
                        if not stopped:
                            print(f"unit_move: anomaly: {member.name}: stop is incomplete", file=sys.stderr)
                            return False
                if not _wait_folder_rows(observe, live, running=False):
                    print(f"unit_move: anomaly: {row.name}: stop is incomplete", file=sys.stderr)
                    return False
            if folder != destination:
                _check_move_deadline()
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = git(folder, "worktree", "move", str(folder), str(destination))
                folder = destination
                changed = True
            if isinstance(branch, str) and git(folder, "branch", "--show-current") != branch:
                _ = git(folder, "branch", "-m", branch)
                changed = True
            if isinstance(branch, str):
                _push_moved_branch(folder, branch)
        finally:
            if not old.exists() and destination.exists():
                folder = destination
            current = record
            try:
                _check_move_deadline()
                current = staff.snapshot()
                _check_move_deadline()
                latest_unit = current.by_id(unit)
                if folder == destination and latest_unit is not None and latest_unit.worktree != replace(location, path=destination):
                    updated = replace(location, path=destination)
                    latest_unit = replace(latest_unit, worktree=updated)
                    current = staff.StaffSnapshot(tuple(latest_unit if item.id == unit else item for item in current.rows))
                    try:
                        _check_move_deadline()
                        _ = staff.set_worktree(unit, updated, expected=location)
                        changed = True
                        _check_move_deadline()
                    except (OSError, ValueError, staff.StaffRefused) as error:
                        raise OSError(f"staff worktree write failed for {row.name}: {error}") from error
            finally:
                try:
                    start_deadline = time.monotonic() + _move_timeout("RENAME_LIVE_WAIT_SECONDS")
                    with worker_lifecycle.operation_deadline(deadline=start_deadline):
                        still_live = observe()
                        for member in members:
                            if member.id not in live or member.id in still_live:
                                continue
                            latest = current.by_id(member.id)
                            if latest is None:
                                continue
                            try:
                                _check_move_deadline()
                                resumed = start_row(latest, current, client)
                                _check_move_deadline()
                                if resumed and member.id in retained:
                                    pane, terminal = retained[member.id]
                                    if resumed != pane:
                                        _ = client.close_retained_pane(pane, terminal=terminal)
                            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
                                restart_notice(latest, error)
                        # Worker restore proves active starts and leaves idle workers closed.
                        expected = {member.id for member in members if member.kind != "worker"} & (live - reported)
                        if not _wait_folder_rows(observe, expected, running=True):
                            for member in members:
                                if member.id in expected - observed_live:
                                    restart_notice(member, "restart is incomplete")
                except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
                    for member in members:
                        if member.id in live - observed_live:
                            restart_notice(member, error)
        _check_move_deadline()
        staff.folder_move_path(unit).unlink(missing_ok=True)
        return changed or not isinstance(pending, NoPendingMove)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move a unit folder without a rename.")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("move-folder")
    _ = command.add_argument("--unit", required=True)
    _ = command.add_argument("--to", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        changed = _move_folder(staff.native_id(cast(str, arguments.unit)), cast(Path, arguments.to))
        print("done" if changed else "unchanged")
        return 0
    except (subprocess.TimeoutExpired, worker_lifecycle.OperationDeadlineExpired) as error:
        print(f"unit_move: anomaly: {error}", file=sys.stderr)
        return 0
    except (RenameRefused, staff.StaffRefused, staff.StaffNotFound, OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"unit_move: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
