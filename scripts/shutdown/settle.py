#!/usr/bin/env python3
"""Conduct each account session through its own safe stop and optional restore."""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import Account
import conversation_pause
from inventory import Inventory, Session, inventory
import record as store
from record import NoRemoteControl, SessionShutdown, SessionStopIssue, TimerRestore, ShowrunnerFooter, NoFooter, ShutdownContinuation, ReadinessCheck
from remote import other_machine, run_remote
import showrunner_footer
import sessions
import showrunners
import stop as stop_work
from user_action import ActionRequired
from user_zone import user_zone
if TYPE_CHECKING:
    from ..notify import review
else:
    import review

SHUTDOWN = Path(__file__).resolve().parent / "shutdown.py"
NOTIFIER = SCRIPTS / "message" / "notifier.sh"
SEND = SCRIPTS / "message" / "send.py"
SETTLE_INTERVAL_SECONDS = 15
SETTLE_RETRY_SECONDS = 300
# A unit in the middle of /unit:direct answered a pause message in about a minute (2026-10-13).
REASK_AFTER = timedelta(minutes=2)
NOT_ANSWERING_AFTER = 2 * REASK_AFTER
SHOWING_FORM = "showing a form"
USAGE_LIMIT_PATTERNS = (
    "hit your session limit",
    "hit your usage limit",
    "hit your weekly limit",
    "usage limit reached",
    "wait for limit to reset",
)


@dataclass(frozen=True)
class ScreenText:
    text: str


@dataclass(frozen=True)
class ScreenUnreadable:
    reason: Literal["no pane host", "no pane id", "capture failed"]


@dataclass(frozen=True)
class EnterUsageLimitHold:
    """The owner cannot answer the settle request while its limit is shown."""


@dataclass(frozen=True)
class LeaveUsageLimitHold:
    """The owner must answer the settle request after its limit stops showing."""


@dataclass(frozen=True)
class NoUsageLimitChange:
    """This observation does not change the owner's shutdown progress."""


@dataclass(frozen=True)
class UsageLimitChangeRefused:
    """The current request no longer permits the observed transition."""


class _UsageLimitChangeRejected(Exception):
    """Abort a store update before it writes a changed request."""

class MessageSent(TypedDict):
    kind: Literal["sent"]

class MessageQueued(TypedDict):
    kind: Literal["queued"]
    reason: str

MessageDelivery = MessageSent | MessageQueued

@dataclass(frozen=True)
class SendSettleMessage:
    """The owner needs the shutdown request delivered."""

@dataclass(frozen=True)
class StopSession:
    """The owner can attempt its stop now."""

@dataclass(frozen=True)
class RelaunchSession:
    """The stopped owner can begin its restore now."""

@dataclass(frozen=True)
class Hold:
    reason: str

@dataclass(frozen=True)
class Finished:
    """The conductor has no further action for this owner."""

Step = SendSettleMessage | StopSession | RelaunchSession | Hold | Finished


@dataclass(frozen=True)
class _StatusUnobserved:
    """Status has no current observation of this session."""


_STATUS_UNOBSERVED = _StatusUnobserved()

def now_utc() -> datetime:
    """Return canonical wall time; tests patch this one function to fix the clock."""
    return datetime.now(timezone.utc).replace(microsecond=0)

def record_time() -> str:
    """Return a canonical timestamp for a shutdown record field."""
    return now_utc().isoformat(timespec="seconds")

def _user_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(
        user_zone()
    ).strftime("%Y-%m-%d %H:%M %Z")

def _machine() -> str:
    name = socket.gethostname().split(".", 1)[0]
    return name or ("mac" if sys.platform == "darwin" else "natedev")

def local_machine() -> str:
    """Return this machine's shutdown-record name."""
    return _machine()

def _notifier_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_NOTIFIER")
    return [override] if override else ["zsh", str(NOTIFIER)]

def _send_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_SEND")
    return [override] if override else [sys.executable, str(SEND)]

def retire_command(login: str, session_id: str) -> list[str]:
    """Build the one command that removes obsolete shutdown messages."""
    return [
        *_send_command(),
        "retire",
        "--to",
        f"session:{session_id}",
        "--from",
        "shutdown",
        "--key",
        f"shutdown-{login}-{session_id}",
    ]

def _retire_shutdown_messages(login: str, session_id: str) -> None:
    result = subprocess.run(
        retire_command(login, session_id),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).splitlines()
        raise RuntimeError(
            reason[0]
            if reason
            else f"retire for session:{session_id} exited {result.returncode}"
        )

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

def _deliver_message(
    command: list[str], recipient: str, text: str
) -> MessageDelivery:
    timeout = 60.0
    if recipient == "user":
        timeout = review.user_send_seconds() + 15
        if "--machine" in command:
            # send.py gives SSH its user wait plus 10 s termination and 30 s transport time.
            timeout += 40
    try:
        result = subprocess.run(
            command,
            input=text,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"send to {recipient} timed out after {timeout:g} seconds") from error
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

def send_message(
    recipient: str,
    summary: str,
    text: str,
    *,
    machine: str = "",
    key: str = "",
) -> MessageDelivery:
    """Send a shutdown message to a session, never to the user."""
    if recipient == "user":
        raise ValueError("user messages require alert_user")
    command = [
        *_send_command(),
        "--to",
        recipient,
        "--from",
        "shutdown",
        "--summary",
        summary,
    ]
    if key:
        command.extend(("--key", key))
    if machine:
        command.extend(("--machine", machine))
    return _deliver_message(command, recipient, text)


def alert_user(summary: str, text: str, action: ActionRequired, *, machine: str = "") -> None:
    """Send the user an action through the shutdown alert path."""
    command = [*_send_command(), "--to", "user", "--from", "shutdown",
               "--summary", summary, "--need", "decision", "--action", action.text]
    if machine:
        command.extend(("--machine", machine))
    try:
        _ = _deliver_message(command, "user", text)
    except RuntimeError as error:
        print(f"shutdown: user alert failed: {error}", file=sys.stderr)


def alert_details(lines: list[str]) -> str:
    details = "\n".join(lines)
    return details if len(details) <= 180 else details[:177] + "..."

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

def blocker(session: Session) -> ReadinessCheck:
    """Check whether outstanding work prevents this session from stopping."""
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

