#!/usr/bin/env python3
"""Merge a checkpoint, run the complete gate, then push (and optionally promote).

The default sampled memory cap is 6144 MiB. Full-tree basedpyright peaked at
4065 MiB on 2026-10-30; its reservation is 4878 MiB. A sample is resident plus swapped memory.
Each job reserves its last recorded peak times 1.2, using records from before this run.
An unrecorded module reserves the largest recorded module peak times 1.2, or 640 MiB
without module records. An unrecorded types job reserves 3072 MiB. Admission counts
the latest aggregate sample, remaining running reservations and the next job's reservation.
Samples are at least 0.25 seconds apart. One job is always allowed to progress;
an observed overshoot makes the run red. This is admission on samples, not an
OS allocation limit; growth between samples can still exceed the cap. A job whose
own sampled memory goes over the cap is killed at that sample and fails the run, and so is
a job still running at the module time limit (default 1800 s).
After the parallel pass, up to eight failed test modules run once more, alone.
Runner-killed jobs and the type check do not run again. Each test module uses a
builds.slice scope when its probe succeeds, unless disabled or already in that slice.
--tests-only runs the same suite runner without changing git refs or files.
Clean trees reuse full passes on the same host, or select affected modules from a
certified ancestor. --full forces all modules; --nightly validates a shared clone.
Type jobs still run as before.
A unit checkpoint commit without its Change-Review line is refused before the merge. After the
push, each visible change becomes a row in the change-review list that --change-review names, or
the production's top-level showrunner keeps (`change_review.list_path`), with the doc stamped into
its frontmatter; the first row creates a missing list, and the list commits itself in its vault.
The production is --production, else the calling showrunner's (its update timer); a gate that
finds neither is refused before the merge, so no merge lands without its rows.
"""
from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time
from typing import Literal, TextIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import process_group

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
from scripts.notify.pushover import API_HOST
from scripts.copies import copies
from scripts.lib import build_folders
from scripts.production import change_review, staff  # noqa: E402
from scripts.production.gate_modules import GateRefused, discover_test_modules, test_module_name
from scripts.production.affected_modules import (  # noqa: E402
    AffectedSelection, CertifiedBase, FullSuite, NoCertifiedBase, select_affected,
)
from scripts.direct.local_release_wait import local_release_waiter  # noqa: E402
from scripts.build_targets import build_targets  # noqa: E402
from scripts.build_hold import build_hold  # noqa: E402
from scripts.buildlog import store  # noqa: E402
from scripts.buildlog.store import host_name  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from scripts.lib import scratch  # noqa: E402
from scripts.test_times import gate_record
from scripts.lint import memory_admit  # noqa: E402
from scripts.build_queue.admission_client import AdmissionHoldCheck  # noqa: E402
from scripts.build_queue.queue_place import (  # noqa: E402
    QueuePlace, QueuePlaceRefused, defer_cleanup_signals, queue_place, suite_scope_prefix,
)

# Full-tree basedpyright peaked at 4065 MiB on 2026-10-30: its 4878 MiB reservation
# leaves room for modules beside it under this cap.
DEFAULT_MEMORY_CAP_MIB = 6144
DEFAULT_MAX_RUNNING_JOBS = max(1, (os.cpu_count() or 2) // 2)
# The largest measured module was 0.52 GiB; this includes the 1.2 growth margin.
UNRECORDED_MODULE_RESERVATION_MIB = 640
# A module still running this long is killed and the run is red: a hung module holds every gate
# behind it. The slowest measured module took 387 s at load 152 (direct/test_verify_mac_offload.py,
# 2026-10-18); the limit is about 4.6 times that.
DEFAULT_MODULE_TIME_LIMIT_S = 1800
RERUN_LIMIT = 8
RELEASE_LOCK_WAIT_S = 60
# basedpyright resolves bpy (Blender's interpreter only) and PIL (a PEP 723 dependency) from this
# type-check-only venv, so the full tree reports 0/0. The stub date matches the 2026-08-23 sweep;
# later dates report new diagnostics in bake_textures.py.
TYPECHECK_REQUIREMENTS = ("pillow==13.0.0", "fake-bpy-module==20260822")
# An unrecorded type job keeps its own fallback; recorded jobs all use one margin.
TYPES_MEMORY_MIB = 3072
MEMORY_PEAK_MARGIN = 1.2
# A module this many times slower than the median of its last MODULE_HISTORY_RUNS times, and at least
# the floor slower, is named in the report. On 2026-10-19 agents/test_codex_mesh.py went from 49 s to
# 1264 s (four tests each waited a 300 s call timeout) and no line said so. Load alone moved modules
# about 2x. The median names a regression on about three runs in a row, then takes it as the normal.
SLOW_MODULE_FACTOR = 3.0
SLOW_MODULE_FLOOR_S = 60.0
MODULE_HISTORY_RUNS = 5
MEMORY_SAMPLE_SECONDS = 0.25
ROLLBACK_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# Read the live roots before a module receives its private environment.
LIVE_ROOTS = (str(Path.home().resolve()),
              str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state").resolve()),
              str(Path("/tmp/claude").resolve()))
LIVE_PUSH_KEYS = tuple(str(path.resolve()) for path in (
    Path(LIVE_ROOTS[0]) / ".config/pushover/env",
    Path(os.environ.get("XDG_CONFIG_HOME") or Path(LIVE_ROOTS[0]) / ".config") / "pushover/env"))
# Python children load this hook from the module's private user site.
SUITE_GUARD = r'''import os, sys
from urllib.parse import urlsplit

def refuse(detail):
    with open(marker, "a") as output:
        output.write(detail + "\n")
    raise PermissionError(detail)

def refuse_external_effect(event, args):
    if event == "urllib.Request":
        host = urlsplit(args[0]).hostname
    elif event == "socket.getaddrinfo":
        host = args[0]
    elif event == "http.client.connect":
        host = args[1]
    elif event == "socket.connect":
        host = args[1][0] if isinstance(args[1], tuple) else ""
    else:
        host = ""
    if isinstance(host, str) and host.lower().rstrip(".") == push_host:
        refuse("test refused a Pushover push: " + host)
    writing = True
    if event == "open":
        writing = args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        paths = ((args[0], -1),)
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime"):
        paths = ((args[0], args[-1]),)
    elif event in ("os.rename", "os.link"):
        paths = ((args[0], args[2]), (args[1], args[3]))
    elif event == "os.symlink":
        paths = ((args[1], args[2]),)
    else:
        return
    for path, directory in paths:
        if isinstance(path, int):
            path = os.readlink("/proc/self/fd/" + str(path))
        if not isinstance(path, (str, bytes)):
            continue
        path = os.fsdecode(path)
        if not os.path.isabs(path) and isinstance(directory, int) and directory >= 0:
            path = os.path.join(os.readlink("/proc/self/fd/" + str(directory)), path)
        path = os.path.realpath(path)
        if path in push_keys:
            refuse("test refused Pushover credential access: " + path)
        if writing and any(path == root or path.startswith(root + os.sep) for root in live_roots):
            refuse("test refused a write to a live root: " + path)

sys.addaudithook(refuse_external_effect)
'''
# Restore signals after the runner registers the child for cleanup.
SUITE_BOOTSTRAP = """import os, signal, sys
for disposition in signal.valid_signals():
    if disposition not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(disposition, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
os.execvp(sys.argv[1], sys.argv[1:])
"""


@dataclass
class GateArguments(argparse.Namespace):
    """Typed command-line boundary; merge fields are validated before use."""
    repo: str = ""
    merge_branch: str = ""
    unit_branch: str = ""
    checkpoint: str = ""
    previous_checkpoint: str = ""
    change_review: str = ""
    production: str = ""
    unit: str = ""
    phase: str = ""
    body: str = ""
    type_dirs: list[str] = field(default_factory=list)
    promote: bool = False
    main_repo: str = ""
    ssh: str = ""
    mac_host: str = ""
    mac_repo: str = ""
    memory_cap_mib: int = DEFAULT_MEMORY_CAP_MIB
    module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S
    tests_only: bool = False
    promote_only: bool = False
    full: bool = False
    nightly: bool = False


class GateJobKind(Enum):
    TEST_MODULE = auto()
    TYPE_CHECK = auto()


@dataclass(frozen=True)
class GateJobRequest:
    """A job to start, with its original path retained for holds and retries."""
    suite: str
    path: Path
    module: str
    kind: GateJobKind = GateJobKind.TEST_MODULE


@dataclass(frozen=True)
class GateJobResult:
    suite: str
    module: str
    returncode: int
    seconds: float
    output: str
    killed_for: str = ""
    tripwire_tripped: bool = False
    kind: GateJobKind = GateJobKind.TEST_MODULE
    cpu_seconds: float = 0.0
    times: gate_record.ModuleTimes | gate_record.NoModuleTimes | gate_record.TimingNotApplicable = gate_record.NoModuleTimes("job has no timing data")
    peak_mib: float = 0.0
    restart: Literal["", "hold"] = ""


@dataclass(frozen=True)
class GateRun:
    jobs: tuple[GateJobResult, ...]
    seconds: float
    peak_memory_mib: float
    memory_within_cap: bool = True
    tests_reused: bool = False
    # A valid affected selection may have no test modules at all.
    selection_certified: bool = False

    @property
    def passed(self) -> bool:
        return (self.memory_within_cap and (bool(self.jobs) or self.tests_reused or self.selection_certified)
                and all(job.returncode == 0 for job in self.jobs))


@dataclass
class RunningGateJob:
    suite: str
    path: Path
    process: subprocess.Popen[str]
    started: float
    output: TextIO
    log: Path
    module: str
    # Set when the runner kills the job for its memory or time limit.
    killed_for: str = ""
    cpu_slot: SharedCPUSlot | NoSharedCPU = field(default_factory=lambda: NoSharedCPU())
    peak_memory_mib: float = 0.0
    kind: GateJobKind = GateJobKind.TEST_MODULE
    cpu_seconds: float = 0.0


@dataclass(frozen=True)
class RecordedJobPeaks:
    """Peaks from before this run, used to size every job's reservation."""
    modules_mib: dict[str, float]
    types_mib: dict[str, float]
    types_key: str

    def reservation_mib(self, job: GateJobRequest) -> float:
        if job.kind is GateJobKind.TYPE_CHECK:
            peaks, key, fallback_mib = self.types_mib, self.types_key, float(TYPES_MEMORY_MIB)
        else:
            peaks, key = self.modules_mib, f"{job.suite}/{job.module}"
            fallback_mib = max(peaks.values()) * MEMORY_PEAK_MARGIN if peaks else float(UNRECORDED_MODULE_RESERVATION_MIB)
        return peaks[key] * MEMORY_PEAK_MARGIN if key in peaks else fallback_mib


@dataclass
class SampledRSSAdmission:
    cap_mib: int
    latest_mib: float = 0.0
    job_memory_mib: dict[int, float] = field(default_factory=dict)
    reservations_mib: dict[int, float] = field(default_factory=dict)
    peak_mib: float = 0.0
    next_sample_at: float = 0.0

    def sample_due(self, now: float) -> bool:
        return now >= self.next_sample_at

    @property
    def reserved_mib(self) -> float:
        # A small startup sample does not release a running job's reservation.
        return sum(max(0.0, expected - self.job_memory_mib.get(pid, 0.0))
                   for pid, expected in self.reservations_mib.items())

    def observe(self, now: float, aggregate_mib: float, job_memory_mib: dict[int, float]) -> None:
        self.latest_mib = aggregate_mib
        self.peak_mib = max(self.peak_mib, aggregate_mib)
        self.job_memory_mib = job_memory_mib.copy()
        self.next_sample_at = now + MEMORY_SAMPLE_SECONDS

    def admits(self, expected_mib: float) -> bool:
        return not self.reservations_mib or self.latest_mib + self.reserved_mib + expected_mib <= self.cap_mib

    def reserve(self, pid: int, expected_mib: float) -> None:
        self.job_memory_mib[pid] = 0.0
        self.reservations_mib[pid] = expected_mib

    def release(self, pid: int) -> None:
        # Keep the last aggregate sample until the next scan; it may still
        # include this finished job, which conservatively delays admission.
        _ = self.job_memory_mib.pop(pid, 0.0)
        _ = self.reservations_mib.pop(pid, 0.0)


@dataclass(frozen=True)
class NoSharedCPU:
    pass


@dataclass(frozen=True)
class WaitingForCPU:
    pass


@dataclass
class SharedCPUSlot:
    descriptor: int
    token: bytes
    returned: bool = False

    def release(self) -> None:
        if not self.returned:
            with defer_cleanup_signals():
                if os.write(self.descriptor, self.token) != 1:
                    raise GateRefused("could not return the shared CPU slot")
                self.returned = True


@dataclass
class SharedCPUPool:
    descriptor: int
    owned: list[SharedCPUSlot] = field(default_factory=list)

    def try_acquire(self) -> SharedCPUSlot | WaitingForCPU:
        with defer_cleanup_signals():
            # CUSE devices need not honor O_NONBLOCK in the same way as pipes.
            if not select.select([self.descriptor], [], [], 0)[0]:
                return WaitingForCPU()
            try:
                token = os.read(self.descriptor, 1)
            except BlockingIOError:
                return WaitingForCPU()
            if not token:
                raise GateRefused("shared CPU pool returned no byte")
            slot = SharedCPUSlot(self.descriptor, token)
            self.owned.append(slot)
            return slot


@contextmanager
def shared_cpu_pool() -> Generator[SharedCPUPool | NoSharedCPU]:
    path = Path(os.environ.get("BUILDLOG_JOBSERVER_FIFO", "/dev/steve"))
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        yield NoSharedCPU()
        return
    except OSError as error:
        raise GateRefused(f"shared CPU pool {path}: {error}") from error
    pool = SharedCPUPool(descriptor)
    try:
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISCHR(mode)):
            raise GateRefused(f"shared CPU pool is not a FIFO or character device: {path}")
        yield pool
    finally:
        with defer_cleanup_signals():
            try:
                for slot in pool.owned:
                    slot.release()
            finally:
                os.close(descriptor)


