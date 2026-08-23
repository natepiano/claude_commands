#!/usr/bin/env python3
"""Per-phase finding ledger, fix-round batching, and the convergence test.

`/plan:delegate` used to bound its fix loop with a counter (`FIX_PASS < 10`).
A counter punishes a phase with ten real defects exactly as hard as one whose
reviews keep re-litigating settled ground, so it stopped runs that were working
and let runs that were not grind to the cap.

This ledger replaces the counter with state:

  * Every finding gets a stable id (`F001`) that survives every round, so
    "issue 2" means the same thing on round four as on round one.
  * `gate` decides whether another automatic fix round may run. It answers
    `converged`, `dispatch`, or `stop` — the orchestrator never decides.
  * `dispatch` refuses a fix that covers only part of the gating open set, so a
    round always closes everything currently on the ledger.
  * Severity narrows after the first round: blockers and minors gate the first
    fix round, blockers alone gate every later one. Nits never gate.

Convergence stop conditions, all computed from recorded state:

  * a finding failed to close `MAX_FIX_ATTEMPTS` times
  * a finding reopened `MAX_REOPENS` times after being accepted
  * the gating open count failed to decrease across `STALLED_ROUNDS` rounds
  * the phase spent its repair budget, which is the first round's gating count
    times `REPAIR_ROUNDS_PER_FINDING`, never below `MIN_REPAIR_BUDGET`
  * `MAX_CONSECUTIVE_SAME_KIND_PASSES` passes of one kind ran in a row
  * the blind review was canceled more than `MAX_REVIEW_CANCELLATIONS` times
  * a runaway backstop at `RUNAWAY_ROUNDS` rounds

Every one of those limits is set in `~/.claude/config/delegate.conf`, which is
authoritative: no limit has a compiled default, and a missing or unusable value
stops the run before any command executes.

Findings live in `<session_dir>/findings_state.json` and are also appended to
the run's durable event stream when `progress_history.py` has started a run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


SCHEMA_VERSION = 1
STATE_FILENAME = "findings_state.json"
PROGRESS_STATE_FILENAME = "progress_history_state.json"

SEVERITIES = ("blocker", "minor", "nit")
FIRST_ROUND_GATING = ("blocker", "minor")
LATER_ROUND_GATING = ("blocker",)

STATE_OPEN = "open"
STATE_PENDING = "fixed_pending_review"
STATE_ACCEPTED = "accepted"

# Every convergence limit is set in `~/.claude/config/delegate.conf` and nowhere
# else -- there are no compiled defaults, so a missing key or an unusable value
# stops the run instead of quietly substituting a limit nobody chose. Problems
# accumulate and are reported together, so one run fixes the whole file rather
# than one key per attempt. `PLAN_DELEGATE_CONFIG` overrides the path so the
# tests read a fixture rather than the machine's live settings.
DEFAULT_CONFIG_PATH = Path("~/.claude/config/delegate.conf").expanduser()

_CONFIG_PROBLEMS: list[str] = []


def _config_path() -> Path:
    configured = os.environ.get("PLAN_DELEGATE_CONFIG")
    return Path(configured) if configured else DEFAULT_CONFIG_PATH


def _config_values() -> dict[str, str]:
    path = _config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        # Stop here: a file nobody can read would otherwise report every key as
        # unset, burying the one problem that matters.
        _CONFIG_PROBLEMS.append(f"the file cannot be read ({error.strerror})")
        _require_usable_config()
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        statement = line.split("#", 1)[0].strip()
        key, separator, value = statement.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _configured_int(values: dict[str, str], key: str, minimum: int = 1) -> int:
    """Read one integer limit. Records a problem and returns a placeholder when unusable."""
    raw = values.get(key)
    if raw is None:
        _CONFIG_PROBLEMS.append(f"{key} is not set")
        return minimum
    try:
        parsed = int(raw)
    except ValueError:
        _CONFIG_PROBLEMS.append(f"{key}={raw} is not a whole number")
        return minimum
    if parsed < minimum:
        _CONFIG_PROBLEMS.append(f"{key}={raw} is below the minimum of {minimum}")
        return minimum
    return parsed


def _configured_float(values: dict[str, str], key: str) -> float:
    """Read one rate. Records a problem and returns a placeholder when unusable."""
    raw = values.get(key)
    if raw is None:
        _CONFIG_PROBLEMS.append(f"{key} is not set")
        return 1.0
    try:
        parsed = float(raw)
    except ValueError:
        _CONFIG_PROBLEMS.append(f"{key}={raw} is not a number")
        return 1.0
    if parsed <= 0:
        _CONFIG_PROBLEMS.append(f"{key}={raw} is not greater than 0")
        return 1.0
    return parsed


def _require_usable_config() -> None:
    """Exit before any command runs when the configuration cannot be trusted."""
    if not _CONFIG_PROBLEMS:
        return
    print(f"delegate.conf ({_config_path()}) is not usable:", file=sys.stderr)
    for problem in _CONFIG_PROBLEMS:
        print(f"  - {problem}", file=sys.stderr)
    print("Every convergence limit must be set; there are no defaults.", file=sys.stderr)
    raise SystemExit(2)


_CONFIG = _config_values()

MAX_FIX_ATTEMPTS = _configured_int(_CONFIG, "MAX_FIX_ATTEMPTS")
MAX_REOPENS = _configured_int(_CONFIG, "MAX_REOPENS")
STALLED_ROUNDS = _configured_int(_CONFIG, "STALLED_ROUNDS")
RUNAWAY_ROUNDS = _configured_int(_CONFIG, "RUNAWAY_ROUNDS")

# Repairs are dispatched one batch per round, so N confirmed findings should
# close in about N/2 rounds. A phase that needs materially more than that is
# bleeding slowly rather than converging -- the stalled-count test never trips
# on 8 -> 7 -> 6 -> 5, and that pattern is what used to run to the backstop.
# The floor is what a phase gets regardless of its first-round count.
REPAIR_ROUNDS_PER_FINDING = _configured_float(_CONFIG, "REPAIR_ROUNDS_PER_FINDING")
MIN_REPAIR_BUDGET = _configured_int(_CONFIG, "MIN_REPAIR_BUDGET")

# Re-dispatching the same pass kind over and over inside one phase is itself a
# convergence failure: observed as six consecutive implementation passes and as
# back-to-back failed fix passes, neither of which the ledger could see.
MAX_CONSECUTIVE_SAME_KIND_PASSES = _configured_int(_CONFIG, "MAX_CONSECUTIVE_SAME_KIND_PASSES")

# <DualReview/> may preempt one obsolete blind review per phase. A second
# cancellation means the broad review is never completing.
MAX_REVIEW_CANCELLATIONS = _configured_int(_CONFIG, "MAX_REVIEW_CANCELLATIONS", minimum=0)

_require_usable_config()


def _now_epoch() -> float:
    configured = os.environ.get("PLAN_DELEGATE_NOW_EPOCH")
    if configured:
        return float(configured)
    return time.time()


def _iso_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds")


def _json_object(text: str) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, object]] = []
    for item in cast(list[object], value):
        entry = _object_dict(item)
        if entry is not None:
            entries.append(entry)
    return entries


def _string(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    return default


def _arg_string(args: argparse.Namespace, name: str, default: str = "") -> str:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    return _string(value, default)


def _arg_integer(args: argparse.Namespace, name: str, default: int = 0) -> int:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    return _integer(value, default)


def _session_dir(args: argparse.Namespace) -> Path:
    value = _arg_string(args, "session_dir")
    if not value:
        raise SystemExit("--session-dir is required")
    return Path(value).expanduser().resolve()


def _state_path(session_dir: Path) -> Path:
    return session_dir / STATE_FILENAME


def _write_state(session_dir: Path, state: dict[str, object]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    target = _state_path(session_dir)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=session_dir,
        prefix=f".{STATE_FILENAME}.",
        delete=False,
    ) as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        _ = handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, target)


def _phase_identity(session_dir: Path) -> tuple[str, str]:
    """Read the active phase from progress state so the ledger is phase-scoped."""
    path = session_dir / PROGRESS_STATE_FILENAME
    if not path.exists():
        return "", ""
    try:
        progress = _json_object(path.read_text(encoding="utf-8"))
    except OSError:
        return "", ""
    if progress is None:
        return "", ""
    phase = _object_dict(progress.get("phase"))
    if phase is None:
        return "", ""
    return _string(phase.get("instance_id")), _string(phase.get("id"))


def _history_file(session_dir: Path) -> Path | None:
    path = session_dir / PROGRESS_STATE_FILENAME
    if not path.exists():
        return None
    try:
        progress = _json_object(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if progress is None:
        return None
    value = _string(progress.get("history_file"))
    return Path(value) if value else None


def _append_event(session_dir: Path, state: dict[str, object], event: dict[str, object]) -> None:
    """Append to the run's durable stream; a run without progress state is local-only."""
    history_file = _history_file(session_dir)
    if history_file is None:
        return
    event.update(
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "phase_instance_id": _string(state.get("phase_instance_id")),
            "phase_id": _string(state.get("phase_id")),
        }
    )
    history_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    with history_file.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fresh_state(session_dir: Path, now: float) -> dict[str, object]:
    instance_id, phase_id = _phase_identity(session_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase_instance_id": instance_id,
        "phase_id": phase_id,
        "started_at": now,
        "next_ordinal": 1,
        "findings": {},
        "rounds": [],
    }


