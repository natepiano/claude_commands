"""Move unit folders with exact rename recovery records.

Folder moves defer while workers or detached commands run.
Idle moves preserve the director's session through a restart.
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
from scripts.herdr import herdr_types  # noqa: E402
from scripts.production import tidy_wrapped  # noqa: E402
from scripts.production import unit_lookup  # noqa: E402
from scripts.herdr import pane_launch
from scripts.herdr import herdr  # noqa: E402
from scripts.agents import pane_record, detached_run_process  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf, git_files, unit_runs  # noqa: E402
from scripts.agents import worker_lifecycle  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.agents import remote  # noqa: E402

BERTH_KEY = "cargoBerthTarget"


class RenameRefused(ValueError):
    """A rename or folder move cannot safely complete the requested operation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


def read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise RenameRefused(f"cannot parse {path}: {error}") from error


def write_json(path: Path, value: object) -> None:
    """Publish folder move state, delivery state, or trust configuration atomically."""
    write_text_atomic(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


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


def checkout_folders(folder: Path) -> git_files.Checkout:
    """Read the checkout's Git folders from its files, with no Git start."""
    try:
        return git_files.locate(folder)
    except git_files.GitFilesError as error:
        raise RenameRefused(str(error)) from error


def checked_out_branch(folder: Path) -> str:
    """Read the checked-out branch from Git's files; "" for a detached HEAD."""
    try:
        return git_files.current_branch(folder)
    except git_files.GitFilesError as error:
        raise RenameRefused(str(error)) from error


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
        raise RenameRefused("expected a JSON object")
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
    current = checked_out_branch(new)
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
    for entry in herdr.proc_root().iterdir():
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
    entry = pane_record.read(found.place.session).get(found.place.pane) if found is not None else None
    if entry is None or pane_launch.reference_id(entry) != session_id:
        try:
            entry = pane_record.launch_for_session(session_id)
        except ValueError as error:
            raise RenameRefused(str(error)) from error
    if entry is None:
        return NoUnitConversation()
    home = pane_launch.config_home(entry)
    if isinstance(home, staff.HomeUnknown):
        print(f"anomaly: {session_id}: session home is unknown; select the normal account", file=sys.stderr)
        return UnitConversation(session_id, accounts.role_account("unit").label)
    return UnitConversation(session_id, accounts.claude_config_label(home.path))



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
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
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
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
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


class PendingRename(NamedTuple):
    source: Path
    destination: Path
    source_branch: str
    branch: str
    name: str
    branch_change: bool = False


def rename_move_path(unit: staff.StaffId) -> Path:
    """Use the existing pending move directory beside the staff record."""
    return staff.record_path().parent / "folder_moves" / f"{staff.native_id(unit)}.json"


def pending_rename(unit: staff.StaffId) -> PendingRename | None:
    path = rename_move_path(unit)
    if not path.exists():
        return None
    fields = json_object(read_json(path))
    values = [fields.get(key) for key in ("source", "destination", "source_branch", "branch", "name")]
    if (fields.get("kind") != "rename" or any(not isinstance(value, str) for value in values)
            or not fields.get("name")):
        raise RenameRefused(f"{path}: the pending rename record is invalid")
    source, destination, source_branch, branch, name = cast(list[str], values)
    if not Path(source).is_absolute() or not Path(destination).is_absolute():
        raise RenameRefused(f"{path}: the pending rename paths must be absolute")
    # Older records keep exact branch names but have no intent field.
    branch_change = fields.get("branch_change", source_branch != branch)
    if (not isinstance(branch_change, bool) or branch_change != (source_branch != branch)
            or branch_change and not branch):
        raise RenameRefused(f"{path}: the pending rename branch intent is invalid")
    return PendingRename(Path(source), Path(destination), source_branch, branch, name, branch_change)


def prepare_rename(row: staff.StaffRow, destination: Path, branch: str | KeepBranch, name: str) -> PendingRename:
    """Record exact source and destination values before the staff rename."""
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise RenameRefused(f"{row.id}: no unit worktree")
    if (pending := pending_rename(row.id)) is not None:
        raise RenameRefused(f"{row.name}: finish the pending rename to {pending.name}")
    source = row.worktree.path.expanduser().resolve()
    destination = destination.expanduser().resolve()
    checkout = move_checkout(staff.snapshot(), row)
    source_branch = checked_out_branch(checkout) if checkout.exists() else ""
    target_branch = branch if isinstance(branch, str) else source_branch
    branch_change = isinstance(branch, str) and branch != source_branch
    pending = PendingRename(source, destination, source_branch, target_branch, name, branch_change)
    write_json(rename_move_path(row.id), {
        "kind": "rename", "name": name, "source": str(source), "destination": str(destination),
        "source_branch": source_branch, "branch": target_branch, "branch_change": branch_change})
    return pending


def rename_observation(record: staff.StaffSnapshot, row: staff.StaffRow,
                       pending: PendingRename) -> tuple[Path, str]:
    """Accept only the recorded folder paths and branch names."""
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise RenameRefused(f"{row.name}: the pending rename has no staff worktree")
    location = row.worktree.path.expanduser().resolve()
    if location not in (pending.source, pending.destination):
        raise RenameRefused(f"{row.name}: pending rename to {pending.name} found staff path {location}")
    checkout = move_checkout(record, row)
    if checkout not in (pending.source, pending.destination):
        raise RenameRefused(f"{row.name}: pending rename to {pending.name} found folder {checkout}")
    branch = checked_out_branch(checkout) if checkout.exists() else ""
    # Records made without a checkout leave both branch values empty.
    if (not checkout.exists() and isinstance(row.worktree, staff.RemovedWorktree)
            and isinstance(row.worktree.branch, staff.ParkedBranch) and (pending.source_branch or pending.branch)):
        branch = row.worktree.branch.value
    if branch not in (pending.source_branch, pending.branch):
        raise RenameRefused(f"{row.name}: pending rename to {pending.name} found branch {branch!r} at {checkout}")
    if not checkout.exists() and not isinstance(row.worktree, staff.RemovedWorktree):
        raise RenameRefused(f"{row.name}: pending rename to {pending.name} found no folder at {checkout}")
    return checkout, branch


def rename_complete(record: staff.StaffSnapshot, row: staff.StaffRow, pending: PendingRename) -> bool:
    checkout, branch = rename_observation(record, row, pending)
    if (isinstance(row.worktree, staff.WorktreeUnfilled)
            or row.worktree.path.expanduser().resolve() != pending.destination
            or checkout != pending.destination or branch != pending.branch):
        return False
    if not checkout.exists():
        return isinstance(row.worktree, staff.RemovedWorktree)
    return not move_locked(row.id, checkout)


def finish_rename(row: staff.StaffRow, pending: PendingRename) -> bool:
    """Clear recovery after the recorded move and any required push."""
    record = staff.snapshot()
    if not rename_complete(record, row, pending):
        return False
    _finish_rename_push(record, row, pending, pending.destination)
    if (isinstance(row.worktree, staff.RemovedWorktree) and pending.branch
            and row.worktree.branch != staff.ParkedBranch(pending.branch)):
        _ = staff.set_worktree(row.id, replace(row.worktree, branch=staff.ParkedBranch(pending.branch)),
                              expected=row.worktree)
    rename_move_path(row.id).unlink()
    return True


def _finish_rename_push(record: staff.StaffSnapshot, row: staff.StaffRow,
                        pending: PendingRename, checkout: Path) -> None:
    """Push only when the recorded request changes the branch name."""
    if not pending.branch_change:
        return
    try:
        if not checkout.exists():
            owner = record.parent_of(row)
            if (owner is None or not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
                    or owner.doc.machine != remote.local_machine()):
                raise RenameRefused("the owner's repository is unavailable")
            checkout = owner.doc.path.parent
        tip = branch_tip(checkout, pending.branch)
        if not tip:
            raise RenameRefused("the local destination branch is unavailable")
        _push_moved_branch(checkout, pending.branch)
        if remote_tip(checkout, pending.branch) != tip or branch_tip(checkout, pending.branch) != tip:
            raise RenameRefused("origin does not have the destination tip")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = " ".join(str(error).splitlines())
        raise RenameRefused(f"branch {pending.branch}: push is unfinished: {detail}") from error


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


def _worktree_lock(folder: Path) -> Path:
    """Locate the native Git lock without copying a worktree path."""
    return checkout_folders(folder).git_dir / "locked"


def _recoverable_folder(record: staff.StaffSnapshot, row: staff.StaffRow, destination: Path) -> bool:
    """Require the owner's repository and the fixed staff identity in Git."""
    owner = record.parent_of(row)
    if (owner is None or not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
            or owner.doc.machine != remote.local_machine() or not (destination / ".git").is_file()):
        return False
    try:
        repository = checkout_folders(owner.doc.path.parent).common_dir
        target = checkout_folders(destination)
        identity = target.git_dir / "locked"
        return (repository == target.common_dir and identity.parent.parent == repository / "worktrees"
                and identity.read_text().strip() == f"staff:{row.id}" and target.toplevel == destination)
    except (RenameRefused, OSError):
        return False


def move_checkout(record: staff.StaffSnapshot, row: staff.StaffRow) -> Path:
    """Find an interrupted move through its native Git lock."""
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{row.id}: no unit worktree")
    folder = row.worktree.path.expanduser().resolve()
    if folder.exists():
        return folder
    owner = record.parent_of(row)
    if (owner is not None and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
            and owner.doc.machine == remote.local_machine()):
        found = [Path(line.removeprefix("worktree ")).resolve()
                 for line in git(owner.doc.path.parent, "worktree", "list", "--porcelain").splitlines()
                 if line.startswith("worktree ")]
        matching = [path for path in found if _recoverable_folder(record, row, path)]
        if len(matching) == 1:
            return matching[0]
    return folder


def require_move_destination(record: staff.StaffSnapshot, row: staff.StaffRow, destination: Path) -> None:
    """Refuse an occupied destination before a caller changes displays or sessions."""
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{row.id}: no unit worktree")
    old = row.worktree.path.expanduser().resolve()
    if old != destination and exists(destination):
        if exists(old) or not _recoverable_folder(record, row, destination):
            raise RenameRefused(f"destination already exists: {destination}")


def move_locked(unit: staff.StaffId, folder: Path) -> bool:
    """Read the fixed unit identity from the native Git worktree lock."""
    lock = _worktree_lock(folder)
    return lock.exists() and lock.read_text().strip() == f"staff:{unit}"


def _lock_move(folder: Path, unit: staff.StaffId) -> bool:
    """Keep only the fixed unit identity in Git during a folder move."""
    lock = _worktree_lock(folder)
    if lock.exists():
        if lock.read_text().strip() != f"staff:{unit}":
            raise RenameRefused(f"worktree is locked: {folder}")
        return False
    _ = git(folder, "worktree", "lock", "--reason", f"staff:{unit}", str(folder))
    return True


def _unlock_move(folder: Path, unit: staff.StaffId) -> bool:
    """Release only this unit's native lock after a move or an early refusal."""
    lock = _worktree_lock(folder)
    if not lock.exists() or lock.read_text().strip() != f"staff:{unit}":
        return False
    _ = git(folder, "worktree", "unlock", str(folder))
    return True


def move_folder(unit: staff.StaffId, destination: Path, *, branch: str | KeepBranch = KeepBranch.CURRENT) -> bool:
    """Move a unit through the existing command API."""
    return _move_folder(unit, destination, branch=branch)


class FolderMoveRuns(NamedTuple):
    """The saved paths that a folder move must preserve."""
    run_folders: tuple[Path, ...]
    dispatch_records: tuple[Path, ...]
    supervisor_records: tuple[Path, ...]
    unit_run_folders: tuple[Path, ...]


def folder_move_runs(row: staff.StaffRow, record: staff.StaffSnapshot) -> FolderMoveRuns:
    members = {row.id, *(member.id for member in record.find(kind="worker", parent=row.id))}
    runs: set[Path] = set()
    unit_runs_owned: set[Path] = set()
    dispatches: set[Path] = set()
    for member in record.rows:
        if isinstance(member.run, staff.OpenRun):
            runs.add(member.run.path.resolve())
            if member.id in members:
                unit_runs_owned.add(member.run.path.resolve())
        link = staff.record_path().parent / "dispatches" / str(member.id)
        if exists(link):
            path = link.resolve()
            dispatches.add(path)
            if member.id in members:
                unit_runs_owned.add(path.parent)
    runs.update(path.parent for path in dispatches)
    paths = {path for root in unit_runs.run_roots()
             for path in root.glob(f"*/{worker_lifecycle.DETACHED_RUNS_DIR}/*.json")}
    paths.update(path for run in runs for path in (run / worker_lifecycle.DETACHED_RUNS_DIR).glob("*.json"))
    return FolderMoveRuns(tuple(sorted(runs)), tuple(sorted(dispatches)), tuple(sorted(paths)), tuple(sorted(unit_runs_owned)))


def require_outside_run_paths(runs: FolderMoveRuns, folder: Path) -> None:
    """Refuse a move before it makes saved run paths unreachable."""
    for path in (*runs.dispatch_records, *runs.run_folders, *runs.supervisor_records):
        if path.resolve().is_relative_to(folder):
            raise RenameRefused(f"run path is inside the worktree: {path.resolve()}")


class DirectorMoveProcesses(NamedTuple):
    """Keep the proved director and its pane ancestors through one folder move."""
    roots: frozenset[int]
    pane_ancestors: frozenset[int]


class DirectorMoveRead(Enum):
    CURRENT = "read the director processes"


def _director_move_processes(row: staff.StaffRow) -> DirectorMoveProcesses:
    roots: frozenset[int] = frozenset()
    try:
        observed = herdr.read_conversation(str(row.current_session))
        if isinstance(observed, herdr.LiveConversation) and len(observed.pids) == 1:
            pid = observed.pids[0]
            if herdr.native_process(pid, observed.kind).current_identity == row.current_session:
                roots = frozenset({pid})
                place = herdr.herdr_place_of(pid)
                if isinstance(place, herdr.HerdrPlace):
                    shell = pane_record.pane_shell_pid(place)
                    ancestors: set[int] = set()
                    while pid > 1 and pid not in ancestors:
                        ancestors.add(pid)
                        if pid == shell:
                            return DirectorMoveProcesses(roots, frozenset(ancestors))
                        pid = pane_record.process_parent(pid)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError):
        pass
    return DirectorMoveProcesses(roots, frozenset())


