#!/usr/bin/env python3
"""Register updates, questions and answers for a production.

Question records use the proposals folder or the owner's question folder.
Fixed staff ids identify their askers.
The JSON list stores ordinary reminders only.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NamedTuple, NotRequired, TypedDict, cast

from scripts.production.production_doc import Production, Refusal, read_production
from scripts.production.checkpoint_gate import report

from scripts.production import showrunners, session_staff, showrunner_tree, staff, unit_lookup
from scripts.production.proposals_folder import issues_folder
from scripts.production.store_owner import showrunner_id
from scripts.production.issues import Issue, frontmatter
from scripts.lib import urgency
from scripts.lib.urgency import Urgency, UrgencyError
from scripts.lib.atomic_file import write_text_atomic

from scripts.usage import agents_log

from scripts.lib import names
from scripts.lib.process_failure import failure_detail
from scripts.agents.remote import local_machine


class InputError(Exception):
    pass


STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
UNREADABLE_LIST = "needs-user list unreadable"
PLUMBING = re.compile(r"\b(?:berth|reservations?|incursions?|holders?|file holds?|file clash\w*|writers?|testers?|retak\w*|re-?shoot\w*)\b", re.IGNORECASE)

@dataclass(frozen=True)
class DueNow:
    pass


@dataclass(frozen=True)
class DeferredUntil:
    at: datetime


OutstandingTiming = DueNow | DeferredUntil


@dataclass(frozen=True)
class Ranking:
    """The item's urgency and the preceding item's text."""
    urgency: Urgency = Urgency()
    follows: str = ""


@dataclass(frozen=True)
class OutstandingReminder:
    since: datetime
    text: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()


@dataclass(frozen=True)
class OutstandingQuestion:
    since: datetime
    text: str
    question: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""


@dataclass(frozen=True)
class ReviewedOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reviewed: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""


@dataclass(frozen=True)
class ReviewUnavailableOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reason: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""


Outstanding = (OutstandingReminder | OutstandingQuestion | ReviewedOutstandingQuestion
               | ReviewUnavailableOutstandingQuestion)


def check_keys(fields: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise InputError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def plumbing(line: str) -> str | None:
    """Why the line fails the plumbing check, or None when it passes."""
    found = PLUMBING.search(line)
    if found is None:
        return None
    return (f"{found.group(0)!r} is the production's own plumbing or shorthand, which the user never sees; "
            + "say what changes in the app, or leave it out")


def read_ranking(fields: dict[str, object], where: str) -> Ranking:
    """An item's `urgency`, `due`, `marked` and `follows`; none of them is a normal item."""
    follows = fields.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise InputError(f"{where}.follows: expected one line of text")
    try:
        return Ranking(urgency.from_fields(fields), follows.strip())
    except UrgencyError as error:
        raise InputError(f"{where}: {error}") from None


def parse_outstanding_entries(entries: object, path: Path) -> list[Outstanding]:
    """Validate reminder and question fields from their owning stores."""
    if not isinstance(entries, list):
        raise InputError(f"{path}: expected a JSON list")
    items: list[Outstanding] = []
    for index, entry in enumerate(cast(list[object], entries)):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise InputError(f"{where}: expected an object")
        fields = cast(dict[str, object], entry)
        check_keys(fields, {"since", "text", "after", "question", "reviewed", "review_unavailable", "urgency", "due",
                            "marked", "follows", "asked_by"}, where)
        rank = read_ranking(fields, where)
        since, text, after = fields.get("since"), fields.get("text"), fields.get("after")
        if not isinstance(since, str) or not STARTED.match(since):
            raise InputError(f"{where}.since: expected YYYY-MM-DDTHH:MM")
        if after is not None and (not isinstance(after, str) or not STARTED.match(after)):
            raise InputError(f"{where}.after: expected YYYY-MM-DDTHH:MM")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise InputError(f"{where}.text: expected one non-empty line")
        # The plumbing check runs when `update_registration.py questions` writes an item, not here:
        # a stored item that fails it must not take the whole list down.
        timing = DeferredUntil(datetime.fromisoformat(after)) if isinstance(after, str) else DueNow()
        since_at = datetime.fromisoformat(since)
        question = fields.get("question")
        reviewed, unavailable = fields.get("reviewed"), fields.get("review_unavailable")
        asked_by = fields.get("asked_by", "")
        if not isinstance(asked_by, str) or "\n" in asked_by or ("asked_by" in fields and not asked_by.strip()):
            raise InputError(f"{where}.asked_by: expected one non-empty line")
        if "question" not in fields:
            if "reviewed" in fields or "review_unavailable" in fields or "asked_by" in fields:
                raise InputError(f"{where}: question required for a review result or an asker")
            items.append(OutstandingReminder(since_at, text, timing, rank))
            continue
        if not isinstance(question, str) or not question.strip():
            raise InputError(f"{where}.question: expected non-empty full question")
        if "reviewed" in fields:
            if not isinstance(reviewed, str) or not reviewed.strip() or "review_unavailable" in fields:
                raise InputError(f"{where}.reviewed: expected completed review without an unavailable result")
            items.append(ReviewedOutstandingQuestion(since_at, text, question, reviewed, timing, rank, asked_by))
        elif "review_unavailable" in fields:
            if not isinstance(unavailable, str):
                raise InputError(f"{where}.review_unavailable: expected reason text")
            items.append(ReviewUnavailableOutstandingQuestion(since_at, text, question, unavailable, timing, rank, asked_by))
        else:
            items.append(OutstandingQuestion(since_at, text, question, timing, rank, asked_by))
    return sorted(items, key=lambda item: item.since)


class ReportItem(TypedDict):
    """A report that the user requested until the specified time."""
    text: str
    until: str


def reports_path(name: str, *, identities: QuestionStaff | None = None) -> Path:
    path = outstanding_file(name, identities=identities)
    return path.parent.parent / "reports" / path.name


class InstancePresent(NamedTuple):
    output: str


class InstanceAbsent(NamedTuple):
    pass


class OutstandingItem(TypedDict):
    since: str
    text: str
    after: NotRequired[str]
    question: NotRequired[str]
    reviewed: NotRequired[str]
    review_unavailable: NotRequired[str]
    urgency: NotRequired[str]
    due: NotRequired[str]
    marked: NotRequired[str]
    follows: NotRequired[str]
    asked_by: NotRequired[str]


class QuestionRegistration(TypedDict):
    time: str
    text: str
    reviewed: NotRequired[str]
    question: NotRequired[str]
    withdrawn: NotRequired[str]


class AnsweredItem(TypedDict):
    time: str
    name: str
    text: str
    question: NotRequired[str]
    asked_by: NotRequired[str]


@dataclass(frozen=True)
class UnitAsker:
    unit: str


@dataclass(frozen=True)
class ShowrunnerAsker:
    pass


@dataclass(frozen=True)
class AskerNotRecorded:
    reason: str


@dataclass(frozen=True)
class OpenItemWithoutAsker:
    pass


@dataclass(frozen=True)
class StaffAsker:
    member_id: staff.StaffId


