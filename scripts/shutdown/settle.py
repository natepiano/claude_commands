#!/usr/bin/env python3
"""Settle every live Claude session of one account before it is stopped."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production", "hooks"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import Account
import conversation_pause
from inventory import Inventory, Session, inventory, parse_inventory
from record import (
    Conductor,
    Entry,
    NoFooter,
    NoLiveRecord,
    Record,
    Scope,
    ShowrunnerFooter,
    ShutdownInProgress,
    TimerRestore,
    archive,
    create,
    find_live,
    live_records,
    parse_records,
    update,
)
from remote import other_machine, run_remote
import showrunner_footer

SHUTDOWN = Path(__file__).resolve().parent / "shutdown.py"
NOTIFIER = SCRIPTS / "message" / "notifier.sh"
SEND = SCRIPTS / "message" / "send.py"
SETTLE_INTERVAL_SECONDS = 15
SETTLE_RETRY_SECONDS = 300
FIRST_HOLDOUT_ALERT = timedelta(minutes=20)
REPEAT_HOLDOUT_ALERT = timedelta(minutes=60)


class RemoteUnavailable(TypedDict):
    kind: Literal["unavailable"]
    machine: str
    rc: int


class RemoteUnreachable(TypedDict):
    kind: Literal["unreachable"]
    machine: str


RemoteFailure = RemoteUnavailable | RemoteUnreachable


class CountsReady(TypedDict):
    kind: Literal["counts ready"]


class Holdout(TypedDict):
    kind: Literal["holdout"]
    line: str


ReadinessVerdict = CountsReady | Holdout


class EntryVerdict(TypedDict):
    session_id: str
    verdict: ReadinessVerdict


class RefreshReport(TypedDict):
    record: Record
    verdicts: list[EntryVerdict]


class RemoteRefresh(TypedDict):
    kind: Literal["refresh"]
    report: RefreshReport


RemoteRefreshOutcome = RemoteRefresh | RemoteFailure


class ReadinessClear(TypedDict):
    kind: Literal["clear"]


class ReadinessBlocked(TypedDict):
    kind: Literal["blocked"]
    reason: str


ReadinessCheck = ReadinessClear | ReadinessBlocked


class PreflightReady(TypedDict):
    kind: Literal["ready"]
    reports: list[Inventory]


PreflightOutcome = PreflightReady | RemoteFailure


class RemoteRecordFound(TypedDict):
    kind: Literal["live"]
    record: Record


class NoRemoteRecord(TypedDict):
    kind: Literal["no shutdown"]


RemoteRecordOutcome = RemoteRecordFound | NoRemoteRecord | RemoteFailure


class MessageSent(TypedDict):
    kind: Literal["sent"]


class MessageQueued(TypedDict):
    kind: Literal["queued"]
    reason: str


MessageDelivery = MessageSent | MessageQueued


def now_utc() -> datetime:
    """Return canonical wall time; tests replace this one clock seam."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp() -> str:
    return now_utc().isoformat(timespec="seconds")


def _pacific(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(
        ZoneInfo("America/Los_Angeles")
    ).strftime("%Y-%m-%d %H:%M %Z")


def _machine() -> str:
    name = socket.gethostname().split(".", 1)[0]
    return name or ("mac" if sys.platform == "darwin" else "natedev")


def _scope(only: Iterable[str]) -> Scope:
    session_ids = list(dict.fromkeys(item for item in only if item))
    if session_ids:
        return {"kind": "selected", "session_ids": session_ids}
    return {"kind": "all account sessions"}


def _only(scope: Scope) -> frozenset[str]:
    if scope["kind"] == "selected":
        return frozenset(scope["session_ids"])
    return frozenset()


def run_inventory(login: str, scope: Scope) -> Inventory:
    """Inventory one account under the shutdown record's immutable scope."""
    return inventory(login, _only(scope))


def _notifier_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_NOTIFIER")
    return [override] if override else ["zsh", str(NOTIFIER)]


def _send_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_SEND")
    return [override] if override else [sys.executable, str(SEND)]


def run_notifier(verb: str, instance: str) -> None:
    result = subprocess.run(
        [*_notifier_command(), verb, instance],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).splitlines()
        raise RuntimeError(
            reason[0] if reason else f"notifier {verb} {instance} exited {result.returncode}"
        )


def send_message(
    recipient: str,
    summary: str,
    text: str,
    *,
    need: Literal["note", "decision", "blocked"] = "note",
) -> MessageDelivery:
    command = [
        *_send_command(),
        "--to",
        recipient,
        "--from",
        "shutdown",
        "--summary",
        summary,
    ]
    if recipient == "user":
        command.extend(("--need", need))
    result = subprocess.run(
        command,
        input=text,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode == 0:
        return {"kind": "sent"}
    if result.returncode == 1:
        reason = next(
            (
                line.removeprefix("QUEUED: ")
                for line in result.stdout.splitlines()
                if line.startswith("QUEUED: ")
            ),
            "exit 1",
        )
        return {"kind": "queued", "reason": reason}
    reason = (result.stderr or result.stdout).splitlines()
    raise RuntimeError(
        reason[0] if reason else f"send to {recipient} exited {result.returncode}"
    )


def _notifier_enabled(instance: str) -> bool:
    root = Path(
        os.environ.get(
            "NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")
        )
    )
    try:
        return "ENABLED=1" in (root / instance / "state").read_text(
            encoding="utf-8"
        ).splitlines()
    except (OSError, UnicodeError):
        return False


def _restore_footer(slug: str) -> None:
    showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.ON)


