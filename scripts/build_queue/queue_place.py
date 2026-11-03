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
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, BinaryIO, TypedDict, Unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lint import memory_admit
from scripts.lib import process_start

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


class CleanupIncomplete(QueuePlaceRefused):
    """The deadline ended before cleanup confirmed every child exit."""

    def __init__(self, unresolved_pids: set[int]) -> None:
        self.unresolved_pids: frozenset[int] = frozenset(unresolved_pids)
        super().__init__("program cleanup incomplete; unresolved pids: "
                         + ", ".join(str(pid) for pid in sorted(unresolved_pids)))


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
    unconfirmed_members: frozenset[int] = field(default_factory=frozenset[int])
    replaced_members: set[int] = field(default_factory=set[int])

    def live_members(self, scope_members: set[int]) -> dict[int, str]:
        """Retain observed descendants after a session change or reparenting."""
        processes = memory_admit.process_table(Path("/proc"))
        fresh_members = memory_admit.process_tree(self.observed, processes) | scope_members
        if self.group:
            for pid in processes:
                try:
                    if os.getpgid(pid) == self.group:
                        fresh_members.add(pid)
                except OSError:
                    continue
        candidates = fresh_members | self.observed.keys() | self.unconfirmed_members
        live: dict[int, str] = {}
        unknown: set[int] = set()
        retained: dict[int, str] = {}
        for pid in candidates - self.replaced_members:
            identity = processes.get(pid)
            if isinstance(identity, memory_admit.ProcessIdentity):
                known = self.observed.get(pid)
                if known is not None and known != identity.start:
                    self.replaced_members.add(pid)
                elif known is not None or pid in fresh_members:
                    live[pid] = identity.start
            elif isinstance(identity, memory_admit.UnreadableProcessIdentity) or pid in scope_members:
                unknown.add(pid)
                if pid in self.observed:
                    retained[pid] = self.observed[pid]
            elif pid in self.observed:
                self.replaced_members.add(pid)
        self.unconfirmed_members = frozenset(unknown)
        self.observed = {**retained, **live}
        return live

    def has_pending_members(self) -> bool:
        """Keep unreadable children pending until an exit is confirmed."""
        pending = set(self.unconfirmed_members)
        for pid in self.unconfirmed_members:
            try:
                if process_start.process_state(pid) is process_start.ProcessRunState.GONE:
                    pending.remove(pid)
                    if pid in self.observed:
                        self.replaced_members.add(pid)
                        del self.observed[pid]
            except process_start.ProcessReadError:
                pass
        self.unconfirmed_members = frozenset(pending)
        return bool(pending)


@dataclass
class ScopedProgram:
    """The published scope contains every program descendant."""


@dataclass(frozen=True)
class CleanupRequest:
    terminate_at: float
    finish_at: float
    failed: bool


@dataclass(frozen=True)
class CleanupPending:
    unresolved_pids: frozenset[int]


@dataclass(frozen=True)
class CleanupComplete:
    status: int


@dataclass(frozen=True)
class CleanupNotRequested:
    pass


@dataclass
class CleanupControl:
    request: CleanupRequest | CleanupNotRequested = field(default_factory=CleanupNotRequested)
    result: CleanupPending | CleanupComplete = field(default_factory=lambda: CleanupPending(frozenset()))
    completed: threading.Event = field(default_factory=threading.Event)


@dataclass
class StartedProgram:
    process: subprocess.Popen[bytes]
    stopped: threading.Event
    sampler: threading.Thread
    tracking: ScopedProgram | ProgramTree
    cleanup: CleanupControl = field(default_factory=CleanupControl)


