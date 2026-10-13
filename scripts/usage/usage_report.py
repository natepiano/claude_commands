#!/usr/bin/env python3
"""Report named usage metrics from the ledger without reading logs or changing it."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import NoReturn, cast, override
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.usage import collect, outcomes, weights
from scripts.lib.user_zone import user_zone_name
from scripts.usage.rollouts import parse_timestamp


@dataclass(frozen=True)
class Dimension:
    name: str
    expression: str


DIMENSIONS = {
    dimension.name: dimension for dimension in (
        Dimension("day", "t.day"),
        Dimension("role", "coalesce(t.role, 'unassigned')"),
        Dimension("lens", "coalesce(t.lens, t.lens_state)"),
        Dimension("model", "t.model"),
        Dimension("effort", "coalesce(t.effort, 'unreported')"),
        Dimension("machine", "t.machine"),
        Dimension("unit", "coalesce(u.unit, 'unknown')"),
    )
}
ALL_DIMENSIONS = tuple(DIMENSIONS.values())
RUN_DIMENSIONS = tuple(DIMENSIONS[name] for name in ("day", "machine", "unit"))


@dataclass(frozen=True)
class QueryOutcome:
    name: str
    query: str
    dimensions: tuple[Dimension, ...]
    aggregate: str = "count(*)"


@dataclass(frozen=True)
class TurnGroupOutcome:
    name: str
    dimensions: tuple[Dimension, ...]
    aggregate: str


Outcome = QueryOutcome | TurnGroupOutcome


OUTCOMES = {
    outcome.name: outcome for outcome in (
        QueryOutcome("checkpoints", """
            SELECT c.at, coalesce(c.machine, 'natedev') AS machine,
                   CASE WHEN c.mapping = 'mapped' THEN r.worktree END AS folder
            FROM checkpoints c LEFT JOIN runs r ON r.machine = c.machine AND r.run_id = c.run_id
        """, RUN_DIMENSIONS),
        QueryOutcome("findings_closed", """
            SELECT f.state_at AS at, f.machine, r.worktree AS folder FROM findings f
            LEFT JOIN runs r ON r.machine = f.machine AND r.run_id = f.run_id
            WHERE f.state = 'accepted'
        """, RUN_DIMENSIONS),
        QueryOutcome("review_rounds", """
            SELECT p.started_at AS at, p.machine, r.worktree AS folder FROM passes p
            LEFT JOIN runs r ON r.machine = p.machine AND r.run_id = p.run_id
            WHERE p.kind = 'review'
        """, RUN_DIMENSIONS),
        QueryOutcome("compactions", """
            SELECT c.at, c.machine, f.folder, coalesce(t.role, 'unassigned') AS role,
                   t.lens, coalesce(t.lens_state, 'not_applicable') AS lens_state,
                   coalesce(t.model, 'unreported') AS model, t.effort
            FROM compactions c LEFT JOIN turns t ON t.rowid = coalesce((
                SELECT rowid FROM turns p WHERE (p.machine, p.family, p.parent_id, p.agent_id)
                    = (c.machine, c.family, c.parent_id, c.agent_id) AND p.at <= c.at
                ORDER BY p.at DESC, p.rowid DESC LIMIT 1), (
                SELECT rowid FROM turns p WHERE (p.machine, p.family, p.parent_id, p.agent_id)
                    = (c.machine, c.family, c.parent_id, c.agent_id)
                ORDER BY p.at, p.rowid LIMIT 1))
            LEFT JOIN folders f ON (f.machine, f.family, f.parent_id, f.agent_id)
                = (c.machine, c.family, c.parent_id, c.agent_id)
        """, ALL_DIMENSIONS),
        TurnGroupOutcome("threads", ALL_DIMENSIONS,
                         "count(DISTINCT json_array(machine, family, parent_id, agent_id))"),
        QueryOutcome("account_switches", """
            SELECT at, machine FROM (
                SELECT *, lag(label) OVER (PARTITION BY machine, role ORDER BY at, label) AS previous_label
                FROM account_role_changes
            ) WHERE previous_label IS NOT NULL AND label != previous_label
        """, tuple(DIMENSIONS[name] for name in ("day", "machine"))),
    )
}


class ReportFormat(Enum):
    NUMBER = "number"
    SHARE = "share"
    COUNT = "count"
    RATE = "rate"


@dataclass(frozen=True)
class TurnAggregate:
    name: str
    expression: str
    where: str = "1"
    weighted: bool = False
    show_turns: bool = False


class TurnValue(Enum):
    INPUT = "input"
    CACHE_WRITE_5M = "cache_write_5m"
    CACHE_WRITE_1H = "cache_write_1h"
    CACHE_READ = "cache_read"
    OUTPUT = "output"
    WEIGHTED = "weighted"
    OUTPUT_WEIGHTED = "output_weighted"
    TURNS = "turns"


@dataclass(frozen=True)
class TurnSum:
    """Sum additive values; where selects turns and condition selects summands by flags."""

    name: str
    values: tuple[TurnValue, ...]
    where: str = "1"
    condition: str = "1"
    show_turns: bool = False

    @property
    def weighted(self) -> bool:
        return any(value in (TurnValue.WEIGHTED, TurnValue.OUTPUT_WEIGHTED) for value in self.values)

    @property
    def expression(self) -> str:
        value = " + ".join(value.value for value in self.values) or "0"
        return f"sum(CASE WHEN {self.condition} THEN {value} ELSE 0 END)"


@dataclass(frozen=True)
class OutcomeCount:
    outcome: Outcome


MetricSide = TurnAggregate | TurnSum | OutcomeCount


@dataclass(frozen=True)
class NoDenominator:
    pass


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    numerator: MetricSide
    denominator: MetricSide | NoDenominator = NoDenominator()
    format: ReportFormat = ReportFormat.NUMBER


WEIGHTED = TurnSum("weighted", (TurnValue.WEIGHTED,))
METRICS = {
    metric.name: metric for metric in (
        Metric("weighted", "Weighted tokens", TurnSum("weighted", (TurnValue.WEIGHTED,), show_turns=True)),
        Metric("weighted_per_checkpoint", "Weighted tokens per checkpoint", WEIGHTED,
               OutcomeCount(OUTCOMES["checkpoints"]), ReportFormat.RATE),
        Metric("before_first_edit_share", "Weighted share before the first edit", TurnSum(
            "before_first_edit", (TurnValue.WEIGHTED,), "edited = 1", "before_first_edit = 1"),
               TurnSum("weighted", (TurnValue.WEIGHTED,), "edited = 1"),
               ReportFormat.SHARE),
        Metric("cache_read_share", "Cache reads as a share of raw input tokens",
               TurnSum("cache_read", (TurnValue.CACHE_READ,)),
               TurnSum("input", (TurnValue.INPUT, TurnValue.CACHE_WRITE_5M,
                                 TurnValue.CACHE_WRITE_1H, TurnValue.CACHE_READ)),
               ReportFormat.SHARE),
        Metric("output_share", "Output as a share of weighted tokens",
               TurnSum("output_weighted", (TurnValue.OUTPUT_WEIGHTED,)),
               WEIGHTED, ReportFormat.SHARE),
        Metric("compactions_per_thread", "Compactions per thread", OutcomeCount(OUTCOMES["compactions"]),
               OutcomeCount(OUTCOMES["threads"]), ReportFormat.RATE),
        Metric("weighted_per_finding_closed", "Weighted tokens per accepted finding", WEIGHTED,
               OutcomeCount(OUTCOMES["findings_closed"]), ReportFormat.RATE),
        Metric("weighted_per_review_round", "Weighted tokens per review pass", WEIGHTED,
               OutcomeCount(OUTCOMES["review_rounds"]), ReportFormat.RATE),
        Metric("account_switches", "Account switches", OutcomeCount(OUTCOMES["account_switches"]),
               format=ReportFormat.COUNT),
    )
}


@dataclass(frozen=True)
class TimeBound:
    at: datetime


@dataclass(frozen=True)
class Unbounded:
    pass


Bound = TimeBound | Unbounded


@dataclass(frozen=True)
class TimeWindow:
    since: Bound
    until: Bound

    def predicate(self, column: str) -> tuple[str, tuple[str, ...]]:
        clauses: list[str] = []
        parameters: list[str] = []
        for bound, operator in ((self.since, ">="), (self.until, "<")):
            if isinstance(bound, TimeBound):
                clauses.append(f"{column} {operator} ?")
                parameters.append(bound.at.astimezone(timezone.utc).isoformat())
        return " AND ".join(clauses) or "1", tuple(parameters)


@dataclass(frozen=True)
class ReportRequest:
    metric: Metric
    dimensions: tuple[Dimension, ...]
    window: TimeWindow
    zone: ZoneInfo
    exact: bool


@dataclass(frozen=True)
class UnitWorktree:
    unit: str
    worktree: Path


def unit_worktrees(production_docs: Path) -> tuple[UnitWorktree, ...]:
    found: list[UnitWorktree] = []
    for document in sorted(production_docs.glob("*-production.md")):
        try:
            lines = document.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        in_units = False
        headings: list[str] = []
        for line in lines:
            if line.strip() == "## Units":
                in_units = True
                continue
            if not in_units:
                continue
            if line.startswith("## "):
                break
            if not line.lstrip().startswith("|"):
                if headings:
                    break
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not headings:
                headings = cells
                continue
            if all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            row = dict(zip(headings, cells, strict=False))
            unit = outcomes.cell_value(row.get("Unit", ""))
            worktree = outcomes.cell_value(row.get("Worktree", ""))
            if unit and worktree:
                found.append(UnitWorktree(unit, Path(worktree).expanduser()))
    return tuple(sorted(found, key=lambda row: len(row.worktree.parts), reverse=True))


def register_report_functions(db: sqlite3.Connection, worktrees: tuple[UnitWorktree, ...]) -> None:
    @lru_cache(maxsize=8192)
    def report_unit(folder: str | None) -> str:
        # A SQL NULL is the boundary value for an agent whose folder was not reported.
        if folder is None:
            return "unknown"
        path = Path(folder)
        for row in worktrees:
            if path.is_relative_to(row.worktree):
                return row.unit
        return path.name or "unknown"

    db.create_function("report_unit", 1, report_unit, deterministic=True)
    weights.register(db)


def metric_sides(metric: Metric) -> tuple[MetricSide, ...]:
    return ((metric.numerator,) if isinstance(metric.denominator, NoDenominator)
            else (metric.numerator, metric.denominator))


def side_dimensions(side: MetricSide) -> tuple[Dimension, ...]:
    return ALL_DIMENSIONS if isinstance(side, (TurnAggregate, TurnSum)) else side.outcome.dimensions


def turn_side(side: MetricSide) -> bool:
    return isinstance(side, (TurnAggregate, TurnSum)) or isinstance(side.outcome, TurnGroupOutcome)


def turn_scope(metric: Metric) -> str:
    conditions = [side.where if isinstance(side, (TurnAggregate, TurnSum)) else "1"
                  for side in metric_sides(metric) if turn_side(side)]
    return " OR ".join(f"({condition})" for condition in dict.fromkeys(conditions)) or "1"


TOKEN_CLASSES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
AGENT_COLUMNS = ("machine", "family", "parent_id", "agent_id")


@dataclass(frozen=True)
class ZoneOffset:
    starts_at: datetime
    seconds: int


def zone_offsets(db: sqlite3.Connection, request: ReportRequest) -> tuple[ZoneOffset, ...]:
    """Read indexed endpoints, then find zone transitions without visiting turns."""
    predicate, parameters = request.window.predicate("at")
    endpoints = [f"SELECT min(at) AS at FROM turns WHERE {predicate}",
                 f"SELECT max(at) AS at FROM turns WHERE {predicate}"]
    parameters = (*parameters, *parameters)
    for side in metric_sides(request.metric):
        if not isinstance(side, OutcomeCount) or isinstance(side.outcome, TurnGroupOutcome):
            continue
        predicate, bounds = request.window.predicate("at")
        endpoints.append(f"SELECT min(at) AS at FROM ({side.outcome.query}) WHERE {predicate}")
        endpoints.append(f"SELECT max(at) AS at FROM ({side.outcome.query}) WHERE {predicate}")
        parameters = (*parameters, *bounds, *bounds)
    rows = cast(Iterable[sqlite3.Row], db.execute(" UNION ALL ".join(endpoints), parameters))
    moments = [datetime.fromisoformat(cast(str, row[0])) for row in rows
               if isinstance(row[0], str)]
    if not moments:
        return ()
    start = min(moments).replace(microsecond=0)
    end = max(moments)

    def offset(at: datetime) -> int:
        difference = at.astimezone(request.zone).utcoffset()
        assert difference is not None
        return int(difference.total_seconds())

    periods = [ZoneOffset(start, offset(start))]
    cursor = start
    while cursor < end:
        next_day = min(cursor + timedelta(days=1), end)
        next_offset = offset(next_day)
        if next_offset != periods[-1].seconds:
            lower, upper = 0, int((next_day - cursor).total_seconds())
            while upper - lower > 1:
                middle = (lower + upper) // 2
                if offset(cursor + timedelta(seconds=middle)) == periods[-1].seconds:
                    lower = middle
                else:
                    upper = middle
            change = cursor + timedelta(seconds=upper)
            periods.append(ZoneOffset(change, next_offset))
        cursor = next_day
    return tuple(periods)


def day_sql(alias: str, offsets: tuple[ZoneOffset, ...]) -> str:
    if not offsets:
        return "NULL"
    choices = [f"WHEN {alias}.at < '{following.starts_at.isoformat()}' THEN {period.seconds}"
               for period, following in zip(offsets, offsets[1:])]
    offset = ("CASE " + " ".join(choices) + f" ELSE {offsets[-1].seconds} END"
              if choices else str(offsets[0].seconds))
    return f"date(substr({alias}.at, 1, 19), printf('%+d seconds', {offset}))"


def dimension_joins(dimensions: Sequence[Dimension]) -> str:
    return " LEFT JOIN folder_units u ON u.folder IS t.folder" if DIMENSIONS["unit"] in dimensions else ""


def weighted_sql(alias: str) -> str:
    return (f"CASE WHEN w.weight_entry IS NULL THEN NULL ELSE {alias}.input "
            f"+ {alias}.cache_read * w.cached + {alias}.cache_write_5m * w.write_5m "
            f"+ {alias}.cache_write_1h * w.write_1h + {alias}.output * w.output END")


def turn_relation(db: sqlite3.Connection, request: ReportRequest) -> tuple[str, tuple[str, ...]]:
    sides = metric_sides(request.metric)
    general = any(isinstance(side, TurnAggregate) for side in sides)
    outcomes = {side.outcome.name: side.outcome for side in sides if isinstance(side, OutcomeCount)}
    dimensions = list(ALL_DIMENSIONS if general else request.dimensions if any(turn_side(side) for side in sides) else ())
    for name in ("role", "lens", "model"):
        if DIMENSIONS[name] not in dimensions:
            dimensions.append(DIMENSIONS[name])
    if any(isinstance(outcome, TurnGroupOutcome) for outcome in outcomes.values()) and DIMENSIONS["unit"] not in dimensions:
        dimensions.append(DIMENSIONS["unit"])
    need_unit = DIMENSIONS["unit"] in dimensions or any(
        DIMENSIONS["unit"] in outcome.dimensions for outcome in outcomes.values())
    need_day = DIMENSIONS["day"] in dimensions or any(
        DIMENSIONS["day"] in request.dimensions and DIMENSIONS["day"] in outcome.dimensions
        for outcome in outcomes.values())
    offsets = zone_offsets(db, request) if need_day else ()
    weight_sql = weights.sql_weights("t.model", "t.at")
    ctes = [f"weight_entries AS ({weight_sql.entries})"]
    if need_unit:
        ctes.append("folder_units AS MATERIALIZED (SELECT folder, report_unit(folder) AS unit FROM "
                    + "(SELECT folder FROM folders UNION SELECT worktree FROM runs))")
    ctes.append("agent_edits AS MATERIALIZED (SELECT machine, family, parent_id, agent_id, "
                + "min(at) AS first_edit FROM edits GROUP BY machine, family, parent_id, agent_id)")
    predicate, parameters = request.window.predicate("t.at")
    exact = " AND t.attribution = 'recorded'" if request.exact else ""
    raw_columns = [*("t." + column for column in AGENT_COLUMNS), "t.attribution", "t.model",
                   "t.role", "t.lens", "t.lens_state", "t.before_first_edit"]
    if DIMENSIONS["effort"] in dimensions:
        raw_columns.append("t.effort")
    if DIMENSIONS["day"] in dimensions:
        raw_columns.append(day_sql("t", offsets) + " AS day")
    raw_columns.append(weight_sql.active_entry + " AS weight_entry")
    group_names = [column.split(" AS ")[-1] if " AS " in column else column[2:] for column in raw_columns]
    # This is the only full turn scan for additive reports. Lookup joins see its groups.
    grouped = [*raw_columns, "count(*) AS turns", *(f"total(t.{column}) AS {column}" for column in TOKEN_CLASSES)]
    ctes.append(f"turn_grain AS MATERIALIZED (SELECT {', '.join(grouped)} FROM turns t "
                + f"WHERE {predicate}{exact} GROUP BY {', '.join(group_names)})")
    lookup_joins = (" LEFT JOIN agent_edits e ON (e.machine, e.family, e.parent_id, e.agent_id) "
                    + "= (t.machine, t.family, t.parent_id, t.agent_id)")
    if DIMENSIONS["unit"] in dimensions:
        lookup_joins += (" LEFT JOIN folders f ON (f.machine, f.family, f.parent_id, f.agent_id) "
                        + "= (t.machine, t.family, t.parent_id, t.agent_id) "
                        + "LEFT JOIN folder_units u ON u.folder IS f.folder")
    normalized = [f"{dimension.expression} AS {dimension.name}" for dimension in dimensions]
    normalized.extend("t." + column for column in AGENT_COLUMNS
                      if column != "machine" or DIMENSIONS["machine"] not in dimensions)
    normalized.extend(["t.attribution", "t.weight_entry", "t.before_first_edit",
                       "e.first_edit IS NOT NULL AS edited", *("t." + column for column in TOKEN_CLASSES)])
    if DIMENSIONS["unit"] in dimensions:
        normalized.append("f.folder")
    output_weighted = "CASE WHEN w.weight_entry IS NULL THEN NULL ELSE t.output * w.output END"
    ctes.append(f"turn_groups AS MATERIALIZED (SELECT {', '.join(normalized)}, t.turns, "
                + f"{weighted_sql('t')} AS weighted, {output_weighted} AS output_weighted "
                + f"FROM turn_grain t{lookup_joins} LEFT JOIN weight_entries w ON w.weight_entry = t.weight_entry)")
    for outcome in outcomes.values():
        if isinstance(outcome, TurnGroupOutcome):
            continue
        predicate, bounds = request.window.predicate("t.at")
        day = ", " + day_sql("t", offsets) + " AS day" if need_day else ""
        ctes.append(f"outcome_{outcome.name} AS MATERIALIZED (SELECT t.*{day} FROM ({outcome.query}) t WHERE {predicate})")
        parameters = (*parameters, *bounds)
    if general:
        # An arbitrary aggregate always receives individual turns and every relation column.
        individual = [*("t." + column for column in AGENT_COLUMNS), "t.at", "t.attribution", "t.model",
                      "t.role", "t.lens", "t.lens_state", "t.effort", "t.before_first_edit",
                      *(f"CAST(t.{column} AS REAL) AS {column}" for column in TOKEN_CLASSES),
                      weight_sql.active_entry + " AS weight_entry", day_sql("t", offsets) + " AS day"]
        predicate, bounds = request.window.predicate("t.at")
        ctes.append(f"individual_counts AS NOT MATERIALIZED (SELECT {', '.join(individual)} FROM turns t "
                    + f"WHERE {predicate}{exact})")
        parameters = (*parameters, *bounds)
        ctes.append(f"turn_relation AS MATERIALIZED (SELECT {', '.join(normalized)}, t.at, 1 AS turns, "
                    + f"{weighted_sql('t')} AS weighted, {output_weighted} AS output_weighted "
                    + f"FROM individual_counts t{lookup_joins} LEFT JOIN weight_entries w ON w.weight_entry = t.weight_entry)")
        coverage_columns = [dimension.name for dimension in dimensions] + ["attribution", "weight_entry", "edited", "before_first_edit"]
        summed = [*coverage_columns, "count(*) AS turns", *(f"total({column}) AS {column}" for column in TOKEN_CLASSES)]
        ctes.append(f"coverage_counts AS MATERIALIZED (SELECT {', '.join(summed)} FROM turn_relation "
                    + f"WHERE {turn_scope(request.metric)} GROUP BY {', '.join(coverage_columns)})")
        ctes.append(f"turn_totals AS MATERIALIZED (SELECT t.*, {weighted_sql('t')} AS weighted, "
                    + f"{output_weighted} AS output_weighted FROM coverage_counts t "
                    + "LEFT JOIN weight_entries w ON w.weight_entry = t.weight_entry)")
    else:
        ctes.append("turn_totals AS MATERIALIZED (SELECT * FROM turn_groups WHERE " + turn_scope(request.metric) + ")")
    return "WITH " + ",\n".join(ctes) + "\n", parameters


@dataclass(frozen=True)
class MeasuredNumber:
    value: float


@dataclass(frozen=True)
class UnweightedGroup:
    pass


@dataclass(frozen=True)
class AbsentGroup:
    pass


GroupValue = MeasuredNumber | UnweightedGroup | AbsentGroup


@dataclass(frozen=True)
class AggregateGroup:
    dimensions: dict[Dimension, str]
    value: GroupValue
    turns: int


@dataclass(frozen=True)
class MetricGroup:
    dimensions: tuple[str, ...]
    numerator: GroupValue
    denominator: GroupValue | NoDenominator
    turns: int


@dataclass(frozen=True)
class ReportQuery:
    name: str
    columns: tuple[str, ...]
    select: str
    parameters: tuple[str, ...] = ()


def side_query(request: ReportRequest, side: MetricSide, name: str) -> ReportQuery:
    carried = tuple(dimension for dimension in request.dimensions if dimension in side_dimensions(side))
    names = ", ".join(dimension.name for dimension in carried)
    group_by = f" GROUP BY {names}" if names else ""
    parameters: tuple[str, ...] = ()
    if isinstance(side, (TurnAggregate, TurnSum)):
        aggregate = side
        expression = (f"CASE WHEN count(weighted) = 0 THEN NULL ELSE {aggregate.expression} END"
                      if side.weighted else aggregate.expression)
        grouped = isinstance(side, TurnSum)
        turns = "coalesce(sum(turns), 0)" if grouped else "count(*)"
        relation = "turn_totals" if grouped else "turn_relation"
        query = f"SELECT {names + ', ' if names else ''}{expression} AS amount, {turns} AS turns "
        query += f"FROM {relation} WHERE {side.where}{group_by}"
    else:
        outcome = side.outcome
        fields = ["t." + dimension.name if isinstance(outcome, TurnGroupOutcome) else dimension.expression for dimension in carried]
        expressions = ", ".join(f"{field} AS {dimension.name}" for dimension, field in zip(carried, fields))
        outcome_group_by = " GROUP BY " + ", ".join(fields) if carried else ""
        relation = "turn_groups" if isinstance(outcome, TurnGroupOutcome) else "outcome_" + outcome.name
        joins = "" if isinstance(outcome, TurnGroupOutcome) else dimension_joins(carried)
        query = (f"SELECT {expressions + ', ' if expressions else ''}{outcome.aggregate} AS amount, 0 AS turns "
                 f"FROM {relation} t{joins}{outcome_group_by}")
    return ReportQuery(name, (*[dimension.name for dimension in carried], "amount", "turns"), query, parameters)


def aggregate_groups(request: ReportRequest, side: MetricSide,
                     rows: list[dict[str, object]]) -> list[AggregateGroup]:
    carried = tuple(dimension for dimension in request.dimensions if dimension in side_dimensions(side))
    groups: list[AggregateGroup] = []
    for row in rows:
        amount = row["amount"]
        value: GroupValue = (MeasuredNumber(float(amount)) if isinstance(amount, (int, float))
                             else UnweightedGroup() if isinstance(side, (TurnAggregate, TurnSum)) and side.weighted
                             else MeasuredNumber(0))
        groups.append(AggregateGroup({dimension: cast(str, row[dimension.name]) for dimension in carried},
                                     value, cast(int, row["turns"])))
    return groups


def report_data(db: sqlite3.Connection, request: ReportRequest) -> dict[str, list[dict[str, object]]]:
    queries = [side_query(request, request.metric.numerator, "numerator")]
    if not isinstance(request.metric.denominator, NoDenominator):
        queries.append(side_query(request, request.metric.denominator, "denominator"))
    queries.extend((
        ReportQuery("attribution", ("attribution", "weighted", "turns"),
                    "SELECT attribution, sum(weighted) AS weighted, sum(turns) AS turns FROM turn_totals " +
                    "GROUP BY attribution"),
        ReportQuery("unweighted", ("model", "turns"),
                    "SELECT model, sum(turns) AS turns FROM turn_totals WHERE weighted IS NULL " +
                    "GROUP BY model ORDER BY model"),
        ReportQuery("review", ("turns",), "SELECT coalesce(sum(turns), 0) AS turns FROM turn_totals WHERE " +
                    "role = 'review' AND lens = 'unrecorded'"),
    ))
    for side in metric_sides(request.metric):
        if isinstance(side, OutcomeCount) and DIMENSIONS["unit"] in side.outcome.dimensions:
            outcome = side.outcome
            relation = "turn_groups" if isinstance(outcome, TurnGroupOutcome) else "outcome_" + outcome.name
            joins = "" if isinstance(outcome, TurnGroupOutcome) else dimension_joins((DIMENSIONS["unit"],))
            unit = "t.unit" if isinstance(outcome, TurnGroupOutcome) else DIMENSIONS["unit"].expression
            queries.append(ReportQuery("unknown_" + side.outcome.name, ("outcomes",),
                f"SELECT {outcome.aggregate} AS outcomes FROM {relation} t{joins} WHERE {unit} = 'unknown'"))
    prefix, parameters = turn_relation(db, request)
    # One grouped token relation serves additive metrics and their coverage figures.
    columns = tuple(dict.fromkeys(column for query in queries for column in query.columns))
    selects: list[str] = []
    for query in queries:
        fields = ", ".join(column if column in query.columns else f"NULL AS {column}" for column in columns)
        selects.append(f"SELECT '{query.name}' AS section, {fields} FROM ({query.select})")
        parameters = (*parameters, *query.parameters)
    data: dict[str, list[dict[str, object]]] = {query.name: [] for query in queries}
    section_columns = {query.name: query.columns for query in queries}
    for row in cast(Iterable[sqlite3.Row], db.execute(prefix + " UNION ALL ".join(selects), parameters)):
        section = cast(str, row["section"])
        data[section].append({column: cast(object, row[column]) for column in section_columns[section]})
    return data


def join_groups(request: ReportRequest, numerator: list[AggregateGroup],
                denominator: list[AggregateGroup]) -> list[MetricGroup]:
    metric = request.metric
    if isinstance(metric.denominator, NoDenominator):
        return [MetricGroup(tuple(group.dimensions[dimension] for dimension in request.dimensions),
                            group.value, NoDenominator(), group.turns) for group in numerator]
    common = tuple(dimension for dimension in request.dimensions
                   if dimension in side_dimensions(metric.numerator)
                   and dimension in side_dimensions(metric.denominator))
    buckets: dict[tuple[str, ...], list[AggregateGroup]] = {}
    for group in denominator:
        buckets.setdefault(tuple(group.dimensions[dimension] for dimension in common), []).append(group)
    joined: list[MetricGroup] = []
    matched: set[tuple[str, ...]] = set()
    for group in numerator:
        key = tuple(group.dimensions[dimension] for dimension in common)
        peers = buckets.get(key, [])
        matched.add(key)
        for peer in peers:
            values = {**group.dimensions, **peer.dimensions}
            joined.append(MetricGroup(tuple(values[dimension] for dimension in request.dimensions),
                                      group.value, peer.value, group.turns or peer.turns))
        if not peers and all(dimension in group.dimensions for dimension in request.dimensions):
            joined.append(MetricGroup(tuple(group.dimensions[dimension] for dimension in request.dimensions),
                                      group.value, AbsentGroup(), group.turns))
    for key, peers in buckets.items():
        if key in matched:
            continue
        for peer in peers:
            if all(dimension in peer.dimensions for dimension in request.dimensions):
                joined.append(MetricGroup(tuple(peer.dimensions[dimension] for dimension in request.dimensions),
                                          AbsentGroup(), peer.value, peer.turns))
    return sorted(joined, key=lambda group: group.dimensions)


def markdown_table(headings: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"

    return [line(headings), line(["---"] * len(headings)), *(line(row) for row in rows)]


def number(value: GroupValue, *, count: bool = False) -> str:
    if isinstance(value, UnweightedGroup):
        return "unweighted"
    if isinstance(value, AbsentGroup):
        return "—"
    return f"{value.value:.0f}" if count else f"{value.value:,.0f}"


def metric_value(group: MetricGroup, format: ReportFormat) -> str:
    if isinstance(group.numerator, UnweightedGroup) or isinstance(group.denominator, UnweightedGroup):
        return "unweighted"
    if isinstance(group.numerator, AbsentGroup):
        if isinstance(group.denominator, NoDenominator):
            return "—"
        numerator = 0.0
    else:
        numerator = group.numerator.value
    if isinstance(group.denominator, NoDenominator):
        value = numerator
    else:
        if isinstance(group.denominator, AbsentGroup) or group.denominator.value == 0:
            return "—"
        value = numerator / group.denominator.value
    match format:
        case ReportFormat.NUMBER:
            return f"{value:,.0f}"
        case ReportFormat.SHARE:
            return f"{value:.1%}"
        case ReportFormat.COUNT:
            return f"{value:.0f}"
        case ReportFormat.RATE:
            return f"{value:.2f}"


def side_name(side: MetricSide) -> str:
    return side.name if isinstance(side, (TurnAggregate, TurnSum)) else side.outcome.name


def metric_table(request: ReportRequest, groups: list[MetricGroup]) -> list[str]:
    metric = request.metric
    headings = [dimension.name for dimension in request.dimensions] + [side_name(metric.numerator)]
    show_turns = isinstance(metric.numerator, (TurnAggregate, TurnSum)) and metric.numerator.show_turns
    if show_turns:
        headings.append("turns")
    if not isinstance(metric.denominator, NoDenominator):
        headings.append(side_name(metric.denominator))
    headings.append("value")
    rows: list[list[str]] = []
    for group in sorted(groups, key=lambda group: group.dimensions):
        row = [*group.dimensions, number(group.numerator, count=isinstance(metric.numerator, OutcomeCount))]
        if show_turns:
            row.append(str(group.turns))
        if not isinstance(group.denominator, NoDenominator):
            row.append(number(group.denominator, count=isinstance(metric.denominator, OutcomeCount)))
        row.append(metric_value(group, metric.format))
        rows.append(row)
    return markdown_table(headings, rows)


def share(value: float, total: float) -> str:
    return f"{value / total:.1%}" if total else "—"


def coverage(request: ReportRequest, data: dict[str, list[dict[str, object]]]) -> list[str]:
    rows = data["attribution"]
    total_weighted = sum(cast(float, row["weighted"] or 0) for row in rows)
    total_turns = sum(cast(int, row["turns"]) for row in rows)
    by_attribution = {cast(str, row["attribution"]): row for row in rows}
    values: list[list[str]] = []
    for attribution in ("recorded", "estimated", "unassigned"):
        row = by_attribution.get(attribution)
        amount = cast(float, row["weighted"] or 0) if row is not None else 0.0
        turns = cast(int, row["turns"]) if row is not None else 0
        values.append([attribution, number(MeasuredNumber(amount)), share(amount, total_weighted),
                       str(turns), share(turns, total_turns)])
    lines = ["Coverage", "", *markdown_table(
        ("attribution", "weighted", "weighted share", "turns", "turn share"), values), ""]
    unweighted = data["unweighted"]
    if unweighted:
        lines.extend(["Unweighted:", "", *markdown_table(("model", "turns"),
                     ([cast(str, row["model"]), str(row["turns"])] for row in unweighted)), ""])
    else:
        lines.extend(["Unweighted: none", ""])
    lines.append(f"Review turns with unrecorded lens: {data['review'][0]['turns']}.")
    for side in metric_sides(request.metric):
        if not isinstance(side, OutcomeCount) or DIMENSIONS["unit"] not in side.outcome.dimensions:
            continue
        unknown = data["unknown_" + side.outcome.name][0]["outcomes"]
        lines.append(f"Outcome rows with unknown unit ({side.outcome.name}): {unknown}.")
    return lines


def source_time(value: object, zone: ZoneInfo) -> str:
    if not isinstance(value, str):
        return "never"
    at = parse_timestamp(value)
    return at.astimezone(zone).isoformat() if at is not None else "never"


def sources(db: sqlite3.Connection, zone: ZoneInfo) -> list[str]:
    rows: list[tuple[str, str, collect.SourceState, str, str]] = []
    for row in cast(Iterable[sqlite3.Row], db.execute("SELECT * FROM turn_sources")):
        rows.append((cast(str, row["machine"]), cast(str, row["family"]),
                     collect.SourceState(row["state"]), source_time(cast(object, row["last_complete_at"]), zone), ""))
    for row in cast(Iterable[sqlite3.Row], db.execute("SELECT * FROM role_log_sources")):
        state = collect.SourceState(row["state"])
        details = f"skipped lines: {row['problems']}" if state is collect.SourceState.READY else ""
        rows.append((cast(str, row["machine"]), "role log", state, source_time(cast(object, row["read_at"]), zone), details))
    rows.sort(key=lambda row: (row[2] is collect.SourceState.READY, row[0], row[1]))
    lines = ["Sources", ""]
    headings = ("machine", "family", "state", "last read", "details")
    incomplete = [row for row in rows if row[2] is not collect.SourceState.READY]
    ready = [row for row in rows if row[2] is collect.SourceState.READY]
    for group in (incomplete, ready):
        if not group:
            continue
        if group is incomplete:
            lines.extend(["Not read in full:", ""])
        lines.extend(markdown_table(headings, ([machine, family, state.value, at, details]
                     for machine, family, state, at, details in group)))
        lines.append("")
    return lines


def render_report(db: sqlite3.Connection, request: ReportRequest) -> str:
    def bound_text(bound: Bound) -> str:
        return bound.at.astimezone(request.zone).isoformat() if isinstance(bound, TimeBound) else "unbounded"

    lines = [f"{request.metric.name} — {request.metric.description}",
             f"Bounds: {bound_text(request.window.since)} <= at < {bound_text(request.window.until)}; " +
             f"zone: {request.zone.key}" + ("; exact evidence only" if request.exact else ""), ""]
    predicate, parameters = request.window.predicate("at")
    exact = " AND attribution = 'recorded'" if request.exact else ""
    has_turns = cast(int, db.execute(f"SELECT EXISTS(SELECT 1 FROM turns WHERE {predicate}{exact} LIMIT 1)", parameters).fetchone()[0])
    if not has_turns:
        lines.extend(["No turns in range.", ""])
    else:
        data = report_data(db, request)
        numerator = aggregate_groups(request, request.metric.numerator, data["numerator"])
        denominator = ([] if isinstance(request.metric.denominator, NoDenominator)
                       else aggregate_groups(request, request.metric.denominator, data["denominator"]))
        lines.extend([*metric_table(request, join_groups(request, numerator, denominator)), "",
                      *coverage(request, data), ""])
    lines.extend(sources(db, request.zone))
    return "\n".join(lines).rstrip() + "\n"


class ReportRefused(ValueError):
    """A report argument cannot name a supported metric, grouping or time window."""


class StoreVersionMismatch(ValueError):
    """The report and collector store have different schema versions."""


def check_store_version(db: sqlite3.Connection) -> None:
    version = cast(int, db.execute("PRAGMA user_version").fetchone()[0])
    if version < collect.STORE_VERSION:
        raise StoreVersionMismatch(f"store version {version} is older than report version {collect.STORE_VERSION}; "
                                   + "run the collector once")
    if version > collect.STORE_VERSION:
        raise StoreVersionMismatch(f"store version {version} is newer than report version {collect.STORE_VERSION}; "
                                   + "this report is older than the store")


class ReportParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise ReportRefused(message + "; accepted: usage_report.py <metric> --by DIM[,DIM…] [--since WHEN] [--until WHEN] [--zone NAME] [--exact]")


def parse_bound(value: str, zone: ZoneInfo) -> Bound:
    if not value:
        return Unbounded()
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return TimeBound(at.replace(tzinfo=zone) if at.tzinfo is None else at)
    except ValueError as error:
        raise ReportRefused(f"invalid date {value!r}; accepted: ISO date or datetime, in {zone.key} without an offset") from error


@dataclass(frozen=True)
class DefaultGrouping:
    pass


class ReportArguments(argparse.Namespace):
    metric: str
    db: Path
    production_docs: Path
    by: str | DefaultGrouping
    since: str
    until: str
    zone: str
    exact: bool

    def __init__(self) -> None:
        super().__init__()
        self.metric = ""
        self.db = collect.store_path()
        self.production_docs = outcomes.default_sources().production_docs
        self.by = DefaultGrouping()
        self.since = ""
        self.until = ""
        self.zone = user_zone_name()
        self.exact = False


def report_request(args: ReportArguments) -> ReportRequest:
    name = args.metric
    if name not in METRICS:
        raise ReportRefused(f"unknown metric {name!r}; accepted: {', '.join(METRICS)}")
    metric = METRICS[name]
    accepted = set(dimension for side in metric_sides(metric) for dimension in side_dimensions(side))
    default = ("role,model" if all(DIMENSIONS[name] in accepted for name in ("role", "model"))
               else ",".join(dimension.name for dimension in ALL_DIMENSIONS if dimension in accepted))
    by = args.by if isinstance(args.by, str) else default
    names = by.split(",")
    if any(name not in DIMENSIONS for name in names) or len(set(names)) != len(names):
        raise ReportRefused(f"invalid dimensions {by!r}; accepted: {', '.join(DIMENSIONS)} (once each)")
    dimensions = tuple(DIMENSIONS[name] for name in names)
    if any(dimension not in accepted for dimension in dimensions):
        raise ReportRefused(f"metric {metric.name!r} cannot group by {by!r}; accepted: " +
                            ", ".join(dimension.name for dimension in ALL_DIMENSIONS if dimension in accepted))
    zone_name = args.zone
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ReportRefused(f"unknown zone {zone_name!r}; accepted: IANA zone names, such as {user_zone_name()} or UTC") from error
    window = TimeWindow(parse_bound(args.since, zone), parse_bound(args.until, zone))
    if (isinstance(window.since, TimeBound) and isinstance(window.until, TimeBound)
            and window.since.at >= window.until.at):
        raise ReportRefused("invalid bounds; accepted: --since earlier than --until (inclusive start, exclusive end)")
    return ReportRequest(metric, dimensions, window, zone, args.exact)


def open_read_store(path: Path, *, factory: type[sqlite3.Connection] = sqlite3.Connection) -> sqlite3.Connection:
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, factory=factory)
    try:
        db.row_factory = sqlite3.Row
        _ = db.execute("BEGIN")
    except BaseException:
        db.close()
        raise
    return db


def main(argv: Sequence[str] = ()) -> int:
    parser = ReportParser(description=__doc__)
    _ = parser.add_argument("metric")
    _ = parser.add_argument("--db", type=Path)
    _ = parser.add_argument("--production-docs", type=Path)
    _ = parser.add_argument("--by")
    _ = parser.add_argument("--since")
    _ = parser.add_argument("--until")
    _ = parser.add_argument("--zone")
    _ = parser.add_argument("--exact", action="store_true")
    try:
        args = parser.parse_args(argv, namespace=ReportArguments())
        if args.metric == "list":
            print("\n".join(f"{metric.name} — {metric.description}" for metric in METRICS.values()))
            return 0
        request = report_request(args)
        with closing(open_read_store(args.db)) as db:
            check_store_version(db)
            register_report_functions(db, unit_worktrees(args.production_docs))
            print(render_report(db, request), end="")
    except ReportRefused as error:
        print("usage_report: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, StoreVersionMismatch) as error:
        print("usage_report: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
