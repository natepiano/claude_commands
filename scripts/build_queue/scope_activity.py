"""Read build-run ownership and process evidence for the queue service."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Protocol


from scripts.lib.suite_runners import starts_suite_runner


def runs_suite_runner(command: tuple[str, ...]) -> bool:
    return any(starts_suite_runner(command[index:]) for index in range(len(command)))


class ProcessIdentity(Protocol):
    @property
    def parent(self) -> int: ...
    @property
    def start(self) -> str: ...


@dataclass(frozen=True)
class NoLiveBuildRun:
    pass


@dataclass(frozen=True)
class LiveBuildRun:
    pid: int
    start_time: str
    started_at: float


@dataclass(frozen=True)
class EmptyBuildScope:
    pass


@dataclass(frozen=True)
class NotServiceSlice:
    pass


@dataclass(frozen=True)
class IdleService:
    pass


@dataclass(frozen=True)
class RunningServiceJob:
    started_at: float


@dataclass(frozen=True)
class ScopeProcess:
    pid: int
    program: str


@dataclass(frozen=True)
class ScopeProcesses:
    count: int
    session_id: str
    session_name: str
    folder: str
    program: str
    started_at: float
    test_fixture: bool
    run: LiveBuildRun | NoLiveBuildRun
    members: tuple[ScopeProcess, ...] = field(default=(), kw_only=True)


class ScopeActivityReader:
    """Reuse one process table and cache process files for one service pass."""

    def __init__(self, processes: Mapping[int, ProcessIdentity], proc: Path,
                 runs: tuple[LiveBuildRun, ...] = ()) -> None:
        self.processes: Mapping[int, ProcessIdentity] = processes
        self.proc: Path = proc
        self.environments: dict[int, dict[str, str]] = {}
        self.commands: dict[int, tuple[str, ...]] = {}
        self.runs: dict[int, LiveBuildRun] = {run.pid: run for run in runs}
        self.resolved_runs: dict[int, LiveBuildRun | NoLiveBuildRun] = {}
        self.boot: float = 0.0
        try:
            self.boot = float(next(line.split()[1] for line in (proc / "stat").read_text().splitlines()
                                   if line.startswith("btime ")))
        except (OSError, ValueError, StopIteration, IndexError):
            pass
        self.ticks: int = int(os.sysconf("SC_CLK_TCK"))

    def environment(self, pid: int) -> dict[str, str]:
        if pid not in self.environments:
            try:
                raw = (self.proc / str(pid) / "environ").read_bytes().decode(errors="replace")
                self.environments[pid] = dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
            except OSError:
                self.environments[pid] = {}
        return self.environments[pid]

    def command(self, pid: int) -> tuple[str, ...]:
        if pid not in self.commands:
            try:
                self.commands[pid] = tuple(item for item in
                    (self.proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0") if item)
            except OSError:
                self.commands[pid] = ()
        return self.commands[pid]

    def started_at(self, pid: int) -> float:
        identity = self.processes.get(pid)
        if identity is None or not self.boot:
            return 0.0
        try:
            return self.boot + int(identity.start) / self.ticks
        except ValueError:
            return 0.0

    def ancestors(self, pid: int) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        while pid in self.processes and pid not in seen:
            seen.add(pid)
            result.append(pid)
            pid = self.processes[pid].parent
        return result

    def run(self, pid: int) -> LiveBuildRun | NoLiveBuildRun:
        chain: list[int] = []
        seen: set[int] = set()
        ancestor = pid
        while ancestor in self.processes and ancestor not in self.resolved_runs and ancestor not in seen:
            seen.add(ancestor)
            chain.append(ancestor)
            ancestor = self.processes[ancestor].parent
        if ancestor in seen:
            # A cycle has no common root. Resolve each member from its own ancestry.
            for member in chain:
                run: LiveBuildRun | NoLiveBuildRun = NoLiveBuildRun()
                for ancestor in self.ancestors(member):
                    run = self._run_at(ancestor, NoLiveBuildRun())
                    if isinstance(run, LiveBuildRun) or self.environment(ancestor).get("BUILDLOG_RUN"):
                        break
                self.resolved_runs[member] = run
            return self.resolved_runs[pid]
        inherited = self.resolved_runs.get(ancestor, NoLiveBuildRun())
        for ancestor in reversed(chain):
            inherited = self._run_at(ancestor, inherited)
            self.resolved_runs[ancestor] = inherited
        return self.resolved_runs.get(pid, NoLiveBuildRun())

    def _run_at(self, pid: int, inherited: LiveBuildRun | NoLiveBuildRun) -> LiveBuildRun | NoLiveBuildRun:
        known = self.runs.get(pid)
        if known is not None and known.start_time == self.processes[pid].start:
            return known
        recorded = self.environment(pid).get("BUILDLOG_RUN", "")
        if recorded:
            owner, separator, start = recorded.partition(":")
            if separator and owner.isdecimal():
                identity = self.processes.get(int(owner))
                if identity is not None and identity.start == start:
                    return LiveBuildRun(int(owner), start, self.started_at(int(owner)))
            # An inherited run identity cannot name a different process lifetime.
            return NoLiveBuildRun()
        command = self.command(pid)
        names = {Path(arg).name for arg in command}
        if "verify.sh" in names or runs_suite_runner(command):
            return LiveBuildRun(pid, self.processes[pid].start, self.started_at(pid))
        return inherited

    def scope(self, members: set[int], name: str = "") -> ScopeProcesses | EmptyBuildScope:
        members = members.intersection(self.processes)
        if not members:
            return EmptyBuildScope()
        captured_members: list[ScopeProcess] = []
        for pid in sorted(members, key=lambda pid: (self.started_at(pid), pid)):
            command = self.command(pid)
            captured_members.append(ScopeProcess(pid, Path(command[0]).name if command else "unknown program"))
        oldest = captured_members[0].pid
        session, name, folder = "", "", ""
        fixture = False
        run: LiveBuildRun | NoLiveBuildRun = NoLiveBuildRun()
        program = captured_members[0].program
        visited: set[int] = set()
        ancestor_runs: dict[int, LiveBuildRun | NoLiveBuildRun] = {}
        for pid in sorted(members):
            chain: list[int] = []
            ancestor = pid
            while ancestor in self.processes and ancestor not in visited:
                visited.add(ancestor)
                chain.append(ancestor)
                env = self.environment(ancestor)
                session = session or env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID", "")
                name = name or env.get("CLAUDE_SESSION_NAME", "")
                command = self.command(ancestor)
                fixture = fixture or bool(env.get("PYTEST_CURRENT_TEST")) or any(
                    arg in ("unittest", "pytest") or Path(arg).name.startswith("test_") for arg in command)
                ancestor = self.processes[ancestor].parent
            inherited = ancestor_runs.get(ancestor, NoLiveBuildRun())
            for ancestor in reversed(chain):
                inherited = self._run_at(ancestor, inherited)
                ancestor_runs[ancestor] = inherited
            if not isinstance(run, LiveBuildRun):
                run = ancestor_runs[pid]
            if not folder:
                try:
                    folder = str((self.proc / str(pid) / "cwd").readlink())
                except OSError:
                    pass
        return ScopeProcesses(len(captured_members), session, name, folder, program,
                              self.started_at(oldest), fixture, run,
                              members=tuple(captured_members))
