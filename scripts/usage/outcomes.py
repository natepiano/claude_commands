"""Read natedev's checkpoints and account history beside the shared log outcomes."""

from __future__ import annotations

import math
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal, cast

from scripts.whoami import run_out, seat_usage


@dataclass(frozen=True)
class CompleteSourceRead:
    pass


@dataclass(frozen=True)
class MissingSource:
    pass


@dataclass(frozen=True)
class UnreadableSource:
    pass


@dataclass(frozen=True)
class PartialSourceRead:
    pass


LocalReadState = CompleteSourceRead | MissingSource | UnreadableSource | PartialSourceRead
LocalSourceFamily = Literal["readings", "account_roles", "checkpoints"]


@dataclass(frozen=True)
class LocalSourceResult:
    family: LocalSourceFamily
    added: int
    state: LocalReadState


@dataclass(frozen=True)
class IncrementalLogReader:
    lines: Callable[[BinaryIO], Iterator[tuple[int, bytes]]]
    fields: Callable[[bytes], dict[str, object]]
    cursor: Callable[[sqlite3.Connection, str, str], int]
    save_cursor: Callable[[sqlite3.Connection, str, str, int], None]


@dataclass(frozen=True)
class LocalOutcomeSources:
    readings: Path
    account_roles: Path
    claude_repo: Path
    production_docs: Path


@dataclass(frozen=True)
class AmountRead:
    value: float


@dataclass(frozen=True)
class AmountNotReported:
    pass


@dataclass(frozen=True)
class AmountUnreadable:
    pass


AccountAmount = AmountRead | AmountNotReported | AmountUnreadable


@dataclass(frozen=True)
class AccountReading:
    account: str
    at: datetime
    remaining: AccountAmount
    credits: AccountAmount


@dataclass(frozen=True)
class CheckpointPhaseNamed:
    plan_slug: str
    phase_id: str


@dataclass(frozen=True)
class CheckpointPhaseNotNamed:
    pass


CheckpointSubject = CheckpointPhaseNamed | CheckpointPhaseNotNamed


@dataclass(frozen=True)
class MappedCheckpoint:
    machine: str
    run_id: str
    phase_instance_id: str


@dataclass(frozen=True)
class UnmappedCheckpoint:
    pass


CheckpointMapping = MappedCheckpoint | UnmappedCheckpoint


def default_paths() -> LocalOutcomeSources:
    # Only the local process imports account configuration; no live account read is needed.
    from scripts.accounts import accounts

    sources = seat_usage.default_sources()
    return LocalOutcomeSources(run_out.READINGS_LOG, accounts.roles_history_file(),
                               sources.claude_repo, sources.production_docs)


def account_amount(value: object, *, remaining: bool) -> AccountAmount:
    if value is None:
        return AmountNotReported()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return AmountUnreadable()
    try:
        number = float(value)
    except OverflowError:
        return AmountUnreadable()
    if not math.isfinite(number) or number < 0 or remaining and number > 100:
        return AmountUnreadable()
    return AmountRead(number)


def amount_columns(amount: AccountAmount) -> tuple[str, float | None]:
    if isinstance(amount, AmountRead):
        return "read", amount.value
    return ("not_reported" if isinstance(amount, AmountNotReported) else "unreadable"), None


def save_reading(db: sqlite3.Connection, record: dict[str, object]) -> int:
    account = record.get("account")
    at = seat_usage.parse_timestamp(record.get("at"))
    if not isinstance(account, str) or at is None:
        return 0
    reading = AccountReading(account, at, account_amount(record.get("remaining"), remaining=True),
                             account_amount(record.get("credits"), remaining=False))
    row = db.execute("INSERT OR IGNORE INTO account_readings VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("natedev", reading.account, reading.at.isoformat(),
                      *amount_columns(reading.remaining), *amount_columns(reading.credits)))
    return row.rowcount


def save_account_role(db: sqlite3.Connection, record: dict[str, object]) -> int:
    at = seat_usage.parse_timestamp(record.get("at"))
    role, label = record.get("role"), record.get("label")
    if at is None or not isinstance(role, str) or not isinstance(label, str):
        return 0
    row = db.execute("INSERT OR IGNORE INTO account_role_changes VALUES (?, ?, ?, ?)",
                     ("natedev", at.isoformat(), role, label))
    return row.rowcount


def collect_account_file(db: sqlite3.Connection, path: Path, family: LocalSourceFamily,
                         save: Callable[[sqlite3.Connection, dict[str, object]], int],
                         reader: IncrementalLogReader) -> LocalSourceResult:
    added = 0
    state: LocalReadState = CompleteSourceRead()
    try:
        size = path.stat().st_size
        previous = reader.cursor(db, "natedev", str(path))
        changed = size != previous
        if size < previous:
            previous = 0
        if changed:
            with db, path.open("rb") as source:
                completed = 0
                count = 0
                for offset, line in reader.lines(source):
                    if offset >= previous:
                        count += save(db, reader.fields(line))
                    completed = offset + len(line)
                reader.save_cursor(db, "natedev", str(path), completed)
            added = count
    except FileNotFoundError:
        state = MissingSource()
    except OSError:
        state = UnreadableSource()
    return LocalSourceResult(family, added, state)


