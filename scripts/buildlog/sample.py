"""Record machine resource counters without opening the SQLite index."""

from __future__ import annotations

import os
import time
import subprocess
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict

import store
import cgroup_capture
from cgroup_capture import (Unmeasured as Unmeasured, StallTotals as StallTotals,
                            PartialStallTotals as PartialStallTotals, read_pressure as read_pressure,
                            parse_pressure as parse_pressure)

MEMINFO = Path("/proc/meminfo")
PRESSURE = Path("/proc/pressure/memory")
CPU_PRESSURE = Path("/proc/pressure/cpu")
IO_PRESSURE = Path("/proc/pressure/io")
STAT = Path("/proc/stat")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
POWER_PROFILE_STATE = Path("/var/lib/power-profiles-daemon/state.ini")


class MachineMemoryUse(NamedTuple):
    memory_bytes: int
    swap_bytes: int


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


def parse_boot_id(text: str) -> str:
    """Return the kernel boot ID, rejecting an empty value."""
    boot_id = text.strip()
    if not boot_id:
        raise ValueError("empty boot ID")
    return boot_id


def anon_bytes(cgroup: Path) -> int | Unmeasured:
    """Read process memory from a slice, or mark an absent slice unmeasured."""
    reading = cgroup_capture.read_cgroup(cgroup)
    if reading["state"] == "present":
        return reading["stat"].get("anon", Unmeasured.VALUE)
    return Unmeasured.VALUE


def sccache_in_service(builds: Path) -> int | Unmeasured:
    if not builds.is_dir():
        return Unmeasured.VALUE
    try:
        return int(bool((builds / "sccache.service/cgroup.procs").read_text().strip()))
    except OSError:
        return 0


class PoolTokensMeasured(TypedDict):
    state: Literal["measured"]
    free_pool_tokens: int


def free_pool_tokens(pool: str) -> PoolTokensMeasured | cgroup_capture.ReadingUnavailable:
    command = ["stevie", "-t"] if pool == "/dev/steve" else ["stevie", "-s", pool, "-t"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=0.5, check=False)
        value = int(result.stdout.strip())
        if result.returncode == 0 and value >= 0:
            return {"state": "measured", "free_pool_tokens": value}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"state": "unavailable"}


class FilesystemMeasured(TypedDict):
    state: Literal["measured"]
    path: str
    total: int | Literal["unavailable"]
    used: int
    free: int
    device: str
    device_name: str


def filesystem_reading() -> FilesystemMeasured | cgroup_capture.ReadingUnavailable:
    import disk

    path = os.environ.get("BUILDLOG_TARGET_FILESYSTEM", os.environ.get("CARGO_TARGET_DIR", str(Path.home() / "rust")))
    try:
        reading = disk.filesystem_usage(path)
        return {"state": "measured", "path": path, "total": reading.total, "used": reading.used,
                "free": reading.free, "device": reading.device, "device_name": reading.device_name}
    except OSError:
        return {"state": "unavailable"}


class SampleTaken(NamedTuple):
    at: float
    host: str
    slices: cgroup_capture.SliceCollection = {"builds": {"state": "absent"}, "ci": {"state": "absent"}}


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
    slices = cgroup_capture.read_slices()
    builds_slice, ci_slice = slices["builds"], slices["ci"]
    builds = builds_slice["stat"].get("anon") if builds_slice["state"] == "present" else None
    ci = ci_slice["stat"].get("anon") if ci_slice["state"] == "present" else None
    sccache = sccache_in_service(cgroup_capture.builds_path())
    profile = read_power_profile(POWER_PROFILE_STATE)
    record: dict[str, object] = {
        "kind": "sample",
        "host": host,
        "at": store.utc_iso(at),
        "boot_id": boot_id,
        "slices": slices,
        "filesystem": filesystem_reading(),
        "jobserver": {"builds": free_pool_tokens("/dev/steve"), "ci": free_pool_tokens("/dev/steve-ci")},
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
    return SampleTaken(at, host, slices)
