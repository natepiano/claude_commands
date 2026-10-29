#!/usr/bin/env python3
"""Read natedev's hierarchy records without changing the live process.

Counts describe recorded work, not proof that commits reached Hana main. Windows
are half open. Date bounds use the user's zone; a date-only --until includes that
whole day. A date-time --until is the excluded end.
The baseline command prints a short summary. Add --full to print the tables
after the summary.
"""
from __future__ import annotations

import argparse
from contextlib import closing, redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import io
from functools import lru_cache
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


@dataclass(frozen=True)
class PhaseOrigin:
    working_dir: str
    plan: str
    production: str
    repository: str


@dataclass(frozen=True)
class PhaseCompletion:
    identity: str
    at: datetime
    hana: bool
    seconds: float | DurationNotRecorded


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
class WaitCauseNotRecorded:
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
        return f"run={self.run}; phase={self.phase}; instance={self.instance}; cwd={self.working_dir}"


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
class HanaWait:
    identity: str
    kind: WaitKind | WaitCauseNotRecorded
    started: datetime
    end: CompletedWait | UnknownEndedWait
    owner: RecordedWaitOwner | WaitOwnerNotRecorded
    phase: WaitPhaseInstance | ProductionHandoff | WaitPhaseNotRecorded
    expected_supplier: NamedSupplier | NoNamedSupplier
    reason: RecordedWaitReason | WaitReasonNotRecorded

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
    merges: list[HanaMerge] | MergeHistoryNotRecorded = MergeHistoryNotRecorded.MISSING
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
            "showrunners + units_table production_units loader",
        ))
    for tree in sorted({worker.worktree for worker in workers}):
        group = [worker for worker in workers if worker.worktree == tree]
        orphan = sum(not worker.director_running for worker in group)
        scope = "Hana" if group[0].hana else "other"
        worktree = tree or "worktree not recorded"
        if not tree:
            scope = "scope not recorded"
        rows.append(log_records.MeasureRow(
            f"workers / {worktree} / {scope}", f"{len(group)}; director not running {orphan}",
            len(group), snapshot, "codex_mesh via session roster; natedev only",
        ))
    if not units:
        rows.append(log_records.MeasureRow("live units", "0", 0, snapshot, "showrunners + units_table"))
    if not workers:
        rows.append(log_records.MeasureRow("workers", "0", 0, snapshot, "session roster; natedev only"))
    return rows


def resource_use(builds: Sequence[BuildStep], tokens: Sequence[WindowTokenUsage], window: log_records.Window) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for build in builds:
        if window.contains(build.started):
            lane = "test" if build.step in ("test", "nextest", "example-test") else "build/check"
            groups[(build.repository, lane)].append(build.seconds / 60)
    for (repo, lane), values in sorted(groups.items()):
        rows.append(log_records.MeasureRow(
            f"{lane} minutes / {repo}", f"{sum(values):.1f}", len(values), window.label,
            "buildlog index steps; natedev only; by start time",
        ))
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
        ))
    if not groups:
        rows.append(log_records.MeasureRow("build/test minutes", "no records in window", 0, window.label, "buildlog index; natedev only"))
    if not totals:
        rows.append(log_records.MeasureRow("tokens", "no records in window", 0, window.label, "usage ledger; natedev only"))
    return rows


def distribution(name: str, values: Sequence[float], window: log_records.Window, source: str) -> log_records.MeasureRow:
    ordered = sorted(values)
    value = "not recorded"
    if ordered:
        p90 = ordered[math.ceil(.9 * len(ordered)) - 1]
        value = f"median {median(ordered):.1f} min; p90 {p90:.1f} min"
    return log_records.MeasureRow(name, value, len(ordered), window.label, source)


def object_rows(record: log_records.JsonObject, key: str) -> list[log_records.JsonObject]:
    value = record.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key}: expected an array")
    return [log_records.object_record(item) for item in cast(list[object], value)]


def wait_candidates(record: log_records.JsonObject) -> tuple[WaitCandidate, ...]:
    return tuple(WaitCandidate(log_records.field_text(item, "run_id"), log_records.field_text(item, "phase_id"),
                               log_records.field_text(item, "phase_instance_id"), log_records.field_text(item, "working_dir"))
                 for item in object_rows(record, "candidates"))


def wait_origin(record: log_records.JsonObject, prefixes: Sequence[str]) -> bool:
    return hana_origin(PhaseOrigin(
        log_records.field_text(record, "working_dir") or log_records.field_text(record, "unit"),
        log_records.field_text(record, "plan_doc"), log_records.field_text(record, "production"),
        log_records.field_text(record, "repository")), prefixes)


