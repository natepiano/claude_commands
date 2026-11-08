#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Merge a checkpoint, run the complete gate, then push (and optionally promote).

The queue reservation and the test runner governor use separate configuration keys.
The governor starts a job when its expected memory fits and a CPU slot is free.
With no job running, the governor always starts one job.
Memory samples update the queue reservation and the recorded peaks.
A memory peak does not kill a job or fail the gate.
A job still running at the module time limit fails the gate (default 1800 s).
After the parallel pass, up to eight failed test modules run once more, alone.
Runner-killed jobs and the type check do not run again. Each test module uses a
builds.slice scope when its probe succeeds, unless disabled or already in that slice.
--tests-only runs the same suite runner without changing git refs or files.
Clean trees reuse full passes on the same host. Merge gates run all modules.
Targeted tests select affected modules from a certified ancestor. --nightly validates a shared clone.
SharedMachine is the default. WholeMachine uses one job per CPU without shared admission or failure reruns.
Merge and tests-only runs omit type checks unless --type-dirs names folders.
The daily --nightly run checks all scripts, including hooks.
A nightly type failure reports the checker output without requiring full test runs.
Each nightly exit sends a result push unless merge_gate.conf disables it.
A failure before the checks start sends the reason. Silence keeps the newest result.
A unit checkpoint commit without its Change-Review line is refused before the merge. After the
push, each visible change becomes a row in the change-review list that --change-review names, or
the production's top-level showrunner keeps (`change_review.list_path`), with the doc stamped into
its frontmatter; the first row creates a missing list, and the list commits itself in its vault.
The production is --production, else the calling showrunner's (its update timer); a gate that
finds neither is refused before the merge, so no merge lands without its rows.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import deque
from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
import fcntl
import hashlib
import inspect
from importlib import import_module
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
import tempfile
import time
from typing import Literal, Protocol, TextIO, cast

from scripts.lib import process_group
from scripts.copies import copies
from scripts.lib import build_folders, conf
from scripts.lib.build_steps import read_step_names
from scripts.production import affected_modules, change_review  # noqa: E402
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
from scripts.build_queue import progress_profile
from scripts.build_queue.admission_client import AdmissionHoldCheck  # noqa: E402
from scripts.build_queue.queue_place import (  # noqa: E402
    QueuePlace, QueuePlaceRefused, defer_cleanup_signals, queue_place, suite_scope_prefix,
)

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
NIGHTLY_TYPE_DIRS = ("scripts",)
NIGHTLY_TYPE_DIAGNOSTIC_LINES = 20
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
# Restore dispositions and the mask in every child, including types. The runner
# defers cancellation until the child is registered for cleanup.
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
    module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S
    tests_only: bool = False
    promote_only: bool = False
    full: bool = False
    nightly: bool = False


class SuiteMode(Enum):
    """Choose the step and whether the runner selects affected modules."""
    MERGE_GATE = "merge-gate"
    TARGETED_TEST = "targeted-test"


class GateJobKind(Enum):
    TEST_MODULE = auto()
    TYPE_CHECK = auto()
    RUNNER_FAILURE = auto()


class NightlyRunState(Enum):
    NOT_STARTED = auto()


@dataclass(frozen=True)
class SharedMachine:
    """Share the machine through admission, holds, memory limits, and CPU slots."""


@dataclass(frozen=True)
class WholeMachine:
    """Use every CPU without shared admission, holds, or failure reruns."""
    module_seconds: dict[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True)
class AllModules:
    """Run every discovered module."""


@dataclass(frozen=True)
class SelectedModules:
    """Run the paths selected on natedev, including a certified empty selection."""
    paths: tuple[str, ...]


