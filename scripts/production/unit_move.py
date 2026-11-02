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
import signal
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
from scripts.lib import atomic_file, conf, relaunch  # noqa: E402
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


FolderMoveStep = Literal["stop", "worktree", "staff", "launch", "run", "resume"]
FOLDER_MOVE_STEPS: tuple[FolderMoveStep, ...] = ("stop", "worktree", "staff", "launch", "run", "resume")


@dataclass
class FolderMove:
    """Paths and durable progress for one folder move."""

    old: Path
    new: Path
    steps: dict[FolderMoveStep, FolderStepState]

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
    info = herdr.json_object(client.result("pane", "process-info", "--pane", pane["pane_id"]).get("process_info"),
                             "folder move process info")
    matches: list[RunningFolderSession] = []
    for value in herdr.json_items(info.get("foreground_processes"), "folder move foreground processes"):
        process = herdr.json_object(value, "folder move process")
        arguments = [herdr.json_text(value, "folder move argument")
                     for value in herdr.json_items(process.get("argv"), "folder move arguments")]
        kind = "codex" if herdr.is_codex_process(arguments) else "claude" if herdr.is_claude_process(arguments) else ""
        if not kind:
            continue
        pid = process.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RenameRefused("folder move process has no pid")
        try:
            observed = herdr.native_process(pid, kind)
        except (herdr.HerdrFailure, OSError, ValueError) as error:
            raise RenameRefused(f"{row.id}: foreground runtime identity is unproved: {error}") from error
        config = observed.environment.get("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR",
                                 str(Path.home() / (".codex" if kind == "codex" else ".claude")))
        if observed.identity != row.current_session:
            raise RenameRefused(f"{row.id}: foreground runtime identity is unproved")
        if Path(config).resolve() != Path(launch.entry["config_dir"]).resolve():
            raise RenameRefused(f"{row.id}: retained pane account changed")
        matches.append(RunningFolderSession(pid, observed.started, kind))
    if len(matches) != 1:
        raise RenameRefused(f"{row.id}: retained pane has no unique member process")
    return matches[0]


def _folder_move_config() -> Path:
    """Select the configuration file for folder moves."""
    return Path(os.environ.get("PRODUCTION_CONFIG", Path(__file__).resolve().parents[2] / "config/production.conf"))


def folder_move_deadline(key: str) -> float:
    """The operation deadline can shorten the configured wait."""
    deadline = min(time.monotonic() + conf.number(_folder_move_config(), key), worker_lifecycle.effective_deadline())
    if deadline <= time.monotonic():
        raise worker_lifecycle.OperationDeadlineExpired("folder move operation deadline expired")
    return deadline