def parse_wait_report(record: log_records.JsonObject, prefixes: Sequence[str] = ()) -> HanaWaitHistory:
    phases: list[PhaseWaitCoverage] = []
    hana_phases: set[tuple[str, str]] = set()
    for phase in object_rows(record, "phases"):
        if not wait_origin(phase, prefixes):
            continue
        run, instance = log_records.field_text(phase, "run_id"), log_records.field_text(phase, "phase_instance_id")
        hana_phases.add((run, instance))
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
    waits: dict[str, HanaWait] = {}
    misses: dict[str, AttributionMiss] = {}
    for item in (*object_rows(record, "waits"), *object_rows(record, "missing")):
        attribution = log_records.object_record(item.get("wait_attribution", {}))
        candidates = wait_candidates(attribution)
        owns_phase = (log_records.field_text(item, "run_id"), log_records.field_text(item, "phase_instance_id")) in hana_phases
        candidate_hana = any(wait_origin({"working_dir": candidate.working_dir}, prefixes)
                             or (candidate.run, candidate.instance) in hana_phases for candidate in candidates)
        if not (owns_phase or wait_origin(item, prefixes) or candidate_hana):
            continue
        identity = log_records.field_text(item, "wait_id")
        if not identity:
            raise ValueError("wait_id not recorded")
        if log_records.field_text(attribution, "state") in ("UnresolvedAttribution", "UnresolvedSupplier"):
            misses[identity] = AttributionMiss(identity, log_records.field_text(attribution, "reason"), candidates)
            # A candidate Hana phase does not establish that this was a Hana wait.
            continue
        kind_text = log_records.field_text(item, "wait_kind")
        kind: WaitKind | WaitCauseNotRecorded
        if kind_text:
            try:
                kind = WaitKind(kind_text)
            except ValueError as error:
                raise ValueError(f"unknown wait kind: {kind_text}") from error
        else:
            kind = WaitCauseNotRecorded(log_records.field_text(item, "wait_reason") or "wait cause not recorded")
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
        waits[identity] = HanaWait(identity, kind, started, end, owner, phase_owner, supplier, wait_reason)
    return HanaWaitHistory(tuple(waits.values()), tuple(phases), tuple(misses.values()))


def read_wait_history(
    root: Path, window: log_records.Window, problems: list[log_records.SourceProblem], prefixes: Sequence[str] = (),
) -> HanaWaitHistory | HistoryNotRecorded:
    external = root / "wait_attribution.jsonl"
    if not (root / "runs").is_dir() and not external.is_file():
        raise FileNotFoundError(root / "runs")
    if external.exists():
        # The supplier reader skips corrupt external rows without a diagnostic.
        external_records = json_lines(external, log_records.object_record)
        if any(log_records.field_text(item, "event_type") not in
               ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded") for item in external_records):
            raise ValueError("external wait history contains an unknown event type")
        if any(not log_records.field_text(item, "wait_id") for item in external_records):
            raise ValueError("external wait history contains a wait event without wait_id")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "direct/progress_history.py"), "waits",
         "--since", window.since.isoformat(), "--until", window.until.isoformat()],
        env={**os.environ, "UNIT_HISTORY_DIR": str(root)}, capture_output=True, text=True, check=True, timeout=60,
    )
    report = log_records.object_record(cast(object, json.loads(result.stdout)))
    ignored = log_records.field_number(report, "ignored_n")
    if ignored:
        problems.append(log_records.SourceProblem("progress_history waits", f"{int(ignored)} unreadable or unsupported history rows; counts can be incomplete"))
    return parse_wait_report(report, prefixes)


def wait_coverage_suffix(history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window) -> str:
    if isinstance(history, HistoryNotRecorded):
        return ""
    missing = sum(isinstance(phase.state, HistoryNotRecorded) for phase in history.phases)
    uncertain = sum(isinstance(phase.state, UncertainWaitCoverage) for phase in history.phases)
    cause_missing = sum(isinstance(wait.kind, WaitCauseNotRecorded) for wait in history.waits if window.contains(wait.started))
    attribution_missing = len(history.attribution_misses)
    if not (missing or uncertain or cause_missing or attribution_missing):
        return ""
    return (f"; partial coverage: HistoryNotRecorded={missing}, UncertainCoverage={uncertain}, "
            + f"WaitCauseNotRecorded={cause_missing}, attribution_miss_n={attribution_missing}")


