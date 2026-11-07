#!/usr/bin/env python3
"""Per-phase finding ledger, fix-round batching, and the convergence test.

`/unit:direct` used to bound its fix loop with a counter (`FIX_PASS < 10`).
A counter punishes a phase with ten real defects exactly as hard as one whose
reviews keep re-litigating settled ground, so it stopped runs that were working
and let runs that were not grind to the cap.

This ledger replaces the counter with state:

  * Every finding gets a stable id (`F001`) that survives every round, so
    "issue 2" means the same thing on round four as on round one.
  * `gate` decides whether another automatic fix round may run. It answers
    `converged` or `dispatch` and never stops the run. The patterns that once
    stopped it are still computed and still reported, as an `advisory` string
    beside the verdict, because they are worth seeing — a repair that keeps
    failing, a count that will not come down. They are for the person watching
    to act on, not for this script to enforce.
  * `dispatch` refuses a fix that covers only part of the gating open set, so a
    round always closes everything currently on the ledger.
  * A dispatched round is `repair_in_flight` until something observes how it
    ended: `landed` on a clean worker exit, `abandon` on a kill or an error.
    Nothing may review a finding still in flight, so a repair that dies cannot
    hand the next reviewer a problem pre-labelled as fixed.
  * Severity narrows after the first round: blockers and minors gate the first
    fix round, blockers alone gate every later one. Nits never gate.

Convergence advisories, all computed from recorded state and all reported
rather than enforced:

  * a finding failed to close `MAX_FIX_ATTEMPTS` times
  * a finding reopened `MAX_REOPENS` times after being accepted
  * the gating open count failed to decrease across `STALLED_ROUNDS` rounds
  * the phase spent its repair budget, which is the first round's gating count
    times `REPAIR_ROUNDS_PER_FINDING`, never below `MIN_REPAIR_BUDGET`
  * one worker ran `MAX_CONSECUTIVE_SAME_KIND_PASSES` passes of one kind in a row
  * one worker had a review canceled more than `MAX_REVIEW_CANCELLATIONS` times
  * a runaway backstop at `RUNAWAY_ROUNDS` rounds

Every one of those limits is set in `~/.claude/config/direct.conf`, which is
authoritative: no limit has a compiled default, and a missing or unusable value
stops the run before any command executes. The convergence limits decide when an advisory
is worth printing. Routing and Astra thresholds require repair evidence before
dispatch (the user, 2026-10-28).

Findings live in `<session_dir>/findings_state.json` and are also appended to
the run's durable event stream when `progress_history.py` has started a run.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import conf, unit_runs  # noqa: E402
from scripts.history.rounds import pass_ledger_round, phase_rounds, refunded_batches  # noqa: E402
from scripts.usage.agents_log import LENSES as REVIEW_LENSES  # noqa: E402

# What history written before the rename records for a finding a worker caught.
FORMER_WORKER = "delegate"


SCHEMA_VERSION = 1
STATE_FILENAME = "findings_state.json"
PROGRESS_STATE_FILENAME = "progress_history_state.json"
# The progress recorder's key for the older open phases (progress_history.OPEN_PHASES_KEY).
OPEN_PHASES_KEY = "open_phases"
PHASE_HELP = "the plan phase id this call is for; required when more than one phase is open"

SEVERITIES = ("blocker", "minor", "nit")
FIRST_ROUND_GATING = ("blocker", "minor")
LATER_ROUND_GATING = ("blocker",)

# What `--lens` took when the broad review had two lenses; it means
# adversary,contract, and the ledger keeps the word its callers already write.
LEGACY_BOTH_LENS = "both"

STATE_OPEN = "open"
# Dispatch reserves an attempt before launch. It never proves that the repair landed.
# These are two states rather than one. A killed, crashed, or abandoned repair
# leaves its findings `repair_in_flight`. The gate and the closure verdict refuse
# that state. One shared state would present a dead repair's findings as fixed.
# The next reviewer would then confirm work that nobody completed.
STATE_IN_FLIGHT = "repair_in_flight"
STATE_PENDING = "fixed_pending_review"
STATE_ACCEPTED = "accepted"
STATE_ROUTED = "routed"
STATE_HELD = "held_for_decision"
STATE_DECIDED = "decided"
KEEP_CATEGORIES = ("regression", "broken", "gate", "ruling")


def _now_epoch() -> float:
    configured = os.environ.get("UNIT_NOW_EPOCH") or os.environ.get("PLAN_DELEGATE_NOW_EPOCH")
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


def _parked_path(session_dir: Path, instance_id: str) -> Path:
    """Where a phase's ledger waits while another open phase is current.

    The current phase owns `findings_state.json`, which assign_claude.py reads.
    Keep the previous ledger here until its phase becomes current again.
    """
    return session_dir / f"findings_state-{instance_id}.json"


def _ledger_instance(path: Path) -> str:
    try:
        state = _json_object(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return _string(state.get("phase_instance_id")) if state is not None else ""


def _write_state(session_dir: Path, state: dict[str, object]) -> None:
    """Write a phase's ledger where its readers look for it.

    The newest open phase owns `findings_state.json`, which assign_claude.py checks.
    An older open phase named with `--phase` writes its parked file.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    live = _state_path(session_dir)
    instance_id = _string(state.get("phase_instance_id"))
    displaced = _ledger_instance(live) if live.exists() else ""
    newest = _newest_open_instance(session_dir)
    if live.exists() and displaced != instance_id and instance_id and newest and newest != instance_id:
        target = _parked_path(session_dir, instance_id)
    else:
        target = live
        if displaced and displaced != instance_id:
            _ = shutil.copyfile(live, _parked_path(session_dir, displaced))
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