def _read_state(session_dir: Path, now: float) -> dict[str, object]:
    """Load the ledger, resetting it when the active phase has moved on."""
    path = _state_path(session_dir)
    instance_id, phase_id = _phase_identity(session_dir)
    if not path.exists():
        return _fresh_state(session_dir, now)
    try:
        state = _json_object(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"Unable to read findings state {path}: {error}") from error
    if state is None:
        raise SystemExit(f"Invalid findings state: {path}")
    if instance_id and _string(state.get("phase_instance_id")) != instance_id:
        fresh = _fresh_state(session_dir, now)
        fresh["phase_instance_id"] = instance_id
        fresh["phase_id"] = phase_id
        return fresh
    return state


def _findings(state: dict[str, object]) -> dict[str, object]:
    findings = _object_dict(state.get("findings"))
    if findings is None:
        findings = {}
        state["findings"] = findings
    return findings


def _finding(state: dict[str, object], finding_id: str) -> dict[str, object]:
    entry = _object_dict(_findings(state).get(finding_id))
    if entry is None:
        raise SystemExit(f"Unknown finding id: {finding_id}")
    return entry


def _rounds(state: dict[str, object]) -> list[dict[str, object]]:
    return _object_list(state.get("rounds"))


def _history_root() -> Path:
    configured = os.environ.get("PLAN_DELEGATE_HISTORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "plan-delegate"


def _phase_passes(session_dir: Path, instance_id: str) -> list[tuple[str, str]]:
    """Every pass this phase instance has run, as (kind, status) in order.

    Read from the durable event stream rather than the session cache: the cache
    holds only the pass currently running, and what the gate needs is the shape
    of the whole phase. `finished` carries the outcome; a pass still running
    contributes its `started` row with an empty status.
    """
    if not instance_id:
        return []
    path = _history_root() / "runs" / f"{session_dir.name}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    passes: dict[str, tuple[int, str, str]] = {}
    for order, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        event = _json_object(line)
        if event is None:
            continue
        if _string(event.get("phase_instance_id")) != instance_id:
            continue
        event_type = _string(event.get("event_type"))
        if event_type not in ("pass_started", "pass_finished"):
            continue
        pass_id = _string(event.get("pass_instance_id"))
        if not pass_id:
            continue
        kind = _string(event.get("pass_kind"))
        status = _string(event.get("status")) if event_type == "pass_finished" else ""
        existing = passes.get(pass_id)
        seen_at = existing[0] if existing else order
        passes[pass_id] = (seen_at, kind or (existing[1] if existing else ""), status)
    return [(kind, status) for _, kind, status in sorted(passes.values())]


def _consecutive_same_kind(passes: list[tuple[str, str]]) -> tuple[str, int]:
    """The longest run of one pass kind ending at the most recent pass."""
    if not passes:
        return "", 0
    kind = passes[-1][0]
    run = 0
    for entry_kind, _ in reversed(passes):
        if entry_kind != kind:
            break
        run += 1
    return kind, run


def _repair_budget(state: dict[str, object]) -> int:
    """How many fix rounds this phase's original finding count justifies."""
    rounds = _rounds(state)
    if not rounds:
        return 0
    initial = _integer(rounds[0].get("gating_open_before"), 0)
    return max(MIN_REPAIR_BUDGET, math.ceil(initial * REPAIR_ROUNDS_PER_FINDING))


def _gating_severities(state: dict[str, object]) -> tuple[str, ...]:
    return FIRST_ROUND_GATING if not _rounds(state) else LATER_ROUND_GATING


def _summarize(entry: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {
        "id": _string(entry.get("id")),
        "severity": _string(entry.get("severity")),
        "state": _string(entry.get("state")),
        "file": _string(entry.get("file")),
        "title": _string(entry.get("title")),
        "caught_by": _string(entry.get("caught_by")),
        "fix_attempts": _integer(entry.get("fix_attempts")),
        "reopen_count": _integer(entry.get("reopen_count")),
    }
    line = _integer(entry.get("line"))
    if line > 0:
        summary["line"] = line
    detail = _string(entry.get("detail"))
    if detail:
        summary["detail"] = detail
    return summary


def _open_entries(state: dict[str, object], severities: tuple[str, ...]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) != STATE_OPEN:
            continue
        if _string(entry.get("severity")) in severities:
            selected.append(entry)
    return selected


def _pending_ids(state: dict[str, object]) -> list[str]:
    return [
        finding_id
        for finding_id in sorted(_findings(state))
        if _string(_finding(state, finding_id).get("state")) == STATE_PENDING
    ]


def _open_counts(state: dict[str, object]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) == STATE_OPEN:
            severity = _string(entry.get("severity"))
            if severity in counts:
                counts[severity] += 1
    return counts


def _stop_reason(
    state: dict[str, object],
    gating_open: int,
    passes: list[tuple[str, str]],
) -> str:
    # Reopens are checked first: a finding that was accepted and then invalidated
    # again says more about the repair than its raw dispatch count does, and the
    # two thresholds are reached on the same round in the ordinary flow.
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) != STATE_OPEN:
            continue
        if _integer(entry.get("reopen_count")) >= MAX_REOPENS:
            return f"{finding_id} reopened {_integer(entry.get('reopen_count'))} times after being accepted"
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) != STATE_OPEN:
            continue
        if _integer(entry.get("fix_attempts")) >= MAX_FIX_ATTEMPTS:
            attempts = _integer(entry.get("fix_attempts"))
            return f"{finding_id} failed to close after {attempts} repair attempts"
    kind, run = _consecutive_same_kind(passes)
    if run >= MAX_CONSECUTIVE_SAME_KIND_PASSES:
        return f"{run} consecutive {kind} passes ran without the phase advancing"
    cancellations = sum(
        1 for pass_kind, status in passes if pass_kind == "review" and status == "canceled"
    )
    if cancellations > MAX_REVIEW_CANCELLATIONS:
        return f"the blind review was canceled {cancellations} times, so it is never completing"
    rounds = _rounds(state)
    if len(rounds) >= STALLED_ROUNDS:
        previous = _integer(rounds[-1].get("gating_open_before"), -1)
        earlier = _integer(rounds[-2].get("gating_open_before"), -1)
        if previous >= 0 and earlier >= 0 and gating_open >= previous >= earlier:
            trend = f"({earlier} -> {previous} -> {gating_open})"
            return f"the gating open count has not decreased for {STALLED_ROUNDS} rounds {trend}"
    budget = _repair_budget(state)
    if budget and len(rounds) >= budget:
        initial = _integer(rounds[0].get("gating_open_before"), 0)
        return (
            f"{len(rounds)} fix rounds have run for {initial} original findings, "
            f"past the {budget}-round repair budget, with {gating_open} still open"
        )
    if len(rounds) >= RUNAWAY_ROUNDS:
        return f"the runaway backstop of {RUNAWAY_ROUNDS} fix rounds was reached"
    return ""


