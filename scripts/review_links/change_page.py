#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Publish all change reviews, or retain an older selected-row link with --production NAME C27."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import html
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, urlsplit

from scripts.production import change_review as change_review
from scripts.production import unit_lookup
from scripts.production import update_registration
from scripts.production import unrouted_alert, change_routing, delivery_state, next_item, showrunners, staff
from scripts.production.delivery_state import (
    AnsweredQuestion as AnsweredQuestion, DeliveryIntent as DeliveryIntent, DeliverySlots as DeliverySlots,
    NoDelivery as NoDelivery, NoQuestion as NoQuestion, OpenQuestion as OpenQuestion,
    ValidationRequested as ValidationRequested,
)
from scripts.lib import atomic_file, conf, file_lock, urgency
from scripts.production.production_doc import Refusal as Refusal, production_field
from scripts.feature_log import feature_log
from scripts.review_links import review_link

MANIFEST = ".review-link-changes.json"
PAGE_NAME = "changes.html"
BROWSER_MANIFEST = ".review-link-browser.json"
SEND_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)
SENDER = Path(__file__).stem
DELIVERY_PREFIX = ".review-link-delivery-"


class BrowserManifest(TypedDict):
    token: str
    initial: str


@dataclass(frozen=True)
class ReviewBrowser:
    token: str
    initial: str = ""


class PageManifest(TypedDict):
    review_list: str
    production: str
    rows: list[str]
    token: str


class StoredDeliveryIntent(TypedDict):
    attempt: str
    state: Literal["pending"]
    recipients: list[str]


class StoredValidationIntent(StoredDeliveryIntent):
    feedback: str


class StoredValidationRequested(TypedDict):
    attempt: str
    state: Literal["sent"]
    feedback: str


class StoredOpenQuestion(TypedDict):
    question_id: str
    text: str
    delivery: StoredDeliveryIntent | None
    kind: NotRequired[Literal["feedback"]]


class StoredAnsweredQuestion(TypedDict):
    question_id: str
    text: str
    answer: str
    kind: NotRequired[Literal["feedback"]]


@dataclass(frozen=True)
class OpenFeedback(OpenQuestion):
    """The showrunner must act before this change returns to the user."""


@dataclass(frozen=True)
class CompletedFeedback(AnsweredQuestion):
    """The showrunner's note returns this change to the user."""


def _decode_slots(value: object) -> DeliverySlots:
    slots = delivery_state.decode_value(value)
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    saved = fields.get("question")
    question = slots.question
    if isinstance(saved, dict) and cast(dict[str, object], saved).get("kind") == "feedback":
        if isinstance(question, OpenQuestion):
            return replace(slots, question=OpenFeedback(question.question_id, question.text, question.delivery))
        if isinstance(question, AnsweredQuestion):
            return replace(slots, question=CompletedFeedback(question.question_id, question.text, question.answer))
    return slots


class StoredDeliverySlots(TypedDict, total=False):
    validation: StoredValidationIntent | StoredValidationRequested
    verdict: StoredDeliveryIntent
    question: StoredOpenQuestion | StoredAnsweredQuestion
    user_answer: StoredUserAnswer


class SentCloseoutAlerts(TypedDict):
    review_list: str
    row: str
    messages: list[str]


class StoredUserAnswer(TypedDict):
    saved: bool
    source: str
    production: str
    owner: str
    question: str
    answer: str
    message: str


@dataclass(frozen=True)
class QuestionDelivery:
    """One question and its message attempt share the same saved identity."""

    question_id: str
    text: str
    attempt: str


@dataclass(frozen=True)
class ReviewPage:
    review_list: Path
    production: Path
    rows: tuple[str, ...]
    token: str


@dataclass(frozen=True)
class AssignedReviewPage(ReviewPage):
    """The staff record supplies this row's current showrunner."""

    owner: change_routing.ChangeOwner


@dataclass(frozen=True)
class UserQuestionPage(ReviewPage):
    """A filed user question and its saved answer share one delivery record."""

    saved: StoredUserAnswer


class AnswerRefused(ValueError):
    """An invalid answer must not change the review record."""


class QuestionAnswerRefused(AnswerRefused):
    """Another session already answered this user question."""


class FeatureLogFailed(RuntimeError):
    """The feature log failed before the verdict could save."""


@dataclass(frozen=True)
class AnswerNotice:
    row: str = ""
    reason: str = ""
    outcome: str = ""
    feedback: str = ""


NO_ANSWER_NOTICE = AnswerNotice()


@dataclass(frozen=True)
class MessageDelivery:
    page: ReviewPage
    row: str
    recipients: tuple[staff.StaffId, ...]
    message: str
    attempt: str
    alert_recipients: tuple[staff.StaffId, ...] = ()


@dataclass(frozen=True)
class NoMessageDelivery:
    """This answer needs no message."""


def stored_intent(intent: DeliveryIntent) -> StoredDeliveryIntent:
    return {"attempt": intent.attempt, "state": "pending", "recipients": list(intent.recipients)}


@dataclass(frozen=True)
class AttemptOwnership:
    """Own an attempt from its claim until its delivery ends."""

    row_file: Path
    state: Literal["claimed", "sending"]