def hana_wait_rows(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window,
) -> list[log_records.MeasureRow]:
    source = "progress_history.py waits; phase starts and wait starts in window; completed durations only"
    rows: list[log_records.MeasureRow] = []
    waits = [wait for wait in history.waits if window.contains(wait.started)] if isinstance(history, HanaWaitHistory) else []
    phases = history.phases if isinstance(history, HanaWaitHistory) else ()
    covered = sum(isinstance(phase.state, CoveredWaitHistory) for phase in phases)
    missing = [phase for phase in phases if isinstance(phase.state, HistoryNotRecorded)]
    uncertain = [phase for phase in phases if isinstance(phase.state, UncertainWaitCoverage)]
    cause_missing = sum(isinstance(wait.kind, WaitCauseNotRecorded) for wait in waits)
    attribution_missing = len(history.attribution_misses) if isinstance(history, HanaWaitHistory) else 0
    for kind in WaitKind:
        completed = [wait for wait in waits if wait.kind == kind and isinstance(wait.end, CompletedWait)]
        minutes = sum((wait.end.at - wait.started).total_seconds() / 60 for wait in completed if isinstance(wait.end, CompletedWait))
        complete_coverage = covered and not (missing or uncertain or cause_missing or attribution_missing)
        value = f"{minutes:.1f} min" if completed or complete_coverage else "not recorded (HistoryNotRecorded)"
        value += wait_coverage_suffix(history, window)
        rows.append(log_records.MeasureRow(f"Hana wait / {kind.value}", value, len(completed), window.label, source))
        open_waits = [wait for wait in waits if wait.kind == kind and isinstance(wait.end, UnknownEndedWait)]
        if open_waits:
            rows.append(log_records.MeasureRow(f"Hana wait unknown-ended / {kind.value}", "duration not recorded", len(open_waits), window.label, source))
    for wait in waits:
        if isinstance(wait.kind, WaitCauseNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitCauseNotRecorded", wait.kind.reason, 1, window.label, wait.identity))
            if isinstance(wait.end, UnknownEndedWait):
                rows.append(log_records.MeasureRow("Hana wait unknown-ended / WaitCauseNotRecorded", "duration not recorded", 1, window.label, wait.identity))
        if wait.production_handoff:
            owner_text = wait.owner.session if isinstance(wait.owner, RecordedWaitOwner) else f"not recorded: {wait.owner.reason}"
            rows.append(log_records.MeasureRow("Hana wait / production handoff", f"no phase; owner={owner_text}", 1, window.label, wait.identity))
        elif isinstance(wait.phase, WaitPhaseNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitPhaseNotRecorded", wait.phase.reason, 1, window.label, wait.identity))
        if isinstance(wait.owner, WaitOwnerNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait / WaitOwnerNotRecorded", wait.owner.reason, 1, window.label, wait.identity))
    if isinstance(history, HistoryNotRecorded) or not phases and not waits:
        reason = history.reason if isinstance(history, HistoryNotRecorded) else "no Hana phase or wait records in window"
        rows.append(log_records.MeasureRow("Hana wait coverage / HistoryNotRecorded", "not recorded (HistoryNotRecorded)", 0, window.label, source + "; " + reason))
    for phase in missing:
        if isinstance(phase.state, HistoryNotRecorded):
            rows.append(log_records.MeasureRow("Hana wait coverage / HistoryNotRecorded", phase.state.reason, 1, window.label, f"{phase.run}/{phase.instance}"))
    if uncertain:
        rows.append(log_records.MeasureRow("Hana wait / uncertain_phase_n", str(len(uncertain)), len(uncertain), window.label, source))
    for phase in uncertain:
        if isinstance(phase.state, UncertainWaitCoverage):
            detail = phase.state.reason + "; candidates: " + " | ".join(candidate.label for candidate in phase.state.candidates)
            rows.append(log_records.MeasureRow("Hana wait coverage / UncertainCoverage", detail, 1, window.label, f"{phase.run}/{phase.instance}"))
    if isinstance(history, HanaWaitHistory) and history.attribution_misses:
        misses = history.attribution_misses
        rows.append(log_records.MeasureRow("Hana wait / attribution_miss_n", str(len(misses)), len(misses), window.label, source))
        for miss in misses:
            detail = miss.reason + "; candidates: " + " | ".join(candidate.label for candidate in miss.candidates)
            rows.append(log_records.MeasureRow("Hana wait attribution / Unresolved", detail, 1, window.label, miss.wait_id))
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
    merges: Sequence[HanaMerge] | MergeHistoryNotRecorded, window: log_records.Window,
) -> list[log_records.MeasureRow]:
    source = "progress_history merge_receipts.jsonl; exact project, target branch and merge commit"
    if isinstance(receipts, HistoryNotRecorded):
        rows = [log_records.MeasureRow("merge receipts / all projects", f"not recorded (HistoryNotRecorded): {receipts.reason}", 0, window.label, source)]
        recorded: Sequence[MergeReceipt] = ()
    else:
        selected = {(item.project, item.target_branch, item.commit): item for item in receipts if window.contains(item.merged_at)}
        recorded = tuple(selected.values())
        rows = [log_records.MeasureRow("merge receipts / all projects", str(len(selected)), len(selected), window.label, source)]
    if isinstance(merges, MergeHistoryNotRecorded):
        rows.append(log_records.MeasureRow("Hana merge receipts / origin/init/catalyst", "not recorded (merge history not recorded)", 0, window.label, source))
        return rows
    selected_merges = {merge.commit: merge for merge in merges if window.contains(merge.at)}
    matched = 0
    for merge in selected_merges.values():
        result = receipt_for_merge(merge, recorded)
        if isinstance(result, ReceiptMissing):
            rows.append(log_records.MeasureRow("Hana merge / ReceiptMissing", result.commit, 1, window.label, source))
        else:
            matched += 1
    rows.insert(1, log_records.MeasureRow("Hana merge receipts / origin/init/catalyst", f"{matched}/{len(selected_merges)}", len(selected_merges), window.label, source))
    return rows


WAIT_HISTORY_NOT_RECORDED = HistoryNotRecorded()


def wait_share_row(
    history: HanaWaitHistory | HistoryNotRecorded, window: log_records.Window, phase_minutes: Sequence[float],
) -> log_records.MeasureRow:
    value = "not recorded (HistoryNotRecorded)"
    suffix = wait_coverage_suffix(history, window)
    if isinstance(history, HanaWaitHistory):
        completed = [wait for wait in history.waits if window.contains(wait.started)
                     and isinstance(wait.kind, WaitKind) and isinstance(wait.end, CompletedWait)]
        covered = any(isinstance(phase.state, CoveredWaitHistory) for phase in history.phases)
        if completed or covered and not suffix:
            minutes = sum((wait.end.at - wait.started).total_seconds() / 60
                          for wait in completed if isinstance(wait.end, CompletedWait))
            total = sum(phase_minutes)
            value = f"{minutes / total * 100:.1f}%" if total > 0 else "not recorded (phase time unavailable)"
    return log_records.MeasureRow("Hana wait / share of phase time with a recorded kind", value + suffix,
                                  len(phase_minutes), window.label,
                                  "progress_history.py waits / Hana phase time; completed known-kind wait minutes / phase minutes")


def phase_flow(
    events: Sequence[PhaseCompletion], window: log_records.Window,
    wait_history: HanaWaitHistory | HistoryNotRecorded = WAIT_HISTORY_NOT_RECORDED,
) -> list[log_records.MeasureRow]:
    unique = {event.identity: event for event in events if event.hana and window.contains(event.at)}
    times = [event.seconds / 60 for event in unique.values() if not isinstance(event.seconds, DurationNotRecorded)]
    source = "progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only"
    rows = [
        log_records.MeasureRow("Hana phases done", str(len(unique)), len(unique), window.label, source),
        distribution("Hana phase time", times, window, source),
    ]
    rows.extend(hana_wait_rows(wait_history, window))
    rows.append(wait_share_row(wait_history, window, times))
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
                   "Hanadocs date_active; stage/date_modified do not record a transition"),
        log_records.MeasureRow("goal 1 issues closed", str(len(closed)), len(closed), window.label,
                   "Hanadocs date_closed; date-only precision; reason declined excluded"),
        distribution("goal 1 active to closed", durations, window, "Hanadocs date_active + date_closed; date-only precision"),
    ]
    source = "Hanadocs project + date_closed; date-only precision; reason declined excluded"
    for project in sorted({issue.value("project") for issue in (*closed, *undated)}):
        count = sum(issue.value("project") == project for issue in closed)
        missing = sum(issue.value("project") == project for issue in undated)
        scope = "Hana" if project in HANA_PROJECTS else "other"
        value = str(count) if not missing else f"not recorded ({count} dated in window; {missing} closure dates not recorded)"
        rows.append(log_records.MeasureRow(f"goal 1 issues closed / {project or 'project not recorded'} / {scope}",
                               value, count, window.label, source))
    hana = [issue for issue in closed if issue.value("project") in HANA_PROJECTS]
    rows.append(log_records.MeasureRow("goal 1 Hana-project issues closed", str(len(hana)), len(hana), window.label, source))
    rows.append(log_records.MeasureRow("goal 1 declined closures excluded", str(len(declined)), len(declined), window.label,
                           "Hanadocs reason declined + date_closed; date-only precision"))
    receipts = sum(closure_names_merge(issue) for issue in hana)
    receipt_source = "Hanadocs reason naming a merge commit or merge_commit; text only, not checked against Git"
    rows.append(log_records.MeasureRow("goal 1 Hana closures naming a merge commit", str(receipts), len(hana), window.label, receipt_source))
    for issue in sorted(hana, key=lambda issue: issue.path.stem):
        value = "yes" if closure_names_merge(issue) else "not recorded"
        rows.append(log_records.MeasureRow(f"Hana closure merge commit / {issue.path.stem}", value, 1, window.label, receipt_source))
    return rows


