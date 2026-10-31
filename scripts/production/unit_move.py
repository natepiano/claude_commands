"""Move one stopped unit's branch, worktree and records: shared by a production rename and a unit rename.

Every function takes the showrunner checkout and the old and new values it moves, so the caller's
journal decides the order and records progress. Each one is safe to run again after a refusal.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
import progress_history  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import herdr  # noqa: E402
import pane_record  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
if TYPE_CHECKING:
    from scripts.shutdown import shutdown_record
from rename_state import RenameRefused, read_json, write_json  # noqa: E402

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


def close_unit_pane(pane: UnitPane | None, _saved: shutdown_record.SessionShutdown) -> None:
    """Close the unit's exact session-qualified pane after the caller stops the unit."""
    if pane is None:
        return
    try:
        herdr.Herdr(pane.place.session).close_pane(pane.place.pane)
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
