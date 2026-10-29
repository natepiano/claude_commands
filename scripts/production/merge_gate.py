#!/usr/bin/env python3
"""Merge a checkpoint, run the complete gate, then push (and optionally promote).

The default sampled memory cap is 4096 MiB. A 163-module run with an empty timing
cache took 102.80 s, peaked at 2.027 GiB, and had a largest module peak of
0.494 GiB (one-minute load 7.79-16.32). A sample is resident plus swapped memory.
Each running job keeps a reservation: admission counts max(job memory, largest job peak seen so far, initially 96 MiB)
plus the runner itself. Startup samples never discard reservations.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Literal, TextIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import change_review  # noqa: E402
from scripts.production.affected_modules import (  # noqa: E402
    AffectedSelection, CertifiedBase, FullSuite, NoCertifiedBase, select_affected,
)
from scripts.direct.local_release_wait import local_release_waiter  # noqa: E402
from scripts.build_targets import build_targets  # noqa: E402
from scripts.build_hold import build_hold  # noqa: E402
from scripts.buildlog.store import host_name  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

TEST_SUITES = (
    "lib", "hooks", "message", "accounts", "whoami", "agents", "build_hold", "build_queue",
    "mac_test", "usage", "bevy_upgrade", "bevy_migration_plan", "production", "direct", "buildlog",
    "lint", "nightly_review", "build_targets", "shutdown", "feature_log",
    "model_study", "herdr", "notify", "statusline", "astra", "style", "history", "guilds", "issue_score",
    "hierarchies",
)
DEFAULT_MEMORY_CAP_MIB = 4096
DEFAULT_MAX_RUNNING_JOBS = max(1, (os.cpu_count() or 2) // 2)
MODULE_MEMORY_MIB = 48
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
# basedpyright is one large job, not a module: the full tree peaked at 2.64 GiB (77 s, 2026-10-19)
# and scripts/production at 0.79 GiB. Admission reserves its last sampled peak for the same dirs,
# times TYPES_PEAK_MARGIN for growth between samples, or this when none is recorded.
TYPES_MEMORY_MIB = 3072
TYPES_PEAK_MARGIN = 1.2
# A module this many times slower than the median of its last MODULE_HISTORY_RUNS times, and at least
# the floor slower, is named in the report. On 2026-10-19 agents/test_codex_mesh.py went from 49 s to
# 1264 s (four tests each waited a 300 s call timeout) and no line said so. Load alone moved modules
# about 2x. The median names a regression on about three runs in a row, then takes it as the normal.
SLOW_MODULE_FACTOR = 3.0
SLOW_MODULE_FLOOR_S = 60.0
MODULE_HISTORY_RUNS = 5
MEMORY_SAMPLE_SECONDS = 0.25
SETTINGS_BLOB = "b3466859"
# Python restores these dispositions before exec, including an inherited SIG_IGN.
# No preexec_fn is needed, and the suite receives signals as a normal foreground job.
SUITE_BOOTSTRAP = """import os, signal, sys
for disposition in signal.valid_signals():
    if disposition not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(disposition, signal.SIG_DFL)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
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
    settings_blob: str = ""
    memory_cap_mib: int = DEFAULT_MEMORY_CAP_MIB
    module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S
    tests_only: bool = False
    promote_only: bool = False
    full: bool = False
    nightly: bool = False


@dataclass(frozen=True)
class GateJobResult:
    suite: str
    module: str
    returncode: int
    seconds: float
    output: str
    killed_for: str = ""
    build_tool_started: bool = False


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
    # Set when the runner kills the job for its memory or time limit.
    killed_for: str = ""