def read_hana_merges(repo: Path) -> list[HanaMerge]:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--first-parent", "--merges", "--format=%H%x09%cI", "origin/init/catalyst"],
        check=True, capture_output=True, text=True,
    )
    merges: list[HanaMerge] = []
    for line in result.stdout.splitlines():
        commit, at = line.split("\t", 1)
        merges.append(HanaMerge(commit, log_records.required_time(at)))
    return merges


def hana_repository(units: Sequence[Unit]) -> Path:
    paths = [Path(unit.worktree) for unit in units if unit.hana and unit.worktree]
    paths.extend(Path.home() / "rust" / name for name in ("hana_catalyst", "hana"))
    for path in paths:
        common = repository(str(path))
        if common != "cwd has no repository" and is_hana(common):
            return Path(common)
    raise FileNotFoundError("Hana repository not found through local Git common directories")


def merge_flow(merges: Sequence[HanaMerge] | MergeHistoryNotRecorded, window: log_records.Window) -> list[log_records.MeasureRow]:
    source = "git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch"
    if isinstance(merges, MergeHistoryNotRecorded):
        return [log_records.MeasureRow("Hana merges / origin/init/catalyst", "not recorded", 0, window.label, source)]
    selected = {merge.commit: merge for merge in merges if window.contains(merge.at)}
    rows = [log_records.MeasureRow("Hana merges / origin/init/catalyst", str(len(selected)), len(selected), window.label, source)]
    days = Counter(merge.at.astimezone(user_zone()).date() for merge in selected.values())
    day = window.since.astimezone(user_zone()).date()
    last = (window.until - timedelta(microseconds=1)).astimezone(user_zone()).date()
    while day <= last:
        count = days[day]
        rows.append(log_records.MeasureRow(f"Hana merges / {day.isoformat()}", str(count), count, window.label, source))
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
            count, window.label, "pushover + review log; natedev only",
        ))
        for kind in SenderKind:
            count = message_days[(day_text, kind)]
            rows.append(log_records.MeasureRow(
                f"messages / {day_text} / {kind.value}", str(count), count, window.label,
                "message log; kind/route, else sender-name heuristic; natedev only",
            ))
        day += timedelta(days=1)
    source = "pushover + review log; natedev only"
    total_pushes = len(push_report.delivered_pushes)
    rows.extend([
        log_records.MeasureRow("phone pushes sent", str(total_pushes), total_pushes, window.label, source),
        log_records.MeasureRow("phone pushes after failed review", str(len(push_report.entries)), total_pushes, window.label, source),
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
        ))
    answer_source = "showrunner/outstanding: no answer history or answered_at; producer must record answers; natedev only"
    rows.append(log_records.MeasureRow("questions answered", "not recorded", 0, window.label, answer_source))
    rows.append(distribution("question answer time", [], window, answer_source))
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
    if scores and all(isinstance(score, float) and score >= 0 for score in scores):
        total = sum(score for score in scores if isinstance(score, float))
        covered_score = sum(score for name in covered_names if isinstance(score := by_name[name].score, float))
        if total > 0:
            weighted = f"{covered_score:g}/{total:g} ({covered_score / total:.1%})"
    return [
        log_records.MeasureRow("top 20 goal 1 open issues with a unit", f"{covered}/{len(top)}", len(top), snapshot,
                   "backlog-rankings + open issue links; unit has a running phase or live session"),
        log_records.MeasureRow("live units naming an open issue", f"{linked}/{len(units)}", len(units), snapshot,
                   "open production Issue link or open issue unit + showrunner; running phase or live session"),
        log_records.MeasureRow("top 20 goal 1 open issues covered score", weighted, len(top), snapshot,
                   "backlog-rankings score; covered score / all top 20 scores; running phase or live session"),
    ]


