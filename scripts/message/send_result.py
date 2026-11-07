"""Read send.py results without loading delivery code or configuration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["sent", "skipped", "queued", "failed", "refused", "held", "replaced", "kept", "pending", "retry"]
SEND_RETRY_EXIT = 6
EXIT: dict[Outcome, int] = {"sent": 0, "skipped": 0, "queued": 1, "failed": 3, "refused": 2, "held": 4, "replaced": 0, "kept": 0, "pending": 5, "retry": SEND_RETRY_EXIT}
OUTCOME_HEADERS: dict[str, Outcome] = {outcome.upper(): outcome for outcome in EXIT}


@dataclass(frozen=True)
class OutcomeHeader:
    """The first send result and the payload that follows it."""
    outcome: Outcome
    detail: str
    payload: str


@dataclass(frozen=True)
class NoOutcomeHeader:
    """The command output contains no send result."""


def parse_outcome(stdout: str) -> OutcomeHeader | NoOutcomeHeader:
    """Read the first outcome header; later headers are payload text."""
    lines = stdout.removesuffix("\n").split("\n")
    for index, line in enumerate(lines):
        word, _, detail = line.partition(": ")
        if word in OUTCOME_HEADERS:
            return OutcomeHeader(OUTCOME_HEADERS[word], detail, "\n".join(lines[index + 1:]))
    return NoOutcomeHeader()


@dataclass(frozen=True)
class ConfirmedSendOutcome:
    """The first result header agrees with the command's exit code."""
    header: OutcomeHeader


@dataclass(frozen=True)
class UnconfirmedSendOutcome:
    """The command did not confirm a consistent send result."""


def send_outcome(returncode: int | None, stdout: str) -> ConfirmedSendOutcome | UnconfirmedSendOutcome:
    """Check the first result header against the command's exit code."""
    header = parse_outcome(stdout)
    if isinstance(header, OutcomeHeader) and returncode == EXIT[header.outcome]:
        return ConfirmedSendOutcome(header)
    return UnconfirmedSendOutcome()

