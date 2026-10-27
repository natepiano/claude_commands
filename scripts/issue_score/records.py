"""The persisted run contract shared by scoring and local evaluation."""
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from . import client, questions

STATE_DIR = Path.home() / ".local/state/issue-score"


class RequestPayload(TypedDict):
    model: str
    state: questions.IssueState | questions.AreaState
    questions: dict[str, questions.Question]


class ScoredIssue(TypedDict):
    issue_path: str
    content_hash: str
    question_set_version: str
    model: str
    payloads: dict[str, RequestPayload]
    answers: dict[str, client.Response]
    decided_fields: dict[str, object]


class FailedIssue(TypedDict):
    issue_path: str
    error: str


@dataclass(frozen=True)
class ScoreRun:
    result_path: Path
    failure_count: int
