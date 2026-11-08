"""Capture phase counters and anonymous memory in five-percent bins."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import cast

from scripts.lib.atomic_file import atomic_sample
from scripts.lib import conf
from scripts.buildlog import parse
from scripts.build_queue.snapshot_codec import parse_snapshot_value, snapshot_value


@dataclass(frozen=True)
class NoCounter:
    reason: str = "no current counter"


@dataclass(frozen=True)
class ProgressCounter:
    phase: str
    done: int
    total: int

    def __post_init__(self) -> None:
        if not self.phase or self.total <= 0 or not 0 <= self.done <= self.total:
            raise ValueError("invalid progress counter")

    @property
    def percent(self) -> float:
        return 100 * self.done / self.total


@dataclass(frozen=True)
class NoMatchingProfile:
    reason: str = "no matching anonymous profile"


@dataclass(frozen=True)
class UnmeasuredBin:
    pass


@dataclass(frozen=True)
class ConfiguredProfileRank:
    pass


CONFIGURED_PROFILE_RANK = ConfiguredProfileRank()


def profile_rank(rank: int | ConfiguredProfileRank = CONFIGURED_PROFILE_RANK) -> int:
    return conf.read("build_admission").whole("progress_profile_rank") if isinstance(rank, ConfiguredProfileRank) else rank


@dataclass(frozen=True)
class ScopeReading:
    directory: Path


@dataclass(frozen=True)
class ProcessTreeReading:
    pids: tuple[int, ...]


@dataclass(frozen=True)
class ReadingSourceNotRecorded:
    reason: str = "reading source not recorded"


ReadingSource = ScopeReading | ProcessTreeReading | ReadingSourceNotRecorded


@dataclass(frozen=True)
class ProcessCommandUnavailable:
    reason: str


@dataclass(frozen=True)
class ProcessRssAnonUnavailable:
    reason: str


@dataclass(frozen=True)
class ProfileProcess:
    pid: int
    command: tuple[str, ...] | ProcessCommandUnavailable
    rss_anon: int | ProcessRssAnonUnavailable


@dataclass(frozen=True)
class PeakProcessesNotRecorded:
    reason: str = "process evidence not recorded"


@dataclass(frozen=True)
class PeakProcesses:
    phase: str
    bin: int
    anonymous_bytes: int
    processes: tuple[ProfileProcess, ...]


READING_SOURCE_NOT_RECORDED = ReadingSourceNotRecorded()
PEAK_PROCESSES_NOT_RECORDED = PeakProcessesNotRecorded()


def read_processes(pids: Iterable[int], proc: Path) -> tuple[ProfileProcess, ...]:
    """Read process evidence only when the caller records a new peak."""
    result: list[ProfileProcess] = []
    for pid in sorted(pids):
        directory = proc / str(pid)
        try:
            command: tuple[str, ...] | ProcessCommandUnavailable = tuple(
                part for part in (directory / "cmdline").read_bytes().decode(errors="replace").split("\0") if part)
        except OSError as error:
            command = ProcessCommandUnavailable(str(error))
        try:
            status = directory / "status"
            fields = next((line.split() for line in status.read_text().splitlines() if line.startswith("RssAnon:")), list[str]())
            rss_anon: int | ProcessRssAnonUnavailable = int(fields[1]) * 1024
        except (OSError, UnicodeError, ValueError, IndexError) as error:
            rss_anon = ProcessRssAnonUnavailable(str(error))
        result.append(ProfileProcess(pid, command, rss_anon))
    return tuple(result)


def _no_peak_processes() -> PeakProcessesNotRecorded:
    return PeakProcessesNotRecorded("process evidence not supplied")


@dataclass(frozen=True)
class PhaseProfile:
    phase: str
    bins: tuple[int | UnmeasuredBin, ...]

    def __post_init__(self) -> None:
        if len(self.bins) != BINS or any(isinstance(value, int) and value < 0 for value in self.bins):
            raise ValueError("invalid phase profile")


@dataclass(frozen=True)
class ProgressProfile:
    phases: tuple[PhaseProfile, ...]


@dataclass(frozen=True)
class WindowPeak:
    bytes: int
    lookahead_percent: float


BINS = 20
GATE_FIRST_PASS = "suites:1"
FRAME_SPLIT = re.compile(r"[\r\n]")
PHASE_END = re.compile(r"\bFinished\b|\bSummary\b|Blocking waiting for")
CARGO_COUNTER = re.compile(r"\b(?:Building|Checking|Compiling)\s+\[[^\]]*\]\s*(\d+)\s*/\s*(\d+)")
TEST_COUNTER = re.compile(r"\b(?:" + "|".join(sorted(parse.PASSED | parse.TIMED_OUT | {"FAIL"})) + r")\b.*?\(\s*(\d+)\s*/\s*(\d+)\s*\)")
TEST_START = re.compile(r"\bStarting\s+(\d+)\s+tests?\b")
TEST_BAR = re.compile(r"\bRunning\s+\[[^\]]*\].*?\b(\d+)\s*/\s*(\d+)\s*:")


@dataclass
class CounterParser:
    counts: dict[str, int] = field(default_factory=dict)
    current: ProgressCounter | NoCounter = field(default_factory=NoCounter)
    previous_kind: str = ""
    tail: str = ""

    def consume(self, text: str, *, complete: bool = True) -> ProgressCounter | NoCounter:
        frames = FRAME_SPLIT.split(self.tail + text)
        self.tail = "" if complete else frames.pop()[-65536:]
        for frame in frames:
            frame = parse.ANSI.sub("", frame)
            match = CARGO_COUNTER.search(frame)
            kind = "build"
            starting = False
            if match is None:
                kind = "test"
                match = TEST_COUNTER.search(frame) or TEST_BAR.search(frame)
            if match is None:
                match = TEST_START.search(frame)
                starting = True
            if match is None:
                if PHASE_END.search(frame):
                    self.current = NoCounter("phase ended or waiting")
                    self.previous_kind = ""
                continue
            done, total = ((0, int(match[1])) if starting else (int(match[1]), int(match[2])))
            if total <= 0 or done > total:
                self.current = NoCounter("invalid counter")
                continue
            if (kind != self.previous_kind or isinstance(self.current, ProgressCounter)
                    and (done < self.current.done or total != self.current.total)):
                self.counts[kind] = self.counts.get(kind, 0) + 1
            self.current = ProgressCounter(f"{kind}:{self.counts[kind]}", done, total)
            self.previous_kind = kind
        if not complete and self.tail:
            preview = CounterParser(self.counts.copy(), self.current, self.previous_kind)
            return preview.consume(self.tail)
        return self.current


def parse_progress(text: str) -> ProgressCounter | NoCounter:
    """Treat a reset or a return to compilation as a new phase."""
    return CounterParser().consume(text)


def window_peak(profile: ProgressProfile | NoMatchingProfile, counter: ProgressCounter | NoCounter,
                lookahead_percent: float) -> WindowPeak | NoMatchingProfile:
    """Cross cargo phases, stop after the gate first pass, and require measured bins."""
    if isinstance(profile, NoMatchingProfile):
        return profile
    if isinstance(counter, NoCounter):
        return NoMatchingProfile(counter.reason)
    if not math.isfinite(lookahead_percent) or not 0 < lookahead_percent <= 100:
        raise ValueError("invalid progress look-ahead")
    phases = [phase.phase for phase in profile.phases]
    if counter.phase not in phases:
        return NoMatchingProfile("phase reset or unmatched phase")
    remaining = lookahead_percent
    at = counter.percent
    peak = 0
    for phase in profile.phases[phases.index(counter.phase):]:
        end = min(100.0, at + remaining)
        first = bin_index(at)
        last = bin_index(end)
        for value in phase.bins[first:last + 1]:
            if isinstance(value, UnmeasuredBin):
                return NoMatchingProfile("unmeasured progress window")
            peak = max(peak, value)
        remaining -= end - at
        if remaining <= 0 or phase.phase == GATE_FIRST_PASS:
            break
        at = 0
    return WindowPeak(peak, lookahead_percent)


def bin_index(percent: float) -> int:
    return min(BINS - 1, int(percent * BINS // 100))


@dataclass
class ProfileRecorder:
    phases: dict[str, list[int | UnmeasuredBin]] = field(default_factory=dict)

    def observe(self, counter: ProgressCounter | NoCounter, anonymous_bytes: int) -> bool:
        if isinstance(counter, NoCounter):
            return False
        bins = self.phases.setdefault(counter.phase, [UnmeasuredBin()] * BINS)
        index = bin_index(counter.percent)
        previous = bins[index]
        if isinstance(previous, int) and previous >= anonymous_bytes:
            return False
        bins[index] = anonymous_bytes
        return True

    def profile(self) -> ProgressProfile:
        return ProgressProfile(tuple(PhaseProfile(name, tuple(bins)) for name, bins in self.phases.items()))


def merge_profiles(profiles: Iterable[ProgressProfile], rank: int | ConfiguredProfileRank = CONFIGURED_PROFILE_RANK
                   ) -> ProgressProfile | NoMatchingProfile:
    """Keep phase order and select the configured highest run in each bin."""
    selected_rank = profile_rank(rank)
    recorder = ProfileRecorder()
    samples: dict[tuple[str, int], list[int]] = {}
    before: dict[str, set[str]] = {}
    for profile in profiles:
        previous: list[str] = []
        run_samples: dict[tuple[str, int], int] = {}
        for phase in profile.phases:
            before.setdefault(phase.phase, set()).update(previous)
            previous.append(phase.phase)
            for index, value in enumerate(phase.bins):
                if isinstance(value, int):
                    key = (phase.phase, index)
                    run_samples[key] = max(value, run_samples.get(key, 0))
            _ = recorder.phases.setdefault(phase.phase, [UnmeasuredBin()] * BINS)
        for key, value in run_samples.items():
            samples.setdefault(key, []).append(value)
    if not before:
        return NoMatchingProfile()
    for (phase, index), values in samples.items():
        values.sort(reverse=True)
        recorder.phases[phase][index] = values[min(selected_rank, len(values)) - 1]
    for name, predecessors in before.items():
        kind, separator, number = name.rpartition(":")
        if separator and number.isdecimal():
            predecessors.update(other for other in before if other.rpartition(":")[0] == kind
                and other.rpartition(":")[2].isdecimal() and int(other.rpartition(":")[2]) < int(number))
    ordered: dict[str, list[int | UnmeasuredBin]] = {}
    while before:
        ready = sorted(name for name, predecessors in before.items() if predecessors <= ordered.keys())
        if not ready:
            return NoMatchingProfile("conflicting phase order")
        for name in ready:
            ordered[name] = recorder.phases[name]
            del before[name]
    recorder.phases = ordered
    return recorder.profile()


def profile_key(parts: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RecordedProfile:
    profile: ProgressProfile
    at: float
    reading_source: ReadingSource = ReadingSourceNotRecorded()
    peak_processes: PeakProcesses | PeakProcessesNotRecorded = PeakProcessesNotRecorded()


def save_profile(path: Path, profile: ProgressProfile, at: float, *,
                 reading_source: ReadingSource = READING_SOURCE_NOT_RECORDED,
                 peak_processes: PeakProcesses | PeakProcessesNotRecorded = PEAK_PROCESSES_NOT_RECORDED) -> None:
    """Publish attributed anonymous samples in the tagged record format."""
    atomic_sample(path, json.dumps({"measurement": "attributed_anonymous",
        "record": snapshot_value(RecordedProfile(profile, at, reading_source, peak_processes))}, separators=(",", ":")))


def read_profile_record(path: Path) -> RecordedProfile | NoMatchingProfile:
    try:
        raw = cast(object, json.loads(path.read_text()))
        if isinstance(raw, dict):
            envelope = cast(dict[str, object], raw)
            if envelope.get("measurement") == "attributed_anonymous":
                return cast(RecordedProfile, parse_snapshot_value(envelope.get("record"), RecordedProfile))
    except (OSError, ValueError, TypeError):
        pass
    return NoMatchingProfile()


def read_profile(path: Path, cutoff: float) -> ProgressProfile | NoMatchingProfile:
    record = read_profile_record(path)
    return record.profile if isinstance(record, RecordedProfile) and record.at >= cutoff else NoMatchingProfile()


def progress_path(sidecar: Path, suffix: str) -> Path:
    return sidecar.with_suffix(".progress." + suffix)


def publish_counter(sidecar: Path, counter: ProgressCounter) -> None:
    atomic_sample(progress_path(sidecar, "json"), json.dumps(snapshot_value(counter), separators=(",", ":")))


def clear_counter(sidecar: Path) -> None:
    progress_path(sidecar, "json").unlink(missing_ok=True)


def read_counter(sidecar: Path) -> ProgressCounter | NoCounter:
    try:
        return cast(ProgressCounter, parse_snapshot_value(
            cast(object, json.loads(progress_path(sidecar, "json").read_text())), ProgressCounter))
    except (OSError, ValueError, TypeError):
        return NoCounter()


@dataclass
class CaptureCursor:
    identity: tuple[int, int]
    offset: int = 0
    parser: CounterParser = field(default_factory=CounterParser)
    result: ProgressCounter | NoCounter = field(default_factory=NoCounter)


@dataclass
class CaptureReader:
    root: Path
    touched: set[Path] = field(default_factory=set)
    cursors: dict[Path, CaptureCursor] = field(default_factory=dict)

    def log_counter(self, path: Path) -> ProgressCounter | NoCounter:
        self.touched.add(path)
        try:
            with path.open("rb") as log:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                cursor = self.cursors.get(path)
                if cursor is None or cursor.identity != identity or stat.st_size < cursor.offset:
                    cursor = CaptureCursor(identity)
                    self.cursors[path] = cursor
                _ = log.seek(cursor.offset)
                data = log.read(65536)
                cursor.offset += len(data)
                if data:
                    cursor.result = cursor.parser.consume(data.decode(errors="replace"), complete=False)
            return cursor.result if cursor.offset >= stat.st_size else NoCounter("capture replay incomplete")
        except OSError:
            return NoCounter("capture unavailable")

    def counter(self, identities: dict[int, str], boot_id: str) -> ProgressCounter | NoCounter:
        """Read bounded capture increments after checking process birth identities."""
        found: list[ProgressCounter] = []
        try:
            accounts = tuple(self.root.iterdir())
        except OSError:
            return NoCounter()
        for account in accounts:
            if not account.name.isdecimal():
                continue
            try:
                registrations = tuple((account / "state/pids").iterdir())
            except OSError:
                continue
            for path in registrations:
                pid_text, separator, generation = path.name.partition(".")
                if not separator or not pid_text.isdecimal() or int(pid_text) not in identities:
                    continue
                try:
                    with path.open("rb") as source:
                        data = source.read(65537)
                    fields = data.split(b"\0")
                    if len(data) > 65536 or len(fields) < 7 or fields[0] != b"cargo-tile-v3":
                        continue
                    if (fields[1].decode() != generation or fields[2].decode() != boot_id
                            or fields[3].decode() != identities[int(pid_text)]):
                        continue
                    name = fields[4].decode()
                    if Path(name).name != name or name in (".", ".."):
                        continue
                    log_path = account / name
                    counter = self.log_counter(log_path)
                    if isinstance(counter, ProgressCounter):
                        found.append(counter)
                except (OSError, UnicodeError, ValueError):
                    continue
        return found[0] if len(found) == 1 else NoCounter("missing or competing Cargo captures")


    def end_pass(self) -> None:
        self.cursors = {path: cursor for path, cursor in self.cursors.items() if path in self.touched}
        self.touched.clear()

    def remove(self, sidecar: Path) -> None:
        _ = self.cursors.pop(progress_path(sidecar, "log"), None)


@dataclass
class PendingProfile:
    path: Path
    recorder: ProfileRecorder
    dirty: bool = False
    reading_source: ReadingSource = ReadingSourceNotRecorded()
    peak_processes: PeakProcesses | PeakProcessesNotRecorded = PeakProcessesNotRecorded()


@dataclass(frozen=True)
class MergedHistory:
    profile: ProgressProfile | NoMatchingProfile
    oldest: float
    rank: int


@dataclass
class ProfileHistory:
    root: Path
    recorders: dict[tuple[str, str], PendingProfile] = field(default_factory=dict)
    cached: dict[Path, RecordedProfile | NoMatchingProfile] = field(default_factory=dict)
    merged: dict[str, MergedHistory] = field(default_factory=dict)

    def _record(self, path: Path) -> RecordedProfile | NoMatchingProfile:
        if path not in self.cached:
            self.cached[path] = read_profile_record(path)
        return self.cached[path]

    def _history(self, key: str, cutoff: float, rank: int) -> MergedHistory:
        profiles: list[ProgressProfile] = []
        oldest = math.inf
        directory = self.root / key
        for path in directory.glob("*"):
            if path.suffix not in (".json", ".active"):
                continue
            record = self._record(path)
            if not isinstance(record, RecordedProfile) or record.at < cutoff:
                path.unlink(missing_ok=True)
                _ = self.cached.pop(path, None)
                continue
            oldest = min(oldest, record.at)
            if path.suffix == ".json":
                profiles.append(record.profile)
        history = MergedHistory(merge_profiles(profiles, rank), oldest, rank)
        self.merged[key] = history
        return history

    def prune(self, cutoff: float, *, rank: int | ConfiguredProfileRank = CONFIGURED_PROFILE_RANK) -> None:
        selected_rank = profile_rank(rank)
        for directory in self.root.glob("*"):
            if directory.is_dir():
                history = self.merged.get(directory.name)
                if history is None or history.oldest < cutoff or history.rank != selected_rank:
                    _ = self._history(directory.name, cutoff, selected_rank)

    def matching(self, key: str, cutoff: float, *, rank: int | ConfiguredProfileRank = CONFIGURED_PROFILE_RANK
                 ) -> ProgressProfile | NoMatchingProfile:
        selected_rank = profile_rank(rank)
        history = self.merged.get(key)
        if history is None or history.oldest < cutoff or history.rank != selected_rank:
            history = self._history(key, cutoff, selected_rank)
        return history.profile

    def observe(self, episode: str, key: str, counter: ProgressCounter | NoCounter,
                anonymous_bytes: int, at: float, *, reading_source: ReadingSource = READING_SOURCE_NOT_RECORDED,
                read_processes: Callable[[], tuple[ProfileProcess, ...] | PeakProcessesNotRecorded] = _no_peak_processes) -> None:
        if isinstance(counter, NoCounter):
            return
        identity = (episode, key)
        pending = self.recorders.get(identity)
        if pending is None:
            directory = self.root / key
            directory.mkdir(parents=True, exist_ok=True)
            active = directory / f"{episode}.active"
            recorder = ProfileRecorder()
            restored = read_profile_record(active)
            if isinstance(restored, RecordedProfile):
                recorder.phases = {phase.phase: list(phase.bins) for phase in restored.profile.phases}
            pending = PendingProfile(active, recorder)
            if isinstance(restored, RecordedProfile):
                pending.reading_source, pending.peak_processes = restored.reading_source, restored.peak_processes
            self.recorders[identity] = pending
        highest = max((value for phase in pending.recorder.phases.values() for value in phase if isinstance(value, int)), default=-1)
        if anonymous_bytes > highest:
            processes = read_processes()
            pending.reading_source = reading_source
            pending.peak_processes = (processes if isinstance(processes, PeakProcessesNotRecorded) else
                PeakProcesses(counter.phase, bin_index(counter.percent), anonymous_bytes, processes))
        changed = pending.recorder.observe(counter, anonymous_bytes)
        pending.dirty = pending.dirty or changed
        if pending.dirty:
            save_profile(pending.path, pending.recorder.profile(), at,
                reading_source=pending.reading_source, peak_processes=pending.peak_processes)
            pending.dirty = False
            _ = self.cached.pop(pending.path, None)
            history = self.merged.get(key)
            if history is not None:
                self.merged[key] = MergedHistory(history.profile, min(history.oldest, at), history.rank)

    def finish(self, episode: str, at: float, cutoff: float) -> None:
        paths = {path for path in self.root.glob(f"*/{episode}.active")}
        paths.update(pending.path for identity, pending in self.recorders.items() if identity[0] == episode)
        for active in sorted(paths):
            key = active.parent.name
            identity = (episode, key)
            pending = self.recorders.get(identity)
            record = (RecordedProfile(pending.recorder.profile(), at, pending.reading_source, pending.peak_processes)
                      if pending is not None else self._record(active))
            if isinstance(record, RecordedProfile):
                save_profile(active.with_suffix(".json"), record.profile, at,
                    reading_source=record.reading_source, peak_processes=record.peak_processes)
            active.unlink(missing_ok=True)
            _ = self.recorders.pop(identity, None)
            _ = self.cached.pop(active, None)
            _ = self.cached.pop(active.with_suffix(".json"), None)
            previous = self.merged.pop(key, None)
            if previous is not None:
                _ = self._history(key, cutoff, previous.rank)
