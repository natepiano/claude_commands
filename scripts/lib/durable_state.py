"""Original-process identity pending consolidation into scripts.lib.process_start."""
from __future__ import annotations

from dataclasses import dataclass

from scripts.message import sessions


@dataclass(frozen=True)
class ProcessLifetime:
    """One original process, distinguished from later reuse of its PID."""

    pid: int
    started: str

    @classmethod
    def current(cls, pid: int) -> ProcessLifetime:
        started = sessions.process_start(pid)
        if not started:
            raise ValueError(f"cannot identify original process {pid}")
        return cls(pid, started)

    def is_live(self) -> bool:
        state = sessions.process_liveness(self.pid, self.started)
        if isinstance(state, sessions.SessionLivenessUnknown):
            raise ValueError(state.reason)
        return isinstance(state, sessions.LiveSessionProcess)
