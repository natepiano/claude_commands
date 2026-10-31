#!/usr/bin/env python3
"""Read natedev's hierarchy records without changing the live process.

Counts describe recorded work, not proof that commits reached Hana main. Windows
are half open. Date bounds use the user's zone; a date-only --until includes that
whole day. A date-time --until is the excluded end.
The baseline and compare commands print short summaries. Compare takes two
START..END windows; date-only END includes that day. Add --full to print the tables.
"""
from __future__ import annotations

import argparse
from contextlib import closing, redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import io
from functools import lru_cache
from itertools import groupby
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from statistics import median
import sys
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for directory in ("lib", "production", "message", "direct", "nightly_review"):
    sys.path.insert(0, str(SCRIPTS / directory))

from user_zone import user_zone
from scripts.accounts.role_map import stored_role_map
from scripts.hierarchies import log_records, push_audit
from scripts.hierarchies.log_records import EventSource as EventSource
from scripts.lib import unit_runs
from scripts.message import roster
from scripts.production import showrunners, production_units, issues, change_review, dailies_render
from scripts.production import unit_lookup
from scripts.production.proposal_issue import proposal_progress, AwaitingApproval
from scripts.production.proposals_table import backlog_rows, REVIEW_STATE
from scripts.nightly_review import candidates
from scripts.usage import collect, usage_report


class DurationNotRecorded(Enum):
    MISSING = "duration not recorded"


class ScoreNotRecorded(Enum):
    MISSING = "score not recorded"


class MergeHistoryNotRecorded(Enum):
    MISSING = "merge history not recorded"


class UnitActivity(Enum):
    LIVE = "running phase or live session"
    INACTIVE = "no running phase or live session"
    UNKNOWN = "unit activity not recorded"


@dataclass(frozen=True)
class Unit:
    production: str
    name: str
    repository: str
    worktree: str
    hana: bool
    issue: str
    showrunner: str = ""
    activity: UnitActivity = UnitActivity.INACTIVE


@dataclass(frozen=True)
class Worker:
    worktree: str
    hana: bool
    director_running: bool


@dataclass(frozen=True)
class WaitingItem:
    kind: str
    opened: log_records.RecordedTime


@dataclass(frozen=True)
class BuildStep:
    repository: str
    step: str
    started: datetime
    seconds: float


@dataclass(frozen=True)
class WindowTokenUsage:
    window: log_records.Window
    repository: str
    tokens: float
    turns: int
    events: tuple[log_records.TimedAmount, ...] = ()
    population: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class RecordedOrigin:
    value: str


@dataclass(frozen=True)
class OriginNotRecorded:
    reason: str


@dataclass(frozen=True)
class PhaseOrigin:
    working_dir: str
    plan: RecordedOrigin | OriginNotRecorded
    production: RecordedOrigin | OriginNotRecorded
    repository: RecordedOrigin | OriginNotRecorded


class SenderKind(Enum):
    AGENT = "agent"
    AUTOMATION = "automation"
    USER_BOUND = "user-bound"


@dataclass(frozen=True)
class AgentMessage:
    at: datetime
    kind: SenderKind


@dataclass(frozen=True)
class IssueRank:
    name: str
    rank: float
    score: float | ScoreNotRecorded = ScoreNotRecorded.MISSING


@dataclass(frozen=True)
class HanaMerge:
    commit: str
    at: datetime


@dataclass(frozen=True)
class HanaTrunkMerges:
    merges: tuple[HanaMerge, ...]
    source: str


class WaitKind(Enum):
    QUOTA = "WaitingForQuotaRefusal"
    CAPACITY = "WaitingForMemoryOrSlot"
    MERGE = "WaitingForMergeGate"
    USER = "WaitingForUser"
    SUPPLIER = "WaitingForSupplier"
    READY = "ReadyWithoutCapacity"


@dataclass(frozen=True)
class HistoryNotRecorded:
    reason: str = "wait history not recorded"


@dataclass(frozen=True)
class WaitKindNotRecorded:
    reason: str


@dataclass(frozen=True)
class CompletedWait:
    at: datetime


@dataclass(frozen=True)
class UnknownEndedWait:
    state: str


@dataclass(frozen=True)
class WaitCandidate:
    run: str
    phase: str
    instance: str
    working_dir: str

    @property
    def label(self) -> str:
        return f"run={self.run}; phase={self.phase}; instance={self.instance}; cwd={self.working_dir or 'not recorded'}"


@dataclass(frozen=True)
class CoveredWaitHistory:
    wait_n: int


@dataclass(frozen=True)
class UncertainWaitCoverage:
    reason: str
    candidates: tuple[WaitCandidate, ...]


@dataclass(frozen=True)
class PhaseWaitCoverage:
    run: str
    instance: str
    state: CoveredWaitHistory | HistoryNotRecorded | UncertainWaitCoverage


@dataclass(frozen=True)
class WaitPhaseInstance:
    run: str
    identity: str

    def __post_init__(self) -> None:
        if not self.run or not self.identity:
            raise ValueError("a wait phase instance needs its run and identity")


@dataclass(frozen=True)
class ProductionHandoff:
    production: str

    def __post_init__(self) -> None:
        if not self.production:
            raise ValueError("a handoff needs its production")


@dataclass(frozen=True)
class WaitPhaseNotRecorded:
    reason: str


@dataclass(frozen=True)
class PhaseCompletion:
    identity: str
    at: datetime
    hana: bool
    seconds: float | DurationNotRecorded
    phase: WaitPhaseInstance | WaitPhaseNotRecorded
    started: log_records.RecordedTime


@dataclass(frozen=True)
class RecordedWaitOwner:
    session: str

    def __post_init__(self) -> None:
        if not self.session:
            raise ValueError("a recorded wait owner needs its session")


@dataclass(frozen=True)
class WaitOwnerNotRecorded:
    reason: str


@dataclass(frozen=True)
class NamedSupplier:
    address: str

    def __post_init__(self) -> None:
        if not self.address:
            raise ValueError("a named supplier needs its address or name")


@dataclass(frozen=True)
class NoNamedSupplier:
    reason: str


@dataclass(frozen=True)
class RecordedWaitReason:
    text: str


@dataclass(frozen=True)
class WaitReasonNotRecorded:
    reason: str


@dataclass(frozen=True)
class SessionHolder:
    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("holder session must not be blank")


@dataclass(frozen=True)
class UnitHolder:
    unit: str

    def __post_init__(self) -> None:
        if not self.unit.strip():
            raise ValueError("holder unit must not be blank")


@dataclass(frozen=True)
class HolderNotNamed:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("unnamed holder needs a reason")


@dataclass(frozen=True)
class MalformedHolder:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("malformed holder needs a reason")


class HolderResourceKind(Enum):
    ACCOUNT = "account"
    SLOT = "slot"
    MEMORY = "memory"


@dataclass(frozen=True)
class NamedHolder:
    resource_kind: HolderResourceKind
    resource: str
    holder: SessionHolder | UnitHolder

    def __post_init__(self) -> None:
        if not self.resource.strip():
            raise ValueError("named holder needs a resource")


@dataclass(frozen=True)
class HolderObservation:
    at: datetime
    holder: NamedHolder | HolderNotNamed | MalformedHolder


@dataclass(frozen=True)
class HolderHistory:
    observations: tuple[HolderObservation, ...]


@dataclass(frozen=True)
class HolderInterval:
    observed: datetime
    started: datetime
    end: CompletedWait | UnknownEndedWait
    holder: NamedHolder | HolderNotNamed | MalformedHolder


class HolderCoverage(Enum):
    WHOLE = "whole wait"
    PART = "part of wait"
    UNNAMED = "HolderNotNamed"


@dataclass(frozen=True)
class UnresolvedPhaseOrigin:
    run: str
    instance: str
    reason: str


@dataclass(frozen=True)
class HanaWait:
    identity: str
    kind: WaitKind | WaitKindNotRecorded
    started: datetime
    end: CompletedWait | UnknownEndedWait
    owner: RecordedWaitOwner | WaitOwnerNotRecorded
    phase: WaitPhaseInstance | ProductionHandoff | WaitPhaseNotRecorded
    expected_supplier: NamedSupplier | NoNamedSupplier
    reason: RecordedWaitReason | WaitReasonNotRecorded
    holders: HolderHistory

    @property
    def production_handoff(self) -> bool:
        return isinstance(self.phase, ProductionHandoff)


@dataclass(frozen=True)
class AttributionMiss:
    wait_id: str
    reason: str
    candidates: tuple[WaitCandidate, ...]


@dataclass(frozen=True)
class HanaWaitHistory:
    waits: tuple[HanaWait, ...]
    phases: tuple[PhaseWaitCoverage, ...]
    attribution_misses: tuple[AttributionMiss, ...]
    unresolved_origins: tuple[UnresolvedPhaseOrigin, ...] = ()
    problems: tuple[log_records.SourceProblem, ...] = ()


@dataclass(frozen=True)
class MergeReceipt:
    project: str
    issue: str
    phase: str
    commit: str
    checkpoint: str
    target_branch: str
    merged_at: datetime


@dataclass(frozen=True)
class ReceiptMissing:
    commit: str


@dataclass
class BaselineSources:
    units: list[Unit]
    workers: list[Worker]
    builds: list[BuildStep]
    tokens: list[WindowTokenUsage]
    phases: list[PhaseCompletion]
    push_report: push_audit.AuditReport
    messages: list[AgentMessage]
    issue_records: list[issues.Issue]
    rankings: list[IssueRank]
    waiting: list[WaitingItem]
    problems: list[log_records.SourceProblem]
    merges: HanaTrunkMerges | MergeHistoryNotRecorded = MergeHistoryNotRecorded.MISSING
    wait_history: HanaWaitHistory | HistoryNotRecorded = HistoryNotRecorded()
    receipts: tuple[MergeReceipt, ...] | HistoryNotRecorded = HistoryNotRecorded("merge receipts not recorded")


def json_lines[T](path: Path, parse: Callable[[log_records.JsonObject], T]) -> list[T]:
    records: list[T] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = cast(object, json.loads(line))
            records.append(parse(log_records.object_record(value)))
        except ValueError:
            bad += 1
    if bad:
        raise ValueError(f"unreadable: {bad} invalid JSON or field rows; counts withheld")
    return records


def parse_rank(record: log_records.JsonObject) -> IssueRank:
    score = log_records.field_number(record, "score") if "score" in record else ScoreNotRecorded.MISSING
    return IssueRank(log_records.field_text(record, "name"), log_records.field_number(record, "rank"), score)


AUTOMATION = re.compile(
    r"(?:^|[-_ :/])(timer|notifier|shutdown|stall|watch|quota|conversation-pause|automation|hook|cron)(?:$|[-_ :/])"
)


def parse_message(record: log_records.JsonObject) -> AgentMessage:
    target = log_records.field_text(record, "to")
    sender = log_records.field_text(record, "from")
    if target == "user":
        return AgentMessage(log_records.record_time(record), SenderKind.USER_BOUND)
    # Explicit role/route metadata wins over the legacy sender-name fallback.
    for key in ("sender_kind", "kind", "route"):
        if key not in record:
            continue
        value = record[key]
        if isinstance(value, dict):
            route = log_records.field_text(log_records.object_record(record[key]), "kind")
        else:
            route = log_records.field_text(record, key)
        if route in ("user", "user-bound", "push", "phone"):
            kind = SenderKind.USER_BOUND
        elif route in ("automation", "timer", "hook", "cron", "notifier"):
            kind = SenderKind.AUTOMATION
        elif route in ("agent", "session", "worker", "codex", "claude", "relay", "queue", "postmaster"):
            kind = SenderKind.AGENT
        else:
            raise ValueError(f"{key}: unknown message kind or route {route!r}")
        return AgentMessage(log_records.record_time(record), kind)
    kind = SenderKind.AUTOMATION if AUTOMATION.search(sender) else SenderKind.AGENT
    return AgentMessage(log_records.record_time(record), kind)


@lru_cache(maxsize=4096)
def is_hana(path: str) -> bool:
    if not path:
        return False
    expanded = Path(path).expanduser()
    root = Path.home() / "rust"
    if expanded.parent == root and (expanded.name == "hana_catalyst" or expanded.name.startswith("hana_catalyst-")):
        return True
    # Git worktrees can have names such as tool-based-ui-demo. The common
    # repository, rather than the folder name, makes these Hana worktrees.
    repo = repository(path)
    return repo != "cwd has no repository" and repo in (str(root / "hana"), str(root / "hana_catalyst"))


def repository(path: str) -> str:
    """Use Git's local common directory; never start Git or read transcripts."""
    folder = Path(path).expanduser()
    for candidate in (folder, *folder.parents):
        marker = candidate / ".git"
        if marker.is_file():
            target = marker.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
            gitdir = (candidate / target).resolve()
            common_file = gitdir / "commondir"
            common = gitdir
            if common_file.exists():
                common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve()
            return str(common.parent)
        if marker.is_dir():
            return str(candidate)
    return "cwd has no repository"


def capacity(units: Sequence[Unit], workers: Sequence[Worker], snapshot: str) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    counts = Counter((unit.production, unit.repository, unit.hana) for unit in units)
    for (production, repo, hana), count in sorted(counts.items()):
        scope = "Hana" if hana else "other"
        rows.append(log_records.MeasureRow(
            f"units / {production} / {repo} / {scope}", str(count), count, snapshot,
            "showrunners + units_table production_units loader", quantity=log_records.Snapshot(count),
        event_source=log_records.SourceNotApplicable.STATE))
    for tree in sorted({worker.worktree for worker in workers}):
        group = [worker for worker in workers if worker.worktree == tree]
        orphan = sum(not worker.director_running for worker in group)
        scope = "Hana" if group[0].hana else "other"
        worktree = tree or "worktree not recorded"
        if not tree:
            scope = "scope not recorded"
        rows.append(log_records.MeasureRow(
            f"workers / {worktree} / {scope}", f"{len(group)}; director not running {orphan}",
            len(group), snapshot, "codex_mesh via session roster; natedev only", quantity=log_records.Snapshot(len(group)),
        event_source=log_records.SourceNotApplicable.STATE))
    if not units:
        rows.append(log_records.MeasureRow("live units", "0", 0, snapshot, "showrunners + units_table", quantity=log_records.Snapshot(0), event_source=log_records.SourceNotApplicable.STATE))
    if not workers:
        rows.append(log_records.MeasureRow("workers", "0", 0, snapshot, "session roster; natedev only", quantity=log_records.Snapshot(0), event_source=log_records.SourceNotApplicable.STATE))
    return rows


def resource_use(builds: Sequence[BuildStep], tokens: Sequence[WindowTokenUsage], window: log_records.Window) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    groups: dict[tuple[str, str], list[BuildStep]] = defaultdict(list)
    for build in builds:
        if window.contains(build.started):
            lane = "test" if build.step in ("test", "nextest", "example-test") else "build/check"
            groups[(build.repository, lane)].append(build)
    for (repo, lane), values in sorted(groups.items()):
        rows.append(log_records.MeasureRow(
            f"{lane} minutes / {repo}", f"{sum(build.seconds / 60 for build in values):.1f}", len(values), window.label,
            "buildlog index steps; natedev only; by start time",
            quantity=log_records.EventValue(sum(build.seconds / 60 for build in values), " min",
                tuple(log_records.TimedAmount(build.started, build.seconds / 60) for build in values),
                tuple(build.started for build in values)),
        event_source=EventSource.BUILDS))
    totals: dict[str, float] = defaultdict(float)
    turn_counts: Counter[str] = Counter()
    for turn in tokens:
        if turn.window == window:
            totals[turn.repository] += turn.tokens
            turn_counts[turn.repository] += turn.turns
    for repo, total in sorted(totals.items()):
        rows.append(log_records.MeasureRow(
            f"tokens / {repo}", f"{total:,.0f}", turn_counts[repo], window.label,
            "usage ledger; input + cache writes + cache reads + output; natedev only",
            quantity=log_records.EventValue(total, " tokens",
                tuple(event for turn in tokens if turn.repository == repo and turn.window == window for event in turn.events),
                tuple(at for turn in tokens if turn.repository == repo and turn.window == window for at in turn.population)),
        event_source=EventSource.TOKENS))
    if not groups:
        rows.append(log_records.MeasureRow("build/test minutes", "no records in window", 0, window.label, "buildlog index; natedev only", quantity=log_records.EventValue(0, " min"), event_source=EventSource.BUILDS))
    if not totals:
        rows.append(log_records.MeasureRow("tokens", "no records in window", 0, window.label, "usage ledger; natedev only", quantity=log_records.EventValue(0, " tokens"), event_source=EventSource.TOKENS))
    return rows


