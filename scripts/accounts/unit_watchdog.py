"""Restore stopped production units from current staff rows."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

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
from typing import NotRequired, TypedDict, Protocol, cast

from scripts.accounts import outage_alerts, role_map
from scripts.production import showrunners, staff
from scripts.whoami import account
from scripts.shutdown import stop_results

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.shutdown.remote import local_machine

from scripts.herdr import pane_launch
from scripts.herdr import herdr
from scripts.production import live_units, unit_lookup
from scripts.shutdown import pane_record
from scripts.message import sessions
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
    """Current staff facts and the account for a recovery attempt."""
    session: RecoverableUnit
    label: str
    identity: str


@dataclass(frozen=True)
class NoRecovery:
    """The unit has no launch record or its newest launch has ended."""
    session_id: str


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


def _sources(report_failure: Callable[[str], None] = lambda _message: None) -> dict[tuple[str, str], RecoverySource | NoRecovery]:
    from scripts.accounts import accounts

    record = staff.initial_snapshot()
    sources: dict[tuple[str, str], RecoverySource | NoRecovery] = {}
    for member in staff.active(record, "unit"):
        if not isinstance(member.worktree, staff.PresentWorktree) or member.worktree.machine != local_machine():
            continue
        parent = record.parent_of(member)
        production = parent.name if parent is not None else ""
        try:
            entry = pane_record.launch_for_session(member.current_session)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            report_failure(f"watchdog: {production}/{member.name} session {member.current_session}: {error}")
            continue
        if entry is None or entry.get("ended", False):
            sources[(production, member.name)] = NoRecovery(member.current_session)
            continue
        label = accounts.claude_config_label(Path(pane_launch.config_home(entry)))
        owner = RecoverableUnit(session_id=member.current_session, name=member.name,
            cwd=str(member.worktree.path), unit=member.name, production=production)
        sources[(production, member.name)] = RecoverySource(owner, label, member.id)
    return sources


class ResumeUnit(Protocol):
    def __call__(self, session: str, *, why: str, to: str) -> stop_results.ResumeResult: ...


def _restart(label: str, identity: str, source: str) -> str:
    """Resolve the current session before the recovery starts."""
    try:
        record = staff.snapshot()
        member = record.by_member_reference(identity) or record.by_member_reference(source) or record.by_path(identity)
        if member is None:
            return f"{identity}: unknown staff member"
        identity = member.current_session
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.SessionLivenessUnknown):
            return live.reason
        if isinstance(live, sessions.LiveSessionProcess):
            return ""  # Another recovery already brought this conversation back.
        resume = cast(ResumeUnit, import_module("scripts.shutdown.stop_resume").resume)
        result = resume(identity, why="unit watchdog recovery", to=label)
        if isinstance(result, stop_results.ClaudeUp):
            return ""
        return (result.reason
                if isinstance(result, stop_results.ResumeFailed)
                else f"{identity}: no pane launch")
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
    # The child resolves the staff reference before the start.
    command = [os.environ.get("SHUTDOWN_SYSTEMD_RUN", "systemd-run"), "--user", "--collect", "--quiet", "--no-block",
        "--property=RuntimeMaxSec=" + str(_recovery_seconds()), "--working-directory=" + str(SCRIPTS.parent),
        "--", str(SCRIPTS / "lib/py"), "-m", "scripts.accounts.unit_watchdog", "--recover",
        source.label, source.identity, job["result"], source.identity]
    try:
        child = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
        failure = "" if child.returncode == 0 else f"could not detach restart (status {child.returncode})"
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"could not detach restart: {type(error).__name__}"
    if failure:
        _ = Path(job["result"]).write_text(json.dumps(failure), encoding="utf-8")


@herdr.process_action()
def _fresh_units() -> unit_lookup.LabeledProductions:
    return unit_lookup.every_labeled_session()


# Recovery decisions share one fresh observation, independent of earlier reports.
_recovery_observations: ContextVar[Callable[[], unit_lookup.LabeledProductions]] = ContextVar(
    "watchdog_recovery_observations", default=_fresh_units)


def _live_account(production_name: str, unit: str) -> str:
    from scripts.accounts import accounts

    for found in live_units.units_in(production_name, _recovery_observations.get()()):
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
    runners = showrunners.ShowrunnerDiscovery()
    from scripts.maintenance.settings import settings
    limits = settings()
    dispatch_deadline = time.monotonic() + limits.recovery_dispatch_seconds
    try:
        state = _read_state()
        before = deepcopy(state)
        sources = _sources(lines.append)
        runners = showrunners.registered_showrunners()
        observed_at = time.time()

        @cache
        def unit_observations() -> unit_lookup.LabeledProductions:
            return unit_lookup.every_labeled_session()

        retained = dict(state)
        for runner in runners:
            try:
                for found in live_units.units_in(runner["name"], unit_observations()):
                    unit = found.unit
                    try:
                        source = sources.get((runner["name"], unit))
                        if source is None:
                            continue
                        if isinstance(source, NoRecovery):
                            if not dry_run:
                                key = source.session_id
                                outage = state.get(key)
                                if outage is not None and "job" in outage:
                                    Path(outage["job"]["result"]).unlink(missing_ok=True)
                                _ = state.pop(key, None)
                                _ = retained.pop(key, None)
                            continue
                        claude = found.session.claude if found.session is not None else unit_lookup.ClaudeNotRunning()
                        if not isinstance(claude, unit_lookup.LiveClaude):
                            # One fresh recovery observation for all absent units.
                            current = {item.unit: item for item in live_units.units_in(runner["name"], _recovery_observations.get()())}
                            found = current.get(unit)
                            if found is None:
                                raise OSError(f"{unit}: unit liveness is unavailable")
                            claude = found.session.claude if found.session is not None else unit_lookup.ClaudeNotRunning()
                        if isinstance(claude, unit_lookup.LiveClaude):
                            outage = state.get(claude.session_id)
                            restored = ""
                            if outage is not None and "job" in outage and not dry_run:
                                restored = _live_account(runner["name"], unit)
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
                        job = outage.get("job")
                        if job is not None:
                            outcome = _recovery_result(job, now)
                            if isinstance(outcome, RecoveryStillRunning):
                                continue
                            failure = outcome.failure
                            restored = _live_account(runner["name"], unit)
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
                        job = RecoveryJob(result=str(results / (uuid.uuid4().hex + ".json")), started=now.isoformat())
                        outage["job"] = job
                        # Save ownership before detaching; a later pass never starts a second copy.
                        _write_state({**state, **retained})
                        _start_recovery(source, job, min(limits.dispatch_seconds, remaining))
                    except (OSError, ValueError, herdr.HerdrFailure) as error:
                        # Preserve incomplete observations, then continue with the next unit.
                        retained.update({key: value for key, value in state.items() if key not in retained})
                        lines.append(f"watchdog: {runner['name']}/{unit}: {error}")
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                # Keep pending recovery evidence when discovery is incomplete.
                retained.update({key: value for key, value in state.items() if key not in retained})
                lines.append(f"watchdog: {runner['name']}: {error}")
        if not dry_run and retained != before:
            _write_state(retained)
    except (OSError, ValueError) as error:
        lines.append(f"watchdog: {error}")
    if runners.lookup_failures:
        for line in lines:
            print(line)
        _ = showrunners.require_complete(runners)
    return lines


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--recover":
        raise SystemExit("usage: unit_watchdog --recover <account> <session> <result> <source>")
    _finish_recovery(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5])
