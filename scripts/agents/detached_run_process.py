"""Read process arguments and identify detached run supervisors."""

from pathlib import Path
import subprocess


def process_args(pid: int) -> list[str]:
    """The pid's command line; ps where there is no /proc (the Mac). Empty when it is gone."""
    if Path("/proc/self").exists():
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            return []
    shown = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False)
    return shown.stdout.split()


def supervises(pid: int, record_path: Path) -> bool:
    """Whether pid is the `supervise` process of this record, so a reused pid does not match."""
    if pid <= 0:
        return False
    parts = process_args(pid)
    if "supervise" not in parts or not any(part.endswith("detached_runs.py") for part in parts):
        return False
    expected = record_path.resolve()
    return any(Path(part).resolve() == expected for part in parts if part)