@dataclass
class SampledRSSAdmission:
    cap_mib: int
    latest_mib: float = 0.0
    # Start conservatively while children are still execing and their RSS is
    # growing; subsequently retain the largest measured module/subprocess peak.
    headroom_mib: float = float(MODULE_MEMORY_MIB * 2)
    job_memory_mib: dict[int, float] = field(default_factory=dict)
    # Jobs reserved at a known size; their samples never raise the per-module headroom.
    fixed_mib: dict[int, float] = field(default_factory=dict)
    peak_mib: float = 0.0
    next_sample_at: float = 0.0

    def sample_due(self, now: float) -> bool:
        return now >= self.next_sample_at

    @property
    def reserved_mib(self) -> float:
        # A small startup sample does not release a running job's reservation.
        return sum(max(0.0, self.fixed_mib.get(pid, self.headroom_mib) - measured)
                   for pid, measured in self.job_memory_mib.items())

    def observe(self, now: float, aggregate_mib: float, job_memory_mib: dict[int, float]) -> None:
        self.latest_mib = aggregate_mib
        self.peak_mib = max(self.peak_mib, aggregate_mib)
        self.headroom_mib = max([self.headroom_mib, *(measured for pid, measured in job_memory_mib.items()
                                                      if pid not in self.fixed_mib)])
        self.job_memory_mib = job_memory_mib.copy()
        self.next_sample_at = now + MEMORY_SAMPLE_SECONDS

    def admits(self, jobs_running: bool) -> bool:
        return not jobs_running or self.latest_mib + self.reserved_mib + self.headroom_mib <= self.cap_mib

    def reserve(self, pid: int, expected_mib: float = 0.0) -> None:
        self.job_memory_mib[pid] = 0.0
        if expected_mib:
            self.fixed_mib[pid] = expected_mib

    def release(self, pid: int) -> None:
        # Keep the last aggregate sample until the next scan; it may still
        # include this finished job, which conservatively delays admission.
        _ = self.job_memory_mib.pop(pid, 0.0)
        _ = self.fixed_mib.pop(pid, 0.0)


class GateRefused(Exception):
    """A gate precondition or operation failed; no later step may run."""


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
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
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
                          text=True, start_new_session=True) as process:
        try:
            output, errors = process.communicate()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
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
        raise GateRefused(f"git {' '.join(arguments)}: {result.stderr.strip() or result.stdout.strip()}")
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
                return f"type-check venv missing: {built.stderr.strip() or built.stdout.strip()}; build it with: {command}"
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


