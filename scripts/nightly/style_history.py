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
from typing import TypedDict
from typing import cast

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


class Outcome(TypedDict, total=False):
    status: str
    reason: str
    summary: str
    finding_source: str
    skipped_by: str


class ReviewedUnit(TypedDict, total=False):
    guideline_id: str
    outcome: Outcome
    finding_source: str


class PendingState(TypedDict, total=False):
    budget: int
    phase: str
    reviewed_unit_ids: list[str]
    reviewed_units: list[ReviewedUnit]
    scored_count: int
    start_time: str
    updated_at: str
    last_unit_id: str
    last_unit_result: str


class ResultEntry(TypedDict, total=False):
    guideline_id: str
    outcome: Outcome
    finding_source: str


class ResultPayload(TypedDict, total=False):
    unit_id: str
    results: list[ResultEntry]


class HistoryRow(TypedDict, total=False):
    start_time: str
    end_time: str
    reviewed_units: list[ReviewedUnit]


@dataclass(frozen=True)
class Unit:
    unit_id: str
    budget_cost: int
    checklist_index: int
    display_name: str
    guideline_ids: tuple[str, ...]
    see_also_guideline_ids: tuple[str, ...]
    pre_filter: str = ""


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


def _extract_wikilink(raw: str) -> str:
    value = raw.strip()
    if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) >= 2:
        value = value[1:-1]
    if value.startswith("[[") and value.endswith("]]"):
        return value[2:-2].strip()
    return ""


def parse_frontmatter(path: Path) -> dict[str, list[str]]:
    tags: list[str] = []
    see_also: list[str] = []
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return {"tags": tags, "see_also": see_also}
    current_key = ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped:
            continue
        if stripped.startswith("-") and current_key:
            value = stripped.lstrip("-").strip()
            if current_key == "tags":
                tags.append(value)
            elif current_key == "see_also":
                extracted = _extract_wikilink(value)
                if extracted:
                    see_also.append(extracted)
            continue
        if ":" not in stripped:
            current_key = ""
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "tags":
            current_key = "tags" if not value else ""
        elif key == "see_also":
            if value:
                extracted = _extract_wikilink(value)
                if extracted:
                    see_also.append(extracted)
                current_key = ""
            else:
                current_key = "see_also"
        else:
            current_key = ""
    return {"tags": tags, "see_also": see_also}


