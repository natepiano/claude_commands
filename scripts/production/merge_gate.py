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
--tests-only runs the same suite runner without performing any git operations.
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
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import TextIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import change_review  # noqa: E402

TEST_SUITES = (
    "lib", "hooks", "message", "accounts", "whoami", "agents", "build_hold",
    "mac_test", "usage", "bevy_upgrade", "production", "delegate", "buildlog",
    "lint", "nightly_review", "build_targets", "shutdown", "feature_log",
    "model_study", "herdr", "notify", "statusline", "astra", "style",
)
DEFAULT_MEMORY_CAP_MIB = 4096
MODULE_MEMORY_MIB = 48
# A module still running this long is killed and the run is red: a hung module holds every gate
# behind it. The slowest measured module took 387 s at load 152 (delegate/test_verify_mac_offload.py,
# 2026-10-18); the limit is about 4.6 times that.
DEFAULT_MODULE_TIME_LIMIT_S = 1800
# basedpyright resolves bpy (Blender's interpreter only) and PIL (a PEP 723 dependency) from this
# type-check-only venv, so the full tree reports 0/0. The stub date matches the 2026-08-23 sweep;
# later dates report new diagnostics in bake_textures.py.
TYPECHECK_REQUIREMENTS = ("pillow==13.0.0", "fake-bpy-module==20260822")
# basedpyright is one large job, not a module: the full tree peaked at 2.64 GiB (77 s, 2026-10-19)
# and scripts/production at 0.79 GiB. Admission reserves its last sampled peak for the same dirs,
# times TYPES_PEAK_MARGIN for growth between samples, or this when none is recorded.
TYPES_MEMORY_MIB = 3072
TYPES_PEAK_MARGIN = 1.2
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


@dataclass(frozen=True)
class GateJobResult:
    suite: str
    module: str
    returncode: int
    seconds: float
    output: str


@dataclass(frozen=True)
class GateRun:
    jobs: tuple[GateJobResult, ...]
    seconds: float
    peak_memory_mib: float
    memory_within_cap: bool = True

    @property
    def passed(self) -> bool:
        return self.memory_within_cap and bool(self.jobs) and all(job.returncode == 0 for job in self.jobs)


@dataclass
class RunningGateJob:
    suite: str
    path: Path
    process: subprocess.Popen[str]
    started: float
    output: TextIO
    log: Path
    # Set when the runner kills the job for its sampled memory; the job's result names it.
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


def release_running() -> bool:
    result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
    if result.returncode:
        raise GateRefused("cannot determine whether build-hold release is running")
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[0] != str(os.getpid()) and re.search(r"build_hold\.py\s+release(?:\s|$)", fields[1]):
            return True
    return False


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


def save_measures(path: Path, measures: dict[str, float]) -> None:
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