def _screen(session: Session) -> ScreenText | ScreenUnreadable:
    host = session["host"]
    if host["kind"] == "herdr":
        from session_pane import read_place
        from herdr import HerdrFailure, HerdrPlace

        try:
            return ScreenText(read_place(HerdrPlace(host["herdr_session"], host["pane_id"]), 60))
        except HerdrFailure:
            return ScreenUnreadable("capture failed")
    if host["kind"] != "tmux" and host["kind"] != "unit":
        return ScreenUnreadable("no pane host")
    entry = sessions.read_session(sessions.sessions_dir() / f"{session['pid']}.json")
    if isinstance(entry, sessions.UnreadableSessionRecord) or entry["sessionId"] != session["session_id"]:
        return ScreenUnreadable("no pane id")
    pane_id = entry["tmux"].rsplit(".", 1)[-1]
    if re.fullmatch(r"%[0-9]+", pane_id) is None or "." not in entry["tmux"]:
        return ScreenUnreadable("no pane id")
    tmux = os.environ.get("SHUTDOWN_TMUX", "tmux")
    try:
        result = subprocess.run(
            [tmux, "capture-pane", "-p", "-t", pane_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ScreenUnreadable("capture failed")
    if result.returncode != 0:
        return ScreenUnreadable("capture failed")
    return ScreenText(result.stdout)


def _showing_form(session: Session) -> bool:
    screen = _screen(session)
    if isinstance(screen, ScreenUnreadable):
        return False
    pane = screen.text.casefold()
    return "form" in pane or "esc to cancel" in pane


def _held_by_usage_limit(screen: ScreenText | ScreenUnreadable) -> bool:
    if isinstance(screen, ScreenUnreadable):
        return False
    lines = [line for line in screen.text.splitlines() if line.strip()][-15:]
    return any(pattern in line.casefold() for line in lines for pattern in USAGE_LIMIT_PATTERNS)


def _usage_limit_shown(session: Session) -> bool:
    from scripts.accounts.refusal_watch import session_refusal, TranscriptRefusal
    if session["status"] == "busy":
        return False
    try:
        if isinstance(session_refusal(session["session_id"]), TranscriptRefusal):
            return True
    except (OSError, ValueError):
        pass
    return _held_by_usage_limit(_screen(session))


def _usage_limit_change(
    saved: SessionShutdown, owner: stop_work.FreshSession,
) -> EnterUsageLimitHold | LeaveUsageLimitHold | NoUsageLimitChange:
    if owner["kind"] != "matching session" or saved["force"] != "wait for ready":
        return NoUsageLimitChange()
    progress = saved["progress"]["kind"]
    if progress in {"waiting", "ready"}:
        if _usage_limit_shown(owner["session"]):
            return EnterUsageLimitHold()
    elif progress == "held by usage limit" and not _usage_limit_shown(owner["session"]):
        return LeaveUsageLimitHold()
    return NoUsageLimitChange()


def _apply_usage_limit_change(
    saved: SessionShutdown,
    change: EnterUsageLimitHold | LeaveUsageLimitHold | NoUsageLimitChange,
) -> SessionShutdown | UsageLimitChangeRefused:
    if isinstance(change, NoUsageLimitChange):
        return saved

    def apply(current: SessionShutdown) -> None:
        if current["request_id"] != saved["request_id"] or current["force"] != "wait for ready":
            raise _UsageLimitChangeRejected
        progress = current["progress"]["kind"]
        if isinstance(change, EnterUsageLimitHold) and progress in {"waiting", "ready"}:
            current["progress"] = {"kind": "held by usage limit", "at": record_time()}
        elif isinstance(change, LeaveUsageLimitHold) and progress == "held by usage limit":
            current["progress"] = {"kind": "waiting"}
            current["settle_message"] = {"kind": "not sent"}
        else:
            raise _UsageLimitChangeRejected

    try:
        return store.update_session(saved["login"], saved["session"]["session_id"], apply)
    except (store.NoLiveRecord, _UsageLimitChangeRejected):
        return UsageLimitChangeRefused()

def shutdown_command(*arguments: str) -> str:
    """A runnable terminal command with shell-quoted arguments."""
    return (
        "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py "
        + shlex.join(arguments)
    )

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

PAUSE_FOR_MOVE = "Pause for move"
PAUSE_FOR_STOP = "Pause for stop"


def _settle_text(record: SessionShutdown, session: Session) -> str:
    """The pause message: a fixed first line the launching skills react to, then what to do now.

    The session stops at once, mid-step: it is never waited on to go idle by itself, and the
    stop follows its ready mark.
    """
    why = record.get("why", "") or "requested by the user"
    target = record.get("target", "")
    asked = f"({why}, {_user_time(record['requested_at'])})"
    first_line = (f"{PAUSE_FOR_MOVE}: {record['label']} → {target} {asked}" if target
                  else f"{PAUSE_FOR_STOP}: {record['label']} {asked}")
    command = (
        "`~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py "
        "ready --where "
    )
    if session["kind"] == "unit":
        where = '"<phase N: the step you stopped in and what comes next>"'
        started = "stop or end any seat or build you started, or name it in that line"
        ending = " and end your turn with `— blocked: shutdown requested by the user`"
    elif session["kind"] == "showrunner":
        where = '"<one line>"'
        started = "append a `### STATE` block to your LOG, stop or name any build you started"
        ending = " and end your turn"
    else:
        where = '"<what you were doing and what comes next>"'
        started = "stop or name anything you started that keeps running"
        ending = " and end your turn"
    return first_line + "\n\n" + (
        f"Stop now, without finishing the step you are in, and start nothing new. Leave uncommitted work in place, "
        f"{started}, then run {command}{where}`{ending}. "
        f"You will be resumed {'on ' + target + ' ' if target else ''}with that line and continue from it."
    )


def _progress_text(record: SessionShutdown) -> str:
    entry_progress = record["progress"]
    progress = entry_progress["kind"]
    where = record["where"]
    if progress == "ready" and where["kind"] == "said":
        progress += f" — {where['text']}"
    if entry_progress["kind"] == "restart failed" or entry_progress["kind"] == "stop failed":
        progress += f" — {entry_progress['reason']}"
    elif entry_progress["kind"] == "timers pending":
        timers = "; ".join(
            f"{timer['instance']}: {timer['reason']}"
            for timer in entry_progress["timers"]
        )
        progress += f" — {timers}"
    elif entry_progress["kind"] == "manual restart":
        progress += f" — run: {entry_progress['command']}"
    return progress


def status_record_lines(record: SessionShutdown, owner: stop_work.FreshSession | _StatusUnobserved = _STATUS_UNOBSERVED,
                        *, local_observation: bool = True) -> list[str]:
    """Render progress, observed readiness details and issues for one owner."""
    progress = _progress_text(record)
    target = record.get("target", "")
    if target:
        progress += f" · to {target}: {record.get('why', '')}"
    if record["progress"]["kind"] in {"waiting", "ready", "held by usage limit"} and not isinstance(owner, _StatusUnobserved) and owner["kind"] == "matching session":
        try:
            detail = ", ".join(_hold_details(owner, record["progress"]["kind"], local_observation=local_observation))
        except Exception:
            detail = record["progress"]["kind"]
        if detail and detail != record["progress"]["kind"]:
            progress += f" — {detail}"
    settle_message = record["settle_message"]
    if settle_message["kind"] == "sent":
        message = " · message sent"
    elif settle_message["kind"] == "queued":
        message = f" · message queued: {settle_message['reason']}"
    else:
        message = ""
    lines = [f"  {record['session']['name']}: {progress}{message}"]
    lines.extend(f"    {stop_issue_text(issue)}" for issue in record["stop_issues"])
    return lines


def run_inventory(login: str, only: Iterable[str] = ()) -> Inventory:
    """Observe the account once, including each owner's current seats."""
    return inventory(login, frozenset(only))


def _owned_seats(report: Inventory, session_id: str) -> list[Session]:
    return [item for item in report["sessions"] if item["kind"] == "seat"
            and item["owner"]["kind"] == "director"
            and item["owner"]["session_id"] == session_id]


def begin(
    login: str, requested_by: str | None = None, only: Iterable[str] = (), *,
    then: ShutdownContinuation = "stop", alerts: Literal["user", "none"] = "user",
    request_id: str,
    target: str = "", why: str = "",
) -> list[SessionShutdown]:
    """Add independent owners, leaving every existing request intact."""
    for line in store.migrate(login):
        print(line)
    ids = tuple(only)
    report = run_inventory(login, ids)
    at = record_time()
    origin: store.RequestOrigin = ({"kind": "session", "session_id": requested_by}
                                   if requested_by else {"kind": "terminal"})
    if not ids and isinstance(store.lookup_account_hold(login), store.AccountOpen):
        store.set_account_hold(store.AccountHold(
            login=login, label=report["label"], machine=report["machine"], requested_at=at,
            request_id=request_id, requested_by=origin, then=then,
        ))
    added: list[SessionShutdown] = []
    for session in report["sessions"]:
        if session["kind"] == "seat":
            continue
        saved = SessionShutdown(
            login=login, label=report["label"], machine=report["machine"], requested_at=at,
            request_id=request_id, requested_by=origin, hold="session" if ids else "account",
            then=then, force="wait for ready", alerts=alerts, session=session,
            seats=_owned_seats(report, session["session_id"]), timers=[],
            settle_message={"kind": "not sent"}, where={"kind": "not said"},
            progress={"kind": "waiting"}, stop_issues=[],
        )
        if target:
            saved["target"] = target
            saved["source_label"] = report["label"]
            saved["why"] = why or f"{report['label']} is out of usage"
        if not store.create_session(saved):
            print(f"{session['name']}: already in progress")
            continue
        try:
            timers = _session_timer_snapshot(session)
            stop_errors: list[Exception] = []
            def save_timers(current: SessionShutdown) -> None:
                current["timers"] = timers
                try:
                    _stop_recorded_timers(timers)
                except Exception as error:
                    stop_errors.append(error)
            saved = store.update_session(login, session["session_id"], save_timers)
            if stop_errors:
                raise stop_errors[0]
        except Exception as error:
            try:
                _ = store.withdraw_session(login, session["session_id"], _restore_record)
            except store.NoLiveRecord:
                pass
            print(f"{session['name']}: timer stop failed: {error}", file=sys.stderr)
            continue
        added.append(saved)
        print(f"{session['name']}: added")
    return added


def _is_requester(saved: SessionShutdown) -> bool:
    origin = saved["requested_by"]
    return origin["kind"] == "session" and origin["session_id"] == saved["session"]["session_id"]


def _hold_details(owner: stop_work.FreshSession, progress: str, *, local_observation: bool = True) -> list[str]:
    """Return observed hold conditions without delivery state."""
    if owner["kind"] == "session account unreadable":
        return ["account unreadable"]
    if owner["kind"] != "matching session":
        return [owner["kind"].removeprefix("session ")]
    session = owner["session"]
    details: list[str] = []
    if session["status"] != "idle":
        details.append(session["status"])
    if session["status"] != "busy" and any(server["busy_seats"] for server in session["codex_servers"]):
        details.append("busy")
    if local_observation and progress != "held by usage limit" and _showing_form(session):
        details.append(SHOWING_FORM)
    checkout = session["checkout"]
    if checkout["kind"] == "git" and checkout["upstream"]["kind"] == "tracking":
        ahead = checkout["upstream"]["ahead"]
        if ahead:
            head = checkout["head"]
            branch = head["name"] if head["kind"] == "branch" else head["commit"]
            details.append(f"ahead {ahead} on {branch}")
    if local_observation and merge_in_progress(session):
        details.append("merge in progress")
    for server in session["codex_servers"]:
        details.extend(f"Codex seat {name} is still running" for name in server["busy_seats"])
    details.extend(f"Codex seat {seat['name']} is still running" for seat in owner["seats"]
                   if seat["status"] != "idle")
    return details


def hold_reason(saved: SessionShutdown, owner: stop_work.FreshSession, *, local_observation: bool = True) -> str:
    """Describe the condition preventing this owner from advancing."""
    details = _hold_details(owner, saved["progress"]["kind"], local_observation=local_observation)
    message = saved["settle_message"]
    if owner["kind"] == "matching session" and message["kind"] == "queued":
        details.append(f"message queued: {message['reason']}")
    return ", ".join(details) or saved["progress"]["kind"]


def next_step(saved: SessionShutdown, owner: stop_work.FreshSession,
              others: list[SessionShutdown], now: datetime) -> Step:
    """Choose only this owner's next action from a single machine observation."""
    progress = saved["progress"]["kind"]
    if progress == "restarted":
        return Finished()
    if progress in {"stopped", "already gone", "process identity lost"} and saved["then"] == "stop" and not _stop_retry_pending(saved):
        return Finished()
    # `now` stops the requester too, without waiting for the rest of its request.
    if saved["force"] != "now" and _is_requester(saved) and any(
        item["request_id"] == saved["request_id"]
        and item["session"]["session_id"] != saved["session"]["session_id"]
        and not _done(item) for item in others
    ):
        return Hold("requester goes last")
    if _stop_retry_pending(saved):
        return StopSession()
    if owner["kind"] == "session account unreadable" and progress in {"waiting", "ready", "held by usage limit"}:
        return Hold("account unreadable")
    if progress in {"waiting", "ready", "held by usage limit"}:
        if owner["kind"] in {"session absent", "session identity lost"}:
            return StopSession()
        if owner["kind"] == "matching session":
            if saved["force"] == "now":
                return StopSession()
            if progress in {"ready", "held by usage limit"} and owner["session"]["status"] == "idle" and all(
                seat["status"] == "idle" for seat in owner["seats"]
            ) and blocker(owner["session"])["kind"] == "clear" and (progress == "held by usage limit" or not _showing_form(owner["session"])):
                return StopSession()
        if progress == "waiting" and not _is_requester(saved):
            message = saved["settle_message"]
            if message["kind"] == "not sent" or (message["kind"] == "queued" and
                now - datetime.fromisoformat(message["at"]) >= timedelta(seconds=SETTLE_RETRY_SECONDS)):
                return SendSettleMessage()
            # One more ask when no ready mark came back; after that the requester is told.
            if (message["kind"] == "sent" and "reasked_at" not in saved
                    and now - datetime.fromisoformat(message["at"]) >= REASK_AFTER):
                return SendSettleMessage()
    if progress in {"stopped", "already gone", "process identity lost"} and saved["then"] == "restart":
        return RelaunchSession()
    if progress in {"stopping", "restarting"}:
        claim = saved["progress"]
        if claim["kind"] == "stopping":
            if now - datetime.fromisoformat(claim["at"]) >= timedelta(seconds=store.CLAIM_STALE_SECONDS):
                return StopSession()
        elif claim["kind"] == "restarting":
            if now - datetime.fromisoformat(claim["at"]) >= timedelta(seconds=store.CLAIM_STALE_SECONDS):
                return RelaunchSession()
    return Hold(hold_reason(saved, owner))


def _send_settle_message(saved: SessionShutdown) -> None:
    session = saved["session"]
    found = store.lookup_session(saved["login"], session["session_id"])
    if (not isinstance(found, store.ShutdownFound) or found.record["request_id"] != saved["request_id"]
        or found.record["progress"]["kind"] != "waiting"):
        return
    text = _settle_text(saved, session)
    delivery = send_message(f"session:{session['session_id']}", text.split("\n", 1)[0], text,
        key=f"shutdown-{saved['login']}-{session['session_id']}")
    at = record_time()
    def save(current: SessionShutdown) -> None:
        if current["request_id"] != saved["request_id"] or current["progress"]["kind"] != "waiting":
            return
        if current["settle_message"]["kind"] == "sent":
            current["reasked_at"] = at
            return
        current["settle_message"] = ({"kind": "sent", "at": at} if delivery["kind"] == "sent"
                                    else {"kind": "queued", "at": at, "reason": delivery["reason"]})
    _ = store.update_session(saved["login"], session["session_id"], save)


def ready(where: str) -> int:
    """Record this owner's next step without replacing its process identity."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for saved in store.all_session_records():
        if saved["session"]["session_id"] != session_id or saved["progress"]["kind"] not in {"waiting", "ready", "held by usage limit"}:
            continue
        owner = stop_work.fresh_session(saved["session"], run_inventory(saved["login"]))
        if owner["kind"] != "matching session":
            print(f"this session is not in a fresh inventory of {saved['label']}: {owner['kind']}", file=sys.stderr)
            return 2
        check = blocker(owner["session"])
        if check["kind"] == "blocked":
            print(check["reason"], file=sys.stderr)
            return 2
        accepted = False
        def mark(current: SessionShutdown) -> None:
            nonlocal accepted
            if current["progress"]["kind"] in {"waiting", "ready", "held by usage limit"}:
                current["where"] = {"kind": "said", "text": where, "at": record_time()}
                current["progress"] = {"kind": "ready", "at": record_time()}
                accepted = True
        _ = store.update_session(saved["login"], session_id, mark)
        if accepted:
            print(f"ready for shutdown: {where}")
            return 0
    print("no shutdown in progress for this session", file=sys.stderr)
    return 1


def stop_issue_text(issue: SessionStopIssue) -> str:
    """Render one owner's stop issue in status and its result."""
    match issue["kind"]:
        case "not ready to stop":
            return f"not stopped: {issue['status']}, {issue['progress']}"
        case "still running":
            return f"still running: {issue['reason']}"
        case "account unreadable":
            return "not stopped: account unreadable"
        case "codex server left running":
            return f"Codex server {issue['run_dir']} left running: {issue['cause']}"
        case "unit session left running":
            return f"{issue['host']} session {issue['session_name']} left running: {issue['cause']}"


def _restore_record(saved: SessionShutdown) -> None:
    for timer in saved["timers"]:
        if timer["was_enabled"]:
            run_notifier("start", timer["instance"])
        footer = timer["footer"]
        if footer["kind"] == "footer":
            _restore_footer(footer["slug"])
    _retire_shutdown_messages(saved["login"], saved["session"]["session_id"])
    if saved["settle_message"]["kind"] in {"sent", "queued"}:
        _ = send_message(f"session:{saved['session']['session_id']}",
            f"Shutdown of {saved['label']} cancelled",
            f"Shutdown of {saved['label']} cancelled by the user: continue where you were.",
            key=f"shutdown-{saved['login']}-{saved['session']['session_id']}")


def unrecorded_local_sessions(login: str, ids: tuple[str, ...], records: list[SessionShutdown]) -> dict[str, str]:
    missing = frozenset(ids) - {saved["session"]["session_id"] for saved in records}
    if not missing:
        return {}
    report = inventory(login, missing)
    return {owner["session_id"]: owner["name"] for owner in report["sessions"]
            if owner["session_id"] in missing and owner["kind"] != "seat"}


def cancel(account: Account, here: bool = False, only: Iterable[str] = ()) -> int:
    """Withdraw waiting owners here, then ask the peer to withdraw its own."""
    for line in store.migrate(account.login):
        print(line)
    retained = False
    ids = tuple(only)
    import restart as restart_work
    every = store.session_records(account.login)
    checked = restart_work.clear_running_on_target(every)
    records = checked.remaining
    for saved in checked.finished:
        if not ids or saved["session"]["session_id"] in ids:
            print(f"{saved['session']['name']} is already running on {saved.get('target') or saved['label']}; its shutdown is finished")
    missing = unrecorded_local_sessions(account.login, ids, every)
    local_ids = {saved["session"]["session_id"] for saved in every} | missing.keys()
    for name in missing.values():
        print(f"{name}: no shutdown in progress")
        retained = True
    withdrawn_requests: list[str] = []
    def still_live(current: SessionShutdown) -> bool:
        report = inventory(account.login, frozenset({current["session"]["session_id"]}))
        return stop_work.fresh_session(current["session"], report)["kind"] == "matching session"
    for saved in records:
        if ids and saved["session"]["session_id"] not in ids:
            continue
        # Suppression takes the watcher lock before withdrawal takes the account lock.
        # The open record keeps concurrent watcher ticks from replacing this request.
        if saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit"} or (
            ids and saved["progress"]["kind"] == "stop failed"
        ):
            from scripts.accounts.refusal_watch import cancelled
            cancelled(saved["session"]["session_id"])
        try:
            outcome = (store.withdraw_failed_stop(account.login, saved["session"]["session_id"], still_live, _restore_record)
                       if ids else store.withdraw_session(account.login, saved["session"]["session_id"], _restore_record))
        except store.NoLiveRecord:
            if ids:
                print(f"{saved['session']['name']}: no shutdown in progress")
                retained = True
            continue
        if isinstance(outcome, store.SessionRetained):
            retained = True
            name = outcome.record["session"]["name"]
            detail = _progress_text(outcome.record)
            action = f"/shutdown restart --only {name}" if ids else "/shutdown restart"
            print(f"{name} was not cancelled: {detail}; {action} brings it back")
        else:
            withdrawn_requests.append(outcome.record["request_id"])
            session = outcome.record["session"]
            if outcome.record.get("target"):
                from scripts.accounts import moves
                moves.append_move(moves.FailedMove(kind="failed", at=record_time(), tool="claude",
                    subject=session["session_id"] + ": " + session["name"],
                    move_id=outcome.record["request_id"], reason="cancelled"))
            if outcome.record["progress"]["kind"] == "stop failed":
                print(f"{outcome.record['session']['name']}: taken out, still running")
            elif ids:
                print(f"{outcome.record['session']['name']}: taken out")
    if ids:
        if withdrawn_requests:
            store.clear_finished_account_hold(account.login, request_ids=tuple(withdrawn_requests))
    else:
        store.clear_account_hold(account.login)
    peer_ids = [identity for identity in ids if identity not in local_ids]
    if not here and (not ids or peer_ids):
        arguments = ["cancel", account.login, "--here"]
        if ids:
            arguments.extend(("--only", ",".join(peer_ids)))
        status, output = run_remote(arguments)
        if output.strip():
            print(output.strip())
        if status not in {0, 1}:
            print(f"{other_machine()}: not reached; run /shutdown there", file=sys.stderr)
            return 1
        retained = retained or status == 1
    return 1 if retained else 0


def now(account: Account, here: bool = False, only: Iterable[str] = ()) -> int:
    """Force waiting owners and retry owners whose stop failed."""
    for line in store.migrate(account.login):
        print(line)
    changed = False
    refused = False
    ids = tuple(only)
    records = store.session_records(account.login)
    missing = unrecorded_local_sessions(account.login, ids, records)
    local_ids = {saved["session"]["session_id"] for saved in records} | missing.keys()
    for name in missing.values():
        print(f"{name}: no shutdown in progress")
        refused = True
    for saved in records:
        if ids and saved["session"]["session_id"] not in ids:
            continue
        if not ids and saved["progress"]["kind"] not in {"waiting", "ready", "held by usage limit"} and not store.stop_incomplete(saved):
            continue
        forced = False
        def force(current: SessionShutdown) -> None:
            nonlocal forced
            if current["progress"]["kind"] in {"waiting", "ready", "held by usage limit"} or store.stop_incomplete(current):
                current["force"] = "now"
                forced = True
        try:
            current = store.update_session(account.login, saved["session"]["session_id"], force)
        except store.NoLiveRecord:
            if ids:
                print(f"{saved['session']['name']}: no shutdown in progress")
                refused = True
            continue
        changed = changed or forced
        if ids:
            if forced:
                print(f"{current['session']['name']}: stopping now")
            else:
                print(f"{current['session']['name']} is {_progress_text(current)}; nothing to stop")
                refused = True
    if changed:
        _launch_conductor(account.login, account.label)
    if not ids:
        print(f"{local_machine()}: stopping now" if changed else f"{local_machine()}: no shutdown in progress")
    peer_ids = [identity for identity in ids if identity not in local_ids]
    if not here and (not ids or peer_ids):
        arguments = ["now", account.login, "--here"]
        if ids:
            arguments.extend(("--only", ",".join(peer_ids)))
        status, output = run_remote(arguments)
        if output.strip():
            print(output.strip())
        if status not in {0, 1}:
            print(f"{other_machine()}: not reached; run /shutdown there", file=sys.stderr)
            return 1
        refused = refused or status == 1
    return 1 if refused else 0


def _launch_conductor(login: str, label: str) -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "account"
    service = f"shutdown-{slug}-{os.getpid()}"
    command = [sys.executable, str(SHUTDOWN), "conduct", login]
    if os.environ.get("REFUSAL_WATCH_INVENTORY") == "read existing":
        # Service managers do not inherit the caller's environment automatically.
        command = ["env", "REFUSAL_WATCH_INVENTORY=read existing", *command]
    launch = (["launchctl", "submit", "-l", service, "--", *command] if sys.platform == "darwin"
              else ["systemd-run", "--user", "--collect", "--quiet", "--no-block", "--unit", service, *command])
    result = subprocess.run(launch, check=False, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"could not start shutdown conductor {service}")


def down(account: Account, here: bool = False, only: Iterable[str] = (), *,
         then: ShutdownContinuation = "stop", alerts: Literal["user", "none"] = "user",
         requested_by: str | None = None, request_id: str = "", target: str = "", why: str = "") -> int:
    """Start local work immediately and ask the peer to conduct its own owners."""
    if not _warm():
        print(f"GitHub keys are cold: run `github-warmup` in a terminal on {_machine()}, then `/shutdown` again", file=sys.stderr)
        return 1
    ids = tuple(only)
    origin = requested_by or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    identity = request_id or str(uuid4())
    local_ids: set[str] = ({saved["session"]["session_id"] for saved in store.session_records(account.login)}
                 | {session["session_id"] for session in inventory(account.login, frozenset(ids))["sessions"]}) if ids else set()
    added = begin(account.login, origin, ids, then=then, alerts=alerts, request_id=identity, target=target, why=why)
    try:
        _launch_conductor(account.login, account.label)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if target:
            for saved in added:
                _ = store.withdraw_session(account.login, saved["session"]["session_id"], _restore_record)
        raise
    if target:
        from scripts.accounts import moves, refusals
        for saved in added:
            session = saved["session"]
            request: moves.RequestedMove = {"kind": "requested", "at": saved["requested_at"],
                "tool": "claude", "subject": session["session_id"] + ": " + session["name"],
                "move_id": saved["request_id"], "from": saved.get("source_label", account.label),
                "to": target, "why": saved.get("why", why), "window": "unknown"}
            refusal = refusals.load().get(refusals.key("claude", account.login))
            if refusal is not None:
                request["window"] = refusal.window.name if isinstance(refusal.window, refusals.KnownWindow) else "unknown"
                if isinstance(refusal.reset, refusals.KnownReset):
                    request["reset"] = refusal.reset.at.isoformat()
            moves.append_move(request)
    peer_ids = [session_id for session_id in ids if session_id not in local_ids]
    if not here and (not ids or peer_ids):
        arguments = ["down", account.login, "--here", "--request-id", identity]
        if origin:
            arguments.extend(("--requested-by", origin))
        if ids:
            arguments.extend(("--only", ",".join(peer_ids)))
        if then == "restart":
            arguments.append("--restart")
        if target:
            arguments.extend(("--to", target, "--why", why or f"{account.label} is out of usage"))
        if alerts == "none":
            arguments.append("--no-alert")
        status, output = run_remote(arguments)
        if status != 0:
            print(f"{other_machine()}: not reached; run /shutdown there", file=sys.stderr)
        elif output.strip():
            print(output.strip())
    print(f"shutdown of {account.label} started; /shutdown status to watch, /shutdown cancel to undo")
    return 0

def _stop_retry_pending(saved: SessionShutdown) -> bool:
    return saved["force"] == "now" and store.stop_incomplete(saved)


def _done(saved: SessionShutdown) -> bool:
    if _stop_retry_pending(saved):
        return False
    progress = saved["progress"]["kind"]
    return progress in {"restarted", "stop failed", "restart failed", "manual restart", "timers pending"} or (
        progress in {"stopped", "already gone", "process identity lost"} and saved["then"] == "stop")


def _action_failed(saved: SessionShutdown, error: Exception, *, stopping: bool) -> None:
    reason = str(error).splitlines()[0] if str(error).splitlines() else type(error).__name__
    if not stopping:
        import restart as restart_work
        found = store.lookup_session(saved["login"], saved["session"]["session_id"])
        if isinstance(found, store.ShutdownFound) and found.record["request_id"] == saved["request_id"]:
            # A launch can raise after recording its claim but before returning it.
            expected = saved if saved["progress"]["kind"] == "restarting" else found.record
            restart_work.mark_restart_failed(expected, reason)
        return
    def fail(current: SessionShutdown) -> None:
        current["progress"] = {"kind": "stop failed", "at": record_time(), "reason": reason}
        current["force"] = "wait for ready"
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], fail)
    except store.NoLiveRecord:
        pass


