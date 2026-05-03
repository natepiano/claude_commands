#!/usr/bin/env python3
"""Parse a nightly orchestrator log into structured records.

Modes:
  default                   Full report parse of one log (newest if no path).
  --list                    Enumerate logs in ~/.local/logs/nightly/ with summaries.
  --phase-detect <log>      Emit the currently-running phase (for /monitor_nightly).
  --filter-regex            Print the live-monitor filter regex (single source of truth).

Output is line-oriented, key=value style. Consumers (slash commands, Claude)
render tables and prose; this script only emits parsed facts.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

LOG_DIR = Path.home() / ".local" / "logs" / "nightly"

# Single source of truth for the live-monitor filter regex.
# Kept identical in spirit to the alternation that previously lived in
# commands/monitor_nightly.md so /monitor_nightly can consume it via --filter-regex.
MONITOR_FILTER_REGEX = (
    r"(^|[[:space:]])(CLEAN|BUILD|MEND|DONE|ERROR|WARNING|TIMEOUT|RETRY|RETRY OK|RETRY FAILED|FAILED|WARN|OK):"
    r"|(^|[[:space:]])WARMUP (OK|FAIL|SKIP):"
    r"|(^|[[:space:]])Launched: "
    r"|^=== "
    r"|(^|[[:space:]])commit-style-results: "
    r"|^Wrote /Users/natemccoy/rust/nate_style/style_report\.md$"
)

PHASES: tuple[str, ...] = ("clean", "warmup", "eval", "review", "fix")
CELL_DASH = "-"

# Permanent exclusions: directory will never be a candidate while it exists in
# its current form. Includes both the user's `[exclude]` config and structural
# reasons (not a Rust project, framework-managed worktree, etc.).
ALWAYS_EXCLUDED_REASONS: frozenset[str] = frozenset(
    {
        "excluded",
        "no Cargo.toml",
        "style-fix worktree",
        "worktree, not primary checkout",
        "no bevy_panorbit_camera/Cargo.toml",
    }
)

# Transient state: project IS a candidate, but a leftover worktree or in-flight
# state from a prior run blocks it. The user can clean these up to unblock.
FRAMEWORK_FILTER_REASONS: frozenset[str] = frozenset(
    {
        "style_fix directory exists",
        "another worktree checkout already exists",
    }
)

# Combined set used during pruning — a row whose only cells are SKIPs in either
# bucket gets pulled out of the matrix.
BOOKKEEPING_REASONS: frozenset[str] = ALWAYS_EXCLUDED_REASONS | FRAMEWORK_FILTER_REASONS

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
COMPLETE_RE = re.compile(r"=== Nightly Rust clean \+ rebuild complete \(([^)]+)\) ===")
EVAL_HEADER_RE = re.compile(r"=== Style evaluation: (\d+) projects ===")
EVAL_DONE_RE = re.compile(r"=== Done: (\d+) succeeded, (\d+) failed out of (\d+) ===")
REVIEW_HEADER_RE = re.compile(r"=== Style eval review: (\d+) projects ===")
REVIEW_DONE_RE = re.compile(r"=== Done: (\d+) reviewed, (\d+) failed out of (\d+) ===")
FIX_HEADER_RE = re.compile(r"=== Style-fix worktrees: (\d+) eligible projects ===")
FIX_DONE_RE = re.compile(
    r"=== Done: (\d+) created, (\d+) failed, (\d+) skipped out of (\d+) ==="
)
FILENAME_TS_RE = re.compile(r"(\d{8}-\d{6})")


@dataclass
class Cell:
    """A single table cell: state plus optional reason."""

    state: str = CELL_DASH  # OK / FAIL / SKIP / - / OK:warning
    reason: str = ""

    def render(self) -> str:
        if self.reason:
            return f"{self.state}:{self.reason}"
        return self.state


@dataclass
class PhaseStats:
    ok: int = 0
    fail: int = 0
    skip: int = 0
    processed: int = 0  # clean phase
    warnings: int = 0
    footer_ok: int | None = None
    footer_fail: int | None = None
    footer_total: int | None = None
    present: bool = False  # was the phase header / data observed at all


@dataclass
class Warning:
    phase: str
    project: str
    message: str


@dataclass
class SkipReason:
    phase: str
    reason: str
    project: str


@dataclass
class ToolWarning:
    """Sub-tool failure (cargo mend, etc.) — not a project failure."""

    phase: str
    project: str
    message: str


@dataclass
class ParseResult:
    path: Path
    run_start: str = ""
    run_end: str = ""
    elapsed: str = ""
    status: str = "in-progress"  # complete / crashed / partial / in-progress
    rows: dict[str, dict[str, Cell]] = field(default_factory=dict)
    stats: dict[str, PhaseStats] = field(default_factory=dict)
    warnings: list[Warning] = field(default_factory=list)
    tool_warnings: list[ToolWarning] = field(default_factory=list)
    skip_reasons: list[SkipReason] = field(default_factory=list)
    always_excluded: list[tuple[str, str]] = field(default_factory=list)
    filtered_out: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def make_blank_row() -> dict[str, Cell]:
    return {phase: Cell() for phase in PHASES}


def get_row(rows: dict[str, dict[str, Cell]], project: str) -> dict[str, Cell]:
    if project not in rows:
        rows[project] = make_blank_row()
    return rows[project]


def slugify_reason(reason: str) -> str:
    """Compress a parenthetical reason into a short token suitable for cells."""
    text = reason.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:60]


def find_newest_log() -> Path | None:
    if not LOG_DIR.exists():
        return None
    candidates = sorted(LOG_DIR.glob("*.log"), key=_filename_ts_key, reverse=True)
    return candidates[0] if candidates else None


def _filename_ts_key(path: Path) -> str:
    match = FILENAME_TS_RE.search(path.name)
    return match.group(1) if match else ""


def detect_phase_boundaries(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Return inclusive start / exclusive end indices for each phase that appears.

    Phases not present are omitted from the dict.
    """
    bounds: dict[str, tuple[int, int]] = {}
    n = len(lines)
    clean_start = 0 if n > 0 else -1
    warmup_start = -1
    eval_start = -1
    eval_end = -1
    review_start = -1
    review_end = -1
    fix_start = -1
    fix_end = n  # default — fix runs to end

    eval_done_seen = False
    review_done_seen = False

    for i, line in enumerate(lines):
        if warmup_start == -1 and "WARMUP:" in line and "WARMUP KILLING" not in line:
            warmup_start = i
        if eval_start == -1 and EVAL_HEADER_RE.search(line):
            eval_start = i
            continue
        if eval_start != -1 and not eval_done_seen and EVAL_DONE_RE.search(line):
            eval_end = i + 1
            eval_done_seen = True
            continue
        if review_start == -1 and (
            REVIEW_HEADER_RE.search(line)
            or "Reviewing EVALUATION.md" in line
        ):
            review_start = i
            continue
        if review_start != -1 and not review_done_seen and REVIEW_DONE_RE.search(line):
            review_end = i + 1
            review_done_seen = True
            continue
        if fix_start == -1 and (
            FIX_HEADER_RE.search(line)
            or line.startswith("ELIGIBLE: ")
            or "Creating style-fix worktrees" in line
        ):
            fix_start = i
        if FIX_DONE_RE.search(line) and fix_start != -1:
            fix_end = i + 1

    # clean phase ends at first warmup OR first eval header OR first
    # "Starting style evaluations" line.
    clean_end = n
    for i, line in enumerate(lines):
        if "WARMUP:" in line and "WARMUP KILLING" not in line:
            clean_end = i
            break
        if EVAL_HEADER_RE.search(line) or "Starting style evaluations" in line:
            clean_end = i
            break

    # warmup ends at clean_end's successor: first eval header / "Starting style".
    warmup_end = clean_end  # default if no warmup
    if warmup_start != -1:
        warmup_end = n
        for i in range(warmup_start, n):
            if EVAL_HEADER_RE.search(lines[i]) or "Starting style evaluations" in lines[i]:
                warmup_end = i
                break

    if clean_start != -1 and clean_end > clean_start:
        # Only register clean if there's a timestamped `CLEAN: <project>` line.
        # Untimestamped SKIP/ELIGIBLE lines belong to style-fix-worktrees.sh, not the clean phase.
        slice_text = "\n".join(lines[clean_start:clean_end])
        if re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} CLEAN: ", slice_text, re.MULTILINE):
            bounds["clean"] = (clean_start, clean_end)
    if warmup_start != -1:
        bounds["warmup"] = (warmup_start, warmup_end)
    if eval_start != -1:
        bounds["eval"] = (eval_start, eval_end if eval_end != -1 else n)
    if review_start != -1:
        bounds["review"] = (review_start, review_end if review_end != -1 else n)
    if fix_start != -1:
        bounds["fix"] = (fix_start, fix_end)

    return bounds


