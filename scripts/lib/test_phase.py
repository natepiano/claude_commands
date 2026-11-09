"""Shared test-phase observations and their build-log context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Mapping


TEST_STEPS = ("nextest", "test", "t")


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


class TestPhaseChargeUse(TypedDict):
    """The charge that admission selected for a test phase."""

    bytes: int
    selected_at: float
    reservation_id: str
    need: dict[str, object]


@dataclass(frozen=True)
class NoTestPhaseChargeUse:
    pass


@dataclass(frozen=True)
class CallRecord:
    call_id: str


@dataclass(frozen=True)
class NoCallRecord:
    pass


def parse_test_phase_charge_use(fields: Mapping[str, object]) -> TestPhaseChargeUse:
    """Check the shared charge fields without interpreting admission's need."""
    amount, selected_at = fields.get("bytes"), fields.get("selected_at")
    reservation_id, need = fields.get("reservation_id"), fields.get("need")
    if (type(amount) is not int or amount <= 0
            or not isinstance(selected_at, (int, float)) or isinstance(selected_at, bool)
            or not isinstance(reservation_id, str) or not isinstance(need, dict)):
        raise ValueError("test-phase charge evidence is invalid")
    try:
        selected_at = float(selected_at)
    except OverflowError as error:
        raise ValueError("test-phase charge time is invalid") from error
    if not -float("inf") < selected_at < float("inf"):
        raise ValueError("test-phase charge time is invalid")
    return {"bytes": amount, "selected_at": selected_at, "reservation_id": reservation_id,
            "need": cast(dict[str, object], need)}
