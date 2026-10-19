"""Read a running production, its open questions and the question review a reply must keep.

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
from typing import TYPE_CHECKING, Literal, TypedDict, cast
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
class PendingReviewedQuestion:
    text: str
    reviewed: str


@dataclass(frozen=True)
class PendingReviewUnavailableQuestion:
    text: str
    question: str
    reason: str


PendingQuestion = PendingReviewedQuestion | PendingReviewUnavailableQuestion


@dataclass(frozen=True)
class QuestionReplyReady:
    pass


@dataclass(frozen=True)
class QuestionReplyBlocked:
    reason: str


class PendingReviewedQuestionRecord(TypedDict):
    kind: Literal["reviewed"]
    text: str
    reviewed: str


class PendingReviewUnavailableQuestionRecord(TypedDict):
    kind: Literal["review unavailable"]
    text: str
    question: str
    reason: str


class PendingReviewedRecord(TypedDict):
    items: list[PendingReviewedQuestionRecord | PendingReviewUnavailableQuestionRecord]


def pending_reviewed_path(instance_dir: Path) -> Path:
    return instance_dir / "pending_reviewed.json"


def read_pending_reviewed(instance_dir: Path) -> list[PendingQuestion]:
    path = pending_reviewed_path(instance_dir)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(raw, dict):
        raise FooterError(f"{path}: expected an object")
    entries = cast(dict[str, object], raw).get("items")
    if not isinstance(entries, list):
        raise FooterError(f"{path}: expected items")
    items: list[PendingQuestion] = []
    for raw_item in cast(list[object], entries):
        if not isinstance(raw_item, dict):
            raise FooterError(f"{path}: expected a question object")
        item = cast(dict[str, object], raw_item)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise FooterError(f"{path}: expected item text")
        if item.get("kind") == "reviewed":
            reviewed = item.get("reviewed")
            if not isinstance(reviewed, str) or not reviewed.strip():
                raise FooterError(f"{path}: expected reviewed question")
            items.append(PendingReviewedQuestion(text, reviewed))
        elif item.get("kind") == "review unavailable":
            question, reason = item.get("question"), item.get("reason")
            if not isinstance(question, str) or not question.strip() or not isinstance(reason, str):
                raise FooterError(f"{path}: expected unavailable question and reason")
            items.append(PendingReviewUnavailableQuestion(text, question, reason))
        else:
            raise FooterError(f"{path}: unknown question kind")
    return items


def add_pending_reviewed(instance_dir: Path, items: list[PendingQuestion]) -> None:
    combined = read_pending_reviewed(instance_dir)
    for item in items:
        if item not in combined:
            combined.append(item)
    if combined:
        write_pending_reviewed(instance_dir, combined)


def write_pending_reviewed(instance_dir: Path, items: list[PendingQuestion]) -> None:
    """Store exactly these questions as waiting to be asked; none deletes the file."""
    if not items:
        delete_pending_reviewed(instance_dir)
        return
    record: PendingReviewedRecord = {"items": []}
    for item in items:
        if isinstance(item, PendingReviewedQuestion):
            record["items"].append({"kind": "reviewed", "text": item.text, "reviewed": item.reviewed})
        else:
            record["items"].append({"kind": "review unavailable", "text": item.text,
                                    "question": item.question, "reason": item.reason})
    path = pending_reviewed_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    _ = scratch.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    _ = scratch.replace(path)


def delete_pending_reviewed(instance_dir: Path) -> None:
    pending_reviewed_path(instance_dir).unlink(missing_ok=True)


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


def normalised(text: str) -> str:
    return " ".join(text.split())


def choice_comparison_text(text: str) -> str:
    return normalised(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text))


def pending_question_text(item: PendingQuestion) -> str:
    return item.reviewed if isinstance(item, PendingReviewedQuestion) else item.question


def instance_slugs(instance_dirs: list[str]) -> set[str]:
    return {Path(instance).name.removeprefix("showrunner-") for instance in instance_dirs}


def with_below(slugs: set[str]) -> set[str]:
    """These productions and every one below them, as a root's count covers them."""
    return slugs.union(*(place_of(slug).below for slug in slugs))


def outstanding_texts(slugs: set[str]) -> list[str]:
    """Every text, question and reviewed question in these productions' open_for_user lists."""
    texts: list[str] = []
    for slug in sorted(slugs):
        try:
            raw = cast(object, json.loads(outstanding_path(slug).read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        if not isinstance(raw, list):
            raise FooterError(f"{slug}: expected outstanding list")
        for entry in cast(list[object], raw):
            if isinstance(entry, dict):
                fields = cast(dict[str, object], entry)
                texts.extend(value for key in ("text", "question", "reviewed")
                             if isinstance(value := fields.get(key), str))
    return texts


def check_reviewed_text(instance_dirs: list[str], reply: str) -> QuestionReplyReady | QuestionReplyBlocked:
    accepted: list[str] = []
    slugs: set[str] = set()
    for instance in instance_dirs:
        pending = read_pending_reviewed(Path(instance))
        missing = [item for item in pending if normalised(pending_question_text(item)) not in normalised(reply)]
        # A question is shown in full once, when it opens: the reply that carries it ends its wait,
        # whatever else this hook says of that reply, so no later reply is made to repeat it.
        if len(missing) < len(pending):
            write_pending_reviewed(Path(instance), missing)
        if missing:
            return QuestionReplyBlocked(f"Question {missing[0].text!r}: paste its reviewed question into this reply:\n"
                                        + pending_question_text(missing[0]))
        accepted.extend(pending_question_text(item) for item in pending)
        slugs.add(Path(instance).name.removeprefix("showrunner-"))
    notify_scripts = str(Path(__file__).resolve().parent.parent / "notify")
    if notify_scripts not in sys.path:
        sys.path.insert(0, notify_scripts)
    if TYPE_CHECKING:
        from ..notify import reply_asks
    else:
        import reply_asks
    asks = reply_asks.user_reply_questions(reply)
    if not asks.sentences and not asks.has_choices:
        return QuestionReplyReady()
    accepted.extend(outstanding_texts(with_below(slugs)))
    registered = [reply_asks.question_prose(choice_comparison_text(question)) for question in accepted]
    for sentence in (*asks.sentences, *asks.choice_lines):
        compared = reply_asks.question_prose(choice_comparison_text(sentence))
        if compared and not any(item and (compared in item or item in compared) for item in registered):
            return QuestionReplyBlocked(f"Unregistered ask {sentence!r}: record the item with "
                                        + "update_registration.py questions --state and paste its reviewed question "
                                        + "into this reply.")
    return QuestionReplyReady()


def block_reason(instance_dirs: list[str], reply: str) -> str | None:
    if not instance_dirs:
        return None
    checked = check_reviewed_text(instance_dirs, reply)
    return checked.reason if isinstance(checked, QuestionReplyBlocked) else None


def counts_line(productions: list[Production], questions: int) -> str | None:
    """The status line's counts line: each production's counts and the open questions."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The counts read the production scripts, which import this module, so this one imports late.
    import footer_counts

    counts = footer_counts.total([footer_counts.read_counts(production.doc) for production in productions])
    return footer_counts.counts_line(counts._replace(questions=questions))
