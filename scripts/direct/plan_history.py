"""Read durable plan phases, run history and progress-based ETA projections."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402
from scripts.direct.recorder_state import STATE_FILENAME as STATE_FILENAME  # noqa: E402


# How far the percentage beside an ETA is assumed to be off when no calibration
# history has an opinion. Ten points is the width the recorded phase estimates
# have shown; a run with enough matching samples replaces it with its own
# measured error, so this only ever governs the first runs of a fresh history.
DEFAULT_PERCENT_SPREAD = 10.0
# The widest the best and worst case may stray from the reported rate: at worst
# the work is half as far along as it says, at best twice.
RATE_FACTOR_LIMIT = 2.0
# Every phase heading in a worker plan, in all three forms it takes over a
# plan's life. `· status: todo` and `· status: done` are the live-zone forms;
# a shrunk or as-built phase drops the status marker and usually carries a
# commit annotation instead. Counting on the status marker alone silently
# ignores every already-archived phase, which is how a 67%-complete plan once
# reported 36%. Match the `Phase <id>` heading itself and classify after.
PHASE_HEADING_PATTERN = re.compile(
    r"^(?P<hashes>#{2,4})[ \t]+Phase[ \t]+(?P<id>\d+[a-z]?)\b(?P<rest>[^\n]*)$",
    re.MULTILINE,
)
PHASE_STATUS_PATTERN = re.compile(r"·[ \t]*status:[ \t]*(?P<status>todo|done)\b")
# `### Phase 12 Review` sits at the same heading level as a phase but is a
# section inside one. A real phase heading always names a title after an
# em-dash separator.
PHASE_TITLE_PATTERN = re.compile(r"^[ \t]*[—–-][ \t]*\S")
DEPENDS_ON_PATTERN = re.compile(r"^\*\*Depends on:\*\*[ \t]*(?P<list>[^\n]*)$", re.MULTILINE)
PHASE_REFERENCE_PATTERN = re.compile(
    r"\b(?i:Phase)[ \t]+(?P<single>\d+[a-z]?(?!\w|[-:./]\w))"
    + r"|\b(?i:Phases)[ \t]+(?P<list>\d+[a-z]?(?!\w|[-:./]\w)"
    + r"(?:[ \t]*(?:,[ \t]*(?:(?i:and)[ \t]+)?|(?i:and)[ \t]+|&[ \t]*)"
    + r"\d+[a-z]?(?!\w|[-:./]\w))*)",
)
UNIT_PHASE_PREFIX = re.compile(
    r"(?<![\w-])(?P<quote>`|'|\"|\*\*)?(?P<unit>[\w-]+-unit)"
    + r"(?:['’]s)?(?(quote)(?P=quote))(?:['’]s)?[ \t]+$"
)
PLAN_UNIT_HEADER = re.compile(
    r"^>[ \t]+\*\*Production:(?:\*\*)?[ \t]+[^\n]*?—[ \t]+unit[ \t]+`(?P<unit>[^`\n]+)`",
    re.MULTILINE,
)
BLOCKED_BY_PATTERN = re.compile(r"^\*\*Blocked by:\*\*[ \t]*(?P<gate>G\d+)", re.MULTILINE)


class PlanPhase(TypedDict):
    """One real phase heading, classified as the plan records it now."""

    id: str
    title: str
    done: bool
    # The phases its `**Depends on:**` line names; None when the line is absent.
    depends_on: list[str] | None
    # The gate its `**Blocked by:**` line names; empty when none.
    blocked_by: str


class EtaBand(NamedTuple):
    """Remaining seconds and the earliest and latest plausible arrivals."""

    remaining: int
    earliest: int
    latest: int


class EtaProjectionUnavailable(NamedTuple):
    """The reported progress cannot support an ETA projection."""


def history_root() -> Path:
    return unit_runs.history_root()


def now_epoch() -> float:
    configured = unit_runs.environ("NOW_EPOCH")
    if configured:
        return float(configured)
    return time.time()


def resolve_plan_path(working_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (working_dir / path).resolve()


def plan_phase_details(plan_path: Path) -> tuple[list[PlanPhase], list[str]]:
    text = plan_path.read_text(encoding="utf-8")
    unit_header = PLAN_UNIT_HEADER.search(text)
    phases: list[PlanPhase] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    headings = list(PHASE_HEADING_PATTERN.finditer(text))
    for position, match in enumerate(headings):
        rest = match.group("rest")
        status_match = PHASE_STATUS_PATTERN.search(rest)
        # `### Phase 12 Review` and its like are sections, not phases.
        if status_match is None and not PHASE_TITLE_PATTERN.match(rest):
            continue
        identifier = match.group("id")
        if identifier in seen:
            duplicates.append(identifier)
            continue
        seen.add(identifier)
        title_text = rest if status_match is None else rest[: status_match.start()]
        title_text = re.sub(r"^[ \t]*[—–-][ \t]*", "", title_text, count=1)
        title = re.sub(
            r"[ \t]+\((?:commit[ \t]+)?(?:`[0-9a-fA-F]{7,40}`|[0-9a-fA-F]{7,40})\)[ \t]*$",
            "",
            title_text,
        ).strip()
        # No status marker means the phase was shrunk into its as-built record,
        # which only happens after the phase completed.
        done = status_match is None or status_match.group("status") == "done"
        end = headings[position + 1].start() if position + 1 < len(headings) else len(text)
        body = text[match.end() : end]
        depends_match = DEPENDS_ON_PATTERN.search(body)
        depends_on: list[str] | None = None
        if depends_match is not None:
            depends_on = []
            dependency_text = depends_match.group("list")
            for reference in PHASE_REFERENCE_PATTERN.finditer(dependency_text):
                # A unit name qualifies only the phase span that follows it.
                qualifier = UNIT_PHASE_PREFIX.search(dependency_text[: reference.start()])
                if qualifier is not None and (
                    unit_header is None or qualifier.group("unit") != unit_header.group("unit")
                ):
                    continue
                phase_ids = reference.group("single") or reference.group("list")
                for dependency in phase_ids.replace(",", " ").replace("&", " ").split():
                    if dependency.lower() != "and" and dependency not in depends_on:
                        depends_on.append(dependency)
        blocked_match = BLOCKED_BY_PATTERN.search(body)
        phases.append(
            PlanPhase(
                id=identifier,
                title=title,
                done=done,
                depends_on=depends_on,
                blocked_by="" if blocked_match is None else blocked_match.group("gate"),
            )
        )
    return phases, duplicates


def plan_phases(plan_path: Path) -> list[PlanPhase]:
    """Return the plan's unique phase headings in document order."""
    phases, _ = plan_phase_details(plan_path)
    return phases