ALL_MODULES = AllModules()
SHARED_MACHINE = SharedMachine()


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
    tests_reused: bool = False
    # A valid affected selection may have no test modules at all.
    selection_certified: bool = False
    executions: tuple[GateJobResult, ...] = ()

    @property
    def passed(self) -> bool:
        return ((bool(self.jobs) or self.tests_reused or self.selection_certified)
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
    # Set when the runner kills the job for its time limit.
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
    governor_mib: float
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
        return not self.reservations_mib or self.latest_mib + self.reserved_mib + expected_mib <= self.governor_mib

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
    blocking: int
    owned: list[SharedCPUSlot] = field(default_factory=list)

    def acquire(self) -> SharedCPUSlot:
        """Wait in the pool's reader queue while no module runs."""
        started = time.monotonic()
        print("waits for a CPU slot", flush=True)
        try:
            token = os.read(self.blocking, 1)
            if not token:
                raise GateRefused("shared CPU pool returned no byte")
            slot = SharedCPUSlot(self.descriptor, token)
            self.owned.append(slot)
            return slot
        finally:
            print(f"CPU slot wait ended after {time.monotonic() - started:.3f} s", flush=True)

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
    try:
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISCHR(mode)):
            raise GateRefused(f"shared CPU pool is not a FIFO or character device: {path}")
        blocking = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        pool = SharedCPUPool(descriptor, blocking)
        try:
            yield pool
        finally:
            with defer_cleanup_signals():
                try:
                    for slot in pool.owned:
                        slot.release()
                finally:
                    os.close(blocking)
    finally:
        os.close(descriptor)


def merge_gate_governor_gib() -> float:
    """Read one memory setting when the gate uses it."""
    try:
        value = conf.read("build_admission").number("merge_gate_governor_gib")
    except conf.Unusable as error:
        raise GateRefused(str(error)) from error
    return value


