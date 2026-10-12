#!/usr/bin/env python3
"""Collect model turns and their role histories into an incremental SQLite ledger."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, Literal, cast

# Direct execution from any directory uses this checkout's modules.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.usage.agents_log import Assignment, Lens, Role, RoleLog, assignments, role_at, state_dir
from scripts.whoami.seat_usage import (
    MAC_HOST, KnownThreadOpening, ThreadRolloutBuilder, decode_rollout, is_compaction,
    is_edit, parse_timestamp, token_usage,
)


@dataclass(frozen=True)
class ClaudeSession:
    machine: str
    agent_id: str


@dataclass(frozen=True)
class ClaudeSubagent:
    machine: str
    parent_id: str
    agent_id: str


@dataclass(frozen=True)
class CodexThread:
    machine: str
    agent_id: str


Agent = ClaudeSession | ClaudeSubagent | CodexThread
Family = Literal["claude", "codex"]
LogFamily = Literal["claude", "codex", "history"]
SourceFamily = Literal["claude", "codex", "history", "checkpoints", "readings", "account_roles"]
LOG_FAMILIES: tuple[LogFamily, ...] = ("claude", "codex", "history")
STORE_VERSION = 6
MAX_SQLITE_INTEGER = (1 << 63) - 1


class SourceState(Enum):
    READY = "ready"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ReportedEffort:
    value: str


@dataclass(frozen=True)
class EffortNotReported:
    pass


Effort = ReportedEffort | EffortNotReported


@dataclass(frozen=True)
class ReportedModel:
    value: str


@dataclass(frozen=True)
class ModelNotReported:
    pass


Model = ReportedModel | ModelNotReported


@dataclass(frozen=True)
class KnownReviewLens:
    value: Lens


@dataclass(frozen=True)
class ReviewLensUnrecorded:
    pass


@dataclass(frozen=True)
class LensNotApplicable:
    pass


ReviewLens = KnownReviewLens | ReviewLensUnrecorded | LensNotApplicable


@dataclass(frozen=True)
class RecordedAttribution:
    role: Role
    lens: ReviewLens


@dataclass(frozen=True)
class EstimatedAttribution:
    role: Role
    lens: ReviewLens


@dataclass(frozen=True)
class UnassignedAttribution:
    pass


Attribution = RecordedAttribution | EstimatedAttribution | UnassignedAttribution


@dataclass(frozen=True)
class EstimatedDirectorAssignment:
    agent: ClaudeSession | CodexThread
    at: datetime


@dataclass(frozen=True)
class RawTokens:
    input: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output: int


@dataclass(frozen=True)
class Turn:
    agent: Agent
    key: str
    at: datetime
    model: Model
    effort: Effort
    tokens: RawTokens
    before_first_edit: bool


@dataclass(frozen=True)
class Compaction:
    agent: Agent
    at: datetime


@dataclass(frozen=True)
class Opening:
    agent: Agent
    at: datetime


@dataclass(frozen=True)
class Edit:
    agent: Agent
    key: str
    at: datetime


@dataclass(frozen=True)
class NoMeasurement:
    pass


ThreadMeasurement = Turn | Compaction | Opening | Edit
Measurement = ThreadMeasurement | NoMeasurement


@dataclass(frozen=True)
class EventFieldNotCarried:
    pass


HistoryField = str | int | EventFieldNotCarried
HistoryEventType = Literal["run_started", "run_finished", "phase_started", "phase_finished",
                           "pass_started", "pass_finished", "finding_opened", "finding_verdict",
                           "finding_batch_dispatched", "finding_batch_landed", "finding_batch_abandoned"]


@dataclass(frozen=True)
class RunHistoryEvent:
    machine: str
    run_id: str
    event_id: str
    kind: HistoryEventType
    at: datetime
    fields: dict[str, HistoryField]


# Each entry names a stored field, its event field, and its validation rule.
HISTORY_FIELDS: dict[HistoryEventType, tuple[tuple[str, str, Literal["text", "count", "covered"]], ...]] = {
    "run_started": (("plan_doc", "plan_doc", "text"), ("worktree", "worktree", "text"),
                    ("branch", "branch", "text")),
    "run_finished": (("state", "status", "text"),),
    "phase_started": (("phase_instance_id", "phase_instance_id", "text"),
                      ("phase_id", "phase_id", "text"), ("title", "phase_title", "text")),
    "phase_finished": (("phase_instance_id", "phase_instance_id", "text"), ("state", "status", "text")),
    "pass_started": (("phase_instance_id", "phase_instance_id", "text"),
                     ("pass_instance_id", "pass_instance_id", "text"), ("kind", "pass_kind", "text"),
                     ("task", "called_task", "text"), ("slot", "team_slot", "text"),
                     ("fix_pass", "fix_pass", "count")),
    "pass_finished": (("pass_instance_id", "pass_instance_id", "text"), ("state", "status", "text")),
    "finding_opened": (("phase_instance_id", "phase_instance_id", "text"),
                       ("finding_id", "finding_id", "text"), ("severity", "severity", "text"),
                       ("caught_by", "caught_by", "text"), ("lens", "lens", "text"),
                       ("opened_round", "round", "count")),
    "finding_verdict": (("phase_instance_id", "phase_instance_id", "text"),
                        ("finding_id", "finding_id", "text"), ("state", "resulting_state", "text")),
    "finding_batch_dispatched": (("phase_instance_id", "phase_instance_id", "text"),
                                 ("round", "round", "count"), ("covered", "covered", "covered")),
    "finding_batch_landed": (("phase_instance_id", "phase_instance_id", "text"), ("round", "round", "count")),
    "finding_batch_abandoned": (("phase_instance_id", "phase_instance_id", "text"), ("round", "round", "count")),
}


@dataclass(frozen=True)
class FileCursor:
    offset: int


@dataclass(frozen=True)
class ThreadNotIdentified:
    pass


@dataclass
class CodexContext:
    machine: str
    agent: CodexThread | ThreadNotIdentified = field(default_factory=ThreadNotIdentified)
    model: Model = field(default_factory=ModelNotReported)
    effort: Effort = field(default_factory=EffortNotReported)
    edit_seen: bool = False
    lifecycle: ThreadRolloutBuilder = field(default_factory=ThreadRolloutBuilder)

    def consume(self, line: str, offset: int) -> tuple[ThreadMeasurement, ...]:
        record = decode_rollout(line)
        if record is None:
            return ()
        previous_opening = self.lifecycle.opening
        payload = object_fields(record.get("payload"))
        if record.get("type") == "session_meta":
            # Reuse the opening parser without retaining its usage sample lists.
            self.lifecycle.consume(record)
            thread_id = payload.get("id")
            if isinstance(thread_id, str) and thread_id:
                self.agent = CodexThread(self.machine, thread_id)
        if record.get("type") == "turn_context":
            self.model = reported_model(payload.get("model"))
            self.effort = reported_effort(payload.get("effort"))
        edited = is_edit(record)
        self.edit_seen = self.edit_seen or edited
        at = parse_timestamp(record.get("timestamp"))
        if not isinstance(self.agent, CodexThread):
            return ()
        values: list[ThreadMeasurement] = []
        opening = self.lifecycle.opening
        if isinstance(opening, KnownThreadOpening) and opening != previous_opening:
            values.append(Opening(self.agent, opening.at))
        if at is None:
            return tuple(values)
        if edited:
            values.append(Edit(self.agent, str(offset), at))
        if is_compaction(record):
            values.append(Compaction(self.agent, at))
        usage = token_usage(record)
        if usage is None:
            return tuple(values)
        # Validate the reported input total before its cached portion is removed.
        _ = token_count(usage.uncached_input + usage.cached_input)
        values.append(Turn(self.agent, str(offset), at, self.model, self.effort,
                           RawTokens(token_count(usage.uncached_input), 0, 0,
                                     token_count(usage.cached_input), token_count(usage.output)),
                           not self.edit_seen))
        return tuple(values)


def object_fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def json_fields(line: str | bytes) -> dict[str, object]:
    try:
        value = cast(object, json.loads(line))
        return object_fields(value)
    except (ValueError, UnicodeError, RecursionError):
        return {}


def reported_effort(value: object) -> Effort:
    return ReportedEffort(value) if isinstance(value, str) and value else EffortNotReported()


def reported_model(value: object) -> Model:
    return ReportedModel(value) if isinstance(value, str) and value else ModelNotReported()


def token_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= MAX_SQLITE_INTEGER:
            raise ValueError("token count outside storage range")
        return value
    return 0


def claude_edit(record: dict[str, object]) -> bool:
    content = object_fields(record.get("message")).get("content")
    if record.get("type") != "assistant" or not isinstance(content, list):
        return False
    for value in cast(list[object], content):
        block = object_fields(value)
        if block.get("type") == "tool_use" and block.get("name") in ("Edit", "Write", "NotebookEdit"):
            return True
    return False


def claude_measurement(record: dict[str, object], agent: ClaudeSession | ClaudeSubagent,
                       edit_seen: bool) -> Measurement:
    at = parse_timestamp(record.get("timestamp"))
    if at is None:
        return NoMeasurement()
    if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
        return Compaction(agent, at)
    message = object_fields(record.get("message"))
    usage_value = message.get("usage")
    message_id = message.get("id")
    if (record.get("type") != "assistant" or not isinstance(usage_value, dict)
            or not isinstance(message_id, str) or not message_id):
        return NoMeasurement()
    usage = cast(dict[str, object], usage_value)
    creation = object_fields(usage.get("cache_creation"))
    five_minute = token_count(creation.get("ephemeral_5m_input_tokens"))
    one_hour = token_count(creation.get("ephemeral_1h_input_tokens"))
    five_minute += max(0, token_count(usage.get("cache_creation_input_tokens")) - five_minute - one_hour)
    key = claude_turn_key(record)
    return Turn(agent, key, at, reported_model(message.get("model")),
                reported_effort(record.get("effort")),
                RawTokens(token_count(usage.get("input_tokens")), five_minute, one_hour,
                          token_count(usage.get("cache_read_input_tokens")),
                          token_count(usage.get("output_tokens"))), not edit_seen)


def claude_turn_key(record: dict[str, object]) -> str:
    message_id = object_fields(record.get("message")).get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Claude message id missing")
    return json.dumps((message_id, record.get("requestId")), separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    machine TEXT NOT NULL, family TEXT NOT NULL, kind TEXT NOT NULL,
    parent_id TEXT NOT NULL, agent_id TEXT NOT NULL, turn_key TEXT NOT NULL,
    at TEXT NOT NULL, model TEXT NOT NULL, effort TEXT,
    input INTEGER NOT NULL, cache_write_5m INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL, cache_read INTEGER NOT NULL, output INTEGER NOT NULL,
    before_first_edit INTEGER NOT NULL, role TEXT, lens TEXT,
    attribution TEXT NOT NULL, lens_state TEXT NOT NULL,
    PRIMARY KEY (machine, family, parent_id, agent_id, turn_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS claude_message ON turns (machine, turn_key)
    WHERE family = 'claude';
CREATE TABLE IF NOT EXISTS compactions (
    machine TEXT NOT NULL, family TEXT NOT NULL, parent_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (machine, family, parent_id, agent_id, at)
);
CREATE TABLE IF NOT EXISTS cursors (
    machine TEXT NOT NULL, path TEXT NOT NULL, offset INTEGER NOT NULL,
    PRIMARY KEY (machine, path)
);
CREATE TABLE IF NOT EXISTS role_log_sources (
    machine TEXT PRIMARY KEY, problems INTEGER NOT NULL, state TEXT NOT NULL, read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_sources (
    machine TEXT NOT NULL, family TEXT NOT NULL, state TEXT NOT NULL,
    read_at TEXT NOT NULL, last_complete_at TEXT,
    PRIMARY KEY (machine, family)
);
CREATE TABLE IF NOT EXISTS openings (
    machine TEXT NOT NULL, family TEXT NOT NULL, parent_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (machine, family, parent_id, agent_id)
);
CREATE TABLE IF NOT EXISTS edits (
    machine TEXT NOT NULL, family TEXT NOT NULL, parent_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, edit_key TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (machine, family, parent_id, agent_id, edit_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS claude_edit_message ON edits (machine, edit_key)
    WHERE family = 'claude';
CREATE TABLE IF NOT EXISTS runs (
    machine TEXT NOT NULL, run_id TEXT NOT NULL, started_at TEXT NOT NULL,
    plan_doc TEXT, worktree TEXT, branch TEXT, state TEXT, finished_at TEXT, source_event TEXT NOT NULL,
    PRIMARY KEY (machine, run_id)
);
CREATE TABLE IF NOT EXISTS phases (
    machine TEXT NOT NULL, run_id TEXT NOT NULL, phase_instance_id TEXT NOT NULL,
    phase_id TEXT, title TEXT, started_at TEXT NOT NULL, state TEXT, finished_at TEXT,
    source_event TEXT NOT NULL, PRIMARY KEY (machine, phase_instance_id)
);
CREATE TABLE IF NOT EXISTS passes (
    machine TEXT NOT NULL, run_id TEXT NOT NULL, phase_instance_id TEXT, pass_instance_id TEXT NOT NULL,
    kind TEXT, task TEXT, slot TEXT, fix_pass INTEGER, started_at TEXT NOT NULL,
    state TEXT, finished_at TEXT, source_event TEXT NOT NULL, PRIMARY KEY (machine, pass_instance_id)
);
CREATE TABLE IF NOT EXISTS findings (
    machine TEXT NOT NULL, run_id TEXT NOT NULL, phase_instance_id TEXT NOT NULL, finding_id TEXT NOT NULL,
    severity TEXT, caught_by TEXT, lens TEXT, opened_round INTEGER, opened_at TEXT NOT NULL,
    state TEXT, state_at TEXT NOT NULL, source_event TEXT NOT NULL,
    PRIMARY KEY (machine, phase_instance_id, finding_id)
);
CREATE TABLE IF NOT EXISTS repair_rounds (
    machine TEXT NOT NULL, run_id TEXT NOT NULL, phase_instance_id TEXT NOT NULL, round INTEGER NOT NULL,
    dispatched_at TEXT NOT NULL, covered INTEGER, state TEXT NOT NULL, state_at TEXT NOT NULL,
    source_event TEXT NOT NULL, PRIMARY KEY (machine, phase_instance_id, round)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    commit_id TEXT PRIMARY KEY, at TEXT NOT NULL, plan_slug TEXT, phase_id TEXT,
    mapping TEXT NOT NULL, machine TEXT, run_id TEXT, phase_instance_id TEXT
);
CREATE TABLE IF NOT EXISTS account_readings (
    machine TEXT NOT NULL, account TEXT NOT NULL, at TEXT NOT NULL,
    remaining_state TEXT NOT NULL, remaining REAL, credits_state TEXT NOT NULL, credits REAL,
    PRIMARY KEY (machine, account, at)
);
CREATE TABLE IF NOT EXISTS account_role_changes (
    machine TEXT NOT NULL, at TEXT NOT NULL, role TEXT NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY (machine, at, role, label)
);
"""


