#!/usr/bin/env python3
"""Generate style report from per-project .history files."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TypedDict
from typing import cast

from style_history import HISTORY_DIR, HistoryRow, Outcome, build_units, list_style_files, normalize_guideline_id, parse_frontmatter, resolve_project_root

STATUS_FIELDS = ("fixed", "partial", "skipped", "fix_failed", "no_findings")
BLOCKING_STATUSES = frozenset({"partial", "skipped", "fix_failed"})


class StyleSummaryRow(TypedDict):
    guideline_id: str
    fixed: int
    partial: int
    skipped: int
    fix_failed: int
    no_findings: int
    last_seen: str


class CoverageRow(TypedDict):
    project: str
    guideline_units: int
    min_review_count: int
    max_review_count: int
    avg_review_count: float


class BlockedRow(TypedDict):
    project: str
    guideline_id: str
    review_count: int
    latest_status: str
    last_seen: str
    streak: int
    latest_reason: str


class UnitView(TypedDict):
    guideline_id: str
    non_negotiable: bool
    status: str
    finding_source: str | None
    summary: str | None
    reason: str | None


class RunView(TypedDict):
    project: str
    start_time: str | None
    end_time: str | None
    selected_unit_count: int
    reviewed_guideline_count: int
    findings_produced: int
    status_counts: dict[str, int]
    units: list[UnitView]


class ReportArgs(argparse.Namespace):
    since: str | None = None
    project: str | None = None
    latest_run: bool = False


def parse_since(value: str) -> timedelta:
    amount = int(value[:-1])
    unit = value[-1]
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return timedelta(days=amount * 30)


def parse_history_row(line: str) -> HistoryRow | None:
    try:
        return cast("HistoryRow", json.loads(line))
    except json.JSONDecodeError:
        return None


def load_project_history(project: str) -> list[HistoryRow]:
    path = HISTORY_DIR / f"{project}.jsonl"
    if not path.exists():
        return []
    rows: list[HistoryRow] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = parse_history_row(stripped)
        if row is not None:
            rows.append(row)
    return rows


def list_projects() -> list[str]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(path.stem for path in HISTORY_DIR.glob("*.jsonl") if path.is_file())


def row_time(row: HistoryRow) -> str:
    end = row.get("end_time") or row.get("start_time")
    return end if isinstance(end, str) else ""


def iter_outcomes(row: HistoryRow) -> Iterator[tuple[str, Outcome]]:
    for unit in row.get("reviewed_units") or []:
        guideline_id = unit.get("guideline_id")
        outcome = unit.get("outcome")
        if isinstance(guideline_id, str) and isinstance(outcome, dict):
            yield guideline_id, outcome


def guideline_metadata(project: str) -> dict[str, bool]:
    project_root = resolve_project_root(project)
    if project_root is None:
        return {}
    metadata: dict[str, bool] = {}
    try:
        style_files = list_style_files(project_root)
    except Exception:
        return metadata
    for style_file in style_files:
        guideline_id = normalize_guideline_id(str(style_file), project_root)
        frontmatter = parse_frontmatter(style_file)
        metadata[guideline_id] = "non-negotiable" in frontmatter["tags"]
    return metadata


def iter_rows(since: timedelta | None, project_filter: str | None) -> list[tuple[str, HistoryRow]]:
    now = datetime.now(tz=timezone.utc)
    rows: list[tuple[str, HistoryRow]] = []
    for project in list_projects():
        if project_filter and project != project_filter:
            continue
        for row in load_project_history(project):
            if since:
                end_time = row_time(row)
                if not end_time:
                    continue
                ts = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if now - ts > since:
                    continue
            rows.append((project, row))
    return rows


def build_style_summary(rows: list[tuple[str, HistoryRow]]) -> list[StyleSummaryRow]:
    counts: dict[str, dict[str, int]] = {}
    last_seen: dict[str, str] = {}
    for _project, row in rows:
        end_time = row_time(row)
        for guideline_id, outcome in iter_outcomes(row):
            tally = counts.setdefault(guideline_id, {field: 0 for field in STATUS_FIELDS})
            if end_time:
                last_seen[guideline_id] = max(last_seen.get(guideline_id, ""), end_time)
            status = outcome.get("status")
            if isinstance(status, str) and status in tally:
                tally[status] += 1
    items: list[StyleSummaryRow] = [
        {
            "guideline_id": guideline_id,
            "fixed": tally["fixed"],
            "partial": tally["partial"],
            "skipped": tally["skipped"],
            "fix_failed": tally["fix_failed"],
            "no_findings": tally["no_findings"],
            "last_seen": last_seen.get(guideline_id, ""),
        }
        for guideline_id, tally in counts.items()
    ]
    items.sort(key=lambda item: (item["fixed"] + item["partial"] + item["skipped"] + item["fix_failed"], item["guideline_id"]), reverse=True)
    return items


def build_coverage_view(project_filter: str | None) -> list[CoverageRow]:
    rows: list[CoverageRow] = []
    for project in list_projects():
        if project_filter and project != project_filter:
            continue
        project_root = resolve_project_root(project)
        if project_root is None:
            continue
        counts: defaultdict[str, int] = defaultdict(int)
        for row in load_project_history(project):
            for unit in row.get("reviewed_units") or []:
                guideline_id = unit.get("guideline_id")
                if isinstance(guideline_id, str):
                    counts[guideline_id] += 1
        try:
            units = build_units(project_root)
        except Exception:
            continue
        unit_counts = [
            min(counts.get(guideline_id, 0) for guideline_id in unit.guideline_ids)
            for unit in units
            if unit.guideline_ids and unit.budget_cost > 0
        ]
        if not unit_counts:
            continue
        rows.append({
            "project": project,
            "guideline_units": len(unit_counts),
            "min_review_count": min(unit_counts),
            "max_review_count": max(unit_counts),
            "avg_review_count": round(sum(unit_counts) / len(unit_counts), 2),
        })
    return rows


def build_blocked_view(rows: list[tuple[str, HistoryRow]]) -> list[BlockedRow]:
    # An item counts as blocked only if its MOST RECENT review still ended in a
    # blocking status. A guideline that failed once and was later fixed (or
    # produced no_findings) is resolved, not blocked — counting lifetime
    # partial/skipped/fix_failed totals made stale April failures read as a
    # current cluster. We walk each (project, guideline) timeline, take the
    # latest outcome, and only surface it when that latest outcome blocks. The
    # trailing run of consecutive blocking outcomes (streak) and last_seen date
    # distinguish "failing right now" from "failed weeks ago, never retried."
    timelines: dict[tuple[str, str], list[tuple[str, str | None, str | None]]] = defaultdict(list)
    for project, row in rows:
        end_time = row_time(row)
        for guideline_id, outcome in iter_outcomes(row):
            timelines[(project, guideline_id)].append((end_time, outcome.get("status"), outcome.get("reason")))

    items: list[BlockedRow] = []
    for (project, guideline_id), events in timelines.items():
        events.sort(key=lambda event: event[0])
        latest_time, latest_status, latest_reason = events[-1]
        if latest_status not in BLOCKING_STATUSES:
            continue
        streak = 0
        for _time, status, _reason in reversed(events):
            if status in BLOCKING_STATUSES:
                streak += 1
            else:
                break
        reason = latest_reason.strip() if isinstance(latest_reason, str) else ""
        items.append({
            "project": project,
            "guideline_id": guideline_id,
            "review_count": len(events),
            "latest_status": latest_status or "",
            "last_seen": latest_time,
            "streak": streak,
            "latest_reason": reason,
        })
    items.sort(key=lambda item: (item["last_seen"], item["streak"], item["guideline_id"]), reverse=True)
    return items


def build_run_views(rows: list[tuple[str, HistoryRow]]) -> list[RunView]:
    grouped_rows: defaultdict[str, list[HistoryRow]] = defaultdict(list)
    for project, row in rows:
        grouped_rows[project].append(row)

    run_views: list[RunView] = []
    for project, project_rows in grouped_rows.items():
        metadata = guideline_metadata(project)
        for row in sorted(project_rows, key=row_time, reverse=True):
            detailed_units: list[UnitView] = []
            status_counts: defaultdict[str, int] = defaultdict(int)
            findings_produced = 0
            for guideline_id, outcome in iter_outcomes(row):
                status = outcome.get("status") or "unknown"
                status_counts[status] += 1
                if status != "no_findings":
                    findings_produced += 1
                detailed_units.append({
                    "guideline_id": guideline_id,
                    "non_negotiable": metadata.get(guideline_id, False),
                    "status": status,
                    "finding_source": outcome.get("finding_source"),
                    "summary": outcome.get("summary"),
                    "reason": outcome.get("reason"),
                })
            run_views.append({
                "project": project,
                "start_time": row.get("start_time"),
                "end_time": row.get("end_time"),
                "selected_unit_count": len(detailed_units),
                "reviewed_guideline_count": len(detailed_units),
                "findings_produced": findings_produced,
                "status_counts": dict(status_counts),
                "units": detailed_units,
            })
    run_views.sort(key=lambda item: ((item["end_time"] or item["start_time"] or ""), item["project"]), reverse=True)
    return run_views


def print_run_views(run_views: list[RunView]) -> None:
    for run in run_views:
        start_time = run["start_time"] or "unknown"
        end_time = run["end_time"] or "unknown"
        print(f"{run['project']} run {start_time} -> {end_time}")
        print(f"  selected_units={run['selected_unit_count']} reviewed_guidelines={run['reviewed_guideline_count']} findings={run['findings_produced']}")
        counts = run["status_counts"]
        rendered_counts = " ".join(f"{status}={counts.get(status, 0)}" for status in STATUS_FIELDS if counts.get(status, 0))
        if rendered_counts:
            print(f"  outcomes: {rendered_counts}")
        for unit in run["units"]:
            extras: list[str] = []
            if unit["non_negotiable"]:
                extras.append("non_negotiable")
            if unit["finding_source"]:
                extras.append(f"source={unit['finding_source']}")
            extra_text = f" ({', '.join(extras)})" if extras else ""
            print(f"  - {unit['guideline_id']}: {unit['status']}{extra_text}")
            if unit["summary"]:
                print(f"    summary: {unit['summary']}")
            if unit["reason"]:
                print(f"    reason: {unit['reason']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--since")
    _ = parser.add_argument("--project")
    _ = parser.add_argument("--latest-run", action="store_true")
    args = parser.parse_args(namespace=ReportArgs())

    since = parse_since(args.since) if args.since else None
    rows = iter_rows(since, args.project)
    style_rows = build_style_summary(rows)
    coverage_rows = build_coverage_view(args.project)
    blocked_rows = build_blocked_view(rows)
    run_views = build_run_views(rows)

    if args.latest_run:
        if args.project:
            run_views = [run for run in run_views if run["project"] == args.project]
        if run_views:
            print_run_views(run_views[:1])
        return

    print("Style History:")
    for style_row in style_rows:
        print(f"{style_row['guideline_id']}: fixed={style_row['fixed']} partial={style_row['partial']} skipped={style_row['skipped']} fix_failed={style_row['fix_failed']} no_findings={style_row['no_findings']}")
    if coverage_rows:
        print("\nReview Coverage:")
        for coverage_row in coverage_rows:
            print(f"{coverage_row['project']}: units={coverage_row['guideline_units']} min={coverage_row['min_review_count']} max={coverage_row['max_review_count']} avg={coverage_row['avg_review_count']}")
    if blocked_rows:
        print("\nBlocked Items (latest review still blocking):")
        for blocked_row in blocked_rows:
            last_seen = (blocked_row["last_seen"] or "unknown")[:10]
            reason = f" reason={blocked_row['latest_reason']}" if blocked_row["latest_reason"] else ""
            print(f"{blocked_row['project']} {blocked_row['guideline_id']}: {blocked_row['latest_status']} @ {last_seen} streak={blocked_row['streak']} reviews={blocked_row['review_count']}{reason}")
    if args.project and run_views:
        print("\nLatest Run:")
        print_run_views(run_views[:1])


if __name__ == "__main__":
    main()
