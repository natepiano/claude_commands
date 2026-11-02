"""Decode the durable delivery record for the page and all waiting counts."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal, cast

from scripts.production import staff


@dataclass(frozen=True)
class NoDelivery:
    """This delivery kind has no message that needs recovery."""


@dataclass(frozen=True)
class DeliveryIntent:
    """One message attempt, without a second copy of a saved verdict."""

    attempt: str
    state: Literal["pending", "failed"]
    kind: Literal["verdict", "validation", "question"]
    feedback: str = ""
    recipients: tuple[staff.StaffId, ...] = ()
    failure_note: str = ""


@dataclass(frozen=True)
class ValidationRequested:
    """The showrunner owns validation after a delivered request."""

    attempt: str
    feedback: str


@dataclass(frozen=True)
class NoQuestion:
    """The user has not asked a question about this row."""


@dataclass(frozen=True)
class OpenQuestion:
    """The user waits for the showrunner's answer."""

    question_id: str
    text: str
    delivery: DeliveryIntent | NoDelivery

    @property
    def awaiting_answer(self) -> bool:
        """Only a delivered question transfers the row to the showrunner."""
        return isinstance(self.delivery, NoDelivery)


@dataclass(frozen=True)
class AnsweredQuestion:
    """The showrunner's answer returns the row to the user."""

    question_id: str
    text: str
    answer: str


@dataclass(frozen=True)
class DeliverySlots:
    """Keep validation ownership and message attempts in one record."""

    validation: DeliveryIntent | ValidationRequested | NoDelivery
    verdict: DeliveryIntent | NoDelivery
    question: NoQuestion | OpenQuestion | AnsweredQuestion = NoQuestion()

    def selected(self, saved: bool) -> DeliveryIntent | NoDelivery:
        intent = (self.verdict if saved else self.question.delivery
                  if isinstance(self.question, OpenQuestion) else self.validation)
        return intent if isinstance(intent, DeliveryIntent) else NoDelivery()

    def updated(self, kind: Literal["verdict", "validation", "question"], intent: DeliveryIntent | NoDelivery) -> DeliverySlots:
        if kind == "question":
            return replace(self, question=replace(self.question, delivery=intent)) if isinstance(self.question, OpenQuestion) else self
        return replace(self, verdict=intent) if kind == "verdict" else replace(self, validation=intent)


def decode_validation(value: object) -> DeliveryIntent | ValidationRequested | NoDelivery:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    attempt, feedback = fields.get("attempt"), fields.get("feedback", "")
    if fields.get("state") == "sent" and isinstance(attempt, str) and isinstance(feedback, str):
        return ValidationRequested(attempt, feedback)
    return decode_slot(fields, "validation")

def decode_question(value: object) -> NoQuestion | OpenQuestion | AnsweredQuestion:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    text, question_id = fields.get("text"), fields.get("question_id")
    if not isinstance(text, str) or not text.strip() or not isinstance(question_id, str) or not question_id:
        return NoQuestion()
    answer = fields.get("answer")
    if isinstance(answer, str):
        return AnsweredQuestion(question_id, text, answer)
    return OpenQuestion(question_id, text, decode_slot(fields.get("delivery"), "question"))

def decode_slot(value: object, kind: Literal["verdict", "validation", "question"]) -> DeliveryIntent | NoDelivery:
    if not isinstance(value, dict):
        return NoDelivery()
    fields = cast(dict[str, object], value)
    attempt, state = fields.get("attempt"), fields.get("state")
    feedback = fields.get("feedback", "") if kind == "validation" else ""
    if not isinstance(attempt, str) or state not in ("pending", "failed") or not isinstance(feedback, str):
        return NoDelivery()
    note = fields.get("failure_note", "")
    return DeliveryIntent(attempt, state, kind, feedback, decode_recipients(fields.get("recipients")),
                          note if isinstance(note, str) else "")

def decode_recipients(value: object) -> tuple[staff.StaffId, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(staff.StaffId(item) for item in cast(list[object], value) if isinstance(item, str))


def decode(content: str) -> DeliverySlots:
    try:
        value = cast(object, json.loads(content))
    except ValueError:
        return DeliverySlots(NoDelivery(), DeliveryIntent("", "failed", "verdict"))
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    if "validation" in fields or "verdict" in fields or "question" in fields:
        return DeliverySlots(decode_validation(fields.get("validation")),
                             decode_slot(fields.get("verdict"), "verdict"),
                             decode_question(fields.get("question")))
    # Older records contain one intent or only validation feedback.
    feedback = fields.get("feedback", "")
    if not isinstance(feedback, str):
        feedback = ""
    attempt, state, kind = (fields.get(key) for key in ("attempt", "state", "kind"))
    if isinstance(attempt, str) and state in ("pending", "failed") and kind in ("verdict", "validation"):
        intent = DeliveryIntent(attempt, state, kind, feedback if kind == "validation" else "",
                                decode_recipients(fields.get("recipients")))
    else:
        intent = DeliveryIntent("", "failed", "validation" if "feedback" in fields else "verdict", feedback)
    return DeliverySlots(NoDelivery(), NoDelivery()).updated(intent.kind, intent)

