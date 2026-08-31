#!/usr/bin/env python3
"""Query stage timings across skill run histories.

Stores are discovered, not configured: any `<state root>/<skill>/runs/*.jsonl`
is a store, and the directory name is the skill unless an event names its own.
A skill becomes queryable by writing events there; nothing registers it here.

Every reader rule for versions lives in `_spine_of` and `_normalize`. Events
predating the spine carry no `spine_version` and read as spine 0; an event from
a spine newer than SPINE_VERSION is counted and reported, never interpreted,
because a silent misread of a newer log is the failure versioning exists to
prevent. The skip happens in `_normalize`, not in `_events`, so `versions` —
the one command whose job is to report a newer spine — still sees them.

`--format json` emits `{"_meta": {...}, "rows": [...]}` and `--format csv`
appends `_meta,<key>,<value>` rows, so a machine consumer reads the same
provenance counts a terminal reader gets on stderr.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast


SPINE_VERSION = 1

# Delegate pass kinds and activity labels onto the stage vocabulary the spine
# defines. Any label not listed is `other` rather than being forced into a
# stage: an unrecognised activity is unclassified data, not a final step.
PASS_STAGE: dict[str, str] = {
    "impl": "implementation",
    "test": "test",
    "fix": "fix",
    "review": "review",
    # Escalation is implementation, not review: it is ambiguous architecture,
    # transform mathematics, or a failed behavioral attempt, dispatched
    # alongside implementation and fix. Real architecture review is 1085 passes
    # of kind `review` carrying task `delegate.architect`.
    #
    # `arch` is a retired kind, kept forever because retiring one cannot rewrite
    # events already written and dropping it would send the back corpus to
    # `other`. It meant "escalated implementation", which was the agent tier
    # leaking into the work kind: `delegate.escalation` runs under `fix` 917
    # times and under `arch` only 172, so the same escalation was encoded two
    # ways depending on the phase. The tier lives on `called_task` for every
    # kind -- group by `stage,task` to separate escalated work from ordinary.
    "arch": "implementation",
}
ACTIVITY_STAGE: dict[str, str] = {
    "verification": "test",
    "verify": "test",
    "test": "test",
    "tests": "test",
    "smoke": "test",
    "shrink": "final",
    "closeout": "final",
    "style": "final",
    "plan update": "final",
    "plan review": "final",
    "phase review": "final",
    "synthesis": "final",
    "triage": "final",
}
STAGE_ORDER: list[str] = ["implementation", "review", "fix", "test", "final", "other"]
GROUP_KEYS: list[str] = [
    "skill",
    "stage",
    "slot",
    "label",
    "agent",
    "task",
    "run",
    "branch",
    "worktree",
    "phase",
]

# `--since` units, matched case-sensitively so `m` is minutes and `M` months.
# A case-insensitive table read `3M` as three minutes, off by four orders of
# magnitude with nothing on screen to say so.
SINCE_UNITS: dict[str, float] = {
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
    "m": 60.0,
    "M": 2592000.0,
}

# 168 finished rows holding 21.1h are canceled, error, or interrupted. Reading
# them as work done overstates every total, so a query keeps `completed` unless
# it asks otherwise.
DEFAULT_STATUS = "completed"

# A phase past this many fix passes is the reading rule the `phases` summary
# reports against; the share of fix time it holds is computed, not assumed.
FIX_PASS_THRESHOLD = 4

# Below this many samples in a cell, a median moves more from resampling than
# from any change being compared.
READABLE_SAMPLES = 80

# Two medians closer than this factor are one median seen twice. Used by
# `compare` to refuse a verdict rather than report a difference that a rerun of
# the same configuration would produce on its own.
NOISE_RATIO = 1.5


class StageRow(TypedDict):
    skill: str
    spine: int
    schema: int
    run: str
    branch: str
    worktree: str
    phase: str
    stage: str
    label: str
    task: str
    agent: str
    slot: str
    result: str
    status: str
    started_at: float
    seconds: int


class RunRow(TypedDict):
    skill: str
    run: str
    branch: str
    started_at: float
    finished: bool
    wall_seconds: int
    stage_seconds: int
    stages: int
    phases: int


class PhaseRow(TypedDict):
    run: str
    phase: str
    reviews: int
    fixes: int
    impl_seconds: int
    fix_seconds: int
    total_seconds: int


class Cell(TypedDict):
    n: int
    median: float
    p90: float
    total: int


def _state_root() -> Path:
    configured = os.environ.get("SKILL_HISTORY_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state"


def _stores() -> list[tuple[str, Path]]:
    root = _state_root()
    if not root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(root.iterdir()):
        runs = child / "runs"
        if runs.is_dir() and glob.glob(str(runs / "*.jsonl")):
            found.append((child.name, runs))
    return found


def _object(text: str) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def _string(value: object, default: str = "") -> str:
    """A non-empty string, or the default.

    An empty string is absence, not a value: one event writes `"branch": ""`
    and would otherwise erase a run's known branch, and `"skill": ""` would
    print nameless instead of falling back to the store it was read from.
    """
    return value if isinstance(value, str) and value else default


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _spine_of(event: dict[str, object]) -> int:
    """Spine version of one event; absent means the pre-spine corpus."""
    return _integer(event.get("spine_version"), 0)


def _agent_label(event: dict[str, object], key: str) -> str:
    """One agent identity as a label, from a `{family, model, effort}` dict.

    A recorder that writes the model as a bare string is read as-is: the spine
    names the field, not what type it holds, and a skill instrumented later has
    no reason to build a dict to say one word.
    """
    value = event.get(key)
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    identity = cast(dict[str, object], value)
    family = _string(identity.get("family"))
    model = _string(identity.get("model"))
    effort = _string(identity.get("effort"))
    if not model:
        return ""
    label = f"{family}/{model}" if family else model
    return f"{label}:{effort}" if effort and effort != "unset" else label


def _events(skills: set[str]) -> tuple[list[tuple[str, dict[str, object]]], dict[str, int]]:
    """Every event from every matching store, with a tally of what was skipped."""
    collected: list[tuple[str, dict[str, object]]] = []
    tally: dict[str, int] = {"unreadable": 0, "future_spine": 0, "unknown_stage": 0}
    stores = _stores()
    known = {name for name, _ in stores}
    missing = sorted(skills - known)
    if missing:
        # An unmatched --skill used to print an empty table and exit 0, which
        # reads as "no history" rather than "no such store".
        available = ", ".join(sorted(known)) if known else "none discovered"
        raise SystemExit(f"No history store named {', '.join(missing)}; stores are {available}")
    for store_skill, runs in stores:
        if skills and store_skill not in skills:
            continue
        for path in sorted(glob.glob(str(runs / "*.jsonl"))):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                tally["unreadable"] += 1
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                event = _object(line)
                if event is None:
                    tally["unreadable"] += 1
                    continue
                collected.append((store_skill, event))
    return collected, tally


def _head_start(events: list[tuple[str, dict[str, object]]]) -> dict[str, int]:
    """Seconds an adopted early review ran before its pass was recorded started.

    An early review armed speculatively and disarmed with reason `adopted`
    became the review pass itself: all 70 adopted arms in the corpus join a
    completed review pass by `pass_instance_id`, and the arm timestamp sits
    exactly `early_review_elapsed_seconds` before the pass start. Nothing else
    reports those 13.6h, so arming reviews earlier read as faster reviews.
    """
    lead: dict[str, int] = {}
    for _store_skill, event in events:
        if _string(event.get("event_type")) != "early_review_disarmed":
            continue
        if _string(event.get("reason")) != "adopted":
            continue
        instance = _string(event.get("pass_instance_id"))
        if not instance:
            continue
        lead[instance] = lead.get(instance, 0) + _integer(event.get("early_review_elapsed_seconds"))
    return lead


def _normalize(
    store_skill: str,
    event: dict[str, object],
    tally: dict[str, int],
    head_start: dict[str, int],
) -> StageRow | None:
    """One finished stage, or None when the event is not one.

    Only `*_finished` events are read: each carries its own elapsed seconds and
    the full run, phase, and agent context, so no start/finish pairing is
    needed and an interrupted stage is simply absent.
    """
    if _spine_of(event) > SPINE_VERSION:
        tally["future_spine"] += 1
        return None
    event_type = _string(event.get("event_type"))
    if event_type == "pass_finished":
        kind = _string(event.get("pass_kind"))
        stage = PASS_STAGE.get(kind, "other")
        label = kind
        seconds = _integer(event.get("pass_elapsed_seconds"))
        started = _number(event.get("pass_started_at"))
        agent = _agent_label(event, "called_agent")
        task = _string(event.get("called_task"))
        result = ""
        # The adopted early review is folded into the pass it became rather
        # than emitted as its own row: it is one round, and a second row would
        # add 70 phantom reviews to the counts `phases` reports.
        lead = head_start.get(_string(event.get("pass_instance_id")), 0)
        if lead:
            seconds += lead
            started = started - lead if started > 0 else started
    elif event_type == "activity_finished":
        label = _string(event.get("activity_label")).strip().lower()
        stage = ACTIVITY_STAGE.get(label, "other")
        seconds = _integer(event.get("activity_elapsed_seconds"))
        started = _number(event.get("activity_started_at"))
        # 246 of these are label-less dispatches carrying the called agent and
        # task; `main_agent` on them is the orchestrator that dispatched the
        # work, not whoever did it.
        agent = _agent_label(event, "called_agent") or _agent_label(event, "main_agent")
        task = _string(event.get("called_task"))
        result = _string(event.get("activity_result"))
    elif event_type == "stage_finished":
        # The spine's own event, for skills instrumented after delegate. It
        # arrives unvalidated, so every field is coerced the way the branches
        # above build theirs.
        label = _string(event.get("label")).strip().lower()
        stage = _string(event.get("stage"), "other")
        if stage not in STAGE_ORDER:
            tally["unknown_stage"] += 1
            stage = "other"
        seconds = _integer(event.get("elapsed_seconds"))
        started = _number(event.get("started_at"))
        agent = _agent_label(event, "agent")
        task = _string(event.get("task"))
        result = _string(event.get("result"))
    else:
        return None
    if started <= 0.0:
        # The finish timestamp minus elapsed, never the bare finish: the same
        # 246 label-less dispatches carry no `*_started_at`, and the finish
        # stamp files each one a whole duration late in a `--since` window.
        started = _number(event.get("timestamp_epoch")) - float(seconds)
    return StageRow(
        skill=_string(event.get("skill"), store_skill),
        spine=_spine_of(event),
        schema=_integer(event.get("schema_version")),
        run=_string(event.get("run_id")),
        branch=_string(event.get("branch")),
        worktree=_string(event.get("worktree")),
        phase=_string(event.get("phase_id")),
        stage=stage,
        label=label,
        task=task,
        agent=agent,
        # Empty on every pre-2026-08-31 pass and on any recorder outside
        # implement.sh, so it groups as "-" rather than claiming a slot.
        # The one-day-old "team_role" spelling is not read: every event
        # written under it carried an empty value, so nothing is lost.
        slot=_string(event.get("team_slot")),
        result=result,
        status=_string(event.get("status")),
        started_at=started,
        seconds=seconds,
    )


def _since_epoch(value: str) -> float:
    if not value:
        return 0.0
    text = value.strip()
    suffix = text[-1:]
    digits = text[:-1]
    if suffix.isalpha() and digits.replace(".", "", 1).isdigit():
        if suffix not in SINCE_UNITS:
            units = ", ".join(SINCE_UNITS)
            raise SystemExit(f"Unreadable --since value {value!r}: unit must be one of {units}")
        return datetime.now(UTC).timestamp() - float(digits) * SINCE_UNITS[suffix]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SystemExit(f"Unreadable --since value {value!r}: {error}") from error
    if parsed.tzinfo is None:
        # A bare date means local midnight, matching the local stamps `runs`
        # prints; reading it as UTC shifted the window by the offset.
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _status_filter(value: str) -> set[str] | None:
    """Statuses a row must carry, or None when every status is wanted.

    `completed` also admits an empty status: a recorder that writes none has
    not reported a failure, and the spine does not require the field.
    """
    wanted = {name.strip().lower() for name in value.split(",") if name.strip()}
    if not wanted or "all" in wanted:
        return None
    if "completed" in wanted:
        wanted.add("")
    return wanted


def _percentile(values: list[float], percentile: float) -> float:
    """Nearest-rank percentile.

    Rounding the index put p90 below the top sample for small n — n=6 landed on
    index 4, not 5 — because Python rounds a half to even. Ceiling the rank
    instead means p90 equals the maximum for any n below 10, which is what a
    ninetieth percentile of nine samples amounts to.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(percentile / 100.0 * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, rank))]