def move_processes_inside(row: staff.StaffRow, folder: Path, *,
                          preserved_workers: Iterable[staff.StaffRow] = (),
                          director: DirectorMoveProcesses | DirectorMoveRead = DirectorMoveRead.CURRENT) -> list[int]:
    """Keep worker trees blocking while excluding the caller and the proved director pane chain."""
    processes = processes_inside(folder)
    if not processes:
        return []
    parents: dict[int, int] = {}

    def ancestors(pid: int) -> set[int]:
        found: set[int] = set()
        while pid > 1 and pid not in found:
            found.add(pid)
            try:
                if pid not in parents:
                    parents[pid] = pane_record.process_parent(pid)
                pid = parents[pid]
            except (OSError, ValueError, subprocess.SubprocessError):
                break
        return found

    caller = ancestors(os.getpid())
    director = _director_move_processes(row) if isinstance(director, DirectorMoveRead) else director
    workers: set[int] = set()
    for member in preserved_workers:
        try:
            observed = herdr.read_conversation(str(member.current_session))
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError):
            continue
        if isinstance(observed, herdr.UnknownConversation):
            continue
        if isinstance(observed, herdr.LiveConversation):
            for pid in observed.pids:
                try:
                    if herdr.native_process(pid, observed.kind).current_identity != member.current_session:
                        continue
                except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError):
                    pass
                workers.add(pid)
    blocking: list[int] = []
    for pid in processes:
        lineage = ancestors(pid)
        if workers.intersection(lineage):
            blocking.append(pid)
            continue
        if pid in caller or pid in director.pane_ancestors:
            continue
        if not director.roots.intersection(lineage):
            blocking.append(pid)
    return blocking