@dataclass(frozen=True)
class QuestionStaff:
    """One command's staff identities and current display labels."""

    record: staff.StaffSnapshot
    sources: session_staff.MemberSources

    @classmethod
    def read(cls) -> QuestionStaff:
        return cls.for_record(staff.snapshot())

    @classmethod
    def for_record(cls, record: staff.StaffSnapshot,
                   sources: session_staff.MemberSources | None = None) -> QuestionStaff:
        if sources is None:
            dependencies = staff.RecordInputs()
            sources = session_staff.member_sources(dependencies=dependencies)
        return cls(record, sources)

    def owner(self, document: Path) -> staff.StaffRow:
        """Select the production owner from the command's staff snapshot."""
        try:
            return unit_lookup.doc_owner(document, self.record)
        except staff.StaffUnreadable as error:
            raise RegistrationFailure("questions", str(error)) from error

    def asker(self, production: Production, unit: str = "") -> staff.StaffRow:
        parent = production.owner
        if not unit:
            return parent
        row = self.record.by_member_reference(unit)
        if row is not None:
            if row.kind != "unit" or row.parent != staff.ReportsTo(parent.id):
                raise RegistrationFailure("ask", f"{unit} is not a unit of {production.name}")
            return row
        selector = session_staff.UnitMember(production.name, unit)
        found = session_staff.unit_match(selector, self.record, parent=session_staff.RequiredParent(parent))
        if isinstance(found, session_staff.NoSuchMember):
            raise RegistrationFailure("ask", f"{unit} is not a unit of {production.name}")
        return found.row

    def display(self, row: staff.StaffRow) -> tuple[str, str]:
        parent = row if row.kind == "showrunner" else self.record.parent_of(row)
        return parent.name if parent else "", row.name

    def documents(self) -> dict[str, staff.StaffRow]:
        """Return local showrunner rows by name."""
        return {row.name: row for row in self.record.rows
                if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
                and row.doc.machine == local_machine()}

    def production_of(self, session_id: str) -> str | None:
        """Return the current showrunner's staff name."""
        row = self.record.by_current_session(session_id) if session_id else None
        return row.name if row is not None and row.kind == "showrunner" else None

    def names(self, name: str, below: bool = False) -> list[str]:
        """Select descendants through fixed parent ids, including owners without timers."""
        return showrunner_tree.user_facing(name, below, record=self.record)


QuestionAsker = StaffAsker | UnitAsker | ShowrunnerAsker | AskerNotRecorded | OpenItemWithoutAsker


def question_asker(fields: OutstandingItem | AnsweredItem,
                   missing: OpenItemWithoutAsker | AskerNotRecorded,
                   identities: QuestionStaff | None = None) -> QuestionAsker:
    name = fields.get("asked_by")
    if not isinstance(name, str) or not name.strip():
        return missing
    if identities is not None:
        row = identities.record.by_member_reference(name)
        if row is not None:
            return StaffAsker(row.id)
        if name != SHOWRUNNER_ASKER:
            return AskerNotRecorded(f"staff member {name} is unavailable")
    return ShowrunnerAsker() if name == SHOWRUNNER_ASKER else UnitAsker(name)


@dataclass(frozen=True)
class NoAnswerRelay:
    """The owning showrunner receives the page answer."""


@dataclass(frozen=True)
class AnswerRelay:
    """The CLI line and fixed recipient describe the same relay decision."""

    line: str
    recipient: staff.StaffId | NoAnswerRelay
    unit: str = ""


def answer_relay(production: Production, name: str, asker: QuestionAsker,
                 identities: QuestionStaff | None = None) -> AnswerRelay:
    identities = identities if identities is not None else QuestionStaff.read()

    def target(line: str, reference: str = "", unit: str = "") -> AnswerRelay:
        row = identities.record.by_member_reference(reference) if reference else None
        if row is None and reference:
            row = next((member for member in identities.record.rows
                        if member.kind == "showrunner" and member.name == reference), None)
        return AnswerRelay(line, row.id if row is not None else NoAnswerRelay(), unit)

    owner = "this production" if name == production.name else f"{name}, below this production"
    if isinstance(asker, OpenItemWithoutAsker):
        return target(f"relay: none; the item belongs to {owner}")
    if isinstance(asker, AskerNotRecorded):
        return target(f"relay: none; {asker.reason}; the item belongs to {owner}")
    if isinstance(asker, StaffAsker):
        row = identities.record.by_member_reference(asker.member_id)
        if row is None:
            return target(f"relay: none; staff member {asker.member_id} is unavailable")
        current_owner = production.owner
        current_name, label = identities.display(row)
        if row.kind == "showrunner":
            if row.id == current_owner.id:
                return target("relay: none; this showrunner asked it")
            loaded = names.runtime_names()
            label = loaded.job("showrunner") if isinstance(loaded, names.Names) else "requesting role (display name unavailable)"
        elif row.parent == staff.ReportsTo(current_owner.id):
            current_name = production.name
        if current_name != production.name:
            return target(f"relay: to the showrunner of {current_name}, for {label} (staff {row.id})", current_name, label)
        return target(f"relay: to {label} (staff {row.id})", row.id, label)
    if name != production.name:
        if isinstance(asker, UnitAsker):
            label = asker.unit
        else:
            loaded = names.runtime_names()
            label = loaded.job("showrunner") if isinstance(loaded, names.Names) else "requesting role (display name unavailable)"
        return target(f"relay: to the showrunner of {name}, for {label}", name, label)
    if isinstance(asker, ShowrunnerAsker):
        return target("relay: none; this showrunner asked it")
    return target(f"relay: to {asker.unit}", asker.unit, asker.unit)



def relay_line(production: Production, name: str, asker: QuestionAsker,
               identities: QuestionStaff | None = None) -> str:
    return answer_relay(production, name, asker, identities).line


def answered_items(outstanding: Path, now: datetime) -> list[AnsweredItem]:
    """Read the retained answers, converting JSON fields before replaying them."""
    history = outstanding.with_suffix(".answered.jsonl")
    if not history.exists():
        return []
    retained: list[AnsweredItem] = []
    for line in history.read_text(encoding="utf-8").splitlines():
        try:
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                continue
            fields = cast(dict[str, object], value)
            stamp, name, text = fields.get("time"), fields.get("name"), fields.get("text")
            if not isinstance(stamp, str) or not isinstance(name, str) or not isinstance(text, str):
                continue
            dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dated.tzinfo is None or dated < now - timedelta(hours=48):
                continue
            entry = AnsweredItem(time=stamp, name=name, text=text)
            question = fields.get("question")
            if isinstance(question, str):
                entry["question"] = question
            asker = fields.get("asked_by")
            if isinstance(asker, str) and asker.strip():
                entry["asked_by"] = asker
            retained.append(entry)
        except ValueError:
            continue
    return retained


def record_answer(outstanding: Path, name: str, item: OutstandingItem, now: datetime) -> None:
    retained = [entry for entry in answered_items(outstanding, now) if entry["text"] != item["text"]]
    entry = AnsweredItem(time=now.isoformat(), name=name, text=item["text"])
    if "question" in item:
        entry["question"] = item["question"]
    if "asked_by" in item:
        entry["asked_by"] = item["asked_by"]
    write_text_atomic(outstanding.with_suffix(".answered.jsonl"),
                      "".join(json.dumps(saved) + "\n" for saved in [*retained, entry]))


def replay_answer(production: Production, item: AnsweredItem, identities: QuestionStaff | None = None) -> None:
    dated = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
    print(f"already answered {dated:%a %b %d %H:%M}")
    print(relay_line(production, item["name"],
                     question_asker(item, AskerNotRecorded("asked_by not recorded in answered record"), identities), identities))
    report("answer", "ok", f"{item['name']}: {item['text']}")


class UnitJudgment(NamedTuple):
    unit: str
    phase: str
    wait: str


