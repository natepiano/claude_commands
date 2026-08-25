#!/usr/bin/env python3
"""Durable plan-delegate progress events, headers, and calibration statistics."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast


SCHEMA_VERSION = 1
MIN_CALIBRATION_SAMPLES = 5
# How far the percentage beside an ETA is assumed to be off when no calibration
# history has an opinion. Ten points is the width the recorded phase estimates
# have shown; a run with enough matching samples replaces it with its own
# measured error, so this only ever governs the first runs of a fresh history.
DEFAULT_PERCENT_SPREAD = 10.0
# The widest the best and worst case may stray from the reported rate: at worst
# the work is half as far along as it says, at best twice.
RATE_FACTOR_LIMIT = 2.0
STATE_FILENAME = "progress_history_state.json"
PROJECT_STARTED_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project started:\*\*[ \t]*(?P<value>.+?)[ \t]*$",
    re.MULTILINE,
)
PROJECT_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project:\*\*[^\n]*$",
    re.MULTILINE,
)

# Every phase heading in a delegate plan, in all three forms it takes over a
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

# A percentage is an estimate; a stage is a fact. These ceilings keep an
# optimistic estimate from claiming a gate that has not run — the failure mode
# was a phase sitting at 99% while its review loop was still unconverged.
PROGRESS_CAPS: dict[str, int] = {
    "implementation": 75,
    "initial_review": 85,
    "open_findings": 90,
    "closure": 95,
    "checkpoint": 98,
    "complete": 100,
}
PROJECT_CAP_BEFORE_COMPLETE = 99


class AgentIdentity(TypedDict):
    family: str
    model: str
    effort: str
    session_id: str


class CalibrationSample(TypedDict):
    percent: int
    raw_percent: int
    suggested_percent: int
    decision_source: str
    override_reason: str
    pass_kind: str
    main_model: str
    main_effort: str
    called_model: str
    called_effort: str
    hold_seconds: int
    unchanged_before_report_seconds: int
    remaining_at_report_seconds: int
    temporal_percent: float
    raw_bias_percentage_points: float
    suggested_bias_percentage_points: float
    reported_bias_percentage_points: float
    implied_total_error_seconds: float


class ProjectTiming(TypedDict):
    started_at: float
    source: str
    plan_doc: str


class StageWindow(TypedDict):
    """One row of the stage table: a launcher's pass or a main-agent activity."""

    instance_id: str
    kind: str
    fix_round: int
    label: str
    started_at: float
    elapsed: int
    status: str
    result: str
    delegate: str
    main: str


class FindingTally(TypedDict):
    opened: int
    landed: int
    accepted: int
    still_open: int
    reopened: int


