#!/usr/bin/env python3
"""Per-project nightly style history helpers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

RUST_DIR = Path(os.environ.get("STYLE_HISTORY_RUST_DIR", str(Path.home() / "rust")))
NATE_STYLE_DIR = Path(os.environ.get("STYLE_HISTORY_NATE_STYLE_DIR", str(RUST_DIR / "nate_style")))
HISTORY_DIR = NATE_STYLE_DIR / ".history"
PENDING_DIR = HISTORY_DIR / ".pending"
LOAD_STYLE_SCRIPT = Path(
    os.environ.get(
        "STYLE_HISTORY_LOAD_STYLE_SCRIPT",
        str(Path.home() / ".claude" / "scripts" / "load-rust-style.sh"),
    )
)
NIGHTLY_CONF_FILE = Path(
    os.environ.get(
        "STYLE_HISTORY_CONF_FILE",
        str(Path.home() / ".claude" / "scripts" / "nightly" / "nightly-rust.conf"),
    )
)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    budget_cost: int
    checklist_index: int
    display_name: str
    guideline_ids: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_guideline_id(raw_path: str, project_root: Path | None = None) -> str:
    path = raw_path.strip().strip("`")
    if path.startswith("~"):
        path = str(Path(path).expanduser())
    path_obj = Path(path)
    try:
        return str(path_obj.relative_to(NATE_STYLE_DIR))
    except ValueError:
        pass
    if "/docs/style/" in path:
        return "docs/style/" + path.split("/docs/style/", 1)[1]
    if project_root is not None:
        try:
            return str(path_obj.relative_to(project_root))
        except ValueError:
            pass
    return path_obj.name


def parse_frontmatter(path: Path) -> dict[str, list[str] | str]:
    tags: list[str] = []
    group = ""
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return {"tags": tags, "group": group}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("group:"):
            group = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("-"):
            tags.append(stripped.lstrip("-").strip())
    return {"tags": tags, "group": group}


def extract_title(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.name


def history_file(project: str) -> Path:
    return HISTORY_DIR / f"{project}.jsonl"


def pending_file(project: str) -> Path:
    return PENDING_DIR / f"{project}.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def excluded_projects() -> set[str]:
    if not NIGHTLY_CONF_FILE.exists():
        return set()
    excluded: set[str] = set()
    current_section = ""
    for raw_line in NIGHTLY_CONF_FILE.read_text().splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            continue
        if current_section == "exclude":
            excluded.add(stripped)
    return excluded


def eligible_project_roots() -> list[Path]:
    excluded = excluded_projects()
    return sorted(
        path
        for path in RUST_DIR.iterdir()
        if path.is_dir()
        and not path.name.endswith("_style_fix")
        and (path / "Cargo.toml").exists()
        and not (path / ".git").is_file()
        and path.name not in excluded
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def load_history(project: str) -> list[dict[str, Any]]:
    path = history_file(project)
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


def list_style_files(project_root: Path) -> list[Path]:
    result = subprocess.run(
        ["zsh", str(LOAD_STYLE_SCRIPT), "--list-files", "--project-root", str(project_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def build_units(project_root: Path) -> list[Unit]:
    style_files = list_style_files(project_root)
    grouped: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for index, style_file in enumerate(style_files, start=1):
        frontmatter = parse_frontmatter(style_file)
        tags = set(frontmatter["tags"])
        group = str(frontmatter["group"])
        guideline_id = normalize_guideline_id(str(style_file), project_root)
        unit_id = f"group::{group}" if group else guideline_id
        if unit_id not in grouped:
            ordered_ids.append(unit_id)
            grouped[unit_id] = {
                "unit_id": unit_id,
                "budget_cost": 0 if "non-negotiable" in tags else 1,
                "checklist_index": index,
                "display_name": extract_title(style_file) if not group else group,
                "guideline_ids": [],
            }
        grouped[unit_id]["guideline_ids"].append(guideline_id)
        if "non-negotiable" in tags:
            grouped[unit_id]["budget_cost"] = 0
    return [
        Unit(
            unit_id=grouped[unit_id]["unit_id"],
            budget_cost=int(grouped[unit_id]["budget_cost"]),
            checklist_index=int(grouped[unit_id]["checklist_index"]),
            display_name=str(grouped[unit_id]["display_name"]),
            guideline_ids=tuple(grouped[unit_id]["guideline_ids"]),
        )
        for unit_id in ordered_ids
    ]


def review_counts(project_root: Path) -> dict[str, int]:
    project = project_root.name.removesuffix("_style_fix")
    counts: dict[str, int] = defaultdict(int)
    for row in load_history(project):
        for reviewed in row.get("reviewed_units", []):
            guideline_id = reviewed.get("guideline_id")
            if isinstance(guideline_id, str):
                counts[guideline_id] += 1
    return counts


def non_negotiable_guideline_ids(project_root: Path) -> list[str]:
    return [
        guideline_id
        for unit in build_units(project_root)
        if unit.budget_cost == 0
        for guideline_id in unit.guideline_ids
    ]


def start_run(project_root: Path, budget: int) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    project = project_root.name.removesuffix("_style_fix")
    payload = {
        "budget": budget,
        "phase": "evaluation",
        "reviewed_unit_ids": [],
        "reviewed_units": [],
        "scored_count": 0,
        "start_time": utc_now(),
        "updated_at": utc_now(),
    }
    write_pending(project, payload)


def load_pending(project: str) -> dict[str, Any]:
    return load_json(pending_file(project), {})


def write_pending(project: str, payload: dict[str, Any]) -> None:
    path = pending_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def set_phase(project: str, phase: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    pending["phase"] = phase
    pending["updated_at"] = utc_now()
    write_pending(project, pending)


def next_unit(project_root: Path) -> dict[str, Any]:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        raise SystemExit(f"No pending run for {project}. Start a run first.")

    scored_count = int(pending.get("scored_count", 0))
    budget = int(pending.get("budget", 0))
    if scored_count >= budget:
        return {
            "budget": budget,
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "scored_count": scored_count,
            "status": "complete",
            "stop_reason": "budget_reached",
        }

    seen_unit_ids = set(pending.get("reviewed_unit_ids", []))
    units = build_units(project_root)
    counts = review_counts(project_root)
    sortable: list[tuple[int, int, str, Unit]] = []
    for unit in units:
        if unit.budget_cost == 0:
            continue
        if unit.unit_id in seen_unit_ids:
            continue
        unit_count = min(counts.get(guideline_id, 0) for guideline_id in unit.guideline_ids) if unit.guideline_ids else 0
        sortable.append((unit_count, unit.checklist_index, unit.unit_id, unit))
    sortable.sort(key=lambda item: (item[0], item[1], item[2]))

    if not sortable:
        return {
            "budget": budget,
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "scored_count": scored_count,
            "status": "complete",
            "stop_reason": "exhausted",
        }

    unit_count, _, _, unit = sortable[0]
    return {
        "budget": budget,
        "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
        "scored_count": scored_count,
        "status": "next",
        "unit": {
            "budget_cost": unit.budget_cost,
            "display_name": unit.display_name,
            "guideline_ids": list(unit.guideline_ids),
            "review_count_before": unit_count,
            "unit_id": unit.unit_id,
        },
    }


def record_unit(project_root: Path, results_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        raise SystemExit(f"No pending run for {project}. Start a run first.")

    payload = load_json(results_path, {})
    unit_id = payload.get("unit_id")
    results = payload.get("results", [])
    if not isinstance(unit_id, str) or not isinstance(results, list):
        raise SystemExit("Results file must contain unit_id and results.")

    reviewed_units = list(pending.get("reviewed_units", []))
    reviewed_unit_ids = list(pending.get("reviewed_unit_ids", []))
    if unit_id in reviewed_unit_ids:
        raise SystemExit(f"Unit already recorded in this run: {unit_id}")

    scored_increment = 0
    found_countable = False
    for result in results:
        if not isinstance(result, dict):
            continue
        guideline_id = result.get("guideline_id")
        if not isinstance(guideline_id, str):
            continue
        reviewed_entry: dict[str, Any] = {"guideline_id": guideline_id}
        outcome = result.get("outcome")
        if isinstance(outcome, dict):
            reviewed_entry["outcome"] = outcome
            if outcome.get("status") != "no_findings":
                found_countable = True
        else:
            finding_source = result.get("finding_source")
            if isinstance(finding_source, str):
                reviewed_entry["finding_source"] = finding_source
                found_countable = True
            else:
                raise SystemExit(f"Result for {guideline_id} must include outcome or finding_source.")
        reviewed_units.append(reviewed_entry)
    if found_countable:
        scored_increment = 1

    pending["reviewed_units"] = reviewed_units
    reviewed_unit_ids.append(unit_id)
    pending["reviewed_unit_ids"] = reviewed_unit_ids
    pending["scored_count"] = int(pending.get("scored_count", 0)) + scored_increment
    pending["phase"] = "evaluation"
    pending["updated_at"] = utc_now()
    pending["last_unit_id"] = unit_id
    pending["last_unit_result"] = "counted" if scored_increment else "no_findings"
    write_pending(project, pending)


def parse_eval_guidelines(path: Path, project_root: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    guideline_by_title: dict[str, str] = {}
    current_title = ""
    in_improvements = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line == "## Improvements":
            in_improvements = True
            continue
        if line.startswith("## ") and line != "## Improvements":
            in_improvements = False
        if not in_improvements:
            continue
        if line.startswith("### "):
            current_title = line.removeprefix("### ").strip()
            continue
        if line.startswith("**Style file**:"):
            raw_path = line.split(":", 1)[1].strip().strip("`")
            guideline_by_title[current_title] = normalize_guideline_id(raw_path, project_root)
    return guideline_by_title


def parse_fix_results(eval_path: Path, project_root: Path) -> dict[str, dict[str, Any]]:
    finding_guidelines: dict[int, str] = {}
    in_improvements = False
    current_finding = 0
    in_fix_summary = False
    current_fix = 0
    fix_entries: dict[int, dict[str, str]] = {}
    for raw_line in eval_path.read_text().splitlines():
        line = raw_line.strip()
        if line == "## Improvements":
            in_improvements = True
            in_fix_summary = False
            continue
        if line == "## Fix Summary":
            in_fix_summary = True
            in_improvements = False
            continue
        if line.startswith("## "):
            if line not in {"## Improvements", "## Fix Summary"}:
                in_improvements = False
                in_fix_summary = False
            continue
        if in_improvements:
            if line.startswith("### "):
                header = line.removeprefix("### ").strip()
                prefix = header.split(".", 1)[0]
                if prefix.isdigit():
                    current_finding = int(prefix)
                continue
            if line.startswith("**Style file**:") and current_finding:
                raw_path = line.split(":", 1)[1].strip().strip("`")
                finding_guidelines[current_finding] = normalize_guideline_id(raw_path, project_root)
                continue
        if in_fix_summary:
            if line.startswith("### Finding "):
                number = line.removeprefix("### Finding ").split(":", 1)[0].strip()
                if number.isdigit():
                    current_fix = int(number)
                    fix_entries[current_fix] = {}
                continue
            if line.startswith("**Status:**") and current_fix:
                fix_entries[current_fix]["status"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("**What was done:**") and current_fix:
                fix_entries[current_fix]["summary"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("**Issues:**") and current_fix:
                fix_entries[current_fix]["reason"] = line.split(":", 1)[1].strip()
                continue
    per_guideline: dict[str, list[dict[str, str]]] = defaultdict(list)
    for finding_num, guideline_id in finding_guidelines.items():
        if finding_num in fix_entries:
            per_guideline[guideline_id].append(fix_entries[finding_num])
    aggregated: dict[str, dict[str, Any]] = {}
    for guideline_id, entries in per_guideline.items():
        statuses = []
        for entry in entries:
            raw_status = entry.get("status", "").lower()
            if raw_status.startswith("applied"):
                statuses.append("fixed")
            elif raw_status.startswith("partially"):
                statuses.append("partial")
            elif raw_status.startswith("skipped"):
                statuses.append("skipped")
            else:
                statuses.append("fixed")
        if all(status == "fixed" for status in statuses):
            final_status = "fixed"
        elif all(status == "skipped" for status in statuses):
            final_status = "skipped"
        else:
            final_status = "partial"
        summaries = [entry.get("summary", "") for entry in entries if entry.get("summary")]
        reasons = [entry.get("reason", "") for entry in entries if entry.get("reason")]
        outcome: dict[str, Any] = {"status": final_status}
        if summaries:
            outcome["summary"] = " ".join(dict.fromkeys(summaries))
        if reasons:
            outcome["reason"] = " ".join(dict.fromkeys(reasons))
        aggregated[guideline_id] = outcome
    return aggregated


def finalize_no_findings(project: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    append_jsonl(history_file(project), {"start_time": pending["start_time"], "end_time": utc_now(), "reviewed_units": pending["reviewed_units"]})
    pending_file(project).unlink(missing_ok=True)


def finalize_fix(project_root: Path, eval_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        return
    fix_results = parse_fix_results(eval_path, project_root)
    reviewed_units = []
    for reviewed in pending.get("reviewed_units", []):
        guideline_id = reviewed["guideline_id"]
        if "outcome" in reviewed:
            reviewed_units.append(reviewed)
            continue
        outcome = dict(fix_results.get(guideline_id, {"status": "fix_failed", "reason": "Missing Fix Summary entry for reviewed guideline."}))
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append({"guideline_id": guideline_id, "outcome": outcome})
    append_jsonl(history_file(project), {"start_time": pending["start_time"], "end_time": utc_now(), "reviewed_units": reviewed_units})
    pending_file(project).unlink(missing_ok=True)


def finalize_failure(project: str, reason: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    reviewed_units = []
    for reviewed in pending.get("reviewed_units", []):
        if "outcome" in reviewed:
            reviewed_units.append(reviewed)
            continue
        outcome = {"status": "fix_failed", "reason": reason}
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append({"guideline_id": reviewed["guideline_id"], "outcome": outcome})
    append_jsonl(history_file(project), {"start_time": pending["start_time"], "end_time": utc_now(), "reviewed_units": reviewed_units})
    pending_file(project).unlink(missing_ok=True)


def discard_pending(project: str) -> None:
    pending_file(project).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start-run")
    start.add_argument("--project-root", required=True)
    start.add_argument("--budget", required=True, type=int)
    next_parser = subparsers.add_parser("next-unit")
    next_parser.add_argument("--project-root", required=True)
    record = subparsers.add_parser("record-unit")
    record.add_argument("--project-root", required=True)
    record.add_argument("--results", required=True)
    no_findings = subparsers.add_parser("finalize-no-findings")
    no_findings.add_argument("--project", required=True)
    finalize = subparsers.add_parser("finalize-fix")
    finalize.add_argument("--project-root", required=True)
    finalize.add_argument("--evaluation", required=True)
    failure = subparsers.add_parser("finalize-failure")
    failure.add_argument("--project", required=True)
    failure.add_argument("--reason", required=True)
    discard = subparsers.add_parser("discard-pending")
    discard.add_argument("--project", required=True)
    phase = subparsers.add_parser("set-phase")
    phase.add_argument("--project", required=True)
    phase.add_argument("--phase", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "start-run":
        start_run(Path(args.project_root).expanduser().resolve(), args.budget)
        return
    if args.command == "next-unit":
        print(json.dumps(next_unit(Path(args.project_root).expanduser().resolve()), indent=2, sort_keys=True))
        return
    if args.command == "record-unit":
        record_unit(Path(args.project_root).expanduser().resolve(), Path(args.results).expanduser().resolve())
        return
    if args.command == "finalize-no-findings":
        finalize_no_findings(args.project)
        return
    if args.command == "finalize-fix":
        finalize_fix(Path(args.project_root).expanduser().resolve(), Path(args.evaluation).expanduser().resolve())
        return
    if args.command == "discard-pending":
        discard_pending(args.project)
        return
    if args.command == "set-phase":
        set_phase(args.project, args.phase)
        return
    finalize_failure(args.project, args.reason)


if __name__ == "__main__":
    main()