def run_started_event(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    try:
        parsed = cast(object, json.loads(first_line))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    event = cast(dict[str, object], parsed)
    if event.get("event_type") != "run_started":
        return None
    return event


def plan_runs(plan_path: Path) -> list[Path]:
    """Return this plan's durable run files, oldest first."""
    target = plan_path.expanduser().resolve()
    try:
        paths = list((history_root() / "runs").glob("*.jsonl"))
    except OSError:
        return []
    matches: list[tuple[float, str, Path]] = []
    for path in paths:
        event = run_started_event(path)
        if event is None:
            continue
        working_dir = event.get("working_dir")
        plan_doc = event.get("plan_doc")
        if not isinstance(working_dir, str) or not isinstance(plan_doc, str):
            continue
        if not working_dir or not plan_doc:
            continue
        if resolve_plan_path(Path(working_dir), plan_doc) != target:
            continue
        started_at = 0.0
        for value in (event.get("run_started_at"), event.get("timestamp_epoch")):
            if isinstance(value, int | float) and not isinstance(value, bool):
                started_at = float(value)
                break
        matches.append((started_at, path.name, path))
    return [path for _, _, path in sorted(matches)]


def eta_seconds(percent: int, elapsed: int) -> int | None:
    """Project the time left by extending the rate the clock has run so far.

    Percent over elapsed is the only rate both scopes share. For the project the
    percent comes from the plan's phase counts, so this is exactly "mean time per
    phase so far, times the phases left"; for the phase it inherits whatever
    accuracy the reporter's estimate has and claims nothing more. Both are read
    off the number the same line displays, capped value included, so the estimate
    never contradicts the percentage sitting beside it.
    """
    if percent <= 0 or percent >= 100 or elapsed <= 0:
        return None
    return int(elapsed * (100 - percent) / percent)


def eta_band_seconds(
    percent: int, elapsed: int, spread: float
) -> EtaBand | EtaProjectionUnavailable:
    """Return the projected remaining seconds and its optimistic/pessimistic band."""
    eta = eta_seconds(percent, elapsed)
    if eta is None:
        return EtaProjectionUnavailable()
    optimistic = min(99.0, percent * RATE_FACTOR_LIMIT, percent + max(0.0, spread))
    pessimistic = max(1.0, percent / RATE_FACTOR_LIMIT, percent - max(0.0, spread))
    low = int(elapsed * (100.0 - optimistic) / optimistic)
    high = int(elapsed * (100.0 - pessimistic) / pessimistic)
    return EtaBand(remaining=eta, earliest=low, latest=high)


def percent_spread(calibration: dict[str, object] | None) -> float:
    """How far off the percentage has actually run, when history can say.

    A calibration that cleared its sample floor has measured this reporter's
    error around this percentage against phases that finished, which beats any
    constant. Below the floor the measurement is noise wearing a number, so the
    default stands.
    """
    if calibration is None or calibration.get("apply_suggestion") is not True:
        return DEFAULT_PERCENT_SPREAD
    measured = calibration.get("median_raw_absolute_error_percentage_points")
    if not isinstance(measured, int | float):
        return DEFAULT_PERCENT_SPREAD
    return max(1.0, float(measured))

