"""Read process arguments and identify detached run supervisors."""

from collections.abc import Sequence
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


def runs_command(pid: int, command: Sequence[str]) -> bool:
    """Match the program name and its arguments against the current process command."""
    if pid <= 0 or not command:
        return False
    parts = process_args(pid)
    return any(Path(part).name == Path(command[0]).name
               and parts[index + 1:index + len(command)] == list(command[1:])
               for index, part in enumerate(parts) if part)


def supervises(pid: int, record_path: Path) -> bool:
    """Match the supervisor command and its record to exclude reused process IDs."""
    return runs_command(pid, ["detached_runs.py", "supervise", str(record_path.resolve())])