def _progress_state(session_dir: Path) -> dict[str, object] | None:
    path = session_dir / PROGRESS_STATE_FILENAME
    if not path.exists():
        return None
    try:
        return _json_object(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _open_phases(progress: dict[str, object]) -> list[dict[str, object]]:
    """Every open phase, oldest first: the older ones the recorder parks, then the newest."""
    phases = _object_list(progress.get(OPEN_PHASES_KEY))
    current = _object_dict(progress.get("phase"))
    if current is not None and _string(current.get("status")) == "active":
        phases.append(current)
    return phases


def _newest_open_instance(session_dir: Path) -> str:
    progress = _progress_state(session_dir)
    phases = _open_phases(progress) if progress is not None else []
    return _string(phases[-1].get("instance_id")) if phases else ""


def _newest_ledger_instance(session_dir: Path, phase_id: str) -> str:
    """Return the newest ledger instance for the named phase."""
    ledgers: list[dict[str, object]] = []
    for path in [_state_path(session_dir), *sorted(session_dir.glob(_parked_path(session_dir, "*").name))]:
        if path.is_file():
            ledger = _json_object(path.read_text(encoding="utf-8"))
            if ledger is not None and _string(ledger.get("phase_id")) == phase_id:
                ledgers.append(ledger)
    if not ledgers:
        return ""
    newest = max(ledgers, key=lambda ledger: _number(ledger.get("started_at")))
    return _string(newest.get("phase_instance_id"))


def _phase_identity(
    session_dir: Path, phase_id: str = "", *, closed_phase_ok: bool = False,
) -> tuple[str, str]:
    """Resolve the phase for this ledger command.

    A named phase must be open unless the caller permits a closed phase.
    An unnamed command selects the only open phase.
    Several open phases require an explicit name.
    """
    progress = _progress_state(session_dir)
    if progress is None:
        return "", phase_id
    phases = _open_phases(progress)
    if phase_id:
        for phase in phases:
            if _string(phase.get("id")) == phase_id:
                return _string(phase.get("instance_id")), phase_id
        if closed_phase_ok:
            instance_id = _newest_ledger_instance(session_dir, phase_id)
            if instance_id:
                return instance_id, phase_id
        open_ids = ", ".join(_string(phase.get("id")) for phase in phases) or "none"
        raise SystemExit(f"Phase {phase_id} is not open (open phases: {open_ids})")
    if len(phases) > 1:
        open_ids = ", ".join(_string(phase.get("id")) for phase in phases)
        raise SystemExit(f"{len(phases)} phases are open ({open_ids}); name one with --phase")
    phase = phases[0] if phases else _object_dict(progress.get("phase"))
    if phase is None:
        return "", ""
    if not closed_phase_ok and _string(phase.get("status")) not in ("", "active"):
        raise SystemExit(f"Phase {_string(phase.get('id'))} is not open (open phases: none)")
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
        # Disposable fixtures may omit disk durability; production syncs by default.
        if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
            os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fresh_state(instance_id: str, phase_id: str, now: float) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "phase_instance_id": instance_id,
        "phase_id": phase_id,
        "started_at": now,
        "next_ordinal": 1,
        "findings": {},
        "rounds": [],
    }


def _read_state(
    session_dir: Path, now: float, phase: str = "", *, closed_phase_ok: bool = False,
) -> dict[str, object]:
    """Load the phase's ledger: the live file, a parked one, or a fresh one."""
    instance_id, phase_id = _phase_identity(session_dir, phase, closed_phase_ok=closed_phase_ok)
    return read_phase_ledger(session_dir, instance_id, phase_id, now)


def read_phase_ledger(
    session_dir: Path, instance_id: str, phase_id: str, now: float,
) -> dict[str, object]:
    """Read the selected instance's ledger without changing the current phase."""
    path = _state_path(session_dir)
    parked = _parked_path(session_dir, instance_id)
    if not path.exists() and instance_id and parked.exists():
        path = parked
    if not path.exists():
        return _fresh_state(instance_id, phase_id, now)
    try:
        state = _json_object(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"Unable to read findings state {path}: {error}") from error
    if state is None:
        raise SystemExit(f"Invalid findings state: {path}")
    if instance_id and _string(state.get("phase_instance_id")) != instance_id:
        if parked.exists():
            try:
                kept = _json_object(parked.read_text(encoding="utf-8"))
            except OSError as error:
                raise SystemExit(f"Unable to read findings state {parked}: {error}") from error
            if kept is not None:
                return kept
        return _fresh_state(instance_id, phase_id, now)
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
    """Every batch ever dispatched, including abandoned batches.

    This is the audit trail and the source of dispatch numbers. The numbers
    stay monotonic, so no two entries in the event stream share one.
    """
    return _object_list(state.get("rounds"))


def _live_rounds(state: dict[str, object]) -> list[dict[str, object]]:
    """The batches whose attempts remain charged, which convergence counts.

    A refunded batch consumed no repair. Its worker died before it made edits.
    Charging it against the repair budget would stop a phase for work that
    never happened. Reading it as a stalled round would have the same effect.
    """
    return [entry for entry in _rounds(state) if not entry.get("abandoned")]


def _caught_by(args: argparse.Namespace) -> str:
    caught_by = _arg_string(args, "caught_by")
    return "worker" if caught_by == FORMER_WORKER else caught_by


def _history_root() -> Path:
    return unit_runs.history_root()


def _repair_round(state: dict[str, object], session_dir: Path) -> int:
    """The next repair round, counting each started ledger batch once."""
    path = _history_root() / "runs" / f"{session_dir.name}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 1
    except OSError as error:
        raise SystemExit(f"Unable to read repair history {path}: {error}") from error
    instance = _string(state.get("phase_instance_id"))
    events = [event for line in lines if (event := _json_object(line)) is not None
              and _string(event.get("phase_instance_id")) == instance]
    return len({number for repair in phase_rounds(events) for number in repair["ledger_rounds"]}) + 1


def _astra_due(entry: dict[str, object], repair_round: int, settings_read: conf.ConfigRead) -> bool:
    if _string(entry.get("state")) != STATE_OPEN:
        return False
    attempts = _integer(entry.get("fix_attempts"))
    triggered = repair_round >= settings_read.whole("ROUTING_FROM_ROUND") or attempts >= settings_read.whole("ASTRA_AFTER_FAILED_FIXES")
    return triggered and not any(
        _integer(record.get("fix_attempts"), -1) == attempts
        for record in _object_list(entry.get("astra_reviews"))
    )


