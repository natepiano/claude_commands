"""Slice snapshots and a memory report bounded by two UTC instants."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import index
import cgroup_capture
from cgroup_capture import SliceAbsent as SliceAbsent, SlicePresent as SlicePresent, SliceCollection as SliceCollection
import store
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402

if TYPE_CHECKING:
    from ..lint.config_values import build_oom_score_adj
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lint"))
    from config_values import build_oom_score_adj

BUILD_OOM_SCORE_ADJ = build_oom_score_adj()
GIB = 2**30
SNAPSHOT_DISTANCE_S = 120
EARLYOOM = re.compile(
    r'sending SIG(?:TERM|KILL) to process (?P<pid>\d+) uid \d+ "(?P<command>[^"]+)"'
    + r'(?:[^\n]*?,\s*oom_score_adj (?P<adj>-?\d+))?'
    + r'(?:[^\n]*?\bcmdline "(?P<cmdline>.*)")?[^\n]*$'
)
OOM_MEMCG = re.compile(r'oom_memcg=([^,\s]+)')


class ZramAbsent(TypedDict):
    state: Literal["absent"]


class ZramPresent(TypedDict):
    state: Literal["present"]
    data_bytes: int
    compressed_bytes: int


class MemorySnapshot(TypedDict):
    kind: Literal["memory_snapshot"]
    at: str
    host: str
    slices: SliceCollection
    zram: ZramPresent | ZramAbsent


class SnapshotUnavailable(TypedDict):
    kind: Literal["unavailable"]


SnapshotNearInstant = MemorySnapshot | SnapshotUnavailable
ServiceState = Literal["in_service", "outside", "unavailable"]


class JournalUnavailable(Enum):
    VALUE = "journal unavailable"


class OomScoreAdjUnavailable(Enum):
    VALUE = "oom_score_adj not recorded"


class CmdlineUnavailable(Enum):
    VALUE = "cmdline not recorded"


def cmdline_program(cmdline: str) -> str:
    """earlyoom joins argv with spaces without shell quoting."""
    return cmdline.partition(" ")[0]


@dataclass(frozen=True)
class EarlyoomKill:
    at: datetime
    pid: int
    command: str
    oom_score_adj: int | OomScoreAdjUnavailable = OomScoreAdjUnavailable.VALUE
    cmdline: str | CmdlineUnavailable = CmdlineUnavailable.VALUE

    @property
    def outside_build_gate(self) -> bool:
        if self.oom_score_adj == BUILD_OOM_SCORE_ADJ or not isinstance(self.cmdline, str):
            return False
        return "/target/" in cmdline_program(self.cmdline)

    @property
    def is_build(self) -> bool:
        return self.oom_score_adj == BUILD_OOM_SCORE_ADJ or self.outside_build_gate


def service_state(value: int | None) -> ServiceState:
    if value is None:
        return "unavailable"
    return "in_service" if value == 1 else "outside"


def stored_bound(instant: datetime) -> str:
    """First millisecond-aligned stored instant at or after an input instant."""
    utc = instant.astimezone(UTC)
    remainder = utc.microsecond % 1000
    if remainder:
        utc += timedelta(microseconds=1000 - remainder)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def builds_path() -> Path:
    return cgroup_capture.builds_path()


def slice_state(path: Path) -> SlicePresent | SliceAbsent:
    return cgroup_capture.read_cgroup(path)


def zram_state(path: Path) -> ZramPresent | ZramAbsent:
    if not path.is_dir():
        return {"state": "absent"}
    try:
        data, compressed, *_ = (path / "mm_stat").read_text().split()
        return {"state": "present", "data_bytes": int(data), "compressed_bytes": int(compressed)}
    except (OSError, ValueError, UnicodeError):
        return {"state": "absent"}


def write_snapshot(at: float, host: str, slices: SliceCollection | None = None) -> MemorySnapshot:
    record: MemorySnapshot = {
        "kind": "memory_snapshot",
        "at": store.utc_iso(at),
        "host": host,
        "slices": slices if slices is not None else cgroup_capture.read_slices(),
        "zram": zram_state(Path(os.environ.get("BUILDLOG_ZRAM", "/sys/block/zram0"))),
    }
    store.append_line(store.sample_file(host, at), record)
    return record


def snapshot() -> MemorySnapshot:
    return write_snapshot(time.time(), store.host_name())


def local_time(value: datetime | str, *, with_date: bool = False) -> str:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    return instant.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M %Z" if with_date else "%H:%M %Z")


def samples_line(rows: list[tuple[str, str, ServiceState]]) -> str:
    observed = [(host, at, state) for host, at, state in rows if state != "unavailable"]
    if not observed:
        return "sccache service: unavailable"
    outside = [(host, at) for host, at, state in observed if state == "outside"]
    if not outside:
        return "sccache in its service: every sampled minute"
    runs: list[list[str]] = []
    last_host = ""
    last_state: ServiceState = "unavailable"
    for host, at, state in rows:
        if state != "outside":
            last_state = state
            continue
        if not runs or host != last_host or last_state != "outside" or (datetime.fromisoformat(at) - datetime.fromisoformat(runs[-1][-1])).total_seconds() > 90:
            runs.append([at])
        else:
            runs[-1].append(at)
        last_host, last_state = host, state
    parts: list[str] = []
    for run in runs:
        first = local_time(run[0])
        if len(run) == 1:
            parts.append(first)
        else:
            parts.append(f"{first.rsplit(' ', 1)[0]}–{local_time(run[-1])}")
    return f"sccache outside its service: {len(outside)} min — {', '.join(parts)}"


def instrument_lines(connection: sqlite3.Connection, predicate: str, params: tuple[str, ...]) -> list[str]:
    steps = predicate.format(column="started_at")
    samples = predicate.format(column="at")
    sampled = cast(list[tuple[str, str, int | None]], connection.execute(
        f"SELECT host, at, sccache_in_service FROM samples WHERE {samples} ORDER BY host, at", params
    ).fetchall())
    rows: list[tuple[str, str, ServiceState]] = [
        (host, at, service_state(value)) for host, at, value in sampled
    ]
    unsliced = cast(tuple[int, int], connection.execute(
        "SELECT count(*) FILTER (WHERE slice = 'fallback'), "
        + "count(*) FILTER (WHERE slice = 'none') "
        + f"FROM steps WHERE {steps}", params
    ).fetchone())
    kills = cast(tuple[int, int, int], connection.execute(
        "SELECT count(*) FILTER (WHERE mem_kills > 0 AND coalesce(mem_kill_stopped, 0) = 0 AND outcome = 'ran' AND status = 0), "
        + "count(*) FILTER (WHERE mem_kills > 0 AND coalesce(mem_kill_stopped, 0) = 0 AND (outcome IS NOT 'ran' OR status IS NOT 0)), "
        + "count(*) FILTER (WHERE mem_kill_stopped = 1) "
        + f"FROM calls WHERE {steps}", params
    ).fetchone())
    unsliced_parts = [
        label for count, label in (
            (unsliced[0], f"{unsliced[0]} fell back to a plain run"),
            (unsliced[1], f"{unsliced[1]} never tried a scope"),
        ) if count
    ]
    kill_parts = [
        label for count, label in (
            (kills[0], f"{kills[0]} calls passed after a re-run"),
            (kills[1], f"{kills[1]} failed later"),
            (kills[2], f"{kills[2]} stopped after a step was killed twice"),
        ) if count
    ]
    return [
        samples_line(rows),
        f"unsliced steps: {', '.join(unsliced_parts)}" if unsliced_parts else "unsliced steps: none",
        f"memory kills: {', '.join(kill_parts)}" if kill_parts else "memory kills: none",
    ]


def journal_messages(args: list[str], start: datetime, end: datetime) -> list[tuple[datetime, str]] | JournalUnavailable:
    command = ["journalctl", *args, "--since", f"@{start.timestamp():.6f}", "--until", f"@{end.timestamp():.6f}", "-o", "json", "--no-pager"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return JournalUnavailable.VALUE
    if result.returncode != 0:
        return JournalUnavailable.VALUE
    messages: list[tuple[datetime, str]] = []
    for line in result.stdout.splitlines():
        try:
            entry = cast(object, json.loads(line))
            if not isinstance(entry, dict):
                continue
            fields = cast(dict[str, object], entry)
            stamp, message = fields.get("__REALTIME_TIMESTAMP"), fields.get("MESSAGE")
            if not isinstance(stamp, str) or not isinstance(message, str):
                continue
            at = datetime.fromtimestamp(int(stamp) / 1_000_000, UTC)
            if start <= at < end:
                messages.append((at, message))
        except (ValueError, OverflowError):
            continue
    return messages


def earlyoom_kills(start: datetime, end: datetime) -> list[EarlyoomKill] | JournalUnavailable:
    """Read real kill lines once; startup threshold announcements are not kills."""
    if store.host_name() != "natedev":
        return JournalUnavailable.VALUE
    messages = journal_messages(["-u", "earlyoom"], start, end)
    if messages is JournalUnavailable.VALUE:
        return messages
    kills: list[EarlyoomKill] = []
    for at, message in messages:
        match = EARLYOOM.search(message)
        if match is None or not start <= at < end:
            continue
        adj, cmdline = match.group("adj"), match.group("cmdline")
        kills.append(EarlyoomKill(at, int(match.group("pid")), match.group("command"),
                                 int(adj) if adj is not None else OomScoreAdjUnavailable.VALUE,
                                 cmdline if cmdline is not None else CmdlineUnavailable.VALUE))
    return kills


def nearest_snapshot(connection: sqlite3.Connection, instant: datetime) -> SnapshotNearInstant:
    left = stored_bound(instant - timedelta(seconds=SNAPSHOT_DISTANCE_S))
    right = stored_bound(instant + timedelta(seconds=SNAPSHOT_DISTANCE_S))
    rows = cast(list[tuple[str, str, str, str]], connection.execute(
        "SELECT at, host, slices, zram FROM memory_snapshots WHERE host = ? AND at >= ? AND at < ?",
        (store.host_name(), left, right),
    ).fetchall())
    if not rows:
        return {"kind": "unavailable"}
    at, host, slices, zram = min(rows, key=lambda row: abs(datetime.fromisoformat(row[0]).timestamp() - instant.timestamp()))
    return {"kind": "memory_snapshot", "at": at, "host": host,
            "slices": cast(SliceCollection, json.loads(slices)), "zram": cast(ZramPresent | ZramAbsent, json.loads(zram))}


def slice_line(name: Literal["builds", "ci"], start: SnapshotNearInstant, end: SnapshotNearInstant,
               start_at: datetime, end_at: datetime) -> str:
    label = "builds" if name == "builds" else "CI"
    if start["kind"] == "unavailable":
        return f"{label}: no snapshot within 2 min of {local_time(start_at)}"
    if end["kind"] == "unavailable":
        return f"{label}: no snapshot within 2 min of {local_time(end_at)}"
    earlier, later = start["slices"][name], end["slices"][name]
    if earlier["state"] == "absent" or later["state"] == "absent":
        return f"{label}: slice absent"
    if any(not isinstance(row[key], int) for row in (earlier, later) for key in ("peak_bytes", "swap_peak_bytes")) or any(
        not isinstance(row["events"][key], int) for row in (earlier, later) for key in ("high", "max", "oom_kill")
    ):
        return f"{label}: slice counters unavailable"
    first_stall = earlier.get("stall_some_us", "unavailable")
    last_stall = later.get("stall_some_us", "unavailable")
    reset = cast(int, later["peak_bytes"]) < cast(int, earlier["peak_bytes"]) or any(
        cast(int, later["events"][key]) < cast(int, earlier["events"][key]) for key in ("high", "max", "oom_kill")
    ) or (isinstance(first_stall, int) and isinstance(last_stall, int) and last_stall < first_stall)
    prefix = f"{label}: reset in the window; since the reset " if reset else f"{label}: "
    events = ", ".join(
        f"{key} {later['events'][key]}" if reset else f"{key} +{cast(int, later['events'][key]) - cast(int, earlier['events'][key])}"
        for key in ("high", "max", "oom_kill")
    )
    if not isinstance(first_stall, int) or not isinstance(last_stall, int):
        stall = "stall unavailable"
    elif reset:
        stall = f"stall {last_stall / 1_000_000:.1f} s"
    else:
        stall = f"stall +{(last_stall - first_stall) / 1_000_000:.1f} s"
    def peak(name: str, before: int | Literal["unavailable"], after: int | Literal["unavailable"], limit: int | Literal["max", "unavailable"]) -> str:
        """memory.peak is the slice's lifetime high, so it measures the window only when it rose in it."""
        if not isinstance(before, int) or not isinstance(after, int):
            return f"{name} unavailable"
        bound = f"limit {limit / GIB:.1f} GiB" if isinstance(limit, int) else "no limit" if limit == "max" else "limit unavailable"
        if reset or after > before:
            return f"{name} {after / GIB:.1f} GiB, {bound}"
        return f"{name} at most {after / GIB:.1f} GiB (no new high in the window), {bound}"
    return (f"{prefix}{events}; {stall}; "
            f"{peak('peak', earlier['peak_bytes'], later['peak_bytes'], later['max'])}; "
            f"{peak('swap peak', earlier['swap_peak_bytes'], later['swap_peak_bytes'], later['swap_max'])}")


