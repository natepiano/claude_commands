"""Read process arguments and identify detached run supervisors."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ProcessCommandText:
    """The process command from ps has no argument boundaries."""
    text: str


def process_args(pid: int) -> list[str] | ProcessCommandText:
    """Read the process arguments or the complete ps command text."""
    if Path("/proc/self").exists():
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            return []
    shown = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False)
    return ProcessCommandText(shown.stdout.strip())


def runs_command(pid: int, command: Sequence[str]) -> bool:
    """Match the program name and its arguments against the current process command."""
    if pid <= 0 or not command:
        return False
    observed = process_args(pid)
    separator = " " if isinstance(observed, ProcessCommandText) else "\0"
    parts = observed.text.split(" ") if isinstance(observed, ProcessCommandText) else observed

    def matches(index: int, expected: Sequence[str]) -> bool:
        if not expected:
            return not isinstance(observed, ProcessCommandText) or index == len(parts)
        ends = range(index + 1, len(parts) + 1) if isinstance(observed, ProcessCommandText) else (index + 1,)
        for end in ends:
            actual = separator.join(parts[index:end])
            if (actual == expected[0] or Path(actual).resolve() == Path(expected[0]).resolve()) and matches(end, expected[1:]):
                return True
        return False

    return any(Path(part).name == Path(command[0]).name and matches(index + 1, command[1:])
               for index, part in enumerate(parts) if part)


def supervises(pid: int, record_path: Path) -> bool:
    """Match the supervisor command and its record to exclude reused process IDs."""
    return runs_command(pid, ["detached_runs.py", "supervise", str(record_path.resolve())])
