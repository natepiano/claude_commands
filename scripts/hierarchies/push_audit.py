#!/usr/bin/env python3
"""Classify this machine's delivered pushes after failed text checks, read only.

This module defines the audit's failed-check rule. Date-time ends are excluded;
a date-only --until includes that local day. Ownership comes from current
production tables and cannot reconstruct historical ownership.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import ClassVar, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "notify"))
sys.path.insert(0, str(SCRIPTS / "production"))

from scripts.hierarchies import log_records
from scripts.notify.pushover import MESSAGE_MAX
from scripts.production import production_units, showrunners

STATE_DIR = Path.home() / ".local/state/notify"
SESSION_CONFIG = SCRIPTS.parent / "config/hierarchies/sessions.toml"
PUSHOVER_SEND = "scripts/notify/pushover.py send()"


@dataclass(frozen=True)
class TextId:
    value: str


@dataclass(frozen=True)
class FreeText:
    pass


@dataclass(frozen=True)
class TextNotRecorded:
    pass


TextIdentity = TextId | FreeText | TextNotRecorded


@dataclass(frozen=True)
class SessionSender:
    name: str


@dataclass(frozen=True)
class BareSender:
    name: str


@dataclass(frozen=True)
class SenderNotRecorded:
    pass


Sender = SessionSender | BareSender | SenderNotRecorded


@dataclass(frozen=True)
class ShowrunnerOwner:
    slug: str


@dataclass(frozen=True)
class ShowrunnerNotRecorded:
    pass


@dataclass(frozen=True)
class SessionWithoutProduction:
    key: str


@dataclass(frozen=True)
class MachineService:
    name: str


ShowrunnerAttribution = ShowrunnerOwner | MachineService | SessionWithoutProduction | ShowrunnerNotRecorded


@dataclass(frozen=True)
class ProductionOwnership:
    exact_names: dict[str, ShowrunnerOwner]
    session_aliases: dict[str, ShowrunnerOwner]


@dataclass(frozen=True)
class TopLevelSession:
    key: str
    display_name: str


@dataclass(frozen=True)
class TopLevelSessions:
    entries: tuple[TopLevelSession, ...]

    def display_name(self, key: str) -> str:
        for session in self.entries:
            if session.key == key:
                return session.display_name
        return key


class PushPriority(Enum):
    NORMAL = "0"
    HIGH = "1"
    EMERGENCY = "2"


class PushOutcome(Enum):
    SENT = "sent"
    FAILED_REVIEW_SENT = "sent despite failed review"
    ORIGINAL_TEXT_SENT = "rewrite not applied"
    KEPT = "kept during silence"
    NOT_DELIVERED = "not delivered"


class DeliveryTiming(Enum):
    IMMEDIATE = "immediate"
    RELEASED_FROM_SILENCE = "released from silence"


class ReviewVerdict(Enum):
    APPROVED = "send"
    REWRITTEN = "rewrite"
    QUESTIONS = "ask"
    FAILED = "failed"
    WITHHELD = "withhold"
    UNREVIEWED = "unreviewed"


class FailedCheckEvidence(Enum):
    NONE = "no failed check recorded"
    OUTCOME = "delivery outcome records a failed check"
    REWRITE_NOT_APPLIED_IN_OUTCOME = "delivery outcome records rewrite not applied"
    REWRITE_NOT_APPLIED_IN_REVIEW = "registered text sent as the failed draft; rewrite not applied"
    FAILED_RULES = "delivered review carries failed rules"
    REJECTED_VERDICT = "delivered review has a failed/withhold/ask verdict"


@dataclass(frozen=True)
class ApprovedPriorityTwo:
    code_path: ClassVar[str] = PUSHOVER_SEND + ": priority == 2 exception"
    reason: ClassVar[str] = "priority-2 send is an approved exception"


@dataclass(frozen=True)
class ApprovedRegisteredText:
    code_path: ClassVar[str] = PUSHOVER_SEND + ": registered text_id exception"
    reason: ClassVar[str] = "recorded text id or registered rewrite outcome; current catalog membership and approval are not required"


@dataclass(frozen=True)
class Unapproved:
    code_path: str
    reason: ClassVar[str] = "no priority-2 or registered-text exception is recorded"


PushClassification = ApprovedPriorityTwo | ApprovedRegisteredText | Unapproved
ClassificationType = type[ApprovedPriorityTwo] | type[ApprovedRegisteredText] | type[Unapproved]
CLASSIFICATIONS = (ApprovedPriorityTwo, ApprovedRegisteredText, Unapproved)


@dataclass(frozen=True)
class PushRecord:
    at: datetime
    sender: Sender
    priority: PushPriority
    title: str
    message: str
    outcome: PushOutcome
    timing: DeliveryTiming


@dataclass(frozen=True)
class SentReview:
    title: str
    body: str
    unchanged_draft: bool


@dataclass(frozen=True)
class ReviewNotSent:
    pass


@dataclass(frozen=True)
class ReviewRecord:
    at: datetime
    sender: Sender
    verdict: ReviewVerdict
    sent: SentReview | ReviewNotSent
    text_identity: TextIdentity
    failed_rules: tuple[str, ...]


@dataclass(frozen=True)
class ReviewNotMatched:
    pass


ReviewMatch = ReviewRecord | ReviewNotMatched


@dataclass(frozen=True)
class AuditSources:
    pushes: list[PushRecord]
    reviews: list[ReviewRecord]
    problems: list[log_records.SourceProblem]
    production_ownership: ProductionOwnership
    sessions: TopLevelSessions


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    sender: Sender
    title: str
    text_identity: TextIdentity
    showrunner: ShowrunnerAttribution
    classification: PushClassification
    evidence: FailedCheckEvidence


@dataclass(frozen=True)
class AuditReport:
    window: log_records.Window
    entries: tuple[AuditEntry, ...]
    problems: tuple[log_records.SourceProblem, ...]
    pushes_by_showrunner: dict[ShowrunnerAttribution, int]
    delivered_pushes: tuple[PushRecord, ...]
    sessions: TopLevelSessions


def parse_sender(source: str) -> Sender:
    if not source:
        return SenderNotRecorded()
    if source.startswith("send.py in "):
        name = source.removeprefix("send.py in ").strip()
        return SessionSender(name) if name else SenderNotRecorded()
    return BareSender(source)


def parse_push(record: log_records.JsonObject) -> PushRecord:
    text = log_records.field_text
    outcome_text = text(record, "outcome")
    outcome = PushOutcome.NOT_DELIVERED
    if outcome_text.startswith("kept: silence"):
        outcome = PushOutcome.KEPT
    if outcome_text == "sent" or outcome_text.startswith("sent,"):
        outcome = PushOutcome.SENT
        if "sent despite failed review" in outcome_text:
            outcome = PushOutcome.FAILED_REVIEW_SENT
        elif "rewrite not applied" in outcome_text:
            outcome = PushOutcome.ORIGINAL_TEXT_SENT
    return PushRecord(log_records.record_time(record), parse_sender(text(record, "source")),
                      PushPriority(text(record, "priority")), text(record, "title"), text(record, "message"), outcome,
                      DeliveryTiming.RELEASED_FROM_SILENCE if "kept during silence" in outcome_text else DeliveryTiming.IMMEDIATE)


def parse_review(record: log_records.JsonObject) -> tuple[ReviewRecord, ...]:
    if any(log_records.field_flag(record, key) for key in ("replay", "dry", "sample")):
        return ()
    text = log_records.field_text
    if text(record, "kind") in ("reply", "question"):
        return ()
    verdict_text = text(record, "verdict")
    verdict = (ReviewVerdict.UNREVIEWED if verdict_text.startswith("unreviewed:") or verdict_text == "review: off"
               else ReviewVerdict(verdict_text))
    sent_value = record.get("sent")
    sent: SentReview | ReviewNotSent = ReviewNotSent()
    if sent_value is not None:
        message = log_records.object_record(sent_value)
        sent = SentReview(text(message, "title"), text(message, "body"), message == record.get("draft"))
    rules = record.get("failed_rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in cast(list[object], rules)):
        raise ValueError("failed_rules: expected a list of rule names")
    identity = text(record, "text_id")
    return (ReviewRecord(log_records.record_time(record), parse_sender(text(record, "source")), verdict, sent,
                         TextId(identity) if identity else TextNotRecorded(), tuple(cast(list[str], rules))),)


def read_log[T](path: Path, parse: Callable[[log_records.JsonObject], T], problems: list[log_records.SourceProblem]) -> list[T]:
    records: list[T] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        kind = "missing" if isinstance(error, FileNotFoundError) else "unreadable"
        problems.append(log_records.SourceProblem(str(path), f"{kind}: {error}"))
        return records
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = cast(object, json.loads(line))
            records.append(parse(log_records.object_record(value)))
        except ValueError as error:
            problems.append(log_records.SourceProblem(str(path), f"unreadable line {line_number}: {error}"))
    return records


def read_owners(problems: list[log_records.SourceProblem]) -> ProductionOwnership:
    """Read current production tables without session lookup or roster traffic."""
    exact_names: dict[str, set[str]] = defaultdict(set)
    session_aliases: dict[str, set[str]] = defaultdict(set)
    for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
        checked = showrunners.checked_doc(instance)
        if isinstance(checked, showrunners.NoCheckedDoc):
            problems.append(log_records.SourceProblem(str(instance / "conf"), checked.reason))
            continue
        slug = instance.name.removeprefix("showrunner-")
        exact_names[slug].add(slug)
        try:
            lines = checked.path.read_text(encoding="utf-8").splitlines()
            for row in production_units.unit_table(lines):
                if production_units.plan_cell_is_retired(row.get("Plan", "")):
                    continue
                name = production_units.cell_value(row.get("Unit", ""))
                if name:
                    exact_names[name].add(slug)
                    if name.endswith("-unit"):
                        session_aliases[name.removesuffix("-unit")].add(slug)
        except (OSError, UnicodeError, production_units.Refusal) as error:
            problems.append(log_records.SourceProblem(str(checked.path), f"unreadable ownership source: {error}"))
    return ProductionOwnership(
        {name: ShowrunnerOwner(next(iter(slugs))) for name, slugs in exact_names.items() if len(slugs) == 1},
        {name: ShowrunnerOwner(next(iter(slugs))) for name, slugs in session_aliases.items() if len(slugs) == 1})


def read_sessions(path: Path, problems: list[log_records.SourceProblem]) -> TopLevelSessions:
    try:
        with path.open("rb") as file:
            settings = log_records.object_record(cast(object, tomllib.load(file)))
        if not settings:
            raise ValueError("expected at least one session table")
        sessions: list[TopLevelSession] = []
        for key, value in settings.items():
            fields = log_records.object_record(value)
            display_name = log_records.field_text(fields, "display_name")
            if not key.strip() or not display_name.strip() or set(fields) != {"display_name"}:
                raise ValueError(f"{key}: expected a session table with a nonempty display_name")
            sessions.append(TopLevelSession(key, display_name))
        return TopLevelSessions(tuple(sessions))
    except (OSError, UnicodeError, ValueError) as error:
        problems.append(log_records.SourceProblem(str(path), f"unreadable session config: {error}"))
        return TopLevelSessions(())


def load_sources(folder: Path) -> AuditSources:
    problems: list[log_records.SourceProblem] = []
    pushes = read_log(folder / "pushover.jsonl", parse_push, problems)
    reviews = [review for group in read_log(folder / "review.jsonl", parse_review, problems) for review in group]
    return AuditSources(pushes, reviews, problems, read_owners(problems), read_sessions(SESSION_CONFIG, problems))


def notification_payload(message: str) -> str:
    footer_start = message.rfind("\n\nSent by: ")
    return message[:footer_start] if footer_start >= 0 else message


def reviewed_body_was_sent(body: str, message: str) -> bool:
    payload = notification_payload(message)
    remaining = payload
    header_size = 0
    for prefix in ("Session: ", ("Action: ", "No action needed.")):
        first, separator, rest = remaining.partition("\n")
        if first.startswith(prefix):
            header_size += len(first + separator)
            remaining = rest
    if body.rstrip() == remaining.rstrip():
        return True
    if not body.strip():
        return False
    limit = MESSAGE_MAX - (len(message) - len(payload))
    expected = payload[:header_size] + body
    return len(expected) > limit and expected[:limit].rstrip() == payload


def submission_time(push: PushRecord, kept: Sequence[PushRecord]) -> datetime:
    if push.timing is DeliveryTiming.IMMEDIATE:
        return push.at
    payload = notification_payload(push.message)
    limit = MESSAGE_MAX - (len(push.message) - len(payload))
    times = [record.at for record in kept if record.at <= push.at and record.sender == push.sender
             and record.title == push.title and notification_payload(record.message)[:limit].rstrip() == payload]
    return max(times, default=push.at)


def pair_reviews(pushes: Sequence[PushRecord], reviews: Sequence[ReviewRecord],
                 kept: Sequence[PushRecord]) -> dict[int, ReviewRecord]:
    candidates: list[tuple[float, int, int]] = []
    for push_index, push in enumerate(pushes):
        submitted = submission_time(push, kept)
        for review_index, review in enumerate(reviews):
            if not isinstance(review.sent, SentReview):
                continue
            if review.sender != push.sender or review.sent.title != push.title:
                continue
            distance = abs((submitted - review.at).total_seconds())
            if distance <= 5 and reviewed_body_was_sent(review.sent.body, push.message):
                candidates.append((distance, push_index, review_index))
    matches: dict[int, ReviewRecord] = {}
    used_reviews: set[int] = set()
    for candidate in sorted(candidates):
        push_index, review_index = candidate[1], candidate[2]
        if push_index not in matches and review_index not in used_reviews:
            matches[push_index] = reviews[review_index]
            used_reviews.add(review_index)
    return matches


def recorded_text_identity(push: PushRecord, review: ReviewMatch) -> TextIdentity:
    found = re.search(r"(?:^|\n)Template: ([^\n]+)\s*$", push.message)
    if found and found[1].strip() != "none (free text)":
        return TextId(found[1].strip())
    if isinstance(review, ReviewRecord) and isinstance(review.text_identity, TextId):
        return review.text_identity
    return FreeText() if found else TextNotRecorded()


def after_failed_check(push: PushRecord, review: ReviewMatch) -> FailedCheckEvidence:
    """The single failed-check definition for this audit, including delayed sends."""
    if push.outcome is PushOutcome.FAILED_REVIEW_SENT:
        return FailedCheckEvidence.OUTCOME
    if push.outcome is PushOutcome.ORIGINAL_TEXT_SENT:
        return FailedCheckEvidence.REWRITE_NOT_APPLIED_IN_OUTCOME
    if isinstance(review, ReviewNotMatched):
        return FailedCheckEvidence.NONE
    if review.verdict is ReviewVerdict.REWRITTEN:
        if isinstance(review.sent, SentReview) and not review.sent.unchanged_draft:
            return FailedCheckEvidence.NONE
        if isinstance(recorded_text_identity(push, review), TextId):
            return FailedCheckEvidence.REWRITE_NOT_APPLIED_IN_REVIEW
    if review.failed_rules:
        return FailedCheckEvidence.FAILED_RULES
    if review.verdict in (ReviewVerdict.FAILED, ReviewVerdict.WITHHELD, ReviewVerdict.QUESTIONS):
        return FailedCheckEvidence.REJECTED_VERDICT
    return FailedCheckEvidence.NONE


def sending_path(push: PushRecord, review: ReviewMatch) -> str:
    if isinstance(review, ReviewRecord) and review.verdict is ReviewVerdict.UNREVIEWED and review.failed_rules:
        return ("scripts/notify/review.py review()/validate() -> unreviewed failed text -> "
                "scripts/notify/pushover.py send() sends original draft")
    if isinstance(push.sender, SessionSender):
        return "scripts/message/send.py user -> " + PUSHOVER_SEND + " -> post_message()"
    return PUSHOVER_SEND + " -> post_message(); upstream caller not recorded"


def classify(push: PushRecord, identity: TextIdentity, review: ReviewMatch) -> PushClassification:
    if push.priority is PushPriority.EMERGENCY:
        return ApprovedPriorityTwo()
    if isinstance(identity, TextId) or push.outcome is PushOutcome.ORIGINAL_TEXT_SENT:
        return ApprovedRegisteredText()
    return Unapproved(sending_path(push, review))


def owner(sender: Sender, owners: ProductionOwnership, sessions: TopLevelSessions) -> ShowrunnerAttribution:
    if isinstance(sender, SenderNotRecorded):
        return ShowrunnerNotRecorded()
    if sender.name in owners.exact_names:
        return owners.exact_names[sender.name]
    if isinstance(sender, SessionSender):
        if sender.name in owners.session_aliases:
            return owners.session_aliases[sender.name]
    for session in sessions.entries:
        if sender.name == session.key:
            return SessionWithoutProduction(session.key)
    if isinstance(sender, SessionSender):
        return SessionWithoutProduction(sender.name)
    return MachineService(sender.name)


def audit(sources: AuditSources, window: log_records.Window) -> AuditReport:
    delivered = [push for push in sources.pushes if push.outcome not in (PushOutcome.NOT_DELIVERED, PushOutcome.KEPT)]
    kept = [push for push in sources.pushes if push.outcome is PushOutcome.KEPT]
    matches = pair_reviews(delivered, sources.reviews, kept)
    entries: list[AuditEntry] = []
    window_pushes: list[PushRecord] = []
    counts: Counter[ShowrunnerAttribution] = Counter()
    for index, push in enumerate(delivered):
        if not window.contains(push.at):
            continue
        review = matches.get(index, ReviewNotMatched())
        window_pushes.append(push)
        showrunner = owner(push.sender, sources.production_ownership, sources.sessions)
        counts[showrunner] += 1
        evidence = after_failed_check(push, review)
        if evidence is FailedCheckEvidence.NONE:
            continue
        identity = recorded_text_identity(push, review)
        entries.append(AuditEntry(push.at, push.sender, push.title, identity, showrunner,
                                  classify(push, identity, review), evidence))
    return AuditReport(window, tuple(sorted(entries, key=lambda entry: entry.at)),
                       tuple(sources.problems), dict(counts), tuple(window_pushes), sources.sessions)


def sender_label(sender: Sender, sessions: TopLevelSessions) -> str:
    return "not recorded" if isinstance(sender, SenderNotRecorded) else sessions.display_name(sender.name)


def owner_label(attribution: ShowrunnerAttribution, sessions: TopLevelSessions) -> str:
    if isinstance(attribution, ShowrunnerOwner):
        return "showrunner / " + attribution.slug
    if isinstance(attribution, MachineService):
        return "machine service / " + attribution.name
    if isinstance(attribution, SessionWithoutProduction):
        return "session / " + sessions.display_name(attribution.key) + " (no current production names it)"
    return "showrunner / not recorded"


def text_label(identity: TextIdentity) -> str:
    if isinstance(identity, TextId):
        return identity.value + (" (agent-written text)" if identity.value == "nate.adhoc" else "")
    return "free text" if isinstance(identity, FreeText) else "not recorded"


def render(report: AuditReport) -> str:
    details = [log_records.MeasureRow(f"{entry.at.isoformat()} / {sender_label(entry.sender, report.sessions)} / {entry.title}",
                                 f"{type(entry.classification).__name__} / {text_label(entry.text_identity)}", 1,
                                 report.window.label,
                                 f"{entry.classification.code_path}; {entry.classification.reason}; {entry.evidence.value}")
               for entry in report.entries]
    grouped: dict[ShowrunnerAttribution, Counter[ClassificationType]] = defaultdict(Counter)
    for entry in report.entries:
        grouped[entry.showrunner][type(entry.classification)] += 1
    source = "pushover.jsonl + review.jsonl; this machine only; n = delivered pushes; current production ownership"
    incomplete = "incomplete; " if report.problems else ""
    totals = [log_records.MeasureRow(owner_label(attribution, report.sessions), incomplete +
                                "; ".join(f"{kind.__name__} {grouped[attribution][kind]}" for kind in CLASSIFICATIONS),
                                count, report.window.label, source)
              for attribution, count in sorted(report.pushes_by_showrunner.items(),
                                               key=lambda item: owner_label(item[0], report.sessions))]
    overall = Counter(type(entry.classification) for entry in report.entries)
    totals.append(log_records.MeasureRow("all senders", incomplete + f"after failed check {len(report.entries)}; " +
                                    "; ".join(f"{kind.__name__} {overall[kind]}" for kind in CLASSIFICATIONS),
                                    sum(report.pushes_by_showrunner.values()), report.window.label, source))
    output = [log_records.table("Sends after failed text checks (n = 1 per delivered push)", details),
              log_records.table("Totals per showrunner (current ownership; historical ownership may be missing)", totals),
              "Neither notification log records a key. Text identity comes from the delivery footer or matched review."]
    if report.problems:
        output.append(log_records.table("Source problems (counts can be incomplete)",
                                    log_records.source_problem_rows(report.problems, report.window)))
    return "\n\n".join(output)


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("report",))
    _ = parser.add_argument("--since", metavar="DATE_OR_TIME", default="")
    _ = parser.add_argument("--until", metavar="DATE_OR_TIME", default="")
    args = parser.parse_args(arguments)
    now = datetime.now(timezone.utc)
    try:
        window = log_records.window_bounds(cast(str, args.since), cast(str, args.until), now)
    except ValueError as error:
        parser.error(str(error))
    print(render(audit(load_sources(STATE_DIR), window)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