def _cell(seconds: list[int]) -> Cell:
    values = [float(second) for second in seconds]
    return Cell(
        n=len(values),
        median=statistics.median(values) if values else 0.0,
        p90=_percentile(values, 90.0),
        total=int(sum(values)),
    )


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, second = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{second:02d}s"
    return f"{second}s"


def _count(value: float, singular: str, plural: str) -> str:
    """A round count without its trailing zero; a median of two keeps its half."""
    rendered = str(int(value)) if float(value).is_integer() else f"{value:g}"
    return f"{rendered} {singular if value == 1 else plural}"


def _stamp(epoch: float) -> str:
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def _meta(tally: dict[str, int]) -> dict[str, object]:
    return {key: value for key, value in tally.items() if value}


def _emit(
    rows: list[list[str]],
    headers: list[str],
    form: str,
    meta: dict[str, object] | None = None,
) -> None:
    facts = meta or {}
    if form == "json":
        payload = {"_meta": facts, "rows": [dict(zip(headers, row, strict=True)) for row in rows]}
        print(json.dumps(payload, indent=2))
        return
    if form == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(rows)
        writer.writerows(["_meta", key, str(value)] for key, value in facts.items())
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    def line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()
    print(line(headers))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(line(row))


def _skills(args: argparse.Namespace) -> set[str]:
    return {name.strip() for name in _arg(args, "skill").split(",") if name.strip()}


