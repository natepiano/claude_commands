#!/usr/bin/env python3
"""Per-project clean-fix style history helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import NamedTuple
from typing import TypedDict
from typing import cast

from candidate_generators import CandidatesSpec, Enumeration, enumerate_candidates, read_candidates_spec  # pyright: ignore[reportImplicitRelativeImport]  # run standalone, not as a package — relative import would break it

RUST_DIR = Path(os.environ.get("STYLE_HISTORY_RUST_DIR", str(Path.home() / "rust")))
NATE_STYLE_DIR = Path(os.environ.get("STYLE_HISTORY_NATE_STYLE_DIR", str(RUST_DIR / "nate_style")))
HISTORY_DIR = NATE_STYLE_DIR / ".history"
PENDING_DIR = HISTORY_DIR / ".pending"
LOAD_STYLE_SCRIPT = Path(
    os.environ.get(
        "STYLE_HISTORY_LOAD_STYLE_SCRIPT",
        str(Path.home() / ".claude" / "scripts" / "rust_style" / "load-rust-style.sh"),
    )
)
CLEAN_FIX_CONF_FILE = Path(
    os.environ.get(
        "STYLE_HISTORY_CONF_FILE",
        str(Path.home() / ".claude" / "scripts" / "clean-fix" / "clean-fix.conf"),
    )
)
LOG_DIR = Path(os.environ.get("STYLE_HISTORY_LOG_DIR", "/private/tmp/claude"))

# Stop reasons that mean the helper itself declared the run finished. A pending
# whose stop_reason is anything else was abandoned mid-run (agent quit early,
# timeout kill, crash) — it must never be finalized into a history row, only
# resumed.
COMPLETED_STOP_REASONS = frozenset({"budget_reached", "exhausted", "quota_reached"})

# Exit code for "refusing to finalize an incomplete run" so shell callers can
# distinguish resume-and-relaunch from a real failure. argparse uses 2.
EXIT_INCOMPLETE_RUN = 3

# skipped_by values that mean the helper disposed the unit without an LLM call:
# a pre_filter regex with zero matches, or a candidate generator that
# enumerated zero sites. Free for quota purposes and excluded from hit rates.
FREE_SKIP_SOURCES = frozenset({"pre_filter", "candidates"})


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
    checked_unit_count: int
    evaluation_markdown: str
    evaluation_complete: bool
    evaluation_summary: dict[str, object]
    evaluation_updated_at: str
    failure_finalized_at: str
    failure_history_recorded_at: str
    failure_reason: str
    finding_count: int
    fix_finalized_at: str
    fix_history_recorded_at: str
    guideline_total: int
    phase: str
    project_root: str
    reviewable_unit_total: int
    reviewed_unit_ids: list[str]
    reviewed_units: list[ReviewedUnit]
    scratch_exports: dict[str, dict[str, str]]
    scored_count: int
    start_time: str
    stop_reason: str
    updated_at: str
    last_unit_id: str
    last_unit_result: str


class ResultEntry(TypedDict, total=False):
    guideline_id: str
    outcome: Outcome
    finding_source: str


class DispositionEntry(TypedDict, total=False):
    index: int
    verdict: str  # "violation" | "exception"
    clause: str  # required for "exception": which exception clause applies


class ResultPayload(TypedDict, total=False):
    unit_id: str
    results: list[ResultEntry]
    dispositions: list[DispositionEntry]


class HistoryRow(TypedDict, total=False):
    start_time: str
    end_time: str
    reviewed_units: list[ReviewedUnit]
    evaluation_summary: dict[str, object]
    fingerprint: str


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


def auto_record_pre_filter_skip(project: str, unit: "Unit", skipped_by: str = "pre_filter") -> None:
    """Record `no_findings` for a unit the helper disposed without an LLM call.

    `skipped_by` is `pre_filter` (skip-gate regex found zero matches) or
    `candidates` (the unit's candidate generator enumerated zero sites).
    """
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
                "outcome": {"status": "no_findings", "skipped_by": skipped_by},
            }
        )
    reviewed_unit_ids.append(unit.unit_id)
    pending["reviewed_units"] = reviewed_units
    pending["reviewed_unit_ids"] = reviewed_unit_ids
    pending["last_unit_id"] = unit.unit_id
    pending["last_unit_result"] = "no_findings"
    pending["updated_at"] = utc_now()
    _ = refresh_evaluation_summary(pending)
    write_pending(project, pending)


def history_file(project: str) -> Path:
    return HISTORY_DIR / f"{project}.jsonl"


def pending_file(project: str) -> Path:
    return PENDING_DIR / f"{project}.json"


def remove_pending(project: str) -> None:
    """Remove a project's pending file and its heartbeat flock file together.

    The `.json.lock` sibling is created by style-eval-heartbeat.sh; leaving it
    behind after the pending file is finalized litters `.pending/` with
    zero-byte locks forever. A heartbeat that re-creates the lock after this
    runs re-checks `pending_file.exists()` under the lock and exits without
    writing, so deleting both here is safe.
    """
    path = pending_file(project)
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)


def _style_eval_conf_int(conf_key: str) -> int:
    """Read an int key from `[style_eval]` in `clean-fix.conf`.

    Single source of truth for eval tunables. Errors loudly if missing — both
    the clean-fix script and the ad-hoc agent path depend on these values, so
    silent defaulting would let arbitrary numbers leak in (the bug that
    motivated centralizing max_new_findings).
    """
    if not CLEAN_FIX_CONF_FILE.exists():
        raise SystemExit(
            f"clean-fix.conf not found at {CLEAN_FIX_CONF_FILE}; [style_eval] {conf_key} must be set there."
        )
    current_section = ""
    for raw_line in CLEAN_FIX_CONF_FILE.read_text().splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            continue
        if current_section != "style_eval" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == conf_key:
            try:
                return int(value.strip())
            except ValueError as exc:
                raise SystemExit(
                    f"[style_eval] {conf_key} in {CLEAN_FIX_CONF_FILE} is not an int: {value.strip()!r}"
                ) from exc
    raise SystemExit(
        f"[style_eval] {conf_key} is not set in {CLEAN_FIX_CONF_FILE}"
    )


def max_new_findings() -> int:
    """Per-run numbered-finding cap (`budget_reached` stop)."""
    return _style_eval_conf_int("max_new_findings")


def eval_unit_quota() -> int:
    """Max LLM-reviewed units per run (`quota_reached` stop). Pre-filter skips are free."""
    return _style_eval_conf_int("eval_unit_quota")


def eval_ttl_days() -> int:
    """Days a recorded unit verdict suppresses re-review (see `unit_is_due`)."""
    return _style_eval_conf_int("eval_ttl_days")


def count_findings_in_markdown(markdown: str) -> int:
    return sum(
        1
        for line in markdown.splitlines()
        if line.startswith("### ") and len(line) > 4 and line[4].isdigit()
    )


def has_no_violations_marker(markdown: str) -> bool:
    return any(line.strip() == "## No violations found" for line in markdown.splitlines())


def has_fix_summary_marker(markdown: str) -> bool:
    return any(line.strip() == "## Fix Summary" for line in markdown.splitlines())


def excluded_projects() -> set[str]:
    if not CLEAN_FIX_CONF_FILE.exists():
        return set()
    excluded: set[str] = set()
    current_section = ""
    for raw_line in CLEAN_FIX_CONF_FILE.read_text().splitlines():
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
    """Workspace-member targets from the ``[targets]`` allowlist.

    A member line is any ``[targets]`` entry containing a ``/`` — the part
    before the first slash is the workspace directory, the rest is the member
    subpath. The history key, package name, and ``_style_fix`` dir name all come
    from the last path segment (e.g. ``bevy_diegetic``).
    """
    if not CLEAN_FIX_CONF_FILE.exists():
        return {}
    members: dict[str, WorkspaceMember] = {}
    current_section = ""
    for raw_line in CLEAN_FIX_CONF_FILE.read_text().splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            continue
        if current_section != "targets" or "/" not in stripped:
            continue
        path_part = stripped.strip("/")
        ws_dir, _, subpath = path_part.partition("/")
        if not ws_dir or not subpath:
            continue
        name = Path(subpath).name
        members[name] = WorkspaceMember(
            workspace_dir=ws_dir,
            member_subpath=subpath,
            package_name=name,
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


def unit_totals(project_root: Path) -> dict[str, int]:
    units = build_units(project_root)
    reviewable = [unit for unit in units if unit.budget_cost > 0]
    return {
        "guideline_total": len(units),
        "reviewable_unit_total": len(reviewable),
        "non_negotiable_unit_total": len(units) - len(reviewable),
    }


def refresh_evaluation_summary(
    pending: PendingState,
    project_root: Path | None = None,
    stop_reason: str | None = None,
) -> dict[str, object]:
    if project_root is not None:
        totals = unit_totals(project_root)
        pending["project_root"] = str(project_root)
        pending["guideline_total"] = totals["guideline_total"]
        pending["reviewable_unit_total"] = totals["reviewable_unit_total"]

    if stop_reason is not None:
        pending["stop_reason"] = stop_reason

    markdown = pending.get("evaluation_markdown", "")
    finding_count = count_findings_in_markdown(markdown)
    checked_unit_count = len(pending.get("reviewed_unit_ids", []))
    reviewable_unit_total = int(pending.get("reviewable_unit_total", 0) or 0)
    stop = pending.get("stop_reason", "")
    evaluation_complete = bool(stop == "exhausted")

    pending["checked_unit_count"] = checked_unit_count
    pending["finding_count"] = finding_count
    pending["scored_count"] = finding_count
    pending["evaluation_complete"] = evaluation_complete

    summary: dict[str, object] = {
        "budget": pending.get("budget", max_new_findings()),
        "checked_unit_count": checked_unit_count,
        "evaluation_complete": evaluation_complete,
        "finding_count": finding_count,
        "guideline_total": pending.get("guideline_total", 0),
        "reviewable_unit_total": reviewable_unit_total,
        "stop_reason": stop,
    }
    if reviewable_unit_total:
        summary["coverage"] = f"{checked_unit_count}/{reviewable_unit_total}"
    pending["evaluation_summary"] = summary
    return summary


def history_summary(pending: PendingState) -> dict[str, object]:
    summary = dict(pending.get("evaluation_summary", {}))
    if not summary:
        summary = refresh_evaluation_summary(pending)
    return summary


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
                if outcome.get("skipped_by") in FREE_SKIP_SOURCES:
                    continue
                reviews[guideline_id] += 1
                if reviewed.get("finding_source") or outcome.get("status") not in (None, "no_findings"):
                    findings[guideline_id] += 1
    return {
        guideline_id: findings.get(guideline_id, 0) / count
        for guideline_id, count in reviews.items()
        if count > 0
    }


def project_fingerprint(project_root: Path) -> str:
    """Hash of the code state a review verdict applies to.

    Covers git HEAD, the working-tree status listing, the tracked diff, and
    the style-guide corpus the project is judged against (so editing any
    guideline changes the fingerprint). Untracked file *content* is not hashed
    — only its path via `git status` — acceptable for a TTL gate that errs
    toward re-reviewing.
    """
    hasher = hashlib.sha256()
    for git_args in (("rev-parse", "HEAD"), ("status", "--porcelain"), ("diff", "HEAD")):
        result = subprocess.run(
            ["git", "-C", str(project_root), *git_args],
            capture_output=True,
            text=True,
            check=False,
        )
        hasher.update(result.stdout.encode())
    for style_file in list_style_files(project_root):
        hasher.update(str(style_file).encode())
        try:
            hasher.update(style_file.read_bytes())
        except OSError:
            continue
    return hasher.hexdigest()


def parse_utc_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class LastReview(NamedTuple):
    end_time: str
    fingerprint: str


def last_review_index(project: str) -> dict[str, LastReview]:
    """Per-guideline most recent review: (row end_time, row fingerprint).

    Pre-filter skips count as reviews — a deterministic zero-candidate check
    is a valid verdict at that code state. The fingerprint is retained for
    audit only; `unit_is_due` keys off `end_time` alone (TTL re-arms each unit
    on time, regardless of fingerprint). `end_time` is what matters here.
    """
    index: dict[str, LastReview] = {}
    for row in load_history(project):
        entry = LastReview(row.get("end_time", ""), row.get("fingerprint", ""))
        for reviewed in row.get("reviewed_units", []):
            guideline_id = reviewed.get("guideline_id")
            if isinstance(guideline_id, str):
                index[guideline_id] = entry
    return index


def unit_is_due(
    unit: Unit,
    review_index: dict[str, LastReview],
    now: datetime,
    ttl: timedelta,
    project_root: Path,
) -> bool:
    """Whether a reviewable unit needs a fresh look this run.

    Due triggers, per guideline:
      - never reviewed in history
      - the guideline file changed since its last review (immediate, TTL ignored)
      - the last review is older than the TTL

    The TTL alone re-arms a unit. The eval agent is non-deterministic, so a
    later pass over the same code can surface a finding an earlier pass missed;
    re-sampling on every TTL lapse is how that recall accumulates. There is no
    dormancy gate — a unit reviewed clean still comes due once its TTL passes,
    whether or not the project changed. The recorded fingerprint is no longer
    consulted here; it stays on history rows for audit only.
    """
    for guideline_id in unit.guideline_ids:
        last = review_index.get(guideline_id)
        if last is None:
            return True
        reviewed_at = parse_utc_timestamp(last.end_time)
        if reviewed_at is None:
            return True
        path = guideline_path(guideline_id, project_root)
        if not path.exists():
            return True
        if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) > reviewed_at:
            return True
        if now - reviewed_at > ttl:
            return True
    return False


def unit_next_due_epoch(
    unit: Unit,
    review_index: dict[str, LastReview],
    ttl: timedelta,
) -> float | None:
    """Soonest epoch this unit re-arms on the TTL alone.

    For a not-currently-due unit, that is the earliest of its guidelines'
    `last_review + ttl`. Returns None when it can't be predicted (a guideline
    with no parseable review timestamp — that unit is already due, not pending).
    Guideline-file edits can pull the real date earlier but are unpredictable,
    so they are not modeled here.
    """
    soonest: float | None = None
    for guideline_id in unit.guideline_ids:
        last = review_index.get(guideline_id)
        if last is None:
            return None
        reviewed_at = parse_utc_timestamp(last.end_time)
        if reviewed_at is None:
            return None
        expiry = (reviewed_at + ttl).timestamp()
        if soonest is None or expiry < soonest:
            soonest = expiry
    return soonest


def due_units_payload(project_root: Path) -> dict[str, object]:
    """Read-only report of which reviewable units are due right now.

    `due_unit_count == 0` is the launch gate: the clean-fix skips spawning an
    eval agent for the project entirely.
    """
    project = project_root.name.removesuffix("_style_fix")
    reviewable = [unit for unit in build_units(project_root) if unit.budget_cost > 0]
    current_fingerprint = project_fingerprint(project_root)
    review_index = last_review_index(project)
    now = datetime.now(tz=timezone.utc)
    ttl = timedelta(days=eval_ttl_days())
    due_id_set = {
        unit.unit_id
        for unit in reviewable
        if unit_is_due(unit, review_index, now, ttl, project_root)
    }
    due_ids = [unit.unit_id for unit in reviewable if unit.unit_id in due_id_set]
    # Soonest a currently-not-due unit re-arms on the TTL alone. 0 means none
    # to predict (no reviewable units, or every unit is already due).
    pending_epochs = [
        epoch
        for unit in reviewable
        if unit.unit_id not in due_id_set
        for epoch in (unit_next_due_epoch(unit, review_index, ttl),)
        if epoch is not None
    ]
    next_due_epoch = int(min(pending_epochs)) if pending_epochs else 0
    return {
        "due_unit_count": len(due_ids),
        "due_unit_ids": due_ids,
        "fingerprint": current_fingerprint,
        "next_due_epoch": next_due_epoch,
        "reviewable_unit_total": len(reviewable),
        "ttl_days": eval_ttl_days(),
    }


def non_negotiable_guideline_ids(project_root: Path) -> list[str]:
    return [
        guideline_id
        for unit in build_units(project_root)
        if unit.budget_cost == 0
        for guideline_id in unit.guideline_ids
    ]


def start_run(project_root: Path) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    project = project_root.name.removesuffix("_style_fix")
    cap = max_new_findings()
    existing = load_pending(project)
    if (
        existing
        and existing.get("phase") == "evaluation"
        and existing.get("stop_reason", "") not in COMPLETED_STOP_REASONS
    ):
        # Resume an interrupted evaluation instead of resetting it. Early-quit
        # agents and timeout kills leave a mid-run pending behind; its per-unit
        # verdicts each went through record-unit and are as valid as any other.
        # Refresh budget and unit totals in case the conf or guide changed.
        existing["budget"] = cap
        _ = refresh_evaluation_summary(existing, project_root)
        existing["updated_at"] = utc_now()
        write_pending(project, existing)
        return
    totals = unit_totals(project_root)
    payload: PendingState = {
        "budget": cap,
        "checked_unit_count": 0,
        "evaluation_markdown": "",
        "evaluation_complete": False,
        "evaluation_summary": {},
        "evaluation_updated_at": "",
        "finding_count": 0,
        "guideline_total": totals["guideline_total"],
        "phase": "evaluation",
        "project_root": str(project_root),
        "reviewable_unit_total": totals["reviewable_unit_total"],
        "reviewed_unit_ids": [],
        "reviewed_units": [],
        "scratch_exports": {},
        "scored_count": 0,
        "start_time": utc_now(),
        "stop_reason": "",
        "updated_at": utc_now(),
    }
    _ = refresh_evaluation_summary(payload, project_root)
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


def save_evaluation(project_root: Path, eval_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        raise SystemExit(f"No pending run for {project}. Start a run first.")
    if not eval_path.exists():
        raise SystemExit(f"Evaluation markdown not found: {eval_path}")
    markdown = eval_path.read_text()
    pending["evaluation_markdown"] = markdown
    pending["evaluation_updated_at"] = utc_now()
    _ = refresh_evaluation_summary(pending, project_root)
    pending["updated_at"] = utc_now()
    write_pending(project, pending)


def export_evaluation(project: str, output: Path, kind: str = "scratch") -> None:
    pending = load_pending(project)
    markdown = pending.get("evaluation_markdown", "") if pending else ""
    if not markdown:
        raise SystemExit(f"No pending evaluation markdown for {project}.")
    output.parent.mkdir(parents=True, exist_ok=True)
    _ = output.write_text(markdown)
    exports = dict(pending.get("scratch_exports", {}))
    exports[kind] = {"exported_at": utc_now(), "path": str(output)}
    pending["scratch_exports"] = exports
    pending["updated_at"] = utc_now()
    write_pending(project, pending)


def pending_summary_root(pending: PendingState, fallback: Path) -> Path:
    raw_root = pending.get("project_root", "")
    if raw_root:
        stored_root = Path(raw_root).expanduser()
        if stored_root.exists():
            return stored_root.resolve()
    return fallback


def evaluation_status_payload(project: str) -> dict[str, object]:
    pending = load_pending(project)
    markdown = pending.get("evaluation_markdown", "") if pending else ""
    finding_count = count_findings_in_markdown(markdown)
    has_review_log = "## Review Log" in markdown
    has_fix_summary = has_fix_summary_marker(markdown)
    phase = str(pending.get("phase", "")) if pending else ""
    if not markdown:
        status = "missing"
    elif finding_count > 0 and has_fix_summary and phase == "fix_failed":
        status = "fix_failed_findings"
    elif finding_count > 0 and has_fix_summary:
        status = "fixed_findings"
    elif finding_count > 0 and has_review_log:
        status = "reviewed_findings"
    elif finding_count > 0:
        status = "findings"
    else:
        status = "no_findings"
    checked_unit_count = (
        int(pending.get("checked_unit_count", 0) or 0)
        if pending
        else 0
    )
    reviewable_unit_total = (
        int(pending.get("reviewable_unit_total", 0) or 0)
        if pending
        else 0
    )
    guideline_total = int(pending.get("guideline_total", 0) or 0) if pending else 0
    stop_reason = pending.get("stop_reason", "") if pending else ""
    budget = int(pending.get("budget", 0) or 0) if pending else 0
    coverage = (
        f"{checked_unit_count}/{reviewable_unit_total}"
        if reviewable_unit_total
        else ""
    )
    return {
        "budget": budget,
        "checked_unit_count": checked_unit_count,
        "coverage": coverage,
        "evaluation_complete": bool(pending.get("evaluation_complete", False)) if pending else False,
        "finding_count": finding_count,
        "guideline_total": guideline_total,
        "has_no_violations": has_no_violations_marker(markdown),
        "has_pending": bool(pending),
        "has_fix_summary": has_fix_summary,
        "has_review_log": has_review_log,
        "line_count": len(markdown.splitlines()) if markdown else 0,
        "phase": phase,
        "reviewable_unit_total": reviewable_unit_total,
        "scratch_exports": pending.get("scratch_exports", {}) if pending else {},
        "status": status,
        "stop_reason": stop_reason,
    }


def next_unit(project_root: Path) -> dict[str, object]:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        raise SystemExit(f"No pending run for {project}. Start a run first.")

    budget = max_new_findings()
    evaluation_markdown = pending.get("evaluation_markdown", "")
    finding_count = count_findings_in_markdown(evaluation_markdown)
    pending["budget"] = budget
    _ = refresh_evaluation_summary(pending, project_root)
    write_pending(project, pending)
    if finding_count >= budget:
        summary = refresh_evaluation_summary(pending, project_root, "budget_reached")
        pending["updated_at"] = utc_now()
        write_pending(project, pending)
        return {
            "budget": budget,
            "checked_unit_count": summary.get("checked_unit_count", 0),
            "coverage": summary.get("coverage", ""),
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "reviewable_unit_total": summary.get("reviewable_unit_total", 0),
            "scored_count": finding_count,
            "status": "complete",
            "stop_reason": "budget_reached",
        }

    # Per-run unit quota: bound a single run to minutes instead of a full-guide
    # sweep. Pre-filter skips are free (no LLM call) and do not count.
    quota = eval_unit_quota()
    llm_reviewed_count = sum(
        1
        for reviewed in pending.get("reviewed_units", [])
        if reviewed.get("outcome", {}).get("skipped_by") not in FREE_SKIP_SOURCES
    )
    if llm_reviewed_count >= quota:
        summary = refresh_evaluation_summary(pending, project_root, "quota_reached")
        pending["updated_at"] = utc_now()
        write_pending(project, pending)
        return {
            "budget": budget,
            "checked_unit_count": summary.get("checked_unit_count", 0),
            "coverage": summary.get("coverage", ""),
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "reviewable_unit_total": summary.get("reviewable_unit_total", 0),
            "scored_count": finding_count,
            "status": "complete",
            "stop_reason": "quota_reached",
        }

    seen_unit_ids: set[str] = set(pending.get("reviewed_unit_ids", []))
    units = build_units(project_root)
    counts = review_counts(project_root)
    # Cross-project hit rates — high-yield guidelines walk first so dirty
    # projects hit the budget cap and exit early instead of plowing through
    # guidelines that never produce findings.
    hit_rates = cross_project_hit_rates()
    # TTL gate: only units that are due get re-reviewed. A unit reviewed clean
    # within the TTL is skipped; once its TTL lapses it re-arms on time alone
    # (the eval agent is non-deterministic, so a later pass can catch what an
    # earlier one missed). "exhausted" means "nothing due left", not "every
    # guideline swept this run".
    review_index = last_review_index(project)
    now = datetime.now(tz=timezone.utc)
    ttl = timedelta(days=eval_ttl_days())
    sortable: list[tuple[float, int, int, str, Unit]] = []
    for unit in units:
        if unit.budget_cost == 0:
            continue
        if unit.unit_id in seen_unit_ids:
            continue
        if not unit_is_due(unit, review_index, now, ttl, project_root):
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
    # candidate without invoking the LLM. Generator-backed units additionally run
    # their candidate generator here: zero candidates is the same free skip, and a
    # non-empty list rides along in the unit payload as the agent's closed list.
    enumeration: Enumeration | None = None
    while sortable:
        enumeration = None
        _, unit_count, _, _, unit = sortable[0]
        if unit.pre_filter and not pre_filter_has_candidates(unit.pre_filter, project_root):
            auto_record_pre_filter_skip(project, unit)
            _ = sortable.pop(0)
            continue
        spec = unit_candidates_spec(unit.unit_id, project_root)
        if spec is not None:
            enumeration = enumerate_candidates(spec, project_root)
            if not enumeration.candidates:
                auto_record_pre_filter_skip(project, unit, skipped_by="candidates")
                _ = sortable.pop(0)
                continue
        break
    else:
        pending = load_pending(project)
        evaluation_markdown = pending.get("evaluation_markdown", "")
        finding_count = count_findings_in_markdown(evaluation_markdown)
        summary = refresh_evaluation_summary(pending, project_root, "exhausted")
        pending["updated_at"] = utc_now()
        write_pending(project, pending)
        return {
            "budget": budget,
            "checked_unit_count": summary.get("checked_unit_count", 0),
            "coverage": summary.get("coverage", ""),
            "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
            "reviewable_unit_total": summary.get("reviewable_unit_total", 0),
            "scored_count": finding_count,
            "status": "complete",
            "stop_reason": "exhausted",
        }

    summary = refresh_evaluation_summary(pending, project_root)
    unit_payload: dict[str, object] = {
        "budget_cost": unit.budget_cost,
        "display_name": unit.display_name,
        "guideline_ids": list(unit.guideline_ids),
        "review_count_before": unit_count,
        "see_also_guideline_ids": list(unit.see_also_guideline_ids),
        "unit_id": unit.unit_id,
    }
    if enumeration is not None:
        unit_payload["candidates"] = [
            {"index": index, "file": candidate.file, "line": candidate.line, "text": candidate.text}
            for index, candidate in enumerate(enumeration.candidates)
        ]
        unit_payload["candidate_count"] = len(enumeration.candidates)
        unit_payload["candidate_source"] = enumeration.source
    return {
        "budget": budget,
        "checked_unit_count": summary.get("checked_unit_count", 0),
        "coverage": summary.get("coverage", ""),
        "non_negotiable_guideline_ids": non_negotiable_guideline_ids(project_root),
        "reviewable_unit_total": summary.get("reviewable_unit_total", 0),
        "scored_count": finding_count,
        "status": "next",
        "unit": unit_payload,
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

    # Generator-backed units: re-run the (deterministic, cheap) generator and
    # refuse the record unless every candidate carries a disposition. An
    # early-quitting agent cannot silently narrow coverage.
    spec = unit_candidates_spec(unit_id, project_root)
    if spec is not None:
        verify_dispositions(unit_id, payload, enumerate_candidates(spec, project_root))

    evaluation_markdown = (
        eval_path.read_text()
        if eval_path.exists()
        else pending.get("evaluation_markdown", "")
    )
    eval_guidelines: set[str] = set(
        parse_eval_guidelines_text(evaluation_markdown, project_root).values()
    )

    found_finding = False
    for result in results:
        guideline_id = result.get("guideline_id")
        if not isinstance(guideline_id, str):
            continue
        reviewed_entry: ReviewedUnit = {"guideline_id": guideline_id}
        outcome = result.get("outcome")
        is_finding = False
        finding_source = ""
        top_source = result.get("finding_source")
        if isinstance(top_source, str):
            reviewed_entry["finding_source"] = top_source
            finding_source = top_source
        if isinstance(outcome, dict):
            reviewed_entry["outcome"] = outcome
            status = outcome.get("status")
            outcome_source = outcome.get("finding_source")
            if isinstance(outcome_source, str):
                finding_source = outcome_source
            if status in {"finding", "fixed", "partial", "skipped"} or finding_source in {"new", "carried_forward"}:
                is_finding = True
        else:
            if isinstance(top_source, str):
                is_finding = top_source in {"new", "carried_forward"}
            else:
                raise SystemExit(f"Result for {guideline_id} must include outcome or finding_source.")
        if is_finding and guideline_id not in eval_guidelines:
            message = (
                f"Refusing to record finding for {guideline_id}: not present under "
                + f"'## Improvements' in {eval_path}. Append the finding to "
                + "the scratch evaluation markdown before calling record-unit."
            )
            raise SystemExit(message)
        if is_finding:
            found_finding = True
        reviewed_units.append(reviewed_entry)

    pending["reviewed_units"] = reviewed_units
    reviewed_unit_ids.append(unit_id)
    pending["reviewed_unit_ids"] = reviewed_unit_ids
    pending["evaluation_markdown"] = evaluation_markdown
    pending["evaluation_updated_at"] = utc_now()
    pending["phase"] = "evaluation"
    pending["updated_at"] = utc_now()
    pending["last_unit_id"] = unit_id
    if found_finding:
        pending["last_unit_result"] = "finding"
    else:
        pending["last_unit_result"] = "no_findings"
    _ = refresh_evaluation_summary(pending, project_root)
    write_pending(project, pending)


def parse_eval_guidelines(path: Path, project_root: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_eval_guidelines_text(path.read_text(), project_root)


def parse_eval_guidelines_text(markdown: str, project_root: Path) -> dict[str, str]:
    guideline_by_title: dict[str, str] = {}
    current_title = ""
    in_improvements = False
    for raw_line in markdown.splitlines():
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
                fix_entries[current_fix]["status"] = line.removeprefix("**Status:**").strip()
                continue
            if line.startswith("**What was done:**") and current_fix:
                fix_entries[current_fix]["summary"] = line.removeprefix("**What was done:**").strip()
                continue
            if line.startswith("**Post-fix search:**") and current_fix:
                fix_entries[current_fix]["post_fix_search"] = line.removeprefix("**Post-fix search:**").strip()
                continue
            if line.startswith("**Issues:**") and current_fix:
                fix_entries[current_fix]["reason"] = line.removeprefix("**Issues:**").strip()
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
                post_fix_search = entry.get("post_fix_search", "")
                if re.search(r"(^|[^0-9])0\s+remaining\b", post_fix_search.lower()):
                    statuses.append("fixed")
                else:
                    statuses.append("partial")
                    reason = entry.get("reason", "")
                    if post_fix_search:
                        coverage_reason = f"Post-fix search did not report 0 remaining: {post_fix_search}"
                    else:
                        coverage_reason = "Applied finding is missing required Post-fix search: 0 remaining."
                    entry["reason"] = " ".join(part for part in (reason, coverage_reason) if part)
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
    _ = refresh_evaluation_summary(pending)
    stop_reason = pending.get("stop_reason", "")
    if stop_reason not in COMPLETED_STOP_REASONS:
        # The helper never declared this run complete — the agent stopped on
        # its own (or was killed). Recording it would write a fake-clean row
        # into history and poison the hit-rate stats; keep the pending so the
        # next run resumes from its recorded units instead of starting over.
        coverage = pending.get("evaluation_summary", {}).get("coverage", "")
        message = (
            f"finalize-no-findings: refusing {project} — run incomplete"
            + f" (stop_reason={stop_reason or 'none'}, coverage={coverage}); pending kept for resume"
        )
        print(message, file=sys.stderr)
        raise SystemExit(EXIT_INCOMPLETE_RUN)
    # Stamp the code state the clean verdicts were earned at. Audit-only now —
    # `unit_is_due` re-arms each unit on its TTL alone and no longer consults
    # the fingerprint (the dormancy gate was removed); kept for traceability.
    fingerprint = ""
    raw_root = pending.get("project_root", "")
    if raw_root:
        root = Path(raw_root).expanduser()
        if root.exists():
            fingerprint = project_fingerprint(root)
    row: HistoryRow = {
        "start_time": pending.get("start_time", ""),
        "end_time": utc_now(),
        "evaluation_summary": history_summary(pending),
        "reviewed_units": list(pending.get("reviewed_units", [])),
    }
    if fingerprint:
        row["fingerprint"] = fingerprint
    append_jsonl_history(history_file(project), row)
    remove_pending(project)


def last_findings(project: str) -> str:
    rows = load_history(project)
    for row in reversed(rows):
        for reviewed in row.get("reviewed_units", []):
            outcome: Outcome = reviewed.get("outcome", {})
            if outcome.get("skipped_by") in FREE_SKIP_SOURCES:
                continue
            if reviewed.get("finding_source") or outcome.get("status") not in (None, "no_findings"):
                end_time = row.get("end_time", "")
                return end_time or "unknown"
    return "never"


def is_finding_like(reviewed: ReviewedUnit) -> bool:
    outcome: Outcome = reviewed.get("outcome", {})
    if outcome.get("skipped_by") in FREE_SKIP_SOURCES:
        return False
    status = outcome.get("status")
    finding_source = reviewed.get("finding_source") or outcome.get("finding_source")
    return bool(finding_source) or status not in (None, "", "no_findings")


def guideline_path(guideline_id: str, project_root: Path) -> Path:
    if guideline_id.startswith("docs/style/"):
        return project_root / guideline_id
    return NATE_STYLE_DIR / guideline_id


def unit_candidates_spec(guideline_id: str, project_root: Path) -> CandidatesSpec | None:
    """The unit's `candidates:` frontmatter spec, or None for agent-enumerated units."""
    path = guideline_path(guideline_id, project_root)
    if not path.exists():
        return None
    return read_candidates_spec(path)


