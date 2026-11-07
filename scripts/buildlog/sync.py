"""Exchange host-owned build records with each configured SSH peer.

Natedev initiates every exchange. Each peer keeps its own success record.
Rsync replaces complete files and excludes local admission and pending state.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import math
import shlex
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import NotRequired, TypedDict, cast
from urllib.parse import quote

from scripts.buildlog import health
from scripts.buildlog import record
from scripts.buildlog import store

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.lib.atomic_file import write_text_atomic
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_targets"))
from scripts.build_targets.target_config import SshTarget, read_targets
from scripts.lib import conf

REMOTE_ROOT = ".local/state/buildlog/"
LOCK_NAME = "sync.lock"


class SyncRecord(TypedDict):
    at: str
    peer: str
    ok: bool
    last_ok: str | None
    last_failure: NotRequired[str]


@dataclass(frozen=True)
class SyncFailure:
    at: str


@dataclass(frozen=True)
class NoSyncFailure:
    pass


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
    last_failure: SyncFailure | NoSyncFailure


@dataclass(frozen=True)
class SyncPaused:
    since: str
    why: str


@dataclass(frozen=True)
class SyncRunning:
    pass


def peer_name() -> str:
    return os.environ.get("BUILDLOG_PEER") or "mac"


@dataclass(frozen=True)
class SyncPeer:
    target: str
    host: str
    legacy_override: bool = False


@dataclass(frozen=True)
class PeerExchange:
    peer: SyncPeer
    status: SyncStatus | NeverSynced


def peer_exchanges() -> tuple[PeerExchange, ...]:
    """Read each active peer without performing an exchange."""
    exchanges: list[PeerExchange] = []
    for peer in configured_peers():
        exchanges.append(PeerExchange(peer, read_target_status(peer.target)))
    return tuple(exchanges)


def configured_peers() -> tuple[SyncPeer, ...]:
    override = os.environ.get("BUILDLOG_PEER", "")
    if override:
        return (SyncPeer("mac", override, legacy_override=True),)
    if store.host_name() != "natedev":
        return ()
    peers: list[SyncPeer] = []
    for target in read_targets():
        if isinstance(target, SshTarget):
            host = conf.read(target.settings).values().get("host", "")
            if not host or host.startswith("-") or any(character.isspace() for character in host):
                raise ValueError(f"invalid sync host for {target.name}")
            peers.append(SyncPeer(target.name, host))
    return tuple(peers)


def status_path(peer: str = "") -> Path:
    if not peer:
        return store.root() / store.SYNC_STATUS_NAME
    return store.root() / "sync-peers" / f"{quote(peer, safe='').replace('.', '%2E')}.json"


def read_status(peer: str = "") -> SyncStatus | NeverSynced:
    path = status_path(peer)
    if peer and not path.exists():
        matching = next((item for item in configured_peers() if item.target == peer), None)
        if matching is not None:
            path = status_path(matching.host)
    try:
        record = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return NeverSynced()
    if not isinstance(record, dict):
        return NeverSynced()
    fields = cast(dict[str, object], record)
    at, peer_host, ok, last_ok = (fields.get(name) for name in ("at", "peer", "ok", "last_ok"))
    if not isinstance(at, str) or not isinstance(peer_host, str) or not isinstance(ok, bool):
        return NeverSynced()
    failure: SyncFailure | NoSyncFailure = NoSyncFailure()
    if isinstance(fields.get('last_failure'), str):
        failure = SyncFailure(str(fields['last_failure']))
    elif not ok:
        failure = SyncFailure(at)
    return SyncStatus(
        at, peer_host, ok,
        LastSynced(last_ok) if isinstance(last_ok, str) else NeverSynced(),
        failure,
    )



def read_target_status(target: str) -> SyncStatus | NeverSynced:
    status = read_status(target)
    if isinstance(status, NeverSynced) and target in record.mac_targets():
        return read_status()
    return status

def write_status(peer: str, ok: bool, target: str = "mac") -> None:
    now = store.utc_iso(time.time())
    previous = read_target_status(target)
    status: SyncRecord = {
        "at": now,
        "peer": peer,
        "ok": ok,
        "last_ok": now if ok else (previous.last_good.at if isinstance(previous, SyncStatus) and isinstance(previous.last_good, LastSynced) else None),
    }
    if not ok:
        status['last_failure'] = now
    elif isinstance(previous, SyncStatus) and isinstance(previous.last_failure, SyncFailure):
        status['last_failure'] = previous.last_failure.at
    write_text_atomic(status_path(peer), json.dumps(status) + "\n")
    if target in record.mac_targets():
        write_text_atomic(status_path(), json.dumps(status) + "\n")


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
def sync_lock(wait_for_running_sync: bool = False, *, blocking: bool = True) -> Generator[None]:
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
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def sync_time(last: str, now: datetime) -> str:
    """The time in the user's zone with its label, and the date too when it is not today."""
    at = datetime.fromisoformat(last).astimezone(user_zone())
    return at.strftime("%H:%M %Z" if at.date() == now.astimezone(user_zone()).date() else "%Y-%m-%d %H:%M %Z")


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