def parse_clean_phase(
    lines: list[str], result: ParseResult
) -> None:
    stats = result.stats["clean"]
    stats.present = True
    project_warnings: dict[str, str] = {}
    done_projects: set[str] = set()
    cleaned_projects: set[str] = set()

    skip_re = re.compile(r"SKIP: (.+?) \(([^)]+)\)")
    clean_re = re.compile(r"CLEAN: (\S+)")
    done_re = re.compile(r"DONE: (\S+)")
    warn_re = re.compile(r"WARNING: (.+?) for (\S+)")
    error_re = re.compile(r"ERROR: (.+?) for (\S+)")

    for line in lines:
        m = clean_re.search(line)
        if m:
            cleaned_projects.add(m.group(1))
            continue
        m = done_re.search(line)
        if m:
            done_projects.add(m.group(1))
            continue
        m = skip_re.search(line)
        if m and "WARMUP" not in line and "PHASE" not in line:
            project, reason = m.group(1), m.group(2)
            row = get_row(result.rows, project)
            row["clean"] = Cell("SKIP", slugify_reason(reason))
            stats.skip += 1
            result.skip_reasons.append(SkipReason("clean", reason, project))
            continue
        m = warn_re.search(line)
        if m:
            msg, project = m.group(1), m.group(2)
            project_warnings[project] = msg
            stats.warnings += 1
            result.tool_warnings.append(ToolWarning("clean", project, msg))
            continue
        m = error_re.search(line)
        if m:
            msg, project = m.group(1), m.group(2)
            project_warnings[project] = f"ERROR {msg}"
            result.warnings.append(Warning("clean", project, f"ERROR {msg}"))

    for project in cleaned_projects:
        row = get_row(result.rows, project)
        if project in done_projects:
            if project in project_warnings:
                row["clean"] = Cell("OK", "warning")
            else:
                row["clean"] = Cell("OK")
        else:
            if project in project_warnings:
                row["clean"] = Cell("FAIL", slugify_reason(project_warnings[project]))
                stats.fail += 1
            else:
                row["clean"] = Cell("OK")
        if row["clean"].state.startswith("OK"):
            stats.ok += 1
        stats.processed += 1


