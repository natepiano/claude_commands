"""One fake stop-and-resume boundary for account move callers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from scripts.accounts import moves
from scripts.agents import stop_resume


_NO_SOURCES: Mapping[str, str] = MappingProxyType({})


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
    sources: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, sessions: list[str], target: str, *, why: str, cause: moves.MoveCause = "by hand",
                 source_accounts: Mapping[str, str] = _NO_SOURCES) -> list[stop_resume.MoveResult]:
        del cause
        self.sources.append(dict(source_accounts))
        self.batches.append(list(sessions))
        return [self.move(session, target, why=why) for session in sessions]

    def move(self, session: str, target: str, *, why: str) -> stop_resume.ClaudeUp | stop_resume.StopFailed:
        call = MoveCall(session, target, why)
        self.calls.append(call)
        if session in self.failures:
            return stop_resume.StopFailed(session, self.failures[session])
        self.moved(call)
        return stop_resume.ClaudeUp(session, "w1:p1", target)
