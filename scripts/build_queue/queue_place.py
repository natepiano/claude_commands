#!/usr/bin/env python3
"""Choose admission from command arguments and manage the program scope."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, TypedDict, Unpack

from scripts.lib import build_folders, atomic_file, conf
from scripts.build_queue import admission_records
from scripts.build_queue import step_estimator
from scripts.lint import memory_admit
from scripts.build_queue import admission_client, admission_protocol as wire
import uuid


@dataclass(frozen=True)
class ConfiguredFirstPeak:
    pass


CONFIGURED_FIRST_PEAK = ConfiguredFirstPeak()


class ProgramPurpose(Enum):
    BATCH = auto()
    PROBE = auto()
    EDITOR_SERVER = auto()
    RUNNER = auto()


def program_purpose(argv: Sequence[str], *, batch: bool = False) -> ProgramPurpose:
    """Classify the complete command before admission or scope startup."""
    if not argv:
        raise QueuePlaceRefused("the program command is empty")
    if Path(argv[0]).name != "rust-analyzer":
        return ProgramPurpose.BATCH
    probes = conf.read("build_admission").words("program_probe_arguments.rust-analyzer")
    if len(argv) == 2 and argv[1] in probes:
        return ProgramPurpose.PROBE
    if not batch and len(argv) == 1:
        return ProgramPurpose.EDITOR_SERVER
    return ProgramPurpose.BATCH


@dataclass(frozen=True)
class UnreservedProgram:
    pass


@dataclass(frozen=True)
class UnmeasuredProbe:
    pass


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


def wait_reasons(result: admission_records.AdmissionWaitSnapshot) -> tuple[str, ...]:
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
    preexec_fn: Callable[[], None]


@dataclass(frozen=True)
class ProgramNotStarted:
    pass


@dataclass
class NoDescendantAdoption:
    pass


@dataclass
class ProgramTree:
    group: int
    adopter: wire.Owner | NoDescendantAdoption = field(default_factory=NoDescendantAdoption)
    observed: dict[int, str] = field(default_factory=dict[int, str])
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def live_members(self, scope_members: set[int]) -> dict[int, str]:
        """Retain observed descendants after a session change or reparenting."""
        with self.lock:
            processes = memory_admit.process_table(Path("/proc"))
            roots = dict(self.observed)
            if isinstance(self.adopter, wire.Owner):
                roots[self.adopter.pid] = self.adopter.start_time
            members = memory_admit.process_tree(roots, processes)
            members.update(scope_members)
            for pid in processes:
                try:
                    if os.getpgid(pid) == self.group:
                        members.add(pid)
                except ProcessLookupError:
                    continue
            if isinstance(self.adopter, wire.Owner):
                members.discard(self.adopter.pid)
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
    sampler: threading.Thread | UnmeasuredProbe
    tracking: ScopedProgram | ProgramTree


def scope_start(sidecar: Path, holder_pid: int, holder_start: str, argv: Sequence[str]) -> None:
    """Publish the scope and confirm its holder before the program starts."""
    if admission_records.process_start_time(holder_pid) != holder_start:
        raise QueuePlaceRefused("the queue place holder exited before scope startup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::") and "/builds.slice/" in line:
            scope = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            atomic_file.atomic_sample(sidecar, str(scope) + "\n")
            if admission_records.process_start_time(holder_pid) != holder_start:
                raise QueuePlaceRefused("the queue place holder exited during scope publication")
            os.execvp(argv[0], list(argv))
    raise QueuePlaceRefused("the program did not enter its memory scope")


def stop_scope(scope: Path, deadline: float, sample_seconds: float) -> None:
    """Stop scope members until the scope is empty."""
    while members := memory_admit.scope_members(scope):
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
            members = memory_admit.scope_members(Path(scope))
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
    configuration: admission_records.MemoryConfig | wire.RunnerExecutionTiming = field(kw_only=True, repr=False)
    purpose: ProgramPurpose = field(default=ProgramPurpose.BATCH, kw_only=True)
    arguments: tuple[str, ...] = field(default=(), kw_only=True)
    _samples: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def publish_sample(self, suffix: str, value: str) -> None:
        path = self.path.with_suffix(suffix)
        atomic_file.atomic_sample(path, value)
        if self.purpose == ProgramPurpose.RUNNER:
            path.chmod(0o644)

    def observe(self, peak_bytes: int, current_bytes: int) -> None:
        """Publish the existing peak and near-peak evidence in bytes."""
        with self._samples:
            now = datetime.now(UTC).timestamp()
            if peak_bytes > self.peak_bytes:
                self.publish_sample(".max", f"{peak_bytes}\n")
                self.peak_bytes = peak_bytes
                self.publish_sample(".wave", f"{peak_bytes} {int(now)}\n")
                self.wave_at = now
            elif self.peak_bytes and current_bytes >= self.peak_bytes / 2 and now - self.wave_at >= self.configuration.peak_wave_seconds:
                self.publish_sample(".wave", f"{self.peak_bytes} {int(now)}\n")
                self.wave_at = now

    def popen(self, argv: Sequence[str], **popen_arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]:
        if isinstance(self.program, StartedProgram):
            raise QueuePlaceRefused("a queue place can start only one program")
        if self.arguments and tuple(argv) != self.arguments:
            raise QueuePlaceRefused("the program arguments changed after the purpose decision")
        if not popen_arguments.get("start_new_session", True):
            raise QueuePlaceRefused("a queued program must start in its own process group")
        popen_arguments["start_new_session"] = True
        prefix: list[str] = []
        if self.purpose not in (ProgramPurpose.PROBE, ProgramPurpose.RUNNER) and os.environ.get("BUILDLOG_SCOPE") != "0":
            with redirect_stdout(sys.stderr):
                prefix = probe_scope_prefix()
            if not prefix:
                raise QueuePlaceRefused(
                    "the queued program requires a memory scope; set BUILDLOG_SCOPE=0 only for tests")
        holder_pid = os.getpid()
        holder_start = admission_records.process_start_time(holder_pid)
        if holder_start is None:
            raise QueuePlaceRefused("the queue place holder has no live process identity")
        command = ([*prefix, str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "--scope-start", str(self.sidecar),
                    str(holder_pid), holder_start, *argv]
                   if prefix else list(argv))
        with build_folders.defer_signals():
            process = subprocess.Popen(command, **popen_arguments)
            stopped = threading.Event()
            tracking: ScopedProgram | ProgramTree = ScopedProgram()
            if not prefix:
                tracking = ProgramTree(process.pid)
                if self.purpose == ProgramPurpose.RUNNER:
                    tracking.adopter = wire.Owner(holder_pid, holder_start)
                start = admission_records.process_start_time(process.pid)
                if start is not None:
                    tracking.observed[process.pid] = start
            sampler = (UnmeasuredProbe() if self.purpose == ProgramPurpose.PROBE else
                       threading.Thread(target=self._sample, args=(tracking, stopped), daemon=True))
            self.program = StartedProgram(process, stopped, sampler, tracking)
            if isinstance(sampler, threading.Thread):
                sampler.start()
            if prefix:
                while not self.sidecar.read_text().strip():
                    if process.poll() is not None and not self.sidecar.read_text().strip():
                        raise QueuePlaceRefused("the program exited before it published its memory scope")
                    time.sleep(self.configuration.program_sample_seconds)
            self.on_started()
        return process

    def _sample(self, tracking: ScopedProgram | ProgramTree, stopped: threading.Event) -> None:
        while True:
            try:
                scope = self.sidecar.read_text().strip()
                if scope:
                    reading = memory_admit.slice_working_set(Path(scope))
                    if isinstance(reading.anonymous, admission_records.AnonymousRunUse):
                        self.observe(reading.anonymous.bytes, reading.anonymous.bytes)
                elif isinstance(tracking, ProgramTree):
                    members = program_members(tracking, self.sidecar)
                    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
                    matching = set(members)
                    if proc != Path("/proc"):
                        processes = memory_admit.process_table(proc)
                        matching = {pid for pid, start in members.items()
                                    if pid in processes and processes[pid].start == start}
                    reading = memory_admit.process_memory(matching, proc)
                    if self.purpose == ProgramPurpose.RUNNER:
                        if isinstance(reading.anonymous, admission_records.AnonymousRunUse):
                            self.observe(reading.anonymous.bytes, reading.anonymous.bytes)
                    else:
                        self.observe(reading.bytes, reading.bytes)
            except (OSError, ValueError, StopIteration):
                pass  # A process or scope can disappear between sampling reads.
            if stopped.wait(self.configuration.program_sample_seconds):
                return

    def finish(self, *, failed: bool = False) -> None:
        if not isinstance(self.program, StartedProgram):
            return
        program = self.program
        process = program.process
        try:
            completed = process.poll()
            config = self.configuration
            deadline = time.monotonic() + config.program_stop_seconds
            if isinstance(program.tracking, ScopedProgram):
                stop_scope(Path(self.sidecar.read_text().strip()), deadline, config.program_sample_seconds)
            else:
                members = program_members(program.tracking, self.sidecar)
                while members:
                    number = signal.SIGKILL if time.monotonic() >= deadline else signal.SIGTERM
                    for pid, start in members.items():
                        try:
                            if admission_records.process_start_time(pid) == start:
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
            if isinstance(program.sampler, threading.Thread):
                program.sampler.join()


def _stop_holder(number: int, _frame: object) -> None:
    raise SystemExit(128 + number)


@contextmanager
def queue_place(program: str | Sequence[str], repo: Path, *, batch: bool = False,
                sizing: admission_records.HistoryPeakSizing | ConfiguredFirstPeak = CONFIGURED_FIRST_PEAK,
                before_check: Callable[[], None] = no_hold_wait) -> Generator[QueuePlace]:
    """Use full commands for purpose selection; retain named batch reservations."""
    # A name requests a batch place. A complete command permits purpose selection.
    try:
        if isinstance(program, str):
            arguments: tuple[str, ...] = ()
            name, purpose = program, ProgramPurpose.BATCH
        else:
            arguments = tuple(program)
            purpose = program_purpose(arguments, batch=batch)
            name = Path(program[0]).name
        config = step_estimator.memory_config()
    except conf.Unusable as error:
        raise QueuePlaceRefused(str(error)) from error
    if purpose == ProgramPurpose.BATCH and isinstance(sizing, ConfiguredFirstPeak):
        first = config.program_first_peaks.get(name)
        if first is None:
            raise QueuePlaceRefused(f"no first peak for {name} in build_admission.conf [memory]; {conf.path('build_admission')}: program_first_peak_gib.{name} is missing or invalid")
        sizing = admission_records.HistoryPeakSizing(first, admission_records.FirstProgramPeak(first), 0.0)
    pid = os.getpid()
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["queue-place"])) as temporary:
        sidecar = temporary / "program.cgroup"
        _ = sidecar.write_text("")
        handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        for number, handler in handlers.items():
            if handler in (signal.SIG_DFL, signal.default_int_handler):
                _ = signal.signal(number, _stop_holder)
        try:
            grant: wire.Admitted | UnreservedProgram = UnreservedProgram()
            if purpose == ProgramPurpose.BATCH:
                assert not isinstance(sizing, ConfiguredFirstPeak)
                profile = admission_records.ProgramProfile(name, str(Path.home() / ".claude"), str(repo.resolve()), sizing)
                start = admission_records.process_start_time(pid)
                if start is None:
                    raise QueuePlaceRefused("owner no longer exists")
                owner, episode, since = wire.Owner(pid, start), uuid.uuid4().hex, time.time()
                def refresh() -> wire.AdmitRequest:
                    return admission_client.step_request(list(arguments) or [name], str(repo), owner, since, str(sidecar), episode, profile)
                output = admission_client.WaitOutput()
                reply = admission_client.admit(refresh(), output, refresh, _attempt=before_check)
                if isinstance(reply, wire.Refused):
                    raise QueuePlaceRefused(reply.reason)
                if isinstance(reply, admission_client.LeftQueue):
                    raise QueuePlaceRefused("left the admission queue")
                grant = reply
                output.finished()
            sample_path = Path(grant.max_path).with_suffix("") if isinstance(grant, wire.Admitted) else temporary / "program"
            place = QueuePlace(sample_path, sidecar, configuration=config, purpose=purpose, arguments=arguments)
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
                    if isinstance(grant, wire.Admitted):
                        _ = admission_client.release(grant.reservation, place.status, grant.cleanup_timing)
        finally:
            with defer_cleanup_signals():
                for number, handler in handlers.items():
                    _ = signal.signal(number, handler)


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--scope-start":
        scope_start(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        argv = sys.argv[1:]
        if argv[:1] == ["--"]:
            argv = argv[1:]
        try:
            with queue_place(argv, Path.cwd()) as place:
                status = place.popen(argv).wait()
            raise SystemExit(status)
        except QueuePlaceRefused as error:
            print(f"queue place: {error}", file=sys.stderr)
            raise SystemExit(1) from error