def _rows(
    args: argparse.Namespace,
    events: list[tuple[str, dict[str, object]]],
    tally: dict[str, int],
) -> list[StageRow]:
    since = _since_epoch(_arg(args, "since"))
    statuses = _status_filter(_arg(args, "status", DEFAULT_STATUS))
    stage_filter = {name.strip() for name in _arg(args, "stage").split(",") if name.strip()}
    label_filter = _arg(args, "label").strip().lower()
    agent_filter = _arg(args, "agent").strip().lower()
    head_start = _head_start(events)
    rows: list[StageRow] = []
    for store_skill, event in events:
        row = _normalize(store_skill, event, tally, head_start)
        if row is None or row["started_at"] < since:
            continue
        if statuses is not None and row["status"] not in statuses:
            continue
        if stage_filter and row["stage"] not in stage_filter:
            continue
        if label_filter and label_filter not in row["label"].lower():
            continue
        if agent_filter and agent_filter not in row["agent"].lower():
            continue
        rows.append(row)
    return rows


def _arg(args: argparse.Namespace, name: str, default: str = "") -> str:
    value: object = getattr(args, name, default)
    return value if isinstance(value, str) else default


def _group_keys(spec: str) -> list[str]:
    keys = [key.strip() for key in spec.split(",") if key.strip()]
    for key in keys:
        if key not in GROUP_KEYS:
            raise SystemExit(f"Unknown --group key {key!r}; choose from {', '.join(GROUP_KEYS)}")
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        # `--group stage,stage` printed two identical columns but collapsed to
        # one key in JSON, so the two formats disagreed about the same query.
        raise SystemExit(f"Repeated --group key {', '.join(repeated)}")
    return keys