def _trial_exposure(
    session_dir: Path, state: dict[str, object], now: float,
    proposal: str, basis: str, decision: str = "",
) -> None:
    _append_event(session_dir, state, {
        "event_type": "trial_exposure", "timestamp": _iso_time(now),
        "timestamp_epoch": now, "proposal": proposal, "basis": basis,
        "decision": decision, "repair_round": _repair_round(state, session_dir),
    })


class PhasePass(TypedDict):
    """One pass a phase ran: what work it was, which worker ran it, how it ended.

    `kind` stays semantic -- impl, test, fix, review -- and says nothing about
    who ran it. `slot` is the worker: a stable worker identity for the length of
    one launcher, not the role that worker is performing, which changes during a
    phase and lives on the coordination board.

    `ledger_round` identifies relaunches of one batch. `repair_refunded` records
    an explicit refund without a later landing. Both use the history reader's rules.
    """

    kind: str
    slot: str
    status: str
    ledger_round: int
    repair_refunded: bool


def _event_slot(event: dict[str, object]) -> str:
    """The worker a pass event was stamped with, under whichever key wrote it.

    The field was `team_role` for a day, before the value was understood to
    name a worker. Reading both keeps a phase that spans the rename to one bucket
    per worker instead of splitting each worker in two.
    """
    if "team_slot" in event:
        return _string(event.get("team_slot"))
    return _string(event.get("team_role"))


def _phase_passes(session_dir: Path, instance_id: str) -> list[PhasePass]:
    """Every pass this phase instance has run, in the order they opened.

    Read from the durable event stream rather than the session cache: the cache
    holds one pass per worker and what the gate needs is the whole phase.
    `finished` carries the outcome; a pass still running contributes its
    `started` row with an empty status.
    """
    if not instance_id:
        return []
    path = _history_root() / "runs" / f"{session_dir.name}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    opened_at: dict[str, int] = {}
    passes: dict[str, PhasePass] = {}
    events = [event for line in lines if (event := _json_object(line)) is not None
              and _string(event.get("phase_instance_id")) == instance_id]
    refunded = refunded_batches(events)
    for order, event in enumerate(events):
        event_type = _string(event.get("event_type"))
        if event_type not in ("pass_started", "pass_finished"):
            continue
        pass_id = _string(event.get("pass_instance_id"))
        if not pass_id:
            continue
        kind = _string(event.get("pass_kind"))
        slot = _event_slot(event)
        status = _string(event.get("status")) if event_type == "pass_finished" else ""
        existing = passes.get(pass_id)
        ledger_round = existing["ledger_round"] if existing and event_type == "pass_finished" else pass_ledger_round(event)
        _ = opened_at.setdefault(pass_id, order)
        passes[pass_id] = PhasePass(
            kind=kind or (existing["kind"] if existing else ""),
            slot=slot or (existing["slot"] if existing else ""),
            status=status,
            ledger_round=ledger_round,
            repair_refunded=ledger_round > 0 and ledger_round in refunded,
        )
    return [passes[pass_id] for pass_id in sorted(passes, key=lambda key: opened_at[key])]


def _passes_by_slot(passes: list[PhasePass]) -> dict[str, list[PhasePass]]:
    """Group the phase's passes by worker and exclude refunded repairs.

    Preserve the recorded order. Passes without a worker share one sequence.
    This preserves the sequence for legacy records and phases with one launcher.
    """
    by_slot: dict[str, list[PhasePass]] = {}
    for entry in passes:
        if entry["kind"] == "fix" and entry["repair_refunded"]:
            continue
        by_slot.setdefault(entry["slot"], []).append(entry)
    return by_slot


def _consecutive_same_kind(passes: list[PhasePass]) -> tuple[str, int]:
    """Count the longest sequence of one kind ending at a worker's latest pass.

    Count each worker separately. Concurrent workers do not extend each other's
    sequence. Fix relaunches of one ledger batch add only one repair to the count.
    Other passes retain the existing count, including legacy fix passes.
    """
    longest_kind = ""
    longest_run = 0
    by_slot = _passes_by_slot(passes)
    for slot in sorted(by_slot):
        worker_passes = by_slot[slot]
        kind = worker_passes[-1]["kind"]
        run = 0
        ledger_rounds: set[int] = set()
        for entry in reversed(worker_passes):
            if entry["kind"] != kind:
                break
            # Relaunches of one batch spend one fix round for this advisory.
            if kind == "fix" and entry["ledger_round"] > 0:
                if entry["ledger_round"] in ledger_rounds:
                    continue
                ledger_rounds.add(entry["ledger_round"])
            run += 1
        if run > longest_run:
            longest_kind, longest_run = kind, run
    return longest_kind, longest_run


def _review_cancellations(passes: list[PhasePass]) -> int:
    """The most times any one worker had a review pass canceled.

    Per worker for the same reason the kind run is: the limit asks whether a
    reviewer is never completing, and one cancellation each across two workers
    is two reviewers interrupted once, not one that cannot finish.
    """
    per_slot: dict[str, int] = {}
    for entry in passes:
        if entry["kind"] == "review" and entry["status"] == "canceled":
            per_slot[entry["slot"]] = per_slot.get(entry["slot"], 0) + 1
    return max(per_slot.values(), default=0)


def _repair_budget(state: dict[str, object], settings_read: conf.ConfigRead) -> int:
    """How many fix rounds this phase's original finding count justifies."""
    rounds = _live_rounds(state)
    if not rounds:
        return 0
    initial = _integer(rounds[0].get("gating_open_before"), 0)
    return max(settings_read.whole("MIN_REPAIR_BUDGET"), math.ceil(initial * settings_read.number("REPAIR_ROUNDS_PER_FINDING")))


def _gating_severities(state: dict[str, object]) -> tuple[str, ...]:
    # Live rounds only: a round that was abandoned before it ran must not spend
    # the first round's wider gate, which is the only one that gates minors.
    return FIRST_ROUND_GATING if not _live_rounds(state) else LATER_ROUND_GATING


