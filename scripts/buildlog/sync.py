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

import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict, cast

import store

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
REMOTE_ROOT = ".local/state/buildlog/"
REACH_TIMEOUT_S = 30
RSYNC_TIMEOUT_S = 600
LOCK_NAME = "sync.lock"


class SyncRecord(TypedDict):
    at: str
    peer: str
    ok: bool
    last_ok: str | None


@dataclass(frozen=True)
class LastSynced:
    at: str


@dataclass(frozen=True)
class NeverSynced:
    pass


@dataclass(frozen=True)
class SyncStatus:
    at: str
    peer: str
    ok: bool
    last_good: LastSynced | NeverSynced


@dataclass(frozen=True)
class SyncPaused:
    since: str
    why: str


@dataclass(frozen=True)
class SyncRunning:
    pass


def peer_name() -> str:
    return os.environ.get("BUILDLOG_PEER") or "mac"


def read_status() -> SyncStatus | NeverSynced:
    try:
        record = cast(object, json.loads((store.root() / store.SYNC_STATUS_NAME).read_text()))
    except (OSError, ValueError):
        return NeverSynced()
    if not isinstance(record, dict):
        return NeverSynced()
    fields = cast(dict[str, object], record)
    at, peer, ok, last_ok = (fields.get(name) for name in ("at", "peer", "ok", "last_ok"))
    if not isinstance(at, str) or not isinstance(peer, str) or not isinstance(ok, bool):
        return NeverSynced()
    return SyncStatus(
        at, peer, ok,
        LastSynced(last_ok) if isinstance(last_ok, str) else NeverSynced(),
    )


def write_status(peer: str, ok: bool) -> None:
    now = store.utc_iso(time.time())
    previous = read_status()
    status: SyncRecord = {
        "at": now,
        "peer": peer,
        "ok": ok,
        "last_ok": now if ok else (previous.last_good.at if isinstance(previous, SyncStatus) and isinstance(previous.last_good, LastSynced) else None),
    }
    path = store.root() / store.SYNC_STATUS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(status) + "\n")
    _ = partial.replace(path)


def read_pause() -> SyncPaused | SyncRunning:
    path = store.root() / store.SYNC_PAUSED_NAME
    try:
        record = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return SyncRunning()
    except (OSError, ValueError):
        return SyncPaused("unknown", "pause record unavailable")
    if not isinstance(record, dict):
        return SyncPaused("unknown", "pause record unavailable")
    fields = cast(dict[str, object], record)
    since, why = fields.get("since"), fields.get("why")
    if not isinstance(since, str) or not isinstance(why, str):
        return SyncPaused("unknown", "pause record unavailable")
    try:
        at = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return SyncPaused("unknown", why)
    if at.tzinfo is None:
        return SyncPaused("unknown", why)
    return SyncPaused(since, why)


@contextmanager
def sync_lock(wait_for_running_sync: bool = False) -> Generator[None]:
    path = store.root() / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if wait_for_running_sync:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("buildlog sync: waiting for the running sync to finish")
                fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sync_time(last: str, now: datetime) -> str:
    """Local time with its zone, and the date too when it is not today."""
    at = datetime.fromisoformat(last).astimezone()
    return at.strftime("%H:%M %Z" if at.date() == now.astimezone().date() else "%Y-%m-%d %H:%M %Z")


def pause(why: str) -> int:
    with sync_lock(wait_for_running_sync=True):
        path = store.root() / store.SYNC_PAUSED_NAME
        partial = path.with_name(path.name + ".tmp")
        _ = partial.write_text(json.dumps({"since": store.utc_iso(time.time()), "why": why}) + "\n")
        _ = partial.replace(path)
    print(f"buildlog sync: paused ({why})")
    return 0


def resume() -> int:
    (store.root() / store.SYNC_PAUSED_NAME).unlink(missing_ok=True)
    print("buildlog sync: resumed")
    return 0


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
    with sync_lock():
        return _sync_locked()


def _sync_locked() -> int:
    paused = read_pause()
    if isinstance(paused, SyncPaused):
        since = sync_time(paused.since, datetime.now()) if paused.since != "unknown" else paused.since
        print(f"buildlog sync: paused since {since} ({paused.why}); buildlog sync resume ends it")
        return 0
    peer = peer_name()
    root = store.root()
    host = store.host_name()
    root.mkdir(parents=True, exist_ok=True)
    reach = run(["ssh", *SSH_OPTIONS, peer, "mkdir -p " + REMOTE_ROOT + '; echo "rc=$?"'], REACH_TIMEOUT_S)
    if reach is None or reach.returncode == 255:
        # ssh's own 255: no connection. One mkdir stands in for a separate
        # `ssh true` reachability probe and saves a connection each hour.
        reason = "timed out" if reach is None else (reach.stderr.strip().splitlines() or ["no output"])[-1]
        print(f"buildlog sync: {peer} did not answer ({reason}); the next hour catches up")
        write_status(peer, False)
        return 0
    codes = [match.group(1) for line in reach.stdout.splitlines() if (match := re.fullmatch(r"rc=(\d+)", line.strip()))]
    if not codes or codes[-1] != "0":
        detail = f"reported rc={codes[-1]}" if codes else "no rc= line in command output"
        print(f"buildlog sync: mkdir on {peer} failed: {detail}")
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
