"""Read repair dispatches without changing the stored run records.

The pure API takes parsed events. Phase boundaries are the recorded phase
start and finish (zero means no finish); pass boundaries and span are separate.
Fix seconds are agent time, so concurrent workers each contribute their time.
An unfinished pass has no observed interval: its start counts, but cannot
extend a round across later work whose overlap is unknown.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics
from typing import Literal, TypedDict, cast

from scripts.history.history import (
    DEFAULT_STATUS, SPINE_VERSION, _arg, _duration, _emit, _events,
    _integer, _meta, _number, _require_rows, _since_epoch, _skills,
    _status_filter, _string, _warn,
)

# A repair activity that names a failed build or check is a gate repair.
GATE_WORDS = ("test", "tests", "lint", "clippy", "basedpyright", "type check", "gate", "failing", "broke")
# Merge work includes conflict resolution and moving a change between branches.
MERGE_WORDS = ("merge", "conflict", "port", "rebase")
# These completed activities can cause a live repair for the next 30 minutes.
LIVE_WORDS = ("live", "shot", "smoke", "ux review")
# Merge rounds more than one day apart belong to different merges.
MERGE_SEPARATION_SECONDS = 24 * 3600
CAUSES = ("review-ux", "review-code", "gate", "merge", "live", "other")
Cause = Literal["review-ux", "review-code", "gate", "merge", "live", "other"]


class Round(TypedDict):
    """One repair dispatch, including concurrent workers and its ledger batch."""

    round: int
    started_at: float
    finished_at: float
    cause: Cause
    fix_seconds: float
    passes: int
    fix_passes: list[int]
    activities: list[str]
    covered: list[str]


class PhaseRounds(TypedDict):
    """One phase instance with recorded phase and observed pass boundaries."""

    run_id: str
    phase_id: str
    phase_instance_id: str
    title: str
    plan: str
    started_at: float
    finished_at: float
    first_pass_at: float
    last_pass_at: float
    span_seconds: float
    status: str
    rounds: list[Round]
    causes: dict[str, int]
    findings: int
    reviews: int
    fix_seconds: float
    check: Literal["ok", "mixed"]
    past_landing: int


class RepairPass(TypedDict):
    """A pass with its start and finish joined, counted once."""

    kind: str
    started_at: float
    finished_at: float
    seconds: float
    fix_pass: int
    activity: str
    status: str


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in cast(list[object], value) if isinstance(item, str)]
    return []


def _stamp(event: dict[str, object]) -> float:
    return _number(event.get("timestamp_epoch"))


def _pass_start(event: dict[str, object]) -> float:
    return _number(
        event.get("pass_started_at"),
        _stamp(event) - _number(event.get("pass_elapsed_seconds")),
    )


def _passes(events: list[dict[str, object]]) -> list[RepairPass]:
    joined: dict[tuple[str, ...], RepairPass] = {}
    # Finishes carry the final status and elapsed time, even if input is unsorted.
    for event in sorted(events, key=lambda event: _string(event.get("event_type")) == "pass_finished"):
        event_type = _string(event.get("event_type"))
        if event_type not in {"pass_started", "pass_finished"}:
            continue
        started = _pass_start(event)
        instance = _string(event.get("pass_instance_id"))
        key = (instance,) if instance else (
            _string(event.get("team_slot")), _string(event.get("pass_kind")),
            str(started), str(event.get("fix_pass", "")),
        )
        finished = event_type == "pass_finished"
        seconds = max(0.0, _number(event.get("pass_elapsed_seconds")))
        joined[key] = RepairPass(
            kind=_string(event.get("pass_kind")), started_at=started,
            finished_at=max(started, _number(event.get("timestamp_epoch"), started + seconds)) if finished else 0.0,
            seconds=seconds if finished else 0.0,
            fix_pass=_integer(event.get("fix_pass")),
            activity=_string(event.get("pass_activity")),
            status=_string(event.get("status"), "completed" if finished else "open"),
        )
    return sorted(joined.values(), key=lambda repair: (repair["started_at"], repair["finished_at"] == 0.0))


def _has_words(text: str, words: tuple[str, ...]) -> bool:
    for word in words:
        variants = (word, word + "s", word + "d" if word.endswith("e") else word + "ed",
                    word.removesuffix("e") + "ing")
        if re.search(r"\b(?:" + "|".join(re.escape(variant) for variant in variants) + r")\b", text, re.IGNORECASE):
            return True
    return False


def _rounds(events: list[dict[str, object]], passes: list[RepairPass]) -> list[Round]:
    batches: list[dict[str, object]] = []
    ux_findings: set[str] = set()
    live_finishes: list[float] = []
    for event in events:
        kind = _string(event.get("event_type"))
        if kind == "finding_batch_dispatched":
            batches.append(event)
        elif kind == "finding_opened" and any("ux" in lens.lower() for lens in _strings(event.get("lens"))):
            ux_findings.add(_string(event.get("finding_id")))
        elif kind == "activity_finished" and _has_words(
            _string(event.get("activity_label")) + " " + _string(event.get("activity_text")), LIVE_WORDS,
        ):
            live_finishes.append(_stamp(event))
    live_finishes.sort()
    groups: list[list[RepairPass]] = []
    open_until = -1.0
    for repair in passes:
        if repair["kind"] != "fix":
            continue
        if not groups or repair["started_at"] >= open_until:
            groups.append([])
            open_until = -1.0
        groups[-1].append(repair)
        if repair["finished_at"]:
            open_until = max(open_until, repair["finished_at"])

    group_batches: dict[int, list[dict[str, object]]] = defaultdict(list)
    unmatched_batches: list[dict[str, object]] = []
    # Each dispatch belongs to the first eligible pass, even within an open group.
    for batch in sorted(batches, key=_stamp):
        number = _integer(batch.get("round"))
        dispatched = _stamp(batch)
        for index, group in enumerate(groups):
            if any(repair["fix_pass"] == number and repair["started_at"] >= dispatched for repair in group):
                group_batches[index].append(batch)
                break
        else:
            unmatched_batches.append(batch)

    result: list[Round] = []
    for index, group in enumerate(groups):
        ledger = group_batches[index]
        numbers = sorted({repair["fix_pass"] for repair in group})
        covered = sorted({finding for batch in ledger for finding in _strings(batch.get("covered"))})
        activities = [repair["activity"] for repair in group]
        text = " ".join(activities)
        started = group[0]["started_at"]
        live_index = bisect_right(live_finishes, started) - 1
        cause: Cause = "other"
        if ledger:
            cause = "review-ux" if ux_findings.intersection(covered) else "review-code"
        elif _has_words(text, GATE_WORDS):
            cause = "gate"
        elif _has_words(text, MERGE_WORDS):
            cause = "merge"
        elif live_index >= 0 and started - live_finishes[live_index] <= 1800:
            cause = "live"
        result.append(Round(
            round=0, started_at=started,
            finished_at=max(repair["finished_at"] for repair in group) if all(repair["finished_at"] for repair in group) else 0.0,
            cause=cause, fix_seconds=sum(repair["seconds"] for repair in group),
            passes=len(group), fix_passes=numbers, activities=activities, covered=covered,
        ))
    # A recorded dispatch is work even when its worker events are absent.
    for batch in unmatched_batches:
        covered = sorted(set(_strings(batch.get("covered"))))
        result.append(Round(
            round=0, started_at=_stamp(batch), finished_at=0.0,
            cause="review-ux" if ux_findings.intersection(covered) else "review-code",
            fix_seconds=0.0, passes=0, fix_passes=[_integer(batch.get("round"))], activities=[], covered=covered,
        ))
    result.sort(key=lambda repair: repair["started_at"])
    for number, repair in enumerate(result, 1):
        repair["round"] = number
    return result


def phase_rounds(events: list[dict[str, object]]) -> list[Round]:
    """Build repair rounds from events belonging to one phase instance."""
    return _rounds(events, _passes(events))


def load_phases(events: list[dict[str, object]]) -> list[PhaseRounds]:
    """Join phase instances, including ledger events without a run_id.

    Input order does not matter. A ledger instance must identify a single run;
    an ambiguous or unknown instance is never joined to an unrelated phase.
    """
    runs: dict[str, dict[str, object]] = {}
    starts: dict[tuple[str, str], dict[str, object]] = {}
    owners: dict[str, set[str]] = defaultdict(set)
    for event in events:
        run_id = _string(event.get("run_id"))
        instance = _string(event.get("phase_instance_id"))
        kind = _string(event.get("event_type"))
        if kind == "run_started" and run_id:
            runs[run_id] = event
        if run_id and instance and kind == "phase_started":
            owners[instance].add(run_id)
            starts[(run_id, instance)] = event
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for event in events:
        instance = _string(event.get("phase_instance_id"))
        if not instance:
            continue
        run_id = _string(event.get("run_id"))
        if not run_id and len(owners[instance]) == 1:
            run_id = next(iter(owners[instance]))
        if (run_id, instance) in starts:
            grouped[(run_id, instance)].append(event)

    phases: list[PhaseRounds] = []
    for (run_id, instance), phase_events in sorted(grouped.items()):
        seed = starts[(run_id, instance)]
        phase_id = str(seed.get("phase_id", ""))
        passes = _passes(phase_events)
        rounds = _rounds(phase_events, passes)
        first = min((repair["started_at"] for repair in passes), default=0.0)
        last = max((repair["finished_at"] or repair["started_at"] for repair in passes), default=first)
        finishes = [event for event in phase_events if _string(event.get("event_type")) == "phase_finished"]
        finish = max(finishes, key=_stamp) if finishes else {}
        phase_start = _number(seed.get("phase_started_at"), _stamp(seed))
        merges = [
            repair["started_at"]
            for repair in rounds if repair["cause"] == "merge"
        ]
        wrong_phase = any(
            str(int(named.group(1))) != phase_id
            for repair in passes
            for named in re.finditer(r"\bphase\s+(\d+)\b", repair["activity"], re.IGNORECASE)
        )
        phases.append(PhaseRounds(
            run_id=run_id, phase_id=phase_id, phase_instance_id=instance,
            title=_string(seed.get("phase_title")),
            plan=Path(_string(runs.get(run_id, {}).get("project_plan_doc"), _string(seed.get("project_plan_doc")))).name,
            started_at=phase_start, finished_at=_stamp(finish),
            first_pass_at=first, last_pass_at=last, span_seconds=max(0.0, last - first),
            status=_string(finish.get("status"), "open"), rounds=rounds,
            causes={cause: sum(repair["cause"] == cause for repair in rounds) for cause in CAUSES},
            findings=sum(_string(event.get("event_type")) == "finding_opened" for event in phase_events),
            reviews=sum(
                _string(event.get("event_type")) == "pass_started"
                and _string(event.get("pass_kind")) == "review"
                and _string(event.get("run_id")) == run_id
                for event in phase_events
            ),
            fix_seconds=sum(repair["fix_seconds"] for repair in rounds),
            check="mixed" if wrong_phase or last - first > 72 * 3600
            or (bool(merges) and max(merges) - min(merges) > MERGE_SEPARATION_SECONDS) else "ok",
            past_landing=max(0, len(rounds) - 3),
        ))
    return phases


def run(args: argparse.Namespace) -> None:
    """Print the filtered round report; the pure API above does no I/O."""
    stored, tally = _events(_skills(args))
    events: list[dict[str, object]] = []
    for _skill, event in stored:
        if _integer(event.get("spine_version")) > SPINE_VERSION:
            tally["future_spine"] += 1
        else:
            events.append(event)
    since = _since_epoch(_arg(args, "since"))
    until_text = _arg(args, "until")
    until = _since_epoch(until_text) if until_text else float("inf")
    statuses = _status_filter(_arg(args, "status", DEFAULT_STATUS))
    plan_filter = _arg(args, "plan").lower()
    phases = [
        phase for phase in load_phases(events)
        if since <= phase["started_at"] <= until
        and (statuses is None or phase["status"] in statuses)
        and plan_filter in phase["plan"].lower()
    ]
    phases.sort(key=lambda phase: (-len(phase["rounds"]), -phase["fix_seconds"], phase["run_id"], phase["phase_instance_id"]))
    top_value: object = getattr(args, "top", 25)
    top = max(0, top_value) if isinstance(top_value, int) else 25
    shown = phases[:top]
    headers = ["run", "phase", "title", "plan", "reviewed", "rounds", *CAUSES, "findings", "fix-time", "span", "check"]
    table = [
        [phase["run_id"][:8], phase["phase_id"], phase["title"][:40], phase["plan"],
         "yes" if phase["reviews"] else "no",
         str(len(phase["rounds"])), *(str(phase["causes"][cause]) for cause in CAUSES),
         str(phase["findings"]), _duration(phase["fix_seconds"]), _duration(phase["span_seconds"]), phase["check"]]
        for phase in shown
    ]
    count = len(phases)
    reviewed = [phase for phase in phases if phase["reviews"] > 0]
    reviewed_count = len(reviewed)
    heavy = [phase for phase in reviewed if len(phase["rounds"]) >= 3]
    fix_seconds = sum(phase["fix_seconds"] for phase in phases)
    reviewed_fix_seconds = sum(phase["fix_seconds"] for phase in reviewed)
    heavy_seconds = sum(phase["fix_seconds"] for phase in heavy)
    cause_counts = {cause: sum(phase["causes"][cause] for phase in phases) for cause in CAUSES}
    total_rounds = sum(cause_counts.values())
    shares = {cause: 100.0 * cause_counts[cause] / total_rounds if total_rounds else 0.0 for cause in CAUSES}
    median = statistics.median([len(phase["rounds"]) for phase in reviewed]) if reviewed else 0.0
    heavy_share = 100.0 * len(heavy) / reviewed_count if reviewed_count else 0.0
    fix_share = 100.0 * heavy_seconds / reviewed_fix_seconds if reviewed_fix_seconds else 0.0
    mixed = sum(phase["check"] == "mixed" for phase in phases)
    meta = _meta(tally)
    meta.update(phases=count, reviewed_phases=reviewed_count, unreviewed_phases=count - reviewed_count,
                median_rounds=median, heavy_phases=len(heavy),
                heavy_phase_share=heavy_share, heavy_fix_seconds=heavy_seconds,
                heavy_fix_share=fix_share, fix_seconds=fix_seconds, reviewed_fix_seconds=reviewed_fix_seconds,
                rounds=total_rounds, causes=cause_counts, cause_shares=shares, mixed=mixed)
    form = _arg(args, "format", "table")
    if form == "json":
        # Keep the table columns and add the full typed records for API users.
        rows: list[dict[str, object]] = []
        for phase, cells in zip(shown, table, strict=True):
            row: dict[str, object] = dict(zip(headers, cells, strict=True))
            row.update(phase)
            row["reviewed"] = phase["reviews"] > 0
            row["title"] = phase["title"][:40]
            row["round_details"] = phase["rounds"]
            row["rounds"] = len(phase["rounds"])
            rows.append(row)
        print(json.dumps({"_meta": meta, "rows": rows}, indent=2))
    else:
        _emit(table, headers, form, meta)
        if form == "table":
            print(f"\n{count} phases counted; {reviewed_count} reviewed; median {median:g} rounds over reviewed phases")
            print(f"{count - reviewed_count} phases with no review recorded (not in the median or shares)")
            print(f"{len(heavy)} reviewed phases with >=3 rounds ({heavy_share:.1f}%) hold {fix_share:.1f}% of reviewed fix time")
            print("Rounds by cause: " + ", ".join(f"{cause} {shares[cause]:.1f}%" for cause in CAUSES))
            print(f"{mixed} mixed records")
    _warn(tally)
    _require_rows(table, "phases")
