"""Shared process-table fixtures and probes for process identity tests."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from scripts.lib import process_start


def write_stat(
    proc_root: Path, pid: int, *, start_ticks: int = 123, state: str = "S",
    command: str = "worker", ppid: int = 1, pgrp: int = 1, session: int = 1,
    tty_nr: int = 0, tpgid: int = -1, utime: int = 0, stime: int = 0, rss: int = 0,
) -> Path:
    """Write fields 1 through 52; return the stat path for later changes or removal.

    The caller owns proc_root. This helper never reads the host process table.
    """
    fields = {number: "0" for number in range(3, 53)}
    fields.update({
        3: state, 4: str(ppid), 5: str(pgrp), 6: str(session), 7: str(tty_nr),
        8: str(tpgid), 14: str(utime), 15: str(stime), 22: str(start_ticks), 24: str(rss),
    })
    path = proc_root / str(pid) / "stat"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        f"{pid} ({command}) " + " ".join(fields[number] for number in range(3, 53)) + "\n",
        encoding="utf-8",
    )
    return path


def stored_start_probe(probe: Callable[[int], str]) -> Callable[[int, Path], process_start.ProcessStart | process_start.ProcessGone]:
    """Adapt fixture text identities at the shared reader's imported boundary."""
    def read(pid: int, _proc_root: Path = process_start.PROC_ROOT) -> process_start.ProcessStart | process_start.ProcessGone:
        try:
            text = probe(pid)
        except OSError as error:
            raise process_start.ProcessReadError(str(error)) from error
        return process_start.parse_start(text) if text else process_start.ProcessGone(pid)
    return read


def process_start_probe(proc_root: Path) -> Callable[[int, Path], process_start.ProcessStart | process_start.ProcessGone]:
    """Read only the fixture process table, including when patched into the reader.

    Save the real reader before a caller patches it. Owner checks pass their
    default root to the patched name; this probe uses the fixture root instead.
    """
    reader = process_start.process_start

    def read(pid: int, _proc_root: Path = process_start.PROC_ROOT) -> process_start.ProcessStart | process_start.ProcessGone:
        return reader(pid, proc_root)

    return read