def _pause_timers(session_id: str) -> list[TimerRestore]:
    released = conversation_pause.release(session_id)
    if isinstance(released, conversation_pause.NoPauseRecord):
        return []
    footers = set(released.footers)
    restore = not isinstance(released.phase, conversation_pause.KeptOff)
    timers: list[TimerRestore] = []
    for instance in released.instances:
        slug = instance.removeprefix("showrunner-")
        footer: ShowrunnerFooter | NoFooter
        if restore and instance.startswith("showrunner-") and slug in footers:
            footer = {"kind": "footer", "slug": slug}
            footers.remove(slug)
        else:
            footer = {"kind": "no footer"}
        timers.append(
            {
                "instance": instance,
                "was_enabled": restore,
                "footer": footer,
            }
        )
    for slug in sorted(footers):
        instance = f"showrunner-{slug}"
        timers.append(
            {
                "instance": instance,
                "was_enabled": _notifier_enabled(instance),
                "footer": (
                    {"kind": "footer", "slug": slug}
                    if restore
                    else {"kind": "no footer"}
                ),
            }
        )
    return timers


def _session_timer_snapshot(session: Session) -> list[TimerRestore]:
    timers = _pause_timers(session["session_id"])
    recorded = {timer["instance"] for timer in timers}
    for instance in session["timers"]:
        if instance in recorded:
            continue
        enabled = _notifier_enabled(instance)
        timers.append(
            {
                "instance": instance,
                "was_enabled": enabled,
                "footer": {"kind": "no footer"},
            }
        )
    return timers


def _stop_recorded_timers(timers: Iterable[TimerRestore]) -> None:
    for timer in timers:
        if timer["was_enabled"]:
            run_notifier("stop", timer["instance"])


def _entry(session: Session) -> Entry:
    return {
        "session": session,
        "timers": [],
        "settle_message": {"kind": "not sent"},
        "where": {"kind": "not said"},
        "progress": {"kind": "waiting"},
    }


def begin(
    login: str,
    requested_by: str | None = None,
    only: Iterable[str] = (),
) -> Record:
    """Create one machine's settling record and stop only its recorded timers."""
    scope = _scope(only)
    report = run_inventory(login, scope)
    requested_at = _timestamp()
    record: Record = {
        "login": login,
        "label": report["label"],
        "machine": report["machine"],
        "state": "settling",
        "requested_at": requested_at,
        "requested_by": (
            {"kind": "session", "session_id": requested_by}
            if requested_by
            else {"kind": "terminal"}
        ),
        "scope": scope,
        "conductor": {"kind": "not started"},
        "force": "wait for ready",
        "entries": [_entry(session) for session in report["sessions"]],
    }
    create(record)
    try:
        for original in record["entries"]:
            session_id = original["session"]["session_id"]
            timers = _session_timer_snapshot(original["session"])
            stop_failures: list[Exception] = []

            def save_timers(current: Record, *, wanted: str = session_id) -> None:
                entry = next(
                    item
                    for item in current["entries"]
                    if item["session"]["session_id"] == wanted
                )
                entry["timers"] = timers
                try:
                    _stop_recorded_timers(timers)
                except Exception as error:
                    stop_failures.append(error)

            record = update(login, save_timers)
            if stop_failures:
                raise stop_failures[0]
    except Exception:
        _ = _cancel_local(login)
        raise
    return record


