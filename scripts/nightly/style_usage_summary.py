#!/usr/bin/env python3
"""Style usage reporting with coverage and blocked-items views."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import TypedDict

from style_review_state import build_units
from style_usage_merge import normalize_reason_category


RUST_DIR = Path.home() / "rust"
USAGE_DIR = RUST_DIR / "nate_style" / "usage"
LOG_FILE = USAGE_DIR / "log.jsonl"
LEDGER_FILE = USAGE_DIR / "review_ledger.json"
EVENTS_FILE = USAGE_DIR / "evaluation_events.jsonl"
REPORT_FILE = RUST_DIR / "nate_style" / "style_report.md"


class FindingEntry(TypedDict, total=False):
    finding: int
    reason: str
    reason_category: str
    status: str


def parse_since(value: str) -> timedelta:
    amount = int(value[:-1])
    unit = value[-1]
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return timedelta(days=amount * 30)


def load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def filter_rows(
    rows: list[dict[str, object]],
    since: timedelta | None,
    project: str | None,
) -> list[dict[str, object]]:
    if since is None and project is None:
        return rows

    now = datetime.now(tz=timezone.utc)
    filtered: list[dict[str, object]] = []
    for row in rows:
        if project and row.get("project") != project:
            continue
        if since:
            timestamp = str(row.get("timestamp", ""))
            if not timestamp:
                continue
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if now - ts > since:
                continue
        filtered.append(row)
    return filtered


def build_style_summary(rows: list[dict[str, object]], include_local: bool) -> list[dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for row in rows:
        if "status" in row:
            continue
        local = bool(row.get("local"))
        if not include_local and local:
            continue
        style_id = str(row["style_id"])
        payload = summary.setdefault(
            style_id,
            {
                "style_file": row["style_file"],
                "projects": set(),
                "applied": 0,
                "partial": 0,
                "skipped": 0,
                "last_seen": row["timestamp"],
            },
        )
        payload["projects"].add(str(row["project"]))
        payload["last_seen"] = max(str(payload["last_seen"]), str(row["timestamp"]))
        for finding in row.get("findings", []):
            if not isinstance(finding, dict):
                continue
            status = finding.get("status")
            if status == "applied":
                payload["applied"] += 1
            elif status == "partial":
                payload["partial"] += 1
            elif status == "skipped":
                payload["skipped"] += 1

    items = list(summary.values())
    items.sort(key=lambda item: (item["applied"] + item["partial"], item["style_file"]), reverse=True)
    return items


def build_blocked_items(rows: list[dict[str, object]], ledger: dict[str, object]) -> list[dict[str, object]]:
    blocked: dict[tuple[str, str], dict[str, object]] = {}
    projects = ledger.get("projects", {}) if isinstance(ledger, dict) else {}
    style_review_count_by_project: dict[str, dict[str, int]] = {}

    if isinstance(projects, dict):
        for project in projects:
            project_root = RUST_DIR / project
            if not project_root.exists():
                continue
            project_guidelines = projects.get(project, {}).get("guidelines", {})
            if not isinstance(project_guidelines, dict):
                continue
            style_review_count_by_project[project] = {}
            try:
                units = build_units(project_root)
            except Exception:
                continue
            for unit in units:
                review_count = int(
                    project_guidelines.get(unit.guideline_id, {}).get("review_count", 0)
                )
                for style_id in unit.style_ids:
                    style_review_count_by_project[project][style_id] = review_count

    for row in rows:
        if "status" in row:
            continue
        project = str(row["project"])
        style_id = str(row["style_id"])
        key = (project, style_id)
        payload = blocked.setdefault(
            key,
            {
                "project": project,
                "style_file": row["style_file"],
                "partial": 0,
                "skipped": 0,
                "review_count": 0,
                "latest_reason": "",
                "latest_category": "other",
                "last_seen": row["timestamp"],
            },
        )
        payload["last_seen"] = max(str(payload["last_seen"]), str(row["timestamp"]))
        payload["review_count"] = max(
            payload["review_count"],
            style_review_count_by_project.get(project, {}).get(style_id, 0),
        )

        for finding in row.get("findings", []):
            if not isinstance(finding, dict):
                continue
            status = finding.get("status")
            if status == "partial":
                payload["partial"] += 1
            elif status == "skipped":
                payload["skipped"] += 1
            else:
                continue
            reason = str(finding.get("reason", "")).strip()
            if reason:
                payload["latest_reason"] = reason
                payload["latest_category"] = str(
                    finding.get("reason_category") or normalize_reason_category(reason)
                )

    items = [
        item
        for item in blocked.values()
        if item["partial"] > 0 or item["skipped"] > 0
    ]
    items.sort(
        key=lambda item: (
            item["skipped"] + item["partial"],
            item["review_count"],
            item["style_file"],
        ),
        reverse=True,
    )
    return items


def build_coverage_view(ledger: dict[str, object]) -> list[dict[str, object]]:
    projects = ledger.get("projects", {}) if isinstance(ledger, dict) else {}
    items: list[dict[str, object]] = []
    if not isinstance(projects, dict):
        return items

    for project, payload in sorted(projects.items()):
        guidelines = payload.get("guidelines", {}) if isinstance(payload, dict) else {}
        if not isinstance(guidelines, dict) or not guidelines:
            continue
        counts = [int(entry.get("review_count", 0)) for entry in guidelines.values() if isinstance(entry, dict)]
        if not counts:
            continue
        items.append(
            {
                "project": project,
                "guidelines": len(counts),
                "min_review_count": min(counts),
                "max_review_count": max(counts),
                "avg_review_count": round(sum(counts) / len(counts), 2),
            }
        )
    return items


def render_report(
    style_rows: list[dict[str, object]],
    blocked_rows: list[dict[str, object]],
    coverage_rows: list[dict[str, object]],
    total_entries: int,
) -> str:
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
        "## Style Usage History",
        "| Style | Projects | Applied | Partial | Skipped | Last Seen |",
        "|---|---|---|---|---|---|",
    ]

    for row in style_rows:
        lines.append(
            f"| {row['style_file']} | {len(row['projects'])} | {row['applied']} | "
            f"{row['partial']} | {row['skipped']} | {str(row['last_seen'])[:16]} |"
        )

    if coverage_rows:
        lines.extend(
            [
                "",
                "## Review Coverage",
                "| Project | Guideline Units | Min Count | Max Count | Avg Count |",
                "|---|---|---|---|---|",
            ]
        )
        for row in coverage_rows:
            lines.append(
                f"| {row['project']} | {row['guidelines']} | {row['min_review_count']} | "
                f"{row['max_review_count']} | {row['avg_review_count']} |"
            )

    if blocked_rows:
        lines.extend(
            [
                "",
                "## Blocked Items View",
                "| Project | Style | Review Count | Partial | Skipped | Category | Latest Reason |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in blocked_rows:
            lines.append(
                f"| {row['project']} | {row['style_file']} | {row['review_count']} | "
                f"{row['partial']} | {row['skipped']} | {row['latest_category']} | "
                f"{row['latest_reason']} |"
            )

    lines.extend(
        [
            "",
            f"*Generated {timestamp} from {total_entries} log entries*",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since")
    parser.add_argument("--project")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()

    since = parse_since(args.since) if args.since else None
    rows = filter_rows(load_jsonl(LOG_FILE), since, args.project)
    ledger = json.loads(LEDGER_FILE.read_text()) if LEDGER_FILE.exists() else {}
    style_rows = build_style_summary(rows, include_local=args.local)
    blocked_rows = build_blocked_items(rows, ledger)
    coverage_rows = build_coverage_view(ledger)

    if args.generate:
        REPORT_FILE.write_text(
            render_report(style_rows, blocked_rows, coverage_rows, len(rows))
        )
        print(f"Wrote {REPORT_FILE}")
        return

    print("Shared Styles:")
    for row in style_rows:
        print(
            f"{row['style_file']}: projects={len(row['projects'])} "
            f"applied={row['applied']} partial={row['partial']} skipped={row['skipped']}"
        )
    if coverage_rows:
        print("\nReview Coverage:")
        for row in coverage_rows:
            print(
                f"{row['project']}: units={row['guidelines']} min={row['min_review_count']} "
                f"max={row['max_review_count']} avg={row['avg_review_count']}"
            )
    if blocked_rows:
        print("\nBlocked Items:")
        for row in blocked_rows:
            print(
                f"{row['project']} {row['style_file']}: partial={row['partial']} "
                f"skipped={row['skipped']} category={row['latest_category']}"
            )


if __name__ == "__main__":
    main()