class DeliveryStatus:
    """Own delivery intents and serialize conditional attempt updates."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.request_attempts: dict[str, AttemptOwnership] = {}

    def read_view(self) -> DeliveryReadView:
        return DeliveryReadView(self, frozenset(self.root.glob(DELIVERY_PREFIX + "*")))

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(self.root / ".review-link-delivery.lock"):
            yield

    def path(self, page: ReviewPage, row: str) -> Path:
        return change_routing.delivery_path(self.root, page.review_list, row)

    def notify_closeout_failure(self, page: ReviewPage, row: str, message: str,
                                recipients: tuple[staff.StaffId, ...]) -> None:
        """Send each row's failure once, including across server restarts."""
        if not recipients:
            return
        name = self.path(page, row).name.removeprefix(DELIVERY_PREFIX)
        path = self.root / (".review-link-closeout-alert-" + name + ".json")
        with self.locked():
            try:
                sent: SentCloseoutAlerts = cast(SentCloseoutAlerts, json.loads(path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                sent = {"review_list": str(page.review_list.resolve()), "row": row, "messages": []}
            if message in sent["messages"]:
                return
            for identity in recipients:
                send_to_staff(identity, SENDER, message)
            sent["messages"].append(message)
            atomic_file.write_bytes_atomic(path, (json.dumps(sent) + "\n").encode("utf-8"), mode=0o600)

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        try:
            content = self.path(page, row).read_text(encoding="utf-8")
        except FileNotFoundError:
            return DeliverySlots(NoDelivery(), NoDelivery())
        try:
            value = cast(object, json.loads(content))
        except ValueError:
            return DeliverySlots(NoDelivery(), NoDelivery())
        return _decode_slots(value)

    def write_slots(self, page: ReviewPage, row: str, slots: DeliverySlots) -> None:
        """Write both slots while the caller holds the delivery lock."""
        fields: StoredDeliverySlots = {}
        if isinstance(page, UserQuestionPage):
            fields["user_answer"] = page.saved
        if isinstance(slots.validation, DeliveryIntent):
            fields["validation"] = {**stored_intent(slots.validation), "feedback": slots.validation.feedback}
        elif isinstance(slots.validation, ValidationRequested):
            fields["validation"] = {"attempt": slots.validation.attempt, "state": "sent",
                                    "feedback": slots.validation.feedback}
        if isinstance(slots.verdict, DeliveryIntent):
            fields["verdict"] = stored_intent(slots.verdict)
        question = slots.question
        if isinstance(question, OpenQuestion):
            fields["question"] = {"question_id": question.question_id, "text": question.text, "delivery": (
                stored_intent(question.delivery)
                if isinstance(question.delivery, DeliveryIntent) else None)}
        elif isinstance(question, AnsweredQuestion):
            fields["question"] = {"question_id": question.question_id, "text": question.text, "answer": question.answer}
        saved_question = fields.get("question")
        if saved_question is not None and isinstance(question, (OpenFeedback, CompletedFeedback)):
            saved_question["kind"] = "feedback"
        path = self.path(page, row)
        published = False
        try:
            if fields:
                try:
                    atomic_file.write_bytes_atomic(path, (json.dumps(fields) + "\n").encode("utf-8"), mode=0o600)
                except atomic_file.AtomicDurabilityUnconfirmed:
                    published = True
                    raise
            else:
                path.unlink(missing_ok=True)
            published = True
        finally:
            if published:
                intents = {intent.attempt for intent in slots.intents()}
                stale = [attempt for attempt, ownership in self.request_attempts.items()
                         if ownership.row_file == path and attempt not in intents]
                for attempt in stale:
                    del self.request_attempts[attempt]

    def begin(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation"], feedback: str = "", *, recipients: tuple[staff.StaffId, ...] = ()) -> str:
        attempt = secrets.token_hex()
        intent = DeliveryIntent(attempt, kind, feedback if kind == "validation" else "", recipients)
        with self.locked():
            self.write_slots(page, row, self.slots(page, row).updated(kind, intent))
            self.request_attempts[attempt] = AttemptOwnership(self.path(page, row), "claimed")
        return attempt

    def record(self, delivery: MessageDelivery) -> None:
        with self.locked():
            _ = self.request_attempts.pop(delivery.attempt, None)
            slots = self.slots(delivery.page, delivery.row)
            for current in slots.intents():
                if current.attempt == delivery.attempt:
                    if current.kind == "validation":
                        updated = replace(slots, validation=ValidationRequested(current.attempt, current.feedback))
                    else:
                        updated = slots.updated(current.kind, NoDelivery())
                    self.write_slots(delivery.page, delivery.row, updated)
                    return

    def clear(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation", "question"]) -> None:
        with self.locked():
            self.clear_locked(page, row, self.slots(page, row), kind)

    def clear_locked(self, page: ReviewPage, row: str, slots: DeliverySlots, kind: Literal["verdict", "validation", "question"]) -> None:
        updated = slots.updated(kind, NoDelivery())
        self.write_slots(page, row, updated)

    def ask(self, page: ReviewPage, row: str, text: str, *, recipients: tuple[staff.StaffId, ...] = (),
            kind: Literal["question", "feedback"] = "question") -> QuestionDelivery | NoDelivery:
        """Save a new question without changing an existing open question."""
        with self.locked():
            slots = self.slots(page, row)
            question = slots.question
            if isinstance(question, OpenQuestion):
                if question.text != text or isinstance(question, OpenFeedback) != (kind == "feedback"):
                    raise AnswerRefused("this row already has an open question")
                return NoDelivery()
            intent = DeliveryIntent(secrets.token_hex(), "question", recipients=recipients)
            question_id = secrets.token_hex(4)
            request = OpenFeedback if kind == "feedback" else OpenQuestion
            self.write_slots(page, row, replace(slots, validation=NoDelivery(), question=request(question_id, text, intent)))
            self.request_attempts[intent.attempt] = AttemptOwnership(self.path(page, row), "claimed")
            return QuestionDelivery(question_id, text, intent.attempt)

    def answer_question(self, page: ReviewPage, row: str, question_id: str, text: str) -> bool:
        """Save an answer once and preserve the delivery record's other slots."""
        if not text.strip():
            raise AnswerRefused("enter an answer")
        text = text.strip()
        with self.locked():
            slots = self.slots(page, row)
            question = slots.question
            if isinstance(question, NoQuestion):
                raise AnswerRefused("this row has no open question")
            if question.question_id != question_id:
                raise AnswerRefused("this question id does not match the current question")
            if isinstance(question, AnsweredQuestion) and question.answer == text:
                return False
            if not isinstance(question, OpenQuestion):
                raise AnswerRefused("this row has no open question")
            record = answer_row(page, row)
            if record.get("Verdict"):
                raise AnswerRefused("this row already has a saved answer")
            _ = row_production(page, record)
            completed = CompletedFeedback if isinstance(question, OpenFeedback) else AnsweredQuestion
            self.write_slots(page, row, replace(slots, question=completed(question_id, question.text, text)))
            return True


@dataclass(frozen=True)
class DeliveryReadView:
    """One request scans record names once and reads only existing records."""

    status: DeliveryStatus
    paths: frozenset[Path]

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        if not self.paths or self.status.path(page, row) not in self.paths:
            return DeliverySlots(NoDelivery(), NoDelivery())
        return self.status.slots(page, row)


def document(content: str) -> bytes:
    return ('''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Changes to review</title><style>
:root{color-scheme:light dark;font:17px/1.55 system-ui,sans-serif}
body{margin:0}main{max-width:680px;margin:auto;padding:40px 16px 64px}
.id{opacity:.7;font-size:14px}.kind{font-weight:600}
h1{font-size:26px;line-height:1.25;margin:0 0 16px}section h1,h2{font-size:20px;line-height:1.3}
h2{margin:16px 0}p{white-space:pre-wrap;margin:16px 0}main{overflow-wrap:anywhere}
section h2{font-size:17px}section details{margin:16px 0}.secondary-actions{margin-top:8px}
ul,ol{margin:16px 0;padding-left:28px;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8886;border-radius:6px;padding:12px;font:14px/1.45 ui-monospace,monospace}
section+section{border-top:1px solid #8886;margin-top:36px;padding-top:28px}
form{margin-top:16px}label,textarea{display:block}textarea{box-sizing:border-box;width:100%;font:inherit;margin:8px 0 16px;resize:vertical}
button{font:inherit;min-height:44px;max-width:100%;margin:0 8px 8px 0;padding:6px 12px;background:light-dark(#f2f2f2,#303030);color:CanvasText;border:1px solid light-dark(#707070,#a0a0a0)}button[aria-pressed=true]{outline:2px solid currentColor}
summary{min-height:44px;align-content:center;cursor:pointer}button:focus-visible,summary:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid currentColor;outline-offset:2px}
</style></head><body><main>''' + content + "</main></body></html>").encode("utf-8")


def answer_notice(page: ReviewPage, body: str, reason: str) -> AnswerNotice:
    fields = parse_qs(body, keep_blank_values=True)
    row = fields.get("row", [""])[0]
    outcome = fields.get("outcome", [""])[0]
    if row not in page.rows:
        row = ""
    if outcome not in ("works", "not works", "validate", "feedback", "question"):
        outcome = ""
    return AnswerNotice(row, reason, outcome, fields.get("feedback", [""])[0])


def answer_form(page: ReviewPage, notice: AnswerNotice, feedback_rows: int) -> str:
    """Render an answer without reading rows or queue state."""
    row = notice.row if notice.row in page.rows else page.rows[0]
    row_id = html.escape(row)
    buttons: list[str] = []
    for value, label in (("works", "Works"), ("not works", "Does not work"),
                         ("validate", "Ask showrunner to check"), ("question", "Ask a question"), ("feedback", "Send feedback")):
        if value in ("validate", "question", "feedback") and isinstance(page, AssignedReviewPage) and isinstance(page.owner, change_routing.UnroutedChange):
            continue
        selected = 'aria-pressed="true" ' if notice.outcome == value else ""
        buttons.append(f'<button {selected}name="outcome" value="{value}">{label}</button>')
    return f'''<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<label for="feedback-{row_id}">Comment, question or feedback</label>
<textarea id="feedback-{row_id}" name="feedback" rows="{feedback_rows}">
{html.escape(notice.feedback)}</textarea>
<div>{"".join(buttons[:2])}</div><div class="secondary-actions"><div>{"".join(buttons[2:3])}</div><div class="feedback-actions">{"".join(buttons[3:])}</div></div></form>'''


def change_details(row: dict[str, str], key: str) -> str:
    """Keep change metadata inside one closed disclosure."""
    return ('<details><summary>Details</summary>'
            + f'<div class="id">{html.escape(key)} · {html.escape(row.get("Merged", ""))}</div>'
            + f'<p class="kind">{html.escape(row.get(change_review.KIND) or "Kind not recorded")}</p></details>')


def fallback_answer(page: ReviewPage, notice: AnswerNotice) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    return document(f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>' + answer_form(page, notice, feedback_rows))


def review_text_html(text: str, block: Literal["p", "pre"] = "p") -> str:
    """Render review text as escaped text blocks and consecutive list runs."""
    lines = text.splitlines()
    if not any(re.match(r"\s*(?:- |[0-9]+\. )", line) for line in lines):
        return f"<{block}>{html.escape(text)}</{block}>"
    content: list[str] = []
    current = ""
    for line in lines:
        line = line.lstrip()
        numbered = re.match(r"([0-9]+)\. (.*)", line)
        tag = "ul" if line.startswith("- ") else "ol" if numbered else block if line else ""
        if tag != current:
            if current:
                content.append(f"</{current}>")
            if tag:
                start = (numbered[1].lstrip("0") or "0") if numbered else "1"
                attribute = f' start="{start}"' if tag == "ol" and start != "1" else ""
                content.append(f"<{tag}{attribute}>")
        elif tag == block:
            content.append("\n")
        if tag in ("ul", "ol"):
            item = numbered[2] if numbered else line[2:]
            content.append(f"<li>{html.escape(item)}</li>")
        elif tag:
            content.append(html.escape(line))
        current = tag
    if current:
        content.append(f"</{current}>")
    return "".join(content)


def question_html(question: NoQuestion | OpenQuestion | AnsweredQuestion) -> str:
    if isinstance(question, NoQuestion):
        return ""
    feedback = isinstance(question, (OpenFeedback, CompletedFeedback))
    content = ('<h2>Feedback</h2>' if feedback else '<h2>Question</h2>') + review_text_html(question.text)
    if isinstance(question, AnsweredQuestion):
        content += review_text_html('Showrunner: ' + question.answer) if feedback else '<h2>Answer</h2>' + review_text_html(question.answer)
    else:
        content += '<p>The showrunner will act on this</p>' if feedback else '<p>The showrunner will answer this</p>'
    return content


def page_at(root: Path, request_path: str) -> ReviewPage:
    """Resolve only a published page's private allowlist, never a requested file path."""
    match = re.fullmatch(r"/([a-z0-9]{6})/changes\.html", urlsplit(request_path).path)
    if match is None:
        raise FileNotFoundError("not a change page")
    folder = root / match[1]
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise FileNotFoundError("not a regular page manifest")
    decoded = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(decoded, dict):
        raise ValueError("invalid page manifest")
    values = cast(dict[str, object], decoded)
    review_list, production, rows, token = (values.get(key) for key in
                                          ("review_list", "production", "rows", "token"))
    if not (isinstance(review_list, str) and isinstance(production, str)
            and isinstance(token, str) and token and isinstance(rows, list)):
        raise ValueError("invalid page manifest")
    row_values = cast(list[object], rows)
    if not row_values or not all(isinstance(row, str) and re.fullmatch(r"C\d+", row) for row in row_values):
        raise ValueError("invalid page rows")
    return ReviewPage(Path(review_list), Path(production), tuple(cast(list[str], rows)), token)


def shown_rows(page: ReviewPage) -> list[dict[str, str]]:
    rows = {row["#"]: row for row in change_review.review_rows(page.review_list)}
    selected: list[dict[str, str]] = []
    for row_id in page.rows:
        row = rows.get(row_id, {})
        if row.get(change_review.KIND) not in change_review.REVIEW_KINDS:
            raise ValueError(f"{row_id}: classify with change_review.py set-kind before publishing")
        if not row.get("Change") or not row.get(change_review.CHECK):
            raise ValueError(f"{row_id}: needs what changed and how to check / proof")
        selected.append(row)
    return selected


def answer_rows(page: ReviewPage) -> list[dict[str, str]]:
    """Allow older unclassified rows when the browser supplied a signed row token."""
    found = {row["#"]: row for row in change_review.review_rows(page.review_list)}
    if any(row not in found for row in page.rows):
        raise AnswerRefused("this change is no longer on the review list")
    return [found[row] for row in page.rows]


def answer_row(page: ReviewPage, row_id: str) -> dict[str, str]:
    return next(row for row in answer_rows(page) if row["#"] == row_id)


def answer_status(verdict: str, slots: DeliverySlots) -> str:
    if verdict:
        return "Saved: " + verdict
    return "The showrunner will validate this" if slots.validation_requested else ""


def list_routing(snapshots: dict[Path, change_review.ReviewListSnapshot]) -> change_routing.StaffRouting:
    documents = change_routing.production_documents(dict(showrunners.timer_docs()),
        ((listing.stem.removeprefix(change_review.PREFIX), snapshot.text) for listing, snapshot in snapshots.items()))
    return change_routing.StaffRouting(staff.snapshot(), documents)


def render(page: ReviewPage, delivery: DeliveryStatus, notice: AnswerNotice = NO_ANSWER_NOTICE) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    sections: list[str] = []
    saved = False
    for row in shown_rows(page):
        row_id = html.escape(row["#"])
        verdict = row.get("Verdict", "")
        saved = saved or bool(verdict)
        slots = delivery.slots(page, row["#"])
        question = slots.question
        warning = "Not saved: " + notice.reason if notice.reason and notice.row == row["#"] else ""
        alert = f'<p role="alert">{html.escape(warning)}</p>' if warning else ""
        form_notice = notice if notice.row == row["#"] else AnswerNotice(row["#"])
        status_text = answer_status(verdict, slots)
        answer = (f'<p role="status">{html.escape(status_text)}</p>' if status_text and (verdict or not warning) else
                  "" if isinstance(question, OpenQuestion) else answer_form(page, form_notice, feedback_rows))
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        sections.append(f'''<section id="{row_id}">
{alert}
<h1>{html.escape(row["Change"])}</h1>
{change_details(row, change_review.display_id(row, route_owner(page, row).tab))}
<h2>{proof_title}</h2>{review_text_html(row[change_review.CHECK], "pre")}
{question_html(question)}{answer}</section>''')
    if notice.reason and not notice.row:
        sections.insert(0, f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>')
    queue = next_item.change_queue(page.production)
    moved = urgency.ended(queue.name)
    if saved or any(queue_key(page, row) in moved for row in page.rows):
        if queue.entries:
            sections.append(f'''<form method="post">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<button name="outcome" value="next">Skip for now</button></form>''')
        else:
            sections.append('<p>No changes to review.</p>')
    return document("".join(sections))


def queue_key(page: ReviewPage, row: str) -> str:
    """Keep the current list's row IDs and qualify rows from an earlier list."""
    if page.review_list.resolve() == change_review.scope_list(page.production).resolve():
        return row
    name = page.review_list.stem.removeprefix(change_review.PREFIX)
    return f"{name}/{row}"


def publish_next(page: ReviewPage, body: str) -> str:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if (set(fields) != {"token", "outcome"} or fields.get("outcome") != ["next"]
            or len(fields["token"]) != 1
            or not secrets.compare_digest(fields["token"][0].encode("utf-8"), page.token.encode("utf-8"))):
        raise AnswerRefused("use the Next control on this page")
    queue = next_item.change_queue(page.production)
    moved = urgency.ended(queue.name)
    if not any(row.get("Verdict") or queue_key(page, row["#"]) in moved for row in shown_rows(page)):
        raise AnswerRefused("answer a change first")
    ordered = urgency.queue_order(queue.entries, queue.name)
    if not ordered:
        raise AnswerRefused("no changes remain")
    name, separator, row = ordered[0].key.rpartition("/")
    review_list = change_review.list_file(name) if separator else change_review.scope_list(page.production)
    return publish(page.production, review_list, [row])


def publish(production: Path, review_list: Path, rows: list[str]) -> str:
    page = ReviewPage(review_list.resolve(), production.resolve(), tuple(dict.fromkeys(rows)), secrets.token_urlsafe())
    if not page.rows or any(re.fullmatch(r"C\d+", row) is None for row in page.rows):
        raise ValueError("name one or more row IDs such as C27")
    _ = shown_rows(page)
    _ = showrunner(page)
    manifest: PageManifest = {"review_list": str(page.review_list), "production": str(page.production),
                              "rows": list(page.rows), "token": page.token}
    # A fresh page has its own immutable row allowlist. Only metadata is stored
    # beside the HTML: every GET reads the answers from the review record.
    root = review_link.review_root()
    with tempfile.TemporaryDirectory(prefix="review-changes-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot_at(root, [source], new=True)[0]
    folder = root / urlsplit(url).path.split("/")[1]
    atomic_file.write_bytes_atomic(folder / MANIFEST, json.dumps(manifest).encode("utf-8"), mode=0o600)
    return url


def showrunner(page: ReviewPage) -> str:
    """The record names the recipient, including when its production is offline."""
    if isinstance(page, AssignedReviewPage):
        return page.owner.tab
    text = change_review.read(page.review_list)
    frontmatter = text.split("---", 2)
    if len(frontmatter) == 3:
        for line in frontmatter[1].splitlines():
            if line.startswith("showrunner:") and (name := line.partition(":")[2].strip()):
                return name
    raise ValueError("review list has no showrunner in its frontmatter")


def row_production(page: ReviewPage, row: dict[str, str]) -> Path:
    """Resolve the row's Production cell for the existing feature-log closeout rule."""
    record = staff.snapshot()
    if not row.get("Verdict"):
        assignment = change_routing.StaffRouting(record, {}).owner_and_production(page.review_list, row)
        if isinstance(assignment.production, Path):
            return assignment.production
        raise AnswerRefused(assignment.production.reason)
    root = change_review.showrunner_of(page.production, record=record)
    owner = row.get(change_review.PRODUCTION) or root
    found = showrunners.owner_for(owner, record)
    if isinstance(found.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return found.doc.path
    raise AnswerRefused(f"unknown production {owner} for {row['#']}")


def route_owner(page: ReviewPage, row: dict[str, str]) -> change_routing.ChangeOwner:
    """Resolve a legacy selected-row page through the shared staff router."""
    if isinstance(page, AssignedReviewPage):
        return page.owner
    routing = list_routing({page.review_list: change_review.review_snapshot(page.review_list)})
    return routing.owner_and_production(page.review_list, row).owner


def send_to_staff(identity: staff.StaffId, sender: str, message: str) -> None:
    """Resolve the current conversation immediately before each send."""
    member = staff.snapshot().by_id(identity)
    if member is None:
        raise RuntimeError(f"no staff record for {identity}")
    send_message(f"session:{member.current_session}", sender, message)


send_message = unrouted_alert.send_message


def close_feature_log(page: ReviewPage, doc: Path, row_id: str, verdict: str) -> None:
    """Apply step one of CloseChangeReview before the verdict closes the row."""
    list_name = page.review_list.stem.removeprefix(change_review.PREFIX)
    review = row_id if list_name == "hana" else f"{list_name}/{row_id}"
    projects = {name for name, spec in feature_log.PROJECTS.items()
                if spec.closes_by_change_review}
    lines = doc.read_text(encoding="utf-8").splitlines()
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    project = feature_log.project_of(str(repo))
    if project not in projects:
        return
    try:
        if change_review.lands(verdict):
            _ = feature_log.land_review(review, if_proposed=True)
        else:
            _ = feature_log.drop_review(review, verdict, if_proposed=True)
    except feature_log.FeatureLogError as error:
        raise RuntimeError(error.diagnostic) from error


def closeout_before_save(page: ReviewPage, doc: Path, row_id: str, verdict: str, status: DeliveryStatus) -> None:
    """Keep the row open and notify its showrunner when feature-log closeout fails."""
    try:
        close_feature_log(page, doc, row_id, verdict)
    except SEND_ERRORS as error:
        message = f"Change review {row_id} stays open: feature log failed.\n{error}\nList: {page.review_list}"
        try:
            row = answer_row(page, row_id)
            owner = route_owner(page, row)
            status.notify_closeout_failure(page, row_id, message, owner.showrunner_recipients)
        except SEND_ERRORS as delivery_error:
            raise FeatureLogFailed(f"{message}\nNotification failed: {delivery_error}") from delivery_error
        raise FeatureLogFailed(message) from error


def submit(page: ReviewPage, body: str, status: DeliveryStatus, *,
           prepared: change_review.ChangeQueue | next_item.ReadChangeLists = next_item.READ_CHANGE_LISTS) -> MessageDelivery | NoMessageDelivery:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(values) != 1 for values in fields.values()):
        raise AnswerRefused("expected only row, token, outcome and feedback")
    row_id, token, outcome, feedback = (fields[key][0] for key in ("row", "token", "outcome", "feedback"))
    feedback = "\n".join(feedback.splitlines())
    if row_id not in page.rows or not secrets.compare_digest(token.encode("utf-8"), page.token.encode("utf-8")):
        raise AnswerRefused("row or page token does not match this page")
    if outcome not in ("works", "not works", "validate", "feedback", "question"):
        raise AnswerRefused("choose an answer")
    if outcome in ("feedback", "question") and not feedback.strip():
        raise AnswerRefused("enter a question" if outcome == "question" else "enter feedback")
    one_line = " ".join(line.strip() for line in feedback.split("\n") if line.strip())
    verdict = one_line or outcome
    if outcome in ("works", "not works") and one_line:
        verdict = f"{outcome}: {one_line}"
    rows = answer_rows(page) if isinstance(page, AssignedReviewPage) else shown_rows(page)
    row = next(row for row in rows if row["#"] == row_id)
    if isinstance(page, AssignedReviewPage):
        assignment = change_routing.ChangeAssignment(page.owner, page.production)
    else:
        routing = list_routing({page.review_list: change_review.review_snapshot(page.review_list)})
        assignment = routing.owner_and_production(page.review_list, row)
    owner = assignment.owner
    if isinstance(assignment.production, change_routing.UnavailableProduction):
        raise AnswerRefused("the production document is not available")
    message_page = replace(page, production=assignment.production)
    recipients = owner.showrunner_recipients
    request_feedback = outcome == "feedback" and not change_review.lands(one_line)
    if (outcome == "question" or request_feedback) and isinstance(owner, change_routing.UnroutedChange):
        raise AnswerRefused("this row has no showrunner to answer a question")
    if outcome == "question" or request_feedback:
        if row.get("Verdict"):
            raise AnswerRefused("this row already has a saved answer")
        _ = row_production(page, row)
        intent = status.ask(page, row_id, feedback, recipients=recipients, kind="feedback" if request_feedback else "question")
        if isinstance(intent, NoDelivery):
            return NoMessageDelivery()
        message = _request_message(message_page, row, intent.question_id, intent.text, request_feedback)
        return MessageDelivery(message_page, row_id, recipients, message, intent.attempt, owner.showrunner_recipients)
    if isinstance(status.slots(page, row_id).question, OpenQuestion):
        raise AnswerRefused("this row has an open question")
    if outcome == "validate":
        if row.get("Verdict"):
            raise AnswerRefused("this row already has a saved answer")
        _ = row_production(page, row)
        if status.slots(page, row_id).validation_requested:
            return NoMessageDelivery()
        _ = next_item.move_to_end(next_item.CHANGE, page.production, key=queue_key(page, row_id), prepared=prepared)
        attempt = status.begin(page, row_id, "validation", feedback, recipients=recipients)
        message = review_message(message_page, row, "the user asks you to validate this", f"User feedback: {feedback}")
        return MessageDelivery(message_page, row_id, recipients, message, attempt, owner.showrunner_recipients)
    if row.get("Verdict"):
        if row["Verdict"] != verdict:
            raise AnswerRefused("this row already has a different saved answer")
        return NoMessageDelivery()
    doc = row_production(page, row)
    recipients = verdict_recipients(owner, verdict)
    closeout_before_save(page, doc, row_id, verdict, status)
    attempt = status.begin(page, row_id, "verdict", recipients=recipients) if recipients else ""
    try:
        production = change_review.production_doc(unit_lookup.doc_owner(page.production, staff.snapshot()).name)
        result = change_review.save_verdict(page.review_list, row_id, verdict, production)
    except (change_review.ChangeReviewError, OSError) as error:
        status.clear(page, row_id, "verdict")
        raise AnswerRefused(str(error).strip()) from error
    if isinstance(result, change_review.VerdictRefused):
        status.clear(page, row_id, "verdict")
        raise AnswerRefused(result.reason)
    message = review_message(message_page, row, verdict, f"Merged: {row.get('Merged', '')}",
                             f"User feedback: {feedback if feedback.strip() else verdict}", "\n".join(result.lines).strip())
    return MessageDelivery(message_page, row_id, recipients, message, attempt, owner.showrunner_recipients) if recipients else NoMessageDelivery()


def review_message(page: ReviewPage, row: dict[str, str], headline: str, *details: str) -> str:
    shown = change_review.display_id(row, route_owner(page, row).tab)
    return "\n".join((f"Change review {shown}: {headline}", f"List: {page.review_list}",
                      f"Production: {unit_lookup.doc_owner(page.production, staff.snapshot()).name}", f"Change: {row['Change']}", *details))


def _request_message(page: ReviewPage, row: dict[str, str], identity: str, text: str, feedback: bool) -> str:
    label = "Feedback" if feedback else "Question"
    return review_message(page, row, "the user sends feedback to act on" if feedback else "the user asks a question",
                          f"{label} id: {identity}", f"{label}: {text}")


def verdict_recipients(owner: change_routing.ChangeOwner, verdict: str) -> tuple[staff.StaffId, ...]:
    """Use the same staff recipients for new and legacy verdict attempts."""
    if verdict != "works" and isinstance(owner, change_routing.StaffRoutedChange):
        return tuple(dict.fromkeys((owner.unit_id, *owner.showrunner_recipients)))
    return owner.showrunner_recipients


@dataclass(frozen=True)
class MissingRecipient:
    """No staff recipient exists for this message."""


@dataclass(frozen=True)
class DeliveryFailure:
    """Keep one failed recipient and its last send error."""

    recipient: staff.StaffId | MissingRecipient
    error: str


def deliver(delivery: MessageDelivery | NoMessageDelivery, status: DeliveryStatus) -> None:
    """Deliver each saved message and report exhausted attempts to its showrunner."""
    if isinstance(delivery, NoMessageDelivery):
        return
    settings = conf.read("review_links")
    tries = settings.whole("delivery_tries")
    pause = settings.number("delivery_pause_seconds")
    with status.locked():
        slots = status.slots(delivery.page, delivery.row)
        ownership = status.request_attempts.get(delivery.attempt)
        if ownership is not None and ownership.state == "sending":
            return
        if not any(intent.attempt == delivery.attempt for intent in slots.intents()):
            _ = status.request_attempts.pop(delivery.attempt, None)
            return
        status.request_attempts[delivery.attempt] = AttemptOwnership(status.path(delivery.page, delivery.row), "sending")
    remaining = delivery.recipients
    failures: list[DeliveryFailure] = []
    if not remaining:
        failures.append(DeliveryFailure(MissingRecipient(), "no showrunner to send to"))
    for attempt in range(tries):
        if not remaining:
            break
        failures = []
        for target in remaining:
            try:
                send_to_staff(target, SENDER, delivery.message)
            except SEND_ERRORS as error:
                failures.append(DeliveryFailure(target, str(error)))
        remaining = tuple(failure.recipient for failure in failures if isinstance(failure.recipient, str))
        if remaining and attempt + 1 < tries:
            time.sleep(pause)
    alert_delivery_failures(delivery, failures, tries)
    status.record(delivery)


def alert_delivery_failures(delivery: MessageDelivery, failures: list[DeliveryFailure], tries: int) -> None:
    for failure in failures:
        target = "no recipient" if isinstance(failure.recipient, MissingRecipient) else failure.recipient
        error = failure.error
        alert = "\n".join(("Change review delivery needs attention", f"List: {delivery.page.review_list}",
                           f"Row: {delivery.row}", f"Recipient: {target}", f"Tries: {tries}",
                           f"Last error: {error}", "The user's answer is saved. Pass it on by hand.", delivery.message))
        if not delivery.alert_recipients:
            print(f"review-links delivery {delivery.row}: {error}; no showrunner for the alert", file=sys.stderr)
        for runner in delivery.alert_recipients:
            try:
                send_to_staff(runner, SENDER, alert)
            except SEND_ERRORS as failure:
                print(f"review-links delivery alert {delivery.row}: " + " ".join(str(failure).splitlines()), file=sys.stderr)
                return


def recover_pending(status: DeliveryStatus) -> None:
    """Find pending messages through one scan of the current review lists."""
    recover_question_answers(status)
    paths = status.read_view().paths
    if not paths:
        return
    snapshots = {listing: change_review.review_snapshot(listing)
                 for listing in change_review.lists_directory().glob(change_review.PREFIX + "*.md")}
    routing: change_routing.StaffRouting | None = None
    for listing, snapshot in snapshots.items():
        for row in (*snapshot.pending, *snapshot.reviewed):
            path = change_routing.delivery_path(status.root, listing, row["#"])
            if path not in paths or path.is_symlink():
                continue
            recovered_attempts: tuple[str, ...] = ()
            try:
                # A live request owns its intent before it saves the verdict.
                with status.locked():
                    if any(ownership.row_file == path for ownership in status.request_attempts.values()):
                        continue
                    fields = cast(object, json.loads(path.read_text(encoding="utf-8")))
                    decoded = _decode_slots(fields)
                    intents = decoded.intents()
                    if not intents or any(intent.attempt in status.request_attempts for intent in intents):
                        continue
                    if isinstance(decoded.verdict, DeliveryIntent) and not row.get("Verdict"):
                        record_page = ReviewPage(listing, listing, (row["#"],), "")
                        status.clear_locked(record_page, row["#"], decoded, "verdict")
                        decoded = decoded.updated("verdict", NoDelivery())
                    recovered_attempts = tuple(intent.attempt for intent in decoded.intents())
                    for attempt in recovered_attempts:
                        status.request_attempts[attempt] = AttemptOwnership(path, "claimed")
                if not decoded.intents():
                    continue
                if routing is None:
                    routing = list_routing(snapshots)
                assignment = routing.owner_and_production(listing, row)
                owner = assignment.owner
                if isinstance(assignment.production, change_routing.UnavailableProduction):
                    raise ValueError("the production document is not available")
                page = AssignedReviewPage(listing, assignment.production, (row["#"],), "", owner)
                for intent in decoded.intents():
                    recipients = intent.recipients or (verdict_recipients(owner, row["Verdict"])
                                  if intent.kind == "verdict" else owner.showrunner_recipients)
                    question = decoded.question
                    if intent.kind == "question" and isinstance(question, OpenQuestion):
                        message = _request_message(page, row, question.question_id, question.text, isinstance(question, OpenFeedback))
                    elif intent.kind == "validation":
                        message = review_message(page, row, "the user asks you to validate this", f"User feedback: {intent.feedback}")
                    else:
                        message = review_message(page, row, row["Verdict"], f"Merged: {row.get('Merged', '')}",
                                                 f"User feedback: {row['Verdict']}")
                    deliver(MessageDelivery(page, row["#"], recipients, message, intent.attempt, owner.showrunner_recipients), status)
            except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
                print(f"review-links recovery {path.name}: {error}", file=sys.stderr)
            finally:
                if recovered_attempts:
                    with status.locked():
                        for attempt in recovered_attempts:
                            ownership = status.request_attempts.get(attempt)
                            if ownership is not None and ownership.state == "claimed":
                                _ = status.request_attempts.pop(attempt, None)


def recover_question_answers(status: DeliveryStatus) -> None:
    """Recover saved user answers through the existing delivery claims and retry budget."""
    for path in status.read_view().paths:
        claimed: tuple[str, ...] = ()
        try:
            with status.locked():
                if any(owner.row_file == path for owner in status.request_attempts.values()):
                    continue
                fields = cast(StoredDeliverySlots, json.loads(path.read_text()))
                saved = fields.get("user_answer")
                if saved is None:
                    continue
                slots = delivery_state.decode_value(fields)
                intents = slots.intents()
                if not intents:
                    continue
                page = UserQuestionPage(Path(saved["source"]), Path(saved["production"]),
                                        (Path(saved["source"]).stem,), "", saved)
                identities = update_registration.QuestionStaff.read()
                question = next((item for item in update_registration.question_records_for_document(page.production, identities)
                                 if item.path == page.review_list), None)
                if question is None or question.status != "closed":
                    status.clear_locked(page, page.rows[0], slots, "verdict")
                    continue
                if not saved.get("saved", False):
                    status.clear_locked(page, page.rows[0], slots, "verdict")
                    print(f"review-links recovery {path.name}: answer save state unknown; not sent", file=sys.stderr)
                    continue
                claimed = tuple(intent.attempt for intent in intents)
                for attempt in claimed:
                    status.request_attempts[attempt] = AttemptOwnership(path, "claimed")
            for intent in intents:
                deliver(MessageDelivery(page, page.rows[0], intent.recipients, saved["message"], intent.attempt,
                                        (staff.StaffId(saved["owner"]),)), status)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"review-links recovery {path.name}: {error}", file=sys.stderr)
        finally:
            with status.locked():
                for attempt in claimed:
                    owner = status.request_attempts.get(attempt)
                    if owner is not None and owner.state == "claimed":
                        del status.request_attempts[attempt]


def publish_browser(initial: str = "") -> str:
    """Publish one all-list page without copying change records."""
    root = review_link.review_root()
    with tempfile.TemporaryDirectory(prefix="review-browser-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot_at(root, [source], new=True)[0]
    folder = root / urlsplit(url).path.split("/")[1]
    manifest: BrowserManifest = {"token": secrets.token_urlsafe(), "initial": initial}
    atomic_file.write_bytes_atomic(folder / BROWSER_MANIFEST, json.dumps(manifest).encode(), mode=0o600)
    return url


def page_url() -> str:
    """Publish the Changes page and return its path under the review root."""
    return urlsplit(publish_browser()).path


def main(arguments: list[str]) -> int:
    if arguments and arguments[0] == "answer-question":
        parser = argparse.ArgumentParser(description="Answer an open question about a change.")
        _ = parser.add_argument("--production", required=True)
        _ = parser.add_argument("--list", required=True, type=Path)
        _ = parser.add_argument("--row", required=True)
        _ = parser.add_argument("--question", required=True)
        _ = parser.add_argument("--text", required=True)
        args = parser.parse_args(arguments[1:])
        try:
            target = change_review.resolve_row(cast(str, args.row), production=cast(str, args.production),
                                               review_list=cast(Path, args.list))
            shown = change_review.row_display_id(target.path, target.row)
            page = ReviewPage(target.path, change_review.production_doc(cast(str, args.production)), (target.row,), "")
            changed = DeliveryStatus(review_link.review_root()).answer_question(
                page, page.rows[0], cast(str, args.question), cast(str, args.text))
        except (OSError, ValueError, change_review.ChangeReviewError, Refusal, subprocess.SubprocessError) as error:
            print(f"change page: {error}", file=sys.stderr)
            return 1
        print(f"Answered {shown}" if changed else f"{shown} already has this answer")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("rows", nargs="*")
    _ = parser.add_argument("--production")
    _ = parser.add_argument("--list", type=Path)
    args = parser.parse_args(arguments)
    rows = cast(list[str], args.rows)
    production = cast(str | None, args.production)
    review_list = cast(Path | None, args.list)
    try:
        if not rows:
            from scripts.review_links import console

            print(console.publish())
            return 0
        if production is None:
            raise ValueError("pass --production for an older selected-row link")
        doc = change_review.production_doc(production)
        targets = [change_review.resolve_row(row, production=production, review_list=review_list) for row in rows]
        if any(target.path.resolve() != targets[0].path.resolve() for target in targets):
            raise ValueError("selected rows must belong to one review list")
        print(publish(doc, targets[0].path, [target.row for target in targets]))
    except (OSError, ValueError, change_review.ChangeReviewError, subprocess.SubprocessError, Refusal) as error:
        print(f"change page: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
