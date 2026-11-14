#!/usr/bin/env python3
"""Request queue placement for a copy while a local test still compiles.

The queue selects the destination and the newest eligible candidate.
The watcher collects results and stops the losing execution.
The offload adapter owns the exact provider claim and uncertain cleanup.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from types import FrameType
from typing import Literal


from scripts.lib import process_group
from scripts.build_hold import override_guard
from scripts.mac_test import claim_state, eligibility
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.lib import build_folders, process_start as process_identity
from scripts.direct import step_record
from scripts.build_queue import placement, admission_protocol as wire


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CARGO_LINE = re.compile(
    r"^\s*(Compiling|Blocking|Finished|Fresh|Checking|Updating|Downloading|Downloaded|Locking)\b"
)
TESTS_LINE = re.compile(r"^\s*Starting \d+ tests?\b")
# A copy declined for one of these reasons would be declined again.
FINAL_DECLINE_REASONS = frozenset({"no_tests", "off", "repo", "linux_only"})
COPY_STOP_TIMEOUT_S = 60.0
STOPPED_BY_TERM = 128 + signal.SIGTERM

CopyOutcome = Literal["won", "natedev_won", "gave_way", "declined", "lost"]
LineKind = Literal["cargo", "tests", "other"]


class CliArguments(argparse.Namespace):
    """Arguments accepted by the copy watcher."""

    def __init__(self) -> None:
        super().__init__()
        self.info: str = ""
        self.result: str = ""
        self.runner: str = ""
        self.settings: str = ""
        self.repo_root: str = ""
        self.package: str = ""
        self.call_id: str = ""
        self.call_started: str = ""
        self.parent_pid: int = 0
        self.parent_episode: str = ""
        self.session: str = ""
        self.filter_run: bool = False
        self.nextest_words: list[str] = []


@dataclass(frozen=True)
class WatchRequest:
    """One natedev call that may get a Mac copy."""

    info: Path
    result: Path
    runner: Path
    settings: Path
    repo_root: Path
    package: str
    call_id: str
    call_started: float
    filter_run: bool
    nextest_words: tuple[str, ...]
    parent_episode: str = ""
    session: claim_state.SubmittingSession = field(default_factory=lambda: NoOwningSession())


@dataclass(frozen=True)
class StepInfo:
    """The natedev step attempt run() last started."""

    pgid: int
    log: Path


@dataclass(frozen=True)
class NoStepInfo:
    """run() has not started the step yet."""


NO_STEP_INFO = NoStepInfo()


@dataclass(frozen=True)
class RunningCopy:
    process: subprocess.Popen[bytes]
    result: Path
    started: float


@dataclass(frozen=True)
class NoCopy:
    """No copy runs for this call now."""


NO_COPY = NoCopy()


@dataclass(frozen=True)
class MacWords:
    """The words a runner wrote to its result file."""

    mac: str
    reason: str
    seconds: str


@dataclass(frozen=True)
class CopyWin:
    """A copy result that ends the call: a failure or a pass."""

    words: MacWords
    runner_status: int


@dataclass(frozen=True)
class CopyEnded:
    """A copy result that leaves natedev to finish; final when no copy may follow."""

    outcome: CopyOutcome
    final: bool


@dataclass(frozen=True)
class NoWin:
    pass


NO_WIN = NoWin()


@dataclass(frozen=True)
class WatchReport:
    outcome: CopyOutcome | Literal[""]
    copy_seconds: float
    copy_count: int
    natedev_seconds: str
    win: CopyWin | NoWin = NO_WIN


def line_kind(line: str) -> LineKind:
    plain = ANSI_ESCAPE.sub("", line)
    if TESTS_LINE.match(plain):
        return "tests"
    if CARGO_LINE.match(plain):
        return "cargo"
    return "other"


def read_step_info(path: Path) -> StepInfo | NoStepInfo:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return NO_STEP_INFO
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    try:
        pgid = int(values.get("pgid", ""))
    except ValueError:
        return NO_STEP_INFO
    log = values.get("log", "")
    return StepInfo(pgid, Path(log)) if pgid > 0 and log else NO_STEP_INFO


class StepProgress:
    """What one natedev step attempt's log has shown so far."""

    def __init__(self, info: StepInfo | NoStepInfo) -> None:
        self.info: StepInfo | NoStepInfo = info
        self.leader_start: process_identity.ProcessStart = (
            process_identity.process_start(info.pgid) if isinstance(info, StepInfo) else process_identity.Gone()
        )
        self.offset: int = 0
        self.pending: bytes = b""
        self.cargo_started: float | None = None
        self.tests_started: bool = False

    @property
    def compiling(self) -> bool:
        return self.cargo_started is not None and not self.tests_started

    def read(self) -> None:
        if isinstance(self.info, NoStepInfo):
            return
        try:
            with self.info.log.open("rb") as log:
                _ = log.seek(self.offset)
                chunk = log.read()
        except OSError:
            return
        self.offset += len(chunk)
        lines = re.split(rb"[\r\n]", self.pending + chunk)
        self.pending = lines.pop()
        for raw in lines:
            kind = line_kind(raw.decode("utf-8", errors="replace"))
            if kind == "tests":
                self.tests_started = True
            elif kind == "cargo" and self.cargo_started is None:
                self.cargo_started = time.monotonic()

    def leader_alive(self) -> bool:
        """The step's group leader still runs: not reaped, not a zombie, not a reused pid."""
        if isinstance(self.info, NoStepInfo) or not isinstance(self.leader_start, process_identity.Started):
            return False
        pgid = self.info.pgid
        if process_identity.process_start(pgid) != self.leader_start:
            return False
        try:
            return os.getpgid(pgid) == pgid
        except OSError:
            return False


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        temporary = Path(output.name)
        _ = output.write(text)
    os.replace(temporary, path)