def verify_dispositions(unit_id: str, payload: ResultPayload, enumeration: Enumeration) -> None:
    """Refuse a generator-backed record whose dispositions don't close the candidate list.

    Same loud-failure posture as `finalize-no-findings`: exit EXIT_INCOMPLETE_RUN
    so wrappers treat it as an incomplete run, not a crash.
    """

    def refuse(reason: str) -> None:
        print(f"record-unit: refusing {unit_id} — {reason}", file=sys.stderr)
        raise SystemExit(EXIT_INCOMPLETE_RUN)

    expected = len(enumeration.candidates)
    dispositions: list[DispositionEntry] = list(payload.get("dispositions", []))
    if expected == 0:
        return
    if not dispositions:
        refuse(
            f"unit has {expected} helper-enumerated candidates but the results payload"
            + " carries no dispositions array"
        )
    seen: set[int] = set()
    violation_count = 0
    for entry in dispositions:
        index = entry.get("index")
        verdict = entry.get("verdict")
        if not isinstance(index, int) or index in seen:
            refuse(f"disposition index {index!r} is missing, non-integer, or duplicated")
            return
        seen.add(index)
        if verdict == "violation":
            violation_count += 1
        elif verdict == "exception":
            if not entry.get("clause"):
                refuse(f"candidate {index} verdict 'exception' has no clause naming the exception")
        else:
            refuse(f"candidate {index} has verdict {verdict!r}; expected 'violation' or 'exception'")
    missing = set(range(expected)) - seen
    extra = seen - set(range(expected))
    if missing or extra:
        refuse(
            f"dispositions cover {sorted(seen)} but the generator enumerates {expected}"
            + f" candidates (missing {sorted(missing)}, extra {sorted(extra)});"
            + f" candidate_source={enumeration.source}"
        )
    if violation_count > 0:
        has_finding = any(
            result.get("outcome", {}).get("status") == "finding"
            or result.get("finding_source") in ("new", "carried_forward")
            or result.get("outcome", {}).get("finding_source") in ("new", "carried_forward")
            for result in payload.get("results", [])
        )
        if not has_finding:
            refuse(
                f"{violation_count} candidate(s) dispositioned as violations but no result"
                + " records a finding"
            )