def _open(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    severity = _arg_string(args, "severity")
    ordinal = _integer(state.get("next_ordinal"), 1)
    finding_id = f"F{ordinal:03d}"
    entry: dict[str, object] = {
        "id": finding_id,
        "severity": severity,
        "file": _arg_string(args, "file"),
        "line": _arg_integer(args, "line"),
        "title": _arg_string(args, "title"),
        "detail": _arg_string(args, "detail"),
        "caught_by": _arg_string(args, "caught_by"),
        "state": STATE_OPEN,
        "opened_at": now,
        "opened_round": len(_rounds(state)),
        "fix_attempts": 0,
        "reopen_count": 0,
    }
    _findings(state)[finding_id] = entry
    state["next_ordinal"] = ordinal + 1
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_opened",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "finding_id": finding_id,
            "severity": severity,
            "file": _arg_string(args, "file"),
            "line": _arg_integer(args, "line"),
            "title": _arg_string(args, "title"),
            "caught_by": _arg_string(args, "caught_by"),
            "round": len(_rounds(state)),
        },
    )
    print(finding_id)


def _verdict(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    finding_id = _arg_string(args, "id")
    entry = _finding(state, finding_id)
    outcome = _arg_string(args, "state")
    current = _string(entry.get("state"))
    if outcome == "accepted":
        entry["state"] = STATE_ACCEPTED
        entry["accepted_at"] = now
    elif outcome == "still_open":
        if current == STATE_ACCEPTED:
            raise SystemExit(
                f"{finding_id} is accepted; use --state reopened with the hunk that invalidates it"
            )
        entry["state"] = STATE_OPEN
    else:
        if current != STATE_ACCEPTED:
            raise SystemExit(f"{finding_id} is not accepted, so it cannot reopen")
        evidence = _arg_string(args, "evidence").strip()
        if not evidence:
            raise SystemExit(
                "--evidence is required to reopen an accepted finding: name the new hunk"
                + " and the recorded dependency it invalidates"
            )
        entry["state"] = STATE_OPEN
        entry["reopen_count"] = _integer(entry.get("reopen_count")) + 1
        entry["reopened_evidence"] = evidence
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_verdict",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "finding_id": finding_id,
            "verdict": outcome,
            "resulting_state": _string(entry.get("state")),
            "fix_attempts": _integer(entry.get("fix_attempts")),
            "reopen_count": _integer(entry.get("reopen_count")),
            "round": len(_rounds(state)),
        },
    )
    print(f"{finding_id} {_string(entry.get('state'))}")