def _group_of(row: StageRow, keys: list[str]) -> tuple[str, ...]:
    """The grouping key, full width.

    Every string carries `-` when empty: an empty `label` is the largest real
    `other` bucket and printed as a nameless row. Runs group on the whole id
    and shorten only at render, so two runs sharing eight hex characters stay
    two runs.
    """
    field: dict[str, str] = {
        "skill": row["skill"] or "-",
        "stage": row["stage"] or "-",
        "label": row["label"] or "-",
        "agent": row["agent"] or "-",
        "slot": row["slot"] or "-",
        "task": row["task"] or "-",
        "run": row["run"] or "-",
        "branch": row["branch"] or "-",
        "worktree": row["worktree"] or "-",
        "phase": row["phase"] or "-",
    }
    return tuple(field[key] for key in keys)


def _render_group(group: tuple[str, ...], keys: list[str]) -> list[str]:
    return [value[:8] if key == "run" else value for key, value in zip(keys, group, strict=True)]


def _elapsed_union(rows: list[StageRow]) -> float:
    """Seconds during which at least one stage was running.

    Summing stage seconds counts parallel agents twice: 45 of 48 runs overlap
    another run, and the sum reads 1.7x the clock time the work occupied.
    """
    spans = sorted(
        (row["started_at"], row["started_at"] + row["seconds"])
        for row in rows
        if row["started_at"] > 0
    )
    if not spans:
        return 0.0
    union = 0.0
    start, end = spans[0]
    for begin, finish in spans[1:]:
        if begin > end:
            union += end - start
            start, end = begin, finish
        elif finish > end:
            end = finish
    return union + (end - start)


def _warn(tally: dict[str, int]) -> None:
    newer = tally["future_spine"]
    if newer:
        message = f"note: {newer} events carry a spine newer than {SPINE_VERSION}, not read"
        print(message, file=sys.stderr)
    if tally["unknown_stage"]:
        print(f"note: {tally['unknown_stage']} events named an unknown stage", file=sys.stderr)
    if tally["unreadable"]:
        print(f"note: {tally['unreadable']} unreadable lines skipped", file=sys.stderr)


def _require_rows(rows: list[list[str]], what: str) -> None:
    """Exit non-zero on an empty result, after the empty output is written.

    A query that matches nothing is a question that went unanswered, not an
    answer of zero, and a caller reading the exit code should see the
    difference.
    """
    if not rows:
        raise SystemExit(f"No {what} matched the query")