def scope_start(sidecar: Path, holder_pid: int, holder_start: str, argv: Sequence[str]) -> None:
    """Publish the scope and confirm its holder before the program starts."""
    if process_start.start_text(holder_pid) != holder_start:
        raise QueuePlaceRefused("the queue place holder exited before scope startup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::") and "/builds.slice/" in line:
            scope = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            atomic_sample(sidecar, str(scope) + "\n")
            if process_start.start_text(holder_pid) != holder_start:
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
        holder_start = process_start.start_text(holder_pid)
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
            sampler = threading.Thread(target=self._run_program, daemon=True)
            self.program = StartedProgram(process, stopped, sampler, tracking)
            self.program.cleanup.result = CleanupPending(frozenset({process.pid}))
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
                    if isinstance(self.program, StartedProgram):
                        self.program.cleanup.result = CleanupPending(frozenset(
                            set(members) | tracking.unconfirmed_members | {self.program.process.pid}))
                    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
                    matching = set(members)
                    if proc != Path("/proc"):
                        processes = memory_admit.process_table(proc)
                        matching = {pid for pid, start in members.items()
                                    if isinstance(identity := processes.get(pid), memory_admit.ProcessIdentity)
                                    and identity.start == start}
                    reading = memory_admit.process_memory(matching, proc)
                    self.observe(reading.bytes, reading.bytes)
            except (OSError, ValueError, StopIteration):
                pass  # A process or scope can disappear between sampling reads.
            if stopped.wait(memory_admit.read_memory_config().program_sample_seconds):
                return

    def _run_program(self) -> None:
        program = self.program
        if not isinstance(program, StartedProgram):
            return
        try:
            if isinstance(program.tracking, ProgramTree) and program.process.pid not in program.tracking.observed:
                if program.process.poll() is None:
                    start = process_start.start_text(program.process.pid)
                    if start is not None and program.process.poll() is None:
                        program.tracking.observed[program.process.pid] = start
            self._sample(program.tracking, program.stopped)
            _ = program.stopped.wait()
            self._complete_cleanup(program)
        except Exception:
            program.cleanup.completed.set()

    def _complete_cleanup(self, program: StartedProgram) -> None:
        request = program.cleanup.request
        if not isinstance(request, CleanupRequest):
            return
        try:
            self._cleanup(program, request)
        except (CleanupIncomplete, OSError, ValueError):
            pass
        finally:
            program.cleanup.completed.set()

    def _cleanup(self, program: StartedProgram, request: CleanupRequest) -> None:
        process = program.process
        tree = program.tracking if isinstance(program.tracking, ProgramTree) else ProgramTree(0)
        config = memory_admit.read_memory_config()
        completed = process.poll()

        def check_deadline() -> None:
            if time.monotonic() > request.finish_at:
                raise CleanupIncomplete(set(program.cleanup.result.unresolved_pids)
                    if isinstance(program.cleanup.result, CleanupPending) else {process.pid})

        check_deadline()
        while True:
            members = program_members(tree, self.sidecar)
            program.cleanup.result = CleanupPending(frozenset(set(members) | tree.unconfirmed_members or {process.pid}))
            check_deadline()
            pending = tree.has_pending_members()
            check_deadline()
            unresolved = set(members) | tree.unconfirmed_members
            program.cleanup.result = CleanupPending(frozenset(unresolved or {process.pid}))
            if not members and not pending:
                scope = self.sidecar.read_text().strip()
                check_deadline()
                if scope:
                    unresolved.update(scope_members(Path(scope)))
                    check_deadline()
                if not unresolved:
                    break
            for pid, start in members.items():
                owner = process_start.check_owner(pid, process_start.parse_start(start))
                check_deadline()
                if owner is process_start.OwnerCheck.ALIVE and time.monotonic() < request.finish_at:
                    number = signal.SIGKILL if time.monotonic() >= request.terminate_at else signal.SIGTERM
                    try:
                        os.kill(pid, number)
                    except ProcessLookupError:
                        pass
            _ = process.poll()
            check_deadline()
            now = time.monotonic()
            if now >= request.finish_at:
                raise CleanupIncomplete(unresolved)
            next_read = (request.terminate_at - now if now < request.terminate_at
                         else (request.finish_at - now) / 2)
            time.sleep(min(config.program_sample_seconds, next_read))
        try:
            _ = process.wait(timeout=max(0, request.finish_at - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise CleanupIncomplete({process.pid}) from error
        check_deadline()
        program.cleanup.result = CleanupComplete(completed if completed is not None and not request.failed else self.status)

    def finish(self, *, failed: bool = False) -> None:
        if not isinstance(self.program, StartedProgram):
            return
        program = self.program
        config = memory_admit.read_memory_config()
        request = program.cleanup.request
        if not isinstance(request, CleanupRequest):
            terminate_at = time.monotonic() + config.program_stop_seconds
            request = CleanupRequest(terminate_at, terminate_at + config.program_stop_seconds, failed)
            program.cleanup.request = request
        finish_at = request.finish_at
        program.stopped.set()
        done = program.cleanup.completed.wait(timeout=max(0, finish_at - time.monotonic()))
        program.sampler.join(timeout=max(0, finish_at - time.monotonic()))
        result = program.cleanup.result
        if not done or program.sampler.is_alive() or not isinstance(result, CleanupComplete):
            raise CleanupIncomplete(set(result.unresolved_pids) or {program.process.pid}
                                    if isinstance(result, CleanupPending) else {program.process.pid})
        self.status = result.status


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
    root = memory_admit.store.root()
    pid = os.getpid()
    previous_need = 0
    previous_reasons: tuple[str, ...] = ()
    reported_at = 0.0
    temporary = Path(tempfile.mkdtemp(prefix="queue-place-"))
    cleanup_incomplete = False
    try:
        sidecar = temporary / "program.cgroup"
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
                            try:
                                place.finish(failed=failed)
                            except CleanupIncomplete:
                                cleanup_incomplete = True
                                raise
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
                    if not cleanup_incomplete:
                        for path, row in memory_admit.live_reservations(memory_admit.admission_dir(root)):
                            if row["pid"] == pid and row["sidecar"] == str(sidecar):
                                memory_admit.release(path, 1, root=root)
                finally:
                    for number, handler in handlers.items():
                        _ = signal.signal(number, handler)

    finally:
        if not cleanup_incomplete:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--scope-start":
        scope_start(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        raise SystemExit("queue_place supports only internal scoped program startup")
