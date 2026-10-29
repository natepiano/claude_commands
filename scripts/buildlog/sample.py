"""Record machine resource counters without opening the SQLite index."""

from __future__ import annotations

import os
import time
from enum import Enum
from pathlib import Path
from typing import Literal, NamedTuple, overload

import store

MEMINFO = Path("/proc/meminfo")
PRESSURE = Path("/proc/pressure/memory")
CPU_PRESSURE = Path("/proc/pressure/cpu")
IO_PRESSURE = Path("/proc/pressure/io")
STAT = Path("/proc/stat")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
CI_CGROUP = Path("/sys/fs/cgroup/hana.slice/hana-ci.slice")
POWER_PROFILE_STATE = Path("/var/lib/power-profiles-daemon/state.ini")


class MachineMemoryUse(NamedTuple):
    memory_bytes: int
    swap_bytes: int


class Unmeasured(Enum):
    VALUE = "unmeasured"


class PowerProfile(NamedTuple):
    name: str
    since_epoch: float


def read_power_profile(path: Path) -> PowerProfile | Unmeasured:
    """Read the daemon's profile and last write time without contacting it."""
    try:
        with path.open() as handle:
            state = os.fstat(handle.fileno())
            if state.st_size >= 1024:
                return Unmeasured.VALUE
            text = handle.read()
    except (OSError, UnicodeError):
        return Unmeasured.VALUE
    in_state = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_state = line == "[State]"
        elif in_state:
            name, separator, value = line.partition("=")
            if separator and name.strip() == "Profile":
                profile = value.strip()
                return PowerProfile(profile, state.st_mtime) if profile else Unmeasured.VALUE
    return Unmeasured.VALUE


class StallTotals(NamedTuple):
    some_us: int
    full_us: int


class PartialStallTotals(NamedTuple):
    some_us: int | Unmeasured
    full_us: int | Unmeasured


class CpuTicks(NamedTuple):
    idle: int
    iowait: int
    total: int


def parse_cpu_ticks(text: str) -> CpuTicks:
    """Read aggregate CPU ticks, excluding guest ticks already in user and nice."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = line.split()[1:]
            if len(fields) < 8:
                raise ValueError("cpu stat lacks the first eight counters")
            ticks = [int(value) for value in fields[:8]]
            return CpuTicks(ticks[3], ticks[4], sum(ticks))
    raise ValueError("cpu stat lacks aggregate cpu line")


def read_cpu_ticks(path: Path) -> CpuTicks | Unmeasured:
    try:
        return parse_cpu_ticks(path.read_text())
    except (OSError, ValueError):
        return Unmeasured.VALUE


def read_pressure(path: Path) -> PartialStallTotals:
    try:
        return parse_pressure(path.read_text(), require_both=False)
    except (OSError, ValueError):
        return PartialStallTotals(Unmeasured.VALUE, Unmeasured.VALUE)


def parse_meminfo(text: str) -> MachineMemoryUse:
    """Return used memory and swap in bytes from Linux meminfo text."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        if separator and name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            words = rest.split()
            if len(words) != 2 or words[1] != "kB":
                raise ValueError(f"invalid {name} in meminfo")
            values[name] = int(words[0]) * 1024
    if len(values) != 4:
        raise ValueError("meminfo lacks memory or swap totals")
    return MachineMemoryUse(values["MemTotal"] - values["MemAvailable"], values["SwapTotal"] - values["SwapFree"])


@overload
def parse_pressure(text: str, *, require_both: Literal[True] = True) -> StallTotals: ...


@overload
def parse_pressure(text: str, *, require_both: Literal[False]) -> PartialStallTotals: ...


