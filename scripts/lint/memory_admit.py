#!/usr/bin/env python3
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildlog"))


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class StepParser(Protocol):
    def step_name(self, argv: list[str]) -> str: ...


class GitIdentityReader(Protocol):
    def git_directory(self, argv: list[str], cwd: str) -> str: ...
    def git_facts(self, directory: str) -> GitFacts: ...


class BuildLogStore(Protocol):
    INDEX_NAME: str

    def root(self) -> Path: ...
    def host_name(self) -> str: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...


parse: StepParser = cast(StepParser, cast(object, importlib.import_module("parse")))
record: GitIdentityReader = cast(GitIdentityReader, cast(object, importlib.import_module("record")))
store: BuildLogStore = cast(BuildLogStore, cast(object, importlib.import_module("store")))

GIB = 1 << 30
FALLBACK = 12 * GIB
# On natedev, isolated hana nextest scopes had p90 anon / memory.peak 61%;
# clippy had 65%, so 0.65 covers the measured p90 process-memory share.
ANON_SHARE = 0.65
HISTORY_DAYS = 14


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: Literal["measured", "buildlog", "fallback"]
    count: int


@dataclass(frozen=True)
class RunningMemory:
    need: int
    anon: int


@dataclass(frozen=True)
class AdmissionDecision:
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int


@dataclass(frozen=True)
class Reserved:
    path: Path


@dataclass(frozen=True)
class NotReserved:
    reason: Literal["held", "launch", "untracked"]


@dataclass(frozen=True)
class StepProfile:
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak


@dataclass(frozen=True)
class CheckResult:
    decision: AdmissionDecision
    peak: ExpectedPeak
    live: list[Reservation]
    reservation: Reserved | NotReserved
    repo: str
    step: str
    available: int
    profile: StepProfile


def decide(available: int, total: int | None, need: int, running: list[RunningMemory]) -> AdmissionDecision:
    """All inputs are bytes; anon already in use remains in MemAvailable."""
    promised = sum(max(0, row.need - row.anon) for row in running)
    reserve = total * 5 // 100 if total is not None else 0
    threshold = need + promised + reserve if running else min(need + reserve, FALLBACK)
    return AdmissionDecision("admit" if available >= threshold else "hold", threshold, promised, reserve)


