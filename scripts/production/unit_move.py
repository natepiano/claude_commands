"""Move a unit folder, or share stopped-unit move steps with rename commands.

Folder moves record paths and step states before they change the folder.
Rename callers control their own step order and progress records.
"""
from __future__ import annotations

import fcntl
import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
import progress_history  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import herdr  # noqa: E402
import pane_record  # noqa: E402
import pane_launch  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from rename_state import RenameRefused, read_json, write_json  # noqa: E402
from scripts.lib import atomic_file, conf, relaunch, unit_runs  # noqa: E402
from scripts.production import staff, staff_sources  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.shutdown import remote  # noqa: E402
from scripts.agents import worker_lifecycle  # noqa: E402

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
    """The tracked plan files under docs/plans that a Units Plan cell names."""
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


def config_dirs() -> list[Path]:
    directories = {path for path in Path.home().glob(".claude*") if path.is_dir()}
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        directories.add(Path(override).expanduser())
    return sorted(directories)


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
    for directory in config_dirs():
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


class FolderStepState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class PriorFolderSession(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"


class PriorFolderWorker(str, Enum):
    RUNNING = "running"
    STARTING = "starting"
    WAITING_CAPACITY = "waiting_capacity"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class OwnedFolderProcess:
    """A proved run process whose identity must still match before a signal."""

    pid: int
    started: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class UnstartedWorkerResume:
    """No resume launcher is recorded for this worker."""


@dataclass
class FolderWorker:
    """A worker's original thread and the progress of its return."""

    run: Path
    name: str
    thread: str
    role: str
    prior_status: PriorFolderWorker
    cwd: Path
    launcher_command: tuple[str, ...]
    restored: bool = False
    resume_process: OwnedFolderProcess | UnstartedWorkerResume = field(default_factory=UnstartedWorkerResume)

    @property
    def was_running(self) -> bool:
        return self.prior_status in (PriorFolderWorker.RUNNING, PriorFolderWorker.STARTING,
                                     PriorFolderWorker.WAITING_CAPACITY)

    def owns(self, record: dict[str, object]) -> bool:
        return record.get("thread_id") == self.thread and not record.get("adopted_to")


@dataclass
class FolderSessions:
    """The sessions selected before any stop in this move."""

    unit_session: str
    prior: PriorFolderSession
    runs: tuple[Path, ...]
    workers: tuple[FolderWorker, ...]
    processes: tuple[OwnedFolderProcess, ...]

    def workers_in(self, run: Path) -> tuple[FolderWorker, ...]:
        return tuple(worker for worker in self.workers if worker.run == run)


@dataclass(frozen=True)
class UnrecordedFolderSessions:
    """An older move records only the unit's stop and resume steps."""


FolderMoveStep = Literal["stop", "worktree", "staff", "launch", "run", "resume"]
FOLDER_MOVE_STEPS: tuple[FolderMoveStep, ...] = ("stop", "worktree", "staff", "launch", "run", "resume")


@dataclass
class FolderMove:
    """Paths and durable progress for one folder move."""

    old: Path
    new: Path
    steps: dict[FolderMoveStep, FolderStepState]
    sessions: FolderSessions | UnrecordedFolderSessions = field(default_factory=UnrecordedFolderSessions)

    @property
    def finished(self) -> bool:
        return all(state is not FolderStepState.PENDING for state in self.steps.values())


@dataclass(frozen=True)
class FolderLaunch:
    """The unique retained launch of the selected member."""

    session: str
    entry: pane_record.PaneLaunch


@dataclass(frozen=True)
class NoFolderLaunch:
    """The stopped member has no retained launch."""


@dataclass(frozen=True)
class RunningFolderSession:
    """An exact foreground process proves the member's current backend and account."""

    pid: int
    started: str
    kind: Literal["claude", "codex"]


@dataclass(frozen=True)
class StoppedFolderSession:
    """No member process is live; its retained pane is closed or at its shell."""


@dataclass(frozen=True)
class PendingFolderStartup:
    """The submitted launch has not yet proved one foreground member process."""

    reason: str


@dataclass(frozen=True)
class ClosedFolderPane:
    """The retained pane no longer exists."""


def folder_launch(row: staff.StaffRow) -> FolderLaunch | NoFolderLaunch:
    matches = [FolderLaunch(path.stem, entry)
               for path in sorted(pane_record.record_directory().glob("*.json"))
               for entry in pane_record.read_strict(path.stem).values()
               if entry["session_id"] in (row.id, row.current_session)]
    if len(matches) > 1:
        raise RenameRefused(f"{row.id}: several retained launches match")
    return matches[0] if matches else NoFolderLaunch()


def folder_pane(row: staff.StaffRow, launch: FolderLaunch) -> herdr.PaneIdentity | ClosedFolderPane:
    client = herdr.Herdr(launch.session)
    panes = client.panes()
    matches = [pane for pane in panes if pane["pane_id"] == launch.entry["pane_id"]]
    if not matches:
        if any(pane["session_id"] == row.current_session for pane in panes):
            raise RenameRefused(f"{row.id}: conversation is on another pane")
        return ClosedFolderPane()
    if len(matches) != 1 or not matches[0].get("terminal_id"):
        raise RenameRefused(f"{row.id}: retained pane has no unique terminal")
    selected = matches[0]
    if sum(pane.get("terminal_id") == selected.get("terminal_id") for pane in panes) != 1:
        raise RenameRefused(f"{row.id}: retained terminal is ambiguous")
    if selected["session_id"] not in (None, row.current_session):
        raise RenameRefused(f"{row.id}: retained pane holds another conversation")
    if any(pane["session_id"] == row.current_session and pane["pane_id"] != selected["pane_id"] for pane in panes):
        raise RenameRefused(f"{row.id}: conversation is on another pane")
    return selected


def _folder_pane(row: staff.StaffRow, launch: FolderLaunch) -> herdr.PaneIdentity:
    pane = folder_pane(row, launch)
    if isinstance(pane, ClosedFolderPane):
        raise RenameRefused(f"{row.id}: retained pane is closed")
    return pane


def _session_without_foreground_agent(
        row: staff.StaffRow, *, claude_live_reason: str = "live session has no retained pane launch",
) -> StoppedFolderSession:
    """Refuse a live target and report unknown liveness without blocking."""
    live = sessions.session_liveness(row.current_session)
    if isinstance(live, sessions.SessionLivenessUnknown):
        print(f"anomaly: {live.reason}; not blocking", file=sys.stderr)
    if isinstance(live, sessions.LiveSessionProcess):
        raise RenameRefused(f"{row.id}: {claude_live_reason}")
    try:
        threads = herdr.running_codex_threads(thread_host=staff_sources.codex_thread_host)
    except (herdr.HerdrFailure, OSError, ValueError) as error:
        print(f"anomaly: Codex liveness is unknown: {error}; not blocking", file=sys.stderr)
        threads = set[str]()
    if row.current_session in threads:
        raise RenameRefused(f"{row.id}: live Codex thread has no retained pane launch")
    return StoppedFolderSession()


def folder_session(row: staff.StaffRow, launch: FolderLaunch | NoFolderLaunch) -> RunningFolderSession | StoppedFolderSession:
    """Prove the runtime identity before a process can be stopped or resumed."""
    if isinstance(launch, NoFolderLaunch):
        return _session_without_foreground_agent(row)
    pane = folder_pane(row, launch)
    if isinstance(pane, ClosedFolderPane):
        return _session_without_foreground_agent(row)
    client = herdr.Herdr(launch.session)
    if isinstance(client.pane_activity(pane["pane_id"]), herdr.PaneAtShell):
        return _session_without_foreground_agent(row, claude_live_reason="live session is outside its retained pane")
    observed = _foreground_folder_session(row, launch, pane)
    if isinstance(observed, PendingFolderStartup):
        raise RenameRefused(observed.reason)
    return observed


def _foreground_folder_session(
        row: staff.StaffRow, launch: FolderLaunch, pane: herdr.PaneIdentity,
) -> RunningFolderSession | PendingFolderStartup:
    """Read startup progress while refusing a proved foreign conversation or account."""
    client = herdr.Herdr(launch.session)
    info = herdr.json_object(client.result("pane", "process-info", "--pane", pane["pane_id"]).get("process_info"),
                             "folder move process info")
    matches: list[RunningFolderSession] = []
    pending: list[PendingFolderStartup] = []
    for value in herdr.json_items(info.get("foreground_processes"), "folder move foreground processes"):
        process = herdr.json_object(value, "folder move process")
        try:
            arguments = [herdr.json_text(value, "folder move argument")
                         for value in herdr.json_items(process.get("argv"), "folder move arguments")]
        except herdr.HerdrFailure as error:
            pending.append(PendingFolderStartup(f"{row.id}: foreground arguments are unreadable: {error}"))
            continue
        kind = "codex" if herdr.is_codex_process(arguments) else "claude" if herdr.is_claude_process(arguments) else ""
        if not kind:
            continue
        pid = positive_int(process.get("pid"))
        if pid is None:
            raise RenameRefused("folder move process has no pid")
        try:
            observed = herdr.native_process(pid, kind)
        except (herdr.HerdrFailure, OSError, ValueError) as error:
            pending.append(PendingFolderStartup(f"{row.id}: foreground runtime identity is unproved: {error}"))
            continue
        config = observed.environment.get("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR",
                                 str(Path.home() / (".codex" if kind == "codex" else ".claude")))
        if observed.identity != row.current_session:
            raise RenameRefused(f"{row.id}: foreground runtime identity is unproved")
        if Path(config).resolve() != Path(launch.entry["config_dir"]).resolve():
            raise RenameRefused(f"{row.id}: retained pane account changed")
        matches.append(RunningFolderSession(pid, observed.started, kind))
    if len(matches) > 1:
        raise RenameRefused(f"{row.id}: retained pane has no unique member process")
    if pending:
        return pending[0]
    if not matches:
        return PendingFolderStartup(f"{row.id}: retained pane has no unique member process")
    return matches[0]


def _folder_move_config() -> Path:
    """Select the configuration file for folder moves."""
    return Path(os.environ.get("PRODUCTION_CONFIG", Path(__file__).resolve().parents[2] / "config/production.conf"))


def folder_move_deadline(key: str, settings: conf.ConfigRead) -> float:
    """The operation deadline can shorten the configured wait."""
    deadline = min(time.monotonic() + settings.number(key), worker_lifecycle.effective_deadline())
    if deadline <= time.monotonic():
        raise worker_lifecycle.OperationDeadlineExpired("folder move operation deadline expired")
    return deadline


class FolderMoveWait:
    """A bounded wait with one poll interval for a folder-move step."""

    def __init__(self, key: str, settings: conf.ConfigRead) -> None:
        self.key: str = key
        self.settings: conf.ConfigRead = settings
        self.poll: float = settings.number("FOLDER_MOVE_POLL_SECONDS")
        self.deadline: float = folder_move_deadline(key, settings)

    def restart(self) -> None:
        self.deadline = folder_move_deadline(self.key, self.settings)

    def sleep(self, failure: str) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RenameRefused(failure)
        time.sleep(min(self.poll, remaining))


def stop_folder_session(row: staff.StaffRow, launch: FolderLaunch,
                        settings: conf.ConfigRead) -> None:
    """Stop only the proved member and retain its pane at the shell."""
    observed = folder_session(row, launch)
    if isinstance(observed, StoppedFolderSession):
        return
    pane = _folder_pane(row, launch)
    wait = FolderMoveWait("FOLDER_MOVE_STOP_SECONDS", settings)
    live = sessions.process_liveness(observed.pid, observed.started)
    if not isinstance(live, sessions.LiveSessionProcess):
        raise RenameRefused(f"{row.id}: process identity changed before stop")
    pane_record.end(launch.session, pane["pane_id"], row.current_session, record=staff.StaffSnapshot((row,)))
    os.kill(observed.pid, signal.SIGTERM)
    client = herdr.Herdr(launch.session)
    while True:
        current = _folder_pane(row, launch)
        if current.get("terminal_id") != pane.get("terminal_id"):
            raise RenameRefused(f"{row.id}: retained terminal changed during stop")
        live = sessions.process_liveness(observed.pid, observed.started)
        if isinstance(live, sessions.SessionLivenessUnknown):
            raise RenameRefused(live.reason)
        if isinstance(live, sessions.SessionNotLive) and isinstance(client.pane_activity(current["pane_id"]), herdr.PaneAtShell):
            return
        wait.sleep(f"{row.id}: session did not leave its retained pane at the shell")


def resume_folder_session(row: staff.StaffRow, launch: FolderLaunch,
                          settings: conf.ConfigRead) -> None:
    """Resume the same conversation and account in the retained terminal."""
    pane = _folder_pane(row, launch)
    entry = pane_record.read_strict(launch.session).get(launch.entry["pane_id"])
    if entry is None or entry["session_id"] not in (row.id, row.current_session):
        raise RenameRefused(f"{row.id}: retained launch changed before resume")
    if not entry["flags_read"]:
        raise RenameRefused(f"{row.id}: retained launch flags are unknown")
    path = pane_record.record_path(launch.session)
    attempt = pane_launch.ResumeAttempt(pane["pane_id"], entry.copy())
    client = herdr.Herdr(launch.session)
    projected = entry.copy()
    projected["session_id"] = row.current_session
    observed = folder_session(row, launch)
    if isinstance(observed, RunningFolderSession):
        if observed.pid not in processes_inside(Path(entry["cwd"])):
            pane_launch.finish_restore(path, entry, attempt, pane_launch.FailedResume(), close=client.close_tab_confirmed)
            raise RenameRefused(f"{row.id}: resumed session is outside the moved folder")
        projected["kind"] = observed.kind
        proof = pane_launch.VerifiedResume(projected, frozenset({row.id, row.current_session}))
        pane_launch.finish_restore(path, entry, attempt, proof, close=client.close_tab_confirmed)
        return
    config = Path(entry["config_dir"])
    from scripts.lib.resume_message import resume_message

    arguments = pane_record.resume_arguments(projected)
    if pane_launch.agent_kind(projected) == "claude":
        arguments.append(resume_message("the session folder moved"))
    environment = {key: value for key, value in entry["environment"].items()
                   if key not in {"STAFF_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "HERDR_TAB_ID"}}
    environment.update({"HERDR_PANE_ID": pane["pane_id"], "HERDR_WORKSPACE_ID": pane["workspace_id"]})
    command = ["env", "-u", "STAFF_ID", "-u", "CODEX_THREAD_ID", "-u", "CLAUDE_CODE_SESSION_ID", "-u", "HERDR_TAB_ID",
               *(f"{key}={value}" for key, value in environment.items()),
               "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
               sys.executable, str(Path(__file__).resolve().parents[1] / "accounts/accounts.py"),
               "exec", config.name.removeprefix("."), *arguments]
    wait = FolderMoveWait("FOLDER_MOVE_RESUME_SECONDS", settings)
    try:
        client.run_in_pane(pane["pane_id"], command)
        while True:
            current = _folder_pane(row, launch)
            if current.get("terminal_id") != pane.get("terminal_id"):
                raise RenameRefused(f"{row.id}: retained terminal changed during resume")
            startup = _foreground_folder_session(row, launch, current)
            if isinstance(startup, RunningFolderSession):
                if startup.pid not in processes_inside(Path(entry["cwd"])):
                    raise RenameRefused(f"{row.id}: resumed session is outside the moved folder")
                proof = pane_launch.VerifiedResume(projected, frozenset({row.id, row.current_session}))
                pane_launch.finish_restore(path, entry, attempt, proof, close=client.close_tab_confirmed)
                return
            wait.sleep(f"{row.id}: resumed session is not yet proved; rerun move-folder")
    except (RenameRefused, herdr.HerdrFailure, OSError, ValueError):
        pane_launch.finish_restore(path, entry, attempt, pane_launch.FailedResume(), close=client.close_tab_confirmed)
        raise


def folder_runs(folder: Path) -> tuple[Path, ...]:
    """Select the newest run's live records by their history file and folder."""
    history = newest_run(folder)
    if history is None:
        return ()
    return tuple(sorted({path.parent for root in progress_history.SESSION_ROOTS
        for path in root.glob(f"*/{unit_runs.STATE_FILE}")
        if (state := worker_lifecycle.read_json_object(path)).get("history_file") == str(history)
        and state.get("working_dir") == str(folder)}))


def positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def run_has_mesh(run: Path) -> bool:
    if not any(run.glob("mesh_*.json")):
        return False
    from scripts.agents import codex_mesh

    return any((run / name).exists() for name in
               (codex_mesh.ROSTER_FILE, codex_mesh.SERVER_FILE, codex_mesh.RETIRED_FILE))


def live_roster(run: Path) -> dict[str, dict[str, object]]:
    if not run_has_mesh(run):
        return {}
    from scripts.agents import codex_mesh

    records = worker_lifecycle.read_worker_state(run / codex_mesh.ROSTER_FILE)
    return {name: record for name, raw in records.items()
            if (record := json_object(raw)).get("status") != "ended" and not record.get("adopted_to")}


def folder_worker_identity(run: Path, name: str) -> worker_lifecycle.WorkerIdentity:
    try:
        return worker_lifecycle.WorkerIdentity(run, name)
    except ValueError as error:
        raise RenameRefused(str(error)) from error


def observe_folder_process(pid: int) -> OwnedFolderProcess | None:
    """Capture a process identity; an incomplete read proves no ownership."""
    try:
        started = sessions.process_start(pid)
        if started:
            arguments = tuple(pane_record.process_arguments(pid))
            if sessions.process_start(pid) == started:
                return OwnedFolderProcess(pid, started, arguments)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def folder_process_live(process: OwnedFolderProcess) -> bool:
    """Check the saved start time and arguments, including an exit during the read."""
    live = sessions.process_liveness(process.pid, process.started)
    if isinstance(live, sessions.SessionNotLive):
        return False
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise RenameRefused(live.reason)
    try:
        arguments = tuple(pane_record.process_arguments(process.pid))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        live = sessions.process_liveness(process.pid, process.started)
        if isinstance(live, sessions.SessionNotLive):
            return False
        raise RenameRefused(f"process {process.pid}: arguments are unreadable: {error}") from error
    if arguments != process.arguments:
        raise RenameRefused(f"process {process.pid}: arguments changed")
    live = sessions.process_liveness(process.pid, process.started)
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise RenameRefused(live.reason)
    return isinstance(live, sessions.LiveSessionProcess)


def _folder_server_roots(run: Path) -> tuple[set[tuple[int, int]], set[int]]:
    from scripts.agents import codex_mesh

    retired = worker_lifecycle.read_worker_state(run / codex_mesh.RETIRED_FILE).get("servers", [])
    records = [json_object(record) for record in cast(list[object], retired)]
    records.append(worker_lifecycle.read_worker_state(run / codex_mesh.SERVER_FILE))
    servers: set[tuple[int, int]] = set()
    roots: set[int] = set()
    for server in records:
        pid, port = positive_int(server.get("pid")), positive_int(server.get("port"))
        if pid is None or port is None:
            continue
        servers.add((pid, port))
        try:
            arguments = pane_record.process_arguments(pid)
            environment = herdr.process_environment(pid)
            if (herdr.is_codex_process(arguments) and "app-server" in arguments and "--listen" in arguments
                    and f"ws://127.0.0.1:{port}" in arguments
                    and environment.get("UNIT_RUN_DIR") == str(run)):
                roots.add(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return servers, roots


def _is_folder_launcher(arguments: Sequence[str], run: Path, name: str, mesh: Path) -> bool:
    return (any(Path(argument).resolve() == mesh for argument in arguments)
            and any(tuple(arguments[index:index + 2]) == ("--session-dir", str(run))
                    for index in range(len(arguments) - 1))
            and any(tuple(arguments[index:index + 2]) == (option, name)
                    for index in range(len(arguments) - 1) for option in ("--to", "--name")))


def _folder_launcher_roots(run: Path, roster: dict[str, dict[str, object]], mesh: Path,
                           launchers: set[Path], parents: dict[int, int]) -> set[int]:
    roots: set[int] = set()
    for name, record in roster.items():
        pid = positive_int(record.get("launcher_pid"))
        if pid is None:
            continue
        try:
            arguments = pane_record.process_arguments(pid)
            if not _is_folder_launcher(arguments, run, name, mesh):
                continue
            roots.add(pid)
            if pid not in parents:
                parents[pid] = pane_record.process_parent(pid)
            parent = parents[pid]
            visited: set[int] = set()
            while parent > 1 and parent not in visited:
                visited.add(parent)
                parent_arguments = pane_record.process_arguments(parent)
                if (str(run) not in parent_arguments
                        or not launchers.intersection(Path(argument).resolve() for argument in parent_arguments)):
                    break
                roots.add(parent)
                if parent not in parents:
                    parents[parent] = pane_record.process_parent(parent)
                parent = parents[parent]
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return roots


def _folder_watcher_roots(inside: set[int], run: Path, servers: set[tuple[int, int]], mesh: Path) -> set[int]:
    if not servers:
        return set()
    roots: set[int] = set()
    for pid in inside:
        try:
            arguments = pane_record.process_arguments(pid)
            if (len(arguments) >= 7 and Path(arguments[0]).name == "sh" and arguments[1] == "-c"
                    and "idle-stop" in arguments[2] and Path(arguments[3]).resolve() == mesh
                    and (int(arguments[4]), int(arguments[5])) in servers
                    and Path(arguments[6]).resolve() == run.resolve()):
                roots.add(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return roots


def folder_run_processes(folder: Path, rosters: dict[Path, dict[str, dict[str, object]]]
                         ) -> tuple[OwnedFolderProcess, ...]:
    """Prove run processes and share parent observations across their ancestry walks."""
    runs = tuple(run for run in rosters if run_has_mesh(run))
    if not runs:
        return ()
    from scripts.agents import codex_mesh

    mesh = Path(codex_mesh.__file__).resolve()
    launchers = {mesh.parents[1] / "direct" / name for name in ("implement.sh", "review.sh", "assign.py")}
    inside = set(processes_inside(folder))
    roots: set[int] = set()
    parents: dict[int, int] = {}
    for run in runs:
        servers, hosts = _folder_server_roots(run)
        roots.update(hosts)
        roots.update(_folder_launcher_roots(run, rosters[run], mesh, launchers, parents))
        roots.update(_folder_watcher_roots(inside, run, servers, mesh))
    if not roots:
        return ()
    ownership = {pid: True for pid in roots}
    found: list[OwnedFolderProcess] = []
    for pid in sorted(inside):
        try:
            parent = pid
            ancestry: set[int] = set()
            while parent > 1 and parent not in ownership and parent not in ancestry:
                ancestry.add(parent)
                if parent not in parents:
                    parents[parent] = pane_record.process_parent(parent)
                parent = parents[parent]
            owned = ownership.get(parent, False)
            ownership.update((ancestor, owned) for ancestor in ancestry)
            if owned and (process := observe_folder_process(pid)) is not None:
                found.append(process)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return tuple(found)


def select_folder_sessions(row: staff.StaffRow, folder: Path,
                           observed: RunningFolderSession | StoppedFolderSession) -> FolderSessions:
    """Save original states before the first stop; retries reuse this selection."""
    runs = folder_runs(folder)
    rosters = {run: live_roster(run) for run in runs}
    selected: list[FolderWorker] = []
    for run, roster in rosters.items():
        if not roster:
            continue
        from scripts.shutdown import inventory

        seats = {seat["seat"]: seat for seat in inventory.codex_seats([run])}
        for name, record in sorted(roster.items()):
            _ = folder_worker_identity(run, name)
            thread, role, status = record.get("thread_id"), record.get("role"), record.get("status")
            cwd = record.get("cwd", str(folder))
            if (not isinstance(thread, str) or not thread or not isinstance(role, str) or not role
                    or not isinstance(status, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
                raise RenameRefused(f"{run}/{name}: worker identity or prior state is unknown")
            seat = seats.get(name)
            selected.append(FolderWorker(run, name, thread, role, PriorFolderWorker(status), Path(cwd),
                                         tuple(seat["launcher_command"]) if seat is not None else ()))
    return FolderSessions(row.current_session,
        PriorFolderSession.RUNNING if isinstance(observed, RunningFolderSession) else PriorFolderSession.PAUSED,
        runs, tuple(selected), folder_run_processes(folder, rosters))


def validate_folder_workers(selected: FolderSessions) -> None:
    """Refuse a new worker or a changed thread before a pause or a restore."""
    expected = {(worker.run, worker.name): worker for worker in selected.workers}
    for run in selected.runs:
        for name, record in live_roster(run).items():
            worker = expected.get((run, name))
            if worker is None or not worker.owns(record):
                raise RenameRefused(f"{run}/{name}: worker changed during the folder move")
        workers = selected.workers_in(run)
        if not workers:
            continue
        from scripts.agents import codex_mesh

        roster = worker_lifecycle.read_worker_state(run / codex_mesh.ROSTER_FILE)
        for worker in workers:
            if not worker.owns(json_object(roster.get(worker.name))):
                raise RenameRefused(f"{run}/{worker.name}: worker changed during the folder move")


def pause_folder_workers(selected: FolderSessions | UnrecordedFolderSessions,
                         settings: conf.ConfigRead) -> None:
    """Use the shutdown pause, then stop only the proved remaining run processes."""
    if not isinstance(selected, FolderSessions):
        return
    runs = tuple(run for run in selected.runs if run_has_mesh(run))
    if not runs and not selected.processes:
        return
    from scripts.agents import codex_mesh

    validate_folder_workers(selected)
    for run in runs:
        try:
            result = codex_mesh.command_stop(argparse.Namespace(session_dir=str(run), pause=True))
        except SystemExit as error:
            raise RenameRefused(f"cannot pause run {run}: {error}") from error
        if result:
            raise RenameRefused(f"cannot pause run {run}")
        if any(record.get("status") != "paused" for record in live_roster(run).values()):
            raise RenameRefused(f"{run}: shutdown did not pause every worker")
    if not selected.processes:
        return
    wait = FolderMoveWait("FOLDER_MOVE_STOP_SECONDS", settings)
    remaining: list[OwnedFolderProcess] = []
    for process in selected.processes:
        if not folder_process_live(process):
            continue
        try:
            os.kill(process.pid, signal.SIGTERM)
            remaining.append(process)
        except ProcessLookupError:
            continue
    while remaining:
        remaining = [process for process in remaining if folder_process_live(process)]
        if remaining:
            wait.sleep(f"run processes did not stop: {[process.pid for process in remaining]}")


def folder_process_fields(process: OwnedFolderProcess) -> dict[str, object]:
    return {"pid": process.pid, "started": process.started, "arguments": list(process.arguments)}


def folder_sessions_fields(selected: FolderSessions) -> dict[str, object]:
    return {"unit": selected.unit_session, "prior": selected.prior.value, "runs": [str(run) for run in selected.runs],
        "workers": [{"run": str(worker.run), "name": worker.name, "thread": worker.thread,
            "role": worker.role, "status": worker.prior_status.value, "cwd": str(worker.cwd),
            "launcher_command": list(worker.launcher_command),
            "restored": worker.restored,
            "resume_process": (folder_process_fields(worker.resume_process)
                               if isinstance(worker.resume_process, OwnedFolderProcess) else None)}
                    for worker in selected.workers],
        "processes": [folder_process_fields(process) for process in selected.processes]}


def read_folder_sessions(value: object) -> FolderSessions:
    fields = json_object(value)
    if (not isinstance(fields.get("unit"), str) or not fields["unit"]
            or not isinstance(fields.get("prior"), str)):
        raise RenameRefused("folder move has no prior unit state")
    runs = fields.get("runs")
    workers = fields.get("workers")
    processes = fields.get("processes")
    if (not isinstance(runs, list) or not isinstance(workers, list) or not isinstance(processes, list)):
        raise RenameRefused("folder move has invalid session lists")
    directories: list[Path] = []
    for run in cast(list[object], runs):
        if not isinstance(run, str) or not Path(run).is_absolute():
            raise RenameRefused("folder move has an invalid run folder")
        directories.append(Path(run))
    selected: list[FolderWorker] = []
    for raw in cast(list[object], workers):
        worker = json_object(raw)
        if any(not isinstance(worker.get(key), str) or not worker[key]
               for key in ("run", "name", "thread", "role", "status", "cwd")):
            raise RenameRefused("folder move has an invalid worker")
        command = worker.get("launcher_command")
        if (not isinstance(command, list) or any(not isinstance(word, str) for word in cast(list[object], command))
                or not isinstance(worker.get("restored"), bool)):
            raise RenameRefused("folder move has invalid worker restore fields")
        run = Path(cast(str, worker["run"]))
        name = cast(str, worker["name"])
        _ = folder_worker_identity(run, name)
        cwd = Path(cast(str, worker["cwd"]))
        if run not in directories or not cwd.is_absolute():
            raise RenameRefused("folder move worker has no valid name or selected run")
        selected.append(FolderWorker(run, name, cast(str, worker["thread"]),
            cast(str, worker["role"]), PriorFolderWorker(cast(str, worker["status"])), cwd, tuple(cast(list[str], command)),
            cast(bool, worker["restored"]),
            read_folder_process(worker["resume_process"]) if worker.get("resume_process") is not None
            else UnstartedWorkerResume()))
    return FolderSessions(cast(str, fields["unit"]), PriorFolderSession(cast(str, fields["prior"])),
                          tuple(directories), tuple(selected),
                          tuple(read_folder_process(raw) for raw in cast(list[object], processes)))


def read_folder_process(value: object) -> OwnedFolderProcess:
    process = json_object(value)
    pid = positive_int(process.get("pid"))
    started, arguments = process.get("started"), process.get("arguments")
    if (pid is None or not isinstance(started, str) or not started or not isinstance(arguments, list)
            or any(not isinstance(word, str) for word in cast(list[object], arguments))):
        raise RenameRefused("folder move has an invalid process identity")
    return OwnedFolderProcess(pid, started, tuple(cast(list[str], arguments)))


def moved_folder_argument(value: str, old: Path, new: Path) -> str:
    return str(new) + value[len(str(old)):] if value == str(old) or value.startswith(f"{old}/") else value


def move_run_marker(run: Path, old: Path, new: Path) -> None:
    marker = run / "working_dir"
    if marker.exists() and marker.read_text().strip() == str(old):
        atomic_file.write_text_atomic(marker, str(new) + "\n")


@contextmanager
def locked_roster(run: Path, *names: str) -> Generator[tuple[worker_lifecycle.WorkerStateFile, dict[str, object]]]:
    from scripts.agents import codex_mesh

    with worker_lifecycle.membership_operation(run), worker_lifecycle.lock_workers(*(
            folder_worker_identity(run, name) for name in names)), worker_lifecycle.locked_state(
                run / codex_mesh.ROSTER_FILE) as handle:
        yield handle, handle.read_object()


def publish_roster(handle: worker_lifecycle.WorkerStateFile, roster: dict[str, object]) -> None:
    handle.publish(json.dumps(roster, indent=2, sort_keys=True))


def move_folder_worker_paths(move: FolderMove) -> None:
    """Move saved worker paths while the run has no active writer."""
    if not isinstance(move.sessions, FolderSessions):
        for run in folder_runs(move.old) or folder_runs(move.new):
            move_run_marker(run, move.old, move.new)
        return
    if not move.sessions.runs:
        return
    validate_folder_workers(move.sessions)
    for run in move.sessions.runs:
        workers = move.sessions.workers_in(run)
        if workers:
            from scripts.agents import codex_mesh

            with locked_roster(run, *(worker.name for worker in workers)) as (handle, roster):
                changed = False
                for worker in workers:
                    record = json_object(roster.get(worker.name))
                    if not worker.owns(record):
                        raise RenameRefused(f"{run}/{worker.name}: worker changed during the folder move")
                    worker_changed = False
                    for key in ("cwd", "instructions_file", "authorized_prompt"):
                        value = record.get(key)
                        if isinstance(value, str) and (moved := moved_folder_argument(value, move.old, move.new)) != value:
                            record[key] = moved
                            worker_changed = True
                    if worker_changed:
                        codex_mesh.advance_worker_roster_revision(record)
                        changed = True
                if changed:
                    publish_roster(handle, roster)
        move_run_marker(run, move.old, move.new)


def _restore_idle_worker(worker: FolderWorker) -> None:
    from scripts.agents import codex_mesh

    with locked_roster(worker.run, worker.name) as (handle, roster):
        record = json_object(roster.get(worker.name))
        if not worker.owns(record):
            raise RenameRefused(f"{worker.name}: worker changed before restore")
        if record.get("status") != worker.prior_status.value:
            if record.get("status") != "paused":
                raise RenameRefused(f"{worker.name}: worker resumed outside the folder move")
            record["status"] = worker.prior_status.value
            codex_mesh.advance_worker_roster_revision(record)
            publish_roster(handle, roster)


def _worker_launcher_live(worker: FolderWorker, record: dict[str, object]) -> bool:
    if isinstance(worker.resume_process, OwnedFolderProcess) and folder_process_live(worker.resume_process):
        return True
    pid = positive_int(record.get("launcher_pid"))
    if pid is None:
        return False
    process = observe_folder_process(pid)
    if process is None:
        started = sessions.process_start(pid) or "unrecorded launcher"
        live = sessions.process_liveness(pid, started)
        if isinstance(live, sessions.SessionNotLive):
            return False
        raise RenameRefused(f"{worker.name}: worker launcher identity is unreadable")
    if not folder_process_live(process):
        return False
    from scripts.agents import codex_mesh

    if not _is_folder_launcher(process.arguments, worker.run, worker.name, Path(codex_mesh.__file__).resolve()):
        raise RenameRefused(f"{worker.name}: worker launcher identity changed")
    return True


def _prove_folder_worker_idle(worker: FolderWorker) -> None:
    from scripts.agents import codex_mesh

    servers, hosts = _folder_server_roots(worker.run)
    for pid, port in servers:
        if pid not in hosts:
            continue
        client = codex_mesh.Client(port, f"folder-move-{worker.name}",
                                  deadline=time.time() + codex_mesh.THREAD_RELEASE_TIMEOUT_SECS)
        try:
            state = codex_mesh.read_worker_thread_state(client, worker.thread)
            if not isinstance(state, codex_mesh.ThreadIdle):
                raise RenameRefused(f"{worker.name}: the run server has not proved the worker idle")
        except (ConnectionError, OSError, SystemExit) as error:
            raise RenameRefused(f"{worker.name}: cannot prove the worker idle: {error}") from error
        finally:
            client.close()


def _recover_worker_claim(worker: FolderWorker, observed: dict[str, object]) -> None:
    """Recover a claim after its saved launcher stops and its thread has no live turn."""
    from scripts.agents import codex_mesh

    if not isinstance(worker.resume_process, OwnedFolderProcess) or folder_process_live(worker.resume_process):
        raise RenameRefused(f"{worker.name}: worker resume process is not proved stopped")
    _prove_folder_worker_idle(worker)
    with locked_roster(worker.run, worker.name) as (handle, roster):
        record = json_object(roster.get(worker.name))
        if not worker.owns(record) or record != observed:
            raise RenameRefused(f"{worker.name}: worker changed before claim recovery")
        if (folder_process_live(worker.resume_process) or _worker_launcher_live(worker, record)
                or record.get("status") not in ("starting", "running", "waiting_capacity")):
            raise RenameRefused(f"{worker.name}: worker claim is not proved abandoned")
        record.update({"paused_from": record["status"], "status": "paused", "paused_turn": True})
        _ = record.pop("launcher_pid", None)
        _ = record.pop("turn_id", None)
        codex_mesh.advance_worker_roster_revision(record)
        publish_roster(handle, roster)


def _start_worker_resume(path: Path, move: FolderMove, worker: FolderWorker) -> None:
    from scripts.lib.resume_message import resume_worker_command
    from scripts.shutdown.inventory import PausedSeat

    cwd = Path(moved_folder_argument(str(worker.cwd), move.old, move.new))
    prompt = worker.run / f"{worker.name}.resume-prompt.md"
    note = (f"Your unit folder moved from {move.old} to {move.new}. "
            + f"Use {cwd} for commands. Continue your current task on this thread.\n")
    previous_prompt = prompt.read_text(encoding="utf-8") if prompt.exists() else ""
    if note not in previous_prompt:
        atomic_file.write_text_atomic(prompt, previous_prompt + "\n" + note)
    seat = PausedSeat(run_dir=str(worker.run), seat=worker.name, role=worker.role,
        thread_id=worker.thread, turn_live=True, launcher_command=list(worker.launcher_command))
    command = [moved_folder_argument(word, move.old, move.new)
               for word in shlex.split(resume_worker_command(seat, str(cwd)))]
    environment = {key: value for key, value in os.environ.items() if key not in
        {"STAFF_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "STAFF_OPERATION_FD"}}
    with (worker.run / f"{worker.name}.folder-move-resume.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    observed = observe_folder_process(process.pid)
    if observed is not None:
        worker.resume_process = observed
        save_folder_move(path, move)


def _resume_worker(path: Path, move: FolderMove, worker: FolderWorker, wait: FolderMoveWait) -> None:
    from scripts.agents import codex_mesh

    wait.restart()
    roster_path = worker.run / codex_mesh.ROSTER_FILE
    current = json_object(worker_lifecycle.read_worker_state(roster_path).get(worker.name))
    if not worker.owns(current):
        raise RenameRefused(f"{worker.name}: worker changed before resume")
    if current.get("status") == "done":
        return
    live = _worker_launcher_live(worker, current)
    if (current.get("status") in ("starting", "running", "waiting_capacity")
            and not live and isinstance(worker.resume_process, OwnedFolderProcess)):
        _recover_worker_claim(worker, current)
        current = json_object(worker_lifecycle.read_worker_state(roster_path).get(worker.name))
    if current.get("status") == "paused" and not live:
        _start_worker_resume(path, move, worker)
    while True:
        current = json_object(worker_lifecycle.read_worker_state(roster_path).get(worker.name))
        if not worker.owns(current):
            raise RenameRefused(f"{worker.name}: worker thread changed during resume")
        if current.get("status") == "done":
            return
        if current.get("status") in ("running", "waiting_capacity"):
            if not _worker_launcher_live(worker, current):
                raise RenameRefused(f"{worker.name}: worker has no proved live launcher; rerun move-folder")
            return
        wait.sleep(f"{worker.name}: worker resume is not yet proved; rerun move-folder")


def restore_folder_workers(path: Path, move: FolderMove,
                           settings: conf.ConfigRead) -> None:
    """Resume only the workers that were running, using their existing thread launchers."""
    if not isinstance(move.sessions, FolderSessions) or not move.sessions.workers:
        return
    validate_folder_workers(move.sessions)
    waiting = tuple(worker for worker in move.sessions.workers if not worker.restored)
    wait = FolderMoveWait("FOLDER_MOVE_RESUME_SECONDS", settings) if any(worker.was_running for worker in waiting) else None
    for worker in waiting:
        if worker.was_running:
            assert wait is not None
            _resume_worker(path, move, worker, wait)
        else:
            _restore_idle_worker(worker)
        worker.restored = True
        save_folder_move(path, move)


def read_folder_move(path: Path) -> FolderMove:
    fields = json_object(read_json(path))
    if set(fields) not in ({"old", "new", "steps"}, {"old", "new", "steps", "sessions"}):
        raise RenameRefused(f"{path}: invalid folder move fields")
    old, new = fields["old"], fields["new"]
    if not isinstance(old, str) or not isinstance(new, str) or not Path(old).is_absolute() or not Path(new).is_absolute():
        raise RenameRefused(f"{path}: folder move paths must be absolute")
    if old == new or Path(new).is_relative_to(Path(old)):
        raise RenameRefused(f"{path}: folder move paths overlap")
    steps = json_object(fields["steps"])
    if set(steps) != set(FOLDER_MOVE_STEPS):
        raise RenameRefused(f"{path}: invalid folder move steps")
    try:
        if any(not isinstance(value, str) for value in steps.values()):
            raise ValueError("step state must be text")
        progress: dict[FolderMoveStep, FolderStepState] = {
            step: FolderStepState(cast(str, steps[step])) for step in FOLDER_MOVE_STEPS}
    except ValueError as error:
        raise RenameRefused(f"{path}: invalid folder move step state") from error
    pending = False
    for step in FOLDER_MOVE_STEPS:
        state = progress[step]
        if state is FolderStepState.SKIPPED and step not in ("stop", "resume"):
            raise RenameRefused(f"{path}: required folder move step was skipped")
        if state is FolderStepState.PENDING:
            pending = True
        elif pending and state is FolderStepState.DONE:
            raise RenameRefused(f"{path}: folder move steps are out of order")
    if (progress["stop"] is FolderStepState.SKIPPED) != (progress["resume"] is FolderStepState.SKIPPED):
        raise RenameRefused(f"{path}: stop and resume states disagree")
    return FolderMove(Path(old), Path(new), progress,
                      read_folder_sessions(fields["sessions"]) if "sessions" in fields else UnrecordedFolderSessions())


def save_folder_move(path: Path, move: FolderMove) -> None:
    fields: dict[str, object] = {"old": str(move.old), "new": str(move.new),
        "steps": {step: move.steps[step].value for step in FOLDER_MOVE_STEPS}}
    if isinstance(move.sessions, FolderSessions) and move.steps["stop"] is not FolderStepState.SKIPPED:
        fields["sessions"] = folder_sessions_fields(move.sessions)
    atomic_file.write_text_atomic(path, json.dumps(fields, indent=2, sort_keys=True) + "\n")


def _folder_boundary(folder: Path, allowed: set[int]) -> None:
    pids = set(processes_inside(folder)) - allowed
    if pids:
        raise RenameRefused(f"processes inside {folder}: {', '.join(str(pid) for pid in sorted(pids))}")


def folder_shell(row: staff.StaffRow, launch: FolderLaunch | NoFolderLaunch) -> set[int]:
    """The retained shell belongs to the unit session and remains open during the move."""
    if isinstance(launch, NoFolderLaunch):
        return set()
    pane = folder_pane(row, launch)
    if isinstance(pane, ClosedFolderPane):
        return set()
    return {pane_record.pane_shell_pid(herdr.HerdrPlace(launch.session, pane["pane_id"]))}


def retained_launch(launch: FolderLaunch | NoFolderLaunch) -> FolderLaunch:
    if not isinstance(launch, FolderLaunch):
        raise RenameRefused("retained launch is missing")
    return launch


def folder_supervisor(observed: RunningFolderSession) -> set[int]:
    """Allow only the live supervisor whose exact command starts this Claude child."""
    if observed.kind != "claude":
        return set()
    try:
        parent = pane_record.process_parent(observed.pid)
        arguments = pane_record.process_arguments(parent)
        program = Path(__file__).resolve().parents[1] / "lib/relaunch.py"
        if (len(arguments) < 5 or Path(arguments[1]).resolve() != program
                or arguments[2:4] != ["run", "--"]):
            return set()
        child = pane_record.process_arguments(observed.pid)
        started = sessions.process_start(parent)
        expected, _ = relaunch.child_launch(arguments[4:], herdr.process_environment(parent))
        if (expected != child or not herdr.is_claude_process(child) or not started
                or not isinstance(sessions.process_liveness(parent, started), sessions.LiveSessionProcess)
                or not isinstance(sessions.process_liveness(observed.pid, observed.started), sessions.LiveSessionProcess)
                or pane_record.process_parent(observed.pid) != parent
                or sessions.process_start(parent) != started):
            return set()
        return {parent}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return set()


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


def move_folder(unit: staff.StaffId, destination: Path) -> None:
    """Finish one folder move without changing the member's name or branch."""
    destination = destination.expanduser().resolve()
    settings = conf.read(_folder_move_config())
    with folder_move_lock(unit), ExitStack() as operation:
        _ = operation.enter_context(staff.operation())
        _ = operation.enter_context(herdr.process_action())
        record = staff.snapshot()
        row = record.by_id(unit)
        if row is None or row.kind != "unit" or not isinstance(row.worktree, staff.PresentWorktree):
            raise RenameRefused(f"{unit}: move-folder requires a unit with a present worktree")
        if row.worktree.machine != remote.local_machine():
            raise RenameRefused(f"{unit}: run move-folder on {row.worktree.machine}")
        for note in herdr.repair_retained_launches(record, pane_record.record_directory(), member=row.id):
            print(note)
        path = staff.folder_move_path(unit)
        previous = read_folder_move(path) if path.exists() else None
        if previous is not None and not previous.finished and previous.new != destination:
            raise RenameRefused(f"{unit}: finish the recorded move to {previous.new} first")
        if previous is not None and previous.new == destination:
            if previous.finished:
                if row.worktree.path != destination:
                    raise RenameRefused(f"{unit}: completed move differs from the staff worktree")
                if previous.steps["resume"] is FolderStepState.DONE:
                    launch = folder_launch(row)
                    if isinstance(launch, FolderLaunch) and launch.entry.get("ended", False):
                        observed = folder_session(row, launch)
                        if isinstance(observed, RunningFolderSession):
                            resume_folder_session(row, launch, settings)
                print("unchanged")
                return
            move = previous
        else:
            if row.worktree.path == destination:
                print("unchanged")
                return
            if exists(destination):
                raise RenameRefused(f"destination already exists: {destination}")
            if destination.is_relative_to(row.worktree.path):
                raise RenameRefused("destination is inside the unit worktree")
            move = FolderMove(row.worktree.path, destination,
                              {step: FolderStepState.PENDING for step in FOLDER_MOVE_STEPS})
        if row.worktree.path not in (move.old, move.new):
            raise RenameRefused(f"{unit}: staff worktree differs from the recorded move")
        launch = folder_launch(row)
        observed = folder_session(row, launch)
        if isinstance(move.sessions, FolderSessions) and move.sessions.unit_session != row.current_session:
            raise RenameRefused(f"{unit}: unit conversation changed during the folder move")
        if previous is None or previous.finished:
            if not (move.old / ".git").is_file() or git(move.old, "rev-parse", "--show-toplevel") != str(move.old):
                raise RenameRefused(f"{move.old}: move-folder requires a linked worktree root")
            allowed = folder_shell(row, launch)
            selected = select_folder_sessions(row, move.old, observed)
            allowed.update(process.pid for process in selected.processes)
            if isinstance(observed, RunningFolderSession):
                allowed.add(observed.pid)
                allowed.update(folder_supervisor(observed))
            _folder_boundary(move.old, allowed)
            move.sessions = selected
            has_mesh = bool(selected.workers or selected.processes or any(run_has_mesh(run) for run in selected.runs))
            if selected.prior is PriorFolderSession.RUNNING:
                entry = retained_launch(launch).entry
                if not entry["flags_read"] or not entry["config_dir"]:
                    raise RenameRefused(f"{unit}: retained launch cannot resume the session")
            elif not has_mesh:
                move.steps["stop"] = move.steps["resume"] = FolderStepState.SKIPPED
            if selected.prior is PriorFolderSession.RUNNING or has_mesh:
                for key in ("FOLDER_MOVE_STOP_SECONDS", "FOLDER_MOVE_RESUME_SECONDS", "FOLDER_MOVE_POLL_SECONDS"):
                    _ = settings.number(key)
            save_folder_move(path, move)
        unit_running = not isinstance(move.sessions, FolderSessions) or move.sessions.prior is PriorFolderSession.RUNNING
        for step in FOLDER_MOVE_STEPS:
            if move.steps[step] is not FolderStepState.PENDING:
                continue
            if step == "stop":
                if unit_running:
                    stop_folder_session(row, retained_launch(launch), settings)
                pause_folder_workers(move.sessions, settings)
            elif step == "worktree":
                if isinstance(folder_session(row, launch), RunningFolderSession):
                    raise RenameRefused(f"{unit}: session resumed before the folder move")
                _folder_boundary(move.old if move.old.exists() else move.new, folder_shell(row, launch))
                if move.old.exists():
                    if exists(move.new):
                        raise RenameRefused(f"destination already exists: {move.new}")
                    move.new.parent.mkdir(parents=True, exist_ok=True)
                    _ = git(move.old, "worktree", "move", str(move.old), str(move.new))
                if git(move.new, "rev-parse", "--show-toplevel") != str(move.new):
                    raise RenameRefused(f"{move.new}: moved worktree is not registered")
                atomic_file.sync_folder(move.old.parent)
                atomic_file.sync_folder(move.new.parent)
            elif step == "staff":
                _ = staff.set_worktree(unit, staff.PresentWorktree(row.worktree.machine, move.new), expected=row.worktree)
            elif step == "launch":
                pane_record.move_cwd(unit, move.old, move.new, record)
            elif step == "run":
                run = newest_run(move.old) or newest_run(move.new)
                if run is not None:
                    move_run(run, move.old, move.new, git(move.new, "branch", "--show-current"))
                move_folder_worker_paths(move)
            else:
                if unit_running:
                    resume_folder_session(row, retained_launch(launch), settings)
                operation.close()
                restore_folder_workers(path, move, settings)
            move.steps[step] = FolderStepState.DONE
            save_folder_move(path, move)
        print(f"moved {unit}: {move.old} -> {move.new}")


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
