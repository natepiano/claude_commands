#!/usr/bin/env python3
"""Remove the scratch copies an ended run left in its run folder.

A run folder under /tmp/claude/unit/ (or /tmp/claude/delegate/) collects the
copies its phases make: gate copies under phases/, exports under scratch/, and
before and after builds such as p62_bin and p26_before. Each is a git checkout,
a cargo target, or both. Nothing reads them after the run ends, and before this
they stayed in /tmp until the disk filled. end_session.sh passes every run
folder the ending director holds. In each one this removes:

  * verify_cache/, the verify.sh pass records; the time they saved is already
    in ~/.local/state/verify/saved.jsonl;
  * every folder, at any depth, that holds .git (a folder or a file): a checkout;
  * every folder, at any depth, that holds .rustc_info.json or CACHEDIR.TAG: a
    cargo target.

It never descends into a folder it removes. A symbolic link is never followed:
it is not a folder to this walk, and shutil.rmtree unlinks a link it finds, so
nothing outside the run folder is reached. The run folder itself stays, with
its ledgers, logs, diffs and json.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import cast

VERIFY_CACHE = "verify_cache"
# A folder holding any of these is a checkout or a cargo target.
COPY_MARKERS = (".git", ".rustc_info.json", "CACHEDIR.TAG")


def is_copy(folder: str) -> bool:
    return any(os.path.lexists(os.path.join(folder, marker)) for marker in COPY_MARKERS)


def scratch_folders(run: Path) -> list[Path]:
    """verify_cache/ and each outermost checkout or cargo target under run, never run itself."""
    if run.is_symlink() or not run.is_dir():
        return []
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
            if (current == str(run) and os.path.basename(folder) == VERIFY_CACHE) or is_copy(folder):
                found.append(Path(folder))
            else:
                frontier.append(folder)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("runs", nargs="*", help="run folders whose scratch copies go")
    runs = [Path(run) for run in cast("list[str]", parser.parse_args().runs)]
    failed = 0
    for run in runs:
        removed = 0
        for folder in scratch_folders(run):
            try:
                shutil.rmtree(folder)
            except OSError as error:
                failed += 1
                print(f"remove_run_scratch: could not remove {folder}: {error}", file=sys.stderr)
                continue
            removed += 1
        if removed:
            print(f"remove_run_scratch: removed {removed} scratch folders from {run}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
