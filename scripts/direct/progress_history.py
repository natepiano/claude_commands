#!/usr/bin/env python3
"""Durable unit progress events, headers, and calibration statistics."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal, NoReturn, TypedDict, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation, plan_history, recorder_state  # noqa: E402
from scripts.direct.recorder_state import OPEN_PHASES_KEY as OPEN_PHASES_KEY  # noqa: E402
from scripts.direct.tag_inventory import contract_table_paths  # noqa: E402
from scripts.lib import agents_registry, unit_runs  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402


SCHEMA_VERSION = 1
# The shared history spine, read by scripts/history/history.py. Kept separate
# from SCHEMA_VERSION, which versions this recorder's own fields: a reader has
# to tell "worker changed its events" apart from "the shared format changed",
# and one number cannot say both once other skills write to the same spine.
SPINE_VERSION = 1
SKILL_NAME = "unit-history"
# The name events carried before the rename: every history written before it, and a run still open.
FORMER_SKILL_NAME = "plan-delegate"
MIN_CALIBRATION_SAMPLES = 5
# How long a stretch with no pass or activity open still counts as phase work: the unit
# director reviews, gates and writes the next prompt between dispatches. Past it the phase
# waited, and an overnight wait counted in full turned a phase with an hour left into one
# finishing the next evening (herdr-trial, 2026-10-15).
IDLE_GRACE_SECONDS = 30 * 60
# A worker finished this long ago drops out of the report's worker lines.
FINISHED_WORKER_SECONDS = 10 * 60
# How far apart two entered percentages may sit and still calibrate each other.
# The entered percent is itself off by about plan_history.DEFAULT_PERCENT_SPREAD, so history
# ten points either side describes the same moment of a phase as well as history
# at the exact number does. An exact match handed each tick its own handful of
# reports: four ticks entering 78, 82, 84 and 86 drew suggestions of 40, 20, 55
# and 15, and the ETA built on them moved by more than nine hours between ticks.
CALIBRATION_WINDOW = 10
# Where the early reviewer lives between its launch and the ready sentinel that
# releases its real pass. A pass opened before the sentinel would file a verdict
# on a final diff that does not exist yet, so an early launch has nowhere to be
# recorded and the stage table showed only the implementer -- exactly during the
# window where the reader most needs to see that two agents are running. This
# marker is presentation-only: it opens no pass, and `findings.py` counts pass
# events, so convergence never sees it.
ARMED_REVIEW_KEY = "early_review"
# A phase closed by finish-phase takes no more passes, its plan review's included.
CLOSED_PHASE_RULE = ("A finished phase takes no more passes: run its plan review before its finish-phase "
                     + "(docs/direct/phase_end.md, step 3 before step 5).")
# The state that describes the current phase alone. A phase a newer one
# displaces keeps its own copy under PARKED_KEY, and gets it back when the newer
# one finishes and it is current again.
PHASE_SCOPED_KEYS = (
    ARMED_REVIEW_KEY,
    "phase_last_percent",
    "phase_percent_started_at",
    "last_percent",
    "percent_started_at",
    "pending_calibration",
)
PARKED_KEY = "parked"
PROJECT_STARTED_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project started:\*\*[ \t]*(?P<value>.+?)[ \t]*$",
    re.MULTILINE,
)
PROJECT_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project:\*\*[^\n]*$",
    re.MULTILINE,
)

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

WAIT_KINDS = (
    "WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
    "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity",
)


class AgentIdentity(TypedDict):
    family: str
    model: str
    effort: str
    session_id: str


@dataclass(frozen=True)
class KnownMainIdentity:
    identity: AgentIdentity


@dataclass(frozen=True)
class MainIdentityUnavailable:
    """No readable transcript supplies a model; detection must try its fallback."""


class CalibrationSample(TypedDict):
    percent: int
    raw_percent: int
    cap_stage: str
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
    """One launcher pass or one unit-director activity.

    Two tables read these. `timeline` renders one row apiece, which is the
    provenance view. The progress report groups them by `fix_round` and lays the
    workers out as columns, which is why `slot` is carried here rather than
    recovered later: a window with no worker is a unit-director activity, and it is
    the absence of a worker that tells the two apart.
    """

    instance_id: str
    slot: str
    kind: str
    fix_round: int
    label: str
    # What the window actually ran, for an activity: the `--activity` text the
    # unit director opened it with ("test hana"). Empty for passes, whose work is
    # described by kind and worker. The gate notes under the round table are the
    # one reader of it -- the label alone renders a block of verification
    # commands as identical rows.
    text: str
    started_at: float
    elapsed: int
    status: str
    result: str
    worker: str
    main: str


class FindingTally(TypedDict):
    opened: int
    landed: int
    accepted: int
    still_open: int
    reopened: int


# direct.conf, whose digest a pass records. `UNIT_CONFIG` overrides the
# path so the tests read a fixture rather than the machine's live settings.
DEFAULT_CONFIG_PATH = Path("~/.claude/config/direct.conf").expanduser()

# A pass already records the agent identity it resolved to, so the digest covers
# what that identity cannot: the assignment tables that produced it and the
# worker settings the run obeyed. Stamped on every pass_started, it lets a
# later query group passes by the configuration in force, which is what makes a
# comparison survive interleaved runs and a conf edited between them.
CONFIG_DIGEST_LENGTH = 12
# agents_config.sh reads the same env override, so a launcher resolving against
# one table can never be stamped with the digest of another.
AGENTS_CONFIG_ENV = "AGENTS_CONFIG_FILE"
DEFAULT_AGENTS_CONFIG_PATH = Path("~/.claude/config/agents.conf").expanduser()


class ContractFileHash(TypedDict):
    state: Literal["Recorded"]
    sha256: str


class ContractFileUnavailable(TypedDict):
    state: Literal["Missing", "NotRecorded"]
    reason: str


class ContractMainHead(TypedDict):
    state: Literal["Recorded"]
    commit: str


class ContractHeadUnavailable(TypedDict):
    state: Literal["NotRecorded"]
    reason: str


class ContractRevision(TypedDict):
    main_head: ContractMainHead | ContractHeadUnavailable
    direct_md: ContractFileHash | ContractFileUnavailable
    files: dict[str, ContractFileHash | ContractFileUnavailable]


def _contract_file(path: Path) -> ContractFileHash | ContractFileUnavailable:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return ContractFileUnavailable(state="Missing", reason=f"Missing file: {path}")
    except OSError as error:
        return ContractFileUnavailable(state="NotRecorded", reason=str(error))
    return ContractFileHash(state="Recorded", sha256=hashlib.sha256(contents).hexdigest())


def _contract_revision() -> ContractRevision:
    """Read the deployed main checkout afresh at each phase boundary."""
    root = Path.home() / ".claude"
    head: ContractMainHead | ContractHeadUnavailable
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        head = ContractMainHead(state="Recorded", commit=result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as error:
        head = ContractHeadUnavailable(state="NotRecorded", reason=str(error))
    direct = root / "commands/unit/direct.md"
    files: dict[str, ContractFileHash | ContractFileUnavailable] = {}
    try:
        contents = direct.read_bytes()
        direct_hash: ContractFileHash | ContractFileUnavailable = ContractFileHash(
            state="Recorded", sha256=hashlib.sha256(contents).hexdigest(),
        )
        for path in contract_table_paths(contents.decode("utf-8")):
            files[path] = _contract_file(root / path)
    except FileNotFoundError:
        direct_hash = ContractFileUnavailable(state="Missing", reason=f"Missing file: {direct}")
    except (OSError, UnicodeError) as error:
        direct_hash = ContractFileUnavailable(state="NotRecorded", reason=str(error))
    return ContractRevision(main_head=head, direct_md=direct_hash, files=files)


def _config_path() -> Path:
    configured = os.environ.get("UNIT_CONFIG") or os.environ.get("PLAN_DELEGATE_CONFIG")
    return Path(configured) if configured else DEFAULT_CONFIG_PATH


def _agents_config_path() -> Path:
    configured = os.environ.get(AGENTS_CONFIG_ENV)
    return Path(configured) if configured else DEFAULT_AGENTS_CONFIG_PATH


@cache
def _config_digest() -> str:
    """Both configuration files hashed together, empty when either cannot be read.

    Cached for the life of the process: one launcher records one pass, and both
    files answer the same way every call. This is telemetry and must never stop
    a unit run, so an unreadable file returns empty rather than raising --
    and empty rather than the hash of the half that could be read, which would
    name a configuration this process never saw and group unlike passes under it.
    """
    contents: list[bytes] = []
    for path in (_agents_config_path(), _config_path()):
        try:
            contents.append(path.read_bytes())
        except OSError:
            return ""
    # This machine's agent rows over the committed registry change the configuration too.
    overlay = agents_registry.overlay_path(_agents_config_path())
    if overlay.is_file():
        try:
            contents.append(overlay.read_bytes())
        except OSError:
            return ""
    return hashlib.sha256(b"".join(contents)).hexdigest()[:CONFIG_DIGEST_LENGTH]


PASS_OWNER_ENV = "UNIT_PASS_OWNER"
PASS_OWNER_TOKEN = "launcher"

PASS_OWNERSHIP_RULE = (
    "Pass lifecycle belongs to the launcher. implement.sh and review.sh already "
    "record pass_started and pass_finished around the worker they wait on, so a "
    "hand-written call forges a pass that never ran. findings.py gate counts "
    "passes to decide whether a phase is converging, and forged passes stop a run "
    "for a condition that never happened. Launch the work through implement.sh or "
    "review.sh. The single exception is a launcher the unit director killed: close "
    "its still-open pass with 'finish-pass --status canceled --orphaned-launcher'."
)


def _launcher_owned() -> bool:
    return (os.environ.get(PASS_OWNER_ENV) or os.environ.get("PLAN_DELEGATE_PASS_OWNER")) == PASS_OWNER_TOKEN


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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


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


def _refuse_eta(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _local_eta_epoch(value: str, option: str) -> float:
    zone = user_zone()
    if re.fullmatch(r"\d{2}:\d{2}", value):
        today = datetime.fromtimestamp(plan_history.now_epoch(), zone)
        value = f"{today:%Y-%m-%d}T{value}"
    layout = "%Y-%m-%dT%H:%M"
    try:
        parsed = datetime.strptime(value, layout)
    except ValueError:
        _refuse_eta(f"{option} must use YYYY-MM-DDTHH:MM")
    if parsed.strftime(layout) != value:
        _refuse_eta(f"{option} must use YYYY-MM-DDTHH:MM")
    epoch = parsed.replace(tzinfo=zone).timestamp()
    if datetime.fromtimestamp(epoch, zone).strftime(layout) != value:
        _refuse_eta(f"{option} is a time the local clock skips")
    return epoch


def _session_dir(args: argparse.Namespace) -> Path:
    value = _arg_string(args, "session_dir")
    if not value:
        raise SystemExit("--session-dir is required")
    return Path(value).expanduser().resolve()


def set_unit(session_dir: Path, production: str, unit: str) -> None:
    """Persist unit identity without changing the run's history or workers."""
    if any(not value or value in (".", "..") or "/" in value or "\\" in value
           for value in (production, unit)):
        raise ValueError("invalid production or unit identity")
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read_state(session_dir)
        state.update({"production": production, "unit": unit})
        _write_state(session_dir, state)


def _set_unit(args: argparse.Namespace) -> None:
    # main already holds this lock; the public API takes it for other callers.
    state = _read_state(_session_dir(args))
    production, unit = _arg_string(args, "production"), _arg_string(args, "unit")
    if any(not value or value in (".", "..") or "/" in value or "\\" in value
           for value in (production, unit)):
        raise SystemExit("invalid production or unit identity")
    state.update({"production": production, "unit": unit})
    _write_state(_session_dir(args), state)


def _state_path(session_dir: Path) -> Path:
    return session_dir / plan_history.STATE_FILENAME


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
        prefix=f".{plan_history.STATE_FILENAME}.",
        delete=False,
    ) as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        _ = handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, target)


SESSION_ROOTS = tuple(
    Path(root)
    for root in (os.environ.get("PROGRESS_SESSION_ROOTS") or "/tmp/claude/unit:/tmp/claude/delegate").split(":")
    if root
)


def _moved_fields(fields: dict[str, object], old: Path, new: Path, new_branch: str) -> bool:
    if fields.get("working_dir") not in (str(old), str(new)):
        return False
    fields.update({"working_dir": str(new), "worktree": new.name, "branch": new_branch})
    for key in ("plan_doc", "project_plan_doc"):
        value = fields.get(key)
        if isinstance(value, str) and (value == str(old) or value.startswith(f"{old}/")):
            fields[key] = str(new) + value[len(str(old)):]
    return True


def move_run(history_file: Path, old: Path, new: Path, new_branch: str) -> list[Path]:
    """Point a run at its unit's moved worktree and new branch: its record and its live state.

    A unit rename calls this while the unit is stopped, so no recorder writes meanwhile. Each
    event and the state keep their run and phase ids; only the location fields change.
    """
    lines: list[str] = []
    for line in history_file.read_text(encoding="utf-8").splitlines(keepends=True):
        event = _json_object(line)
        if event is not None and _moved_fields(event, old, new, new_branch):
            line = json.dumps(event, separators=(",", ":")) + "\n"
        lines.append(line)
    temporary = history_file.with_name(f".{history_file.name}.move")
    _ = temporary.write_text("".join(lines), encoding="utf-8")
    os.replace(temporary, history_file)
    moved = [history_file]
    for root in SESSION_ROOTS:
        for path in root.glob(f"*/{plan_history.STATE_FILENAME}"):
            try:
                state = _json_object(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if state is not None and state.get("history_file") == str(history_file) and _moved_fields(
                state, old, new, new_branch
            ):
                _write_state(path.parent, state)
                moved.append(path)
    return moved


def _refresh_phase_table(session_dir: Path) -> None:
    """Rewrite a production unit's phase note without affecting the recorder."""
    try:
        state = _read_state(session_dir)
        plan_doc = _string(state.get("project_plan_doc")) or _string(
            state.get("plan_doc")
        )
        if not plan_doc:
            return
        plan_path = plan_history.resolve_plan_path(
            Path(_string(state.get("working_dir"))),
            plan_doc,
        )
        if "> **Production:" not in plan_path.read_text(encoding="utf-8"):
            return
    except (OSError, UnicodeError, SystemExit):
        return

    command = [
        sys.executable,
        str(Path(__file__).with_name("phase_table.py")),
        "refresh",
        "--session-dir",
        str(session_dir),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"phase table not written: {error}", file=sys.stderr)
        return
    if result.returncode == 0:
        return
    reason = " ".join(result.stderr.split()) or f"exit status {result.returncode}"
    print(f"phase table not written: {reason}", file=sys.stderr)


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
        # Disposable fixtures may omit disk durability; production syncs by default.
        if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
            os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _main_identity_from_codex(session_id: str) -> KnownMainIdentity | MainIdentityUnavailable:
    session_root = Path.home() / ".codex" / "sessions"
    matches = list(session_root.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return MainIdentityUnavailable()
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
        return MainIdentityUnavailable()
    if not model:
        return MainIdentityUnavailable()
    return KnownMainIdentity(AgentIdentity(
        family="codex",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    ))


def _main_identity_from_claude(session_id: str) -> KnownMainIdentity | MainIdentityUnavailable:
    project_root = Path.home() / ".claude" / "projects"
    matches = list(project_root.rglob(f"{session_id}.jsonl"))
    if not matches:
        return MainIdentityUnavailable()
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
        return MainIdentityUnavailable()
    if not model:
        return MainIdentityUnavailable()
    return KnownMainIdentity(AgentIdentity(
        family="claude",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    ))


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
        if isinstance(identity, KnownMainIdentity):
            return identity.identity

    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if claude_session:
        identity = _main_identity_from_claude(claude_session)
        if isinstance(identity, KnownMainIdentity):
            return identity.identity

    return AgentIdentity(
        family=explicit_family or "unknown",
        model="unknown",
        effort=explicit_effort or "unset",
        session_id=explicit_session or codex_session or claude_session,
    )


def _refresh_main_identity(session_dir: Path, state: dict[str, object]) -> None:
    """Re-read the unit director's model and effort whenever a window opens.

    `start-run` detects the unit director once, and its model or effort can
    change part way through a run. Windows are also opened from inside workers:
    a Codex worker inherits the director's `CLAUDE_CODE_SESSION_ID` but detects
    as its own `CODEX_THREAD_ID`, and a Claude worker is a Claude Code session
    of its own. So a refresh replaces `main_agent` only when the detected
    session is the run's director (`unit_runs.director`), else the stored
    identity's session; with neither known, any detection is taken. The
    director is read from the run folder, not from `main_agent`, because runs
    started before this check carry a worker's thread there, and the director's
    next window repairs it. Detection that comes back unknown leaves
    `main_agent` alone too: a window that cannot answer must not erase the
    answer already recorded.
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
    stored = _object_dict(state.get("main_agent")) or {}
    owner = unit_runs.director(session_dir) or _string(stored.get("session_id"))
    if owner and detected["session_id"] != owner:
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


def _count_plan_phases(plan_path: Path) -> dict[str, object]:
    """Count a plan's phases by heading, classifying every form.

    Returns done/todo/total plus any heading this could not classify. An
    unclassifiable heading is reported, never counted by default: a miscount here
    silently corrupts every project percentage the run reports.
    """
    try:
        phases, duplicates = plan_history.plan_phase_details(plan_path)
    except OSError:
        return {"available": False, "reason": f"unable to read {plan_path}"}
    done = sum(phase["done"] for phase in phases)
    todo = len(phases) - done
    total = done + todo
    if total == 0:
        return {"available": False, "reason": f"no phase headings in {plan_path}"}
    return {
        "available": True,
        "done": done,
        "todo": todo,
        "total": total,
        "order": [phase["id"] for phase in phases],
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


def _bullet_end(text: str, first_line_end: int) -> int:
    """Offset just past a Markdown bullet, continuation lines included.

    A Delegation Context bullet routinely wraps across several lines, and its
    sub-bullets are indented under it. Inserting after only the first line would
    split the bullet in half and orphan its remainder.
    """
    end = first_line_end
    while end < len(text) and text[end] == "\n":
        line_start = end + 1
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if not line.strip() or not line[:1].isspace():
            break
        end = line_end
    return end


def _persist_plan_project_start(plan_path: Path, value: str) -> None:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to read plan document {plan_path}: {error}") from error
    project_match = PROJECT_PATTERN.search(text)
    if project_match is None:
        raise SystemExit(f"Plan document has no Delegation Context Project field: {plan_path}")
    insert_at = _bullet_end(text, project_match.end())
    updated = (
        text[:insert_at]
        + f"\n- **Project started:** {value}"
        + text[insert_at:]
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
    plan_path = plan_history.resolve_plan_path(working_dir, plan_doc)
    existing = _read_plan_project_start(plan_path)
    if existing is not None:
        return ProjectTiming(
            started_at=existing,
            source="plan_field",
            plan_doc=str(plan_path),
        )

    # The plan's own first checkpoint, never the plan file's first commit. A
    # phased plan is routinely born as the previous plan's `-next.md` backlog,
    # committed by that project's very first checkpoint — so dating a project
    # from the file's birth measures its predecessor's whole run as this one's
    # elapsed time, and the ETA extrapolates from a rate nobody worked at.
    # <CheckpointCommit/> writes `checkpoint(<plan-slug>):` on every phase, and
    # the closing paren keeps `github-runners` from matching
    # `github-runners-next`.
    checkpoint_times = _git_value(
        working_dir,
        "log",
        "--fixed-strings",
        f"--grep=checkpoint({plan_path.stem}):",
        "--format=%cI",
    )
    committed = [line.strip() for line in checkpoint_times.splitlines() if line.strip()]
    if committed:
        value = committed[-1]
        started_at = _iso_epoch(value, f"First checkpoint for {plan_path}")
        source = "plan_first_checkpoint"
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


def _historical_project_timing(
    working_dir: Path,
    branch: str,
) -> ProjectTiming | None:
    runs_dir = plan_history.history_root() / "runs"
    latest_event: dict[str, object] | None = None
    latest_started_at = -1.0
    try:
        history_paths = runs_dir.glob("*.jsonl")
    except OSError:
        return None
    for history_path in history_paths:
        event = plan_history.run_started_event(history_path)
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
    plan_path = plan_history.resolve_plan_path(working_dir, project_plan_doc)
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


# The worker this launcher occupies in its phase team, exported by implement.sh
# before it calls start-pass. A phase runs two launchers against one state
# file and each records its own pass, so this is the key their windows are kept
# apart under -- without it a team phase and a solo phase produce identical rows.
#
# The variable says role and its value is a worker: `impl` and `test` name the
# two chairs, not the work done in them. Both workers can be implementing at one moment and reviewing at another; the role a worker holds
# right now lives on the coordination board, which records it as a `handoff` and
# never writes it here. Reading the value as a role is the mistake this comment
# exists to stop -- the variable is the half with the wrong name, and it stays
# that way because implement.sh exports it.
TEAM_SLOT_ENV = "UNIT_TEAM_ROLE"


def _team_slot() -> str:
    """The recording member's worker, empty when nothing named one.

    Empty is a worker like any other rather than a missing value: passes predating
    the field, and every recorder that does not run through implement.sh --
    review.sh included -- record under it, and must stay distinguishable from a
    launcher that really is sitting in `impl`.
    """
    return os.environ.get(TEAM_SLOT_ENV) or os.environ.get("PLAN_DELEGATE_TEAM_ROLE", "")


def _record_slot(record: dict[str, object]) -> str:
    """The worker stamped on a pass record, under whichever key wrote it.

    The field was `team_role` for a day, until the value turned out to name the
    worker rather than the work. A run in flight across that change holds the old
    key on a pass it has already opened, and must not lose its worker halfway
    through.
    """
    if "team_slot" in record:
        return _string(record.get("team_slot"))
    return _string(record.get("team_role"))


def _pass_slots(state: dict[str, object]) -> dict[str, dict[str, object]]:
    """Every pass this phase has recorded, keyed by the slot that opened it.

    Returns the records state itself holds, so a caller that changes one changes
    state. State written before the phase team existed holds a single pass
    object instead of a map; it reads as the entry for its own worker,
    which is how a run already in flight keeps its open pass. Migration happens
    in memory and reaches disk only when something else writes state -- a run is
    never rewritten for the change alone.
    """
    raw = _object_dict(state.get("pass"))
    slots: dict[str, dict[str, object]] = {}
    if raw is not None and "instance_id" in raw:
        slots[_record_slot(raw)] = raw
    elif raw is not None:
        for slot, value in raw.items():
            record = _object_dict(value)
            if record is not None:
                slots[slot] = record
    state["pass"] = slots
    return slots


def _open_passes(state: dict[str, object]) -> dict[str, dict[str, object]]:
    """The slots whose pass is still running, keyed the same way."""
    return {
        slot: record
        for slot, record in _pass_slots(state).items()
        if _string(record.get("status")) == "active"
    }


def _pass_phase_instance(record: dict[str, object]) -> str:
    """The instance id of the phase a pass was opened in; empty for a pass older than the stamp."""
    phase = _object_dict(record.get("phase"))
    return _string(phase.get("instance_id")) if phase is not None else ""


def _phase_stamp(phase: dict[str, object]) -> dict[str, object]:
    """The fields a pass carries to name the phase it was opened in."""
    return {
        "instance_id": _string(phase.get("instance_id")),
        "id": _string(phase.get("id")),
        "title": _string(phase.get("title")),
        "started_at": _number(phase.get("started_at")),
    }


def _parked_phases(state: dict[str, object]) -> list[dict[str, object]]:
    """The older open phases, oldest first. Returns the list state holds."""
    raw = state.get(OPEN_PHASES_KEY)
    items = cast(list[object], raw) if isinstance(raw, list) else []
    parked = [record for item in items if (record := _object_dict(item)) is not None]
    state[OPEN_PHASES_KEY] = parked
    return parked


def _park_current(state: dict[str, object], current: dict[str, object]) -> None:
    """Move the current phase among the older open ones, with its own state.

    Its open passes stay in the slot map, where their launchers close them; a
    pass opened before passes carried a phase gains this one's stamp, so the
    phase that finishes later cannot claim it.
    """
    for record in _open_passes(state).values():
        if "phase" not in record:
            record["phase"] = _phase_stamp(current)
    current[PARKED_KEY] = {key: state.get(key) for key in PHASE_SCOPED_KEYS}
    _parked_phases(state).append(current)


def _unpark_newest(state: dict[str, object]) -> None:
    """Make the most recently started older open phase current again."""
    parked = _parked_phases(state)
    if not parked:
        return
    phase = parked.pop()
    saved = _object_dict(phase.pop(PARKED_KEY, None)) or {}
    for key in PHASE_SCOPED_KEYS:
        state[key] = saved.get(key)
    state["phase"] = phase


def _open_phase_named(state: dict[str, object], phase_id: str) -> dict[str, object] | None:
    """The open phase with this id: the current one, or an older one still open."""
    current = _object_dict(state.get("phase"))
    if current is not None and _string(current.get("status")) == "active":
        if _string(current.get("id")) == phase_id:
            return current
    for phase in _parked_phases(state):
        if _string(phase.get("id")) == phase_id:
            return phase
    return None


def _optional_arg(args: argparse.Namespace, name: str) -> str:
    """An option callers added after the recorder's own internal calls were written."""
    value: object = getattr(args, name, "")
    return value if isinstance(value, str) else ""


def _optional_epoch(args: argparse.Namespace, name: str, option: str, now: float) -> float:
    """A past moment an option names, for a phase recorded after the fact; now when absent."""
    value = _optional_arg(args, name)
    if not value:
        return now
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SystemExit(f"{option} must be an ISO 8601 time with a zone; got {value}") from error
    if parsed.tzinfo is None:
        raise SystemExit(f"{option} must be an ISO 8601 time with a zone; got {value}")
    moment = parsed.timestamp()
    if moment > now:
        raise SystemExit(f"{option} {value} is in the future")
    return moment


def _reporting_pass(state: dict[str, object]) -> dict[str, object] | None:
    """The open pass a call that is not a pass lifecycle call reports against.

    A launcher answers for its own slot. The unit director occupies no slot and
    still has to stamp its events, render a header, calibrate against what it is
    watching, and arm a review beside it, so it falls back to the pass that
    opened first. First rather than last because that one covers the phase from
    the front, and because a later slot opening must not move the header off the
    window the reader has been following.
    """
    open_passes = _open_passes(state)
    own = open_passes.get(_team_slot())
    if own is not None:
        return own
    current = _object_dict(state.get("phase"))
    current_id = _string(current.get("instance_id")) if current is not None else ""
    ordered = sorted(
        (
            entry
            for entry in open_passes.items()
            if _pass_phase_instance(entry[1]) in ("", current_id)
        ),
        key=lambda entry: (_number(entry[1].get("started_at")), entry[0]),
    )
    return ordered[0][1] if ordered else None


def _event(
    state: dict[str, object],
    event_type: str,
    now: float,
    pass_record: dict[str, object] | None = None,
    phase_record: dict[str, object] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "spine_version": SPINE_VERSION,
        "skill": SKILL_NAME,
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
    # An event that names its phase is that phase's own record, so the pass or
    # activity running in another phase does not belong on it.
    current_pass = pass_record
    if current_pass is None and phase_record is None:
        current_pass = _reporting_pass(state)
    # The phase an event belongs to: the one the caller names, else the one the
    # pass it reports against was opened in, else the current one. A pass of an
    # older open phase that finishes after a newer phase started still belongs
    # to the phase it worked for.
    phase = phase_record
    if phase is None and current_pass is not None:
        phase = _object_dict(current_pass.get("phase"))
    if phase is None:
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
    # worker happened to run before it. A pass lifecycle event names its own
    # record instead: several slots are open at once, and the launcher that
    # closes a stale pass is not always the one that opened it.
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
                "worker_name": _string(current_pass.get("worker_name")),
            }
        )
    activity = _object_dict(state.get("activity")) if phase_record is None else None
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


def _close_slot_pass(
    session_dir: Path,
    state: dict[str, object],
    slot: str,
    status: str,
    now: float,
) -> bool:
    """Close one slot's open pass, reporting whether there was one to close.

    One slot's and no other's: a launcher that closes a peer's window ends a
    pass whose agent is still working, and the ledger then describes a phase
    that never happened.
    """
    current_pass = _open_passes(state).get(slot)
    if current_pass is None:
        return False
    event = _event(state, "pass_finished", now, current_pass)
    event["status"] = status
    # Taken from the pass, not from the environment. A stale pass is closed by
    # whichever launcher starts the next one in that worker, and reading the
    # environment here would stamp the closer's worker onto the opener's work.
    event["team_slot"] = _record_slot(current_pass)
    event["pass_elapsed_seconds"] = max(
        0,
        int(now - _number(current_pass.get("started_at"), now)),
    )
    _append_event(state, event)
    current_pass["status"] = status
    current_pass["finished_at"] = now
    _write_state(session_dir, state)
    return True


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
    if _string(state.get("status")) != "active":
        raise SystemExit("Cannot start an activity: the run is finished")
    if _object_dict(state.get("phase")) is None:
        raise SystemExit("Start a phase before starting an activity")
    now = plan_history.now_epoch()
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = _read_state(session_dir)
    _refresh_main_identity(session_dir, state)
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
        plan_history.now_epoch(),
    )