def migrate_store(db: sqlite3.Connection) -> None:
    version = cast(int, db.execute("PRAGMA user_version").fetchone()[0])
    if version < STORE_VERSION:
        with db:
            _ = db.execute("DELETE FROM cursors")
            _ = db.execute(f"PRAGMA user_version = {STORE_VERSION}")


def identity_columns(agent: Agent) -> tuple[str, str, str, str, str]:
    if isinstance(agent, ClaudeSubagent):
        return agent.machine, "claude", "subagent", agent.parent_id, agent.agent_id
    if isinstance(agent, ClaudeSession):
        return agent.machine, "claude", "session", "", agent.agent_id
    return agent.machine, "codex", "thread", "", agent.agent_id


def stored_identity(row: sqlite3.Row) -> Agent:
    machine = cast(str, row["machine"])
    agent_id = cast(str, row["agent_id"])
    if row["family"] == "codex":
        return CodexThread(machine, agent_id)
    if row["kind"] == "subagent":
        return ClaudeSubagent(machine, cast(str, row["parent_id"]), agent_id)
    return ClaudeSession(machine, agent_id)


def attribution_columns(value: Attribution) -> tuple[str | None, str | None, str, str]:
    if isinstance(value, UnassignedAttribution):
        return None, None, "unassigned", "not_applicable"
    state = "recorded" if isinstance(value, RecordedAttribution) else "estimated"
    if isinstance(value.lens, KnownReviewLens):
        return value.role, value.lens.value, state, "known"
    lens_state = "unrecorded" if isinstance(value.lens, ReviewLensUnrecorded) else "not_applicable"
    return value.role, None, state, lens_state