def _summarize(entry: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {
        "id": _string(entry.get("id")),
        "severity": _string(entry.get("severity")),
        "state": _string(entry.get("state")),
        "file": _string(entry.get("file")),
        "title": _string(entry.get("title")),
        "caught_by": _string(entry.get("caught_by")),
        "lens": _string(entry.get("lens")),
        "fix_attempts": _integer(entry.get("fix_attempts")),
        "reopen_count": _integer(entry.get("reopen_count")),
    }
    line = _integer(entry.get("line"))
    if line > 0:
        summary["line"] = line
    detail = _string(entry.get("detail"))
    if detail:
        summary["detail"] = detail
    for key in ("route", "astra_reviews", "decision", "decision_history"):
        if key in entry:
            summary[key] = entry[key]
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
    return _ids_in_state(state, STATE_PENDING)


def _in_flight_ids(state: dict[str, object]) -> list[str]:
    return _ids_in_state(state, STATE_IN_FLIGHT)


def _ids_in_state(state: dict[str, object], wanted: str) -> list[str]:
    return [
        finding_id
        for finding_id in sorted(_findings(state))
        if _string(_finding(state, finding_id).get("state")) == wanted
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


def _advisory_reason(
    state: dict[str, object],
    gating_open: int,
    passes: list[PhasePass],
    settings_read: conf.ConfigRead,
) -> str:
    """The convergence pattern worth reporting this round, or "" for none.

    Every branch below used to stop the phase. None of them does now: the run
    belongs to whoever is watching it, and these patterns are advice offered to
    that person rather than a verdict imposed on them. What each branch is still
    good at is naming the shape of a phase that is not converging, which is
    exactly what a watcher wants said out loud and early.
    """
    # Reopens are checked first: a finding that was accepted and then invalidated
    # again says more about the repair than its raw dispatch count does, and the
    # two thresholds are reached on the same round in the ordinary flow.
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) != STATE_OPEN:
            continue
        if _integer(entry.get("reopen_count")) >= settings_read.whole("MAX_REOPENS"):
            return f"{finding_id} reopened {_integer(entry.get('reopen_count'))} times after being accepted"
    for finding_id in sorted(_findings(state)):
        entry = _finding(state, finding_id)
        if _string(entry.get("state")) != STATE_OPEN:
            continue
        if _integer(entry.get("fix_attempts")) >= settings_read.whole("MAX_FIX_ATTEMPTS"):
            attempts = _integer(entry.get("fix_attempts"))
            return f"{finding_id} failed to close after {attempts} repair attempts"
    kind, run = _consecutive_same_kind(passes)
    if run >= settings_read.whole("MAX_CONSECUTIVE_SAME_KIND_PASSES"):
        return f"{run} consecutive {kind} passes ran without the phase advancing"
    cancellations = _review_cancellations(passes)
    if cancellations > settings_read.whole("MAX_REVIEW_CANCELLATIONS"):
        return f"the blind review was canceled {cancellations} times, so it is never completing"
    # Stall and budget read live rounds: an abandoned round repaired nothing, so
    # counting it would stop the phase for a repair that never ran.
    rounds = _live_rounds(state)
    if len(rounds) >= settings_read.whole("STALLED_ROUNDS"):
        previous = _integer(rounds[-1].get("gating_open_before"), -1)
        earlier = _integer(rounds[-2].get("gating_open_before"), -1)
        if previous >= 0 and earlier >= 0 and gating_open >= previous >= earlier:
            trend = f"({earlier} -> {previous} -> {gating_open})"
            return f"the gating open count has not decreased for {settings_read.whole("STALLED_ROUNDS")} rounds {trend}"
    budget = _repair_budget(state, settings_read)
    if budget and len(rounds) >= budget:
        initial = _integer(rounds[0].get("gating_open_before"), 0)
        return (
            f"{len(rounds)} fix rounds have run for {initial} original findings, "
            f"past the {budget}-round repair budget, with {gating_open} still open"
        )
    # Refunded batches spend no repair round, including this advisory threshold.
    if len(rounds) >= settings_read.whole("RUNAWAY_ROUNDS"):
        return f"the runaway backstop of {settings_read.whole("RUNAWAY_ROUNDS")} fix rounds was reached"
    return ""


def _open(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    severity = _arg_string(args, "severity")
    held = bool(getattr(args, "held_for_decision", False))
    lenses = _arg_string(args, "lens").split(",")
    if held and "simplify" not in lenses:
        raise SystemExit("--held-for-decision requires --lens simplify")
    if "simplify" in lenses and severity != "minor":
        raise SystemExit("simplify findings require --severity minor")
    ordinal = _integer(state.get("next_ordinal"), 1)
    finding_id = f"F{ordinal:03d}"
    entry: dict[str, object] = {
        "id": finding_id,
        "severity": severity,
        "file": _arg_string(args, "file"),
        "line": _arg_integer(args, "line"),
        "title": _arg_string(args, "title"),
        "detail": _arg_string(args, "detail"),
        "caught_by": _caught_by(args),
        "lens": _arg_string(args, "lens"),
        "state": STATE_HELD if held else STATE_OPEN,
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
            "caught_by": _caught_by(args),
            "lens": _arg_string(args, "lens"),
            "state": entry["state"],
            "round": len(_rounds(state)),
        },
    )
    print(finding_id)


def _verdict(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    finding_id = _arg_string(args, "id")
    entry = _finding(state, finding_id)
    outcome = _arg_string(args, "state")
    current = _string(entry.get("state"))
    answer = _string((_object_dict(entry.get("decision")) or {}).get("answer"))
    if current == STATE_DECIDED and answer == "declined":
        raise SystemExit(f"{finding_id} is closed by the declined decision; no closure verdict can change it")
    if current == STATE_HELD or (current == STATE_DECIDED and answer != "approved"):
        raise SystemExit(f"{finding_id} requires the user's decision; no closure verdict can change it")
    # A verdict judges a repair that exists. While one is in flight nothing has
    # confirmed any edit landed, so there is nothing a reviewer could have read.
    if current == STATE_IN_FLIGHT:
        raise SystemExit(
            f"{finding_id} has a repair in flight; record `landed` once its worker"
            + " exits cleanly, or `abandon --reason ...` if it died, before any verdict"
        )
    if current == STATE_ROUTED:
        raise SystemExit(f"{finding_id} is routed; its receiving checkpoint must use close-route")
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


def _gate_payload(state: dict[str, object], session_dir: Path, settings_read: conf.ConfigRead) -> dict[str, object]:
    gating = _gating_severities(state)
    batch = _open_entries(state, gating)
    counts = _open_counts(state)
    gating_open = sum(counts[severity] for severity in gating)
    passes = _phase_passes(session_dir, _string(state.get("phase_instance_id")))
    kind, run = _consecutive_same_kind(passes)
    repair_round = _repair_round(state, session_dir)
    payload: dict[str, object] = {
        "round": len(_rounds(state)) + 1,
        "repair_round": repair_round,
        "astra_review_due": [_string(entry.get("id")) for entry in batch
                             if _astra_due(entry, repair_round, settings_read)],
        "gating_severities": list(gating),
        "open_counts": counts,
        "batch": [_summarize(entry) for entry in batch],
        "non_gating_open": [
            _summarize(entry)
            for entry in _open_entries(state, tuple(s for s in SEVERITIES if s not in gating))
        ],
        "repair_budget": _repair_budget(state, settings_read),
        "passes_run": len(passes),
        "consecutive_same_kind": {"kind": kind, "count": run} if run else None,
        "review_cancellations": _review_cancellations(passes),
    }
    if not batch:
        payload["verdict"] = "converged"
        payload["advisory"] = None
        return payload
    # An advisory never changes the verdict. A phase that has run four rounds on
    # one finding still gets its fifth; the difference is that the gate says so
    # out loud instead of refusing.
    payload["verdict"] = "dispatch"
    payload["advisory"] = _advisory_reason(state, gating_open, passes, settings_read) or None
    return payload


def _gate(args: argparse.Namespace) -> None:
    settings_read = _command_settings(args)
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    in_flight = _in_flight_ids(state)
    if in_flight:
        raise SystemExit(
            "A repair round is still in flight for "
            + ", ".join(in_flight)
            + ". Record how it ended -- `landed` if its worker exited cleanly,"
            + " `abandon --reason ...` if it was killed or errored -- before gating"
        )
    pending = _pending_ids(state)
    if pending:
        raise SystemExit(
            "Record a closure verdict for "
            + ", ".join(pending)
            + " before gating the next round"
        )
    payload = _gate_payload(state, session_dir, settings_read)
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_gate",
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "verdict": payload.get("verdict"),
            "advisory": payload.get("advisory"),
            "round": payload.get("round"),
            "repair_round": payload.get("repair_round"),
            "astra_review_due": payload.get("astra_review_due"),
            "open_counts": payload.get("open_counts"),
        },
    )
    if _integer(payload.get("repair_round")) >= settings_read.whole("ROUTING_FROM_ROUND") and any(
        _integer(entry.get("fix_attempts")) >= 1
        for entry in _open_entries(state, _gating_severities(state))
    ):
        _trial_exposure(session_dir, state, now, "P3", "round-4-or-later decision with a repeated open finding")
    print(json.dumps(payload, indent=2, sort_keys=True))