def running_work(row: staff.StaffRow, record: staff.StaffSnapshot, folder: Path,
                 runs: FolderMoveRuns, processes: Iterable[int]) -> tuple[str, ...]:
    """Name running processes and detached commands for moves and renames."""
    workers = record.find(kind="worker", parent=row.id)
    names: list[str] = []
    members = {row.id, *(member.id for member in workers)}
    folder_path = re.compile(r"(?<![\w./-])" + re.escape(str(folder)) + r"(?=$|[/\s'\";|&<>])")
    for path in runs.supervisor_records:
        try:
            fields = worker_lifecycle.read_json_object(path)
        except (OSError, ValueError):
            continue
        pid = fields.get("pid")
        member = fields.get("staff_id")
        command = fields.get("command")
        command_inside = (isinstance(command, list)
            and any(isinstance(argument, str) and folder_path.search(argument) is not None
                    for argument in cast(list[object], command)))
        cwd = fields.get("cwd")
        cwd_inside = isinstance(cwd, str) and bool(cwd) and Path(cwd).expanduser().resolve().is_relative_to(folder)
        if (isinstance(pid, int)
                and (path.parent.parent.resolve() in runs.unit_run_folders
                     or isinstance(member, str) and member in members or command_inside or cwd_inside)
                and detached_run_process.supervises(pid, path)):
            label = str(fields.get("label") or path.stem)
            names.append(" ".join(label.splitlines()))
    names.extend(f"process {pid}" for pid in processes)
    return tuple(names)