def parse_pressure(text: str, *, require_both: bool = True) -> StallTotals | PartialStallTotals:
    """Read PSI totals; require both for machine memory, allow partial readings otherwise."""
    totals: dict[str, int] = {}
    for line in text.splitlines():
        words = line.split()
        if not words or words[0] not in ("some", "full"):
            continue
        total = next((word.removeprefix("total=") for word in words[1:] if word.startswith("total=")), None)
        if total is not None:
            try:
                totals[words[0]] = int(total)
            except ValueError:
                if require_both:
                    raise
    if require_both and len(totals) != 2:
        raise ValueError("memory pressure lacks some or full total")
    if require_both:
        return StallTotals(totals["some"], totals["full"])
    return PartialStallTotals(totals.get("some", Unmeasured.VALUE), totals.get("full", Unmeasured.VALUE))


def parse_boot_id(text: str) -> str:
    """Return the kernel boot ID, rejecting an empty value."""
    boot_id = text.strip()
    if not boot_id:
        raise ValueError("empty boot ID")
    return boot_id


def anon_bytes(cgroup: Path) -> int | Unmeasured:
    """Read process memory from a slice, or mark an absent slice unmeasured."""
    try:
        for line in (cgroup / "memory.stat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                return int(value)
    except (OSError, ValueError):
        pass
    return Unmeasured.VALUE


def sccache_in_service(builds: Path) -> int | Unmeasured:
    if not builds.is_dir():
        return Unmeasured.VALUE
    try:
        return int(bool((builds / "sccache.service/cgroup.procs").read_text().strip()))
    except OSError:
        return 0


class SampleTaken(NamedTuple):
    at: float
    host: str


def sample() -> SampleTaken:
    """Append the current machine counters to a separate monthly JSONL file."""
    if not all(path.exists() for path in (MEMINFO, PRESSURE, BOOT_ID)):
        raise SystemExit("buildlog sample is Linux only (requires /proc).")
    at = time.time()
    host = store.host_name()
    boot_id = parse_boot_id(BOOT_ID.read_text())
    memory = parse_meminfo(MEMINFO.read_text())
    stalls = parse_pressure(PRESSURE.read_text())
    cpu_stalls = read_pressure(CPU_PRESSURE)
    io_stalls = read_pressure(IO_PRESSURE)
    ticks = read_cpu_ticks(STAT)
    builds_path = Path(os.environ.get("BUILDLOG_BUILDS_CGROUP", f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/builds.slice"))
    builds = anon_bytes(builds_path)
    sccache = sccache_in_service(builds_path)
    ci = anon_bytes(Path(os.environ.get("BUILDLOG_CI_CGROUP", str(CI_CGROUP))))
    profile = read_power_profile(POWER_PROFILE_STATE)
    record: dict[str, object] = {
        "kind": "sample",
        "host": host,
        "at": store.utc_iso(at),
        "boot_id": boot_id,
        "mem_used_bytes": memory.memory_bytes,
        "swap_used_bytes": memory.swap_bytes,
        "builds_anon_bytes": builds if isinstance(builds, int) else None,
        "sccache_in_service": sccache if isinstance(sccache, int) else None,
        "ci_anon_bytes": ci if isinstance(ci, int) else None,
        "stall_some_us": stalls.some_us,
        "stall_full_us": stalls.full_us,
        "cpu_stall_some_us": cpu_stalls.some_us if isinstance(cpu_stalls.some_us, int) else None,
        "cpu_stall_full_us": cpu_stalls.full_us if isinstance(cpu_stalls.full_us, int) else None,
        "io_stall_some_us": io_stalls.some_us if isinstance(io_stalls.some_us, int) else None,
        "io_stall_full_us": io_stalls.full_us if isinstance(io_stalls.full_us, int) else None,
        "cpu_idle_ticks": ticks.idle if isinstance(ticks, CpuTicks) else None,
        "cpu_iowait_ticks": ticks.iowait if isinstance(ticks, CpuTicks) else None,
        "cpu_total_ticks": ticks.total if isinstance(ticks, CpuTicks) else None,
        "power_profile": profile.name if isinstance(profile, PowerProfile) else None,
        "power_profile_since": store.utc_iso(profile.since_epoch) if isinstance(profile, PowerProfile) else None,
    }
    store.append_line(store.sample_file(host, at), record)
    return SampleTaken(at, host)
