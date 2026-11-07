#!/usr/bin/env python3
"""Fast-forward a clean main checkout to origin/main without deleting local work.

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
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
class Outcome:
    state: str
    detail: str

    @property
    def blocked(self) -> bool:
        return self.state not in (CURRENT, ADVANCED)


def _git(checkout: Path, *arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True, text=True, check=False,
                              timeout=timeout, env={**os.environ, **GIT_ENVIRONMENT})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git"], 124, "", f"git {arguments[0]} took over {timeout:.0f} s")


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    return failure_detail(result, lines=1).replace("\n", "; ")


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
    if status.returncode or status.stdout.strip():
        changed = ", ".join(line[3:] for line in status.stdout.splitlines()[:5])
        return Outcome("dirty", f"{checkout} has changed tracked files ({changed or _first_line(status)})")
    merged = _git(checkout, "merge", "--ff-only", "-q", "origin/main")
    if merged.returncode:
        return Outcome("ff-failed", f"git merge --ff-only origin/main failed in {checkout}: {_first_line(merged)}")
    return Outcome(ADVANCED, f"{head[:8]}..{target[:8]}")


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