def read_mac_words(path: Path) -> MacWords | None:
    """The runner's result words, read by the rules try_mac_test uses."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    macs = [line.removeprefix("mac=") for line in lines if line.startswith("mac=")]
    if len(macs) != 1 or macs[0] not in ("passed", "failed", "lost", "declined"):
        return None
    reason = seconds = ""
    for line in lines:
        if line.startswith("reason="):
            reason = line.removeprefix("reason=")
        elif line.startswith("seconds="):
            seconds = line.removeprefix("seconds=")
    return MacWords(macs[0], reason, seconds)


def copy_result(words: MacWords | None, status: int) -> CopyWin | CopyEnded:
    """What a copy that ended without the watcher stopping it means for the call."""
    if status == STOPPED_BY_TERM:
        # Only a new test's claim stops a copy this watcher did not stop.
        return CopyEnded("gave_way", final=False)
    if words is None or (words.mac == "passed" and status != 0) or (words.mac == "failed" and status == 0):
        return CopyEnded("lost", final=False)
    if words.mac in ("failed", "passed"):
        return CopyWin(words, status)
    if words.mac == "declined":
        return CopyEnded("declined", final=words.reason in FINAL_DECLINE_REASONS)
    return CopyEnded("lost", final=False)


def start_copy(request: WatchRequest, result: Path, assignment_file: Path) -> RunningCopy:
    result.unlink(missing_ok=True)
    command = [
        str(Path(__file__).resolve().parents[1] / "lib/py"), str(request.runner), "run", "--copy",
        "--repo-root", str(request.repo_root),
        "--package", request.package,
        "--call-id", request.call_id,
        "--settings", str(request.settings),
        "--result", str(result),
        "--assignment-file", str(assignment_file),
    ]
    if request.filter_run:
        command.append("--filter-run")
    for name in os.environ.get("VERIFY_TEST_ENV_NAMES", "").split():
        command.extend(("--env", name))
    command += ["--", *request.nextest_words]
    return RunningCopy(subprocess.Popen(command, stdin=subprocess.DEVNULL), result, time.monotonic())


def stop_copy(copy: RunningCopy) -> None:
    if copy.process.poll() is None:
        try:
            copy.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            _ = copy.process.wait(timeout=COPY_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            copy.process.kill()
            _ = copy.process.wait()


def result_text(report: WatchReport) -> str:
    started = report.copy_count > 0
    lines = [
        f"copy={report.outcome if started else ''}",
        f"copy_s={report.copy_seconds:.3f}" if started else "copy_s=",
        f"copy_count={report.copy_count}" if started else "copy_count=",
        f"natedev_s_at_end={report.natedev_seconds if started else ''}",
    ]
    if isinstance(report.win, CopyWin):
        words = report.win.words
        lines += [
            f"mac={words.mac}",
            f"reason={words.reason}",
            f"seconds={words.seconds}",
            f"runner_status={report.win.runner_status}",
            "stopped_natedev=1",
        ]
    return "".join(f"{line}\n" for line in lines)


def poll_seconds() -> float:
    try:
        seconds = float(os.environ.get("BUILD_TARGETS_POLL_S", "15"))
    except ValueError:
        return 15.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 15.0


class Wakeup:
    """Stop on TERM, HUP, INT or parent loss; signals end sleep at once."""

    def __init__(self, read_fd: int, parent_pid: int) -> None:
        self.read_fd: int = read_fd
        self.parent_pid: int = parent_pid
        self.stopped: bool = False

    @property
    def stop_requested(self) -> bool:
        if os.getppid() != self.parent_pid:
            self.stopped = True
        return self.stopped

    def request_stop(self, signal_number: int, frame: FrameType | None) -> None:
        _ = (signal_number, frame)
        self.stopped = True

    def sleep(self, seconds: float, *, readiness: tuple[int, ...] = ()) -> bool:
        if self.stop_requested:
            return False
        # Parent loss sends no signal; neither watch loop may sleep past it.
        readable, _, _ = select.select([self.read_fd, *readiness], [], [], min(seconds, 1.0))
        if self.read_fd in readable:
            try:
                while os.read(self.read_fd, 512):
                    pass
            except BlockingIOError:
                pass
        return any(descriptor in readiness for descriptor in readable)


def note_child(signal_number: int, frame: FrameType | None) -> None:
    """A copy ended; the wakeup file ends the sleep so its result is read at once."""
    _ = (signal_number, frame)


@contextmanager
def wakeup_on_signals(parent_pid: int) -> Generator[Wakeup, None, None]:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    wakeup = Wakeup(read_fd, parent_pid)
    names = [signal.SIGTERM, signal.SIGHUP]
    if signal.getsignal(signal.SIGINT) != signal.SIG_IGN:
        names.append(signal.SIGINT)
    previous_fd = signal.set_wakeup_fd(write_fd)
    previous = {name: signal.getsignal(name) for name in [*names, signal.SIGCHLD]}
    for name in names:
        _ = signal.signal(name, wakeup.request_stop)
    _ = signal.signal(signal.SIGCHLD, note_child)
    try:
        yield wakeup
    finally:
        for name, handler in previous.items():
            _ = signal.signal(name, handler)
        _ = signal.set_wakeup_fd(previous_fd)
        os.close(read_fd)
        os.close(write_fd)


@dataclass(frozen=True)
class NoCopyPlacement:
    """This watcher has no submitted speculative candidate."""


@dataclass(frozen=True)
class QueuedCopyPlacement:
    request: wire.PlacementRequest


@dataclass(frozen=True)
class WaitingCopyAssignment(QueuedCopyPlacement):
    """The queue must assign this submitted copy before it can start."""



class Watch:
    """One call's copy state across ticks."""

    def __init__(self, request: WatchRequest, repository: eligibility.KnownRepository, work: Path) -> None:
        self.request: WatchRequest = request
        self.repository: eligibility.KnownRepository = repository
        self.work: Path = work
        self.step: StepProgress = StepProgress(NO_STEP_INFO)
        self.copy: RunningCopy | NoCopy = NO_COPY
        self.outcome: CopyOutcome | Literal[""] = ""
        self.final: bool = False
        self.copy_count: int = 0
        self.copy_seconds: float = 0.0
        self.poll_s: float = poll_seconds()
        self.next_check: float = 0.0
        self.placement: NoCopyPlacement | QueuedCopyPlacement = NoCopyPlacement()
        self.episode_number: int = 0

    def report(self, win: CopyWin | NoWin = NO_WIN) -> WatchReport:
        started = self.step.cargo_started
        natedev = "" if started is None else f"{max(0.0, time.monotonic() - started):.3f}"
        return WatchReport(self.outcome, self.copy_seconds, self.copy_count, natedev, win)

    def refresh_step(self) -> None:
        info = read_step_info(self.request.info)
        if isinstance(info, StepInfo) and (
            isinstance(self.step.info, NoStepInfo) or info.pgid != self.step.info.pgid
        ):
            self.step = StepProgress(info)
        self.step.read()

    def finish_copy(self, copy: RunningCopy) -> CopyWin | None:
        status = copy.process.poll()
        if status is None:
            return None
        self.copy_seconds += time.monotonic() - copy.started
        self.copy = NO_COPY
        words = read_mac_words(copy.result)
        result = copy_result(words, status)
        if words is not None and words.reason == "source_changed" and isinstance(self.placement, QueuedCopyPlacement):
            from scripts.build_queue import admission_client

            submitted = self.placement.request
            _ = admission_client.cancel_placement(wire.CancelPlacement(
                admission_client.request_id(), submitted.episode, submitted.owner, "copy source changed"))
            self.placement = NoCopyPlacement()
        copy.result.unlink(missing_ok=True)
        if isinstance(result, CopyWin):
            return result
        self.outcome = result.outcome
        self.final = result.final
        self.next_check = time.monotonic() + self.poll_s
        return None

    def take_win(self, win: CopyWin) -> WatchReport:
        """Stop the natedev step for a copy win; a step already gone keeps its result."""
        if isinstance(self.step.info, StepInfo) and self.step.leader_alive():
            stopped = Path(f"{self.request.info}.stopped")
            stopped.touch()
            try:
                process_group.send_process_group(self.step.info.pgid, signal.SIGTERM)
            except OSError:
                stopped.unlink(missing_ok=True)
            else:
                self.outcome = "won"
                return self.report(win)
        self.outcome = "natedev_won"
        return self.report()

    def update_candidate(self) -> bool:
        """Submit one speculative episode while its local parent compiles."""
        from scripts.build_queue import admission_client
        from scripts.build_targets import build_targets
        from scripts.direct.step_selection import Pinned
        from scripts.buildlog import treekey, store

        if isinstance(self.copy, RunningCopy):
            return False
        wanted = not self.final and self.step.compiling
        live_step = step_record.read(self.request.call_id)
        if isinstance(live_step, step_record.StepRecord):
            wanted = wanted and step_record.mac_eligible(live_step) and live_step.state not in (
                step_record.StepState.ON_SSH, step_record.StepState.ON_MAC)
        elif not isinstance(live_step, step_record.MissingRecord):
            wanted = False
        if not wanted:
            if isinstance(self.placement, QueuedCopyPlacement):
                submitted = self.placement.request
                _ = admission_client.cancel_placement(wire.CancelPlacement(
                    admission_client.request_id(), submitted.episode, submitted.owner, "copy candidate ended"))
                self.placement = NoCopyPlacement()
            return False
        if isinstance(self.placement, NoCopyPlacement):
            identity = process_identity.process_start(os.getpid())
            measured_key = treekey.tree_key(str(self.request.repo_root))
            if not isinstance(identity, process_identity.Started):
                return False
            source_key = (placement.SourceKeyKnown(measured_key)
                          if measured_key is not None else placement.SourceKeyUnavailable(f"the source tree key for {self.repository.name} is unavailable"))
            self.episode_number += 1
            episode = f"{self.request.call_id}-copy-{os.getpid()}-{self.episode_number}"
            argv = ("cargo", "nextest", "run", *self.request.nextest_words)
            now = time.time()
            local = admission_client.step_request(list(argv), str(self.request.repo_root),
                wire.Owner(os.getpid(), identity.start), now, "", episode)
            constraints = build_targets.fixed_placement_constraints(self.request.repo_root, self.request.package,
                "test", argv, self.request.session, source_key, selection=Pinned("mac"))
            constraints = replace(constraints, candidates=tuple(candidate for candidate in constraints.candidates
                                                               if candidate.destination.provider != "local"))
            request = wire.PlacementRequest(episode,
                placement.SubmittingOwner(store.host_name(), os.getpid(), identity.start), now,
                local.profile, constraints, identity=local.identity, run=local.run,
                prerequisite=placement.TokenNotRequired(),
                purpose=placement.SpeculativeCopy(self.request.parent_episode, self.request.call_started))
            self.placement = QueuedCopyPlacement(request)
        return True

    def try_copy(self) -> None:
        from scripts.build_queue import admission_client

        now = time.monotonic()
        if now < self.next_check or not isinstance(self.placement, QueuedCopyPlacement):
            return
        self.next_check = now + self.poll_s
        answer = admission_client.request_placement(self.placement.request)
        self.placement = (WaitingCopyAssignment(self.placement.request)
                          if isinstance(answer, wire.PlacementWait) else QueuedCopyPlacement(self.placement.request))
        if not isinstance(answer, wire.Assignment):
            return
        assignment_file = self.work / "copy.assignment.json"
        admission_client.write_assignment(assignment_file, answer)
        self.copy = start_copy(self.request, self.work / "copy.result", assignment_file)
        self.copy_count += 1

    def run(self, wakeup: Wakeup) -> WatchReport:
        from scripts.buildlog import store
        from scripts.direct.local_release_wait import LocalReleaseEvents, episode_is_assigned, local_release_waiter

        tick = min(1.0, self.poll_s)
        state_file = store.root() / "admission/state.json"
        assignment_event = False
        with local_release_waiter(state_file.parent, state_file.parent, wake_files=(state_file,)) as events:
            while not wakeup.stop_requested:
                events.arm()
                if (assignment_event and isinstance(self.placement, WaitingCopyAssignment)
                        and episode_is_assigned(state_file, self.placement.request.episode)):
                    self.next_check = 0.0
                self.refresh_step()
                if isinstance(self.copy, RunningCopy):
                    win = self.finish_copy(self.copy)
                    if win is not None and not wakeup.stop_requested:
                        return self.take_win(win)
                    if win is not None:
                        self.outcome = "natedev_won"
                if self.update_candidate():
                    self.try_copy()
                readiness = (events.descriptor,) if isinstance(events, LocalReleaseEvents) else ()
                assignment_event = wakeup.sleep(tick, readiness=readiness)
                if assignment_event:
                    events.wait(0)
        return self.stop()

    def stop(self) -> WatchReport:
        """verify.sh ended or sent TERM, so a running copy stops.

        A copy that ended before the TERM keeps its outcome, except a win,
        which stopped nothing.
        """
        natedev_seconds = self.report().natedev_seconds
        if isinstance(self.copy, RunningCopy) and self.finish_copy(self.copy) is not None:
            self.outcome = "natedev_won"
        if isinstance(self.copy, RunningCopy):
            stop_copy(self.copy)
            self.copy_seconds += time.monotonic() - self.copy.started
            self.copy.result.unlink(missing_ok=True)
            self.copy = NO_COPY
            self.outcome = "natedev_won"
        return WatchReport(self.outcome, self.copy_seconds, self.copy_count, natedev_seconds)

    def close(self) -> None:
        from scripts.build_queue import admission_client

        if isinstance(self.copy, RunningCopy):
            stop_copy(self.copy)
        if isinstance(self.placement, QueuedCopyPlacement):
            submitted = self.placement.request
            try:
                _ = admission_client.cancel_placement(wire.CancelPlacement(
                    admission_client.request_id(), submitted.episode, submitted.owner, "copy watcher ended"))
            except wire.AdmissionUnavailable as error:
                print(f"copy_watch: cannot cancel placement: {error}", file=sys.stderr)