@contextmanager
def gate_reservation(repo: Path, cap_mib: int, temporary: Path, hold: BuildHoldWait) -> Generator[QueuePlace]:
    if not temporary.is_dir():
        raise GateRefused(f"gate temporary directory is missing: {temporary}")
    cap = cap_mib * (1 << 20)
    try:
        with queue_place("merge-gate", repo,
                         sizing=memory_admit.FixedCapSizing(cap),
                         before_check=AdmissionHoldCheck(hold.wait_until_free, hold.admits)) as place:
            yield place
    except QueuePlaceRefused as error:
        raise GateRefused(str(error)) from error


@dataclass
class BuildHoldWait:
    """Track admission and time spent waiting for another session's natedev hold."""
    seconds: float = field(default=0.0, init=False)
    waiting: bool = field(default=False, init=False)
    started: float = field(default=0.0, init=False)

    def admits(self) -> bool:
        try:
            directory = build_hold.holder_directory()
            holds = build_hold.natedev_holds(directory)
            held = bool(holds.in_force) and not build_targets.caller_holds_only_own()
        except (OSError, ValueError) as error:
            raise GateRefused(f"natedev hold state cannot be read: {error}") from error
        if held and not self.waiting:
            self.started = time.monotonic()
            self.waiting = True
            names = "; ".join(" ".join(holder.name.split()) for holder in holds.in_force)
            print(f"Gate hold: natedev is held by {names}; waiting for release", flush=True)
        elif not held and self.waiting:
            waited = time.monotonic() - self.started
            self.seconds += waited
            self.waiting = False
            print(f"Gate hold: released after {waited:.2f}s", flush=True)
        return not held

    def waited_s(self) -> float:
        """Include the current wait before it ends."""
        return self.seconds + (time.monotonic() - self.started if self.waiting else 0.0)

    def wait_until_free(self) -> None:
        if self.admits():
            return
        with local_release_waiter(build_hold.holder_directory(), build_hold.release_directory()) as events:
            while True:
                events.arm()
                if self.admits():
                    return
                events.wait(15.0)


def interrupted(signum: int, _frame: object) -> None:
    raise GateRefused(f"interrupted by {signal.Signals(signum).name}")