def distribution(name: str, values: Sequence[float], window: log_records.Window, source: str) -> log_records.MeasureRow:
    ordered = sorted(values)
    value = "not recorded"
    if ordered:
        p90 = ordered[math.ceil(.9 * len(ordered)) - 1]
        value = f"median {median(ordered):.1f} min; p90 {p90:.1f} min"
    return log_records.MeasureRow(name, value, len(ordered), window.label, source,
        quantity=log_records.Statistic(tuple(ordered)))


def object_rows(record: log_records.JsonObject, key: str) -> list[log_records.JsonObject]:
    value = record.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key}: expected an array")
    return [log_records.object_record(item) for item in cast(list[object], value)]


def wait_candidates(record: log_records.JsonObject) -> tuple[WaitCandidate, ...]:
    return tuple(WaitCandidate(log_records.field_text(item, "run_id"), log_records.field_text(item, "phase_id"),
                               log_records.field_text(item, "phase_instance_id"), log_records.field_text(item, "working_dir"))
                 for item in object_rows(record, "candidates"))


def origin_field(record: log_records.JsonObject, key: str) -> RecordedOrigin | OriginNotRecorded:
    """Read tagged origin metadata and the text stored in earlier history files."""
    if key not in record:
        return OriginNotRecorded(f"{key} not recorded")
    value = record[key]
    if isinstance(value, str):
        return RecordedOrigin(value) if value.strip() else OriginNotRecorded(f"{key} not recorded")
    fields = log_records.object_record(value)
    state = log_records.field_text(fields, "state")
    if state == "Recorded":
        text = log_records.field_text(fields, "value")
        if text.strip():
            return RecordedOrigin(text)
    elif state == "NotRecorded":
        reason = log_records.field_text(fields, "reason")
        if reason.strip():
            return OriginNotRecorded(reason)
    raise ValueError(f"{key}: expected Recorded value or NotRecorded reason")


def wait_origin(record: log_records.JsonObject, prefixes: Sequence[str]) -> bool:
    return hana_origin(PhaseOrigin(
        log_records.field_text(record, "working_dir") or log_records.field_text(record, "unit"),
        origin_field(record, "plan_doc"), origin_field(record, "production"),
        origin_field(record, "repository")), prefixes)


def parse_holder_history(record: log_records.JsonObject, started: datetime) -> HolderHistory:
    observations: list[HolderObservation] = []
    for item in object_rows(record, "holders"):
        if set(item) - {"at", "epoch", "holder"}:
            raise ValueError("unknown holder observation field")
        at = log_records.required_time(item.get("epoch", item.get("at")))
        fields = log_records.object_record(item.get("holder"))
        state = log_records.field_text(fields, "state")
        holder: NamedHolder | HolderNotNamed | MalformedHolder
        if state == "HolderNotNamed":
            if set(fields) - {"state", "reason"}:
                raise ValueError("unknown unnamed holder field")
            reason = log_records.field_text(fields, "reason")
            holder = MalformedHolder(reason) if reason.startswith("invalid holder record:") else HolderNotNamed(reason)
        elif state == "MalformedHolder":
            if set(fields) - {"state", "reason", "raw_value"}:
                raise ValueError("unknown malformed holder field")
            holder = MalformedHolder(log_records.field_text(fields, "reason"))
        elif state == "NamedHolder":
            if set(fields) - {"state", "resource_kind", "resource", "holder"}:
                raise ValueError("unknown named holder field")
            owner = log_records.object_record(fields.get("holder"))
            owner_state = log_records.field_text(owner, "state")
            identity: SessionHolder | UnitHolder
            if owner_state == "SessionHolder" and not set(owner) - {"state", "session_id"}:
                identity = SessionHolder(log_records.field_text(owner, "session_id"))
            elif owner_state == "UnitHolder" and not set(owner) - {"state", "unit"}:
                identity = UnitHolder(log_records.field_text(owner, "unit"))
            else:
                raise ValueError("holder must name a session or unit")
            holder = NamedHolder(HolderResourceKind(log_records.field_text(fields, "resource_kind")),
                                 log_records.field_text(fields, "resource"), identity)
        else:
            raise ValueError(f"unknown holder state: {state!r}")
        observations.append(HolderObservation(at, holder))
    observations.sort(key=lambda observation: observation.at)
    if not observations:
        observations.append(HolderObservation(started, HolderNotNamed("recorded before holders")))
    elif observations[0].at > started:
        observations.insert(0, HolderObservation(started, HolderNotNamed("holder not recorded at wait start")))
    return HolderHistory(tuple(observations))


def parse_wait_report(record: log_records.JsonObject, prefixes: Sequence[str] = ()) -> HanaWaitHistory:
    phases: list[PhaseWaitCoverage] = []
    hana_phases: set[tuple[str, str]] = set()
    unresolved: list[UnresolvedPhaseOrigin] = []
    problems: list[log_records.SourceProblem] = []
    for phase in object_rows(record, "phases"):
        try:
            run, instance = log_records.field_text(phase, "run_id"), log_records.field_text(phase, "phase_instance_id")
            belongs_to_hana = wait_origin(phase, prefixes)
            origins = tuple(origin_field(phase, key) for key in ("plan_doc", "production", "repository"))
        except ValueError as error:
            problems.append(log_records.SourceProblem(
                f"phase {phase.get('run_id')!r}/{phase.get('phase_instance_id')!r}", str(error), event_source=EventSource.WAITS, kind=log_records.RowsSkipped(1)))
            continue
        if not belongs_to_hana:
            if all(isinstance(origin, OriginNotRecorded) for origin in origins):
                reason = "; ".join(origin.reason for origin in origins if isinstance(origin, OriginNotRecorded))
                unresolved.append(UnresolvedPhaseOrigin(run, instance, reason))
            continue
        try:
            state = log_records.field_text(phase, "state")
            reason = log_records.field_text(phase, "reason")
            coverage: CoveredWaitHistory | HistoryNotRecorded | UncertainWaitCoverage
            if state == "Covered":
                count = log_records.field_number(phase, "wait_n")
                if count < 0 or not count.is_integer():
                    raise ValueError("wait_n: expected a nonnegative integer")
                coverage = CoveredWaitHistory(int(count))
            elif state == "HistoryNotRecorded":
                coverage = HistoryNotRecorded(reason or "phase start has no wait coverage record")
            elif state == "UncertainCoverage":
                if "wait_n" in phase:
                    raise ValueError("UncertainCoverage cannot carry wait_n")
                coverage = UncertainWaitCoverage(reason, wait_candidates(phase))
            else:
                raise ValueError(f"unknown phase wait coverage: {state!r}")
            phases.append(PhaseWaitCoverage(run, instance, coverage))
            hana_phases.add((run, instance))
        except ValueError as error:
            problems.append(log_records.SourceProblem(f"phase {run}/{instance}", str(error), event_source=EventSource.WAITS, kind=log_records.RowsSkipped(1)))
    waits: dict[str, HanaWait] = {}
    misses: dict[str, AttributionMiss] = {}
    for item in (*object_rows(record, "waits"), *object_rows(record, "missing")):
        try:
            attribution = log_records.object_record(item.get("wait_attribution", {}))
            candidates = wait_candidates(attribution)
            owns_phase = (log_records.field_text(item, "run_id"), log_records.field_text(item, "phase_instance_id")) in hana_phases
            candidate_hana = any(wait_origin({"working_dir": candidate.working_dir}, prefixes)
                                 or (candidate.run, candidate.instance) in hana_phases for candidate in candidates)
            belongs_to_hana = wait_origin(item, prefixes)
            if not (owns_phase or belongs_to_hana or candidate_hana):
                continue
            identity = log_records.field_text(item, "wait_id")
            if not identity:
                raise ValueError("wait_id not recorded")
            if log_records.field_text(attribution, "state") in ("UnresolvedAttribution", "UnresolvedSupplier"):
                misses[identity] = AttributionMiss(identity, log_records.field_text(attribution, "reason"), candidates)
                # A candidate Hana phase does not establish that this was a Hana wait.
                continue
            kind_text = log_records.field_text(item, "wait_kind")
            kind: WaitKind | WaitKindNotRecorded
            if kind_text:
                try:
                    kind = WaitKind(kind_text)
                except ValueError as error:
                    raise ValueError(f"unknown wait kind: {kind_text}") from error
            else:
                kind = WaitKindNotRecorded(log_records.field_text(item, "wait_reason") or "wait kind not recorded")
            started = log_records.required_time(item.get("wait_started_epoch", item.get("wait_started_at")))
            end_record = log_records.object_record(item.get("wait_end", {}))
            end_state = log_records.field_text(end_record, "state")
            end: CompletedWait | UnknownEndedWait
            if end_state == "Finished":
                end_at = log_records.required_time(end_record.get("epoch", end_record.get("at")))
                if end_at < started:
                    raise ValueError("wait ends before its start")
                end = CompletedWait(end_at)
            elif end_state in ("Open", "Unclosed", "NotRecorded"):
                end = UnknownEndedWait(end_state)
            else:
                raise ValueError(f"unknown wait end state: {end_state!r}")
            owner_session = log_records.field_text(item, "owner_session")
            missing_record = log_records.object_record(item.get("wait_record_state", {}))
            missing_reason = log_records.field_text(missing_record, "reason")
            owner: RecordedWaitOwner | WaitOwnerNotRecorded = (
                RecordedWaitOwner(owner_session) if owner_session else
                WaitOwnerNotRecorded(missing_reason or "wait owner session not recorded")
            )
            phase_owner: WaitPhaseInstance | ProductionHandoff | WaitPhaseNotRecorded
            run, instance = log_records.field_text(item, "run_id"), log_records.field_text(item, "phase_instance_id")
            if log_records.field_text(attribution, "state") == "ProductionHandoff":
                phase_owner = ProductionHandoff(log_records.field_text(item, "production"))
            elif run and instance:
                phase_owner = WaitPhaseInstance(run, instance)
            else:
                phase_owner = WaitPhaseNotRecorded(missing_reason or "wait phase instance not recorded")
            supplier_record = log_records.object_record(item.get("expected_supplier", {}))
            supplier_state = log_records.field_text(supplier_record, "state")
            supplier: NamedSupplier | NoNamedSupplier
            if supplier_state == "NamedSupplier":
                supplier = NamedSupplier(log_records.field_text(supplier_record, "address"))
            elif supplier_state in ("NoNamedSupplier", ""):
                supplier = NoNamedSupplier(log_records.field_text(supplier_record, "reason") or "expected supplier not recorded")
            else:
                raise ValueError(f"unknown expected supplier state: {supplier_state!r}")
            reason_text = log_records.field_text(item, "wait_reason")
            wait_reason: RecordedWaitReason | WaitReasonNotRecorded = (
                RecordedWaitReason(reason_text) if reason_text else WaitReasonNotRecorded("wait reason not recorded")
            )
            holders = parse_holder_history(item, started)
            waits[identity] = HanaWait(identity, kind, started, end, owner, phase_owner, supplier, wait_reason, holders)
        except ValueError as error:
            problems.append(log_records.SourceProblem("wait " + log_records.field_text(item, "wait_id"), str(error), event_source=EventSource.WAITS, kind=log_records.RowsSkipped(1)))
    without_id = log_records.field_number(record, "wait_without_id_n")
    if without_id:
        problems.append(log_records.SourceProblem("progress_history waits", f"{int(without_id)} wait events without wait_id; wait_without_id_n={int(without_id)}", event_source=EventSource.WAITS, kind=log_records.RowsSkipped(int(without_id))))
    return HanaWaitHistory(tuple(waits.values()), tuple(phases), tuple(misses.values()), tuple(unresolved), tuple(problems))


def read_wait_history(
    root: Path, window: log_records.Window, problems: list[log_records.SourceProblem], prefixes: Sequence[str],
    phases: Sequence[PhaseCompletion],
) -> HanaWaitHistory | HistoryNotRecorded:
    external = root / "wait_attribution.jsonl"
    rejected: list[log_records.SourceProblem] = []
    if not (root / "runs").is_dir() and not external.is_file():
        raise FileNotFoundError(root / "runs")
    if external.exists():
        # The supplier reader skips corrupt external rows without a diagnostic.
        external_records = json_lines(external, log_records.object_record)
        if any(log_records.field_text(item, "event_type") not in
               ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded", "wait_holder_seen") for item in external_records):
            raise ValueError("external wait history contains an unknown event type")
        for index, item in enumerate(external_records, 1):
            if not log_records.field_text(item, "wait_id"):
                rejected.append(log_records.SourceProblem(f"{external}:row {index}", "wait event without wait_id", event_source=EventSource.WAITS, kind=log_records.RowsSkipped(1)))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "direct/progress_history.py"), "waits",
         "--since", window.since.isoformat(), "--until", window.until.isoformat()],
        env={**os.environ, "UNIT_HISTORY_DIR": str(root)}, capture_output=True, text=True, check=True, timeout=60,
    )
    report = log_records.object_record(cast(object, json.loads(result.stdout)))
    selected = completed_hana_phases(phases, window)
    earlier = [phase.started for phase in selected if isinstance(phase.started, datetime) and phase.started < window.since]
    if earlier:
        previous = subprocess.run(
            [sys.executable, str(SCRIPTS / "direct/progress_history.py"), "waits",
             "--since", min(earlier).isoformat(), "--until", window.until.isoformat()],
            env={**os.environ, "UNIT_HISTORY_DIR": str(root)}, capture_output=True, text=True, check=True, timeout=60,
        )
        previous_report = log_records.object_record(cast(object, json.loads(previous.stdout)))
        identities = {(phase.phase.run, phase.phase.identity) for phase in selected
                      if isinstance(phase.phase, WaitPhaseInstance)
                      and isinstance(phase.started, datetime) and phase.started < window.since}
        for key in ("phases", "waits", "missing"):
            records = object_rows(previous_report, key)
            matched = [item for item in records if (item.get("run_id"), item.get("phase_instance_id")) in identities]
            current = [item for item in object_rows(report, key)
                       if (item.get("run_id"), item.get("phase_instance_id")) not in identities]
            report[key] = [*current, *matched]
        # Both supplier windows read the same files; these diagnostics are global.
        report["ignored_n"] = max(log_records.field_number(report, "ignored_n"),
                                  log_records.field_number(previous_report, "ignored_n"))
        report["wait_without_id_n"] = max(log_records.field_number(report, "wait_without_id_n"),
                                          log_records.field_number(previous_report, "wait_without_id_n"))
    ignored = log_records.field_number(report, "ignored_n")
    if ignored:
        rejected.append(log_records.SourceProblem("progress_history waits", f"{int(ignored)} unreadable or unsupported history rows; counts can be incomplete", event_source=EventSource.WAITS, kind=log_records.RowsSkipped(int(ignored))))
    history = parse_wait_report(report, prefixes)
    history = HanaWaitHistory(history.waits, history.phases, history.attribution_misses, history.unresolved_origins,
                              (*history.problems, *rejected))
    problems.extend(history.problems)
    return history


def wait_coverage_counts(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window,
    phases: Sequence[PhaseCompletion],
) -> dict[str, int]:
    if isinstance(history, HistoryNotRecorded):
        return {}
    selected = {phase.phase for phase in completed_hana_phases(phases, window)
                if isinstance(phase.phase, WaitPhaseInstance)}
    counts = {
        "HistoryNotRecorded": sum(isinstance(phase.state, HistoryNotRecorded) for phase in history.phases),
        "UncertainCoverage": sum(isinstance(phase.state, UncertainWaitCoverage) for phase in history.phases),
        "WaitKindNotRecorded": sum(isinstance(wait.kind, WaitKindNotRecorded) for wait in history.waits
                                   if window.contains(wait.started) or isinstance(wait.phase, WaitPhaseInstance) and wait.phase in selected),
        "attribution_miss_n": len(history.attribution_misses),
        "source_problem_n": len(history.problems),
        "unresolved_origin_n": len(history.unresolved_origins),
    }
    return {name: count for name, count in counts.items() if count}


def wait_coverage_rows(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window, table: str,
    phases: Sequence[PhaseCompletion],
) -> list[log_records.MeasureRow]:
    source = "progress_history.py waits; incomplete coverage counts"
    if isinstance(history, HistoryNotRecorded):
        return [log_records.MeasureRow(table + " / coverage / HistoryNotRecorded", "not recorded (HistoryNotRecorded)",
                                       0, window.label, source + "; " + history.reason, quantity=log_records.NotRecorded(history.reason), event_source=EventSource.WAITS)]
    return [log_records.MeasureRow(f"{table} / coverage / {name}", str(count), count, window.label, source, quantity=log_records.Diagnostic(count), event_source=EventSource.WAITS)
            for name, count in wait_coverage_counts(history, window, phases).items()]


def partial_wait_total(value: str, partial: bool) -> str:
    return value + " (partial)" if partial and not value.startswith("not recorded") else value


