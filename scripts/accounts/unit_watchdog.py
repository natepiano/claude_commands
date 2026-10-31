"""Restore down production units after one notes pass of grace."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
from contextvars import ContextVar
from datetime import datetime, timedelta
from importlib import import_module
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts import outage_alerts, role_map
from scripts.production import showrunners
from scripts.whoami import account
from scripts.shutdown import stop_results

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in ("production", "shutdown", "herdr", "message", "whoami"):
    sys.path.insert(0, str(SCRIPTS / directory))

from scripts.shutdown import shutdown_record

if TYPE_CHECKING:
    from scripts.production import live_units, production_units, unit_lookup
    from scripts.herdr import herdr
    from scripts.shutdown import pane_record
    from scripts.production import unit_move
    from scripts.message import sessions
else:
    herdr = import_module("herdr")
    live_units = import_module("live_units")
    production_units = import_module("production_units")
    unit_lookup = import_module("unit_lookup")
    pane_record = import_module("pane_record")
    unit_move = import_module("unit_move")
    sessions = import_module("sessions")
from scripts.lib.user_zone import user_zone


class RecoveryJob(TypedDict):
    result: str
    started: str
    source: NotRequired[str]


class RecoveryOwner(TypedDict):
    production: str
    unit: str


class DownOutage(TypedDict):
    first_seen: str
    job: NotRequired[RecoveryJob]
    dropped: NotRequired[str]
    alert_context: NotRequired[RecoveryOwner]


@dataclass(frozen=True)
class RecoveryStillRunning:
    """The detached worker has time left to finish."""


@dataclass(frozen=True)
class RecoveryFinished:
    """The worker result has been read but not consumed."""
    failure: str
    result_path: Path


class RecoverableUnit(TypedDict):
    session_id: str
    name: str
    cwd: str
    unit: str
    production: str


@dataclass(frozen=True)
class RecoverySource:
    """A unit's current pane launch, checked again before starting recovery."""
    session: RecoverableUnit
    label: str
    identity: str


def _state_file() -> Path:
    return Path.home() / ".local/state/accounts/unit_watchdog.json"


