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
from pathlib import Path
from typing import BinaryIO, Literal, cast

# Direct execution from any directory uses this checkout's modules.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.usage.agents_log import Assignment, Lens, Role, RoleLog, assignments, role_at, state_dir
from scripts.whoami.seat_usage import (
    MAC_HOST, decode_rollout, is_compaction, is_edit, parse_timestamp, token_usage,
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
class NoMeasurement:
    pass


Measurement = Turn | Compaction | NoMeasurement


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

    def consume(self, line: str, offset: int) -> Measurement:
        record = decode_rollout(line)
        if record is None:
            return NoMeasurement()
        payload = object_fields(record.get("payload"))
        if record.get("type") == "session_meta":
            thread_id = payload.get("id")
            if isinstance(thread_id, str) and thread_id:
                self.agent = CodexThread(self.machine, thread_id)
        if record.get("type") == "turn_context":
            self.model = reported_model(payload.get("model"))
            self.effort = reported_effort(payload.get("effort"))
        self.edit_seen = self.edit_seen or is_edit(record)
        at = parse_timestamp(record.get("timestamp"))
        if at is None or not isinstance(self.agent, CodexThread):
            return NoMeasurement()
        if is_compaction(record):
            return Compaction(self.agent, at)
        usage = token_usage(record)
        if usage is None:
            return NoMeasurement()
        return Turn(self.agent, str(offset), at, self.model, self.effort,
                    RawTokens(usage.uncached_input, 0, 0, usage.cached_input, usage.output),
                    not self.edit_seen)


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
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
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
    key = json.dumps((message_id, record.get("requestId")), separators=(",", ":"))
    return Turn(agent, key, at, reported_model(message.get("model")),
                reported_effort(record.get("effort")),
                RawTokens(token_count(usage.get("input_tokens")), five_minute, one_hour,
                          token_count(usage.get("cache_read_input_tokens")),
                          token_count(usage.get("output_tokens"))), not edit_seen)


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
"""


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


def collect_file(db: sqlite3.Connection, machine: str, path: str, local_path: Path,
                 family: Family, resolver: RoleResolver) -> int:
    previous = cursor_at(db, machine, path)
    size = local_path.stat().st_size
    if previous == size:
        return 0
    if previous > size:
        previous = 0
    added = 0
    context = CodexContext(machine)
    agent = claude_identity(machine, Path(path))
    edit_seen = False
    completed = 0
    with local_path.open("rb") as source:
        # Changed files replay their prefix to recover context and the first edit.
        # Unchanged files never enter this loop; cursors store only durable offsets.
        for offset, line in complete_lines(source):
            try:
                if family == "claude":
                    record = json_fields(line)
                    edit_seen = edit_seen or claude_edit(record)
                    value = (claude_measurement(record, agent, edit_seen)
                             if offset >= previous else NoMeasurement())
                else:
                    value = context.consume(line.decode("utf-8"), offset)
            except (ValueError, UnicodeError, RecursionError):
                value = NoMeasurement()
            if offset >= previous:
                added += save_measurement(db, value, resolver)
            completed = offset + len(line)
    _ = db.execute("INSERT INTO cursors VALUES (?, ?, ?) " +
                   "ON CONFLICT(machine, path) DO UPDATE SET offset = excluded.offset",
                   (machine, path, completed))
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


def role_source(path: Path) -> tuple[RoleLog, str]:
    try:
        if not path.exists():
            return assignments(path), "missing"
        return assignments(path), "ready"
    except OSError:
        return RoleLog({}, ()), "unreadable"


def save_role_source(db: sqlite3.Connection, machine: str, log: RoleLog, state: str) -> None:
    _ = db.execute("INSERT INTO role_log_sources VALUES (?, ?, ?, ?) " +
                   "ON CONFLICT(machine) DO UPDATE SET problems = excluded.problems, " +
                   "state = excluded.state, read_at = excluded.read_at",
                   (machine, len(log.problems), state, datetime.now(timezone.utc).isoformat()))


def source_line(machine: str, source: str, added: int, state: str) -> None:
    print(f"{machine} {source}: {added} turns added; {state}")


def source_paths(root: Path, family: Family) -> tuple[tuple[Path, ...], str]:
    try:
        if not root.exists():
            return (), "missing"
        patterns = ("*/*.jsonl", "*/*/subagents/agent-*.jsonl") if family == "claude" else ("*/*/*/*.jsonl",)
        paths = {path for pattern in patterns for path in root.glob(pattern)}
        return tuple(sorted(paths, key=lambda path: (path.parent.name == "subagents", path))), "ready"
    except OSError:
        return (), "unreadable"


def collect_local(db: sqlite3.Connection, args: CommandLine) -> None:
    claude, claude_state = source_paths(args.claude_root, "claude")
    codex, codex_state = source_paths(args.codex_root, "codex")
    log, log_state = role_source(args.agents_log)
    resolver = RoleResolver(log, known_sessions(db, args.machine, claude), local_directors(args.machine))
    results: list[tuple[str, int, str]] = []
    with db:
        for family, paths, state in (("claude", claude, claude_state), ("codex", codex, codex_state)):
            added = 0
            for path in paths:
                try:
                    added += collect_file(db, args.machine, str(path), path, cast(Family, family), resolver)
                except OSError:
                    state = "partial"
            results.append((family, added, state))
        if log_state != "unreadable":
            reattribute(db, args.machine, resolver)
        save_role_source(db, args.machine, log, log_state)
    for family, added, state in results:
        source_line(args.machine, family, added, state)
    source_line(args.machine, "agents", 0, f"{log_state}; {len(log.problems)} problems")


# The remote reader only transports bytes. Parsing and all database writes stay here.
# One source file is held at a time, including on the first import.
REMOTE_READER = """
import base64, json, sys
from pathlib import Path
offsets: dict[str, int] = json.load(sys.stdin)
def emit(record):
    print(json.dumps(record, separators=(',', ':')), flush=True)
def paths(root, patterns):
    try:
        found = {p for pattern in patterns for p in root.glob(pattern)}
        return sorted(found, key=lambda p: (p.parent.name == 'subagents', p)), 'ready' if root.exists() else 'missing'
    except OSError:
        return [], 'unreadable'
def send(path, kind):
    emit({'type': 'file', 'path': str(path), 'kind': kind})
    try:
        with path.open('rb') as source:
            while True:
                data = source.read(262144)
                if not data:
                    break
                emit({'type': 'chunk', 'data': base64.b64encode(data).decode('ascii')})
        state = 'ready'
    except FileNotFoundError:
        state = 'missing'
    except OSError:
        state = 'unreadable'
    emit({'type': 'end', 'state': state})
home = Path.home()
claude, claude_state = paths(home / '.claude/projects', ('*/*.jsonl', '*/*/subagents/agent-*.jsonl'))
codex, codex_state = paths(home / '.codex/sessions', ('*/*/*/*.jsonl',))
emit({'type': 'manifest', 'claude': [str(p) for p in claude], 'claude_state': claude_state, 'codex_state': codex_state})
send(home / '.local/state/usage-model/agents.jsonl', 'agents')
history, _ = paths(home / '.local/state/plan-delegate/runs', ('*.jsonl',))
for path in history:
    send(path, 'history')
for kind, sources in (('claude', claude), ('codex', codex)):
    for path in sources:
        try:
            if path.stat().st_size != offsets.get(str(path), 0):
                send(path, kind)
        except OSError:
            emit({'type': 'problem', 'kind': kind})
"""


class RemoteReadFailed(Exception):
    """The remote stream did not end with a successful command status."""


def remote_records(lines: Iterable[str]) -> Iterator[dict[str, object]]:
    for line in lines:
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


def receive_file(records: Iterator[dict[str, object]], path: Path) -> str:
    with path.open("wb") as output:
        for record in records:
            if record.get("type") == "end":
                state = record.get("state")
                return state if isinstance(state, str) else "unreadable"
            data = record.get("data")
            if record.get("type") != "chunk" or not isinstance(data, str):
                raise RemoteReadFailed("incomplete remote file")
            try:
                _ = output.write(base64.b64decode(data, validate=True))
            except ValueError as error:
                raise RemoteReadFailed("invalid remote file bytes") from error
    raise RemoteReadFailed("remote file status missing")


def consume_remote(db: sqlite3.Connection, lines: Iterable[str], machine: str, scratch: Path) -> None:
    records = remote_records(lines)
    manifest = next(records)
    if manifest.get("type") != "manifest":
        raise RemoteReadFailed("remote inventory missing")
    raw_paths = manifest.get("claude")
    paths = [Path(path) for path in cast(list[object], raw_paths) if isinstance(path, str)] if isinstance(raw_paths, list) else []
    states = {family: str(manifest.get(f"{family}_state", "unreadable")) for family in ("claude", "codex")}
    totals = {family: 0 for family in ("claude", "codex")}
    log = RoleLog({}, ())
    log_state = "unreadable"
    directors: dict[Agent, tuple[Assignment, ...]] = {}
    resolver = RoleResolver(log, known_sessions(db, machine, paths), directors)
    received_path = scratch / "source"
    for record in records:
        if record.get("type") == "success":
            if log_state != "unreadable":
                reattribute(db, machine, resolver)
            save_role_source(db, machine, log, log_state)
            for family in ("claude", "codex"):
                source_line(machine, family, totals[family], states[family])
            source_line(machine, "agents", 0, f"{log_state}; {len(log.problems)} problems")
            return
        kind = record.get("kind")
        if record.get("type") == "problem":
            if isinstance(kind, str) and kind in states:
                states[kind] = "partial"
            continue
        path = record.get("path")
        if record.get("type") != "file" or not isinstance(path, str):
            raise RemoteReadFailed("unexpected remote record")
        file_state = receive_file(records, received_path)
        if kind == "agents":
            log_state = file_state
            log = assignments(received_path) if file_state == "ready" else RoleLog({}, ())
            resolver = RoleResolver(log, known_sessions(db, machine, paths), directors)
        elif kind == "history" and file_state == "ready":
            with received_path.open("rb") as source:
                director_evidence(source, machine, directors)
        elif kind in ("claude", "codex"):
            family = kind
            if file_state == "ready":
                totals[family] += collect_file(db, machine, path, received_path, family, resolver)
            else:
                states[family] = "partial"
        received_path.unlink()
    raise RemoteReadFailed("remote command status missing")


def collect_mac(db: sqlite3.Connection) -> None:
    machine = "mac"
    cursor_rows = db.execute("SELECT path, offset FROM cursors WHERE machine = ?", (machine,))
    offsets = {cast(str, row[0]): cast(int, row[1])
               for row in cast(Iterable[sqlite3.Row], cursor_rows)}
    command = shlex.join(("python3", "-c", REMOTE_READER)) + '; printf "\\nrc=%s\\n" "$?"'
    try:
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
                    _ = remote_input.write(json.dumps(offsets))
                output = process.stdout
                if output is None:
                    raise RemoteReadFailed("ssh produced no stream")
                with output, db:
                    consume_remote(db, output, machine, Path(directory))
                _ = process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    _ = process.wait()
    except (OSError, UnicodeError, RemoteReadFailed) as error:
        with db:
            # An outage preserves the last diagnostic snapshot and attribution.
            _ = db.execute("INSERT INTO role_log_sources VALUES (?, 0, 'unreachable', ?) " +
                           "ON CONFLICT(machine) DO UPDATE SET state = excluded.state, read_at = excluded.read_at",
                           (machine, datetime.now(timezone.utc).isoformat()))
        reason = " ".join(str(error).splitlines())
        for source in ("claude", "codex", "agents"):
            source_line(machine, source, 0, f"unreachable ({reason})")


class CommandLine(argparse.Namespace):
    db: Path
    machine: str
    claude_root: Path
    codex_root: Path
    agents_log: Path
    no_mac: bool

    def __init__(self) -> None:
        super().__init__()
        self.db = state_dir() / "usage.db"
        self.machine = "natedev"
        self.claude_root = Path.home() / ".claude/projects"
        self.codex_root = Path.home() / ".codex/sessions"
        self.agents_log = state_dir() / "agents.jsonl"
        self.no_mac = False


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--db", type=Path)
    _ = parser.add_argument("--machine")
    _ = parser.add_argument("--claude-root", type=Path)
    _ = parser.add_argument("--codex-root", type=Path)
    _ = parser.add_argument("--agents-log", type=Path)
    _ = parser.add_argument("--no-mac", action="store_true")
    args = parser.parse_args(argv, namespace=CommandLine())
    try:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(args.db)
        try:
            db.row_factory = sqlite3.Row
            _ = db.executescript(SCHEMA)
            collect_local(db, args)
            if args.no_mac:
                for source in ("claude", "codex", "agents"):
                    source_line("mac", source, 0, "skipped")
            else:
                collect_mac(db)
        finally:
            db.close()
    except (OSError, sqlite3.Error) as error:
        print("collect: database write failed: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
