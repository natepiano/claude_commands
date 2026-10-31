"""Shared raw cgroup files for scope and slice measurement regressions."""

from pathlib import Path
import subprocess

GIB = 2**30
CPU = {"state": "measured", "usage_usec": 120000000, "user_usec": 90000000, "system_usec": 30000000}
IO_LINES = (
    "254:0 rbytes=1048576 wbytes=2097152 rios=3 wios=4",
    "259:0 rbytes=1048576 wbytes=2097152 rios=3 wios=4",
    "253:0 rbytes=4096 wbytes=8192 rios=1 wios=2",
)


def write_cgroup(path: Path, *, with_io: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    fields = {
        "memory.current": str(12 * GIB), "memory.swap.current": str(2 * GIB),
        "memory.peak": str(40 * GIB), "memory.swap.peak": str(3 * GIB),
        "memory.max": str(44 * GIB), "memory.high": str(40 * GIB), "memory.swap.max": "max",
        "memory.events": "high 2\nmax 0\noom_kill 0",
        "memory.pressure": "some avg10=0 total=1250000\nfull avg10=0 total=250000",
        "memory.stat": f"anon {4 * GIB}\nfile {6 * GIB}\nkernel {2 * GIB}\nshmem {GIB}\ninactive_file {3 * GIB}\nsock 17\nfile_mapped 19\nfile_dirty 23\nfile_writeback 29\nactive_file 31\nslab 37",
        "cpu.stat": "usage_usec 120000000\nuser_usec 90000000\nsystem_usec 30000000",
    }
    if with_io:
        fields["io.stat"] = "\n".join(IO_LINES)
    for name, value in fields.items():
        written = (path / name).write_text(value + "\n")
        if written <= 0:
            raise AssertionError(f"empty cgroup fixture: {name}")


def slice_reading(current: int, *, cpu: int = 0, identity: int = 42) -> dict[str, object]:
    return {
        "state": "present", "identity": identity, "current_bytes": current,
        "swap_current_bytes": 2 * GIB, "max_bytes": 44 * GIB, "peak_bytes": 100 * GIB,
        "stat": {"anon": current // 4, "file": current // 2, "inactive_file": current // 4},
        "cpu": {"state": "measured", "usage_usec": cpu, "user_usec": cpu, "system_usec": 0},
        "io": {"state": "measured", "devices": [
            {"device": device, "name": name, "raw": f"{device} rbytes={cpu} wbytes={cpu * 2}",
             "rbytes": cpu, "wbytes": cpu * 2}
            for device, name in (("254:0", "dm-0"), ("259:0", "nvme0n1"), ("253:0", "zram0"))
        ]},
    }


def save_scope_counters(peak: Path, scope: Path) -> subprocess.CompletedProcess[str]:
    """Leave raw scope sidecars, including an empty file when IO is absent."""
    return subprocess.run(
        ["/bin/sh", "-c", 'cat "$1/cpu.stat" > "$2.cpu.stat"; cat "$1/io.stat" > "$2.io.stat" 2>/dev/null || true',
         "fixture-scope-counters", str(scope), str(peak)],
        capture_output=True, text=True, check=False,
    )