def stop_folder_session(row: staff.StaffRow, launch: FolderLaunch) -> None:
    """Stop only the proved member and retain its pane at the shell."""
    observed = folder_session(row, launch)
    if isinstance(observed, StoppedFolderSession):
        return
    pane = _folder_pane(row, launch)
    deadline = folder_move_deadline("FOLDER_MOVE_STOP_SECONDS")
    poll = conf.number(_folder_move_config(), "FOLDER_MOVE_POLL_SECONDS")
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
        if time.monotonic() >= deadline:
            raise RenameRefused(f"{row.id}: session did not leave its retained pane at the shell")
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def resume_folder_session(row: staff.StaffRow, launch: FolderLaunch) -> None:
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
    agents = [agent for agent in client.agents() if agent["pane_id"] == pane["pane_id"]]
    if len(agents) != 1 or agents[0].get("agent") not in ("claude", "codex"):
        raise RenameRefused(f"{row.id}: retained pane backend is unavailable")
    kind = agents[0].get("agent")
    projected["kind"] = cast(Literal["claude", "codex"], kind)
    arguments = pane_record.resume_arguments(projected)
    environment = {key: value for key, value in entry["environment"].items()
                   if key not in {"STAFF_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "HERDR_TAB_ID"}}
    environment.update({"HERDR_PANE_ID": pane["pane_id"], "HERDR_WORKSPACE_ID": pane["workspace_id"]})
    command = ["env", "-u", "STAFF_ID", "-u", "CODEX_THREAD_ID", "-u", "CLAUDE_CODE_SESSION_ID", "-u", "HERDR_TAB_ID",
               *(f"{key}={value}" for key, value in environment.items()),
               "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
               sys.executable, str(Path(__file__).resolve().parents[1] / "accounts/accounts.py"),
               "exec", config.name.removeprefix("."), *arguments]
    deadline = folder_move_deadline("FOLDER_MOVE_RESUME_SECONDS")
    poll = conf.number(_folder_move_config(), "FOLDER_MOVE_POLL_SECONDS")
    try:
        client.run_in_pane(pane["pane_id"], command)
        while True:
            current = _folder_pane(row, launch)
            if current.get("terminal_id") != pane.get("terminal_id"):
                raise RenameRefused(f"{row.id}: retained terminal changed during resume")
            observed = folder_session(row, launch)
            if isinstance(observed, RunningFolderSession):
                if observed.pid not in processes_inside(Path(entry["cwd"])):
                    raise RenameRefused(f"{row.id}: resumed session is outside the moved folder")
                proof = pane_launch.VerifiedResume(projected, frozenset({row.id, row.current_session}))
                pane_launch.finish_restore(path, entry, attempt, proof, close=client.close_tab_confirmed)
                return
            if time.monotonic() >= deadline:
                raise RenameRefused(f"{row.id}: resumed session is not yet proved; rerun move-folder")
            time.sleep(min(poll, max(0, deadline - time.monotonic())))
    except (RenameRefused, herdr.HerdrFailure, OSError, ValueError):
        pane_launch.finish_restore(path, entry, attempt, pane_launch.FailedResume(), close=client.close_tab_confirmed)
        raise


def read_folder_move(path: Path) -> FolderMove:
    fields = json_object(read_json(path))
    if set(fields) != {"old", "new", "steps"}:
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
    return FolderMove(Path(old), Path(new), progress)


def save_folder_move(path: Path, move: FolderMove) -> None:
    atomic_file.write_text_atomic(path, json.dumps({"old": str(move.old), "new": str(move.new),
        "steps": {step: move.steps[step].value for step in FOLDER_MOVE_STEPS}}, indent=2, sort_keys=True) + "\n")


def _folder_boundary(folder: Path, allowed: set[int]) -> None:
    run = newest_run(folder)
    if run is not None and (phase := open_phase(run)):
        raise RenameRefused(f"phase {phase} is open; move the folder at a phase boundary")
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


def move_folder(unit: staff.StaffId, destination: Path) -> None:
    """Finish one folder move without changing the member's name or branch."""
    destination = destination.expanduser().resolve()
    with staff.operation(), herdr.process_action():
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
                            resume_folder_session(row, launch)
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
        if previous is None or previous.finished:
            if not (move.old / ".git").is_file() or git(move.old, "rev-parse", "--show-toplevel") != str(move.old):
                raise RenameRefused(f"{move.old}: move-folder requires a linked worktree root")
            allowed = folder_shell(row, launch)
            if isinstance(observed, RunningFolderSession):
                allowed.add(observed.pid)
                allowed.update(folder_supervisor(observed))
            _folder_boundary(move.old, allowed)
            if isinstance(observed, StoppedFolderSession):
                move.steps["stop"] = move.steps["resume"] = FolderStepState.SKIPPED
            elif isinstance(launch, FolderLaunch) and (not launch.entry["flags_read"] or not launch.entry["config_dir"]):
                raise RenameRefused(f"{unit}: retained launch cannot resume the session")
            if isinstance(observed, RunningFolderSession):
                for key in ("FOLDER_MOVE_STOP_SECONDS", "FOLDER_MOVE_RESUME_SECONDS", "FOLDER_MOVE_POLL_SECONDS"):
                    _ = conf.number(_folder_move_config(), key)
            save_folder_move(path, move)
        for step in FOLDER_MOVE_STEPS:
            if move.steps[step] is not FolderStepState.PENDING:
                continue
            if step == "stop":
                if not isinstance(launch, FolderLaunch):
                    raise RenameRefused(f"{unit}: retained launch is missing")
                stop_folder_session(row, launch)
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
                _ = staff.set_worktree(unit, staff.PresentWorktree(row.worktree.machine, move.new))
            elif step == "launch":
                pane_record.move_cwd(unit, move.old, move.new, record)
            elif step == "run":
                run = newest_run(move.old) or newest_run(move.new)
                if run is not None:
                    move_run(run, move.old, move.new, git(move.new, "branch", "--show-current"))
            else:
                if not isinstance(launch, FolderLaunch):
                    raise RenameRefused(f"{unit}: retained launch is missing")
                resume_folder_session(row, launch)
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
