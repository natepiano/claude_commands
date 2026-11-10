#!/usr/bin/env python3
"""Fast-forward a main checkout to origin/main without deleting local work.

Changes that match origin/main can come from an interrupted update. Prove every
candidate's content, mode, and index state before changing any file. Remove proved
untracked files before restoring tracked paths to HEAD. Then finish the update
with git merge --ff-only. A blocked report names every path that fails the proof.

Each run appends a JSON line to SYNC_LOG. Each blocked run with --notify calls
send.py with a key for the host and state. send.py limits delivery to one message
per main_sync_repeat_minutes window from send.conf. A successful run changes no
notification state. Fetch failures advise github-warmup only when
github-warm-status fails, times out, or is missing.

Exit 0 means the checkout is at origin/main. Exit 1 means the run is blocked.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib import conf
from scripts.message import session_send
from scripts.lib.process_failure import failure_detail

STATE_ENV = "MAIN_SYNC_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "main-sync")
SYNC_LOG = "sync.jsonl"
FETCH_TIMEOUT_SECONDS = 120
IN_PROGRESS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")
# A job has no terminal: git and ssh must fail rather than ask.
GIT_ENVIRONMENT = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=20"}

CURRENT = "current"
ADVANCED = "advanced"


@dataclass(frozen=True)
class GitFileState:
    mode: str
    object_id: str


ABSENT_FILE = GitFileState("000000", "")


@dataclass(frozen=True)
class Outcome:
    state: str
    detail: str

    @property
    def blocked(self) -> bool:
        return self.state not in (CURRENT, ADVANCED)


def _git(checkout: Path, *arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True, text=True,
                              errors="surrogateescape", check=False,
                              timeout=timeout, env={**os.environ, **GIT_ENVIRONMENT})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git"], 124, "", f"git {arguments[0]} took over {timeout:.0f} s")


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    return failure_detail(result, lines=1).replace("\n", "; ")


def _matches_file(checkout: Path, name: str, entry: GitFileState) -> bool:
    path = checkout / name
    if any((checkout / parent).is_symlink() for parent in Path(name).parents):
        return False
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return entry == ABSENT_FILE
    if stat.S_ISLNK(mode):
        git_mode = "120000"
        content = os.fsencode(os.readlink(path))
    elif stat.S_ISREG(mode):
        git_mode = "100755" if mode & stat.S_IXUSR else "100644"
        if entry.mode != git_mode:
            return False
        content = path.read_bytes()
    else:
        return False
    if entry.mode != git_mode:
        return False
    hashed = subprocess.run(["git", "-C", str(checkout), "hash-object", "--no-filters", "--stdin"],
                            input=content, capture_output=True, check=False, timeout=30,
                            env={**os.environ, **GIT_ENVIRONMENT})
    return hashed.returncode == 0 and hashed.stdout.decode("ascii").strip() == entry.object_id


def _has_untracked_update(checkout: Path, blocking_details: list[str]) -> bool:
    untracked = _git(checkout, "ls-files", "--others", "-z")
    if untracked.returncode:
        blocking_details.append(_first_line(untracked))
        return False
    if not untracked.stdout:
        return False
    added = _git(checkout, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z",
                 "HEAD", "origin/main", "--")
    if added.returncode:
        blocking_details.append(_first_line(added))
        return False
    added_paths = set(added.stdout.split("\0")) - {""}
    return bool(set(untracked.stdout.split("\0")) & added_paths)


def _finish_interrupted_update(checkout: Path, blocking_details: list[str]) -> bool:
    """Restore candidates only after every changed working file matches origin/main."""
    queries = [
        _git(checkout, "ls-tree", "-r", "-z", "HEAD"),
        _git(checkout, "ls-tree", "-r", "-z", "origin/main"),
        _git(checkout, "-c", "core.filemode=true", "diff", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        _git(checkout, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        _git(checkout, "ls-files", "--others", "-z"),
        _git(checkout, "ls-files", "--stage", "-z"),
    ]
    for result in queries:
        if result.returncode:
            blocking_details.append(_first_line(result))
            return False
    trees: list[dict[str, GitFileState]] = []
    for result in queries[:2]:
        files: dict[str, GitFileState] = {}
        for record in result.stdout.split("\0"):
            if record:
                metadata, name = record.split("\t", 1)
                mode, kind, object_id = metadata.split()
                if kind not in ("blob", "commit"):
                    blocking_details.append(f"git reported an unsupported object type for {name}: {kind}")
                    return False
                files[name] = GitFileState(mode, object_id)
        trees.append(files)
    head_files, target_files = trees
    index_files: dict[str, GitFileState] = {}
    unmerged: set[str] = set()
    for record in queries[5].stdout.split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, object_id, stage = metadata.split()
            if stage != "0":
                unmerged.add(name)
            else:
                index_files[name] = GitFileState(mode, object_id)
    tracked = (set(queries[2].stdout.split("\0") + queries[3].stdout.split("\0")) - {""}) | unmerged
    untracked = {name for name in queries[4].stdout.split("\0")
                 if name in target_files and name not in head_files}
    restore: list[str] = []
    remove: list[str] = []
    for name in sorted(tracked | untracked):
        try:
            if name in unmerged or (name in tracked and index_files.get(name, ABSENT_FILE) not in (
                    head_files.get(name, ABSENT_FILE), target_files.get(name, ABSENT_FILE))):
                blocking_details.append(name)
                continue
            if _matches_file(checkout, name, head_files.get(name, ABSENT_FILE)):
                continue
            if not _matches_file(checkout, name, target_files.get(name, ABSENT_FILE)):
                blocking_details.append(name)
                continue
            (restore if name in tracked else remove).append(name)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            blocking_details.append(name)
    if blocking_details:
        return False
    try:
        for name in remove:
            (checkout / name).unlink()
        if restore:
            restored = _git(checkout, "--literal-pathspecs", "restore", "--source=HEAD", "--staged", "--worktree",
                            "--", *restore)
            if restored.returncode:
                blocking_details.append(_first_line(restored))
                return False
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        blocking_details.append(str(error) or type(error).__name__)
        return False
    return True


def sync(checkout: Path) -> Outcome:
    """Fast-forward checkout to origin/main when that is safe, else say why not."""
    branch = _git(checkout, "branch", "--show-current")
    if branch.returncode:
        return Outcome("unreadable", f"git cannot read {checkout}: {_first_line(branch)}")
    if branch.stdout.strip() != "main":
        return Outcome("not-main", f"{checkout} is on {branch.stdout.strip() or 'a detached HEAD'}, not main")
    git_dir = Path(_git(checkout, "rev-parse", "--absolute-git-dir").stdout.strip() or checkout / ".git")
    busy = [name for name in IN_PROGRESS if (git_dir / name).exists()]
    if busy:
        return Outcome("in-progress", f"{checkout} has a git operation in progress ({busy[0]})")
    fetched = _git(checkout, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main",
                   timeout=FETCH_TIMEOUT_SECONDS)
    if fetched.returncode:
        try:
            warm = subprocess.run(["github-warm-status"], capture_output=True, check=False, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            warm = False
        advice = ("GitHub credentials are warm. GitHub refused the fetch. This is likely a GitHub incident; "
                  + "the job retries" if warm else f"run github-warmup at a terminal on {socket.gethostname()}")
        return Outcome("fetch-failed", f"git fetch origin main failed in {checkout}: {_first_line(fetched)}; {advice}")
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    target = _git(checkout, "rev-parse", "origin/main").stdout.strip()
    if head == target:
        return Outcome(CURRENT, head[:8])
    if _git(checkout, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode:
        return Outcome("diverged", f"{checkout} main holds commits not on origin/main, so it cannot fast-forward")
    status = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    interrupted = False
    blocking_details: list[str] = []
    if status.returncode:
        blocking_details.append(_first_line(status))
    elif status.stdout.strip() or _has_untracked_update(checkout, blocking_details):
        interrupted = _finish_interrupted_update(checkout, blocking_details)
    if blocking_details:
        return Outcome("dirty", f"{checkout} has local changes that block the update ({', '.join(blocking_details)})")
    merged = _git(checkout, "merge", "--ff-only", "-q", "origin/main")
    if merged.returncode:
        return Outcome("ff-failed", f"git merge --ff-only origin/main failed in {checkout}: {_first_line(merged)}")
    detail = f"{head[:8]}..{target[:8]}"
    if interrupted:
        detail += "; finished an interrupted update"
    return Outcome(ADVANCED, detail)


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV, DEFAULT_STATE_DIR)))


def record(outcome: Outcome, checkout: Path, now: float) -> None:
    """Append the run to the log."""
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / SYNC_LOG).open("a", encoding="utf-8") as log:
        _ = log.write(json.dumps({"at": now, "checkout": str(checkout), "state": outcome.state,
                                  "detail": outcome.detail}) + "\n")


def notify(name: str, outcome: Outcome) -> None:
    minutes = conf.read("send").number("main_sync_repeat_minutes")
    host = socket.gethostname()
    text = (f"main sync on {host}: {outcome.detail}. Commands there stay old until this is "
            + "cleared; the job retries every 15 minutes.")
    _ = subprocess.run(session_send.send_argv("--to", name, "--from", "main-sync", "--text", text,
                        "--key", f"main-sync-{host}-{outcome.state}", "--repeat-minutes", f"{minutes:g}"),
                       check=False, capture_output=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="main_sync.py", description="Fast-forward a main checkout to origin/main.")
    _ = parser.add_argument("--checkout", default="~/.claude")
    _ = parser.add_argument("--notify", default="")
    arguments = parser.parse_args(argv)
    checkout = Path(os.path.expanduser(cast(str, arguments.checkout)))
    outcome = sync(checkout)
    print(f"main sync: {outcome.state}: {outcome.detail}")
    notify_name = cast(str, arguments.notify)
    record(outcome, checkout, time.time())
    if outcome.blocked and notify_name:
        notify(notify_name, outcome)
    return 1 if outcome.blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