def holder_intervals(wait: HanaWait) -> tuple[HolderInterval, ...]:
    observations = wait.holders.observations
    intervals: list[HolderInterval] = []
    for index, observation in enumerate(observations):
        started = max(observation.at, wait.started)
        if isinstance(wait.end, CompletedWait) and started > wait.end.at:
            continue
        end: CompletedWait | UnknownEndedWait = wait.end
        if index + 1 < len(observations):
            next_at = observations[index + 1].at
            if isinstance(end, UnknownEndedWait) or next_at < end.at:
                end = CompletedWait(next_at)
        if isinstance(end, CompletedWait) and end.at < started:
            continue
        intervals.append(HolderInterval(observation.at, started, end, observation.holder))
    return tuple(intervals)


def holder_coverage(wait: HanaWait) -> HolderCoverage:
    intervals = tuple(interval for interval in holder_intervals(wait)
                      if isinstance(interval.end, UnknownEndedWait) or interval.end.at > interval.started)
    named = any(isinstance(interval.holder, NamedHolder) for interval in intervals)
    unnamed = any(isinstance(interval.holder, (HolderNotNamed, MalformedHolder)) for interval in intervals)
    if not named:
        return HolderCoverage.UNNAMED
    if unnamed or isinstance(wait.end, UnknownEndedWait):
        return HolderCoverage.PART
    return HolderCoverage.WHOLE


def holder_wait_duration(waits: Sequence[HanaWait], complete_coverage: bool) -> str:
    completed = [wait for wait in waits if isinstance(wait.end, CompletedWait)]
    minutes = sum((wait.end.at - wait.started).total_seconds() / 60
                  for wait in completed if isinstance(wait.end, CompletedWait))
    open_n = sum(isinstance(wait.end, UnknownEndedWait) for wait in waits)
    if open_n:
        finished = f"{minutes:.1f} min; {len(completed)} done; " if completed else ""
        return finished + f"{open_n} open, duration unknown"
    return f"{minutes:.1f} min" if completed or complete_coverage else "not recorded (HistoryNotRecorded)"


def wait_quantity(waits: Sequence[HanaWait], complete: bool, partial: bool = False) -> log_records.Quantity:
    if any(isinstance(wait.end, UnknownEndedWait) for wait in waits):
        return log_records.NotRecorded("duration unknown for open waits")
    if not waits and not complete:
        return log_records.NotRecorded("HistoryNotRecorded")
    events = tuple(log_records.TimedAmount(wait.started, (wait.end.at - wait.started).total_seconds() / 60)
                   for wait in waits if isinstance(wait.end, CompletedWait))
    return log_records.EventValue(sum(event.amount for event in events), " min", events,
                                 tuple(wait.started for wait in waits), wait_rate=True, partial=partial)


def holder_rows(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window,
    completions: Sequence[PhaseCompletion],
) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    waits = [wait for wait in history.waits if window.contains(wait.started)] if isinstance(history, HanaWaitHistory) else []
    source = "progress_history.py waits holders; total completed wait minutes by holder coverage; unknown ends excluded"
    recorded = isinstance(history, HanaWaitHistory) and any(isinstance(phase.state, CoveredWaitHistory) for phase in history.phases)
    partial = bool(wait_coverage_counts(history, window, completions))
    complete_coverage = recorded and not partial
    rows.extend(wait_coverage_rows(history, window, "Hana wait holders", completions))
    for kind in WaitKind:
        for coverage in HolderCoverage:
            selected = [wait for wait in waits if wait.kind == kind and holder_coverage(wait) == coverage]
            rows.append(log_records.MeasureRow(f"Hana wait holders / {kind.value} / {coverage.value}",
                                               partial_wait_total(holder_wait_duration(selected, complete_coverage), partial), len(selected), window.label, source, quantity=wait_quantity(selected, complete_coverage, partial), event_source=EventSource.WAITS))
    unnamed_groups: dict[tuple[str, str], list[HanaWait]] = {}
    for wait in waits:
        observations = wait.holders.observations
        if observations and all(observation.holder == observations[0].holder for observation in observations):
            holder = observations[0].holder
            if isinstance(holder, (HolderNotNamed, MalformedHolder)):
                reason = ("MalformedHolder: " if isinstance(holder, MalformedHolder) else "") + holder.reason
                kind_label = wait.kind.value if isinstance(wait.kind, WaitKind) else "WaitKindNotRecorded"
                unnamed_groups.setdefault((kind_label, reason), []).append(wait)
                continue
        for interval in holder_intervals(wait):
            holder = interval.holder
            if isinstance(holder, NamedHolder):
                owner = ("session:" + holder.holder.session_id if isinstance(holder.holder, SessionHolder)
                         else "unit:" + holder.holder.unit)
                detail = f"{holder.resource_kind.value}:{holder.resource}; {owner}"
            else:
                detail = type(holder).__name__ + ": " + holder.reason
            start = interval.started.astimezone(timezone.utc).isoformat()
            observed = interval.observed.astimezone(timezone.utc).isoformat()
            if isinstance(interval.end, CompletedWait):
                end = interval.end.at.astimezone(timezone.utc).isoformat()
                duration = f"{(interval.end.at - interval.started).total_seconds() / 60:.1f} min"
            else:
                end, duration = "not recorded", "duration unknown"
            rows.append(log_records.MeasureRow(f"Hana wait holder / {wait.identity}",
                                               f"{detail}; observed={observed}; from={start}; until={end}; {duration}",
                                               1, window.label, wait.identity, detail=True, event_source=EventSource.WAITS))
    for (kind_label, reason), grouped in sorted(unnamed_groups.items()):
        rows.append(log_records.MeasureRow(f"Hana wait holders unnamed / {kind_label} / {reason}",
                                           partial_wait_total(holder_wait_duration(grouped, complete_coverage), partial), len(grouped), window.label, source, quantity=wait_quantity(grouped, complete_coverage, partial), event_source=EventSource.WAITS))
    return rows


def candidate_summary(candidates: Sequence[WaitCandidate]) -> str:
    return " | ".join(dict.fromkeys(candidate.label for candidate in candidates))


def collapse_wait_rows(
    rows: Sequence[log_records.MeasureRow], source: str, identity_kind: str,
) -> list[log_records.MeasureRow]:
    groups: dict[tuple[str, str], list[log_records.MeasureRow]] = {}
    for row in rows:
        groups.setdefault((row.measure, row.value), []).append(row)
    collapsed: list[log_records.MeasureRow] = []
    for grouped in groups.values():
        first = grouped[0]
        count = sum(row.n for row in grouped)
        quantity = replace(first.quantity, amount=count) if isinstance(first.quantity, log_records.Diagnostic) else first.quantity
        collapsed.append(replace(first, n=count, source=source + "; " + identity_kind + ": "
                                 + ", ".join(row.source for row in grouped), quantity=quantity))
    return collapsed


