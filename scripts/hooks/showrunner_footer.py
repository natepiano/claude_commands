"""Read a running production and its open questions.

The reply footer and its Waiting on block are gone (user, 2026-10-18: "i want footers gone for
good"). The user reads the counts in the status line.
"""

from __future__ import annotations

import re
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone_name  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "production"))
from update_registration import ReportItem as ReportItem, reports_path as reports_path
from update_registration import QuestionStaff
from scripts.production import showrunners, staff

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


def outstanding_path(slug: str, *, identities: QuestionStaff | None = None) -> Path:
    from update_registration import outstanding_file

    return outstanding_file(slug, identities=identities)


def state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def review_pause_path(slug: str) -> Path:
    return state_root() / "review-paused" / f"{slug}.json"


def targeted_instances(session_id: str) -> list[Path]:
    root = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    owner = staff.snapshot().by_current_session(session_id)
    reference = owner.id if owner is not None else session_id
    matches: list[Path] = []
    try:
        for instance in root.iterdir():
            if not instance.name.startswith("showrunner-") or not instance.is_dir():
                continue
            try:
                if f"TARGET=session:{reference}" in (instance / "conf").read_text().splitlines():
                    matches.append(instance)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(matches)


def production_doc(instance_dir: Path) -> Path | None:
    """Read the timer owner's document from staff."""
    checked = showrunners.checked_doc(instance_dir)
    return checked.path if isinstance(checked, showrunners.CheckedDoc) else None


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
        return Production(showrunners.production_slug(str(doc)), doc, zone, schedule)
    except (OSError, ValueError, ZoneInfoNotFoundError, KeyError):
        return None


def open_questions(productions: list[Production], *, identities: QuestionStaff | None = None) -> int | None:
    """Count due questions and report unavailable ownership or unreadable records."""
    sys.path.insert(0, str(DAILIES.parent))
    from update_registration import RegistrationFailure, read_open_items
    from dailies_render import InputError, due

    try:
        identities = identities if identities is not None else QuestionStaff.read()
    except (OSError, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        return None
    count = 0
    for production in {production.slug: production for production in productions}.values():
        try:
            owner = identities.owner(production.doc)
        except RegistrationFailure as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            continue
        except staff.StaffRefused as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            return None
        try:
            items = read_open_items(outstanding_path(owner.id, identities=identities),
                                    document=production.doc, identities=identities)
            now = datetime.now(ZoneInfo(production.zone)).replace(tzinfo=None)
            count += len(due(items, now))
        except (InputError, OSError, ValueError, RegistrationFailure, staff.StaffRefused) as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            return None
    return count


def deferred_until(slugs: list[str], zone: str, *, identities: QuestionStaff | None = None) -> datetime | None:
    """Return the first deferred time from readable stores and report unavailable stores."""
    sys.path.insert(0, str(DAILIES.parent))
    from update_registration import RegistrationFailure, read_open_items
    from dailies_render import InputError, DeferredUntil

    try:
        identities = identities if identities is not None else QuestionStaff.read()
    except (OSError, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        return None
    now = datetime.now(ZoneInfo(zone)).replace(tzinfo=None)
    afters: list[datetime] = []
    for slug in slugs:
        try:
            items = read_open_items(outstanding_path(slug, identities=identities), identities=identities)
        except (InputError, OSError, ValueError, RegistrationFailure, staff.StaffRefused) as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            continue
        afters.extend(item.timing.at for item in items
                      if isinstance(item.timing, DeferredUntil) and item.timing.at > now)
    return min(afters).replace(tzinfo=ZoneInfo(zone)) if afters else None


def counts_line(productions: list[Production], questions: int) -> str | None:
    """The status line's counts line: each production's counts and the open questions."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The counts read the production scripts, which import this module, so this one imports late.
    import footer_counts

    from scripts.production import staff

    record = staff.snapshot()
    counts = footer_counts.total([footer_counts.read_counts(production.doc, record=record) for production in productions])
    return footer_counts.counts_line(counts._replace(questions=questions))
