#!/usr/bin/env python3
"""Publish all change reviews, or retain an older selected-row link with --production DOC C27."""
from __future__ import annotations

import argparse
import fcntl
import html
import json
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import change_review as change_review
from scripts.production import change_routing, delivery_state, next_item, showrunners, showrunner_tree, staff
from scripts.production.delivery_state import (
    AnsweredQuestion as AnsweredQuestion, DeliveryIntent as DeliveryIntent, DeliverySlots as DeliverySlots,
    NoDelivery as NoDelivery, NoQuestion as NoQuestion, OpenQuestion as OpenQuestion,
    ValidationRequested as ValidationRequested,
)
from scripts.lib import urgency
from scripts.production.production_units import Refusal as Refusal, production_field
from scripts.production.proposals_folder import owner_docs, slug_of
from scripts.feature_log import feature_log
from scripts.review_links import review_link

MANIFEST = ".review-link-changes.json"
PAGE_NAME = "changes.html"
BROWSER_MANIFEST = ".review-link-browser.json"
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
    state: Literal["pending", "failed"]
    recipients: list[str]
    failure_note: NotRequired[str]


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


class StoredAnsweredQuestion(TypedDict):
    question_id: str
    text: str
    answer: str


class StoredDeliverySlots(TypedDict, total=False):
    validation: StoredValidationIntent | StoredValidationRequested
    verdict: StoredDeliveryIntent
    question: StoredOpenQuestion | StoredAnsweredQuestion


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


class AnswerRefused(ValueError):
    """An invalid answer must not change the review record."""


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


@dataclass(frozen=True)
class NoMessageDelivery:
    """A repeated question needs no additional message."""


def stored_intent(intent: DeliveryIntent) -> StoredDeliveryIntent:
    fields: StoredDeliveryIntent = {"attempt": intent.attempt, "state": intent.state, "recipients": list(intent.recipients)}
    if intent.failure_note:
        fields["failure_note"] = intent.failure_note
    return fields