def _kept_evidence(args: argparse.Namespace, covered: set[str]) -> dict[str, dict[str, str]]:
    raw: object = getattr(args, "keep", [])
    values = cast(list[object], raw) if isinstance(raw, list) else []
    kept: dict[str, dict[str, str]] = {}
    for value in values:
        finding_id, equals, detail = _string(value).partition("=")
        category, colon, evidence = detail.partition(":")
        if not equals or not colon or category not in KEEP_CATEGORIES or not evidence.strip():
            raise SystemExit("--keep must be F003=<regression|broken|gate|ruling>:<evidence>")
        if finding_id not in covered:
            raise SystemExit(f"--keep {finding_id} is not in --covers")
        if finding_id in kept:
            raise SystemExit(f"Duplicate --keep for {finding_id}")
        kept[finding_id] = {"category": category, "evidence": evidence.strip()}
    return kept


def _required_text(args: argparse.Namespace, name: str) -> str:
    value = _arg_string(args, name).strip()
    if not value:
        raise SystemExit(f"--{name.replace('_', '-')} must not be empty")
    return value


def _route(args: argparse.Namespace) -> None:
    settings_read = _command_settings(args)
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    finding_id = _arg_string(args, "id")
    entry = _finding(state, finding_id)
    if _string(entry.get("state")) != STATE_OPEN:
        raise SystemExit(f"{finding_id} is {entry.get('state')}; only open findings can be routed")
    repair_round = _repair_round(state, session_dir)
    if _string(entry.get("severity")) in _gating_severities(state) and _astra_due(entry, repair_round, settings_read):
        raise SystemExit(f"Astra review due for {finding_id}; record it with findings.py astra before routing")
    records = _object_list(entry.get("astra_reviews"))
    route: dict[str, object] = {
        "finding_id": finding_id, "reason": _arg_string(args, "reason"),
        "owner": _required_text(args, "owner"), "work_order": _required_text(args, "work_order"),
        "due": _required_text(args, "due"), "evidence": _arg_string(args, "evidence"),
        "repair_round": repair_round, "round": len(_rounds(state)) + 1,
        "fix_attempts": _integer(entry.get("fix_attempts")),
        "astra_approach": _string(records[-1].get("approach")) if records else "",
        "timestamp": _iso_time(now), "timestamp_epoch": now,
    }
    entry["state"] = STATE_ROUTED
    entry["route"] = route.copy()
    _write_state(session_dir, state)
    _append_event(session_dir, state, {"event_type": "finding_routed", **route})
    print(f"{finding_id} routed to {route['owner']}: {route['work_order']}")


