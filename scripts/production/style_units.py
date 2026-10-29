#!/usr/bin/env python3
"""Read style ledgers and run records for the showrunner's ticket table."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "style"))

import live_units
import rules
import style_ledger
import unit_table_cells
from issues import Issue, frontmatter
from proposal_issue import ApprovedDecision, decision_answer
from scripts.direct import plan_history
from scripts.lib.user_zone import user_zone

DECISION_FOLDER = Path()


class Ticket(NamedTuple):
    id: str
    unit: str
    kind: str
    rule: str
    crate: str
    files: str
    sites: str
    rules: str
    finding_ids: str
    state: str


class Refill(NamedTuple):
    at: str
    outcome: str


class FamilyCounts(NamedTuple):
    built: int = 0
    left: int = 0
    recent: int = 0


class FindingGroup(NamedTuple):
    proposal: str
    state: str
    build_ticket: str
    built_at: str
    findings: int


class DecisionFamily(NamedTuple):
    family: rules.Family | rules.NotStyleUnit
    approved: bool


class DecisionFileMissing(NamedTuple):
    """The decision file cannot be read; built findings can use their ticket."""


class TimeUnavailable(NamedTuple):
    """A stored value has no valid time with an offset."""


@dataclass
class LedgerSnapshot:
    tickets: dict[str, Ticket] = field(default_factory=dict)
    refills: dict[str, Refill] = field(default_factory=dict)
    counts: dict[str, FamilyCounts] = field(default_factory=dict)
    splits: dict[str, style_ledger.RecordedSplit] = field(default_factory=dict)


def parsed_time(value: str) -> datetime | TimeUnavailable:
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return TimeUnavailable()
    return result if result.tzinfo is not None else TimeUnavailable()


def clock(value: str, zone: tzinfo) -> str:
    at = parsed_time(value)
    return at.astimezone(zone).strftime("%H:%M") if isinstance(at, datetime) else unit_table_cells.NONE


def decision_family(path: Path) -> DecisionFamily | DecisionFileMissing:
    if not path.is_file():
        return DecisionFileMissing()
    try:
        issue = Issue(path, frontmatter(path))
    except (OSError, UnicodeError):
        return DecisionFileMissing()
    return DecisionFamily(rules.style_family(issue.value("unit")), isinstance(decision_answer(issue), ApprovedDecision))


def columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = cast(list[tuple[int, str, str, int, object, int]],
                connection.execute(f"PRAGMA table_info({table_name})").fetchall())
    return {row[1] for row in rows}


def read_snapshot(database: Path, now: datetime, decision_folder: Path,
                  decisions: dict[Path, DecisionFamily | DecisionFileMissing]) -> LedgerSnapshot:
    """Open a database once, without a lock file, migration or write."""
    snapshot = LedgerSnapshot()
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return snapshot
    with closing(connection):
        # Map read pages to avoid one system call for each ledger page.
        _ = connection.execute("PRAGMA mmap_size=536870912")
        ticket_columns = columns(connection, "tickets")
        finding_columns = columns(connection, "held_findings")
        if ticket_columns:
            rows = cast(list[tuple[str, str, str, str, str, str, str, str, str, str]], connection.execute(
                "SELECT id,unit,kind,rule,crate,files,sites,rules,finding_ids,state FROM tickets "
                + "WHERE state IN ('assigned','checkpointed') ORDER BY created_at,id").fetchall())
            for row in rows:
                ticket = Ticket(*row)
                _ = snapshot.tickets.setdefault(ticket.unit, ticket)
        if columns(connection, "refill_results"):
            rows_refill = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT unit,at,outcome FROM refill_results").fetchall())
            snapshot.refills = {row[0]: Refill(row[1], row[2])
                                for row in rows_refill if row[2]}
            rows_split = cast(list[tuple[str, int, int, int, int, str]], connection.execute(
                "SELECT unit,ready,changed,structure,other,split_at FROM refill_results "
                + "WHERE split_at != ''").fetchall())
            snapshot.splits = {
                row[0]: style_ledger.RecordedSplit(row[1], row[2], row[3], row[4], row[5]) for row in rows_split
            }
        if not finding_columns:
            return snapshot
        built_at = "h.built_at" if "built_at" in finding_columns else "h.updated_at"
        if "built_at" not in finding_columns and ticket_columns and "build_ticket" in finding_columns:
            built_at = style_ledger.legacy_built_at_sql("h")
        build_ticket = "build_ticket" if "build_ticket" in finding_columns else "''"
        ticket_families: dict[str, str] = {}
        if ticket_columns:
            ticket_families = dict(cast(list[tuple[str, str]], connection.execute(
                "SELECT id,family FROM tickets").fetchall()))
        grouped = cast(list[tuple[str, str, str, str, int]], connection.execute(
            f"SELECT h.proposal,h.state,{build_ticket},{built_at},count(*) FROM held_findings h "
            + f"WHERE h.state IN ('built','filed') GROUP BY h.proposal,h.state,{build_ticket},{built_at}"
        ).fetchall())
        for row in grouped:
            finding = FindingGroup(*row)
            path = (decision_folder / Path(finding.proposal).expanduser()).resolve()
            if path not in decisions:
                decisions[path] = decision_family(path)
            decision = decisions[path]
            if isinstance(decision, DecisionFamily):
                family = decision.family
                approved = decision.approved
            else:
                family = ticket_families.get(finding.build_ticket, "") if finding.state == "built" else rules.NotStyleUnit()
                approved = False
            if not isinstance(family, str) or not family:
                continue
            counts = snapshot.counts.get(family, FamilyCounts())
            at = parsed_time(finding.built_at)
            recent = finding.state == "built" and isinstance(at, datetime) and now - timedelta(days=1) <= at <= now
            snapshot.counts[family] = FamilyCounts(
                counts.built + (finding.findings if finding.state == "built" else 0),
                counts.left + (finding.findings if finding.state == "filed" and approved else 0),
                counts.recent + (finding.findings if recent else 0),
            )
    return snapshot


def json_items(value: str) -> list[object]:
    try:
        decoded = cast(object, json.loads(value))
    except json.JSONDecodeError:
        return []
    return cast(list[object], decoded) if isinstance(decoded, list) else []


def ticket_cell(ticket: Ticket) -> str:
    rule = Path(ticket.rule).stem
    if not rule:
        stems = [Path(value).stem for value in json_items(ticket.rules) if isinstance(value, str)]
        rule = stems[0] + (f"+{len(stems) - 1}" if len(stems) > 1 else "") if stems else unit_table_cells.NONE
    if ticket.kind == "proposal":
        count, noun = len(json_items(ticket.finding_ids)), "finding"
    elif ticket.kind == "listed":
        count, noun = len(json_items(ticket.sites)), "site"
    else:
        count, noun = len(json_items(ticket.files)), "file"
    number = ticket.id.removeprefix(ticket.unit + "-")
    return f"#{number} · {rule} · {ticket.crate} · {count:,} {noun}{'' if count == 1 else 's'}"


def record_fields(record: object) -> dict[str, object]:
    return cast(dict[str, object], record) if isinstance(record, dict) else {}


def newest_step(record: object) -> unit_table_cells.OpenStep | unit_table_cells.NoOpenStep:
    fields = record_fields(record)
    plan = fields.get("plan")
    runs = plan_history.plan_runs(Path(plan)) if isinstance(plan, str) else []
    return unit_table_cells.open_step(unit_table_cells.run_events(runs[-1])) if runs else unit_table_cells.NoOpenStep()


def format_step(record: object, checkpointed: bool, zone: tzinfo,
                step: unit_table_cells.OpenStep | unit_table_cells.NoOpenStep) -> str:
    label = step.label if isinstance(step, unit_table_cells.OpenStep) and step.label else (
        "checkpoint sent" if checkpointed else unit_table_cells.NONE)
    eta = record_fields(record_fields(record_fields(record).get("current")).get("eta")).get("time")
    if isinstance(eta, str):
        eta_text = f"ETA {clock(eta, zone)}"
        label = eta_text if label == unit_table_cells.NONE else f"{label} · {eta_text}"
    return label


def step_cell(record: object, checkpointed: bool, zone: tzinfo) -> str:
    return format_step(record, checkpointed, zone, newest_step(record))


def unit_steps(units: list[live_units.LiveUnit], records: dict[str, object]
               ) -> dict[str, unit_table_cells.OpenStep | unit_table_cells.NoOpenStep]:
    return {unit.unit: newest_step(records.get(unit.unit)) for unit in units}


def left_cell(total: int, split: style_ledger.RecordedSplit | style_ledger.NoSplitRecorded,
              now: datetime) -> str:
    """Show the last measured split and its whole-minute age."""
    text = f"{total:,}"
    if isinstance(split, style_ledger.NoSplitRecorded):
        return text
    at = parsed_time(split.measured_at)
    if isinstance(at, TimeUnavailable):
        return text
    minutes = max(0, int((now - at).total_seconds() // 60))
    age = f"{minutes} min" if minutes < 60 else (
        f"{minutes // 60} h" if minutes < 1440 else f"{minutes // 1440} d")
    return (f"{text}: ready {split.ready:,} · changed {split.changed:,} · "
            + f"structure {split.structure:,} · other {split.other:,} ({age})")


def table(units: list[live_units.LiveUnit], records: dict[str, object], *,
          ledger_paths: Mapping[str, Path],
          now: datetime | None = None, zone: tzinfo | None = None,
          decision_folder: Path = DECISION_FOLDER) -> str:
    """Render a snapshot; callers can supply fixed time and paths for tests."""
    now = now if now is not None else datetime.now(UTC)
    zone = zone if zone is not None else user_zone()
    paths = ledger_paths
    decisions: dict[Path, DecisionFamily | DecisionFileMissing] = {}
    snapshots: dict[Path, LedgerSnapshot] = {}
    with ThreadPoolExecutor(max_workers=1) as reads:
        steps = reads.submit(unit_steps, units, records)
        for path in paths.values():
            path = path.resolve()
            if path not in snapshots:
                snapshots[path] = read_snapshot(path, now, decision_folder, decisions)
        open_steps = steps.result()
    lines = ["| Unit | Ticket | Step | Built | Left |", "| --- | --- | --- | --- | --- |"]
    for unit in units:
        snapshot = snapshots[paths[unit.unit].resolve()] if unit.unit in paths else LedgerSnapshot()
        family = rules.style_family(unit.unit)
        counts = snapshot.counts.get(family, FamilyCounts()) if isinstance(family, str) else FamilyCounts()
        ticket = snapshot.tickets.get(unit.unit)
        refill = snapshot.refills.get(unit.unit)
        idle = f"idle — {refill.outcome} ({clock(refill.at, zone)})" if refill else "idle"
        left = left_cell(counts.left, snapshot.splits.get(unit.unit, style_ledger.NoSplitRecorded()), now)
        cells = (unit_table_cells.session_cell(unit), ticket_cell(ticket) if ticket else idle,
                 format_step(records.get(unit.unit), bool(ticket and ticket.state == "checkpointed"), zone,
                             open_steps[unit.unit]),
                 f"{counts.built:,} ({counts.recent:,} in 24 h)", left)
        lines.append("| " + " | ".join(unit_table_cells.escaped(cell) for cell in cells) + " |")
    all_counts = [counts for snapshot in snapshots.values() for counts in snapshot.counts.values()]
    totals = FamilyCounts(sum(counts.built for counts in all_counts), sum(counts.left for counts in all_counts),
                          sum(counts.recent for counts in all_counts))
    total = f"All families: {totals.built:,} built · {totals.left:,} left · {totals.recent:,} built in the last 24 h · "
    if totals.recent:
        finish = (now + timedelta(days=totals.left / totals.recent)).astimezone(zone).strftime("%Y-%m-%d")
        total += f"ETA at that rate {finish}"
    else:
        total += "no ETA: nothing built in 24 h"
    return "\n".join([*lines, "", total])