def checkpoint_subject(subject: str) -> CheckpointSubject:
    match = re.fullmatch(r"checkpoint\(([^()]+)\): phase (.+?) — .+", subject)
    if match is None:
        return CheckpointPhaseNotNamed()
    return CheckpointPhaseNamed(match[1], match[2])


def checkpoint_mapping(db: sqlite3.Connection, subject: CheckpointSubject, at: datetime) -> CheckpointMapping:
    if isinstance(subject, CheckpointPhaseNotNamed):
        return UnmappedCheckpoint()
    rows = db.execute("SELECT p.machine, p.run_id, p.phase_instance_id, r.plan_doc FROM phases p " +
                      "JOIN runs r ON r.machine = p.machine AND r.run_id = p.run_id " +
                      "WHERE p.phase_id = ? AND p.started_at <= ? " +
                      "AND (p.state = 'open' OR p.finished_at >= ?) ORDER BY p.started_at DESC, " +
                      "p.machine, p.phase_instance_id",
                      (subject.phase_id, at.isoformat(), at.isoformat()))
    for row in cast(Iterable[sqlite3.Row], rows):
        plan_doc = cast(object, row["plan_doc"])
        if isinstance(plan_doc, str) and Path(plan_doc).name.removesuffix(".md") == subject.plan_slug:
            return MappedCheckpoint(cast(str, row["machine"]), cast(str, row["run_id"]),
                                    cast(str, row["phase_instance_id"]))
    return UnmappedCheckpoint()


def mapping_columns(mapping: CheckpointMapping) -> tuple[str, str | None, str | None, str | None]:
    if isinstance(mapping, MappedCheckpoint):
        return "mapped", mapping.machine, mapping.run_id, mapping.phase_instance_id
    return "unmapped", None, None, None


def collect_checkpoints(db: sqlite3.Connection, claude_repo: Path, production_docs: Path) -> LocalSourceResult:
    state: LocalReadState = CompleteSourceRead()
    added = 0
    try:
        candidates = (claude_repo, *(path for document in sorted(production_docs.glob("*-production.md"))
                                     for path in seat_usage.production_paths(document)))
        # Repository discovery omits failures; retain them in this source's state.
        # A worktree removed after its merge is no failure: its commits stay in the shared repository.
        for candidate in candidates:
            if candidate.exists() and seat_usage.git_text(candidate, "rev-parse", "--git-common-dir") is None:
                state = PartialSourceRead()
        repos = seat_usage.repositories(claude_repo, production_docs)
        if not repos:
            state = PartialSourceRead()
        for repo in repos:
            try:
                result = subprocess.run(["git", "-C", str(repo), "log", "--all", "--format=%H%x00%ct%x00%s"],
                                        capture_output=True, text=True, check=False)
            except (OSError, UnicodeError):
                state = PartialSourceRead()
                continue
            if result.returncode:
                state = PartialSourceRead()
                continue
            with db:
                for line in result.stdout.splitlines():
                    parts = line.split("\x00", 2)
                    if len(parts) != 3 or not parts[2].startswith("checkpoint("):
                        continue
                    commit_id, seconds, text = parts
                    try:
                        at = datetime.fromtimestamp(int(seconds), timezone.utc)
                    except (ValueError, OverflowError, OSError):
                        continue
                    subject = checkpoint_subject(text)
                    row = db.execute("INSERT OR IGNORE INTO checkpoints VALUES (?, ?, ?, ?, 'unmapped', NULL, NULL, NULL)",
                                     (commit_id, at.isoformat(),
                                      subject.plan_slug if isinstance(subject, CheckpointPhaseNamed) else None,
                                      subject.phase_id if isinstance(subject, CheckpointPhaseNamed) else None))
                    added += row.rowcount
    except (OSError, UnicodeError):
        state = PartialSourceRead()
    with db:
        # Revisit stored commits too, so new history can map an unchanged repository.
        rows = db.execute("SELECT commit_id, at, plan_slug, phase_id FROM checkpoints")
        for row in cast(Iterable[sqlite3.Row], rows):
            slug = cast(object, row["plan_slug"])
            phase = cast(object, row["phase_id"])
            subject = (CheckpointPhaseNamed(slug, phase) if isinstance(slug, str) and isinstance(phase, str)
                       else CheckpointPhaseNotNamed())
            at = seat_usage.parse_timestamp(cast(str, row["at"]))
            if at is None:
                continue
            columns = mapping_columns(checkpoint_mapping(db, subject, at))
            _ = db.execute("UPDATE checkpoints SET mapping = ?, machine = ?, run_id = ?, phase_instance_id = ? " +
                           "WHERE commit_id = ? AND (mapping, machine, run_id, phase_instance_id) IS NOT (?, ?, ?, ?)",
                           (*columns, row["commit_id"], *columns))
    return LocalSourceResult("checkpoints", added, state)


def collect(db: sqlite3.Connection, readings: Path, account_roles: Path,
            claude_repo: Path, production_docs: Path, reader: IncrementalLogReader) -> tuple[LocalSourceResult, ...]:
    return (collect_account_file(db, readings, "readings", save_reading, reader),
            collect_account_file(db, account_roles, "account_roles", save_account_role, reader),
            collect_checkpoints(db, claude_repo, production_docs))