@dataclass
class RoleResolver:
    log: RoleLog
    sessions: set[str]
    directors: dict[Agent, tuple[Assignment, ...]] = field(default_factory=dict)
    exact_logs: dict[str, RoleLog] = field(default_factory=dict)
    worker_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        candidates: dict[str, set[str]] = {}
        for agent_id in self.sessions:
            candidates.setdefault(agent_id[:8], set()).add(agent_id)
        for prefix, history in self.log.assignments.items():
            if (re.fullmatch(r"[0-9a-fA-F]{8}", prefix)
                    and any(entry.role == "worker" for entry in history)):
                matches = candidates.get(prefix, set())
                if len(matches) == 1:
                    self.worker_ids.update(matches)

    def resolve(self, agent: Agent, at: datetime) -> Attribution:
        if isinstance(agent, ClaudeSession) and agent.agent_id in self.worker_ids:
            log = self.log
        else:
            if agent.agent_id not in self.exact_logs:
                self.exact_logs[agent.agent_id] = RoleLog(
                    {agent.agent_id: self.log.assignments.get(agent.agent_id, ())}, ())
            log = self.exact_logs[agent.agent_id]
        assignment = role_at(log, agent.agent_id, at)
        # A run event names its director explicitly, independently of cwd.
        evidence = [entry for entry in self.directors.get(agent, ()) if entry.at <= at]
        if evidence:
            director = max(evidence, key=lambda entry: entry.at)
            if (not isinstance(assignment, Assignment)
                    or assignment.role == "interactive" and director.at >= assignment.at):
                assignment = director
        if not isinstance(assignment, Assignment):
            return UnassignedAttribution()
        lens: ReviewLens = LensNotApplicable()
        if assignment.role == "review":
            lens = (KnownReviewLens(assignment.lens) if assignment.lens is not None
                    else ReviewLensUnrecorded())
        return (EstimatedAttribution(assignment.role, lens) if assignment.approximate
                else RecordedAttribution(assignment.role, lens))


def save_measurement(db: sqlite3.Connection, value: Measurement, resolver: RoleResolver) -> int:
    if isinstance(value, NoMeasurement):
        return 0
    machine, family, kind, parent_id, agent_id = identity_columns(value.agent)
    if isinstance(value, Opening):
        # Codex metadata can revise its opening; Claude keeps its first timestamp.
        _ = db.execute("INSERT INTO openings VALUES (?, ?, ?, ?, ?) " +
                       "ON CONFLICT(machine, family, parent_id, agent_id) DO UPDATE SET at = excluded.at " +
                       "WHERE openings.family = 'codex' AND openings.at != excluded.at",
                       (machine, family, parent_id, agent_id, value.at.isoformat()))
        return 0
    if isinstance(value, Edit):
        _ = db.execute("INSERT OR IGNORE INTO edits VALUES (?, ?, ?, ?, ?, ?)",
                       (machine, family, parent_id, agent_id, value.key, value.at.isoformat()))
        return 0
    if isinstance(value, Compaction):
        _ = db.execute("INSERT OR IGNORE INTO compactions VALUES (?, ?, ?, ?, ?)",
                       (machine, family, parent_id, agent_id, value.at.isoformat()))
        return 0
    tokens = value.tokens
    result = db.execute("INSERT OR IGNORE INTO turns VALUES (" + ",".join("?" for _ in range(19)) + ")",
                        (machine, family, kind, parent_id, agent_id, value.key, value.at.isoformat(),
                         value.model.value if isinstance(value.model, ReportedModel) else "unreported",
                         value.effort.value if isinstance(value.effort, ReportedEffort) else None,
                         tokens.input, tokens.cache_write_5m, tokens.cache_write_1h,
                         tokens.cache_read, tokens.output, int(value.before_first_edit),
                         *attribution_columns(resolver.resolve(value.agent, value.at))))
    if result.rowcount == 0 and family == "claude":
        _ = db.execute("UPDATE turns SET input = MAX(input, ?), " +
                       "cache_write_5m = MAX(cache_write_5m, ?), " +
                       "cache_write_1h = MAX(cache_write_1h, ?), " +
                       "cache_read = MAX(cache_read, ?), output = MAX(output, ?), " +
                       "before_first_edit = MIN(before_first_edit, ?) " +
                       "WHERE machine = ? AND family = 'claude' AND turn_key = ?",
                       (tokens.input, tokens.cache_write_5m, tokens.cache_write_1h,
                        tokens.cache_read, tokens.output, int(value.before_first_edit),
                        machine, value.key))
    return result.rowcount


def reattribute(db: sqlite3.Connection, machine: str, resolver: RoleResolver) -> None:
    rows = db.execute("SELECT machine, family, kind, parent_id, agent_id, turn_key, at, " +
                      "role, lens, attribution, lens_state FROM turns WHERE machine = ?", (machine,))
    for row in cast(Iterable[sqlite3.Row], rows):
        at = parse_timestamp(cast(str, row["at"]))
        if at is None:
            continue
        columns = attribution_columns(resolver.resolve(stored_identity(row), at))
        if columns != (row["role"], row["lens"], row["attribution"], row["lens_state"]):
            _ = db.execute("UPDATE turns SET role = ?, lens = ?, attribution = ?, lens_state = ? " +
                           "WHERE machine = ? AND family = ? AND parent_id = ? AND agent_id = ? AND turn_key = ?",
                           (*columns, row["machine"], row["family"], row["parent_id"],
                            row["agent_id"], row["turn_key"]))


