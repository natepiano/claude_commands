#!/usr/bin/env python3
"""Remove the scratch copies an ended run left in its run folder.

A run folder under /tmp/claude/unit/ (or /tmp/claude/delegate/) collects the
copies its phases make: gate copies under phases/, exports under scratch/, and
before and after builds such as p62_bin and p26_before. Each is a git checkout,
a cargo target, or both. Every non-retired worker's live detached command keeps its
working directory and containing checkout. end_session.sh passes every run
folder the ending director holds. In each one this removes:

  * verify_cache/, the verify.sh pass records; the time they saved is already
    in ~/.local/state/verify/saved.jsonl;
  * every folder, at any depth, that holds .git (a folder or a file): a checkout;
  * every folder, at any depth, that holds .rustc_info.json or CACHEDIR.TAG: a
    cargo target.

With --tracked it also ends tracked scratch for these runs, preserving live
command directories and the scratch registry's existing unsaved-work holds.

A checkout whose .git is a file is a linked worktree. It goes through its
source repo (git worktree remove), so the source keeps no stale entry; a stale
entry keeps the branch "already checked out" and the next git worktree add of
it refuses. When that fails, the folder is deleted and the source pruned.

It never descends into a folder it removes. A symbolic link is never followed:
it is not a folder to this walk, and shutil.rmtree unlinks a link it finds, so
nothing outside the run folder is reached. The run folder itself stays, with
its ledgers, logs, diffs and json.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import workers
from scripts.lib import scratch

VERIFY_CACHE = "verify_cache"
# A folder holding any of these is a checkout or a cargo target.
COPY_MARKERS = (".git", ".rustc_info.json", "CACHEDIR.TAG")


def is_copy(folder: str) -> bool:
    return any(os.path.lexists(os.path.join(folder, marker)) for marker in COPY_MARKERS)


def scratch_folders(run: Path) -> list[Path]:
    """verify_cache/ and each outermost checkout or cargo target under run, never run itself."""
    if run.is_symlink() or not run.is_dir():
        return []
    protected = workers.protected_working_directories(run)
    found: list[Path] = []
    frontier = [str(run)]
    while frontier:
        current = frontier.pop()
        try:
            with os.scandir(current) as entries:
                folders = [entry.path for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for folder in sorted(folders):
            candidate = Path(folder).resolve()
            if candidate in protected:
                continue
            if (current == str(run) and os.path.basename(folder) == VERIFY_CACHE) or is_copy(folder):
                if not any(candidate in cwd.parents for cwd in protected):
                    found.append(Path(folder))
            else:
                frontier.append(folder)
    return found


def worktree_source(folder: Path) -> Path | None:
    """The common git folder of the repo folder is a linked worktree of, or None when folder is no
    linked worktree. .git holds "gitdir: <common>/worktrees/<name>"; that folder names the common
    one in its commondir file, which is read when present."""
    dot_git = folder / ".git"
    if dot_git.is_symlink() or not dot_git.is_file():
        return None
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = folder / gitdir
    try:
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, UnicodeDecodeError):
        common = gitdir.parent.parent
    return common if common.is_dir() else None


def git(common: Path, *arguments: str) -> bool:
    try:
        result = subprocess.run(["git", "--git-dir", str(common), "worktree", *arguments],
                                check=False, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def remove(folder: Path) -> None:
    """Remove one scratch folder; a linked worktree through its source repo, so no entry stays."""
    source = worktree_source(folder)
    # Twice --force also removes a locked worktree: the run that locked it has ended.
    if source is not None and git(source, "remove", "--force", "--force", str(folder)):
        return
    shutil.rmtree(folder)
    if source is not None:
        _ = git(source, "prune")


def end_tracked_scratch(run: Path) -> int:
    """End the run's tracked scratch except folders still hosting its commands."""
    task = f"run:{run.name}"
    protected = workers.protected_working_directories(run)

    def retained(path: Path) -> bool:
        resolved = path.resolve()
        return any(cwd == resolved or resolved in cwd.parents for cwd in protected)

    return len(scratch.end(task, keep=retained))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("runs", nargs="*", help="run folders whose scratch copies go")
    _ = parser.add_argument("--tracked", action="store_true", help="also end tracked scratch for these runs")
    args = parser.parse_args()
    runs = [Path(run) for run in cast("list[str]", args.runs)]
    failed = 0
    for run in runs:
        removed = 0
        for folder in scratch_folders(run):
            try:
                remove(folder)
            except OSError as error:
                failed += 1
                print(f"remove_run_scratch: could not remove {folder}: {error}", file=sys.stderr)
                continue
            removed += 1
        if cast(bool, args.tracked):
            removed += end_tracked_scratch(run)
        if removed:
            print(f"remove_run_scratch: removed {removed} scratch folders from {run}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