def _live_override(state: dict[str, object], reason: str) -> dict[str, object] | None:
    """The user's standing override of one specific stop reason, or None.

    A stop can be wrong about the world — a review the orchestrator recorded by
    hand, a count skewed by an aborted launcher — and the durable event history
    that produced it must never be edited to make it go away. The override is the
    correction path instead: it names the exact reason it clears, carries the
    user's own words, and is spent by the fix round it authorizes, so it can
    silence one wrong stop and nothing else.
    """
    override = _object_dict(state.get("stop_override"))
    if override is None:
        return None
    if override.get("consumed_round") is not None:
        return None
    if _string(override.get("stop_reason")) != reason:
        return None
    return override


def _gate_payload(state: dict[str, object], session_dir: Path) -> dict[str, object]:
    gating = _gating_severities(state)
    batch = _open_entries(state, gating)
    counts = _open_counts(state)
    gating_open = sum(counts[severity] for severity in gating)
    passes = _phase_passes(session_dir, _string(state.get("phase_instance_id")))
    kind, run = _consecutive_same_kind(passes)
    payload: dict[str, object] = {
        "round": len(_rounds(state)) + 1,
        "gating_severities": list(gating),
        "open_counts": counts,
        "batch": [_summarize(entry) for entry in batch],
        "non_gating_open": [
            _summarize(entry)
            for entry in _open_entries(state, tuple(s for s in SEVERITIES if s not in gating))
        ],
        "repair_budget": _repair_budget(state),
        "passes_run": len(passes),
        "consecutive_same_kind": {"kind": kind, "count": run} if run else None,
        "review_cancellations": sum(
            1 for pass_kind, status in passes if pass_kind == "review" and status == "canceled"
        ),
    }
    if not batch:
        payload["verdict"] = "converged"
        payload["stop_reason"] = None
        return payload
    reason = _stop_reason(state, gating_open, passes)
    override = _live_override(state, reason) if reason else None
    if override is not None:
        payload["verdict"] = "dispatch"
        payload["stop_reason"] = None
        payload["overridden_stop"] = {
            "stop_reason": reason,
            "reason": _string(override.get("reason")),
            "granted_at": _string(override.get("granted_at")),
        }
        return payload
    payload["verdict"] = "stop" if reason else "dispatch"
    payload["stop_reason"] = reason or None
    return payload