def report_stop(records: list[SessionShutdown]) -> None:
    """Report local outcomes alongside one current observation of the peer."""
    if not records:
        return
    import restart as restart_work
    checked = restart_work.clear_running_on_target(records)
    records = checked.remaining + checked.finished
    first = records[0]
    status, output = run_remote(["records", "--json", "--here"])
    lines = [f"{saved['machine']} {saved['session']['name']}: {saved['progress']['kind']}" for saved in records]
    if status == 0:
        try:
            peer = store.parse_session_records(output)
            lines.extend(f"{saved['machine']} {saved['session']['name']}: {saved['progress']['kind']}"
                         for saved in peer if saved["login"].casefold() == first["login"].casefold())
        except store.InvalidRecord:
            lines.append(f"{other_machine()}: shutdown state unreadable")
    else:
        lines.append(f"{other_machine()}: not reached")
    for saved in records:
        lines.extend(f"  {saved['session']['name']}: {stop_issue_text(issue)}" for issue in saved["stop_issues"])
    failed = [saved for saved in records if store.stop_incomplete(saved)]
    restore_failed = [saved for saved in records if saved["progress"]["kind"] in {"restart failed", "manual restart", "timers pending"}]
    if failed:
        summary = f"{first['label']}: stop partial"
        lines.append("/shutdown now retries the failed stop")
    elif restore_failed:
        summary = f"{first['label']} is not fully back"
        lines.append(shutdown_command("restart", first["label"]))
    elif all(saved["progress"]["kind"] == "restarted" for saved in records):
        summary = f"{first['label']} is back"
    else:
        summary = f"{first['label']} is down"
        lines.append(f"/shutdown restart in any Claude session on {first['label']}, or in a terminal "
                     + shutdown_command("restart", first["label"]))
    tell_showrunners_outcome([saved for saved in records if saved["machine"] == local_machine()])
    text = "\n".join(lines)
    origins = {saved["requested_by"]["session_id"] for saved in records if saved["requested_by"]["kind"] == "session"}
    account = json.dumps(first["label"])
    def failure_details(items: list[SessionShutdown], verb: str) -> str:
        details: list[str] = []
        for saved in items:
            progress = saved["progress"]
            if progress["kind"] == "stop failed" or progress["kind"] == "restart failed":
                reason = progress["reason"]
            elif progress["kind"] == "manual restart":
                reason = "manual restart needed: " + progress["command"]
            elif progress["kind"] == "timers pending":
                reason = "session restarted; some scheduled jobs have not resumed"
            else:
                reason = progress["kind"]
            details.append(f"The session named {saved['session']['name']} on {saved['machine']} {verb}: {reason}.")
            details.extend(stop_issue_text(issue) for issue in saved["stop_issues"])
        return alert_details(details)
    try:
        if any(saved["hold"] == "account" and saved["alerts"] == "user" for saved in failed):
            alert_user("Claude account shutdown is incomplete",
                       f"The shutdown you requested on Claude account {account} did not finish. " + failure_details(failed, "could not stop")
                       + "\n\nThe status command lists what failed. The now command tries to stop them again.",
                       ActionRequired(f"In a Claude session on natedev, read /shutdown status {account}, then retry with /shutdown now {account}."),
                       machine="natedev" if local_machine().casefold() == "mac" else "")
        elif unpushed := [saved for saved in restore_failed
                          if saved["alerts"] == "user" and saved.get("restore_alerted") != _not_back_text(saved)]:
            alert_user(f"{first['label'] if len(first['label']) <= 16 else 'Claude'} sessions did not come back",
                       f"Sessions on Claude account {account} did not return after shutdown. " + failure_details(unpushed, "could not return")
                       + "\n\nThe status command lists what failed. The restart command tries to bring the sessions back.",
                       ActionRequired(f"In a Claude session on natedev, read /shutdown status {account}, then /shutdown restart {account} to retry."),
                       machine="natedev" if local_machine().casefold() == "mac" else "")
            for saved in unpushed:
                _mark_not_back_pushed(saved)
    finally:
        for origin in sorted(origins):
            _ = send_message(f"session:{origin}", summary, text, key=f"shutdown-result-{first['login']}")