def cursor_at(db: sqlite3.Connection, machine: str, path: str) -> int:
    value = cast(object, db.execute("SELECT offset FROM cursors WHERE machine = ? AND path = ?",
                                    (machine, path)).fetchone())
    return cast(int, cast(sqlite3.Row, value)[0]) if value is not None else 0


def complete_lines(source: BinaryIO) -> Iterator[tuple[int, bytes]]:
    while True:
        offset = source.tell()
        line = source.readline()
        if not line or not line.endswith(b"\n"):
            return
        yield offset, line


def claude_identity(machine: str, path: Path) -> ClaudeSession | ClaudeSubagent:
    if path.parent.name == "subagents":
        return ClaudeSubagent(machine, path.parent.parent.name, path.stem.removeprefix("agent-"))
    return ClaudeSession(machine, path.stem)


def read_file(machine: str, path: Path, family: Family,
              previous: int) -> Iterator[ThreadMeasurement | FileCursor]:
    """Replay context locally or on the Mac, yielding only new measurements."""
    size = path.stat().st_size
    if previous == size:
        return
    if previous > size:
        previous = 0
    context = CodexContext(machine)
    agent = claude_identity(machine, path)
    edit_seen = False
    opening_seen = False
    completed = 0
    with path.open("rb") as source:
        # Changed files replay their prefix to recover context and the first edit.
        # Unchanged files never enter this loop; cursors store only durable offsets.
        for offset, line in complete_lines(source):
            values: list[ThreadMeasurement] = []
            try:
                if family == "claude":
                    record = json_fields(line)
                    edited = claude_edit(record)
                    edit_seen = edit_seen or edited
                    at = parse_timestamp(record.get("timestamp"))
                    if at is not None:
                        if not opening_seen:
                            values.append(Opening(agent, at))
                            opening_seen = True
                        if edited:
                            values.append(Edit(agent, claude_turn_key(record), at))
                    value = (claude_measurement(record, agent, edit_seen)
                             if offset >= previous else NoMeasurement())
                    if not isinstance(value, NoMeasurement):
                        values.append(value)
                else:
                    values.extend(context.consume(line.decode("utf-8"), offset))
            except (ValueError, UnicodeError, RecursionError):
                pass
            if offset >= previous:
                yield from values
            completed = offset + len(line)
    yield FileCursor(completed)


def save_cursor(db: sqlite3.Connection, machine: str, path: str, cursor: FileCursor) -> None:
    _ = db.execute("INSERT INTO cursors VALUES (?, ?, ?) " +
                   "ON CONFLICT(machine, path) DO UPDATE SET offset = excluded.offset",
                   (machine, path, cursor.offset))


def collect_file(db: sqlite3.Connection, machine: str, path: str, local_path: Path,
                 family: Family, resolver: RoleResolver) -> int:
    added = 0
    with db:
        for value in read_file(machine, local_path, family, cursor_at(db, machine, path)):
            if isinstance(value, FileCursor):
                save_cursor(db, machine, path, value)
            else:
                added += save_measurement(db, value, resolver)
    return added


def known_sessions(db: sqlite3.Connection, machine: str, paths: Iterable[Path]) -> set[str]:
    sessions = {path.stem for path in paths if path.parent.name != "subagents"}
    rows = db.execute("SELECT DISTINCT agent_id FROM turns " +
                      "WHERE machine = ? AND family = 'claude' AND kind = 'session'", (machine,))
    sessions.update(cast(str, row[0]) for row in cast(Iterable[sqlite3.Row], rows))
    return sessions


def history_root() -> Path:
    configured = os.environ.get("PLAN_DELEGATE_HISTORY_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local/state/plan-delegate"


def validate_history_fields(kind: HistoryEventType, fields: dict[str, HistoryField]) -> None:
    required = [name for name in ("phase_instance_id", "pass_instance_id", "finding_id", "round")
                if name in fields]
    if kind == "pass_started":
        required.remove("phase_instance_id")
    for name in required:
        value = fields[name]
        if isinstance(value, EventFieldNotCarried) or value == "":
            raise ValueError(f"history {name} missing")
    if kind.endswith("finished"):
        state = fields["state"]
        if state == "open":
            raise ValueError("finished history event has open status")


@dataclass(frozen=True)
class HistoryEventNotKept:
    pass


def history_event(record: dict[str, object], machine: str, run_id: str) -> RunHistoryEvent | HistoryEventNotKept:
    kind_value = record.get("event_type")
    event_id = record.get("event_id")
    at = parse_timestamp(record.get("timestamp"))
    if (not isinstance(kind_value, str) or kind_value not in HISTORY_FIELDS
            or not isinstance(event_id, str) or not event_id or at is None):
        return HistoryEventNotKept()
    kind = kind_value
    fields: dict[str, HistoryField] = {}
    try:
        for name, source, rule in HISTORY_FIELDS[kind]:
            value = record.get(source)
            if value is None:
                fields[name] = EventFieldNotCarried()
            elif rule == "text" and isinstance(value, str):
                fields[name] = value
            elif rule == "count" and isinstance(value, int) and not isinstance(value, bool):
                fields[name] = token_count(value)
            elif rule == "covered" and isinstance(value, list):
                covered = cast(list[object], value)
                if not all(isinstance(item, str) for item in covered):
                    raise ValueError("invalid covered finding ids")
                fields[name] = token_count(len(covered))
            else:
                raise ValueError(f"invalid history {source}")
        validate_history_fields(kind, fields)
    except ValueError:
        return HistoryEventNotKept()
    return RunHistoryEvent(machine, run_id, event_id, kind, at, fields)


def read_history_file(machine: str, path: Path, previous: int) -> Iterator[RunHistoryEvent | FileCursor]:
    size = path.stat().st_size
    if size == previous:
        return
    if size < previous:
        previous = 0
    completed = 0
    with path.open("rb") as source:
        for offset, line in complete_lines(source):
            if offset >= previous:
                event = history_event(json_fields(line), machine, path.stem)
                if isinstance(event, RunHistoryEvent):
                    yield event
            completed = offset + len(line)
    yield FileCursor(completed)


def history_column(event: RunHistoryEvent, name: str) -> str | int | None:
    value = event.fields[name]
    return None if isinstance(value, EventFieldNotCarried) else value


def save_history_event(db: sqlite3.Connection, event: RunHistoryEvent) -> int:
    kind = event.kind
    machine, run_id, at = event.machine, event.run_id, event.at.isoformat()
    fields = event.fields
    values = {name: history_column(event, name) for name in fields}
    if kind == "run_started":
        row = db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
                         (machine, run_id, at, values["plan_doc"], values["worktree"], values["branch"], event.event_id))
    elif kind == "phase_started":
        row = db.execute("INSERT OR IGNORE INTO phases VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
                         (machine, run_id, values["phase_instance_id"], values["phase_id"], values["title"], at, event.event_id))
    elif kind == "pass_started":
        row = db.execute("INSERT OR IGNORE INTO passes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
                         (machine, run_id, values["phase_instance_id"], values["pass_instance_id"], values["kind"],
                          values["task"], values["slot"], values["fix_pass"], at, event.event_id))
    elif kind == "finding_opened":
        row = db.execute("INSERT OR IGNORE INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                         (machine, run_id, values["phase_instance_id"], values["finding_id"], values["severity"],
                          values["caught_by"], values["lens"], values["opened_round"], at, at, event.event_id))
    elif kind == "finding_batch_dispatched":
        row = db.execute("INSERT OR IGNORE INTO repair_rounds VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?, ?)",
                         (machine, run_id, values["phase_instance_id"], values["round"], at, values["covered"], at, event.event_id))
    else:
        if kind in ("run_finished", "phase_finished", "pass_finished"):
            table, key = {"run_finished": ("runs", "run_id"), "phase_finished": ("phases", "phase_instance_id"),
                          "pass_finished": ("passes", "pass_instance_id")}[kind]
            identity = run_id if key == "run_id" else values[key]
            _ = db.execute(f"UPDATE {table} SET state = ?, finished_at = ? WHERE machine = ? AND {key} = ?",
                           (values["state"], at, machine, identity))
        elif kind == "finding_verdict":
            _ = db.execute("UPDATE findings SET state = ?, state_at = ? " +
                           "WHERE machine = ? AND phase_instance_id = ? AND finding_id = ?",
                           (values["state"], at, machine, values["phase_instance_id"], values["finding_id"]))
        else:
            state = "landed" if kind == "finding_batch_landed" else "abandoned"
            _ = db.execute("UPDATE repair_rounds SET state = ?, state_at = ? " +
                           "WHERE machine = ? AND phase_instance_id = ? AND round = ?",
                           (state, at, machine, values["phase_instance_id"], values["round"]))
        return 0
    return row.rowcount


