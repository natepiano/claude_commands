"""Private memory readings for tests that use the admission gate."""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if TYPE_CHECKING:
    from ..build_hold.override_guard import require_checkout
else:
    from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))


def write_config_fixture(path: Path, overrides: str = "", *, template: str | None = None) -> int:
    """Merge committed defaults, existing settings, then explicit overrides. Keep each setting's explicit limit."""
    source = Path(__file__).resolve().parents[2] / "config" / (f"{template}.conf" if template else path.name)
    lines = source.read_text().splitlines() if source.exists() else []
    existing = path.read_text() if path.exists() else ""
    limit_prefixes = ("# whole:", "# range:", "# one of:")
    for layer in (existing, overrides):
        previous = ""
        for raw in layer.splitlines():
            limit = previous if previous.startswith(limit_prefixes) else ""
            previous = raw.strip()
            key, separator, value = raw.partition("=")
            if not separator or key.lstrip().startswith("#"):
                continue
            for index, line in enumerate(lines):
                if line.partition("=")[0].strip() == key.strip():
                    lines[index] = f"{key.strip()}={value}"
                    if limit:
                        if index and lines[index - 1].strip().startswith(limit_prefixes):
                            lines[index - 1] = limit
                        else:
                            lines.insert(index, limit)
                    break
            else:
                if limit:
                    lines.append(limit)
                lines.append(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_config_fault(path: Path, source: str) -> int:
    """Keep revision limits when a test supplies a partial or damaged config."""
    template = Path(__file__).resolve().parents[2] / "config" / path.name
    limits: dict[str, str] = {}
    previous = ""
    for line in template.read_text().splitlines():
        key, separator, _ = line.partition("=")
        if separator:
            limits[key.strip()] = previous if previous.startswith(("# whole:", "# range:", "# one of:")) else ""
        previous = line.strip()
    lines: list[str] = []
    for line in source.splitlines():
        key = line.partition("=")[0].strip()
        if limit := limits.get(key):
            lines.append(limit)
        lines.append(line)
    return path.write_text("\n".join(lines) + "\n")


def commit_config_fixture(path: Path) -> None:
    """Give the private configuration a known-good git revision for fault tests."""
    import subprocess
    from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for arguments in (("init", "-q"), ("config", "user.name", "Config fixture"),
                      ("config", "user.email", "fixture@example.invalid"),
                      ("add", path.name), ("commit", "-qm", "known good configuration")):
        _ = subprocess.run(["git", "-C", str(path.parent), *arguments], check=True,
                           capture_output=True, env=environment, timeout=TEST_HANG_GUARD_SECS)


def config_environment(root: Path) -> dict[str, str]:
    """Give subprocess fixtures their own complete configuration folder."""
    folder = root
    for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
        destination = folder / source.name
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = destination.write_bytes(source.read_bytes())
    return {"CONF_DIR": str(folder)}


def gate_isolation_environment(root: Path) -> dict[str, str]:
    """Keep a gate subprocess outside the caller's holds and session records."""
    return {
        **config_environment(root),
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
        **config_environment(root),
        "BUILDLOG_MEMINFO": str(meminfo),
        "BUILDLOG_BUILDS_CGROUP": str(root / "builds.slice"),
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get("BUILDLOG_LIVE_MEMORY_TRIPWIRE",
                                                         str(root / "live-memory-reads.jsonl")),
    }