def _start_run(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    existing = _state_path(session_dir)
    if existing.exists():
        state = _ensure_project_timing(
            session_dir,
            _read_state(session_dir),
            plan_history.now_epoch(),
        )
        print(_string(state.get("history_file"), str(existing)))
        return

    working_dir_value = _arg_string(args, "working_dir")
    if not working_dir_value:
        raise SystemExit("--working-dir is required")
    working_dir = Path(working_dir_value).expanduser().resolve()
    now = plan_history.now_epoch()
    run_id = session_dir.name
    branch = _git_value(working_dir, "branch", "--show-current")
    if not branch:
        short_hash = _git_value(working_dir, "rev-parse", "--short", "HEAD")
        branch = f"detached@{short_hash or 'unknown'}"
    plan_doc = _arg_string(args, "plan_doc")
    project_timing = _resolve_project_timing(working_dir, branch, plan_doc, now)
    history_file = plan_history.history_root() / "runs" / f"{run_id}.jsonl"
    main_agent = _detect_main_identity(args)
    if main_agent["model"] == "unknown":
        raise SystemExit(
            "Unable to detect the unit director's model and effort; pass explicit --main-* values"
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
        OPEN_PHASES_KEY: [],
        "pass": {},
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
    setting them from one bad phase is a threshold with no evidence behind it —
    this is how the data arrives.
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
    now = plan_history.now_epoch()
    phase_id = _arg_string(args, "phase_id", "ad hoc")
    # Starting a phase that is already open does nothing. Starting any other
    # leaves every open phase open: each closes only by its own finish-phase.
    if _open_phase_named(state, phase_id) is not None:
        return
    # A finished phase reopened for repair rounds gets its reservation state back from its kept
    # /sync board answer, so its edits pass the berth hook with no hand-written state.
    _ = phase_reservation.restore(phase_reservation.phase_directory(session_dir, phase_id), phase_id)
    # The phase count reads the plan's headings, so a phase the plan lacks makes
    # every later report disagree with its own phase number.
    missing_from = _phase_missing_from_plan(state, phase_id)
    if missing_from is not None:
        raise SystemExit(
            f"Phase {phase_id} has no heading in {missing_from}. Add "
            + f"`### Phase {phase_id} — <title>  · status: todo` under ## Phases, then start it."
        )
    started_at = _optional_epoch(args, "started_at", "--started-at", now)
    if _string(state.get("status")) != "active" and "finished_at" in state:
        previous_status = _string(state.get("status"))
        state["status"] = "active"
        _ = state.pop("finished_at", None)
        reopened = _event(state, "reopened", now)
        reopened.update({"previous_status": previous_status, "cause": "start-phase", "phase_id": phase_id})
        _append_event(state, reopened)
        _write_state(session_dir, state)
        director = unit_runs.director(session_dir) or _string(
            (_object_dict(state.get("main_agent")) or {}).get("session_id"))
        if director:
            active_root = os.environ.get("UNIT_ACTIVE_DIR")
            # A temporary run stays within its temporary root unless explicitly configured.
            marker = unit_runs.marker(director, Path(active_root) if active_root else session_dir.parent / "active")
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(f".{marker.name}.{os.getpid()}")
            _ = temporary.write_text(f"{session_dir}\n", encoding="utf-8")
            os.replace(temporary, marker)
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = _read_state(session_dir)
    active = _object_dict(state.get("phase"))
    if active is not None and _string(active.get("status")) == "active":
        _park_current(state, active)
    else:
        state["pass"] = {}
    phase: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "id": phase_id,
        "title": _arg_string(args, "phase_title", "Ad hoc work"),
        "started_at": started_at,
        "status": "active",
    }
    state["phase"] = phase
    state[ARMED_REVIEW_KEY] = None
    state["phase_last_percent"] = None
    state["phase_percent_started_at"] = None
    state["last_percent"] = None
    state["percent_started_at"] = None
    state["pending_calibration"] = None
    _write_state(session_dir, state)
    event = _event(state, "phase_started", started_at)
    event["contract_revision"] = _contract_revision()
    event["wait_coverage"] = {"state": "Recorded", "version": 1}
    if started_at != now:
        event["recorded_at"] = _iso_time(now)
        event["backfilled"] = True
    event.update(_work_order_metrics(_arg_string(args, "work_order_file")))
    _append_event(state, event)
    _refresh_phase_table(session_dir)


class LedgerRepairCause(TypedDict):
    """The repair batch that still authorizes work before closure review finishes."""

    round: int
    covered: list[str]


class LaunchCheck(TypedDict):
    """A launch checked against its open phase and prompt before any pass starts."""

    phase_id: str
    phase_title: str
    prompt_phase_line: Literal["matched", "absent"]
    fix_cause: str | LedgerRepairCause


def _launch_refused(
    state: dict[str, object], args: argparse.Namespace, reason: str,
) -> NoReturn:
    event = _event(state, "launch_refused", plan_history.now_epoch(), phase_record={})
    event.update(reason=reason, pass_kind=_arg_string(args, "pass_kind"),
                 phase_id=_optional_arg(args, "phase_id"), prompt_file=_optional_arg(args, "prompt_file"),
                 lens=_optional_arg(args, "lens"))
    _append_event(state, event)
    raise SystemExit(reason)


def _launch_check(
    args: argparse.Namespace, state: dict[str, object], phase: dict[str, object], session_dir: Path,
) -> LaunchCheck:
    """Check prompt identity and a fix's cause before changing any open pass."""
    prompt_phase_line: Literal["matched", "absent"] = "absent"
    prompt_file = _optional_arg(args, "prompt_file")
    if prompt_file:
        try:
            text = Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            _launch_refused(state, args, f"Unable to read prompt file {prompt_file}: {error}")
        for line in text.splitlines():
            named = re.match(r"^Phase (\S+) — (.*)$", line)
            if named is None:
                continue
            if named.group(1) != _string(phase.get("id")) or named.group(2).strip() != _string(phase.get("title")).strip():
                _launch_refused(state, args, f"Stale prompt phase: {line}; open phase is Phase {phase.get('id')} — {phase.get('title')}")
            prompt_phase_line = "matched"
            break
    fix_cause: str | LedgerRepairCause = _optional_arg(args, "fix_cause").strip()
    if _arg_string(args, "pass_kind") == "fix":
        instance = _string(phase.get("instance_id"))
        for path in (session_dir / "findings_state.json", session_dir / f"findings_state-{instance}.json"):
            try:
                ledger = _json_object(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except OSError as error:
                _launch_refused(state, args, f"Unable to read findings ledger {path}: {error}")
            if ledger is None:
                _launch_refused(state, args, f"Invalid findings ledger: {path}")
            if _string(ledger.get("phase_instance_id")) != instance:
                continue
            rounds: object = ledger.get("rounds")
            if isinstance(rounds, list) and rounds:
                repair = _object_dict(cast(list[object], rounds)[-1])
                if repair is not None:
                    covered = _string_list(repair.get("covered"))
                    findings = _object_dict(ledger.get("findings")) or {}
                    pending = any(
                        _string((_object_dict(findings.get(finding_id)) or {}).get("state")) == "fixed_pending_review"
                        for finding_id in covered
                    )
                    outcome = _string(repair.get("outcome"))
                    if outcome == "in_flight" or (outcome == "landed" and pending):
                        fix_cause = LedgerRepairCause(round=_integer(repair.get("round")), covered=covered)
            break
        if not fix_cause:
            _launch_refused(state, args, "Fix launch has no ledger round in flight or awaiting closure; give --fix-cause naming the failed gate or defect")
    return {"phase_id": _string(phase.get("id")), "phase_title": _string(phase.get("title")),
            "prompt_phase_line": prompt_phase_line, "fix_cause": fix_cause}


def _checked_launch(
    args: argparse.Namespace, state: dict[str, object], session_dir: Path,
) -> tuple[dict[str, object], LaunchCheck]:
    """Validate a named open phase and its launch without changing any pass."""
    phase_id = _optional_arg(args, "phase_id")
    if not phase_id:
        _launch_refused(state, args, "Missing phase: --phase-id is required for a recorded run")
    phase = _open_phase_named(state, phase_id)
    if phase is None:
        open_ids = ", ".join(recorder_state.open_phase_ids(state)) or "none"
        _launch_refused(state, args, f"Phase {phase_id} is not open; open phases: {open_ids}. {CLOSED_PHASE_RULE}")
    return phase, _launch_check(args, state, phase, session_dir)


def _check_launch(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase, launch_check = _checked_launch(args, state, session_dir)
    if _optional_arg(args, "lens") == "ux":
        event = _event(state, "launch_checked", plan_history.now_epoch(), phase_record=phase)
        event.update(launch_check=launch_check, pass_kind=_arg_string(args, "pass_kind"),
                     lens="ux", prompt_file=_optional_arg(args, "prompt_file"))
        _append_event(state, event)


def _start_pass(args: argparse.Namespace) -> None:
    if not _launcher_owned():
        raise SystemExit(f"start-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
    session_dir = _session_dir(args)
    pass_kind = _arg_string(args, "pass_kind")
    fix_pass = _arg_integer(args, "fix_pass")
    called_agent = AgentIdentity(
        family=_arg_string(args, "called_family", "unknown"),
        model=_arg_string(args, "called_model", "unknown"),
        effort=_arg_string(args, "called_effort", "unset") or "unset",
        session_id="",
    )
    team_slot = _team_slot()
    state = _read_state(session_dir)
    # A phase running beside the current one names itself, so its pass is not stamped with
    # the current phase's.
    phase, launch_check = _checked_launch(args, state, session_dir)
    now = plan_history.now_epoch()
    # This slot's stale pass and no other. The peers belong to launchers still
    # waiting on their own agents, and a phase team opens both within the same
    # second: closing them here is the corruption this key exists to
    # prevent, not the cleanup it looks like.
    _ = _close_slot_pass(session_dir, state, team_slot, "interrupted", now)
    state = _read_state(session_dir)
    _refresh_main_identity(session_dir, state)
    current_pass: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "kind": pass_kind,
        "fix_pass": fix_pass,
        "activity": _arg_string(args, "activity"),
        "started_at": now,
        "called_task": _arg_string(args, "called_task"),
        "worker_name": _optional_arg(args, "worker_name"),
        "called_agent": called_agent,
        "team_slot": team_slot,
        "phase": _phase_stamp(phase),
        "status": "active",
    }
    slots = _pass_slots(state)
    slots[team_slot] = current_pass
    _write_state(session_dir, state)
    event = _event(state, "pass_started", now, current_pass)
    event["launch_check"] = launch_check
    event["config_digest"] = _config_digest()
    event["team_slot"] = team_slot
    _append_event(state, event)
    # The reviewer this pass belongs to has been rendering as an armed row since
    # its early launch; its real window supersedes that one rather than joining
    # it, so the marker retires here instead of leaving two review rows.
    if pass_kind == "review":
        _ = _clear_armed_review(session_dir, _read_state(session_dir), "adopted", now)


def _finish_pass(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    status = _arg_string(args, "status")
    orphaned = bool(getattr(args, "orphaned_launcher", False))
    if not _launcher_owned():
        if not orphaned:
            raise SystemExit(f"finish-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
        if status != "canceled":
            raise SystemExit(
                "--orphaned-launcher closes the pass of a launcher the unit director "
                + f"killed, so it takes --status canceled, not {status}"
            )
    # The launcher that finishes is the launcher that started, so the slot comes
    # from the environment here rather than from the record. Resolving it any
    # other way would let one member close a peer's pass by being the only one
    # still running.
    slot = _team_slot()
    state = _read_state(session_dir)
    closed = _close_slot_pass(session_dir, state, slot, status, plan_history.now_epoch())
    if orphaned and not closed:
        raise SystemExit(
            "No pass is open in this slot, so no launcher was orphaned and "
            + "nothing was recorded"
        )


def _resolve_worker_agent(task: str) -> AgentIdentity:
    """Ask the shared agent registry which agent a task resolves to.

    The early reviewer is armed by the unit director in the same turn it launches
    `review.sh`, so the launcher has not written its resolved identity yet and
    waiting for it would be a poll. Resolving through the same bash entry point
    the launcher uses gives the Worker cell the same answer with no race.
    """
    resolver = Path(__file__).resolve().parent.parent / "agents" / "agents_config.sh"
    unknown = AgentIdentity(family="", model="", effort="", session_id="")
    if not resolver.is_file():
        return unknown
    try:
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && agents_resolve_print "$2"', "_", str(resolver), task],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return unknown
    if result.returncode != 0:
        return unknown
    fields = dict(
        part.split("=", 1)
        for part in result.stdout.strip().split(" ")
        if "=" in part
    )
    return AgentIdentity(
        family=fields.get("family", ""),
        model=fields.get("agent", ""),
        effort=fields.get("effort", "") or "unset",
        session_id="",
    )


def _arm_review(args: argparse.Namespace) -> None:
    """Record that a blind reviewer is running alongside the open pass.

    Deliberately not launcher-owned: no pass is opened and no pass event is
    written, so nothing convergence counts can be forged from here. The marker
    only earns the reviewer a row in the stage table while the writer it
    overlaps is still going.
    """
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase_id = _arg_string(args, "phase_id")
    phase = _open_phase_named(state, phase_id) if phase_id else _object_dict(state.get("phase"))
    if phase is None or _string(phase.get("status")) != "active":
        raise SystemExit(f"Phase {phase_id} is not open" if phase_id else "Start a phase before arming an early review")
    if _reporting_pass(state) is None:
        raise SystemExit(
            "An early review is the reviewer that overlaps a running dispatch, so "
            + "a pass must be open. With none open the review is not early -- "
            + "launch it through review.sh and let it record its own pass."
        )
    now = plan_history.now_epoch()
    called_task = _arg_string(args, "called_task", "direct.review") or "direct.review"
    lens = _arg_string(args, "lens")
    suffix = f"_{lens}" if lens else ""
    # `review.sh --phase` writes its files in the phase's directory, so the marker names the same one.
    artifact_dir = phase_reservation.phase_directory(session_dir, phase_id) if phase_id else session_dir
    armed: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "kind": "review",
        "phase_instance_id": _string(phase.get("instance_id")),
        "activity": _arg_string(args, "activity"),
        "started_at": now,
        "called_task": called_task,
        "called_agent": _resolve_worker_agent(called_task),
        # `review.sh` suffixes both files with its lens, so an armed reviewer
        # must name the same pair the launcher it precedes will write. Empty is
        # the single-reviewer layout and the unsuffixed names.
        "status_file": str(artifact_dir / f"review_status{suffix}"),
        "pid_file": str(artifact_dir / f"review_pid{suffix}"),
        "status": "armed",
    }
    state[ARMED_REVIEW_KEY] = armed
    _write_state(session_dir, state)
    event = _event(state, "early_review_armed", now)
    event.update(
        {
            "early_review_instance_id": _string(armed.get("instance_id")),
            "early_review_activity": _string(armed.get("activity")),
        }
    )
    _append_event(state, event)


def _clear_armed_review(
    session_dir: Path,
    state: dict[str, object],
    reason: str,
    now: float,
) -> bool:
    armed = _object_dict(state.get(ARMED_REVIEW_KEY))
    if armed is None:
        return False
    state[ARMED_REVIEW_KEY] = None
    _write_state(session_dir, state)
    event = _event(state, "early_review_disarmed", now)
    event.update(
        {
            "early_review_instance_id": _string(armed.get("instance_id")),
            "reason": reason,
            "early_review_elapsed_seconds": max(
                0,
                int(now - _number(armed.get("started_at"), now)),
            ),
        }
    )
    _append_event(state, event)
    return True


def _disarm_review(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    _ = _clear_armed_review(
        session_dir,
        state,
        _arg_string(args, "reason", "cleared") or "cleared",
        plan_history.now_epoch(),
    )


def _load_events() -> tuple[list[dict[str, object]], int]:
    events: list[dict[str, object]] = []
    ignored = 0
    runs_dir = plan_history.history_root() / "runs"
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
        phase_events = [event for event in events if _string(event.get("phase_instance_id")) == phase_instance_id]
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
                if phase_finished_at <= phase_started_at or reported_at < phase_started_at:
                    continue
                phase_duration = _work_seconds(phase_events, phase_instance_id, phase_started_at, phase_finished_at)
                phase_elapsed = _work_seconds(phase_events, phase_instance_id, phase_started_at, reported_at)
                if phase_duration <= 0:
                    continue
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
                        cap_stage=_string(report.get("cap_stage")),
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


def _calibration_scopes(
    samples: list[CalibrationSample],
    state: dict[str, object],
    cap_stage: str,
) -> list[tuple[str, list[CalibrationSample]]]:
    """Every history a suggestion may draw on, narrowest first.

    The stage is a fact and the percent is an estimate, so a named stage narrows
    every identity scope and the whole history sits behind them as the widest.
    An 86 entered during implementation and an 86 entered during closure sit
    hours apart, and pooling them is how one tick drew a suggestion of 55 between
    neighbours of 20 and 15. With no stage named the identity scopes are the
    whole ladder.
    """
    # Match on the open window, the same rule `_event` records by. A finished
    # pass left in state would otherwise match this activity's report against
    # samples from a worker that is no longer running.
    current_pass = _reporting_pass(state) or {}
    main_agent = _object_dict(state.get("main_agent")) or {}
    called_agent = _object_dict(current_pass.get("called_agent")) or {}
    pass_kind = _string(current_pass.get("kind"))
    main_model = _string(main_agent.get("model"))
    main_effort = _string(main_agent.get("effort"))
    called_model = _string(called_agent.get("model"))
    called_effort = _string(called_agent.get("effort"))

    identity: list[tuple[str, Callable[[CalibrationSample], bool]]] = [
        (
            "main_called_pass",
            lambda sample: sample["pass_kind"] == pass_kind
            and sample["main_model"] == main_model
            and sample["main_effort"] == main_effort
            and sample["called_model"] == called_model
            and sample["called_effort"] == called_effort,
        ),
        (
            "called_pass",
            lambda sample: sample["pass_kind"] == pass_kind
            and sample["called_model"] == called_model
            and sample["called_effort"] == called_effort,
        ),
        ("pass", lambda sample: sample["pass_kind"] == pass_kind),
        ("all_models_and_passes", lambda _sample: True),
    ]
    if not cap_stage:
        return [
            (name, [sample for sample in samples if matches(sample)])
            for name, matches in identity
        ]
    staged = [sample for sample in samples if sample["cap_stage"] == cap_stage]
    return [
        *(
            (f"stage_{name}", [sample for sample in staged if matches(sample)])
            for name, matches in identity
        ),
        ("all_stages", samples),
    ]


def _window_weight(sample: CalibrationSample, candidate_percent: int) -> float:
    """How far one report speaks for the entered percent: fully at the same
    number, fading to nothing at the edge of CALIBRATION_WINDOW."""
    distance = abs(sample["raw_percent"] - candidate_percent)
    return max(0.0, 1.0 - distance / CALIBRATION_WINDOW)


def _window_totals(
    samples: list[CalibrationSample],
    candidate_percent: int,
) -> tuple[float, float]:
    """A scope's weight around the entered percent, and its weighted bias sum."""
    weight = 0.0
    weighted_bias = 0.0
    for sample in samples:
        sample_weight = _window_weight(sample, candidate_percent)
        weight += sample_weight
        weighted_bias += sample_weight * sample["raw_bias_percentage_points"]
    return weight, weighted_bias


def _smoothed_bias(
    scopes: list[tuple[str, list[CalibrationSample]]],
    candidate_percent: int,
) -> tuple[float, float]:
    """The bias the history around this percent shows, and the widest scope's weight.

    Each narrower scope leans on the one around it until it holds
    MIN_CALIBRATION_SAMPLES of weight of its own, so the answer slides from the
    broad history to the matching one as samples accumulate. Picking one scope
    and one exact percent outright made the answer jump wherever either choice
    flipped; this blend moves only as far as the entered percent does. The
    widest scope's weight decides whether history says anything at all.
    """
    *narrower, (_, widest) = scopes
    support, weighted_bias = _window_totals(widest, candidate_percent)
    bias = weighted_bias / support if support > 0 else 0.0
    for _, scoped in reversed(narrower):
        weight, weighted_bias = _window_totals(scoped, candidate_percent)
        bias = (weighted_bias + MIN_CALIBRATION_SAMPLES * bias) / (
            weight + MIN_CALIBRATION_SAMPLES
        )
    return bias, support


def _matching_scope(
    scopes: list[tuple[str, list[CalibrationSample]]],
    candidate_percent: int,
) -> tuple[str, list[CalibrationSample]]:
    """The narrowest scope that clears the sample floor on its own weight, cut to
    its reports inside the window: the history the suggestion leans on most, and
    the one the metrics beside it describe."""
    window = f"within_{CALIBRATION_WINDOW}_percentage_points"
    for name, scoped in scopes:
        weight, _ = _window_totals(scoped, candidate_percent)
        if weight >= MIN_CALIBRATION_SAMPLES:
            return f"{window}:{name}", [
                sample for sample in scoped if _window_weight(sample, candidate_percent) > 0
            ]
    _, widest = scopes[-1]
    return f"{window}:insufficient", [
        sample for sample in widest if _window_weight(sample, candidate_percent) > 0
    ]


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
    now = plan_history.now_epoch()
    candidate = _arg_integer(args, "candidate_percent")
    if not 0 <= candidate <= 100:
        raise SystemExit("--candidate-percent must be between 0 and 100")
    cap_stage = _arg_string(args, "cap_stage")
    events, ignored = _load_events()
    scopes = _calibration_scopes(_calibration_samples(events), state, cap_stage)
    scope, samples = _matching_scope(scopes, candidate)
    bias, support = _smoothed_bias(scopes, candidate)
    apply_suggestion = support >= MIN_CALIBRATION_SAMPLES
    suggestion = candidate
    if apply_suggestion:
        corrected = max(5.0, min(95.0, candidate - bias))
        suggestion = int(5 * round(corrected / 5.0))
    current_hold = _current_hold_seconds(state, suggestion, now)
    metrics = _sample_metrics(samples, current_hold)
    calibration: dict[str, object] = {
        "calibration_id": str(uuid.uuid4()),
        "candidate_percent": candidate,
        "cap_stage": cap_stage,
        "suggested_percent": suggestion,
        "smoothed_bias_percentage_points": _round_metric(bias),
        "apply_suggestion": apply_suggestion,
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
    # An activity is unit-director work with no convergence meaning, so it carries a
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
        "test": "Test",
        # `arch` is retired, not renamed: escalation is an agent tier, not a kind
        # of work, and `called_task` already records it for 156 fix passes.
        # Four back-corpus passes still spell it, and title-casing renders those.
    }.get(pass_kind, pass_kind.title() or "Pass")


def _progress_decision(
    calibration: dict[str, object] | None,
    raw_percent: int,
    reported_percent: int,
    override_reason: str,
    scope: str,
    cap_stage: str,
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
        # A suggestion drawn from one stage's history says nothing about another's.
        calibrated_stage = _string(calibration.get("cap_stage"))
        if calibrated_stage and calibrated_stage != cap_stage:
            raise SystemExit(
                f"The pending {scope} calibration was drawn for stage {calibrated_stage}, "
                + f"not {cap_stage or 'none'}; run calibrate again with the same --cap-stage"
            )
        suggested_percent = _integer(calibration.get("suggested_percent"), raw_percent)
        apply_suggestion = calibration.get("apply_suggestion") is True
        bias_value = calibration.get("smoothed_bias_percentage_points")
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


def _work_seconds(
    events: list[dict[str, object]], phase_instance_id: str, started_at: float, until: float
) -> int:
    """The phase's working time from `started_at` to `until`, idle stretches left out.

    A pass or an activity open is work. A stretch with none open counts up to
    `IDLE_GRACE_SECONDS`, the unit director's own work between dispatches; the rest of
    it is waiting, and an ETA extended from a clock that ran through a night quotes
    that night again for the work left.
    """
    opened: dict[str, float] = {}
    windows: list[tuple[float, float]] = []
    for event in events:
        if _string(event.get("phase_instance_id")) != phase_instance_id:
            continue
        event_type = _string(event.get("event_type"))
        key = _string(event.get("pass_instance_id" if event_type.startswith("pass_") else "activity_instance_id"))
        at = min(until, max(started_at, _number(event.get("timestamp_epoch"))))
        if event_type in ("pass_started", "activity_started") and key:
            opened[key] = at
        elif event_type in ("pass_finished", "activity_finished") and key in opened:
            windows.append((opened.pop(key), at))
    windows.extend((start, until) for start in opened.values())
    if not windows:
        # History with no windows says nothing about idleness, so the wall clock stands.
        return max(0, int(until - started_at))
    work, idle_from = 0.0, started_at
    for start, end in sorted(windows):
        if start > idle_from:
            work += min(start - idle_from, IDLE_GRACE_SECONDS)
        if end > idle_from:
            work += end - max(start, idle_from)
            idle_from = end
    if until > idle_from:
        work += min(until - idle_from, IDLE_GRACE_SECONDS)
    return int(work)


def _clock_line(now: float) -> str:
    """Anchor the durations above to wall-clock time.

    Every other number in the header is a duration, which says how long but
    never when. This line is the one place a reader can tell whether the report
    they are looking at is current.
    """
    return f"**now {datetime.fromtimestamp(now, user_zone()):%Y-%m-%d %H:%M:%S}**"


def _moment_stamp(epoch: float, now: float) -> str:
    """A local clock time, dated only when it falls on a day other than `now`'s."""
    moment = datetime.fromtimestamp(epoch, user_zone())
    if moment.date() == datetime.fromtimestamp(now, user_zone()).date():
        return f"{moment:%H:%M:%S}"
    return f"{moment:%Y-%m-%d %H:%M:%S}"


STAGE_HEADERS: tuple[str, ...] = (
    "Stage",
    "Main",
    "Worker",
    "Start",
    "Elapsed",
    "Result",
)
# The two slots a phase always runs, in the order they are reported: the workers
# that write. A slot is a fixed identity; the role it is doing is what the cell
# shows, so a `test` slot opened as a writer reads "impl" under Agent 2.
ROUND_SLOTS: tuple[str, ...] = ("impl", "test")
# The worker the broad review's craft lens records under. No writer sits there,
# so it is a third column only when a stage the table shows assigned it, and a
# table without one keeps the two columns it always had.
REVIEW_SLOT = "review"
# The statuses that mean a window is being worked right now. Narrower than the
# set `_window_span` runs to the clock: a window left `open` is one whose finish
# was never recorded, so its launcher is gone and its span is merely unbounded.
# Reading that as work is the mistake the worker states exist to stop making.
LIVE_WINDOW_STATUSES: tuple[str, ...] = ("running", "armed")
# A pass kind's display name, where the two differ. `arch` is deliberately
# absent: it is retired rather than renamed, and the handful of back-corpus
# passes carrying it fall through to `.title()` unchanged.
STAGE_KIND_LABELS: dict[str, str] = {"impl": "Impl", "test": "Test"}
SUMMARY_HEADERS: tuple[str, ...] = ("Scope", "%", "Elapsed", "ETA", "Unchanged")
# Every summary column holding a magnitude or a time, named rather than indexed
# so the alignment follows a column that moves. The two scope labels and the
# phase tally read as words and stay left.
RIGHT_ALIGNED_SUMMARY_COLUMNS = frozenset({"%", "Elapsed", "ETA", "ETA low", "ETA high"})
# How much of a worker's last board line the note under the table carries. The
# board caps a post far higher than a terminal line can hold, and a wrapped
# narration costs the reader the three-line scan the note exists to give.
BOARD_NOTE_CHARS = 72
# What implement.sh puts in front of the `done` and `blocked` it posts on the
# worker's exit, so the note can tell the launcher's line from the worker's own.
LAUNCHER_PREFIX = "launcher:"
# What a worker's own narration says when it is alive but not working: held up on
# a peer's edit, on a token, on a gate. Nothing in the pass record separates that
# from work -- the process is up and the window is open either way -- so the last
# board line is the only source there is, and these are the words the workers use.
WAITING_PHRASES: tuple[str, ...] = ("waiting", "wait for", "blocked on", "standing by")
# Where a window that sits in no round is drawn when its kind names a worker. A
# lone reviewer between rounds -- the closure review, the broad review of a
# solo run -- is doing review work and reads in the `test` column, where the
# adversary lens sits; a row of dashes would hide it. A unit-director activity
# names no worker and keeps them.
LONE_WORKERS: dict[str, str] = {"impl": "impl", "fix": "impl", "test": "test", "review": "test"}
# How many stages the round table draws. A long phase runs dozens -- ten repair
# rounds and eleven verifications in the one that prompted the cap -- and a
# table that tall is scrolled past rather than read. What a reader acts on is
# the last few; the rest is history, and `timeline` still renders it in full.
ROUND_TABLE_STAGES = 3
# How close together two role movements are before they are one movement of the
# team. Workers converge in sequence -- each worker's `board.sh role` call landing
# a second or two apart -- and a boundary each would put a near-empty row on
# every call. The cost is small: a movement inside the window describes its row
# from the row's start, at most this many seconds early, which is invisible at
# the minute granularity the table prints.
ROLE_WAVE_SECONDS = 30.0
# The label the director opens an activity with while the UX helper judges the
# phase's shots; review-trial counts that time as review time.
UX_REVIEW_ACTIVITY_LABEL = "UX review"
# What findings.py's `--lens both` stands for: the two lenses the broad review
# ran before `craft` joined them.
LEGACY_BOTH_LENS = "both"
LEGACY_BOTH_LENSES: tuple[str, ...] = ("adversary", "contract")


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
    return f"{datetime.fromtimestamp(epoch, user_zone()):%H:%M:%S}"


def _started_window(event: dict[str, object], event_type: str) -> StageWindow:
    if event_type == "pass_started":
        model, effort = _agent_fields(event, "called_agent")
        instance_id = _string(event.get("pass_instance_id"))
        kind = _string(event.get("pass_kind"))
        label = ""
        started_at = _number(event.get("pass_started_at"))
    else:
        # An activity is the unit director working directly, so it has no worker
        # and the Worker cell stays empty rather than repeating the unit director.
        model, effort = "", ""
        instance_id = _string(event.get("activity_instance_id"))
        kind = "activity"
        label = _string(event.get("activity_label"), "Work")
        started_at = _number(event.get("activity_started_at"))
    main_model, main_effort = _agent_fields(event, "main_agent")
    return StageWindow(
        instance_id=instance_id,
        # An activity is the unit director, which sits in no worker. Passes recorded
        # before the field existed also read empty, and land in the same place:
        # a row of their own rather than a worker's column.
        slot="" if event_type != "pass_started" else _record_slot(event),
        kind=kind,
        fix_round=_integer(event.get("fix_pass")),
        label=label,
        text=_string(event.get("activity_text")) if kind == "activity" else "",
        started_at=started_at or _number(event.get("timestamp_epoch")),
        elapsed=0,
        status="open",
        result="",
        worker=_agent_display(model, effort),
        main=_agent_display(main_model, main_effort),
    )


def _running_window(
    current: dict[str, object],
    slot: str,
    kind: str,
    label: str,
    worker: str,
    main: str,
    fallback_id: str,
    now: float,
    text: str = "",
) -> StageWindow:
    started_at = _number(current.get("started_at"), now)
    return StageWindow(
        instance_id=_string(current.get("instance_id")) or fallback_id,
        slot=slot,
        kind=kind,
        fix_round=_integer(current.get("fix_pass")),
        label=label,
        text=text,
        started_at=started_at,
        elapsed=max(0, int(now - started_at)),
        status="running",
        result="running",
        worker=worker,
        main=main,
    )


def _live_windows(state: dict[str, object], now: float) -> list[StageWindow]:
    """The windows running right now, read from state rather than the events.

    Their finishing events do not exist yet, and a session upgraded mid-phase
    has an activity whose start event predates activity identity, so state is
    the only source that always knows what is running. A phase team runs a
    launcher per slot and each opens its own pass, so this is a row apiece; the
    unit director's activity is the running window only when no pass is open at
    all, which is what makes verification a row and never a second one beside a
    dispatch.
    """
    main = _agent_display(*_agent_fields(state, "main_agent"))
    open_passes = _open_passes(state)
    if open_passes:
        return [
            _running_window(
                current,
                slot,
                _string(current.get("kind")),
                "",
                _agent_display(*_agent_fields(current, "called_agent")),
                main,
                f"live:{slot}",
                now,
            )
            for slot, current in sorted(open_passes.items())
        ]
    activity = _object_dict(state.get("activity"))
    if activity is None or _string(activity.get("status")) != "active":
        return []
    return [
        _running_window(
            activity,
            "",
            "activity",
            _string(activity.get("label"), "Work"),
            "",
            main,
            "live",
            now,
            text=_string(activity.get("activity")),
        )
    ]


def _launcher_file_written_since(path_value: str, epoch: float) -> str | None:
    """A launcher-written file's contents, or None when it predates `epoch`."""
    if not path_value:
        return None
    path = Path(path_value)
    try:
        if path.stat().st_mtime <= epoch:
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _early_reviewer_working(armed: dict[str, object]) -> bool:
    """Whether the launcher behind an armed row is still doing the review.

    Two files `review.sh` writes, because neither covers the other. Its status
    catches a reviewer that failed before delivery and left the marker standing;
    its pid catches a kill, which writes no status at all.

    Both are believed only once written after the arm. At launch each still
    holds the previous pass's value, and reading that would retire the row
    before this reviewer had drawn breath -- the same stale-artifact trap the
    early launch itself has to clear before it starts.
    """
    started_at = _number(armed.get("started_at"))
    status = _launcher_file_written_since(_string(armed.get("status_file")), started_at)
    if status is not None and status != "reviewing":
        return False
    pid = _launcher_file_written_since(_string(armed.get("pid_file")), started_at)
    if pid is None or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _armed_window(
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
) -> StageWindow | None:
    """The early reviewer's row, running beside the pass it overlaps.

    Rendered from the marker rather than an event, and dropped the moment the
    launcher that owns it is gone: a marker outliving a killed reviewer would
    show a phantom agent working, which is worse than showing nothing.
    """
    armed = _object_dict(state.get(ARMED_REVIEW_KEY))
    if armed is None or _string(armed.get("status")) != "armed":
        return None
    if _string(armed.get("phase_instance_id")) != phase_instance_id:
        return None
    if not _early_reviewer_working(armed):
        return None
    model, effort = _agent_fields(armed, "called_agent")
    main_model, main_effort = _agent_fields(state, "main_agent")
    started_at = _number(armed.get("started_at"), now)
    return StageWindow(
        instance_id=_string(armed.get("instance_id")) or "armed",
        # The early reviewer occupies the `test` worker before its pass exists --
        # where the adversary lens sits and where a lone closure reviewer reads
        # -- so it claims that column. Its round is left at zero and resolved
        # against the phase in `_round_windows`: the marker is written before
        # the round it reads is known, and the writer running beside it is what
        # defines it.
        slot="test",
        kind="review",
        fix_round=0,
        label="",
        text="",
        started_at=started_at,
        elapsed=max(0, int(now - started_at)),
        status="armed",
        result="running (early)",
        worker=_agent_display(model, effort),
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
        for live in _live_windows(state, now):
            started = windows.get(live["instance_id"])
            if started is not None and started["text"]:
                # Every progress tick rewrites the state activity's text with
                # its --activity narration, so the live record no longer says
                # what the window ran. The start event does, and keeps saying
                # it for as long as the window is open.
                live["text"] = started["text"]
            windows[live["instance_id"]] = live
        # A phase team runs a launcher per slot, so several passes are open at
        # once and each has a row above. The armed marker adds one more: an
        # early-launched reviewer reading the diff a writer beside it is still
        # producing, which has no pass of its own until the ready sentinel.
        armed = _armed_window(state, phase_instance_id, now)
        if armed is not None:
            windows[armed["instance_id"]] = armed
    return sorted(windows.values(), key=lambda window: window["started_at"])


def _stage_labels(windows: list[StageWindow]) -> list[str]:
    """Name each window by kind, and a review by the pass whose diff it reads.

    A review row answers "which one" far better as `Impl Review` or `Review Fix
    2` than as an ordinal the reader has to count rows to place, because a
    review always follows the pass it judges. Activities are transparent here:
    verification sits between a fix and its review without breaking the pair. A
    review with no unclaimed pass behind it -- the closure review after
    verification -- keeps the ordinal, which is all that is true of it. A fix
    carries the round the ledger dispatched, so its number is the one
    convergence counts, and the review naming it repeats that number.
    """
    counts: dict[str, int] = {}
    labels: list[str] = []
    reviewed: tuple[str, str] | None = None
    for window in windows:
        kind = window["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        index = counts[kind]
        if kind == "activity":
            labels.append(window["label"] or "Work")
        elif kind == "review":
            if reviewed is None:
                labels.append(f"Review {index}")
            elif reviewed[0] == "fix":
                labels.append(f"Review {reviewed[1]}")
            else:
                labels.append(f"{reviewed[1]} Review")
            reviewed = None
        else:
            if kind == "fix":
                name = f"Fix {window['fix_round'] or index}"
            else:
                base = STAGE_KIND_LABELS.get(kind) or kind.title() or "Pass"
                name = base if index == 1 else f"{base} {index}"
            reviewed = (kind, name)
            labels.append(name)
    return labels


def _finding_tally(
    findings: list[dict[str, object]],
    lower: float,
    upper: float,
) -> FindingTally:
    """What the ledger recorded between one window opening and the next.

    Findings are opened, dispatched, and settled by the unit director in the gap
    after a window closes, so the interval that starts at a window and ends at
    its successor is what attributes them to the pass that produced them.

    This is the `timeline` view's attribution and it only holds where passes run
    one at a time. A phase team opens two at once, and slicing by timestamp
    then gives the first a window under a second wide and hands the whole
    phase to whichever registered last -- so the progress report groups by
    `_round_tally` instead, on a round the ledger already stamps.
    """
    return _tallied(
        event
        for event in findings
        if lower <= _number(event.get("timestamp_epoch")) < upper
    )


def _round_tally(findings: list[dict[str, object]], round_number: int) -> FindingTally:
    """What the ledger recorded for one repair round.

    Exact where a time slice can only estimate: `findings.py` stamps `round` on
    every event it writes, so a finding names the round it belongs to rather
    than being inferred from when it happened. Events predating the field are
    left out entirely instead of defaulting into round zero, which would credit
    the impl round with the whole back corpus.
    """
    return _tallied(
        event
        for event in findings
        if "round" in event and _integer(event.get("round")) == round_number
    )


def _tallied(findings: Iterable[dict[str, object]]) -> FindingTally:
    """Count one already-selected set of finding events."""
    tally = FindingTally(opened=0, landed=0, accepted=0, still_open=0, reopened=0)
    for event in findings:
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
    if status in ("running", "armed"):
        return window["result"] or "running"
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


class BoardActivity(TypedDict):
    """The latest board line under a worker, and the worker's own last words."""

    at: float
    kind: str
    message: str
    # The worker's latest line that the launcher did not write, empty when the
    # worker has said nothing itself since it was launched. Carried beside
    # `message` because the launcher's `done` lands after the worker's final
    # narration, and a note that read only the latest line reported every
    # finished worker with the same boilerplate.
    own: str


class RoleChange(TypedDict):
    """One moment a slot took a role, as the coordination board recorded it."""

    at: float
    slot: str
    role: str
    # `register` or `handoff`. Only a handoff is a movement: the launchers
    # register a second or two apart, and treating that stagger as role
    # changes splits a round into a row per launch plus the real one.
    kind: str


def _board_stamp(line: str) -> float | None:
    """The epoch a board line was written, or None if it carries no timestamp."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\b", line)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


def _board_role_changes(session_dir: Path) -> list[RoleChange]:
    """Every role each slot has held this run, in the order it took them.

    Roles move during a phase and the movement is the point: a writer that
    finishes early takes a slice of the tests, and at the end both converge on
    review. The board is what knows -- `board.sh
    role` stamps a `role=<name>` field on each `handoff` so it reads back exactly
    rather than being inferred from prose, and the launcher stamps the same field
    on its `register` line so a slot reports a role from second zero.

    The whole history rather than the latest entry, because a round that changed
    composition part way through has to render as more than one row: collapsing
    to the current answer would report the last shape as though it had held all
    along.
    """
    changes: list[RoleChange] = []
    try:
        text = (session_dir / "board.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return changes
    for line in text.splitlines():
        match = re.search(r"\[([^\]]+)\] (register|handoff): (.*)$", line)
        if match is None:
            continue
        role = re.search(r"\brole=(\w+)", match.group(3))
        at = _board_stamp(line)
        if role is None or at is None:
            continue
        changes.append(
            RoleChange(
                at=at,
                slot=match.group(1),
                role=role.group(1),
                kind=match.group(2),
            )
        )
    return sorted(changes, key=lambda change: change["at"])


def _launcher_authored(kind: str, message: str) -> bool:
    """Whether a board line came from implement.sh rather than from the worker.

    The launcher writes two kinds of line: `register`, which is always its, and
    a `done` or `blocked` on exit, which it prefixes `launcher:` so the note can
    tell it from a `done` the worker posted itself.
    """
    return kind == "register" or message.startswith(LAUNCHER_PREFIX)


def _board_activity(session_dir: Path) -> dict[str, BoardActivity]:
    """The most recent line under each worker on the board, keyed by slot.

    A worker that recorded no pass reaches the table with a role and no duration,
    and a role on its own cannot tell a worker deep in its work from one that
    stopped an hour ago. The board can. Every worker posts what it is about to
    do before doing it, attributed to its slot, so the latest line and its age
    answer the question a pass window alone cannot -- for a worker that has one as
    much as for a worker that does not, since an open window says only that the
    launcher is still waiting.

    The latest line of any kind, not just `status`: a worker whose last act was a
    handoff or a `blocked` is exactly the worker a reader needs to hear about, and
    filtering to narration would render it as silent.

    Beside it, the worker's own last words. The launcher posts `done` when the
    worker exits, which is after the worker's final narration, so the latest line
    of every finished worker is the launcher's boilerplate and the narration the
    reader wanted sits one line up. A `register` clears them: it opens a new
    occupancy of the worker, and words from the previous one do not describe it.
    """
    activity: dict[str, BoardActivity] = {}
    try:
        text = (session_dir / "board.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return activity
    for line in text.splitlines():
        match = re.search(r"\[([^\]]+)\] (\w+): (.*)$", line)
        at = _board_stamp(line)
        if match is None or at is None:
            continue
        slot, kind, message = match.group(1), match.group(2), match.group(3).strip()
        previous = activity.get(slot)
        if kind == "register":
            own = ""
        elif _launcher_authored(kind, message):
            own = previous["own"] if previous is not None else ""
        else:
            own = message
        activity[slot] = BoardActivity(at=at, kind=kind, message=message, own=own)
    return activity


def _board_phrase(said: BoardActivity) -> str:
    """One worker's last board line, cut to fit a terminal row.

    A `status` is narration and reads as itself. Any other kind is named,
    because the kind is then the news -- `blocked` and `done` change what the
    reader should do next, where a bare message would look like more work in
    progress. The words are the worker's own wherever it has any: the launcher's
    `done` supplies the kind and the worker's last narration supplies what it was
    doing when it finished, which is what the reader asked. A leading `role=`
    field is dropped: the role it announces is already in that worker's column,
    and repeating it here spends the line.
    """
    message = said["own"] or said["message"].removeprefix(LAUNCHER_PREFIX)
    message = re.sub(r"^\s*role=\S+\s*", "", message).strip()
    body = message if said["kind"] == "status" else f"{said['kind']}: {message}"
    body = body.strip() or said["kind"]
    if len(body) > BOARD_NOTE_CHARS:
        body = body[: BOARD_NOTE_CHARS - 1].rstrip() + "\u2026"
    return body


def _roles_over(changes: list[RoleChange], upper: float) -> dict[str, str]:
    """What each slot was doing across one stretch of a round.

    Any entry before the stretch ends describes it from the start. For a
    register that is the roll-call reasoning: the launchers land a second
    or two apart, and reading them strictly would show a worker idle while the
    third works. For a handoff it holds because `_round_segments` opens a
    boundary at every movement wave: a handoff inside this stretch is either
    the movement that opened it or a member of that movement's wave, seconds
    behind the boundary, and both belong to the whole stretch.
    """
    roles: dict[str, str] = {}
    for change in changes:
        if change["at"] < upper:
            roles[change["slot"]] = change["role"]
    return roles


def _role_movements(changes: list[RoleChange]) -> list[RoleChange]:
    """The handoffs that changed what their slot was doing.

    Workers re-announce the role they already hold -- an opening `board.sh role`
    seconds after the launcher registered the same role, or narration routed
    through `role` because it is the command that takes a note. Neither moves
    the team, and a row split on one shows columns identical to the row
    above with a fresh clock: noise with a timestamp. A movement is a handoff
    whose role differs from the one its slot was last reported doing, by any
    entry kind -- a register opens an occupancy with a role, and re-stating that
    role is no more a movement than re-stating a handoff's.
    """
    current: dict[str, str] = {}
    movements: list[RoleChange] = []
    for change in changes:
        if change["kind"] == "handoff" and current.get(change["slot"]) != change["role"]:
            movements.append(change)
        current[change["slot"]] = change["role"]
    return movements


def _compact_duration(seconds: int) -> str:
    """A duration narrow enough to share a cell with a role word.

    `_format_duration`'s `HH:MM:SS` is right for a column of its own, where the
    colons align and the reader compares magnitudes down the column. Two of
    these sit side by side in one row instead, each behind a role, so the unit
    is spelled and the leading zero components are dropped.
    """
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _round_result(tally: FindingTally, running: bool) -> str:
    """A round's outcome: what it settled, what it left, what it turned up."""
    unresolved = tally["still_open"] + tally["reopened"]
    parts: list[str] = []
    if tally["accepted"]:
        parts.append(f"{tally['accepted']} fixed")
    if unresolved:
        parts.append(f"{unresolved} open")
    if tally["opened"]:
        # "found" alone on the impl round, where nothing had been found before;
        # "new" once a round has settled something, because there the count is
        # what this round added on top rather than the phase total.
        parts.append(f"{tally['opened']} new" if parts else f"{tally['opened']} found")
    if parts:
        return ", ".join(parts)
    return "running" if running else "clean"


def _gate_report(
    label: str,
    block: list[StageWindow],
    now: float,
) -> tuple[str, list[str]]:
    """One verification block's Result cell, and the gate notes under the table.

    A block is a run of consecutive unit-director activities sharing a label: the
    phase gates, run one command at a time. Each distinct command is one gate,
    and a window repeating an earlier command is the same gate run again after
    a repair -- one line telling fail-then-pass is the story, where a row per
    command rendered a healthy retry as a stretch of near-identical rows the
    reader could tell apart only by result.
    """
    gates: dict[str, list[StageWindow]] = {}
    for window in block:
        gates.setdefault(window["text"] or window["label"], []).append(window)
    lines: list[str] = []
    failing = reran = 0
    live_position = 0
    for position, (text, runs) in enumerate(gates.items(), start=1):
        last = runs[-1]
        earlier_fails = sum(
            1 for run in runs[:-1] if run["status"] not in LIVE_WINDOW_STATUSES
        )
        retried = (
            f"failed {earlier_fails}x"
            if earlier_fails > 1
            else f"{_compact_duration(runs[0]['elapsed'])} failed"
        )
        if last["status"] in LIVE_WINDOW_STATUSES:
            live_position = position
            duration = _compact_duration(max(0, int(now - last["started_at"])))
            if earlier_fails:
                lines.append(f"  - ✗→▸ {text} · {retried} · rerunning {duration}")
            else:
                lines.append(f"  - ▸ {text} · running {duration}")
        elif last["status"] == "completed":
            duration = _compact_duration(last["elapsed"])
            if earlier_fails:
                reran += 1
                lines.append(f"  - ✗→✓ {text} · {retried} · passed on rerun {duration}")
            else:
                lines.append(f"  - ✓ {text} · {duration}")
        else:
            failing += 1
            lines.append(f"  - ✗ {text} · {_compact_duration(last['elapsed'])} failed")
    count = len(gates)
    header = f"- **{label}** (unit director) · {count} gate{'s' if count > 1 else ''}:"
    if live_position:
        summary = f"gate {live_position} running"
    else:
        parts: list[str] = []
        if failing:
            parts.append(f"{failing} failing")
        if reran:
            parts.append(f"{reran} failed, reran clean")
        summary = ", ".join(parts) or (f"{count} pass" if count > 1 else "pass")
    return summary, [header, *lines]


def _round_of(window: StageWindow, open_round: int) -> int:
    """The round a window belongs to.

    An armed reviewer is the one window that cannot answer for itself: the
    marker is written before the round it will read has a number, so it takes
    the round of the writer it is running beside.
    """
    return open_round if window["status"] == "armed" else window["fix_round"]


class RoundEntry(TypedDict):
    """One row of the progress report: a round of workers, or a lone window."""

    label: str
    started_at: float
    # The round these workers worked, and None for a window that sits in no round:
    # a unit-director activity, or a pass from a run that recorded no worker at all.
    # The distinction picks the row's finding attribution, which is the whole
    # reason the two shapes stay separable rather than being forced into one.
    number: int | None
    members: list[StageWindow]


def _round_entries(windows: list[StageWindow], labels: list[str]) -> list[RoundEntry]:
    """The phase's rows: one per round, plus one per window that has no round.

    A round is the unit that advances -- it is dispatched, it lands, and the
    ledger stamps it on every finding it produces -- so it is the row, and the
    workers working it read across as columns. Everything else keeps a row
    of its own: verification is the unit director between rounds, in no worker and in
    no round, and a solo run records no worker at all, so both render one window
    per row exactly as they did before the phase team existed.
    """
    rounds = {
        window["fix_round"]
        for window in windows
        if window["slot"] and window["status"] != "armed"
    }
    open_round = max(rounds, default=0)
    grouped: dict[int, list[StageWindow]] = defaultdict(list)
    entries: list[RoundEntry] = []
    for index, window in enumerate(windows):
        # An armed reviewer joins the round it reads only when a round exists to
        # join. In a solo run it is the only assigned window there is, and it
        # belongs beside the writer in a row of its own, as it always rendered.
        lone = not window["slot"] or (window["status"] == "armed" and not rounds)
        if lone:
            entries.append(
                RoundEntry(
                    label=labels[index],
                    started_at=window["started_at"],
                    number=None,
                    members=[window],
                )
            )
            continue
        grouped[_round_of(window, open_round)].append(window)
    for number, members in grouped.items():
        # Round zero is whatever the phase opened with -- an implementation, or a
        # review of work that arrived already written -- so it is named for the
        # kind its workers actually ran. Every later round is a repair round, and
        # carries the number convergence counts.
        if number:
            label = f"Fix {number}"
        else:
            kinds = {member["kind"] for member in members}
            opening = next((k for k in ("impl", "fix", "test", "review") if k in kinds), "")
            label = STAGE_KIND_LABELS.get(opening) or opening.title() or "Impl"
        entries.append(
            RoundEntry(
                label=label,
                started_at=min(member["started_at"] for member in members),
                number=number,
                members=members,
            )
        )
    return sorted(entries, key=lambda entry: entry["started_at"])


def _stage_groups(entries: list[RoundEntry]) -> list[list[RoundEntry]]:
    """The phase's stages: consecutive entries sharing a label are one stage.

    Only a window in no round repeats a label -- a round carries its number --
    and those come in runs: the four verifications a repair round triggers land
    seconds apart and are one stage of the phase, not four. Counting them singly
    would let a run of them fill the whole table and push the repair work that
    produced them off the bottom.
    """
    groups: list[list[RoundEntry]] = []
    for entry in entries:
        if groups and groups[-1][0]["label"] == entry["label"]:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _earlier_note(elided: list[list[RoundEntry]]) -> str:
    """The one line standing in for the stages the table does not draw.

    Without it a phase eleven hours and twenty stages deep opens on `Fix 9` and
    reads as one that has barely started, and the reader cannot tell whether the
    missing rows are a truncation or an absence.
    """
    if not elided:
        return ""
    first, last = elided[0][0]["label"], elided[-1][-1]["label"]
    span = first if first == last else f"{first} through {last}"
    stages = "stage" if len(elided) == 1 else "stages"
    return f"*Earlier: {len(elided)} {stages} not shown - {span}.*"


def _window_span(window: StageWindow, now: float) -> tuple[float, float]:
    """When a window opened and when it closed, an open one running to now."""
    started_at = window["started_at"]
    if window["status"] in ("running", "armed", "open"):
        return started_at, max(now, started_at)
    return started_at, started_at + window["elapsed"]


def _round_segments(
    entry: RoundEntry,
    changes: list[RoleChange],
    now: float,
) -> list[tuple[float, float, dict[str, str]]]:
    """A round split at every moment its workers changed what they were doing.

    A round is not one shape held to the end. Workers get recruited across as the
    work moves -- one writing while the other tests, then both writing, then
    both converging on review -- and a single row would report whichever shape happened to be last
    as though it had held throughout. Each segment is a stretch over which no
    worker changed role, so a row per segment tells the reader what the team was
    when, which is what the board records and nothing else preserves.
    """
    start = entry["started_at"]
    end = max(
        (_window_span(member, now)[1] for member in entry["members"]),
        default=max(now, start),
    )
    end = max(end, start)
    # Only a movement splits: a register is a launcher opening its slot, and a
    # handoff that re-states the role its slot already holds moves nothing, so
    # splitting on either repeats the row above with a fresh clock. A movement
    # exactly at the round's start opens it rather than splitting it, and one
    # at or past its end belongs to the round that follows.
    # A worker re-occupied is the other way a round moves on, and the one no
    # handoff marks: a blind reviewer is a fresh session that takes the chair
    # after the writer's has exited, so nothing posts a movement between them.
    # One boundary per wave rather than per movement -- workers converge seconds
    # apart, and a boundary each would put a near-empty row on every one, so a
    # candidate within ROLE_WAVE_SECONDS of the boundary before it joins that
    # boundary's row instead of opening its own.
    occupancies = _slot_occupancies(entry["members"])
    depth = max((len(windows) for windows in occupancies.values()), default=0)
    waves = {
        min(
            windows[index]["started_at"]
            for windows in occupancies.values()
            if len(windows) > index
        )
        for index in range(1, depth)
    }
    # A worker outside the phase's opening impl/test pair joins the round when its
    # first occupancy begins. Treat that as a reassigning workers wave just like the
    # original craft-review worker, so `review2` and `fix2` do not appear to have
    # worked from the writers' start time.
    for slot, assigned in occupancies.items():
        if slot not in ROUND_SLOTS and assigned:
            waves.add(assigned[0]["started_at"])
    candidates = sorted({change["at"] for change in _role_movements(changes)} | waves)
    boundaries = [start]
    for at in candidates:
        if start < at < end and at - boundaries[-1] >= ROLE_WAVE_SECONDS:
            boundaries.append(at)
    segments: list[tuple[float, float, dict[str, str]]] = []
    for index, lower in enumerate(boundaries):
        upper = boundaries[index + 1] if index + 1 < len(boundaries) else end
        segments.append((lower, upper, _roles_over(changes, upper)))
    return segments


def _slot_occupancies(members: list[StageWindow]) -> dict[str, list[StageWindow]]:
    """Each worker's windows in one round, oldest first.

    A worker sits down more than once in a round wherever the round runs a stage
    after the one it opened with: the writers finish, and the reviewers that
    read what they wrote take the same chairs. Keeping one
    member per worker reported whichever occupancy happened to be last as though
    it had held the round from the start.
    """
    occupancies: dict[str, list[StageWindow]] = defaultdict(list)
    for member in members:
        occupancies[member["slot"]].append(member)
    for windows in occupancies.values():
        windows.sort(key=lambda window: window["started_at"])
    return occupancies


def _worker_is_waiting(said: BoardActivity | None) -> bool:
    """Whether a worker's last board line says it is held up rather than working."""
    if said is None or said["message"].startswith(LAUNCHER_PREFIX):
        # A launcher writes one line only as its worker exits, and a worker that has
        # exited is not waiting on anything.
        return False
    if said["kind"] == "blocked":
        return True
    text = (said["own"] or said["message"]).lower()
    return any(phrase in text for phrase in WAITING_PHRASES)


def _worker_state(member: StageWindow, live: bool, said: BoardActivity | None) -> str:
    """What this worker is doing at the end of the stretch its cell covers.

    The duration cannot say it. An open window and a closed one both render a
    number, and a number that stopped growing looks exactly like one still
    growing, so the reader's first question -- is anyone still working -- is the
    one the row could not answer.

    `done` is every stretch that is already over, which is every row but the
    last of a live round. Inside that last row three answers separate. A worker
    whose window has closed is `idle`: finished with what it was given while the
    round it belongs to is not, so it is a worker available for more work rather
    than a phase that ended. A worker whose window is open is working, unless its
    own narration says it is held up on a peer, a token, or a gate -- which the
    records cannot see, and which is `waiting`. Both workers reading `running`
    while one sits on the other is the picture this exists to correct.
    """
    if not live:
        return "done"
    if member["status"] not in LIVE_WINDOW_STATUSES:
        return "idle"
    return "waiting" if _worker_is_waiting(said) else "running"


def _round_cells(
    members: list[StageWindow],
    roles: dict[str, str],
    lower: float,
    upper: float,
    now: float,
    live: bool,
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
) -> list[str]:
    """A cell per worker in `slots`: its role over this stretch, how long, and its state."""
    occupancies = _slot_occupancies(members)
    cells: list[str] = []
    for slot in slots:
        # The occupancy this stretch falls in: the last one that had begun by
        # the time the stretch ended.
        assigned = occupancies.get(slot, [])
        member = next(
            (window for window in reversed(assigned) if window["started_at"] < upper),
            None,
        )
        if member is None:
            # A worker that recorded no pass: it was launched without a pass
            # kind, or it died before `start-pass`. The board still knows its
            # role, and knows it after the round closes -- the old reading gated
            # this on the round running, so a finished round rendered its other
            # workers as "-" and read as a solo pass. The duration is what is
            # genuinely unknown here; the worker is not.
            cells.append(roles.get(slot) or "-")
            continue
        # The board's role wins wherever it has one, for this stretch rather than
        # for now: it is the only source that tracks a worker recruited across, and
        # reading it per segment is what keeps an earlier row describing the team
        # that was. A worker whose board never spoke reports the kind it recorded.
        # Only for the worker's opening occupancy, though: a later one is a
        # different session in the same chair, and the role the last occupant
        # left behind describes work that is already over.
        role = (roles.get(slot) or member["kind"]) if member is assigned[0] else member["kind"]
        opened, closed = _window_span(member, now)
        overlap = int(min(closed, upper) - max(opened, lower))
        cells.append(
            " ".join(
                word
                for word in (
                    role,
                    _compact_duration(overlap) if overlap > 0 else "",
                    _worker_state(member, live, activity.get(slot)),
                )
                if word
            )
        )
    return cells


def _lone_cells(
    window: StageWindow,
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
) -> list[str]:
    """The worker cells of a window in no round: its kind's worker filled, or none."""
    cells = ["-"] * len(slots)
    worker = LONE_WORKERS.get(window["kind"])
    if worker is not None:
        # One worker, so there is no round to outlive: it is either running or
        # over, and `idle` -- a worker free while its round continues -- cannot
        # arise here.
        live = window["status"] in LIVE_WINDOW_STATUSES
        said = activity.get(window["slot"]) if window["slot"] else None
        cells[slots.index(worker)] = " ".join(
            (
                window["kind"],
                _compact_duration(window["elapsed"]),
                _worker_state(window, live, said),
            )
        )
    return cells


class RoundTable(TypedDict):
    """The rows the round table draws, and the note for the ones it drops."""

    rows: list[list[str]]
    earlier: str
    # The gate notes for the newest collapsed verification block: its header
    # line and one line per gate. Empty when the shown stages hold no such
    # block. Only the newest block's -- an earlier one keeps its row and its
    # tally, but its gate-by-gate story is history the reader is not acting on.
    gates: list[str]
    # The workers the rows have cells for, in column order: ROUND_SLOTS, and the
    # review worker after them when a shown stage assigned it.
    slots: tuple[str, ...]


def _round_headers(slots: tuple[str, ...]) -> list[str]:
    """The round table's header, with a column for each worker in `slots`."""
    return [
        "Stage",
        "Start",
        "Elapsed",
        *(f"Agent {index}" for index, _slot in enumerate(slots, start=1)),
        "Result",
    ]


def _reported_slots(
    shown: list[list[RoundEntry]],
    windows: list[StageWindow],
    changes: list[RoleChange],
) -> tuple[str, ...]:
    """The worker columns this phase needs, in first-seen order after impl/test.

    The writers' two always, so a phase without a craft review keeps the table
    it always drew. The original `review` worker retains its prior visibility
    rule when it only recorded a pass: it appears while a shown stage workers it,
    keeping existing impl/test/review tables byte-identical. Every other worker
    that recorded a phase pass, plus every worker with a board handoff, gets a
    column ordered by the first of those records.
    """
    shown_assigned = {
        member["slot"]
        for group in shown
        for entry in group
        for member in entry["members"]
    }
    appearances: list[tuple[float, str]] = []
    for window in windows:
        slot = window["slot"]
        if not slot or slot in ROUND_SLOTS:
            continue
        if slot == REVIEW_SLOT and REVIEW_SLOT not in shown_assigned:
            continue
        appearances.append((window["started_at"], slot))
    appearances.extend(
        (change["at"], change["slot"])
        for change in changes
        if change["kind"] == "handoff" and change["slot"] not in ROUND_SLOTS
    )
    extra: list[str] = []
    for _at, slot in sorted(appearances, key=lambda appearance: appearance[0]):
        if slot not in extra:
            extra.append(slot)
    return (*ROUND_SLOTS, *extra)


def _round_table(
    windows: list[StageWindow],
    labels: list[str],
    findings: list[dict[str, object]],
    changes: list[RoleChange],
    activity: dict[str, BoardActivity],
    now: float,
) -> RoundTable:
    """The last ROUND_TABLE_STAGES stages, each split wherever its workers moved.

    A stage keeps every row it owns: a repair round that recruited two workers
    across renders the two rows that say so. The cap counts stages rather than
    rows because a row is not a unit a reader asks for -- "what happened in the
    last few stages" is the question, and a stage answers it whole.
    """
    # A window with no round is attributed the way the stage table always did
    # it, by the interval running to whatever opened next. That slice is wrong
    # for concurrent workers, which is why a round does not use it -- but it is
    # exactly right for the sequential windows that still reach this branch.
    # Its worker cells come from its kind: a lone review sits under the test worker.
    uppers = {
        window["instance_id"]: (
            windows[index + 1]["started_at"]
            if index + 1 < len(windows)
            else max(now, window["started_at"]) + 1.0
        )
        for index, window in enumerate(windows)
    }
    groups = _stage_groups(_round_entries(windows, labels))
    shown = groups[-ROUND_TABLE_STAGES:]
    slots = _reported_slots(shown, windows, changes)
    rows: list[list[str]] = []
    gate_lines: list[str] = []
    for group in shown:
        # A stage of two or more unit-director activities is a verification block:
        # the gates of the phase, run one command at a time. One row for the
        # block, with the gate-by-gate story -- which command, what happened,
        # retries folded into their gate -- in the notes under the table, where
        # a row per command told the reader nothing but pass/error.
        if len(group) > 1 and all(
            entry["number"] is None and entry["members"][0]["kind"] == "activity"
            for entry in group
        ):
            block = [entry["members"][0] for entry in group]
            summary, notes = _gate_report(group[0]["label"], block, now)
            block_start = block[0]["started_at"]
            block_end = max(_window_span(window, now)[1] for window in block)
            rows.append(
                [
                    group[0]["label"],
                    _clock_stamp(block_start),
                    _compact_duration(int(block_end - block_start)),
                    *(["-"] * len(slots)),
                    summary,
                ]
            )
            gate_lines = notes
            continue
        for entry in group:
            members, started_at = entry["members"], entry["started_at"]
            running = any(member["status"] in LIVE_WINDOW_STATUSES for member in members)
            if entry["number"] is None:
                lone = members[0]
                tally = _finding_tally(findings, started_at, uppers[lone["instance_id"]])
                rows.append(
                    [
                        entry["label"],
                        _clock_stamp(started_at),
                        _compact_duration(lone["elapsed"]),
                        *_lone_cells(lone, activity, slots),
                        _window_result(lone, tally),
                    ]
                )
                continue
            segments = _round_segments(entry, changes, now)
            previous_roles: list[str] = []
            for index, (lower, upper, roles) in enumerate(segments):
                last = index + 1 == len(segments)
                # Only the closing row of a live round is the present. Every
                # earlier segment is a stretch that ended, whatever its workers
                # are doing now.
                cells = _round_cells(
                    members, roles, lower, upper, now, running and last, activity, slots
                )
                # A continuation row exists because the team moved, and the
                # reader should not have to diff two rows of cells to find
                # where. The row's result cell says so in the table's own
                # column terms, from the rendered cells rather than the board
                # -- what it reports is exactly what changed on screen, whether
                # a handoff or a reassigned chair caused it.
                worker_roles = [cell.split()[0] for cell in cells]
                moved = ", ".join(
                    f"Agent {position} → {role}"
                    for position, (_slot, before, role) in enumerate(
                        zip(slots, previous_roles, worker_roles), start=1
                    )
                    if role != before and role != "-"
                )
                previous_roles = worker_roles
                # The result belongs to the round, which is the granularity the
                # ledger stamps, so it sits on the row that closes it, after
                # the movement that opened the row if one did.
                outcome = (
                    _round_result(_round_tally(findings, entry["number"]), running)
                    if last
                    else ""
                )
                # The round label names the round once. A continuation row is
                # the same round with its workers moved, and repeating the name
                # there would read as a second round rather than as the team
                # having moved.
                rows.append(
                    [
                        entry["label"] if index == 0 else "",
                        _clock_stamp(lower),
                        _compact_duration(int(upper - lower)),
                        *cells,
                        "; ".join(part for part in (moved, outcome) if part),
                    ]
                )
    return RoundTable(
        rows=rows,
        earlier=_earlier_note(groups[: len(groups) - len(shown)]),
        gates=gate_lines,
        slots=slots,
    )


def _current_worker_windows(windows: list[StageWindow]) -> dict[str, StageWindow]:
    """Each worker's live window, otherwise its most recent finished window."""
    current: dict[str, StageWindow] = {}
    for window in windows:
        slot = window["slot"]
        if not slot:
            continue
        previous = current.get(slot)
        if previous is None or (
            window["status"] in LIVE_WINDOW_STATUSES, window["started_at"]
        ) >= (previous["status"] in LIVE_WINDOW_STATUSES, previous["started_at"]):
            current[slot] = window
    return current


def _current_worker_cells(
    windows: list[StageWindow],
    labels: list[str],
    changes: list[RoleChange],
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
    now: float,
) -> tuple[str, ...]:
    """Current worker details, independent of the director's latest activity."""
    current = _current_worker_windows(windows)
    roles = _roles_over(changes, now + 1)
    live_round = {
        member["instance_id"]: any(peer["status"] in LIVE_WINDOW_STATUSES for peer in entry["members"])
        for entry in _round_entries(windows, labels)
        for member in entry["members"]
    }
    cells: list[str] = []
    for slot in slots:
        window = current.get(slot)
        if window is None:
            cells.append(roles.get(slot) or "-")
            continue
        cells.append(" ".join((
            roles.get(slot) or window["kind"],
            _compact_duration(window["elapsed"]),
            _worker_state(window, live_round[window["instance_id"]], activity.get(slot)),
        )))
    return tuple(cells)


def _worker_note(
    windows: list[StageWindow],
    activity: dict[str, BoardActivity],
    now: float,
    slots: tuple[str, ...],
    worker_cells: tuple[str, ...] = (),
) -> list[str]:
    """A line per worker the table has a column for: its agent, and its last words.

    Which model is sitting in which worker is the thing a reader cannot recover
    from the table and asks about first when a worker runs long -- and it is not
    the only thing the table cannot say. A duration is the age of an open pass
    window, which grows whether the worker is working or wedged, and a worker that
    recorded no pass shows a role and nothing else. The board line closes both
    gaps: what the worker said last, and how long ago, which is the difference
    between working and stalled.

    It goes under the table rather than into it -- one answer per worker, not per
    row, and a column repeating it down every row would crowd out the roles. The
    unit director is left out; the reader is the unit director. So is a worker the
    table draws no column for: the review worker's board line outlives the phase
    that assigned it, and a line with no column above it names nothing on screen.
    """
    model_windows = list(_current_worker_windows(windows).values()) if worker_cells else windows
    live = {
        window["slot"]: window["worker"]
        for window in model_windows
        if window["slot"] and (worker_cells or window["status"] in LIVE_WINDOW_STATUSES)
    }
    ended = {window["slot"]: window["started_at"] + window["elapsed"] for window in model_windows if window["slot"]}
    lines: list[str] = []
    for position, slot in enumerate(slots, start=1):
        agent, said = live.get(slot), activity.get(slot)
        if agent is None and said is None:
            continue
        # A worker whose stretch ended long ago is not news, and its row crowds out the live ones.
        if (worker_cells and worker_cells[position - 1].endswith(" done")
                and now - max(ended.get(slot, 0.0), said["at"] if said else 0.0) > FINISHED_WORKER_SECONDS):
            continue
        if said is None:
            # Launched and yet to narrate. Naming that is the useful answer: a
            # worker short of its first line is either still starting or gone
            # before it began, and neither should read to the reader as work.
            detail = "no board line yet"
        else:
            age = _compact_duration(max(int(now - said["at"]), 0))
            detail = f"{age} ago \u00b7 {_board_phrase(said)}"
        lines.append(
            f"- **Agent {position}** ({slot})"
            + (f" {worker_cells[position - 1]} \u00b7" if worker_cells else "")
            + f"{f' {agent}' if agent else ''} \u00b7 {detail}"
        )
    return lines

def _stage_table(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool = True,
) -> tuple[list[StageWindow], list[str], list[list[str]]]:
    """The phase's windows, their labels, and the rows rendered from both.

    Callers that only print take the rows; the progress header also needs the
    label of one particular window, which it can no longer find by position now
    that a second row can be running.
    """
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
                window["worker"],
                _clock_stamp(window["started_at"]),
                _format_duration(window["elapsed"]),
                _window_result(window, _finding_tally(findings, window["started_at"], upper)),
            ]
        )
    return windows, labels, rows


def _stage_rows(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool = True,
) -> list[list[str]]:
    return _stage_table(events, state, phase_instance_id, now, include_live)[2]


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


def _arrival_label(arrival_epoch: float, now: float) -> str:
    """A point in time named the way a person would say it aloud on `now`'s day."""
    arrival = datetime.fromtimestamp(arrival_epoch, user_zone())
    days = (arrival.date() - datetime.fromtimestamp(now, user_zone()).date()).days
    if days == 0:
        return f"today {arrival:%H:%M}"
    if days == 1:
        return f"tomorrow {arrival:%H:%M}"
    return f"{arrival:%Y-%m-%d %H:%M}"


def _eta_cell(percent: int, elapsed: int, as_of: float, now: float, floor: int = 0) -> str:
    """When the work is expected to land, as a clock time rather than a duration.

    A remaining duration has to be added to the current time by hand every time
    it is read, and the answer changes with every report. An arrival stamp is
    that addition already done, and it says the same thing tomorrow morning.
    `elapsed` is measured at `as_of`, which a report between windows sets to
    its last recorded moment; the day is still named from `now`.
    """
    eta = plan_history.eta_seconds(percent, elapsed)
    return "" if eta is None else _arrival_label(as_of + max(eta, floor), now)


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
    as_of: float,
    now: float,
    spread: float,
    floor: plan_history.EtaBand | None = None,
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
    band = plan_history.eta_band_seconds(percent, elapsed, spread)
    if isinstance(band, plan_history.EtaProjectionUnavailable):
        return "", ""
    if floor is not None:
        band = plan_history.EtaBand(
            remaining=max(band.remaining, floor.remaining),
            earliest=max(band.earliest, floor.earliest),
            latest=max(band.latest, floor.latest),
        )
    return (
        f"{_arrival_label(as_of + band.earliest, now)} "
        + f"(-{_format_offset(band.remaining - band.earliest)})",
        f"{_arrival_label(as_of + band.latest, now)} "
        + f"(+{_format_offset(band.latest - band.remaining)})",
    )


def _unchanged_cell(seconds: int) -> str:
    return _format_duration(seconds) if seconds > 0 else ""


def _scope_line(state: dict[str, object]) -> str:
    """Where the run is working: the worktree, and the branch when it differs.

    How far through the plan the run is belongs to the Phases column, and the
    phase in flight is named by its id in the Phase row. The line carries no
    position of its own: a plan with deferred earlier phases or retired ids
    puts the active phase at an ordinal that matches neither number.
    """
    worktree = _string(state.get("worktree"))
    branch = _string(state.get("branch"))
    if not branch or branch == worktree:
        return f"**{worktree}**"
    return f"**{worktree} - {branch}**"


def _timeline(args: argparse.Namespace) -> None:
    """Render the stage table for one phase, or for every phase of the run.

    The progress header only ever shows the phase in flight. This answers the
    questions asked after the fact -- how many fix passes, how long each review
    took -- without reading the raw event stream by hand.
    """
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = plan_history.now_epoch()
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


# What a report that describes no live window adds. The recorder knows what its
# own records say and nothing about the processes behind them: a live run once
# had two workers with passes recorded `error` while their workers were still
# posting to the board, and reading the closed passes as a finished dispatch
# would have routed a healthy round through the abandon path.
WORKERS_UNSEEN = (
    "These records say nothing about whether the workers are alive: a pass can be "
    + "closed while its worker still runs. Before concluding anything, check "
    + "impl_status_<slot>, recent board posts, and whether the launcher has exited."
)


class ScopeClock(TypedDict):
    """One summary row's numbers: the percentage and the clocks beside it."""

    percent: int
    elapsed: int
    unchanged: int


class RecordedReport(TypedDict):
    """The summary table's numbers as the phase's last `progress` call left them."""

    at: float
    # What `at` is, in the words the as-of line prints.
    source: str
    project: ScopeClock
    phase: ScopeClock
    calibration: dict[str, object] | None


@dataclass(frozen=True)
class ReportedPassWindow:
    """A progress report that measures a live launcher pass."""

    record: dict[str, object]


@dataclass(frozen=True)
class ReportedActivityWindow:
    """A progress report that describes a live unit-director activity."""

    record: dict[str, object]


@dataclass(frozen=True)
class NoReportedWindow:
    """A progress report made between live windows."""


def _state_plan_phase_counts(state: dict[str, object]) -> dict[str, object]:
    """The run's plan counted by phase heading, unavailable without an absolute path."""
    plan_doc = _string(state.get("project_plan_doc")) or _string(state.get("plan_doc"))
    if plan_doc:
        candidate = Path(plan_doc).expanduser()
        if candidate.is_absolute():
            return _with_unlisted_phase(_count_plan_phases(candidate), state, candidate.resolve())
    return {"available": False, "reason": "no plan doc"}


def _run_plan_path(state: dict[str, object]) -> Path | None:
    """The plan this run's phase ids come from, which the project plan may not be."""
    plan_doc = _string(state.get("plan_doc"))
    if not plan_doc:
        return None
    return plan_history.resolve_plan_path(Path(_string(state.get("working_dir")) or str(Path.cwd())), plan_doc)


def _with_unlisted_phase(counts: dict[str, object], state: dict[str, object], counted: Path) -> dict[str, object]:
    """The counts with the run's phase added when the plan has no heading for it.

    Start-phase refuses such a phase, so this only covers a phase opened before that
    rule. Without it a report reads "19 of 31 done" beside "Phase 32".
    """
    phase = _object_dict(state.get("phase"))
    phase_id = _string(phase.get("id")) if phase is not None else ""
    order = counts.get("order")
    if (phase is None or phase_id in ("", "ad hoc") or not isinstance(order, list)
            or _run_plan_path(state) != counted or _plan_lists_phase(counted, phase_id)):
        return counts
    finished = _string(phase.get("status")) == "completed"
    done, todo = _integer(counts.get("done")), _integer(counts.get("todo"))
    return {
        **counts,
        "done": done + int(finished),
        "todo": todo + int(not finished),
        "total": done + todo + 1,
        "order": [*order, phase_id],
    }


def _plan_lists_phase(plan: Path, phase_id: str) -> bool:
    """Whether a heading names `phase_id` by its number or, as a standing plan writes
    `### Phase 201 — types-unit-201`, by the first word of its title."""
    try:
        phases, _ = plan_history.plan_phase_details(plan)
    except OSError:
        return False
    return any(phase_id in (phase["id"], *phase["title"].split()[:1]) for phase in phases)


def _phase_missing_from_plan(state: dict[str, object], phase_id: str) -> Path | None:
    """The run's plan when it lists phases and none of them is `phase_id`."""
    plan = _run_plan_path(state)
    if plan is None or phase_id == "ad hoc":
        return None
    listed = _count_plan_phases(plan).get("available") is not True or _plan_lists_phase(plan, phase_id)
    return None if listed else plan


def _reported_window(
    state: dict[str, object],
) -> ReportedPassWindow | ReportedActivityWindow | NoReportedWindow:
    """The live window a report describes, or the state between windows.

    A launcher's pass when one is open, and otherwise the unit director's
    activity. Both render the same line under the round table; only a pass
    carries convergence meaning. Which pass, when a phase team has two of them
    open, is `_reporting_pass`; the round table is where the other is visible.
    """
    current_pass = _reporting_pass(state)
    if current_pass is not None:
        return ReportedPassWindow(current_pass)
    activity = _object_dict(state.get("activity"))
    if activity is not None and _string(activity.get("status")) == "active":
        return ReportedActivityWindow(activity)
    return NoReportedWindow()


def _scope_row(label: str, clock: ScopeClock, as_of: float, now: float, floor: int = 0) -> list[str]:
    return [
        label,
        str(clock["percent"]),
        _format_duration(clock["elapsed"]),
        _eta_cell(clock["percent"], clock["elapsed"], as_of, now, floor),
        _unchanged_cell(clock["unchanged"]),
    ]


def _summary_table(
    phase_id: str,
    project: ScopeClock,
    phase: ScopeClock,
    plan_phase_counts: dict[str, object],
    phase_spread: float,
    as_of: float,
    now: float,
    state: dict[str, object],
) -> list[str]:
    """The clocks table: a project row and a phase row.

    Percentages and elapsed clocks are measured at `as_of`. The project ETA
    follows the current plan chain; every arrival is named from `now`'s day.
    """
    headers = list(SUMMARY_HEADERS)
    project_row = [
        "Project", str(project["percent"]), _format_duration(project["elapsed"]),
        "—", _unchanged_cell(project["unchanged"]),
    ]
    band = plan_history.eta_band_seconds(phase["percent"], phase["elapsed"], phase_spread)
    phase_swing = (None if isinstance(band, plan_history.EtaProjectionUnavailable)
                   else (band.remaining - band.earliest, band.latest - band.remaining))
    project_eta, project_low, project_high = _project_chain_eta(state, now, phase_swing)
    project_row[3] = project_eta
    phase_row = _scope_row(f"Phase {phase_id}", phase, as_of, now)
    if plan_phase_counts.get("available") is True:
        headers.append("Phases")
        done = _integer(plan_phase_counts.get("done"))
        total = _integer(plan_phase_counts.get("total"))
        project_row.append(f"{done} of {total} done")
        phase_row.append("")
    headers.extend(("ETA low", "ETA high"))
    project_row.extend((project_low, project_high))
    phase_row.extend(
        _eta_band_cells(phase["percent"], phase["elapsed"], as_of, now, phase_spread)
    )
    return _render_table(
        headers,
        [project_row, phase_row],
        {index for index, header in enumerate(headers) if header in RIGHT_ALIGNED_SUMMARY_COLUMNS},
    )


def _project_chain_eta(
    state: dict[str, object], now: float, phase_swing: tuple[int, int] | None,
) -> tuple[str, str, str]:
    """Render the same plan finish used by the phase table and vault note.

    The low and high shift the finish by the open phase's swing: its projected
    band, else the range stated with its ETA, else `phase_swing`, the band of
    the report's Phase row. So a stated ETA with no range still gets the
    Phase row's band (user, 2026-10-26). A finished plan's low and high are
    its recorded finish.
    """
    from scripts.direct import phase_table

    plan_doc = _string(state.get("project_plan_doc")) or _string(state.get("plan_doc"))
    if not plan_doc:
        return "—", "—", "—"
    working_dir = Path(_string(state.get("working_dir")) or str(Path.cwd()))
    try:
        record = phase_table.build_plan(plan_history.resolve_plan_path(working_dir, plan_doc))
    except phase_table.NoPlan:
        return "—", "—", "—"
    finish = record.plan_finish
    if isinstance(finish, phase_table.GateBlockedFinish):
        return f"after {finish.gate}", "—", "—"
    if not isinstance(finish, phase_table.FinishedAt | phase_table.PredictedFinish):
        return "—", "—", "—"
    target = finish.at.timestamp()
    eta_cell = _arrival_label(target, now)
    current = record.current
    eta = current.eta if isinstance(current, phase_table.OpenPhase) else None
    if isinstance(finish, phase_table.FinishedAt):
        low, high = 0.0, 0.0
    elif isinstance(eta, phase_table.ProjectedEta):
        low = (eta.time - eta.earliest).total_seconds()
        high = (eta.latest - eta.time).total_seconds()
    elif isinstance(eta, phase_table.StatedEta) and isinstance(eta.range, phase_table.EtaRange):
        low = (eta.time - eta.range.earliest).total_seconds()
        high = (eta.range.latest - eta.time).total_seconds()
    elif phase_swing is not None:
        low, high = float(phase_swing[0]), float(phase_swing[1])
    else:
        return eta_cell, "—", "—"
    return (
        eta_cell,
        f"{_arrival_label(target - low, now)} (-{_format_offset(int(low))})",
        f"{_arrival_label(target + high, now)} (+{_format_offset(int(high))})",
    )


def _phase_eta_line(session_dir: Path, phase_number: str) -> str:
    """Describe the phase's promises, never its advanced schedule end."""
    from scripts.direct import phase_table

    try:
        record = phase_table.build(session_dir)
    except (phase_table.NoPlan, phase_table.NoState):
        return "ETA unavailable"
    now = plan_history.now_epoch()
    zone = user_zone()

    def promise_time(target: datetime) -> str:
        local = target.astimezone(zone)
        return local.strftime("%H:%M" if local.date() == datetime.fromtimestamp(now, zone).date()
                              else "%a %d %b %H:%M")

    candidates = [*record.phases]
    if isinstance(record.current, phase_table.OpenPhase):
        candidates = [record.current, *candidates]
    for row in candidates:
        if row.phase != phase_number:
            continue
        if isinstance(row, phase_table.TodoPhase):
            gate = row.gate
            if isinstance(gate, phase_table.TimedGate) and gate.until.timestamp() > now:
                return f"gated until {gate.until.astimezone(zone):%a %d %b %H:%M}"
            if isinstance(gate, phase_table.UntimedGate):
                return f"gated by {gate.gate}: {gate.waits_on}"
        if not row.eta_trail:
            return "ETA unavailable"
        target = row.eta_trail[-1][0]
        first = row.eta_trail[0][0]
        details: list[str] = []
        if target.timestamp() < now and not isinstance(row, phase_table.DonePhase):
            details.append("passed")
        if row.moves:
            minutes = int((target - first).total_seconds() / 60)
            hours, remainder = divmod(abs(minutes), 60)
            details.extend((
                f"first {promise_time(first)}",
                f"{'+' if minutes >= 0 else '-'}{hours}:{remainder:02d}",
                f"moved {row.moves}\u00d7",
            ))
        else:
            details.append("on its first ETA")
        if row.fix_done or row.fix_running:
            fixes = (
                f"{row.fix_done} fix round{'s' if row.fix_done != 1 else ''}"
                if row.fix_done else "1 fix round running"
            )
            if row.fix_running and row.fix_done:
                fixes += " + 1 running"
            details.append(fixes)
        return f"ETA {promise_time(target)} ({', '.join(details)})"
    return "ETA unavailable"


def _phase_section(
    session_dir: Path,
    state: dict[str, object],
    phase: dict[str, object],
    events: list[dict[str, object]],
    now: float,
    tables: bool = True,
    include_heading: bool = True,
) -> tuple[list[str], list[StageWindow], list[str]]:
    """The phase heading, its round table and the notes under it.

    Opens with a blank line and closes every block with one. Also returns the
    windows and labels the table was built from, which the caller's closing
    line reads.
    """
    phase_instance_id = _string(phase.get("instance_id"))
    stage_windows, stage_labels, _ = _stage_table(events, state, phase_instance_id, now)
    # One row per round, one column per worker. A round is what advances and
    # what the ledger stamps on its findings, so it is the row that can carry
    # a Start, an Elapsed, and a Result that all mean something; the
    # workers working it read across. Per-pass provenance -- which model ran
    # where, and a review overlapping the writer as its own row -- is the
    # `timeline` view, which still renders one row per window, and so is the
    # history behind the last few stages this table keeps.
    phase_findings = [
        event
        for event in events
        if _string(event.get("phase_instance_id")) == phase_instance_id
        and _string(event.get("event_type")).startswith("finding_")
    ]
    board_activity = _board_activity(session_dir)
    role_changes = _board_role_changes(session_dir)
    round_table = _round_table(
        stage_windows,
        stage_labels,
        phase_findings,
        role_changes,
        board_activity,
        now,
    )
    slots = round_table["slots"]
    # A phase that has opened no window yet still draws its table: one row
    # saying so, never a header with nothing under it.
    rows = round_table["rows"] or [["-", "-", "-", *(["-"] * len(slots)), "no stage yet"]]
    worker_note = _worker_note(
        stage_windows, board_activity, now, slots,
        _current_worker_cells(stage_windows, stage_labels, role_changes, board_activity, slots, now)
        if not tables else (),
    )
    title = _string(phase.get("title"), "Ad hoc work")
    lines = ([
        "",
        f"**Phase {_string(phase.get('id'), 'ad hoc')}: {title}**",
        "",
        _phase_eta_line(session_dir, _string(phase.get("id"))),
        "",
    ] if include_heading else []) + [
        *([round_table["earlier"], ""] if tables and round_table["earlier"] else []),
        *([*_render_table(_round_headers(slots), rows, set()), ""] if tables else []),
        *([*round_table["gates"], ""] if round_table["gates"] else []),
        *([*worker_note, ""] if worker_note else []),
    ]
    return lines, stage_windows, stage_labels


def _reported_label(
    windows: list[StageWindow],
    labels: list[str],
    current_pass: dict[str, object],
) -> str:
    """The round-table label of the window this report describes.

    The sentence beneath the table is what a fixed-width Result column cannot
    carry, and it belongs to the window this report is about. That is not
    simply the last row: an early-launched reviewer runs beside the writer and
    starts later, so position would name the reviewer and describe it with the
    writer's activity.
    """
    reported_id = _string(current_pass.get("instance_id"))
    return next(
        (
            labels[index]
            for index, window in enumerate(windows)
            if window["instance_id"] == reported_id
        ),
        labels[-1] if labels else _pass_display(current_pass),
    )


def _recorded_report(
    state: dict[str, object],
    phase: dict[str, object],
    events: list[dict[str, object]],
    plan_phase_counts: dict[str, object],
    now: float,
) -> RecordedReport:
    """The summary numbers the phase's last `progress` call recorded.

    Read from that call's event, the one record carrying the moment it was made
    and the calibration it used. A phase not reported yet reads as of its own
    start: no phase progress, and the project where the plan's phase count puts
    it, held below 100 because a phase is still active.
    """
    instance_id = _string(phase.get("instance_id"))
    last = next(
        (
            event
            for event in reversed(events)
            if _string(event.get("event_type")) == "progress_reported"
            and _string(event.get("phase_instance_id")) == instance_id
        ),
        None,
    )
    fields: dict[str, object] = last if last is not None else {}
    at = _number(fields.get("timestamp_epoch"), _number(phase.get("started_at"), now))
    phase_percent = _integer(fields.get("phase_percent"), _integer(fields.get("percent")))
    derived = _plan_derived_project_percent(plan_phase_counts, phase_percent)
    project_fallback = (
        _integer(state.get("project_last_percent"))
        if derived is None
        else min(derived, PROJECT_CAP_BEFORE_COMPLETE)
    )
    return RecordedReport(
        at=at,
        source=(
            "the last progress report"
            if last is not None
            else "the phase start, before any progress report"
        ),
        project=ScopeClock(
            percent=_integer(fields.get("project_percent"), project_fallback),
            elapsed=max(0, int(at - _number(state.get("project_started_at"), at))),
            unchanged=_integer(fields.get("project_same_percent_elapsed_seconds")),
        ),
        phase=ScopeClock(
            percent=phase_percent,
            elapsed=_integer(
                fields.get("phase_elapsed_seconds"), max(0, int(at - _number(phase.get("started_at"), at)))
            ),
            unchanged=_integer(fields.get("phase_same_percent_elapsed_seconds")),
        ),
        calibration=_object_dict(fields.get("phase_calibration")),
    )


def _print_last_recorded(
    session_dir: Path,
    state: dict[str, object],
    phase: dict[str, object],
    now: float,
    reported_window: ReportedActivityWindow | NoReportedWindow,
    tables: bool = False,
) -> None:
    """Report the last recorded clocks without assessing progress again.

    Between windows -- the reviews closed, the repair writers not launched yet
    -- there is nothing live to measure. The same is true of an activity opened
    after its phase closed: it belongs in the round table, but does not revise
    the closed phase's assessment. The summary table keeps the last recorded
    clocks. With --tables, the round table shows the phase's current windows.
    It writes no event, window or state, so pass counts, convergence and
    calibration never see it.
    """
    events = _run_events(state)
    plan_phase_counts = _state_plan_phase_counts(state)
    recorded = _recorded_report(state, phase, events, plan_phase_counts, now)
    section, stage_windows, stage_labels = _phase_section(
        session_dir, state, phase, events, now, tables=tables
    )
    if isinstance(reported_window, ReportedActivityWindow):
        activity = reported_window.record
        reported_label = _reported_label(stage_windows, stage_labels, activity)
        window_line = f"▸ **{reported_label} - {_string(activity.get('activity'))}**"
    else:
        window_line = f"No pass or activity is open. {WORKERS_UNSEEN}"
    lines = [
        _scope_line(state),
        "",
        f"*Percentages and clocks as of {_moment_stamp(recorded['at'], now)}, "
        + f"{recorded['source']}.*",
        "",
        *_summary_table(
            _string(phase.get("id"), "ad hoc"),
            recorded["project"],
            recorded["phase"],
            plan_phase_counts,
            plan_history.percent_spread(recorded["calibration"]),
            recorded["at"],
            now,
            state,
        ),
        *section,
        window_line,
        _clock_line(now),
    ]
    print("\n".join(lines))


def _report_progress(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    tables = getattr(args, "tables", False) is True
    now = plan_history.now_epoch()
    state = _ensure_project_timing(session_dir, _read_state(session_dir), now)
    phase = _object_dict(state.get("phase"))
    reported_window = _reported_window(state)
    if phase is None:
        raise SystemExit(f"No active phase to report: phase missing. {WORKERS_UNSEEN}")
    if _string(phase.get("status")) != "active":
        if isinstance(reported_window, ReportedActivityWindow):
            _print_last_recorded(
                session_dir, state, phase, now, reported_window, tables=tables
            )
            return
        phase_status = _string(phase.get("status")) or "missing"
        raise SystemExit(f"No active phase to report: phase {phase_status}. {WORKERS_UNSEEN}")
    if isinstance(reported_window, NoReportedWindow):
        _print_last_recorded(
            session_dir, state, phase, now, reported_window, tables=tables
        )
        return
    current_pass = reported_window.record
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
    cap_stage = _arg_string(args, "cap_stage")
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
        cap_stage,
    )
    # The project clock is derived, never estimated. An agent eyeballing phase
    # headings misses the archived ones; counting them here removes the judgment
    # call entirely, and a supplied --project-percent becomes advisory.
    plan_phase_counts = _state_plan_phase_counts(state)
    project_percent_source = "supplied"
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
            cap_stage,
        )
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

    phase_elapsed = _work_seconds(
        _run_events(state), _string(phase.get("instance_id")), _number(phase.get("started_at"), now), now
    )
    pass_elapsed = max(0, int(now - _number(current_pass.get("started_at"), now)))
    total_elapsed = max(0, int(now - _number(state.get("project_started_at"), now)))
    event = _event(state, "progress_reported", now)
    event.update(
        {
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
    # The reported window is the object state already holds, so the activity
    # edit above has landed either way; a pass is written back through its slot,
    # never over the map that holds every slot's.
    if isinstance(reported_window, ReportedActivityWindow):
        state["activity"] = current_pass
    _write_state(session_dir, state)

    phase_id = _string(phase.get("id"), "ad hoc")
    section, stage_windows, stage_labels = _phase_section(
        session_dir, state, phase, _run_events(state), now, tables=tables,
        include_heading=uses_dual_layout,
    )
    reported_label = _reported_label(stage_windows, stage_labels, current_pass)
    display_project_percent = project_percent if uses_dual_layout else (
        derived_project_percent if derived_project_percent is not None else phase_percent
    )
    summary = _summary_table(
        phase_id,
        ScopeClock(
            percent=display_project_percent,
            elapsed=total_elapsed,
            unchanged=project_unchanged_seconds,
        ),
        ScopeClock(
            percent=phase_percent,
            elapsed=phase_elapsed,
            unchanged=phase_unchanged_seconds,
        ),
        plan_phase_counts,
        plan_history.percent_spread(phase_calibration),
        now,
        now,
        state,
    ) if uses_dual_layout or tables else []
    if uses_dual_layout:
        lines = [
            _scope_line(state),
            "",
            *summary,
            *section,
            f"▸ **{reported_label} - {_string(current_pass.get('activity'))}**",
        ]
    else:
        legacy_progress_line = f"**{phase_percent}% complete"
        if phase_unchanged_seconds > 0:
            legacy_progress_line += f" - unchanged for {_format_duration(phase_unchanged_seconds)}"
        lines = [
            f"**{_string(state.get('worktree'))} - {_string(state.get('branch'))}**",
            f"**Phase {phase_id}: {_string(phase.get('title'), 'Ad hoc work')} "
            + f"- elapsed {_format_duration(phase_elapsed)}**",
            _phase_eta_line(session_dir, phase_id),
            f"**{_pass_display(current_pass)} - {_string(current_pass.get('activity'))} "
            + f"- elapsed {_format_duration(pass_elapsed)}**",
            legacy_progress_line + "**",
            f"**Total elapsed {_format_duration(total_elapsed)}**",
            *summary,
            *(section if tables else []),
        ]
    lines.append(_clock_line(now))
    print("\n".join(lines))


def _progress(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    _report_progress(args)
    _refresh_phase_table(session_dir)


def _eta(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase = _object_dict(state.get("phase"))
    phase_id = _optional_arg(args, "phase_id").strip()
    if not phase_id:
        if phase is None or _string(phase.get("status")) != "active":
            _refuse_eta("An active phase is required to record an ETA")
        phase_id = _string(phase.get("id"))
    basis = _arg_string(args, "basis").strip()
    if not basis:
        _refuse_eta("--basis must not be blank")

    now = plan_history.now_epoch()
    eta_at = _local_eta_epoch(_arg_string(args, "time"), "--time")
    if eta_at < now:
        _refuse_eta("--time must not be in the past")

    earliest_text = _arg_string(args, "earliest")
    latest_text = _arg_string(args, "latest")
    if bool(earliest_text) != bool(latest_text):
        _refuse_eta("--earliest and --latest must be supplied together")

    event = _event(state, "eta_stated", now)
    event.update({"eta_at": eta_at, "basis": basis, "eta_phase_id": phase_id})
    if earliest_text and latest_text:
        earliest_at = _local_eta_epoch(earliest_text, "--earliest")
        latest_at = _local_eta_epoch(latest_text, "--latest")
        if earliest_at > eta_at:
            _refuse_eta("--earliest must not be after --time")
        if latest_at < eta_at:
            _refuse_eta("--latest must not be before --time")
        event.update(
            {
                "eta_earliest_at": earliest_at,
                "eta_latest_at": latest_at,
            }
        )

    _append_event(state, event)
    _refresh_phase_table(session_dir)
    local_eta = datetime.fromtimestamp(eta_at, user_zone())
    print(f"ETA recorded: {local_eta:%H:%M %Z}")


def _waits_by_id(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    waits: dict[str, dict[str, object]] = {}
    for event in events:
        if event.get("event_type") in ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded"):
            wait_id = _string(event.get("wait_id"))
            if wait_id:
                waits[wait_id] = event
    return waits


@dataclass(frozen=True)
class WaitPhaseResolved:
    phase: dict[str, object]


@dataclass(frozen=True)
class WaitPhaseNotRecorded:
    reason: str


@dataclass(frozen=True)
class ResolvedWorkerPass:
    phase: dict[str, object]
    pass_instance_id: str
    worker_name: str
    team_slot: str


@dataclass(frozen=True)
class UnresolvedWaitAttribution:
    reason: str
    candidates: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ResolvedCheckpointPhase:
    identity: dict[str, object]


@dataclass(frozen=True)
class ProductionHandoff:
    production: str
    checkpoint_commit: str


@dataclass(frozen=True)
class NamedSupplier:
    address: str


@dataclass(frozen=True)
class NoNamedSupplier:
    reason: str


@dataclass(frozen=True)
class ProductionUnitIdentity:
    production: str
    unit: str


@dataclass(frozen=True)
class ProductionNotRecorded:
    reason: str


def resolve_production_unit(state: Mapping[str, object]) -> ProductionUnitIdentity | ProductionNotRecorded:
    """Read metadata only; never recover worker moves or take a run lock."""
    production, unit = _string(state.get("production")), _string(state.get("unit"))
    if not production or not unit:
        worktree = Path(_string(state.get("working_dir")) or ".").expanduser()
        plan = _string(state.get("project_plan_doc")) or _string(state.get("plan_doc"))
        try:
            content = (worktree / plan).read_text(encoding="utf-8") if plan else ""
        except OSError:
            content = ""
        match = re.search(r'^> \*\*Production:(?: (.+?)\*\*|\*\* (.+?)) — unit `([^`]+)`;', content, re.MULTILINE)
        if match:
            production, unit = match.group(1) or match.group(2), match.group(3)
    if not production or not unit:
        return ProductionNotRecorded("run and plan have no production and unit identity")
    return ProductionUnitIdentity(production, unit)


def _phase_identity(state: dict[str, object], phase: dict[str, object]) -> dict[str, object]:
    return {"run_id": _string(state.get("run_id")), "phase_id": _string(phase.get("id")),
            "phase_instance_id": _string(phase.get("instance_id"))}


def resolve_worker_pass(session_dir: Path, state: dict[str, object],
                        thread_id: str) -> ResolvedWorkerPass | UnresolvedWaitAttribution:
    """Use the thread's full roster name, never a server's inherited slot."""
    phases = [*_parked_phases(state), _object_dict(state.get("phase")) or {}]
    candidates: list[dict[str, object]] = []
    for record in _open_passes(state).values():
        candidates.append({"run_id": _string(state.get("run_id")),
                           "phase_instance_id": _pass_phase_instance(record),
                           "phase_id": _string((_object_dict(record.get("phase")) or {}).get("id")),
                           "pass_instance_id": _string(record.get("instance_id")),
                           "worker_name": _string(record.get("worker_name")),
                           "team_slot": _string(record.get("team_slot"))})
    if not candidates:
        candidates = [_phase_identity(state, phase) for phase in phases if phase.get("status") == "active"]
    try:
        roster = _json_object((session_dir / "mesh_roster.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return UnresolvedWaitAttribution(f"worker roster unavailable: {error}", tuple(candidates))
    names = [name for name, value in (roster or {}).items()
             if thread_id and (_object_dict(value) or {}).get("thread_id") == thread_id]
    if len(names) != 1:
        return UnresolvedWaitAttribution(f"worker thread has {len(names)} roster matches", tuple(candidates))
    matching = [candidate for candidate in candidates if candidate.get("worker_name") == names[0]]
    if len(matching) != 1:
        return UnresolvedWaitAttribution(f"worker {names[0]} has {len(matching)} active pass matches",
                                         tuple(matching or candidates))
    candidate = matching[0]
    no_phase: dict[str, object] = {}
    phase = next((phase for phase in phases if phase.get("instance_id") == candidate["phase_instance_id"]
                  and phase.get("status") == "active"), no_phase)
    if not phase or not candidate["pass_instance_id"]:
        return UnresolvedWaitAttribution("worker pass has no active phase or pass id", (candidate,))
    return ResolvedWorkerPass(phase, _string(candidate["pass_instance_id"]), names[0],
                              _string(candidate["team_slot"]))


def _wait_phase(state: dict[str, object], args: argparse.Namespace) -> (
    WaitPhaseResolved | WaitPhaseNotRecorded | ResolvedWorkerPass | UnresolvedWaitAttribution
):
    """Resolve the caller's pass now, or an explicitly named historical instance."""
    slot = _optional_arg(args, "slot")
    instance_id = _optional_arg(args, "phase_instance_id")
    phase_id = _optional_arg(args, "phase_id")
    if hasattr(args, "worker_thread_id"):
        return resolve_worker_pass(_session_dir(args), state, _optional_arg(args, "worker_thread_id"))
    if hasattr(args, "director") and cast(bool, args.director):
        phases = [phase for phase in [*_parked_phases(state), _object_dict(state.get("phase")) or {}]
                  if phase.get("status") == "active"]
        if len(phases) == 1:
            return WaitPhaseResolved(phases[0])
        return UnresolvedWaitAttribution(f"director has {len(phases)} active phase candidates",
                                         tuple(_phase_identity(state, phase) for phase in phases))
    if hasattr(args, "slot"):
        current_pass = _open_passes(state).get(slot)
        if current_pass is None:
            return WaitPhaseNotRecorded(f"slot {slot} has no open pass")
        instance_id = _pass_phase_instance(current_pass)
        if not instance_id:
            return WaitPhaseNotRecorded(f"slot {slot} open pass has no phase instance")
        # An open pass can only belong to an open phase. Never substitute the
        # current phase when a reused server carries an older environment.
        candidates = [*_parked_phases(state), _object_dict(state.get("phase")) or {}]
        for phase in candidates:
            if phase.get("instance_id") == instance_id and phase.get("status") == "active":
                return WaitPhaseResolved(phase)
        return WaitPhaseNotRecorded(f"slot {slot} open pass names no open phase")
    if hasattr(args, "phase_instance_id"):
        if not instance_id:
            return WaitPhaseNotRecorded("phase instance selector is blank")
        for event in reversed(_run_events(state)):
            if event.get("event_type") == "phase_started" and event.get("phase_instance_id") == instance_id:
                return WaitPhaseResolved({"instance_id": instance_id, "id": event.get("phase_id"),
                                          "title": event.get("phase_title"),
                                          "started_at": event.get("timestamp_epoch")})
        return WaitPhaseNotRecorded(f"phase instance {instance_id} has no start record in this run")
    if _optional_arg(args, "missing_reason") and not hasattr(args, "phase_id"):
        return WaitPhaseNotRecorded("missing wait has no phase selector")
    phase = _open_phase_named(state, phase_id) if hasattr(args, "phase_id") else _object_dict(state.get("phase"))
    if phase is not None and phase.get("status") == "active" and state.get("status") == "active":
        return WaitPhaseResolved(phase)
    return WaitPhaseNotRecorded(f"phase {phase_id or '(current)'} is not open")


def _start_wait(args: argparse.Namespace) -> None:
    if cast(bool, args.probe) or os.environ.get(PROBE_ENV) == "1":
        return
    state = _read_state(_session_dir(args))
    print(_record_wait(state, args))


def _record_wait(state: dict[str, object], args: argparse.Namespace) -> str:
    resolution = _wait_phase(state, args)
    owner = _arg_string(args, "owner_session") or _string((_object_dict(state.get("main_agent")) or {}).get("session_id"))
    missing_reason = _optional_arg(args, "missing_reason")
    if isinstance(resolution, (WaitPhaseNotRecorded, UnresolvedWaitAttribution)):
        missing_reason = "; ".join(part for part in (missing_reason, resolution.reason) if part)
    if not owner:
        missing_reason = "; ".join(part for part in (missing_reason, "wait owner session not recorded") if part)
    now = plan_history.now_epoch()
    # An empty explicit phase prevents _event from borrowing a reporting pass
    # or the current phase when attribution failed.
    event = _event(state, "wait_not_recorded" if missing_reason else "wait_started", now,
                   phase_record=resolution.phase if isinstance(resolution, (WaitPhaseResolved, ResolvedWorkerPass)) else {})
    wait_id = str(uuid.uuid4())
    event.update({"wait_id": wait_id, "wait_kind": _arg_string(args, "kind"),
                  "wait_reason": _arg_string(args, "reason"),
                  "unit": _arg_string(args, "unit") or _string(state.get("worktree")),
                  "owner_session": owner, "wait_started_at": _iso_time(now),
                  "wait_started_epoch": now, "wait_end": {"state": "Open"}})
    if hasattr(args, "expected_supplier"):
        event["expected_supplier"] = cast(object, args.expected_supplier)
    if isinstance(resolution, ResolvedWorkerPass):
        event.update(pass_instance_id=resolution.pass_instance_id, worker_name=resolution.worker_name,
                     team_slot=resolution.team_slot, wait_attribution={"state": "WorkerPass"})
    elif isinstance(resolution, UnresolvedWaitAttribution):
        event["wait_attribution"] = {"state": "UnresolvedAttribution", "reason": resolution.reason,
                                     "candidates": list(resolution.candidates)}
    else:
        event["wait_attribution"] = {"state": "PhaseInstance" if isinstance(resolution, WaitPhaseResolved)
                                     else "UnresolvedAttribution", "candidates": [],
                                     "reason": resolution.reason if isinstance(resolution, WaitPhaseNotRecorded) else "explicit phase attribution"}
    supplier = _object_dict(event.get("expected_supplier")) or {}
    if event["wait_kind"] == "WaitingForSupplier" and supplier.get("state") == "NoNamedSupplier":
        event["wait_attribution"] = {"state": "UnresolvedSupplier", "reason": supplier.get("reason"),
                                     "candidates": [{key: event[key] for key in
                                                     ("run_id", "phase_id", "phase_instance_id") if key in event}]}
    if missing_reason:
        event.update(wait_record_state={"state": "NotRecorded", "reason": missing_reason},
                     wait_end={"state": "NotRecorded"},
                     requested_slot=_optional_arg(args, "slot"),
                     requested_phase_id=_optional_arg(args, "phase_id"),
                     requested_phase_instance_id=_optional_arg(args, "phase_instance_id"))
    _append_event(state, event)
    return wait_id


def _end_wait(state: dict[str, object], wait: dict[str, object], now: float, *, unclosed: bool) -> None:
    event = dict(wait)
    event.update({"event_id": str(uuid.uuid4()), "event_type": "wait_unclosed" if unclosed else "wait_finished",
                  "timestamp": _iso_time(now), "timestamp_epoch": now,
                  "wait_end": {"state": "Unclosed"} if unclosed else
                  {"state": "Finished", "at": _iso_time(now), "epoch": now}})
    _append_event(state, event)


def _finish_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    state = _read_state(_session_dir(args))
    wait_id = _arg_string(args, "wait_id")
    wait = _waits_by_id(_run_events(state)).get(wait_id)
    if wait is None:
        raise SystemExit(f"Wait {wait_id} was not recorded in this run")
    if wait.get("event_type") != "wait_started":
        return
    now = plan_history.now_epoch()
    if now < _number(wait.get("wait_started_epoch")):
        raise SystemExit("Cannot finish a wait before its start")
    _end_wait(state, wait, now, unclosed=False)


def _checkpoint(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    state = _read_state(_session_dir(args))
    commit = _arg_string(args, "commit")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise SystemExit("checkpoint requires a full commit hash")
    phase = _open_phase_named(state, _arg_string(args, "phase"))
    if phase is None:
        raise SystemExit("checkpoint requires the named open phase")
    events = _run_events(state)
    for event in events:
        if (event.get("event_type") == "checkpoint" and event.get("checkpoint_commit") == commit
                and event.get("phase_instance_id") == phase.get("instance_id")):
            return
    event = _event(state, "checkpoint", plan_history.now_epoch(), phase_record=phase)
    event.update(checkpoint_commit=commit, session_dir=str(_session_dir(args)))
    _append_event(state, event)


def resolve_checkpoint(commit: str, working_dir: Path, phase_id: str) -> (
    ResolvedCheckpointPhase | UnresolvedWaitAttribution
):
    """Match a full checkpoint, including finished instances, across durable runs."""
    events, ignored = _load_events()
    phases = [event for event in events if event.get("event_type") == "phase_started"
              and _string(event.get("working_dir"))
              and Path(_string(event.get("working_dir"))).resolve() == working_dir.resolve()
              and event.get("phase_id") == phase_id]
    matches = [event for event in events if event.get("event_type") == "checkpoint"
               and event.get("checkpoint_commit") == commit]
    valid = [event for event in matches if any(
        (event.get("run_id"), event.get("phase_instance_id"), event.get("working_dir")) ==
        (phase.get("run_id"), phase.get("phase_instance_id"), phase.get("working_dir")) for phase in phases)]
    if len(matches) == len(valid) == 1 and not ignored:
        return ResolvedCheckpointPhase(valid[0])
    candidates = list({(_string(event.get("run_id")), _string(event.get("phase_instance_id"))): event
                       for event in [*phases, *matches]}.values())
    identities = tuple({key: event[key] for key in
                        ("run_id", "phase_id", "phase_instance_id", "session_dir", "working_dir", "checkpoint_commit")
                        if key in event} for event in candidates)
    reason = (f"checkpoint {commit} has {len(matches)} records and {len(valid)} valid phase matches"
              + (f"; {ignored} unreadable history rows" if ignored else ""))
    return UnresolvedWaitAttribution(reason, identities)


def _external_wait_file() -> Path:
    return plan_history.history_root() / "wait_attribution.jsonl"


def _external_wait_events() -> list[dict[str, object]]:
    # This reader creates no file or parent directory.
    return _run_events({"history_file": str(_external_wait_file())})


@contextmanager
def _external_wait_lock() -> Generator[None, None, None]:
    path = _external_wait_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _start_external_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    with _external_wait_lock():
        _record_external_wait(args)


def _record_external_wait(args: argparse.Namespace) -> None:
    commit = _arg_string(args, "commit")
    attribution = (ProductionHandoff(_arg_string(args, "production"), commit)
                   if _arg_string(args, "command") == "start-handoff-wait" else
                   resolve_checkpoint(commit, Path(_arg_string(args, "working_dir")), _arg_string(args, "phase_id")))
    now = plan_history.now_epoch()
    owner = (_arg_string(args, "owner_session") or os.environ.get("CLAUDE_CODE_SESSION_ID")
             or os.environ.get("CODEX_THREAD_ID", ""))
    if not owner and isinstance(attribution, ResolvedCheckpointPhase):
        owner = _string((_object_dict(attribution.identity.get("main_agent")) or {}).get("session_id"))
    event: dict[str, object] = {"schema_version": SCHEMA_VERSION, "event_id": str(uuid.uuid4()),
        "event_type": "wait_started", "wait_id": str(uuid.uuid4()), "timestamp_epoch": now,
        "timestamp": _iso_time(now), "wait_started_at": _iso_time(now), "wait_started_epoch": now,
        "wait_kind": "WaitingForMergeGate", "wait_reason": _arg_string(args, "reason"),
        "owner_session": owner, "unit": _arg_string(args, "unit"),
        "checkpoint_commit": commit, "wait_end": {"state": "Open"}}
    if isinstance(attribution, ResolvedCheckpointPhase):
        event.update({key: attribution.identity[key] for key in
                      ("run_id", "phase_id", "phase_instance_id", "working_dir", "session_dir")})
        event["wait_attribution"] = {"state": "CheckpointPhase", "reason": "one checkpoint phase match",
                                     "candidates": [attribution.identity]}
    elif isinstance(attribution, ProductionHandoff):
        event["production"] = attribution.production
        event["wait_attribution"] = {"state": "ProductionHandoff", "reason": "child production checkpoint has no owning phase",
                                     "candidates": []}
    else:
        event.update(event_type="wait_not_recorded", wait_end={"state": "NotRecorded"},
                     wait_record_state={"state": "NotRecorded", "reason": attribution.reason},
                     wait_attribution={"state": "UnresolvedAttribution", "reason": attribution.reason,
                                       "candidates": list(attribution.candidates)})
    _append_event({"history_file": str(_external_wait_file())}, event)
    print(event["wait_id"])


def _finish_external_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    path = _external_wait_file()
    with _external_wait_lock():
        wait = _waits_by_id(_external_wait_events()).get(_arg_string(args, "wait_id"))
        if wait is None:
            raise SystemExit("External wait was not recorded")
        if wait.get("event_type") == "wait_started":
            now = plan_history.now_epoch()
            if now < _number(wait.get("wait_started_epoch")):
                raise SystemExit("Cannot finish a wait before its start")
            _end_wait({"history_file": str(path)}, wait, now, unclosed=False)


def _showrunner_supplier(state: dict[str, object]) -> NamedSupplier | NoNamedSupplier:
    identity = resolve_production_unit(state)
    if isinstance(identity, ProductionNotRecorded):
        return NoNamedSupplier(identity.reason)
    from scripts.production import showrunners

    try:
        address = showrunners.current_address(identity.production)
    except (OSError, ValueError) as error:
        return NoNamedSupplier(f"showrunner address unavailable: {error}")
    # Keep the production alias, so a restart resolves its new socket on arrival.
    return NamedSupplier("showrunner:" + identity.production) if address else NoNamedSupplier("production has no running showrunner address")


def _supplier_fields(supplier: NamedSupplier | NoNamedSupplier) -> dict[str, object]:
    if isinstance(supplier, NamedSupplier):
        return {"state": "NamedSupplier", "address": supplier.address}
    return {"state": "NoNamedSupplier", "reason": supplier.reason}


def _turn_supplier(state: dict[str, object], reason: str) -> NamedSupplier | NoNamedSupplier:
    if "waiting on the showrunner" in reason.lower():
        return _showrunner_supplier(state)
    match = re.search(r"blocked: waiting on ((?:claude-|codex-|worker:|session:|codex:|uds:)[^\s,;]+)", reason, re.IGNORECASE)
    return NamedSupplier(match[1]) if match else NoNamedSupplier("wait names no supplier address")


def _supplier_address(identity: str) -> NamedSupplier | NoNamedSupplier:
    """Resolve the supplier alias at arrival, including a restarted showrunner."""
    if identity.startswith("showrunner:"):
        from scripts.production import showrunners

        address = showrunners.current_address(identity.removeprefix("showrunner:"))
        return NamedSupplier(address) if address else NoNamedSupplier("showrunner has no current address")
    if identity.startswith(("uds:/", "codex:")):
        return NamedSupplier(identity)
    if identity.startswith("worker:"):
        run_id, separator, name = identity.partition(":")[2].partition("/")
        if not separator or run_id in ("", ".", "..") or "/" in name:
            return NoNamedSupplier("worker alias has no run and worker identity")
        threads: set[str] = set()
        for root in unit_runs.run_roots(os.environ.get("UNIT_RUN_ROOT")):
            try:
                roster = _json_object((root / run_id / "mesh_roster.json").read_text(encoding="utf-8")) or {}
                thread = _string((_object_dict(roster.get(name)) or {}).get("thread_id"))
                if thread:
                    threads.add(thread)
            except (OSError, ValueError):
                continue
        return NamedSupplier("codex:" + next(iter(threads))) if len(threads) == 1 else NoNamedSupplier("worker alias has no unique roster thread")
    from scripts.message import sessions

    records = sessions.live_sessions()
    matches = [record for record in records if identity in
               (record["name"], "session:" + record["sessionId"], *record["formerNames"])]
    # A duplicate name is not an identity, even if addressed() would pick one.
    if len(matches) != 1:
        return NoNamedSupplier(f"supplier alias has {len(matches)} live addresses")
    return NamedSupplier("uds:" + matches[0]["messagingSocketPath"])


@dataclass(frozen=True)
class VerifiedSupplierMessage:
    body: str


@dataclass(frozen=True)
class UnverifiedSupplierMessage:
    reason: str


def _supplier_message(identity: str, prompt: str) -> VerifiedSupplierMessage | UnverifiedSupplierMessage:
    """Trust wrapper `from`, never `from-name` or a claimed sender in text.

    send.py's separate relay inherits the supplied name, not its origin socket;
    that alone proves nothing. Postmaster forwarding is not yet verifiable:
    its replies log is written after delivery, so the recipient's hook can run
    before the evidence exists. Forwarded messages leave the wait open, with or
    without that log. Direct delivery resolves supplier aliases anew.
    """
    wrapper = re.fullmatch(r'\s*<cross-session-message\b([^>]*)>(.*)</cross-session-message>\s*', prompt, re.DOTALL)
    if wrapper is None:
        return UnverifiedSupplierMessage("message has no cross-session origin")
    pairs = cast(list[tuple[str, str]], re.findall(r'([\w-]+)="([^"]*)"', wrapper[1]))
    attributes = dict(pairs)
    if len(pairs) != len(attributes):
        return UnverifiedSupplierMessage("message has duplicate origin attributes")
    address = _supplier_address(identity)
    if isinstance(address, NoNamedSupplier):
        return UnverifiedSupplierMessage(address.reason)
    body = wrapper[2].strip()
    origin = attributes.get("from", "")
    if origin == address.address:
        return VerifiedSupplierMessage(body)
    return UnverifiedSupplierMessage("message origin is not the supplier current address")


def record_turn_wait(session_dir: Path, owner_session: str, final_message: str) -> bool:
    """Start a director's declared user/supplier wait once, before the stop."""
    if os.environ.get(PROBE_ENV) == "1":
        return False
    lines = final_message.strip().splitlines()
    if not lines:
        return False
    reason = lines[-1].strip().removeprefix("— ")
    line = reason.lower()
    if line.startswith(("decision:", "gate:")):
        kind = "WaitingForUser"
    elif line.startswith("blocked: waiting on ") and (
        line.startswith("blocked: waiting on the showrunner")
        or re.search(r"\bpeer\b", line) is not None
        or re.match(r"blocked: waiting on (?:claude-|codex-|worker:|session:|codex:|uds:)", line) is not None
    ):
        kind = "WaitingForSupplier"
    else:
        return False
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(session_dir)
        supplier = _turn_supplier(state, reason) if kind == "WaitingForSupplier" else _showrunner_supplier(state)
        supplier_fields = _supplier_fields(supplier)
        marker_path = session_dir / "turn_wait.json"
        try:
            marker = _json_object(marker_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            marker = {}
        previous = _waits_by_id(_run_events(state)).get(_string(marker.get("wait_id")))
        if previous is not None and previous.get("event_type") == "wait_started":
            if (previous.get("owner_session") == owner_session and previous.get("wait_kind") == kind
                    and previous.get("expected_supplier") == supplier_fields):
                return True
            _end_wait(state, previous, plan_history.now_epoch(), unclosed=True)
        args = argparse.Namespace(missing_reason="", kind=kind, reason=reason,
                                  owner_session=owner_session, unit="", expected_supplier=supplier_fields)
        wait_id = _record_wait(state, args)
        _ = marker_path.write_text(json.dumps({"wait_id": wait_id, "owner_session": owner_session}) + "\n",
                                   encoding="utf-8")
    return True


def finish_turn_wait(session_dir: Path, owner_session: str, *, source: str, prompt: str) -> None:
    """Finish only for typed input or a message from the recorded supplier."""
    if os.environ.get(PROBE_ENV) == "1" or not (session_dir / "turn_wait.json").exists():
        return
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        marker_path = session_dir / "turn_wait.json"
        try:
            marker = _json_object(marker_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return
        if marker.get("owner_session") != owner_session:
            return
        state = _read_state(session_dir)
        wait = _waits_by_id(_run_events(state)).get(_string(marker.get("wait_id")))
        if wait is None or wait.get("event_type") != "wait_started":
            return
        supplier = _object_dict(wait.get("expected_supplier")) or {}
        message = (_supplier_message(_string(supplier.get("address")), prompt)
                   if source == "PEER" and supplier.get("state") == "NamedSupplier"
                   else UnverifiedSupplierMessage("input is not a named supplier message"))
        from_supplier = isinstance(message, VerifiedSupplierMessage)
        user_wait = wait.get("wait_kind") == "WaitingForUser"
        body = message.body if isinstance(message, VerifiedSupplierMessage) else ""
        body = re.sub(r'^Message from [^\n]+ via postmaster: [^\n]* \[[a-fA-F0-9]{8}\]\r?\n\r?\n', "", body, count=1)
        relay = from_supplier and body.lstrip().startswith("From the user (via the showrunner):")
        if not ((user_wait and (source == "TYPED" or relay)) or (not user_wait and from_supplier)):
            return
        _finish_wait(argparse.Namespace(session_dir=str(session_dir), wait_id=_string(marker.get("wait_id"))))
        marker_path.unlink()


def write_merge_receipt(*, project: str, phase_id: str, merge_commit: str,
                        source_checkpoint: str, target_branch: str, merged_at: float,
                        issue: str = "") -> dict[str, object]:
    """Record a pushed merge once; callers must not call this for a rolled-back merge."""
    if not all((project, merge_commit, source_checkpoint, target_branch)) or not (phase_id or issue):
        raise ValueError("A merge receipt needs its project, phase or issue, both commits, and target branch")
    path = plan_history.history_root() / "merge_receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        for line in handle:
            receipt = _json_object(line)
            if receipt is not None and (receipt.get("project"), receipt.get("target_branch"), receipt.get("merge_commit")) == (project, target_branch, merge_commit):
                return receipt
        record: dict[str, object] = {"event_type": "merge_receipt", "schema_version": SCHEMA_VERSION,
                   "project": project, "issue": issue, "phase_id": phase_id,
                   "merge_commit": merge_commit, "source_checkpoint": source_checkpoint,
                   "target_branch": target_branch, "merged_at": _iso_time(merged_at),
                   "timestamp_epoch": merged_at}
        _ = handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
            os.fsync(handle.fileno())
    return record


def _history_window(args: argparse.Namespace) -> tuple[float, float]:
    bounds: list[float] = []
    for name, default in (("since", 0.0), ("until", plan_history.now_epoch())):
        text = _arg_string(args, name)
        try:
            parsed = datetime.fromisoformat(text) if text else None
        except ValueError as error:
            raise SystemExit(f"--{name} must be an ISO 8601 time") from error
        bounds.append(default if parsed is None else
                      (parsed if parsed.tzinfo else parsed.replace(tzinfo=user_zone())).timestamp())
    since, until = bounds
    if since >= until:
        raise SystemExit("--since must be before --until")
    return since, until


def _attribution_candidates(wait: dict[str, object]) -> list[dict[str, object]]:
    value = (_object_dict(wait.get("wait_attribution")) or {}).get("candidates")
    return [record for item in cast(list[object], value) if (record := _object_dict(item)) is not None] if isinstance(value, list) else []


def _read_waits(args: argparse.Namespace) -> None:
    since, until = _history_window(args)
    session = _arg_string(args, "session_dir")
    ignored = 0
    if session:
        events = _run_events(_read_state(_session_dir(args)))
        run_ids = {event.get("run_id") for event in events}
        events.extend(wait for wait in _external_wait_events() if wait.get("run_id") in run_ids or any(
            candidate.get("run_id") in run_ids for candidate in
            _attribution_candidates(wait)))
    else:
        events, ignored = _load_events()
        events.extend(_external_wait_events())
    records = list(_waits_by_id(events).values())
    selected = [wait for wait in records if since <= _number(wait.get("wait_started_epoch")) < until]
    missing = [wait for wait in selected if wait.get("event_type") == "wait_not_recorded"]
    waits = [wait for wait in selected if wait.get("event_type") != "wait_not_recorded"]
    uncertain = [wait for wait in selected if (_object_dict(wait.get("wait_attribution")) or {}).get("state")
                 in ("UnresolvedAttribution", "UnresolvedSupplier")]
    handoffs = [wait for wait in waits if (_object_dict(wait.get("wait_attribution")) or {}).get("state") == "ProductionHandoff"]
    phases: list[dict[str, object]] = []
    old_phase_n = 0
    for event in events:
        if event.get("event_type") != "phase_started" or not since <= _number(event.get("timestamp_epoch")) < until:
            continue
        own = [wait for wait in waits if (wait.get("run_id"), wait.get("phase_instance_id")) ==
               (event.get("run_id"), event.get("phase_instance_id"))]
        own_missing = [wait for wait in missing if (wait.get("run_id"), wait.get("phase_instance_id")) ==
                       (event.get("run_id"), event.get("phase_instance_id"))]
        recorded = (_object_dict(event.get("wait_coverage")) or {}).get("state") == "Recorded"
        own_uncertain = [wait for wait in uncertain if any(
            (candidate.get("run_id"), candidate.get("phase_instance_id")) ==
            (event.get("run_id"), event.get("phase_instance_id")) for candidate in _attribution_candidates(wait))]
        old_phase_n += not recorded
        phase: dict[str, object] = {"run_id": event.get("run_id"), "phase_id": event.get("phase_id"),
                                    "phase_instance_id": event.get("phase_instance_id"),
                                    "unit": event.get("worktree"),
                                    "state": "Covered" if recorded and not own_missing else "HistoryNotRecorded"}
        if own_missing:
            phase.update(reason="wait emission has a missing record", wait_missing_n=len(own_missing),
                         recorded_wait_n=len(own))
        elif recorded:
            phase["wait_n"] = len(own)
        else:
            phase["reason"] = "phase start has no wait coverage record"
        phase["candidates"] = []
        if recorded and not own_missing:
            phase["reason"] = "wait coverage recorded"
        if own_uncertain:
            _ = phase.pop("wait_n", None)
            phase.update(state="UncertainCoverage", recorded_wait_n=len(own),
                         reason="; ".join(_string((_object_dict(wait.get("wait_attribution")) or {}).get("reason"))
                                          for wait in own_uncertain),
                         candidates=[candidate for wait in own_uncertain for candidate in _attribution_candidates(wait)],
                         attribution_miss_ids=[wait["wait_id"] for wait in own_uncertain])
        phases.append(phase)
    print(json.dumps({"since": _iso_time(since), "until": _iso_time(until),
                      "selection": "phase starts and wait starts in [since, until)",
                      "covered_n": sum(phase["state"] == "Covered" for phase in phases),
                      "missing_n": old_phase_n + len(missing),
                      "missing_phase_n": sum(phase["state"] == "HistoryNotRecorded" for phase in phases),
                      "missing_record_n": len(missing),
                      "attribution_miss_n": len(uncertain),
                      "uncertain_phase_n": sum(phase["state"] == "UncertainCoverage" for phase in phases),
                      "production_handoff_n": len(handoffs), "handoffs": handoffs,
                      "ignored_n": ignored, "phases": phases, "waits": waits, "missing": missing}, sort_keys=True))


def _read_receipts(args: argparse.Namespace) -> None:
    since, until = _history_window(args)
    path = plan_history.history_root() / "merge_receipts.jsonl"
    receipts: list[dict[str, object]] = []
    problems: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line in handle:
                receipt = _json_object(line)
                if receipt is None:
                    problems.append("NotRecorded: invalid receipt record")
                elif receipt.get("event_type") == "merge_receipt":
                    receipts.append(receipt)
    except FileNotFoundError:
        pass
    except OSError as error:
        problems.append(f"NotRecorded: {error}")
    selected = [receipt for receipt in receipts if since <= _number(receipt.get("timestamp_epoch")) < until
                and all(not _arg_string(args, option) or receipt.get(option) == _arg_string(args, option)
                        for option in ("project", "target_branch", "merge_commit"))]
    expected = _arg_string(args, "merge_commit")
    missing: list[dict[str, str]] = ([{"state": "ReceiptMissing", "merge_commit": expected}]
                                    if expected and not selected else [])
    print(json.dumps({"since": _iso_time(since), "until": _iso_time(until),
                      "covered_n": len(selected), "missing_n": len(missing),
                      "receipts": selected, "missing": missing, "problems": problems}, sort_keys=True))


def _finish_phase(args: argparse.Namespace) -> None:
    # The run lock is already held. External starts use this same history lock,
    # through the cleanup scan and publication of phase_finished.
    with _external_wait_lock():
        _finish_phase_with_wait_lock(args)


def _finish_phase_with_wait_lock(args: argparse.Namespace) -> None:
    """Close one open phase: the one --phase-id names, else the current one.

    Only that phase's own passes close with it; another open phase keeps its
    workers. When the current phase closes, the most recently started older open
    phase becomes current again.
    """
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase_id = _optional_arg(args, "phase_id")
    current = _object_dict(state.get("phase"))
    current_open = current is not None and _string(current.get("status")) == "active"
    if phase_id:
        phase = _open_phase_named(state, phase_id)
        if phase is None:
            open_ids = ", ".join(recorder_state.open_phase_ids(state)) or "none"
            raise SystemExit(f"Phase {phase_id} is not open; open phases: {open_ids}")
    elif current is not None and current_open:
        phase = current
    else:
        return
    is_current = phase is current
    now = plan_history.now_epoch()
    finished_at = _optional_epoch(args, "finished_at", "--finished-at", now)
    started_at = _number(phase.get("started_at"), finished_at)
    if finished_at < started_at:
        raise SystemExit(
            f"--finished-at is before Phase {_string(phase.get('id'))} started "
            + f"({_iso_time(started_at)})"
        )
    instance_id = _string(phase.get("instance_id"))
    for wait in _waits_by_id(_run_events(state)).values():
        if wait.get("phase_instance_id") == instance_id and wait.get("event_type") == "wait_started":
            _end_wait(state, wait, now, unclosed=True)
    for wait in _waits_by_id(_external_wait_events()).values():
        if (wait.get("run_id") == state.get("run_id") and wait.get("phase_instance_id") == instance_id
                and wait.get("event_type") == "wait_started"):
            _end_wait({"history_file": str(_external_wait_file())}, wait, now, unclosed=True)
    # Every window this phase left open, one per team member: the launchers that
    # would have closed them are gone. A pass from before passes carried their
    # phase belongs to the current one.
    for slot, record in sorted(_open_passes(state).items()):
        owner = _pass_phase_instance(record)
        if owner == instance_id or (is_current and not owner):
            _ = _close_slot_pass(session_dir, state, slot, "interrupted", now)
    if is_current:
        _close_active_activity(session_dir, state, "interrupted", "", now)
        _ = _clear_armed_review(session_dir, state, "phase ended", now)
    status = _arg_string(args, "status")
    event = _event(state, "phase_finished", finished_at, phase_record=phase)
    event["contract_revision"] = _contract_revision()
    event["status"] = status
    event["phase_elapsed_seconds"] = max(0, int(finished_at - started_at))
    if finished_at != now:
        event["recorded_at"] = _iso_time(now)
        event["backfilled"] = True
    _append_event(state, event)
    phase["status"] = status
    phase["finished_at"] = finished_at
    if is_current:
        _unpark_newest(state)
    else:
        state[OPEN_PHASES_KEY] = [
            record for record in _parked_phases(state) if record is not phase
        ]
    _write_state(session_dir, state)
    # A completed phase's state that owns no reservation ends with it. Active and a pending
    # release stay: only the checkpoint and its confirmed release clear them.
    if status == "completed":
        phase_dir = phase_reservation.phase_directory(session_dir, _string(phase.get("id")))
        state_kind = _string(phase_reservation.read_state(phase_dir).get("kind"))
        if state_kind in phase_reservation.BOARD_STATES.values():
            _ = phase_reservation.clear(phase_dir)
    _refresh_phase_table(session_dir)


def _finish_run(args: argparse.Namespace) -> None:
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    now = plan_history.now_epoch()
    run_status = _arg_string(args, "status")
    # Every open phase, newest first: closing the current one makes the next
    # older one current.
    while recorder_state.open_phase_ids(state):
        finish_args = argparse.Namespace(session_dir=str(session_dir), status=run_status)
        _finish_phase(finish_args)
        state = _read_state(session_dir)
    if _string(state.get("status")) != "active":
        return
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = _read_state(session_dir)
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
    _refresh_phase_table(session_dir)


def _finding_lenses(event: dict[str, object]) -> set[str]:
    """The lenses a `finding_opened` event names, with `both` read as its pair."""
    lens = _string(event.get("lens"))
    if lens == LEGACY_BOTH_LENS:
        return set(LEGACY_BOTH_LENSES)
    return {part.strip() for part in lens.split(",") if part.strip()}


def _raised_code_findings(session_dir: Path, instance_id: str) -> int | None:
    """The code findings the phase's reviewers raised, from review.sh's turn records.

    Sums `raised` over the phase's finished (`reviewed`) code-review turns, as
    review_pool_report.py's `Raised` does. None when the phase has no such turn:
    a solo review writes no record.
    """
    raised: int | None = None
    root = plan_history.history_root() / "review_turns" / session_dir.name
    for path in sorted(root.glob("*.json")):
        try:
            record = _object_dict(cast(object, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
        if (
            record is None
            or _string(record.get("phase_instance_id")) != instance_id
            or _string(record.get("status")) != "reviewed"
            or _string(record.get("kind")) == "ux"
        ):
            continue
        raised = (raised or 0) + _integer(record.get("raised"))
    return raised


def _review_trial(args: argparse.Namespace) -> None:
    """Print one line: the phase's UX and code findings, and review time.

    It feeds the unit's checkpoint notice line, which the showrunner records with
    review_regime.py. The phase is the one --phase-id names -- an open one, else
    the latest this run started under that id -- or, without it, the one in
    state, active or just closed by finish-phase. Only events stamped with its
    instance count. The code number is what the reviewers raised, from the
    phase's review turn records; a phase with none counts the code findings
    the unit director opened. Review-worker
    minutes are every worker's finished review passes, whatever their status, plus
    the UX review activities. The screenshot check's own cost follows: its `UX
    review` activities alone, then every fix pass of a round whose dispatched
    batch covered a `ux` finding. All round to the nearest whole minute.
    """
    session_dir = _session_dir(args)
    state = _read_state(session_dir)
    phase_id = _optional_arg(args, "phase_id")
    if phase_id:
        phase = _open_phase_named(state, phase_id)
        if phase is None:
            phase = next(
                (
                    {"instance_id": _string(event.get("phase_instance_id"))}
                    for event in reversed(_run_events(state))
                    if _string(event.get("event_type")) == "phase_started"
                    and _string(event.get("phase_id")) == phase_id
                ),
                None,
            )
        if phase is None:
            raise SystemExit(f"This run never started Phase {phase_id}")
    else:
        phase = _object_dict(state.get("phase"))
    instance_id = _string(phase.get("instance_id")) if phase is not None else ""
    if not instance_id:
        raise SystemExit(
            f"No phase recorded in {_state_path(session_dir)}: review-trial reports "
            + "the phase start-phase opened, and this run has not opened one"
        )
    ux_findings = 0
    code_findings = 0
    review_seconds = 0
    ux_check_seconds = 0
    ux_finding_ids: set[str] = set()
    dispatched: list[dict[str, object]] = []
    fix_passes: list[dict[str, object]] = []
    for event in _run_events(state):
        if _string(event.get("phase_instance_id")) != instance_id:
            continue
        event_type = _string(event.get("event_type"))
        if event_type == "finding_opened":
            lenses = _finding_lenses(event)
            if "ux" in lenses:
                ux_findings += 1
                ux_finding_ids.add(_string(event.get("finding_id")))
            if not lenses or lenses - {"ux"}:
                code_findings += 1
        elif event_type == "finding_batch_dispatched":
            dispatched.append(event)
        elif event_type == "pass_finished" and _string(event.get("pass_kind")) == "review":
            review_seconds += _integer(event.get("pass_elapsed_seconds"))
        elif event_type == "pass_finished" and _string(event.get("pass_kind")) == "fix":
            fix_passes.append(event)
        elif (
            event_type == "activity_finished"
            and _string(event.get("activity_label")) == UX_REVIEW_ACTIVITY_LABEL
        ):
            elapsed = _integer(event.get("activity_elapsed_seconds"))
            review_seconds += elapsed
            ux_check_seconds += elapsed
    # A fix pass carries the round the ledger dispatched (`fix_pass`), and the
    # dispatch names the findings that round covers.
    ux_rounds = {
        _integer(event.get("round"))
        for event in dispatched
        if ux_finding_ids.intersection(_string_list(event.get("covered")))
    }
    ux_repair_seconds = sum(
        _integer(event.get("pass_elapsed_seconds"))
        for event in fix_passes
        if _integer(event.get("fix_pass")) in ux_rounds
    )
    raised = _raised_code_findings(session_dir, instance_id)
    if raised is not None:
        code_findings = raised
    print(
        f"review trial: ux {ux_findings} findings, code {code_findings} findings, "
        + f"review-worker minutes {(review_seconds + 30) // 60}, "
        + f"ux check minutes {(ux_check_seconds + 30) // 60}, "
        + f"ux repair minutes {(ux_repair_seconds + 30) // 60}"
    )


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
        "generated_at": _iso_time(plan_history.now_epoch()),
        "history_root": str(plan_history.history_root()),
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
        description="Record and aggregate durable unit progress events."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity = subparsers.add_parser("set-unit")
    _ = identity.add_argument("--session-dir", required=True)
    _ = identity.add_argument("--production", required=True)
    _ = identity.add_argument("--unit", required=True)
    identity.set_defaults(handler=_set_unit)

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
    _ = start_phase.add_argument("--started-at", default="")
    start_phase.set_defaults(handler=_start_phase)

    start_wait = subparsers.add_parser("start-wait")
    _ = start_wait.add_argument("--session-dir", required=True)
    _ = start_wait.add_argument("--kind", choices=WAIT_KINDS, required=True)
    wait_phase = start_wait.add_mutually_exclusive_group()
    _ = wait_phase.add_argument("--phase-id", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--slot", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--worker-thread-id", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--director", action="store_true")
    _ = wait_phase.add_argument("--phase-instance-id", default=argparse.SUPPRESS)
    _ = start_wait.add_argument("--missing-reason", default="")
    _ = start_wait.add_argument("--reason", default="")
    _ = start_wait.add_argument("--owner-session", default="")
    _ = start_wait.add_argument("--unit", default="")
    _ = start_wait.add_argument("--probe", action="store_true")
    start_wait.set_defaults(handler=_start_wait)

    finish_wait = subparsers.add_parser("finish-wait")
    _ = finish_wait.add_argument("--session-dir", required=True)
    _ = finish_wait.add_argument("--wait-id", required=True)
    finish_wait.set_defaults(handler=_finish_wait)

    checkpoint = subparsers.add_parser("checkpoint")
    _ = checkpoint.add_argument("--session-dir", required=True)
    _ = checkpoint.add_argument("--phase", required=True)
    _ = checkpoint.add_argument("--commit", required=True)
    checkpoint.set_defaults(handler=_checkpoint)

    for verb in ("start-checkpoint-wait", "start-handoff-wait"):
        external_wait = subparsers.add_parser(verb)
        _ = external_wait.add_argument("--commit", required=True)
        _ = external_wait.add_argument("--owner-session", default="")
        _ = external_wait.add_argument("--unit", default="")
        _ = external_wait.add_argument("--reason", default="")
        if verb == "start-checkpoint-wait":
            _ = external_wait.add_argument("--working-dir", required=True)
            _ = external_wait.add_argument("--phase-id", required=True)
        else:
            _ = external_wait.add_argument("--production", required=True)
        external_wait.set_defaults(handler=_start_external_wait)

    finish_external = subparsers.add_parser("finish-external-wait")
    _ = finish_external.add_argument("--wait-id", required=True)
    finish_external.set_defaults(handler=_finish_external_wait)

    waits = subparsers.add_parser("waits")
    _ = waits.add_argument("--session-dir", default="")
    _ = waits.add_argument("--since", default="")
    _ = waits.add_argument("--until", default="")
    waits.set_defaults(handler=_read_waits)

    receipts = subparsers.add_parser("receipts")
    _ = receipts.add_argument("--since", default="")
    _ = receipts.add_argument("--until", default="")
    _ = receipts.add_argument("--merge-commit", default="")
    _ = receipts.add_argument("--project", default="")
    _ = receipts.add_argument("--target-branch", default="")
    receipts.set_defaults(handler=_read_receipts)

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

    check_launch = subparsers.add_parser("check-launch")
    _ = check_launch.add_argument("--session-dir", required=True)
    _ = check_launch.add_argument("--pass-kind", choices=("impl", "test", "fix", "review"), required=True)
    _ = check_launch.add_argument("--phase-id", default="")
    _ = check_launch.add_argument("--prompt-file", default="")
    _ = check_launch.add_argument("--fix-cause", default="")
    _ = check_launch.add_argument("--lens", default="")
    check_launch.set_defaults(handler=_check_launch)

    start_pass = subparsers.add_parser("start-pass")
    _ = start_pass.add_argument("--session-dir", required=True)
    _ = start_pass.add_argument("--pass-kind", choices=("impl", "test", "fix", "review"), required=True)
    _ = start_pass.add_argument("--fix-pass", type=int, default=0)
    _ = start_pass.add_argument("--activity", required=True)
    _ = start_pass.add_argument("--called-task", required=True)
    _ = start_pass.add_argument("--worker-name", default="", help="the worker this pass was launched to")
    _ = start_pass.add_argument("--called-family", required=True)
    _ = start_pass.add_argument("--called-model", required=True)
    _ = start_pass.add_argument("--called-effort", default="unset")
    _ = start_pass.add_argument("--phase-id", default="", help="the open phase this pass belongs to; required for recorded runs")
    _ = start_pass.add_argument("--prompt-file", default="")
    _ = start_pass.add_argument("--fix-cause", default="")
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
        help="close this slot's open pass when the unit director killed its "
        + "launcher (--status canceled only)",
    )
    # Accepted and ignored: implement.sh and review.sh launchers started before
    # 2026-10-15 18:00 EDT still pass it, since bash keeps the script it began
    # with. Remove after 2026-10-22.
    _ = finish_pass.add_argument(
        "--agent-awake-seconds",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    finish_pass.set_defaults(handler=_finish_pass)

    arm_review = subparsers.add_parser(
        "arm-review",
        help="show an early-launched reviewer as a running row beside the passes "
        + "already open",
    )
    _ = arm_review.add_argument("--session-dir", required=True)
    _ = arm_review.add_argument("--activity", required=True)
    _ = arm_review.add_argument("--called-task", default="direct.review")
    _ = arm_review.add_argument("--lens", default="")
    _ = arm_review.add_argument("--phase-id", default="", help="the open phase the review belongs to, as review.sh --phase names it")
    arm_review.set_defaults(handler=_arm_review)

    disarm_review = subparsers.add_parser(
        "disarm-review",
        help="drop the early reviewer's row when its launch is canceled or discarded",
    )
    _ = disarm_review.add_argument("--session-dir", required=True)
    _ = disarm_review.add_argument("--reason", default="cleared")
    disarm_review.set_defaults(handler=_disarm_review)

    calibrate = subparsers.add_parser("calibrate")
    _ = calibrate.add_argument("--session-dir", required=True)
    _ = calibrate.add_argument("--candidate-percent", type=int, required=True)
    _ = calibrate.add_argument("--cap-stage", choices=tuple(PROGRESS_CAPS), default="")
    calibrate.set_defaults(handler=_calibrate)

    progress = subparsers.add_parser("progress")
    _ = progress.add_argument("--session-dir", required=True)
    _ = progress.add_argument("--tables", action="store_true", help="include the round table")
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

    eta = subparsers.add_parser("eta")
    _ = eta.add_argument("--session-dir", required=True)
    _ = eta.add_argument("--phase-id", default="")
    _ = eta.add_argument("--time", required=True)
    _ = eta.add_argument("--earliest", default="")
    _ = eta.add_argument("--latest", default="")
    _ = eta.add_argument("--basis", required=True)
    eta.set_defaults(handler=_eta)

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
    _ = finish_phase.add_argument("--phase-id", default="")
    _ = finish_phase.add_argument("--finished-at", default="")
    finish_phase.set_defaults(handler=_finish_phase)

    finish_run = subparsers.add_parser("finish-run")
    _ = finish_run.add_argument("--session-dir", required=True)
    _ = finish_run.add_argument(
        "--status",
        choices=("completed", "stopped", "error"),
        required=True,
    )
    finish_run.set_defaults(handler=_finish_run)

    review_trial = subparsers.add_parser(
        "review-trial",
        help="one line for the checkpoint notice: the phase's ux and craft finding "
        + "counts and its review-worker minutes",
    )
    _ = review_trial.add_argument("--session-dir", required=True)
    _ = review_trial.add_argument("--phase-id", default="")
    review_trial.set_defaults(handler=_review_trial)

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
    if _arg_string(args, "command") in ("waits", "receipts"):
        handler(args)
        return
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