def sync_target(target: str) -> None:
    """Exchange one target after a step without waiting for another sync."""
    try:
        with sync_lock(blocking=False):
            if isinstance(read_pause(), SyncPaused):
                return
            peer = next((peer for peer in configured_peers() if peer.target == target), None)
            if peer is None or not record.target_hosts(target):
                return
            _ = health.write_snapshot()
            settings = record.sync_config()
            _ = sync_peer(peer, store.root(), store.host_name(), ssh_options(),
                          configured_timeout(settings, "sync.reach_timeout_s"),
                          configured_timeout(settings, "sync.rsync_timeout_s"))
    except BlockingIOError:
        print(f"buildlog sync: {target}: another sync holds the lock; hourly sync can retry")
    except Exception as error:  # The recorder must preserve the step result.
        detail = " ".join(str(error).split())
        print(f"buildlog sync: {target}: exchange failed ({detail}); hourly sync can retry")


def _sync_locked() -> int:
    paused = read_pause()
    if isinstance(paused, SyncPaused):
        since = sync_time(paused.since, datetime.now()) if paused.since != "unknown" else paused.since
        print(f"buildlog sync: paused since {since} ({paused.why}); buildlog sync resume ends it")
        return 0
    root = store.root()
    host = store.host_name()
    peers = configured_peers()
    root.mkdir(parents=True, exist_ok=True)
    _ = health.write_snapshot()
    settings = record.sync_config()
    reach_timeout = configured_timeout(settings, "sync.reach_timeout_s")
    rsync_timeout = configured_timeout(settings, "sync.rsync_timeout_s")
    options = ssh_options()
    status = 0
    for peer in peers:
        status = max(status, sync_peer(peer, root, host, options, reach_timeout, rsync_timeout))
    return status


def configured_timeout(settings: dict[str, str], key: str) -> float:
    value = float(settings.get(key, ""))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key}: expected a positive timeout")
    return value


def ssh_options() -> list[str]:
    timeout = configured_timeout(record.sync_config(), "sync.connect_timeout_s")
    return ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout:g}"]


def sync_peer(peer: SyncPeer, root: Path, host: str, options: list[str],
              reach_timeout: float, rsync_timeout: float) -> int:
    remote_refresh = ("mkdir -p " + REMOTE_ROOT + '; echo "rc=$?"; '
                      + "~/.claude/scripts/lib/py ~/.claude/scripts/buildlog/health.py")
    reach = run(["ssh", *options, peer.host, remote_refresh], reach_timeout)
    if reach is None or reach.returncode == 255:
        reason = "timed out" if reach is None else (reach.stderr.strip().splitlines() or ["no output"])[-1]
        print(f"buildlog sync: {peer.host} did not answer ({reason}); a later sync can retry")
        write_status(peer.host, False, peer.target)
        return 0
    codes = [match.group(1) for line in reach.stdout.splitlines() if (match := re.fullmatch(r"rc=(\d+)", line.strip()))]
    if not codes or codes[-1] != "0":
        detail = f"reported rc={codes[-1]}" if codes else "no rc= line in command output"
        print(f"buildlog sync: mkdir on {peer.host} failed: {detail}")
        write_status(peer.host, False, peer.target)
        return 1
    shell = shlex.join(["ssh", *options])
    exclusions = ["--exclude=admission/", "--exclude=pending-steps/", "--exclude=pending-launches/"]
    push_folders = (host, store.CI_DIR) if host == "natedev" else (host,)
    sources = [str(root / name) for name in push_folders if (root / name).is_dir()]
    if sources and failed(
        f"push to {peer.host}",
        run(["rsync", "-rt", "-e", shell, *exclusions, *sources, f"{peer.host}:{REMOTE_ROOT}"], rsync_timeout),
    ):
        write_status(peer.host, False, peer.target)
        return 1
    # Host-owned append files keep their paths across peers. Never replace local files.
    owned_hosts = record.synchronized_hosts() if peer.legacy_override else record.target_hosts(peer.target)
    pull = [
        "rsync", "-rt", "-e", shell, *exclusions,
        *(f"--include=/{name}/***" for name in owned_hosts if name != host),
        "--exclude=*", f"{peer.host}:{REMOTE_ROOT}", f"{root}/",
    ]
    if failed(f"pull from {peer.host}", run(pull, rsync_timeout)):
        write_status(peer.host, False, peer.target)
        return 1
    write_status(peer.host, True, peer.target)
    print(f"buildlog sync: exchanged with {peer.host}")
    return 0