def kill_job_group(job: RunningGateJob) -> None:
    # Also run this after the leader exits: a successful module may have leaked
    # children. SIGKILL prevents a child that ignores TERM from surviving cleanup.
    try:
        os.killpg(job.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _ = job.process.wait()


def one_minute_load() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def module_order(modules: Sequence[tuple[str, Path]], timings: dict[str, float]) -> list[tuple[str, Path]]:
    """`modules` in start order: modules with no recorded time first, then the slowest recorded first."""
    return sorted(modules, key=lambda item: (f"{item[0]}/{item[1].name}" in timings,
                                             -timings.get(f"{item[0]}/{item[1].name}", 0)))


def in_builds_slice() -> bool:
    try:
        return "/builds.slice/" in Path("/proc/self/cgroup").read_text()
    except OSError:
        return False


def suite_scope_prefix() -> list[str]:
    if os.environ.get("BUILDLOG_SCOPE") == "0":
        print("Module scope skipped: BUILDLOG_SCOPE=0", flush=True)
        return []
    if in_builds_slice():
        print("Module scope skipped: gate already runs in builds.slice", flush=True)
        return []
    prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
              "--expand-environment=no", "--slice=builds.slice", "--"]
    try:
        probe = subprocess.run([*prefix, "true"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Module scope skipped: probe failed ({error})", flush=True)
        return []
    if probe.returncode:
        print(f"Module scope skipped: probe failed (exit {probe.returncode})", flush=True)
        return []
    print("Module scope: builds.slice", flush=True)
    return prefix


def start_gate_job(suite: str, path: Path, argv: Sequence[str], repo: Path,
                   environment: dict[str, str], log: Path) -> RunningGateJob:
    output = log.open("w+")
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, cwd=repo, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
    except BaseException:
        output.close()
        raise
    return RunningGateJob(suite, path, process, started, output, log)


def test_module_environment(root: Path, environment: dict[str, str]) -> dict[str, str]:
    """Give a module a private build log and commands that record toolchain calls."""
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
    return {**environment, "TMPDIR": str(root), "BUILDLOG_DIR": str(buildlog),
            "PATH": str(tools) + os.pathsep + environment.get("PATH", os.defpath)}


def finish_gate_job(job: RunningGateJob, status: int) -> GateJobResult:
    seconds = time.monotonic() - job.started
    kill_job_group(job)
    job.output.close()
    captured = job.log.read_text(errors="replace")
    marker = job.log.parent / "build-tool-calls.jsonl"
    calls = [cast(list[str], json.loads(line)) for line in marker.read_text().splitlines()] if marker.exists() else []
    for call in calls:
        captured += f"\n{job.path.name}: a test started real {Path(call[0]).name}: {shlex.join(call[1:])}\n"
    shutil.rmtree(job.log.parent)
    if job.killed_for:
        captured = f"{job.killed_for}\n{captured}"
    # Python 3.13 returns 5 for empty helper modules; import errors still return 1.
    if status == 5 and re.search(r"^Ran 0 tests in", captured, re.MULTILINE) and "NO TESTS RAN" in captured:
        status = 0
    if job.suite == "types":
        status = 0 if status in (0, 3) and re.search(r"^0 errors, 0 warnings, 0 notes\s*$", captured, re.MULTILINE) else 1
    if calls:
        status = 1
    return GateJobResult(job.suite, job.path.name, status, seconds, captured, job.killed_for, bool(calls))


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
                        and record["suites"] == sorted(TEST_SUITES)):
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
                        or record["suites"] != sorted(TEST_SUITES)):
                    continue
                commit = cast(str, record["commit"])
                if not git(self.repo, "merge-base", "--is-ancestor", commit, "HEAD").returncode:
                    return CertifiedBase(commit, cast(str, record["at"]))
        except (OSError, ValueError) as error:
            self.report_error(error)
        return NoCertifiedBase()

    def record_pass(self, kind: Literal["full", "affected"] = "full") -> None:
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
                      "checkout": str(self.repo.resolve()), "suites": sorted(TEST_SUITES), "source": self.source, "kind": kind}
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
    modules = [(suite, path) for suite in TEST_SUITES
               for path in sorted((repo / "scripts" / suite).glob("test_*.py"))]
    missing = [suite for suite in TEST_SUITES if not any(name == suite for name, _ in modules)]
    if missing:
        raise GateRefused(f"suites have no test modules: {', '.join(missing)}")
    if not modules:
        raise GateRefused("the test list is empty")
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
    ledger = PassedTreeLedger(repo, source, tested_tree)
    required = full_required()
    reused = not full and not isinstance(required, FullSuite) and ledger.reuse()
    selection: AffectedSelection | FullSuite = FullSuite("passed tree reused")
    if not reused:
        selection = (required if isinstance(required, FullSuite) else FullSuite("--full") if full else
                     select_affected(repo, ledger.certified_base(), [path for _, path in modules]))
        report_selection(selection, len(modules))
        if isinstance(selection, AffectedSelection):
            selected = set(selection.modules)
            modules = [(suite, path) for suite, path in modules if path in selected]
    print(f"Runner: {len(modules)} modules, sampled memory cap {memory_cap_mib} MiB", flush=True)
    scope_prefix = suite_scope_prefix()
    timings_path = gate_state_dir() / "module_seconds.json"
    timings = read_measures(timings_path)
    types_peaks_path = gate_state_dir() / "types_peak_mib.json"
    types_peaks = read_measures(types_peaks_path)
    types_key = " ".join(type_dirs)
    types_peak_mib = 0.0
    slow_modules: list[str] = []
    history_path = gate_state_dir() / "module_history.json"
    history = read_history(history_path)
    pending = deque(module_order(modules, timings) if not reused else [])
    running: list[RunningGateJob] = []
    results: list[GateJobResult] = []
    first_runs: dict[tuple[str, str], GateJobResult] = {}
    rerunning = False
    types_python = typecheck_python() if type_dirs else ""
    if isinstance(types_python, Path):
        pending.appendleft(("types", repo / "basedpyright"))
    elif types_python:
        results.append(GateJobResult("types", "basedpyright", 1, 0.0, types_python))
    suite_started: dict[str, float] = {}
    suite_finished: dict[str, float] = {}
    admission = SampledRSSAdmission(memory_cap_mib)
    largest_module_peak_mib = 0.0
    largest_module = ""
    starting_load = one_minute_load()
    maximum_load = starting_load
    began = time.monotonic()
    python = os.environ.get("MERGE_GATE_PYTHON", sys.executable)
    with tempfile.TemporaryDirectory(prefix="merge-gate-", dir=tmpfs) as temporary:
        environment = {**os.environ, "TMPDIR": temporary}
        try:
            while pending or running:
                now = time.monotonic()
                if admission.sample_due(now):
                    maximum_load = max(maximum_load, one_minute_load())
                    sample = process_memory_mib({os.getpid(), *(job.process.pid for job in running)})
                    admission.observe(time.monotonic(), sample.get(os.getpid(), 0.0),
                                      {job.process.pid: sample.get(job.process.pid, 0.0) for job in running})
                    for job in running:
                        measured = sample.get(job.process.pid, 0.0)
                        if job.suite == "types":
                            types_peak_mib = max(types_peak_mib, measured)
                        elif measured > largest_module_peak_mib:
                            largest_module_peak_mib = measured
                            largest_module = f"{job.suite}/{job.path.name}"
                        if measured > memory_cap_mib and not job.killed_for:
                            # Admission cannot stop a job that grows after it starts; on 2026-10-18 one
                            # module grew to 28 GiB in 56 minutes and filled natedev's swap.
                            job.killed_for = (f"killed: sampled memory {measured / 1024:.3f} GiB is over the "
                                              + f"{memory_cap_mib / 1024:.3f} GiB cap")
                            print(f"{job.suite}/{job.path.name} {job.killed_for}", flush=True)
                            kill_job_group(job)
                while (pending and len(running) < max_running_jobs
                       and admission.admits(bool(running)) and (not rerunning or not running)):
                    if not hold.admits():
                        if not running:
                            hold.wait_until_free()
                        break
                    suite, path = pending.popleft()
                    root = Path(tempfile.mkdtemp(prefix=f"{suite}-{path.stem}-", dir=temporary))
                    log = root / "output.log"
                    job_environment = ({**environment, "TMPDIR": str(root)} if suite == "types"
                                       else test_module_environment(root, environment))
                    argv = ([os.environ.get("MERGE_GATE_BASEDPYRIGHT", "basedpyright"), "--pythonpath", str(types_python),
                             *type_dirs]
                            if suite == "types" else
                            [*scope_prefix, python, "-c", SUITE_BOOTSTRAP, "-m", "unittest", "discover", "-s", str(path.parent), "-p", path.name])
                    job = start_gate_job(suite, path, argv, repo, job_environment, log)
                    running.append(job)
                    _ = suite_started.setdefault(suite, job.started)
                    admission.reserve(job.process.pid, (types_peaks[types_key] * TYPES_PEAK_MARGIN
                                                    if types_key in types_peaks else TYPES_MEMORY_MIB)
                                      if suite == "types" else 0.0)
                for job in running[:]:
                    status = job.process.poll()
                    if status is None and not job.killed_for and time.monotonic() - job.started >= module_time_limit_s:
                        job.killed_for = f"killed: still running after {module_time_limit_s:g} s, the module time limit"
                        print(f"{job.suite}/{job.path.name} {job.killed_for}", flush=True)
                        kill_job_group(job)
                        status = job.process.poll()
                    if status is None:
                        continue
                    result = finish_gate_job(job, status)
                    running.remove(job)
                    admission.release(job.process.pid)
                    if status < 0 and not job.killed_for and not hold.admits():
                        pending.appendleft((job.suite, job.path))
                        print(f"{job.suite}/{job.path.name}: stopped during a natedev hold; queued again", flush=True)
                        continue
                    if job.suite == "types":
                        if not job.killed_for and types_peak_mib:
                            types_peaks[types_key] = types_peak_mib
                    elif not rerunning:
                        key, seconds = f"{job.suite}/{job.path.name}", result.seconds
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
                    suite_finished[job.suite] = job.started + result.seconds
                if not pending and not running and not rerunning:
                    failed = [result for result in results
                              if result.suite != "types" and result.returncode and not result.killed_for
                              and not result.build_tool_started]
                    rerunning = True
                    if len(failed) > RERUN_LIMIT:
                        print(f"{len(failed)} modules failed; this is a real breakage, not load; no reruns", flush=True)
                    else:
                        first_runs = {(result.suite, result.module): result for result in failed}
                        pending.extend((result.suite, repo / "scripts" / result.suite / result.module) for result in failed)
                if running or pending:
                    time.sleep(0.05)
        finally:
            # Only terminate groups created by this runner. Reap them before tmpfs cleanup.
            for job in running:
                kill_job_group(job)
                job.output.close()
            save_measures(timings_path, timings)
            save_measures(types_peaks_path, types_peaks)
            save_measures(history_path, history)
    elapsed = time.monotonic() - began - (hold.seconds - initial_hold_seconds)
    ending_load = one_minute_load()
    maximum_load = max(maximum_load, ending_load)
    for suite in TEST_SUITES:
        if reused:
            print(f"{suite}: OK reused", flush=True)
            continue
        if suite not in suite_started:
            print(f"{suite}: skipped (not affected)", flush=True)
            continue
        failed = [module for module in results if module.suite == suite and module.returncode]
        print(f"{suite}: {'RED' if failed else 'OK'} {suite_finished[suite] - suite_started[suite]:.2f}s", flush=True)
        for module in (result for result in results if result.suite == suite):
            key = (suite, module.module)
            if key in first_runs:
                first = first_runs[key]
                label = "passed on rerun alone; first run" if module.returncode == 0 else "first run"
                print(f"--- {suite}/{module.module} {label} exit={first.returncode} ---\n{first.output}", flush=True)
            if module.returncode:
                label = "rerun alone " if key in first_runs else ""
                print(f"--- {suite}/{module.module} {label}exit={module.returncode} ---\n{module.output}", flush=True)
    for result in results:
        if result.suite == "types":
            print(f"basedpyright: {'RED' if result.returncode else 'OK'}\n{result.output}", flush=True)
    for slow in slow_modules:
        print(f"Slow module: {slow} ({SLOW_MODULE_FACTOR:g}x or more)", flush=True)
    if hold.seconds > 0:
        print(f"Gate hold wait: {hold.seconds:.2f}s (excluded from Gate tests)", flush=True)
    print(f"Gate tests: {elapsed:.2f}s; at most {max_running_jobs} modules at once; "
          + f"peak memory: {admission.peak_mib / 1024:.3f} GiB (sampled memory); cap: {memory_cap_mib / 1024:.3f} GiB", flush=True)
    print(f"Largest module peak: {largest_module_peak_mib / 1024:.3f} GiB (sampled memory); module: {largest_module or '(not sampled)'}", flush=True)
    print(f"One-minute load: start={starting_load:.2f} max={maximum_load:.2f} end={ending_load:.2f}", flush=True)
    if admission.peak_mib > memory_cap_mib:
        print("RED: sampled memory exceeded the cap", flush=True)
    run = GateRun(tuple(results), elapsed, admission.peak_mib, admission.peak_mib <= memory_cap_mib, reused,
                  isinstance(selection, AffectedSelection))
    if run.passed and not reused:
        ledger.record_pass("affected" if isinstance(selection, AffectedSelection) else "full")
        if isinstance(selection, FullSuite):
            (gate_state_dir() / "full-required").unlink(missing_ok=True)
    return run


