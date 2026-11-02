#!/usr/bin/env python3
"""Publish a change-review page: change_page.py --production DOC [--list L] C27 [C28 ...]."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
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
from typing import Literal, TypedDict, cast
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import change_review as change_review
from scripts.production import next_item, showrunners, showrunner_tree
from scripts.lib import urgency
from scripts.production.production_units import Refusal as Refusal, cell_value, production_field, unit_table
from scripts.production.proposals_folder import owner_docs, slug_of
from scripts.feature_log import feature_log
from scripts.review_links import review_link

MANIFEST = ".review-link-changes.json"
PAGE_NAME = "changes.html"


class PageManifest(TypedDict):
    review_list: str
    production: str
    rows: list[str]
    token: str


class StoredDeliveryIntent(TypedDict):
    attempt: str
    state: Literal["pending", "failed"]


class StoredValidationIntent(StoredDeliveryIntent):
    feedback: str


class StoredDeliverySlots(TypedDict, total=False):
    validation: StoredValidationIntent
    verdict: StoredDeliveryIntent


@dataclass(frozen=True)
class NoDelivery:
    """This delivery kind has no message that needs recovery."""


@dataclass(frozen=True)
class DeliveryIntent:
    """One message attempt, without a second copy of a saved verdict."""

    attempt: str
    state: Literal["pending", "failed"]
    kind: Literal["verdict", "validation"]
    feedback: str = ""


@dataclass(frozen=True)
class DeliverySlots:
    """Keep validation and verdict attempts independent in one record."""

    validation: DeliveryIntent | NoDelivery
    verdict: DeliveryIntent | NoDelivery

    def selected(self, saved: bool) -> DeliveryIntent | NoDelivery:
        return self.verdict if saved else self.validation

    def updated(self, kind: Literal["verdict", "validation"], intent: DeliveryIntent | NoDelivery) -> DeliverySlots:
        return replace(self, verdict=intent) if kind == "verdict" else replace(self, validation=intent)


@dataclass(frozen=True)
class ReviewPage:
    review_list: Path
    production: Path
    rows: tuple[str, ...]
    token: str


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
    recipients: tuple[str, ...]
    message: str
    attempt: str


class DeliveryStatus:
    """Own delivery intents and serialize conditional attempt updates."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root

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
        identity = json.dumps([str(page.review_list.resolve()), row]).encode("utf-8")
        return self.root / (".review-link-delivery-" + hashlib.sha256(identity).hexdigest())

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        try:
            content = self.path(page, row).read_text(encoding="utf-8")
        except FileNotFoundError:
            return DeliverySlots(NoDelivery(), NoDelivery())
        try:
            value = cast(object, json.loads(content))
        except ValueError:
            return DeliverySlots(NoDelivery(), DeliveryIntent("", "failed", "verdict"))
        fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
        if "validation" in fields or "verdict" in fields:
            return DeliverySlots(self.decode_slot(fields.get("validation"), "validation"),
                                 self.decode_slot(fields.get("verdict"), "verdict"))
        # Older records contain one intent or only validation feedback.
        feedback = fields.get("feedback", "")
        if not isinstance(feedback, str):
            feedback = ""
        attempt, state, kind = (fields.get(key) for key in ("attempt", "state", "kind"))
        if isinstance(attempt, str) and state in ("pending", "failed") and kind in ("verdict", "validation"):
            intent = DeliveryIntent(attempt, state, kind, feedback if kind == "validation" else "")
        else:
            intent = DeliveryIntent("", "failed", "validation" if "feedback" in fields else "verdict", feedback)
        return DeliverySlots(NoDelivery(), NoDelivery()).updated(intent.kind, intent)

    def decode_slot(self, value: object, kind: Literal["verdict", "validation"]) -> DeliveryIntent | NoDelivery:
        if not isinstance(value, dict):
            return NoDelivery()
        fields = cast(dict[str, object], value)
        attempt, state = fields.get("attempt"), fields.get("state")
        feedback = fields.get("feedback", "") if kind == "validation" else ""
        if not isinstance(attempt, str) or state not in ("pending", "failed") or not isinstance(feedback, str):
            return NoDelivery()
        return DeliveryIntent(attempt, state, kind, feedback)

    def read(self, page: ReviewPage, row: str) -> DeliveryIntent | NoDelivery:
        saved = any(item["#"] == row and item.get("Verdict") for item in change_review.review_rows(page.review_list))
        return self.slots(page, row).selected(saved)

    def write_slots(self, page: ReviewPage, row: str, slots: DeliverySlots) -> None:
        """Write both slots while the caller holds the delivery lock."""
        fields: StoredDeliverySlots = {}
        if isinstance(slots.validation, DeliveryIntent):
            fields["validation"] = {"attempt": slots.validation.attempt, "state": slots.validation.state,
                                    "feedback": slots.validation.feedback}
        if isinstance(slots.verdict, DeliveryIntent):
            fields["verdict"] = {"attempt": slots.verdict.attempt, "state": slots.verdict.state}
        path = self.path(page, row)
        if fields:
            review_link.replace_file(path, (json.dumps(fields) + "\n").encode("utf-8"))
        else:
            path.unlink(missing_ok=True)

    def failed(self, page: ReviewPage, row: str) -> bool:
        intent = self.read(page, row)
        return isinstance(intent, DeliveryIntent) and intent.state == "failed"

    def begin(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation"], feedback: str = "") -> str:
        attempt = secrets.token_hex()
        intent = DeliveryIntent(attempt, "pending", kind, feedback if kind == "validation" else "")
        with self.locked():
            self.write_slots(page, row, self.slots(page, row).updated(kind, intent))
        return attempt

    def record(self, delivery: MessageDelivery, failed: bool) -> None:
        with self.locked():
            slots = self.slots(delivery.page, delivery.row)
            for current in (slots.validation, slots.verdict):
                if isinstance(current, DeliveryIntent) and current.attempt == delivery.attempt:
                    intent = replace(current, state="failed") if failed else NoDelivery()
                    self.write_slots(delivery.page, delivery.row, slots.updated(current.kind, intent))
                    return


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
    if outcome not in ("works", "not works", "validate", "feedback"):
        outcome = ""
    return AnswerNotice(row, reason, outcome, fields.get("feedback", [""])[0])


def answer_form(page: ReviewPage, notice: AnswerNotice) -> str:
    """Render an answer without reading rows or queue state."""
    row = notice.row if notice.row in page.rows else page.rows[0]
    row_id = html.escape(row)
    buttons: list[str] = []
    for value, label in (("works", "Works"), ("not works", "Not works"),
                         ("validate", "Validate yourself"), ("feedback", "Send feedback")):
        selected = 'aria-pressed="true" ' if notice.outcome == value else ""
        buttons.append(f'<button {selected}name="outcome" value="{value}">{label}</button>')
    return f'''<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<label for="feedback-{row_id}">Other feedback</label>
<input id="feedback-{row_id}" name="feedback" type="text" value="{html.escape(notice.feedback, quote=True)}">
<div>{"".join(buttons)}</div></form>'''


def fallback_answer(page: ReviewPage, notice: AnswerNotice) -> bytes:
    return document(f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>' + answer_form(page, notice))


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


def render(page: ReviewPage, delivery: DeliveryStatus, notice: AnswerNotice = NO_ANSWER_NOTICE) -> bytes:
    sections: list[str] = []
    saved = False
    for row in shown_rows(page):
        row_id = html.escape(row["#"])
        verdict = row.get("Verdict", "")
        saved = saved or bool(verdict)
        intent = delivery.slots(page, row["#"]).selected(bool(verdict))
        failure = False
        pending = False
        failed_feedback = ""
        if isinstance(intent, DeliveryIntent):
            failure = True
            pending = intent.state == "pending"
            failed_feedback = intent.feedback if not verdict else ""
        warning = ("Not saved: " + notice.reason if notice.reason and notice.row == row["#"]
                   else "The message may not have sent" if pending
                   else "Saved, but the message did not send" if verdict and failure
                   else "The validation message did not send" if failure else "")
        alert = f'<p role="alert">{html.escape(warning)}</p>' if warning else ""
        retry_outcome = "retry" if verdict else "validate"
        retry_label = "Send saved answer again" if verdict else "Send validation request again"
        retry = (f'''
<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<input type="hidden" name="feedback" value="{html.escape(failed_feedback, quote=True)}">
<button name="outcome" value="{retry_outcome}">{retry_label}</button>
</form>''' if failure else "")
        form_notice = notice if notice.row == row["#"] else AnswerNotice(row["#"], feedback=failed_feedback)
        answer = (f'<p role="status">Saved: {html.escape(verdict)}</p>' + retry if verdict
                  else answer_form(page, form_notice) + retry)
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        sections.append(f'''<section id="{row_id}">
{alert}
<p class="kind">{html.escape(row[change_review.KIND])}</p>
<div class="id">{row_id} · {html.escape(row.get("Merged", ""))}</div>
<h1>{html.escape(row["Change"])}</h1>
<h2>{proof_title}</h2><pre>{html.escape(row[change_review.CHECK])}</pre>
{answer}</section>''')
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
    if root not in docs:
        raise AnswerRefused(f"root production {root} has no production document")
    owner = row.get(change_review.PRODUCTION) or root
    for doc in owner_docs(docs[root]):
        if slug_of(doc) == owner:
            return doc
    raise AnswerRefused(f"unknown production {owner} for {row['#']}")


def returning_unit(page: ReviewPage, row: dict[str, str]) -> str:
    """Follow the existing verdict workflow: merge -> checkpoint -> Units row."""
    doc = row_production(page, row)
    lines = doc.read_text(encoding="utf-8").splitlines()
    merge = re.search(r"\b[0-9a-f]{7,40}\b", row.get("Merged", ""))
    if merge is None:
        return production_field(lines, "Showrunner session")
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    commits = change_review.merge_commits(repo, merge[0])
    if not commits:
        result = subprocess.run(["git", "-C", str(repo), "show", "-s", "--format=%B", merge[0], "--"],
                                capture_output=True, text=True, check=True)
        message = result.stdout
        commits = [change_review.Commit(merge[0], message.split("\n", 1)[0], message, ())]
    units = unit_table(lines)
    candidates: set[str] = set()
    for commit in commits:
        checkpoint = re.match(r"checkpoint\(([^)]+)\):", commit.subject)
        if checkpoint is None:
            continue
        for unit in units:
            names = (cell_value(unit.get("Branch", "")), cell_value(unit.get("Unit", "")),
                     Path(cell_value(unit.get("Plan", ""))).stem,
                     Path(cell_value(unit.get("Worktree", ""))).name)
            if checkpoint[1] in names:
                name = cell_value(unit["Unit"])
                candidates.add(name)
                if any(isinstance(item, change_review.Visible) and item.change == row["Change"]
                       for item in change_review.entries(commit.message)):
                    return name
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        raise ValueError("cannot identify one owning unit for this change")
    # A change outside a unit returns to the session that owns that production.
    return production_field(lines, "Showrunner session")


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
            send_message(showrunner(page), Path(__file__).stem, message)
        except RuntimeError as delivery_error:
            raise FeatureLogFailed(f"{message}\nNotification failed: {delivery_error}") from delivery_error
        raise FeatureLogFailed(message) from error


def submit(page: ReviewPage, body: str, status: DeliveryStatus) -> MessageDelivery:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(values) != 1 for values in fields.values()):
        raise AnswerRefused("expected only row, token, outcome and feedback")
    row_id, token, outcome, feedback = (fields[key][0] for key in ("row", "token", "outcome", "feedback"))
    if row_id not in page.rows or not secrets.compare_digest(token.encode("utf-8"), page.token.encode("utf-8")):
        raise AnswerRefused("row or page token does not match this page")
    if outcome not in ("works", "not works", "validate", "feedback", "retry") or "\n" in feedback or "\r" in feedback:
        raise AnswerRefused("choose an answer and use one line of feedback")
    if outcome == "feedback" and not feedback.strip():
        raise AnswerRefused("enter feedback")
    verdict = feedback.strip() if feedback.strip() else outcome
    if outcome in ("works", "not works") and feedback.strip():
        verdict = f"{outcome}: {feedback.strip()}"
    row = next(row for row in shown_rows(page) if row["#"] == row_id)
    recipient = showrunner(page)
    if outcome == "validate":
        if row.get("Verdict"):
            raise AnswerRefused("this row already has a saved answer")
        _ = row_production(page, row)
        attempt = status.begin(page, row_id, "validation", feedback)
        _ = next_item.move_to_end(next_item.CHANGE, page.production, key=queue_key(page, row_id))
        message = (f"Change review {row_id}: the user asks you to validate this\n"
                   f"List: {page.review_list}\nProduction: {page.production}\n"
                   f"Change: {row['Change']}\nUser feedback: {feedback}")
        return MessageDelivery(page, row_id, (recipient,), message, attempt)
    if outcome == "retry":
        if not row.get("Verdict") or feedback:
            raise AnswerRefused("retry requires a saved answer and no new feedback")
        verdict = row["Verdict"]
    if row.get("Verdict") and row["Verdict"] != verdict:
        raise AnswerRefused("this row already has a different saved answer")
    doc = row_production(page, row)
    unit = returning_unit(page, row) if verdict != "works" else recipient
    if not row.get("Verdict"):
        closeout_before_save(page, doc, row_id, verdict)
    attempt = status.begin(page, row_id, "verdict")
    result = subprocess.run([sys.executable, str(Path(change_review.__file__)), "verdict",
                             "--production", str(page.production), "--list", str(page.review_list),
                             "--row", row_id, f"--verdict={verdict}"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AnswerRefused((result.stderr or result.stdout).strip())
    message = (f"Change review {row_id}: {verdict}\n"
               f"List: {page.review_list}\nProduction: {page.production}\n"
               f"Change: {row['Change']}\nMerged: {row.get('Merged', '')}\n"
               f"User feedback: {feedback if feedback.strip() else verdict}\n{result.stdout.strip()}")
    return MessageDelivery(page, row_id, tuple(dict.fromkeys((unit, recipient))), message, attempt)


def deliver(delivery: MessageDelivery, status: DeliveryStatus) -> None:
    errors: list[str] = []
    for target in delivery.recipients:
        try:
            send_message(target, Path(__file__).stem, delivery.message)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{target}: {error}")
    if errors:
        print(f"review-links delivery {delivery.row}: " + "; ".join(errors), file=sys.stderr)
    status.record(delivery, bool(errors))


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("rows", nargs="+")
    _ = parser.add_argument("--production", type=Path, required=True)
    _ = parser.add_argument("--list", type=Path)
    args = parser.parse_args(arguments)
    production = cast(Path, args.production).expanduser()
    review_list = cast(Path | None, args.list)
    try:
        path = review_list.expanduser() if review_list else change_review.production_list(str(production))
        print(publish(production, path, cast(list[str], args.rows)))
    except (OSError, ValueError, change_review.ChangeReviewError, subprocess.SubprocessError, Refusal) as error:
        print(f"change page: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
