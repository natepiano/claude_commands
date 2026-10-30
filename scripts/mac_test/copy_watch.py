#!/usr/bin/env python3
"""Run a Mac copy of a natedev test that is still compiling; the first result wins.

verify.sh run_local_test starts this beside a natedev nextest step that may
still leave for the Mac. It reads the step's process group and log from the
file run() writes, and registers the call as a copy candidate while cargo
compiles and nextest has not started its tests. When the Mac is free counting
copies as busy, the repository's newest candidate starts `offload.py run
--copy`. A Mac failure or pass stops the natedev step; any other copy result
leaves natedev to finish. The result file tells verify.sh which finished
first.
"""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import json
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
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from ..lib import process_start
    from ..lib.atomic_file import write_text_atomic
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start
    from scripts.lib.atomic_file import write_text_atomic


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import override_guard
import mac_test
import offload


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


@dataclass(frozen=True)
class StepInfo:
    """The natedev step attempt run() last started."""

    pgid: int
    log: Path


@dataclass(frozen=True)
class NoStepInfo:
    """run() has not started the step yet."""


NO_STEP_INFO = NoStepInfo()


class CandidateRecord(TypedDict):
    pid: int
    proc_start: str
    call_started: float
    call_id: str


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
        self.leader_start: process_start.ProcessStart | process_start.ProcessGone | process_start.ProcessReadError | NoStepInfo = NO_STEP_INFO
        if isinstance(info, StepInfo):
            try:
                stat = process_start.read_stat(info.pgid)
                self.leader_start = stat if isinstance(stat, process_start.ProcessGone) else stat.start
            except process_start.ProcessReadError as error:
                self.leader_start = error
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
        """Only a matching group leader permits stopping this step's group."""
        if isinstance(self.info, NoStepInfo) or not isinstance(self.leader_start, (process_start.ProcessStartTime, process_start.ProcessStartDate)):
            return False
        if process_start.check_group_leader(self.info.pgid, self.leader_start) is not process_start.OwnerCheck.ALIVE:
            return False
        try:
            return os.getpgid(self.info.pgid) == self.info.pgid
        except OSError:
            return False


def read_candidate(path: Path) -> CandidateRecord | None:
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    values = cast(dict[str, object], decoded)
    pid, proc_start = values.get("pid"), values.get("proc_start")
    call_started, call_id = values.get("call_started"), values.get("call_id")
    if not (
        isinstance(pid, int)
        and isinstance(proc_start, str)
        and isinstance(call_started, (int, float))
        and isinstance(call_id, str)
    ):
        return None
    return {"pid": pid, "proc_start": proc_start, "call_started": float(call_started), "call_id": call_id}


def newest_candidate(directory: Path) -> str | None:
    """The newest candidate whose owner is not proven stale (start, then call id).

    Remove proven stale records. Keep records with no start identity.
    """
    newest: CandidateRecord | None = None
    for path in sorted(directory.glob("*.json")):
        record = read_candidate(path)
        if record is None:
            continue
        if mac_test.check_recorded_owner(record["pid"], record["proc_start"]) in (
            process_start.OwnerCheck.GONE, process_start.OwnerCheck.REPLACED
        ):
            path.unlink(missing_ok=True)
            continue
        if newest is None or (record["call_started"], record["call_id"]) > (
            newest["call_started"],
            newest["call_id"],
        ):
            newest = record
    return newest["call_id"] if newest is not None else None


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


def start_copy(request: WatchRequest, result: Path) -> RunningCopy:
    result.unlink(missing_ok=True)
    command = [
        sys.executable, str(request.runner), "run", "--copy",
        "--repo-root", str(request.repo_root),
        "--package", request.package,
        "--call-id", request.call_id,
        "--settings", str(request.settings),
        "--result", str(result),
    ]
    if request.filter_run:
        command.append("--filter-run")
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
    """A stop flag set by TERM, HUP or INT, and a sleep any signal ends at once."""

    def __init__(self, read_fd: int) -> None:
        self.read_fd: int = read_fd
        self.stop_requested: bool = False

    def request_stop(self, signal_number: int, frame: FrameType | None) -> None:
        _ = (signal_number, frame)
        self.stop_requested = True

    def sleep(self, seconds: float) -> None:
        if self.stop_requested:
            return
        readable, _, _ = select.select([self.read_fd], [], [], seconds)
        if readable:
            try:
                while os.read(self.read_fd, 512):
                    pass
            except BlockingIOError:
                pass


def note_child(signal_number: int, frame: FrameType | None) -> None:
    """A copy ended; the wakeup file ends the sleep so its result is read at once."""
    _ = (signal_number, frame)