def _history_root() -> Path:
    configured = os.environ.get("PLAN_DELEGATE_HISTORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "plan-delegate"


def _now_epoch() -> float:
    configured = os.environ.get("PLAN_DELEGATE_NOW_EPOCH")
    if configured:
        return float(configured)
    return time.time()


# The report interval lives in delegate.conf, the same file progress_timer.sh
# reads, so the next tick this header names and the timer that actually fires
# can never disagree. `PLAN_DELEGATE_CONFIG` overrides the path so the tests
# read a fixture rather than the machine's live settings.
DEFAULT_CONFIG_PATH = Path("~/.claude/config/delegate.conf").expanduser()
PROGRESS_INTERVAL_KEY = "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS"
TIMER_MARKER_FILENAME = "progress_timer"


def _config_path() -> Path:
    configured = os.environ.get("PLAN_DELEGATE_CONFIG")
    return Path(configured) if configured else DEFAULT_CONFIG_PATH


def _progress_interval_seconds() -> int | None:
    """Configured seconds between reports, or None when the key is unusable.

    A report is not a timer, so an unusable interval drops the next-tick clause
    instead of stopping the header. progress_timer.sh reads the same key and is
    the caller that fails loudly on it.
    """
    try:
        config_text = _config_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in config_text.splitlines():
        key, separator, value = line.split("#", 1)[0].strip().partition("=")
        if not separator or key.strip() != PROGRESS_INTERVAL_KEY:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


PASS_OWNER_ENV = "PLAN_DELEGATE_PASS_OWNER"
PASS_OWNER_TOKEN = "launcher"

PASS_OWNERSHIP_RULE = (
    "Pass lifecycle belongs to the launcher. implement.sh and review.sh already "
    "record pass_started and pass_finished around the worker they wait on, so a "
    "hand-written call forges a pass that never ran. findings.py gate counts "
    "passes to decide whether a phase is converging, and forged passes stop a run "
    "for a condition that never happened. Launch the work through implement.sh or "
    "review.sh. The single exception is a launcher the orchestrator killed: close "
    "its still-open pass with 'finish-pass --status canceled --orphaned-launcher'."
)


def _launcher_owned() -> bool:
    return os.environ.get(PASS_OWNER_ENV) == PASS_OWNER_TOKEN


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


def _read_state(session_dir: Path) -> dict[str, object]:
    path = _state_path(session_dir)
    try:
        state = _json_object(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"Unable to read progress state {path}: {error}") from error
    if state is None:
        raise SystemExit(f"Invalid progress state: {path}")
    return state


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


def _append_event(state: dict[str, object], event: dict[str, object]) -> None:
    history_file_value = _string(state.get("history_file"))
    if not history_file_value:
        raise SystemExit("Progress state has no history_file")
    history_file = Path(history_file_value)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    with history_file.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _main_identity_from_codex(session_id: str) -> AgentIdentity | None:
    session_root = Path.home() / ".codex" / "sessions"
    matches = list(session_root.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return None
    model = ""
    effort = ""
    try:
        with matches[0].open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                event = _json_object(line)
                if event is None or event.get("type") != "turn_context":
                    continue
                payload = _object_dict(event.get("payload"))
                if payload is None:
                    continue
                candidate_model = _string(payload.get("model"))
                candidate_effort = _string(payload.get("effort"))
                if candidate_model:
                    model = candidate_model
                if candidate_effort:
                    effort = candidate_effort
    except OSError:
        return None
    if not model:
        return None
    return AgentIdentity(
        family="codex",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    )


def _main_identity_from_claude(session_id: str) -> AgentIdentity | None:
    project_root = Path.home() / ".claude" / "projects"
    matches = list(project_root.rglob(f"{session_id}.jsonl"))
    if not matches:
        return None
    model = ""
    effort = ""
    try:
        with matches[0].open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                event = _json_object(line)
                if event is None or event.get("type") != "assistant":
                    continue
                message = _object_dict(event.get("message"))
                candidate_model = _string(message.get("model")) if message else ""
                candidate_effort = _string(event.get("effort"))
                if candidate_model:
                    model = candidate_model
                if candidate_effort:
                    effort = candidate_effort
    except OSError:
        return None
    if not model:
        return None
    return AgentIdentity(
        family="claude",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    )


def _detect_main_identity(args: argparse.Namespace) -> AgentIdentity:
    explicit_model = _arg_string(args, "main_model")
    explicit_family = _arg_string(args, "main_family")
    explicit_effort = _arg_string(args, "main_effort", "unset")
    explicit_session = _arg_string(args, "main_session_id")
    if explicit_model:
        return AgentIdentity(
            family=explicit_family or "unknown",
            model=explicit_model,
            effort=explicit_effort or "unset",
            session_id=explicit_session,
        )

    codex_session = os.environ.get("CODEX_THREAD_ID", "")
    if codex_session:
        identity = _main_identity_from_codex(codex_session)
        if identity is not None:
            return identity

    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if claude_session:
        identity = _main_identity_from_claude(claude_session)
        if identity is not None:
            return identity

    return AgentIdentity(
        family=explicit_family or "unknown",
        model="unknown",
        effort=explicit_effort or "unset",
        session_id=explicit_session or codex_session or claude_session,
    )


def _refresh_main_identity(state: dict[str, object]) -> None:
    """Re-read the orchestrator's identity whenever a window opens.

    `start-run` detects it once, and the main agent's model or effort can change
    part way through a run. Detection that comes back unknown leaves the stored
    identity alone: a window that cannot answer must not erase the answer
    already recorded.
    """
    detected = _detect_main_identity(
        argparse.Namespace(
            main_model="",
            main_family="",
            main_effort="",
            main_session_id="",
        )
    )
    if detected["model"] == "unknown":
        return
    state["main_agent"] = detected


def _git_value(working_dir: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=working_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _iso_epoch(value: str, context: str) -> float:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SystemExit(
            f"Malformed Project started timestamp in {context}: {value}"
        ) from error
    if parsed.tzinfo is None:
        raise SystemExit(
            f"Project started timestamp must include a timezone in {context}: {value}"
        )
    return parsed.timestamp()


def _plan_path(working_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (working_dir / path).resolve()


def _count_plan_phases(plan_path: Path) -> dict[str, object]:
    """Count a plan's phases by heading, classifying every form.

    Returns done/todo/total plus any heading this could not classify. An
    unclassifiable heading is reported rather than guessed at: a miscount here
    silently corrupts every project percentage the run reports.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return {"available": False, "reason": f"unable to read {plan_path}"}
    done = 0
    todo = 0
    seen: set[str] = set()
    order: list[str] = []
    duplicates: list[str] = []
    for match in PHASE_HEADING_PATTERN.finditer(text):
        rest = match.group("rest")
        status_match = PHASE_STATUS_PATTERN.search(rest)
        if status_match is None and not PHASE_TITLE_PATTERN.match(rest):
            # `### Phase 12 Review` and friends — a section, not a phase.
            continue
        identifier = match.group("id")
        if identifier in seen:
            duplicates.append(identifier)
            continue
        seen.add(identifier)
        order.append(identifier)
        # No status marker means the phase was shrunk into an as-built record,
        # which only ever happens after it completed.
        if status_match is not None and status_match.group("status") == "todo":
            todo += 1
        else:
            done += 1
    total = done + todo
    if total == 0:
        return {"available": False, "reason": f"no phase headings in {plan_path}"}
    return {
        "available": True,
        "done": done,
        "todo": todo,
        "total": total,
        "order": order,
        "duplicate_ids": sorted(set(duplicates)),
    }


def _plan_derived_project_percent(
    counts: dict[str, object], phase_percent: int
) -> int | None:
    """Project completion from phase counts, crediting the phase in flight.

    The active phase contributes its own percentage as a fraction of one phase,
    so a project clock advances during a long phase instead of stepping only at
    checkpoints.
    """
    if not counts.get("available"):
        return None
    done = counts.get("done")
    total = counts.get("total")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0:
        return None
    completed = done + max(0, min(100, phase_percent)) / 100
    return max(0, min(100, round(100 * completed / total)))


def _read_plan_project_start(plan_path: Path) -> float | None:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to read plan document {plan_path}: {error}") from error
    matches = list(PROJECT_STARTED_PATTERN.finditer(text))
    if len(matches) > 1:
        raise SystemExit(f"Plan document has multiple Project started fields: {plan_path}")
    if not matches:
        return None
    return _iso_epoch(matches[0].group("value"), str(plan_path))


def _persist_plan_project_start(plan_path: Path, value: str) -> None:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to read plan document {plan_path}: {error}") from error
    project_match = PROJECT_PATTERN.search(text)
    if project_match is None:
        raise SystemExit(f"Plan document has no Delegation Context Project field: {plan_path}")
    updated = (
        text[: project_match.end()]
        + f"\n- **Project started:** {value}"
        + text[project_match.end() :]
    )
    try:
        _ = plan_path.write_text(updated, encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to write Project started to {plan_path}: {error}") from error


def _explicit_plan_timing(
    working_dir: Path,
    plan_doc: str,
    now: float,
) -> ProjectTiming:
    plan_path = _plan_path(working_dir, plan_doc)
    existing = _read_plan_project_start(plan_path)
    if existing is not None:
        return ProjectTiming(
            started_at=existing,
            source="plan_field",
            plan_doc=str(plan_path),
        )

    try:
        git_path = plan_path.relative_to(working_dir)
    except ValueError:
        commit_times = ""
    else:
        commit_times = _git_value(
            working_dir,
            "log",
            "--follow",
            "--format=%cI",
            "--",
            str(git_path),
        )
    committed = [line.strip() for line in commit_times.splitlines() if line.strip()]
    if committed:
        value = committed[-1]
        started_at = _iso_epoch(value, f"Git history for {plan_path}")
        source = "plan_git"
    else:
        value = _iso_time(now)
        started_at = now
        source = "plan_run"
    _persist_plan_project_start(plan_path, value)
    return ProjectTiming(
        started_at=started_at,
        source=source,
        plan_doc=str(plan_path),
    )


def _run_started_event(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    event = _json_object(first_line)
    if event is None or _string(event.get("event_type")) != "run_started":
        return None
    return event


def _historical_project_timing(
    working_dir: Path,
    branch: str,
) -> ProjectTiming | None:
    runs_dir = _history_root() / "runs"
    latest_event: dict[str, object] | None = None
    latest_started_at = -1.0
    try:
        history_paths = runs_dir.glob("*.jsonl")
    except OSError:
        return None
    for history_path in history_paths:
        event = _run_started_event(history_path)
        if event is None:
            continue
        if (
            _string(event.get("working_dir")) != str(working_dir)
            or _string(event.get("branch")) != branch
        ):
            continue
        project_plan_doc = _string(event.get("project_plan_doc")) or _string(
            event.get("plan_doc")
        )
        if not project_plan_doc:
            continue
        run_started_at = _number(event.get("run_started_at"), -1.0)
        if run_started_at > latest_started_at:
            latest_event = event
            latest_started_at = run_started_at
    if latest_event is None:
        return None

    project_plan_doc = _string(latest_event.get("project_plan_doc")) or _string(
        latest_event.get("plan_doc")
    )
    plan_path = _plan_path(working_dir, project_plan_doc)
    if plan_path.is_file():
        existing = _read_plan_project_start(plan_path)
        if existing is not None:
            return ProjectTiming(
                started_at=existing,
                source="history_plan_field",
                plan_doc=str(plan_path),
            )
    return ProjectTiming(
        started_at=_number(latest_event.get("project_started_at")),
        source="history_recorded",
        plan_doc=str(plan_path),
    )


def _resolve_project_timing(
    working_dir: Path,
    branch: str,
    plan_doc: str,
    now: float,
) -> ProjectTiming:
    if plan_doc:
        return _explicit_plan_timing(working_dir, plan_doc, now)
    historical = _historical_project_timing(working_dir, branch)
    if historical is not None:
        return historical
    return ProjectTiming(started_at=now, source="run_started", plan_doc="")


def _ensure_project_timing(
    session_dir: Path,
    state: dict[str, object],
    now: float,
) -> dict[str, object]:
    if _string(state.get("project_start_source")):
        return state
    working_dir = Path(_string(state.get("working_dir"))).resolve()
    timing = _resolve_project_timing(
        working_dir,
        _string(state.get("branch")),
        _string(state.get("plan_doc")),
        now,
    )
    state["project_started_at"] = timing["started_at"]
    state["project_start_source"] = timing["source"]
    state["project_plan_doc"] = timing["plan_doc"]
    _write_state(session_dir, state)
    return state


def _event(state: dict[str, object], event_type: str, now: float) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": _iso_time(now),
        "timestamp_epoch": now,
        "run_id": _string(state.get("run_id")),
        "working_dir": _string(state.get("working_dir")),
        "worktree": _string(state.get("worktree")),
        "branch": _string(state.get("branch")),
        "plan_doc": _string(state.get("plan_doc")),
        "run_started_at": _number(state.get("run_started_at")),
        "project_started_at": _number(state.get("project_started_at")),
        "project_start_source": _string(state.get("project_start_source")),
        "project_plan_doc": _string(state.get("project_plan_doc")),
        "main_agent": state.get("main_agent", {}),
    }
    phase = _object_dict(state.get("phase"))
    if phase is not None:
        event.update(
            {
                "phase_instance_id": _string(phase.get("instance_id")),
                "phase_id": _string(phase.get("id")),
                "phase_title": _string(phase.get("title")),
                "phase_started_at": _number(phase.get("started_at")),
            }
        )
    # Only the window that is open right now identifies an event. A finished
    # pass stays in state, so stamping it unconditionally attributed every
    # activity -- and every progress report made during one -- to whichever
    # delegate happened to run before it.
    current_pass = _object_dict(state.get("pass"))
    if current_pass is not None and _string(current_pass.get("status")) == "active":
        event.update(
            {
                "pass_instance_id": _string(current_pass.get("instance_id")),
                "pass_kind": _string(current_pass.get("kind")),
                "fix_pass": _integer(current_pass.get("fix_pass")),
                "pass_activity": _string(current_pass.get("activity")),
                "pass_started_at": _number(current_pass.get("started_at")),
                "called_agent": current_pass.get("called_agent", {}),
                "called_task": _string(current_pass.get("called_task")),
            }
        )
    activity = _object_dict(state.get("activity"))
    if activity is not None and _string(activity.get("status")) == "active":
        event.update(
            {
                "activity_instance_id": _string(activity.get("instance_id")),
                "activity_label": _string(activity.get("label")),
                "activity_text": _string(activity.get("activity")),
                "activity_started_at": _number(activity.get("started_at")),
            }
        )
    return event


def _close_active_pass(
    session_dir: Path,
    state: dict[str, object],
    status: str,
    now: float,
) -> None:
    current_pass = _object_dict(state.get("pass"))
    if current_pass is None or _string(current_pass.get("status")) != "active":
        return
    event = _event(state, "pass_finished", now)
    event["status"] = status
    event["pass_elapsed_seconds"] = max(
        0,
        int(now - _number(current_pass.get("started_at"), now)),
    )
    _append_event(state, event)
    current_pass["status"] = status
    current_pass["finished_at"] = now
    _write_state(session_dir, state)


def _close_active_activity(
    session_dir: Path,
    state: dict[str, object],
    status: str,
    result: str,
    now: float,
) -> None:
    activity = _object_dict(state.get("activity"))
    if activity is None or _string(activity.get("status")) != "active":
        return
    event = _event(state, "activity_finished", now)
    event["status"] = status
    event["activity_result"] = result
    event["activity_elapsed_seconds"] = max(
        0,
        int(now - _number(activity.get("started_at"), now)),
    )
    _append_event(state, event)
    activity["status"] = status
    activity["result"] = result
    activity["finished_at"] = now
    _write_state(session_dir, state)


def _start_activity(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase = _object_dict(state.get("phase"))
    if phase is None or _string(phase.get("status")) != "active":
        raise SystemExit("Start a phase before starting an activity")
    now = _now_epoch()
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = _read_state(session_dir)
    _refresh_main_identity(state)
    activity: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "label": _arg_string(args, "label", "Work") or "Work",
        "activity": _arg_string(args, "activity"),
        "started_at": now,
        "status": "active",
    }
    state["activity"] = activity
    _write_state(session_dir, state)
    _append_event(state, _event(state, "activity_started", now))


def _finish_activity(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    status = _arg_string(args, "status", "completed") or "completed"
    _close_active_activity(
        session_dir,
        state,
        status,
        _arg_string(args, "result"),
        _now_epoch(),
    )


def _start_run(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    existing = _state_path(session_dir)
    if existing.exists():
        state = _ensure_project_timing(
            session_dir,
            _read_state(session_dir),
            _now_epoch(),
        )
        print(_string(state.get("history_file"), str(existing)))
        return

    working_dir_value = _arg_string(args, "working_dir")
    if not working_dir_value:
        raise SystemExit("--working-dir is required")
    working_dir = Path(working_dir_value).expanduser().resolve()
    now = _now_epoch()
    run_id = session_dir.name
    branch = _git_value(working_dir, "branch", "--show-current")
    if not branch:
        short_hash = _git_value(working_dir, "rev-parse", "--short", "HEAD")
        branch = f"detached@{short_hash or 'unknown'}"
    plan_doc = _arg_string(args, "plan_doc")
    project_timing = _resolve_project_timing(working_dir, branch, plan_doc, now)
    history_file = _history_root() / "runs" / f"{run_id}.jsonl"
    main_agent = _detect_main_identity(args)
    if main_agent["model"] == "unknown":
        raise SystemExit(
            "Unable to detect the main agent model and effort; pass explicit --main-* values"
        )
    state: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "history_file": str(history_file),
        "working_dir": str(working_dir),
        "worktree": working_dir.name,
        "branch": branch,
        "plan_doc": plan_doc,
        "run_started_at": now,
        "project_started_at": project_timing["started_at"],
        "project_start_source": project_timing["source"],
        "project_plan_doc": project_timing["plan_doc"],
        "main_agent": main_agent,
        "phase": None,
        "pass": None,
        "project_last_percent": None,
        "project_percent_started_at": None,
        "phase_last_percent": None,
        "phase_percent_started_at": None,
        "last_percent": None,
        "percent_started_at": None,
        "pending_calibration": None,
        "status": "active",
    }
    _write_state(session_dir, state)
    _append_event(state, _event(state, "run_started", now))
    print(history_file)


def _work_order_metrics(work_order_file: str) -> dict[str, object]:
    """Measure a phase's Work Order so phase size can be correlated with outcome.

    Recorded only. Thresholds belong upstream in `/plan:to_phased_plan`, and
    setting them from one bad phase is guesswork — this is how the data arrives.
    """
    if not work_order_file:
        return {}
    try:
        text = Path(work_order_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    targets: set[str] = set()
    quoted_spans = cast(list[str], re.findall(r"`([^`\n]+)`", text))
    for quoted in quoted_spans:
        candidate = quoted.strip()
        if "/" in candidate or re.search(r"\.[A-Za-z0-9]{1,5}$", candidate):
            targets.add(candidate)
    return {
        "work_order_lines": sum(1 for line in lines if line.strip()),
        "work_order_words": len(text.split()),
        "work_order_file_targets": len(targets),
        "work_order_top_level_bullets": sum(
            1 for line in lines if re.match(r"^[-*] ", line)
        ),
    }


def _start_phase(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = _now_epoch()
    active = _object_dict(state.get("phase"))
    if active is not None and _string(active.get("status")) == "active":
        if _string(active.get("id")) == _arg_string(args, "phase_id"):
            return
        raise SystemExit(
            f"Phase {_string(active.get('id'))} is still active; finish it before starting another"
        )
    phase: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "id": _arg_string(args, "phase_id", "ad hoc"),
        "title": _arg_string(args, "phase_title", "Ad hoc work"),
        "started_at": now,
        "status": "active",
    }
    state["phase"] = phase
    state["pass"] = None
    state["phase_last_percent"] = None
    state["phase_percent_started_at"] = None
    state["last_percent"] = None
    state["percent_started_at"] = None
    state["pending_calibration"] = None
    _write_state(session_dir, state)
    event = _event(state, "phase_started", now)
    event.update(_work_order_metrics(_arg_string(args, "work_order_file")))
    _append_event(state, event)


def _start_pass(args: argparse.Namespace) -> None:
    if not _launcher_owned():
        raise SystemExit(f"start-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase = _object_dict(state.get("phase"))
    if phase is None or _string(phase.get("status")) != "active":
        raise SystemExit("Start a phase before starting a pass")
    now = _now_epoch()
    _close_active_pass(session_dir, state, "interrupted", now)
    state = _read_state(session_dir)
    pass_kind = _arg_string(args, "pass_kind")
    fix_pass = _arg_integer(args, "fix_pass")
    called_agent = AgentIdentity(
        family=_arg_string(args, "called_family", "unknown"),
        model=_arg_string(args, "called_model", "unknown"),
        effort=_arg_string(args, "called_effort", "unset") or "unset",
        session_id="",
    )
    _refresh_main_identity(state)
    current_pass: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "kind": pass_kind,
        "fix_pass": fix_pass,
        "activity": _arg_string(args, "activity"),
        "started_at": now,
        "called_task": _arg_string(args, "called_task"),
        "called_agent": called_agent,
        "status": "active",
    }
    state["pass"] = current_pass
    _write_state(session_dir, state)
    event = _event(state, "pass_started", now)
    _append_event(state, event)


def _finish_pass(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    status = _arg_string(args, "status")
    orphaned = bool(getattr(args, "orphaned_launcher", False))
    if not _launcher_owned():
        if not orphaned:
            raise SystemExit(f"finish-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
        if status != "canceled":
            raise SystemExit(
                "--orphaned-launcher closes the pass of a launcher the orchestrator "
                + f"killed, so it takes --status canceled, not {status}"
            )
    state = _read_state(session_dir)
    if orphaned:
        current = _object_dict(state.get("pass"))
        if current is None or _string(current.get("status")) != "active":
            raise SystemExit(
                "No pass is open, so no launcher was orphaned and nothing was recorded"
            )
    _close_active_pass(session_dir, state, status, _now_epoch())


def _load_events() -> tuple[list[dict[str, object]], int]:
    events: list[dict[str, object]] = []
    ignored = 0
    runs_dir = _history_root() / "runs"
    if not runs_dir.exists():
        return events, ignored
    for path in sorted(runs_dir.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                lines = handle.read().splitlines()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            ignored += 1
            continue
        for line in lines:
            event = _json_object(line)
            if event is None or _integer(event.get("schema_version")) != SCHEMA_VERSION:
                ignored += 1
                continue
            events.append(event)
    return events, ignored


def _agent_fields(event: dict[str, object], key: str) -> tuple[str, str]:
    identity = _object_dict(event.get(key))
    if identity is None:
        return "", ""
    return _string(identity.get("model")), _string(identity.get("effort"))


def _calibration_samples(events: list[dict[str, object]]) -> list[CalibrationSample]:
    phase_finishes: dict[str, float] = {}
    progress_by_phase: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        phase_instance_id = _string(event.get("phase_instance_id"))
        if not phase_instance_id:
            continue
        event_type = _string(event.get("event_type"))
        if event_type == "phase_finished" and _string(event.get("status")) == "completed":
            phase_finishes[phase_instance_id] = _number(event.get("timestamp_epoch"))
        elif event_type == "progress_reported":
            progress_by_phase[phase_instance_id].append(event)

    samples: list[CalibrationSample] = []
    for phase_instance_id, reports in progress_by_phase.items():
        phase_finished_at = phase_finishes.get(phase_instance_id)
        if phase_finished_at is None:
            continue
        reports.sort(key=lambda event: _number(event.get("timestamp_epoch")))
        index = 0
        while index < len(reports):
            first = reports[index]
            percent = _integer(first.get("percent"))
            next_index = index + 1
            while next_index < len(reports) and _integer(reports[next_index].get("percent")) == percent:
                next_index += 1
            streak_ended_at = (
                _number(reports[next_index].get("timestamp_epoch"))
                if next_index < len(reports)
                else phase_finished_at
            )
            streak_started_at = _number(first.get("timestamp_epoch"))
            seen_raw_percentages: set[int] = set()
            for report in reports[index:next_index]:
                raw_percent = _integer(report.get("raw_percent"), percent)
                if raw_percent in seen_raw_percentages:
                    continue
                seen_raw_percentages.add(raw_percent)
                reported_at = _number(report.get("timestamp_epoch"))
                phase_started_at = _number(report.get("phase_started_at"))
                phase_duration = phase_finished_at - phase_started_at
                if phase_duration <= 0 or reported_at < phase_started_at:
                    continue
                phase_elapsed = reported_at - phase_started_at
                temporal_percent = max(0.0, min(100.0, 100.0 * phase_elapsed / phase_duration))
                suggested_percent = _integer(report.get("suggested_percent"), raw_percent)
                main_model, main_effort = _agent_fields(report, "main_agent")
                called_model, called_effort = _agent_fields(report, "called_agent")
                implied_total = (
                    phase_elapsed / (percent / 100.0) if percent > 0 else phase_duration
                )
                samples.append(
                    CalibrationSample(
                        percent=percent,
                        raw_percent=raw_percent,
                        suggested_percent=suggested_percent,
                        decision_source=_string(report.get("decision_source"), "legacy"),
                        override_reason=_string(report.get("override_reason")),
                        pass_kind=_string(report.get("pass_kind")),
                        main_model=main_model,
                        main_effort=main_effort,
                        called_model=called_model,
                        called_effort=called_effort,
                        hold_seconds=max(0, int(streak_ended_at - streak_started_at)),
                        unchanged_before_report_seconds=max(
                            0,
                            int(reported_at - streak_started_at),
                        ),
                        remaining_at_report_seconds=max(
                            0,
                            int(phase_finished_at - reported_at),
                        ),
                        temporal_percent=temporal_percent,
                        raw_bias_percentage_points=raw_percent - temporal_percent,
                        suggested_bias_percentage_points=suggested_percent - temporal_percent,
                        reported_bias_percentage_points=percent - temporal_percent,
                        implied_total_error_seconds=implied_total - phase_duration,
                    )
                )
            index = next_index
    return samples


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _round_metric(value: float) -> float:
    return round(value, 2)


def _sample_metrics(samples: list[CalibrationSample], current_hold: int) -> dict[str, object]:
    if not samples:
        return {
            "sample_count": 0,
            "comparable_after_current_hold_count": 0,
            "decision_source_counts": {},
            "override_sample_count": 0,
        }
    decision_source_counts: dict[str, int] = defaultdict(int)
    for sample in samples:
        decision_source_counts[sample["decision_source"]] += 1
    survivors = [sample for sample in samples if sample["hold_seconds"] >= current_hold]
    remaining_after_hold = [
        max(
            0.0,
            sample["remaining_at_report_seconds"]
            + sample["unchanged_before_report_seconds"]
            - current_hold,
        )
        for sample in survivors
    ]
    return {
        "sample_count": len(samples),
        "completed_raw_estimate_sample_count": len(samples),
        "median_unchanged_seconds": int(statistics.median(sample["hold_seconds"] for sample in samples)),
        "p75_unchanged_seconds": int(_percentile([float(sample["hold_seconds"]) for sample in samples], 0.75)),
        "p90_unchanged_seconds": int(_percentile([float(sample["hold_seconds"]) for sample in samples], 0.90)),
        "median_remaining_at_report_seconds": int(
            statistics.median(sample["remaining_at_report_seconds"] for sample in samples)
        ),
        "median_temporal_percent_at_report": _round_metric(
            statistics.median(sample["temporal_percent"] for sample in samples)
        ),
        "median_reported_percent": _round_metric(
            statistics.median(sample["percent"] for sample in samples)
        ),
        "median_suggested_percent": _round_metric(
            statistics.median(sample["suggested_percent"] for sample in samples)
        ),
        "median_raw_bias_percentage_points": _round_metric(
            statistics.median(sample["raw_bias_percentage_points"] for sample in samples)
        ),
        "median_reported_bias_percentage_points": _round_metric(
            statistics.median(sample["reported_bias_percentage_points"] for sample in samples)
        ),
        "median_raw_absolute_error_percentage_points": _round_metric(
            statistics.median(abs(sample["raw_bias_percentage_points"]) for sample in samples)
        ),
        "median_suggested_absolute_error_percentage_points": _round_metric(
            statistics.median(
                abs(sample["suggested_bias_percentage_points"]) for sample in samples
            )
        ),
        "median_reported_absolute_error_percentage_points": _round_metric(
            statistics.median(abs(sample["reported_bias_percentage_points"]) for sample in samples)
        ),
        "median_suggested_improvement_percentage_points": _round_metric(
            statistics.median(
                abs(sample["raw_bias_percentage_points"])
                - abs(sample["suggested_bias_percentage_points"])
                for sample in samples
            )
        ),
        "median_reported_improvement_percentage_points": _round_metric(
            statistics.median(
                abs(sample["raw_bias_percentage_points"])
                - abs(sample["reported_bias_percentage_points"])
                for sample in samples
            )
        ),
        "decision_source_counts": dict(sorted(decision_source_counts.items())),
        "override_sample_count": sum(
            sample["decision_source"] == "override" for sample in samples
        ),
        "median_implied_total_error_seconds": int(
            statistics.median(sample["implied_total_error_seconds"] for sample in samples)
        ),
        "comparable_after_current_hold_count": len(survivors),
        "median_remaining_after_current_hold_seconds": (
            int(statistics.median(remaining_after_hold)) if remaining_after_hold else None
        ),
    }


def _matching_scope(
    samples: list[CalibrationSample],
    candidate_percent: int,
    state: dict[str, object],
) -> tuple[str, list[CalibrationSample]]:
    exact = [sample for sample in samples if sample["raw_percent"] == candidate_percent]
    candidates = exact
    percent_scope = "exact_percent"
    if len(candidates) < MIN_CALIBRATION_SAMPLES:
        nearby = [
            sample
            for sample in samples
            if abs(sample["raw_percent"] - candidate_percent) <= 5
        ]
        if len(nearby) > len(candidates):
            candidates = nearby
            percent_scope = "within_5_percentage_points"

    # Match on the open window, the same rule `_event` records by. A finished
    # pass left in state would otherwise match this activity's report against
    # samples from a delegate that is no longer running.
    active_pass = _object_dict(state.get("pass"))
    current_pass = (
        active_pass
        if active_pass is not None and _string(active_pass.get("status")) == "active"
        else {}
    )
    main_agent = _object_dict(state.get("main_agent")) or {}
    called_agent = _object_dict(current_pass.get("called_agent")) or {}
    pass_kind = _string(current_pass.get("kind"))
    main_model = _string(main_agent.get("model"))
    main_effort = _string(main_agent.get("effort"))
    called_model = _string(called_agent.get("model"))
    called_effort = _string(called_agent.get("effort"))

    filters: list[tuple[str, list[CalibrationSample]]] = [
        (
            "main_called_pass",
            [
                sample
                for sample in candidates
                if sample["pass_kind"] == pass_kind
                and sample["main_model"] == main_model
                and sample["main_effort"] == main_effort
                and sample["called_model"] == called_model
                and sample["called_effort"] == called_effort
            ],
        ),
        (
            "called_pass",
            [
                sample
                for sample in candidates
                if sample["pass_kind"] == pass_kind
                and sample["called_model"] == called_model
                and sample["called_effort"] == called_effort
            ],
        ),
        (
            "pass",
            [sample for sample in candidates if sample["pass_kind"] == pass_kind],
        ),
        ("all_models_and_passes", candidates),
    ]
    for scope, scoped_samples in filters:
        if len(scoped_samples) >= MIN_CALIBRATION_SAMPLES:
            return f"{percent_scope}:{scope}", scoped_samples
    return f"{percent_scope}:insufficient", candidates


def _current_hold_seconds(state: dict[str, object], candidate_percent: int, now: float) -> int:
    last_percent = _integer(
        state.get("phase_last_percent"),
        _integer(state.get("last_percent"), -1),
    )
    if last_percent != candidate_percent:
        return 0
    started_at = _number(
        state.get("phase_percent_started_at"),
        _number(state.get("percent_started_at"), now),
    )
    return max(0, int(now - started_at))


def _calibrate(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = _now_epoch()
    candidate = _arg_integer(args, "candidate_percent")
    if not 0 <= candidate <= 100:
        raise SystemExit("--candidate-percent must be between 0 and 100")
    events, ignored = _load_events()
    all_samples = _calibration_samples(events)
    scope, samples = _matching_scope(all_samples, candidate, state)
    metrics = _sample_metrics(samples, 0)
    sample_count = _integer(metrics.get("sample_count"))
    median_bias = _number(metrics.get("median_raw_bias_percentage_points"))
    suggestion = candidate
    if sample_count >= MIN_CALIBRATION_SAMPLES:
        corrected = max(5.0, min(95.0, candidate - median_bias))
        suggestion = int(5 * round(corrected / 5.0))
    current_hold = _current_hold_seconds(state, suggestion, now)
    metrics = _sample_metrics(samples, current_hold)
    calibration: dict[str, object] = {
        "calibration_id": str(uuid.uuid4()),
        "candidate_percent": candidate,
        "suggested_percent": suggestion,
        "apply_suggestion": sample_count >= MIN_CALIBRATION_SAMPLES,
        "minimum_samples": MIN_CALIBRATION_SAMPLES,
        "scope": scope,
        "current_unchanged_seconds": current_hold,
        "ignored_history_rows": ignored,
        **metrics,
    }
    state["pending_calibration"] = calibration
    _write_state(session_dir, state)
    print(json.dumps(calibration, indent=2, sort_keys=True))


def _format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if days:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} {hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _pass_display(current_pass: dict[str, object]) -> str:
    # An activity is main-agent work with no convergence meaning, so it carries a
    # plain label instead of a pass kind. findings.py counts passes, never these.
    label = _string(current_pass.get("label"))
    if label:
        return label
    pass_kind = _string(current_pass.get("kind"))
    if pass_kind == "fix":
        return f"Fix {_integer(current_pass.get('fix_pass'))}"
    return {
        "impl": "Impl",
        "review": "Review",
        "arch": "Arch",
    }.get(pass_kind, pass_kind.title() or "Pass")


def _progress_decision(
    calibration: dict[str, object] | None,
    raw_percent: int,
    reported_percent: int,
    override_reason: str,
    scope: str,
) -> dict[str, object]:
    reason_option = (
        "--override-reason" if scope == "legacy" else f"--{scope}-override-reason"
    )
    if calibration is None:
        suggested_percent = raw_percent
        apply_suggestion = False
        historical_bias: float | None = None
    else:
        candidate_percent = _integer(calibration.get("candidate_percent"), raw_percent)
        if candidate_percent != raw_percent:
            raise SystemExit(
                f"The pending {scope} calibration candidate does not match its raw percent; "
                + "run calibrate again"
            )
        suggested_percent = _integer(calibration.get("suggested_percent"), raw_percent)
        apply_suggestion = calibration.get("apply_suggestion") is True
        bias_value = calibration.get("median_raw_bias_percentage_points")
        historical_bias = (
            _number(bias_value) if isinstance(bias_value, int | float) else None
        )

    if apply_suggestion and reported_percent == suggested_percent:
        decision_source = "calibrated"
    elif not apply_suggestion and reported_percent == raw_percent:
        decision_source = "raw"
    else:
        decision_source = "override"

    cleaned_reason = override_reason.strip()
    if decision_source == "override" and not cleaned_reason:
        raise SystemExit(
            f"{reason_option} is required when the reported percentage differs "
            + "from the applicable raw or calibrated value"
        )
    if decision_source != "override" and cleaned_reason:
        raise SystemExit(f"{reason_option} is valid only for an override decision")

    return {
        "decision_source": decision_source,
        "override_reason": cleaned_reason,
        "suggested_percent": suggested_percent,
        "historical_bias_percentage_points": historical_bias,
        "suggested_adjustment_percentage_points": suggested_percent - raw_percent,
        "reported_adjustment_percentage_points": reported_percent - raw_percent,
    }


def _assessment_timing(
    state: dict[str, object],
    scope: str,
    percent: int,
    now: float,
) -> tuple[float, int]:
    last_key = f"{scope}_last_percent"
    started_key = f"{scope}_percent_started_at"
    if _integer(state.get(last_key), -1) == percent:
        started_at = _number(state.get(started_key), now)
        return started_at, max(0, int(now - started_at))
    state[last_key] = percent
    state[started_key] = now
    return now, 0


def _prefixed_decision(scope: str, decision: dict[str, object]) -> dict[str, object]:
    return {f"{scope}_{key}": value for key, value in decision.items()}


def _eta_seconds(percent: int, elapsed: int) -> int | None:
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


def _next_report_at(session_dir: Path, now: float) -> float | None:
    """When the next progress report is due, in epoch seconds.

    progress_timer.sh clears its marker as it ticks, so at the usual reporting
    moment -- after the tick, before the next timer is armed -- no marker exists
    and the configured interval supplies the answer. A marker still present and
    still ahead of the clock is a timer genuinely armed right now, and that
    deadline beats an interval added to the current time.
    """
    try:
        marker_text = (session_dir / TIMER_MARKER_FILENAME).read_text(encoding="utf-8")
    except OSError:
        marker_text = ""
    for line in marker_text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "deadline_epoch":
            continue
        try:
            deadline = float(value.strip())
        except ValueError:
            break
        if deadline > now:
            return deadline
        break
    interval = _progress_interval_seconds()
    return now + interval if interval is not None else None


def _clock_line(now: float, next_report_at: float | None) -> str:
    """Anchor the durations above to wall-clock time and name the next report.

    Every other number in the header is a duration, which says how long but
    never when. This line is the one place a reader can tell whether the report
    they are looking at is current and how long until the next one lands.
    """
    now_local = datetime.fromtimestamp(now)
    line = f"**now {now_local:%Y-%m-%d %H:%M:%S}"
    if next_report_at is not None:
        next_local = datetime.fromtimestamp(next_report_at)
        stamp = (
            f"{next_local:%H:%M:%S}"
            if next_local.date() == now_local.date()
            else f"{next_local:%Y-%m-%d %H:%M:%S}"
        )
        line += f" - next report {stamp}"
    return line + "**"


STAGE_HEADERS: tuple[str, ...] = (
    "Stage",
    "Main",
    "Delegate",
    "Start",
    "Elapsed",
    "Result",
)
SUMMARY_HEADERS: tuple[str, ...] = ("Scope", "%", "Elapsed", "ETA", "Unchanged")
# Every summary column holding a magnitude or a time, named rather than indexed
# so the alignment follows a column that moves. The two scope labels and the
# phase tally read as words and stay left.
RIGHT_ALIGNED_SUMMARY_COLUMNS = frozenset({"%", "Elapsed", "ETA", "ETA low", "ETA high"})


def _run_events(state: dict[str, object]) -> list[dict[str, object]]:
    """Every event this run recorded, oldest first.

    Scoped to the one run rather than `_load_events`' whole history: the stage
    table describes the phase in front of the reader, and reading every past run
    to build it would grow with the machine's history instead of the phase.
    """
    history_file = _string(state.get("history_file"))
    if not history_file:
        return []
    try:
        with Path(history_file).open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            lines = handle.read().splitlines()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        event = _json_object(line)
        if event is not None and _integer(event.get("schema_version")) == SCHEMA_VERSION:
            events.append(event)
    return events


def _agent_display(model: str, effort: str) -> str:
    if not model:
        return ""
    return model if not effort or effort == "unset" else f"{model} {effort}"


def _clock_stamp(epoch: float) -> str:
    return f"{datetime.fromtimestamp(epoch):%H:%M:%S}"


def _started_window(event: dict[str, object], event_type: str) -> StageWindow:
    if event_type == "pass_started":
        model, effort = _agent_fields(event, "called_agent")
        instance_id = _string(event.get("pass_instance_id"))
        kind = _string(event.get("pass_kind"))
        label = ""
        started_at = _number(event.get("pass_started_at"))
    else:
        # An activity is the main agent working directly, so it has no delegate
        # and the Delegate cell stays empty rather than repeating the orchestrator.
        model, effort = "", ""
        instance_id = _string(event.get("activity_instance_id"))
        kind = "activity"
        label = _string(event.get("activity_label"), "Work")
        started_at = _number(event.get("activity_started_at"))
    main_model, main_effort = _agent_fields(event, "main_agent")
    return StageWindow(
        instance_id=instance_id,
        kind=kind,
        fix_round=_integer(event.get("fix_pass")),
        label=label,
        started_at=started_at or _number(event.get("timestamp_epoch")),
        elapsed=0,
        status="open",
        result="",
        delegate=_agent_display(model, effort),
        main=_agent_display(main_model, main_effort),
    )


def _live_window(state: dict[str, object], now: float) -> StageWindow | None:
    """The window running right now, read from state rather than the events.

    Its finishing event does not exist yet, and a session upgraded mid-phase has
    an activity whose start event predates activity identity, so state is the
    only source that always knows what is running.
    """
    current = _object_dict(state.get("pass"))
    if current is not None and _string(current.get("status")) == "active":
        model, effort = _agent_fields(current, "called_agent")
        kind = _string(current.get("kind"))
        label = ""
    else:
        current = _object_dict(state.get("activity"))
        if current is None or _string(current.get("status")) != "active":
            return None
        model, effort = "", ""
        kind = "activity"
        label = _string(current.get("label"), "Work")
    main_model, main_effort = _agent_fields(state, "main_agent")
    started_at = _number(current.get("started_at"), now)
    return StageWindow(
        instance_id=_string(current.get("instance_id")) or "live",
        kind=kind,
        fix_round=_integer(current.get("fix_pass")),
        label=label,
        started_at=started_at,
        elapsed=max(0, int(now - started_at)),
        status="running",
        result="running",
        delegate=_agent_display(model, effort),
        main=_agent_display(main_model, main_effort),
    )


def _phase_windows(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool,
) -> list[StageWindow]:
    windows: dict[str, StageWindow] = {}
    for event in events:
        if _string(event.get("phase_instance_id")) != phase_instance_id:
            continue
        event_type = _string(event.get("event_type"))
        if event_type in ("pass_started", "activity_started"):
            window = _started_window(event, event_type)
            if window["instance_id"]:
                windows[window["instance_id"]] = window
            continue
        if event_type not in ("pass_finished", "activity_finished"):
            continue
        finished_pass = event_type == "pass_finished"
        key = _string(
            event.get("pass_instance_id" if finished_pass else "activity_instance_id")
        )
        window = windows.get(key)
        if window is None:
            continue
        window["status"] = _string(event.get("status"), "completed")
        window["result"] = _string(event.get("activity_result"))
        window["elapsed"] = _integer(
            event.get(
                "pass_elapsed_seconds" if finished_pass else "activity_elapsed_seconds"
            )
        )
    if include_live:
        live = _live_window(state, now)
        if live is not None:
            windows[live["instance_id"]] = live
    return sorted(windows.values(), key=lambda window: window["started_at"])


def _stage_labels(windows: list[StageWindow]) -> list[str]:
    """Name each window by kind and its position among that kind in the phase.

    Reviews and fixes are numbered because a phase runs several and the reader's
    question is which one; a fix carries the round the ledger dispatched, so its
    number matches the one convergence counts.
    """
    counts: dict[str, int] = {}
    labels: list[str] = []
    for window in windows:
        kind = window["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        index = counts[kind]
        if kind == "activity":
            labels.append(window["label"] or "Work")
        elif kind == "fix":
            labels.append(f"Fix {window['fix_round'] or index}")
        elif kind == "review":
            labels.append(f"Review {index}")
        else:
            name = {"impl": "Impl", "arch": "Arch"}.get(kind) or kind.title() or "Pass"
            labels.append(name if index == 1 else f"{name} {index}")
    return labels


def _finding_tally(
    findings: list[dict[str, object]],
    lower: float,
    upper: float,
) -> FindingTally:
    """What the ledger recorded between one window opening and the next.

    Findings are opened, dispatched, and settled by the main agent in the gap
    after a window closes, so the interval that starts at a window and ends at
    its successor is what attributes them to the pass that produced them.
    """
    tally = FindingTally(opened=0, landed=0, accepted=0, still_open=0, reopened=0)
    for event in findings:
        at = _number(event.get("timestamp_epoch"))
        if not lower <= at < upper:
            continue
        event_type = _string(event.get("event_type"))
        if event_type == "finding_opened":
            tally["opened"] += 1
        elif event_type == "finding_batch_landed":
            landed = event.get("landed")
            if isinstance(landed, list):
                tally["landed"] += len(cast(list[object], landed))
        elif event_type == "finding_verdict":
            verdict = _string(event.get("verdict"))
            if verdict == "accepted":
                tally["accepted"] += 1
            elif verdict == "still_open":
                tally["still_open"] += 1
            elif verdict == "reopened":
                tally["reopened"] += 1
    return tally


def _window_result(window: StageWindow, tally: FindingTally) -> str:
    status = window["status"]
    if status == "running":
        return "running"
    if status not in ("completed", "open"):
        return status
    if window["kind"] == "activity":
        return window["result"] or ("open" if status == "open" else "done")
    if status == "open":
        return "open"
    if window["kind"] == "fix":
        return f"{tally['landed']} landed" if tally["landed"] else "done"
    if window["kind"] == "review":
        unresolved = tally["still_open"] + tally["reopened"]
        if tally["accepted"] or unresolved:
            parts = [f"{tally['accepted']} fixed"]
            if unresolved:
                parts.append(f"{unresolved} open")
            if tally["opened"]:
                parts.append(f"{tally['opened']} new")
            return ", ".join(parts)
        return f"{tally['opened']} found" if tally["opened"] else "clean"
    return "done"


def _stage_rows(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool = True,
) -> list[list[str]]:
    windows = _phase_windows(events, state, phase_instance_id, now, include_live)
    findings = [
        event
        for event in events
        if _string(event.get("phase_instance_id")) == phase_instance_id
        and _string(event.get("event_type")).startswith("finding_")
    ]
    labels = _stage_labels(windows)
    rows: list[list[str]] = []
    for index, window in enumerate(windows):
        upper = (
            windows[index + 1]["started_at"]
            if index + 1 < len(windows)
            else max(now, window["started_at"]) + 1.0
        )
        rows.append(
            [
                labels[index],
                window["main"],
                window["delegate"],
                _clock_stamp(window["started_at"]),
                _format_duration(window["elapsed"]),
                _window_result(window, _finding_tally(findings, window["started_at"], upper)),
            ]
        )
    return rows


def _render_table(
    headers: list[str],
    rows: list[list[str]],
    numeric: set[int],
) -> list[str]:
    """Pad a Markdown table so its columns line up as plain text as well."""
    # Three is the narrowest column a Markdown rule can still describe: `--:`
    # keeps the `%` column's alignment marker legal where `:` alone is not.
    widths = [max(len(header), 3) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def rendered(cells: list[str]) -> str:
        padded = [
            cell.rjust(widths[index]) if index in numeric else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return "| " + " | ".join(padded) + " |"

    rule = (
        "| "
        + " | ".join(
            "-" * (width - 1) + ":" if index in numeric else "-" * width
            for index, width in enumerate(widths)
        )
        + " |"
    )
    return [rendered(headers), rule, *(rendered(row) for row in rows)]


def _arrival_label(seconds_out: int, now: float) -> str:
    """A point in the future named the way a person would say it aloud."""
    arrival = datetime.fromtimestamp(now + seconds_out)
    days = (arrival.date() - datetime.fromtimestamp(now).date()).days
    if days == 0:
        return f"today {arrival:%H:%M}"
    if days == 1:
        return f"tomorrow {arrival:%H:%M}"
    return f"{arrival:%Y-%m-%d %H:%M}"


def _eta_cell(percent: int, elapsed: int, now: float) -> str:
    """When the work is expected to land, as a clock time rather than a duration.

    A remaining duration has to be added to the current time by hand every time
    it is read, and the answer changes with every report. An arrival stamp is
    that addition already done, and it says the same thing tomorrow morning.
    """
    eta = _eta_seconds(percent, elapsed)
    return "" if eta is None else _arrival_label(eta, now)


def _format_offset(seconds: int) -> str:
    """A swing, as hours and minutes, however many hours that turns out to be.

    `_format_duration` breaks past a day into a `<days> HH:MM:SS` phrase, which
    reads well for an elapsed clock and badly for the two numbers whose whole
    job is to be compared at a glance. One unbroken hours field renders a swing
    of forty minutes and a swing of forty hours the same way.
    """
    hours, remainder = divmod(max(0, seconds), 3600)
    return f"{hours:02d}:{remainder // 60:02d}"


def _eta_band_cells(
    percent: int,
    elapsed: int,
    now: float,
    spread: float,
) -> tuple[str, str]:
    """The best and worst arrival the percentage beside it still allows.

    The ETA extends the current rate from a percentage that is an estimate, so
    every hour it predicts inherits that estimate's error. Moving the percentage
    a plausible distance each way and re-running the same projection turns that
    error into the two times it actually implies. The band is asymmetric on
    purpose: at 20% a few points of optimism cost far more hours than the same
    few points of pessimism save, and a symmetric band would hide exactly that.

    Each cell carries its own distance from the ETA, so the swing reads off the
    row without subtracting two clock times by hand.

    Neither end is allowed past double or half the reported progress, whatever
    the spread says. A measured error wider than the percentage itself would
    otherwise push the pessimistic end down to 1% and quote an arrival ninety-nine
    times the elapsed clock — a number no reader can use and none should trust.
    """
    eta = _eta_seconds(percent, elapsed)
    if eta is None:
        return "", ""
    optimistic = min(99.0, percent * RATE_FACTOR_LIMIT, percent + max(0.0, spread))
    pessimistic = max(1.0, percent / RATE_FACTOR_LIMIT, percent - max(0.0, spread))
    low = int(elapsed * (100.0 - optimistic) / optimistic)
    high = int(elapsed * (100.0 - pessimistic) / pessimistic)
    return (
        f"{_arrival_label(low, now)} (-{_format_offset(eta - low)})",
        f"{_arrival_label(high, now)} (+{_format_offset(high - eta)})",
    )


def _percent_spread(calibration: dict[str, object] | None) -> float:
    """How far off the percentage has actually run, when history can say.

    A calibration that cleared its sample floor has measured this reporter's
    error at this percentage against phases that finished, which beats any
    constant. Below the floor the measurement is noise wearing a number, so the
    default stands.
    """
    if calibration is None:
        return DEFAULT_PERCENT_SPREAD
    if _integer(calibration.get("sample_count")) < MIN_CALIBRATION_SAMPLES:
        return DEFAULT_PERCENT_SPREAD
    measured = calibration.get("median_raw_absolute_error_percentage_points")
    if not isinstance(measured, int | float):
        return DEFAULT_PERCENT_SPREAD
    return max(1.0, float(measured))


def _unchanged_cell(seconds: int) -> str:
    return _format_duration(seconds) if seconds > 0 else ""


def _scope_line(state: dict[str, object], counts: dict[str, object]) -> str:
    """Where the run is working, and how far into the plan that leaves it.

    Worktree and branch say where; neither says how much plan is left. The
    position is the ordinal of the heading the active phase actually occupies,
    looked up by the id `start-phase` named. Counting finished phases and adding
    one is wrong: `/plan:phase_review` flips a phase to `done` before its
    checkpoint, so for the whole review window the phase in flight is counted
    twice and the position runs one ahead. A plan whose phases could not be
    counted, or whose headings do not name the active phase, keeps the short
    form rather than showing a position nothing verified.
    """
    line = f"{_string(state.get('worktree'))} - {_string(state.get('branch'))}"
    if counts.get("available") is not True:
        return f"**{line}**"
    order = counts.get("order")
    phase = _object_dict(state.get("phase"))
    if phase is None or not isinstance(order, list):
        return f"**{line}**"
    identifiers = [identifier for identifier in cast(list[object], order) if isinstance(identifier, str)]
    active = _string(phase.get("id"))
    if active not in identifiers:
        return f"**{line}**"
    total = _integer(counts.get("total"))
    return f"**{line} - phase {identifiers.index(active) + 1} of {total}**"


def _timeline(args: argparse.Namespace) -> None:
    """Render the stage table for one phase, or for every phase of the run.

    The progress header only ever shows the phase in flight. This answers the
    questions asked after the fact -- how many fix passes, how long each review
    took -- without reading the raw event stream by hand.
    """
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = _now_epoch()
    events = _run_events(state)
    wanted = _arg_string(args, "phase")
    started: list[tuple[str, str, str, float]] = []
    finished: dict[str, tuple[str, int]] = {}
    for event in events:
        event_type = _string(event.get("event_type"))
        if event_type == "phase_started":
            started.append(
                (
                    _string(event.get("phase_instance_id")),
                    _string(event.get("phase_id"), "ad hoc"),
                    _string(event.get("phase_title"), "Ad hoc work"),
                    _number(
                        event.get("phase_started_at"),
                        _number(event.get("timestamp_epoch")),
                    ),
                )
            )
        elif event_type == "phase_finished":
            finished[_string(event.get("phase_instance_id"))] = (
                _string(event.get("status"), "completed"),
                _integer(event.get("phase_elapsed_seconds")),
            )
    lines = [
        f"**{_string(state.get('worktree'))} - {_string(state.get('branch'))}**",
    ]
    matched = 0
    for instance_id, phase_id, title, started_at in started:
        if wanted and phase_id != wanted:
            continue
        matched += 1
        live = instance_id not in finished
        status, elapsed = finished.get(
            instance_id,
            ("running", max(0, int(now - started_at))),
        )
        heading = (
            f"Phase {phase_id}: {title} - elapsed {_format_duration(elapsed)}"
            + f" - {status}"
        )
        lines.extend(
            [
                "",
                f"**{heading}**",
                "",
                *_render_table(
                    list(STAGE_HEADERS),
                    _stage_rows(events, state, instance_id, now, include_live=live),
                    set(),
                ),
            ]
        )
    if matched == 0:
        raise SystemExit(
            f"This run recorded no phase {wanted}"
            if wanted
            else "This run has recorded no phase yet"
        )
    print("\n".join(lines))


def _phase_count(args: argparse.Namespace) -> None:
    """Report a plan's phase counts and the project percent they imply.

    Standalone query for the same numbers `progress` derives, so a plan's
    completion can be checked without an active phase and pass.
    """
    plan_doc = _arg_string(args, "plan_doc")
    counts = _count_plan_phases(Path(plan_doc).expanduser().resolve())
    phase_percent = _arg_integer(args, "phase_percent", 0)
    payload = dict(counts)
    payload["project_percent"] = _plan_derived_project_percent(counts, phase_percent)
    print(json.dumps(payload, indent=2, sort_keys=True))


def _progress(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    now = _now_epoch()
    state = _ensure_project_timing(session_dir, _read_state(session_dir), now)
    phase = _object_dict(state.get("phase"))
    # The reported window is the launcher's pass when one is open, and otherwise
    # the main agent's activity. Both render the same third header line; only a
    # pass carries convergence meaning.
    window_key = "pass"
    current_pass = _object_dict(state.get("pass"))
    if current_pass is None or _string(current_pass.get("status")) != "active":
        activity = _object_dict(state.get("activity"))
        if activity is not None and _string(activity.get("status")) == "active":
            window_key = "activity"
            current_pass = activity
        else:
            current_pass = None
    if phase is None or _string(phase.get("status")) != "active" or current_pass is None:
        raise SystemExit(
            "An active phase and either an active pass or an active activity are "
            + "required before reporting progress"
        )
    legacy_raw_percent = _arg_integer(args, "raw_percent", -1)
    legacy_percent = _arg_integer(args, "percent", -1)
    project_raw_percent = _arg_integer(args, "project_raw_percent", -1)
    project_percent = _arg_integer(args, "project_percent", -1)
    phase_raw_percent = _arg_integer(args, "phase_raw_percent", -1)
    phase_percent = _arg_integer(args, "phase_percent", -1)
    uses_dual_layout = any(
        value >= 0
        for value in (
            project_raw_percent,
            project_percent,
            phase_raw_percent,
            phase_percent,
        )
    )
    if uses_dual_layout:
        if legacy_raw_percent >= 0 or legacy_percent >= 0:
            raise SystemExit("Do not combine legacy and project/phase percent options")
        if not all(
            0 <= value <= 100
            for value in (
                project_raw_percent,
                project_percent,
                phase_raw_percent,
                phase_percent,
            )
        ):
            raise SystemExit("All project and phase percent values are required")
    else:
        if not 0 <= legacy_raw_percent <= 100 or not 0 <= legacy_percent <= 100:
            raise SystemExit("--raw-percent and --percent are required for legacy calls")
        phase_raw_percent = legacy_raw_percent
        phase_percent = legacy_percent

    if not 0 <= phase_raw_percent <= 100 or not 0 <= phase_percent <= 100:
        raise SystemExit("Percent values must be between 0 and 100")
    phase_calibration = _object_dict(state.get("pending_calibration"))
    phase_override_reason = (
        _arg_string(args, "phase_override_reason")
        if uses_dual_layout
        else _arg_string(args, "override_reason")
    )
    phase_decision = _progress_decision(
        phase_calibration,
        phase_raw_percent,
        phase_percent,
        phase_override_reason,
        "phase" if uses_dual_layout else "legacy",
    )
    # The project clock is derived, never estimated. An agent eyeballing phase
    # headings misses the archived ones; counting them here removes the judgment
    # call entirely, and a supplied --project-percent becomes advisory.
    plan_phase_counts: dict[str, object] = {"available": False, "reason": "no plan doc"}
    project_percent_source = "supplied"
    state_plan_doc = _string(state.get("project_plan_doc")) or _string(
        state.get("plan_doc")
    )
    if state_plan_doc:
        candidate = Path(state_plan_doc).expanduser()
        if candidate.is_absolute():
            plan_phase_counts = _count_plan_phases(candidate)
    derived_project_percent = _plan_derived_project_percent(
        plan_phase_counts, phase_percent
    )
    if uses_dual_layout and derived_project_percent is not None:
        project_percent_source = "plan_phase_count"
        project_raw_percent = derived_project_percent
        project_percent = derived_project_percent

    project_decision: dict[str, object] | None = None
    if uses_dual_layout:
        project_decision = _progress_decision(
            None,
            project_raw_percent,
            project_percent,
            _arg_string(args, "project_override_reason"),
            "project",
        )
    cap_stage = _arg_string(args, "cap_stage")
    if uses_dual_layout and not cap_stage:
        raise SystemExit(
            "--cap-stage is required: name the gate this phase has actually reached ("
            + ", ".join(sorted(PROGRESS_CAPS))
            + ")"
        )
    phase_uncapped_percent = phase_percent
    project_uncapped_percent = project_percent
    if cap_stage:
        phase_percent = min(phase_percent, PROGRESS_CAPS.get(cap_stage, 100))
        if cap_stage != "complete":
            project_percent = min(project_percent, PROJECT_CAP_BEFORE_COMPLETE)

    activity = _arg_string(args, "activity")
    if activity:
        current_pass["activity"] = activity

    phase_percent_started_at, phase_unchanged_seconds = _assessment_timing(
        state,
        "phase",
        phase_percent,
        now,
    )
    state["last_percent"] = phase_percent
    state["percent_started_at"] = phase_percent_started_at
    project_percent_started_at = now
    project_unchanged_seconds = 0
    if uses_dual_layout:
        project_percent_started_at, project_unchanged_seconds = _assessment_timing(
            state,
            "project",
            project_percent,
            now,
        )

    phase_elapsed = max(0, int(now - _number(phase.get("started_at"), now)))
    pass_elapsed = max(0, int(now - _number(current_pass.get("started_at"), now)))
    total_elapsed = max(0, int(now - _number(state.get("project_started_at"), now)))
    next_report_at = _next_report_at(session_dir, now)
    event = _event(state, "progress_reported", now)
    event.update(
        {
            "next_report_at": next_report_at,
            "raw_percent": phase_raw_percent,
            "percent": phase_percent,
            "same_percent_started_at": phase_percent_started_at,
            "same_percent_elapsed_seconds": phase_unchanged_seconds,
            "phase_raw_percent": phase_raw_percent,
            "phase_percent": phase_percent,
            "phase_same_percent_started_at": phase_percent_started_at,
            "phase_same_percent_elapsed_seconds": phase_unchanged_seconds,
            "phase_elapsed_seconds": phase_elapsed,
            "pass_elapsed_seconds": pass_elapsed,
            "total_elapsed_seconds": total_elapsed,
            "cap_stage": cap_stage,
            "phase_uncapped_percent": phase_uncapped_percent,
            "phase_percent_capped_by": (
                cap_stage if phase_percent < phase_uncapped_percent else ""
            ),
            "calibration": phase_calibration,
            "phase_calibration": phase_calibration,
            **phase_decision,
            **_prefixed_decision("phase", phase_decision),
        }
    )
    if uses_dual_layout and project_decision is not None:
        event.update(
            {
                "project_raw_percent": project_raw_percent,
                "project_percent": project_percent,
                "project_percent_source": project_percent_source,
                "project_plan_phase_counts": plan_phase_counts,
                "project_uncapped_percent": project_uncapped_percent,
                "project_same_percent_started_at": project_percent_started_at,
                "project_same_percent_elapsed_seconds": project_unchanged_seconds,
                "project_calibration": None,
                **_prefixed_decision("project", project_decision),
            }
        )
    _append_event(state, event)
    state["pending_calibration"] = None
    state[window_key] = current_pass
    _write_state(session_dir, state)

    phase_id = _string(phase.get("id"), "ad hoc")
    phase_title = _string(phase.get("title"), "Ad hoc work")
    pass_line = (
        f"**{_pass_display(current_pass)} - {_string(current_pass.get('activity'))} "
        f"- elapsed {_format_duration(pass_elapsed)}**"
    )
    if uses_dual_layout:
        summary_headers = list(SUMMARY_HEADERS)
        project_row = [
            "Project",
            str(project_percent),
            _format_duration(total_elapsed),
            _eta_cell(project_percent, total_elapsed, now),
            _unchanged_cell(project_unchanged_seconds),
        ]
        phase_row = [
            f"Phase {phase_id}",
            str(phase_percent),
            _format_duration(phase_elapsed),
            _eta_cell(phase_percent, phase_elapsed, now),
            _unchanged_cell(phase_unchanged_seconds),
        ]
        if plan_phase_counts.get("available") is True:
            summary_headers.append("Phases")
            done = _integer(plan_phase_counts.get("done"))
            total = _integer(plan_phase_counts.get("total"))
            project_row.append(f"{done} of {total} done")
            phase_row.append("")
        # The project percentage is a phase count, so it is exact in phases and
        # only an estimate in work; the same spread reads there as "the phases
        # left may be worth more or less time than their share of the count".
        summary_headers.extend(("ETA low", "ETA high"))
        project_row.extend(
            _eta_band_cells(
                project_percent,
                total_elapsed,
                now,
                DEFAULT_PERCENT_SPREAD,
            )
        )
        phase_row.extend(
            _eta_band_cells(
                phase_percent,
                phase_elapsed,
                now,
                _percent_spread(phase_calibration),
            )
        )
        stage_rows = _stage_rows(
            _run_events(state),
            state,
            _string(phase.get("instance_id")),
            now,
        )
        # The running window is the newest one, so its row is the last row; the
        # sentence beneath the table is what a fixed-width Result column cannot
        # carry.
        live_label = stage_rows[-1][0] if stage_rows else _pass_display(current_pass)
        lines = [
            _scope_line(state, plan_phase_counts),
            "",
            *_render_table(
                summary_headers,
                [project_row, phase_row],
                {
                    index
                    for index, header in enumerate(summary_headers)
                    if header in RIGHT_ALIGNED_SUMMARY_COLUMNS
                },
            ),
            "",
            f"**Phase {phase_id}: {phase_title}**",
            "",
            *_render_table(list(STAGE_HEADERS), stage_rows, set()),
            "",
            f"▸ **{live_label} - {_string(current_pass.get('activity'))}**",
            _clock_line(now, next_report_at),
        ]
    else:
        legacy_progress_line = f"**{phase_percent}% complete"
        if phase_unchanged_seconds > 0:
            legacy_progress_line += (
                f" - unchanged for {_format_duration(phase_unchanged_seconds)}"
            )
        legacy_progress_line += "**"
        lines = [
            f"**{_string(state.get('worktree'))} - {_string(state.get('branch'))}**",
            f"**Phase {phase_id}: {phase_title} - elapsed {_format_duration(phase_elapsed)}**",
            pass_line,
            legacy_progress_line,
            f"**Total elapsed {_format_duration(total_elapsed)}**",
            _clock_line(now, next_report_at),
        ]
    print("\n".join(lines))


def _finish_phase(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = _now_epoch()
    _close_active_pass(session_dir, state, "interrupted", now)
    state = _read_state(session_dir)
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = _read_state(session_dir)
    phase = _object_dict(state.get("phase"))
    if phase is None or _string(phase.get("status")) != "active":
        return
    event = _event(state, "phase_finished", now)
    event["status"] = _arg_string(args, "status")
    event["phase_elapsed_seconds"] = max(
        0,
        int(now - _number(phase.get("started_at"), now)),
    )
    _append_event(state, event)
    phase["status"] = _arg_string(args, "status")
    phase["finished_at"] = now
    state["phase"] = phase
    _write_state(session_dir, state)


def _finish_run(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = _now_epoch()
    run_status = _arg_string(args, "status")
    phase = _object_dict(state.get("phase"))
    if phase is not None and _string(phase.get("status")) == "active":
        finish_args = argparse.Namespace(session_dir=str(session_dir), status=run_status)
        _finish_phase(finish_args)
        state = _read_state(session_dir)
    if _string(state.get("status")) != "active":
        return
    event = _event(state, "run_finished", now)
    event["status"] = run_status
    event["run_elapsed_seconds"] = max(
        0,
        int(now - _number(state.get("run_started_at"), now)),
    )
    event["total_elapsed_seconds"] = max(
        0,
        int(now - _number(state.get("project_started_at"), now)),
    )
    _append_event(state, event)
    state["status"] = run_status
    state["finished_at"] = now
    _write_state(session_dir, state)


def _aggregate(args: argparse.Namespace) -> None:
    events, ignored = _load_events()
    samples = _calibration_samples(events)
    requested_percent = _arg_integer(args, "percent", -1)
    grouped: dict[tuple[int, str], list[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        if requested_percent >= 0 and sample["raw_percent"] != requested_percent:
            continue
        grouped[(sample["raw_percent"], sample["pass_kind"])].append(sample)
    groups: list[dict[str, object]] = []
    for (raw_percent, pass_kind), group_samples in sorted(grouped.items()):
        groups.append(
            {
                "raw_percent": raw_percent,
                "pass_kind": pass_kind,
                **_sample_metrics(group_samples, 0),
            }
        )
    output = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_time(_now_epoch()),
        "history_root": str(_history_root()),
        "completed_raw_estimate_samples": len(samples),
        "ignored_history_rows": ignored,
        "groups": groups,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


def _add_identity_options(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--main-family", default="")
    _ = parser.add_argument("--main-model", default="")
    _ = parser.add_argument("--main-effort", default="unset")
    _ = parser.add_argument("--main-session-id", default="")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and aggregate durable plan-delegate progress events."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_run = subparsers.add_parser("start-run")
    _ = start_run.add_argument("--session-dir", required=True)
    _ = start_run.add_argument("--working-dir", required=True)
    _ = start_run.add_argument("--plan-doc", default="")
    _add_identity_options(start_run)
    start_run.set_defaults(handler=_start_run)

    start_phase = subparsers.add_parser("start-phase")
    _ = start_phase.add_argument("--session-dir", required=True)
    _ = start_phase.add_argument("--phase-id", required=True)
    _ = start_phase.add_argument("--phase-title", required=True)
    _ = start_phase.add_argument("--work-order-file", default="")
    start_phase.set_defaults(handler=_start_phase)

    start_activity = subparsers.add_parser("start-activity")
    _ = start_activity.add_argument("--session-dir", required=True)
    _ = start_activity.add_argument("--label", required=True)
    _ = start_activity.add_argument("--activity", required=True)
    start_activity.set_defaults(handler=_start_activity)

    finish_activity = subparsers.add_parser("finish-activity")
    _ = finish_activity.add_argument("--session-dir", required=True)
    _ = finish_activity.add_argument(
        "--status",
        choices=("completed", "error", "canceled", "interrupted"),
        default="completed",
    )
    _ = finish_activity.add_argument(
        "--result",
        default="",
        help="short outcome for the stage table, such as pass, clean, or no change",
    )
    finish_activity.set_defaults(handler=_finish_activity)

    start_pass = subparsers.add_parser("start-pass")
    _ = start_pass.add_argument("--session-dir", required=True)
    _ = start_pass.add_argument("--pass-kind", choices=("impl", "fix", "review", "arch"), required=True)
    _ = start_pass.add_argument("--fix-pass", type=int, default=0)
    _ = start_pass.add_argument("--activity", required=True)
    _ = start_pass.add_argument("--called-task", required=True)
    _ = start_pass.add_argument("--called-family", required=True)
    _ = start_pass.add_argument("--called-model", required=True)
    _ = start_pass.add_argument("--called-effort", default="unset")
    start_pass.set_defaults(handler=_start_pass)

    finish_pass = subparsers.add_parser("finish-pass")
    _ = finish_pass.add_argument("--session-dir", required=True)
    _ = finish_pass.add_argument(
        "--status",
        choices=("completed", "error", "canceled", "interrupted"),
        required=True,
    )
    _ = finish_pass.add_argument(
        "--orphaned-launcher",
        action="store_true",
        help="close the open pass of a launcher the orchestrator killed (--status canceled only)",
    )
    finish_pass.set_defaults(handler=_finish_pass)

    calibrate = subparsers.add_parser("calibrate")
    _ = calibrate.add_argument("--session-dir", required=True)
    _ = calibrate.add_argument("--candidate-percent", type=int, required=True)
    calibrate.set_defaults(handler=_calibrate)

    progress = subparsers.add_parser("progress")
    _ = progress.add_argument("--session-dir", required=True)
    _ = progress.add_argument("--raw-percent", type=int, default=-1)
    _ = progress.add_argument("--percent", type=int, default=-1)
    _ = progress.add_argument("--project-raw-percent", type=int, default=-1)
    _ = progress.add_argument("--project-percent", type=int, default=-1)
    _ = progress.add_argument("--phase-raw-percent", type=int, default=-1)
    _ = progress.add_argument("--phase-percent", type=int, default=-1)
    _ = progress.add_argument("--activity", required=True)
    _ = progress.add_argument("--cap-stage", choices=tuple(PROGRESS_CAPS), default="")
    _ = progress.add_argument("--override-reason", default="")
    _ = progress.add_argument("--project-override-reason", default="")
    _ = progress.add_argument("--phase-override-reason", default="")
    progress.set_defaults(handler=_progress)

    timeline = subparsers.add_parser("timeline")
    _ = timeline.add_argument("--session-dir", required=True)
    _ = timeline.add_argument(
        "--phase",
        default="",
        help="restrict the table to this phase identifier; omit for every phase",
    )
    timeline.set_defaults(handler=_timeline)

    phase_count = subparsers.add_parser("phase-count")
    _ = phase_count.add_argument("--plan-doc", required=True)
    _ = phase_count.add_argument("--phase-percent", type=int, default=0)
    phase_count.set_defaults(handler=_phase_count)

    finish_phase = subparsers.add_parser("finish-phase")
    _ = finish_phase.add_argument("--session-dir", required=True)
    _ = finish_phase.add_argument(
        "--status",
        choices=("completed", "stopped", "error"),
        required=True,
    )
    finish_phase.set_defaults(handler=_finish_phase)

    finish_run = subparsers.add_parser("finish-run")
    _ = finish_run.add_argument("--session-dir", required=True)
    _ = finish_run.add_argument(
        "--status",
        choices=("completed", "stopped", "error"),
        required=True,
    )
    finish_run.set_defaults(handler=_finish_run)

    aggregate = subparsers.add_parser("aggregate")
    _ = aggregate.add_argument("--percent", type=int, default=-1)
    aggregate.set_defaults(handler=_aggregate)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    handler_value: object = getattr(args, "handler")  # pyright: ignore[reportAny]
    if not callable(handler_value):
        raise SystemExit("No command handler selected")
    handler = cast(Callable[[argparse.Namespace], None], handler_value)
    session_value: object = getattr(args, "session_dir", "")
    session_text = _string(session_value)
    if not session_text:
        handler(args)
        return
    session_dir = Path(session_text).expanduser().resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            handler(args)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