def _gate(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    pending = _pending_ids(state)
    if pending:
        raise SystemExit(
            "Record a closure verdict for "
            + ", ".join(pending)
            + " before gating the next round"
        )
    payload = _gate_payload(state, session_dir)
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_gate",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "verdict": payload.get("verdict"),
            "stop_reason": payload.get("stop_reason"),
            "round": payload.get("round"),
            "open_counts": payload.get("open_counts"),
        },
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


def _dispatch(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    payload = _gate_payload(state, session_dir)
    verdict = _string(payload.get("verdict"))
    if verdict != "dispatch":
        raise SystemExit(
            f"gate says {verdict}"
            + (f" ({_string(payload.get('stop_reason'))})" if payload.get("stop_reason") else "")
            + "; a fix round is not authorized"
        )
    gating = _gating_severities(state)
    expected = {_string(entry.get("id")) for entry in _open_entries(state, gating)}
    covered = {value.strip() for value in _arg_string(args, "covers").split(",") if value.strip()}
    unknown = sorted(covered - set(_findings(state)))
    if unknown:
        raise SystemExit("Unknown finding ids in --covers: " + ", ".join(unknown))
    missing = sorted(expected - covered)
    if missing:
        raise SystemExit(
            "A fix round must repair every gating open finding together. Missing: "
            + ", ".join(missing)
        )
    counts = _open_counts(state)
    gating_open = sum(counts[severity] for severity in gating)
    round_number = len(_rounds(state)) + 1
    for finding_id in sorted(covered):
        entry = _finding(state, finding_id)
        entry["state"] = STATE_PENDING
        entry["fix_attempts"] = _integer(entry.get("fix_attempts")) + 1
        entry["last_dispatch_round"] = round_number
    rounds = _rounds(state)
    rounds.append(
        {
            "round": round_number,
            "dispatched_at": now,
            "covered": sorted(covered),
            "gating_open_before": gating_open,
            "gating_severities": list(gating),
        }
    )
    state["rounds"] = rounds
    spent = _object_dict(payload.get("overridden_stop"))
    if spent is not None:
        override = _object_dict(state.get("stop_override"))
        if override is not None:
            override["consumed_round"] = round_number
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_batch_dispatched",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "round": round_number,
            "covered": sorted(covered),
            "gating_open_before": gating_open,
            "gating_severities": list(gating),
            "overridden_stop": spent,
        },
    )
    print(f"round {round_number} covering {', '.join(sorted(covered))}")