def collect_history_file(db: sqlite3.Connection, machine: str, path: Path) -> int:
    added = 0
    with db:
        for value in read_history_file(machine, path, cursor_at(db, machine, str(path))):
            if isinstance(value, FileCursor):
                save_cursor(db, machine, str(path), value)
            else:
                added += save_history_event(db, value)
    return added


def director_evidence(lines: Iterable[bytes], machine: str,
                      evidence: dict[Agent, tuple[Assignment, ...]]) -> None:
    seen: set[Agent] = set()
    for line in lines:
        record = json_fields(line)
        main = object_fields(record.get("main_agent"))
        agent_id = main.get("session_id")
        family = main.get("family")
        if not isinstance(agent_id, str) or not agent_id or family not in ("claude", "codex"):
            continue
        at = parse_timestamp(record.get("timestamp"))
        if at is None:
            continue
        agent: Agent = ClaudeSession(machine, agent_id) if family == "claude" else CodexThread(machine, agent_id)
        if agent in seen:
            continue
        seen.add(agent)
        assignment = Assignment(at, "director", None, True)
        if assignment not in evidence.get(agent, ()):
            evidence[agent] = (*evidence.get(agent, ()), assignment)


def local_directors(machine: str) -> dict[Agent, tuple[Assignment, ...]]:
    evidence: dict[Agent, tuple[Assignment, ...]] = {}
    for path in sorted((history_root() / "runs").glob("*.jsonl")):
        try:
            with path.open("rb") as source:
                director_evidence(source, machine, evidence)
        except OSError:
            continue
    return evidence


def role_source(path: Path) -> tuple[RoleLog, SourceState]:
    try:
        if not path.exists():
            return assignments(path), SourceState.MISSING
        return assignments(path), SourceState.READY
    except OSError:
        return RoleLog({}, ()), SourceState.UNREADABLE


def save_role_source(db: sqlite3.Connection, machine: str, log: RoleLog, state: SourceState) -> None:
    _ = db.execute("INSERT INTO role_log_sources VALUES (?, ?, ?, ?) " +
                   "ON CONFLICT(machine) DO UPDATE SET problems = excluded.problems, " +
                   "state = excluded.state, read_at = excluded.read_at",
                   (machine, len(log.problems), state.value, datetime.now(timezone.utc).isoformat()))


def save_turn_source(db: sqlite3.Connection, machine: str, family: SourceFamily, state: SourceState) -> None:
    at = datetime.now(timezone.utc).isoformat()
    _ = db.execute("INSERT INTO turn_sources VALUES (?, ?, ?, ?, ?) " +
                   "ON CONFLICT(machine, family) DO UPDATE SET state = excluded.state, " +
                   "read_at = excluded.read_at, last_complete_at = CASE " +
                   "WHEN excluded.state = 'ready' THEN excluded.last_complete_at " +
                   "ELSE turn_sources.last_complete_at END",
                   (machine, family, state.value, at, at if state is SourceState.READY else None))


def source_line(machine: str, source: str, added: int, state: SourceState, detail: str = "") -> None:
    unit = "turns" if source in ("claude", "codex", "agents") else "outcomes"
    print(f"{machine} {source}: {added} {unit} added; {state.value}{detail}")


def source_paths(root: Path, family: LogFamily) -> tuple[tuple[Path, ...], SourceState]:
    try:
        with os.scandir(root) as entries:
            root_entries = tuple(entries)
    except FileNotFoundError:
        return (), SourceState.MISSING
    except OSError:
        return (), SourceState.UNREADABLE
    patterns = {"claude": (("*", "*.jsonl"), ("*", "*", "subagents", "agent-*.jsonl")),
                "codex": (("*", "*", "*", "*.jsonl"),), "history": (("*.jsonl",),)}[family]
    paths: set[Path] = set()
    state = SourceState.READY

    def find(entries: Iterable[os.DirEntry[str]], remaining: tuple[tuple[str, ...], ...]) -> None:
        nonlocal state
        for entry in entries:
            matched = tuple(parts for parts in remaining if fnmatchcase(entry.name, parts[0]))
            if any(len(parts) == 1 for parts in matched):
                paths.add(Path(entry.path))
            children = tuple(parts[1:] for parts in matched if len(parts) > 1)
            if not children:
                continue
            try:
                if entry.is_dir():
                    with os.scandir(entry.path) as nested:
                        find(tuple(nested), children)
            except OSError:
                state = SourceState.PARTIAL

    find(root_entries, patterns)
    return tuple(sorted(paths, key=lambda path: (path.parent.name == "subagents", path))), state


def collect_local(db: sqlite3.Connection, args: CommandLine) -> None:
    claude, claude_state = source_paths(args.claude_root, "claude")
    codex, codex_state = source_paths(args.codex_root, "codex")
    history, history_state = source_paths(history_root() / "runs", "history")
    log, log_state = role_source(args.agents_log)
    resolver = RoleResolver(log, known_sessions(db, args.machine, claude), local_directors(args.machine))
    with db:
        if log_state is not SourceState.UNREADABLE:
            reattribute(db, args.machine, resolver)
        save_role_source(db, args.machine, log, log_state)
    sources: tuple[tuple[LogFamily, tuple[Path, ...], SourceState], ...] = (
        ("claude", claude, claude_state), ("codex", codex, codex_state), ("history", history, history_state))
    for family, paths, state in sources:
        added = 0
        for path in paths:
            try:
                added += (collect_history_file(db, args.machine, path) if family == "history"
                          else collect_file(db, args.machine, str(path), path, family, resolver))
            except OSError:
                state = SourceState.PARTIAL
        with db:
            save_turn_source(db, args.machine, family, state)
        source_line(args.machine, family, added, state)
    source_line(args.machine, "agents", 0, log_state, f"; {len(log.problems)} problems")


# Only the bootstrap is a separate program; all reading uses this module.
REMOTE_BOOTSTRAP = """
import json, sys, types
request = json.load(sys.stdin)
for name in ('scripts', 'scripts.usage', 'scripts.whoami'):
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
for name, source in request['modules'].items():
    module = types.ModuleType(name)
    module.__file__ = '/' + name.replace('.', '/') + '.py'
    module.__package__ = name.rpartition('.')[0]
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
sys.modules['scripts.usage.collect'].read_mac(request['cursors'])
"""