def parse_warmup_phase(lines: list[str], result: ParseResult) -> None:
    stats = result.stats["warmup"]
    stats.present = True
    ok_re = re.compile(r"WARMUP OK: (\S+)")
    fail_re = re.compile(r"WARMUP FAIL: (\S+) \(([^)]+)\)")
    skip_re = re.compile(r"WARMUP SKIP: (\S+) \(([^)]+)\)")

    for line in lines:
        m = ok_re.search(line)
        if m:
            project = m.group(1)
            get_row(result.rows, project)["warmup"] = Cell("OK")
            stats.ok += 1
            continue
        m = fail_re.search(line)
        if m:
            project, reason = m.group(1), m.group(2)
            get_row(result.rows, project)["warmup"] = Cell("FAIL", slugify_reason(reason))
            stats.fail += 1
            result.warnings.append(Warning("warmup", project, reason))
            continue
        m = skip_re.search(line)
        if m:
            project, reason = m.group(1), m.group(2)
            get_row(result.rows, project)["warmup"] = Cell("SKIP", slugify_reason(reason))
            stats.skip += 1
            result.skip_reasons.append(SkipReason("warmup", reason, project))


def parse_eval_phase(lines: list[str], result: ParseResult) -> None:
    stats = result.stats["eval"]
    stats.present = True
    launched: set[str] = set()
    resolved: set[str] = set()
    ok_re = re.compile(r"^OK: (\S+) \(\s*\d+ lines\)")
    fail_re = re.compile(r"^(FAILED|ERROR|TIMEOUT): (\S+)(?:\s*\(([^)]+)\))?")
    skip_re = re.compile(r"^SKIP: (\S+) \(([^)]+)\)")
    launched_re = re.compile(r"^Launched: (\S+) ")

    for line in lines:
        m = ok_re.match(line)
        if m:
            project = m.group(1)
            get_row(result.rows, project)["eval"] = Cell("OK")
            stats.ok += 1
            resolved.add(project)
            continue
        m = fail_re.match(line)
        if m:
            project = m.group(2)
            reason = m.group(3) or m.group(1).lower()
            get_row(result.rows, project)["eval"] = Cell("FAIL", slugify_reason(reason))
            stats.fail += 1
            resolved.add(project)
            result.warnings.append(Warning("eval", project, reason))
            continue
        m = skip_re.match(line)
        if m:
            project, reason = m.group(1), m.group(2)
            get_row(result.rows, project)["eval"] = Cell("SKIP", slugify_reason(reason))
            stats.skip += 1
            result.skip_reasons.append(SkipReason("eval", reason, project))
            continue
        m = launched_re.match(line)
        if m:
            launched.add(m.group(1))
        m = EVAL_DONE_RE.search(line)
        if m:
            stats.footer_ok = int(m.group(1))
            stats.footer_fail = int(m.group(2))
            stats.footer_total = int(m.group(3))

    # Launched but never resolved → no result; treat as fail.
    for project in launched - resolved:
        get_row(result.rows, project)["eval"] = Cell("FAIL", "no-result")
        stats.fail += 1
        result.warnings.append(Warning("eval", project, "launched but no result"))