def run_suites(repo: Path, memory_cap_mib: int = DEFAULT_MEMORY_CAP_MIB, type_dirs: Sequence[str] = (),
               module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S) -> GateRun:
    if memory_cap_mib <= 0:
        raise GateRefused("memory cap must be positive")
    if module_time_limit_s <= 0:
        raise GateRefused("module time limit must be positive")
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
    print(f"Runner: {len(modules)} modules, sampled memory cap {memory_cap_mib} MiB", flush=True)
    timings_path = gate_state_dir() / "module_seconds.json"
    timings = read_measures(timings_path)
    types_peaks_path = gate_state_dir() / "types_peak_mib.json"
    types_peaks = read_measures(types_peaks_path)
    types_key = " ".join(type_dirs)
    types_peak_mib = 0.0
    modules.sort(key=lambda item: (f"{item[0]}/{item[1].name}" in timings,
                                  -timings.get(f"{item[0]}/{item[1].name}", 0)))
    pending = deque(modules)
    running: list[RunningGateJob] = []
    results: list[GateJobResult] = []
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
                while pending and admission.admits(bool(running)):
                    suite, path = pending.popleft()
                    log = Path(temporary) / f"{suite}-{path.stem}.log"
                    output = log.open("w+")
                    started = time.monotonic()
                    _ = suite_started.setdefault(suite, started)
                    try:
                        argv = ([os.environ.get("MERGE_GATE_BASEDPYRIGHT", "basedpyright"), "--pythonpath", str(types_python),
                                 *type_dirs]
                                if suite == "types" else
                                [python, "-c", SUITE_BOOTSTRAP, "-m", "unittest", "discover", "-s", str(path.parent), "-p", path.name])
                        process = subprocess.Popen(
                            argv, cwd=repo, env=environment,
                            stdin=subprocess.DEVNULL,
                            stdout=output, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True,
                        )
                    except BaseException:
                        output.close()
                        raise
                    running.append(RunningGateJob(suite, path, process, started, output, log))
                    admission.reserve(process.pid, (types_peaks[types_key] * TYPES_PEAK_MARGIN
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
                    finished = time.monotonic()
                    kill_job_group(job)
                    job.output.close()
                    captured = job.log.read_text(errors="replace")
                    job.log.unlink()
                    if job.killed_for:
                        captured = f"{job.killed_for}\n{captured}"
                    # Python 3.13's unittest CLI exits 5 for an empty module. Helper
                    # modules were also empty within whole-suite discovery; no tests
                    # are discarded, and import errors still return 1.
                    if status == 5 and re.search(r"^Ran 0 tests in", captured, re.MULTILINE) and "NO TESTS RAN" in captured:
                        status = 0
                    if job.suite == "types":
                        if not job.killed_for and types_peak_mib:
                            types_peaks[types_key] = types_peak_mib
                        status = 0 if status in (0, 3) and re.search(r"^0 errors, 0 warnings, 0 notes\s*$", captured, re.MULTILINE) else 1
                    else:
                        timings[f"{job.suite}/{job.path.name}"] = finished - job.started
                    results.append(GateJobResult(job.suite, job.path.name, status, finished - job.started, captured))
                    suite_finished[job.suite] = finished
                    running.remove(job)
                    admission.release(job.process.pid)
                if running or pending:
                    time.sleep(0.05)
        finally:
            # Only terminate groups created by this runner. Reap them before tmpfs cleanup.
            for job in running:
                kill_job_group(job)
                job.output.close()
            save_measures(timings_path, timings)
            save_measures(types_peaks_path, types_peaks)
    elapsed = time.monotonic() - began
    ending_load = one_minute_load()
    maximum_load = max(maximum_load, ending_load)
    for suite in TEST_SUITES:
        failed = [module for module in results if module.suite == suite and module.returncode]
        print(f"{suite}: {'RED' if failed else 'OK'} {suite_finished[suite] - suite_started[suite]:.2f}s", flush=True)
        for module in failed:
            print(f"--- {suite}/{module.module} exit={module.returncode} ---\n{module.output}", flush=True)
    for result in results:
        if result.suite == "types":
            print(f"basedpyright: {'RED' if result.returncode else 'OK'}\n{result.output}", flush=True)
    print(f"Gate tests: {elapsed:.2f}s; peak memory: {admission.peak_mib / 1024:.3f} GiB (sampled memory); cap: {memory_cap_mib / 1024:.3f} GiB", flush=True)
    print(f"Largest module peak: {largest_module_peak_mib / 1024:.3f} GiB (sampled memory); module: {largest_module or '(not sampled)'}", flush=True)
    print(f"One-minute load: start={starting_load:.2f} max={maximum_load:.2f} end={ending_load:.2f}", flush=True)
    if admission.peak_mib > memory_cap_mib:
        print("RED: sampled memory exceeded the cap", flush=True)
    return GateRun(tuple(results), elapsed, admission.peak_mib, admission.peak_mib <= memory_cap_mib)


def check_settings(repo: Path, revision: str, approved: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{8,40}", approved):
        raise GateRefused("approved settings blob must be an 8-40 character git hash")
    actual = checked_git(repo, "rev-parse", f"{revision}:settings.json")
    if not actual.startswith(approved):
        raise GateRefused(f"settings.json differs from approved blob {approved}: {actual}")


def promote(args: GateArguments) -> None:
    main_repo = Path(str(args.main_repo)).expanduser().resolve()
    if release_running():
        raise GateRefused("build-hold release is running; main promotion refused")
    if checked_git(main_repo, "branch", "--show-current") != "main":
        raise GateRefused("promotion checkout must be on main")
    fetch_branches(main_repo, "main", args.merge_branch)
    if checked_git(main_repo, "rev-parse", "HEAD") != checked_git(main_repo, "rev-parse", "origin/main"):
        raise GateRefused("main HEAD differs from origin/main")
    if git(main_repo, "merge-base", "--is-ancestor", "origin/main", f"origin/{args.merge_branch}").returncode:
        raise GateRefused("main is not an ancestor of the merge branch")
    check_settings(main_repo, f"origin/{args.merge_branch}", str(args.settings_blob))
    _ = checked_git(main_repo, "merge", "--ff-only", f"origin/{args.merge_branch}")
    _ = checked_git(main_repo, "push", "origin", "main")
    promoted = checked_git(main_repo, "rev-parse", "HEAD")
    # Retain shell expansion of ~, while quoting arbitrary non-home paths.
    mac_repo = str(args.mac_repo)
    remote_path = '"$HOME"/' + shlex.quote(mac_repo[2:]) if mac_repo.startswith("~/") else shlex.quote(mac_repo)
    remote = (f"(cd {remote_path} && git fetch -q origin +refs/heads/main:refs/remotes/origin/main && "
              "git merge --ff-only -q origin/main); rc=$?; "
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
        raise GateRefused(f"main promoted to {promoted}; Mac fast-forward failed: ssh exit={result.returncode}, ff_rc={statuses}; {result.stderr.strip()}")


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
        result = git(repo, "merge", "--no-ff", "-m", f"{subject}\n\n{args.body}", checkpoint)
        if result.returncode:
            conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            abort = git(repo, "merge", "--abort")
            raise GateRefused(f"merge failed; conflicts: {conflicts or '(none)'}; {result.stderr.strip()}"
                              + (f"; abort failed: {abort.stderr.strip()}" if abort.returncode else ""))
        suites = run_suites(repo, int(args.memory_cap_mib), args.type_dirs, float(args.module_time_limit_s))
        if not suites.passed:
            raise GateRefused("RED: types or suites failed")
        check_settings(repo, "HEAD", str(args.settings_blob))
        fetch_branches(repo, "main")
        if git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
            raise GateRefused("origin/main is not an ancestor of the merge branch")
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
                                         production)
        drifted = change_review.drift(repo, merge_branch, change_review.read(review_list), change_review.LANDED)
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
    args = cast(GateArguments, parser.parse_args(argv))
    if not args.tests_only:
        required = ("merge_branch", "unit_branch", "checkpoint", "previous_checkpoint", "unit", "phase")
        missing = [name.replace("_", "-") for name in required if not getattr(args, name)]
        if missing:
            parser.error(f"required: {', '.join('--' + name for name in missing)}")
    try:
        with rollback_signals():
            if args.tests_only:
                return 0 if run_suites(Path(str(args.repo)).resolve(), int(args.memory_cap_mib),
                                       module_time_limit_s=float(args.module_time_limit_s)).passed else 1
            return merge_checkpoint(args)
    except (GateRefused, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