def _not_back_text(saved: SessionShutdown) -> str:
    """What a not-back push says about this record; an unchanged one is not pushed again."""
    progress = saved["progress"]
    if progress["kind"] == "restart failed":
        return f"{saved['request_id']} restart failed: {progress['reason']}"
    if progress["kind"] == "manual restart":
        return f"{saved['request_id']} manual restart: {progress['command']}"
    if progress["kind"] == "timers pending":
        return f"{saved['request_id']} timers pending: " + ", ".join(timer["instance"] for timer in progress["timers"])
    return f"{saved['request_id']} {progress['kind']}"


SHOWRUNNER_TOLD_STOPPING = "stopping"
SHOWRUNNER_TOLD_BACK = "back"


def _unit_showrunner(saved: SessionShutdown) -> str:
    """The name now of the showrunner of this unit's production; empty for any other session, or when none runs."""
    session = saved["session"]
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        return ""
    try:
        return showrunners.current_name(showrunners.production_slug(host["doc"]))
    except (OSError, ValueError):
        return ""


def _reserve_showrunner_line(saved: SessionShutdown, told: str) -> bool:
    """Mark `told` as sent to the showrunner unless it already was; only the caller that marks it sends it."""
    reserved = False
    def mark(current: SessionShutdown) -> None:
        nonlocal reserved
        if current["request_id"] == saved["request_id"] and current.get("showrunner_told") != told:
            current["showrunner_told"] = told
            reserved = True
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], mark)
    except store.NoLiveRecord:
        return False
    return reserved