class DeliveryStatus:
    """Own delivery intents and serialize conditional attempt updates."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.question_messages_in_flight: set[str] = set()

    def read_view(self) -> DeliveryReadView:
        return DeliveryReadView(self, frozenset(self.root.glob(DELIVERY_PREFIX + "*")))

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".review-link-delivery.lock").open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def path(self, page: ReviewPage, row: str) -> Path:
        return change_routing.delivery_path(self.root, page.review_list, row)

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        try:
            content = self.path(page, row).read_text(encoding="utf-8")
        except FileNotFoundError:
            return DeliverySlots(NoDelivery(), NoDelivery())
        return delivery_state.decode(content)

    def read(self, page: ReviewPage, row: str) -> DeliveryIntent | NoDelivery:
        saved = any(item["#"] == row and item.get("Verdict") for item in change_review.review_rows(page.review_list))
        return self.slots(page, row).selected(saved)

    def write_slots(self, page: ReviewPage, row: str, slots: DeliverySlots) -> None:
        """Write both slots while the caller holds the delivery lock."""
        fields: StoredDeliverySlots = {}
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
        path = self.path(page, row)
        if fields:
            review_link.replace_file(path, (json.dumps(fields) + "\n").encode("utf-8"))
        else:
            path.unlink(missing_ok=True)

    def failed(self, page: ReviewPage, row: str) -> bool:
        intent = self.read(page, row)
        return isinstance(intent, DeliveryIntent) and intent.state == "failed"

    def begin(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation"], feedback: str = "", *, recipients: tuple[staff.StaffId, ...] = ()) -> str:
        attempt = secrets.token_hex()
        intent = DeliveryIntent(attempt, "pending", kind, feedback if kind == "validation" else "", recipients)
        with self.locked():
            self.write_slots(page, row, self.slots(page, row).updated(kind, intent))
        return attempt

    def record(self, delivery: MessageDelivery, failed: bool, *, failure_note: str = "") -> None:
        with self.locked():
            self.question_messages_in_flight.discard(delivery.attempt)
            slots = self.slots(delivery.page, delivery.row)
            question_delivery = slots.question.delivery if isinstance(slots.question, OpenQuestion) else NoDelivery()
            for current in (slots.validation, slots.verdict, question_delivery):
                if isinstance(current, DeliveryIntent) and current.attempt == delivery.attempt:
                    if failed:
                        updated = slots.updated(current.kind, replace(current, state="failed", failure_note=failure_note))
                    elif current.kind == "validation":
                        updated = replace(slots, validation=ValidationRequested(current.attempt, current.feedback))
                    else:
                        updated = slots.updated(current.kind, NoDelivery())
                    self.write_slots(delivery.page, delivery.row, updated)
                    return

    def ask(self, page: ReviewPage, row: str, text: str, *, recipients: tuple[staff.StaffId, ...] = ()) -> QuestionDelivery | NoDelivery:
        """Save a new question without changing an existing open question."""
        with self.locked():
            slots = self.slots(page, row)
            question = slots.question
            if isinstance(question, OpenQuestion):
                if question.text != text:
                    raise AnswerRefused("this row already has an open question")
                return NoDelivery()
            intent = DeliveryIntent(secrets.token_hex(), "pending", "question", recipients=recipients)
            question_id = secrets.token_hex(4)
            self.write_slots(page, row, replace(slots, validation=NoDelivery(), question=OpenQuestion(question_id, text, intent)))
            self.question_messages_in_flight.add(intent.attempt)
            return QuestionDelivery(question_id, text, intent.attempt)

    def retry_question(self, page: ReviewPage, row: str, *, recipients: tuple[staff.StaffId, ...] = ()) -> QuestionDelivery:
        """Retry the current question without creating or replacing a question."""
        with self.locked():
            slots = self.slots(page, row)
            question = slots.question
            if (not isinstance(question, OpenQuestion) or not isinstance(question.delivery, DeliveryIntent)
                    or question.delivery.attempt in self.question_messages_in_flight):
                raise AnswerRefused("this row has no question to send again")
            intent = replace(question.delivery, attempt=secrets.token_hex(), state="pending",
                             recipients=question.delivery.recipients or recipients)
            self.write_slots(page, row, replace(slots, question=replace(question, delivery=intent)))
            self.question_messages_in_flight.add(intent.attempt)
            return QuestionDelivery(question.question_id, question.text, intent.attempt)

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
            record = next(item for item in answer_rows(page) if item["#"] == row)
            if record.get("Verdict"):
                raise AnswerRefused("this row already has a saved answer")
            _ = row_production(page, record)
            self.write_slots(page, row, replace(slots, question=AnsweredQuestion(question_id, question.text, text)))
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
.id,h2{opacity:.7;font-size:14px}.kind{font-weight:600}
h1{font-size:26px;line-height:1.25}h2{margin:28px 0 6px}p{white-space:pre-wrap}
pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8886;border-radius:6px;padding:12px;font:14px/1.45 ui-monospace,monospace}
section+section{border-top:1px solid #8886;margin-top:36px;padding-top:28px}
form{margin-top:28px}label,input{display:block}input[type=text]{box-sizing:border-box;width:100%;font:inherit;margin:6px 0 12px}
button{font:inherit;margin:0 8px 8px 0;padding:6px 12px}button[aria-pressed=true]{outline:2px solid currentColor}
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


def answer_form(page: ReviewPage, notice: AnswerNotice) -> str:
    """Render an answer without reading rows or queue state."""
    row = notice.row if notice.row in page.rows else page.rows[0]
    row_id = html.escape(row)
    buttons: list[str] = []
    for value, label in (("works", "Works"), ("not works", "Not works"),
                         ("validate", "Validate yourself"), ("feedback", "Send feedback"), ("question", "Ask a question")):
        if value == "question" and isinstance(page, AssignedReviewPage) and isinstance(page.owner, change_routing.UnroutedChange):
            continue
        selected = 'aria-pressed="true" ' if notice.outcome == value else ""
        buttons.append(f'<button {selected}name="outcome" value="{value}">{label}</button>')
    return f'''<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<label for="feedback-{row_id}">Feedback or question</label>
<input id="feedback-{row_id}" name="feedback" type="text" value="{html.escape(notice.feedback, quote=True)}">
<div>{"".join(buttons)}</div></form>'''


def fallback_answer(page: ReviewPage, notice: AnswerNotice) -> bytes:
    return document(f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>' + answer_form(page, notice))


def question_html(question: NoQuestion | OpenQuestion | AnsweredQuestion) -> str:
    if isinstance(question, NoQuestion):
        return ""
    content = f'<h2>Question</h2><p>{html.escape(question.text)}</p>'
    if isinstance(question, AnsweredQuestion):
        content += f'<h2>Answer</h2><p>{html.escape(question.answer)}</p>'
    else:
        content += '<p>The showrunner will answer this</p>'
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


def render(page: ReviewPage, delivery: DeliveryStatus, notice: AnswerNotice = NO_ANSWER_NOTICE) -> bytes:
    sections: list[str] = []
    saved = False
    for row in shown_rows(page):
        row_id = html.escape(row["#"])
        verdict = row.get("Verdict", "")
        saved = saved or bool(verdict)
        slots = delivery.slots(page, row["#"])
        intent = slots.selected(bool(verdict))
        question = slots.question
        failure = False
        pending = False
        failed_feedback = ""
        if isinstance(intent, DeliveryIntent):
            failure = True
            pending = intent.state == "pending"
            failed_feedback = question.text if isinstance(question, OpenQuestion) else intent.feedback if not verdict else ""
        warning = ("Not saved: " + notice.reason if notice.reason and notice.row == row["#"]
                   else "The message may not have sent" if pending
                   else "Saved, but the message did not send" if verdict and failure
                   else "The question message did not send" if failure and isinstance(question, OpenQuestion)
                   else "The validation message did not send" if failure else "")
        alert = f'<p role="alert">{html.escape(warning)}</p>' if warning else ""
        retry_outcome = "retry" if verdict or isinstance(question, OpenQuestion) else "validate"
        retry_feedback = "" if isinstance(question, OpenQuestion) else failed_feedback
        retry_label = ("Send saved answer again" if verdict else "Send question again"
                       if isinstance(question, OpenQuestion) else "Send validation request again")
        retry = (f'''
<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<input type="hidden" name="feedback" value="{html.escape(retry_feedback, quote=True)}">
<button name="outcome" value="{retry_outcome}">{retry_label}</button>
</form>''' if failure else "")
        form_notice = notice if notice.row == row["#"] else AnswerNotice(row["#"], feedback=failed_feedback)
        answer = (f'<p role="status">Saved: {html.escape(verdict)}</p>' + retry if verdict
                  else retry if isinstance(question, OpenQuestion) else answer_form(page, form_notice) + retry)
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        sections.append(f'''<section id="{row_id}">
{alert}
<p class="kind">{html.escape(row[change_review.KIND])}</p>
<div class="id">{row_id} · {html.escape(row.get("Merged", ""))}</div>
<h1>{html.escape(row["Change"])}</h1>
<h2>{proof_title}</h2><pre>{html.escape(row[change_review.CHECK])}</pre>
{question_html(question)}{answer}</section>''')
    if notice.reason and not notice.row:
        sections.insert(0, f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>')
    queue = next_item.change_queue(page.production)
    moved = urgency.ended(queue.name)
    if saved or any(queue_key(page, row) in moved for row in page.rows):
        sections.append(f'<p>{len(queue.entries)} changes left</p>')
        if queue.entries:
            sections.append(f'''<form method="post">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<button name="outcome" value="next">Next</button></form>''')
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
    with tempfile.TemporaryDirectory(prefix="review-changes-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot([source], new=True)[0]
    folder = review_link.review_root() / urlsplit(url).path.split("/")[1]
    review_link.replace_file(folder / MANIFEST, json.dumps(manifest).encode("utf-8"))
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
    """Resolve the row's owner across the root production's complete hierarchy."""
    root = showrunner_tree.root_of(slug_of(page.production))
    docs = {**showrunners.timer_docs(), slug_of(page.production): page.production}
    docs.update({slug_of(doc): doc for doc in change_routing.list_documents(change_review.read(page.review_list))})
    if root not in docs:
        raise AnswerRefused(f"root production {root} has no production document")
    owner = row.get(change_review.PRODUCTION) or root
    for doc in dict.fromkeys([*owner_docs(docs[root]), *docs.values()]):
        if slug_of(doc) == owner:
            return doc
    raise AnswerRefused(f"unknown production {owner} for {row['#']}")


def route_owner(page: ReviewPage, row: dict[str, str]) -> change_routing.ChangeOwner:
    """Resolve a legacy selected-row page through the shared staff router."""
    if isinstance(page, AssignedReviewPage):
        return page.owner
    documents = {**showrunners.timer_docs(), slug_of(page.production): page.production,
                 showrunner(page): page.production}
    routing = change_routing.StaffRouting(staff.snapshot(), documents, change_review)
    return routing.owner(row, showrunner(page))


def send_to_staff(identity: staff.StaffId, sender: str, message: str) -> None:
    """Resolve the current conversation immediately before each send."""
    member = staff.snapshot().by_id(identity)
    if member is not None:
        send_message(f"session:{member.current_session}", sender, message)


def send_message(recipient: str, sender: str, message: str) -> None:
    """The single outside-send boundary; use the existing routing and delivery rules."""
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "message/send.py"),
                             "--to", recipient, "--from", sender, "--text", message],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "message delivery failed")


def close_feature_log(page: ReviewPage, doc: Path, row_id: str, verdict: str) -> None:
    """Apply step one of CloseChangeReview before the verdict closes the row."""
    list_name = page.review_list.stem.removeprefix(change_review.PREFIX)
    review = row_id if list_name == "hana" else f"{list_name}/{row_id}"
    projects = {name for name, spec in feature_log.PROJECTS.items()
                if spec.closes_by_change_review}
    lines = doc.read_text(encoding="utf-8").splitlines()
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    script = str(Path(feature_log.__file__))
    project = subprocess.run([sys.executable, script, "project-of", str(repo)],
                             capture_output=True, text=True, check=True).stdout.strip()
    if project not in projects:
        return
    command = "add" if change_review.lands(verdict) else "drop"
    arguments = ["--review", review, "--if-proposed"]
    if command == "drop":
        arguments.append(f"--verdict={verdict}")
    result = subprocess.run([sys.executable, script, command, *arguments],
                            capture_output=True, text=True, check=False)
    # The writer names the required operation when a proposal replaces an old note.
    if command == "add" and result.returncode and (
            f"proposal {review} uses supersede; run supersede --review {review}" in result.stderr):
        result = subprocess.run([sys.executable, script, "supersede", *arguments],
                                capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "feature-log operation failed")


def closeout_before_save(page: ReviewPage, doc: Path, row_id: str, verdict: str) -> None:
    """Keep the row open and notify its showrunner when feature-log closeout fails."""
    try:
        close_feature_log(page, doc, row_id, verdict)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        message = f"Change review {row_id} stays open: feature log failed.\n{error}\nList: {page.review_list}"
        try:
            row = next(row for row in answer_rows(page) if row["#"] == row_id)
            owner = route_owner(page, row)
            for identity in owner.showrunner_recipients:
                send_to_staff(identity, Path(__file__).stem, message)
        except RuntimeError as delivery_error:
            raise FeatureLogFailed(f"{message}\nNotification failed: {delivery_error}") from delivery_error
        raise FeatureLogFailed(message) from error


def submit(page: ReviewPage, body: str, status: DeliveryStatus, *,
           prepared: change_review.ChangeQueue | next_item.ReadChangeLists = next_item.READ_CHANGE_LISTS) -> MessageDelivery | NoMessageDelivery:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(values) != 1 for values in fields.values()):
        raise AnswerRefused("expected only row, token, outcome and feedback")
    row_id, token, outcome, feedback = (fields[key][0] for key in ("row", "token", "outcome", "feedback"))
    if row_id not in page.rows or not secrets.compare_digest(token.encode("utf-8"), page.token.encode("utf-8")):
        raise AnswerRefused("row or page token does not match this page")
    if (outcome not in ("works", "not works", "validate", "feedback", "question", "retry")
            or feedback.splitlines() not in ([], [feedback])):
        raise AnswerRefused("choose an answer and use one line of feedback")
    if outcome in ("feedback", "question") and not feedback.strip():
        raise AnswerRefused("enter a question" if outcome == "question" else "enter feedback")
    verdict = feedback.strip() if feedback.strip() else outcome
    if outcome in ("works", "not works") and feedback.strip():
        verdict = f"{outcome}: {feedback.strip()}"
    rows = answer_rows(page) if isinstance(page, AssignedReviewPage) else shown_rows(page)
    row = next(row for row in rows if row["#"] == row_id)
    owner = route_owner(page, row)
    recipients = owner.showrunner_recipients
    if outcome == "question" and isinstance(owner, change_routing.UnroutedChange):
        raise AnswerRefused("this row has no showrunner to answer a question")
    if outcome == "question" or (outcome == "retry" and not row.get("Verdict")):
        if row.get("Verdict"):
            raise AnswerRefused("this row already has a saved answer")
        if outcome == "retry" and feedback:
            raise AnswerRefused("retry requires no new feedback")
        _ = row_production(page, row)
        intent = (status.ask(page, row_id, feedback.strip(), recipients=recipients) if outcome == "question"
                  else status.retry_question(page, row_id, recipients=recipients))
        if isinstance(intent, NoDelivery):
            return NoMessageDelivery()
        saved_question = status.slots(page, row_id).question
        if isinstance(saved_question, OpenQuestion) and isinstance(saved_question.delivery, DeliveryIntent):
            recipients = saved_question.delivery.recipients
        message = (f"Change review {row_id}: the user asks a question\n"
                   f"List: {page.review_list}\nProduction: {page.production}\n"
                   f"Change: {row['Change']}\nQuestion id: {intent.question_id}\nQuestion: {intent.text}")
        return MessageDelivery(page, row_id, recipients, message, intent.attempt)
    if isinstance(status.slots(page, row_id).question, OpenQuestion):
        raise AnswerRefused("this row has an open question")
    if outcome == "validate":
        if row.get("Verdict"):
            raise AnswerRefused("this row already has a saved answer")
        _ = row_production(page, row)
        _ = next_item.move_to_end(next_item.CHANGE, page.production, key=queue_key(page, row_id), prepared=prepared)
        attempt = status.begin(page, row_id, "validation", feedback, recipients=recipients)
        message = (f"Change review {row_id}: the user asks you to validate this\n"
                   f"List: {page.review_list}\nProduction: {page.production}\n"
                   f"Change: {row['Change']}\nUser feedback: {feedback}")
        return MessageDelivery(page, row_id, recipients, message, attempt)
    if outcome == "retry":
        if not row.get("Verdict") or feedback:
            raise AnswerRefused("retry requires a saved answer and no new feedback")
        verdict = row["Verdict"]
    if row.get("Verdict") and row["Verdict"] != verdict:
        raise AnswerRefused("this row already has a different saved answer")
    doc = row_production(page, row)
    recipients = verdict_recipients(owner, verdict)
    if outcome == "retry":
        saved = status.slots(page, row_id).verdict
        if isinstance(saved, DeliveryIntent) and saved.recipients:
            recipients = saved.recipients
    if not row.get("Verdict"):
        closeout_before_save(page, doc, row_id, verdict)
    attempt = status.begin(page, row_id, "verdict", recipients=recipients) if recipients else ""
    result = subprocess.run([sys.executable, str(Path(change_review.__file__)), "verdict",
                             "--production", str(page.production), "--list", str(page.review_list),
                             "--row", row_id, f"--verdict={verdict}"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AnswerRefused((result.stderr or result.stdout).strip())
    message = (f"Change review {row_id}: {verdict}\n"
               f"List: {page.review_list}\nProduction: {page.production}\n"
               f"Change: {row['Change']}\nMerged: {row.get('Merged', '')}\n"
               f"User feedback: {feedback if feedback.strip() else verdict}\n{result.stdout.strip()}")
    return MessageDelivery(page, row_id, recipients, message, attempt) if recipients else NoMessageDelivery()


def verdict_recipients(owner: change_routing.ChangeOwner, verdict: str) -> tuple[staff.StaffId, ...]:
    """Use the same staff recipients for new and legacy verdict attempts."""
    if verdict != "works" and isinstance(owner, change_routing.StaffRoutedChange):
        return tuple(dict.fromkeys((owner.unit_id, *owner.showrunner_recipients)))
    return owner.showrunner_recipients


def deliver(delivery: MessageDelivery | NoMessageDelivery, status: DeliveryStatus) -> None:
    if isinstance(delivery, NoMessageDelivery):
        return
    errors: list[str] = [] if delivery.recipients else ["no showrunner to send to"]
    for target in delivery.recipients:
        try:
            send_to_staff(target, Path(__file__).stem, delivery.message)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{target}: {error}")
    if errors:
        print(f"review-links delivery {delivery.row}: " + "; ".join(errors), file=sys.stderr)
    status.record(delivery, bool(errors), failure_note="no showrunner to send to" if not delivery.recipients else "")


def publish_browser(initial: str = "") -> str:
    """Publish one all-list page without copying change records."""
    with tempfile.TemporaryDirectory(prefix="review-browser-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot([source], new=True)[0]
    folder = review_link.review_root() / urlsplit(url).path.split("/")[1]
    manifest: BrowserManifest = {"token": secrets.token_urlsafe(), "initial": initial}
    review_link.replace_file(folder / BROWSER_MANIFEST, json.dumps(manifest).encode())
    return url


def main(arguments: list[str]) -> int:
    if arguments and arguments[0] == "answer-question":
        parser = argparse.ArgumentParser(description="Answer an open question about a change.")
        _ = parser.add_argument("--production", required=True, type=Path)
        _ = parser.add_argument("--list", required=True, type=Path)
        _ = parser.add_argument("--row", required=True)
        _ = parser.add_argument("--question", required=True)
        _ = parser.add_argument("--text", required=True)
        args = parser.parse_args(arguments[1:])
        page = ReviewPage(cast(Path, args.list).expanduser(), cast(Path, args.production).expanduser(),
                          (cast(str, args.row),), "")
        try:
            changed = DeliveryStatus(review_link.review_root()).answer_question(
                page, page.rows[0], cast(str, args.question), cast(str, args.text))
        except (OSError, ValueError, change_review.ChangeReviewError, Refusal, subprocess.SubprocessError) as error:
            print(f"change page: {error}", file=sys.stderr)
            return 1
        print(f"Answered {page.rows[0]}" if changed else f"{page.rows[0]} already has this answer")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("rows", nargs="*")
    _ = parser.add_argument("--production", type=Path)
    _ = parser.add_argument("--list", type=Path)
    args = parser.parse_args(arguments)
    rows = cast(list[str], args.rows)
    production = cast(Path | None, args.production)
    review_list = cast(Path | None, args.list)
    try:
        if not rows:
            print(publish_browser())
            return 0
        if production is None:
            raise ValueError("pass --production for an older selected-row link")
        production = production.expanduser()
        path = review_list.expanduser() if review_list else change_review.production_list(str(production))
        print(publish(production, path, rows))
    except (OSError, ValueError, change_review.ChangeReviewError, subprocess.SubprocessError, Refusal) as error:
        print(f"change page: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
