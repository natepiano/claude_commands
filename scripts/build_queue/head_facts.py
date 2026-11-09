"""Read bounded head history for the queue page without making admission decisions."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
import json
import math
import os
from pathlib import Path
import re
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo

from scripts.buildlog import store
from scripts.lib.build_steps import StepNames
from scripts.build_queue import admission_records
from scripts.message import unit_labels

BLOCK_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 32 * BLOCK_BYTES
BUMP_TABLE_ROWS = 8
QUEUE_TIMES_PATH = Path("admission/queue_times.jsonl")
_BUMP_KIND = re.compile(rb'"kind"\s*:\s*"queue_bump"')


@dataclass(frozen=True)
class BumpsUnavailable:
    reason: str


@dataclass(frozen=True)
class BumpRecord:
    at: float
    actor: str
    session: str
    step: str
    folder: str
    old_position: int


class MoveIdentity(NamedTuple):
    session: str
    step: str
    folder: str


@dataclass(frozen=True)
class QueueMoves:
    """Matched move histories in the queue reading's waiting and running order."""

    waiting: tuple[tuple[BumpRecord, ...], ...]
    running: tuple[tuple[BumpRecord, ...], ...]


@dataclass(frozen=True)
class BumpGroup:
    session: str
    step: str
    folder: str
    count: int
    actors: tuple[str, ...]
    last_at: float


@dataclass(frozen=True)
class BumpHistory:
    groups: tuple[BumpGroup, ...]
    total: int
    records: tuple[BumpRecord, ...] = ()


@dataclass(frozen=True)
class TokenReturned:
    at: float


@dataclass(frozen=True)
class CargoTokenWait:
    session_id: str
    step: str
    pid: int
    start_time: str
    left_at: float
    returned: TokenReturned


@dataclass(frozen=True)
class TokenWaitsNotRecorded:
    pass


@dataclass(frozen=True)
class RecordedTokenWaits:
    waits: tuple[CargoTokenWait, ...]
    has_unknown: bool = False
    head_waits: tuple[CargoTokenWait, ...] = ()


@dataclass(frozen=True)
class HeadFacts:
    now: float
    joined_at: float
    bumps: BumpsUnavailable | BumpHistory
    has_priority: bool
    token_waits: TokenWaitsNotRecorded | RecordedTokenWaits
    head_need: int
    bump_records: tuple[BumpRecord, ...] = ()
    priority: admission_records.QueuePriority | admission_records.NoQueuePriority = admission_records.NoQueuePriority()

    @cached_property
    def moves_by_step(self) -> Mapping[MoveIdentity, tuple[BumpRecord, ...]]:
        groups: dict[MoveIdentity, list[BumpRecord]] = {}
        for record in self.bump_records:
            identity = MoveIdentity(record.session, record.step, record.folder)
            groups.setdefault(identity, []).append(record)
        return {identity: tuple(records) for identity, records in groups.items()}


@dataclass
class LogScanBudget:
    read_bytes: int = 0


class _BumpReadError(ValueError):
    pass


@dataclass(frozen=True)
class _PartialLine:
    pass


