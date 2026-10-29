"""Private memory readings for tests that use the admission gate."""

import os
from pathlib import Path


def fake_memory_readings(root: Path, *, available_kib: int = 67108864,
                         total_kib: int = 67108864, full_avg10: float = 0.0) -> dict[str, str]:
    meminfo = root / "meminfo"
    pressure = root / "pressure"
    _ = meminfo.write_text(f"MemTotal: {total_kib} kB\nMemAvailable: {available_kib} kB\n")
    _ = pressure.write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        + f"full avg10={full_avg10} avg60=0.00 avg300=0.00 total=0\n"
    )
    return {
        "BUILDLOG_MEMINFO": str(meminfo),
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get("BUILDLOG_LIVE_MEMORY_TRIPWIRE",
                                                         str(root / "live-memory-reads.jsonl")),
    }
