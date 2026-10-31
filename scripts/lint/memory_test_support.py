"""Private memory readings for tests that use the admission gate."""

import os
from pathlib import Path


def gate_isolation_environment(root: Path) -> dict[str, str]:
    """Keep a gate subprocess outside the caller's holds and session records."""
    return {
        "BUILD_HOLD_DIR": str(root / "holders"),
        "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
        "UNIT_ACTIVE_DIR": str(root / "active"),
        "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
    }


def fake_memory_readings(root: Path, *, available_kib: int = 67108864,
                         total_kib: int = 67108864, full_avg10: float = 0.0,
                         swap_total_kib: int = 0, swap_free_kib: int = 0, some_avg10: float = 0.0) -> dict[str, str]:
    meminfo = root / "meminfo"
    pressure = root / "pressure"
    _ = meminfo.write_text(f"MemTotal: {total_kib} kB\nMemAvailable: {available_kib} kB\n"
                          + f"SwapTotal: {swap_total_kib} kB\nSwapFree: {swap_free_kib} kB\n")
    _ = pressure.write_text(
        f"some avg10={some_avg10} avg60=0.00 avg300=0.00 total=0\n"
        + f"full avg10={full_avg10} avg60=0.00 avg300=0.00 total=0\n"
    )
    return {
        "BUILDLOG_MEMINFO": str(meminfo),
        "BUILDLOG_BUILDS_CGROUP": str(root / "builds.slice"),
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get("BUILDLOG_LIVE_MEMORY_TRIPWIRE",
                                                         str(root / "live-memory-reads.jsonl")),
    }
