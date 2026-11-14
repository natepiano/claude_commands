"""Find the session that holds the producer row named morty."""
from dataclasses import dataclass

from scripts.production import staff

NAME = "morty"


@dataclass(frozen=True)
class MortyHolder:
    """The native conversation on the live morty row."""

    native: staff.StaffId


@dataclass(frozen=True)
class NoMortyHolder:
    """No live record names one morty row."""


def holder() -> MortyHolder | NoMortyHolder:
    """Read the live record at each use; the retained local copy is never read."""
    try:
        rows = [row for row in staff.snapshot().find(kind="producer") if row.name == NAME]
    except staff.StaffUnreadable:
        return NoMortyHolder()
    return MortyHolder(rows[0].current_session) if len(rows) == 1 else NoMortyHolder()