def unit_names_issue(unit: Unit, issue: issues.Issue) -> bool:
    if issue.value("unit") == unit.name and issue.value("showrunner") in (unit.production, unit.showrunner, ""):
        return True
    cell = unit.issue.strip().strip("`")
    wiki = re.fullmatch(r"\[\[([^]|]+)(?:\|[^]]+)?\]\]", cell)
    link = re.fullmatch(r"\[[^]]+\]\(([^)]+)\)", cell)
    target = wiki[1] if wiki else link[1] if link else cell
    return Path(target).name.removesuffix(".md") == issue.path.stem


def read_source[T](source: str, loader: Callable[[], T], fallback: T, problems: list[log_records.SourceProblem]) -> T:
    try:
        return loader()
    except (OSError, ValueError, UnicodeError, sqlite3.Error, RuntimeError, production_units.Refusal, subprocess.SubprocessError,
            change_review.ChangeReviewError, dailies_render.InputError) as error:
        reason = "missing" if isinstance(error, FileNotFoundError) else "unreadable"
        problems.append(log_records.SourceProblem(source, f"{reason}: {error}"))
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
        problems.append(log_records.SourceProblem("showrunners", warnings.getvalue().strip().replace("\n", "; ")))
    marks = read_source("unit sessions", unit_lookup.every_marked_unit, unit_lookup.MarkedProductions({}, {}), problems)
    running = read_source("unit running phases", lambda: running_phase_worktrees(unit_lookup.runs_dir(), problems), set[str](), problems)
    for runner in runners:
        doc = Path(runner["doc"])
        lines = read_source(str(doc), lambda: doc.read_text(encoding="utf-8").splitlines(), [], problems)
        repo = read_source(str(doc), lambda: production_units.production_field(lines, "Repository"), "repository not recorded", problems)
        rows = read_source(str(doc), lambda: production_units.live_unit_table(lines, runner["slug"]), [], problems)
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
                problems.append(log_records.SourceProblem(f"unit activity / {runner['slug']} / {name}", "running phase or live session not recorded"))
            result.append(Unit(runner["slug"], name, repo, tree, is_hana(tree), issue, runner["session"], activity))
    return result


