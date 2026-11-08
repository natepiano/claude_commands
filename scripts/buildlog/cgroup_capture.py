"""One raw cgroup-v2 capture for slice samples and completed step scopes.

memory.current is the whole charge. Child charges, stat components and swap
must never be added to it. Missing files affect only their own reading.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, NamedTuple, overload
from enum import Enum

Unavailable = Literal["unavailable"]
UNAVAILABLE: Unavailable = "unavailable"
STAT_FIELDS = frozenset(("anon", "file", "kernel", "shmem", "file_mapped", "file_dirty",
                         "file_writeback", "active_file", "inactive_file", "slab", "sock", "zswap"))
SYS_DEV_BLOCK = Path("/sys/dev/block")


class Unmeasured(Enum):
    VALUE = "unmeasured"


class StallTotals(NamedTuple):
    some_us: int
    full_us: int


class PartialStallTotals(NamedTuple):
    some_us: int | Unmeasured
    full_us: int | Unmeasured


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


def read_pressure(path: Path) -> PartialStallTotals:
    try:
        return parse_pressure(path.read_text(), require_both=False)
    except (OSError, ValueError, UnicodeError):
        return PartialStallTotals(Unmeasured.VALUE, Unmeasured.VALUE)


class ReadingUnavailable(TypedDict):
    state: Literal["unavailable"]


class CpuMeasured(TypedDict):
    state: Literal["measured"]
    usage_usec: int
    user_usec: int
    system_usec: int


class DeviceTraffic(TypedDict):
    device: str
    name: str
    raw: str
    rbytes: NotRequired[int]
    wbytes: NotRequired[int]


class IoMeasured(TypedDict):
    state: Literal["measured"]
    devices: list[DeviceTraffic]


class StepScopeCounters(TypedDict):
    """Own-scope counters; CPU excludes sccache.service compiles."""

    cpu: CpuMeasured | ReadingUnavailable
    io: IoMeasured | ReadingUnavailable


class SliceAbsent(TypedDict):
    state: Literal["absent"]


class SliceEvents(TypedDict):
    high: int | Unavailable
    max: int | Unavailable
    oom_kill: int | Unavailable


class SlicePresent(TypedDict):
    state: Literal["present"]
    identity: int | Unavailable
    events: SliceEvents
    current_bytes: int | Unavailable
    swap_current_bytes: int | Unavailable
    stat: dict[str, int]
    peak_bytes: int | Unavailable
    swap_peak_bytes: int | Unavailable
    high: int | Literal["max", "unavailable"]
    max: int | Literal["max", "unavailable"]
    max_bytes: int | Literal["max", "unavailable"]
    swap_max: int | Literal["max", "unavailable"]
    stall_some_us: int | Unavailable
    cpu: CpuMeasured | ReadingUnavailable
    io: IoMeasured | ReadingUnavailable


class SliceCollection(TypedDict):
    builds: SlicePresent | SliceAbsent
    ci: SlicePresent | SliceAbsent


def builds_path() -> Path:
    uid = os.getuid()
    default = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/builds.slice"
    return Path(os.environ.get("BUILDLOG_BUILDS_CGROUP", default))


def ci_path() -> Path:
    return Path(os.environ.get("BUILDLOG_CI_CGROUP", "/sys/fs/cgroup/hana.slice/hana-ci.slice"))


def integer_file(path: Path) -> int | Unavailable:
    try:
        value = int(path.read_text().strip())
        return value if value >= 0 else UNAVAILABLE
    except (OSError, ValueError, UnicodeError):
        return UNAVAILABLE


def parse_named_counters(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        name, value = line.split()
        number = int(value)
        if number < 0:
            raise ValueError("negative counter")
        result[name] = number
    return result


def named_counters(path: Path) -> dict[str, int]:
    try:
        return parse_named_counters(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return {}


def parse_cpu(text: str) -> CpuMeasured | ReadingUnavailable:
    try:
        counters = parse_named_counters(text)
        return {"state": "measured", "usage_usec": counters["usage_usec"],
                "user_usec": counters["user_usec"], "system_usec": counters["system_usec"]}
    except (KeyError, ValueError):
        return {"state": "unavailable"}


def read_cpu(path: Path) -> CpuMeasured | ReadingUnavailable:
    try:
        return parse_cpu(path.read_text())
    except (OSError, UnicodeError):
        return {"state": "unavailable"}


def limit_file(path: Path) -> int | Literal["max", "unavailable"]:
    try:
        value = path.read_text().strip()
        return "max" if value == "max" else int(value)
    except (OSError, ValueError, UnicodeError):
        return UNAVAILABLE


def device_name(device: str) -> str:
    """Name the filesystem's top layer, never walk down and sum its slaves."""
    path = SYS_DEV_BLOCK / device
    return path.resolve().name if path.exists() else device


def parse_io(text: str) -> IoMeasured | ReadingUnavailable:
    try:
        if not text.strip():
            return {"state": "unavailable"}
        devices: list[DeviceTraffic] = []
        for line in text.splitlines():
            fields = line.split()
            major, minor = fields[0].split(":")
            if not major.isdecimal() or not minor.isdecimal():
                raise ValueError("invalid device")
            device: DeviceTraffic = {"device": fields[0], "name": device_name(fields[0]), "raw": line}
            for field in fields[1:]:
                key, value = field.split("=")
                if key in ("rbytes", "wbytes"):
                    number = int(value)
                    if number < 0:
                        raise ValueError("negative traffic")
                    if key == "rbytes":
                        device["rbytes"] = number
                    else:
                        device["wbytes"] = number
            devices.append(device)
        return {"state": "measured", "devices": devices}
    except (ValueError, IndexError):
        return {"state": "unavailable"}


def read_io(path: Path) -> IoMeasured | ReadingUnavailable:
    try:
        return parse_io(path.read_text())
    except (OSError, UnicodeError):
        return {"state": "unavailable"}


def read_cgroup(path: Path) -> SlicePresent | SliceAbsent:
    try:
        identity: int | Unavailable = path.stat().st_ino
    except OSError:
        return {"state": "absent"}
    events = named_counters(path / "memory.events")
    stat = {key: value for key, value in named_counters(path / "memory.stat").items() if key in STAT_FIELDS}
    pressure = read_pressure(path / "memory.pressure").some_us
    maximum = limit_file(path / "memory.max")
    return {"state": "present", "identity": identity,
            "events": {"high": events.get("high", UNAVAILABLE), "max": events.get("max", UNAVAILABLE),
                       "oom_kill": events.get("oom_kill", UNAVAILABLE)},
            "current_bytes": integer_file(path / "memory.current"),
            "swap_current_bytes": integer_file(path / "memory.swap.current"), "stat": stat,
            "peak_bytes": integer_file(path / "memory.peak"),
            "swap_peak_bytes": integer_file(path / "memory.swap.peak"),
            "high": limit_file(path / "memory.high"), "max": maximum, "max_bytes": maximum,
            "swap_max": limit_file(path / "memory.swap.max"),
            "stall_some_us": pressure if isinstance(pressure, int) else UNAVAILABLE,
            "cpu": read_cpu(path / "cpu.stat"), "io": read_io(path / "io.stat")}


def read_slices() -> SliceCollection:
    return {"builds": read_cgroup(builds_path()), "ci": read_cgroup(ci_path())}