def worktree_busy(row: staff.StaffRow, record: staff.StaffSnapshot, folder: Path) -> bool:
    """Require live process evidence before a rename defers its move."""
    processes = move_processes_inside(row, folder, preserved_workers=record.find(kind="worker", parent=row.id))
    return bool(running_work(row, record, folder, folder_move_runs(row, record), processes))


def move_idle_folder(unit: staff.StaffId, destination: Path, *, branch: str | KeepBranch) -> bool:
    """Move a rename's folder and branch without any session lifecycle operation."""
    destination = destination.expanduser().resolve()
    with (worker_lifecycle.operation_deadline(timeout=_move_timeout("RENAME_COMMAND_TIMEOUT_SECONDS")),
          staff.row_operation(str(unit))):
        _check_move_deadline()
        record = staff.snapshot()
        row = record.by_id(unit)
        if row is None or row.kind != "unit" or isinstance(row.worktree, staff.WorktreeUnfilled):
            raise ValueError(f"{unit}: rename requires a unit with a worktree")
        location = row.worktree
        if location.machine != remote.local_machine():
            raise ValueError(f"{unit}: run rename on {location.machine}")
        pending = pending_rename(unit)
        if pending is not None:
            if destination != pending.destination or branch != pending.branch or row.name != pending.name:
                raise RenameRefused(f"{row.name}: finish the pending rename to {pending.name}")
        old = location.path.expanduser().resolve()
        folder = rename_observation(record, row, pending)[0] if pending is not None else old if old.exists() else destination
        require_move_destination(record, row, destination)
        if worktree_busy(row, record, folder):
            print(f"{row.name}: folder and branch kept while work runs in the worktree; rerun rename when idle")
            return False
        if not folder.exists():
            updated = location
            if isinstance(location, staff.RemovedWorktree):
                updated = replace(location, path=destination)
                if pending is not None and pending.branch:
                    _finish_rename_push(record, row, pending, folder)
                    updated = replace(updated, branch=staff.ParkedBranch(pending.branch))
            changed = updated != location
            if changed:
                _ = staff.set_worktree(unit, updated, expected=location)
            if pending is not None and pending.branch and old == destination:
                return changed
            missing = destination if isinstance(location, staff.RemovedWorktree) else old
            print(f"anomaly: folder is absent: {missing}", file=sys.stderr)
            return changed
        current_branch = checked_out_branch(folder)
        complete = old == destination and (isinstance(branch, KeepBranch) or current_branch == branch)
        if complete and not move_locked(unit, folder):
            if pending is not None:
                _finish_rename_push(record, row, pending, folder)
            elif isinstance(branch, str):
                _push_moved_branch(folder, branch)
            return False
        restart = import_module("scripts.agents.restart")
        live_members = cast(Callable[[Iterable[staff.StaffRow], staff.StaffSnapshot], list[staff.StaffRow]], restart.live_members)
        members = [row, *record.find(kind="worker", parent=row.id)]
        live = {member.id for member in live_members(members, record)}
        for member in members:
            if member.id not in live:
                print(f"info: {member.name}: stopped; agents restart --only {member.id}")
        changed = False
        push_finished = False
        _ = _lock_move(folder, unit)
        try:
            if folder != destination:
                _check_move_deadline()
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = git(folder, "worktree", "move", "--force", "--force", str(folder), str(destination))
                folder = destination
                changed = True
            current_branch = checked_out_branch(folder)
            if pending is not None and current_branch not in (pending.source_branch, pending.branch):
                raise RenameRefused(f"{row.name}: pending rename to {pending.name} found branch {current_branch!r} at {folder}")
            if isinstance(branch, str) and current_branch != branch:
                _ = git(folder, "branch", "-m", branch)
                changed = True
            if pending is not None:
                _finish_rename_push(record, row, pending, folder)
            elif isinstance(branch, str):
                _push_moved_branch(folder, branch)
            push_finished = True
        finally:
            if not old.exists() and destination.exists():
                folder = destination
            if folder == destination:
                updated = replace(location, path=destination)
                if isinstance(updated, staff.RemovedWorktree) and isinstance(branch, str) and branch and push_finished:
                    updated = replace(updated, branch=staff.ParkedBranch(checked_out_branch(folder)))
                if updated != location:
                    _check_move_deadline()
                    _ = staff.set_worktree(unit, updated, expected=location)
                    changed = True
        return _unlock_move(folder, unit) or changed