def merge_in_progress(session: Session) -> bool:
    if session["kind"] != "showrunner" or session["checkout"]["kind"] != "git":
        return False
    try:
        result = subprocess.run(
            (
                "git",
                "-C",
                session["cwd"],
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _blocker(session: Session) -> ReadinessCheck:
    checkout = session["checkout"]
    if (
        checkout["kind"] == "git"
        and checkout["upstream"]["kind"] == "tracking"
        and checkout["upstream"]["ahead"] > 0
    ):
        head = checkout["head"]
        branch = head["name"] if head["kind"] == "branch" else head["commit"]
        return {"kind": "blocked", "reason": f"push {branch} first"}
    for server in session["codex_servers"]:
        if server["busy_seats"]:
            return {
                "kind": "blocked",
                "reason": f"Codex seat {server['busy_seats'][0]} is still running",
            }
    if merge_in_progress(session):
        return {"kind": "blocked", "reason": "finish the merge in progress first"}
    return {"kind": "clear"}


def _counts_ready(entry: Entry, current: Session) -> bool:
    progress = entry["progress"]["kind"]
    if progress in {"already gone", "stopped", "process identity lost"}:
        return True
    if current["kind"] == "seat":
        return progress == "passive seat ready"
    return progress == "ready" and _blocker(current)["kind"] == "clear"


def _unattributed_reasons(report: Inventory) -> dict[int, str]:
    return {item["pid"]: item["reason"] for item in report["unattributed"]}


def _refresh_entries(record: Record, report: Inventory) -> list[TimerRestore]:
    current = {session["session_id"]: session for session in report["sessions"]}
    unattributed = _unattributed_reasons(report)
    entries = {
        entry["session"]["session_id"]: entry for entry in record["entries"]
    }
    timers_to_stop: list[TimerRestore] = []
    returned: set[str] = set()
    for session_id, session in current.items():
        entry = entries.get(session_id)
        if entry is None:
            new_entry = _entry(session)
            new_entry["timers"] = _session_timer_snapshot(session)
            timers_to_stop.extend(new_entry["timers"])
            record["entries"].append(new_entry)
        else:
            entry["session"] = session
            if entry["progress"]["kind"] == "already gone":
                entry["progress"] = {"kind": "waiting"}
                returned.add(session_id)

    for entry in record["entries"]:
        session = entry["session"]
        if session["session_id"] in current or session["pid"] in unattributed:
            continue
        if entry["progress"]["kind"] != "already gone":
            entry["progress"] = {"kind": "already gone", "at": _timestamp()}

    entries = {
        entry["session"]["session_id"]: entry for entry in record["entries"]
    }
    for entry in record["entries"]:
        session = entry["session"]
        if session["kind"] != "seat":
            continue
        if session["session_id"] in returned:
            continue
        if (
            session["session_id"] not in current
            and session["pid"] in unattributed
        ):
            continue
        live_seat = current.get(session["session_id"])
        owner = session["owner"]
        owner_ready = owner["kind"] == "no director"
        if owner["kind"] == "director":
            owner_entry = entries.get(owner["session_id"])
            live_owner = current.get(owner["session_id"])
            if owner_entry is None:
                owner_ready = True
            elif live_owner is None:
                owner_ready = (
                    owner_entry["session"]["pid"] not in unattributed
                    and owner_entry["progress"]["kind"] == "already gone"
                )
            else:
                owner_ready = _counts_ready(owner_entry, live_owner)
        seat_idle = live_seat is not None and live_seat["status"] == "idle"
        if owner_ready and seat_idle:
            if entry["progress"]["kind"] != "passive seat ready":
                entry["progress"] = {
                    "kind": "passive seat ready",
                    "at": _timestamp(),
                }
        elif entry["progress"]["kind"] == "passive seat ready":
            entry["progress"] = {"kind": "waiting"}
    return timers_to_stop


def _settle_text(record: Record, session: Session) -> str:
    opening = (
        f"Shutdown of account {record['label']} requested by the user with /shutdown "
        f"({_pacific(record['requested_at'])}). "
    )
    command = (
        "`~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py "
        "ready --where "
    )
    if session["kind"] == "unit":
        return opening + (
            "Reach a safe stop: let any seat turn already running finish, dispatch "
            "nothing new, commit nothing new, push your branch if it has unpushed "
            f"commits, and leave uncommitted work in place. Then run {command}"
            '"<phase N: the step you finished and the step that comes next>"` and '
            "end your turn with `— blocked: shutdown requested by the user`. You "
            "will be resumed with that line."
        )
    if session["kind"] == "showrunner":
        return opening + (
            "Merge any checkpoint already sent to you, push the merge branch and main "
            "as your production rules say, append a `### STATE` block to your LOG, "
            f'then run {command}"<one line>"` and end your turn.'
        )
    return opening + (
        "Finish the turn you are in, start nothing new, push any commits you made "
        "that are ahead of their upstream, then run "
        f'{command}"<what you were doing and what comes next>"` and end your turn.'
    )


def _send_settle_message(record: Record, entry: Entry) -> None:
    session = entry["session"]
    delivery = send_message(
        f"session:{session['session_id']}",
        f"Shutdown of {record['label']}: reach a safe stop",
        _settle_text(record, session),
    )
    at = _timestamp()
    if delivery["kind"] == "sent":
        entry["settle_message"] = {"kind": "sent", "at": at}
    else:
        entry["settle_message"] = {
            "kind": "queued",
            "at": at,
            "reason": delivery["reason"],
        }


def _settle_message_due(entry: Entry, current: datetime) -> bool:
    message = entry["settle_message"]
    if message["kind"] == "not sent":
        return True
    if message["kind"] == "sent":
        return False
    queued_at = datetime.fromisoformat(message["at"])
    return current - queued_at >= timedelta(seconds=SETTLE_RETRY_SECONDS)


def _readiness_verdicts(record: Record, report: Inventory) -> list[EntryVerdict]:
    current = {session["session_id"]: session for session in report["sessions"]}
    unattributed = _unattributed_reasons(report)
    verdicts: list[EntryVerdict] = []
    for entry in record["entries"]:
        stored = entry["session"]
        session_id = stored["session_id"]
        if session_id in current:
            session = current[session_id]
            verdict: ReadinessVerdict = (
                {"kind": "counts ready"}
                if _counts_ready(entry, session)
                else {"kind": "holdout", "line": _holdout_line(record, session)}
            )
        elif stored["pid"] in unattributed:
            verdict = {
                "kind": "holdout",
                "line": (
                    f"{record['machine']} {stored['kind']} {stored['name']}: "
                    f"{unattributed[stored['pid']]}"
                ),
            }
        elif entry["progress"]["kind"] in {
            "already gone",
            "stopped",
            "process identity lost",
        }:
            verdict = {"kind": "counts ready"}
        else:
            verdict = {
                "kind": "holdout",
                "line": f"{record['machine']} {stored['kind']} {stored['name']}: not found",
            }
        verdicts.append({"session_id": session_id, "verdict": verdict})
    return verdicts


def refresh(login: str, message: Iterable[str] = ()) -> RefreshReport:
    """Refresh one record, stop new timers, advance seats, and send selected notices."""
    found = find_live(login)
    if found["kind"] == "no shutdown":
        raise NoLiveRecord(f"no live shutdown for {login}")
    if found["record"]["state"] != "settling":
        raise NoLiveRecord(f"shutdown for {login} is no longer settling")
    report = run_inventory(login, found["record"]["scope"])
    message_ids = frozenset(message)
    fresh_ids = frozenset(session["session_id"] for session in report["sessions"])
    current_time = now_utc()
    timers_to_stop: list[TimerRestore] = []
    stop_failures: list[Exception] = []

    def apply(current: Record) -> None:
        if current["state"] != "settling":
            return
        timers_to_stop.extend(_refresh_entries(current, report))
        try:
            _stop_recorded_timers(timers_to_stop)
        except Exception as error:
            stop_failures.append(error)
        for entry in current["entries"]:
            session = entry["session"]
            if (
                session["session_id"] in message_ids
                and session["session_id"] in fresh_ids
                and session["kind"] != "seat"
                and _settle_message_due(entry, current_time)
            ):
                _send_settle_message(current, entry)

    current = update(login, apply)
    if current["state"] != "settling":
        raise NoLiveRecord(f"shutdown for {login} is no longer settling")
    if stop_failures:
        raise stop_failures[0]
    return {"record": current, "verdicts": _readiness_verdicts(current, report)}


def ready(where: str) -> int:
    """Mark the calling session ready after checking its fresh checkout and seats."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for live in live_records():
        if live["state"] != "settling":
            continue
        for original in live["entries"]:
            if original["session"]["session_id"] != session_id:
                continue
            report = run_inventory(live["login"], live["scope"])
            fresh = {
                item["session_id"]: item for item in report["sessions"]
            }
            if session_id not in fresh:
                reasons = _unattributed_reasons(report)
                reason = reasons.get(original["session"]["pid"], "not found")
                print(
                    f"this session is not in a fresh inventory of {live['label']}: {reason}",
                    file=sys.stderr,
                )
                return 2
            session = fresh[session_id]
            if session["kind"] == "seat":
                print(
                    "a seat is ready on its own once its director is ready and it is idle",
                    file=sys.stderr,
                )
                return 2
            blocker = _blocker(session)
            if blocker["kind"] == "blocked":
                print(blocker["reason"], file=sys.stderr)
                return 2
            at = _timestamp()
            accepted = False

            def mark_ready(current: Record) -> None:
                nonlocal accepted
                if current["state"] != "settling":
                    return
                entry = next(
                    item
                    for item in current["entries"]
                    if item["session"]["session_id"] == session_id
                )
                entry["session"] = session
                entry["where"] = {"kind": "said", "text": where, "at": at}
                entry["progress"] = {"kind": "ready", "at": at}
                accepted = True

            _ = update(live["login"], mark_ready)
            if not accepted:
                print("no shutdown in progress for this session", file=sys.stderr)
                return 1
            print(f"ready for shutdown: {where}")
            return 0
    print("no shutdown in progress for this session", file=sys.stderr)
    return 1


def _message_due(
    reports: list[RefreshReport], *, allow_showrunners: bool
) -> dict[str, list[str]]:
    ready_by_id: dict[str, bool] = {}
    for report in reports:
        for result in report["verdicts"]:
            ready_by_id[result["session_id"]] = (
                result["verdict"]["kind"] == "counts ready"
            )

    unit_ready_by_production: dict[str, list[bool]] = {}
    for report in reports:
        record = report["record"]
        for entry in record["entries"]:
            session = entry["session"]
            if session["kind"] != "unit":
                continue
            unit_ready_by_production.setdefault(
                session["host"]["production"], []
            ).append(ready_by_id[session["session_id"]])

    due: dict[str, list[str]] = {}
    for report in reports:
        record = report["record"]
        requesting = (
            record["requested_by"]["session_id"]
            if record["requested_by"]["kind"] == "session"
            else ""
        )
        for entry in record["entries"]:
            session = entry["session"]
            if (
                session["kind"] == "seat"
                or ready_by_id[session["session_id"]]
                or session["session_id"] == requesting
                or entry["settle_message"]["kind"] == "sent"
            ):
                continue
            if session["kind"] == "showrunner":
                if not allow_showrunners or not all(
                    unit_ready_by_production.get(session["production"], [])
                ):
                    continue
            due.setdefault(record["machine"], []).append(session["session_id"])
    return due


def _parse_refresh_report(text: str) -> RefreshReport:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ValueError("remote refresh did not return one report")
    items = cast(list[object], value)
    if len(items) != 1:
        raise ValueError("remote refresh did not return one report")
    raw_report = items[0]
    if not isinstance(raw_report, dict):
        raise ValueError("remote refresh report is not an object")
    fields = cast(dict[str, object], raw_report)
    record = parse_records(json.dumps([fields.get("record")]))[0]
    raw_verdicts = fields.get("verdicts")
    if not isinstance(raw_verdicts, list):
        raise ValueError("remote refresh verdicts are not a list")
    verdicts: list[EntryVerdict] = []
    for raw_verdict in cast(list[object], raw_verdicts):
        if not isinstance(raw_verdict, dict):
            raise ValueError("remote entry verdict is not an object")
        verdict_fields = cast(dict[str, object], raw_verdict)
        session_id = verdict_fields.get("session_id")
        raw_result = verdict_fields.get("verdict")
        if not isinstance(session_id, str) or not isinstance(raw_result, dict):
            raise ValueError("remote entry verdict is invalid")
        result_fields = cast(dict[str, object], raw_result)
        kind = result_fields.get("kind")
        if kind == "counts ready":
            verdict: ReadinessVerdict = {"kind": "counts ready"}
        elif kind == "holdout" and isinstance(result_fields.get("line"), str):
            verdict = {
                "kind": "holdout",
                "line": cast(str, result_fields["line"]),
            }
        else:
            raise ValueError("remote entry verdict has an invalid result")
        verdicts.append({"session_id": session_id, "verdict": verdict})
    expected = [
        entry["session"]["session_id"] for entry in record["entries"]
    ]
    if [item["session_id"] for item in verdicts] != expected:
        raise ValueError("remote refresh verdicts do not match its record")
    return {"record": record, "verdicts": verdicts}


def _remote_record(
    login: str, message: Iterable[str] = ()
) -> RemoteRefreshOutcome:
    arguments = ["refresh", login]
    ids = list(message)
    if ids:
        arguments.extend(("--message", *ids))
    status, output = run_remote(arguments)
    machine = other_machine()
    if status == 255:
        return {"kind": "unreachable", "machine": machine}
    if status != 0:
        return {"kind": "unavailable", "machine": machine, "rc": status}
    try:
        report = _parse_refresh_report(output)
    except (TypeError, ValueError):
        return {"kind": "unavailable", "machine": machine, "rc": 1}
    if report["record"]["login"] != login:
        return {"kind": "unavailable", "machine": machine, "rc": 1}
    return {"kind": "refresh", "report": report}


def _showing_form(session: Session) -> bool:
    host = session["host"]
    if host["kind"] != "tmux" and host["kind"] != "unit":
        return False
    tmux = os.environ.get("SHUTDOWN_TMUX", "tmux")
    try:
        result = subprocess.run(
            (tmux, "capture-pane", "-pt", host["tmux_session"]),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    pane = result.stdout.casefold()
    return result.returncode == 0 and ("form" in pane or "esc to cancel" in pane)


def _holdout_line(
    record: Record,
    session: Session,
) -> str:
    details = [session["status"]]
    if session["status"] != "busy" and any(
        server["busy_seats"] for server in session["codex_servers"]
    ):
        details.append("busy")
    if _showing_form(session):
        details.append("showing a form")
    checkout = session["checkout"]
    if checkout["kind"] == "git" and checkout["upstream"]["kind"] == "tracking":
        ahead = checkout["upstream"]["ahead"]
        if ahead:
            head = checkout["head"]
            branch = head["name"] if head["kind"] == "branch" else head["commit"]
            details.append(f"ahead {ahead} on {branch}")
    if merge_in_progress(session):
        details.append("merge in progress")
    return (
        f"{record['machine']} {session['kind']} {session['name']}: "
        + ", ".join(details)
    )


def _holdouts(reports: list[RefreshReport]) -> list[str]:
    return [
        result["verdict"]["line"]
        for report in reports
        for result in report["verdicts"]
        if result["verdict"]["kind"] == "holdout"
    ]


def _send_holdout_alert(
    reports: list[RefreshReport], unreached_since: dict[str, datetime]
) -> None:
    holdouts = _holdouts(reports)
    lines = list(holdouts)
    for machine, since in sorted(unreached_since.items()):
        lines.append(
            f"{machine}: not reached since "
            + since.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
        )
    lines.extend(
        (
            "/shutdown now stops them anyway",
            "/shutdown cancel undoes the shutdown",
        )
    )
    label = reports[0]["record"]["label"]
    text = "\n".join(lines)
    summary = f"Shutdown of {label}: {len(holdouts)} not ready"
    _ = send_message("user", summary, text, need="decision")
    requesters: set[str] = set()
    for report in reports:
        requested_by = report["record"]["requested_by"]
        if requested_by["kind"] == "session":
            requesters.add(requested_by["session_id"])
    for session_id in sorted(requesters):
        _ = send_message(f"session:{session_id}", summary, text)


def conduct_cycle(
    login: str, unreached_since: dict[str, datetime], here: bool = False
) -> tuple[bool, list[RefreshReport]]:
    """Run one deterministic conductor pass; return whether all work is ready."""
    try:
        local = refresh(login)
    except NoLiveRecord:
        return True, []
    reports = [local]
    if not here:
        outcome = _remote_record(login)
        if outcome["kind"] == "unavailable":
            _ = unreached_since.setdefault(outcome["machine"], now_utc())
        elif outcome["kind"] == "unreachable":
            _ = unreached_since.setdefault(outcome["machine"], now_utc())
        else:
            reports.append(outcome["report"])
            _ = unreached_since.pop(outcome["report"]["record"]["machine"], None)

    due = _message_due(reports, allow_showrunners=not unreached_since)
    local_due = due.get(local["record"]["machine"], [])
    if local_due:
        reports[0] = refresh(login, local_due)
    if len(reports) > 1:
        remote_report = reports[1]
        remote_due = due.get(remote_report["record"]["machine"], [])
        if remote_due:
            outcome = _remote_record(login, remote_due)
            if outcome["kind"] == "unavailable":
                _ = unreached_since.setdefault(outcome["machine"], now_utc())
                _ = reports.pop()
            elif outcome["kind"] == "unreachable":
                _ = unreached_since.setdefault(outcome["machine"], now_utc())
                _ = reports.pop()
            else:
                reports[1] = outcome["report"]

    if unreached_since:
        return False, reports
    return (
        all(
            result["verdict"]["kind"] == "counts ready"
            for report in reports
            for result in report["verdicts"]
        ),
        reports,
    )


def conduct(login: str, here: bool = False) -> int:
    """Coordinate both machines until every session has reached a safe stop."""
    unreached_since: dict[str, datetime] = {}
    last_alert = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        complete, reports = conduct_cycle(login, unreached_since, here)
        if not reports:
            return 0
        if complete:
            return 0
        requested_at = datetime.fromisoformat(
            reports[0]["record"]["requested_at"]
        )
        current = now_utc()
        if (
            current - requested_at >= FIRST_HOLDOUT_ALERT
            and current - last_alert >= REPEAT_HOLDOUT_ALERT
        ):
            _send_holdout_alert(reports, unreached_since)
            last_alert = current
        time.sleep(SETTLE_INTERVAL_SECONDS)


def _restore_record(record: Record) -> None:
    for entry in record["entries"]:
        for timer in entry["timers"]:
            if timer["was_enabled"]:
                run_notifier("start", timer["instance"])
            footer = timer["footer"]
            if footer["kind"] == "footer":
                _restore_footer(footer["slug"])
        if entry["settle_message"]["kind"] in {"sent", "queued"}:
            _ = send_message(
                f"session:{entry['session']['session_id']}",
                f"Shutdown of {record['label']} cancelled",
                f"Shutdown of {record['label']} cancelled by the user: continue where you were.",
            )


def _stop_conductor(conductor: Conductor) -> None:
    if conductor["kind"] == "systemd":
        _ = subprocess.run(
            ("systemctl", "--user", "stop", conductor["unit"]),
            check=False,
            timeout=15,
        )
    elif conductor["kind"] == "launchd":
        _ = subprocess.run(
            ("launchctl", "remove", conductor["label"]),
            check=False,
            timeout=15,
        )


def _cancel_local(login: str) -> bool:
    found = find_live(login)
    if found["kind"] == "no shutdown":
        return False
    _stop_conductor(found["record"]["conductor"])

    def mark_cancelled(current: Record) -> None:
        current["state"] = "cancelled"

    cancelled = update(login, mark_cancelled)
    _restore_record(cancelled)
    archive(login)
    return True


def cancel(account: Account, here: bool = False) -> int:
    """Undo a settling shutdown everywhere reachable and archive its records."""
    _ = _cancel_local(account.login)
    if here:
        return 0
    status, _ = run_remote(["cancel", account.login, "--here"])
    if status != 0:
        machine = other_machine()
        print(
            f"{machine} not reached: run /shutdown cancel there when it is back",
            file=sys.stderr,
        )
        return 1
    return 0


def _inventory_text(report: Inventory) -> str:
    lines = [f"{report['machine']}:"]
    for session in report["sessions"]:
        lines.append(
            f"  {session['kind']} {session['name']} · {session['status']}"
        )
    lines.extend(
        f"  unknown account · {item['pid']} {item['name']} · {item['reason']}"
        for item in report["unattributed"]
    )
    if not report["sessions"] and not report["unattributed"]:
        lines.append(f"  no sessions on {report['label']}")
    return "\n".join(lines)


def _remote_failure(machine: str, status: int) -> str:
    state = "unreachable" if status == 255 else f"unavailable (rc {status})"
    return (
        f"shutdown: {machine} is {state}: nothing was shut down; "
        "/shutdown --here shuts down only this machine"
    )


def _failure_text(failure: RemoteFailure) -> str:
    status = 255 if failure["kind"] == "unreachable" else failure["rc"]
    return _remote_failure(failure["machine"], status)


def _preflight_inventories(
    account: Account, here: bool, scope: Scope
) -> PreflightOutcome:
    reports = [run_inventory(account.login, scope)]
    if here:
        return {"kind": "ready", "reports": reports}
    status, output = run_remote(["status", account.login, "--json", "--here"])
    machine = other_machine()
    if status == 255:
        return {"kind": "unreachable", "machine": machine}
    if status != 0:
        return {"kind": "unavailable", "machine": machine, "rc": status}
    try:
        reports.append(parse_inventory(output))
    except ValueError:
        return {"kind": "unavailable", "machine": machine, "rc": status or 1}
    return {"kind": "ready", "reports": reports}


def _live_on_other(login: str) -> RemoteRecordOutcome:
    status, output = run_remote(["records", "--json", "--here"])
    machine = other_machine()
    if status == 255:
        return {"kind": "unreachable", "machine": machine}
    if status != 0:
        return {"kind": "unavailable", "machine": machine, "rc": status}
    try:
        records = parse_records(output)
    except ValueError:
        return {"kind": "unavailable", "machine": machine, "rc": status or 1}
    for record in records:
        if record["login"] == login:
            return {"kind": "live", "record": record}
    return {"kind": "no shutdown"}


def _warm() -> bool:
    try:
        result = subprocess.run(
            ("github-warm-status",),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _launch_conductor(login: str, label: str, here: bool) -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "account"
    service = f"shutdown-{slug}-{os.getpid()}"
    command = [sys.executable, str(SHUTDOWN), "conduct", login]
    if here:
        command.append("--here")
    if sys.platform == "darwin":
        launch = ["launchctl", "submit", "-l", service, "--", *command]
        conductor: Conductor = {"kind": "launchd", "label": service}
    else:
        launch = [
            "systemd-run",
            "--user",
            "--collect",
            "--quiet",
            "--no-block",
            "--unit",
            service,
            *command,
        ]
        conductor = {"kind": "systemd", "unit": service}
    result = subprocess.run(launch, check=False, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"could not start shutdown conductor {service}")

    def save(current: Record) -> None:
        current["conductor"] = conductor

    _ = update(login, save)


def down(account: Account, here: bool = False, only: Iterable[str] = ()) -> int:
    """Start an account-scoped shutdown after both machines pass preflight."""
    if not _warm():
        print(
            f"GitHub keys are cold: run `github-warmup` in a terminal on {_machine()}, then `/shutdown` again",
            file=sys.stderr,
        )
        return 1
    ids = list(only)
    scope = _scope(ids)
    preflight = _preflight_inventories(account, here, scope)
    if preflight["kind"] == "unavailable":
        print(_failure_text(preflight), file=sys.stderr)
        return 1
    if preflight["kind"] == "unreachable":
        print(_failure_text(preflight), file=sys.stderr)
        return 1
    reports = preflight["reports"]
    local_live = find_live(account.login)
    if local_live["kind"] == "live":
        record = local_live["record"]
        print(f"shutdown: {record['machine']} is already {record['state']}")
        return 1
    if not here:
        remote = _live_on_other(account.login)
        if remote["kind"] == "unavailable":
            print(_failure_text(remote), file=sys.stderr)
            return 1
        if remote["kind"] == "unreachable":
            print(_failure_text(remote), file=sys.stderr)
            return 1
        if remote["kind"] == "live":
            print(
                f"shutdown: {remote['record']['machine']} is already {remote['record']['state']}"
            )
            return 1
    print(f"Account: {account.label} ({account.login})")
    print("\n".join(_inventory_text(report) for report in reports))
    requested_by = os.environ.get("CLAUDE_CODE_SESSION_ID")
    try:
        _ = begin(account.login, requested_by, ids)
    except ShutdownInProgress as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 1
    if not here:
        arguments = ["begin", account.login]
        if requested_by:
            arguments.extend(("--requested-by", requested_by))
        if ids:
            arguments.extend(("--only", ",".join(ids)))
        status, _ = run_remote(arguments)
        if status != 0:
            _ = _cancel_local(account.login)
            print(
                f"shutdown: begin failed on {other_machine()}: nothing was shut down",
                file=sys.stderr,
            )
            return 1
    try:
        _launch_conductor(account.login, account.label, here)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        _ = _cancel_local(account.login)
        if not here:
            _ = run_remote(["cancel", account.login, "--here"])
        print(
            "shutdown: could not start the conductor: nothing was shut down",
            file=sys.stderr,
        )
        return 1
    print(
        f"shutdown of {account.label} started; /shutdown status to watch, /shutdown cancel to undo"
    )
    return 0


def status_record_lines(record: Record) -> list[str]:
    """Render the settling state appended to user-facing status output."""
    lines = [f"{record['machine']}: shutdown {record['state']}"]
    if record["scope"]["kind"] == "selected":
        lines.append("  selected: " + ", ".join(record["scope"]["session_ids"]))
    for entry in record["entries"]:
        progress = entry["progress"]["kind"]
        if progress == "ready" and entry["where"]["kind"] == "said":
            progress += f" ({entry['where']['text']})"
        settle_message = entry["settle_message"]
        if settle_message["kind"] == "sent":
            message = " · message sent"
        elif settle_message["kind"] == "queued":
            message = f" · message queued: {settle_message['reason']}"
        else:
            message = ""
        lines.append(f"  {entry['session']['name']}: {progress}{message}")
    return lines