def _cmd_stages(args: argparse.Namespace) -> None:
    events, tally = _events(_skills(args))
    rows = _rows(args, events, tally)
    keys = _group_keys(_arg(args, "group", "stage,agent"))
    buckets: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for row in rows:
        buckets[_group_of(row, keys)].append(row["seconds"])
    grand = sum(sum(seconds) for seconds in buckets.values())
    elapsed = _elapsed_union(rows)
    parallel = grand / elapsed if elapsed else 0.0
    table: list[list[str]] = []
    ordered = sorted(
        buckets.items(),
        key=lambda item: (-sum(item[1]), item[0]),
    )
    for group, seconds in ordered:
        cell = _cell(seconds)
        share = (cell["total"] / grand * 100.0) if grand else 0.0
        table.append(
            [
                *_render_group(group, keys),
                str(cell["n"]),
                _duration(cell["median"]),
                _duration(cell["p90"]),
                _duration(cell["total"]),
                f"{share:.0f}%",
            ]
        )
    meta = _meta(tally)
    meta["stages"] = len(rows)
    meta["agent_seconds"] = grand
    meta["elapsed_seconds"] = int(round(elapsed))
    form = _arg(args, "format", "table")
    _emit(table, [*keys, "n", "median", "p90", "total", "share"], form, meta)
    if form == "table" and table:
        clock = f"{_duration(elapsed)} elapsed ({parallel:.2f}x parallel)"
        print(f"\n{len(rows)} stages, {_duration(grand)} of agent time across {clock}")
        readable = f"Below roughly {READABLE_SAMPLES} samples in a cell,"
        print(f"{readable} nothing under 1.5x separates from resampling noise.")
    _warn(tally)
    _require_rows(table, "stages")


def _test_outcome(text: str) -> str:
    """pass, fail, or other, from whatever the orchestrator wrote as a result.

    The word boundary is the whole rule: a substring match read
    `refusal, bypass and recovery all correct` as a pass, out of `bypass`.
    """
    lowered = text.strip().lower()
    if re.search(r"\bfail", lowered) or "defect" in lowered or "will not build" in lowered:
        return "fail"
    if re.search(r"\bpass", lowered) or lowered in {"ok", "clean", "green"}:
        return "pass"
    return "other"


def _cmd_tests(args: argparse.Namespace) -> None:
    setattr(args, "stage", "test")
    events, tally = _events(_skills(args))
    rows = _rows(args, events, tally)
    raw = bool(getattr(args, "raw", False))
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for row in rows:
        text = row["result"] or row["status"] or "-"
        outcome = text if raw else _test_outcome(text)
        buckets[(row["skill"], row["label"] or "-", outcome)].append(row["seconds"])
    table: list[list[str]] = []
    for (skill, label, outcome), seconds in sorted(buckets.items(), key=lambda item: -sum(item[1])):
        cell = _cell(seconds)
        table.append(
            [
                skill,
                label,
                outcome[:48],
                str(cell["n"]),
                _duration(cell["median"]),
                _duration(cell["p90"]),
                _duration(cell["total"]),
            ]
        )
    _emit(
        table,
        ["skill", "label", "result", "n", "median", "p90", "total"],
        _arg(args, "format", "table"),
        _meta(tally),
    )
    _warn(tally)
    _require_rows(table, "test stages")


def _cmd_runs(args: argparse.Namespace) -> None:
    events, tally = _events(_skills(args))
    rows = _rows(args, events, tally)
    since = _since_epoch(_arg(args, "since"))
    started: dict[str, float] = {}
    last: dict[str, float] = {}
    elapsed: dict[str, int] = {}
    branch: dict[str, str] = {}
    skill: dict[str, str] = {}
    for store_skill, event in events:
        run = _string(event.get("run_id"))
        if not run:
            continue
        stamp = _number(event.get("timestamp_epoch"))
        _ = started.setdefault(run, _number(event.get("run_started_at"), stamp))
        last[run] = max(last.get(run, 0.0), stamp)
        branch[run] = _string(event.get("branch"), branch.get(run, ""))
        skill[run] = _string(event.get("skill"), skill.get(run, store_skill))
        if _string(event.get("event_type")) == "run_finished":
            # `delegate_session.jsonl` reuses one run id across sessions, so
            # the longest close is the run rather than the last line written.
            close = _integer(event.get("run_elapsed_seconds"))
            elapsed[run] = max(elapsed.get(run, 0), close)
    per_run: dict[str, list[StageRow]] = defaultdict(list)
    for row in rows:
        per_run[row["run"]].append(row)
    # Runs come from the event stream, not from the stage rows: run aaddfa60
    # holds 8.4h of wall clock and no stage at all, and building the list from
    # rows dropped it. A stage, label, or agent filter narrows the list back to
    # the runs that still have a row, so the run count and `n` agree.
    narrowed = bool(_arg(args, "stage") or _arg(args, "label") or _arg(args, "agent"))
    ordered = sorted(started, key=lambda run: -started[run])
    table: list[list[str]] = []
    for run in ordered:
        run_rows = per_run.get(run, [])
        if started[run] < since or (narrowed and not run_rows):
            continue
        closed = run in elapsed
        # An unclosed run has no recorded duration; last event minus start is
        # the only figure available and prints with a `+` so it cannot be read
        # as one.
        open_wall = max(0.0, last.get(run, 0.0) - started[run])
        record = RunRow(
            skill=skill.get(run, ""),
            run=run,
            branch=branch.get(run, ""),
            started_at=started[run],
            finished=closed,
            wall_seconds=elapsed[run] if closed else int(round(open_wall)),
            stage_seconds=sum(row["seconds"] for row in run_rows),
            stages=len(run_rows),
            phases=len({row["phase"] for row in run_rows if row["phase"]}),
        )
        table.append(
            [
                record["skill"],
                record["run"][:8],
                record["branch"][:28] or "-",
                _stamp(record["started_at"]),
                "yes" if record["finished"] else "no",
                _duration(record["wall_seconds"]) + ("" if record["finished"] else "+"),
                _duration(record["stage_seconds"]),
                str(record["stages"]),
                str(record["phases"]),
            ]
        )
    form = _arg(args, "format", "table")
    _emit(
        table,
        ["skill", "run", "branch", "started", "closed", "wall", "stages", "n", "phases"],
        form,
        _meta(tally),
    )
    if form == "table" and table:
        print("\nwall with a + is last event minus start: the run never closed.")
    _warn(tally)
    _require_rows(table, "runs")