def window_report(connection: sqlite3.Connection, start: datetime, end: datetime) -> str:
    lines = [f"Memory, {local_time(start, with_date=True)} to {local_time(end, with_date=True)}"]
    early = earlyoom_kills(start, end)
    if early is JournalUnavailable.VALUE:
        lines.append("earlyoom kills: journal unavailable")
    else:
        lines.append(f"earlyoom kills: {len(early)}")
        lines.extend(f"{local_time(kill.at)} {kill.command} pid {kill.pid}" for kill in early)
    kernel_messages = journal_messages(["-k"], start, end)
    if kernel_messages is JournalUnavailable.VALUE:
        lines.append("kernel OOM kills: journal unavailable")
    else:
        oom = [match.group(1) if (match := OOM_MEMCG.search(message)) else ""
               for _, message in kernel_messages if "oom-kill:" in message]
        builds = sum(path.endswith("/builds.slice") for path in oom)
        ci = sum(path.endswith("/hana-ci.slice") for path in oom)
        lines.append(f"kernel OOM kills: {len(oom)} (builds {builds}, CI {ci}, elsewhere {len(oom) - builds - ci})")
    first, last = nearest_snapshot(connection, start), nearest_snapshot(connection, end)
    lines.extend(slice_line(name, first, last, start, end) for name in ("builds", "ci"))
    bounds = (stored_bound(start), stored_bound(end))
    waits = cast(tuple[int, int | None, int | None, int], connection.execute(
        "SELECT count(*), sum(mem_wait_s), max(mem_wait_s), count(*) FILTER (WHERE mem_wait_s >= 900) "
        + "FROM steps WHERE host = ? AND started_at >= ? AND started_at < ? AND mem_wait_s > 0",
        (store.host_name(), *bounds),
    ).fetchone())
    if waits[0]:
        lines.append(f"memory waits: {waits[0]} steps, total {duration(waits[1])}, longest {duration(waits[2])}, {waits[3]} reached the 15-min limit")
    else:
        lines.append("memory waits: none")
    lines.extend(instrument_lines(connection, "host = ? AND {column} >= ? AND {column} < ?", (store.host_name(), *bounds)))
    zram: ZramPresent | ZramAbsent = last["zram"] if last["kind"] == "memory_snapshot" else {"state": "absent"}
    if zram["state"] == "present":
        ratio = zram["data_bytes"] / zram["compressed_bytes"] if zram["compressed_bytes"] else 0.0
        lines.append(f"zram: {zram['data_bytes'] / GIB:.1f} GiB stored in {zram['compressed_bytes'] / GIB:.1f} GiB ({ratio:.1f}:1)")
    else:
        lines.append("zram: unavailable")
    return "\n".join(lines)


def duration(value: int | None) -> str:
    seconds = value or 0
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def parse_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("an offset is required")
    return instant.astimezone(UTC)


def memory_command(args: list[str]) -> int:
    try:
        if len(args) != 2:
            raise ValueError("start and end required")
        start, end = parse_instant(args[0]), parse_instant(args[1])
        if end <= start:
            raise ValueError("end must follow start")
    except ValueError:
        print("usage: buildlog memory START END (ISO 8601 instants with offsets)", file=sys.stderr)
        return 2
    _ = index.update()
    connection = index.read_only()
    try:
        print(window_report(connection, start, end))
    finally:
        connection.close()
    return 0
