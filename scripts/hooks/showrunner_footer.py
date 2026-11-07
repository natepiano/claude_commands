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

from scripts.lib.user_zone import user_zone_name  # noqa: E402

from scripts.production.update_registration import ReportItem as ReportItem, reports_path as reports_path
from scripts.production.update_registration import QuestionStaff
from scripts.production import showrunners, staff
from scripts.shutdown.remote import local_machine

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
    name: str
    doc: Path
    zone: str
    schedule: ScheduledDailies | NoDailiesSchedule
    owner: staff.StaffRow


def key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def outstanding_path(name: str, *, identities: QuestionStaff | None = None) -> Path:
    from scripts.production.update_registration import outstanding_file

    return outstanding_file(name, identities=identities)


def state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def review_pause_path(name: str) -> Path:
    return state_root() / "review-paused" / f"{name}.json"


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
            except (OSError, UnicodeError):
                continue
    except OSError:
        pass
    return sorted(matches)


def production_doc(instance_dir: Path) -> Path | None:
    """Read the timer owner's document from staff."""
    owner = showrunners.owner_for_timer(instance_dir)
    return owner.doc.path if isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) else None


def read_production(instance_dir: Path) -> Production | None:
    try:
        owner = showrunners.owner_for_timer(instance_dir)
        if not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
            return None
        doc = owner.doc.path
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
        return Production(owner.name, doc, zone, schedule, owner)
    except (OSError, ValueError, ZoneInfoNotFoundError, KeyError):
        return None


def open_questions(productions: list[Production], *, identities: QuestionStaff | None = None) -> int | None:
    """Count due questions and report unavailable ownership or unreadable records."""
    from scripts.production.update_registration import RegistrationFailure, read_open_items
    from scripts.production.dailies_render import InputError, due

    try:
        identities = identities if identities is not None else QuestionStaff.read()
    except (OSError, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        return None
    count = 0
    for production in {production.name: production for production in productions}.values():
        owner = production.owner
        if not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) or owner.doc.machine != local_machine():
            print(f"questions: unavailable — {production.doc}: no showrunner staff row", file=sys.stderr)
            continue
        try:
            items = read_open_items(outstanding_path(owner.id, identities=identities),
                                    document=production.doc, identities=identities)
            now = datetime.now(ZoneInfo(production.zone)).replace(tzinfo=None)
            count += len(due(items, now))
        except (InputError, OSError, ValueError, RegistrationFailure, staff.StaffRefused) as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            return None
    return count


def deferred_until(names: list[str], zone: str, *, identities: QuestionStaff | None = None) -> datetime | None:
    """Return the first deferred time from readable stores and report unavailable stores."""
    from scripts.production.update_registration import RegistrationFailure, read_open_items
    from scripts.production.dailies_render import InputError, DeferredUntil

    try:
        identities = identities if identities is not None else QuestionStaff.read()
    except (OSError, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        return None
    now = datetime.now(ZoneInfo(zone)).replace(tzinfo=None)
    afters: list[datetime] = []
    for name in names:
        try:
            items = read_open_items(outstanding_path(name, identities=identities), identities=identities)
        except (InputError, OSError, ValueError, RegistrationFailure, staff.StaffRefused) as error:
            print(f"questions: unavailable — {error}", file=sys.stderr)
            continue
        afters.extend(item.timing.at for item in items
                      if isinstance(item.timing, DeferredUntil) and item.timing.at > now)
    return min(afters).replace(tzinfo=ZoneInfo(zone)) if afters else None


def counts_line(productions: list[Production], questions: int) -> str | None:
    """The status line's counts line: each production's counts and the open questions."""
    # The counts read the production scripts, which import this module, so this one imports late.
    from scripts.production import footer_counts

    from scripts.production import staff

    record = staff.snapshot()
    counts = footer_counts.total([footer_counts.read_counts(production.doc, record=record) for production in productions])
    return footer_counts.counts_line(counts._replace(questions=questions))