def _push_moved_branch(checkout: Path, branch: str) -> None:
    upstream = git(checkout, "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}")
    if upstream != f"origin/{branch}" or remote_tip(checkout, branch) != branch_tip(checkout, branch):
        _ = git(checkout, "push", "--set-upstream", "origin", f"{branch}:refs/heads/{branch}")


def _move_folder(unit: staff.StaffId, destination: Path, *, branch: str | KeepBranch = KeepBranch.CURRENT) -> bool:
    """Defer running work, then move the idle worktree and resume the director."""
    destination = destination.expanduser().resolve()
    with (worker_lifecycle.operation_deadline(timeout=_move_timeout("RENAME_COMMAND_TIMEOUT_SECONDS")),
          staff.row_operation(str(unit)), ExitStack() as locks):
        _check_move_deadline()
        record = staff.snapshot()
        row = record.by_id(unit)
        if row is None or row.kind != "unit" or isinstance(row.worktree, staff.WorktreeUnfilled):
            raise ValueError(f"{unit}: move-folder requires a unit with a worktree")
        if (pending := pending_rename(unit)) is not None:
            raise RenameRefused(f"{row.name}: finish the pending rename to {pending.name}")
        location = row.worktree
        if location.machine != remote.local_machine():
            raise ValueError(f"{unit}: run move-folder on {location.machine}")
        old = location.path.expanduser().resolve()
        require_move_destination(record, row, destination)
        folder = old if old.exists() else destination
        runs = folder_move_runs(row, record)
        require_outside_run_paths(runs, old)
        if folder != old:
            require_outside_run_paths(runs, folder)
        director = _director_move_processes(row)
        if work := running_work(row, record, folder, runs, move_processes_inside(row, folder, director=director)):
            print(f"{row.name}: folder move deferred while {', '.join(work)} runs; rerun when idle")
            return False
        if not folder.exists():
            changed = isinstance(location, staff.RemovedWorktree) and location.path != destination
            if changed:
                _check_move_deadline()
                _ = staff.set_worktree(unit, replace(location, path=destination), expected=location)
                _check_move_deadline()
            missing = destination if isinstance(location, staff.RemovedWorktree) else old
            print(f"anomaly: folder is absent: {missing}", file=sys.stderr)
            return changed
        current_branch = checked_out_branch(folder)
        move_complete = old == destination and (isinstance(branch, KeepBranch) or current_branch == branch)
        if move_complete:
            if not move_locked(unit, folder):
                if isinstance(branch, str):
                    _push_moved_branch(folder, branch)
                return False
        members = [row, *record.find(kind="worker", parent=row.id)]
        for member in members[1:]:
            _check_move_deadline()
            _ = locks.enter_context(staff.row_operation(str(member.id)))
        # The delayed import avoids the restart module's dependency on unit_move.
        restart = import_module("scripts.agents.restart")
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
        if workers := [member.name for member in members if member.kind == "worker" and member.id in live]:
            print(f"{row.name}: folder move deferred while {', '.join(workers)} runs; rerun when idle")
            return False
        for member in members:
            if member.id not in live:
                print(f"info: {member.name}: stopped; agents restart --only {member.id}")
        if move_complete:
            if isinstance(branch, str):
                _push_moved_branch(folder, branch)
            _ = _unlock_move(folder, unit)
            return True
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
        move_lock_created = False
        try:
            if folder != destination:
                move_lock_created = _lock_move(folder, unit)
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
            current = staff.snapshot()
            runs = folder_move_runs(row, current)
            require_outside_run_paths(runs, folder)
            work = list(running_work(row, current, folder, runs, move_processes_inside(row, folder, director=director)))
            work.extend(member.name for member in live_members(current.find(kind="worker", parent=unit), current))
            if work:
                print(f"{row.name}: folder move deferred while {', '.join(work)} runs; rerun when idle")
                return False
            if folder != destination:
                _check_move_deadline()
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = git(folder, "worktree", "move", "--force", "--force", str(folder), str(destination))
                folder = destination
                changed = True
            if isinstance(branch, str) and checked_out_branch(folder) != branch:
                _ = git(folder, "branch", "-m", branch)
                changed = True
            if isinstance(branch, str):
                _push_moved_branch(folder, branch)
        finally:
            if not old.exists() and destination.exists():
                folder = destination
            current = record
            try:
                if folder == old and move_lock_created:
                    _ = _unlock_move(folder, unit)
                _check_move_deadline()
                current = staff.snapshot()
                _check_move_deadline()
                latest_unit = current.by_id(unit)
                updated = replace(location, path=destination)
                if folder == destination and isinstance(updated, staff.RemovedWorktree) and isinstance(branch, str):
                    updated = replace(updated, branch=staff.ParkedBranch(checked_out_branch(folder)))
                if folder == destination and latest_unit is not None and latest_unit.worktree != updated:
                    latest_unit = replace(latest_unit, worktree=updated)
                    current = staff.StaffSnapshot(tuple(latest_unit if item.id == unit else item for item in current.rows))
                    try:
                        _check_move_deadline()
                        _ = staff.set_worktree(unit, updated, expected=location)
                        changed = True
                        _check_move_deadline()
                    except (OSError, ValueError, staff.StaffRefused) as error:
                        raise OSError(f"staff worktree write failed for {row.name}: {error}") from error
                if folder == destination:
                    _ = _unlock_move(folder, unit)
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
                            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                                restart_notice(latest, error)
                        # Worker restore proves active starts and leaves idle workers closed.
                        expected = {member.id for member in members if member.kind != "worker"} & (live - reported)
                        if not _wait_folder_rows(observe, expected, running=True):
                            for member in members:
                                if member.id in expected - observed_live:
                                    restart_notice(member, "restart is incomplete")
                except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                    for member in members:
                        if member.id in live - observed_live:
                            restart_notice(member, error)
        _check_move_deadline()
        return changed


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
    except (RenameRefused, staff.StaffRefused, staff.StaffNotFound, OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"unit_move: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