def running_phase_worktrees(folder: Path, problems: list[log_records.SourceProblem]) -> set[str]:
    newest: dict[str, tuple[datetime, bool]] = {}
    for path in folder.iterdir():
        if path.suffix != ".jsonl":
            continue
        records = read_source(str(path), lambda: json_lines(path, log_records.object_record), [], problems)
        if not records or log_records.field_text(records[0], "event_type") != "run_started":
            continue
        first = records[0]
        tree = log_records.field_text(first, "working_dir")
        started = log_records.moment(first.get("run_started_at", first.get("timestamp")))
        if not isinstance(started, datetime):
            problems.append(log_records.SourceProblem(str(path), "unreadable: run start time not recorded"))
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
                problems.append(log_records.SourceProblem(str(path), "unreadable: phase identity not recorded"))
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


def read_builds(path: Path) -> list[BuildStep]:
    with closing(usage_report.open_read_store(path)) as db:
        query = "SELECT repo,step,started_at,duration_s FROM steps WHERE host='natedev'"
        rows = cast(Iterable[sqlite3.Row], db.execute(query))
        result: list[BuildStep] = []
        for row in rows:
            fields: log_records.JsonObject = dict(zip(("repo", "step", "at", "seconds"), cast(tuple[object, ...], tuple(row))))
            # Older build records have SQL NULL when the repository was unknown.
            repo = "repository not recorded" if fields["repo"] is None else log_records.field_text(fields, "repo")
            result.append(BuildStep(repo, log_records.field_text(fields, "step"), log_records.record_time(fields), log_records.field_number(fields, "seconds")))
        return result


def read_tokens(path: Path, window: log_records.Window) -> list[WindowTokenUsage]:
    with closing(usage_report.open_read_store(path)) as db:
        # Filter before grouping. One row per cwd keeps the window's sum and n
        # without loading each turn into Python. This total belongs to this window.
        query = """SELECT f.folder,
            sum(t.input+t.cache_write_5m+t.cache_write_1h+t.cache_read+t.output), count(*)
        FROM turns t LEFT JOIN folders f USING(machine,family,parent_id,agent_id)
        WHERE t.machine='natedev' AND julianday(t.at)>=julianday(?) AND julianday(t.at)<julianday(?)
        GROUP BY f.folder"""
        parameters = (window.since.isoformat(), window.until.isoformat())
        rows = cast(Iterable[sqlite3.Row], db.execute(query, parameters))
        result: list[WindowTokenUsage] = []
        for row in rows:
            fields: log_records.JsonObject = dict(zip(("cwd", "tokens", "turns"), cast(tuple[object, ...], tuple(row))))
            cwd = fields["cwd"]
            repo = "cwd not recorded"
            if cwd is not None:
                if not isinstance(cwd, str):
                    raise ValueError("cwd: expected text or SQL NULL")
                if cwd:
                    repo = repository(cwd)
            result.append(WindowTokenUsage(window, repo, log_records.field_number(fields, "tokens"), int(log_records.field_number(fields, "turns"))))
        return result


def hana_origin(origin: PhaseOrigin, prefixes: Sequence[str]) -> bool:
    """Use durable provenance before consulting a worktree that may be deleted."""
    if origin.production in ("hana", "tool-based-ui"):
        return True
    if origin.repository and Path(origin.repository).expanduser().name in ("hana", "hana_catalyst"):
        return True
    patterns = ("hana_catalyst", "tool-based-ui-", *prefixes)
    for value in (origin.working_dir, origin.plan):
        if not value:
            continue
        if any(part.startswith(patterns) for part in Path(value).parts):
            return True
    return bool(origin.working_dir) and is_hana(origin.working_dir)


