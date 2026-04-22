#!/usr/bin/env python3
"""Generate style report from per-project .history files."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from style_history import HISTORY_DIR, RUST_DIR, build_units, list_style_files, normalize_guideline_id, parse_frontmatter, resolve_project_root

REPORT_FILE = RUST_DIR / "nate_style" / "style_report.md"


def parse_since(value: str) -> timedelta:
    amount = int(value[:-1])
    unit = value[-1]
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return timedelta(days=amount * 30)


def load_project_history(project: str) -> list[dict[str, Any]]:
    path = HISTORY_DIR / f"{project}.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def list_projects() -> list[str]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(path.stem for path in HISTORY_DIR.glob("*.jsonl") if path.is_file())


def guideline_metadata(project: str) -> dict[str, dict[str, Any]]:
    project_root = resolve_project_root(project)
    if project_root is None:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    try:
        style_files = list_style_files(project_root)
    except Exception:
        return metadata
    for style_file in style_files:
        guideline_id = normalize_guideline_id(str(style_file), project_root)
        frontmatter = parse_frontmatter(style_file)
        tags = frontmatter["tags"] if isinstance(frontmatter["tags"], list) else []
        metadata[guideline_id] = {
            "non_negotiable": "non-negotiable" in tags,
        }
    return metadata


def iter_rows(since: timedelta | None, project_filter: str | None) -> list[tuple[str, dict[str, Any]]]:
    now = datetime.now(tz=timezone.utc)
    rows: list[tuple[str, dict[str, Any]]] = []
    for project in list_projects():
        if project_filter and project != project_filter:
            continue
        for row in load_project_history(project):
            if since:
                end_time = row.get("end_time") or row.get("start_time")
                if not isinstance(end_time, str):
                    continue
                ts = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if now - ts > since:
                    continue
            rows.append((project, row))
    return rows


def build_style_summary(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for project, row in rows:
        end_time = row.get("end_time") or row.get("start_time")
        for reviewed in row.get("reviewed_units", []):
            if not isinstance(reviewed, dict):
                continue
            guideline_id = reviewed.get("guideline_id")
            outcome = reviewed.get("outcome", {})
            if not isinstance(guideline_id, str) or not isinstance(outcome, dict):
                continue
            payload = summary.setdefault(
                guideline_id,
                {
                    "guideline_id": guideline_id,
                    "projects": set(),
                    "fixed": 0,
                    "partial": 0,
                    "skipped": 0,
                    "fix_failed": 0,
                    "no_findings": 0,
                    "last_seen": end_time,
                },
            )
            payload["projects"].add(project)
            if isinstance(end_time, str):
                payload["last_seen"] = max(str(payload["last_seen"]), end_time)
            status = outcome.get("status")
            if isinstance(status, str) and status in payload:
                payload[status] += 1
    items = list(summary.values())
    items.sort(key=lambda item: (item["fixed"] + item["partial"] + item["skipped"] + item["fix_failed"], item["guideline_id"]), reverse=True)
    return items


def build_coverage_view(project_filter: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project in list_projects():
        if project_filter and project != project_filter:
            continue
        project_root = resolve_project_root(project)
        if project_root is None:
            continue
        counts: dict[str, int] = defaultdict(int)
        for row in load_project_history(project):
            for reviewed in row.get("reviewed_units", []):
                guideline_id = reviewed.get("guideline_id")
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


def build_blocked_view(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    blocked: dict[tuple[str, str], dict[str, Any]] = {}
    cumulative: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for project, row in rows:
        for reviewed in row.get("reviewed_units", []):
            guideline_id = reviewed.get("guideline_id")
            if isinstance(guideline_id, str):
                cumulative[project][guideline_id] += 1
            outcome = reviewed.get("outcome", {})
            if not isinstance(guideline_id, str) or not isinstance(outcome, dict):
                continue
            status = outcome.get("status")
            if status not in {"partial", "skipped", "fix_failed"}:
                continue
            payload = blocked.setdefault(
                (project, guideline_id),
                {
                    "project": project,
                    "guideline_id": guideline_id,
                    "review_count": 0,
                    "partial": 0,
                    "skipped": 0,
                    "fix_failed": 0,
                    "latest_reason": "",
                },
            )
            payload["review_count"] = cumulative[project][guideline_id]
            payload[status] += 1
            reason = outcome.get("reason")
            if isinstance(reason, str) and reason.strip():
                payload["latest_reason"] = reason.strip()
    items = list(blocked.values())
    items.sort(key=lambda item: (item["partial"] + item["skipped"] + item["fix_failed"], item["review_count"], item["guideline_id"]), reverse=True)
    return items


def build_run_views(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for project, row in rows:
        grouped_rows[project].append(row)

    run_views: list[dict[str, Any]] = []
    for project, project_rows in grouped_rows.items():
        metadata = guideline_metadata(project)
        for row in sorted(project_rows, key=lambda item: item.get("end_time") or item.get("start_time") or "", reverse=True):
            reviewed_units = row.get("reviewed_units", [])
            if not isinstance(reviewed_units, list):
                continue
            detailed_units: list[dict[str, Any]] = []
            status_counts: dict[str, int] = defaultdict(int)
            findings_produced = 0
            for reviewed in reviewed_units:
                if not isinstance(reviewed, dict):
                    continue
                guideline_id = reviewed.get("guideline_id")
                outcome = reviewed.get("outcome", {})
                if not isinstance(guideline_id, str) or not isinstance(outcome, dict):
                    continue
                status = str(outcome.get("status", "unknown"))
                meta = metadata.get(guideline_id, {})
                non_negotiable = bool(meta.get("non_negotiable"))
                status_counts[status] += 1
                if status != "no_findings":
                    findings_produced += 1
                detailed_units.append({
                    "guideline_id": guideline_id,
                    "non_negotiable": non_negotiable,
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


def render_report(style_rows: list[dict[str, Any]], coverage_rows: list[dict[str, Any]], blocked_rows: list[dict[str, Any]], total_runs: int) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f'date_created: "[[{today}]]"',
        f'date_modified: "[[{today}]]"',
        "tags:",
        "  - report",
        "  - style",
        "---",
        "# Style Report",
        "## Style History",
        "| Guideline | Projects | Fixed | Partial | Skipped | Fix Failed | No Findings | Last Seen |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in style_rows:
        lines.append(
            f"| {row['guideline_id']} | {len(row['projects'])} | {row['fixed']} | {row['partial']} | "
            f"{row['skipped']} | {row['fix_failed']} | {row['no_findings']} | {str(row['last_seen'])[:16]} |"
        )
    if coverage_rows:
        lines.extend(["", "## Review Coverage", "| Project | Guideline Units | Min Count | Max Count | Avg Count |", "|---|---|---|---|---|"])
        for row in coverage_rows:
            lines.append(
                f"| {row['project']} | {row['guideline_units']} | {row['min_review_count']} | {row['max_review_count']} | {row['avg_review_count']} |"
            )
    if blocked_rows:
        lines.extend(["", "## Blocked Items View", "| Project | Guideline | Review Count | Partial | Skipped | Fix Failed | Latest Reason |", "|---|---|---|---|---|---|---|"])
        for row in blocked_rows:
            lines.append(
                f"| {row['project']} | {row['guideline_id']} | {row['review_count']} | {row['partial']} | {row['skipped']} | {row['fix_failed']} | {row['latest_reason']} |"
            )
    lines.extend(["", f"*Generated {timestamp} from {total_runs} recorded runs*", ""])
    return "\n".join(lines)


def print_run_views(run_views: list[dict[str, Any]]) -> None:
    for run in run_views:
        start_time = run.get("start_time") or "unknown"
        end_time = run.get("end_time") or "unknown"
        print(f"{run['project']} run {start_time} -> {end_time}")
        print(
            "  "
            f"selected_units={run['selected_unit_count']} "
            f"reviewed_guidelines={run['reviewed_guideline_count']} "
            f"findings={run['findings_produced']}"
        )
        counts = run["status_counts"]
        ordered_statuses = ["fixed", "partial", "skipped", "fix_failed", "no_findings"]
        rendered_counts = " ".join(f"{status}={counts.get(status, 0)}" for status in ordered_statuses if counts.get(status, 0))
        if rendered_counts:
            print(f"  outcomes: {rendered_counts}")
        for unit in run["units"]:
            extras: list[str] = []
            if unit.get("non_negotiable"):
                extras.append("non_negotiable")
            if unit.get("finding_source"):
                extras.append(f"source={unit['finding_source']}")
            extra_text = f" ({', '.join(extras)})" if extras else ""
            print(f"  - {unit['guideline_id']}: {unit['status']}{extra_text}")
            if unit.get("summary"):
                print(f"    summary: {unit['summary']}")
            if unit.get("reason"):
                print(f"    reason: {unit['reason']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since")
    parser.add_argument("--project")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--latest-run", action="store_true")
    args = parser.parse_args()

    since = parse_since(args.since) if args.since else None
    rows = iter_rows(since, args.project)
    style_rows = build_style_summary(rows)
    coverage_rows = build_coverage_view(args.project)
    blocked_rows = build_blocked_view(rows)
    run_views = build_run_views(rows)

    if args.generate:
        REPORT_FILE.write_text(render_report(style_rows, coverage_rows, blocked_rows, len(rows)))
        print(f"Wrote {REPORT_FILE}")
        return

    if args.latest_run:
        if args.project:
            run_views = [run for run in run_views if run["project"] == args.project]
        if run_views:
            print_run_views(run_views[:1])
        return

    print("Style History:")
    for row in style_rows:
        print(f"{row['guideline_id']}: fixed={row['fixed']} partial={row['partial']} skipped={row['skipped']} fix_failed={row['fix_failed']} no_findings={row['no_findings']}")
    if coverage_rows:
        print("\nReview Coverage:")
        for row in coverage_rows:
            print(f"{row['project']}: units={row['guideline_units']} min={row['min_review_count']} max={row['max_review_count']} avg={row['avg_review_count']}")
    if blocked_rows:
        print("\nBlocked Items:")
        for row in blocked_rows:
            print(f"{row['project']} {row['guideline_id']}: partial={row['partial']} skipped={row['skipped']} fix_failed={row['fix_failed']}")
    if args.project and run_views:
        print("\nLatest Run:")
        print_run_views(run_views[:1])


if __name__ == "__main__":
    main()
