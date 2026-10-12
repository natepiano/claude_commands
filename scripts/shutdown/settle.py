#!/usr/bin/env python3
"""Conduct each account session through its own safe stop and optional restore."""
from __future__ import annotations

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
from typing import Literal, TypedDict, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import Account
import conversation_pause
from inventory import Inventory, Session, inventory
import record as store
from record import SessionShutdown, SessionStopIssue, TimerRestore, ShowrunnerFooter, NoFooter, ShutdownContinuation, ReadinessCheck
from remote import other_machine, run_remote
import showrunner_footer
import stop as stop_work
from user_action import ActionRequired

SHUTDOWN = Path(__file__).resolve().parent / "shutdown.py"
NOTIFIER = SCRIPTS / "message" / "notifier.sh"
SEND = SCRIPTS / "message" / "send.py"
SETTLE_INTERVAL_SECONDS = 15
SETTLE_RETRY_SECONDS = 300

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

def _pacific(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(
        ZoneInfo("America/Los_Angeles")
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
    _ = _deliver_message(command, "user", text)

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

def _settle_text(record: SessionShutdown, session: Session) -> str:
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
    if record["progress"]["kind"] in {"waiting", "ready"} and not isinstance(owner, _StatusUnobserved) and owner["kind"] == "matching session":
        try:
            detail = ", ".join(_hold_details(owner, local_observation=local_observation))
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
) -> list[SessionShutdown]:
    """Add independent owners, leaving every existing request intact."""
    for line in store.migrate(login):
        print(line)
    ids = tuple(only)
    report = run_inventory(login, ids)
    at = record_time()
    origin: store.RequestOrigin = ({"kind": "session", "session_id": requested_by}
                                   if requested_by else {"kind": "terminal"})
    if not ids and store.account_hold(login) is None:
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


def _hold_details(owner: stop_work.FreshSession, *, local_observation: bool = True) -> list[str]:
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
    if local_observation and _showing_form(session):
        details.append("showing a form")
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
    details = _hold_details(owner, local_observation=local_observation)
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
    if _is_requester(saved) and any(
        item["request_id"] == saved["request_id"]
        and item["session"]["session_id"] != saved["session"]["session_id"]
        and not _done(item) for item in others
    ):
        return Hold("requester goes last")
    if _stop_retry_pending(saved):
        return StopSession()
    if owner["kind"] == "session account unreadable" and progress in {"waiting", "ready"}:
        return Hold("account unreadable")
    if progress in {"waiting", "ready"}:
        if owner["kind"] in {"session absent", "session identity lost"}:
            return StopSession()
        if owner["kind"] == "matching session":
            if saved["force"] == "now":
                return StopSession()
            if progress == "ready" and owner["session"]["status"] == "idle" and all(
                seat["status"] == "idle" for seat in owner["seats"]
            ) and blocker(owner["session"])["kind"] == "clear" and not _showing_form(owner["session"]):
                return StopSession()
        if progress == "waiting" and not _is_requester(saved):
            message = saved["settle_message"]
            if message["kind"] == "not sent" or (message["kind"] == "queued" and
                now - datetime.fromisoformat(message["at"]) >= timedelta(seconds=SETTLE_RETRY_SECONDS)):
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
    delivery = send_message(f"session:{session['session_id']}",
        f"Shutdown of {saved['label']}: reach a safe stop", _settle_text(saved, session),
        key=f"shutdown-{saved['login']}-{session['session_id']}")
    at = record_time()
    def save(current: SessionShutdown) -> None:
        if current["progress"]["kind"] != "waiting":
            return
        current["settle_message"] = ({"kind": "sent", "at": at} if delivery["kind"] == "sent"
                                    else {"kind": "queued", "at": at, "reason": delivery["reason"]})
    _ = store.update_session(saved["login"], session["session_id"], save)


def ready(where: str) -> int:
    """Record this owner's next step without replacing its process identity."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for saved in store.all_session_records():
        if saved["session"]["session_id"] != session_id or saved["progress"]["kind"] not in {"waiting", "ready"}:
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
            if current["progress"]["kind"] in {"waiting", "ready"}:
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
        case "unit tmux session left running":
            return f"tmux session {issue['tmux_session']} left running: {issue['cause']}"


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


def cancel(account: Account, here: bool = False) -> int:
    """Withdraw waiting owners here, then ask the peer to withdraw its own."""
    for line in store.migrate(account.login):
        print(line)
    retained = False
    for saved in store.session_records(account.login):
        try:
            outcome = store.withdraw_session(account.login, saved["session"]["session_id"], _restore_record)
        except store.NoLiveRecord:
            continue
        if isinstance(outcome, store.SessionRetained):
            retained = True
            name = outcome.record["session"]["name"]
            detail = _progress_text(outcome.record)
            print(f"{name} was not cancelled: {detail}; /shutdown restart brings it back")
    store.clear_account_hold(account.login)
    if not here:
        status, output = run_remote(["cancel", account.login, "--here"])
        if output.strip():
            print(output.strip())
        if status not in {0, 1}:
            print(f"{other_machine()}: not reached; run /shutdown there", file=sys.stderr)
            return 1
        retained = retained or status == 1
    return 1 if retained else 0


def now(account: Account, here: bool = False) -> int:
    """Force waiting owners and retry owners whose stop failed."""
    for line in store.migrate(account.login):
        print(line)
    changed = False
    for saved in store.session_records(account.login):
        if saved["progress"]["kind"] not in {"waiting", "ready"} and not store.stop_incomplete(saved):
            continue
        def force(current: SessionShutdown) -> None:
            if current["progress"]["kind"] in {"waiting", "ready"} or store.stop_incomplete(current):
                current["force"] = "now"
        _ = store.update_session(account.login, saved["session"]["session_id"], force)
        changed = True
    if changed:
        _launch_conductor(account.login, account.label)
    print(f"{local_machine()}: stopping now" if changed else f"{local_machine()}: no shutdown in progress")
    if not here:
        status, output = run_remote(["now", account.login, "--here"])
        if output.strip():
            print(output.strip())
        if status != 0:
            print(f"{other_machine()}: not reached; run /shutdown there", file=sys.stderr)
            return 1
    return 0


def _launch_conductor(login: str, label: str) -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "account"
    service = f"shutdown-{slug}-{os.getpid()}"
    command = [sys.executable, str(SHUTDOWN), "conduct", login]
    launch = (["launchctl", "submit", "-l", service, "--", *command] if sys.platform == "darwin"
              else ["systemd-run", "--user", "--collect", "--quiet", "--no-block", "--unit", service, *command])
    result = subprocess.run(launch, check=False, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"could not start shutdown conductor {service}")


def down(account: Account, here: bool = False, only: Iterable[str] = (), *,
         then: ShutdownContinuation = "stop", alerts: Literal["user", "none"] = "user",
         requested_by: str | None = None, request_id: str = "") -> int:
    """Start local work immediately and ask the peer to conduct its own owners."""
    if not _warm():
        print(f"GitHub keys are cold: run `github-warmup` in a terminal on {_machine()}, then `/shutdown` again", file=sys.stderr)
        return 1
    ids = tuple(only)
    origin = requested_by or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    identity = request_id or str(uuid4())
    _ = begin(account.login, origin, ids, then=then, alerts=alerts, request_id=identity)
    _launch_conductor(account.login, account.label)
    if not here:
        arguments = ["down", account.login, "--here", "--request-id", identity]
        if origin:
            arguments.extend(("--requested-by", origin))
        if ids:
            arguments.extend(("--only", ",".join(ids)))
        if then == "restart":
            arguments.append("--restart")
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
    def fail(current: SessionShutdown) -> None:
        current["progress"] = ({"kind": "stop failed", "at": record_time(), "reason": reason}
                               if stopping else {"kind": "restart failed", "at": record_time(), "reason": reason})
        if stopping:
            current["force"] = "wait for ready"
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], fail)
    except store.NoLiveRecord:
        pass


def report_stop(records: list[SessionShutdown]) -> None:
    """Report local outcomes alongside one current observation of the peer."""
    if not records:
        return
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
    text = "\n".join(lines)
    origins = {saved["requested_by"]["session_id"] for saved in records if saved["requested_by"]["kind"] == "session"}
    try:
        if any(saved["hold"] == "account" and saved["alerts"] == "user" for saved in failed):
            alert_user(summary, text, ActionRequired("Run /shutdown now to retry the failed stop."),
                       machine="natedev" if local_machine().casefold() == "mac" else "")
        elif any(saved["alerts"] == "user" for saved in restore_failed):
            alert_user(summary, text, ActionRequired("Run /shutdown restart to finish restoring these sessions."),
                       machine="natedev" if local_machine().casefold() == "mac" else "")
    finally:
        for origin in sorted(origins):
            _ = send_message(f"session:{origin}", summary, text, key=f"shutdown-result-{first['login']}")


@dataclass(frozen=True)
class PendingSessionAction:
    record: SessionShutdown
    step: StopSession | RelaunchSession
    future: Future[SessionShutdown]


@dataclass(frozen=True)
class PendingRegistryConfirmation:
    records: list[SessionShutdown]
    future: Future[frozenset[str]]


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
    hold = store.account_hold(login)
    if hold is not None and any(saved["request_id"] == hold["request_id"]
        and saved["progress"]["kind"] == "restarted" for saved in seen.values()
    ) and not any(saved["request_id"] == hold["request_id"] for saved in store.session_records(login)):
        store.clear_account_hold(login)


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
        with ThreadPoolExecutor(max_workers=restart_work.RESTART_LAUNCH_WORKERS) as executor, ThreadPoolExecutor(max_workers=restart_work.RESTART_LAUNCH_WORKERS) as registry:
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
                        live = batch.future.result()
                    except Exception as error:
                        for saved in batch.records:
                            _action_failed(saved, error, stopping=False)
                        continue
                    for saved in batch.records:
                        claim = saved["progress"]
                        if claim["kind"] != "restarting":
                            continue
                        try:
                            restart_work.confirm_entry(saved, claim["at"], saved["session"]["session_id"] in live)
                        except store.NoLiveRecord:
                            continue
                        except Exception as error:
                            _action_failed(saved, error, stopping=False)
                        latest = store.find_session(login, saved["session"]["session_id"])
                        if latest is not None:
                            if latest["progress"]["kind"] == "restarted":
                                archive(latest)

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
                    confirming_ids = {saved["session"]["session_id"] for batch in confirmations for saved in batch.records}
                    confirming_ids.update(saved["session"]["session_id"] for saved in launched)
                    for saved in records:
                        session_id = saved["session"]["session_id"]
                        if session_id in pending or session_id in confirming_ids or session_id not in observed_ids:
                            continue
                        owner = stop_work.fresh_session(saved["session"], report)
                        step = next_step(saved, owner, records, now_utc())
                        if (session_id, type(step)) in attempted:
                            continue
                        if isinstance(step, SendSettleMessage):
                            attempted.add((session_id, type(step)))
                            try:
                                _send_settle_message(saved)
                            except store.NoLiveRecord:
                                pass
                        elif isinstance(step, StopSession):
                            attempted.add((session_id, type(step)))
                            future = executor.submit(stop_work.stop_session, saved)
                            pending[session_id] = PendingSessionAction(saved, step, future)
                        elif isinstance(step, RelaunchSession):
                            attempted.add((session_id, type(step)))
                            start_launch(saved)
                        elif isinstance(step, Finished) and saved["progress"]["kind"] == "restarted":
                            archive(saved)
                if launched and (due or not any(isinstance(action.step, RelaunchSession) for action in pending.values())):
                    batch_records, launched = launched, []
                    future = registry.submit(restart_work.wait_for_sessions,
                        frozenset(saved["session"]["session_id"] for saved in batch_records), restart_work.SESSION_WAIT_SECONDS)
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
