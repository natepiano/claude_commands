#!/usr/bin/env python3
"""Hold one build-queue place for a program and record its scoped peak."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, BinaryIO, TypedDict, Unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import build_folders
from scripts.lint import memory_admit
from scripts.build_queue import admission_client, admission_protocol as wire
import uuid

if TYPE_CHECKING:
    from scripts.lib import conf
else:
    import conf


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
    return probe_scope_prefix()


def probe_scope_prefix() -> list[str]:
    """Check whether the user manager can start a separate program scope."""
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


def wait_reasons(result: memory_admit.AdmissionWaitSnapshot) -> tuple[str, ...]:
    return admission_client.wait_reasons(result)


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
class ScopedProgram:
    """The published scope contains every program descendant."""


@dataclass
class StartedProgram:
    process: subprocess.Popen[bytes]
    stopped: threading.Event
    sampler: threading.Thread
    tracking: ScopedProgram | ProgramTree


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


def scope_members(scope: Path) -> set[int]:
    """Read current scope members, including members of nested scopes."""
    members: set[int] = set()
    for path in scope.rglob("cgroup.procs"):
        try:
            members.update(int(pid) for pid in path.read_text().split())
        except FileNotFoundError:
            continue
    return members


def stop_scope(scope: Path, deadline: float, sample_seconds: float) -> None:
    """Stop scope members until the scope is empty."""
    while members := scope_members(scope):
        number = signal.SIGKILL if time.monotonic() >= deadline else signal.SIGTERM
        killed_scope = False
        if number == signal.SIGKILL:
            try:
                _ = (scope / "cgroup.kill").write_text("1\n")
                killed_scope = True
            except FileNotFoundError:
                pass
        if not killed_scope:
            for pid in members:
                try:
                    os.kill(pid, number)
                except ProcessLookupError:
                    pass
        time.sleep(min(sample_seconds, max(0, deadline - time.monotonic()))
                   if number == signal.SIGTERM else sample_seconds)


def program_members(tree: ProgramTree, sidecar: Path) -> dict[int, str]:
    """Find observed tree members and scope members, including nested scopes."""
    members: set[int] = set()
    try:
        scope = sidecar.read_text().strip()
        if scope:
            members = scope_members(Path(scope))
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
    on_started: Callable[[], None] = no_hold_wait
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
        prefix: list[str] = []
        if os.environ.get("BUILDLOG_SCOPE") != "0":
            with redirect_stdout(sys.stderr):
                prefix = probe_scope_prefix()
            if not prefix:
                raise QueuePlaceRefused(
                    "the queued program requires a memory scope; set BUILDLOG_SCOPE=0 only for tests")
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
            tracking: ScopedProgram | ProgramTree = ScopedProgram()
            if not prefix:
                tracking = ProgramTree(process.pid)
                start = memory_admit.process_start_time(process.pid)
                if start is not None:
                    tracking.observed[process.pid] = start
            sampler = threading.Thread(target=self._sample, args=(tracking, stopped), daemon=True)
            self.program = StartedProgram(process, stopped, sampler, tracking)
            sampler.start()
            if prefix:
                while not self.sidecar.read_text().strip():
                    if process.poll() is not None and not self.sidecar.read_text().strip():
                        raise QueuePlaceRefused("the program exited before it published its memory scope")
                    time.sleep(memory_admit.read_memory_config().program_sample_seconds)
            self.on_started()
        return process

    def _sample(self, tracking: ScopedProgram | ProgramTree, stopped: threading.Event) -> None:
        while True:
            try:
                scope = self.sidecar.read_text().strip()
                if scope:
                    stat = Path(scope) / "memory.stat"
                    memory_admit.check_memory_reading(stat)
                    anon = next(int(line.split()[1]) for line in stat.read_text().splitlines()
                                if line.startswith("anon "))
                    self.observe(anon, anon)
                elif isinstance(tracking, ProgramTree):
                    members = program_members(tracking, self.sidecar)
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
            if isinstance(program.tracking, ScopedProgram):
                stop_scope(Path(self.sidecar.read_text().strip()), deadline, config.program_sample_seconds)
            else:
                members = program_members(program.tracking, self.sidecar)
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
                    members = program_members(program.tracking, self.sidecar)
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
    try:
        config = memory_admit.memory_config()
    except conf.Unusable as error:
        raise QueuePlaceRefused(str(error)) from error
    if isinstance(sizing, ConfiguredFirstPeak):
        first = config.program_first_peaks.get(program)
        if first is None:
            raise QueuePlaceRefused(f"no first peak for {program} in lint.conf [memory]; {conf.path('lint')}: program_first_peak_gib.{program} is missing or invalid")
        sizing = memory_admit.HistoryPeakSizing(first, memory_admit.FirstProgramPeak(first), 0.0)
    profile: memory_admit.ProgramProfile | memory_admit.StepProfile = memory_admit.ProgramProfile(
        program, str(Path.home() / ".claude"), str(repo.resolve()), sizing)
    pid = os.getpid()
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["queue-place"])) as temporary:
        sidecar = temporary / "program.cgroup"
        _ = sidecar.write_text("")
        handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        for number, handler in handlers.items():
            if handler in (signal.SIG_DFL, signal.default_int_handler):
                _ = signal.signal(number, _stop_holder)
        try:
            start = memory_admit.process_start_time(pid)
            if start is None:
                raise QueuePlaceRefused("owner no longer exists")
            owner, episode, since = wire.Owner(pid, start), uuid.uuid4().hex, time.time()
            def refresh() -> wire.AdmitRequest:
                return admission_client.step_request([program], str(repo), owner, since, str(sidecar), episode, profile)
            output = admission_client.WaitOutput()
            grant = admission_client.admit(refresh(), output, refresh, _attempt=before_check)
            if isinstance(grant, wire.Refused):
                raise QueuePlaceRefused(grant.reason)
            if isinstance(grant, admission_client.LeftQueue):
                raise QueuePlaceRefused("left the admission queue")
            output.finished()
            place = QueuePlace(Path(grant.max_path).with_suffix(""), sidecar)
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
                    _ = admission_client.release(grant.reservation, place.status)
        finally:
            with defer_cleanup_signals():
                for number, handler in handlers.items():
                    _ = signal.signal(number, handler)


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--scope-start":
        scope_start(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        raise SystemExit("queue_place supports only internal scoped program startup")