def parse_phase(record: log_records.JsonObject, prefixes: Sequence[str]) -> tuple[PhaseCompletion, ...]:
    if log_records.field_text(record, "event_type") != "phase_finished" or log_records.field_text(record, "status") != "completed":
        return ()
    plan = log_records.field_text(record, "plan_doc") or log_records.field_text(record, "plan_path") or log_records.field_text(record, "project_plan_doc")
    origin = PhaseOrigin(log_records.field_text(record, "working_dir"), plan,
                         log_records.field_text(record, "production"), log_records.field_text(record, "repository"))
    at = log_records.record_time(record)
    identity = log_records.field_text(record, "event_id")
    if not identity:
        identity = f"{log_records.field_text(record, 'run_id')}:{log_records.field_text(record, 'phase_instance_id')}:{at}"
    seconds = log_records.field_number(record, "phase_elapsed_seconds") if "phase_elapsed_seconds" in record else DurationNotRecorded.MISSING
    return (PhaseCompletion(identity, at, hana_origin(origin, prefixes), seconds),)


def read_phases(folder: Path, problems: list[log_records.SourceProblem], prefixes: Sequence[str] = ()) -> list[PhaseCompletion]:
    events: list[PhaseCompletion] = []
    for path in sorted(folder.glob("*.jsonl")):
        def read_file() -> list[PhaseCompletion]:
            # The shared run_events loader returns raw maps and hides bad lines.
            # Validate and convert each line once, so the audit adds no second parse.
            completed: list[PhaseCompletion] = []
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
                        completed.extend(parse_phase(record, prefixes))
                    except ValueError:
                        bad_fields += 1
            if bad_json:
                problems.append(log_records.SourceProblem(str(path), f"unreadable: {bad_json} invalid JSON rows; counts can be incomplete"))
            if bad_fields:
                problems.append(log_records.SourceProblem(str(path), f"unreadable: {bad_fields} invalid phase field rows; counts can be incomplete"))
            return completed

        events.extend(read_source(str(path), read_file, [], problems))
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    return events


def read_waiting(
    state: Path, vault: Path, found: Sequence[issues.Issue], problems: list[log_records.SourceProblem],
) -> list[WaitingItem]:
    waiting: list[WaitingItem] = []
    changes = vault / "change-review"
    paths = read_source(str(changes), lambda: list(changes.iterdir()), [], problems)
    for path in paths:
        if path.suffix == ".md":
            rows = read_source(str(path), lambda: change_review.pending(path), [], problems)
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

    waiting.extend(read_source(str(backlog_path), read_backlog, [], problems))
    ledger = state / "nightly-review/ledger.md"
    lines = read_source(str(ledger), lambda: ledger.read_text(encoding="utf-8").splitlines(), [], problems)
    for candidate in candidates.ranked(lines):
        if candidate.state == "proposed":
            waiting.append(WaitingItem("efficiency items", log_records.moment(candidate.night[:10])))
    folder = state / "showrunner/outstanding"
    paths = read_source(str(folder), lambda: [path for path in folder.iterdir() if path.suffix == ".json"], [], problems)
    for path in sorted(paths):
        outstanding = read_source(str(path), lambda: dailies_render.read_outstanding(path), [], problems)
        for item in outstanding:
            if isinstance(item, dailies_render.OutstandingReminder):
                continue
            since = item.since
            opened = since.replace(tzinfo=user_zone()) if since.tzinfo is None else since
            waiting.append(WaitingItem("questions", opened))
    return waiting


def load_sources(window: log_records.Window) -> BaselineSources:
    state, vault = Path.home() / ".local/state", Path.home() / "rust/hanadocs"
    problems: list[log_records.SourceProblem] = []
    units = read_source("showrunners / units_table", lambda: read_units(problems), [], problems)
    # The roster and usage store do not depend on the other sources. Read them
    # while the main thread reads files. Each reader keeps its own diagnostics.
    worker_problems: list[log_records.SourceProblem] = []
    token_problems: list[log_records.SourceProblem] = []
    token_path = collect.store_path()
    with ThreadPoolExecutor(max_workers=2) as readers:
        worker_read = readers.submit(read_source, "session roster / codex_mesh", read_workers, ([], []), worker_problems)
        token_read = readers.submit(read_source, str(token_path), lambda: read_tokens(token_path, window), [], token_problems)
        builds = read_source("buildlog/index.sqlite", lambda: read_builds(state / "buildlog/index.sqlite"), [], problems)
        hana_prefixes = [Path(unit.worktree).name for unit in units if unit.hana and unit.worktree]
        phases = read_source("progress_history runs",
                             lambda: read_phases(unit_runs.history_root_in(state) / "runs", problems, hana_prefixes), [], problems)
        push_report = push_audit.audit(push_audit.load_sources(state / "notify"), window)
        problems.extend(push_report.problems)
        messages = read_source("message/log.jsonl", lambda: json_lines(state / "message/log.jsonl", parse_message), [], problems)
        found = read_source("Hanadocs issues", lambda: read_issues(vault / "issues"), [], problems)
        archive = vault / "archive"
        for folder in read_source(str(archive), lambda: [p for p in archive.rglob("*") if p.is_dir()], [], problems):
            archived = read_source(str(folder), lambda: [issues.Issue(p, issues.frontmatter(p)) for p in folder.glob("*.md")], [], problems)
            found.extend(issue for issue in archived if issue.value("status") == "closed")
        rankings = read_source("Hanadocs backlog-rankings", lambda: json_lines(vault / "backlog-rankings.jsonl", parse_rank), [], problems)
        waiting = read_waiting(state, vault, found, problems)
        workers, roster_problems = worker_read.result()
        tokens = token_read.result()
    problems.extend(worker_problems)
    problems.extend(log_records.SourceProblem("session roster", problem) for problem in roster_problems)
    problems.extend(token_problems)
    def load_merges() -> list[HanaMerge] | MergeHistoryNotRecorded:
        return read_hana_merges(hana_repository(units))

    merges = read_source("Hana origin/init/catalyst merge log", load_merges, MergeHistoryNotRecorded.MISSING, problems)
    history_root = unit_runs.history_root()
    wait_history = read_source("progress_history waits", lambda: read_wait_history(history_root, window, problems, hana_prefixes),
                               WAIT_HISTORY_NOT_RECORDED, problems)
    receipts = read_source("progress_history merge receipts", lambda: read_receipts(history_root / "merge_receipts.jsonl"),
                           HistoryNotRecorded("merge receipt history not recorded"), problems)
    return BaselineSources(units, workers, builds, tokens, phases, push_report, messages, found, rankings, waiting, problems,
                           merges, wait_history, receipts)