def parse_review_phase(lines: list[str], result: ParseResult) -> None:
    stats = result.stats["review"]
    stats.present = True
    launched: set[str] = set()
    resolved: set[str] = set()
    ok_re = re.compile(r"^OK: (\S+)\s*$")
    fail_re = re.compile(r"^FAILED: (\S+)(?:\s*\(([^)]+)\))?")
    skip_re = re.compile(r"^SKIP: (\S+) \(([^)]+)\)")
    launched_re = re.compile(r"^Launched: (\S+) ")

    for line in lines:
        m = ok_re.match(line)
        if m:
            project = m.group(1)
            get_row(result.rows, project)["review"] = Cell("OK")
            stats.ok += 1
            resolved.add(project)
            continue
        m = fail_re.match(line)
        if m:
            project = m.group(1)
            reason = m.group(2) or "failed"
            get_row(result.rows, project)["review"] = Cell("FAIL", slugify_reason(reason))
            stats.fail += 1
            resolved.add(project)
            result.warnings.append(Warning("review", project, reason))
            continue
        m = skip_re.match(line)
        if m:
            project, reason = m.group(1), m.group(2)
            get_row(result.rows, project)["review"] = Cell("SKIP", slugify_reason(reason))
            stats.skip += 1
            result.skip_reasons.append(SkipReason("review", reason, project))
            continue
        m = launched_re.match(line)
        if m:
            launched.add(m.group(1))
        m = REVIEW_DONE_RE.search(line)
        if m:
            stats.footer_ok = int(m.group(1))
            stats.footer_fail = int(m.group(2))
            stats.footer_total = int(m.group(3))

    for project in launched - resolved:
        get_row(result.rows, project)["review"] = Cell("FAIL", "no-result")
        stats.fail += 1
        result.warnings.append(Warning("review", project, "launched but no result"))


