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


class ProductionJudgment(TypedDict):
    issue_path: str
    content_hash: str
    question_set_version: str
    model: str
    payloads: dict[str, RequestPayload]
    answers: dict[str, client.Response]
    decided_fields: dict[str, object]


class ScoredIssue(ProductionJudgment, total=False):
    variant_set: str
    variant_definitions: dict[str, questions.Question]
    experiment_id: str
    experiment_answers: dict[str, client.Response]


class BlindItem(TypedDict):
    id: str
    field: str
    title: str
    body: str
    A: str | int
    B: str | int
    pick: str


class BlindSheet(TypedDict):
    run_id: str
    instructions: str
    items: list[BlindItem]


class BlindAssignment(TypedDict):
    id: str
    field: str
    issue_path: str
    jev_option: str
    A: str | int
    B: str | int


class BlindKey(TypedDict):
    seed: int
    run_id: str
    entries: list[BlindAssignment]


@dataclass(frozen=True)
class BlindFiles:
    sheet_path: Path
    key_path: Path


@dataclass(frozen=True)
class PreferenceTally:
    jev: int
    vault: int
    same: int


@dataclass(frozen=True)
class BlindScore:
    result_path: Path
    counts: dict[str, PreferenceTally]
    unanswered: int


class FailedIssue(TypedDict):
    issue_path: str
    error: str


@dataclass(frozen=True)
class ScoreRun:
    result_path: Path
    failure_count: int