def baseline_summary(
    window: log_records.Window, flow_rows: Sequence[log_records.MeasureRow], attention_rows: Sequence[log_records.MeasureRow],
    problem_rows: Sequence[log_records.MeasureRow],
) -> str:
    values = {row.measure: row.value for row in (*flow_rows, *attention_rows)}
    start, end = window.since.astimezone(user_zone()), window.until.astimezone(user_zone())
    start_format = "%Y-%m-%d %H:%M" if start.tzname() == end.tzname() else "%Y-%m-%d %H:%M %Z"
    return "\n".join([
        f"Window: {start.strftime(start_format)} to {end:%Y-%m-%d %H:%M %Z} (end excluded).",
        f"Hana: {values['Hana merges / origin/init/catalyst']} merges to origin/init/catalyst; "
        + f"{values['goal 1 Hana-project issues closed']} Hana-project issues closed.",
        f"Goal 1: {values['goal 1 issues closed']} issues closed; "
        + f"{values['goal 1 declined closures excluded']} declined closures left out.",
        f"User attention: {values['phone pushes sent']} pushes sent to user; "
        + f"{values['phone pushes after failed review']} after a failed text check.",
        f"Source problems: {len(problem_rows)}. Use --full to print the tables.",
    ])


def render(sources: BaselineSources, window: log_records.Window, now: datetime, full: bool = False) -> str:
    flow_rows = phase_flow(sources.phases, window, sources.wait_history)
    flow_rows.extend(issue_flow(sources.issue_records, window))
    flow_rows.extend(merge_flow(sources.merges, window))
    flow_rows.extend(receipt_rows(sources.receipts, sources.merges, window))
    attention_rows = attention(sources.push_report, sources.messages)
    problem_rows = log_records.source_problem_rows(sources.problems, window)
    summary = baseline_summary(window, flow_rows, attention_rows, problem_rows)
    if not full:
        return summary
    snapshot = f"snapshot {now.astimezone(user_zone()):%Y-%m-%d %H:%M %Z}"
    capacity_rows = capacity(sources.units, sources.workers, snapshot)
    capacity_rows.extend(resource_use(sources.builds, sources.tokens, window))
    attention_rows.extend(pending_attention(sources.waiting, now, window))
    coverage_rows = rank_coverage(sources.issue_records, sources.rankings, sources.units, snapshot)
    groups = [
        summary,
        log_records.table("Capacity on Hana", capacity_rows),
        log_records.table("Flow to Hana main (recorded phase/issue completion; local-ref merge commits)", flow_rows),
        log_records.table("User attention", attention_rows),
        log_records.table("Rank coverage", coverage_rows),
    ]
    if problem_rows:
        groups.append(log_records.table("Source problems (dependent counts can be incomplete)", problem_rows))
    return "\n\n".join(groups)


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("baseline",))
    _ = parser.add_argument("--since", metavar="DATE_OR_TIME", default="")
    _ = parser.add_argument("--until", metavar="DATE_OR_TIME", default="")
    _ = parser.add_argument("--full", action="store_true", help="print the tables after the summary")
    args = parser.parse_args(arguments)
    now = datetime.now(timezone.utc)
    since_text = cast(str, args.since)
    until_text = cast(str, args.until)
    try:
        window = log_records.window_bounds(since_text, until_text, now)
    except ValueError as error:
        parser.error(str(error))
    print(render(load_sources(window), window, now, full=cast(bool, args.full)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
