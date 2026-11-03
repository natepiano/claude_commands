"""Identify the question or report that a kept push concerns."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast


class KeptPushDropped(int):
    """A successful release removed a push without phone delivery."""

    def __new__(cls) -> KeptPushDropped:
        return super().__new__(cls, 0)


class QuestionReference(TypedDict):
    kind: Literal["question"]
    path: str
    since: str


class ReviewWatchReference(TypedDict):
    kind: Literal["review_watch"]


class NoReference(TypedDict):
    kind: Literal["none"]


PushReference = QuestionReference | ReviewWatchReference | NoReference
NO_REFERENCE: NoReference = {"kind": "none"}


def read_reference(value: object) -> PushReference:
    if value is None:
        return NO_REFERENCE
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        if record.get("kind") == "none":
            return NO_REFERENCE
        if record.get("kind") == "review_watch":
            return ReviewWatchReference(kind="review_watch")
        path, since = record.get("path"), record.get("since")
        if record.get("kind") == "question" and isinstance(path, str) and Path(path).is_absolute() and isinstance(since, str) and since:
            return QuestionReference(kind="question", path=path, since=since)
    raise ValueError("invalid push reference")


@dataclass(frozen=True)
class QuestionOpening:
    status: str
    since: str


def read_question(path: Path) -> QuestionOpening:
    """Read the question frontmatter without changing the record."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read question record {path}: {error}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"not a question record: {path}")
    fields: dict[str, str] = {}
    for line in lines[1:lines.index("---", 1)]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if fields.get("record_type") != "question":
        raise ValueError(f"not a question record: {path}")
    registration = cast(object, json.loads(fields.get("registration", "null")))
    since = cast(dict[str, object], registration).get("since") if isinstance(registration, dict) else None
    if not isinstance(since, str) or not since:
        raise ValueError(f"question has no registration since: {path}")
    status = fields.get("status", "")
    if status not in ("open", "closed"):
        raise ValueError(f"invalid question status: {path}")
    return QuestionOpening(status, since)


def admission_reference(text_id: str, question: str = "", forwarded: str = "", *,
                        remote_forward: bool = False) -> PushReference:
    """Read local questions before admission. Preserve missing remote references."""
    reference: PushReference
    if forwarded:
        reference = read_reference(cast(object, json.loads(forwarded)))
    else:
        reference = NO_REFERENCE
    if question and reference["kind"] != "question":
        path = Path(question).expanduser().resolve()
        opening = read_question(path)
        if opening.status != "open":
            raise ValueError(f"question is {opening.status}: {path}")
        reference = QuestionReference(kind="question", path=str(path), since=opening.since)
    elif reference["kind"] == "question":
        path = Path(reference["path"])
        if not (remote_forward and not path.exists()):
            opening = read_question(path)
            if opening.status != "open":
                raise ValueError(f"question closed: {path}")
            if opening.since != reference["since"]:
                raise ValueError(f"question reopened since the push was made: {path}")
    if text_id == "decision.choices" and reference["kind"] != "question":
        raise ValueError("decision.choices requires --question with an open question record")
    if text_id == "review_watch.acknowledge":
        return ReviewWatchReference(kind="review_watch")
    return reference