@contextmanager
def gate_reservation(repo: Path, temporary: Path, hold: BuildHoldWait, *,
                     type_dirs: Sequence[str] = (),
                     mode: SuiteMode = SuiteMode.MERGE_GATE) -> Generator[QueuePlace]:
    if not temporary.is_dir():
        raise GateRefused(f"gate temporary directory is missing: {temporary}")
    try:
        program: str | Sequence[str] = (mode.value, "--type-dirs", *type_dirs) if type_dirs else mode.value
        with queue_place(program, repo,
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
def rollback_signals() -> Generator[None, None, None]:
    previous = {sig: signal.signal(sig, interrupted) for sig in ROLLBACK_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            _ = signal.signal(sig, handler)


def fetch_branches(repo: Path, *branches: str) -> None:
    _ = checked_git(repo, "fetch", "origin", *(f"+refs/heads/{branch}:refs/remotes/origin/{branch}" for branch in dict.fromkeys(branches)))


def command(arguments: Sequence[str], repo: Path) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(arguments, cwd=repo, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", start_new_session=True) as process:
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
    with build_folders.defer_signals():
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


def stop_gate_jobs(jobs: Sequence[RunningGateJob]) -> None:
    """Stop all groups with one deadline, then reap their leaders."""
    config = memory_admit.read_memory_config()
    deadline = time.monotonic() + config.program_stop_seconds
    for job in jobs:
        try:
            process_group.send_process_group(job.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    while True:
        for job in jobs:
            reap_gate_job(job)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or all(job.process.returncode is not None for job in jobs):
            break
        time.sleep(min(config.program_sample_seconds, remaining))
    for job in jobs:
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
        process = subprocess.Popen([str(python_shim(repo)), "-c", SUITE_BOOTSTRAP, *argv],
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
    script = f"""#!{Path(__file__).resolve().parents[1] / "lib/py"}
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
    tripwire_tripped = bool(calls or readings)
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
    dirty_paths: tuple[str, ...] = ()


def clean_test_tree(repo: Path) -> CleanTestTree | UnrecordableTestTree:
    try:
        paths = sorted(affected_modules.dirty_paths(repo))
    except affected_modules.GraphError as error:
        raise GateRefused(str(error)) from error
    if paths:
        return UnrecordableTestTree("checkout is dirty", tuple(paths))
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


def record_gate_run(ledger: PassedTreeLedger, executions: Sequence[GateJobResult],
                    results: Sequence[GateJobResult], suite_started: dict[str, float],
                    suite_finished: dict[str, float], *, started_at: float, wall_s: float,
                    hold_wait_s: float, selection: AffectedSelection | FullSuite, reused: bool,
                    reason: str, status: int, max_jobs: int, peak_mib: float, cap_mib: float,
                    aborted: bool, mode: SuiteMode) -> None:
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
            "source": ledger.source, "step": mode.value, "aborted": aborted,
            "dirty": isinstance(ledger.tree, UnrecordableTestTree),
            "dirty_paths": list(ledger.tree.dirty_paths[:50]) if isinstance(ledger.tree, UnrecordableTestTree) else [],
            "dirty_path_count": len(ledger.tree.dirty_paths) if isinstance(ledger.tree, UnrecordableTestTree) else 0,
            "suites": suites, "modules": modules, "slow_test_s": slow_test_s,
        }
    except Exception as error:
        gate_record.report_not_written(error)
        return
    gate_record.write_gate_run(run)


def python_shim(repo: Path) -> Path:
    """Use the Python shim from the tested checkout."""
    shim = repo / "scripts" / "lib" / "py"
    if not shim.is_file():
        raise GateRefused(f"Python shim not found: {shim}")
    return shim


def module_runner(repo: Path) -> Path:
    """Use the test runner from the tested checkout."""
    runner = repo / "scripts" / "test_times" / "module_run.py"
    if not runner.is_file():
        raise GateRefused(f"test runner not found: {runner}")
    return runner


def run_suites(repo: Path, type_dirs: Sequence[str] = (),
               module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S,
               max_running_jobs: int = DEFAULT_MAX_RUNNING_JOBS, *,
               source: Literal["tests-only", "checkpoint"] = "tests-only", full: bool = False,
               mode: SuiteMode = SuiteMode.MERGE_GATE,
               machine: SharedMachine | WholeMachine = SHARED_MACHINE,
               selected: AllModules | SelectedModules = ALL_MODULES) -> GateRun:
    shared = isinstance(machine, SharedMachine)
    governor_gib = merge_gate_governor_gib() if shared else 0.0
    if isinstance(machine, WholeMachine):
        max_running_jobs = os.cpu_count() or 1
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
    if isinstance(selected, SelectedModules):
        paths = set(selected.paths)
        discovered = {str(path.relative_to(repo)) for _, path in modules}
        if not paths <= discovered:
            raise GateRefused('selected modules are absent from the tested tree')
        modules = [(suite, path) for suite, path in modules if str(path.relative_to(repo)) in paths]
    runner = module_runner(repo)
    tmpfs = build_folders.tmpfs_temporary_parent()
    if not tmpfs.is_dir() or not os.access(tmpfs, os.W_OK):
        raise GateRefused(f"the test runner requires writable tmpfs at {tmpfs}")
    hold = BuildHoldWait() if shared else None
    if hold is not None:
        hold.wait_until_free()
    initial_hold_seconds = hold.seconds if hold is not None else 0.0
    suites = sorted({suite for suite, _ in modules})
    ledger: PassedTreeLedger | None = None
    required: FullSuite | NoFullRunRequired = NoFullRunRequired()
    reused = False
    selection: AffectedSelection | FullSuite = FullSuite("whole machine")
    if shared:
        try:
            tested_tree = clean_test_tree(repo)
        except (GateRefused, OSError) as error:
            tested_tree = UnrecordableTestTree(str(error))
            print(f"Test tree unavailable; tests cannot be reused or recorded: {str(error).replace(chr(10), '; ')}", flush=True)
        if isinstance(tested_tree, UnrecordableTestTree) and tested_tree.dirty_paths:
            print(f"Dirty checkout: {len(tested_tree.dirty_paths)} paths", flush=True)
        ledger = PassedTreeLedger(repo, source, tested_tree, suites)
        required = full_required() if mode is SuiteMode.MERGE_GATE else NoFullRunRequired()
        reused = not full and not isinstance(required, FullSuite) and ledger.reuse()
        selection = FullSuite("passed tree reused")
        if not reused:
            if isinstance(required, FullSuite):
                selection = required
            elif full:
                selection = FullSuite("--full")
            elif mode is SuiteMode.MERGE_GATE:
                selection = FullSuite(f"{read_step_names().prose(mode.value)} runs all modules")
            else:
                dirty: set[str] = set(tested_tree.dirty_paths) if isinstance(tested_tree, UnrecordableTestTree) else set()
                selection = select_affected(repo, ledger.certified_base(), [path for _, path in modules], dirty,
                                            state_dir=gate_state_dir())
            report_selection(selection, len(modules))
            if isinstance(selection, AffectedSelection):
                selected_paths = set(selection.modules)
                modules = [(suite, path) for suite, path in modules if path in selected_paths]
        print(f"Runner: {len(modules)} modules, memory governor {governor_gib:g} GiB", flush=True)
    else:
        print(f"Runner: {len(modules)} modules, whole machine, {max_running_jobs} CPUs", flush=True)
    scope_prefix = suite_scope_prefix() if shared else []
    state_directory = gate_state_dir() if shared else tmpfs
    timings_path = state_directory / "module_seconds.json"
    timings = read_measures(timings_path) if isinstance(machine, SharedMachine) else machine.module_seconds.copy()
    cpu_path = state_directory / "module_cpu_seconds.json"
    cpu_measures = read_measures(cpu_path) if shared else {}
    run_cpu: dict[str, float] = {}
    types_peaks_path = state_directory / "types_peak_mib.json"
    types_peaks = read_measures(types_peaks_path) if shared else {}
    types_key = " ".join(type_dirs)
    module_peaks_path = state_directory / "module_peak_mib.json"
    module_peaks = read_measures(module_peaks_path) if shared else {}
    recorded_peaks = RecordedJobPeaks(module_peaks.copy(), types_peaks.copy(), types_key)
    slow_modules: list[str] = []
    history_path = state_directory / "module_history.json"
    history = read_history(history_path) if shared else {}
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
    admission = SampledRSSAdmission(governor_gib * 1024) if shared else None
    largest_module_peak_mib = 0.0
    largest_module = ""
    starting_load = one_minute_load()
    maximum_load = starting_load
    began = time.monotonic()
    started_at = datetime.now(UTC).timestamp()
    python = os.environ.get("MERGE_GATE_PYTHON", str(python_shim(repo)))
    reason = selection.reason if isinstance(selection, FullSuite) and not reused else ""
    elapsed = 0.0

    def record(status: int, aborted: bool) -> None:
        if ledger is None or hold is None or admission is None:
            return
        waited = hold.waited_s()
        wall_s = time.monotonic() - began - (waited - initial_hold_seconds) if aborted else elapsed
        record_gate_run(ledger, executions, results, suite_started, suite_finished, started_at=started_at,
                        wall_s=wall_s, hold_wait_s=waited, selection=selection, reused=reused,
                        reason=reason, status=status, max_jobs=max_running_jobs, peak_mib=admission.peak_mib,
                        cap_mib=admission.governor_mib, aborted=aborted, mode=mode)

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
              build_folders.FOLDER_PREFIXES[mode.value], build_folders.ExplicitBuildParent(tmpfs))) as temporary,
          (gate_reservation(repo, temporary, hold, type_dirs=type_dirs, mode=mode) if hold is not None else nullcontext(None)) as reservation,
          (shared_cpu_pool() if shared else nullcontext(NoSharedCPU())) as cpu_pool):
        environment = {**os.environ, "TMPDIR": str(temporary), "BUILDLOG_CALLER": mode.value}
        _ = environment.pop("PYTHONPATH", None)
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
        progress_total = len(pending)
        progress_done = 0
        def publish_progress() -> None:
            if progress_total and reservation is not None:
                progress_profile.publish_counter(reservation.sidecar,
                    progress_profile.ProgressCounter("suites:1", progress_done, progress_total))

        publish_progress()
        try:
            while pending or running:
                now = time.monotonic()
                if admission is not None and admission.sample_due(now):
                    maximum_load = max(maximum_load, one_minute_load())
                    sample = process_memory_mib({os.getpid(), *(job.process.pid for job in running)})
                    admission.observe(time.monotonic(), sample.get(os.getpid(), 0.0),
                                      {job.process.pid: sample.get(job.process.pid, 0.0) for job in running})
                    if reservation is not None:
                        reservation.observe(int(admission.peak_mib * (1 << 20)), int(admission.latest_mib * (1 << 20)))
                    for job in running:
                        measured = sample.get(job.process.pid, 0.0)
                        job.peak_memory_mib = max(job.peak_memory_mib, measured)
                        if job.kind is GateJobKind.TEST_MODULE and measured > largest_module_peak_mib:
                            largest_module_peak_mib = measured
                            largest_module = f"{job.suite}/{job.module}"
                while (pending and len(running) < max_running_jobs
                       and (admission is None or admission.admits(recorded_peaks.reservation_mib(pending[0])))
                       and (not rerunning or not running)):
                    if hold is not None and not hold.admits():
                        if not running:
                            hold.wait_until_free()
                        break
                    if isinstance(cpu_pool, SharedCPUPool):
                        slot = cpu_pool.try_acquire()
                        if isinstance(slot, WaitingForCPU):
                            if running:
                                break
                            slot = cpu_pool.acquire()
                            if hold is not None and not hold.admits():
                                slot.release()
                                hold.wait_until_free()
                                break
                    else:
                        slot = cpu_pool
                    request = pending.popleft()
                    suite, path = request.suite, request.path
                    root = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
                        build_folders.FOLDER_PREFIXES["suite-module"].format(owner=f"{suite}-{path.stem}"), build_folders.ExplicitBuildParent(temporary)))
                    log = root / "output.log"
                    job_environment = ({**environment, "TMPDIR": str(root)} if request.kind is GateJobKind.TYPE_CHECK
                                       else test_module_environment(root, environment))
                    if request.kind is GateJobKind.TYPE_CHECK:
                        _ = job_environment.pop("BUILDLOG_LIVE_MEMORY_TRIPWIRE", None)
                    argv = ([os.environ.get("MERGE_GATE_BASEDPYRIGHT", "basedpyright"), "--pythonpath", str(types_python),
                             *type_dirs]
                            if request.kind is GateJobKind.TYPE_CHECK else
                            [*scope_prefix, python, str(runner), str(path.parent), path.name])
                    with build_folders.defer_signals():
                        if request.kind is GateJobKind.TYPE_CHECK:
                            job = start_gate_job(suite, path, argv, repo, job_environment, log, kind=request.kind)
                        else:
                            job = start_gate_job(suite, path, argv, repo, job_environment, log)
                        job.cpu_slot = slot
                        running.append(job)
                        job_started = True
                    if job.kind is GateJobKind.TEST_MODULE:
                        _ = suite_started.setdefault(suite, job.started)
                    if admission is not None:
                        admission.reserve(job.process.pid, recorded_peaks.reservation_mib(request))
                for job in running[:]:
                    reap_gate_job(job)
                    status = job.process.returncode
                    if status is None and not job.killed_for and time.monotonic() - job.started >= module_time_limit_s:
                        job.killed_for = f"killed: still running after {module_time_limit_s:g} s, the module time limit"
                        print(f"{job.suite}/{job.module} {job.killed_for}", flush=True)
                        stop_gate_jobs([job])
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
                    if admission is not None:
                        admission.release(job.process.pid)
                    if not shared:
                        print(f"{result.suite}/{result.module} exit={result.returncode} seconds={result.seconds:.2f}"
                              + (" stopped" if result.killed_for else ""), flush=True)
                    cpu_key = f"{job.suite}/{result.module}"
                    run_cpu[cpu_key] = run_cpu.get(cpu_key, 0.0) + result.cpu_seconds
                    if status < 0 and not job.killed_for and not result.tripwire_tripped and hold is not None and not hold.admits():
                        executions[-1] = replace(result, restart="hold")
                        pending.appendleft(requests[(job.kind, job.suite, job.module)])
                        print(f"{job.suite}/{result.module}: stopped during a natedev hold; queued again", flush=True)
                        continue
                    progress_done += 1
                    publish_progress()
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
                if (shared or isinstance(selected, SelectedModules)) and not pending and not running and not rerunning:
                    failed = [result for result in results
                              if result.kind is GateJobKind.TEST_MODULE and result.returncode and not result.killed_for
                              and not result.tripwire_tripped]
                    rerunning = True
                    if len(failed) > RERUN_LIMIT:
                        print(f"{len(failed)} modules failed; this is a real breakage, not load; no reruns", flush=True)
                    else:
                        first_runs = {(result.suite, result.module): result for result in failed}
                        pending.extend(requests[(result.kind, result.suite, result.module)] for result in failed)
                        if failed and reservation is not None:
                            progress_total = 0
                            progress_profile.clear_counter(reservation.sidecar)
                if running or pending:
                    time.sleep(0.05)
        finally:
            # Only terminate groups created by this runner. Reap them before tmpfs cleanup.
            with build_folders.defer_signals():
                stop_gate_jobs(running)
                for job in running:
                    job.output.close()
                    if isinstance(job.cpu_slot, SharedCPUSlot):
                        job.cpu_slot.release()
            if shared:
                save_measures(timings_path, timings)
                save_measures(cpu_path, cpu_measures)
                save_measures(types_peaks_path, types_peaks)
                save_measures(module_peaks_path, module_peaks)
                save_measures(history_path, history)
        if reservation is not None:
            reservation.status = int(any(result.returncode for result in results))
    hold_seconds = hold.seconds if hold is not None else 0.0
    peak_mib = admission.peak_mib if admission is not None else 0.0
    elapsed = time.monotonic() - began - (hold_seconds - initial_hold_seconds)
    run = GateRun(tuple(results), elapsed, peak_mib, reused,
                  isinstance(selection, AffectedSelection) or isinstance(selected, SelectedModules), tuple(executions))
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
    if hold_seconds > 0:
        print(f"Gate hold wait: {hold_seconds:.2f}s (excluded from Gate tests)", flush=True)
    full_reason = f"; full suite: {reason}" if reason else ""
    print(f"Gate tests: {elapsed:.2f}s; at most {max_running_jobs} modules at once; "
          + f"peak memory: {peak_mib / 1024:.3f} GiB (sampled memory){full_reason}", flush=True)
    print(f"Largest module peak: {largest_module_peak_mib / 1024:.3f} GiB (sampled memory); module: {largest_module or '(not sampled)'}", flush=True)
    most_cpu = sorted(run_cpu, key=lambda key: (-run_cpu[key], key))[:3]
    cpu_jobs = ", ".join(f"{run_cpu[key]:.2f} s {key}" for key in most_cpu) or "(none)"
    print(f"Most CPU: {cpu_jobs}; total {sum(run_cpu.values()):.2f} s", flush=True)
    print(f"One-minute load: start={starting_load:.2f} max={maximum_load:.2f} end={ending_load:.2f}", flush=True)
    if run.passed and not reused and ledger is not None:
        ledger.record_pass(selection)
        if isinstance(required, FullSuite):
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
        python = str(Path(__file__).resolve().parents[1] / "lib/py")
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
    if review_list is not None:
        try:
            producer = change_review.unit_producer(args.unit)
            _ = change_review.publication_changes(commits, production, known_producer=producer)
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
            suites = run_gate_suites(repo, args.type_dirs, float(args.module_time_limit_s),
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
    if args.promote:
        promote(args)
    if review_list is not None:
        producer = change_review.unit_producer(args.unit)
        record_changes(repo, review_list, production, str(args.merge_branch),
                       known_producer=change_review.KnownMergeProducer(merged, producer)
                       if isinstance(producer, str) else producer)
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


def record_changes(repo: Path, review_list: Path, production: Path, merge_branch: str, *,
                   known_producer: change_review.KnownMergeProducer | change_review.UnknownProducer = change_review.UNKNOWN_PRODUCER) -> None:
    """Scan published merges after every promotion step succeeds."""
    try:
        rows = change_review.list_ready(repo, production, merge_branch, review_list=review_list,
                                        known_producer=known_producer)
    except (change_review.ChangeReviewError, OSError) as error:
        raise GateRefused(f"change review list {review_list} not written: {error}") from error
    if rows:
        ids = [change_review.row_id(row) for row in rows]
        print(f"Change review: {len(rows)} row(s) added to {review_list}: "
              + (ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"), flush=True)
    for row in rows:
        print(f"Change review: {row}", flush=True)


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
    return str(unit_lookup.doc_owner(doc, staff.snapshot()).id)


def type_check_summaries(output: str) -> list[str]:
    return [line for line in output.splitlines()
            if re.fullmatch(r"\d+ errors?, \d+ warnings?, \d+ notes?", line)]


def push_nightly_result(args: GateArguments, result: GateRun | Exception) -> None:
    try:
        from scripts.message import send_budget
        from scripts.notify import push_texts, review
        from scripts.notify.pushover import MESSAGE_MAX
        config = conf.read("merge_gate")
        if config.text("nightly_result_push") == "off":
            return
        values = {"branch": args.merge_branch, "time": datetime.now(user_zone()).strftime("%H:%M %Z")}
        if isinstance(result, Exception):
            values["action"] = "None. The next nightly run tries again."
            values["reason"] = ""
            empty = push_texts.render("nightly.not_run", **values)
            reason = " ".join(str(result).split()).rstrip(".") or type(result).__name__
            values["reason"] = reason[:max(0, MESSAGE_MAX - len(empty.message))].rstrip(".")
            rendered = push_texts.render("nightly.not_run", **values)
        else:
            types = [job for job in result.jobs if job.kind is GateJobKind.TYPE_CHECK]
            failed_types = [job for job in types if job.returncode]
            summaries = type_check_summaries("\n".join(job.output for job in failed_types))
            values["types"] = ("did not run" if not types else "passed" if not failed_types
                               else "failed with " + summaries[-1] if summaries else "failed")
            failed = [f"{job.suite}/{job.module}" for job in result.jobs
                      if job.kind is GateJobKind.TEST_MODULE and job.returncode]
            limit = config.whole("nightly_result_modules")
            unfinished = (any(job.kind is GateJobKind.RUNNER_FAILURE for job in result.jobs)
                          or not result.jobs and not result.passed)
            values["tests"] = ("did not finish" if unfinished else "failed in " + ", ".join(failed[:limit])
                               + (f" and {len(failed) - limit} more" if len(failed) > limit else "")
                               if failed else "passed")
            values["outcome"] = "passed" if result.passed else "failed"
            rendered = push_texts.render("nightly.result", **values)
        sent = subprocess.run(push_texts.user_send_argv(rendered, key="nightly-result"),
                              capture_output=True, text=True, check=False,
                              timeout=send_budget.user_seconds(review.user_send_seconds()))
        if sent.returncode:
            raise ValueError(failure_detail(sent))
        print("Nightly result push: " + next(iter(sent.stdout.splitlines()), ""), flush=True)
    except Exception as error:
        print("Nightly result push could not be sent: " + " ".join(str(error).split()), flush=True)


def tell_nightly_red(args: GateArguments, tree: str, failed: Sequence[str], *,
                     type_failures: Sequence[GateJobResult] = ()) -> None:
    from scripts.production import showrunners

    requires_full = bool(failed) or not type_failures
    try:
        detail = (f"Failed modules: {', '.join(failed) or '(runner failure)'}. "
                  + "Every merge gate now requires the full suite until a merge gate passes."
                  if requires_full else "Tests passed. A type failure does not require full test runs.")
        if type_failures:
            lines = [line for job in type_failures for line in job.output.splitlines()]
            summaries = type_check_summaries("\n".join(lines))
            diagnostics = [line for line in lines if line not in summaries]
            detail += "\nType check failed:\n" + "\n".join(diagnostics[:NIGHTLY_TYPE_DIAGNOSTIC_LINES])
            detail += f"\n{max(0, len(diagnostics) - NIGHTLY_TYPE_DIAGNOSTIC_LINES)} other diagnostic lines omitted."
            if summaries:
                detail += "\n" + summaries[-1]
        name = copies.owner_name(copies.copy_owner(nightly_owner(args)))
        outcome = showrunners.tell(name, f"merge-gate-nightly:{tree}",
                                   f"Nightly merge gate is red: {args.merge_branch} tree {tree}. " + detail)
        print(f"Nightly notice: {outcome}", flush=True)
    except (GateRefused, change_review.ChangeReviewError, OSError, ValueError) as error:
        marker_note = "; full-required marker kept" if requires_full else ""
        print(f"Nightly notice could not be sent: {error}{marker_note}", flush=True)


def nightly_git(repo: Path, *arguments: str) -> str:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                   "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -oBatchMode=yes"}
    result = subprocess.run(["git", "-C", str(repo), *arguments], env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise GateRefused(f"nightly git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


def nightly(args: GateArguments) -> int:
    run: GateRun | NightlyRunState = NightlyRunState.NOT_STARTED
    result_sent = False
    try:
        repo = Path(args.repo).expanduser().resolve()
        _ = nightly_git(repo, "check-ref-format", f"refs/heads/{args.merge_branch}")
        _ = nightly_git(repo, "fetch", "origin",
                        f"+refs/heads/{args.merge_branch}:refs/remotes/origin/{args.merge_branch}")
        commit = checked_git(repo, "rev-parse", f"refs/remotes/origin/{args.merge_branch}")
        with copies.standing(nightly_owner(args), repo, copies.CommittedSource(commit)) as clone:
            tree = checked_git(clone, "rev-parse", "HEAD^{tree}")
            try:
                run = run_suites(clone, args.type_dirs,
                                 module_time_limit_s=args.module_time_limit_s, full=True)
            except (GateRefused, OSError, subprocess.SubprocessError) as error:
                print(f"Nightly suite runner failed: {error}", flush=True)
                run = GateRun((GateJobResult("runner", "suite-run", 1, 0, str(error),
                                            kind=GateJobKind.RUNNER_FAILURE),), 0, 0)
            marker = gate_state_dir() / "full-required"
            if run.passed:
                marker.unlink(missing_ok=True)
                result_sent = True
                push_nightly_result(args, run)
                return 0
            type_failures = [job for job in run.jobs if job.returncode and job.kind is GateJobKind.TYPE_CHECK]
            failed = [f"{job.suite}/{job.module}" for job in run.jobs
                      if job.returncode and job.kind is not GateJobKind.TYPE_CHECK]
            if failed or not type_failures:
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
            else:
                marker.unlink(missing_ok=True)
            tell_nightly_red(args, tree, failed, type_failures=type_failures)
            result_sent = True
            push_nightly_result(args, run)
            return 1
    except (Exception, KeyboardInterrupt) as error:
        if not result_sent:
            reason = GateRefused("interrupted by SIGINT") if isinstance(error, KeyboardInterrupt) else error
            push_nightly_result(args, run if isinstance(run, GateRun) else reason)
        raise


def add_suite_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--repo", "--checkout", default=str(Path.cwd()))
    _ = parser.add_argument("--type-dirs", "--type-dir", nargs="+", default=[],
                            help="check these folders by hand; nightly always checks scripts")
    _ = parser.add_argument("--module-time-limit-s", type=float, default=DEFAULT_MODULE_TIME_LIMIT_S,
                            help="kill a module still running after this many seconds (default: 1800); slowest measured: 387 s at load 152")
    _ = parser.add_argument("--full", action="store_true", help="run all test modules even with a certified base")


class CloudGateRoute(Protocol):
    """Define the cloud route boundary without coupling the local runner to AWS."""
    def route(self, repo: Path, *, mode: SuiteMode, full: bool,
              module_time_limit_s: float) -> GateRun | str: ...


def run_gate_suites(repo: Path, type_dirs: Sequence[str] = (),
                    module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S, *,
                    source: Literal["tests-only", "checkpoint"] = "tests-only", full: bool = False,
                    mode: SuiteMode = SuiteMode.MERGE_GATE) -> GateRun:
    """Use the scaler service and retain the permitted local fallback."""
    cloud = cast(CloudGateRoute, cast(object, import_module('scripts.aws_target.cloud_gate')))
    outcome = cloud.route(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s)
    if isinstance(outcome, str):
        print('Cloud gate uses natedev: ' + outcome, flush=True)
        return run_suites(repo, type_dirs, module_time_limit_s, source=source, full=full, mode=mode)
    remote = outcome
    if not type_dirs:
        return remote
    local = run_suites(repo, type_dirs, module_time_limit_s, source=source, full=True,
                       mode=mode, selected=SelectedModules(()))
    return GateRun(remote.jobs + local.jobs, remote.seconds + local.seconds,
                   max(remote.peak_memory_mib, local.peak_memory_mib), selection_certified=True,
                   executions=remote.executions + local.executions)


def run_tests_only(args: GateArguments, mode: SuiteMode) -> int:
    try:
        with rollback_signals(), scratch.copy_lock(Path(args.repo).expanduser().resolve()):
            return 0 if run_gate_suites(Path(args.repo).resolve(), args.type_dirs,
                                   module_time_limit_s=args.module_time_limit_s, full=args.full, mode=mode).passed else 1
    except (copies.CopyError, GateRefused, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_suite_arguments(parser)
    for name in ("merge-branch", "unit-branch", "checkpoint", "previous-checkpoint", "unit", "phase"):
        _ = parser.add_argument(f"--{name}", default="")
    _ = parser.add_argument("--body", default="Checkpoint verified by the complete merge gate.")
    _ = parser.add_argument("--change-review", default="", help="the change-review list to add rows to")
    _ = parser.add_argument("--production", default="",
                            help="the production staff name (default: the calling showrunner's)")
    _ = parser.add_argument("--promote", action="store_true")
    _ = parser.add_argument("--main-repo", "--main-checkout", default="~/.claude")
    _ = parser.add_argument("--ssh", default="ssh")
    _ = parser.add_argument("--mac-host", default="mac")
    _ = parser.add_argument("--mac-repo", default="~/.claude")
    _ = parser.add_argument("--tests-only", action="store_true")
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
    if args.nightly:
        args.type_dirs = list(NIGHTLY_TYPE_DIRS)
    if not args.tests_only and not args.promote_only and not args.nightly:
        required = ("merge_branch", "unit_branch", "checkpoint", "previous_checkpoint", "unit", "phase")
        missing = [name.replace("_", "-") for name in required if not getattr(args, name)]
        if missing:
            parser.error(f"required: {', '.join('--' + name for name in missing)}")
    if args.tests_only:
        return run_tests_only(args, SuiteMode.MERGE_GATE)
    try:
        checkout = Path(args.main_repo if args.promote_only else args.repo).expanduser().resolve()
        with rollback_signals(), scratch.copy_lock(checkout):
            if args.nightly:
                return nightly(args)
            if args.promote_only:
                promote(args)
                production, review_list = change_review_list(args)
                if review_list is not None:
                    record_changes(checkout, review_list, production, args.merge_branch)
                return 0
            return merge_checkpoint(args)
    except (copies.CopyError, GateRefused, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