def tell_showrunner_stopping(saved: SessionShutdown) -> None:
    """Before a unit is stopped, one line to its production's showrunner: what stops, why, and when it is back."""
    showrunner = _unit_showrunner(saved)
    if not showrunner or "showrunner_told" in saved or not _reserve_showrunner_line(saved, SHOWRUNNER_TOLD_STOPPING):
        return
    name = saved["session"]["name"]
    target = saved.get("target", "")
    why = saved.get("why", "") or "requested by the user"
    back = (("it comes back on " + target if target else "it comes back") + " within minutes"
            if saved["then"] == "restart" else "it stays stopped until the user restarts it")
    summary = f"{name} is stopping: {why}"
    _ = send_message(showrunner, summary, f"{summary}; {back}. One more line follows when it is back or has not come back.",
                     key=f"shutdown-unit-{saved['session']['session_id']}")


def tell_showrunners_outcome(records: list[SessionShutdown]) -> None:
    """The second line to a stopped unit's showrunner: back, or not back and why."""
    for saved in records:
        if saved.get("showrunner_told") is None:
            continue
        progress = saved["progress"]
        name = saved["session"]["name"]
        if progress["kind"] in {"restarted", "timers pending"}:
            told = SHOWRUNNER_TOLD_BACK
            summary = f"{name} is back on {saved.get('target') or saved['label']}"
            text = summary
        elif progress["kind"] in {"restart failed", "manual restart"}:
            told = _not_back_text(saved)
            reason = progress["reason"] if progress["kind"] == "restart failed" else "it needs a manual restart"
            summary = f"{name} did not come back: {reason}"
            text = f"{summary}. The user is told; {shutdown_command('restart', saved['label'])} retries it."
        else:
            continue
        if saved.get("showrunner_told") == told:
            continue
        showrunner = _unit_showrunner(saved)
        # A record archived on success is no longer live, so it cannot be marked; it is reported once anyway.
        if showrunner and (progress["kind"] == "restarted" or _reserve_showrunner_line(saved, told)):
            _ = send_message(showrunner, summary, text, key=f"shutdown-unit-{saved['session']['session_id']}")