def _astra(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    finding_id = _arg_string(args, "id")
    entry = _finding(state, finding_id)
    note = Path(_required_text(args, "note")).expanduser().resolve()
    if not note.is_file():
        raise SystemExit(f"Astra note does not exist: {note}")
    approach = _arg_string(args, "approach").strip()
    unavailable = _arg_string(args, "unavailable").strip()
    if bool(approach) == bool(unavailable):
        raise SystemExit("Give --approach or --unavailable with a non-empty value")
    record: dict[str, object] = {
        "finding_id": finding_id, "note": str(note), "approach": approach,
        "unavailable": unavailable, "fix_attempts": _integer(entry.get("fix_attempts")),
        "repair_round": _repair_round(state, session_dir),
        "timestamp": _iso_time(now), "timestamp_epoch": now,
    }
    records = _object_list(entry.get("astra_reviews"))
    records.append(record)
    entry["astra_reviews"] = records
    _write_state(session_dir, state)
    _append_event(session_dir, state, {"event_type": "finding_astra_review", **record})
    print(f"{finding_id} Astra review recorded")


def _exposure(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    _trial_exposure(session_dir, state, now, _arg_string(args, "proposal"),
                    _required_text(args, "basis"), _arg_string(args, "decision"))
    print("trial exposure recorded")


def _close_route(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    source = _required_text(args, "from")
    parts = source.split(":")
    if len(parts) != 3 or not parts[0].strip() or not parts[2].strip():
        raise SystemExit(
            "--from must be <run id>:<phase instance id>:<finding id> or <run id>::<finding id>; "
            + "run id and finding id must not be empty"
        )
    if _history_file(session_dir) is None:
        raise SystemExit("Start a recorded run before closing routed work")
    _append_event(session_dir, state, {
        "event_type": "finding_route_closed", "timestamp": _iso_time(now),
        "timestamp_epoch": now, "from": source, "evidence": _required_text(args, "evidence"),
    })
    print(f"route {source} closed")


def _dispatch(args: argparse.Namespace) -> None:
    settings_read = _command_settings(args)
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    payload = _gate_payload(state, session_dir, settings_read)
    verdict = _string(payload.get("verdict"))
    if verdict != "dispatch":
        raise SystemExit(f"gate says {verdict}; there is nothing to dispatch")
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
    invalid = sorted(finding_id for finding_id in covered
                     if _string(_finding(state, finding_id).get("state")) != STATE_OPEN)
    if invalid:
        raise SystemExit("Only open findings can be covered: " + ", ".join(invalid))
    if _in_flight_ids(state) or _pending_ids(state):
        raise SystemExit("Record the in-flight repair and its closure verdicts before dispatch")
    accepted_checks = _arg_string(args, "accepted_checks").strip()
    if not accepted_checks:
        raise SystemExit('--accepted-checks is required: name affected accepted checks or "none: <why>"')
    if accepted_checks == "none:":
        raise SystemExit('--accepted-checks "none:" requires a reason')
    repair_round = _integer(payload.get("repair_round"))
    due = sorted(finding_id for finding_id in covered & expected
                 if _astra_due(_finding(state, finding_id), repair_round, settings_read))
    if due:
        raise SystemExit("Astra review due for " + ", ".join(due) + "; record it with findings.py astra")
    kept = _kept_evidence(args, covered)
    if repair_round >= settings_read.whole("ROUTING_FROM_ROUND"):
        missing_kept = sorted(covered - set(kept))
        if missing_kept:
            raise SystemExit(
                "Missing --keep for " + ", ".join(missing_kept)
                + ": route it with `findings.py route` or give the kept category and its evidence"
            )
    counts = _open_counts(state)
    gating_open = sum(counts[severity] for severity in gating)
    round_number = len(_rounds(state)) + 1
    for finding_id in sorted(covered):
        entry = _finding(state, finding_id)
        # In flight, not fixed: dispatch reserves an attempt before launch.
        # `landed` promotes these findings to review after the worker exits cleanly.
        entry["state"] = STATE_IN_FLIGHT
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
            "outcome": "in_flight",
        }
    )
    state["rounds"] = rounds
    _write_state(session_dir, state)
    _append_event(
        session_dir,
        state,
        {
            "event_type": "finding_batch_dispatched",
            "repair_round": repair_round,
            "kept": kept,
            "accepted_checks": accepted_checks,
            "timestamp": _iso_time(now),
            "timestamp_epoch": now,
            "round": round_number,
            "covered": sorted(covered),
            "gating_open_before": gating_open,
            "gating_severities": list(gating),
            "advisory": payload.get("advisory"),
        },
    )
    print(f"round {round_number} covering {', '.join(sorted(covered))}")


def _in_flight_round(state: dict[str, object]) -> dict[str, object] | None:
    """The dispatched round nobody has resolved yet, or None."""
    for entry in reversed(_rounds(state)):
        if _string(entry.get("outcome")) == "in_flight":
            return entry
    return None


def _resolve_round(args: argparse.Namespace) -> None:
    """Resolve the repair round in flight."""
    session_dir, now = _session_dir(args), _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"))
    round_entry = _in_flight_round(state)
    landed = _arg_string(args, "resolution") == "landed"
    in_flight = _in_flight_ids(state)
    if round_entry is None or not in_flight:
        if not landed:
            raise SystemExit("No repair round is in flight, so there is nothing to abandon")
        print("no repair round in flight")
        return
    reason = _arg_string(args, "reason").strip()
    if not landed and not reason:
        raise SystemExit("--reason is required: name how the repair ended")
    edits_landed = bool(getattr(args, "edits_landed", False))
    for finding_id in in_flight:
        entry = _finding(state, finding_id)
        entry["state"] = STATE_PENDING if landed else STATE_OPEN
        if landed:
            entry["landed_at"] = now
        elif not edits_landed:
            entry["fix_attempts"] = max(0, _integer(entry.get("fix_attempts")) - 1)
    outcome = "landed" if landed else "abandoned"
    round_entry.update(outcome=outcome, **{f"{outcome}_at": now})
    details: dict[str, object] = {"landed": in_flight}
    if not landed:
        round_entry.update(abandoned_reason=reason, edits_landed=edits_landed)
        if not edits_landed:
            round_entry["abandoned"] = True
        details = {"reopened": in_flight, "reason": reason, "edits_landed": edits_landed}
    _write_state(session_dir, state)
    _append_event(session_dir, state, {
        "event_type": f"finding_batch_{outcome}", "timestamp": _iso_time(now), "timestamp_epoch": now,
        "round": _integer(round_entry.get("round")), **details,
    })
    refund = "attempt stands" if edits_landed else "attempt refunded"
    label = "landed" if landed else f"abandoned ({refund})"
    print(f"round {_integer(round_entry.get('round'))} {label}: {', '.join(in_flight)}")


def _record_decision_event(session_dir: Path, state: dict[str, object], entry: dict[str, object]) -> None:
    """Record the saved decision once, including its withdrawn ruling."""
    decision = _object_dict(entry.get("decision")) or {}
    event: dict[str, object] = {
        "event_type": "finding_decision", "finding_id": entry["id"],
        "timestamp": _iso_time(_number(decision.get("timestamp_epoch"))), **decision,
        "phase_instance_id": _string(state.get("phase_instance_id")),
    }
    history = _object_list(entry.get("decision_history"))
    if history:
        event["withdrew"] = history[-1]
    history_file = _history_file(session_dir)
    if history_file is not None and history_file.exists():
        for line in history_file.read_text(encoding="utf-8").splitlines():
            recorded = _json_object(line)
            if recorded is not None and all(recorded.get(key) == value for key, value in event.items()):
                return
    _append_event(session_dir, state, event)


def _decision(args: argparse.Namespace) -> None:
    """Record the user's answer without applying the proposed change."""
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"), closed_phase_ok=True)
    entry = _finding(state, _arg_string(args, "id"))
    current = _string(entry.get("state"))
    withdrawing = hasattr(args, "withdraw")
    if current not in (STATE_HELD, STATE_DECIDED):
        raise SystemExit("Only a held or decided finding can receive an answer")
    if current == STATE_HELD and withdrawing:
        raise SystemExit("--withdraw requires a decided finding; this finding has no ruling to withdraw")
    previous = _object_dict(entry.get("decision")) or {}
    answer = _arg_string(args, "answer")
    words = _arg_string(args, "user_words").strip()
    repeated = (current == STATE_DECIDED and previous.get("answer") == answer
                and previous.get("user_words") == words)
    if current == STATE_DECIDED and not withdrawing and not repeated:
        raise SystemExit("This finding is decided; use --withdraw to replace its current ruling")
    decision: dict[str, object] = {
        "answer": answer,
        "user_words": _required_text(args, "user_words"),
        "timestamp_epoch": now,
    }
    if not repeated:
        if current == STATE_DECIDED:
            withdrew = {**previous, "withdrawn_epoch": now, "withdraw_reason": _required_text(args, "withdraw")}
            entry["decision_history"] = [*_object_list(entry.get("decision_history")), withdrew]
        entry["state"] = STATE_DECIDED
        entry["decision"] = decision
        _write_state(session_dir, state)
    _record_decision_event(session_dir, state, entry)
    print(f"{entry['id']} decided: {decision['answer']}")


def _status(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _read_state(session_dir, now, _arg_string(args, "phase"), closed_phase_ok=True)
    findings = [_summarize(_finding(state, key)) for key in sorted(_findings(state))]
    payload: dict[str, object] = {
        "phase_id": _string(state.get("phase_id")),
        "rounds_completed": len(_live_rounds(state)),
        "rounds_dispatched": len(_rounds(state)),
        "in_flight": _in_flight_ids(state),
        "repair_round": _repair_round(state, session_dir),
        "routed": [_summarize(_finding(state, key)) for key in _ids_in_state(state, STATE_ROUTED)],
        "held_for_decision": [_summarize(_finding(state, key)) for key in _ids_in_state(state, STATE_HELD)],
        "gating_severities": list(_gating_severities(state)),
        "open_counts": _open_counts(state),
        "findings": findings,
        "rounds": _rounds(state),
        "started_at": _number(state.get("started_at")),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _lens_list(value: str) -> str:
    """`--lens` checked against REVIEW_LENSES: one lens, several, or `both`.

    argparse runs the default through here too, so empty passes as no lens.
    A list is stored with bare commas and no repeats, so a reader splits on one
    character; `both` is stored as given.
    """
    if not value or value == LEGACY_BOTH_LENS:
        return value
    lenses = [part.strip() for part in value.split(",")]
    unknown = [lens for lens in lenses if lens not in REVIEW_LENSES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown lens {', '.join(repr(lens) for lens in unknown)} in {value!r}: "
            + f"give one of {', '.join(REVIEW_LENSES)}, a comma-separated list "
            + f"of them, or {LEGACY_BOTH_LENS!r} alone for adversary,contract"
        )
    return ",".join(dict.fromkeys(lenses))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track unit review findings and bound the fix loop by convergence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    opened = subparsers.add_parser("open", help="record a confirmed finding and print its id")
    _ = opened.add_argument("--session-dir", required=True)
    _ = opened.add_argument("--phase", default="", help=PHASE_HELP)
    _ = opened.add_argument("--severity", choices=SEVERITIES, required=True)
    _ = opened.add_argument("--title", required=True)
    _ = opened.add_argument("--file", default="")
    _ = opened.add_argument("--line", type=int, default=0)
    _ = opened.add_argument("--detail", default="")
    # `delegate` is the former name of `worker`, kept for directors started before the rename.
    _ = opened.add_argument(
        "--caught-by", choices=("worker", "main", "both", FORMER_WORKER), required=True
    )
    # Which broad-review lenses raised it, comma-separated: `adversary` and
    # `contract` read the diff against its work order, `craft` reads its code
    # quality, and `ux` is the UX screenshot reviewer's. Empty for a closure
    # review or the main review alone.
    _ = opened.add_argument("--lens", type=_lens_list, default="")
    _ = opened.add_argument("--held-for-decision", action="store_true")
    opened.set_defaults(handler=_open)

    decision = subparsers.add_parser("decision", help="record the user's answer to a held or decided finding")
    _ = decision.add_argument("--session-dir", required=True)
    _ = decision.add_argument("--phase", default="", help=PHASE_HELP)
    _ = decision.add_argument("--id", required=True)
    _ = decision.add_argument("--answer", choices=("approved", "declined"), required=True)
    _ = decision.add_argument("--user-words", required=True)
    _ = decision.add_argument("--withdraw", default=argparse.SUPPRESS,
                              help="explain why the current ruling no longer stands")
    decision.set_defaults(handler=_decision)

    verdict = subparsers.add_parser("verdict", help="record a closure review's outcome")
    _ = verdict.add_argument("--session-dir", required=True)
    _ = verdict.add_argument("--phase", default="", help=PHASE_HELP)
    _ = verdict.add_argument("--id", required=True)
    _ = verdict.add_argument(
        "--state", choices=("accepted", "still_open", "reopened"), required=True
    )
    _ = verdict.add_argument("--evidence", default="")
    verdict.set_defaults(handler=_verdict)

    gate = subparsers.add_parser("gate", help="decide converged / dispatch / stop")
    _ = gate.add_argument("--session-dir", required=True)
    _ = gate.add_argument("--phase", default="", help=PHASE_HELP)
    gate.set_defaults(handler=_gate)

    dispatch = subparsers.add_parser("dispatch", help="record a fix round covering the batch")
    _ = dispatch.add_argument("--session-dir", required=True)
    _ = dispatch.add_argument("--phase", default="", help=PHASE_HELP)
    _ = dispatch.add_argument("--covers", required=True)
    _ = dispatch.add_argument("--keep", action="append", default=[])
    _ = dispatch.add_argument("--accepted-checks", default="")
    dispatch.set_defaults(handler=_dispatch)

    route = subparsers.add_parser("route", help="move open work to an owned follow-up Work Order")
    _ = route.add_argument("--id", required=True)
    _ = route.add_argument("--reason", choices=("spec-gap", "outside-kept"), required=True)
    _ = route.add_argument("--owner", required=True)
    _ = route.add_argument("--work-order", required=True)
    _ = route.add_argument("--due", required=True)
    _ = route.add_argument("--evidence", default="")
    route.set_defaults(handler=_route)

    astra = subparsers.add_parser("astra", help="record Astra's approach at the current fix attempt")
    _ = astra.add_argument("--id", required=True)
    _ = astra.add_argument("--note", required=True)
    outcome = astra.add_mutually_exclusive_group(required=True)
    _ = outcome.add_argument("--approach")
    _ = outcome.add_argument("--unavailable")
    astra.set_defaults(handler=_astra)

    exposure = subparsers.add_parser("exposure", help="record a P1 or P2 trial exposure")
    _ = exposure.add_argument("--proposal", choices=("P1", "P2"), required=True)
    _ = exposure.add_argument("--basis", required=True)
    _ = exposure.add_argument("--decision", default="")
    exposure.set_defaults(handler=_exposure)

    close_route = subparsers.add_parser("close-route", help="record a receiving checkpoint's closure")
    _ = close_route.add_argument("--from", required=True)
    _ = close_route.add_argument("--evidence", required=True)
    close_route.set_defaults(handler=_close_route)

    for command in (route, astra, exposure, close_route):
        _ = command.add_argument("--session-dir", required=True)
        _ = command.add_argument("--phase", default="", help=PHASE_HELP)

    for verb in ("landed", "abandon"):
        resolution = subparsers.add_parser(verb, help="resolve a repair round after its worker exits")
        _ = resolution.add_argument("--session-dir", required=True)
        _ = resolution.add_argument("--phase", default="", help=PHASE_HELP)
        resolution.set_defaults(handler=_resolve_round, resolution=verb, reason="", edits_landed=False)
        if verb == "abandon":
            _ = resolution.add_argument("--reason", required=True)
            _ = resolution.add_argument("--edits-landed", action="store_true", help="keep the repair attempt")


    status = subparsers.add_parser("status", help="print the whole ledger")
    _ = status.add_argument("--session-dir", required=True)
    _ = status.add_argument("--phase", default="", help=PHASE_HELP)
    status.set_defaults(handler=_status)

    return parser


def _command_settings(args: argparse.Namespace) -> conf.ConfigRead:
    """Use the file read that main made for this command."""
    value = cast(object, getattr(args, "config_read"))
    if not isinstance(value, conf.ConfigRead):
        raise TypeError("command configuration must be a ConfigRead")
    return value


def require_configuration(settings_read: conf.ConfigRead) -> None:
    problems: list[str] = []
    for key in ("MAX_FIX_ATTEMPTS", "MAX_REOPENS", "STALLED_ROUNDS", "RUNAWAY_ROUNDS",
                "ROUTING_FROM_ROUND", "ASTRA_AFTER_FAILED_FIXES", "MIN_REPAIR_BUDGET",
                "MAX_CONSECUTIVE_SAME_KIND_PASSES", "MAX_REVIEW_CANCELLATIONS", "REPAIR_ROUNDS_PER_FINDING"):
        try:
            if key == "REPAIR_ROUNDS_PER_FINDING":
                _ = settings_read.number(key)
            else:
                _ = settings_read.whole(key)
        except conf.Unusable as error:
            message = str(error)
            if "key is missing" in message:
                message = f"{key} is not set: {message}"
            elif "expected a whole number" in message or "expected a finite number" in message:
                raw = conf.effective_line("direct", key)[1]
                message = f"{key}={raw} is not a whole number: {message}"
            elif not conf.path("direct").is_file():
                message = f"cannot be read: {message}"
            problems.append(message)
    if problems:
        raise conf.Unusable("\n".join(problems))


def main() -> None:
    args = _build_parser().parse_args()
    settings_read = conf.read("direct")
    require_configuration(settings_read)
    args.config_read = settings_read
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
    try:
        main()
    except conf.Unusable as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