def _backward_lines(path: Path, budget: LogScanBudget) -> Generator[bytes | _PartialLine]:
    """Yield complete lines backward and mark an unterminated tail once."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        position = handle.seek(0, os.SEEK_END)
        pending = b""
        skipping_tail = True
        while position:
            amount = min(position, BLOCK_BYTES, MAX_SCAN_BYTES - budget.read_bytes)
            if amount <= 0:
                raise _BumpReadError("scan exceeds 32 MiB")
            position -= amount
            if handle.seek(position) != position:
                raise OSError("build log seek failed")
            block = handle.read(amount)
            if len(block) != amount:
                raise OSError("build log changed during read")
            budget.read_bytes += amount
            parts = (block + pending).split(b"\n")
            pending = parts[0]
            if skipping_tail:
                if len(parts) > 1 and parts[-1]:
                    yield _PartialLine()
                complete = parts[1:-1]
                skipping_tail = len(parts) == 1
            else:
                complete = parts[1:]
            yield from reversed(complete)
        if not skipping_tail:
            yield pending
        elif pending:
            yield _PartialLine()


def _months_backward(since: float, now: float) -> Iterator[float]:
    first = datetime.fromtimestamp(since, UTC)
    month = datetime.fromtimestamp(now, UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while (month.year, month.month) >= (first.year, first.month):
        yield month.timestamp()
        if month.month == 1:
            if month.year == 1:
                return
            month = month.replace(year=month.year - 1, month=12)
        else:
            month = month.replace(month=month.month - 1)


def _json_record(line: bytes) -> Mapping[str, object]:
    value = cast(object, json.loads(line))
    if not isinstance(value, dict):
        raise ValueError("malformed record")
    return cast(Mapping[str, object], value)


def _bump_row(line: bytes) -> tuple[Mapping[str, object], float]:
    row = _json_record(line)
    if row.get("kind") != "queue_bump":
        raise _BumpReadError("malformed bump line")
    at = row.get("at")
    if not isinstance(at, str):
        raise _BumpReadError("malformed bump timestamp")
    instant = datetime.fromisoformat(at)
    if instant.utcoffset() is None:
        raise _BumpReadError("bump timestamp has no zone")
    for name in ("actor", "session", "step", "folder"):
        if not isinstance(row.get(name), str):
            raise _BumpReadError(f"malformed bump {name}")
    position = row.get("old_position")
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise _BumpReadError("malformed bump old_position")
    return row, instant.timestamp()


def _read_bumps(since: float, now: float) -> BumpHistory | BumpsUnavailable:
    records: list[BumpRecord] = []
    budget = LogScanBudget()
    try:
        host = store.host_name()
        for month in _months_backward(since, now):
            lines = _backward_lines(store.host_file(host, month), budget)
            try:
                for line in lines:
                    if isinstance(line, _PartialLine) or not _BUMP_KIND.search(line):
                        continue
                    row, at = _bump_row(line)
                    if at < since:
                        return group_bumps(tuple(reversed(records)))
                    if at <= now:
                        records.append(BumpRecord(at, cast(str, row["actor"]), cast(str, row["session"]),
                            cast(str, row["step"]), cast(str, row["folder"]), cast(int, row["old_position"])))
            finally:
                lines.close()
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        reason = str(error) if isinstance(error, _BumpReadError) else "build log could not be read or has a malformed bump line"
        return BumpsUnavailable(reason)
    return group_bumps(tuple(reversed(records)))


def group_bumps(records: tuple[BumpRecord, ...]) -> BumpHistory:
    records = tuple(sorted(records, key=lambda record: record.at))
    groups: dict[tuple[str, str, str], BumpGroup] = {}
    for record in records:
        key = (record.session, record.step, record.folder)
        previous = groups.get(key)
        groups[key] = BumpGroup(*key, 1 if previous is None else previous.count + 1,
            tuple(sorted({record.actor} if previous is None else {*previous.actors, record.actor})),
            record.at if previous is None else max(record.at, previous.last_at))
    history = _bump_history(groups)
    return BumpHistory(history.groups, history.total, records)


def _bump_history(groups: Mapping[tuple[str, str, str], BumpGroup]) -> BumpHistory:
    ordered = tuple(sorted(groups.values(), key=lambda group: (-group.count, -group.last_at,
                                                             group.session, group.step, group.folder)))
    return BumpHistory(ordered, sum(group.count for group in ordered))


@dataclass(frozen=True)
class _TimestampNotRecorded:
    pass


def _recorded_time(row: Mapping[str, object], name: str) -> float | _TimestampNotRecorded:
    value = row.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value >= 0 and math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return _TimestampNotRecorded()


def _token_gap(record: Mapping[str, object], now: float) -> CargoTokenWait:
    for field in ("session_id", "step", "start_time"):
        if not isinstance(record.get(field), str):
            raise ValueError("malformed cargo-token identity")
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("malformed cargo-token pid")
    left = _recorded_time(record, "token_left_at")
    back = _recorded_time(record, "token_back_at")
    if not isinstance(left, float) or not isinstance(back, float) or back < left or back > now:
        raise ValueError("cargo-token interval not recorded")
    return CargoTokenWait(cast(str, record["session_id"]), cast(str, record["step"]), pid,
                          cast(str, record["start_time"]), left, TokenReturned(back))


@dataclass(frozen=True)
class _NoTokenGap:
    pass


def _gap_matches_waiter(gap: CargoTokenWait, waiter: admission_records.WaitingStep,
                        process_starts: Mapping[int, str]) -> bool:
    if gap.step != waiter.step or gap.left_at < waiter.since:
        return False
    return (gap.pid == waiter.pid and bool(gap.start_time)
            and gap.start_time == process_starts.get(waiter.pid)
            and (not gap.session_id or gap.session_id == waiter.session_id))


def _read_token_waits(reading: admission_records.QueueRead) -> RecordedTokenWaits | TokenWaitsNotRecorded:
    """Match finished gaps to waiting runs, stopping before their oldest join time."""
    waiting = reading.view.waiting
    if not waiting:
        return TokenWaitsNotRecorded()
    oldest_since = min(waiter.since for waiter in waiting)
    process_starts = {entry[1]["pid"]: entry[1]["start_time"] for entry in reading.ordered}
    waits: list[CargoTokenWait] = []
    head_waits: list[CargoTokenWait] = []
    seen: set[tuple[str, str, int, str, float]] = set()
    has_unknown = False
    has_records = False
    lines = _backward_lines(store.root() / QUEUE_TIMES_PATH, LogScanBudget())
    try:
        for line in lines:
            if isinstance(line, _PartialLine):
                has_unknown = True
                continue
            try:
                record = _json_record(line)
                at = _recorded_time(record, "at")
                if not isinstance(at, float):
                    raise ValueError("queue event write time not recorded")
            except (ValueError, OverflowError, RecursionError):
                has_unknown = True
                continue
            try:
                gap = (_token_gap(record, reading.explanation.now)
                       if record.get("kind") == "token_gap" else _NoTokenGap())
            except (ValueError, OverflowError, RecursionError):
                has_unknown = True
                gap = _NoTokenGap()
            else:
                has_records = True
            if at < oldest_since:
                break
            if isinstance(gap, _NoTokenGap):
                continue
            matches = [waiter for waiter in waiting if _gap_matches_waiter(gap, waiter, process_starts)]
            if not matches:
                continue
            key = (gap.session_id, gap.step, gap.pid, gap.start_time, gap.left_at)
            if key in seen:
                continue
            seen.add(key)
            waits.append(gap)
            head = reading.explanation.head
            if isinstance(head, admission_records.HeadExplanation) and head.waiter in matches:
                head_waits.append(gap)
    except (OSError, ValueError):
        return TokenWaitsNotRecorded()
    finally:
        lines.close()
    return (RecordedTokenWaits(tuple(reversed(waits)), has_unknown, tuple(reversed(head_waits)))
            if has_records else TokenWaitsNotRecorded())


def read_head_facts(reading: admission_records.QueueRead) -> HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter:
    """Read optional admission fields and one bounded pass of local bump history."""
    head = reading.explanation.head
    if not isinstance(head, admission_records.HeadExplanation):
        return head
    now = reading.explanation.now
    history = _read_bumps(min(step.since for step in reading.view.waiting), now)
    records = history.records if isinstance(history, BumpHistory) else ()
    bumps = (group_bumps(tuple(record for record in records if record.at >= head.waiter.since))
             if isinstance(history, BumpHistory) else history)
    return HeadFacts(now, head.waiter.since, bumps,
                     head.has_priority, _read_token_waits(reading),
                     head_need=head.waiter.need, bump_records=records, priority=reading.explanation.priority)


def clock(instant: float, zone: ZoneInfo, *, seconds: bool = True, zone_name: bool = False) -> str:
    form = "%H:%M:%S" if seconds else "%H:%M"
    return datetime.fromtimestamp(instant, zone).strftime(form + (" %Z" if zone_name else ""))


def bump_text(facts: HeadFacts, zone: ZoneInfo, *, names: StepNames, short: bool = False) -> str:
    bumps = facts.bumps
    if isinstance(bumps, BumpsUnavailable):
        return "bumps: unknown" if short else f"Bumps since it joined: unknown ({bumps.reason})"
    if short:
        if not bumps.groups:
            return "no bumps since it joined"
        most = bumps.groups[0]
        folder = Path(most.folder).name or "unknown"
        noun = "bump" if bumps.total == 1 else "bumps"
        return f"{bumps.total} {noun} since it joined, most: {folder} {names.prose(most.step)} ({most.count})"
    count = str(bumps.total) if bumps.groups else "none"
    return f"Bumps since it joined at {clock(facts.joined_at, zone)}: {count}"


def _session_label(session: str, labels: Mapping[str, unit_labels.UnitLabels]) -> str:
    return labels[session].session if session in labels else session or "unknown"


def bump_lines(facts: HeadFacts, zone: ZoneInfo, labels: Mapping[str, unit_labels.UnitLabels], *, names: StepNames) -> list[str]:
    if isinstance(facts.bumps, BumpsUnavailable):
        return []
    lines = [f"  {_session_label(group.session, labels)} {names.prose(group.step)}, {group.folder}: {group.count} bumps; "
             + f"by {', '.join(_session_label(actor, labels) for actor in group.actors)}; "
             + f"last bump {clock(group.last_at, zone)}"
             for group in facts.bumps.groups[:BUMP_TABLE_ROWS]]
    remaining = facts.bumps.groups[BUMP_TABLE_ROWS:]
    if remaining:
        count = sum(group.count for group in remaining)
        noun = "bump" if count == 1 else "bumps"
        lines.append(f"and {len(remaining)} more steps with {count} {noun}")
    return lines


def priority_text(priority: admission_records.QueuePriority | admission_records.NoQueuePriority) -> str:
    return ("" if isinstance(priority, admission_records.NoQueuePriority) else
            f"queue priority: {priority['unit']} ({priority['why']})")


def order_rule(facts: HeadFacts) -> str:
    return (f"younger runs start only if room still covers the first run's need of {facts.head_need / admission_records.GIB:.1f} GiB")


def order_text(facts: HeadFacts) -> str:
    return "; ".join(filter(None, [priority_text(facts.priority), order_rule(facts)]))


def cargo_token_lines(facts: HeadFacts, zone: ZoneInfo, labels: Mapping[str, unit_labels.UnitLabels], *, names: StepNames) -> list[str]:
    if isinstance(facts.token_waits, TokenWaitsNotRecorded):
        return ["Cargo token waits: unknown"]
    if not facts.token_waits.waits:
        return ["Cargo token waits: unknown" if facts.token_waits.has_unknown else "Cargo token waits: none"]
    lines: list[str] = []
    for wait in facts.token_waits.waits:
        session = _session_label(wait.session_id, labels)
        prefix = f"Cargo token: {session} {names.prose(wait.step)}: "
        lines.append(prefix + token_wait_text((wait,), zone))
    if facts.token_waits.has_unknown:
        lines.append("Cargo token waits: unknown")
    return lines


def cargo_token_text(facts: HeadFacts) -> str:
    waits = known_head_waits(facts)
    if isinstance(waits, TokenWaitsNotRecorded):
        return "cargo token: unknown"
    if not waits:
        return "no cargo token wait"
    seconds = token_wait_seconds(waits)
    return f"cargo token waited {seconds} s"


def session_ids(facts: HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter) -> list[str]:
    if not isinstance(facts, HeadFacts):
        return []
    sessions: set[str] = set()
    if isinstance(facts.bumps, BumpHistory):
        sessions.update(record.session for record in facts.bump_records)
        sessions.update(record.actor for record in facts.bump_records)
        for group in facts.bumps.groups:
            sessions.add(group.session)
            sessions.update(group.actors)
    if isinstance(facts.token_waits, RecordedTokenWaits):
        sessions.update(wait.session_id for wait in facts.token_waits.waits)
    return sorted(sessions - {""})


@dataclass(frozen=True)
class RecordedPace:
    steps: int


@dataclass(frozen=True)
class PaceUnavailable:
    pass


def read_pace(now: float) -> RecordedPace | PaceUnavailable:
    """Read this host's completed admission releases in the preceding hour."""
    host = store.host_name()
    count = 0
    lines = _backward_lines(store.root() / "admission/anon_peaks.jsonl", LogScanBudget())
    try:
        for line in lines:
            if isinstance(line, _PartialLine):
                continue
            row = _json_record(line)
            value = row.get("ended_at")
            if isinstance(value, str):
                instant = datetime.fromisoformat(value)
                if instant.utcoffset() is None:
                    return PaceUnavailable()
                ended = instant.timestamp()
            else:
                ended = _recorded_time(row, "ended_at")
            if not isinstance(ended, float):
                return PaceUnavailable()
            if ended < now - 3600:
                break
            if ended <= now and row.get("host") == host:
                count += 1
    except (OSError, ValueError, OverflowError, RecursionError):
        return PaceUnavailable()
    finally:
        lines.close()
    return RecordedPace(count)