def parse_fix_phase(lines: list[str], result: ParseResult) -> None:
    stats = result.stats["fix"]
    stats.present = True
    eligible: set[str] = set()
    fix_results: dict[str, Cell] = {}
    eligible_re = re.compile(r"^ELIGIBLE: (\S+)")
    skip_re = re.compile(r"^SKIP: (\S+) \(([^)]+)\)")
    ok_re = re.compile(r"^OK: (\S+)")
    fail_re = re.compile(r"^(FAILED|ERROR|TIMEOUT|WARN): (\S+)(?:\s*\(([^)]+)\))?")
    retry_failed_re = re.compile(r"^RETRY FAILED: (\S+)")
    retry_error_re = re.compile(r"^ERROR: (\S+) \(([^)]+)\)")

    for line in lines:
        m = eligible_re.match(line)
        if m:
            eligible.add(m.group(1))
            continue
        m = skip_re.match(line)
        if m:
            project, reason = m.group(1), m.group(2)
            get_row(result.rows, project)["fix"] = Cell("SKIP", slugify_reason(reason))
            stats.skip += 1
            result.skip_reasons.append(SkipReason("fix", reason, project))
            continue
        m = retry_error_re.match(line)
        if m:
            project, reason = m.group(1), m.group(2)
            fix_results[project] = Cell("FAIL", slugify_reason(reason))
            result.warnings.append(Warning("fix", project, reason))
            continue
        m = retry_failed_re.match(line)
        if m:
            project = m.group(1)
            existing = fix_results.get(project, Cell("FAIL", "retry-failed"))
            existing.state = "FAIL"
            if not existing.reason:
                existing.reason = "retry-failed"
            fix_results[project] = existing
            continue
        m = fail_re.match(line)
        if m:
            project = m.group(2)
            reason = m.group(3) or m.group(1).lower()
            fix_results[project] = Cell("FAIL", slugify_reason(reason))
            result.warnings.append(Warning("fix", project, reason))
            continue
        m = ok_re.match(line)
        if m:
            project = m.group(1)
            # Don't downgrade an existing FAIL with a later OK in a different result block.
            if fix_results.get(project, Cell()).state != "FAIL":
                fix_results[project] = Cell("OK")
            continue
        m = FIX_DONE_RE.search(line)
        if m:
            stats.footer_ok = int(m.group(1))
            stats.footer_fail = int(m.group(2))
            stats.footer_total = int(m.group(4))

    for project in eligible:
        cell = fix_results.get(project)
        if cell is None:
            # Eligible but no result — try worktree on disk fallback.
            worktree = Path.home() / "rust" / f"{project}_style_fix" / "EVALUATION.md"
            if worktree.exists():
                text = worktree.read_text(errors="replace")
                if re.search(r"^## Fix Summary", text, re.MULTILINE):
                    cell = Cell("OK", "from-disk")
                else:
                    cell = Cell("FAIL", "no-fix-summary")
            else:
                cell = Cell("FAIL", "no-result")
        get_row(result.rows, project)["fix"] = cell
        if cell.state == "OK":
            stats.ok += 1
        elif cell.state == "FAIL":
            stats.fail += 1


def parse_log(path: Path) -> ParseResult:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    result = ParseResult(path=path)
    for phase in PHASES:
        result.stats[phase] = PhaseStats()

    if not lines:
        result.notes.append("empty log")
        return result

    # Run window.
    for line in lines:
        m = TS_RE.match(line)
        if m:
            result.run_start = m.group(1)
            break
    for line in lines:
        m = COMPLETE_RE.search(line)
        if m:
            result.elapsed = m.group(1)
            ts = TS_RE.match(line)
            if ts:
                result.run_end = ts.group(1)
            result.status = "complete"
            break
    if not result.run_end:
        # Look for a trailing timestamp on the last timestamped line.
        for line in reversed(lines):
            m = TS_RE.match(line)
            if m:
                result.run_end = m.group(1)
                break
        result.status = "in-progress" if result.run_start else "partial"

    bounds = detect_phase_boundaries(lines)
    if "clean" in bounds:
        s, e = bounds["clean"]
        parse_clean_phase(lines[s:e], result)
    if "warmup" in bounds:
        s, e = bounds["warmup"]
        parse_warmup_phase(lines[s:e], result)
    if "eval" in bounds:
        s, e = bounds["eval"]
        parse_eval_phase(lines[s:e], result)
    if "review" in bounds:
        s, e = bounds["review"]
        parse_review_phase(lines[s:e], result)
    if "fix" in bounds:
        s, e = bounds["fix"]
        parse_fix_phase(lines[s:e], result)

    # Status refinement: if fix header appeared but no per-project work, mark crashed.
    if "fix" in bounds and result.stats["fix"].ok == 0 and result.stats["fix"].fail == 0:
        if any("style-fix worktree script failed" in line for line in lines):
            result.status = "crashed"
            result.notes.append("style-fix script failed before per-project work")

    # End-of-run heads-up signals worth surfacing.
    for line in lines:
        if "SKIP commit-style-results:" in line:
            # The orchestrator left ~/rust/nate_style with uncommitted changes
            # because some worktree fix failed. The user must review and commit
            # (or discard) manually before the next run touches it.
            after = line.split("SKIP commit-style-results:", 1)[1].strip()
            result.notes.append(f"~/rust/nate_style left dirty: {after}")
            break

    # Partial logs: only fix phase present (style-fix-manual logs).
    phases_present = [p for p in PHASES if result.stats[p].present]
    absent = [p for p in PHASES if not result.stats[p].present]
    if phases_present == ["fix"]:
        result.status = "partial" if result.status != "complete" else "complete"
        result.notes.append(f"phases not in this log: {','.join(absent)}")

    _prune_bookkeeping_rows(result)
    return result


