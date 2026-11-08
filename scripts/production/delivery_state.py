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
    kind: Literal["verdict", "validation", "question"]
    feedback: str = ""
    recipients: tuple[staff.StaffId, ...] = ()


@dataclass(frozen=True)
class ValidationRequested:
    """The showrunner owns validation after a saved request."""

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

    @property
    def validation_requested(self) -> bool:
        return not isinstance(self.validation, NoDelivery)

    def waits_for_user(self, verdict_saved: bool) -> bool:
        return not (verdict_saved or self.validation_requested or isinstance(self.question, OpenQuestion))

    def intents(self) -> tuple[DeliveryIntent, ...]:
        question = self.question.delivery if isinstance(self.question, OpenQuestion) else NoDelivery()
        return tuple(intent for intent in (self.validation, self.verdict, question) if isinstance(intent, DeliveryIntent))

    def updated(self, kind: Literal["verdict", "validation", "question"], intent: DeliveryIntent | NoDelivery) -> DeliverySlots:
        if kind == "question":
            return replace(self, question=replace(self.question, delivery=intent)) if isinstance(self.question, OpenQuestion) else self
        return replace(self, verdict=intent) if kind == "verdict" else replace(self, validation=intent)


def decode_validation(value: object) -> DeliveryIntent | ValidationRequested | NoDelivery:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    attempt, feedback = fields.get("attempt"), fields.get("feedback", "")
    if fields.get("state") in ("sent", "failed") and isinstance(attempt, str) and isinstance(feedback, str):
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
    if not isinstance(attempt, str) or state != "pending" or not isinstance(feedback, str):
        return NoDelivery()
    return DeliveryIntent(attempt, kind, feedback, decode_recipients(fields.get("recipients")))

def decode_recipients(value: object) -> tuple[staff.StaffId, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(staff.StaffId(item) for item in cast(list[object], value) if isinstance(item, str))


def decode(content: str) -> DeliverySlots:
    try:
        value = cast(object, json.loads(content))
    except ValueError:
        return DeliverySlots(NoDelivery(), NoDelivery())
    return decode_value(value)


def decode_value(value: object) -> DeliverySlots:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    if "validation" in fields or "verdict" in fields or "question" in fields:
        return DeliverySlots(decode_validation(fields.get("validation")),
                             decode_slot(fields.get("verdict"), "verdict"),
                             decode_question(fields.get("question")))
    # Older records contain one intent.
    kind = fields.get("kind")
    if kind == "validation":
        return DeliverySlots(decode_validation(fields), NoDelivery())
    if kind == "verdict":
        return DeliverySlots(NoDelivery(), decode_slot(fields, "verdict"))
    feedback = fields.get("feedback")
    if isinstance(feedback, str):
        return DeliverySlots(ValidationRequested("", feedback), NoDelivery())
    return DeliverySlots(NoDelivery(), NoDelivery())
