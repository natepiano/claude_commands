#!/usr/bin/env python3
"""Hold one build-queue place for a program and record its scoped peak."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, TypedDict, Unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lint import memory_admit


@dataclass(frozen=True)
class ConfiguredFirstPeak:
    pass


CONFIGURED_FIRST_PEAK = ConfiguredFirstPeak()


class QueuePlaceRefused(Exception):
    """The program cannot acquire or keep its queue place."""


def no_hold_wait() -> None:
    pass


@contextmanager
def defer_cleanup_signals() -> Generator[None]:
    """Keep ownership changes atomic with respect to runner cancellation."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
    try:
        yield
    finally:
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def defer_start_signals() -> Generator[None]:
    """Record cancellation until the newly started child has an owner."""
    pending: list[int] = []

    def record(number: int, _frame: object) -> None:
        pending.append(number)

    previous = {number: signal.signal(number, record) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)
    if pending:
        signal.raise_signal(pending[0])


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


def atomic_sample(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        _ = temporary.write_text(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def wait_reasons(result: memory_admit.CheckResult) -> tuple[str, ...]:
    """Describe why admission holds, without treating changing samples as a new reason."""
    decision = result.decision
    if isinstance(decision.pressure, memory_admit.MemoryPressure):
        if decision.pressure.next_start_at:
            return ("memory pressure recovery",)
        if decision.pressure.swap_used_percent > result.policy.pressure.swap_used_limit:
            return ("swap memory pressure",)
        return ("memory pressure",)
    if isinstance(result.head_reason, memory_admit.BehindOlderFit):
        return (memory_admit.older_fit_text(result.head_reason),)
    reasons: list[str] = []
    if result.head_need:
        reasons.append(f"queue head {result.head_label}")
    if decision.charged + result.peak.bytes > decision.budget:
        reasons.append("build memory budget")
    if result.available < decision.threshold:
        reasons.append("free memory")
    return tuple(reasons) or ("admission refresh",)


class PopenArguments(TypedDict, total=False):
    cwd: str | Path
    env: Mapping[str, str]
    stdin: int | BinaryIO
    stdout: int | BinaryIO
    stderr: int | BinaryIO
    bufsize: int
    close_fds: bool
    pass_fds: Sequence[int]
    start_new_session: bool


@dataclass(frozen=True)
class ProgramNotStarted:
    pass


@dataclass
class ProgramTree:
    group: int
    observed: dict[int, str] = field(default_factory=dict[int, str])
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def live_members(self, scope_members: set[int]) -> dict[int, str]:
        """Retain observed descendants after a session change or reparenting."""
        with self.lock:
            processes = memory_admit.process_table(Path("/proc"))
            members = memory_admit.process_tree(self.observed, processes)
            members.update(scope_members)
            for pid in processes:
                try:
                    if os.getpgid(pid) == self.group:
                        members.add(pid)
                except ProcessLookupError:
                    continue
            live = {pid: processes[pid].start for pid in members if pid in processes}
            self.observed = live
            return live


@dataclass
class StartedProgram:
    process: subprocess.Popen[bytes]
    stopped: threading.Event
    sampler: threading.Thread
    tree: ProgramTree


def scope_start(sidecar: Path, holder_pid: int, holder_start: str, argv: Sequence[str]) -> None:
    """Publish the scope and confirm its holder before the program starts."""
    if memory_admit.process_start_time(holder_pid) != holder_start:
        raise QueuePlaceRefused("the queue place holder exited before scope startup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::") and "/builds.slice/" in line:
            scope = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            atomic_sample(sidecar, str(scope) + "\n")
            if memory_admit.process_start_time(holder_pid) != holder_start:
                raise QueuePlaceRefused("the queue place holder exited during scope publication")
            os.execvp(argv[0], list(argv))
    raise QueuePlaceRefused("the program did not enter its memory scope")


def program_members(tree: ProgramTree, sidecar: Path) -> dict[int, str]:
    """Find observed tree members and scope members, including nested scopes."""
    members: set[int] = set()
    try:
        scope = sidecar.read_text().strip()
        if scope:
            for path in Path(scope).rglob("cgroup.procs"):
                members.update(int(pid) for pid in path.read_text().split())
    except FileNotFoundError:
        pass
    return tree.live_members(members)


@dataclass
class QueuePlace:
    path: Path
    sidecar: Path
    peak_bytes: int = 0
    wave_at: float = 0.0
    status: int = 1
    program: ProgramNotStarted | StartedProgram = field(default_factory=ProgramNotStarted, init=False)
    _samples: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def observe(self, peak_bytes: int, current_bytes: int) -> None:
        """Publish the existing peak and near-peak evidence in bytes."""
        with self._samples:
            now = datetime.now(UTC).timestamp()
            if peak_bytes > self.peak_bytes:
                atomic_sample(self.path.with_suffix(".max"), f"{peak_bytes}\n")
                self.peak_bytes = peak_bytes
                atomic_sample(self.path.with_suffix(".wave"), f"{peak_bytes} {int(now)}\n")
                self.wave_at = now
            elif self.peak_bytes and current_bytes >= self.peak_bytes / 2 and now - self.wave_at >= memory_admit.read_memory_config().peak_wave_seconds:
                atomic_sample(self.path.with_suffix(".wave"), f"{self.peak_bytes} {int(now)}\n")
                self.wave_at = now

    def popen(self, argv: Sequence[str], **popen_arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]:
        if isinstance(self.program, StartedProgram):
            raise QueuePlaceRefused("a queue place can start only one program")
        if not popen_arguments.get("start_new_session", True):
            raise QueuePlaceRefused("a queued program must start in its own process group")
        popen_arguments["start_new_session"] = True
        with redirect_stdout(sys.stderr):
            prefix = suite_scope_prefix()
        holder_pid = os.getpid()
        holder_start = memory_admit.process_start_time(holder_pid)
        if holder_start is None:
            raise QueuePlaceRefused("the queue place holder has no live process identity")
        command = ([*prefix, sys.executable, str(Path(__file__).resolve()), "--scope-start", str(self.sidecar),
                    str(holder_pid), holder_start, *argv]
                   if prefix else list(argv))
        with defer_start_signals():
            process = subprocess.Popen(command, **popen_arguments)
            stopped = threading.Event()
            tree = ProgramTree(process.pid)
            start = memory_admit.process_start_time(process.pid)
            if start is not None:
                tree.observed[process.pid] = start
            sampler = threading.Thread(target=self._sample, args=(tree, stopped, bool(prefix)), daemon=True)
            self.program = StartedProgram(process, stopped, sampler, tree)
            sampler.start()
            if prefix:
                while not self.sidecar.read_text().strip():
                    if process.poll() is not None and not self.sidecar.read_text().strip():
                        raise QueuePlaceRefused("the program exited before it published its memory scope")
                    time.sleep(memory_admit.read_memory_config().program_sample_seconds)
        return process

    def _sample(self, tree: ProgramTree, stopped: threading.Event, scoped: bool) -> None:
        while True:
            try:
                members = program_members(tree, self.sidecar)
                scope = self.sidecar.read_text().strip()
                if scope:
                    stat = Path(scope) / "memory.stat"
                    memory_admit.check_memory_reading(stat)
                    anon = next(int(line.split()[1]) for line in stat.read_text().splitlines()
                                if line.startswith("anon "))
                    self.observe(anon, anon)
                elif not scoped:
                    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
                    matching = set(members)
                    if proc != Path("/proc"):
                        processes = memory_admit.process_table(proc)
                        matching = {pid for pid, start in members.items()
                                    if pid in processes and processes[pid].start == start}
                    reading = memory_admit.process_memory(matching, proc)
                    self.observe(reading.bytes, reading.bytes)
            except (OSError, ValueError, StopIteration):
                pass  # A process or scope can disappear between sampling reads.
            if stopped.wait(memory_admit.read_memory_config().program_sample_seconds):
                return

    def finish(self, *, failed: bool = False) -> None:
        if not isinstance(self.program, StartedProgram):
            return
        program = self.program
        process = program.process
        try:
            completed = process.poll()
            config = memory_admit.read_memory_config()
            deadline = time.monotonic() + config.program_stop_seconds
            members = program_members(program.tree, self.sidecar)
            while members:
                number = signal.SIGKILL if time.monotonic() >= deadline else signal.SIGTERM
                for pid, start in members.items():
                    try:
                        if memory_admit.process_start_time(pid) == start:
                            os.kill(pid, number)
                    except ProcessLookupError:
                        pass
                _ = process.poll()
                time.sleep(min(config.program_sample_seconds,
                               max(0, deadline - time.monotonic())) if number == signal.SIGTERM
                           else config.program_sample_seconds)
                members = program_members(program.tree, self.sidecar)
            _ = process.wait()
            if completed is not None and not failed:
                self.status = completed
        finally:
            program.stopped.set()
            program.sampler.join()


def _stop_holder(number: int, _frame: object) -> None:
    raise SystemExit(128 + number)


@contextmanager
def queue_place(program: str, repo: Path, *,
                sizing: memory_admit.FixedCapSizing | memory_admit.HistoryPeakSizing | ConfiguredFirstPeak = CONFIGURED_FIRST_PEAK,
                before_check: Callable[[], None] = no_hold_wait) -> Generator[QueuePlace]:
    config = memory_admit.memory_config()
    if isinstance(sizing, ConfiguredFirstPeak):
        first = config.program_first_peaks.get(program)
        if first is None:
            raise QueuePlaceRefused(f"no first peak for {program} in lint.conf [memory]")
        sizing = memory_admit.HistoryPeakSizing(first, memory_admit.FirstProgramPeak(first), 0.0)
    profile: memory_admit.ProgramProfile | memory_admit.StepProfile = memory_admit.ProgramProfile(
        program, str(Path.home() / ".claude"), str(repo.resolve()), sizing)
    root = memory_admit.store.root()
    pid = os.getpid()
    previous_need = 0
    previous_reasons: tuple[str, ...] = ()
    reported_at = 0.0
    with tempfile.TemporaryDirectory(prefix="queue-place-") as temporary:
        sidecar = Path(temporary) / "program.cgroup"
        _ = sidecar.write_text("")
        handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        for number, handler in handlers.items():
            if handler in (signal.SIG_DFL, signal.default_int_handler):
                _ = signal.signal(number, _stop_holder)
        try:
            while True:
                before_check()
                diagnostics = io.StringIO()
                try:
                    with redirect_stderr(diagnostics):
                        result = memory_admit.check(
                            Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")), pid, str(sidecar),
                            [program], root=root, profile=profile)
                except Exception as error:
                    lines = diagnostics.getvalue().splitlines()
                    raise QueuePlaceRefused(lines[-1] if lines else str(error)) from error
                profile = result.profile
                if result.decision.state == "admit":
                    if not isinstance(result.reservation, memory_admit.Reserved):
                        raise QueuePlaceRefused("memory admission did not return a program reservation")
                    place = QueuePlace(result.reservation.path, sidecar)
                    failed = False
                    try:
                        yield place
                    except BaseException as error:
                        failed = True
                        place.status = error.code if isinstance(error, SystemExit) and isinstance(error.code, int) else 1
                        raise
                    finally:
                        with defer_cleanup_signals():
                            place.finish(failed=failed)
                            memory_admit.release(place.path, place.status, root=root)
                    return
                now = time.monotonic()
                reasons = wait_reasons(result)
                if result.peak.bytes != previous_need or reasons != previous_reasons or now - reported_at >= config.wait_reply_seconds:
                    print(f"{program} memory wait: {result.peak.bytes / memory_admit.GIB:.3f} GiB needed; "
                          + "; ".join(reasons), flush=True)
                    previous_need, previous_reasons, reported_at = result.peak.bytes, reasons, now
                time.sleep(config.waiting_pass_seconds)
        finally:
            with defer_cleanup_signals():
                try:
                    memory_admit.cancel_waiter(pid, root=root)
                    for path, row in memory_admit.live_reservations(memory_admit.admission_dir(root)):
                        if row["pid"] == pid and row["sidecar"] == str(sidecar):
                            memory_admit.release(path, 1, root=root)
                finally:
                    for number, handler in handlers.items():
                        _ = signal.signal(number, handler)


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--scope-start":
        scope_start(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        raise SystemExit("queue_place supports only internal scoped program startup")