def _prune_bookkeeping_rows(result: ParseResult) -> None:
    """Move framework-bookkeeping rows out of the matrix into result.excluded.

    A row is bookkeeping-only if every non-dash cell is a SKIP whose original
    reason text is in BOOKKEEPING_REASONS. Track each project's earliest
    encountered reason for the summary line.
    """
    # Map (phase, project) -> original reason text from skip_reasons.
    reasons_by_project: dict[str, str] = {}
    for sr in result.skip_reasons:
        _ = reasons_by_project.setdefault(sr.project, sr.reason)

    to_remove: list[str] = []
    for project, row in result.rows.items():
        non_dash = [cell for cell in row.values() if cell.state != CELL_DASH]
        if not non_dash:
            continue
        if all(cell.state == "SKIP" for cell in non_dash):
            reason = reasons_by_project.get(project, "")
            if reason in ALWAYS_EXCLUDED_REASONS:
                to_remove.append(project)
                result.always_excluded.append((project, reason))
            elif reason in FRAMEWORK_FILTER_REASONS:
                to_remove.append(project)
                result.filtered_out.append((project, reason))

    for project in to_remove:
        del result.rows[project]

    # Drop bookkeeping skip reasons from commentary.
    pruned_projects = {p for p, _ in result.always_excluded} | {p for p, _ in result.filtered_out}
    result.skip_reasons = [
        sr
        for sr in result.skip_reasons
        if sr.project not in pruned_projects and sr.reason not in BOOKKEEPING_REASONS
    ]


def detect_current_phase(path: Path) -> tuple[str, str]:
    """Return (current_phase, latest_meaningful_line)."""
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    if not lines:
        return ("unknown", "")

    if any("=== Nightly Rust clean + rebuild complete" in line for line in lines):
        return ("done", lines[-1] if lines else "")

    # Walk from end backwards looking for the latest phase signal.
    for line in reversed(lines[-200:]):
        if FIX_HEADER_RE.search(line) or re.search(r"^Launched: \S+ \(PID", line):
            return ("style-fix", line)
        if EVAL_HEADER_RE.search(line) or "Launched:" in line and "via claude" in line:
            return ("style-eval", line)
        if REVIEW_HEADER_RE.search(line):
            return ("style-eval-review", line)
        if "WARMUP" in line:
            return ("warmup", line)
        if re.search(r"^\d{4}-\d{2}-\d{2}.*(CLEAN:|BUILD:|MEND:|DONE:)", line):
            return ("clean+rebuild", line)
    return ("unknown", lines[-1])