def emit_remote(record: dict[str, object]) -> None:
    print(json.dumps(record, separators=(",", ":")), flush=True)


def send_role_log(path: Path) -> None:
    emit_remote({"type": "file", "path": str(path), "kind": "agents"})
    try:
        with path.open("rb") as source:
            while data := source.read(262144):
                emit_remote({"type": "chunk", "data": base64.b64encode(data).decode("ascii")})
        state = SourceState.READY
    except FileNotFoundError:
        state = SourceState.MISSING
    except OSError:
        state = SourceState.UNREADABLE
    emit_remote({"type": "end", "state": state.value})


def agent_record(agent: Agent) -> dict[str, object]:
    identity: dict[str, object] = {"machine": agent.machine, "id": agent.agent_id}
    if isinstance(agent, ClaudeSubagent):
        identity.update(kind="subagent", parent_id=agent.parent_id)
    else:
        identity["kind"] = "session" if isinstance(agent, ClaudeSession) else "thread"
    return identity


def measurement_record(value: ThreadMeasurement) -> dict[str, object]:
    kind = ("turn" if isinstance(value, Turn) else "compaction" if isinstance(value, Compaction)
            else "opening" if isinstance(value, Opening) else "edit")
    record: dict[str, object] = {
        "type": "measurement", "kind": kind,
        "agent": agent_record(value.agent), "at": value.at.isoformat(),
    }
    if isinstance(value, Edit):
        record["key"] = token_count(int(value.key)) if isinstance(value.agent, CodexThread) else value.key
    if isinstance(value, Turn):
        tokens = value.tokens
        record.update(key=value.key,
                      model=value.model.value if isinstance(value.model, ReportedModel) else None,
                      effort=value.effort.value if isinstance(value.effort, ReportedEffort) else None,
                      tokens={"input": tokens.input, "cache_write_5m": tokens.cache_write_5m,
                              "cache_write_1h": tokens.cache_write_1h, "cache_read": tokens.cache_read,
                              "output": tokens.output}, before_first_edit=value.before_first_edit)
    return record


def history_record(event: RunHistoryEvent) -> dict[str, object]:
    return {"type": "outcome", "kind": event.kind, "machine": event.machine, "run_id": event.run_id,
            "event_id": event.event_id, "at": event.at.isoformat(),
            "fields": {name: history_column(event, name) for name in event.fields}}


def read_mac(offsets: dict[str, int]) -> None:
    """Run from the in-memory modules on the Mac; never open a database there."""
    home = Path.home()
    claude, claude_state = source_paths(home / ".claude/projects", "claude")
    codex, codex_state = source_paths(home / ".codex/sessions", "codex")
    history, history_state = source_paths(history_root() / "runs", "history")
    emit_remote({"type": "manifest", "claude": [str(path) for path in claude],
                 "claude_state": claude_state.value, "codex_state": codex_state.value,
                 "history_state": history_state.value})
    send_role_log(state_dir() / "agents.jsonl")
    for agent, assignments_for_agent in local_directors("mac").items():
        for assignment in assignments_for_agent:
            emit_remote({"type": "director", "agent": agent_record(agent),
                         "at": assignment.at.isoformat(), "role": assignment.role,
                         "approximate": assignment.approximate})
    emit_remote({"type": "attribution_ready"})
    sources: tuple[tuple[LogFamily, tuple[Path, ...]], ...] = (
        ("claude", claude), ("codex", codex), ("history", history))
    for family, paths in sources:
        for path in paths:
            name = str(path)
            try:
                previous = offsets.get(name, 0)
                if path.stat().st_size == previous:
                    continue
                emit_remote({"type": "file", "kind": family, "path": name})
                values = (read_history_file("mac", path, previous) if family == "history"
                          else read_file("mac", path, family, previous))
                for value in values:
                    record: dict[str, object] = ({"type": "cursor", "offset": value.offset}
                                                 if isinstance(value, FileCursor) else history_record(value)
                                                 if isinstance(value, RunHistoryEvent) else measurement_record(value))
                    record.update(path=name, family=family)
                    emit_remote(record)
            except OSError:
                emit_remote({"type": "problem", "family": family, "path": name})


class RemoteReadFailed(Exception):
    """The remote stream is incomplete or contains a record we cannot read."""


class RemoteFileUnreadable(Exception):
    """Discard this file's measurements, then continue reading other files."""


