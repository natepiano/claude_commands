"""Shared test-phase observations and their build-log context."""

from dataclasses import dataclass
from typing import Literal, TypedDict


@dataclass(frozen=True)
class NoTestPhaseRecord:
    pass


NO_TEST_PHASE_RECORD = NoTestPhaseRecord()


@dataclass(frozen=True)
class UnobservedTestPhase:
    pass


@dataclass(frozen=True)
class ObservedTestPhase:
    peak_bytes: int
    started_at: float


class TestPhaseContext(TypedDict):
    """Admission selection and actual width at the JSON persistence boundary."""

    repo_identity: str | None
    test_scope: Literal["full", "filtered"] | None
    package: str | None
    filters: list[str] | None
    call_id: str | None
    test_threads: int | None


@dataclass(frozen=True)
class TestPhaseRecord:
    window: ObservedTestPhase | UnobservedTestPhase
    context: TestPhaseContext


@dataclass(frozen=True)
class UnreadableTestPhaseRecord:
    reason: str