def move_identity(step: admission_records.WaitingStep | admission_records.RunningStep) -> MoveIdentity:
    return MoveIdentity(step.session_id or step.session_name, step.step, step.folder)


def waiting_moves(facts: HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter, step: admission_records.WaitingStep,
                  row: admission_records.Waiter) -> tuple[BumpRecord, ...]:
    if (not isinstance(facts, HeadFacts) or isinstance(facts.bumps, BumpsUnavailable)
            or not isinstance(admission_records.waiter_priority(row), admission_records.PriorityUnit)):
        return ()
    return tuple(record for record in facts.moves_by_step.get(move_identity(step), ())
                 if record.at >= step.since)


def running_moves(facts: HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter, step: admission_records.ExplainedRunningStep) -> tuple[BumpRecord, ...]:
    if (not isinstance(facts, HeadFacts) or isinstance(facts.bumps, BumpsUnavailable)
            or isinstance(step.admitted_at, admission_records.AdmissionTimeNotRecorded)):
        return ()
    return tuple(record for record in facts.moves_by_step.get(move_identity(step.step), ())
                 if record.at <= step.admitted_at)


def queue_moves(reading: admission_records.QueueRead, facts: HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter) -> QueueMoves:
    return QueueMoves(
        tuple(waiting_moves(facts, step, reading.ordered[index][1])
              for index, step in enumerate(reading.view.waiting)),
        tuple(running_moves(facts, step) for step in reading.explanation.running),
    )