def remote_string(record: dict[str, object], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not value:
        raise RemoteReadFailed(f"remote {name} missing or invalid")
    return value


def remote_integer(record: dict[str, object], name: str) -> int:
    value = record.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SQLITE_INTEGER:
        raise RemoteReadFailed(f"remote {name} missing or invalid")
    return value


def remote_object(record: dict[str, object], name: str) -> dict[str, object]:
    value = record.get(name)
    if not isinstance(value, dict):
        raise RemoteReadFailed(f"remote {name} missing or invalid")
    return cast(dict[str, object], value)


def remote_state(record: dict[str, object], name: str) -> SourceState:
    try:
        return SourceState(remote_string(record, name))
    except ValueError as error:
        raise RemoteReadFailed(f"invalid remote {name}") from error


def remote_agent(identity: dict[str, object], machine: str) -> Agent:
    if remote_string(identity, "machine") != machine:
        raise RemoteReadFailed("remote agent names another machine")
    agent_id = remote_string(identity, "id")
    kind = remote_string(identity, "kind")
    if kind == "session":
        return ClaudeSession(machine, agent_id)
    if kind == "subagent":
        return ClaudeSubagent(machine, remote_string(identity, "parent_id"), agent_id)
    if kind == "thread":
        return CodexThread(machine, agent_id)
    raise RemoteReadFailed("invalid remote agent kind")


def remote_director(record: dict[str, object], machine: str) -> EstimatedDirectorAssignment:
    agent = remote_agent(remote_object(record, "agent"), machine)
    if isinstance(agent, ClaudeSubagent):
        raise RemoteReadFailed("invalid remote director identity")
    at = parse_timestamp(remote_string(record, "at"))
    if at is None:
        raise RemoteReadFailed("invalid remote director time")
    if remote_string(record, "role") != "director" or record.get("approximate") is not True:
        raise RemoteReadFailed("remote assignment is not an estimated director")
    return EstimatedDirectorAssignment(agent, at)


def remote_history_event(record: dict[str, object], machine: str, run_id: str) -> RunHistoryEvent:
    if remote_string(record, "machine") != machine or remote_string(record, "run_id") != run_id:
        raise RemoteReadFailed("remote outcome names another run")
    kind_value = remote_string(record, "kind")
    if kind_value not in HISTORY_FIELDS:
        raise RemoteReadFailed("unknown remote outcome kind")
    kind = kind_value
    event_id = remote_string(record, "event_id")
    at = parse_timestamp(remote_string(record, "at"))
    if at is None:
        raise RemoteReadFailed("invalid remote outcome time")
    raw = remote_object(record, "fields")
    fields: dict[str, HistoryField] = {}
    for name, _, rule in HISTORY_FIELDS[kind]:
        if name not in raw:
            raise RemoteReadFailed(f"remote outcome {name} missing")
        if raw[name] is None:
            fields[name] = EventFieldNotCarried()
        elif rule == "text":
            # An explicitly reported empty string remains distinct from null.
            if not isinstance(raw[name], str):
                raise RemoteReadFailed(f"invalid remote outcome {name}")
            fields[name] = cast(str, raw[name])
        else:
            fields[name] = remote_integer(raw, name)
    try:
        validate_history_fields(kind, fields)
    except ValueError as error:
        raise RemoteReadFailed(str(error)) from error
    return RunHistoryEvent(machine, run_id, event_id, kind, at, fields)


def remote_measurement(record: dict[str, object], machine: str, family: Family) -> ThreadMeasurement:
    agent = remote_agent(remote_object(record, "agent"), machine)
    if (family == "claude" and isinstance(agent, CodexThread)
            or family == "codex" and not isinstance(agent, CodexThread)):
        raise RemoteReadFailed("invalid remote agent kind")
    at = parse_timestamp(remote_string(record, "at"))
    if at is None:
        raise RemoteReadFailed("invalid remote measurement time")
    measurement_kind = remote_string(record, "kind")
    if measurement_kind == "compaction":
        return Compaction(agent, at)
    if measurement_kind == "opening":
        return Opening(agent, at)
    if measurement_kind == "edit":
        key = str(remote_integer(record, "key")) if isinstance(agent, CodexThread) else remote_string(record, "key")
        return Edit(agent, key, at)
    if measurement_kind != "turn":
        raise RemoteReadFailed("unknown remote measurement kind")
    tokens = remote_object(record, "tokens")
    # Explicit null denotes unreported; a missing field is a failed read.
    if "model" not in record or "effort" not in record:
        raise RemoteReadFailed("remote model or effort missing")
    model: Model = (ModelNotReported() if record["model"] is None
                    else ReportedModel(remote_string(record, "model")))
    effort: Effort = (EffortNotReported() if record["effort"] is None
                      else ReportedEffort(remote_string(record, "effort")))
    before = record.get("before_first_edit")
    if not isinstance(before, bool):
        raise RemoteReadFailed("remote first edit flag missing or invalid")
    return Turn(agent, remote_string(record, "key"), at, model, effort,
                RawTokens(remote_integer(tokens, "input"), remote_integer(tokens, "cache_write_5m"),
                          remote_integer(tokens, "cache_write_1h"), remote_integer(tokens, "cache_read"),
                          remote_integer(tokens, "output")), before)


def remote_records(lines: Iterable[str]) -> Iterator[dict[str, object]]:
    for line in lines:
        if not line.endswith("\n"):
            raise RemoteReadFailed("remote connection cut inside a record")
        if line.startswith("rc="):
            if line.strip() != "rc=0":
                raise RemoteReadFailed(line.strip())
            yield {"type": "success"}
            return
        if not line.strip():
            continue
        record = json_fields(line)
        if not record:
            raise RemoteReadFailed("invalid remote stream")
        yield record
    raise RemoteReadFailed("remote command status missing")


def receive_file(records: Iterator[dict[str, object]], path: Path) -> SourceState:
    with path.open("wb") as output:
        for record in records:
            if record.get("type") == "end":
                state = remote_state(record, "state")
                if state not in (SourceState.READY, SourceState.MISSING, SourceState.UNREADABLE):
                    raise RemoteReadFailed("invalid remote file state")
                return state
            if record.get("type") != "chunk":
                raise RemoteReadFailed("incomplete remote file")
            try:
                # An empty chunk is valid, though the reader never needs one.
                data = record.get("data")
                if not isinstance(data, str):
                    raise RemoteReadFailed("remote file bytes missing")
                _ = output.write(base64.b64decode(data, validate=True))
            except ValueError as error:
                raise RemoteReadFailed("invalid remote file bytes") from error
    raise RemoteReadFailed("remote file status missing")


@dataclass(frozen=True)
class RoleSnapshotPending:
    pass


@dataclass(frozen=True)
class RoleSourceSnapshot:
    log: RoleLog
    state: SourceState


@dataclass
class MacReadProgress:
    states: dict[LogFamily, SourceState] = field(default_factory=lambda: {
        family: SourceState.UNREACHABLE for family in LOG_FAMILIES})
    totals: dict[LogFamily, int] = field(default_factory=lambda: {family: 0 for family in LOG_FAMILIES})
    completed_files: int = 0
    role_snapshot: RoleSourceSnapshot | RoleSnapshotPending = field(default_factory=RoleSnapshotPending)


def receive_measurements(db: sqlite3.Connection, records: Iterator[dict[str, object]],
                         machine: str, path: str, family: LogFamily, resolver: RoleResolver) -> int:
    added = 0
    # A merge into an earlier row also belongs to this file's transaction.
    with db:
        for record in records:
            if remote_string(record, "path") != path or remote_string(record, "family") != family:
                raise RemoteReadFailed("remote file changed before its cursor")
            if record.get("type") == "cursor":
                save_cursor(db, machine, path, FileCursor(remote_integer(record, "offset")))
                return added
            if record.get("type") == "problem":
                raise RemoteFileUnreadable()
            if family == "history":
                if record.get("type") != "outcome":
                    raise RemoteReadFailed("unexpected remote outcome record")
                added += save_history_event(db, remote_history_event(record, machine, Path(path).stem))
                continue
            if record.get("type") != "measurement":
                raise RemoteReadFailed("unexpected remote measurement record")
            added += save_measurement(db, remote_measurement(record, machine, family), resolver)
    raise RemoteReadFailed("remote file cursor missing")


def consume_remote(db: sqlite3.Connection, lines: Iterable[str], machine: str, scratch: Path,
                   progress: MacReadProgress) -> None:
    records = remote_records(lines)
    manifest = next(records)
    if manifest.get("type") != "manifest":
        raise RemoteReadFailed("remote inventory missing")
    raw_paths = manifest.get("claude")
    if not isinstance(raw_paths, list):
        raise RemoteReadFailed("remote session paths missing")
    paths: list[Path] = []
    for path in cast(list[object], raw_paths):
        if not isinstance(path, str) or not path:
            raise RemoteReadFailed("invalid remote session path")
        paths.append(Path(path))
    for family in LOG_FAMILIES:
        progress.states[family] = remote_state(manifest, f"{family}_state")
    snapshot: RoleSourceSnapshot | RoleSnapshotPending = RoleSnapshotPending()
    directors: dict[Agent, tuple[Assignment, ...]] = {}
    resolver = RoleResolver(RoleLog({}, ()), known_sessions(db, machine, paths), directors)
    attribution_ready = False
    received_path = scratch / "source"
    for record in records:
        record_type = record.get("type")
        if record_type == "success":
            if not attribution_ready:
                raise RemoteReadFailed("remote attribution snapshot incomplete")
            return
        if record_type == "attribution_ready":
            if attribution_ready or not isinstance(snapshot, RoleSourceSnapshot):
                raise RemoteReadFailed("unexpected remote attribution marker")
            resolver = RoleResolver(snapshot.log, known_sessions(db, machine, paths), directors)
            with db:
                if snapshot.state is not SourceState.UNREADABLE:
                    reattribute(db, machine, resolver)
                save_role_source(db, machine, snapshot.log, snapshot.state)
            progress.role_snapshot = snapshot
            attribution_ready = True
            continue
        if record_type == "director":
            if attribution_ready or isinstance(snapshot, RoleSnapshotPending):
                raise RemoteReadFailed("unexpected remote director evidence")
            evidence = remote_director(record, machine)
            assignment = Assignment(evidence.at, "director", None, True)
            history = directors.get(evidence.agent, ())
            if assignment not in history:
                directors[evidence.agent] = (*history, assignment)
            continue
        if record_type == "problem":
            family_name = remote_string(record, "family")
            if not attribution_ready or family_name not in LOG_FAMILIES:
                raise RemoteReadFailed("unexpected remote source problem")
            progress.states[family_name] = SourceState.PARTIAL
            continue
        if record_type != "file":
            raise RemoteReadFailed("unexpected remote record")
        kind = remote_string(record, "kind")
        path = remote_string(record, "path")
        if kind == "agents":
            if attribution_ready or isinstance(snapshot, RoleSourceSnapshot):
                raise RemoteReadFailed("unexpected remote attribution file")
            file_state = receive_file(records, received_path)
            log = assignments(received_path) if file_state is SourceState.READY else RoleLog({}, ())
            snapshot = RoleSourceSnapshot(log, file_state)
            received_path.unlink()
        elif kind in LOG_FAMILIES and attribution_ready:
            family = kind
            try:
                added = receive_measurements(db, records, machine, path, family, resolver)
            except RemoteFileUnreadable:
                progress.states[family] = SourceState.PARTIAL
                continue
            progress.totals[family] += added
            progress.completed_files += 1
        else:
            raise RemoteReadFailed("unexpected remote file kind or order")
    raise RemoteReadFailed("remote command status missing")


def reader_payload(offsets: dict[str, int]) -> str:
    root = Path(__file__).resolve().parents[2]
    modules = {name: (root / relative).read_text(encoding="utf-8") for name, relative in (
        ("scripts.usage.agents_log", "scripts/usage/agents_log.py"),
        ("scripts.whoami.seat_usage", "scripts/whoami/seat_usage.py"),
        ("scripts.usage.collect", "scripts/usage/collect.py"),
    )}
    return json.dumps({"modules": modules, "cursors": offsets}, separators=(",", ":"))


def collect_mac(db: sqlite3.Connection) -> None:
    machine = "mac"
    progress = MacReadProgress()
    cursor_rows = db.execute("SELECT path, offset FROM cursors WHERE machine = ?", (machine,))
    offsets = {cast(str, row[0]): cast(int, row[1])
               for row in cast(Iterable[sqlite3.Row], cursor_rows)}
    command = shlex.join(("python3", "-c", REMOTE_BOOTSTRAP)) + '; printf "\\nrc=%s\\n" "$?"'
    detail = ""
    try:
        payload = reader_payload(offsets)
        with tempfile.TemporaryDirectory(prefix="usage-mac-") as directory, tempfile.TemporaryFile(mode="w+") as errors:
            process: subprocess.Popen[str] = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", MAC_HOST, command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", errors="strict")
            try:
                remote_input = process.stdin
                if remote_input is None:
                    raise RemoteReadFailed("ssh produced no input stream")
                with remote_input:
                    _ = remote_input.write(payload)
                output = process.stdout
                if output is None:
                    raise RemoteReadFailed("ssh produced no stream")
                with output:
                    consume_remote(db, output, machine, Path(directory), progress)
                status = process.wait()
                if status != 0:
                    raise RemoteReadFailed(f"ssh exited {status}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    _ = process.wait()
    except (OSError, UnicodeError, RemoteReadFailed) as error:
        state = SourceState.PARTIAL if progress.completed_files else SourceState.UNREACHABLE
        progress.states = {family: state for family in LOG_FAMILIES}
        detail = " (" + " ".join(str(error).splitlines()) + ")"
        if isinstance(progress.role_snapshot, RoleSnapshotPending):
            with db:
                # Keep the previous problem count until a new snapshot commits.
                _ = db.execute("INSERT INTO role_log_sources VALUES (?, 0, 'unreachable', ?) " +
                               "ON CONFLICT(machine) DO UPDATE SET state = excluded.state, read_at = excluded.read_at",
                               (machine, datetime.now(timezone.utc).isoformat()))
    with db:
        for family in LOG_FAMILIES:
            save_turn_source(db, machine, family, progress.states[family])
    for family in LOG_FAMILIES:
        source_line(machine, family, progress.totals[family], progress.states[family], detail)
    snapshot = progress.role_snapshot
    if isinstance(snapshot, RoleSourceSnapshot):
        source_line(machine, "agents", 0, snapshot.state, f"; {len(snapshot.log.problems)} problems")
    else:
        source_line(machine, "agents", 0, SourceState.UNREACHABLE, detail)


class CommandLine(argparse.Namespace):
    db: Path
    machine: str
    claude_root: Path
    codex_root: Path
    agents_log: Path
    no_mac: bool
    readings: Path
    account_roles: Path
    claude_repo: Path
    production_docs: Path

    def __init__(self, readings: Path, account_roles: Path, claude_repo: Path, production_docs: Path) -> None:
        super().__init__()
        self.db = state_dir() / "usage.db"
        self.machine = "natedev"
        self.claude_root = Path.home() / ".claude/projects"
        self.codex_root = Path.home() / ".codex/sessions"
        self.agents_log = state_dir() / "agents.jsonl"
        self.no_mac = False
        self.readings = readings
        self.account_roles = account_roles
        self.claude_repo = claude_repo
        self.production_docs = production_docs


def main(argv: Sequence[str] = ()) -> int:
    # The Mac has only the three shipped modules; local sources never run there.
    from scripts.usage import outcomes

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--db", type=Path)
    _ = parser.add_argument("--machine")
    _ = parser.add_argument("--claude-root", type=Path)
    _ = parser.add_argument("--codex-root", type=Path)
    _ = parser.add_argument("--agents-log", type=Path)
    _ = parser.add_argument("--no-mac", action="store_true")
    _ = parser.add_argument("--readings", type=Path)
    _ = parser.add_argument("--account-roles", type=Path)
    _ = parser.add_argument("--claude-repo", type=Path)
    _ = parser.add_argument("--production-docs", type=Path)
    defaults = outcomes.default_paths()
    args = parser.parse_args(argv, namespace=CommandLine(defaults.readings, defaults.account_roles,
                                                       defaults.claude_repo, defaults.production_docs))
    try:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(args.db)
        try:
            db.row_factory = sqlite3.Row
            _ = db.executescript(SCHEMA)
            migrate_store(db)
            collect_local(db, args)
            if args.no_mac:
                with db:
                    for family in LOG_FAMILIES:
                        save_turn_source(db, "mac", family, SourceState.SKIPPED)
                for source in (*LOG_FAMILIES, "agents"):
                    source_line("mac", source, 0, SourceState.SKIPPED)
            else:
                collect_mac(db)
            reader = outcomes.IncrementalLogReader(complete_lines, json_fields, cursor_at,
                                                  lambda ledger, machine, path, offset:
                                                  save_cursor(ledger, machine, path, FileCursor(offset)))
            for result in outcomes.collect(db, args.readings, args.account_roles,
                                           args.claude_repo, args.production_docs, reader):
                state = (SourceState.READY if isinstance(result.state, outcomes.CompleteSourceRead)
                         else SourceState.MISSING if isinstance(result.state, outcomes.MissingSource)
                         else SourceState.UNREADABLE if isinstance(result.state, outcomes.UnreadableSource)
                         else SourceState.PARTIAL)
                with db:
                    save_turn_source(db, "natedev", result.family, state)
                source_line("natedev", result.family, result.added, state)
        finally:
            db.close()
    except (OSError, sqlite3.Error) as error:
        print("collect: database write failed: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