def _read_state() -> dict[str, DownOutage]:
    try:
        value = cast(object, json.loads(_state_file().read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            raise ValueError("watchdog state is not an object")
        result: dict[str, DownOutage] = {}
        for key, raw in cast(dict[str, object], value).items():
            if not isinstance(raw, dict):
                raise ValueError("invalid watchdog outage")
            fields = cast(dict[str, object], raw)
            at = fields.get("first_seen")
            if not isinstance(at, str) or datetime.fromisoformat(at).tzinfo is None:
                raise ValueError("invalid watchdog outage")
            outage = DownOutage(first_seen=at)
            dropped = fields.get("dropped")
            if isinstance(dropped, str):
                outage["dropped"] = dropped
            context = fields.get("alert_context")
            if isinstance(context, dict):
                details = cast(dict[str, object], context)
                production, unit = details.get("production"), details.get("unit")
                if not isinstance(production, str) or not isinstance(unit, str):
                    raise ValueError("invalid watchdog recovery owner")
                outage["alert_context"] = RecoveryOwner(production=production, unit=unit)
            job = fields.get("job")
            if isinstance(job, dict):
                job_fields = cast(dict[str, object], job)
                path, started = job_fields.get("result"), job_fields.get("started")
                if not isinstance(path, str) or not isinstance(started, str) or datetime.fromisoformat(started).tzinfo is None:
                    raise ValueError("invalid watchdog recovery job")
                outage["job"] = RecoveryJob(result=path, started=started)
                identity = job_fields.get("source")
                if isinstance(identity, str):
                    outage["job"]["source"] = identity
            result[key] = outage
        return result
    except FileNotFoundError:
        return {}


def _write_state(state: dict[str, DownOutage]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            _ = stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sources(report_failure: Callable[[str], None] = lambda _message: None) -> dict[tuple[str, str], RecoverySource]:
    sources: dict[tuple[str, str], RecoverySource] = {}
    newest: dict[tuple[str, str], pane_record.PaneLaunch] = {}
    # Records are ordered by their validated UTC request time; the latest stop wins.
    stops = {record["session"]["session_id"]: record for record in shutdown_record.all_session_records()
             if record["then"] == "stop"}
    for entry in sorted(pane_record.launches(), key=lambda item: item["written_at"]):
        marks = entry["environment"]
        unit, production = marks.get("SHOWRUNNER_UNIT_ID", ""), marks.get("SHOWRUNNER_UNIT", "")
        if not unit or not production or not entry["session_id"]:
            continue
        key = (production, unit)
        # The newest launch also records an intentional /exit. It supersedes older entries.
        newest[key] = entry
    for key, entry in newest.items():
        if entry.get("ended", False):
            continue
        stopped = stops.get(entry["session_id"])
        if stopped is not None and datetime.fromisoformat(stopped["requested_at"]) >= datetime.fromisoformat(entry["written_at"]):
            continue
        production, unit = key
        try:
            conversation = unit_move.unit_conversation(None, session_id=entry["session_id"])
            if isinstance(conversation, unit_move.NoUnitConversation):
                continue
            owner = RecoverableUnit(session_id=entry["session_id"], name=entry["name"] or unit,
                cwd=entry["cwd"], unit=unit, production=production)
            sources[key] = RecoverySource(owner, conversation.account, json.dumps(entry, sort_keys=True))
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            report_failure(f"watchdog: {production}/{unit} session {entry['session_id']}: {error}")
    return sources


class ResumeUnit(Protocol):
    def __call__(self, session: str, *, why: str, to: str, restore_missing: bool) -> stop_results.ClaudeUp | stop_results.ResumeFailed | stop_results.SessionNotFound: ...


def _restart(label: str, identity: str, source: str) -> str:
    """Resume only the launch that was queued; a changed launch cancels stale work."""
    try:
        current = next((item for item in _sources().values() if item.session["session_id"] == identity), None)
        if current is None or current.identity != source or current.label != label:
            return "unit pane launch changed; recovery refused"
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.SessionLivenessUnknown):
            return live.reason
        if isinstance(live, sessions.LiveSessionProcess):
            return ""  # Another recovery already brought this conversation back.
        resume = cast(ResumeUnit, import_module("scripts.shutdown.stop_resume").resume)
        result = resume(identity, why="unit watchdog recovery", to=label, restore_missing=True)
        if isinstance(result, stop_results.ClaudeUp):
            return ""
        return result.reason if isinstance(result, stop_results.ResumeFailed) else f"{identity}: no pane launch"
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        return str(error)


def _finish_recovery(label: str, identity: str, result: Path, source: str) -> None:
    """Run outside the notes pass. Retain only a short status, never command output."""
    failure = _restart(label, identity, source)
    temporary = result.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(failure), encoding="utf-8")
    os.replace(temporary, result)


def _recovery_seconds() -> float:
    from scripts.maintenance.settings import settings
    return settings().recovery_seconds


def _start_recovery(source: RecoverySource, job: RecoveryJob, timeout: float) -> None:
    # The child rechecks this pane launch identity before starting Claude.
    command = [os.environ.get("SHUTDOWN_SYSTEMD_RUN", "systemd-run"), "--user", "--collect", "--quiet", "--no-block",
        "--property=RuntimeMaxSec=" + str(_recovery_seconds()), "--working-directory=" + str(SCRIPTS.parent),
        "--", sys.executable, "-m", "scripts.accounts.unit_watchdog", "--recover",
        source.label, source.session["session_id"], job["result"], source.identity]
    try:
        child = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
        failure = "" if child.returncode == 0 else f"could not detach restart (status {child.returncode})"
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"could not detach restart: {type(error).__name__}"
    if failure:
        _ = Path(job["result"]).write_text(json.dumps(failure), encoding="utf-8")


@herdr.process_action()
def _fresh_units() -> unit_lookup.MarkedProductions:
    return unit_lookup.every_marked_unit()


# Recovery decisions share one fresh observation, independent of earlier reports.
_recovery_observations: ContextVar[Callable[[], unit_lookup.MarkedProductions]] = ContextVar(
    "watchdog_recovery_observations", default=_fresh_units)


def _live_account(doc: Path, unit: str) -> str:
    from scripts.accounts import accounts

    for found in live_units.units_in(doc, _recovery_observations.get()()):
        if found.unit != unit or found.session is None:
            continue
        claude = found.session.claude
        if isinstance(claude, unit_lookup.ClaudeUnknown):
            raise OSError(claude.reason)
        if isinstance(claude, unit_lookup.LiveClaude):
            identity = account.process_config_dir(claude.pid)
            if not isinstance(identity, account.ConfiguredClaudeFolder):
                raise OSError("the restarted session's account is unreadable")
            return accounts.claude_config_label(identity.path)
    return ""


def _recovery_result(job: RecoveryJob, now: datetime) -> RecoveryStillRunning | RecoveryFinished:
    from scripts.maintenance.settings import settings
    result = Path(job["result"])
    if not result.exists() and now - datetime.fromisoformat(job["started"]) < timedelta(seconds=settings().recovery_result_seconds):
        return RecoveryStillRunning()
    failure = (cast(object, json.loads(result.read_text(encoding="utf-8"))) if result.exists()
               else "detached restart did not finish within its time limit")
    if not isinstance(failure, str):
        raise ValueError("invalid watchdog recovery result")
    return RecoveryFinished(failure, result)


def run(now: datetime, *, dry_run: bool) -> list[str]:
    """Serialize direct calls with the same state lock as the scheduled pass."""
    if not role_map.is_role_map_host():
        return []
    if dry_run:
        with herdr.process_snapshot():
            return _run(now, dry_run=True)
    path = _state_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with herdr.process_snapshot():
            token = _recovery_observations.set(cache(_fresh_units))
            try:
                return _run(now, dry_run=dry_run)
            finally:
                _recovery_observations.reset(token)


def _run(now: datetime, *, dry_run: bool) -> list[str]:
    """Check local production units, under the watcher's lock."""
    lines: list[str] = []
    from scripts.maintenance.settings import settings
    limits = settings()
    dispatch_deadline = time.monotonic() + limits.recovery_dispatch_seconds
    try:
        state = _read_state()
        before = deepcopy(state)
        sources = _sources(lines.append)
        runners = showrunners.registered_showrunners()
        runs = unit_lookup.read_run_states()
        observed_at = time.time()

        @cache
        def unit_observations() -> unit_lookup.MarkedProductions:
            return unit_lookup.every_marked_unit()

        retained: dict[str, DownOutage] = {}
        for runner in runners:
            if not runner["doc"]:
                continue
            doc = Path(runner["doc"])
            try:
                rows = production_units.live_unit_table(doc.read_text(encoding="utf-8").splitlines(), runner["slug"])
                for row in rows:
                    unit = production_units.cell_value(row.get("Unit", ""))
                    try:
                        worktree = production_units.cell_value(row.get("Worktree", ""))
                        if not worktree or runs.state(Path(worktree)) is unit_lookup.UnitState.RUN_FINISHED:
                            continue
                        observed = {item.unit: item for item in live_units.units_in(doc, unit_observations())}
                        found = observed.get(unit)
                        if found is None:
                            raise OSError(f"{unit}: unit liveness is unavailable")
                        claude = found.session.claude if found.session is not None else unit_lookup.ClaudeNotRunning()
                        if not isinstance(claude, unit_lookup.LiveClaude):
                            # One fresh recovery observation for all absent units.
                            current = {item.unit: item for item in live_units.units_in(doc, _recovery_observations.get()())}
                            found = current.get(unit)
                            if found is None:
                                raise OSError(f"{unit}: unit liveness is unavailable")
                            claude = found.session.claude if found.session is not None else unit_lookup.ClaudeNotRunning()
                        if isinstance(claude, unit_lookup.LiveClaude):
                            outage = state.get(claude.session_id)
                            restored = ""
                            if outage is not None and "job" in outage and not dry_run:
                                restored = _live_account(doc, unit)
                                if not restored:
                                    claude = unit_lookup.ClaudeNotRunning()
                            if isinstance(claude, unit_lookup.LiveClaude):
                                if not dry_run:
                                    _ = outage_alerts.clear(claude.session_id, observed_at)
                                if outage is not None and not dry_run:
                                    if "job" in outage:
                                        Path(outage["job"]["result"]).unlink(missing_ok=True)
                                        lines.append(f"watchdog: {unit}: restarted on {restored}")
                                    _ = state.pop(claude.session_id, None)
                                    _ = retained.pop(claude.session_id, None)
                                    _write_state({**state, **retained})
                                continue
                        source = sources.get((runner["slug"], unit))
                        if source is None:
                            for key, outage in state.items():
                                context = outage.get("alert_context")
                                if context is None or (context["production"], context["unit"]) != (runner["slug"], unit):
                                    continue
                                # A launch entry can disappear while a recovery job finishes. Keep its result for recovery bookkeeping.
                                retained[key] = outage
                                if isinstance(claude, unit_lookup.ClaudeUnknown) or dry_run:
                                    continue
                                job = outage.get("job")
                                if job is not None:
                                    outcome = _recovery_result(job, now)
                                    if isinstance(outcome, RecoveryStillRunning):
                                        continue
                                    # Keep the job retryable if result cleanup fails.
                                    outcome.result_path.unlink(missing_ok=True)
                                    outage["dropped"] = outcome.failure or "restart did not make the session live"
                                    _ = outage.pop("job")
                                if "dropped" in outage:
                                    text = f"watchdog: {unit}: {outage['dropped']}"
                                    lines.append(text)
                                    _write_state({**state, **retained})
                            lines.append(f"watchdog: {unit}: no pane launch entry")
                            continue
                        key = source.session["session_id"]
                        outage = state.get(key)
                        if isinstance(claude, unit_lookup.ClaudeUnknown):
                            if key in state:
                                retained[key] = state[key]
                            lines.append(f"watchdog: {unit}: {claude.reason}")
                            continue
                        if outage is None:
                            outage = DownOutage(first_seen=now.isoformat())
                            retained[key] = outage
                            continue
                        retained[key] = outage
                        if datetime.fromisoformat(outage["first_seen"]) >= now:
                            continue
                        if dry_run:
                            lines.append(f"watchdog: would restart {unit} from {source.label}'s pane launch")
                            continue
                        if "dropped" in outage or not Path(source.session["cwd"]).is_dir():
                            outage["dropped"] = f"folder {source.session['cwd']} no longer exists; restore dropped"
                            text = f"watchdog: {unit}: {outage['dropped']}"
                            lines.append(text)
                            _write_state({**state, **retained})
                            continue
                        job = outage.get("job")
                        if job is not None:
                            outcome = _recovery_result(job, now)
                            if isinstance(outcome, RecoveryStillRunning):
                                continue
                            failure = outcome.failure
                            restored = _live_account(doc, unit)
                            # Keep the result and job if the fresh read is unreadable.
                            outcome.result_path.unlink(missing_ok=True)
                            _ = outage.pop("job")
                            since = datetime.fromisoformat(outage["first_seen"]).astimezone(user_zone()).strftime("%H:%M %Z")
                            text = (f"watchdog: {unit} was down since {since}; restarted on {restored}" if restored else
                                f"watchdog: {unit} was down since {since}; {failure or 'restart did not make the session live'}; "
                                + f"retry from {source.label}'s pane launch")
                            lines.append(text)
                            if restored:
                                del retained[key]
                                _ = state.pop(key, None)
                            _write_state({**state, **retained})
                            if restored:
                                continue
                        remaining = dispatch_deadline - time.monotonic()
                        if remaining <= 0:
                            continue
                        results = _state_file().parent / "unit-watchdog-results"
                        results.mkdir(parents=True, exist_ok=True)
                        job = RecoveryJob(result=str(results / (uuid.uuid4().hex + ".json")), started=now.isoformat(), source=source.identity)
                        outage["job"] = job
                        outage["alert_context"] = RecoveryOwner(production=runner["slug"], unit=unit)
                        # Save ownership before detaching; a later pass never starts a second copy.
                        _write_state({**state, **retained})
                        _start_recovery(source, job, min(limits.dispatch_seconds, remaining))
                    except (OSError, ValueError, herdr.HerdrFailure) as error:
                        # Preserve incomplete observations, then continue with the next unit.
                        retained.update({key: value for key, value in state.items() if key not in retained})
                        lines.append(f"watchdog: {runner['slug']}/{unit}: {error}")
            except (OSError, ValueError) as error:
                # Keep pending recovery evidence when discovery is incomplete.
                retained.update({key: value for key, value in state.items() if key not in retained})
                lines.append(f"watchdog: {runner['slug']}: {error}")
        if not dry_run and retained != before:
            _write_state(retained)
    except (OSError, ValueError) as error:
        lines.append(f"watchdog: {error}")
    return lines


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--recover":
        raise SystemExit("usage: unit_watchdog --recover <account> <session> <result> <source>")
    _finish_recovery(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5])