def actor_text(actor: str, labels: Mapping[str, unit_labels.UnitLabels]) -> str:
    return "the user" if actor.startswith("user:") else _session_label(actor, labels)


def move_text(records: tuple[BumpRecord, ...], zone: ZoneInfo,
              labels: Mapping[str, unit_labels.UnitLabels]) -> str:
    return "; ".join(f"from #{record.old_position} by {actor_text(record.actor, labels)} at "
                     + clock(record.at, zone, seconds=False) for record in records)


def known_head_waits(facts: HeadFacts) -> tuple[CargoTokenWait, ...] | TokenWaitsNotRecorded:
    waits = facts.token_waits
    if isinstance(waits, TokenWaitsNotRecorded) or waits.has_unknown:
        return TokenWaitsNotRecorded()
    return waits.head_waits


def head_token_text(facts: HeadFacts, zone: ZoneInfo) -> str:
    waits = known_head_waits(facts)
    if isinstance(waits, TokenWaitsNotRecorded) or not waits:
        return ""
    return "cargo token: " + token_wait_text(waits, zone)


def token_wait_seconds(waits: Sequence[CargoTokenWait]) -> int:
    return int(sum(wait.returned.at - wait.left_at for wait in waits))


def token_wait_text(waits: Sequence[CargoTokenWait], zone: ZoneInfo) -> str:
    spans = ", ".join(clock(wait.left_at, zone, seconds=False) + "-"
                      + clock(wait.returned.at, zone, seconds=False) for wait in waits)
    return f"waited {token_wait_seconds(waits)} s ({spans})"
