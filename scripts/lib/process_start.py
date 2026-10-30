#!/usr/bin/env python3
"""Read process identity and state without treating a dead process as an owner.

Add the repository root to sys.path, then use from scripts.lib import
process_start (or import its names from scripts.lib.process_start). The command
line uses that same package module so identity types have one definition.

process_start reports zombies as gone: they cannot own a claim, session or wait.
Process-group cleanup uses check_group_leader to keep a matching zombie leader
while its children remain live. On Linux, read_stat(...).start keeps the zombie
identity; read_stat also keeps fields for process tree walks.

ps dates use LC_ALL=C and TZ=UTC. Keep their padding unchanged in stored values;
only output line endings are removed. A failed probe raises ProcessReadError;
it does not prove that the process is gone.

Compare a recorded start with check_owner, or check_group_leader for group
cleanup. Only GONE and REPLACED permit reclaiming a stale record (reclaim,
release, delete). Only ALIVE permits a signal to the recorded pid or group.
UNKNOWN keeps the owner and sends no signal. These checks return UNKNOWN on
process-table errors; callers must not catch OSError around them and treat
the record as stale.
Different dates are UNKNOWN because older records may use an unknown zone.

Command line (use the interpreter wrapper on both machines):
    scripts/lib/py scripts/lib/process_start.py start <pid>
    scripts/lib/py scripts/lib/process_start.py state <pid>
    scripts/lib/py scripts/lib/process_start.py owner <pid> '<recorded text>'
    scripts/lib/py scripts/lib/process_start.py leader <pid> '<recorded text>'
start exits 0 with identity text, 1 when gone, 2 on a read error. state prints
running, stopped or gone and exits 0, or 2 on a read error. owner and leader
print alive, replaced, gone or unknown and exit 0. Bad arguments exit 2 with
stderr and no stdout. Process-table errors print unknown for the checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
from stat import S_ISDIR
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start as _process_start

    raise SystemExit(_process_start.main())

PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcessStartTime:
    """Linux stat field 22: clock ticks after boot when the process started."""

    ticks: int

    @property
    def text(self) -> str:
        return str(self.ticks)


@dataclass(frozen=True)
class ProcessStartDate:
    """A ps lstart date read with LC_ALL=C and TZ=UTC, with its padding kept."""

    date: str

    @property
    def text(self) -> str:
        return self.date


@dataclass(frozen=True)
class ProcessGone:
    """No live process holds this pid now."""

    pid: int


ProcessStart = ProcessStartTime | ProcessStartDate


class ProcessReadError(OSError):
    """The process table could not be read."""


class ProcessRunState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    GONE = "gone"


class OwnerCheck(Enum):
    ALIVE = "alive"
    REPLACED = "replaced"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessStat:
    """One stat line; its command may hold spaces, ')' or incomplete UTF-8."""

    state: str
    ppid: int
    pgrp: int
    session: int
    tty_nr: int
    tpgid: int
    utime: int
    stime: int
    start: ProcessStartTime
    rss: int


def _has_proc(proc_root: Path) -> bool:
    """Use ps only when the default proc root is absent, never for a fake root."""
    try:
        mode = proc_root.stat().st_mode
    except FileNotFoundError as error:
        if proc_root == PROC_ROOT:
            return False
        raise ProcessReadError(f"process root is missing: {proc_root}") from error
    except OSError as error:
        raise ProcessReadError(f"cannot read process root {proc_root}: {error}") from error
    if not S_ISDIR(mode):
        raise ProcessReadError(f"process root is not a directory: {proc_root}")
    return True


def read_stat(pid: int, proc_root: Path = PROC_ROOT) -> ProcessStat | ProcessGone:
    """Read Linux stat bytes. Keep zombie identity and process tree fields."""
    if pid <= 0:
        return ProcessGone(pid)
    if not _has_proc(proc_root):
        raise ProcessReadError(f"Linux stat is unavailable at {proc_root}")
    path = proc_root / str(pid) / "stat"
    try:
        record = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return ProcessGone(pid)
    except OSError as error:
        raise ProcessReadError(f"cannot read {path}: {error}") from error
    try:
        opening, closing = record.index(b"("), record.rindex(b")")
        if closing <= opening or record[:opening].strip() != str(pid).encode("ascii"):
            raise ValueError("invalid pid or command field")
        fields = record[closing + 1:].split()
        state = fields[0].decode("ascii")
        if len(state) != 1 or state not in "RSDTtZXxKWPIUN":
            raise ValueError("invalid process state")
        start = int(fields[19])
        if start < 0:
            raise ValueError("negative process start time")
        return ProcessStat(
            state=state, ppid=int(fields[1]), pgrp=int(fields[2]),
            session=int(fields[3]), tty_nr=int(fields[4]), tpgid=int(fields[5]),
            utime=int(fields[11]), stime=int(fields[12]),
            start=ProcessStartTime(start), rss=int(fields[21]),
        )
    except (IndexError, ValueError) as error:
        raise ProcessReadError(f"malformed process stat at {path}: {error}") from error


def _ps_field(pid: int, field: str) -> str:
    """An empty ps selection means the process is gone; a failed probe is an error."""
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise ProcessReadError(f"cannot read process {pid} with ps: {error}") from error
    value = result.stdout.rstrip("\r\n")
    if result.stderr.strip() or (result.returncode and (result.returncode != 1 or value.strip())):
        raise ProcessReadError(f"ps could not read process {pid}: {result.stderr.strip()}")
    return value if value.strip() else ""


def process_start(pid: int, proc_root: Path = PROC_ROOT) -> ProcessStart | ProcessGone:
    """Read a live start. Only a missing default PROC_ROOT uses ps."""
    if pid <= 0:
        return ProcessGone(pid)
    if _has_proc(proc_root):
        stat = read_stat(pid, proc_root)
        if isinstance(stat, ProcessGone) or stat.state in ("Z", "X", "x"):
            return ProcessGone(pid)
        return stat.start
    date = _ps_field(pid, "lstart")
    if not date:
        return ProcessGone(pid)
    start = ProcessStartDate(date)
    # Reject malformed ps output before it can become a stored identity.
    _ = started_at(start, proc_root)
    if process_state(pid, proc_root) is ProcessRunState.GONE:
        return ProcessGone(pid)
    return start


def parse_start(text: str) -> ProcessStart:
    """Restore a stored tick count or ps date without losing its kind."""
    if not text.strip():
        raise ValueError("process start text is empty")
    return ProcessStartTime(int(text)) if text.isdecimal() else ProcessStartDate(text)


def process_state(pid: int, proc_root: Path = PROC_ROOT) -> ProcessRunState:
    """Read scheduling state. An unavailable explicit proc root is an error."""
    if pid <= 0:
        return ProcessRunState.GONE
    if _has_proc(proc_root):
        stat = read_stat(pid, proc_root)
        if isinstance(stat, ProcessGone):
            return ProcessRunState.GONE
        state = stat.state
    else:
        state = _ps_field(pid, "stat").strip()
    return _run_state(pid, state)


def _run_state(pid: int, state: str) -> ProcessRunState:
    if not state:
        return ProcessRunState.GONE
    if state[0] in ("Z", "X", "x"):
        return ProcessRunState.GONE
    if state[0] == "T":
        return ProcessRunState.STOPPED
    if state[0] not in "RSDIUtWPNK":
        raise ProcessReadError(f"unknown process state for {pid}: {state!r}")
    return ProcessRunState.RUNNING


def _compare_starts(recorded: ProcessStart, live: ProcessStart) -> OwnerCheck:
    if isinstance(recorded, ProcessStartTime) and isinstance(live, ProcessStartTime):
        return OwnerCheck.ALIVE if recorded.ticks == live.ticks else OwnerCheck.REPLACED
    if isinstance(recorded, ProcessStartDate) and isinstance(live, ProcessStartDate):
        if recorded.date.split() == live.date.split():
            return OwnerCheck.ALIVE
    return OwnerCheck.UNKNOWN


def check_owner(pid: int, recorded: ProcessStart, proc_root: Path = PROC_ROOT) -> OwnerCheck:
    """Check a live owner; process-table errors keep it as UNKNOWN.

    Only GONE and REPLACED permit reclaiming the record; only ALIVE permits a
    signal to the pid. UNKNOWN does neither. Callers must not catch OSError
    around this check and treat the record as stale.
    Equal dates ignore padding; different dates and kinds stay UNKNOWN.
    """
    try:
        live = process_start(pid, proc_root)
        if isinstance(live, ProcessGone):
            return OwnerCheck.GONE
        return _compare_starts(recorded, live)
    except ProcessReadError:
        return OwnerCheck.UNKNOWN


def check_group_leader(pid: int, recorded: ProcessStart, proc_root: Path = PROC_ROOT) -> OwnerCheck:
    """Check a group leader, keeping a matching zombie for its live children.

    ALIVE (a matching leader, a zombie too) permits signals to its group; GONE
    and REPLACED permit dropping the group record only. UNKNOWN does neither. Process-table errors do not escape as OSError.
    """
    if pid <= 0:
        return OwnerCheck.GONE
    try:
        if _has_proc(proc_root):
            stat = read_stat(pid, proc_root)
            if isinstance(stat, ProcessGone) or stat.state in ("X", "x"):
                return OwnerCheck.GONE
            live: ProcessStart = stat.start
        else:
            text = _ps_field(pid, "lstart=,stat")
            if not text:
                return OwnerCheck.GONE
            try:
                date, state = text.rsplit(maxsplit=1)
            except ValueError as error:
                raise ProcessReadError(f"malformed ps group leader for {pid}: {text!r}") from error
            live = ProcessStartDate(date)
            _ = started_at(live, proc_root)
            if _run_state(pid, state) is ProcessRunState.GONE and state[0] != "Z":
                return OwnerCheck.GONE
        return _compare_starts(recorded, live)
    except ProcessReadError:
        return OwnerCheck.UNKNOWN


def started_at(start: ProcessStart, proc_root: Path = PROC_ROOT) -> datetime:
    """Convert either start form to UTC wall time."""
    try:
        if isinstance(start, ProcessStartDate):
            return datetime.strptime(start.date, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
        for line in (proc_root / "stat").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if fields and fields[0] == "btime":
                boot_time = int(fields[1])
                ticks_per_second = os.sysconf("SC_CLK_TCK")
                if ticks_per_second <= 0:
                    raise ValueError("clock tick rate must be positive")
                return datetime.fromtimestamp(boot_time + start.ticks / ticks_per_second, timezone.utc)
        raise ValueError("boot time is missing")
    except (OSError, UnicodeError, ValueError, IndexError, OverflowError) as error:
        raise ProcessReadError(f"cannot read process start wall time: {error}") from error


class _ProcessCommand(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = "start"
        self.pid: int = 0
        self.recorded: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "state", "owner", "leader"):
        command = commands.add_parser(name)
        _ = command.add_argument("pid", type=int)
        if name in ("owner", "leader"):
            _ = command.add_argument("recorded")
    args = parser.parse_args(namespace=_ProcessCommand())
    if args.command in ("owner", "leader"):
        try:
            recorded = parse_start(args.recorded)
        except ValueError as error:
            parser.error(str(error))
        check = check_owner if args.command == "owner" else check_group_leader
        print(check(args.pid, recorded).value)
        return 0
    try:
        if args.command == "state":
            print(process_state(args.pid).value)
            return 0
        start = process_start(args.pid)
        if isinstance(start, ProcessGone):
            return 1
        print(start.text)
        return 0
    except ProcessReadError as error:
        print(f"process_start: {error}", file=sys.stderr)
        return 2
