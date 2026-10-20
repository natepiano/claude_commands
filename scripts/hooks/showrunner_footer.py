"""Read a running production and its open questions.

The reply footer and its Waiting on block are gone (user, 2026-10-18: "i want footers gone for
good"). The user reads the counts in the status line.
"""

from __future__ import annotations

import re
import json
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone_name  # noqa: E402

if TYPE_CHECKING:
    from showrunners import Place


DAILIES = Path(__file__).resolve().parent.parent / "production" / "dailies_render.py"
STATUS = re.compile(r"Status: PRODUCTION — ([a-z]*)")


class NoDailiesSchedule(Enum):
    PAUSED_OR_UNSCHEDULED = "paused or unscheduled"


class FooterError(Exception):
    """A production's open-questions list cannot be read."""


@dataclass(frozen=True)
class ScheduledDailies:
    next_due: int


@dataclass(frozen=True)
class Production:
    slug: str
    doc: Path
    zone: str
    schedule: ScheduledDailies | NoDailiesSchedule


def key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def outstanding_path(slug: str) -> Path:
    return state_root() / "outstanding" / f"{slug}.json"


class ReportItem(TypedDict):
    """Something the user asked to be told about, such as "tell me when hanadocs is back"."""
    text: str
    until: str


def reports_path(slug: str) -> Path:
    return state_root() / "reports" / f"{slug}.json"


def state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def review_pause_path(slug: str) -> Path:
    return state_root() / "review-paused" / f"{slug}.json"


def targeted_instances(session_id: str) -> list[Path]:
    root = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    matches: list[Path] = []
    try:
        for instance in root.iterdir():
            if not instance.name.startswith("showrunner-") or not instance.is_dir():
                continue
            try:
                if f"TARGET=session:{session_id}" in (instance / "conf").read_text().splitlines():
                    matches.append(instance)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(matches)


def production_doc(instance_dir: Path) -> Path | None:
    """The production doc an update timer checks: the last word of its conf's CHECK line."""
    check = shlex.split(key_values(instance_dir / "conf").get("CHECK", ""))
    return Path(check[-1]) if check else None


def read_production(instance_dir: Path) -> Production | None:
    try:
        doc = production_doc(instance_dir)
        if doc is None:
            return None
        lines = doc.read_text().splitlines()
        status = next((match.group(1) for line in lines if (match := STATUS.search(line))), None)
        if status != "running":
            return None
        # The user's zone setting; the doc's own User zone line is no longer read.
        zone = user_zone_name()
        try:
            state = key_values(instance_dir / "state")
        except OSError:
            state = {}
        schedule = (ScheduledDailies(int(state["NEXT_DUE"])) if state.get("ENABLED") == "1"
                    else NoDailiesSchedule.PAUSED_OR_UNSCHEDULED)
        return Production(instance_dir.name.removeprefix("showrunner-"), doc, zone, schedule)
    except (OSError, ValueError, ZoneInfoNotFoundError, KeyError):
        return None


def place_of(slug: str) -> Place:
    """Where `slug` sits among the showrunners: the root above it, or the productions below it."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The tree lives with the production scripts. They import this module, so this one imports late.
    import showrunners
    return showrunners.place_of(slug)


def open_questions(productions: list[Production], below: dict[str, list[str]]) -> int | None:
    """How many items now wait on the user across these productions; None when a list cannot be read.

    `below` names the productions under each root showrunner. A root's count covers them, since
    the root is where the user sees everything below it. An item the user deferred is not counted until its `after`
    time, as the footer's own `nothing needed` is decided.
    """
    # One user, one zone: a lower production's `after` is judged in the zone of the one that counts it.
    zones: dict[str, str] = {}
    for production in productions:
        for slug in (production.slug, *below.get(production.slug, [])):
            _ = zones.setdefault(slug, production.zone)
    count = 0
    for slug, zone in zones.items():
        path = outstanding_path(slug)
        if not path.exists():
            continue
        try:
            entries = cast(object, json.loads(path.read_text(encoding="utf-8")))
            now = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")
        except (OSError, ValueError, KeyError):
            return None
        if not isinstance(entries, list):
            return None
        for entry in cast(list[object], entries):
            after = cast(dict[str, object], entry).get("after") if isinstance(entry, dict) else None
            # Both are `YYYY-MM-DDTHH:MM` in that zone, so they compare as text.
            if not isinstance(after, str) or after <= now:
                count += 1
    return count


def deferred_until(slugs: list[str], zone: str) -> datetime | None:
    """When the soonest item the user deferred falls due, so a count kept from now knows when it ends."""
    now = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")
    afters: list[str] = []
    for slug in slugs:
        try:
            entries = cast(object, json.loads(outstanding_path(slug).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        for entry in cast(list[object], entries) if isinstance(entries, list) else []:
            after = cast(dict[str, object], entry).get("after") if isinstance(entry, dict) else None
            if isinstance(after, str) and after > now:
                afters.append(after)
    if not afters:
        return None
    try:
        return datetime.fromisoformat(min(afters)).replace(tzinfo=ZoneInfo(zone))
    except ValueError:
        return None


def counts_line(productions: list[Production], questions: int) -> str | None:
    """The status line's counts line: each production's counts and the open questions."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The counts read the production scripts, which import this module, so this one imports late.
    import footer_counts

    counts = footer_counts.total([footer_counts.read_counts(production.doc) for production in productions])
    return footer_counts.counts_line(counts._replace(questions=questions))
