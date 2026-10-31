"""Restore down production units after one notes pass of grace."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from scripts.accounts import accounts, outage_alerts, role_map
from scripts.production import showrunners, staff_sources
import account

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in ("production", "shutdown"):
    sys.path.insert(0, str(SCRIPTS / directory))

if TYPE_CHECKING:
    from scripts.production import live_units, production_units, unit_lookup
    from scripts.herdr import herdr
    from scripts.shutdown import inventory, launch_permission, shutdown_record, restart
else:
    herdr = import_module("herdr")
    live_units = import_module("live_units")
    production_units = import_module("production_units")
    unit_lookup = import_module("unit_lookup")
    inventory = import_module("inventory")
    launch_permission = import_module("launch_permission")
    shutdown_record = import_module("shutdown_record")
    restart = import_module("restart")
import quota_alert
from scripts.notify.push_texts import render
from scripts.lib.user_zone import user_zone


class RecoveryJob(TypedDict):
    result: str
    started: str
    source: NotRequired[str]


class RecoveryAlertContext(TypedDict):
    production: str
    unit: str
    account: str
    session_name: str


class DownOutage(TypedDict):
    first_seen: str
    alerted: bool
    job: NotRequired[RecoveryJob]
    recovery: NotRequired[shutdown_record.RecoverySnapshot]
    dropped: NotRequired[str]
    alert_context: NotRequired[RecoveryAlertContext]


@dataclass(frozen=True)
class RecoveryStillRunning:
    """The detached worker has time left to finish."""


@dataclass(frozen=True)
class RecoveryFinished:
    """The worker result has been read but not consumed."""
    failure: str
    result_path: Path


@dataclass(frozen=True)
class RecoverySource:
    session: inventory.Session
    label: str
    login: str
    # A move record delegates its fallback and phone alert to restart.
    move_record: bool
    blocked: bool
    identity: str
    restart_claim: bool = False
    snapshot_restore: bool = False


_IN_PROGRESS = frozenset({"waiting", "ready", "stopping", "restarting", "held by usage limit", "reaping"})


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
            at, alerted = fields.get("first_seen"), fields.get("alerted")
            if not isinstance(at, str) or not isinstance(alerted, bool) or datetime.fromisoformat(at).tzinfo is None:
                raise ValueError("invalid watchdog outage")
            outage = DownOutage(first_seen=at, alerted=alerted)
            dropped = fields.get("dropped")
            if isinstance(dropped, str):
                outage["dropped"] = dropped
            context = fields.get("alert_context")
            if isinstance(context, dict):
                details = cast(dict[str, object], context)
                production, unit = details.get("production"), details.get("unit")
                account_label, session_name = details.get("account"), details.get("session_name")
                if not all(isinstance(item, str) for item in (production, unit, account_label, session_name)):
                    raise ValueError("invalid watchdog alert context")
                outage["alert_context"] = RecoveryAlertContext(production=cast(str, production), unit=cast(str, unit),
                    account=cast(str, account_label), session_name=cast(str, session_name))
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
            if "recovery" in fields:
                outage["recovery"] = shutdown_record.parse_snapshot(fields["recovery"])
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


def _sources(state: dict[str, DownOutage]) -> dict[tuple[str, str], RecoverySource]:
    # Pick the newest session identity first. A record can hold only that identity.
    sessions_by_id: dict[str, tuple[str, RecoverySource]] = {}
    stored = sorted(shutdown_record.all_snapshots(), key=lambda item: datetime.fromisoformat(item["taken_at"]))
    stored_ids = {entry["session"]["session_id"] for snapshot in stored
                  if snapshot["machine"] == "natedev" for entry in snapshot["sessions"]}
    # The store is authoritative while it still holds the session.
    for key, outage in state.items():
        for snapshot in stored:
            if snapshot["machine"] != "natedev":
                continue
            for entry in snapshot["sessions"]:
                if entry["session"]["session_id"] == key:
                    recovery = snapshot.copy()
                    recovery["sessions"] = [entry]
                    outage["recovery"] = recovery
    cached = [outage["recovery"] for key, outage in state.items()
              if "recovery" in outage and key not in stored_ids]
    snapshots = cached + stored
    for snapshot in sorted(snapshots, key=lambda item: datetime.fromisoformat(item["taken_at"])):
        if snapshot["machine"] != "natedev":
            continue
        for entry in snapshot["sessions"]:
            owner = entry["session"]
            if entry["agent"] == "claude" and owner["kind"] == "unit":
                previous = sessions_by_id.get(owner["session_id"])
                if previous is not None and datetime.fromisoformat(previous[0]) > datetime.fromisoformat(entry["last_seen"]):
                    continue
                sessions_by_id[owner["session_id"]] = (entry["last_seen"], RecoverySource(
                    owner, snapshot["label"], snapshot["login"], False, False,
                    json.dumps({"kind": "snapshot", "session": owner, "last_seen": entry["last_seen"]})))
    for saved in sorted(shutdown_record.all_session_records(), key=lambda item: datetime.fromisoformat(item["requested_at"])):
        owner = saved["session"]
        if saved["machine"] != "natedev" or owner["kind"] != "unit":
            continue
        identity = owner["session_id"]
        previous = sessions_by_id.get(identity)
        seen = previous[0] if previous is not None else saved["requested_at"]
        stopped_by_user = saved["then"] == "stop" and saved["requested_by"]["kind"] in {"session", "terminal"}
        sessions_by_id[identity] = (max(seen, saved["requested_at"], key=datetime.fromisoformat), RecoverySource(
            owner, saved["label"], saved["login"], bool(saved.get("target")) and saved["requested_by"]["kind"] != "snapshot",
            stopped_by_user or saved["progress"]["kind"] in _IN_PROGRESS,
            json.dumps({"kind": "record", "request_id": saved["request_id"], "requested_at": saved["requested_at"]}),
            saved["then"] == "restart" and saved["progress"]["kind"] == "restarting",
            saved["requested_by"]["kind"] == "snapshot"))
    sources: dict[tuple[str, str], RecoverySource] = {}
    for _, source in sorted(sessions_by_id.values(), key=lambda item: datetime.fromisoformat(item[0])):
        host = source.session["host"]
        if host["kind"] == "unit":
            parent = staff_sources.unit_parent(host["unit"], source.session["session_id"])
            sources[(parent.slug, host["unit"])] = source
    return sources


def _held(source: RecoverySource, *, dry_run: bool) -> bool:
    hold = shutdown_record.lookup_account_hold(source.login)
    if not isinstance(hold, shutdown_record.AccountHeld) or hold.hold["then"] != "stop":
        return False
    if dry_run:
        return True
    permission = launch_permission.launch_permission(
        {**os.environ, "CLAUDE_CONFIG_DIR": str(accounts.claude_account_dir(source.label))},
        launch_permission.NewWorkLaunchPurpose())
    return not isinstance(permission, launch_permission.AllowedByShutdownState)


def _restart(label: str, identity: str, source: str) -> str:
    """Return a short failure, or empty text after a successful restart command."""
    try:
        child = subprocess.run([sys.executable, str(SCRIPTS / "shutdown/shutdown.py"),
            "restart", label, "--here", "--only", identity, "--no-alert"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=False,
            env={**os.environ, "UNIT_WATCHDOG_SESSION": identity, "UNIT_WATCHDOG_SOURCE": source})
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    # Command output can contain session text. Keep it out of notes and pushes.
    if any(line.endswith(" no longer exists; restore dropped") for line in child.stdout.splitlines()):
        return "session folder no longer exists; restore dropped"
    return "" if child.returncode == 0 else f"restart on {label} exited {child.returncode}"


def _finish_recovery(label: str, identity: str, result: Path, source: str) -> None:
    """Run outside the notes pass. Retain only a short status, never command output."""
    failure = _restart(label, identity, source)
    temporary = result.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(failure), encoding="utf-8")
    os.replace(temporary, result)


def _start_recovery(source: RecoverySource, job: RecoveryJob, timeout: float) -> None:
    # The recovery owner selects the snapshot or move record. Restart chooses the group.
    command = [os.environ.get("SHUTDOWN_SYSTEMD_RUN", "systemd-run"), "--user", "--collect", "--quiet", "--no-block",
        "--property=RuntimeMaxSec=240", "--working-directory=" + str(SCRIPTS.parent),
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
def _live_account(doc: Path, unit: str) -> str:
    for found in live_units.production_units(doc):
        if found.unit != unit or found.session is None:
            continue
        claude = found.session.claude
        if isinstance(claude, unit_lookup.ClaudeUnknown):
            raise OSError(claude.reason)
        if isinstance(claude, unit_lookup.LiveClaude):
            identity = account.process_config_dir(claude.pid)
            if not isinstance(identity, account.ConfiguredClaudeFolder):
                raise OSError("the restarted session's account is unreadable")
            return identity.path.name.removeprefix(".")
    return ""


def _recovery_result(job: RecoveryJob, now: datetime) -> RecoveryStillRunning | RecoveryFinished:
    result = Path(job["result"])
    if not result.exists() and now - datetime.fromisoformat(job["started"]) < timedelta(minutes=5):
        return RecoveryStillRunning()
    failure = (cast(object, json.loads(result.read_text(encoding="utf-8"))) if result.exists()
               else "detached restart did not finish within 5 minutes")
    if not isinstance(failure, str):
        raise ValueError("invalid watchdog recovery result")
    return RecoveryFinished(failure, result)


def _alert_without_record(session_id: str, context: RecoveryAlertContext, outage: DownOutage, cause: str) -> None:
    claimed = outage_alerts.claim(session_id)
    outage["alerted"] = isinstance(claimed, outage_alerts.AlertDelivered)
    if not isinstance(claimed, outage_alerts.AlertClaim):
        return
    try:
        push = render("shutdown.restart_failed", account=context["account"], machine="natedev",
                      stopped=context["session_name"], cause=cause)
        if isinstance(quota_alert.tell_user(push), quota_alert.PushSent):
            outage_alerts.delivered(claimed)
            outage["alerted"] = True
    finally:
        if not outage["alerted"]:
            outage_alerts.release(claimed)


def _alert_failure(source: RecoverySource, outage: DownOutage, cause: str) -> None:
    saved = shutdown_record.find_session(source.login, source.session["session_id"]) if source.move_record else None
    if saved is not None:
        claimed = outage_alerts.claim(source.session["session_id"])
        outage["alerted"] = isinstance(claimed, outage_alerts.AlertDelivered)
        if not isinstance(claimed, outage_alerts.AlertClaim):
            return
        try:
            _ = shutdown_record.claim_restore_alert(saved, restart.restart_support.not_back_text(saved))
            if isinstance(quota_alert.tell_user(restart.move_failed_push(saved)), quota_alert.PushSent):
                outage_alerts.delivered(claimed)
                outage["alerted"] = True
        finally:
            if not outage["alerted"]:
                outage_alerts.release(claimed)
                shutdown_record.clear_restore_alert(saved)
    else:
        host = source.session["host"]
        assert host["kind"] == "unit"
        parent = staff_sources.unit_parent(host["unit"], source.session["session_id"])
        _alert_without_record(source.session["session_id"], RecoveryAlertContext(production=parent.slug, unit=host["unit"],
            account=source.label, session_name=source.session["name"]), outage, cause)


def run(now: datetime, *, dry_run: bool) -> list[str]:
    """Check local running productions after move passes, under the watcher's lock."""
    if not role_map.is_role_map_host():
        return []
    lines: list[str] = []
    dispatch_deadline = time.monotonic() + 30
    try:
        state = _read_state()
        before = deepcopy(state)
        sources = _sources(state)
        runners = showrunners.registered_showrunners()
        runs = unit_lookup.read_run_states()
        retained: dict[str, DownOutage] = {}
        for runner in runners:
            if not runner["doc"]:
                continue
            doc = Path(runner["doc"])
            try:
                rows = production_units.live_unit_table(doc.read_text(encoding="utf-8").splitlines(), runner["slug"])
                observed_at = time.time()
                observed = {unit.unit: unit for unit in live_units.production_units(doc)}
                for row in rows:
                    unit = production_units.cell_value(row.get("Unit", ""))
                    worktree = production_units.cell_value(row.get("Worktree", ""))
                    if not worktree or runs.state(Path(worktree)) != unit_lookup.UnitState.RUNNING:
                        continue
                    found = observed.get(unit)
                    if found is None:
                        raise OSError(f"{unit}: unit liveness is unavailable")
                    claude = found.session.claude if found.session is not None else unit_lookup.ClaudeNotRunning()
                    if not isinstance(claude, unit_lookup.LiveClaude):
                        # A report can suggest an outage. Prove it fresh before recovery.
                        with herdr.process_action():
                            current = {item.unit: item for item in live_units.production_units(doc)}
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
                                if outage_alerts.clear(claude.session_id, observed_at):
                                    for saved in shutdown_record.all_session_records():
                                        if (saved["session"]["session_id"] == claude.session_id
                                                and "restore_alerted" in saved):
                                            shutdown_record.clear_restore_alert(saved)
                            if outage is not None and "job" in outage and not dry_run:
                                Path(outage["job"]["result"]).unlink(missing_ok=True)
                                since = datetime.fromisoformat(outage["first_seen"]).astimezone(user_zone()).strftime("%H:%M %Z")
                                text = f"watchdog: {unit} was down since {since}; restarted on {restored}"
                                lines.append(text)
                                lines.append(showrunners.tell(runner["slug"], f"unit-watchdog-{claude.session_id}-{now.isoformat()}", text))
                            continue
                    source = sources.get((runner["slug"], unit))
                    if source is None:
                        for key, outage in state.items():
                            context = outage.get("alert_context")
                            if context is None or (context["production"], context["unit"]) != (runner["slug"], unit):
                                continue
                            # A worker can archive the only recovery record. Keep its result and alert retry.
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
                                lines.append(showrunners.tell(runner["slug"], f"unit-watchdog-{key}-{now.isoformat()}", text))
                                _alert_without_record(key, context, outage, outage["dropped"])
                        lines.append(f"watchdog: {unit}: no recovery record or snapshot")
                        continue
                    key = source.session["session_id"]
                    outage = state.get(key)
                    if isinstance(claude, unit_lookup.ClaudeUnknown):
                        if key in state:
                            retained[key] = state[key]
                        lines.append(f"watchdog: {unit}: {claude.reason}")
                        continue
                    if outage is None:
                        if source.blocked or _held(source, dry_run=dry_run):
                            continue
                        outage = DownOutage(first_seen=now.isoformat(), alerted=False)
                        for snapshot in shutdown_record.all_snapshots():
                            for candidate in snapshot["sessions"]:
                                if candidate["agent"] == "claude" and candidate["session"] == source.session:
                                    recovery = snapshot.copy()
                                    recovery["sessions"] = [candidate]
                                    outage["recovery"] = recovery
                        retained[key] = outage
                        if not dry_run and "recovery" in outage:
                            evidence = restart.watchdog_crash_signal(source.session)
                            if isinstance(evidence, restart.AgentCrashConfirmed):
                                candidate = outage["recovery"]["sessions"][0]
                                shutdown_record.mark_snapshot_agent_exited(source.login, key, candidate["last_seen"])
                                if candidate["agent"] == "claude":
                                    candidate["crash"] = "agent exited"
                        continue
                    retained[key] = outage
                    if datetime.fromisoformat(outage["first_seen"]) >= now:
                        continue
                    if dry_run:
                        lines.append(f"watchdog: would restart {unit} from {source.label}'s recovery record")
                        continue
                    if "dropped" in outage or not Path(source.session["cwd"]).is_dir():
                        if source.blocked and not source.restart_claim:
                            continue
                        outage["dropped"] = f"folder {source.session['cwd']} no longer exists; restore dropped"
                        text = f"watchdog: {unit}: {outage['dropped']}"
                        lines.append(text)
                        lines.append(showrunners.tell(runner["slug"], f"unit-watchdog-{key}-{now.isoformat()}", text))
                        _alert_failure(source, outage, outage["dropped"])
                        continue
                    job = outage.get("job")
                    if job is not None:
                        outcome = _recovery_result(job, now)
                        if isinstance(outcome, RecoveryStillRunning):
                            continue
                        failure = outcome.failure
                        # A finished job can own the source's restart claim.
                        owns_claim = source.restart_claim and (job.get("source") == source.identity or source.snapshot_restore)
                        if (source.blocked and not owns_claim) or _held(source, dry_run=False):
                            outcome.result_path.unlink(missing_ok=True)
                            del retained[key]
                            continue
                        restored = _live_account(doc, unit)
                        # Keep the result and job if the fresh read is unreadable.
                        outcome.result_path.unlink(missing_ok=True)
                        _ = outage.pop("job")
                        since = datetime.fromisoformat(outage["first_seen"]).astimezone(user_zone()).strftime("%H:%M %Z")
                        text = (f"watchdog: {unit} was down since {since}; restarted on {restored}" if restored else
                            f"watchdog: {unit} was down since {since}; {failure or 'restart did not make the session live'}; "
                            + f"retry from {source.label}'s recovery record")
                        lines.append(text)
                        lines.append(showrunners.tell(runner["slug"], f"unit-watchdog-{key}-{now.isoformat()}", text))
                        if restored:
                            del retained[key]
                            _ = state.pop(key, None)
                            continue
                        _alert_failure(source, outage, failure or "restart did not make the session live")
                    elif source.blocked or _held(source, dry_run=dry_run):
                        continue
                    if "recovery" in outage and not source.move_record and not source.snapshot_restore:
                        recovery = outage["recovery"]
                        candidate = recovery["sessions"][0]
                        if candidate["agent"] == "claude" and not candidate.get("crash"):
                            evidence = restart.watchdog_crash_signal(source.session)
                            if not isinstance(evidence, restart.AgentCrashConfirmed):
                                lines.append(f"watchdog: {unit}: waiting for readable crash evidence")
                                continue
                            if not shutdown_record.restore_snapshot_agent_exited(recovery, candidate):
                                continue
                            candidate["crash"] = "agent exited"
                    remaining = dispatch_deadline - time.monotonic()
                    if remaining <= 0:
                        continue
                    results = _state_file().parent / "unit-watchdog-results"
                    results.mkdir(parents=True, exist_ok=True)
                    job = RecoveryJob(result=str(results / (uuid.uuid4().hex + ".json")), started=now.isoformat(), source=source.identity)
                    outage["job"] = job
                    outage["alert_context"] = RecoveryAlertContext(production=runner["slug"], unit=unit,
                        account=source.label, session_name=source.session["name"])
                    # Save ownership before detaching; a later pass never starts a second copy.
                    _write_state({**state, **retained})
                    _start_recovery(source, job, min(5, remaining))
            except (OSError, ValueError) as error:
                # Do not lose an outage's alert mark when discovery is incomplete.
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