def _cmd_versions(args: argparse.Namespace) -> None:
    events, tally = _events(_skills(args))
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for store_skill, event in events:
        skill = _string(event.get("skill"), f"{store_skill} (from store)")
        spine = str(_spine_of(event)) if "spine_version" in event else "0 (pre-spine)"
        counts[(skill, spine, str(_integer(event.get("schema_version"))))] += 1
    table = [
        [skill, spine, schema, str(count)]
        for (skill, spine, schema), count in sorted(counts.items())
    ]
    form = _arg(args, "format", "table")
    meta = _meta(tally)
    meta["reader_spine"] = SPINE_VERSION
    _emit(table, ["skill", "spine", "schema", "events"], form, meta)
    if form == "table":
        print(f"\nreader spine {SPINE_VERSION}")
    _warn(tally)
    _require_rows(table, "events")


def _phase_rows(rows: list[StageRow]) -> list[PhaseRow]:
    """One record per (run, phase): rounds run, and the seconds each held.

    Rows with no run or no phase join nothing and are dropped rather than
    collapsed into a shared empty key, which would merge unrelated work into
    one enormous phase.
    """
    buckets: dict[tuple[str, str], PhaseRow] = {}
    for row in rows:
        if not row["run"] or not row["phase"]:
            continue
        cell = buckets.setdefault(
            (row["run"], row["phase"]),
            PhaseRow(
                run=row["run"],
                phase=row["phase"],
                reviews=0,
                fixes=0,
                impl_seconds=0,
                fix_seconds=0,
                total_seconds=0,
            ),
        )
        cell["total_seconds"] += row["seconds"]
        if row["stage"] == "review":
            cell["reviews"] += 1
        elif row["stage"] == "fix":
            cell["fixes"] += 1
            cell["fix_seconds"] += row["seconds"]
        elif row["stage"] == "implementation":
            cell["impl_seconds"] += row["seconds"]
    return list(buckets.values())


def _cmd_phases(args: argparse.Namespace) -> None:
    """Rounds per phase, not seconds per stage.

    No findings column: all 1,637 `finding_opened` events carry no `phase_id`,
    so every one of them would join nothing.
    """
    events, tally = _events(_skills(args))
    rows = _rows(args, events, tally)
    phases = _phase_rows(rows)
    if _arg(args, "sort", "total") == "fix_passes":
        phases.sort(key=lambda cell: (-cell["fixes"], -cell["fix_seconds"], cell["run"]))
    else:
        phases.sort(key=lambda cell: (-cell["total_seconds"], cell["run"]))
    table = [
        [
            cell["run"][:8],
            cell["phase"],
            str(cell["reviews"]),
            str(cell["fixes"]),
            _duration(cell["impl_seconds"]),
            _duration(cell["fix_seconds"]),
            _duration(cell["total_seconds"]),
        ]
        for cell in phases
    ]
    fix_total = sum(cell["fix_seconds"] for cell in phases)
    heavy = [cell for cell in phases if cell["fixes"] >= FIX_PASS_THRESHOLD]
    heavy_fix = sum(cell["fix_seconds"] for cell in heavy)
    heavy_share = (len(heavy) / len(phases) * 100.0) if phases else 0.0
    fix_share = (heavy_fix / fix_total * 100.0) if fix_total else 0.0
    median_reviews = statistics.median([cell["reviews"] for cell in phases]) if phases else 0.0
    median_fixes = statistics.median([cell["fixes"] for cell in phases]) if phases else 0.0
    reviews = _count(median_reviews, "review", "reviews")
    fixes = _count(median_fixes, "fix", "fixes")
    meta = _meta(tally)
    meta["phases"] = len(phases)
    meta["median_reviews"] = median_reviews
    meta["median_fixes"] = median_fixes
    meta["heavy_phases"] = len(heavy)
    meta["heavy_fix_seconds"] = heavy_fix
    meta["fix_seconds"] = fix_total
    form = _arg(args, "format", "table")
    _emit(
        table,
        ["run", "phase", "reviews", "fixes", "impl", "fix", "total"],
        form,
        meta,
    )
    if form == "table" and table:
        counted = _count(len(phases), "phase", "phases")
        print(f"\n{counted} · median {reviews}, {fixes}")
        held = f"hold {_duration(heavy_fix)} — {fix_share:.0f}% of all fix time"
        weight = f"{_count(len(heavy), 'phase', 'phases')} ({heavy_share:.0f}%)"
        print(f"{weight} with >={FIX_PASS_THRESHOLD} fix passes {held}")
    _warn(tally)
    _require_rows(table, "phases")