@contextmanager
def wakeup_on_signals() -> Generator[Wakeup, None, None]:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    wakeup = Wakeup(read_fd)
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


class Watch:
    """One call's copy state across ticks."""

    def __init__(self, request: WatchRequest, repository: offload.KnownRepository, work: Path) -> None:
        self.request: WatchRequest = request
        self.work: Path = work
        self.candidate: Path = (
            mac_test.state_paths().directory / "copy_candidates" / repository.name / f"{request.call_id}.json"
        )
        self.step: StepProgress = StepProgress(NO_STEP_INFO)
        self.copy: RunningCopy | NoCopy = NO_COPY
        self.outcome: CopyOutcome | Literal[""] = ""
        self.final: bool = False
        self.copy_count: int = 0
        self.copy_seconds: float = 0.0
        self.poll_s: float = poll_seconds()
        self.next_check: float = 0.0

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
        result = copy_result(read_mac_words(copy.result), status)
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
                os.killpg(self.step.info.pgid, signal.SIGTERM)
            except OSError:
                stopped.unlink(missing_ok=True)
            else:
                self.outcome = "won"
                return self.report(win)
        self.outcome = "natedev_won"
        return self.report()

    def update_candidate(self) -> bool:
        """Keep the candidate file in step with the call; true while it is a candidate."""
        wanted = not self.final and isinstance(self.copy, NoCopy) and self.step.compiling
        if not wanted:
            self.candidate.unlink(missing_ok=True)
        elif not self.candidate.exists():
            try:
                watcher_start = process_start.process_start(os.getpid())
            except process_start.ProcessReadError:
                return False
            if isinstance(watcher_start, process_start.ProcessGone):
                return False
            record: CandidateRecord = {
                "pid": os.getpid(),
                "proc_start": watcher_start.text,
                "call_started": self.request.call_started,
                "call_id": self.request.call_id,
            }
            # Candidate state is ours to create; the result folder belongs to
            # verify.sh and must never be recreated after it has been retired.
            self.candidate.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self.candidate, json.dumps(record) + "\n")
        return wanted

    def try_copy(self) -> None:
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + self.poll_s
        settings, repo_root = self.request.settings, self.request.repo_root
        if mac_test.free_now(mac_test.state_paths(), settings, repo_root, copy=True) != "free":
            return
        if newest_candidate(self.candidate.parent) != self.request.call_id:
            return
        self.candidate.unlink(missing_ok=True)
        self.copy = start_copy(self.request, self.work / "copy.result")
        self.copy_count += 1

    def run(self, wakeup: Wakeup) -> WatchReport:
        tick = min(1.0, self.poll_s)
        while not wakeup.stop_requested:
            self.refresh_step()
            if isinstance(self.copy, RunningCopy):
                win = self.finish_copy(self.copy)
                if win is not None and not wakeup.stop_requested:
                    return self.take_win(win)
                if win is not None:
                    self.outcome = "natedev_won"
            if self.update_candidate():
                self.try_copy()
            wakeup.sleep(tick)
        return self.stop()

    def stop(self) -> WatchReport:
        """verify.sh's TERM: natedev finished, so a running copy stops.

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
        self.candidate.unlink(missing_ok=True)
        if isinstance(self.copy, RunningCopy):
            stop_copy(self.copy)


def wait_for_stop(wakeup: Wakeup) -> WatchReport:
    """A repository the Mac cannot name is never a candidate: wait for verify's TERM."""
    while not wakeup.stop_requested:
        wakeup.sleep(60.0)
    return WatchReport("", 0.0, 0, "")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    for name in ("--info", "--result", "--runner", "--settings", "--repo-root", "--package", "--call-id",
                 "--call-started"):
        _ = root.add_argument(name, required=True)
    _ = root.add_argument("--filter-run", action="store_true")
    _ = root.add_argument("nextest_words", nargs=argparse.REMAINDER)
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
    )


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/copy_watch.py")
    request = request_from_arguments(parser().parse_args(namespace=CliArguments()))
    with wakeup_on_signals() as wakeup, tempfile.TemporaryDirectory(prefix="copy-watch.") as work:
        repository = offload.repository_identity(request.repo_root)
        if isinstance(repository, offload.UnknownRepository):
            report = wait_for_stop(wakeup)
        else:
            watch = Watch(request, repository, Path(work))
            try:
                report = watch.run(wakeup)
            finally:
                watch.close()
        try:
            write_text_atomic(request.result, result_text(report), create_parent=False)
        except OSError as error:
            print(f"copy_watch.py: cannot publish result {request.result}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