def hana_wait_rows(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window,
    completions: Sequence[PhaseCompletion],
) -> list[log_records.MeasureRow]:
    source = "progress_history.py waits; phase starts and wait starts in window; completed durations only"
    rows: list[log_records.MeasureRow] = []
    waits = [wait for wait in history.waits if window.contains(wait.started)] if isinstance(history, HanaWaitHistory) else []
    phases = history.phases if isinstance(history, HanaWaitHistory) else ()
    covered = sum(isinstance(phase.state, CoveredWaitHistory) for phase in phases)
    missing = [phase for phase in phases if isinstance(phase.state, HistoryNotRecorded)]
    uncertain = [phase for phase in phases if isinstance(phase.state, UncertainWaitCoverage)]
    partial = bool(wait_coverage_counts(history, window, completions))
    rows.extend(wait_coverage_rows(history, window, "Hana wait", completions))
    for kind in WaitKind:
        completed = [wait for wait in waits if wait.kind == kind and isinstance(wait.end, CompletedWait)]
        minutes = sum((wait.end.at - wait.started).total_seconds() / 60 for wait in completed if isinstance(wait.end, CompletedWait))
        complete_coverage = covered and not partial
        value = f"{minutes:.1f} min" if completed or complete_coverage else "not recorded (HistoryNotRecorded)"
        value = partial_wait_total(value, partial)
        rows.append(log_records.MeasureRow(f"Hana wait / {kind.value}", value, len(completed), window.label, source, quantity=wait_quantity(completed, bool(complete_coverage), partial), event_source=EventSource.WAITS))
        open_waits = [wait for wait in waits if wait.kind == kind and isinstance(wait.end, UnknownEndedWait)]
        if open_waits:
            rows.append(log_records.MeasureRow(f"Hana wait unknown-ended / {kind.value}", "duration not recorded", len(open_waits), window.label, source, quantity=log_records.NotRecorded("duration not recorded"), event_source=EventSource.WAITS))
    handoffs = [wait for wait in waits if isinstance(wait.phase, ProductionHandoff)]
    if handoffs:
        completed_handoffs = [wait for wait in handoffs if isinstance(wait.end, CompletedWait)]
        minutes = sum((wait.end.at - wait.started).total_seconds() / 60 for wait in completed_handoffs
                      if isinstance(wait.end, CompletedWait))
        value = f"{minutes:.1f} min" if completed_handoffs else "duration not recorded"
        rows.append(log_records.MeasureRow("Hana wait / production handoffs without a phase", partial_wait_total(value, partial),
                                           len(handoffs), window.label, source + "; excluded from phase share",
            quantity=wait_quantity(completed_handoffs, bool(completed_handoffs), partial), event_source=EventSource.WAITS))
        open_handoffs = len(handoffs) - len(completed_handoffs)
        if open_handoffs:
            rows.append(log_records.MeasureRow("Hana wait unknown-ended / production handoffs without a phase",
                                               "duration not recorded", open_handoffs, window.label, source, quantity=log_records.NotRecorded("duration not recorded"), event_source=EventSource.WAITS))
    for wait in waits:
        if isinstance(wait.kind, WaitKindNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitKindNotRecorded", wait.kind.reason, 1, window.label, wait.identity, quantity=log_records.Diagnostic(1, wait.kind.reason), event_source=EventSource.WAITS))
            if isinstance(wait.end, UnknownEndedWait):
                rows.append(log_records.MeasureRow("Hana wait unknown-ended / WaitKindNotRecorded", "duration not recorded", 1, window.label, wait.identity, quantity=log_records.NotRecorded("duration not recorded"), event_source=EventSource.WAITS))
        if wait.production_handoff:
            owner_text = wait.owner.session if isinstance(wait.owner, RecordedWaitOwner) else f"not recorded: {wait.owner.reason}"
            rows.append(log_records.MeasureRow("Hana wait / production handoff", f"no phase; owner={owner_text}", 1, window.label, wait.identity, quantity=log_records.Diagnostic(1), event_source=EventSource.WAITS))
        elif isinstance(wait.phase, WaitPhaseNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitPhaseNotRecorded", wait.phase.reason, 1, window.label, wait.identity, quantity=log_records.Diagnostic(1, wait.phase.reason), event_source=EventSource.WAITS))
        if isinstance(wait.owner, WaitOwnerNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitOwnerNotRecorded", wait.owner.reason, 1, window.label, wait.identity, quantity=log_records.Diagnostic(1, wait.owner.reason), event_source=EventSource.WAITS))
    if isinstance(history, HistoryNotRecorded) or not phases and not waits:
        reason = history.reason if isinstance(history, HistoryNotRecorded) else "no Hana phase or wait records in window"
        rows.append(log_records.MeasureRow("Hana wait coverage / HistoryNotRecorded", "not recorded (HistoryNotRecorded)", 0, window.label, source + "; " + reason, quantity=log_records.NotRecorded(reason), event_source=EventSource.WAITS))
    missing_reasons: dict[str, list[str]] = {}
    for phase in missing:
        if isinstance(phase.state, HistoryNotRecorded):
            missing_reasons.setdefault(phase.state.reason, []).append(f"{phase.run}/{phase.instance}")
    for reason, identities in missing_reasons.items():
        rows.append(log_records.MeasureRow("Hana wait coverage / HistoryNotRecorded", reason, len(identities),
                                           window.label, source + "; phases: " + ", ".join(identities), quantity=log_records.NotRecorded(reason), event_source=EventSource.WAITS))
    phase_rows: list[log_records.MeasureRow] = []
    for phase in uncertain:
        if isinstance(phase.state, UncertainWaitCoverage):
            detail = phase.state.reason + "; candidates: " + candidate_summary(phase.state.candidates)
            phase_rows.append(log_records.MeasureRow("Hana wait coverage / UncertainCoverage", detail, 1, window.label, f"{phase.run}/{phase.instance}", quantity=log_records.Diagnostic(1, detail), event_source=EventSource.WAITS))
    rows.extend(collapse_wait_rows(phase_rows, source, "phases"))
    if isinstance(history, HanaWaitHistory) and history.attribution_misses:
        miss_rows: list[log_records.MeasureRow] = []
        misses = history.attribution_misses
        for miss in misses:
            detail = miss.reason + "; candidates: " + candidate_summary(miss.candidates)
            miss_rows.append(log_records.MeasureRow("Hana wait attribution / Unresolved", detail, 1, window.label, miss.wait_id, quantity=log_records.Diagnostic(1, detail), event_source=EventSource.WAITS))
        rows.extend(collapse_wait_rows(miss_rows, source, "waits"))
    if isinstance(history, HanaWaitHistory):
        origin_rows: list[log_records.MeasureRow] = []
        for origin in history.unresolved_origins:
            origin_rows.append(log_records.MeasureRow("Hana wait origin / UnresolvedPhaseOrigin", origin.reason, 1,
                                               window.label, f"{origin.run}/{origin.instance}", quantity=log_records.Diagnostic(1, origin.reason), event_source=EventSource.WAITS))
        rows.extend(collapse_wait_rows(origin_rows, source, "phases"))
        problem_rows: list[log_records.MeasureRow] = []
        for problem in history.problems:
            problem_rows.append(log_records.MeasureRow("Hana wait / source problem", problem.reason, 1, window.label, problem.source, quantity=log_records.Diagnostic(1, problem.reason), event_source=EventSource.WAITS))
        rows.extend(collapse_wait_rows(problem_rows, source, "waits"))
    return rows


def parse_receipt(record: log_records.JsonObject) -> MergeReceipt:
    if log_records.field_text(record, "event_type") != "merge_receipt":
        raise ValueError("expected merge_receipt")
    fields = {key: log_records.field_text(record, key) for key in
              ("project", "issue", "phase_id", "merge_commit", "source_checkpoint", "target_branch")}
    if not all(fields[key] for key in ("project", "merge_commit", "source_checkpoint", "target_branch")) or not (fields["issue"] or fields["phase_id"]):
        raise ValueError("receipt must name project, phase or issue, both commits and target branch")
    return MergeReceipt(fields["project"], fields["issue"], fields["phase_id"], fields["merge_commit"],
                        fields["source_checkpoint"], fields["target_branch"],
                        log_records.required_time(record.get("merged_at", record.get("timestamp_epoch"))))


def read_receipts(path: Path) -> tuple[MergeReceipt, ...] | HistoryNotRecorded:
    if not path.exists():
        return HistoryNotRecorded("merge receipt history not recorded")
    unique = {(item.project, item.target_branch, item.commit): item for item in json_lines(path, parse_receipt)}
    return tuple(unique.values())


def receipt_for_merge(merge: HanaMerge, receipts: Sequence[MergeReceipt]) -> MergeReceipt | ReceiptMissing:
    for receipt in receipts:
        if receipt.commit == merge.commit and receipt.target_branch.removeprefix("origin/") == "init/catalyst" and (
            receipt.project in HANA_PROJECTS or Path(receipt.project).name in ("hana", "hana_catalyst")
        ):
            return receipt
    return ReceiptMissing(merge.commit)


def receipt_rows(
    receipts: Sequence[MergeReceipt] | HistoryNotRecorded,
    merges: HanaTrunkMerges | MergeHistoryNotRecorded, window: log_records.Window,
) -> list[log_records.MeasureRow]:
    source = "progress_history merge_receipts.jsonl; exact project, target branch and merge commit"
    if isinstance(receipts, HistoryNotRecorded):
        rows = [log_records.MeasureRow("merge receipts / all projects", f"not recorded (HistoryNotRecorded): {receipts.reason}", 0, window.label, source, quantity=log_records.NotRecorded(receipts.reason), event_source=EventSource.RECEIPTS)]
        recorded: Sequence[MergeReceipt] = ()
    else:
        selected = {(item.project, item.target_branch, item.commit): item for item in receipts if window.contains(item.merged_at)}
        recorded = tuple(selected.values())
        rows = [log_records.MeasureRow("merge receipts / all projects", str(len(selected)), len(selected), window.label, source,
            quantity=log_records.EventValue(len(selected), events=tuple(log_records.TimedAmount(item.merged_at) for item in recorded),
                population=tuple(item.merged_at for item in recorded)), event_source=EventSource.RECEIPTS)]
    if isinstance(merges, MergeHistoryNotRecorded):
        rows.append(log_records.MeasureRow("Hana merge receipts / origin/init/catalyst", "not recorded (merge history not recorded)", 0, window.label, source, quantity=log_records.NotRecorded("merge history not recorded"), event_source=EventSource.RECEIPTS))
        return rows
    selected_merges = {merge.commit: merge for merge in merges.merges if window.contains(merge.at)}
    matched = 0
    for merge in selected_merges.values():
        result = receipt_for_merge(merge, recorded)
        if isinstance(result, ReceiptMissing):
            rows.append(log_records.MeasureRow("Hana merge / ReceiptMissing", result.commit, 1, window.label, source,
                quantity=log_records.EventValue(1, events=(log_records.TimedAmount(merge.at),), population=(merge.at,)), event_source=EventSource.RECEIPTS))
        else:
            matched += 1
    rows.insert(1, log_records.MeasureRow("Hana merge receipts / origin/init/catalyst", f"{matched}/{len(selected_merges)}", len(selected_merges), window.label, source,
        quantity=log_records.Fraction(matched, len(selected_merges)), event_source=EventSource.RECEIPTS))
    return rows


WAIT_HISTORY_NOT_RECORDED = HistoryNotRecorded()


def phase_completion_key(event: PhaseCompletion) -> WaitPhaseInstance | str:
    return event.phase if isinstance(event.phase, WaitPhaseInstance) else event.identity


def completed_hana_phases(events: Sequence[PhaseCompletion], window: log_records.Window) -> tuple[PhaseCompletion, ...]:
    unique: dict[WaitPhaseInstance | str, PhaseCompletion] = {}
    for event in events:
        if event.hana and window.contains(event.at) and not isinstance(event.seconds, DurationNotRecorded):
            unique[phase_completion_key(event)] = event
    return tuple(unique.values())


def phase_interval_start(phase: PhaseCompletion) -> datetime:
    if isinstance(phase.started, datetime):
        return phase.started
    if isinstance(phase.seconds, DurationNotRecorded):
        raise ValueError("phase interval needs a start or recorded duration")
    return phase.at - timedelta(seconds=phase.seconds)


def phase_wait_seconds(phase: PhaseCompletion, waits: Sequence[HanaWait]) -> float:
    if not isinstance(phase.phase, WaitPhaseInstance) or isinstance(phase.seconds, DurationNotRecorded):
        return 0
    started = phase_interval_start(phase)
    intervals = sorted((max(wait.started, started), min(wait.end.at, phase.at)) for wait in waits
                       if wait.phase == phase.phase and isinstance(wait.kind, WaitKind)
                       and isinstance(wait.end, CompletedWait))
    seconds = 0.0
    through = started
    for start, end in intervals:
        start = max(start, through)
        if end > start:
            seconds += (end - start).total_seconds()
            through = end
    return seconds


def wait_share_row(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window, phases: Sequence[PhaseCompletion],
) -> log_records.MeasureRow:
    selected = completed_hana_phases(phases, window)
    value = "not recorded (HistoryNotRecorded)"
    quantity: log_records.Quantity = log_records.NotRecorded("HistoryNotRecorded")
    partial = bool(wait_coverage_counts(history, window, selected))
    if isinstance(history, HanaWaitHistory):
        seconds = sum(phase_wait_seconds(phase, history.waits) for phase in selected)
        coverage = {(phase.run, phase.instance): phase.state for phase in history.phases}
        partial = partial or any(isinstance(phase.phase, WaitPhaseNotRecorded)
                      or (phase.phase.run, phase.phase.identity) not in coverage for phase in selected)
        covered = bool(selected) and all(isinstance(phase.phase, WaitPhaseInstance)
                    and isinstance(coverage.get((phase.phase.run, phase.phase.identity)), CoveredWaitHistory)
                    for phase in selected)
        if seconds or covered and not partial:
            total = sum(max(0.0, (phase.at - phase_interval_start(phase)).total_seconds()) for phase in selected)
            value = f"{seconds / total * 100:.1f}%" if total > 0 else "not recorded (phase time unavailable)"
            quantity = log_records.Fraction(seconds, total, percent=True, partial=partial) if total > 0 else log_records.NotRecorded("phase time unavailable")
    return log_records.MeasureRow("Hana wait / share of phase time with a recorded kind", partial_wait_total(value, partial),
                                  len(selected), window.label,
                                  "progress_history.py waits / Hana phase time; union of completed known-kind waits per completed phase / full phase time", quantity=quantity, event_source=EventSource.WAITS)


def phase_flow(
    events: Sequence[PhaseCompletion], window: log_records.Window,
    wait_history: HanaWaitHistory | HistoryNotRecorded = WAIT_HISTORY_NOT_RECORDED,
) -> list[log_records.MeasureRow]:
    unique = {phase_completion_key(event): event for event in events if event.hana and window.contains(event.at)}
    selected = completed_hana_phases(events, window)
    times = [event.seconds / 60 for event in selected if not isinstance(event.seconds, DurationNotRecorded)]
    source = "progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only"
    rows = [
        log_records.MeasureRow("Hana phases done", str(len(unique)), len(unique), window.label, source,
            quantity=log_records.EventValue(len(unique), events=tuple(log_records.TimedAmount(event.at) for event in unique.values()),
                population=tuple(event.at for event in unique.values())), event_source=EventSource.PHASES),
        replace(distribution("Hana phase time", times, window, source), event_source=EventSource.PHASES),
    ]
    rows.extend(hana_wait_rows(wait_history, window, selected))
    rows.append(wait_share_row(wait_history, window, selected))
    rows.extend(holder_rows(wait_history, window, selected))
    return rows


HANA_PROJECTS = frozenset(("hana", "hana_diegetic", "hana_conduit", "hana_liminal"))


def closure_names_merge(issue: issues.Issue) -> bool:
    receipt = issue.value("merge_commit")
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", receipt):
        return True
    return re.search(r"\bmerg(?:e|ed)\b[^\n]*\b[0-9a-fA-F]{7,40}\b", issue.value("reason"), re.IGNORECASE) is not None


def is_declined(issue: issues.Issue) -> bool:
    words = issue.value("reason").split()
    return bool(words) and words[0].casefold().removesuffix(":") == "declined"


def issue_quantity(records: Sequence[issues.Issue], field: str = "date_closed") -> log_records.EventValue:
    times = tuple(log_records.required_time(issue.value(field)) for issue in records)
    return log_records.EventValue(len(records), events=tuple(log_records.TimedAmount(at) for at in times), population=times)


def issue_flow(found: Sequence[issues.Issue], window: log_records.Window) -> list[log_records.MeasureRow]:
    goal = [issue for issue in found if issue.value("backlog_goal") == "1 - Ship Hana"]
    activated = [issue for issue in goal if window.contains(log_records.moment(issue.value("date_active")))]
    candidates = [issue for issue in goal if issue.value("status") == "closed" and window.contains(log_records.moment(issue.value("date_closed")))]
    declined = [issue for issue in candidates if is_declined(issue)]
    closed = [issue for issue in candidates if issue not in declined]
    undated = [issue for issue in goal if issue.value("status") == "closed"
               and not is_declined(issue)
               and not isinstance(log_records.moment(issue.value("date_closed")), datetime)]
    durations: list[float] = []
    for issue in closed:
        start, finish = log_records.moment(issue.value("date_active")), log_records.moment(issue.value("date_closed"))
        if isinstance(start, datetime) and isinstance(finish, datetime) and finish >= start:
            durations.append((finish - start).total_seconds() / 60)
    active_value = str(len(activated)) if any(issue.value("date_active") for issue in goal) else "not recorded"
    rows = [
        log_records.MeasureRow("goal 1 issues moved to active", active_value, len(activated), window.label,
                   "Hanadocs date_active; stage/date_modified do not record a transition",
                   quantity=issue_quantity(activated, "date_active") if any(issue.value("date_active") for issue in goal)
                   else log_records.NotRecorded("activation time not recorded"), event_source=EventSource.ISSUES),
        log_records.MeasureRow("goal 1 issues closed", str(len(closed)), len(closed), window.label,
                   "Hanadocs date_closed; date-only precision; reason declined excluded", quantity=issue_quantity(closed), event_source=EventSource.ISSUES),
        replace(distribution("goal 1 active to closed", durations, window, "Hanadocs date_active + date_closed; date-only precision"), event_source=EventSource.ISSUES),
    ]
    source = "Hanadocs project + date_closed; date-only precision; reason declined excluded"
    for project in sorted({issue.value("project") for issue in (*closed, *undated)}):
        count = sum(issue.value("project") == project for issue in closed)
        missing = sum(issue.value("project") == project for issue in undated)
        scope = "Hana" if project in HANA_PROJECTS else "other"
        value = str(count) if not missing else f"not recorded ({count} dated in window; {missing} closure dates not recorded)"
        rows.append(log_records.MeasureRow(f"goal 1 issues closed / {project or 'project not recorded'} / {scope}",
                               value, count, window.label, source, quantity=issue_quantity([issue for issue in closed if issue.value("project") == project])
                if not missing else log_records.NotRecorded(f"{count} dated in window; {missing} closure dates not recorded"), event_source=EventSource.ISSUES))
    hana = [issue for issue in closed if issue.value("project") in HANA_PROJECTS]
    rows.append(log_records.MeasureRow("goal 1 Hana-project issues closed", str(len(hana)), len(hana), window.label, source, quantity=issue_quantity(hana), event_source=EventSource.ISSUES))
    rows.append(log_records.MeasureRow("goal 1 declined closures excluded", str(len(declined)), len(declined), window.label,
                           "Hanadocs reason declined + date_closed; date-only precision", quantity=issue_quantity(declined), event_source=EventSource.ISSUES))
    receipts = sum(closure_names_merge(issue) for issue in hana)
    receipt_source = "Hanadocs reason naming a merge commit or merge_commit; text only, not checked against Git"
    rows.append(log_records.MeasureRow("goal 1 Hana closures naming a merge commit", str(receipts), len(hana), window.label, receipt_source,
        quantity=replace(issue_quantity([issue for issue in hana if closure_names_merge(issue)]),
                         population=tuple(log_records.required_time(issue.value("date_closed")) for issue in hana)), event_source=EventSource.ISSUES))
    for issue in sorted(hana, key=lambda issue: issue.path.stem):
        value = "yes" if closure_names_merge(issue) else "not recorded"
        rows.append(log_records.MeasureRow(f"Hana closure merge commit / {issue.path.stem}", value, 1, window.label, receipt_source, detail=True, event_source=EventSource.ISSUES))
    return rows


def read_hana_merges(repo: Path) -> HanaTrunkMerges:
    return walk_hana_trunk(repo)


def walk_hana_trunk(repo: Path) -> HanaTrunkMerges:
    ref = "refs/remotes/origin/init/catalyst"
    def git(*arguments: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                              capture_output=True, text=True).stdout

    tips = list(dict.fromkeys(git("reflog", "show", "--format=%H", ref).splitlines()))
    commits: dict[str, tuple[tuple[str, ...], datetime]] = {}
    for line in git("log", "--topo-order", "--format=%H%x09%P%x09%cI", ref).splitlines():
        commit, parents, at = line.split("\t")
        commits[commit] = (tuple(parents.split()), log_records.required_time(at))
    current = next(iter(commits))
    merges: list[HanaMerge] = []
    recorded: list[str] = []
    if current in tips:
        recorded.append(current)
    for older in tips:
        if older == current or older not in commits:
            continue
        allowed = set(git("rev-list", "--ancestry-path", f"{older}..{current}").splitlines())
        if current not in allowed:
            continue
        allowed.add(older)
        recorded.append(older)
        while current != older:
            parents, at = commits[current]
            if len(parents) > 1:
                merges.append(HanaMerge(current, at))
            current = next(parent for parent in parents if parent in allowed)
    while True:
        parents, at = commits[current]
        if len(parents) > 1:
            merges.append(HanaMerge(current, at))
        if not parents:
            break
        current = parents[0]
    limit = (f"trunk path from {len(recorded)} recorded tips back to {commits[recorded[-1]][1].isoformat()}; "
             "first parent before that; git expires reflog entries after 90 days") if recorded else "no recorded tips; first parent only"
    return HanaTrunkMerges(tuple(merges), "git origin/init/catalyst merge commits; local ref; commit time; no fetch; "
                           + limit)


def hana_repository(units: Sequence[Unit]) -> Path:
    paths = [Path(unit.worktree) for unit in units if unit.hana and unit.worktree]
    paths.extend(Path.home() / "rust" / name for name in ("hana_catalyst", "hana"))
    for path in paths:
        common = repository(str(path))
        if common != "cwd has no repository" and is_hana(common):
            return Path(common)
    raise FileNotFoundError("Hana repository not found through local Git common directories")


def merge_flow(merges: HanaTrunkMerges | MergeHistoryNotRecorded, window: log_records.Window) -> list[log_records.MeasureRow]:
    if isinstance(merges, MergeHistoryNotRecorded):
        return [log_records.MeasureRow("Hana merges / origin/init/catalyst", "not recorded", 0, window.label, merges.value, quantity=log_records.NotRecorded("merge history not recorded"), event_source=EventSource.MERGES)]
    selected = {merge.commit: merge for merge in merges.merges if window.contains(merge.at)}
    source = merges.source
    rows = [log_records.MeasureRow("Hana merges / origin/init/catalyst", str(len(selected)), len(selected), window.label, source,
        quantity=log_records.EventValue(len(selected), events=tuple(log_records.TimedAmount(merge.at) for merge in selected.values()),
            population=tuple(merge.at for merge in selected.values())), event_source=EventSource.MERGES)]
    days = Counter(merge.at.astimezone(user_zone()).date() for merge in selected.values())
    day = window.since.astimezone(user_zone()).date()
    last = (window.until - timedelta(microseconds=1)).astimezone(user_zone()).date()
    while day <= last:
        count = days[day]
        rows.append(log_records.MeasureRow(f"Hana merges / {day.isoformat()}", str(count), count, window.label, source, detail=True, event_source=EventSource.MERGES))
        day += timedelta(days=1)
    return rows


def attention(
    push_report: push_audit.AuditReport, messages: Sequence[AgentMessage],
) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    zone = user_zone()
    window = push_report.window
    push_days = Counter(push.at.astimezone(zone).date().isoformat() for push in push_report.delivered_pushes)
    failed_days = Counter(entry.at.astimezone(zone).date().isoformat() for entry in push_report.entries)
    message_days: Counter[tuple[str, SenderKind]] = Counter()
    for message in messages:
        if window.contains(message.at):
            day_text = message.at.astimezone(zone).date().isoformat()
            message_days[(day_text, message.kind)] += 1
    day = window.since.astimezone(zone).date()
    last_day = (window.until - timedelta(microseconds=1)).astimezone(zone).date()
    while day <= last_day:
        day_text = day.isoformat()
        count, failed = push_days[day_text], failed_days[day_text]
        rows.append(log_records.MeasureRow(
            f"phone pushes / {day_text}", f"{count}; after failed review {failed}",
            count, window.label, "pushover + review log; natedev only", detail=True,
        event_source=EventSource.PUSHES))
        for kind in SenderKind:
            count = message_days[(day_text, kind)]
            rows.append(log_records.MeasureRow(
                f"messages / {day_text} / {kind.value}", str(count), count, window.label,
                "message log; kind/route, else sender-name heuristic; natedev only",
                comparison_name="messages / " + kind.value,
                quantity=log_records.EventValue(count, events=tuple(log_records.TimedAmount(message.at) for message in messages
                    if window.contains(message.at) and message.kind is kind and message.at.astimezone(zone).date() == day),
                    population=tuple(message.at for message in messages if window.contains(message.at)
                    and message.kind is kind and message.at.astimezone(zone).date() == day)),
            event_source=EventSource.MESSAGES))
        day += timedelta(days=1)
    source = "pushover + review log; natedev only"
    total_pushes = len(push_report.delivered_pushes)
    rows.extend([
        log_records.MeasureRow("phone pushes sent", str(total_pushes), total_pushes, window.label, source,
            quantity=log_records.EventValue(total_pushes, events=tuple(log_records.TimedAmount(push.at) for push in push_report.delivered_pushes),
                population=tuple(push.at for push in push_report.delivered_pushes)), event_source=EventSource.PUSHES),
        log_records.MeasureRow("phone pushes after failed review", str(len(push_report.entries)), total_pushes, window.label, source,
            quantity=log_records.EventValue(len(push_report.entries), events=tuple(log_records.TimedAmount(entry.at) for entry in push_report.entries),
                population=tuple(push.at for push in push_report.delivered_pushes)), event_source=EventSource.PUSHES),
    ])
    return rows


def pending_attention(items: Sequence[WaitingItem], snapshot: datetime, window: log_records.Window) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    for kind in ("changes", "proposals", "efficiency items", "questions"):
        group = [item for item in items if item.kind == kind]
        known = [item.opened for item in group if isinstance(item.opened, datetime)]
        age = "not recorded"
        if known:
            age = f"{max(0, (snapshot - min(known)).total_seconds()) / 86400:.1f} days"
        if len(known) < len(group):
            age += f" ({len(group) - len(known)} ages not recorded)"
        source = "change-review / proposal issues / nightly ledger; natedev only"
        if kind == "questions":
            source = "showrunner/outstanding/*.json; question since stamps; natedev only"
        rows.append(log_records.MeasureRow(
            f"waiting / {kind}", f"{len(group)}; oldest {age}", len(group),
            f"snapshot {snapshot.astimezone(user_zone()):%Y-%m-%d %H:%M %Z}", source,
            quantity=log_records.Snapshot(len(group), visibility_changed=kind == "changes"),
        event_source=log_records.SourceNotApplicable.STATE))
    answer_source = ("showrunner/outstanding: answer history is a 48-hour replay record; "
                     "no window-complete answer history or question opening time for latency; natedev only")
    rows.append(log_records.MeasureRow("questions answered", "not recorded", 0, window.label, answer_source, quantity=log_records.NotRecorded(answer_source), event_source=log_records.SourceNotApplicable.STATE))
    rows.append(replace(distribution("question answer time", [], window, answer_source),
                        event_source=log_records.SourceNotApplicable.STATE))
    return rows


def rank_coverage(found: Sequence[issues.Issue], rankings: Sequence[IssueRank], units: Sequence[Unit], snapshot: str) -> list[log_records.MeasureRow]:
    open_issues = [issue for issue in found if issue.value("status") == "open"]
    open_goal = {issue.path.stem: issue for issue in open_issues if issue.value("backlog_goal") == "1 - Ship Hana"}
    ranked = sorted((row for row in rankings if row.name in open_goal and row.rank > 0), key=lambda row: row.rank)
    top = list(dict.fromkeys(row.name for row in ranked))[:20]
    live = [unit for unit in units if unit.activity is UnitActivity.LIVE]
    covered_names = {name for name in top if any(unit_names_issue(unit, open_goal[name]) for unit in live)}
    covered = len(covered_names)
    linked = 0
    for unit in units:
        linked += int(unit.activity is UnitActivity.LIVE and any(unit_names_issue(unit, issue) for issue in open_issues))
    by_name = {row.name: row for row in reversed(ranked)}
    scores = [by_name[name].score for name in top]
    weighted = "not recorded"
    weighted_quantity: log_records.Fraction | log_records.NotRecorded = log_records.NotRecorded("ranking scores not recorded")
    if scores and all(isinstance(score, float) and score >= 0 for score in scores):
        total = sum(score for score in scores if isinstance(score, float))
        covered_score = sum(score for name in covered_names if isinstance(score := by_name[name].score, float))
        if total > 0:
            weighted = f"{covered_score:g}/{total:g} ({covered_score / total:.1%})"
            weighted_quantity = log_records.Fraction(covered_score, total)
    return [
        log_records.MeasureRow("top 20 goal 1 open issues with a unit", f"{covered}/{len(top)}", len(top), snapshot,
                   "backlog-rankings + open issue links; unit has a running phase or live session",
                   quantity=log_records.Snapshot(log_records.Fraction(covered, len(top))), event_source=log_records.SourceNotApplicable.STATE),
        log_records.MeasureRow("live units naming an open issue", f"{linked}/{len(units)}", len(units), snapshot,
                   "open production Issue link or open issue unit + showrunner; running phase or live session",
                   quantity=log_records.Snapshot(log_records.Fraction(linked, len(units))), event_source=log_records.SourceNotApplicable.STATE),
        log_records.MeasureRow("top 20 goal 1 open issues covered score", weighted, len(top), snapshot,
                   "backlog-rankings score; covered score / all top 20 scores; running phase or live session",
                   quantity=log_records.Snapshot(weighted_quantity), event_source=log_records.SourceNotApplicable.STATE),
    ]


def unit_names_issue(unit: Unit, issue: issues.Issue) -> bool:
    if issue.value("unit") == unit.name and issue.value("showrunner") in (unit.production, unit.showrunner, ""):
        return True
    cell = unit.issue.strip().strip("`")
    wiki = re.fullmatch(r"\[\[([^]|]+)(?:\|[^]]+)?\]\]", cell)
    link = re.fullmatch(r"\[[^]]+\]\(([^)]+)\)", cell)
    target = wiki[1] if wiki else link[1] if link else cell
    return Path(target).name.removesuffix(".md") == issue.path.stem


def read_source[T](source: str, loader: Callable[[], T], fallback: T, problems: list[log_records.SourceProblem],
                   event_source: EventSource | log_records.SourceNotApplicable = log_records.SourceNotApplicable.UNSPECIFIED) -> T:
    try:
        return loader()
    except (OSError, ValueError, UnicodeError, sqlite3.Error, RuntimeError, production_units.Refusal, subprocess.SubprocessError,
            change_review.ChangeReviewError, dailies_render.InputError) as error:
        reason = "missing" if isinstance(error, FileNotFoundError) else "unreadable"
        problems.append(log_records.SourceProblem(source, f"{reason}: {error}", event_source))
        return fallback


def read_issues(folder: Path) -> list[issues.Issue]:
    found: list[issues.Issue] = []
    for path in folder.iterdir():
        if path.suffix != ".md" or not path.is_file():
            continue
        fields = issues.frontmatter(path)
        if not fields:
            raise ValueError(f"{path}: unreadable or missing frontmatter")
        found.append(issues.Issue(path, fields))
    return found


def read_units(problems: list[log_records.SourceProblem]) -> list[Unit]:
    result: list[Unit] = []
    # These are the same loaders used by showrunners list and units_table.
    warnings = io.StringIO()
    with redirect_stderr(warnings):
        runners = showrunners.registered_showrunners()
    if warnings.getvalue().strip():
        problems.append(log_records.SourceProblem("showrunners", warnings.getvalue().strip().replace("\n", "; "), event_source=log_records.SourceNotApplicable.STATE))
    marks = read_source("unit sessions", unit_lookup.every_marked_unit, unit_lookup.MarkedProductions({}, {}), problems, event_source=log_records.SourceNotApplicable.STATE)
    running = read_source("unit running phases", lambda: running_phase_worktrees(unit_lookup.runs_dir(), problems), set[str](), problems, event_source=log_records.SourceNotApplicable.STATE)
    for runner in runners:
        doc = Path(runner["doc"])
        lines = read_source(str(doc), lambda: doc.read_text(encoding="utf-8").splitlines(), [], problems, event_source=log_records.SourceNotApplicable.STATE)
        repo = read_source(str(doc), lambda: production_units.production_field(lines, "Repository"), "repository not recorded", problems, event_source=log_records.SourceNotApplicable.STATE)
        rows = read_source(str(doc), lambda: production_units.live_unit_table(lines, runner["slug"]), [], problems, event_source=log_records.SourceNotApplicable.STATE)
        for row in rows:
            tree = production_units.cell_value(row.get("Worktree", ""))
            name = production_units.cell_value(row.get("Unit", ""))
            issue = production_units.cell_value(row.get("Issue", ""))
            marked = marks.units.get(runner["slug"], {}).get(name)
            activity = UnitActivity.INACTIVE
            if tree in running or (marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude)):
                activity = UnitActivity.LIVE
            elif marks.production_fault(runner["slug"]) or (marked is not None and isinstance(marked.claude, unit_lookup.ClaudeUnknown)):
                activity = UnitActivity.UNKNOWN
                problems.append(log_records.SourceProblem(f"unit activity / {runner['slug']} / {name}", "running phase or live session not recorded", event_source=log_records.SourceNotApplicable.STATE))
            result.append(Unit(runner["slug"], name, repo, tree, is_hana(tree), issue, runner["session"], activity))
    return result


def running_phase_worktrees(folder: Path, problems: list[log_records.SourceProblem]) -> set[str]:
    newest: dict[str, tuple[datetime, bool]] = {}
    for path in folder.iterdir():
        if path.suffix != ".jsonl":
            continue
        records = read_source(str(path), lambda: json_lines(path, log_records.object_record), [], problems, event_source=log_records.SourceNotApplicable.STATE)
        if not records or log_records.field_text(records[0], "event_type") != "run_started":
            continue
        first = records[0]
        tree = log_records.field_text(first, "working_dir")
        started = log_records.moment(first.get("run_started_at", first.get("timestamp")))
        if not isinstance(started, datetime):
            problems.append(log_records.SourceProblem(str(path), "unreadable: run start time not recorded", event_source=log_records.SourceNotApplicable.STATE))
            continue
        phases: set[str] = set()
        for record in records:
            kind = log_records.field_text(record, "event_type")
            if kind == "run_finished":
                phases.clear()
                continue
            if kind not in ("phase_started", "phase_finished"):
                continue
            phase = log_records.field_text(record, "phase_instance_id") or log_records.field_text(record, "phase_id")
            if not phase:
                problems.append(log_records.SourceProblem(str(path), "unreadable: phase identity not recorded", event_source=log_records.SourceNotApplicable.STATE))
                continue
            if kind == "phase_started":
                phases.add(phase)
            else:
                phases.discard(phase)
        if tree not in newest or started > newest[tree][0]:
            newest[tree] = (started, bool(phases))
    return {tree for tree, (_, running) in newest.items() if running}


def read_workers() -> tuple[list[Worker], list[str]]:
    # Account role reads use saved state. This command must not refresh caches.
    with stored_role_map():
        observed = roster.roster(os.environ, roster.ThisMachine())
    workers: list[Worker] = []
    for entry in observed.entries:
        if entry.kind != "codex" or not isinstance(entry.assignment, roster.WorkerAssignment):
            continue
        running = isinstance(entry.director, roster.UnderDirector)
        workers.append(Worker(entry.cwd, is_hana(entry.cwd), running))
    return workers, observed.problems


def read_builds(path: Path, problems: list[log_records.SourceProblem]) -> list[BuildStep]:
    with closing(usage_report.open_read_store(path)) as db:
        query = "SELECT repo,step,started_at,duration_s FROM steps WHERE host='natedev'"
        rows = cast(Iterable[sqlite3.Row], db.execute(query))
        result: list[BuildStep] = []
        invalid: Counter[str] = Counter()
        for row in rows:
            fields: log_records.JsonObject = dict(zip(("repo", "step", "at", "seconds"), cast(tuple[object, ...], tuple(row))))
            try:
                # Older build records have SQL NULL when the repository was unknown.
                repo = "repository not recorded" if fields["repo"] is None else log_records.field_text(fields, "repo")
                result.append(BuildStep(repo, log_records.field_text(fields, "step"), log_records.record_time(fields), log_records.field_number(fields, "seconds")))
            except (ValueError, OverflowError) as error:
                invalid[str(error)] += 1
        if invalid:
            detail = "; ".join(f"{reason} ({count})" for reason, count in invalid.items())
            problems.append(log_records.SourceProblem(str(path), f"invalid build rows skipped: {sum(invalid.values())}; {detail}", kind=log_records.RowsSkipped(sum(invalid.values())), event_source=EventSource.BUILDS))
        return result


def read_tokens(path: Path, window: log_records.Window) -> list[WindowTokenUsage]:
    with closing(usage_report.open_read_store(path)) as db:
        query = """SELECT f.folder,t.at,
            t.input+t.cache_write_5m+t.cache_write_1h+t.cache_read+t.output AS tokens
        FROM turns t LEFT JOIN folders f USING(machine,family,parent_id,agent_id)
        WHERE t.machine='natedev' AND julianday(t.at)>=julianday(?) AND julianday(t.at)<julianday(?)"""
        events: dict[str, list[log_records.TimedAmount]] = defaultdict(list)
        for row in cast(Iterable[sqlite3.Row], db.execute(query, (window.since.isoformat(), window.until.isoformat()))):
            fields: log_records.JsonObject = dict(zip(("cwd", "at", "tokens"), cast(tuple[object, ...], tuple(row))))
            cwd = fields["cwd"]
            if cwd is not None and not isinstance(cwd, str):
                raise ValueError("cwd: expected text or SQL NULL")
            repo = repository(cwd) if isinstance(cwd, str) and cwd else "cwd not recorded"
            events[repo].append(log_records.TimedAmount(log_records.record_time(fields), log_records.field_number(fields, "tokens")))
        return [WindowTokenUsage(window, repo, sum(event.amount for event in samples), len(samples), tuple(samples),
                                 tuple(event.at for event in samples)) for repo, samples in events.items()]


def hana_origin(origin: PhaseOrigin, prefixes: Sequence[str]) -> bool:
    """Use durable provenance before consulting a worktree that may be deleted."""
    if isinstance(origin.production, RecordedOrigin) and origin.production.value in ("hana", "tool-based-ui"):
        return True
    if isinstance(origin.repository, RecordedOrigin) and Path(origin.repository.value).expanduser().name in ("hana", "hana_catalyst"):
        return True
    patterns = ("hana_catalyst", "tool-based-ui-", *prefixes)
    for value in (RecordedOrigin(origin.working_dir), origin.plan):
        if not isinstance(value, RecordedOrigin) or not value.value:
            continue
        if any(part.startswith(patterns) for part in Path(value.value).parts):
            return True
    return bool(origin.working_dir) and is_hana(origin.working_dir)


def parse_phase(record: log_records.JsonObject, prefixes: Sequence[str]) -> tuple[PhaseCompletion, ...]:
    if log_records.field_text(record, "event_type") != "phase_finished" or log_records.field_text(record, "status") != "completed":
        return ()
    plan = next((origin_field(record, key) for key in ("plan_doc", "plan_path", "project_plan_doc")
                 if key in record and record[key] != ""), OriginNotRecorded("plan document not recorded"))
    origin = PhaseOrigin(log_records.field_text(record, "working_dir"), plan,
                         origin_field(record, "production"), origin_field(record, "repository"))
    at = log_records.record_time(record)
    identity = log_records.field_text(record, "event_id")
    if not identity:
        identity = f"{log_records.field_text(record, 'run_id')}:{log_records.field_text(record, 'phase_instance_id')}:{at}"
    seconds = log_records.field_number(record, "phase_elapsed_seconds") if "phase_elapsed_seconds" in record else DurationNotRecorded.MISSING
    if not isinstance(seconds, DurationNotRecorded) and seconds < 0:
        raise ValueError("phase elapsed seconds cannot be negative")
    run, instance = log_records.field_text(record, "run_id"), log_records.field_text(record, "phase_instance_id")
    phase = WaitPhaseInstance(run, instance) if run and instance else WaitPhaseNotRecorded("completion run or phase instance not recorded")
    started = log_records.moment(record.get("phase_started_at"))
    if not isinstance(started, datetime) and not isinstance(seconds, DurationNotRecorded):
        started = at - timedelta(seconds=seconds)
    return (PhaseCompletion(identity, at, hana_origin(origin, prefixes), seconds, phase, started),)


def read_phases(folder: Path, problems: list[log_records.SourceProblem], prefixes: Sequence[str] = ()) -> list[PhaseCompletion]:
    events: list[PhaseCompletion] = []
    for path in sorted(folder.glob("*.jsonl")):
        def read_file() -> list[PhaseCompletion]:
            # The shared run_events loader returns raw maps and hides bad lines.
            # Validate and convert each line once, so the audit adds no second parse.
            completed: list[PhaseCompletion] = []
            starts: dict[tuple[str, str], log_records.JsonObject] = {}
            records: list[log_records.JsonObject] = []
            bad_json = 0
            bad_fields = 0
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    try:
                        value = cast(object, json.loads(line))
                        record = log_records.object_record(value)
                    except ValueError:
                        bad_json += 1
                        continue
                    try:
                        if log_records.field_text(record, "event_type") == "phase_started":
                            key = (log_records.field_text(record, "run_id"), log_records.field_text(record, "phase_instance_id"))
                            if all(key):
                                starts[key] = record
                        records.append(record)
                    except ValueError:
                        bad_fields += 1
            for record in records:
                try:
                    key = (log_records.field_text(record, "run_id"), log_records.field_text(record, "phase_instance_id"))
                    if log_records.field_text(record, "event_type") == "phase_finished" and key in starts:
                        start = starts[key]
                        joined = {**record}
                        for field in ("working_dir", "plan_doc", "plan_path", "project_plan_doc", "production", "repository"):
                            if field in start:
                                joined[field] = start[field]
                        joined["phase_started_at"] = log_records.record_time(start).isoformat()
                        record = joined
                    completed.extend(parse_phase(record, prefixes))
                except ValueError:
                    bad_fields += 1
            if bad_json:
                problems.append(log_records.SourceProblem(str(path), f"unreadable: {bad_json} invalid JSON rows; counts can be incomplete", kind=log_records.RowsSkipped(bad_json), event_source=EventSource.PHASES))
            if bad_fields:
                problems.append(log_records.SourceProblem(str(path), f"unreadable: {bad_fields} invalid phase field rows; counts can be incomplete", kind=log_records.RowsSkipped(bad_fields), event_source=EventSource.PHASES))
            return completed

        events.extend(read_source(str(path), read_file, [], problems, event_source=EventSource.PHASES))
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    return events


def read_waiting(
    state: Path, vault: Path, found: Sequence[issues.Issue], problems: list[log_records.SourceProblem],
) -> list[WaitingItem]:
    waiting: list[WaitingItem] = []
    changes = vault / "change-review"
    paths = read_source(str(changes), lambda: list(changes.iterdir()), [], problems, event_source=log_records.SourceNotApplicable.STATE)
    for path in paths:
        if path.suffix == ".md":
            rows = read_source(str(path), lambda: change_review.pending(path), [], problems, event_source=log_records.SourceNotApplicable.STATE)
            for row in rows:
                match = re.search(r"\d{4}-\d{2}-\d{2}", row.get("Merged", ""))
                waiting.append(WaitingItem("changes", log_records.moment(match[0] if match else "")))
    for issue in found:
        if issue.value("status") != "open" or "proposal" not in issue.fields.get("tags", []):
            continue
        if isinstance(proposal_progress(issue), AwaitingApproval):
            waiting.append(WaitingItem("proposals", log_records.moment(issue.value("date_created"))))
    backlog = state / "plan-backlog"
    backlog_path = backlog / "ledger.md"
    def read_backlog() -> list[WaitingItem]:
        # The shared loader hides file errors, so check readability first.
        if not backlog_path.read_text(encoding="utf-8").strip():
            return []
        return [WaitingItem("proposals", log_records.moment(row.date)) for row in backlog_rows(backlog) if row.state == REVIEW_STATE]

    waiting.extend(read_source(str(backlog_path), read_backlog, [], problems, event_source=log_records.SourceNotApplicable.STATE))
    ledger = state / "nightly-review/ledger.md"
    lines = read_source(str(ledger), lambda: ledger.read_text(encoding="utf-8").splitlines(), [], problems, event_source=log_records.SourceNotApplicable.STATE)
    for candidate in candidates.ranked(lines):
        if candidate.state == "proposed":
            waiting.append(WaitingItem("efficiency items", log_records.moment(candidate.night[:10])))
    folder = state / "showrunner/outstanding"
    paths = read_source(str(folder), lambda: [path for path in folder.iterdir() if path.suffix == ".json"], [], problems, event_source=log_records.SourceNotApplicable.STATE)
    for path in sorted(paths):
        outstanding = read_source(str(path), lambda: dailies_render.read_outstanding(path), [], problems, event_source=log_records.SourceNotApplicable.STATE)
        for item in outstanding:
            if isinstance(item, dailies_render.OutstandingReminder):
                continue
            since = item.since
            opened = since.replace(tzinfo=user_zone()) if since.tzinfo is None else since
            waiting.append(WaitingItem("questions", opened))
    return waiting


def load_sources(window: log_records.Window) -> BaselineSources:
    return load_window_sources(window, push_audit.load_sources(Path.home() / ".local/state/notify"))


def load_window_sources(window: log_records.Window, notifications: push_audit.AuditSources) -> BaselineSources:
    state, vault = Path.home() / ".local/state", Path.home() / "rust/hanadocs"
    problems: list[log_records.SourceProblem] = []
    units = read_source("showrunners / units_table", lambda: read_units(problems), [], problems, event_source=log_records.SourceNotApplicable.STATE)
    # The roster and usage store do not depend on the other sources. Read them
    # while the main thread reads files. Each reader keeps its own diagnostics.
    worker_problems: list[log_records.SourceProblem] = []
    token_problems: list[log_records.SourceProblem] = []
    token_path = collect.store_path()
    with ThreadPoolExecutor(max_workers=2) as readers:
        worker_read = readers.submit(read_source, "session roster / codex_mesh", read_workers, ([], []), worker_problems,
                                     log_records.SourceNotApplicable.STATE)
        token_read = readers.submit(read_source, str(token_path), lambda: read_tokens(token_path, window), [], token_problems, EventSource.TOKENS)
        builds = read_source("buildlog/index.sqlite", lambda: read_builds(state / "buildlog/index.sqlite", problems), [], problems, event_source=EventSource.BUILDS)
        hana_prefixes = [Path(unit.worktree).name for unit in units if unit.hana and unit.worktree]
        phases = read_source("progress_history runs",
                             lambda: read_phases(unit_runs.history_root_in(state) / "runs", problems, hana_prefixes), [], problems, event_source=EventSource.PHASES)
        push_report = push_audit.audit(notifications, window)
        problems.extend(push_report.problems)
        messages = read_source("message/log.jsonl", lambda: json_lines(state / "message/log.jsonl", parse_message), [], problems, event_source=EventSource.MESSAGES)
        found = read_source("Hanadocs issues", lambda: read_issues(vault / "issues"), [], problems, event_source=EventSource.ISSUES)
        archive = vault / "archive"
        for folder in read_source(str(archive), lambda: [p for p in archive.rglob("*") if p.is_dir()], [], problems, event_source=EventSource.ISSUES):
            archived = read_source(str(folder), lambda: [issues.Issue(p, issues.frontmatter(p)) for p in folder.glob("*.md")], [], problems, event_source=EventSource.ISSUES)
            found.extend(issue for issue in archived if issue.value("status") == "closed")
        rankings = read_source("Hanadocs backlog-rankings", lambda: json_lines(vault / "backlog-rankings.jsonl", parse_rank), [], problems, event_source=log_records.SourceNotApplicable.STATE)
        waiting = read_waiting(state, vault, found, problems)
        workers, roster_problems = worker_read.result()
        tokens = token_read.result()
    problems.extend(worker_problems)
    problems.extend(log_records.SourceProblem("session roster", problem, event_source=log_records.SourceNotApplicable.STATE) for problem in roster_problems)
    problems.extend(token_problems)
    def load_merges() -> HanaTrunkMerges | MergeHistoryNotRecorded:
        return read_hana_merges(hana_repository(units))

    merges = read_source("Hana origin/init/catalyst merge log", load_merges, MergeHistoryNotRecorded.MISSING, problems, event_source=EventSource.MERGES)
    history_root = unit_runs.history_root()
    wait_history = read_source("progress_history waits", lambda: read_wait_history(history_root, window, problems, hana_prefixes, phases),
                               WAIT_HISTORY_NOT_RECORDED, problems, event_source=EventSource.WAITS)
    receipts = read_source("progress_history merge receipts", lambda: read_receipts(history_root / "merge_receipts.jsonl"),
                           HistoryNotRecorded("merge receipt history not recorded"), problems, event_source=EventSource.RECEIPTS)
    return BaselineSources(units, workers, builds, tokens, phases, push_report, messages, found, rankings, waiting, problems,
                           merges, wait_history, receipts)


def baseline_summary(
    window: log_records.Window, flow_rows: Sequence[log_records.MeasureRow], attention_rows: Sequence[log_records.MeasureRow],
    problem_rows: Sequence[log_records.MeasureRow], full: bool = False,
) -> str:
    values = {row.measure: row.value for row in (*flow_rows, *attention_rows)}
    start, end = window.since.astimezone(user_zone()), window.until.astimezone(user_zone())
    start_format = "%Y-%m-%d %H:%M" if start.tzname() == end.tzname() else "%Y-%m-%d %H:%M %Z"
    return "\n".join(log_records.wrap_text(line) for line in [
        f"Window: {start.strftime(start_format)} to {end:%Y-%m-%d %H:%M %Z} (end excluded).",
        f"Hana: {values['Hana merges / origin/init/catalyst']} merges to origin/init/catalyst; "
        + f"{values['goal 1 Hana-project issues closed']} Hana-project issues closed.",
        f"Goal 1: {values['goal 1 issues closed']} issues closed; "
        + f"{values['goal 1 declined closures excluded']} declined closures left out.",
        f"User attention: {values['phone pushes sent']} pushes sent to user; "
        + f"{values['phone pushes after failed review']} after a failed text check.",
        f"Source problems: {len(problem_rows)}." + ("" if full else " Use --full to print the tables."),
    ])


def baseline_groups(
    sources: BaselineSources, window: log_records.Window, now: datetime,
) -> tuple[tuple[str, list[log_records.MeasureRow]], ...]:
    flow_rows = phase_flow(sources.phases, window, sources.wait_history)
    flow_rows.extend(issue_flow(sources.issue_records, window))
    flow_rows.extend(merge_flow(sources.merges, window))
    flow_rows.extend(receipt_rows(sources.receipts, sources.merges, window))
    attention_rows = attention(sources.push_report, sources.messages)
    snapshot = f"snapshot {now.astimezone(user_zone()):%Y-%m-%d %H:%M %Z}"
    capacity_rows = capacity(sources.units, sources.workers, snapshot)
    capacity_rows.extend(resource_use(sources.builds, sources.tokens, window))
    attention_rows.extend(pending_attention(sources.waiting, now, window))
    coverage_rows = rank_coverage(sources.issue_records, sources.rankings, sources.units, snapshot)
    return (
        ("Capacity on Hana", capacity_rows),
        ("Flow to Hana main (recorded phase/issue completion; local-ref merge commits)", flow_rows),
        ("User attention", attention_rows),
        ("Rank coverage", coverage_rows),
    )


def render(sources: BaselineSources, window: log_records.Window, now: datetime, full: bool = False) -> str:
    row_groups = baseline_groups(sources, window, now)
    problem_rows = log_records.source_problem_rows(sources.problems, window)
    summary = baseline_summary(window, row_groups[1][1], row_groups[2][1], problem_rows, full=full)
    if not full:
        return summary
    groups = [summary, *(log_records.blocks(title, rows) for title, rows in row_groups)]
    if problem_rows:
        groups.append(log_records.blocks("Source problems (dependent counts can be incomplete)", problem_rows))
    return "\n\n".join(groups)


@dataclass(frozen=True)
class RecordedCoverage:
    intervals: tuple[log_records.Window, ...]

    def __post_init__(self) -> None:
        if any(interval.since >= interval.until for interval in self.intervals):
            raise ValueError("coverage intervals must have a positive duration")


@dataclass(frozen=True)
class SourceUnavailable:
    reason: str


@dataclass(frozen=True)
class CoverageUnknown:
    reason: str


SourceCoverage = RecordedCoverage | SourceUnavailable | CoverageUnknown


@dataclass(frozen=True)
class RoleAccount:
    role: str
    account: str


@dataclass(frozen=True)
class AccountNotRecorded:
    reason: str


@dataclass(frozen=True)
class HourlyTokenUsage:
    hour: datetime
    account: RoleAccount | AccountNotRecorded
    repository: str
    tokens: float
    turns: int


@dataclass(frozen=True)
class HanaProject:
    name: str


@dataclass(frozen=True)
class NonHanaProject:
    name: str


@dataclass(frozen=True)
class ProjectNotRecorded:
    reason: str


ProjectEvidence = HanaProject | NonHanaProject | ProjectNotRecorded


@dataclass(frozen=True)
class CauseProject:
    subject: str
    project: ProjectEvidence


@dataclass(frozen=True)
class NonHanaCause:
    subject: str
    project: str


@dataclass(frozen=True)
class HanaCause:
    subject: str


@dataclass(frozen=True)
class CauseNotRecorded:
    reason: str


WaitCause = NonHanaCause | HanaCause | CauseNotRecorded


@dataclass(frozen=True)
class WaitCauseInterval:
    wait: str
    kind: WaitKind | WaitKindNotRecorded
    started: datetime
    until: datetime
    cause: WaitCause
    holder: NamedHolder | HolderNotNamed | MalformedHolder


@dataclass(frozen=True)
class BuildMemoryUsage:
    repository: str
    started: datetime
    until: datetime
    peak_bytes: float


@dataclass(frozen=True)
class BuildMemoryHistory:
    records: tuple[BuildMemoryUsage, ...]
    source: str


@dataclass(frozen=True)
class MemoryNotRecorded:
    reason: str


@dataclass(frozen=True)
class ComparisonWindow:
    window: log_records.Window
    sources: BaselineSources
    coverage: dict[EventSource, SourceCoverage]
    snapshot_rows: tuple[log_records.MeasureRow, ...]
    hourly_tokens: tuple[HourlyTokenUsage, ...]
    projects: tuple[CauseProject, ...]
    build_memory: BuildMemoryHistory | MemoryNotRecorded


def parse_compare_window(text: str) -> log_records.Window:
    return log_records.parse_window(text)


BASELINE_WINDOW = parse_compare_window("2026-10-13T17:24-04:00..2026-10-20T17:24-04:00")
BASELINE_DOCUMENT = SCRIPTS.parent / "docs/plans/build-followups-hierarchies-design.md"
COVERAGE_CONFIG = SCRIPTS.parent / "config/hierarchies/coverage.toml"


def recorded_baseline_rows(path: Path = BASELINE_DOCUMENT) -> tuple[log_records.MeasureRow, ...]:
    """Read the saved report, without reconstructing its snapshots from live state."""
    content = path.read_text(encoding="utf-8")
    if "## Baseline\n" not in content:
        raise ValueError(f"{path}: saved Baseline section not recorded")
    text = content.split("## Baseline\n", 1)[1].split("### Prior-week Hana merges", 1)[0]
    rows: list[log_records.MeasureRow] = []
    for line in text.splitlines():
        fields = re.split(r"\s{2,}", line.strip())
        if len(fields) == 5 and fields[2].isdigit():
            count = re.fullmatch(r"(\d+)(?:;.*)?", fields[1])
            fraction = re.fullmatch(r"(\d+)/(\d+)(?: \((\d+(?:\.\d+)?)%\))?", fields[1])
            amount: float | log_records.Fraction | log_records.NotRecorded
            if count:
                amount = float(count[1])
            elif fraction:
                amount = log_records.Fraction(float(fraction[1]), float(fraction[2]), fraction[3] is not None)
            else:
                amount = log_records.NotRecorded(fields[1])
            rows.append(log_records.MeasureRow(fields[0], fields[1], int(fields[2]), fields[3], fields[4],
                        log_records.SourceNotApplicable.STATE, log_records.Snapshot(amount)))
    if not rows:
        raise ValueError(f"{path}: saved Baseline report not recorded")
    return tuple(rows)


def covered_days(coverage: RecordedCoverage, window: log_records.Window) -> float:
    intervals = sorted((max(item.since, window.since), min(item.until, window.until))
                       for item in coverage.intervals if item.since < window.until and item.until > window.since)
    seconds = 0.0
    end = window.since
    for start, finish in intervals:
        seconds += max(0.0, (finish - max(start, end)).total_seconds())
        end = max(end, finish)
    return seconds / 86400


def rate_text(amount: float, coverage: SourceCoverage, window: log_records.Window, unit: str = "") -> str:
    if not isinstance(coverage, RecordedCoverage):
        label = "source unavailable" if isinstance(coverage, SourceUnavailable) else "coverage unknown"
        return f"not recorded ({label}: {coverage.reason})"
    days = covered_days(coverage, window)
    if days <= 0:
        return "not recorded (0 covered days)"
    partial = days < (window.until - window.since).total_seconds() / 86400
    return f"{amount * 7 / days:.1f}{unit}/week; {days:g} covered days" + (" (partial)" if partial else "")


def snapshot_row(row: log_records.MeasureRow) -> bool:
    return isinstance(row.quantity, log_records.Snapshot)


def comparable_rows(part: ComparisonWindow, now: datetime, *, before: bool) -> list[log_records.MeasureRow]:
    rows = [row for group in dict(baseline_groups(part.sources, part.window, now)).values() for row in group]
    saved = {row.measure: row for row in part.snapshot_rows}
    rows = [saved.pop(row.measure) if row.measure in saved else
            replace(row, value="comparison unavailable (no recorded before row)", n=0,
                    window="snapshot not recorded", quantity=log_records.NotRecorded("no recorded before row"))
            if before and snapshot_row(row) else row for row in rows]
    rows.extend(saved.values())
    return comparison_measure_rows(rows)


def comparison_measure_rows(rows: Sequence[log_records.MeasureRow]) -> list[log_records.MeasureRow]:
    grouped: dict[str, list[log_records.MeasureRow]] = defaultdict(list)
    for row in rows:
        if not row.detail:
            grouped[row.comparison_name or row.measure].append(row)
    result: list[log_records.MeasureRow] = []
    for name, members in grouped.items():
        first = members[0]
        if len(members) == 1:
            result.append(replace(first, measure=name))
            continue
        quantities = tuple(row.quantity for row in members)
        n = sum(row.n for row in members)
        if all(isinstance(value, log_records.EventValue) for value in quantities):
            events = tuple(value for value in quantities if isinstance(value, log_records.EventValue))
            quantity: log_records.Quantity = log_records.EventValue(
                sum(value.amount for value in events), events[0].unit,
                tuple(event for value in events for event in value.events),
                tuple(at for value in events for at in value.population),
                events[0].wait_rate, any(value.partial for value in events))
            text = f"{quantity.amount:g}"
        elif all(isinstance(value, log_records.Diagnostic) for value in quantities):
            quantity = log_records.Diagnostic(sum(value.amount for value in quantities if isinstance(value, log_records.Diagnostic)))
            text = f"{quantity.amount:g}"
        elif any(isinstance(value, log_records.NotRecorded) for value in quantities):
            reason = "; ".join(dict.fromkeys(value.reason for value in quantities if isinstance(value, log_records.NotRecorded)))
            quantity = log_records.NotRecorded(reason)
            text = "not recorded (" + reason + ")"
        else:
            quantity = first.quantity
            text = "; ".join(dict.fromkeys(row.value for row in members))
        source = (first.event_source.value if isinstance(first.event_source, EventSource)
                  and all(row.event_source is first.event_source for row in members)
                  else "; ".join(dict.fromkeys(row.source for row in members)))
        result.append(replace(first, measure=name, value=text, n=n, quantity=quantity, source=source))
    return result


@dataclass(frozen=True)
class WeeklyRate:
    amount: float
    unit: str


@dataclass(frozen=True)
class ComparedValue:
    text: str
    quantity: log_records.Quantity | WeeklyRate
    n: int


def covered_event(at: datetime, coverage: RecordedCoverage, window: log_records.Window) -> bool:
    return window.contains(at) and any(interval.contains(at) for interval in coverage.intervals)


def covered_event_count(starts: Iterable[datetime], coverage: SourceCoverage, window: log_records.Window) -> int:
    return sum(covered_event(at, coverage, window) if isinstance(coverage, RecordedCoverage) else 1 for at in starts)


def evaluate_row(row: log_records.MeasureRow, part: ComparisonWindow) -> ComparedValue:
    quantity = row.quantity
    if isinstance(quantity, log_records.NotRecorded):
        text = row.value if quantity.reason == "no recorded before row" else "not recorded (" + quantity.reason + ")"
        return ComparedValue(text, quantity, row.n)
    if isinstance(quantity, log_records.Statistic) and not quantity.amounts:
        return ComparedValue(row.value, log_records.NotRecorded("duration samples not recorded"), row.n)
    if not isinstance(quantity, log_records.EventValue):
        return ComparedValue(row.value, quantity, row.n)
    coverage = part.coverage.get(row.event_source, CoverageUnknown("continuous source coverage not recorded")) if isinstance(row.event_source, EventSource) else CoverageUnknown("event source not recorded")
    if not isinstance(coverage, RecordedCoverage) or covered_days(coverage, part.window) <= 0:
        text = rate_text(quantity.amount, coverage, part.window, quantity.unit)
        return ComparedValue(text, log_records.NotRecorded(text), row.n)
    days = covered_days(coverage, part.window)
    if quantity.amount and not quantity.events and days < (part.window.until - part.window.since).total_seconds() / 86400:
        return ComparedValue("not recorded (event times not recorded)", log_records.NotRecorded("event times not recorded"), row.n)
    amount = sum(event.amount for event in quantity.events if covered_event(event.at, coverage, part.window)) if quantity.events else quantity.amount
    n = covered_event_count(quantity.population, coverage, part.window) if quantity.population else row.n
    text = rate_text(amount, coverage, part.window, quantity.unit)
    if quantity.wait_rate:
        text += "; waits " + rate_text(n, coverage, part.window)
    if quantity.partial and days >= (part.window.until - part.window.since).total_seconds() / 86400:
        text += " (partial)"
    skipped = sum(problem.kind.count for problem in dict.fromkeys((*part.sources.problems, *part.sources.push_report.problems))
                  if problem.event_source == row.event_source and isinstance(problem.kind, log_records.RowsSkipped))
    if skipped:
        text += f"; incomplete: {skipped} rows skipped"
    return ComparedValue(text, WeeklyRate(amount * 7 / days, quantity.unit), n)


def quantity_change(before: ComparedValue, after: ComparedValue) -> str:
    left, right = before.quantity, after.quantity
    if isinstance(left, log_records.NotRecorded) or isinstance(right, log_records.NotRecorded):
        return "not recorded"
    if isinstance(left, WeeklyRate) and isinstance(right, WeeklyRate) and left.unit == right.unit:
        return f"{right.amount - left.amount:+.1f}/week"
    if isinstance(left, log_records.Snapshot) and isinstance(right, log_records.Snapshot):
        left, right = left.amount, right.amount
    if isinstance(left, log_records.NotRecorded) or isinstance(right, log_records.NotRecorded):
        return "not recorded"
    if isinstance(left, log_records.Fraction) and isinstance(right, log_records.Fraction):
        if left.denominator <= 0 or right.denominator <= 0:
            return "not recorded"
        change = right.numerator / right.denominator - left.numerator / left.denominator
        return f"{change * 100:+.1f} percentage points" if change else "unchanged"
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return f"{right - left:+.1f}" if right != left else "unchanged"
    if isinstance(left, log_records.Diagnostic) and isinstance(right, log_records.Diagnostic):
        return f"{right.amount - left.amount:+.1f}" if right.amount != left.amount else "unchanged"
    if left == right:
        return "unchanged"
    return "see before and after"


def comparison_rows(before: ComparisonWindow, after: ComparisonWindow, now: datetime) -> list[log_records.MeasureRow]:
    left = {row.measure: row for row in comparable_rows(before, now, before=True)}
    right = {row.measure: row for row in comparable_rows(after, now, before=False)}
    rows: list[log_records.MeasureRow] = []
    for key in dict.fromkeys((*left, *right)):
        first, second = left.get(key), right.get(key)
        before_value = evaluate_row(first, before) if first is not None else ComparedValue(
            "comparison unavailable (no recorded before row)", log_records.NotRecorded("no recorded before row"), 0)
        after_value = evaluate_row(second, after) if second is not None else ComparedValue(
            "comparison unavailable (no recorded after row)", log_records.NotRecorded("no recorded after row"), 0)
        source = "; ".join(dict.fromkeys(row.source for row in (first, second) if row is not None))
        windows = f"before {before.window.label}; after {after.window.label}"
        if first is not None and snapshot_row(first):
            windows += "; before " + first.window
        if second is not None and snapshot_row(second):
            windows += "; after " + second.window
        detail = f"before: {before_value.text}; after: {after_value.text}; change: {quantity_change(before_value, after_value)}"
        detail += f"; n before={before_value.n}; after={after_value.n}"
        if any(isinstance(row.quantity, log_records.Snapshot) and row.quantity.visibility_changed
               for row in (first, second) if row is not None):
            detail += "; visibility changed: current reader includes earlier lists; delta is not backlog growth"
        rows.append(log_records.MeasureRow(key, detail, after_value.n, windows, source))
    return rows


def project_cause(subject: str, projects: Sequence[CauseProject]) -> WaitCause:
    matches = {item.project for item in projects if item.subject == subject}
    if len(matches) != 1:
        return CauseNotRecorded(f"{subject}: project not recorded or conflicting")
    project = next(iter(matches))
    if isinstance(project, NonHanaProject):
        return NonHanaCause(subject, project.name)
    if isinstance(project, HanaProject):
        return HanaCause(subject)
    return CauseNotRecorded(f"{subject}: {project.reason}")


def holder_subject(holder: NamedHolder) -> str:
    return ("session:" + holder.holder.session_id if isinstance(holder.holder, SessionHolder)
            else "unit:" + holder.holder.unit)


def wait_causes(wait: HanaWait, projects: Sequence[CauseProject], window: log_records.Window) -> tuple[WaitCauseInterval, ...]:
    if not isinstance(wait.end, CompletedWait):
        return ()
    start, end = max(wait.started, window.since), min(wait.end.at, window.until)
    if start >= end:
        return ()
    if wait.kind is WaitKind.SUPPLIER:
        supplier = wait.expected_supplier
        cause = (project_cause(supplier.address, projects) if isinstance(supplier, NamedSupplier)
                 else CauseNotRecorded(supplier.reason))
        return (WaitCauseInterval(wait.identity, wait.kind, start, end, cause,
                                  HolderNotNamed("supplier evidence, no resource holder")),)
    if wait.kind not in (WaitKind.QUOTA, WaitKind.CAPACITY):
        return ()
    result: list[WaitCauseInterval] = []
    cursor = start
    for interval in holder_intervals(wait):
        if not isinstance(interval.end, CompletedWait):
            continue
        left, right = max(interval.started, start), min(interval.end.at, end)
        if right <= left:
            continue
        if cursor < left:
            result.append(WaitCauseInterval(wait.identity, wait.kind, cursor, left,
                          CauseNotRecorded("holder not named before first observation"), HolderNotNamed("no observation")))
        holder = interval.holder
        valid_resource = (holder.resource_kind is HolderResourceKind.ACCOUNT if wait.kind is WaitKind.QUOTA
                          else holder.resource_kind in (HolderResourceKind.SLOT, HolderResourceKind.MEMORY)) if isinstance(holder, NamedHolder) else False
        if isinstance(holder, NamedHolder) and valid_resource:
            cause = project_cause(holder_subject(holder), projects)
        elif isinstance(holder, NamedHolder):
            cause = CauseNotRecorded("holder resource does not match wait kind")
        else:
            cause = CauseNotRecorded(type(holder).__name__ + ": " + holder.reason)
        result.append(WaitCauseInterval(wait.identity, wait.kind, left, right, cause, holder))
        cursor = max(cursor, right)
    if cursor < end:
        result.append(WaitCauseInterval(wait.identity, wait.kind, cursor, end,
                      CauseNotRecorded("holder not named"), HolderNotNamed("no observation")))
    return tuple(result)


@dataclass(frozen=True)
class CauseMinutes:
    cause: WaitCause
    minutes: float
    wait_started: datetime


@dataclass(frozen=True)
class WaitEvidence:
    wait: HanaWait
    intervals: tuple[WaitCauseInterval, ...]
    causes: tuple[CauseMinutes, ...]
    named_minutes: float
    unnamed_minutes: float
    malformed: bool


def wait_evidence(part: ComparisonWindow) -> tuple[WaitEvidence, ...]:
    history = part.sources.wait_history
    if isinstance(history, HistoryNotRecorded):
        return ()
    result: list[WaitEvidence] = []
    for wait in history.waits:
        if wait.kind not in (WaitKind.QUOTA, WaitKind.CAPACITY, WaitKind.SUPPLIER):
            continue
        intervals = wait_causes(wait, part.projects, part.window)
        if not intervals and not (part.window.contains(wait.started) and isinstance(wait.end, UnknownEndedWait)):
            continue
        grouped: dict[WaitCause, float] = defaultdict(float)
        named = unnamed = 0.0
        for interval in intervals:
            minutes = (interval.until - interval.started).total_seconds() / 60
            grouped[interval.cause] += minutes
            if isinstance(interval.holder, NamedHolder):
                named += minutes
            else:
                unnamed += minutes
        result.append(WaitEvidence(wait, intervals, tuple(CauseMinutes(cause, minutes, wait.started) for cause, minutes in grouped.items()),
                      named, unnamed, any(isinstance(interval.holder, MalformedHolder) for interval in intervals)))
    return tuple(result)


WAIT_CAUSE_SOURCE = "progress_history.py waits; named supplier/holder and recorded project; same-hour load is context only"


def holder_evidence_row(evidence: WaitEvidence, window: log_records.Window) -> log_records.MeasureRow:
    return log_records.MeasureRow(f"Hana wait holder minutes / {evidence.wait.identity}",
        f"named {evidence.named_minutes:.1f} min; not named {evidence.unnamed_minutes:.1f} min", 1, window.label,
        WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.Diagnostic(1))


def cause_description(item: CauseMinutes, part: ComparisonWindow) -> str:
    cause = item.cause
    if isinstance(cause, NonHanaCause):
        label = f"non-Hana cause {cause.subject}; project {cause.project}"
    elif isinstance(cause, HanaCause):
        label = f"Hana cause {cause.subject}"
    else:
        label = "cause not recorded: " + cause.reason
    coverage = part.coverage.get(EventSource.WAITS, CoverageUnknown("continuous wait coverage not recorded"))
    count = covered_event_count((item.wait_started,), coverage, part.window)
    return f"{label}; {item.minutes:.1f} min; " + rate_text(count, coverage, part.window)


def wait_cause_rows(part: ComparisonWindow) -> list[log_records.MeasureRow]:
    history = part.sources.wait_history
    if isinstance(history, HistoryNotRecorded):
        return [log_records.MeasureRow("Hana wait causes", "not recorded: " + history.reason, 0, part.window.label,
                WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.NotRecorded(history.reason))]
    rows: list[log_records.MeasureRow] = []
    for evidence in wait_evidence(part):
        wait = evidence.wait
        if not evidence.intervals:
            rows.append(log_records.MeasureRow(f"Hana wait cause / {wait.identity}", "cause not recorded; duration unknown",
                        1, part.window.label, WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.NotRecorded("duration unknown")))
            continue
        rows.append(holder_evidence_row(evidence, part.window))
        rows.extend(log_records.MeasureRow(f"Hana wait cause / {wait.identity}", cause_description(item, part),
                    1, part.window.label, WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.Diagnostic(1))
                    for item in evidence.causes)
        rows.extend(wait_load_context(wait, evidence.intervals, part))
    return rows


def wait_hours(intervals: Sequence[WaitCauseInterval]) -> tuple[datetime, ...]:
    hours: set[datetime] = set()
    for interval in intervals:
        hour = interval.started.replace(minute=0, second=0, microsecond=0)
        while hour < interval.until:
            hours.add(hour)
            hour += timedelta(hours=1)
    return tuple(sorted(hours))


def wait_accounts(intervals: Sequence[WaitCauseInterval], hour: datetime) -> set[str]:
    return {interval.holder.resource for interval in intervals if isinstance(interval.holder, NamedHolder)
            and interval.holder.resource_kind is HolderResourceKind.ACCOUNT
            and interval.started < hour + timedelta(hours=1) and interval.until > hour}


def wait_load_context(wait: HanaWait, intervals: Sequence[WaitCauseInterval], part: ComparisonWindow) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    hours = wait_hours(intervals)
    source = "usage ledger turns + account_role_changes at each turn; same-hour context, never proof of cause"
    if wait.kind is WaitKind.QUOTA:
        for hour in sorted(hours):
            selected, unknown = hourly_context_tokens(hour, wait_accounts(intervals, hour), part.hourly_tokens)
            value = "; ".join(f"{turn.account.role}/{turn.account.account}: {turn.tokens:,.0f} tokens ({turn.repository})"
                              for turn in selected if isinstance(turn.account, RoleAccount))
            if not value:
                value = "not recorded (no matched non-Hana account/role token records)"
            if unknown:
                value += f"; {sum(turn.turns for turn in unknown)} turns with attribution not recorded"
            rows.append(log_records.MeasureRow(f"Same-hour non-Hana tokens / {wait.identity} / {hour.isoformat()}",
                        value, sum(turn.turns for turn in selected), part.window.label, source, EventSource.TOKENS,
                        log_records.Diagnostic(sum(turn.turns for turn in selected))))
    elif wait.kind is WaitKind.CAPACITY:
        for hour in sorted(hours):
            if isinstance(part.build_memory, MemoryNotRecorded):
                value, n = "not recorded: " + part.build_memory.reason, 0
                memory_source = "buildlog steps on natedev"
            else:
                builds = hourly_context_builds(hour, part.build_memory)
                value = "; ".join(f"{record.repository}: peak {record.peak_bytes:g} bytes" for record in builds)
                value = value or "not recorded (no overlapping attributed non-Hana memory records)"
                n, memory_source = len(builds), part.build_memory.source
            rows.append(log_records.MeasureRow(f"Same-hour non-Hana build memory / {wait.identity} / {hour.isoformat()}",
                        value, n, part.window.label, memory_source + "; per-step peaks, not concurrent usage; context only",
                        EventSource.BUILDS, log_records.Diagnostic(n)))
    return rows


def hourly_context_tokens(
    hour: datetime, resources: set[str], turns: Sequence[HourlyTokenUsage],
) -> tuple[tuple[HourlyTokenUsage, ...], tuple[HourlyTokenUsage, ...]]:
    selected = tuple(turn for turn in turns if turn.hour == hour and isinstance(turn.account, RoleAccount)
                     and (turn.account.account in resources or f"{turn.account.role}/{turn.account.account}" in resources
                          or f"{account_group(turn.account.role)}/{turn.account.account}" in resources)
                     and isinstance(repository_project(turn.repository), NonHanaProject))
    unknown = tuple(turn for turn in turns if turn.hour == hour
                    and (isinstance(turn.account, AccountNotRecorded)
                         or isinstance(repository_project(turn.repository), ProjectNotRecorded)))
    return selected, unknown


def hourly_context_builds(hour: datetime, memory: BuildMemoryHistory) -> tuple[BuildMemoryUsage, ...]:
    return tuple(record for record in memory.records if record.started < hour + timedelta(hours=1)
                 and record.until > hour and isinstance(repository_project(record.repository), NonHanaProject))


class WaitMeasure(Enum):
    NAMED = "holder named"
    UNNAMED = "holder not named"
    HANA = "Hana cause"
    UNKNOWN = "cause not recorded"
    UNKNOWN_DURATION = "cause not recorded; duration unknown"


def compact_wait_rows(part: ComparisonWindow) -> list[log_records.MeasureRow]:
    history = part.sources.wait_history
    if isinstance(history, HistoryNotRecorded):
        return wait_cause_rows(part)
    evidence_rows = wait_evidence(part)
    minutes: dict[tuple[WaitKind, WaitMeasure], float] = defaultdict(float)
    counts: dict[tuple[WaitKind, WaitMeasure], dict[str, datetime]] = defaultdict(dict)
    token_context: set[HourlyTokenUsage] = set()
    memory_context: set[BuildMemoryUsage] = set()
    memory_waits: set[str] = set()
    rows: list[log_records.MeasureRow] = []
    for evidence in evidence_rows:
        wait = evidence.wait
        if not isinstance(wait.kind, WaitKind):
            continue
        kind = wait.kind
        if not evidence.intervals:
            counts[(kind, WaitMeasure.UNKNOWN_DURATION)][wait.identity] = wait.started
        for label, amount in ((WaitMeasure.NAMED, evidence.named_minutes), (WaitMeasure.UNNAMED, evidence.unnamed_minutes)):
            if amount:
                minutes[(kind, label)] += amount
                counts[(kind, label)][wait.identity] = wait.started
        if evidence.malformed:
            rows.append(holder_evidence_row(evidence, part.window))
        named = tuple(item for item in evidence.causes if isinstance(item.cause, NonHanaCause))
        if named:
            rows.append(log_records.MeasureRow(f"Hana wait cause / {wait.identity}",
                        "; ".join(cause_description(item, part) for item in named), 1, part.window.label,
                        WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.Diagnostic(1)))
        for item in evidence.causes:
            if not isinstance(item.cause, NonHanaCause):
                label = WaitMeasure.HANA if isinstance(item.cause, HanaCause) else WaitMeasure.UNKNOWN
                minutes[(kind, label)] += item.minutes
                counts[(kind, label)][wait.identity] = wait.started
        for hour in wait_hours(evidence.intervals):
            if kind is WaitKind.QUOTA:
                selected, unknown = hourly_context_tokens(hour, wait_accounts(evidence.intervals, hour), part.hourly_tokens)
                token_context.update((*selected, *unknown))
            elif kind is WaitKind.CAPACITY:
                memory_waits.add(wait.identity)
                if isinstance(part.build_memory, BuildMemoryHistory):
                    memory_context.update(hourly_context_builds(hour, part.build_memory))
    coverage = part.coverage.get(EventSource.WAITS, CoverageUnknown("continuous wait coverage not recorded"))
    for (kind, label), identities in counts.items():
        value = label.value
        if label is not WaitMeasure.UNKNOWN_DURATION:
            value += f"; {minutes[(kind, label)]:.1f} min"
        value += "; waits " + rate_text(covered_event_count(identities.values(), coverage, part.window), coverage, part.window)
        rows.append(log_records.MeasureRow("Hana wait causes / " + kind.value + " / " + label.value, value,
                    len(identities), part.window.label, WAIT_CAUSE_SOURCE, EventSource.WAITS, log_records.Diagnostic(len(identities))))
    if any(evidence.wait.kind is WaitKind.QUOTA for evidence in evidence_rows):
        totals: dict[RoleAccount, float] = defaultdict(float)
        unknown = 0
        for turn in token_context:
            if isinstance(turn.account, RoleAccount) and isinstance(repository_project(turn.repository), NonHanaProject):
                totals[turn.account] += turn.tokens
            else:
                unknown += turn.turns
        value = "; ".join(f"{account.role}/{account.account}: {amount:,.0f} tokens" for account, amount in sorted(
            totals.items(), key=lambda item: (item[0].role, item[0].account))) or "not recorded (no matched non-Hana account/role token records)"
        if unknown:
            value += f"; {unknown} turns with attribution not recorded"
        rows.append(log_records.MeasureRow("Same-hour non-Hana tokens / WaitingForQuotaRefusal", value,
                    sum(turn.turns for turn in token_context), part.window.label,
                    "usage ledger turns + account_role_changes; unique same-hour turns across waits; context only", EventSource.TOKENS,
                    log_records.Diagnostic(sum(turn.turns for turn in token_context))))
    if memory_waits:
        if isinstance(part.build_memory, MemoryNotRecorded):
            value = "not recorded: " + part.build_memory.reason
        else:
            peak = max((record.peak_bytes for record in memory_context), default=0)
            value = f"maximum recorded step peak {peak:g} bytes" if memory_context else "not recorded (no overlapping attributed non-Hana memory records)"
        rows.append(log_records.MeasureRow("Same-hour non-Hana build memory / WaitingForMemoryOrSlot", value,
                    len(memory_context), part.window.label, "buildlog steps on natedev; unique overlapping steps; per-step peaks, not concurrent usage; context only",
                    EventSource.BUILDS, log_records.Diagnostic(len(memory_context))))
    return rows


def origin_project(origin: PhaseOrigin) -> ProjectEvidence:
    if hana_origin(origin, ()):
        name = origin.production.value if isinstance(origin.production, RecordedOrigin) else origin.working_dir
        return HanaProject(name)
    if isinstance(origin.repository, RecordedOrigin):
        return NonHanaProject(origin.repository.value)
    if isinstance(origin.production, RecordedOrigin):
        return NonHanaProject(origin.production.value)
    return ProjectNotRecorded("project attribution not recorded")


def repository_project(path: str) -> ProjectEvidence:
    if path in ("", "repository not recorded", "cwd not recorded", "cwd has no repository", "unknown"):
        return ProjectNotRecorded("repository attribution not recorded")
    return origin_project(PhaseOrigin(path, OriginNotRecorded("plan not recorded"),
                          OriginNotRecorded("production not recorded"), RecordedOrigin(path)))


ROLE_ACCOUNT_GROUPS = {
    "impl": "workers", "test": "workers", "fix": "workers", "review": "workers",
    "worker": "workers", "workers": "workers", "director": "units", "unit": "units", "units": "units",
    "showrunner": "showrunners", "interactive": "showrunners", "showrunners": "showrunners",
}


def account_group(role: str) -> str:
    return ROLE_ACCOUNT_GROUPS.get(role, role)


def read_hourly_tokens(path: Path, window: log_records.Window) -> tuple[HourlyTokenUsage, ...]:
    with closing(usage_report.open_read_store(path)) as db:
        db.create_function("account_group", 1, account_group, deterministic=True)
        query = """SELECT t.at,t.role,f.folder,
            t.input+t.cache_write_5m+t.cache_write_1h+t.cache_read+t.output AS tokens,
            (SELECT c.label FROM account_role_changes c WHERE c.machine=t.machine
             AND c.role=account_group(t.role)
             AND julianday(c.at)<=julianday(t.at) ORDER BY julianday(c.at) DESC LIMIT 1) AS account
            FROM turns t LEFT JOIN folders f USING(machine,family,parent_id,agent_id)
            WHERE t.machine='natedev' AND julianday(t.at)>=julianday(?) AND julianday(t.at)<julianday(?)"""
        totals: dict[tuple[datetime, RoleAccount | AccountNotRecorded, str], float] = defaultdict(float)
        counts: Counter[tuple[datetime, RoleAccount | AccountNotRecorded, str]] = Counter()
        for row in cast(Iterable[sqlite3.Row], db.execute(query, (window.since.isoformat(), window.until.isoformat()))):
            values = cast(tuple[object, ...], tuple(row))
            at = log_records.required_time(values[0]).astimezone(timezone.utc)
            role, folder, account = values[1:3] + (values[4],)
            identity = RoleAccount(role, account) if isinstance(role, str) and role and isinstance(account, str) and account else AccountNotRecorded("role account at turn time not recorded")
            repo = repository(folder) if isinstance(folder, str) and folder else "repository not recorded"
            key = (at.replace(minute=0, second=0, microsecond=0), identity, repo)
            totals[key] += log_records.field_number({"tokens": values[3]}, "tokens")
            counts[key] += 1
        return tuple(HourlyTokenUsage(hour, account, repo, amount, counts[(hour, account, repo)])
                     for (hour, account, repo), amount in totals.items())


def read_build_memory(path: Path) -> BuildMemoryHistory | MemoryNotRecorded:
    try:
        with closing(usage_report.open_read_store(path)) as db:
            rows = cast(Iterable[sqlite3.Row], db.execute(
                "SELECT repo,started_at,duration_s,peak_mem_bytes FROM steps WHERE host='natedev' AND peak_mem_bytes IS NOT NULL"))
            records: list[BuildMemoryUsage] = []
            for row in rows:
                record: log_records.JsonObject = dict(zip(("repository", "at", "seconds", "bytes"), cast(tuple[object, ...], tuple(row))))
                started = log_records.record_time(record)
                records.append(BuildMemoryUsage(log_records.field_text(record, "repository"), started,
                               started + timedelta(seconds=log_records.field_number(record, "seconds")), log_records.field_number(record, "bytes")))
        return BuildMemoryHistory(tuple(records), str(path) + ": steps.peak_mem_bytes; natedev")
    except (OSError, ValueError, sqlite3.Error) as error:
        return MemoryNotRecorded(str(path) + ": " + str(error))


def read_cause_projects(folder: Path, units: Sequence[Unit], problems: list[log_records.SourceProblem]) -> tuple[CauseProject, ...]:
    projects: dict[str, set[ProjectEvidence]] = defaultdict(set)
    for path in folder.glob("*.jsonl"):
        records = read_source(str(path), lambda: json_lines(path, lambda record: record), [], problems,
                              event_source=log_records.SourceNotApplicable.ATTRIBUTION)
        for record in records:
            try:
                if log_records.field_text(record, "event_type") != "phase_started":
                    continue
                origin = PhaseOrigin(log_records.field_text(record, "working_dir"), origin_field(record, "plan_doc"),
                                     origin_field(record, "production"), origin_field(record, "repository"))
                unit = origin_field(record, "unit")
                project = origin_project(origin)
                if isinstance(unit, RecordedOrigin):
                    projects["unit:" + unit.value].add(project)
                    projects[unit.value].add(project)
                sessions = [log_records.field_text(record, field) for field in ("owner_session", "session_id")]
                if "main_agent" in record:
                    sessions.append(log_records.field_text(log_records.object_record(record["main_agent"]), "session_id"))
                for identity in sessions:
                    if identity:
                        for subject in ("session:" + identity, "codex:" + identity, identity):
                            projects[subject].add(project)
            except ValueError as error:
                problems.append(log_records.SourceProblem(str(path), "cause project: " + str(error),
                                log_records.SourceNotApplicable.ATTRIBUTION, log_records.RowsSkipped(1)))
    for unit in units:
        subjects = ("unit:" + unit.name, unit.name)
        recorded = {project for subject in subjects for project in projects.get(subject, ())}
        if not recorded:
            recorded = {origin_project(PhaseOrigin(unit.worktree, OriginNotRecorded("plan not recorded"),
                        RecordedOrigin(unit.production), RecordedOrigin(unit.repository)))}
        for subject in subjects:
            projects[subject].update(recorded)
    return tuple(CauseProject(subject, project) for subject, values in projects.items() for project in values)


def comparison_coverage(sources: BaselineSources) -> dict[EventSource, SourceCoverage]:
    coverage: dict[EventSource, SourceCoverage] = {
        key: CoverageUnknown("continuous coverage bounds not recorded by this source") for key in EventSource}
    problems = tuple(dict.fromkeys((*sources.problems, *sources.push_report.problems)))
    for key in EventSource:
        failures = [problem.reason for problem in problems if problem.event_source is key
                    and isinstance(problem.kind, log_records.SourceUnreadable)]
        if failures:
            coverage[key] = SourceUnavailable("; ".join(failures))
    return coverage


def load_comparison(before: log_records.Window, after: log_records.Window) -> tuple[ComparisonWindow, ComparisonWindow]:
    state = Path.home() / ".local/state"
    notifications = push_audit.load_sources(state / "notify")
    config_problems: list[log_records.SourceProblem] = []
    claims = push_audit.read_coverage(COVERAGE_CONFIG, datetime.now(timezone.utc), config_problems)
    for key in claims.keys() - {source.value for source in EventSource}:
        config_problems.append(log_records.SourceProblem(str(COVERAGE_CONFIG), f"coverage config [{key}]: unknown source key", event_source=log_records.SourceNotApplicable.CONFIG))
    parts: list[ComparisonWindow] = []
    memory = read_build_memory(state / "buildlog/index.sqlite")
    for index, window in enumerate((before, after)):
        sources = load_window_sources(window, notifications)
        sources.problems.extend(config_problems)
        saved: tuple[log_records.MeasureRow, ...] = ()
        if index == 0 and window == BASELINE_WINDOW:
            saved = read_source(str(BASELINE_DOCUMENT), recorded_baseline_rows, (), sources.problems, event_source=log_records.SourceNotApplicable.STATE)
            saved = tuple(row for row in saved if row.window.startswith("snapshot"))
        hourly = read_source("hourly usage ledger", lambda: read_hourly_tokens(collect.store_path(), window), (), sources.problems, event_source=EventSource.TOKENS)
        projects = read_cause_projects(unit_runs.history_root_in(state) / "runs", sources.units, sources.problems)
        coverage = comparison_coverage(sources)
        for key in EventSource:
            if key.value in claims and not isinstance(coverage[key], SourceUnavailable):
                coverage[key] = RecordedCoverage(claims[key.value].intervals)
        parts.append(ComparisonWindow(window, sources, coverage, saved, hourly, projects, memory))
    return parts[0], parts[1]


def render_compare(before: ComparisonWindow, after: ComparisonWindow, now: datetime, full: bool = False) -> str:
    rows = comparison_rows(before, after, now)
    by_name = {row.measure: row.value for row in rows}
    non_hana = sum(isinstance(item.cause, NonHanaCause) for part in (before, after)
                   for evidence in wait_evidence(part) for item in evidence.causes)
    problems = list(dict.fromkeys((*before.sources.problems, *after.sources.problems)))
    summary = "\n".join(log_records.wrap_text(line) for line in (
        "Before: " + before.window.label,
        "After: " + after.window.label,
        "Hana: merges " + by_name.get("Hana merges / origin/init/catalyst", "not recorded"),
        "User attention: failed text checks " + by_name.get("phone pushes after failed review", "not recorded"),
        f"Non-Hana causes: {non_hana} named wait causes; " + ("HI10 proposal evidence recorded." if non_hana else "no named non-Hana cause recorded."),
        f"Source problems: {len(problems)}." + ("" if full else " Use --full to print the tables."),
    ))
    if not full:
        return summary
    groups = [summary]
    groups.extend(log_records.blocks("Baseline comparison (rates per covered week; snapshots retain their dates)", list(chunk[1]))
                  for chunk in groupby(rows, key=lambda row: row.window))
    for label, part in (("Before", before), ("After", after)):
        groups.append(log_records.blocks(label + " waits, named causes and same-hour context", compact_wait_rows(part)))
        groups.append(log_records.blocks(label + " source coverage", [log_records.MeasureRow(
            key.value, f"{covered_days(value, part.window):g} covered days" if isinstance(value, RecordedCoverage)
            else type(value).__name__ + ": " + value.reason, 0, part.window.label, key.value)
            for key, value in part.coverage.items()]))
    if problems:
        groups.append(log_records.blocks("Source problems", log_records.source_problem_rows(problems, after.window)))
    return "\n\n".join(groups)


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("baseline", "compare"))
    _ = parser.add_argument("--before", metavar="START..END", default="", help="compare before window; date-only END includes that day; local zone without offset")
    _ = parser.add_argument("--after", metavar="START..END", default="", help="compare after window; date-only END includes that day; local zone without offset")
    _ = parser.add_argument("--since", metavar="DATE_OR_TIME", default="")
    _ = parser.add_argument("--until", metavar="DATE_OR_TIME", default="")
    _ = parser.add_argument("--full", action="store_true", help="print the tables after the summary")
    args = parser.parse_args(arguments)
    now = datetime.now(timezone.utc)
    since_text = cast(str, args.since)
    until_text = cast(str, args.until)
    try:
        if cast(str, args.command) == "compare":
            before = parse_compare_window(cast(str, args.before))
            after = parse_compare_window(cast(str, args.after))
            comparison = load_comparison(before, after)
            print(render_compare(*comparison, now, full=cast(bool, args.full)))
            return 0
        window = log_records.window_bounds(since_text, until_text, now)
    except ValueError as error:
        parser.error(str(error))
    print(render(load_sources(window), window, now, full=cast(bool, args.full)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