def format_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m {s % 60}s ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d {(s % 86400) // 3600}h ago"


def emit_full_report(result: ParseResult) -> None:
    mtime_age = format_age(time.time() - result.path.stat().st_mtime)
    print(f"PATH: {result.path}")
    print(f"MTIME_AGO: {mtime_age}")
    print(f"RUN_START: {result.run_start}")
    print(f"RUN_END: {result.run_end}")
    print(f"ELAPSED: {result.elapsed or '-'}")
    print(f"STATUS: {result.status}")
    print()

    for phase in PHASES:
        s = result.stats[phase]
        if not s.present:
            print(f"PHASE {phase} present=false")
            continue
        parts = [f"PHASE {phase}", "present=true"]
        if phase == "clean":
            parts.append(f"processed={s.processed}")
            parts.append(f"warnings={s.warnings}")
        parts.append(f"ok={s.ok}")
        parts.append(f"fail={s.fail}")
        parts.append(f"skip={s.skip}")
        if s.footer_total is not None:
            parts.append(f"footer_ok={s.footer_ok}")
            parts.append(f"footer_fail={s.footer_fail}")
            parts.append(f"footer_total={s.footer_total}")
        print(" ".join(parts))
    print()

    for project in sorted(result.rows.keys()):
        row = result.rows[project]
        cells = " ".join(f"{p}={row[p].render()}" for p in PHASES)
        print(f"ROW {project}  {cells}")
    print()

    def _emit_grouped(record: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        grouped_local: dict[str, list[str]] = {}
        for project, reason in items:
            grouped_local.setdefault(reason, []).append(project)
        for reason, projects in sorted(grouped_local.items()):
            rsafe = reason.replace('"', "'")
            joined = ",".join(sorted(projects))
            print(f'{record} "{rsafe}" count={len(projects)} projects={joined}')

    _emit_grouped("ALWAYS_EXCLUDED", result.always_excluded)
    _emit_grouped("FILTERED_OUT", result.filtered_out)

    for w in result.warnings:
        msg = w.message.replace('"', "'")
        print(f'WARNING {w.phase} {w.project} "{msg}"')

    for tw in result.tool_warnings:
        msg = tw.message.replace('"', "'")
        print(f'TOOL_WARNING {tw.phase} {tw.project} "{msg}"')

    # Aggregate remaining skip reasons by phase+reason (bookkeeping already pruned).
    grouped: dict[tuple[str, str], list[str]] = {}
    for sr in result.skip_reasons:
        grouped.setdefault((sr.phase, sr.reason), []).append(sr.project)
    for (phase, reason), projects in sorted(grouped.items()):
        rsafe = reason.replace('"', "'")
        joined = ",".join(sorted(projects))
        print(f'SKIP_REASON {phase} "{rsafe}" count={len(projects)} projects={joined}')

    for note in result.notes:
        print(f"NOTE {note}")


def emit_list() -> None:
    if not LOG_DIR.exists():
        print("ERROR: log directory missing")
        sys.exit(1)
    logs = sorted(LOG_DIR.glob("*.log"), key=_filename_ts_key, reverse=True)
    if not logs:
        print("ERROR: no logs found")
        sys.exit(1)
    now = time.time()
    for path in logs:
        ts = _filename_ts_key(path)
        try:
            ts_struct = time.strptime(ts, "%Y%m%d-%H%M%S")
            ts_epoch = time.mktime(ts_struct)
            age = format_age(now - ts_epoch)
        except ValueError:
            age = "unknown"
        result = parse_log(path)
        present = ",".join(p for p in PHASES if result.stats[p].present) or "none"
        print(
            f"LOG path={path} ts={ts} age={age} status={result.status} phases={present}"
        )


def emit_phase_detect(path: Path) -> None:
    if not path.exists():
        print(f"ERROR: log not found: {path}")
        sys.exit(1)
    phase, latest = detect_current_phase(path)
    print(f"PHASE {phase}")
    if latest:
        safe = latest.replace('"', "'")
        print(f'LATEST_EVENT "{safe}"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("path", nargs="?", help="log path; default = newest in ~/.local/logs/nightly/")
    _ = parser.add_argument("--list", action="store_true", help="list available logs with summaries")
    _ = parser.add_argument("--phase-detect", metavar="LOG", help="detect current phase of a log (for /monitor_nightly)")
    _ = parser.add_argument("--filter-regex", action="store_true", help="print live-monitor filter regex")
    args = parser.parse_args()

    filter_regex = cast(bool, getattr(args, "filter_regex"))
    list_mode = cast(bool, getattr(args, "list"))
    phase_detect = cast("str | None", getattr(args, "phase_detect"))
    path_arg = cast("str | None", getattr(args, "path"))

    if filter_regex:
        print(MONITOR_FILTER_REGEX)
        return

    if list_mode:
        emit_list()
        return

    if phase_detect is not None:
        emit_phase_detect(Path(phase_detect))
        return

    if path_arg is not None:
        log_path = Path(path_arg)
        if not log_path.exists():
            print(f"ERROR: log not found: {log_path}")
            sys.exit(1)
    else:
        found = find_newest_log()
        if not found:
            print("ERROR: no nightly logs available")
            sys.exit(1)
        log_path = found

    result = parse_log(log_path)
    emit_full_report(result)


if __name__ == "__main__":
    main()