@contextmanager
def rollback_signals(*, defer: bool = False) -> Generator[None, None, None]:
    pending: list[int] = []

    def record(signum: int, _frame: object) -> None:
        pending.append(signum)

    signals = (*ROLLBACK_SIGNALS, signal.SIGINT) if defer else ROLLBACK_SIGNALS
    previous = {sig: signal.signal(sig, record if defer else interrupted) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            _ = signal.signal(sig, handler)
    if pending:
        signal.raise_signal(pending[0])


def fetch_branches(repo: Path, *branches: str) -> None:
    _ = checked_git(repo, "fetch", "origin", *(f"+refs/heads/{branch}:refs/remotes/origin/{branch}" for branch in dict.fromkeys(branches)))


def command(arguments: Sequence[str], repo: Path) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(arguments, cwd=repo, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            output, errors = process.communicate()
        except BaseException:
            try:
                process_group.send_process_group(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.wait()
            raise
        return subprocess.CompletedProcess(arguments, process.returncode, output, errors)


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return command(["git", *arguments], repo)


def checked_git(repo: Path, *arguments: str) -> str:
    result = git(repo, *arguments)
    if result.returncode:
        raise GateRefused(f"git {' '.join(arguments)}: {failure_detail(result)}")
    return result.stdout.strip()


class ReleaseLockState(Enum):
    ABSENT = auto()
    FREE = auto()
    HELD = auto()


def release_lock_state() -> ReleaseLockState:
    try:
        with (build_hold.release_directory() / "release.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ReleaseLockState.HELD
            fcntl.flock(lock, fcntl.LOCK_UN)
            return ReleaseLockState.FREE
    except FileNotFoundError:
        return ReleaseLockState.ABSENT
    except OSError as error:
        raise GateRefused(f"build-hold release lock cannot be read: {error}") from error


def wait_for_release_lock(wait_s: float = RELEASE_LOCK_WAIT_S, poll_interval_s: float = 0.2) -> None:
    deadline = time.monotonic() + wait_s
    while release_lock_state() is ReleaseLockState.HELD:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateRefused(f"build-hold state stayed locked for {wait_s:g} s; main promotion refused")
        time.sleep(min(poll_interval_s, remaining))


def swapped_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def process_memory_mib(roots: set[int]) -> dict[int, float]:
    """Sum resident and swapped memory of our child roots and descendants, never other jobs.

    Swap counts: a job that leaks under memory pressure keeps its old pages swapped out, so its
    RSS alone stays low while it grows (2026-10-18: 10 GiB resident of 28 GiB held).
    """
    parents: dict[int, int] = {}
    resident: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(entry.name)
            parents[pid] = int(fields[1])
            resident[pid] = int(fields[21])
        except (OSError, ValueError, IndexError):
            continue
    totals: dict[int, float] = {}
    for root in roots:
        owned = {root}
        frontier = {root}
        while frontier:
            frontier = {pid for pid, parent in parents.items() if parent in frontier and pid not in owned}
            owned.update(frontier)
        pages = sum(resident.get(pid, 0) for pid in owned)
        totals[root] = (pages * os.sysconf("SC_PAGE_SIZE") / 1024 + sum(swapped_kib(pid) for pid in owned)) / 1024
    return totals


def slower_than_recorded(seconds: float, recorded: float) -> bool:
    return seconds >= recorded * SLOW_MODULE_FACTOR and seconds - recorded >= SLOW_MODULE_FLOOR_S


def gate_state_dir() -> Path:
    return Path(os.environ.get("MERGE_GATE_STATE_DIR", "~/.local/state/merge-gate")).expanduser()


def typecheck_venv_path() -> Path:
    return gate_state_dir() / "typecheck-venv"


def typecheck_python() -> Path | str:
    """The pinned type-check venv's interpreter, built with uv when absent; a failure is its reason."""
    venv = typecheck_venv_path()
    python = venv / "bin" / "python"
    stamp = venv / "gate-requirements.txt"
    wanted = "\n".join(TYPECHECK_REQUIREMENTS) + "\n"
    uv = os.environ.get("MERGE_GATE_UV", "uv")
    command = f"{uv} venv --clear {venv} && {uv} pip install --python {python} {' '.join(TYPECHECK_REQUIREMENTS)}"
    venv.parent.mkdir(parents=True, exist_ok=True)
    with (venv.parent / "typecheck-venv.lock").open("w") as lock:
        # Gates run at the same time; one builds and the others wait for its stamp.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if python.exists() and stamp.exists() and stamp.read_text() == wanted:
            return python
        stamp.unlink(missing_ok=True)
        for argv in ([uv, "venv", "--clear", str(venv)],
                     [uv, "pip", "install", "--python", str(python), *TYPECHECK_REQUIREMENTS]):
            try:
                built = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired) as error:
                return f"type-check venv missing: {error}; build it with: {command}"
            if built.returncode:
                return f"type-check venv missing: {failure_detail(built)}; build it with: {command}"
        _ = stamp.write_text(wanted)
        print(f"Type-check venv built: {venv}", flush=True)
        return python


def read_measures(path: Path) -> dict[str, float]:
    try:
        value = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: float(seconds) for key, seconds in cast(dict[object, object], value).items()
            if isinstance(key, str) and isinstance(seconds, (int, float)) and seconds >= 0}


def read_history(path: Path) -> dict[str, list[float]]:
    try:
        value = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: [float(seconds) for seconds in cast(list[object], runs)
                  if isinstance(seconds, (int, float)) and seconds >= 0]
            for key, runs in cast(dict[object, object], value).items()
            if isinstance(key, str) and isinstance(runs, list)}


def save_measures(path: Path, measures: dict[str, float] | dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            _ = output.write(json.dumps(measures, sort_keys=True) + "\n")
            output.close()
            _ = temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def reap_gate_job(job: RunningGateJob, *, block: bool = False) -> None:
    """Collect the leader's status and CPU once, including children it waited for."""
    if job.process.returncode is not None:
        return
    with rollback_signals(defer=True):
        pid, status, usage = os.wait4(job.process.pid, 0 if block else os.WNOHANG)
        if pid:
            job.process.returncode = os.waitstatus_to_exitcode(status)
            job.cpu_seconds = usage.ru_utime + usage.ru_stime


def kill_job_group(job: RunningGateJob) -> None:
    # Also run this after the leader exits: a successful module may have leaked
    # children. SIGKILL prevents a child that ignores TERM from surviving cleanup.
    try:
        process_group.send_process_group(job.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    reap_gate_job(job, block=True)


def stop_gate_job(job: RunningGateJob) -> None:
    """Stop the group and reap its leader before capacity returns."""
    config = memory_admit.read_memory_config()
    try:
        process_group.send_process_group(job.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + config.program_stop_seconds
    while job.process.returncode is None:
        reap_gate_job(job)
        remaining = deadline - time.monotonic()
        if job.process.returncode is not None or remaining <= 0:
            break
        time.sleep(min(config.program_sample_seconds, remaining))
    # Kill surviving group members even when the leader has exited.
    kill_job_group(job)


def one_minute_load() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def module_order(modules: Sequence[tuple[str, Path]], timings: dict[str, float],
                 repo: Path) -> list[tuple[str, Path]]:
    """`modules` in start order: modules with no recorded time first, then the slowest recorded first."""
    return sorted(modules, key=lambda item: (f"{item[0]}/{test_module_name(repo, *item)}" in timings,
                                             -timings.get(f"{item[0]}/{test_module_name(repo, *item)}", 0)))


def start_gate_job(suite: str, path: Path, argv: Sequence[str], repo: Path,
                   environment: dict[str, str], log: Path, *,
                   kind: GateJobKind = GateJobKind.TEST_MODULE) -> RunningGateJob:
    output = log.open("w+")
    started = time.monotonic()
    try:
        if kind is GateJobKind.TEST_MODULE:
            root = Path(tempfile.mkdtemp(prefix="roots-", dir=log.parent))
            home, state, claude, config = (root / name for name in ("home", "state", "claude", "config"))
            for folder in (home, state, claude, config):
                folder.mkdir()
            userbase = home / ".local"
            guard = Path(sysconfig.get_path("purelib", "posix_user", vars={"userbase": str(userbase)}))
            guard.mkdir(parents=True)
            _ = (guard / "sitecustomize.py").write_text(
                f"live_roots = {LIVE_ROOTS!r}\npush_keys = {LIVE_PUSH_KEYS!r}\n"
                + f"push_host = {API_HOST!r}\nmarker = {str(log.parent / 'refused-effects')!r}\n" + SUITE_GUARD)
            environment = {**environment, "HOME": str(home), "XDG_STATE_HOME": str(state),
                           "XDG_CONFIG_HOME": str(config), "SCRATCH_ROOT": str(claude / "scratch"),
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUSERBASE": str(userbase)}
            for name in ("PUSHOVER_USER", "PUSHOVER_TOKEN", "PYTHONNOUSERSITE"):
                _ = environment.pop(name, None)
        process = subprocess.Popen([sys.executable, "-c", SUITE_BOOTSTRAP, *argv],
                                   cwd=repo, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
    except BaseException:
        output.close()
        raise
    module = test_module_name(repo, suite, path)
    return RunningGateJob(suite, path, process, started, output, log, module, kind=kind)


def test_module_environment(root: Path, environment: dict[str, str]) -> dict[str, str]:
    """Give a module a private build log and tripwires for tools and live memory."""
    buildlog = root / "buildlog"
    buildlog.mkdir()
    tools = root / "build-tool-tripwires"
    tools.mkdir()
    marker = root / "build-tool-calls.jsonl"
    script = f"""#!{sys.executable}
import fcntl, json, sys
with open({str(marker)!r}, "a") as output:
    fcntl.flock(output, fcntl.LOCK_EX)
    output.write(json.dumps(sys.argv) + "\\n")
raise SystemExit(99)
"""
    for name in ("cargo", "rustc", "rustdoc", "rustup", "cargo-nextest"):
        executable = tools / name
        _ = executable.write_text(script)
        executable.chmod(0o755)
    return {**environment, "TMPDIR": str(root),
            "STAFF_FILE": str(root / "staff.json"),
            "HERDR_PANE_RECORD_DIR": str(root / "herdr-panes"),
            "BUILDLOG_DIR": str(buildlog),
            "TEST_TIMES_OUT": str(root / "test-times.json"),
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(root / "live-memory-reads.jsonl"),
            "PATH": str(tools) + os.pathsep + environment.get("PATH", os.defpath)}


def finish_gate_job(job: RunningGateJob, status: int) -> GateJobResult:
    seconds = time.monotonic() - job.started
    kill_job_group(job)
    job.output.close()
    captured = job.log.read_text(errors="replace")
    marker = job.log.parent / "build-tool-calls.jsonl"
    calls = [cast(list[str], json.loads(line)) for line in marker.read_text().splitlines()] if marker.exists() else []
    for call in calls:
        captured += f"\n{job.module}: a test started real {Path(call[0]).name}: {shlex.join(call[1:])}\n"
    memory_marker = job.log.parent / "live-memory-reads.jsonl"
    readings = memory_marker.read_text(errors="replace").splitlines() if memory_marker.exists() else []
    for line in readings:
        try:
            decoded = cast(object, json.loads(line))
            if not isinstance(decoded, dict):
                raise ValueError("record must be an object")
            reading = cast(dict[str, object], decoded)
            path, argv, pid = reading.get("reading"), reading.get("argv"), reading.get("pid")
            if (not isinstance(path, str) or not isinstance(argv, list) or type(pid) is not int
                    or not all(isinstance(arg, str) for arg in cast(list[object], argv))):
                raise ValueError("record fields are invalid")
            detail = f"{path}: {shlex.join(cast(list[str], argv))}"
        except ValueError:
            detail = f"(unreadable record): {line}"
        captured += f"\n{job.module}: a test read live memory {detail}\n"
    effects_marker = job.log.parent / "refused-effects"
    effects = effects_marker.read_text().splitlines() if effects_marker.exists() else []
    for detail in effects:
        captured += f"\n{job.module}: {detail}\n"
    times = (gate_record.TimingNotApplicable() if job.kind is GateJobKind.TYPE_CHECK
             else gate_record.read_module_times(job.log.parent / "test-times.json"))
    shutil.rmtree(job.log.parent)
    if job.killed_for:
        captured = f"{job.killed_for}\n{captured}"
    # Python 3.13 returns 5 for empty helper modules; import errors still return 1.
    if status == 5 and re.search(r"^Ran 0 tests in", captured, re.MULTILINE) and "NO TESTS RAN" in captured:
        status = 0
    if job.kind is GateJobKind.TYPE_CHECK:
        status = 0 if status == 0 and re.search(r"^0 errors, 0 warnings, 0 notes\s*$", captured, re.MULTILINE) else 1
    tripwire_tripped = bool(calls or readings or effects)
    if tripwire_tripped:
        status = 1
    return GateJobResult(job.suite, job.module, status, seconds, captured, job.killed_for,
                         tripwire_tripped, job.kind, job.cpu_seconds, times, job.peak_memory_mib)


@dataclass(frozen=True)
class CleanTestTree:
    tree: str
    commit: str


@dataclass(frozen=True)
class UnrecordableTestTree:
    reason: str


def clean_test_tree(repo: Path) -> CleanTestTree | UnrecordableTestTree:
    if checked_git(repo, "status", "--porcelain", "--untracked-files=all"):
        return UnrecordableTestTree("checkout is dirty")
    return CleanTestTree(checked_git(repo, "rev-parse", "HEAD^{tree}"), checked_git(repo, "rev-parse", "HEAD"))


@dataclass
class PassedTreeLedger:
    repo: Path
    source: Literal["tests-only", "checkpoint"]
    tree: CleanTestTree | UnrecordableTestTree
    suites: list[str]
    unavailable: bool = False

    @property
    def path(self) -> Path:
        directory = Path(os.environ.get("MERGE_GATE_PASSED_DIR", "~/.local/state/merge-gate")).expanduser()
        return directory / "passed-trees.jsonl"

    def report_error(self, error: Exception) -> None:
        if not self.unavailable:
            print(f"Test pass ledger unavailable; tests cannot be reused or recorded: {str(error).replace(chr(10), '; ')}", flush=True)
        self.unavailable = True

    def records(self) -> list[dict[str, object]]:
        if self.unavailable:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Both operations require append access; unavailable certification runs
        # all modules and cannot silently reuse a record it cannot extend.
        with self.path.open("a+", encoding="utf-8") as ledger:
            _ = ledger.seek(0)
            records: list[dict[str, object]] = []
            for line in ledger:
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    raise ValueError("invalid test pass record")
                record = cast(dict[str, object], value)
                if (not all(isinstance(record.get(key), str) for key in
                            ("tree", "commit", "at", "host", "checkout", "source"))
                        or not isinstance(record.get("suites"), list)
                        or not all(isinstance(name, str) for name in cast(list[object], record["suites"]))
                        or record["source"] not in ("tests-only", "checkpoint")
                        or record.get("kind", "full") not in ("full", "affected")):
                    raise ValueError("invalid test pass fields")
                at = datetime.fromisoformat(cast(str, record["at"]))
                if at.tzinfo is None:
                    raise ValueError("test pass time has no zone")
                records.append(record)
        return records

    def reuse(self) -> bool:
        if isinstance(self.tree, UnrecordableTestTree):
            return False
        try:
            for record in reversed(self.records()):
                if (record.get("kind", "full") == "full"
                        and record["tree"] == self.tree.tree and record["host"] == host_name()
                        and record["suites"] == self.suites):
                    at = datetime.fromisoformat(cast(str, record["at"]))
                    when = at.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")
                    print(f"tests reused from {self.tree.tree[:12]} ({when}, {record['source']}, {record['checkout']})", flush=True)
                    return True
        except (OSError, ValueError) as error:
            self.report_error(error)
        return False

    def certified_base(self) -> CertifiedBase | NoCertifiedBase:
        if isinstance(self.tree, UnrecordableTestTree):
            return NoCertifiedBase()
        try:
            records = sorted(self.records(), key=lambda record: datetime.fromisoformat(cast(str, record["at"])), reverse=True)
            for record in records:
                if (record.get("kind", "full") != "full" or record["host"] != host_name()
                        or record["suites"] != self.suites):
                    continue
                commit = cast(str, record["commit"])
                if not git(self.repo, "merge-base", "--is-ancestor", commit, "HEAD").returncode:
                    return CertifiedBase(commit, cast(str, record["at"]))
        except (OSError, ValueError) as error:
            self.report_error(error)
        return NoCertifiedBase()

    def record_pass(self, selection: AffectedSelection | FullSuite) -> None:
        if self.unavailable or isinstance(self.tree, UnrecordableTestTree):
            return
        try:
            # A changed HEAD or a test that leaves edits must not certify a
            # tree that differs from the clean tree at the start of the run.
            ending = clean_test_tree(self.repo)
            if not isinstance(ending, CleanTestTree) or ending.tree != self.tree.tree:
                return
            record = {"tree": self.tree.tree, "commit": self.tree.commit,
                      "at": datetime.now(UTC).isoformat(), "host": host_name(),
                      "checkout": str(self.repo.resolve()), "suites": self.suites, "source": self.source,
                      "kind": "full" if isinstance(selection, FullSuite) else "affected"}
            if isinstance(selection, FullSuite):
                record["reason"] = selection.reason
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as ledger:
                line = (json.dumps(record) + "\n").encode("utf-8")
                if os.write(ledger.fileno(), line) != len(line):
                    raise OSError("incomplete test pass write")
        except (GateRefused, OSError, ValueError) as error:
            self.report_error(error)


@dataclass(frozen=True)
class NoFullRunRequired:
    pass


def full_required() -> FullSuite | NoFullRunRequired:
    marker = gate_state_dir() / "full-required"
    try:
        value = cast(dict[str, object], json.loads(marker.read_text()))
        return FullSuite(f"last nightly run was red ({value['tree']}, {value['at']})")
    except FileNotFoundError:
        return NoFullRunRequired()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return FullSuite(f"last nightly run was red (unreadable marker: {error})")


def report_selection(selection: AffectedSelection | FullSuite, total: int) -> None:
    if isinstance(selection, FullSuite):
        print(f"full suite: {selection.reason}", flush=True)
        return
    when = datetime.fromisoformat(selection.base.at).astimezone(user_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"affected: {len(selection.modules)} of {total} modules (base {selection.base.commit[:12]}, {when}), "
          + f"{len(selection.changed_files)} changed files", flush=True)
    if len(selection.modules) <= 10:
        print(", ".join(f"{path.parent.name}/{path.name}" for path in selection.modules) or "(none)", flush=True)
    print(f"Tree-scanning modules: {len(selection.tree_scanners)} (always affected)", flush=True)


def record_gate_run(ledger: PassedTreeLedger, executions: Sequence[GateJobResult],
                    results: Sequence[GateJobResult], suite_started: dict[str, float],
                    suite_finished: dict[str, float], *, started_at: float, wall_s: float,
                    hold_wait_s: float, selection: AffectedSelection | FullSuite, reused: bool,
                    reason: str, status: int, max_jobs: int, peak_mib: float, cap_mib: float,
                    aborted: bool) -> None:
    """Build one gate record without changing the gate verdict."""
    try:
        slow_test_s = gate_record.slow_test_s()
        modules = gate_record.module_runs([
            gate_record.Execution(job.suite, job.module, job.seconds, job.cpu_seconds,
                                  job.peak_mib, job.returncode, job.restart, job.times)
            for job in executions
        ], slow_test_s)
        suites: list[gate_record.SuiteRun] = [
            {"suite": suite, "wall_s": suite_finished.get(suite, began) - began,
             "status": int(any(job.returncode for job in results
                               if job.kind is GateJobKind.TEST_MODULE and job.suite == suite))}
            for suite, began in suite_started.items()
        ]
        sha, branch = checked_git(ledger.repo, "rev-parse", "HEAD", "--abbrev-ref", "HEAD").splitlines()
        run: gate_record.GateRun = {
            "started_at": store.utc_iso(started_at), "ended_at": store.utc_iso(datetime.now(UTC).timestamp()),
            "wall_s": wall_s, "hold_wait_s": hold_wait_s,
            "selection": "reused" if reused else "affected" if isinstance(selection, AffectedSelection) else "full",
            "reason": reason,
            "status": status, "max_jobs": max_jobs, "peak_mib": peak_mib, "cap_mib": cap_mib,
            "checkout": str(ledger.repo), "branch": branch, "sha": sha,
            "tree": ledger.tree.tree if isinstance(ledger.tree, CleanTestTree) else "",
            "source": ledger.source, "aborted": aborted, "dirty": isinstance(ledger.tree, UnrecordableTestTree),
            "suites": suites, "modules": modules, "slow_test_s": slow_test_s,
        }
    except Exception as error:
        gate_record.report_not_written(error)
        return
    gate_record.write_gate_run(run)


def module_runner(repo: Path) -> Path:
    """Use the test runner from the tested checkout."""
    runner = repo / "scripts" / "test_times" / "module_run.py"
    if not runner.is_file():
        raise GateRefused(f"test runner not found: {runner}")
    return runner


def run_suites(repo: Path, memory_cap_mib: int = DEFAULT_MEMORY_CAP_MIB, type_dirs: Sequence[str] = (),
               module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S,
               max_running_jobs: int = DEFAULT_MAX_RUNNING_JOBS, *,
               source: Literal["tests-only", "checkpoint"] = "tests-only", full: bool = False) -> GateRun:
    if memory_cap_mib <= 0:
        raise GateRefused("memory cap must be positive")
    if module_time_limit_s <= 0:
        raise GateRefused("module time limit must be positive")
    if max_running_jobs <= 0:
        raise GateRefused("running job limit must be positive")
    absent = [name for name in type_dirs if not (repo / name).exists()]
    if absent:
        raise GateRefused(f"type dirs not found under {repo}: {', '.join(absent)}")
    modules = discover_test_modules(repo)
    if not modules:
        raise GateRefused("the test list is empty")
    runner = module_runner(repo)
    tmpfs = Path("/dev/shm")
    if not tmpfs.is_dir() or not os.access(tmpfs, os.W_OK):
        raise GateRefused("the test runner requires writable tmpfs at /dev/shm")
    hold = BuildHoldWait()
    hold.wait_until_free()
    initial_hold_seconds = hold.seconds
    try:
        tested_tree = clean_test_tree(repo)
    except (GateRefused, OSError) as error:
        tested_tree = UnrecordableTestTree(str(error))
        print(f"Test tree unavailable; tests cannot be reused or recorded: {str(error).replace(chr(10), '; ')}", flush=True)
    suites = sorted({suite for suite, _ in modules})
    ledger = PassedTreeLedger(repo, source, tested_tree, suites)
    required = full_required()
    reused = not full and not isinstance(required, FullSuite) and ledger.reuse()
    selection: AffectedSelection | FullSuite = FullSuite("passed tree reused")
    if not reused:
        selection = (required if isinstance(required, FullSuite) else FullSuite("--full") if full else
                     select_affected(repo, ledger.certified_base(), [path for _, path in modules], state_dir=gate_state_dir()))
        report_selection(selection, len(modules))
        if isinstance(selection, AffectedSelection):
            selected = set(selection.modules)
            modules = [(suite, path) for suite, path in modules if path in selected]
    print(f"Runner: {len(modules)} modules, sampled memory cap {memory_cap_mib} MiB", flush=True)
    scope_prefix = suite_scope_prefix()
    timings_path = gate_state_dir() / "module_seconds.json"
    timings = read_measures(timings_path)
    cpu_path = gate_state_dir() / "module_cpu_seconds.json"
    cpu_measures = read_measures(cpu_path)
    run_cpu: dict[str, float] = {}
    types_peaks_path = gate_state_dir() / "types_peak_mib.json"
    types_peaks = read_measures(types_peaks_path)
    types_key = " ".join(type_dirs)
    module_peaks_path = gate_state_dir() / "module_peak_mib.json"
    module_peaks = read_measures(module_peaks_path)
    recorded_peaks = RecordedJobPeaks(module_peaks.copy(), types_peaks.copy(), types_key)
    slow_modules: list[str] = []
    history_path = gate_state_dir() / "module_history.json"
    history = read_history(history_path)
    pending = deque(GateJobRequest(suite, path, test_module_name(repo, suite, path))
                    for suite, path in (module_order(modules, timings, repo) if not reused else []))
    running: list[RunningGateJob] = []
    results: list[GateJobResult] = []
    executions: list[GateJobResult] = []
    job_started = False
    first_runs: dict[tuple[str, str], GateJobResult] = {}
    rerunning = False
    suite_started: dict[str, float] = {}
    suite_finished: dict[str, float] = {}
    admission = SampledRSSAdmission(memory_cap_mib)
    largest_module_peak_mib = 0.0
    largest_module = ""
    starting_load = one_minute_load()
    maximum_load = starting_load
    began = time.monotonic()
    started_at = datetime.now(UTC).timestamp()
    python = os.environ.get("MERGE_GATE_PYTHON", sys.executable)
    reason = selection.reason if isinstance(selection, FullSuite) and not reused else ""
    elapsed = 0.0

    def record(status: int, aborted: bool) -> None:
        waited = hold.waited_s()
        wall_s = time.monotonic() - began - (waited - initial_hold_seconds) if aborted else elapsed
        record_gate_run(ledger, executions, results, suite_started, suite_finished, started_at=started_at,
                        wall_s=wall_s, hold_wait_s=waited, selection=selection, reused=reused,
                        reason=reason, status=status, max_jobs=max_running_jobs, peak_mib=admission.peak_mib,
                        cap_mib=memory_cap_mib, aborted=aborted)

    @contextmanager
    def record_on_abort() -> Generator[None, None, None]:
        try:
            yield
        except BaseException:
            if job_started:
                record(1, aborted=True)
            raise

    with (record_on_abort(),
          build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
              build_folders.FOLDER_PREFIXES["merge-gate"], build_folders.ExplicitBuildParent(tmpfs))) as temporary,
          gate_reservation(repo, memory_cap_mib, temporary, hold) as reservation,
          shared_cpu_pool() as cpu_pool):
        environment = {**os.environ, "TMPDIR": str(temporary)}
        types_python = typecheck_python() if type_dirs else ""
        types_suite, types_module = gate_record.TYPE_CHECK_JOB
        if isinstance(types_python, Path):
            pending.appendleft(GateJobRequest(types_suite, repo / types_module, types_module, GateJobKind.TYPE_CHECK))
        elif types_python:
            result = GateJobResult(types_suite, types_module, 1, 0.0, types_python,
                                   kind=GateJobKind.TYPE_CHECK, times=gate_record.TimingNotApplicable())
            results.append(result)
            executions.append(result)
        requests = {(job.kind, job.suite, job.module): job for job in pending}
        try:
            while pending or running:
                now = time.monotonic()
                if admission.sample_due(now):
                    maximum_load = max(maximum_load, one_minute_load())
                    sample = process_memory_mib({os.getpid(), *(job.process.pid for job in running)})
                    admission.observe(time.monotonic(), sample.get(os.getpid(), 0.0),
                                      {job.process.pid: sample.get(job.process.pid, 0.0) for job in running})
                    reservation.observe(int(admission.peak_mib * (1 << 20)), int(admission.latest_mib * (1 << 20)))
                    for job in running:
                        measured = sample.get(job.process.pid, 0.0)
                        job.peak_memory_mib = max(job.peak_memory_mib, measured)
                        if job.kind is GateJobKind.TEST_MODULE and measured > largest_module_peak_mib:
                            largest_module_peak_mib = measured
                            largest_module = f"{job.suite}/{job.module}"
                        if measured > memory_cap_mib and not job.killed_for:
                            # Admission cannot stop a job that grows after it starts; on 2026-10-18 one
                            # module grew to 28 GiB in 56 minutes and filled natedev's swap.
                            job.killed_for = (f"killed: sampled memory {measured / 1024:.3f} GiB is over the "
                                              + f"{memory_cap_mib / 1024:.3f} GiB cap")
                            print(f"{job.suite}/{job.module} {job.killed_for}", flush=True)
                            kill_job_group(job)
                while (pending and len(running) < max_running_jobs
                       and admission.admits(recorded_peaks.reservation_mib(pending[0]))
                       and (not rerunning or not running)):
                    if not hold.admits():
                        if not running:
                            hold.wait_until_free()
                        break
                    slot = cpu_pool.try_acquire() if isinstance(cpu_pool, SharedCPUPool) else cpu_pool
                    if isinstance(slot, WaitingForCPU):
                        break
                    request = pending.popleft()
                    suite, path = request.suite, request.path
                    root = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
                        build_folders.FOLDER_PREFIXES["merge-gate-module"].format(owner=f"{suite}-{path.stem}"), build_folders.ExplicitBuildParent(temporary)))
                    log = root / "output.log"
                    job_environment = ({**environment, "TMPDIR": str(root)} if request.kind is GateJobKind.TYPE_CHECK
                                       else test_module_environment(root, environment))
                    if request.kind is GateJobKind.TYPE_CHECK:
                        _ = job_environment.pop("BUILDLOG_LIVE_MEMORY_TRIPWIRE", None)
                    argv = ([os.environ.get("MERGE_GATE_BASEDPYRIGHT", "basedpyright"), "--pythonpath", str(types_python),
                             *type_dirs]
                            if request.kind is GateJobKind.TYPE_CHECK else
                            [*scope_prefix, python, str(runner), str(path.parent), path.name])
                    with rollback_signals(defer=True):
                        if request.kind is GateJobKind.TYPE_CHECK:
                            job = start_gate_job(suite, path, argv, repo, job_environment, log, kind=request.kind)
                        else:
                            job = start_gate_job(suite, path, argv, repo, job_environment, log)
                        job.cpu_slot = slot
                        running.append(job)
                        job_started = True
                    if job.kind is GateJobKind.TEST_MODULE:
                        _ = suite_started.setdefault(suite, job.started)
                    admission.reserve(job.process.pid, recorded_peaks.reservation_mib(request))
                for job in running[:]:
                    reap_gate_job(job)
                    status = job.process.returncode
                    if status is None and not job.killed_for and time.monotonic() - job.started >= module_time_limit_s:
                        job.killed_for = f"killed: still running after {module_time_limit_s:g} s, the module time limit"
                        print(f"{job.suite}/{job.module} {job.killed_for}", flush=True)
                        kill_job_group(job)
                        status = job.process.returncode
                    if status is None:
                        continue
                    try:
                        result = finish_gate_job(job, status)
                        executions.append(result)
                    finally:
                        if isinstance(job.cpu_slot, SharedCPUSlot):
                            job.cpu_slot.release()
                    running.remove(job)
                    admission.release(job.process.pid)
                    cpu_key = f"{job.suite}/{result.module}"
                    run_cpu[cpu_key] = run_cpu.get(cpu_key, 0.0) + result.cpu_seconds
                    if status < 0 and not job.killed_for and not result.tripwire_tripped and not hold.admits():
                        executions[-1] = replace(result, restart="hold")
                        pending.appendleft(requests[(job.kind, job.suite, job.module)])
                        print(f"{job.suite}/{result.module}: stopped during a natedev hold; queued again", flush=True)
                        continue
                    if not job.killed_for:
                        cpu_measures[cpu_key] = result.cpu_seconds
                    if not job.killed_for and job.peak_memory_mib:
                        if job.kind is GateJobKind.TYPE_CHECK:
                            types_peaks[types_key] = job.peak_memory_mib
                        else:
                            module_peaks[f"{job.suite}/{result.module}"] = job.peak_memory_mib
                    if job.kind is GateJobKind.TEST_MODULE and not rerunning:
                        key, seconds = f"{job.suite}/{result.module}", result.seconds
                        past = history.get(key) or ([timings[key]] if key in timings else [])
                        if past and slower_than_recorded(seconds, statistics.median(past)):
                            slow_modules.append(f"{key} {seconds:.0f} s, median of the last {len(past)} runs "
                                                + f"{statistics.median(past):.0f} s")
                        history[key] = [*past, seconds][-MODULE_HISTORY_RUNS:]
                        timings[key] = seconds
                    if rerunning:
                        first = first_runs[(result.suite, result.module)]
                        results[results.index(first)] = result
                        if result.returncode == 0:
                            print(f"Rerun alone: {result.suite}/{result.module} passed "
                                  + f"(first run exit {first.returncode}, {first.seconds:.2f} s)", flush=True)
                    else:
                        results.append(result)
                    if job.kind is GateJobKind.TEST_MODULE:
                        suite_finished[job.suite] = job.started + result.seconds
                if not pending and not running and not rerunning:
                    failed = [result for result in results
                              if result.kind is GateJobKind.TEST_MODULE and result.returncode and not result.killed_for
                              and not result.tripwire_tripped]
                    rerunning = True
                    if len(failed) > RERUN_LIMIT:
                        print(f"{len(failed)} modules failed; this is a real breakage, not load; no reruns", flush=True)
                    else:
                        first_runs = {(result.suite, result.module): result for result in failed}
                        pending.extend(requests[(result.kind, result.suite, result.module)] for result in failed)
                if running or pending:
                    time.sleep(0.05)
        finally:
            # Only terminate groups created by this runner. Reap them before tmpfs cleanup.
            with rollback_signals(defer=True):
                for job in running:
                    stop_gate_job(job)
                    job.output.close()
                    if isinstance(job.cpu_slot, SharedCPUSlot):
                        job.cpu_slot.release()
            save_measures(timings_path, timings)
            save_measures(cpu_path, cpu_measures)
            save_measures(types_peaks_path, types_peaks)
            save_measures(module_peaks_path, module_peaks)
            save_measures(history_path, history)
        reservation.status = int(admission.peak_mib > memory_cap_mib or any(result.returncode for result in results))
    elapsed = time.monotonic() - began - (hold.seconds - initial_hold_seconds)
    run = GateRun(tuple(results), elapsed, admission.peak_mib, admission.peak_mib <= memory_cap_mib, reused,
                  isinstance(selection, AffectedSelection))
    record(0 if run.passed else 1, aborted=False)
    ending_load = one_minute_load()
    maximum_load = max(maximum_load, ending_load)
    for suite in suites:
        if reused:
            print(f"{suite}: OK reused", flush=True)
            continue
        if suite not in suite_started:
            print(f"{suite}: skipped (not affected)", flush=True)
            continue
        suite_results = [result for result in results
                         if result.kind is GateJobKind.TEST_MODULE and result.suite == suite]
        failed = [module for module in suite_results if module.returncode]
        print(f"{suite}: {'RED' if failed else 'OK'} {suite_finished[suite] - suite_started[suite]:.2f}s", flush=True)
        for module in suite_results:
            key = (suite, module.module)
            if key in first_runs:
                first = first_runs[key]
                label = "passed on rerun alone; first run" if module.returncode == 0 else "first run"
                print(f"--- {suite}/{module.module} {label} exit={first.returncode} ---\n{first.output}", flush=True)
            if module.returncode:
                label = "rerun alone " if key in first_runs else ""
                print(f"--- {suite}/{module.module} {label}exit={module.returncode} ---\n{module.output}", flush=True)
    for result in results:
        if result.kind is GateJobKind.TYPE_CHECK:
            print(f"basedpyright: {'RED' if result.returncode else 'OK'}\n{result.output}", flush=True)
    for slow in slow_modules:
        print(f"Slow module: {slow} ({SLOW_MODULE_FACTOR:g}x or more)", flush=True)
    if hold.seconds > 0:
        print(f"Gate hold wait: {hold.seconds:.2f}s (excluded from Gate tests)", flush=True)
    full_reason = f"; full suite: {reason}" if reason else ""
    print(f"Gate tests: {elapsed:.2f}s; at most {max_running_jobs} modules at once; "
          + f"peak memory: {admission.peak_mib / 1024:.3f} GiB (sampled memory); cap: {memory_cap_mib / 1024:.3f} GiB{full_reason}", flush=True)
    print(f"Largest module peak: {largest_module_peak_mib / 1024:.3f} GiB (sampled memory); module: {largest_module or '(not sampled)'}", flush=True)
    most_cpu = sorted(run_cpu, key=lambda key: (-run_cpu[key], key))[:3]
    cpu_jobs = ", ".join(f"{run_cpu[key]:.2f} s {key}" for key in most_cpu) or "(none)"
    print(f"Most CPU: {cpu_jobs}; total {sum(run_cpu.values()):.2f} s", flush=True)
    print(f"One-minute load: start={starting_load:.2f} max={maximum_load:.2f} end={ending_load:.2f}", flush=True)
    if admission.peak_mib > memory_cap_mib:
        print("RED: sampled memory exceeded the cap", flush=True)
    if run.passed and not reused:
        ledger.record_pass(selection)
        if isinstance(selection, FullSuite):
            (gate_state_dir() / "full-required").unlink(missing_ok=True)
    return run


def promotion_edit_conflict(repo: Path, target: str) -> str:
    """Name local edits on paths that the promotion changes."""
    environment = dict(os.environ)
    _ = environment.pop("GIT_INDEX_FILE", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*arguments: str, index: str = "") -> bytes:
        return subprocess.run(["git", *arguments], cwd=repo,
                              env={**environment, **({"GIT_INDEX_FILE": index} if index else {})},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

    try:
        staged = git("diff", "--cached", "--no-ext-diff", "--no-renames", "--name-only", "-z", "HEAD", "--")
        # A fresh index exposes edits hidden by the live index flags.
        with tempfile.TemporaryDirectory(prefix="promotion-index-") as temporary:
            index = str(Path(temporary) / "index")
            _ = git("read-tree", "HEAD", index=index)
            files = git("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", "HEAD", "--", index=index)
        promoted = git("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", "HEAD", target, "--")
        conflicts = sorted(set((staged + files).split(b"\0")) & set(promoted.split(b"\0")) - {b""})
        if conflicts:
            return "promotion refused: tracked edits overlap promotion: " + "; ".join(
                json.dumps(os.fsdecode(path)) for path in conflicts)
    except (OSError, subprocess.SubprocessError) as error:
        return "promotion refused: tracked edit check failed: " + str(error)
    return ""


# Send the same function to a Mac without repository Python modules.
PROMOTION_TRACKED_CHECK = ("import json, os, subprocess, sys, tempfile\nfrom pathlib import Path\n"
                           + inspect.getsource(promotion_edit_conflict)
                           + "reason = promotion_edit_conflict(Path.cwd(), sys.argv[1])\n"
                           + "if reason: print(reason)\nraise SystemExit(bool(reason))\n")


@dataclass(frozen=True)
class RecordedPush:
    """The gate's exact local commit and the source and base that produced it."""

    base: str
    commit: str
    source_commit: str


@dataclass(frozen=True)
class NoRecordedPush:
    pass


def push_record_path(repo: Path, operation: str, source: str) -> Path:
    directory = Path(os.environ.get("MERGE_GATE_PASSED_DIR", "~/.local/state/merge-gate")).expanduser()
    identity = json.dumps([str(repo.resolve()), operation, source]).encode()
    return directory / "push-recovery" / (hashlib.sha256(identity).hexdigest() + ".json")


def read_push_record(path: Path) -> RecordedPush | NoRecordedPush:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoRecordedPush()
    except (OSError, ValueError) as error:
        raise GateRefused(f"push recovery record {path}: {error}") from error
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        hashes = [fields.get(key) for key in ("base", "commit", "source_commit")]
        if all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40,64}", item) for item in hashes):
            return RecordedPush(*(cast(str, item) for item in hashes))
    raise GateRefused(f"invalid push recovery record: {path}")


def write_push_record(path: Path, record: RecordedPush) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish one complete record before changing refs or attempting a push.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump({"base": record.base, "commit": record.commit,
                       "source_commit": record.source_commit}, output)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def checkpoint_retry_origin(repo: Path, origin: str, checkpoint: str) -> str:
    """Trust only this checkpoint's recorded merge, with its original parents."""
    record = read_push_record(push_record_path(repo, "checkpoint", origin + ":" + checkpoint))
    if isinstance(record, NoRecordedPush):
        return origin
    head = checked_git(repo, "rev-parse", "HEAD")
    if head != record.commit or record.source_commit != checkpoint:
        return origin
    parents = checked_git(repo, "rev-list", "--parents", "-n", "1", head).split()
    if (parents != [record.commit, record.base, checkpoint]
            or checked_git(repo, "rev-parse", origin) != record.base):
        return origin
    return record.commit


def promote(args: GateArguments, *, release_lock_wait_s: float = RELEASE_LOCK_WAIT_S,
            release_lock_poll_s: float = 0.2) -> None:
    main_repo = Path(str(args.main_repo)).expanduser().resolve()
    hold = BuildHoldWait()
    hold.wait_until_free()
    wait_for_release_lock(release_lock_wait_s, release_lock_poll_s)
    if checked_git(main_repo, "branch", "--show-current") != "main":
        raise GateRefused("promotion checkout must be on main")
    fetch_branches(main_repo, "main", args.merge_branch)
    head = checked_git(main_repo, "rev-parse", "HEAD")
    origin = checked_git(main_repo, "rev-parse", "origin/main")
    path = push_record_path(main_repo, "promotion", args.merge_branch)
    target = checked_git(main_repo, "rev-parse", f"origin/{args.merge_branch}")
    if head != origin:
        record = read_push_record(path)
        if (not isinstance(record, RecordedPush) or head != record.commit
                or record.source_commit != record.commit
                or git(main_repo, "merge-base", "--is-ancestor", origin, head).returncode
                or git(main_repo, "merge-base", "--is-ancestor", head, target).returncode):
            raise GateRefused("main HEAD differs from origin/main")
        target = record.commit
    if git(main_repo, "merge-base", "--is-ancestor", "origin/main", f"origin/{args.merge_branch}").returncode:
        raise GateRefused("main is not an ancestor of the merge branch")
    hold.wait_until_free()
    if conflict := promotion_edit_conflict(main_repo, target):
        raise GateRefused(conflict)
    write_push_record(path, RecordedPush(origin, target, target))
    _ = checked_git(main_repo, "merge", "--ff-only", target)
    _ = checked_git(main_repo, "push", "origin", "main")
    promoted = checked_git(main_repo, "rev-parse", "HEAD")
    # Retain shell expansion of ~, while quoting arbitrary non-home paths.
    mac_repo = str(args.mac_repo)
    remote_path = '"$HOME"/' + shlex.quote(mac_repo[2:]) if mac_repo.startswith("~/") else shlex.quote(mac_repo)
    remote_check = "python3 -c " + shlex.quote(PROMOTION_TRACKED_CHECK) + " origin/main"
    remote = (f"(cd {remote_path} && "
              "git fetch -q origin +refs/heads/main:refs/remotes/origin/main && "
              f"{remote_check} && git merge --ff-only -q origin/main); rc=$?; "
              "printf 'ff_rc=%s\\n' \"$rc\"; exit 0")
    try:
        result = subprocess.run([args.ssh, "-o", "ConnectTimeout=10", args.mac_host, remote],
                                cwd=main_repo, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, check=False, timeout=25)
    except (GateRefused, KeyboardInterrupt, OSError, subprocess.SubprocessError) as error:
        raise GateRefused(f"main promoted to {promoted}; Mac fast-forward failed: {error}") from error
    print(result.stdout, end="", flush=True)
    statuses = re.findall(r"^ff_rc=(\d+)\s*$", result.stdout, re.MULTILINE)
    if result.returncode or statuses != ["0"]:
        detail = "; ".join(result.stdout.splitlines() + result.stderr.splitlines())
        raise GateRefused(f"main promoted to {promoted}; Mac fast-forward failed: ssh exit={result.returncode}, ff_rc={statuses}; {detail}; the claude-sync job on the Mac retries within 15 minutes")


def rollback_merge(repo: Path, baseline: str) -> None:
    if git(repo, "rev-parse", "--verify", "MERGE_HEAD").returncode == 0:
        _ = git(repo, "merge", "--abort")
    merged = checked_git(repo, "rev-parse", "HEAD")
    result = git(repo, "reset", "--keep", baseline)
    if result.returncode == 0:
        return
    # The checkout started clean. Tracked edits created during the gate belong
    # to this run; restore them to the merge tree before retrying once.
    changed = checked_git(repo, "diff", "HEAD", "--name-only", "-z").split("\0")
    tracked = [path for path in changed if path]
    if tracked:
        _ = git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", *tracked)
    result = git(repo, "reset", "--keep", baseline)
    if result.returncode:
        blocking = git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip()
        raise GateRefused(f"merge NOT undone; merge commit {merged}; blocking files: {blocking or result.stderr.strip()}; {result.stderr.strip()}")


def notify_style(repo: Path, branch: str, tip: str) -> None:
    try:
        script = (os.environ.get("MERGE_GATE_STYLE_SYNC") or shutil.which("style_sync.py")
                  or str(Path(__file__).resolve().parents[1] / "style/style_sync.py"))
        python = shutil.which("python3") or sys.executable
        _ = sys.stdout.flush()
        result = subprocess.run(
            [python, script, "notify", "--repo-dir", str(repo), "--branch", branch, "--tip", tip],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode or result.stdout.strip() or result.stderr.strip():
            detail = failure_detail(result)
            print("style sync: " + detail.replace("\n", "; "), flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style sync: " + str(error).replace("\n", "; "), flush=True)


# add_unit.commit_paths commits a production doc change on the merge branch and leaves its push to
# this gate (user, 2026-10-08: a push of its own starts CI). Its subjects start with this.
DOC_COMMIT_HEAD = "production("


def check_local_doc_commits(repo: Path, origin: str) -> None:
    """The merge HEAD is origin's merge branch plus only production doc commits, which go out with the merge."""
    if git(repo, "merge-base", "--is-ancestor", origin, "HEAD").returncode:
        raise GateRefused("merge HEAD differs from origin merge branch")
    ahead = checked_git(repo, "log", "--format=%H %P%x09%s", f"{origin}..HEAD")
    for line in ahead.splitlines():
        hashes, _, subject = line.partition("\t")
        if len(hashes.split()) != 2 or not subject.startswith(DOC_COMMIT_HEAD):
            raise GateRefused(f"merge HEAD differs from origin merge branch: {hashes.split()[0][:12]} {subject}")
        print(f"Local doc commit goes out with this merge: {hashes.split()[0][:12]} {subject}", flush=True)


def merge_checkpoint(args: GateArguments) -> int:
    hold = BuildHoldWait()
    hold.wait_until_free()
    repo = Path(str(args.repo)).resolve()
    # Untracked files (each showrunner's LOG) never block; a merge that would
    # overwrite one fails on its own and is aborted.
    if checked_git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise GateRefused("merge checkout has uncommitted tracked changes")
    if checked_git(repo, "branch", "--show-current") != args.merge_branch:
        raise GateRefused("checkout is not on the merge branch")
    fetch_branches(repo, args.merge_branch, args.unit_branch)
    baseline = checked_git(repo, "rev-parse", "HEAD")
    checkpoint = checked_git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.checkpoint}^{{commit}}")
    origin = f"origin/{args.merge_branch}"
    check_local_doc_commits(repo, checkpoint_retry_origin(repo, origin, checkpoint))
    previous = checked_git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.previous_checkpoint}^{{commit}}")
    if git(repo, "merge-base", "--is-ancestor", checkpoint, f"origin/{args.unit_branch}").returncode:
        raise GateRefused("checkpoint is not on origin unit branch")
    if git(repo, "merge-base", "--is-ancestor", previous, checkpoint).returncode:
        raise GateRefused("previous checkpoint is not an ancestor of checkpoint")
    production, review_list = change_review_list(args)
    try:
        commits = change_review.unit_commits(repo, checkpoint, previous, str(args.merge_branch))
    except change_review.ChangeReviewError as error:
        raise GateRefused(str(error)) from error
    if refused := change_review.missing(commits):
        raise GateRefused(f"checkpoint commits without a {change_review.TRAILER} line: "
                          + "; ".join(refused) + "; " + change_review.HELD_NEXT_STEP)
    changes: list[change_review.PublicationChange] = []
    if review_list is not None:
        try:
            changes = change_review.publication_changes(commits, production)
        except (change_review.ChangeReviewError, OSError) as error:
            raise GateRefused(f"{error}: the staff record is read before the merge") from error
    subject = f"Merge {args.unit} {args.phase} ({args.checkpoint}) into {args.merge_branch}"
    try:
        hold.wait_until_free()
        result = git(repo, "merge", "--no-ff", "-m", f"{subject}\n\n{args.body}", checkpoint)
        if result.returncode:
            conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            abort = git(repo, "merge", "--abort")
            raise GateRefused(f"merge failed; conflicts: {conflicts or '(none)'}; {result.stderr.strip()}"
                              + (f"; abort failed: {abort.stderr.strip()}" if abort.returncode else ""))
        merged = checked_git(repo, "rev-parse", "HEAD")
        if merged != baseline:
            write_push_record(push_record_path(repo, "checkpoint", origin + ":" + checkpoint),
                              RecordedPush(baseline, merged, checkpoint))
        previous_unit = os.environ.get(memory_admit.BUILDLOG_UNIT)
        os.environ[memory_admit.BUILDLOG_UNIT] = args.unit
        try:
            suites = run_suites(repo, int(args.memory_cap_mib), args.type_dirs, float(args.module_time_limit_s),
                                source="checkpoint", full=args.full)
        finally:
            if previous_unit is None:
                _ = os.environ.pop(memory_admit.BUILDLOG_UNIT, None)
            else:
                os.environ[memory_admit.BUILDLOG_UNIT] = previous_unit
        if not suites.passed:
            raise GateRefused("RED: types or suites failed")
        fetch_branches(repo, "main")
        if git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
            raise GateRefused("origin/main is not an ancestor of the merge branch")
        hold.wait_until_free()
    except BaseException as error:
        rollback_merge(repo, baseline)
        raise GateRefused(f"{error or type(error).__name__}; merge undone, not pushed") from error
    _ = checked_git(repo, "push", "origin", str(args.merge_branch))
    notify_style(repo, str(args.merge_branch), checked_git(repo, "rev-parse", "HEAD"))
    print(f"Pushed {args.merge_branch}: {checked_git(repo, 'rev-parse', '--short', 'HEAD')}", flush=True)
    if review_list is not None:
        record_changes(repo, review_list, production, changes, str(args.merge_branch))
    if args.promote:
        promote(args)
    return 0


def change_review_list(args: GateArguments) -> tuple[Path, Path | None]:
    """The production whose merge this is, and the list its rows go to: --change-review, else its own.

    The production is --production, else the calling showrunner's. Rows always name their
    production, so --change-review needs --production too.
    """
    if args.change_review and not args.production:
        raise GateRefused("--change-review needs --production: every row's list names its production")
    try:
        production = change_review.production_doc(str(args.production))
    except change_review.ChangeReviewError as error:
        raise GateRefused(f"{error}: the gate adds this merge's change-review rows to its list") from error
    if args.change_review:
        return production, Path(str(args.change_review)).expanduser()
    try:
        found = change_review.list_path(production)
    except OSError as error:
        raise GateRefused(f"production doc unreadable: {error}") from error
    if found is None:
        print("Change review: the production doc has no Change review line; no rows written", flush=True)
    return production, found


def record_changes(repo: Path, review_list: Path, production: Path, changes: list[change_review.PublicationChange],
                   merge_branch: str) -> None:
    """Add a row per visible change to the list, then name any landed merge still missing its rows."""
    merged = checked_git(repo, "rev-parse", f"--short={change_review.SHORT}", "HEAD")
    try:
        rows = change_review.append_rows(review_list, changes, change_review.today(), merged, production, record=staff.snapshot())
        drifted = change_review.drift(repo, merge_branch, change_review.read(review_list), change_review.LANDED,
                                      change_review.foreign_units(production))
    except (change_review.ChangeReviewError, OSError) as error:
        raise GateRefused(f"pushed {merged}; change review list {review_list} not written: {error}") from error
    if rows:
        ids = [change_review.row_id(row) for row in rows]
        print(f"Change review: {len(rows)} row(s) added to {review_list}: "
              + (ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"), flush=True)
    for row in rows:
        print(f"Change review: {row}", flush=True)
    for line in drifted:
        print(f"Change review drift: {line}", file=sys.stderr, flush=True)


def nightly_owner(args: GateArguments) -> str:
    from scripts.production import session_staff, showrunners, staff, unit_lookup
    from scripts.shutdown import remote

    try:
        doc = change_review.production_doc(args.production)
    except change_review.ChangeReviewError:
        matching: list[Path] = []
        record = staff.initial_snapshot()
        resolver = session_staff.RowResolver(record)
        for candidate in showrunners.timer_docs().values():
            row = record.by_doc(remote.local_machine(), candidate)
            if row is not None and resolver.merge_branch(row) == session_staff.Branch(args.merge_branch):
                matching.append(candidate)
        if len(matching) != 1:
            return copies.fallback_owner("nightly")
        doc = matching[0]
    return unit_lookup.doc_owner(doc, staff.snapshot()).name


def tell_nightly_red(args: GateArguments, tree: str, failed: Sequence[str]) -> None:
    from scripts.production import showrunners

    try:
        outcome = showrunners.tell(nightly_owner(args), f"merge-gate-nightly:{tree}",
                                   f"Nightly merge gate is red: {args.merge_branch} tree {tree}. "
                                   + f"Failed modules: {', '.join(failed) or '(runner failure)'}. "
                                   + "Every gate now requires the full suite until a full run passes.")
        print(f"Nightly notice: {outcome}", flush=True)
    except (GateRefused, change_review.ChangeReviewError, OSError, ValueError) as error:
        print(f"Nightly notice could not be sent: {error}; full-required marker kept", flush=True)


def nightly_git(repo: Path, *arguments: str) -> str:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                   "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -oBatchMode=yes"}
    result = subprocess.run(["git", "-C", str(repo), *arguments], env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise GateRefused(f"nightly git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


def nightly(args: GateArguments) -> int:
    repo = Path(args.repo).expanduser().resolve()
    _ = nightly_git(repo, "check-ref-format", f"refs/heads/{args.merge_branch}")
    _ = nightly_git(repo, "fetch", "origin",
                    f"+refs/heads/{args.merge_branch}:refs/remotes/origin/{args.merge_branch}")
    commit = checked_git(repo, "rev-parse", f"refs/remotes/origin/{args.merge_branch}")
    with copies.standing(nightly_owner(args), repo, commit) as clone:
        tree = checked_git(clone, "rev-parse", "HEAD^{tree}")
        try:
            run = run_suites(clone, args.memory_cap_mib, args.type_dirs,
                             module_time_limit_s=args.module_time_limit_s, full=True)
        except (GateRefused, OSError, subprocess.SubprocessError) as error:
            print(f"Nightly suite runner failed: {error}", flush=True)
            run = GateRun((GateJobResult("runner", "suite-run", 1, 0, str(error)),), 0, 0)
        marker = gate_state_dir() / "full-required"
        if run.passed:
            marker.unlink(missing_ok=True)
            return 0
        failed = [f"{job.suite}/{job.module}" for job in run.jobs if job.returncode]
        marker.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=marker.parent, delete=False) as output:
            pending = Path(output.name)
            try:
                _ = output.write(json.dumps({"tree": tree, "at": datetime.now(UTC).isoformat(),
                                              "failed_modules": failed}) + "\n")
                output.close()
                _ = pending.replace(marker)
            finally:
                pending.unlink(missing_ok=True)
        tell_nightly_red(args, tree, failed)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", "--checkout", default=str(Path.cwd()))
    for name in ("merge-branch", "unit-branch", "checkpoint", "previous-checkpoint", "unit", "phase"):
        _ = parser.add_argument(f"--{name}", default="")
    _ = parser.add_argument("--body", default="Checkpoint verified by the complete merge gate.")
    _ = parser.add_argument("--change-review", default="", help="the change-review list to add rows to")
    _ = parser.add_argument("--production", default="",
                            help="the production staff name (default: the calling showrunner's)")
    _ = parser.add_argument("--type-dirs", "--type-dir", nargs="+", default=["scripts"])
    _ = parser.add_argument("--promote", action="store_true")
    _ = parser.add_argument("--main-repo", "--main-checkout", default="~/.claude")
    _ = parser.add_argument("--ssh", default="ssh")
    _ = parser.add_argument("--mac-host", default="mac")
    _ = parser.add_argument("--mac-repo", default="~/.claude")
    _ = parser.add_argument("--memory-cap-mib", type=int, default=DEFAULT_MEMORY_CAP_MIB,
                            help="sampled memory admission cap in MiB (default: 6144); full-tree basedpyright peaked at 4065 MiB on 2026-10-30, reserving 4878 MiB")
    _ = parser.add_argument("--module-time-limit-s", type=float, default=DEFAULT_MODULE_TIME_LIMIT_S,
                            help="kill a module still running after this many seconds (default: 1800); slowest measured: 387 s at load 152")
    _ = parser.add_argument("--tests-only", action="store_true")
    _ = parser.add_argument("--full", action="store_true", help="run all test modules even with a certified base")
    _ = parser.add_argument("--nightly", action="store_true", help="fetch and fully test origin/<merge-branch> in a shared clone")
    _ = parser.add_argument("--promote-only", action="store_true",
                            help="only promote origin/<merge-branch> to main and the Mac (needs --merge-branch)")
    args = cast(GateArguments, parser.parse_args(argv))
    if args.promote_only and not args.merge_branch:
        parser.error("--promote-only needs --merge-branch")
    if args.nightly and not args.merge_branch:
        parser.error("--nightly needs --merge-branch")
    if args.nightly and (args.tests_only or args.promote_only or args.promote):
        parser.error("--nightly cannot be combined with --tests-only or promotion")
    if not args.tests_only and not args.promote_only and not args.nightly:
        required = ("merge_branch", "unit_branch", "checkpoint", "previous_checkpoint", "unit", "phase")
        missing = [name.replace("_", "-") for name in required if not getattr(args, name)]
        if missing:
            parser.error(f"required: {', '.join('--' + name for name in missing)}")
    try:
        with rollback_signals(), scratch.copy_lock(Path(args.repo).expanduser().resolve()):
            if args.nightly:
                return nightly(args)
            if args.tests_only:
                return 0 if run_suites(Path(str(args.repo)).resolve(), int(args.memory_cap_mib),
                                       args.type_dirs,
                                       module_time_limit_s=float(args.module_time_limit_s), full=args.full).passed else 1
            if args.promote_only:
                promote(args)
                return 0
            return merge_checkpoint(args)
    except (copies.CopyError, GateRefused, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