def percentile(values: list[int]) -> int:
    values.sort()
    return values[(90 * len(values) + 99) // 100 - 1]


def expected_peak(repo: str, step: str, host: str, now: datetime, root: Path) -> ExpectedPeak:
    cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat().replace("+00:00", "Z")
    measured: list[int] = []
    try:
        with (root / "admission/anon_peaks.jsonl").open() as lines:
            for line in lines:
                try:
                    item = cast(dict[str, object], json.loads(line))
                    if (item.get("host"), item.get("repo"), item.get("step")) == (host, repo, step) and str(item.get("ended_at", "")) >= cutoff:
                        peak = item.get("anon_peak_bytes")
                        if isinstance(peak, int) and peak > 0:
                            measured.append(peak)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    if len(measured) >= 5:
        return ExpectedPeak(percentile(measured), "measured", len(measured))
    peaks: list[int] = []
    index = root / store.INDEX_NAME
    try:
        connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        try:
            rows = cast(list[tuple[int]], connection.execute(
                "SELECT peak_mem_bytes FROM steps WHERE host = ? AND repo = ? AND step = ? "
                + "AND started_at >= ? AND peak_mem_bytes > 0",
                (host, repo, step, cutoff),
            ).fetchall())
            peaks = [int(value) for (value,) in rows]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        pass
    if len(peaks) >= 5:
        return ExpectedPeak(int(percentile(peaks) * ANON_SHARE), "buildlog", len(peaks))
    return ExpectedPeak(FALLBACK, "fallback", 0)


def process_start_time(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        return stat.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def cgroup_anon(sidecar: str) -> int:
    try:
        scope = Path(sidecar).read_text().strip()
        for line in (Path(scope) / "memory.stat").read_text().splitlines():
            if line.startswith("anon "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def read_meminfo(path: Path) -> tuple[int, int | None]:
    available: int | None = None
    total: int | None = None
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
            if parts[0] == "MemAvailable:":
                available = int(parts[1]) * 1024
            elif parts[0] == "MemTotal:":
                total = int(parts[1]) * 1024
    if available is None:
        raise ValueError(f"MemAvailable missing from {path}")
    return available, total


def admission_dir(root: Path) -> Path:
    directory = root / "admission"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def live_reservations(directory: Path) -> list[tuple[Path, Reservation]]:
    live: list[tuple[Path, Reservation]] = []
    for path in directory.glob("*.reservation"):
        try:
            row = cast(Reservation, json.loads(path.read_text()))
            if process_start_time(row["pid"]) == row["start_time"]:
                live.append((path, row))
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
        path.with_suffix(".max").unlink(missing_ok=True)
    return live


def git_identity(argv: list[str], cwd: str) -> tuple[str, str]:
    if not argv:
        return "", ""
    facts = record.git_facts(record.git_directory(argv, cwd))
    repo_path = facts["repo_path"]
    return (Path(repo_path).name if repo_path else "", Path(facts["worktree"] or cwd).name)


def check(meminfo: Path, pid: int, sidecar: str, argv: list[str], force: bool = False,
          root: Path | None = None, now: datetime | None = None,
          profile: StepProfile | None = None) -> CheckResult:
    root = root if root is not None else store.root()
    now = now if now is not None else datetime.now(UTC)
    if profile is None:
        repo, worktree = git_identity(argv, os.getcwd())
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        peak = expected_peak(repo, step, host, now, root) if repo else ExpectedPeak(FALLBACK, "fallback", 0)
        profile = StepProfile(repo, step, worktree, peak)
    repo, step, worktree, peak = profile.repo, profile.step, profile.worktree, profile.peak
    if not argv and not (root / "admission").exists():
        available, total = read_meminfo(meminfo)
        return CheckResult(decide(available, total, peak.bytes, []), peak, [], NotReserved("launch"), repo, step, available, profile)
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        live = live_reservations(directory)
        running = [RunningMemory(row["need"], cgroup_anon(row["sidecar"])) for _, row in live]
        available, total = read_meminfo(meminfo)
        decision = decide(available, total, peak.bytes, running)
        reservation: Reserved | NotReserved = NotReserved("held" if argv else "launch")
        if argv and (decision.state == "admit" or force):
            start = process_start_time(pid)
            if start is not None:
                path = directory / f"{pid}-{start}-{time.time_ns()}.reservation"
                row: Reservation = {"pid": pid, "start_time": start, "need": peak.bytes,
                                    "repo": repo, "step": step, "worktree": worktree,
                                    "admitted_at": store.utc_iso(now.timestamp()), "sidecar": sidecar}
                _ = path.write_text(json.dumps(row, separators=(",", ":")))
                reservation = Reserved(path)
            else:
                reservation = NotReserved("untracked")
        return CheckResult(decision, peak, [row for _, row in live], reservation, repo, step, available, profile)


def release(path: Path, status: int, root: Path | None = None) -> None:
    root = root if root is not None else store.root()
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        maximum = path.with_suffix(".max")
        try:
            peak = int(maximum.read_text())
            if peak > 0 and row["sidecar"] and Path(row["sidecar"]).is_file():
                store.append_line(directory / "anon_peaks.jsonl", {
                    "host": store.host_name(), "repo": row["repo"], "step": row["step"],
                    "worktree": row["worktree"], "anon_peak_bytes": peak, "status": status,
                    "ended_at": store.utc_iso(time.time()),
                })
        except (OSError, ValueError):
            pass
        path.unlink(missing_ok=True)
        maximum.unlink(missing_ok=True)
        if row["sidecar"]:
            Path(row["sidecar"]).unlink(missing_ok=True)


def clean_field(value: str) -> str:
    return value.replace("|", " ").replace("\n", " ")


def main() -> None:
    action = sys.argv[1]
    if action == "release":
        release(Path(sys.argv[2]), int(sys.argv[3]))
        return
    _, _, meminfo, pid, sidecar, forced, cached_repo, cached_step, cached_worktree, cached_need, cached_source, cached_count, *argv = sys.argv
    profile = None
    if cached_need != "-":
        profile = StepProfile(cached_repo, cached_step, cached_worktree,
                              ExpectedPeak(int(cached_need), cast(Literal["measured", "buildlog", "fallback"], cached_source), int(cached_count)))
    result = check(Path(meminfo), int(pid), sidecar, argv, forced == "1", profile=profile)
    decision, peak, live = result.decision, result.peak, result.live
    summary = ", ".join(f"{row['repo']} {row['step']} in {row['worktree']}" for row in live)
    reservation = result.reservation
    path = str(reservation.path) if isinstance(reservation, Reserved) else ""
    reason = "reserved" if isinstance(reservation, Reserved) else reservation.reason
    fields: list[str | int] = [decision.state, decision.threshold, peak.bytes, peak.source, peak.count,
                               decision.promised, len(live), decision.reserve, summary, path, result.repo, result.step,
                               result.available, reason, result.profile.worktree]
    print("|".join(clean_field(str(field)) for field in fields))


if __name__ == "__main__":
    main()