class OpenReminder(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


class QuestionForReview(NamedTuple):
    text: str
    after: str
    question: str
    rank: Ranking = Ranking()


class OpenQuestionWithoutText(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


OpenForUserItem = OpenReminder | QuestionForReview | OpenQuestionWithoutText


class KeepOutstanding(NamedTuple):
    pass


class ReplaceOutstanding(NamedTuple):
    items: tuple[OpenForUserItem, ...]


class KeepReports(NamedTuple):
    pass


class ReplaceReports(NamedTuple):
    items: tuple[ReportItem, ...]


class ShowrunnerState(NamedTuple):
    units: tuple[UnitJudgment, ...]
    merges_held: tuple[str, ...]
    outstanding: KeepOutstanding | ReplaceOutstanding
    reports: KeepReports | ReplaceReports = KeepReports()
    # What the showrunner owes and what sets each off; a resume acts on it first.
    owed: tuple[str, ...] = ()


class RegistrationFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def command(step: str, arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RegistrationFailure(step, failure_detail(result))
    return result.stdout.strip()


def notifier(arguments: list[str]) -> str:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    return command("notifier", ["zsh", str(script), *arguments])


def remove_updates(owner: staff.StaffRow) -> None:
    """Remove the generated schedule files and retain reminders and history."""
    _ = notifier(["remove", f"showrunner-{owner.id}"])
    root = _showrunner_state_root()
    (root / "counts" / owner.id).unlink(missing_ok=True)


def instance(name: str) -> InstancePresent | InstanceAbsent:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    result = subprocess.run(["zsh", str(script), "status", name], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return InstancePresent(result.stdout.strip())
    if "no instance" in result.stdout + result.stderr or "no such instance" in result.stdout + result.stderr:
        return InstanceAbsent()
    raise RegistrationFailure("notifier", f"status: {failure_detail(result)}")


class ScheduledUpdates(NamedTuple):
    minutes: int
    aligned: bool


DEFAULT_MINUTES = 15


class UpdatesOnDemand(NamedTuple):
    """The document has no scheduled update interval."""


def update_schedule(lines: list[str]) -> ScheduledUpdates | UpdatesOnDemand:
    line = next((line for line in lines if line.startswith("- **Updates:**")), "")
    match = re.search(r"every (\d+) minutes", line)
    # Quiet by default (user, 2026-10-13): only a line asking for a schedule makes one.
    if match is None or re.search(r"\bon demand\b", line):
        return UpdatesOnDemand()
    minutes = int(match.group(1))
    if minutes <= 0:
        raise RegistrationFailure("prompt", "Updates interval must be positive")
    return ScheduledUpdates(minutes, "on the hour" in line)


def scheduled_prompt(minutes: int, production: Production) -> str:
    source = Path(__file__).resolve().parents[2] / "commands/showrunner/produce.md"
    document = source.read_text(encoding="utf-8")
    if "The prompt:\n" not in document:
        raise RegistrationFailure("register", "The prompt: marker is missing")
    tail = document.split("The prompt:\n", 1)[1]
    if "\n**A tick**" not in tail:
        raise RegistrationFailure("register", "**A tick** marker is missing")
    template = tail.split("\n**A tick**", 1)[0]
    prompt = "\n".join(line.removeprefix("> ").removeprefix(">") for line in template.strip().splitlines())
    prompt = prompt.replace("<N>", str(minutes)).replace("<zone>", str(production.zone)).replace("<showrunner name>", production.name)
    prompt = prompt.replace("<STATE_DIR>", str(showrunners.session_state_dir(production.owner.id)))
    return prompt


def adopt_moved_state(production: Production, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Copy missing scratchpad state."""
    done: list[str] = []
    try:
        record = staff.snapshot() if record is None else record
        identity = production.owner.id
        copied = showrunners.adopt_session_state(identity, record=record)
        if copied:
            done.append(f"copied {', '.join(copied)} to {showrunners.session_state_dir(identity)}")
    except (OSError, UnicodeError, RegistrationFailure, staff.StaffRefused, staff.StaffUnreadable) as error:
        done.append(f"state move skipped: {error}")
    return done


def register(production: Production) -> None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        raise RegistrationFailure("register", "CLAUDE_CODE_SESSION_ID is required")
    try:
        agents_log.append(session_id, "showrunner", None, datetime.now(timezone.utc))
    except Exception as exc:
        # Recording must never interrupt registration.
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)
    relative = production.doc.relative_to(production.checkout)
    dirty = command("register", ["git", "status", "--porcelain", "--", str(relative)], cwd=production.checkout)
    if dirty:
        raise RegistrationFailure("register", "the production doc has uncommitted edits; commit or discard them first")
    # The staff row owns the timer.
    owner = production.owner
    updates = f"showrunner-{owner.id}"
    _ = notifier(["new", updates, "--to", f"session:{owner.id}", "--every", str(DEFAULT_MINUTES),
                  "--prompt", "scheduled", "--from", f"showrunner-timer-{production.name}"])
    _ = notifier(["stop", updates])
    if isinstance(instance("stall-watch"), InstanceAbsent):
        run = f"{Path.home()}/.claude/scripts/lib/py {Path.home()}/.claude/scripts/production/stall_watch.py"
        _ = notifier(["new", "stall-watch", "--every", "1", "--run", run])
    report("register", "ok", f"{updates} registered; automatic reports are off")


def production_time(production: Production, stamp: str) -> str:
    zone = production.zone
    if re.fullmatch(r"\d{1,2}:\d{2}", stamp):
        today = datetime.now(zone).date()
        hour, minute = (int(part) for part in stamp.split(":"))
        moment = datetime(today.year, today.month, today.day, hour, minute, tzinfo=zone)
    else:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return moment.strftime("%H:%M %Z")


def state_error(step: str, path: Path, key: str, expected: str) -> RegistrationFailure:
    return RegistrationFailure(step, f"{path}: {key} must be {expected}")


def judgment_state(path: Path | None, step: str) -> ShowrunnerState:
    if path is None:
        return ShowrunnerState((), (), KeepOutstanding())
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise state_error(step, path, "root", "an object")
    fields = cast(dict[str, object], value)
    for key in fields:
        if key not in {"units", "merges_held", "open_for_user", "report_to_user", "next"}:
            raise state_error(step, path, key, "a known key")
    raw_units = fields.get("units", [])
    if not isinstance(raw_units, list):
        raise state_error(step, path, "units", "a list")
    units: list[UnitJudgment] = []
    for index, raw in enumerate(cast(list[object], raw_units)):
        key = f"units[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        unit = cast(dict[str, object], raw)
        for field in unit:
            if field not in {"unit", "phase", "wait"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        for field in ("unit", "phase", "wait"):
            if field in unit and not isinstance(unit[field], str):
                raise state_error(step, path, f"{key}.{field}", "text")
        units.append(UnitJudgment(cast(str, unit.get("unit", "")),
                                  cast(str, unit.get("phase", "not stated")),
                                  cast(str, unit.get("wait", "not stated"))))
    raw_held = fields.get("merges_held", [])
    if not isinstance(raw_held, list) or not all(isinstance(item, str) for item in cast(list[object], raw_held)):
        raise state_error(step, path, "merges_held", "a list of text")
    raw_next = fields.get("next", [])
    if not isinstance(raw_next, list) or not all(isinstance(item, str) and item.strip() and "\n" not in item
                                                 for item in cast(list[object], raw_next)):
        raise state_error(step, path, "next", "a list of one-line text")
    outstanding: KeepOutstanding | ReplaceOutstanding = KeepOutstanding()
    if "open_for_user" in fields:
        raw_open = fields["open_for_user"]
        if not isinstance(raw_open, list):
            raise state_error(step, path, "open_for_user", "a list")
        items: list[OpenForUserItem] = []
        for index, raw in enumerate(cast(list[object], raw_open)):
            key = f"open_for_user[{index}]"
            if isinstance(raw, str):
                if not raw.strip() or "\n" in raw:
                    raise state_error(step, path, key, "one nonempty line of text")
                items.append(OpenReminder(raw, ""))
                continue
            if not isinstance(raw, dict):
                raise state_error(step, path, key, "text or an object")
            item = cast(dict[str, object], raw)
            for field in item:
                if field not in {"text", "after", "question", "urgency", "due", "follows"}:
                    raise state_error(step, path, f"{key}.{field}", "a known key")
            rank = item_ranking(item, path, step, key)
            if not isinstance(item.get("text"), str) or not cast(str, item["text"]).strip() or "\n" in cast(str, item["text"]):
                raise state_error(step, path, f"{key}.text", "one nonempty line of text")
            after = item.get("after", "")
            if not isinstance(after, str) or (after and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", after) is None):
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM")
            try:
                if after:
                    _ = datetime.fromisoformat(after)
            except ValueError:
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM") from None
            if "question" in item:
                question = item["question"]
                if not isinstance(question, str) or not question.strip():
                    raise state_error(step, path, f"{key}.question", "nonempty text")
                items.append(QuestionForReview(cast(str, item["text"]), after, question, rank))
            else:
                items.append(OpenQuestionWithoutText(cast(str, item["text"]), after, rank))
        outstanding = ReplaceOutstanding(tuple(items))
    return ShowrunnerState(tuple(units), tuple(cast(list[str], raw_held)), outstanding,
                           report_items(fields, path, step), tuple(cast(list[str], raw_next)))


def item_ranking(item: dict[str, object], path: Path, step: str, key: str) -> Ranking:
    """An item's `urgency` (user, blocks, soon, normal or later), `due` (with soon) and `follows`
    (the text of the item it comes after); none of them is a normal item."""
    follows = item.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise state_error(step, path, f"{key}.follows", "one line of text")
    level, due = item.get("urgency", ""), item.get("due", "")
    if not isinstance(level, str) or not isinstance(due, str):
        raise state_error(step, path, f"{key}.urgency", "text")
    try:
        return Ranking(urgency.read(level, due), follows.strip())
    except urgency.UrgencyError:
        raise state_error(step, path, f"{key}.urgency", "user, blocks, soon with `due`, normal or later") from None


def report_items(fields: dict[str, object], path: Path, step: str) -> KeepReports | ReplaceReports:
    """`report_to_user`: what the user asked to be told about, each until an ISO time."""
    if "report_to_user" not in fields:
        return KeepReports()
    raw_reports = fields["report_to_user"]
    if not isinstance(raw_reports, list):
        raise state_error(step, path, "report_to_user", "a list")
    reports: list[ReportItem] = []
    for index, raw in enumerate(cast(list[object], raw_reports)):
        key = f"report_to_user[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        item = cast(dict[str, object], raw)
        for field in item:
            if field not in {"text", "until"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        text, until = item.get("text"), item.get("until")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise state_error(step, path, f"{key}.text", "one nonempty line of text")
        if not isinstance(until, str):
            raise state_error(step, path, f"{key}.until", "an ISO time")
        try:
            _ = datetime.fromisoformat(until)
        except ValueError:
            raise state_error(step, path, f"{key}.until", "an ISO time") from None
        reports.append(ReportItem(text=text, until=until))
    return ReplaceReports(tuple(reports))


def _showrunner_state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def outstanding_file(reference: str, *, identities: QuestionStaff | None = None) -> Path:
    root = _showrunner_state_root()
    identities = identities if identities is not None else QuestionStaff.read()
    identity = showrunner_id(reference, record=identities.record)
    return root / "outstanding" / f"{identity}.json"


def outstanding_path(production: Production) -> Path:
    root = _showrunner_state_root()
    owner = production.owner
    return root / "outstanding" / f"{owner.id}.json"


@dataclass(frozen=True)
class QuestionFolder:
    """The question folder and its fixed production owner."""

    path: Path
    owner: staff.StaffId


@dataclass(frozen=True)
class QuestionOwnerUnavailable:
    """The document has no local showrunner row to own its questions."""

    document: Path


def question_folder(document: Path, identities: QuestionStaff, *,
                    lines: list[str] | None = None) -> QuestionFolder | QuestionOwnerUnavailable:
    """Select the production's question folder from its document and staff context."""
    owner = identities.record.by_doc(local_machine(), document)
    if owner is None or owner.kind != "showrunner":
        return QuestionOwnerUnavailable(document)
    assert isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
    folder = issues_folder(owner.doc.path, lines=lines)
    root = _showrunner_state_root()
    return QuestionFolder(folder if folder is not None else root / owner.id / "questions", owner.id)


@dataclass(frozen=True)
class QuestionRecord:
    """A question issue with a fixed asker and its existing queue fields."""

    path: Path
    owner: staff.StaffId
    asker: staff.StaffId
    item: OutstandingItem
    status: str


def question_records(production: Production, identities: QuestionStaff) -> list[QuestionRecord]:
    return question_records_for_document(production.doc, identities)


def question_records_for_document(doc: Path, identities: QuestionStaff, *, include_unrouted: bool = False,
                                  lines: list[str] | None = None) -> list[QuestionRecord]:
    """Read canonical question records without requiring unrelated checkpoint fields."""
    folder = question_folder(doc, identities, lines=lines)
    if isinstance(folder, QuestionOwnerUnavailable):
        source = issues_folder(doc, lines=lines) if include_unrouted else None
        if source is None:
            return []
    else:
        source = folder.path
    issues = [Issue(path, frontmatter(path)) for path in sorted(source.glob("question-*.md"))]
    if isinstance(folder, QuestionFolder):
        issues = [issue for issue in issues if issue.value("production_id") == folder.owner]
    return _question_records(issues)


def unowned_question_records(identities: QuestionStaff) -> list[QuestionRecord]:
    """Read state folders whose owners have no live showrunner row."""
    owners = {row.id for row in identities.record.rows if row.kind == "showrunner"}
    paths = sorted(_showrunner_state_root().glob("*/questions/question-*.md"))
    return _question_records([Issue(path, frontmatter(path)) for path in paths if path.parent.parent.name not in owners])


def _question_records(issues: list[Issue]) -> list[QuestionRecord]:
    """Read canonical question fields from their issue records."""
    questions = [issue for issue in issues if issue.value("record_type") == "question"]
    if not questions:
        return []
    records: list[QuestionRecord] = []
    for issue in questions:
        path = issue.path
        try:
            raw = cast(object, json.loads(issue.value("registration")))
        except ValueError as error:
            raise RegistrationFailure("questions", f"{path}: invalid question registration: {error}") from error
        if not isinstance(raw, dict):
            raise RegistrationFailure("questions", f"{path}: invalid question registration")
        fields = cast(dict[str, object], raw)
        _ = parse_outstanding_entries([fields], path)
        item = cast(OutstandingItem, cast(object, fields))
        asker = item.get("asked_by", "")
        if not asker or asker != issue.value("asked_by"):
            raise RegistrationFailure("questions", f"{path}: invalid question asker")
        status = issue.value("status")
        if status not in {"open", "closed"}:
            raise RegistrationFailure("questions", f"{path}: question status must be open or closed")
        records.append(QuestionRecord(path, staff.StaffId(issue.value("production_id")), staff.StaffId(asker), item, status))
    return records


def close_questions(document: Path, identities: QuestionStaff) -> None:
    """Close the production's questions and retain their records for history."""
    _ = identities.owner(document)
    with outstanding_lock(outstanding_file(identities.owner(document).id, identities=identities)):
        for record in question_records_for_document(document, identities):
            if record.status == "open":
                write_question_record(QuestionRecord(record.path, record.owner, record.asker, record.item, "closed"))


def write_question_record(record: QuestionRecord) -> None:
    item = record.item
    if "question" not in item:
        raise RegistrationFailure("questions", f"{record.path}: the question text is missing")
    text = "\n".join([
        "---", "record_type: question", "tags: [question]", f"status: {record.status}",
        f"production_id: {record.owner}", f"asked_by: {record.asker}",
        "registration: " + json.dumps(item, ensure_ascii=False), "---", "",
        f"# {item['text']}", "", item["question"], "",
    ])
    if not record.path.exists() or record.path.read_text(encoding="utf-8") != text:
        write_text_atomic(record.path, text)


def store_questions(production: Production, entries: list[OutstandingItem], identities: QuestionStaff) -> None:
    """Store questions in their records and close questions absent from the new list."""
    questions = [entry for entry in entries if "question" in entry]
    existing = question_records(production, identities)
    if not questions and not existing:
        return
    folder = question_folder(production.doc, identities)
    if isinstance(folder, QuestionOwnerUnavailable):
        raise RegistrationFailure("questions", f"{folder.document}: no showrunner staff row")
    owner = folder.owner
    folder.path.mkdir(parents=True, exist_ok=True)
    open_paths: set[Path] = set()
    for item in questions:
        row = question_member(production, item, identities)
        item["asked_by"] = row.id
        digest = hashlib.sha256(f"{owner}:{row.id}:{item['text']}".encode()).hexdigest()
        path = next((record.path for record in existing
                     if record.asker == row.id and record.item["text"] == item["text"]),
                    folder.path / f"question-{row.id}-{digest[:16]}.md")
        open_paths.add(path)
        write_question_record(QuestionRecord(path, owner, row.id, item, "open"))
    for record in existing:
        if record.status == "open" and record.path not in open_paths:
            write_question_record(QuestionRecord(record.path, record.owner, record.asker, record.item, "closed"))


def question_member(production: Production, item: OutstandingItem, identities: QuestionStaff) -> staff.StaffRow:
    reference = item.get("asked_by", "")
    row = identities.record.by_member_reference(reference) if reference else None
    if row is None:
        row = identities.asker(production, "" if reference == SHOWRUNNER_ASKER else reference)
    return row


def migrate_questions(production: Production, *, identities: QuestionStaff | None = None) -> None:
    """Move legacy question identities into issue records without changing queue keys."""
    identities = identities if identities is not None else QuestionStaff.read()
    path = outstanding_path(production)
    with outstanding_lock(path):
        legacy = legacy_entries(path)
        existing = open_entries(production, identities)
        recorded = {(record.asker, record.item["text"]) for record in question_records(production, identities)}
        questions = {(question_member(production, entry, identities).id, entry["text"]): entry
                     for entry in existing if "question" in entry}
        for entry in legacy:
            if "question" in entry:
                key = (question_member(production, entry, identities).id, entry["text"])
                if key not in questions and key not in recorded:
                    questions[key] = entry
        reminders = [entry for entry in legacy if "question" not in entry]
        store_questions(production, [*reminders, *questions.values()], identities)
        if any("question" in entry for entry in legacy):
            write_text_atomic(path, json.dumps(reminders) + "\n")
    report("migrate-questions", "ok", "recorded")


def questions_table(production: Production, *, identities: QuestionStaff | None = None) -> None:
    """Print question issues without including proposals or ordinary reminders."""
    identities = identities if identities is not None else QuestionStaff.read()
    records = [record for record in question_records(production, identities) if record.status == "open"]
    if not records:
        print("no open questions")
        return
    print("| Asked by | Question | Date | State |")
    print("| --- | --- | --- | --- |")
    now = datetime.now(production.zone).replace(tzinfo=None)
    for record in records:
        row = identities.record.by_member_reference(record.asker)
        label = identities.display(row)[1] if row is not None else f"unavailable staff {record.asker}"
        item = record.item
        state = "waiting for your answer"
        after = item.get("after", "")
        if after and datetime.fromisoformat(after) > now:
            state = f"deferred until {after}"
        if "review_unavailable" in item:
            state += "; review unavailable"
        cells = (label, item["text"], item["since"], state)
        print("| " + " | ".join(" ".join(cell.split()).replace("|", "\\|") for cell in cells) + " |")


@contextmanager
def outstanding_lock(outstanding: Path) -> Generator[None]:
    """Lock the list during reads and writes. Never take this lock twice in one process."""
    outstanding.parent.mkdir(parents=True, exist_ok=True)
    with outstanding.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def legacy_entries(outstanding: Path) -> list[OutstandingItem]:
    """Read legacy entries only for migration and reminder filtering."""
    if not outstanding.exists():
        return []
    raw = cast(object, json.loads(outstanding.read_text(encoding="utf-8")))
    _ = parse_outstanding_entries(raw, outstanding)
    return cast(list[OutstandingItem], raw)


def stored_entries(outstanding: Path) -> list[OutstandingItem]:
    """Read reminders without treating legacy JSON questions as live records."""
    if not outstanding.exists():
        return []
    raw = cast(object, json.loads(outstanding.read_text(encoding="utf-8")))
    if not isinstance(raw, list):
        raise InputError(f"{outstanding}: expected a JSON list")
    reminders: list[object] = [cast(object, entry) for entry in cast(list[object], raw)
                 if not isinstance(entry, dict) or "question" not in entry]
    _ = parse_outstanding_entries(reminders, outstanding)
    return cast(list[OutstandingItem], reminders)


def open_entries(production: Production, identities: QuestionStaff) -> list[OutstandingItem]:
    """Read reminders and open question records from their owning stores."""
    return [*stored_entries(outstanding_path(production)),
            *(record.item.copy() for record in question_records(production, identities) if record.status == "open")]


class QuestionStoreUnreadable(InputError, ValueError):
    """The canonical question store or its reminder list is invalid."""


def read_open_fields(path: Path | None, *, production: Production | None = None,
                     identities: QuestionStaff | None = None, document: Path | None = None) -> list[OutstandingItem]:
    """Read reminder fields and canonical question fields with one identity context."""
    document = production.doc if production is not None else document
    if path is None and document is None:
        return []
    try:
        entries = stored_entries(path) if path is not None else []
        identities = identities if identities is not None else QuestionStaff.read()
        if document is None:
            row = identities.record.by_id(staff.StaffId(cast(Path, path).stem))
            document = (row.doc.path if row is not None and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
                        else None)
            if document is None:
                return entries
        entries.extend(record.item.copy() for record in question_records_for_document(document, identities)
                       if record.status == "open")
        return entries
    except (RegistrationFailure, InputError) as error:
        raise QuestionStoreUnreadable(str(error)) from error


def read_open_items(path: Path | None, *, production: Production | None = None,
                    identities: QuestionStaff | None = None, document: Path | None = None) -> list[Outstanding]:
    """Read reminders and canonical questions for one production."""
    entries = read_open_fields(path, production=production, identities=identities, document=document)
    location = path or document or (production.doc if production is not None else Path("questions"))
    return parse_outstanding_entries(entries, location)


def due(items: list[Outstanding], now: datetime) -> list[Outstanding]:
    """Return the items whose deferred time has passed."""
    return [item for item in items if isinstance(item.timing, DueNow) or item.timing.at <= now]


def read_all_open_fields() -> list[OutstandingItem]:
    """Read local open fields with one staff context and skip unreadable stores."""
    root = _showrunner_state_root()
    paths = {path.stem: path for path in (root / "outstanding").glob("*.json")}
    fields: list[OutstandingItem] = []
    identities = QuestionStaff.for_record(staff.initial_snapshot())
    documents: dict[str, Path] = {row.id: cast(staff.LiveDoc | staff.WrappedDoc, row.doc).path
                                  for row in identities.documents().values()}
    for identity in sorted(set(paths) | set(documents)):
        try:
            fields.extend(read_open_fields(paths.get(identity), document=documents.get(identity), identities=identities))
        except (OSError, ValueError):
            continue
    return fields


def write_open_entries(production: Production, entries: list[OutstandingItem], identities: QuestionStaff) -> None:
    """Keep unrecorded legacy questions before replacing their JSON list."""
    path = outstanding_path(production)
    legacy = [entry for entry in legacy_entries(path) if "question" in entry]
    if legacy:
        recorded = {(record.asker, record.item["text"]) for record in question_records(production, identities)}
        supplied = {(question_member(production, entry, identities).id, entry["text"])
                    for entry in entries if "question" in entry}
        entries = list(entries)
        for entry in legacy:
            key = (question_member(production, entry, identities).id, entry["text"])
            if key not in recorded and key not in supplied:
                entries.append(entry)
                supplied.add(key)
    store_questions(production, entries, identities)
    reminders = [entry for entry in entries if "question" not in entry]
    text = json.dumps(reminders) + "\n"
    if reminders or path.exists():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            write_text_atomic(path, text)


# An item can wait longer than a week, so the day is named in full.
WHEN = "%a %b %d %H:%M"


class OpenItem(NamedTuple):
    name: str
    item: Outstanding
    asker_label: str = ""

    def line(self) -> str:
        """The item in full, led by its production and any level it has."""
        text = self.item.text if isinstance(self.item, OutstandingReminder) else self.item.question
        level = urgency.cell(self.item.rank.urgency)
        asker = f", asked by {self.asker_label}" if self.asker_label else ""
        return f"{self.name}{f' [{level}]' if level else ''}, since {self.item.since:{WHEN}}{asker}: {text}"


class OpenLists(NamedTuple):
    due: list[urgency.Entry[OpenItem]]
    deferred: list[OpenItem]
    unreadable: list[str]


def question_queue(production: Production, below: bool = False) -> str:
    """The queue name for the production question that /next displayed."""
    return urgency.scope_queue(f"question-{production.owner.id}", below)


def item_key(name: str, text: str) -> str:
    return f"{name}: {text}"


def open_lists(production: Production, below: bool = False, *, identities: QuestionStaff | None = None) -> OpenLists:
    """Read due items, deferred items and unreadable lists from the selected productions."""
    identities = identities if identities is not None else QuestionStaff.read()
    now = datetime.now(production.zone).replace(tzinfo=None)
    due: list[urgency.Entry[OpenItem]] = []
    deferred: list[OpenItem] = []
    unreadable: list[str] = []
    documents = identities.documents()
    for name in identities.names(production.name, below):
        if name not in documents:
            print(f"questions: {name}: ownership evidence is unavailable", file=sys.stderr)
            unreadable.append(name)
            continue
        try:
            items = read_open_items(outstanding_file(name, identities=identities),
                                    production=production if name == production.name else None, identities=identities)
        except (InputError, OSError, ValueError):
            unreadable.append(name)
            continue
        for item in items:
            label = ""
            if not isinstance(item, OutstandingReminder) and item.asked_by:
                asker = identities.record.by_member_reference(item.asked_by)
                label = identities.display(asker)[1] if asker is not None else item.asked_by
            opened = OpenItem(name, item, label)
            if isinstance(item.timing, DeferredUntil) and item.timing.at > now:
                deferred.append(opened)
            else:
                owner = documents[name].id
                due.append(urgency.Entry(item_key(owner, item.text), opened, item.rank.urgency,
                                         item.since, after=item_key(owner, item.rank.follows) if item.rank.follows else ""))
    deferred.sort(key=lambda found: (cast(DeferredUntil, found.item.timing).at, found.name))
    return OpenLists(urgency.queue_order(due, question_queue(production, below)), deferred, unreadable)


def open_items(production: Production) -> None:
    """Print in full what waits on the user: this production's own items.

    Replies show only the count, so this list is what the showrunner gives when the user asks what
    the open questions are. Each line is led by the production that owns the item.
    """
    lists = open_lists(production)
    for number, entry in enumerate(lists.due, 1):
        print(f"{number}. {entry.item.line()}")
    for found in lists.deferred:
        print(f"deferred until {cast(DeferredUntil, found.item.timing).at:{WHEN}}: {found.name}: "
              + (found.item.text if isinstance(found.item, OutstandingReminder) else found.item.question))
    for name in lists.unreadable:
        print(f"{name}: {UNREADABLE_LIST}")
    report("open", "ok", f"{len(lists.due)} due, {len(lists.deferred)} deferred"
           + (f", {len(lists.unreadable)} unreadable" if lists.unreadable else ""))


def next_item(production: Production) -> None:
    """Print the one question /next shows: the one shown last until it is answered, else the first due."""
    lists = open_lists(production)
    shown = urgency.top(lists.due, question_queue(production))
    print(shown.item.line() if shown is not None else "no question is open")
    report("next", "ok", f"{max(len(lists.due) - 1, 0)} more due")


def mark_important(production: Production, words: str) -> None:
    """Mark the one item whose text is `words`, or holds them, as the user's: it goes first after the shown one."""
    identities = QuestionStaff.read()
    wanted = words.strip()
    exact: list[tuple[Path, str]] = []
    partial: list[tuple[Path, str]] = []
    # The item the user names may come from an all-below /next, so the lists below are searched too.
    for name in identities.names(production.name, below=True):
        for item in read_open_items(outstanding_file(name, identities=identities),
                                    production=production if name == production.name else None, identities=identities):
            full = item.text if isinstance(item, OutstandingReminder) else item.question
            if item.text == wanted:
                exact.append((outstanding_file(name, identities=identities), item.text))
            elif wanted and (wanted in item.text or wanted in full):
                partial.append((outstanding_file(name, identities=identities), item.text))
    found = exact or partial
    if len(found) != 1:
        raise RegistrationFailure("mark-important", f"{len(found)} open items match {wanted!r}; name one by its text")
    path, text = found[0]
    with outstanding_lock(path):
        owner = production if path == outstanding_path(production) else read_production(path.stem, identities.record)
        entries = open_entries(owner, identities)
        entry = next(entry for entry in entries if entry.get("text") == text)
        for key in ("urgency", "due", "marked"):
            _ = entry.pop(key, None)
        store_ranking(entry, Ranking(urgency.mark(datetime.now(production.zone).replace(tzinfo=None))))
        write_open_entries(owner, entries, identities)
    report("mark-important", "ok", text)


# What `ask` records as the asker when the showrunner files its own question.
SHOWRUNNER_ASKER = "showrunner"


def ask(production: Production, text: str, question: str, unit: str, rank: Ranking, *,
        identities: QuestionStaff | None = None) -> None:
    """Review and file a question with its asker's fixed staff id."""
    identities = identities if identities is not None else QuestionStaff.read()
    asker = identities.asker(production, unit)
    if not text.strip() or "\n" in text:
        raise RegistrationFailure("ask", "--text must be one nonempty line")
    if not question.strip():
        raise RegistrationFailure("ask", "--question must hold the full question")
    problem = plumbing(text)
    if problem is not None:
        raise RegistrationFailure("ask", f"{text}: {problem}")
    outstanding = outstanding_path(production)
    if any(entry["text"] == text for entry in open_entries(production, identities)):
        raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
    # The review can take a minute, so it runs before the lock: `questions` is not held up by it.
    accepted = review_open_question(production.name, QuestionForReview(text, "", question, rank))
    if isinstance(accepted, HeldRegistrationQuestion):
        raise RegistrationFailure("ask", f"question held for review: {text}: {accepted.details}")
    now = datetime.now(production.zone)
    entry = OutstandingItem(since=now.strftime("%Y-%m-%dT%H:%M"), text=text, question=question,
                            asked_by=asker.id)
    if isinstance(accepted, ReviewedQuestion):
        entry["reviewed"] = accepted.reviewed
    else:
        entry["review_unavailable"] = accepted.reason
    store_ranking(entry, rank)
    with outstanding_lock(outstanding):
        entries = open_entries(production, identities)
        if any(stored["text"] == text for stored in entries):
            raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
        entries.append(entry)
        record_question_history(outstanding, [entry], now)
        write_open_entries(production, entries, identities)
    filed = accepted.reviewed if isinstance(accepted, ReviewedQuestion) else question
    print("filed question: " + filed)
    report("ask", "ok", f"{text}: on the open-questions list of {production.name}, not shown to the user")


def answer(production: Production, words: str, *, identities: QuestionStaff | None = None,
           only_open: bool = False) -> AnswerRelay:
    """Answer one item and resolve its asker's current staff row for relay."""
    identities = identities if identities is not None else QuestionStaff.read()
    wanted = words.strip()
    exact: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    answered_exact: list[AnsweredItem] = []
    answered_partial: list[AnsweredItem] = []
    now = datetime.now(production.zone)
    names = [production.name] if only_open else identities.names(production.name, below=True)
    for name in names:
        for item in read_open_items(outstanding_file(name, identities=identities),
                                    production=production if name == production.name else None, identities=identities):
            full = item.text if isinstance(item, OutstandingReminder) else item.question
            if item.text == wanted:
                exact.append((name, item.text))
            elif wanted and (wanted in item.text or wanted in full):
                partial.append((name, item.text))
        for saved in answered_items(outstanding_file(name, identities=identities), now):
            # The fixed file owner supplies the current display name.
            saved["name"] = name
            if saved["text"] == wanted:
                answered_exact.append(saved)
            elif wanted and (wanted in saved["text"] or wanted in saved.get("question", "")):
                answered_partial.append(saved)
    found = exact if only_open else exact or answered_exact or partial or answered_partial
    if len(found) != 1:
        raise RegistrationFailure("answer", f"{len(found)} open items match {wanted!r}; name one by its text")
    selected = found[0]
    if not isinstance(selected, tuple):
        replay_answer(production, selected, identities)
        return answer_relay(production, selected["name"], question_asker(
            selected, AskerNotRecorded("asked_by not recorded in answered record"), identities), identities)
    name, text = selected
    removed = remove_open_item(production, name, text, identities, "answer",
                               lambda path, item, now: record_answer(path, name, item, now))
    relay = answer_relay(production, name, question_asker(removed, OpenItemWithoutAsker(), identities), identities)
    print(relay.line)
    report("answer", "ok", f"{name}: {text}")
    return relay


def remove_open_item(production: Production, name: str, text: str, identities: QuestionStaff, verb: str,
                     record: Callable[[Path, OutstandingItem, datetime], None]) -> OutstandingItem:
    """Record and remove one open item under its owner's lock."""
    path = outstanding_file(name, identities=identities)
    with outstanding_lock(path):
        owner = production if name == production.name else read_production(name, identities.record)
        entries = open_entries(owner, identities)
        removed = next((entry for entry in entries if entry["text"] == text), None)
        if removed is None:
            raise RegistrationFailure(verb, f"{text}: no longer open in {name}")
        remaining = [entry for entry in entries if entry["text"] != text]
        if "question" in removed:
            removed["asked_by"] = question_member(owner, removed, identities).id
        record(path, removed, datetime.now(production.zone))
        write_open_entries(owner, remaining, identities)
    return removed


def withdraw(production: Production, words: str, *, identities: QuestionStaff | None = None) -> None:
    """Remove one exact open item without an answer or a relay."""
    identities = identities if identities is not None else QuestionStaff.read()
    wanted = words.strip()
    found = [(name, item.text) for name in identities.names(production.name, below=True)
             for item in read_open_items(outstanding_file(name, identities=identities),
                                        production=production if name == production.name else None, identities=identities)
             if item.text == wanted]
    if len(found) != 1:
        raise RegistrationFailure("withdraw", f"{len(found)} open items match {wanted!r}; name one by its exact text")
    name, text = found[0]
    _ = remove_open_item(production, name, text, identities, "withdraw",
                         lambda path, item, now: record_question_history(path, [item], now, withdrawn=True))
    report("withdraw", "ok", f"{name}: {text}")


def state_block(production: Production, state: ShowrunnerState, timestamp: str) -> str:
    from scripts.production.merge_checkpoint import merged_checkpoints

    judgments = {unit.unit: unit for unit in state.units}
    unit_lines: list[str] = []
    record = staff.snapshot()
    checkpoints = {entry.unit: entry for entry in merged_checkpoints(production, record=record)}
    for row in record.find(kind="unit", parent=production.owner.id):
        name = row.name
        judgment = judgments.get(row.id)
        entry = checkpoints.get(name)
        last = f"phase {entry.phase} ({entry.commit[:7]})" if entry is not None else "none"
        phase = judgment.phase if judgment is not None else "not stated"
        wait = judgment.wait if judgment is not None else "not stated"
        unit_lines.append(f"{name}: {phase}; last merged {last}; waits on {wait}")
    held_lines = ", ".join(state.merges_held)
    open_lines = ", ".join(item.text for item in read_open_items(outstanding_path(production), production=production))
    return "\n".join([f"### STATE {timestamp}", *unit_lines, f"Merges accepted but held: {held_lines or 'none'}",
                      f"Open for the user: {open_lines or 'none'}", f"Next: {'; '.join(state.owed) or 'none'}", ""])


def append_log(production: Production, event: str, path: Path | None, before_compaction: bool) -> None:
    if "\n" in event or not event.strip():
        raise RegistrationFailure("log", "event must be one nonempty line")
    state = judgment_state(path, "log")
    timestamp = production_time(production, datetime.now(production.zone).isoformat())
    previous = production.log.read_text(encoding="utf-8") if production.log.exists() else ""
    count = sum(bool(re.match(r"^- \d\d:\d\d \S+: ", line)) for line in previous.splitlines()) + 1
    block = state_block(production, state, timestamp) if count % 10 == 0 or before_compaction else ""
    production.log.parent.mkdir(parents=True, exist_ok=True)
    with production.log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {timestamp}: {event}\n")
        if block:
            _ = output.write(block)
    report("log", "ok", f"event {count}" + (" with STATE" if block else ""))


def record_question_history(outstanding: Path, items: list[OutstandingItem], now: datetime, *, withdrawn: bool = False) -> None:
    """Retain two days of registrations, including questions that have closed."""
    history = outstanding.with_suffix(".history.jsonl")
    retained: list[QuestionRegistration] = []
    cutoff = now - timedelta(hours=48)
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    continue
                fields = cast(dict[str, object], value)
                stamp, text = fields.get("time"), fields.get("text")
                if not isinstance(stamp, str) or not isinstance(text, str):
                    continue
                dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if dated.tzinfo is not None and dated >= cutoff:
                    entry = QuestionRegistration(time=stamp, text=text)
                    for field in ("question", "reviewed", "withdrawn"):
                        content = fields.get(field)
                        if isinstance(content, str):
                            if field == "question":
                                entry["question"] = content
                            elif field == "reviewed":
                                entry["reviewed"] = content
                            else:
                                entry["withdrawn"] = content
                    retained.append(entry)
            except ValueError:
                continue
    for item in items:
        entry = next((entry for entry in retained if entry["text"] == item["text"]
                      and entry.get("question") == item.get("question")), None)
        if entry is None:
            entry = QuestionRegistration(time=now.isoformat(), text=item["text"])
            if "question" in item:
                entry["question"] = item["question"]
            if "reviewed" in item:
                entry["reviewed"] = item["reviewed"]
            retained.append(entry)
        if withdrawn:
            entry["withdrawn"] = now.isoformat()
    write_text_atomic(history, "".join(json.dumps(item) + "\n" for item in retained))


def store_ranking(entry: OutstandingItem, rank: Ranking) -> None:
    """Store only what is not normal: the level, its time, and the item this one comes after."""
    for key, value in urgency.fields(rank.urgency).items():
        if key == "urgency":
            entry["urgency"] = value
        elif key == "due":
            entry["due"] = value
        else:
            entry["marked"] = value
    if rank.follows:
        entry["follows"] = rank.follows


def keep_question_fields(entry: OutstandingItem, previous: Outstanding) -> None:
    if not isinstance(previous, OutstandingReminder):
        entry["question"] = previous.question
        if previous.asked_by:
            entry["asked_by"] = previous.asked_by
    if isinstance(previous, ReviewedOutstandingQuestion):
        entry["reviewed"] = previous.reviewed
    elif isinstance(previous, ReviewUnavailableOutstandingQuestion):
        entry["review_unavailable"] = previous.reason


class HeldRegistrationQuestion(NamedTuple):
    details: str


class ReviewedQuestion(NamedTuple):
    text: str
    reviewed: str


class ReviewUnavailableQuestion(NamedTuple):
    text: str
    question: str
    reason: str


AcceptedQuestion = ReviewedQuestion | ReviewUnavailableQuestion


def review_open_question(name: str, item: QuestionForReview) -> AcceptedQuestion | HeldRegistrationQuestion:
    # Copied registration trees can run reminder-only commands without notify installed.
    from scripts.notify import review
    outcome = review.review_question(review.QuestionKey(name, item.text), item.question)
    if isinstance(outcome, review.QuestionAccepted):
        return ReviewedQuestion(item.text, outcome.question)
    if isinstance(outcome, review.QuestionReviewUnavailable):
        return ReviewUnavailableQuestion(item.text, outcome.question, outcome.reason)
    details = outcome.questions if isinstance(outcome, review.QuestionAwaitingFacts) else outcome.details
    return HeldRegistrationQuestion("; ".join(details + tuple(f"{failure.rule}: {failure.quote}" for failure in outcome.failures)))


def questions(production: Production, path: Path | None, *, identities: QuestionStaff | None = None) -> None:
    """Record what waits on the user and review each new question before the reply asks it."""
    state = judgment_state(path, "questions")
    outstanding = outstanding_path(production)
    if isinstance(state.outstanding, ReplaceOutstanding):
        with outstanding_lock(outstanding):
            replace_outstanding(production, outstanding, state.outstanding, identities=identities)
    if isinstance(state.reports, ReplaceReports):
        reports = reports_path(production.name, identities=identities)
        write_text_atomic(reports, json.dumps(list(state.reports.items)) + "\n")
    report("questions", "ok", "recorded")


def replace_outstanding(production: Production, outstanding: Path, replacement: ReplaceOutstanding, *,
                        identities: QuestionStaff | None = None) -> None:
    """Write the showrunner's new list; a question someone filed with `ask` leaves only through `answer`."""
    identities = identities if identities is not None else QuestionStaff.read()
    previous = {item.text: item for item in read_open_items(outstanding, production=production, identities=identities)}
    now = datetime.now(production.zone)
    since = now.strftime("%Y-%m-%dT%H:%M")
    entries: list[OutstandingItem] = []
    pending: list[AcceptedQuestion] = []
    held: list[str] = []
    items_by_text: dict[str, OpenForUserItem] = {}
    for item in replacement.items:
        if item.text not in items_by_text:
            items_by_text[item.text] = item
    # Validate all new object questions before starting any review.
    for item in items_by_text.values():
        if item.text not in previous and isinstance(item, OpenQuestionWithoutText):
            raise RegistrationFailure("questions", f"{item.text}: a new question needs its full text in `question`")
    # A new or changed item must pass the plumbing check; a stored one that fails is only named.
    refused: list[str] = []
    for text in items_by_text:
        problem = plumbing(text)
        if problem is not None and text in previous:
            report("questions", "reword", f"{text}: {problem}")
        elif problem is not None:
            refused.append(f"{text}: {problem}")
    if refused:
        raise RegistrationFailure("questions", "; ".join(refused))
    for item in items_by_text.values():
        entry = OutstandingItem(since=previous[item.text].since.strftime("%Y-%m-%dT%H:%M")
                                if item.text in previous else since, text=item.text)
        if item.after:
            entry["after"] = item.after
        old = previous[item.text].rank.urgency if item.text in previous else urgency.Urgency()
        store_ranking(entry, Ranking(urgency.keep_mark(item.rank.urgency, old, now.replace(tzinfo=None)),
                                     item.rank.follows))
        if item.text in previous:
            keep_question_fields(entry, previous[item.text])
        elif isinstance(item, QuestionForReview):
            accepted = review_open_question(production.name, item)
            if isinstance(accepted, ReviewedQuestion):
                entry["question"], entry["reviewed"] = item.question, accepted.reviewed
                pending.append(accepted)
            elif isinstance(accepted, ReviewUnavailableQuestion):
                entry["question"], entry["review_unavailable"] = item.question, accepted.reason
                pending.append(accepted)
            else:
                held.append(f"{item.text}: {accepted.details}")
        entries.append(entry)
    if held:
        raise RegistrationFailure("questions", "question held for review: " + "; ".join(held))
    for stored in open_entries(production, identities):
        asker = stored.get("asked_by", "")
        if asker and stored["text"] not in items_by_text:
            entries.append(stored)
            report("questions", "kept", f"{stored['text']}: asked by {asker}; it leaves only through `answer` or `withdraw`")
    record_question_history(outstanding, [entry for entry in entries if entry["text"] not in previous], now)
    write_open_entries(production, entries, identities)
    for item in pending:
        accepted_text = item.reviewed if isinstance(item, ReviewedQuestion) else item.question
        print("reviewed question: " + accepted_text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prompt = commands.add_parser("prompt")
    _ = prompt.add_argument("--owner", required=True)
    for action in ("register", "time", "log", "questions", "open", "next", "mark-important", "ask", "answer", "withdraw",
                   "questions-table", "migrate-questions"):
        subcommand = commands.add_parser(action)
        _ = subcommand.add_argument("--production", type=str, required=action not in ("ask", "questions-table"))
        if action == "register":
            # Accepted and unused: a showrunner started before its name was looked up still passes it.
            _ = subcommand.add_argument("--session")
        elif action == "time":
            _ = subcommand.add_argument("stamp")
        elif action == "log":
            _ = subcommand.add_argument("event")
            _ = subcommand.add_argument("--state", type=Path)
            _ = subcommand.add_argument("--before-compaction", action="store_true")
        elif action == "questions":
            _ = subcommand.add_argument("--state", type=Path)
        elif action in ("mark-important", "answer", "withdraw"):
            _ = subcommand.add_argument("--item", required=True)
        elif action == "ask":
            _ = subcommand.add_argument("--text", required=True, help="the one-line item the count and lists name")
            _ = subcommand.add_argument("--question", required=True,
                                        help="what is asked, why, each choice and what it does, the recommended choice")
            _ = subcommand.add_argument("--unit", default="", help="the asking unit's staff id; without --production, select its parent")
            _ = subcommand.add_argument("--urgency", default="", help="user, blocks, soon (with --due), normal or later")
            _ = subcommand.add_argument("--due", default="")
    args = parser.parse_args()
    action = cast(str, args.action)
    try:
        if action == "prompt":
            production = read_production(cast(str, args.owner))
            schedule = update_schedule(production.doc.read_text(encoding="utf-8").splitlines())
            minutes = schedule.minutes if isinstance(schedule, ScheduledUpdates) else DEFAULT_MINUTES
            print(scheduled_prompt(minutes, production))
            return 0
        identities = QuestionStaff.read() if action in ("ask", "questions-table") else None
        name = cast(str | None, args.production)
        if name is None:
            assert identities is not None
            if action == "ask":
                unit = cast(str, args.unit)
                if not unit:
                    raise RegistrationFailure(action, "pass --unit <staff id> or --production <name>")
                name = unit_lookup.unit_parent(unit, session_staff.StoredReference(unit), record=identities.record).name
            else:
                name = identities.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
                if name is None:
                    raise RegistrationFailure(action, "this session is no production's showrunner; pass --production <name>")
        production = read_production(name, identities.record if identities else None)
        for line in adopt_moved_state(production, record=identities.record if identities is not None else None):
            report("state-dir", "ok", line)
        if action == "register":
            register(production)
        elif action == "time":
            report("time", "ok", production_time(production, cast(str, args.stamp)))
        elif action == "log":
            append_log(production, cast(str, args.event), cast(Path | None, args.state), cast(bool, args.before_compaction))
        elif action == "open":
            open_items(production)
        elif action == "next":
            next_item(production)
        elif action == "mark-important":
            mark_important(production, cast(str, args.item))
        elif action == "answer":
            _ = answer(production, cast(str, args.item))
        elif action == "withdraw":
            withdraw(production, cast(str, args.item))
        elif action == "ask":
            try:
                level = urgency.read(cast(str, args.urgency), cast(str, args.due))
            except urgency.UrgencyError:
                raise RegistrationFailure("ask", "--urgency is user, blocks, soon with --due, normal or later") from None
            ask(production, cast(str, args.text), cast(str, args.question), cast(str, args.unit), Ranking(level),
                identities=identities)
        elif action == "questions-table":
            questions_table(production, identities=identities)
        elif action == "migrate-questions":
            migrate_questions(production)
        else:
            questions(production, cast(Path | None, args.state))
        return 0
    except (staff.StaffRefused, RegistrationFailure, InputError, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, RegistrationFailure) else action
        report(step, "failed", str(error).replace("\n", "; "))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
