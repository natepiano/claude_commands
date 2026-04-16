#!/usr/bin/env python3
"""State management for nightly style review coverage.

Tracks per-project review counts for guideline selection units and records
evaluation events separately from fix outcome history.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


RUST_DIR = Path.home() / "rust"
USAGE_DIR = RUST_DIR / "nate_style" / "usage"
LEDGER_FILE = USAGE_DIR / "review_ledger.json"
EVENTS_FILE = USAGE_DIR / "evaluation_events.jsonl"
STYLE_LOG_FILE = USAGE_DIR / "log.jsonl"
LOAD_STYLE_SCRIPT = Path.home() / ".claude" / "scripts" / "load-rust-style.sh"


@dataclass(frozen=True)
class Unit:
    guideline_id: str
    guideline_kind: str
    budget_kind: str
    checklist_index: int
    display_name: str
    style_ids: tuple[str, ...]
    style_files: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def normalize_style_path(raw_path: str, project: str) -> str:
    path = raw_path.strip()
    if path.startswith("~"):
        path = str(Path(path).expanduser())
    nate_style_dir = str(RUST_DIR / "nate_style")
    if path.startswith(nate_style_dir + "/"):
        relative = path[len(nate_style_dir) + 1 :]
        return f"shared:{relative}"
    if "/docs/style/" in path:
        return f"local:{project}:{Path(path).name}"
    return f"shared:rust/{Path(path).name}"


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


def list_style_files(project_root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "zsh",
            str(LOAD_STYLE_SCRIPT),
            "--list-files",
            "--project-root",
            str(project_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def build_units(project_root: Path) -> list[Unit]:
    project = project_root.name.removesuffix("_style_fix")
    style_files = list_style_files(project_root)
    unit_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []

    for style_file in style_files:
        frontmatter = parse_frontmatter(style_file)
        tags = set(frontmatter["tags"])
        group = str(frontmatter["group"])
        style_id = normalize_style_path(str(style_file), project)

        if group:
            namespace = style_id.split(":", 2)[0]
            guideline_id = f"group:{namespace}:{group}"
            guideline_kind = "group"
        else:
            guideline_id = style_id
            guideline_kind = "rule"

        if guideline_id not in unit_by_id:
            ordered_ids.append(guideline_id)
            unit_by_id[guideline_id] = {
                "guideline_id": guideline_id,
                "guideline_kind": guideline_kind,
                "budget_kind": "normal",
                "checklist_index": len(ordered_ids),
                "display_name": extract_title(style_file) if not group else group,
                "style_ids": [],
                "style_files": [],
            }

        unit = unit_by_id[guideline_id]
        unit["style_ids"].append(style_id)
        unit["style_files"].append(style_file.name)
        if "non-negotiable" in tags:
            unit["budget_kind"] = "non_negotiable"
        elif guideline_kind == "group":
            unit["budget_kind"] = "group"

    return [
        Unit(
            guideline_id=payload["guideline_id"],
            guideline_kind=payload["guideline_kind"],
            budget_kind=payload["budget_kind"],
            checklist_index=payload["checklist_index"],
            display_name=payload["display_name"],
            style_ids=tuple(payload["style_ids"]),
            style_files=tuple(payload["style_files"]),
        )
        for payload in (unit_by_id[unit_id] for unit_id in ordered_ids)
    ]


def load_ledger() -> dict[str, Any]:
    return load_json(LEDGER_FILE, {"version": 1, "generated_at": None, "projects": {}})


def project_guidelines(ledger: dict[str, Any], project: str) -> dict[str, Any]:
    projects = ledger.setdefault("projects", {})
    project_state = projects.setdefault(project, {"guidelines": {}})
    return project_state.setdefault("guidelines", {})


def parse_eval_findings(path: Path, project: str) -> dict[str, set[str]]:
    if not path.exists():
        return {}

    findings_by_unit: dict[str, set[str]] = defaultdict(set)
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
            style_id = normalize_style_path(raw_path, project)
            findings_by_unit[style_id].add(current_title)
    return findings_by_unit


def bootstrap_projects(project_roots: list[Path]) -> None:
    ledger = load_ledger()
    timestamp_sets: dict[tuple[str, str], set[str]] = defaultdict(set)

    if STYLE_LOG_FILE.exists():
        for raw_line in STYLE_LOG_FILE.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if "status" in entry:
                continue
            key = (entry["project"], entry["style_id"])
            timestamp_sets[key].add(entry["timestamp"])

    for project_root in project_roots:
        project = project_root.name.removesuffix("_style_fix")
        units = build_units(project_root)
        guidelines = project_guidelines(ledger, project)

        unit_counts: dict[str, int] = defaultdict(int)
        unit_last_seen: dict[str, str] = {}
        style_to_unit = {
            style_id: unit.guideline_id
            for unit in units
            for style_id in unit.style_ids
        }

        for (entry_project, style_id), timestamps in timestamp_sets.items():
            if entry_project != project:
                continue
            unit_id = style_to_unit.get(style_id)
            if unit_id is None:
                continue
            count = len(timestamps)
            if count > unit_counts[unit_id]:
                unit_counts[unit_id] = count
            unit_last_seen[unit_id] = max(timestamps)

        for unit in units:
            guidelines[unit.guideline_id] = {
                "review_count": unit_counts.get(unit.guideline_id, 0),
                "last_reviewed_at": unit_last_seen.get(unit.guideline_id),
                "guideline_kind": unit.guideline_kind,
                "budget_kind": unit.budget_kind,
            }

    ledger["generated_at"] = utc_now()
    save_json(LEDGER_FILE, ledger)


def select_units(project_root: Path, budget: int, output_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    ledger = load_ledger()
    if project not in ledger.get("projects", {}):
        bootstrap_projects([project_root])
        ledger = load_ledger()

    units = build_units(project_root)
    guidelines = project_guidelines(ledger, project)
    selected: list[dict[str, Any]] = []
    spent_budget = 0

    sortable: list[tuple[int, int, Unit]] = []
    for unit in units:
        review_count = guidelines.get(unit.guideline_id, {}).get("review_count", 0)
        sortable.append((review_count, unit.checklist_index, unit))

    sortable.sort(key=lambda item: (item[0], item[1], item[2].guideline_id))

    for review_count, _, unit in sortable:
        budget_cost = 0 if unit.budget_kind == "non_negotiable" else 1
        if budget_cost and spent_budget >= budget:
            break
        selected.append(
            {
                "guideline_id": unit.guideline_id,
                "guideline_kind": unit.guideline_kind,
                "budget_kind": unit.budget_kind,
                "budget_cost": budget_cost,
                "checklist_index": unit.checklist_index,
                "display_name": unit.display_name,
                "style_ids": list(unit.style_ids),
                "style_files": list(unit.style_files),
                "review_count_before": review_count,
                "review_count_after": review_count + 1,
            }
        )
        spent_budget += budget_cost

    payload = {
        "generated_at": utc_now(),
        "project": project,
        "budget": budget,
        "spent_budget": spent_budget,
        "selected_units": selected,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_events(
    project_root: Path,
    manifest_path: Path,
    prior_eval_path: Path,
    current_eval_path: Path,
    events_path: Path | None,
) -> None:
    project = project_root.name.removesuffix("_style_fix")
    manifest = load_json(manifest_path, {})
    selected_units = manifest.get("selected_units", [])
    ledger = load_ledger()
    guidelines = project_guidelines(ledger, project)

    current_findings = parse_eval_findings(current_eval_path, project)
    prior_findings = parse_eval_findings(prior_eval_path, project)

    event_file = events_path or EVENTS_FILE
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a") as handle:
        for selected in selected_units:
            unit_style_ids = selected["style_ids"]
            current_titles: set[str] = set()
            prior_titles: set[str] = set()
            for style_id in unit_style_ids:
                current_titles.update(current_findings.get(style_id, set()))
                prior_titles.update(prior_findings.get(style_id, set()))

            if current_titles:
                produced_finding = True
                finding_source = (
                    "carried_forward"
                    if current_titles and current_titles.issubset(prior_titles)
                    else "new"
                )
            else:
                produced_finding = False
                finding_source = "none"

            guidelines[selected["guideline_id"]] = {
                "review_count": selected["review_count_after"],
                "last_reviewed_at": utc_now(),
                "guideline_kind": selected["guideline_kind"],
                "budget_kind": selected["budget_kind"],
            }

            event = {
                "timestamp": utc_now(),
                "project": project,
                "guideline_id": selected["guideline_id"],
                "guideline_kind": selected["guideline_kind"],
                "budget_kind": selected["budget_kind"],
                "review_count_after": selected["review_count_after"],
                "produced_finding": produced_finding,
                "finding_source": finding_source,
            }
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    ledger["generated_at"] = utc_now()
    save_json(LEDGER_FILE, ledger)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--project-root", action="append", default=[])

    select = subparsers.add_parser("select")
    select.add_argument("--project-root", required=True)
    select.add_argument("--budget", required=True, type=int)
    select.add_argument("--output", required=True)

    append = subparsers.add_parser("append-events")
    append.add_argument("--project-root", required=True)
    append.add_argument("--manifest", required=True)
    append.add_argument("--prior-eval", required=True)
    append.add_argument("--current-eval", required=True)
    append.add_argument("--events-file")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "bootstrap":
        if args.project_root:
            roots = [Path(path).expanduser().resolve() for path in args.project_root]
        else:
            roots = sorted(
                path
                for path in RUST_DIR.iterdir()
                if path.is_dir()
                and not path.name.endswith("_style_fix")
                and (path / "Cargo.toml").exists()
                and not (path / ".git").is_file()
            )
        bootstrap_projects(roots)
        return

    if args.command == "select":
        select_units(
            Path(args.project_root).expanduser().resolve(),
            args.budget,
            Path(args.output).expanduser().resolve(),
        )
        return

    append_events(
        Path(args.project_root).expanduser().resolve(),
        Path(args.manifest).expanduser().resolve(),
        Path(args.prior_eval).expanduser().resolve(),
        Path(args.current_eval).expanduser().resolve(),
        Path(args.events_file).expanduser().resolve() if args.events_file else None,
    )


if __name__ == "__main__":
    main()
