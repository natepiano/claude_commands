"""Exchange the build log with the other machine; natedev's hourly job runs it.

Each machine appends only under its own <root>/<host>/, so the two never write
the same file. This pushes this host's folder and ci/ (only natedev polls CI)
to the peer, then pulls every other host's folder back. rsync never deletes,
and replaces a changed file by rename, which index.py expects.

The peer is `mac`; BUILDLOG_PEER overrides it. A peer that does not answer --
the Mac asleep with its lid closed times out on port 22 -- is not a failure:
one line, exit 0, and the next hour catches up. <root>/sync.json records the
last attempt and the last success.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import TypedDict, cast

import store

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
REMOTE_ROOT = ".local/state/buildlog/"
REACH_TIMEOUT_S = 30
RSYNC_TIMEOUT_S = 600


class SyncStatus(TypedDict):
    at: str
    peer: str
    ok: bool
    last_ok: str | None


def peer_name() -> str:
    return os.environ.get("BUILDLOG_PEER") or "mac"


def read_status() -> SyncStatus | None:
    try:
        return cast(SyncStatus, json.loads((store.root() / store.SYNC_STATUS_NAME).read_text()))
    except (OSError, ValueError):
        return None


def write_status(peer: str, ok: bool) -> None:
    now = store.utc_iso(time.time())
    previous = read_status()
    status: SyncStatus = {
        "at": now,
        "peer": peer,
        "ok": ok,
        "last_ok": now if ok else (previous["last_ok"] if previous else None),
    }
    path = store.root() / store.SYNC_STATUS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(status) + "\n")
    _ = partial.replace(path)


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def failed(step: str, result: subprocess.CompletedProcess[str] | None) -> bool:
    if result is not None and result.returncode == 0:
        return False
    detail = "timed out" if result is None else (result.stderr.strip().splitlines() or ["no output"])[-1]
    print(f"buildlog sync: {step} failed: {detail}")
    return True


def sync() -> int:
    peer = peer_name()
    root = store.root()
    host = store.host_name()
    root.mkdir(parents=True, exist_ok=True)
    reach = run(["ssh", *SSH_OPTIONS, peer, "mkdir -p " + REMOTE_ROOT], REACH_TIMEOUT_S)
    if reach is None or reach.returncode == 255:
        # ssh's own 255: no connection. One mkdir stands in for a separate
        # `ssh true` reachability probe and saves a connection each hour.
        reason = "timed out" if reach is None else (reach.stderr.strip().splitlines() or ["no output"])[-1]
        print(f"buildlog sync: {peer} did not answer ({reason}); the next hour catches up")
        write_status(peer, False)
        return 0
    if failed(f"mkdir on {peer}", reach):
        write_status(peer, False)
        return 1
    shell = "ssh " + " ".join(SSH_OPTIONS)
    sources = [str(root / name) for name in (host, store.CI_DIR) if (root / name).is_dir()]
    if sources and failed(
        "push", run(["rsync", "-rt", "-e", shell, *sources, f"{peer}:{REMOTE_ROOT}"], RSYNC_TIMEOUT_S)
    ):
        write_status(peer, False)
        return 1
    # Top-level folders only, minus this host's and ci/: the peer's own
    # folders. Its index, status and error files stay where they are.
    pull = [
        "rsync",
        "-rt",
        "-e",
        shell,
        f"--exclude=/{host}/",
        f"--exclude=/{store.CI_DIR}/",
        "--include=/*/",
        "--exclude=/*",
        f"{peer}:{REMOTE_ROOT}",
        f"{root}/",
    ]
    if failed("pull", run(pull, RSYNC_TIMEOUT_S)):
        write_status(peer, False)
        return 1
    write_status(peer, True)
    print(f"buildlog sync: exchanged with {peer}")
    return 0