def _verdict(before: Cell, after: Cell) -> str:
    """What the two windows support saying, which is often less than a ratio.

    The failure this exists to prevent: two agents each holding hundreds of
    samples across the corpus, never having run in the same week, reported as
    though one beat the other. A cell absent from one window is an era, not a
    result, and says so before any number is printed.
    """
    if not before["n"] or not after["n"]:
        return "one window only"
    if not before["median"] or not after["median"]:
        return "no median"
    ratio = before["median"] / after["median"]
    if 1.0 / NOISE_RATIO < ratio < NOISE_RATIO:
        return "unchanged"
    change = f"{ratio:.1f}x faster" if ratio > 1.0 else f"{1.0 / ratio:.1f}x slower"
    if before["n"] < READABLE_SAMPLES or after["n"] < READABLE_SAMPLES:
        return f"{change} (thin)"
    return change


def _cmd_compare(args: argparse.Namespace) -> None:
    """One config change: the window before it against the window after.

    `--since` is ignored here because the two windows are the only time filter,
    and a third would silently empty one of them.
    """
    events, tally = _events(_skills(args))
    args.since = ""
    rows = _rows(args, events, tally)
    on = _arg(args, "on")
    if not on:
        raise SystemExit("compare needs --on <date>: the day the change landed")
    pivot = _since_epoch(on)
    span = datetime.now(UTC).timestamp() - _since_epoch(_arg(args, "window", "14d"))
    keys = _group_keys(_arg(args, "group", "agent"))
    before: dict[tuple[str, ...], list[int]] = defaultdict(list)
    after: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for row in rows:
        started = row["started_at"]
        if pivot - span <= started < pivot:
            before[_group_of(row, keys)].append(row["seconds"])
        elif pivot <= started < pivot + span:
            after[_group_of(row, keys)].append(row["seconds"])
    groups = sorted(
        set(before) | set(after),
        key=lambda group: (-(sum(before.get(group, [])) + sum(after.get(group, []))), group),
    )
    table: list[list[str]] = []
    for group in groups:
        old, new = _cell(before.get(group, [])), _cell(after.get(group, []))
        table.append(
            [
                *_render_group(group, keys),
                str(old["n"]),
                _duration(old["median"]) if old["n"] else "-",
                str(new["n"]),
                _duration(new["median"]) if new["n"] else "-",
                _verdict(old, new),
            ]
        )
    meta = _meta(tally)
    meta["pivot"] = _stamp(pivot)
    meta["window_seconds"] = int(round(span))
    form = _arg(args, "format", "table")
    _emit(table, [*keys, "n before", "before", "n after", "after", "verdict"], form, meta)
    if form == "table" and table:
        window = _duration(span)
        print(f"\n{window} either side of {_stamp(pivot)}")
        thin = f"'thin' means under {READABLE_SAMPLES} samples"
        flat = f"'unchanged' means under {NOISE_RATIO}x apart"
        print(f"{thin}; {flat}, which a rerun alone can produce.")
    _warn(tally)
    _require_rows(table, "compare")


