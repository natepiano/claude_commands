"""Read and group production unit marks without loading process discovery."""
from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "herdr", SCRIPTS.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.production import showrunners
from sessions import SessionRecord
from scripts.production.showrunners import canonical_slug
from place import HerdrPlace

PRODUCTION_MARK = "SHOWRUNNER_UNIT"
UNIT_MARK = "SHOWRUNNER_UNIT_ID"


class LiveClaude(NamedTuple):
    name: str
    session_id: str
    socket: str
    pid: int


class ClaudeNotRunning(NamedTuple):
    pass


class ClaudeUnknown(NamedTuple):
    """The session records could not all be read, which says nothing about whether Claude runs."""

    reason: str


Claude = LiveClaude | ClaudeNotRunning | ClaudeUnknown


@dataclass(frozen=True)
class MarkedUnit:
    unit: str
    claude: Claude
    place: HerdrPlace
    pid: int = 0
    command: str = ""


def place_text(place: HerdrPlace) -> str:
    return f"herdr:{place.session}:{place.pane}"


def holder_text(unit: MarkedUnit) -> str:
    """Identify the process whose marks caused a refusal, including uncertain holders."""
    pid = unit.pid or (unit.claude.pid if isinstance(unit.claude, LiveClaude) else 0)
    command = unit.command or ("claude" if isinstance(unit.claude, LiveClaude) else "unknown")
    return f"pid {pid} ({command}) in {place_text(unit.place)}"


class MarkedProductions(NamedTuple):
    # Each production's marked units, by production slug and then unit id.
    units: dict[str, dict[str, MarkedUnit]]
    # Why a unit was left out, by production slug and then unit id: two sessions carry its mark.
    faults: dict[str, dict[str, str]]

    def production_fault(self, slug: str) -> str:
        """Every left-out unit of the production, in one line; empty when none was left out."""
        return "; ".join(detail for _, detail in sorted(self.faults.get(slug, {}).items()))


def lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def group_marked_units(marked: Iterable[tuple[str, MarkedUnit]]) -> MarkedProductions:
    """Resolve current production slugs and refuse units marked in more than one place."""
    found: dict[str, dict[str, list[MarkedUnit]]] = {}
    current_slugs: dict[str, str] = {}
    for slug, unit in marked:
        if slug not in current_slugs:
            current_slugs[slug] = lookup_slug(slug)
        found.setdefault(current_slugs[slug], {}).setdefault(unit.unit, []).append(unit)
    units: dict[str, dict[str, MarkedUnit]] = {}
    faults: dict[str, dict[str, str]] = {}
    for slug, production in found.items():
        units[slug] = {}
        for name, places in production.items():
            # Inherited marks preserve a stopped pane for recovery, but cannot
            # compete with a live Claude or unresolved session for ownership.
            holders = [place for place in places if isinstance(place.claude, (LiveClaude, ClaudeUnknown))]
            if len(holders) > 1:
                sessions = " and ".join(sorted(holder_text(place) for place in holders))
                faults.setdefault(slug, {})[name] = f"{name}: its mark is on herdr sessions {sessions}"
            else:
                units[slug][name] = holders[0] if holders else places[0]
    return MarkedProductions(units, faults)


def session_unit_marks(
    records: list[SessionRecord], environment: Mapping[str, str],
) -> tuple[MarkedProductions, dict[str, str]]:
    """Read unit marks only for known session PIDs, without enumerating processes."""
    marked: list[tuple[str, MarkedUnit]] = []
    directors: dict[str, str] = {}
    known = {record["sessionId"] for record in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
    for record in records:
        try:
            # Herdr's Linux environment reader is private; keep this PID-only IO here.
            raw = (proc / str(record["pid"]) / "environ").read_bytes()
            values = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0")
                          if "=" in item)
        except OSError:
            continue
        owner = values.get("CLAUDE_CODE_SESSION_ID", "")
        if owner in known and owner != record["sessionId"]:
            directors[record["sessionId"]] = owner
            continue
        slug = values.get(PRODUCTION_MARK, "")
        unit = values.get(UNIT_MARK, "")
        if slug and unit:
            unit_mark = MarkedUnit(
                unit, LiveClaude(record["name"], record["sessionId"],
                                 record["messagingSocketPath"], record["pid"]),
                HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")),
            )
            marked.append((slug, unit_mark))
    return group_marked_units(marked), directors