def check_settings(repo: Path, revision: str, approved: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{8,40}", approved):
        raise GateRefused("approved settings blob must be an 8-40 character git hash")
    actual = checked_git(repo, "rev-parse", f"{revision}:settings.json")
    if not actual.startswith(approved):
        raise GateRefused(f"settings.json differs from approved blob {approved}: {actual}")


# Use a fresh index for the file comparison. The live index can hide edits
# with assume-unchanged or skip-worktree flags. Never change that index.
PROMOTION_TRACKED_CHECK = '''import json, os, subprocess, tempfile
from pathlib import Path

environment = dict(os.environ)
environment.pop("GIT_INDEX_FILE", None)
environment["GIT_OPTIONAL_LOCKS"] = "0"

def git(*arguments, env=environment):
    return subprocess.run(["git", *arguments], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout

try:
    staged = git("diff", "--cached", "--no-ext-diff", "--name-only", "-z", "HEAD", "--")
    with tempfile.TemporaryDirectory(prefix="promotion-index-") as temporary:
        private = {**environment, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git("read-tree", "HEAD", env=private)
        files = git("diff", "--no-ext-diff", "--name-only", "-z", "HEAD", "--", env=private)
    changed = sorted({os.fsdecode(path) for path in (staged + files).split(b"\\0") if path})
    if changed:
        print("tracked edits differ from HEAD: " + "; ".join(json.dumps(path) for path in changed))
        raise SystemExit(1)
except (OSError, subprocess.SubprocessError) as error:
    print("tracked edit check failed: " + str(error))
    raise SystemExit(1)
'''


def require_clean_promotion(repo: Path) -> None:
    result = command([sys.executable, "-c", PROMOTION_TRACKED_CHECK], repo)
    if result.returncode:
        raise GateRefused("promotion refused: " + (result.stdout.strip() or result.stderr.strip()))


def promote(args: GateArguments, *, release_lock_wait_s: float = RELEASE_LOCK_WAIT_S,
            release_lock_poll_s: float = 0.2) -> None:
    main_repo = Path(str(args.main_repo)).expanduser().resolve()
    require_clean_promotion(main_repo)
    hold = BuildHoldWait()
    hold.wait_until_free()
    wait_for_release_lock(release_lock_wait_s, release_lock_poll_s)
    if checked_git(main_repo, "branch", "--show-current") != "main":
        raise GateRefused("promotion checkout must be on main")
    fetch_branches(main_repo, "main", args.merge_branch)
    if checked_git(main_repo, "rev-parse", "HEAD") != checked_git(main_repo, "rev-parse", "origin/main"):
        raise GateRefused("main HEAD differs from origin/main")
    if git(main_repo, "merge-base", "--is-ancestor", "origin/main", f"origin/{args.merge_branch}").returncode:
        raise GateRefused("main is not an ancestor of the merge branch")
    check_settings(main_repo, f"origin/{args.merge_branch}", str(args.settings_blob))
    hold.wait_until_free()
    require_clean_promotion(main_repo)
    _ = checked_git(main_repo, "merge", "--ff-only", f"origin/{args.merge_branch}")
    _ = checked_git(main_repo, "push", "origin", "main")
    promoted = checked_git(main_repo, "rev-parse", "HEAD")
    # Retain shell expansion of ~, while quoting arbitrary non-home paths.
    mac_repo = str(args.mac_repo)
    remote_path = '"$HOME"/' + shlex.quote(mac_repo[2:]) if mac_repo.startswith("~/") else shlex.quote(mac_repo)
    remote_check = "python3 -c " + shlex.quote(PROMOTION_TRACKED_CHECK)
    remote = (f"(cd {remote_path} && {remote_check} && "
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
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
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
    check_local_doc_commits(repo, f"origin/{args.merge_branch}")
    checkpoint = checked_git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.checkpoint}^{{commit}}")
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
        raise GateRefused("checkpoint commits without a Change-Review line: " + "; ".join(refused))
    subject = f"Merge {args.unit} {args.phase} ({args.checkpoint}) into {args.merge_branch}"
    try:
        hold.wait_until_free()
        result = git(repo, "merge", "--no-ff", "-m", f"{subject}\n\n{args.body}", checkpoint)
        if result.returncode:
            conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            abort = git(repo, "merge", "--abort")
            raise GateRefused(f"merge failed; conflicts: {conflicts or '(none)'}; {result.stderr.strip()}"
                              + (f"; abort failed: {abort.stderr.strip()}" if abort.returncode else ""))
        suites = run_suites(repo, int(args.memory_cap_mib), args.type_dirs, float(args.module_time_limit_s),
                            source="checkpoint", full=args.full)
        if not suites.passed:
            raise GateRefused("RED: types or suites failed")
        check_settings(repo, "HEAD", str(args.settings_blob))
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
        record_changes(repo, review_list, production, commits, str(args.merge_branch))
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


def record_changes(repo: Path, review_list: Path, production: Path, commits: list[change_review.Commit],
                   merge_branch: str) -> None:
    """Add a row per visible change to the list, then name any landed merge still missing its rows."""
    merged = checked_git(repo, "rev-parse", f"--short={change_review.SHORT}", "HEAD")
    try:
        rows = change_review.append_rows(review_list, change_review.visible(commits), change_review.today(), merged,
                                         production, owners=change_review.visible_owners(commits, production))
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


def tell_nightly_red(args: GateArguments, tree: str, failed: Sequence[str]) -> None:
    from scripts.production import showrunners
    from scripts.production.production_units import Refusal, production_field

    try:
        try:
            doc = change_review.production_doc(args.production)
        except change_review.ChangeReviewError:
            # A user timer has no calling Claude session. Its merge branch
            # identifies a registered production without assuming slug == branch.
            matching: list[Path] = []
            for candidate in showrunners.timer_docs().values():
                try:
                    branch = production_field(candidate.read_text().splitlines(), "Merge branch")
                except Refusal:
                    continue
                if branch == args.merge_branch:
                    matching.append(candidate)
            if len(matching) != 1:
                raise ValueError(f"cannot identify one production for {args.merge_branch}; pass --production <doc>")
            doc = matching[0]
        slug = showrunners.production_slug(str(doc))
        outcome = showrunners.tell(slug, f"merge-gate-nightly:{tree}",
                                   f"Nightly merge gate is red: {args.merge_branch} tree {tree}. "
                                   + f"Failed modules: {', '.join(failed) or '(runner failure)'}. "
                                   + "Every gate now requires the full suite until a full run passes.")
        print(f"Nightly notice: {outcome}", flush=True)
    except (change_review.ChangeReviewError, OSError, ValueError) as error:
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
    with tempfile.TemporaryDirectory(prefix="merge-gate-nightly-") as temporary:
        clone = Path(temporary) / "checkout"
        _ = nightly_git(repo, "clone", "--shared", "--no-checkout", "--", str(repo), str(clone))
        _ = nightly_git(clone, "checkout", "--detach", commit)
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
                            help="the production doc whose header names the list (default: the calling showrunner's)")
    _ = parser.add_argument("--type-dirs", "--type-dir", nargs="+", default=["scripts"])
    _ = parser.add_argument("--promote", action="store_true")
    _ = parser.add_argument("--main-repo", "--main-checkout", default="~/.claude")
    _ = parser.add_argument("--ssh", default="ssh")
    _ = parser.add_argument("--mac-host", default="mac")
    _ = parser.add_argument("--mac-repo", default="~/.claude")
    _ = parser.add_argument("--settings-blob", default=SETTINGS_BLOB)
    _ = parser.add_argument("--memory-cap-mib", type=int, default=DEFAULT_MEMORY_CAP_MIB,
                            help="sampled memory admission cap in MiB (default: 4096); measured 163-module run: 102.80 s, 2.027 GiB peak, 0.494 GiB largest module")
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
        with rollback_signals():
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
    except (GateRefused, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