def _cmd_summary(args: argparse.Namespace) -> None:
    """Where the time goes and what holds the tail, without choosing a view.

    Every number here can be assembled from `stages` and `phases`. This runs
    both and prints the few lines that point at a change worth making.
    """
    events, tally = _events(_skills(args))
    rows = _rows(args, events, tally)
    by_stage: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_stage[row["stage"]].append(row["seconds"])
    grand = sum(sum(seconds) for seconds in by_stage.values())
    table: list[list[str]] = []
    for stage in sorted(by_stage, key=lambda name: -sum(by_stage[name])):
        cell = _cell(by_stage[stage])
        share = (cell["total"] / grand * 100.0) if grand else 0.0
        table.append(
            [
                stage,
                str(cell["n"]),
                _duration(cell["median"]),
                _duration(cell["total"]),
                f"{share:.0f}%",
            ]
        )
    phases = _phase_rows(rows)
    fix_total = sum(cell["fix_seconds"] for cell in phases)
    heavy = [cell for cell in phases if cell["fixes"] >= FIX_PASS_THRESHOLD]
    heavy_fix = sum(cell["fix_seconds"] for cell in heavy)
    heavy_share = (len(heavy) / len(phases) * 100.0) if phases else 0.0
    fix_share = (heavy_fix / fix_total * 100.0) if fix_total else 0.0
    meta = _meta(tally)
    meta["stages"] = len(rows)
    meta["phases"] = len(phases)
    meta["heavy_phases"] = len(heavy)
    meta["heavy_fix_seconds"] = heavy_fix
    form = _arg(args, "format", "table")
    _emit(table, ["stage", "n", "median", "total", "share"], form, meta)
    if form == "table" and table:
        counted = _count(len(phases), "phase", "phases")
        print(f"\n{counted}, {_duration(grand)} of agent time")
        if heavy:
            held = f"hold {_duration(heavy_fix)} — {fix_share:.0f}% of all fix time"
            weight = f"{_count(len(heavy), 'phase', 'phases')} ({heavy_share:.0f}%)"
            print(f"{weight} with >={FIX_PASS_THRESHOLD} fix passes {held}")
        print("\nNext:")
        print("  history.py phases --sort fix_passes        the phases holding the tail")
        print("  history.py stages --stage review --group agent   cost per agent")
        print("  history.py compare --on <date>             did a change help")
    _warn(tally)
    _require_rows(table, "summary")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query stage timings across skill run histories.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def shared(target: argparse.ArgumentParser, *, filters: bool = True) -> None:
        _ = target.add_argument("--skill", default="", help="comma-separated skill names")
        _ = target.add_argument("--since", default="", help="30d, 12h, 3M, or an ISO date")
        _ = target.add_argument(
            "--format", default="table", choices=["table", "json", "csv"]
        )
        if filters:
            _ = target.add_argument("--stage", default="", help=",".join(STAGE_ORDER))
            _ = target.add_argument("--label", default="", help="substring match")
            _ = target.add_argument("--agent", default="", help="substring match")

    def status(target: argparse.ArgumentParser) -> None:
        _ = target.add_argument(
            "--status",
            default=DEFAULT_STATUS,
            help="comma-separated statuses, or all (default: completed)",
        )

    stages = subparsers.add_parser("stages", help="stage timings, grouped")
    shared(stages)
    status(stages)
    _ = stages.add_argument("--group", default="stage,agent", help=",".join(GROUP_KEYS))
    stages.set_defaults(handler=_cmd_stages)

    tests = subparsers.add_parser("tests", help="verification and smoke history")
    shared(tests, filters=False)
    status(tests)
    _ = tests.add_argument("--label", default="")
    _ = tests.add_argument("--agent", default="")
    _ = tests.add_argument("--raw", action="store_true", help="group by the result text")
    tests.set_defaults(handler=_cmd_tests)

    runs = subparsers.add_parser("runs", help="one row per run")
    shared(runs)
    status(runs)
    runs.set_defaults(handler=_cmd_runs)

    versions = subparsers.add_parser("versions", help="what versions the stores hold")
    shared(versions, filters=False)
    versions.set_defaults(handler=_cmd_versions)

    phases = subparsers.add_parser("phases", help="rounds and time per phase")
    shared(phases, filters=False)
    status(phases)
    _ = phases.add_argument("--sort", default="total", choices=["fix_passes", "total"])
    phases.set_defaults(handler=_cmd_phases)

    compare = subparsers.add_parser("compare", help="before and after one change")
    _ = compare.add_argument("--on", default="", help="the date the change landed")
    _ = compare.add_argument("--window", default="14d", help="span either side")
    _ = compare.add_argument("--group", default="agent", help=",".join(GROUP_KEYS))
    _ = compare.add_argument("--skill", default="", help="comma-separated skill names")
    _ = compare.add_argument("--format", default="table", choices=["table", "json", "csv"])
    _ = compare.add_argument("--stage", default="", help=",".join(STAGE_ORDER))
    _ = compare.add_argument("--label", default="", help="substring match")
    _ = compare.add_argument("--agent", default="", help="substring match")
    status(compare)
    compare.set_defaults(handler=_cmd_compare)

    summary = subparsers.add_parser("summary", help="where the time goes, and what to do")
    shared(summary, filters=False)
    status(summary)
    summary.set_defaults(handler=_cmd_summary)

    args = parser.parse_args()
    handler_value: object = getattr(args, "handler")  # pyright: ignore[reportAny]
    if not callable(handler_value):
        raise SystemExit("No command handler selected")
    handler = cast(Callable[[argparse.Namespace], None], handler_value)
    handler(args)


if __name__ == "__main__":
    main()