def _mark_not_back_pushed(saved: SessionShutdown) -> None:
    def mark(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"] and current["progress"] == saved["progress"]:
            current["restore_alerted"] = _not_back_text(saved)
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], mark)
    except store.NoLiveRecord:
        pass


@dataclass(frozen=True)
class PendingSessionAction:
    record: SessionShutdown
    step: StopSession | RelaunchSession
    future: Future[SessionShutdown]


@dataclass(frozen=True)
class ConfirmedRelaunchPass:
    records: list[SessionShutdown]
    no_remote_control: tuple[NoRemoteControl, ...]


@dataclass(frozen=True)
class PendingRegistryConfirmation:
    records: list[SessionShutdown]
    future: Future[ConfirmedRelaunchPass]


def _confirm_relaunch_pass(records: list[SessionShutdown]) -> ConfirmedRelaunchPass:
    import restart as restart_work
    confirmation = restart_work.wait_for_confirmation(
        frozenset(saved["session"]["session_id"] for saved in records), restart_work.SESSION_WAIT_SECONDS)
    confirmed: list[SessionShutdown] = []
    for saved in records:
        claim = saved["progress"]
        if claim["kind"] == "restarting":
            try:
                restart_work.confirm_entry(saved, claim["at"], saved["session"]["session_id"] in confirmation.live_ids)
            except store.NoLiveRecord:
                continue
            except Exception as error:
                _action_failed(saved, error, stopping=False)
                continue
            confirmed.append(saved)
    confirmed_ids = {saved["session"]["session_id"] for saved in confirmed}
    control_records = restart_work.RegistryConfirmation(tuple(
        item for item in confirmation.records if item["sessionId"] in confirmed_ids))
    missing = restart_work.remote_control_missing(confirmed, control_records)
    return ConfirmedRelaunchPass(records, missing)


