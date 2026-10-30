"""One fake stop-and-resume boundary for account move callers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from scripts.shutdown import stop_resume


@dataclass(frozen=True)
class MoveCall:
    session_id: str
    target: str
    why: str


@dataclass
class FakeSessionMoves:
    moved: Callable[[MoveCall], None] = lambda _call: None
    calls: list[MoveCall] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    batches: list[list[str]] = field(default_factory=list)

    def __call__(self, sessions: list[str], target: str, *, why: str) -> list[stop_resume.MoveResult]:
        self.batches.append(list(sessions))
        return [self.move(session, target, why=why) for session in sessions]

    def move(self, session: str, target: str, *, why: str) -> stop_resume.Resumed | stop_resume.ResumeFailed:
        call = MoveCall(session, target, why)
        self.calls.append(call)
        if session in self.failures:
            return stop_resume.ResumeFailed(session, self.failures[session])
        self.moved(call)
        return stop_resume.Resumed(session, "w1:p1", target)