def wait_for_stop(wakeup: Wakeup) -> WatchReport:
    """An unknown repository is never a candidate: wait for verify to end."""
    while not wakeup.stop_requested:
        _ = wakeup.sleep(60.0)
    return WatchReport("", 0.0, 0, "")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    for name in ("--info", "--result", "--runner", "--settings", "--repo-root", "--package", "--call-id",
                 "--call-started"):
        _ = root.add_argument(name, required=True)
    _ = root.add_argument("--filter-run", action="store_true")
    _ = root.add_argument("--parent-pid", type=int, required=True)
    _ = root.add_argument("nextest_words", nargs=argparse.REMAINDER)
    _ = root.add_argument("--parent-episode", default="")
    _ = root.add_argument("--session", default="")
    return root


def epoch_seconds(value: str) -> float:
    # EPOCHREALTIME takes the locale's decimal point.
    try:
        seconds = float(value.replace(",", "."))
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) else 0.0


def request_from_arguments(arguments: CliArguments) -> WatchRequest:
    words = arguments.nextest_words
    if words[:1] == ["--"]:
        words = words[1:]
    return WatchRequest(
        info=Path(arguments.info),
        result=Path(arguments.result),
        runner=Path(arguments.runner),
        settings=Path(arguments.settings),
        repo_root=Path(arguments.repo_root).resolve(),
        package=arguments.package,
        call_id=arguments.call_id,
        call_started=epoch_seconds(arguments.call_started),
        filter_run=arguments.filter_run,
        nextest_words=tuple(words),
        parent_episode=arguments.parent_episode,
        session=OwningSession(arguments.session) if arguments.session else NoOwningSession(),
    )


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/copy_watch.py")
    arguments = parser().parse_args(namespace=CliArguments())
    request = request_from_arguments(arguments)
    with wakeup_on_signals(arguments.parent_pid) as wakeup, build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["copy-watch"])) as work:
        repository = eligibility.repository_identity(request.repo_root)
        if isinstance(repository, eligibility.UnknownRepository):
            report = wait_for_stop(wakeup)
        else:
            watch = Watch(request, repository, work)
            try:
                report = watch.run(wakeup)
            finally:
                watch.close()
        write_text_atomic(request.result, result_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