def extract_title(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.name


def read_pre_filter(path: Path) -> str:
    """Extract a top-level scalar `pre_filter:` field from the frontmatter.

    Returns the regex string, or empty string when absent. Strips surrounding
    quotes (single or double) so the value can be passed directly to ripgrep.
    """
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped.startswith("pre_filter:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        return value
    return ""


def pre_filter_has_candidates(pattern: str, project_root: Path) -> bool:
    """Run `rg` against project_root with the pattern. Return True if any match."""
    if not pattern:
        return True
    try:
        result = subprocess.run(
            ["rg", "--quiet", "--type", "rust", pattern, str(project_root)],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return True
    return result.returncode == 0


def auto_record_pre_filter_skip(project: str, unit: "Unit") -> None:
    """Record `no_findings` for a unit whose pre_filter found zero candidate sites."""
    pending = load_pending(project)
    if not pending:
        return
    reviewed_units: list[ReviewedUnit] = list(pending.get("reviewed_units", []))
    reviewed_unit_ids: list[str] = list(pending.get("reviewed_unit_ids", []))
    if unit.unit_id in reviewed_unit_ids:
        return
    for guideline_id in unit.guideline_ids:
        reviewed_units.append(
            {
                "guideline_id": guideline_id,
                "outcome": {"status": "no_findings", "skipped_by": "pre_filter"},
            }
        )
    reviewed_unit_ids.append(unit.unit_id)
    pending["reviewed_units"] = reviewed_units
    pending["reviewed_unit_ids"] = reviewed_unit_ids
    pending["last_unit_id"] = unit.unit_id
    pending["last_unit_result"] = "no_findings"
    pending["updated_at"] = utc_now()
    write_pending(project, pending)


def history_file(project: str) -> Path:
    return HISTORY_DIR / f"{project}.jsonl"


def pending_file(project: str) -> Path:
    return PENDING_DIR / f"{project}.json"


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


@dataclass(frozen=True)
class WorkspaceMember:
    workspace_dir: str
    member_subpath: str
    package_name: str


def workspace_members() -> dict[str, WorkspaceMember]:
    if not NIGHTLY_CONF_FILE.exists():
        return {}
    members: dict[str, WorkspaceMember] = {}
    current_section = ""
    for raw_line in NIGHTLY_CONF_FILE.read_text().splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            continue
        if current_section != "workspace_members" or "=" not in stripped:
            continue
        name, _, rhs = stripped.partition("=")
        name = name.strip()
        rhs = rhs.strip()
        if not name or not rhs:
            continue
        path_part, _, pkg_part = rhs.partition(":")
        path_part = path_part.strip().strip("/")
        pkg_part = pkg_part.strip()
        if "/" not in path_part:
            continue
        ws_dir, _, subpath = path_part.partition("/")
        if not ws_dir or not subpath:
            continue
        package_name = pkg_part if pkg_part else Path(subpath).name
        members[name] = WorkspaceMember(
            workspace_dir=ws_dir,
            member_subpath=subpath,
            package_name=package_name,
        )
    return members


def resolve_project_root(project_name: str) -> Path | None:
    members = workspace_members()
    if project_name in members:
        m = members[project_name]
        path = RUST_DIR / m.workspace_dir / m.member_subpath
        return path if path.exists() else None
    default = RUST_DIR / project_name
    return default if default.exists() else None


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


def append_jsonl_history(path: Path, payload: HistoryRow) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        _ = handle.write(json.dumps(payload, sort_keys=True) + "\n")


def load_history(project: str) -> list[HistoryRow]:
    path = history_file(project)
    if not path.exists():
        return []
    rows: list[HistoryRow] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed: object = json.loads(line)  # pyright: ignore[reportAny]
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(cast(HistoryRow, cast(object, parsed)))
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
    all_files = list_style_files(project_root)
    return _build_units_from_files(all_files, project_root, all_files)


def _build_units_from_files(
    style_files: list[Path],
    project_root: Path,
    resolution_corpus: list[Path],
) -> list[Unit]:
    # Resolve see_also wikilink stems against the full style-file population,
    # not just the subset being built into units. This matters for focused evals
    # where only a handful of guidelines are requested but their see_alsos may
    # point anywhere in the guide.
    stem_to_guideline_id: dict[str, str] = {
        style_file.stem: normalize_guideline_id(str(style_file), project_root)
        for style_file in resolution_corpus
    }
    units: list[Unit] = []
    for index, style_file in enumerate(style_files, start=1):
        frontmatter = parse_frontmatter(style_file)
        tags = set(frontmatter["tags"])
        see_also_stems = list(frontmatter["see_also"])
        guideline_id = normalize_guideline_id(str(style_file), project_root)
        see_also_ids: list[str] = []
        for stem in see_also_stems:
            resolved = stem_to_guideline_id.get(stem)
            if resolved and resolved not in see_also_ids:
                see_also_ids.append(resolved)
        units.append(
            Unit(
                unit_id=guideline_id,
                budget_cost=0 if "non-negotiable" in tags else 1,
                checklist_index=index,
                display_name=extract_title(style_file),
                guideline_ids=(guideline_id,),
                see_also_guideline_ids=tuple(see_also_ids),
                pre_filter=read_pre_filter(style_file),
            )
        )
    return units


def resolve_focus_targets(requested: list[str], project_root: Path) -> list[Path]:
    """Resolve user-supplied guideline identifiers to absolute style-file paths.

    Accepts any mix of:
      - bare stems (`when-to-split-a-module`)
      - filenames (`when-to-split-a-module.md`)
      - guideline IDs (`rust/when-to-split-a-module.md`)
      - absolute paths
    Raises SystemExit with the first unresolvable entry so the caller can fix input.
    """
    style_files = list_style_files(project_root)
    by_stem: dict[str, Path] = {p.stem: p for p in style_files}
    by_name: dict[str, Path] = {p.name: p for p in style_files}
    by_guideline_id: dict[str, Path] = {
        normalize_guideline_id(str(p), project_root): p for p in style_files
    }
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in requested:
        value = raw.strip()
        if not value:
            continue
        candidates: list[Path | None] = [
            by_guideline_id.get(value),
            by_name.get(value),
            by_stem.get(value),
        ]
        if value.endswith(".md"):
            candidates.append(by_stem.get(value[:-3]))
        else:
            candidates.append(by_name.get(f"{value}.md"))
        absolute = Path(value).expanduser()
        if absolute.is_absolute() and absolute.exists():
            candidates.append(absolute)
        path = next((c for c in candidates if c is not None), None)
        if path is None:
            raise SystemExit(f"Could not resolve guideline: {raw}")
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def focused_units(project_root: Path, requested: list[str]) -> list[Unit]:
    """Build units for an explicit subset of guidelines — read-only, no state writes."""
    targets = resolve_focus_targets(requested, project_root)
    all_files = list_style_files(project_root)
    return _build_units_from_files(targets, project_root, all_files)


def review_counts(project_root: Path) -> dict[str, int]:
    project = project_root.name.removesuffix("_style_fix")
    counts: dict[str, int] = defaultdict(int)
    for row in load_history(project):
        for reviewed in row.get("reviewed_units", []):
            guideline_id = reviewed.get("guideline_id")
            if isinstance(guideline_id, str):
                counts[guideline_id] += 1
    return counts


def cross_project_hit_rates() -> dict[str, float]:
    """Per-guideline finding rate across every project's .history JSONL.

    Returns a {guideline_id: hit_rate} map where hit_rate ∈ [0.0, 1.0] is
    findings_count / reviews_count. Used by `next_unit` to walk high-yield
    guidelines first so dirty projects hit the budget cap and exit early
    instead of plowing through guidelines that never produce findings.

    Pre-filter skips (`outcome.skipped_by == "pre_filter"`) are excluded from
    both numerator and denominator — they aren't real LLM reviews.
    """
    reviews: dict[str, int] = defaultdict(int)
    findings: dict[str, int] = defaultdict(int)
    if not HISTORY_DIR.exists():
        return {}
    for jsonl_path in HISTORY_DIR.glob("*.jsonl"):
        try:
            text = jsonl_path.read_text()
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed: object = json.loads(line)  # pyright: ignore[reportAny]
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            row = cast(HistoryRow, cast(object, parsed))
            for reviewed in row.get("reviewed_units", []):
                guideline_id = reviewed.get("guideline_id")
                if not isinstance(guideline_id, str):
                    continue
                outcome: Outcome = reviewed.get("outcome", {})
                if outcome.get("skipped_by") == "pre_filter":
                    continue
                reviews[guideline_id] += 1
                if reviewed.get("finding_source") or outcome.get("status") not in (None, "no_findings"):
                    findings[guideline_id] += 1
    return {
        guideline_id: findings.get(guideline_id, 0) / count
        for guideline_id, count in reviews.items()
        if count > 0
    }


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
    payload: PendingState = {
        "budget": budget,
        "phase": "evaluation",
        "reviewed_unit_ids": [],
        "reviewed_units": [],
        "scored_count": 0,
        "start_time": utc_now(),
        "updated_at": utc_now(),
    }
    write_pending(project, payload)


def load_pending(project: str) -> PendingState:
    path = pending_file(project)
    empty: PendingState = {}
    if not path.exists():
        return empty
    parsed: object = json.loads(path.read_text())  # pyright: ignore[reportAny]
    if isinstance(parsed, dict):
        return cast(PendingState, cast(object, parsed))
    return empty


def write_pending(project: str, payload: PendingState) -> None:
    path = pending_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def set_phase(project: str, phase: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    pending["phase"] = phase
    pending["updated_at"] = utc_now()
    write_pending(project, pending)


def next_unit(project_root: Path) -> dict[str, object]:
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

    seen_unit_ids: set[str] = set(pending.get("reviewed_unit_ids", []))
    units = build_units(project_root)
    counts = review_counts(project_root)
    # Cross-project hit rates — high-yield guidelines walk first so dirty
    # projects hit the budget cap and exit early instead of plowing through
    # guidelines that never produce findings.
    hit_rates = cross_project_hit_rates()
    sortable: list[tuple[float, int, int, str, Unit]] = []
    for unit in units:
        if unit.budget_cost == 0:
            continue
        if unit.unit_id in seen_unit_ids:
            continue
        unit_count = min(counts.get(guideline_id, 0) for guideline_id in unit.guideline_ids) if unit.guideline_ids else 0
        # Negate hit-rate so higher rates sort earlier under ascending sort.
        # Unseen guidelines (no history yet) get rate 0.0 → sort to the end.
        unit_hit_rate = max((hit_rates.get(guideline_id, 0.0) for guideline_id in unit.guideline_ids), default=0.0)
        sortable.append((-unit_hit_rate, unit_count, unit.checklist_index, unit.unit_id, unit))
    sortable.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    # Walk candidates in sort order. For each, run its pre_filter (if any) against
    # the project tree. If the pattern finds zero matches, the violation cannot be
    # present — auto-record `no_findings` for the unit and continue to the next
    # candidate without invoking the LLM.
    while sortable:
        _, unit_count, _, _, unit = sortable[0]
        if unit.pre_filter and not pre_filter_has_candidates(unit.pre_filter, project_root):
            auto_record_pre_filter_skip(project, unit)
            _ = sortable.pop(0)
            continue
        break
    else:
        return {
            "budget": budget,
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "scored_count": scored_count,
            "status": "complete",
            "stop_reason": "exhausted",
        }

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
            "see_also_guideline_ids": list(unit.see_also_guideline_ids),
            "unit_id": unit.unit_id,
        },
    }


def record_unit(project_root: Path, results_path: Path, eval_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        raise SystemExit(f"No pending run for {project}. Start a run first.")

    raw_payload: object = json.loads(results_path.read_text())  # pyright: ignore[reportAny]
    if not isinstance(raw_payload, dict):
        raise SystemExit("Results file must contain a JSON object with unit_id and results.")
    payload = cast(ResultPayload, cast(object, raw_payload))
    unit_id = payload.get("unit_id")
    results: list[ResultEntry] = list(payload.get("results", []))
    if not isinstance(unit_id, str):
        raise SystemExit("Results file must contain unit_id and results.")

    reviewed_units: list[ReviewedUnit] = list(pending.get("reviewed_units", []))
    reviewed_unit_ids: list[str] = list(pending.get("reviewed_unit_ids", []))
    if unit_id in reviewed_unit_ids:
        raise SystemExit(f"Unit already recorded in this run: {unit_id}")

    eval_guidelines: set[str] = (
        set(parse_eval_guidelines(eval_path, project_root).values())
        if eval_path.exists()
        else set()
    )

    scored_increment = 0
    found_countable = False
    for result in results:
        guideline_id = result.get("guideline_id")
        if not isinstance(guideline_id, str):
            continue
        reviewed_entry: ReviewedUnit = {"guideline_id": guideline_id}
        outcome = result.get("outcome")
        is_finding = False
        if isinstance(outcome, dict):
            reviewed_entry["outcome"] = outcome
            status = outcome.get("status")
            if status != "no_findings":
                found_countable = True
            if status in {"finding", "fixed", "partial", "skipped"} or outcome.get("finding_source") in {"new", "carried_forward"}:
                is_finding = True
        else:
            finding_source = result.get("finding_source")
            if isinstance(finding_source, str):
                reviewed_entry["finding_source"] = finding_source
                found_countable = True
                is_finding = finding_source in {"new", "carried_forward"}
            else:
                raise SystemExit(f"Result for {guideline_id} must include outcome or finding_source.")
        if is_finding and guideline_id not in eval_guidelines:
            message = (
                f"Refusing to record finding for {guideline_id}: not present under "
                + f"'## Improvements' in {eval_path}. Append the finding to "
                + "EVALUATION.md before calling record-unit."
            )
            raise SystemExit(message)
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


def parse_fix_results(eval_path: Path, project_root: Path) -> dict[str, Outcome]:
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
    aggregated: dict[str, Outcome] = {}
    for guideline_id, entries in per_guideline.items():
        statuses: list[str] = []
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
        outcome: Outcome = {"status": final_status}
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
    row: HistoryRow = {
        "start_time": pending.get("start_time", ""),
        "end_time": utc_now(),
        "reviewed_units": list(pending.get("reviewed_units", [])),
    }
    append_jsonl_history(history_file(project), row)
    pending_file(project).unlink(missing_ok=True)


def finalize_fix(project_root: Path, eval_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        return
    fix_results = parse_fix_results(eval_path, project_root)
    eval_guidelines: set[str] = set(parse_eval_guidelines(eval_path, project_root).values())
    reviewed_units: list[ReviewedUnit] = []
    for reviewed in pending.get("reviewed_units", []):
        guideline_id = reviewed.get("guideline_id", "")
        if "outcome" in reviewed:
            reviewed_units.append(reviewed)
            continue
        if guideline_id in fix_results:
            outcome: Outcome = cast(Outcome, cast(object, dict(fix_results[guideline_id])))
        elif guideline_id in eval_guidelines:
            outcome = {
                "status": "fix_failed",
                "reason": "Finding present in EVALUATION.md ## Improvements but no matching ## Fix Summary entry.",
            }
        else:
            outcome = {
                "status": "eval_dropped",
                "reason": "Recorded as a finding via record-unit but absent from EVALUATION.md ## Improvements.",
            }
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append({"guideline_id": guideline_id, "outcome": outcome})
    row: HistoryRow = {
        "start_time": pending.get("start_time", ""),
        "end_time": utc_now(),
        "reviewed_units": reviewed_units,
    }
    append_jsonl_history(history_file(project), row)
    pending_file(project).unlink(missing_ok=True)


def finalize_failure(project: str, reason: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    reviewed_units: list[ReviewedUnit] = []
    for reviewed in pending.get("reviewed_units", []):
        if "outcome" in reviewed:
            reviewed_units.append(reviewed)
            continue
        outcome: Outcome = {"status": "fix_failed", "reason": reason}
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append(
            {"guideline_id": reviewed.get("guideline_id", ""), "outcome": outcome}
        )
    row: HistoryRow = {
        "start_time": pending.get("start_time", ""),
        "end_time": utc_now(),
        "reviewed_units": reviewed_units,
    }
    append_jsonl_history(history_file(project), row)
    pending_file(project).unlink(missing_ok=True)


def discard_pending(project: str) -> None:
    pending_file(project).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start-run")
    _ = start.add_argument("--project-root", required=True)
    _ = start.add_argument("--budget", required=True, type=int)
    next_parser = subparsers.add_parser("next-unit")
    _ = next_parser.add_argument("--project-root", required=True)
    record = subparsers.add_parser("record-unit")
    _ = record.add_argument("--project-root", required=True)
    _ = record.add_argument("--results", required=True)
    _ = record.add_argument(
        "--eval-path",
        required=True,
        help="Path to EVALUATION.md. record-unit refuses to record a finding whose guideline is not present under ## Improvements.",
    )
    no_findings = subparsers.add_parser("finalize-no-findings")
    _ = no_findings.add_argument("--project", required=True)
    finalize = subparsers.add_parser("finalize-fix")
    _ = finalize.add_argument("--project-root", required=True)
    _ = finalize.add_argument("--evaluation", required=True)
    failure = subparsers.add_parser("finalize-failure")
    _ = failure.add_argument("--project", required=True)
    _ = failure.add_argument("--reason", required=True)
    discard = subparsers.add_parser("discard-pending")
    _ = discard.add_argument("--project", required=True)
    phase = subparsers.add_parser("set-phase")
    _ = phase.add_argument("--project", required=True)
    _ = phase.add_argument("--phase", required=True)
    focused = subparsers.add_parser(
        "focused-eval",
        help="Read-only: emit one JSON unit per requested guideline (no pending/history state).",
    )
    _ = focused.add_argument("--project-root", required=True)
    _ = focused.add_argument(
        "--guideline",
        action="append",
        required=True,
        help="Guideline to include. Accepts stem, filename, or guideline id. Repeatable.",
    )
    return parser.parse_args()


def _arg_str(args: argparse.Namespace, name: str) -> str:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    if not isinstance(value, str):
        raise SystemExit(f"Argument --{name.replace('_', '-')} must be a string.")
    return value


def _arg_int(args: argparse.Namespace, name: str) -> int:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    if not isinstance(value, int):
        raise SystemExit(f"Argument --{name.replace('_', '-')} must be an int.")
    return value


def _arg_str_list(args: argparse.Namespace, name: str) -> list[str]:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    if not isinstance(value, list):
        raise SystemExit(f"Argument --{name.replace('_', '-')} must be a list.")
    items: list[str] = []
    for entry in cast(list[object], value):
        if not isinstance(entry, str):
            raise SystemExit(f"Argument --{name.replace('_', '-')} must contain strings.")
        items.append(entry)
    return items


def main() -> None:
    args = parse_args()
    command = _arg_str(args, "command")
    if command == "start-run":
        start_run(Path(_arg_str(args, "project_root")).expanduser().resolve(), _arg_int(args, "budget"))
        return
    if command == "next-unit":
        print(json.dumps(next_unit(Path(_arg_str(args, "project_root")).expanduser().resolve()), indent=2, sort_keys=True))
        return
    if command == "record-unit":
        record_unit(
            Path(_arg_str(args, "project_root")).expanduser().resolve(),
            Path(_arg_str(args, "results")).expanduser().resolve(),
            Path(_arg_str(args, "eval_path")).expanduser().resolve(),
        )
        return
    if command == "finalize-no-findings":
        finalize_no_findings(_arg_str(args, "project"))
        return
    if command == "finalize-fix":
        finalize_fix(
            Path(_arg_str(args, "project_root")).expanduser().resolve(),
            Path(_arg_str(args, "evaluation")).expanduser().resolve(),
        )
        return
    if command == "discard-pending":
        discard_pending(_arg_str(args, "project"))
        return
    if command == "set-phase":
        set_phase(_arg_str(args, "project"), _arg_str(args, "phase"))
        return
    if command == "focused-eval":
        project_root = Path(_arg_str(args, "project_root")).expanduser().resolve()
        units = focused_units(project_root, _arg_str_list(args, "guideline"))
        for unit in units:
            print(json.dumps(
                {
                    "unit_id": unit.unit_id,
                    "display_name": unit.display_name,
                    "guideline_ids": list(unit.guideline_ids),
                    "see_also_guideline_ids": list(unit.see_also_guideline_ids),
                },
                sort_keys=True,
            ))
        return
    finalize_failure(_arg_str(args, "project"), _arg_str(args, "reason"))


if __name__ == "__main__":
    main()