def _remote_control_alert(records: list[SessionShutdown], missing: tuple[NoRemoteControl, ...]) -> None:
    due = [item for item in missing if item["alerts"] == "user"]
    if not due:
        return
    names = ", ".join(item["name"] for item in due)
    machine = records[0]["machine"]
    instruction = f"In the {names} session's terminal on {machine}, run /remote-control." if len(due) == 1 else f"In each listed session's terminal on {machine}, run /remote-control."
    text = (f"The Claude sessions on {machine} were restarted and are running again. "
            f"Remote control is still off in {names}, so you cannot reach {'it' if len(due) == 1 else 'them'} from your phone until you turn it back on.")
    title = f"{names} session unreachable from your phone" if len(due) == 1 and len(names) <= 5 else "Claude sessions unreachable from your phone"
    alert_user(title, text, ActionRequired(instruction),
        machine="natedev" if local_machine().casefold() == "mac" else "")


def _shows_form(reason: str) -> bool:
    return SHOWING_FORM in reason.split(", ")


def _holdout_due(saved: SessionShutdown, at: datetime, *, form: bool = False) -> bool:
    """A holdout is told once to its requester, and pushed once more only when a form opens in its pane."""
    message = saved["settle_message"]
    since = message["at"] if message["kind"] == "sent" else saved["requested_at"]
    return (saved["alerts"] == "user" and at - datetime.fromisoformat(since) >= NOT_ANSWERING_AFTER
        and ("alerted_at" not in saved or (form and "form_alerted_at" not in saved)))


def _reserve_holdout_alert(saved: SessionShutdown, at: datetime, *, form: bool = False) -> bool:
    accepted = False
    def mark(current: SessionShutdown) -> None:
        nonlocal accepted
        if (current["request_id"] == saved["request_id"] and current["progress"]["kind"] in {"waiting", "ready"}
            and _holdout_due(current, at, form=form)):
            current["alerted_at"] = record_time()
            if form:
                current["form_alerted_at"] = current["alerted_at"]
            accepted = True
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], mark)
    except store.NoLiveRecord:
        pass
    return accepted


def _send_holdout_alert(held: list[tuple[SessionShutdown, str]]) -> None:
    """Tell the requester about every holdout; push to the phone only a form the user must answer."""
    first = held[0][0]
    summaries = [f"{saved['session']['name']} is not answering the shutdown of {saved['label']}" for saved, _ in held]
    lines: list[str] = []
    for saved, reason in held:
        name = saved["session"]["name"]
        action = f"/shutdown cancel --only {name} (take it out; it carries on), /shutdown now --only {name} (stop it anyway)"
        lines.append(f"{saved['machine']} {name}: {reason}; {action}")
    text = "\n".join(lines)
    summary = "; ".join(summaries)
    due = [(saved, reason) for saved, reason in held if _shows_form(reason)]
    if not due:
        _tell_holdout_requesters(held, summary, text)
        return
    waiting = [f"Its session named {saved['session']['name']} on {saved['machine']} is {reason}, so it has not stopped." for saved, reason in due]
    account = json.dumps(first["label"])
    names = ",".join(saved["session"]["name"] for saved, _ in due)
    subject = "it" if len(due) == 1 else "them"
    possessive = "its" if len(due) == 1 else "their"
    form_instruction = " Read any open form in that session's terminal before choosing." if len(due) == 1 else ""
    opening = f"Shutdown of Claude account {account} is waiting for sessions to stop. "
    action_text = "In the natedev Claude session, choose cancel or stop now."
    choices = (f"\n\nCancel (recommended): /shutdown cancel {account} --only {names}. This keeps {subject} running so you can check {possessive} work.\n\n"
               + f"Stop now: /shutdown now {account} --only {names}. This stops {subject} without waiting.")
    omitted = f"{len(waiting)} more sessions are waiting."
    fixed_length = len("Action: " + action_text + "\n" + opening + form_instruction)
    account_wide = len(due) > 1 and fixed_length + len(choices) + len(omitted) >= 500
    if account_wide:
        choices = (f"\n\nCancel (recommended): /shutdown cancel {account}. This keeps every waiting session running so you can check their work.\n\n"
                   + f"Stop now: /shutdown now {account}. This stops every waiting session without waiting for its work to finish.")
    budget = min(120, 499 - fixed_length - len(choices))
    included: list[str] = []
    for index, sentence in enumerate(waiting):
        remaining = len(waiting) - index - 1
        suffix = f"{remaining} more {'session is' if remaining == 1 else 'sessions are'} waiting." if remaining else ""
        if len(" ".join([*included, sentence, *([suffix] if suffix else [])])) > budget:
            break
        included.append(sentence)
        omitted = suffix
    if account_wide and not included:
        omitted = f"{len(waiting)} sessions are waiting."
        reason_sentence = f"One is {due[0][1]}, so it has not stopped."
        if len(omitted + " " + reason_sentence) <= budget:
            omitted += " " + reason_sentence
    phone_text = opening + " ".join([*included, *([omitted] if omitted else [])]) + form_instruction + choices
    title = "Shutdown waits on a session showing a form"
    try:
        alert_user(title, phone_text,
            ActionRequired(action_text),
            machine="natedev" if local_machine().casefold() == "mac" else "")
    finally:
        _tell_holdout_requesters(held, summary, text)


def _tell_holdout_requesters(held: list[tuple[SessionShutdown, str]], summary: str, text: str) -> None:
    origins = {saved["requested_by"]["session_id"] for saved, _ in held if saved["requested_by"]["kind"] == "session"}
    for origin in sorted(origins):
        _ = send_message(f"session:{origin}", summary, text, key=f"shutdown-holdout-{held[0][0]['login']}")


def _stop_after_telling(saved: SessionShutdown) -> SessionShutdown:
    try:
        tell_showrunner_stopping(saved)
    except Exception as error:
        print(f"{saved['session']['name']}: showrunner not told of the stop: {error}", file=sys.stderr)
    return stop_work.stop_session(saved)


def _archive_finished(saved: SessionShutdown) -> bool:
    try:
        store.archive_session(saved["login"], saved["session"]["session_id"])
    except store.NoLiveRecord:
        return False
    return True


def _complete_stop_action(saved: SessionShutdown) -> None:
    if not _stop_retry_pending(saved):
        return
    def consume(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"] and current["progress"] == saved["progress"]:
            current["force"] = "wait for ready"
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], consume)
    except store.NoLiveRecord:
        pass


def _clear_finished_hold(login: str, seen: dict[str, SessionShutdown]) -> None:
    outcome = store.lookup_account_hold(login)
    if isinstance(outcome, store.AccountHeld) and any(saved["request_id"] == outcome.hold["request_id"]
        and saved["progress"]["kind"] == "restarted" for saved in seen.values()
    ):
        store.clear_finished_account_hold(login)


