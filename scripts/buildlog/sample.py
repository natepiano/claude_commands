"""Record one machine-wide memory sample without opening the SQLite index."""

from __future__ import annotations

import os
import time
from enum import Enum
from pathlib import Path
from typing import Literal, NamedTuple, overload

import store

MEMINFO = Path("/proc/meminfo")
PRESSURE = Path("/proc/pressure/memory")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
CI_CGROUP = Path("/sys/fs/cgroup/hana.slice/hana-ci.slice")


class MachineMemoryUse(NamedTuple):
    memory_bytes: int
    swap_bytes: int


class Unmeasured(Enum):
    VALUE = "unmeasured"


class MemoryStallTotals(NamedTuple):
    some_us: int
    full_us: int


class PartialMemoryStallTotals(NamedTuple):
    some_us: int | Unmeasured
    full_us: int | Unmeasured


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
def parse_pressure(text: str, *, require_both: Literal[True] = True) -> MemoryStallTotals: ...


@overload
def parse_pressure(text: str, *, require_both: Literal[False]) -> PartialMemoryStallTotals: ...


def parse_pressure(text: str, *, require_both: bool = True) -> MemoryStallTotals | PartialMemoryStallTotals:
    """Read PSI totals; require both for a machine sample, allow missing scope lines."""
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
        return MemoryStallTotals(totals["some"], totals["full"])
    return PartialMemoryStallTotals(totals.get("some", Unmeasured.VALUE), totals.get("full", Unmeasured.VALUE))


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


def sample() -> None:
    """Append the current machine counters to a separate monthly JSONL file."""
    if not all(path.exists() for path in (MEMINFO, PRESSURE, BOOT_ID)):
        raise SystemExit("buildlog sample is Linux only (requires /proc).")
    at = time.time()
    host = store.host_name()
    boot_id = parse_boot_id(BOOT_ID.read_text())
    memory = parse_meminfo(MEMINFO.read_text())
    stalls = parse_pressure(PRESSURE.read_text())
    builds = anon_bytes(Path(os.environ.get("BUILDLOG_BUILDS_CGROUP", f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/builds.slice")))
    ci = anon_bytes(Path(os.environ.get("BUILDLOG_CI_CGROUP", str(CI_CGROUP))))
    record: dict[str, object] = {
        "kind": "sample",
        "host": host,
        "at": store.utc_iso(at),
        "boot_id": boot_id,
        "mem_used_bytes": memory.memory_bytes,
        "swap_used_bytes": memory.swap_bytes,
        "builds_anon_bytes": builds if isinstance(builds, int) else None,
        "ci_anon_bytes": ci if isinstance(ci, int) else None,
        "stall_some_us": stalls.some_us,
        "stall_full_us": stalls.full_us,
    }
    store.append_line(store.sample_file(host, at), record)