def guideline_title(guideline_id: str, project_root: Path) -> str:
    path = guideline_path(guideline_id, project_root)
    if path.exists():
        return extract_title(path)
    return guideline_id


def latest_finding_history_row(project: str) -> HistoryRow:
    rows = load_history(project)
    for row in reversed(rows):
        if any(is_finding_like(reviewed) for reviewed in row.get("reviewed_units", [])):
            return row
    raise SystemExit(f"No history row with findings for {project}.")


def json_block(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def recovery_notice(project: str, source: str) -> list[str]:
    status = evaluation_status_payload(project)
    pending = load_pending(project)
    lines = [
        "## Handoff Recovery",
        "",
        "ERROR STATE: pending JSON did not contain the fix agent's `## Fix Summary` for this worktree.",
        f"Recovery source: {source}.",
        "",
        "Review this as a salvage pass: evaluate the current worktree diff against the recovered style rules, and state in user-facing output that the normal pending handoff was stale or incomplete.",
        "",
        "### Pending Status At Recovery",
        "",
        "```json",
        json_block(
            {
                "status": status.get("status"),
                "phase": status.get("phase"),
                "has_fix_summary": status.get("has_fix_summary"),
                "updated_at": pending.get("updated_at", "") if pending else "",
                "scratch_exports": status.get("scratch_exports", {}),
            }
        ),
        "```",
    ]
    return lines


def recover_evaluation(project: str, project_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = LOG_DIR / f"style_fix_{project}_evaluation.md"
    if scratch.exists():
        scratch_markdown = scratch.read_text()
        if has_fix_summary_marker(scratch_markdown):
            markdown = "\n".join(recovery_notice(project, f"scratch file `{scratch}`"))
            markdown += "\n\n"
            markdown += scratch_markdown
            _ = output.write_text(markdown)
            print(f"Recovered {project} style-fix evaluation from {scratch}")
            return

    row = latest_finding_history_row(project)
    finding_units = [
        reviewed for reviewed in row.get("reviewed_units", []) if is_finding_like(reviewed)
    ]
    lines = recovery_notice(project, f"history row ending `{row.get('end_time', '')}`")
    lines += [
        "",
        "## Improvements",
        "",
    ]
    for index, reviewed in enumerate(finding_units, start=1):
        guideline_id = reviewed.get("guideline_id", "")
        path = guideline_path(guideline_id, project_root)
        outcome: Outcome = reviewed.get("outcome", {})
        title = guideline_title(guideline_id, project_root)
        lines += [
            f"### {index}. Recovered style rule: {title}",
            "",
            f"**Style file**: `{path}`",
            f"**Recovered guideline id**: `{guideline_id}`",
            f"**Recovered from history row**: `{row.get('end_time', '')}`",
            "",
            "Original numbered evaluation text was not available. Verify the current worktree diff against this style file and the recorded outcome below.",
            "",
            "**Recorded outcome**:",
            "",
            "```json",
            json_block(outcome),
            "```",
            "",
        ]
    lines += [
        "## Fix Summary",
        "",
    ]
    for index, reviewed in enumerate(finding_units, start=1):
        guideline_id = reviewed.get("guideline_id", "")
        outcome = reviewed.get("outcome", {})
        status = outcome.get("status", "finding")
        summary = outcome.get("summary", "")
        reason = outcome.get("reason", "")
        lines += [
            f"### Finding {index}: Recovered style rule: {guideline_title(guideline_id, project_root)}",
            f"**Status:** Recovered from history (`{status}`)",
            f"**What was done:** {summary or 'Original Fix Summary text was unavailable; review the current diff directly.'}",
            "**Post-fix search:** unavailable in recovered history row",
        ]
        if reason:
            lines.append(f"**Issues:** {reason}")
        lines.append("")
    lines += [
        "### Cargo Mend Changes",
        "Original Cargo Mend section was unavailable in the recovered history row.",
        "",
        "### Clippy Changes",
        "Original Clippy section was unavailable in the recovered history row.",
        "",
        "### Build Status",
        "- **clippy:** unknown from recovered history row",
        "- **tests:** unknown from recovered history row",
        "",
    ]
    _ = output.write_text("\n".join(lines))
    print(f"Recovered {project} style-fix evaluation from history row {row.get('end_time', '')}")


def finalize_fix(project_root: Path, eval_path: Path) -> None:
    project = project_root.name.removesuffix("_style_fix")
    pending = load_pending(project)
    if not pending:
        return
    if not eval_path.exists():
        raise SystemExit(f"Evaluation markdown not found: {eval_path}")
    markdown = eval_path.read_text()
    if not has_fix_summary_marker(markdown):
        raise SystemExit(f"Fix Summary not found in evaluation markdown: {eval_path}")
    now = utc_now()
    pending["evaluation_markdown"] = markdown
    pending["evaluation_updated_at"] = now
    summary_root = pending_summary_root(pending, project_root)
    _ = refresh_evaluation_summary(pending, summary_root)
    fix_results = parse_fix_results(eval_path, project_root)
    eval_guidelines: set[str] = set(parse_eval_guidelines(eval_path, project_root).values())
    reviewed_units: list[ReviewedUnit] = []
    for reviewed in pending.get("reviewed_units", []):
        guideline_id = reviewed.get("guideline_id", "")
        existing_outcome: Outcome = reviewed.get("outcome", {})
        if existing_outcome and existing_outcome.get("status") != "fix_failed":
            reviewed_units.append(reviewed)
            continue
        if guideline_id in fix_results:
            outcome: Outcome = cast(Outcome, cast(object, dict(fix_results[guideline_id])))
        elif guideline_id in eval_guidelines:
            outcome = {
                "status": "fix_failed",
                "reason": "Finding present in evaluation markdown ## Improvements but no matching ## Fix Summary entry.",
            }
        else:
            outcome = {
                "status": "eval_dropped",
                "reason": "Recorded as a finding via record-unit but absent from evaluation markdown ## Improvements.",
            }
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append({"guideline_id": guideline_id, "outcome": outcome})
    pending["reviewed_units"] = reviewed_units
    pending["phase"] = "fixed"
    pending["fix_finalized_at"] = now
    exports = dict(pending.get("scratch_exports", {}))
    exports["fix"] = {"exported_at": now, "finalized_at": now, "path": str(eval_path)}
    pending["scratch_exports"] = exports
    pending["updated_at"] = now
    if not pending.get("fix_history_recorded_at"):
        row: HistoryRow = {
            "start_time": pending.get("start_time", ""),
            "end_time": now,
            "evaluation_summary": history_summary(pending),
            "reviewed_units": reviewed_units,
        }
        append_jsonl_history(history_file(project), row)
        pending["fix_history_recorded_at"] = now
    write_pending(project, pending)


def finalize_failure(project: str, reason: str) -> None:
    pending = load_pending(project)
    if not pending:
        return
    now = utc_now()
    _ = refresh_evaluation_summary(pending)
    reviewed_units: list[ReviewedUnit] = []
    for reviewed in pending.get("reviewed_units", []):
        existing_outcome: Outcome = reviewed.get("outcome", {})
        if existing_outcome and existing_outcome.get("status") != "fix_failed":
            reviewed_units.append(reviewed)
            continue
        outcome: Outcome = {"status": "fix_failed", "reason": reason}
        if "finding_source" in reviewed:
            outcome["finding_source"] = reviewed["finding_source"]
        reviewed_units.append(
            {"guideline_id": reviewed.get("guideline_id", ""), "outcome": outcome}
        )
    pending["reviewed_units"] = reviewed_units
    pending["phase"] = "fix_failed"
    pending["failure_reason"] = reason
    pending["failure_finalized_at"] = now
    pending["updated_at"] = now
    if not pending.get("failure_history_recorded_at"):
        row: HistoryRow = {
            "start_time": pending.get("start_time", ""),
            "end_time": now,
            "evaluation_summary": history_summary(pending),
            "reviewed_units": reviewed_units,
        }
        append_jsonl_history(history_file(project), row)
        pending["failure_history_recorded_at"] = now
    write_pending(project, pending)


def discard_pending(project: str) -> None:
    remove_pending(project)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start-run")
    _ = start.add_argument("--project-root", required=True)
    next_parser = subparsers.add_parser("next-unit")
    _ = next_parser.add_argument("--project-root", required=True)
    record = subparsers.add_parser("record-unit")
    _ = record.add_argument("--project-root", required=True)
    _ = record.add_argument("--results", required=True)
    _ = record.add_argument(
        "--eval-path",
        required=True,
        help="Path to the scratch evaluation markdown. record-unit refuses to record a finding whose guideline is not present under ## Improvements.",
    )
    save_eval = subparsers.add_parser("save-evaluation")
    _ = save_eval.add_argument("--project-root", required=True)
    _ = save_eval.add_argument("--evaluation", required=True)
    export_eval = subparsers.add_parser("export-evaluation")
    _ = export_eval.add_argument("--project", required=True)
    _ = export_eval.add_argument("--output", required=True)
    _ = export_eval.add_argument("--kind", default="scratch")
    recover_eval = subparsers.add_parser(
        "recover-evaluation",
        help="Write a salvage review markdown for a style-fix worktree whose pending Fix Summary was lost.",
    )
    _ = recover_eval.add_argument("--project", required=True)
    _ = recover_eval.add_argument("--project-root", required=True)
    _ = recover_eval.add_argument("--output", required=True)
    status_eval = subparsers.add_parser("evaluation-status")
    _ = status_eval.add_argument("--project", required=True)
    _ = status_eval.add_argument(
        "--field",
        choices=(
            "budget",
            "checked_unit_count",
            "coverage",
            "evaluation_complete",
            "finding_count",
            "guideline_total",
            "has_fix_summary",
            "has_no_violations",
            "has_pending",
            "has_review_log",
            "line_count",
            "phase",
            "reviewable_unit_total",
            "scratch_exports",
            "status",
            "stop_reason",
        ),
    )
    no_findings = subparsers.add_parser("finalize-no-findings")
    _ = no_findings.add_argument("--project", required=True)
    due = subparsers.add_parser(
        "due-units",
        help="Read-only: report which reviewable units are due under the eval TTL at the current code state.",
    )
    _ = due.add_argument("--project-root", required=True)
    _ = due.add_argument(
        "--field",
        choices=("due_unit_count", "fingerprint", "next_due_epoch", "reviewable_unit_total", "ttl_days"),
    )
    last = subparsers.add_parser(
        "last-findings",
        help="Print end_time of the most recent history row with a real finding outcome, or 'never'.",
    )
    _ = last.add_argument("--project", required=True)
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
    enum_cmd = subparsers.add_parser(
        "enumerate-candidates",
        help="Read-only: run a guideline's candidate generator and print the candidate list.",
    )
    _ = enum_cmd.add_argument("--project-root", required=True)
    _ = enum_cmd.add_argument(
        "--guideline",
        required=True,
        help="Guideline to enumerate. Accepts stem, filename, guideline id, or absolute path.",
    )
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
        start_run(Path(_arg_str(args, "project_root")).expanduser().resolve())
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
    if command == "save-evaluation":
        save_evaluation(
            Path(_arg_str(args, "project_root")).expanduser().resolve(),
            Path(_arg_str(args, "evaluation")).expanduser().resolve(),
        )
        return
    if command == "export-evaluation":
        export_evaluation(
            _arg_str(args, "project"),
            Path(_arg_str(args, "output")).expanduser().resolve(),
            _arg_str(args, "kind"),
        )
        return
    if command == "recover-evaluation":
        recover_evaluation(
            _arg_str(args, "project"),
            Path(_arg_str(args, "project_root")).expanduser().resolve(),
            Path(_arg_str(args, "output")).expanduser().resolve(),
        )
        return
    if command == "evaluation-status":
        payload = evaluation_status_payload(_arg_str(args, "project"))
        field = getattr(args, "field")  # pyright: ignore[reportAny]
        if isinstance(field, str):
            print(payload[field])
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if command == "finalize-no-findings":
        finalize_no_findings(_arg_str(args, "project"))
        return
    if command == "due-units":
        payload = due_units_payload(Path(_arg_str(args, "project_root")).expanduser().resolve())
        field = getattr(args, "field")  # pyright: ignore[reportAny]
        if isinstance(field, str):
            print(payload[field])
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if command == "last-findings":
        print(last_findings(_arg_str(args, "project")))
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
    if command == "enumerate-candidates":
        project_root = Path(_arg_str(args, "project_root")).expanduser().resolve()
        target = resolve_focus_targets([_arg_str(args, "guideline")], project_root)[0]
        spec = read_candidates_spec(target)
        if spec is None:
            raise SystemExit(f"{target} has no candidates: block in its frontmatter.")
        enumeration = enumerate_candidates(spec, project_root)
        print(json.dumps(
            {
                "candidate_count": len(enumeration.candidates),
                "candidate_source": enumeration.source,
                "candidates": [
                    {"index": index, "file": c.file, "line": c.line, "text": c.text}
                    for index, c in enumerate(enumeration.candidates)
                ],
            },
            indent=2,
            sort_keys=True,
        ))
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