def _override(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    gating = _gating_severities(state)
    batch = _open_entries(state, gating)
    counts = _open_counts(state)
    gating_open = sum(counts[severity] for severity in gating)
    passes = _phase_passes(session_dir, _string(state.get("phase_instance_id")))
    reason = _stop_reason(state, gating_open, passes) if batch else ""
    if not reason:
        raise SystemExit("The gate is not stopping, so there is nothing to override")
    justification = _arg_string(args, "reason")
    if len(justification) < 20:
        raise SystemExit(
            "Record why the stop is wrong in the user's own words, not a token; "
            + "this text is the whole audit trail for the round it authorizes"
        )
    override: dict[str, object] = {
        "stop_reason": reason,
        "reason": justification,
        "granted_at": _iso_time(now),
        "granted_at_epoch": now,
        "consumed_round": None,
    }
    state["stop_override"] = override
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_stop_overridden",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "stop_reason": reason,
            "reason": justification,
        },
    )
    print(f"override recorded for: {reason}")


def _status(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now)
    findings = [_summarize(_finding(state, key)) for key in sorted(_findings(state))]
    payload: dict[str, object] = {
        "phase_id": _string(state.get("phase_id")),
        "rounds_completed": len(_rounds(state)),
        "gating_severities": list(_gating_severities(state)),
        "open_counts": _open_counts(state),
        "findings": findings,
        "rounds": _rounds(state),
        "started_at": _number(state.get("started_at")),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track plan-delegate review findings and bound the fix loop by convergence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    opened = subparsers.add_parser("open", help="record a confirmed finding and print its id")
    _ = opened.add_argument("--session-dir", required=True)
    _ = opened.add_argument("--severity", choices=SEVERITIES, required=True)
    _ = opened.add_argument("--title", required=True)
    _ = opened.add_argument("--file", default="")
    _ = opened.add_argument("--line", type=int, default=0)
    _ = opened.add_argument("--detail", default="")
    _ = opened.add_argument(
        "--caught-by", choices=("delegate", "main", "both"), required=True
    )
    opened.set_defaults(handler=_open)

    verdict = subparsers.add_parser("verdict", help="record a closure review's outcome")
    _ = verdict.add_argument("--session-dir", required=True)
    _ = verdict.add_argument("--id", required=True)
    _ = verdict.add_argument(
        "--state", choices=("accepted", "still_open", "reopened"), required=True
    )
    _ = verdict.add_argument("--evidence", default="")
    verdict.set_defaults(handler=_verdict)

    gate = subparsers.add_parser("gate", help="decide converged / dispatch / stop")
    _ = gate.add_argument("--session-dir", required=True)
    gate.set_defaults(handler=_gate)

    dispatch = subparsers.add_parser("dispatch", help="record a fix round covering the batch")
    _ = dispatch.add_argument("--session-dir", required=True)
    _ = dispatch.add_argument("--covers", required=True)
    dispatch.set_defaults(handler=_dispatch)

    override = subparsers.add_parser(
        "override", help="record the user's override of one wrong stop verdict"
    )
    _ = override.add_argument("--session-dir", required=True)
    _ = override.add_argument("--reason", required=True)
    override.set_defaults(handler=_override)

    status = subparsers.add_parser("status", help="print the whole ledger")
    _ = status.add_argument("--session-dir", required=True)
    status.set_defaults(handler=_status)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    handler_value: object = getattr(args, "handler")  # pyright: ignore[reportAny]
    if not callable(handler_value):
        raise SystemExit("No command handler selected")
    handler = cast(Callable[[argparse.Namespace], None], handler_value)
    session_dir = _session_dir(args)
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / ".findings.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            handler(args)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