def conduct(login: str) -> int:
    """Keep an owner available for work added while a conductor releases its lock."""
    for line in store.migrate(login):
        print(line)
    archived: dict[str, SessionShutdown] = {}
    while True:
        with store.conductor_lock(login) as ownership:
            if isinstance(ownership, store.ConductorAlreadyRunning):
                return 0
            _conduct_owned(login, archived)
        records = store.session_records(login)
        if any(not _done(saved) for saved in records):
            continue
        outcomes = {**archived, **{saved["session"]["session_id"]: saved for saved in records}}
        report_stop(list(outcomes.values()))
        return 0


def _conduct_owned(login: str, archived: dict[str, SessionShutdown]) -> None:
    """Advance one locked account while long actions run independently."""
    import restart as restart_work

    def archive(saved: SessionShutdown) -> None:
        if _archive_finished(saved):
            archived[saved["session"]["session_id"]] = saved

    desktop = restart_work.DesktopLaunchState()
    pending: dict[str, PendingSessionAction] = {}
    confirmations: list[PendingRegistryConfirmation] = []
    launched: list[SessionShutdown] = []
    observed_ids: set[str] = set()
    attempted: set[tuple[str, type[Step]]] = set()
    report: Inventory = {"machine": local_machine(), "login": login, "label": login,
                         "sessions": [], "unattributed": []}
    next_inventory_at = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=restart_work.RESTART_LAUNCH_WORKERS) as executor, ThreadPoolExecutor(max_workers=restart_work.RESTART_LAUNCH_WORKERS) as registry, ThreadPoolExecutor(max_workers=restart_work.RESTART_LAUNCH_WORKERS) as notifications:
            sends: list[Future[None]] = []
            messages: dict[str, Future[None]] = {}
            def accept_launch(original: SessionShutdown, result: SessionShutdown) -> None:
                if restart_work.owns_relaunch_claim(original, result):
                    launched.append(result)
                elif result["progress"]["kind"] == "restarted":
                    archive(result)

            def start_launch(saved: SessionShutdown) -> None:
                if saved["session"]["host"]["kind"] in {"ghostty", "zed", "terminal"}:
                    try:
                        result = restart_work.relaunch_session(saved, dry_run=False, desktop_state=desktop)
                        accept_launch(saved, result)
                    except Exception as error:
                        _action_failed(saved, error, stopping=False)
                else:
                    future = executor.submit(restart_work.relaunch_session, saved, dry_run=False, desktop_state=desktop)
                    pending[saved["session"]["session_id"]] = PendingSessionAction(saved, RelaunchSession(), future)

            while True:
                changed = False
                for session_id, action in list(pending.items()):
                    if not action.future.done():
                        continue
                    del pending[session_id]
                    changed = True
                    try:
                        result = action.future.result()
                    except store.NoLiveRecord:
                        continue
                    except Exception as error:
                        _action_failed(action.record, error, stopping=isinstance(action.step, StopSession))
                        continue
                    if isinstance(action.step, RelaunchSession):
                        accept_launch(action.record, result)
                    else:
                        _complete_stop_action(result)

                for batch in list(confirmations):
                    if not batch.future.done():
                        continue
                    confirmations.remove(batch)
                    changed = True
                    try:
                        completed = batch.future.result()
                    except Exception as error:
                        for saved in batch.records:
                            _action_failed(saved, error, stopping=False)
                        continue
                    if completed.no_remote_control:
                        sends.append(notifications.submit(_remote_control_alert, completed.records, completed.no_remote_control))
                    for saved in completed.records:
                        found = store.lookup_session(login, saved["session"]["session_id"])
                        if isinstance(found, store.ShutdownFound) and found.record["progress"]["kind"] == "restarted":
                            archive(found.record)

                for identity, future in list(messages.items()):
                    if future.done():
                        del messages[identity]
                        try:
                            future.result()
                        except store.NoLiveRecord:
                            pass
                sends = [future for future in sends if not future.done()]

                records = store.session_records(login)
                _clear_finished_hold(login, archived)
                if not pending and not confirmations and not launched and (not records or all(_done(saved) for saved in records)):
                    for saved in records:
                        if saved["progress"]["kind"] == "restarted":
                            archive(saved)
                    break
                current = time.monotonic()
                due = current >= next_inventory_at
                if due:
                    report = run_inventory(login)
                    observed_ids = {saved["session"]["session_id"] for saved in records}
                    attempted.clear()
                    next_inventory_at = current + SETTLE_INTERVAL_SECONDS
                if due or changed:
                    holdouts: list[tuple[SessionShutdown, str]] = []
                    confirming_ids = {saved["session"]["session_id"] for batch in confirmations for saved in batch.records}
                    confirming_ids.update(saved["session"]["session_id"] for saved in launched)
                    for saved in records:
                        session_id = saved["session"]["session_id"]
                        if session_id in pending or session_id in confirming_ids or session_id not in observed_ids:
                            continue
                        owner = stop_work.fresh_session(saved["session"], report)
                        change = _usage_limit_change(saved, owner)
                        if not isinstance(change, NoUsageLimitChange):
                            updated = _apply_usage_limit_change(saved, change)
                            if isinstance(updated, UsageLimitChangeRefused):
                                continue
                            saved = updated
                        step = next_step(saved, owner, records, now_utc())
                        if (due and saved["progress"]["kind"] in {"waiting", "ready"}
                            and (saved["progress"]["kind"] == "waiting" or isinstance(step, Hold))
                            and _holdout_due(saved, now_utc(), form=True)):
                            reason = hold_reason(saved, owner)
                            if _reserve_holdout_alert(saved, now_utc(), form=_shows_form(reason)):
                                holdouts.append((saved, reason))
                        if (session_id, type(step)) in attempted:
                            continue
                        if isinstance(step, SendSettleMessage):
                            attempted.add((session_id, type(step)))
                            if session_id not in messages:
                                messages[session_id] = notifications.submit(_send_settle_message, saved)
                        elif isinstance(step, StopSession):
                            attempted.add((session_id, type(step)))
                            future = executor.submit(_stop_after_telling, saved)
                            pending[session_id] = PendingSessionAction(saved, step, future)
                        elif isinstance(step, RelaunchSession):
                            attempted.add((session_id, type(step)))
                            start_launch(saved)
                        elif isinstance(step, Finished) and saved["progress"]["kind"] == "restarted":
                            archive(saved)
                    if holdouts:
                        sends.append(notifications.submit(_send_holdout_alert, holdouts))
                if launched and (due or not any(isinstance(action.step, RelaunchSession) for action in pending.values())):
                    batch_records, launched = launched, []
                    future = registry.submit(_confirm_relaunch_pass, batch_records)
                    confirmations.append(PendingRegistryConfirmation(batch_records, future))
                # wait observes completion without setting either result type.
                futures = [cast(Future[object], cast(object, action.future)) for action in pending.values()]
                futures.extend(cast(Future[object], cast(object, batch.future)) for batch in confirmations)
                if futures:
                    _ = wait(futures, timeout=max(0.0, next_inventory_at - time.monotonic()), return_when=FIRST_COMPLETED)
                else:
                    time.sleep(max(0.0, next_inventory_at - time.monotonic()))
        _clear_finished_hold(login, archived)
        return
    finally:
        desktop.restore(dry_run=False)
