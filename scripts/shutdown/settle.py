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
import tempfile
import time
import threading
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks", "lib", "herdr"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import Account
import conversation_pause
import detached
import herdr
import note_file
from inventory import Inventory, Session, inventory, last_tool_call
from launch_permission import shutdown_state_root
import launch_permission
import shutdown_record as store
from shutdown_record import CheckoutMove, SessionShutdown, SessionStopIssue, TimerRestore, ShutdownContinuation, ReadinessCheck
from remote import other_machine, run_remote
from scripts.production import staff_sources
import showrunners
import sessions
import stop as stop_work
from user_zone import user_zone
from scripts.accounts.accounts import HAND_ORIGIN
from scripts.accounts import outage_alerts
if TYPE_CHECKING:
    from ..accounts import refusals, accounts
    from . import restart as restart_work
    from ..notify import review
    from ..notify.push_texts import RenderedPush, render, user_send_argv
else:
    import review
    from push_texts import RenderedPush, render, user_send_argv

SHUTDOWN = Path(__file__).resolve().parent / "shutdown.py"
NOTIFIER = SCRIPTS / "message" / "notifier.sh"
SEND = SCRIPTS / "message" / "send.py"
SETTLE_INTERVAL_SECONDS = 15
SETTLE_RETRY_SECONDS = 300
# A unit in the middle of /unit:direct answered a pause message in about a minute (2026-10-13).
REASK_AFTER = timedelta(minutes=2)
NOT_ANSWERING_AFTER = 2 * REASK_AFTER
# A move waits this long for its ready mark, then stops the session anyway: a move is never held.
MOVE_ANSWER_AFTER = timedelta(minutes=2)
_STOPPABLE_STATUSES = frozenset({"idle", "shell"})
SHOWING_FORM = "showing a form"
USAGE_LIMIT_PATTERNS = (
    "hit your session limit",
    "hit your usage limit",
    "hit your weekly limit",
    "usage limit reached",
    "wait for limit to reset",
)


@dataclass(frozen=True)
class PromptSubject:
    session_id: str
    login: str
    config_dir: Path


@dataclass(frozen=True)
class TakesPrompts:
    because: Literal["room", "credits", "no fresh reading"]


@dataclass(frozen=True)
class StoppedTakingPrompts:
    because: Literal["refused in its transcript", "account out of usage"]
    detail: str


SessionPromptState = TakesPrompts | StoppedTakingPrompts


@dataclass(frozen=True)
class InventoryNeeded:
    """A direct caller has not yet observed the account's live sessions."""


_INVENTORY_NEEDED = InventoryNeeded()


class RequestFollower(Protocol):
    def follow(self, login: str, request_id: str) -> int: ...


def _limit_detail(window: str, reset: refusals.KnownReset | refusals.NoResetKnown) -> str:
    from scripts.accounts import refusals
    detail = ("weekly" if window == "weekly" else "5-hour" if window == "five_hour" else "usage") + " limit"
    if isinstance(reset, refusals.KnownReset):
        detail += ", resets " + reset.at.astimezone(user_zone()).strftime("%a %-I:%M %p")
    return detail


def prompt_state(session: PromptSubject, now: datetime) -> SessionPromptState:
    """Decide whether this Claude session can receive its shutdown pause."""
    from scripts.accounts import refusal_watch, refusals
    try:
        refusal = refusal_watch.transcript_refusal(session.session_id, config_dir=session.config_dir)
    except (OSError, ValueError):
        refusal = refusal_watch.NoSessionRefusal()
    if isinstance(refusal, refusal_watch.TranscriptRefusal):
        window = refusal.window.name if isinstance(refusal.window, refusals.KnownWindow) else "unknown"
        return StoppedTakingPrompts("refused in its transcript", _limit_detail(window, refusal.reset))
    reading = refusals.stored_reading("claude", session.login, now)
    if not isinstance(reading, refusals.FreshReading):
        return TakesPrompts("no fresh reading")
    for name, window in (("weekly", reading.weekly), ("five_hour", reading.five_hour)):
        if isinstance(window, refusals.ReadingWindow) and window.left <= 0:
            return StoppedTakingPrompts("account out of usage", _limit_detail(name, window.reset))
    if isinstance(reading.five_hour, refusals.NoWindow):
        return TakesPrompts("no fresh reading")
    return TakesPrompts("room")


@dataclass(frozen=True)
class ScreenText:
    text: str


@dataclass(frozen=True)
class ScreenUnreadable:
    reason: Literal["no pane host", "no pane id", "capture failed", "session registry identity changed"]


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

    idle_reason: stop_work.IdleStopReason = "not an idle stop"
    # A move past its answer time stops the owner without its ready mark.
    forced: bool = False
    # The owner was in a turn when the forced stop was chosen; its resume note names the last step.
    mid_turn: bool = False

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


def elapsed_text(seconds: int) -> str:
    """Format an elapsed duration in whole seconds, minutes, hours, or days."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"

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


def stopped_sessions_text(count: int) -> str:
    """Name the number of sessions still waiting on a stop or restart."""
    return "One session" if count == 1 else f"{count} sessions"


def alert_user(push: RenderedPush, *, machine: str = "") -> bool:
    """Send a registry action through the shutdown alert path."""
    command = [*_send_command(), *user_send_argv(push, machine=machine)[2:]]
    try:
        return _deliver_message(command, "user", "")["kind"] == "sent"
    except (OSError, RuntimeError) as error:
        print(f"shutdown: user alert failed: {error}", file=sys.stderr)
        return False


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

def _pause_timers(session_id: str) -> list[TimerRestore]:
    released = conversation_pause.release(session_id)
    if isinstance(released, conversation_pause.NoPauseRecord):
        return []
    restore = not isinstance(released.phase, conversation_pause.KeptOff)
    return [{"instance": instance, "was_enabled": restore} for instance in released.instances]

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

def blocker(session: Session, *, moving: bool = False) -> ReadinessCheck:
    """Check whether outstanding work prevents this session from stopping.

    A move keeps the checkout and pauses Codex workers during owner cleanup.
    """
    checkout = session["checkout"]
    if (
        not moving
        and checkout["kind"] == "git"
        and checkout["upstream"]["kind"] == "tracking"
        and checkout["upstream"]["ahead"] > 0
    ):
        head = checkout["head"]
        branch = head["name"] if head["kind"] == "branch" else head["commit"]
        return {"kind": "blocked", "reason": f"push {branch} first"}
    for server in session["codex_servers"]:
        if not moving and server["busy_workers"]:
            return {
                "kind": "blocked",
                "reason": f"Codex worker {server['busy_workers'][0]} is still running",
            }
    if merge_in_progress(session):
        return {"kind": "blocked", "reason": "finish the merge in progress first"}
    return {"kind": "clear"}

def _screen(session: Session) -> ScreenText | ScreenUnreadable:
    from session_pane import read_place
    from herdr import HerdrFailure, HerdrPlace

    host = session["host"]
    if host["kind"] == "herdr":
        place = HerdrPlace(host["herdr_session"], host["pane_id"])
    elif host["kind"] == "unit":
        registered = sessions.read_session(sessions.sessions_dir() / f"{session['pid']}.json")
        if isinstance(registered, sessions.UnreadableSessionRecord) or registered["sessionId"] != session["session_id"]:
            return ScreenUnreadable("session registry identity changed")
        place = herdr.herdr_place_of(session["pid"])
        if not isinstance(place, HerdrPlace):
            return ScreenUnreadable("no pane id")
    else:
        return ScreenUnreadable("no pane host")
    try:
        return ScreenText(read_place(place, 60))
    except HerdrFailure:
        return ScreenUnreadable("capture failed")


# A prompt that waits for the user replaces the input box with numbered choices, one selected, and a key footer.
_PROMPT_CHOICE = re.compile(r"^\s*❯\s*\d+\.")
_PROMPT_FOOTERS = ("esc to cancel", "enter to confirm", "enter to select")
_INPUT_RULE = "──"
_PROMPT_LINES = 25


def waits_on_prompt(text: str) -> bool:
    """Whether the bottom of this pane text is a prompt that waits for the user.

    Words in the transcript never count: the prompt needs its selected choice and its key footer,
    and an input box on screen means the session is at its own prompt instead.
    """
    lines = [line for line in text.splitlines() if line.strip()][-_PROMPT_LINES:]
    if any(line.lstrip().startswith("❯") and not _PROMPT_CHOICE.match(line)
           and previous.lstrip().startswith(_INPUT_RULE) for previous, line in zip(lines, lines[1:])):
        return False
    return (any(_PROMPT_CHOICE.match(line) for line in lines)
            and any(footer in line.casefold() for line in lines for footer in _PROMPT_FOOTERS))


def _showing_form(session: Session) -> bool:
    # A busy session is working, so nothing on its screen waits for the user.
    if session["status"] == "busy":
        return False
    screen = _screen(session)
    return isinstance(screen, ScreenText) and waits_on_prompt(screen.text)


def _held_by_usage_limit(screen: ScreenText | ScreenUnreadable) -> bool:
    if isinstance(screen, ScreenUnreadable):
        return False
    lines = [line for line in screen.text.splitlines() if line.strip()][-15:]
    return any(pattern in line.casefold() for line in lines for pattern in USAGE_LIMIT_PATTERNS)


def _usage_limit_shown(session: Session) -> bool:
    from scripts.accounts.refusal_watch import session_refusal, TranscriptRefusal, ProofRefusal
    if session["status"] == "busy":
        return False
    try:
        if isinstance(session_refusal(session["session_id"]), (TranscriptRefusal, ProofRefusal)):
            return True
    except (OSError, ValueError):
        pass
    return _held_by_usage_limit(_screen(session))


def _reap_refusal_during_wait(saved: SessionShutdown) -> SessionShutdown:
    if saved["progress"]["kind"] != "waiting" or saved["settle_message"]["kind"] != "sent":
        return saved
    from scripts.accounts import refusal_watch, refusals
    try:
        refusal = refusal_watch.transcript_refusal(
            saved["session"]["session_id"], config_dir=Path(saved["session"].get("config_dir", str(Path.home() / ".claude"))),
        )
    except (OSError, ValueError):
        return saved
    if not isinstance(refusal, refusal_watch.TranscriptRefusal):
        return saved
    sent = saved["settle_message"]["at"]
    if refusal.refused_at.at <= datetime.fromisoformat(sent):
        return saved
    window = refusal.window.name if isinstance(refusal.window, refusals.KnownWindow) else "unknown"
    detail = _limit_detail(window, refusal.reset)

    def reap(current: SessionShutdown) -> None:
        if (current["request_id"] == saved["request_id"] and current["progress"]["kind"] == "waiting"
                and current["settle_message"]["kind"] == "sent" and current["settle_message"]["at"] == sent):
            current["progress"] = {"kind": "reaping", "at": record_time(),
                                   "because": "refused in its transcript", "detail": detail}
    return store.update_session(saved["login"], saved["session"]["session_id"], reap)


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


def running_restart_line(saved: SessionShutdown, *, for_showrunner: bool = False) -> str:
    """Explain an unconfirmed live restart and how to close its record."""
    command = shutdown_command("forget", saved["session"]["session_id"], "--account", saved["login"])
    name = saved["session"]["name"]
    subject = (f"{name} is back on {saved.get('target') or saved['label']}; its restart prompt was not confirmed"
               if for_showrunner else f"{name}: restart prompt not confirmed; session is running")
    return f"{subject} — check it, then close its record in a terminal on {saved['machine']}: {command}"


def failed_prompt_screen_lines(saved: SessionShutdown) -> list[str]:
    """Render a local failed restart's saved submit screen, when it was read."""
    if saved["progress"]["kind"] != "restart failed":
        return []
    import restart as restart_work
    screen = restart_work.saved_prompt_screen(saved)
    if isinstance(screen, herdr.PromptScreenNotRead):
        return []
    return ["  last screen of the failed prompt submit:",
            *("    " + line for line in screen.splitlines())]


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


def pause_confirmation_line(record: SessionShutdown) -> str:
    """Name the request's source and its local status check."""
    moving = bool(record.get("target", ""))
    reference = 'docs/accounts.md, "Move a session"' if moving else "docs/as-built/shutdown-and-restart.md"
    command = shutdown_command("status", note_file.canonical_label(record["label"]), "--here")
    return (
        f"This request comes from the shutdown the user started ({reference}), not from another session's own choice. "
        f"To confirm it, run in this terminal: {command}. It lists this session under the {'move' if moving else 'stop'}."
    )


RETIRE_REASON = "retired; /showrunner:resume_unit brings it back."


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
        started = "stop any build you started, or name it in that line"
        ending = " and end your turn with `— blocked: shutdown requested by the user`"
    elif session["kind"] == "showrunner":
        where = '"<one line>"'
        started = "append a `### STATE` block to your LOG, stop or name any build you started"
        ending = " and end your turn"
    else:
        where = '"<what you were doing and what comes next>"'
        started = "stop or name any build you started"
        ending = " and end your turn"
    codex = ("Leave Codex workers running; the mover pauses them after this session exits." if target
             else "If ready reports a running Codex worker, retry it when that turn finishes; start no new turns.")
    return first_line + "\n" + pause_confirmation_line(record) + "\n\n" + (
        f"Stop now, without finishing the step you are in, and start nothing new. Leave uncommitted work in place, "
        f"{started}. {codex} In this terminal run {command}{where}`{ending}. "
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
    elif entry_progress["kind"] == "reaping":
        progress += f" — {entry_progress['because']}: {entry_progress['detail']}"
    elif entry_progress["kind"] == "timers pending":
        timers = "; ".join(
            f"{timer['instance']}: {timer['reason']}"
            for timer in entry_progress["timers"]
        )
        progress += f" — {timers}"
    elif entry_progress["kind"] == "manual restart":
        progress += f" — in a terminal on {record['machine']}: {entry_progress['command']}"
    return located_detail(record, progress)


def located_detail(record: SessionShutdown, detail: str) -> str:
    """Keep commands inside stored progress readable with their execution place."""
    if any(word in detail for word in ("/shutdown", "shutdown.py", "accounts.py", "github-warm")) and not any(
        phrase in detail for phrase in ("in this terminal", "in a terminal on ", "in any Claude session on ")
    ):
        return f"in a terminal on {record['machine']}: {detail}"
    return detail


def status_record_lines(record: SessionShutdown, owner: stop_work.FreshSession | _StatusUnobserved = _STATUS_UNOBSERVED,
                        *, local_observation: bool = True) -> list[str]:
    """Render progress, observed readiness details and issues for one owner."""
    progress = _progress_text(record)
    target = record.get("target", "")
    if target:
        progress += f" · to {target}: {record.get('why', '')}"
    if record["progress"]["kind"] in {"waiting", "ready", "held by usage limit"} and not isinstance(owner, _StatusUnobserved) and owner["kind"] == "matching session":
        try:
            detail = ", ".join(_hold_details(owner, record["progress"]["kind"], local_observation=local_observation,
                                             moving=bool(target)))
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
    if store.legacy_tmux_leftovers(record):
        from restart import legacy_resume_command
        lines.append(f"    manual restart: {legacy_resume_command(record)}")
    for step in record.get("trail", []):
        at = datetime.fromisoformat(step["at"]).astimezone(user_zone()).strftime("%H:%M:%S")
        lines.append(f"    {at} {step['kind']} {located_detail(record, step['detail'])}".rstrip())
    lines.extend(f"    {stop_issue_text(issue)}" for issue in record["stop_issues"])
    lines.extend(f"    restart issue: {issue}" for issue in record.get("restart_issues", []))
    return lines


def run_inventory(
    login: str, only: Iterable[str] = (), *, refresh_desktops: bool = True,
) -> Inventory:
    """Observe the account once, including each owner's current workers."""
    return inventory(login, only=frozenset(only), refresh_desktops=refresh_desktops)


def _owned_workers(report: Inventory, session_id: str) -> list[Session]:
    return [item for item in report["sessions"] if item["kind"] == "worker"
            and item["owner"]["kind"] == "director"
            and item["owner"]["session_id"] == session_id]


def begin(
    login: str, requested_by: str | None = None, only: Iterable[str] = (), *,
    then: ShutdownContinuation = "stop", alerts: Literal["user", "none"] = "user",
    request_id: str,
    target: str = "", why: str = "", move_id: str = "", checkout_move: CheckoutMove | None = None,
    report: Inventory | InventoryNeeded = _INVENTORY_NEEDED,
    stop_deadline: str = "",
) -> list[SessionShutdown]:
    """Add independent owners, leaving every existing request intact."""
    for line in store.migrate(login):
        print(line)
    ids = tuple(only)
    if isinstance(report, InventoryNeeded):
        report = run_inventory(login, ids)
    at = record_time()
    origin: store.RequestOrigin = ({"kind": "session", "session_id": requested_by}
                                   if requested_by else {"kind": "terminal"})
    store.register_account_hold_request(store.AccountHold(
        login=login, label=report["label"], machine=report["machine"], requested_at=at,
        request_id=request_id, requested_by=origin, then=then,
    ), account_wide=not ids, expires_at=(datetime.fromisoformat(stop_deadline) + timedelta(
        seconds=stop_work.TERM_GRACE_SECONDS + 1)).isoformat() if stop_deadline else "")
    added: list[SessionShutdown] = []
    for session in report["sessions"]:
        if session["kind"] == "worker":
            continue
        state = prompt_state(PromptSubject(session["session_id"], login, Path(session.get("config_dir", str(Path.home() / ".claude")))), now_utc())
        progress: store.SessionProgress = (
            store.SessionReaping(kind="reaping", at=at, because=state.because, detail=state.detail)
            if isinstance(state, StoppedTakingPrompts) else store.SessionWaiting(kind="waiting")
        )
        probe = session["probe"]
        saved = SessionShutdown(
            login=login, label=report["label"], machine=report["machine"], requested_at=at,
            request_id=request_id, requested_by=origin, hold="session" if ids else "account",
            then=then, force="wait for ready", alerts="none" if probe else alerts,
            probe=probe, session=session,
            workers=_owned_workers(report, session["session_id"]), timers=[],
            settle_message={"kind": "not sent"}, where={"kind": "not said"},
            progress=progress, trail=[], stop_issues=[],
        )
        scopes = stop_work.owner_pane_scopes(session, saved["workers"])
        if scopes:
            saved["owner_pane_scopes"] = scopes
        if stop_deadline:
            saved["stop_deadline"] = stop_deadline
        if move_id:
            saved["move_id"] = move_id
        if checkout_move is not None:
            saved["checkout_move"] = checkout_move
        if why:
            saved["why"] = why
        if target:
            saved["target"] = target
            saved["source_label"] = report["label"]
            saved["why"] = why or f"{report['label']} is out of usage"
        if not store.create_session(saved):
            taken = _take_over_for_move(saved) if target else None
            if taken is None:
                previous = store.find_session(login, session["session_id"])
                if previous is not None and not store.request_matches_process(previous, session):
                    print(f"{session['name']}: shutdown is in flight for another process; retained its record", flush=True)
                else:
                    print(f"{session['name']}: already in progress")
            else:
                added.append(taken)
                print(f"{session['name']}: the move replaces its waiting stop")
            continue
        try:
            timers = _session_timer_snapshot(session)
            stop_errors: list[Exception] = []
            def save_timers(current: SessionShutdown) -> None:
                current["timers"] = store.merge_timer_restores(current["timers"], timers)
                try:
                    _stop_recorded_timers(current["timers"])
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
    store.complete_account_hold_registration(login, request_id)
    return added


def _revoke_launches(login: str, only: tuple[str, ...] = ()) -> None:
    # Revocation owns its short launch barrier. Never nest that flock here.
    for claim in launch_permission.revoke_pending_launches(login, only):
        if claim.pane:
            herdr.close_agent(claim.herdr_session, claim.pane)


def down_all() -> int:
    started = time.monotonic()
    deadline = store.RecordDeadline(started + stop_work.SHUTDOWN_GRACE_SECONDS + stop_work.TERM_GRACE_SECONDS + 1,
                                    lambda: time.monotonic(), lambda seconds: time.sleep(seconds))
    try:
        with store.record_deadline(deadline), launch_permission.block_launches_during_shutdown():
            return _down_all(started)
    except store.RecordDeadlineExpired as error:
        print(f"shutdown: {error}", file=sys.stderr, flush=True)
        return 1


def _down_all(started: float) -> int:
    """Request every local account together, then enforce their one shared deadline."""
    from inventory import all_live_accounts

    deadline = (now_utc() + timedelta(seconds=stop_work.SHUTDOWN_GRACE_SECONDS)).isoformat()
    request_id = str(uuid4())
    reports = all_live_accounts()
    with launch_permission.launch_barrier():
        pending = launch_permission.pending_launches()
    known = {report["login"] for report in reports}
    saved_logins = {saved["login"] for saved in store.all_session_records()
                    if not store.finished_stop_request(saved) or store.stop_incomplete(saved)}
    for login in sorted(({claim.login for claim in pending} | saved_logins) - known):
        reports.append(run_inventory(login))
    # Admit the account holds before revocation, so no later launch can slip past.
    for report in reports:
        empty = report.copy()
        empty["sessions"] = []
        _ = begin(report["login"], request_id=request_id, alerts="none", report=empty, stop_deadline=deadline)
        _revoke_launches(report["login"])
    final = {report["login"]: report for report in all_live_accounts()}
    for login in final.keys() - {report["login"] for report in reports}:
        report = final[login]
        empty = report.copy()
        empty["sessions"] = []
        _ = begin(login, request_id=request_id, alerts="none", report=empty, stop_deadline=deadline)
        _revoke_launches(login)
        reports.append(report)
    selected: list[SessionShutdown] = []
    kept_sessions: list[Session] = []
    refused = False
    requester = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for initial in reports:
        report = final.get(initial["login"], initial).copy()
        kept = [owner for owner in report["sessions"] if owner["kind"] != "worker"
                and stop_work.keep_infrastructure(owner)]
        kept_sessions.extend(kept)
        for owner in kept:
            previous = store.find_session(report["login"], owner["session_id"])
            if previous is not None:
                stop_work.retire_kept_request(previous)
            print(f"{owner['name']}: kept (protected infrastructure)", flush=True)
        kept_ids = {owner["session_id"] for owner in kept}
        report["sessions"] = [owner for owner in report["sessions"] if owner["session_id"] not in kept_ids]
        for owner in report["sessions"]:
            if owner["kind"] == "worker":
                continue
            previous = store.find_session(report["login"], owner["session_id"])
            if (previous is not None and not store.finished_stop_request(previous)
                    and not store.request_matches_process(previous, owner)):
                def adopt(current: SessionShutdown) -> None:
                    current["session"] = owner
                    current["workers"] = _owned_workers(report, owner["session_id"])
                    # Revoke the old restore's progress claim before it can confirm
                    # and archive the record while this process is being stopped.
                    current["progress"] = {"kind": "waiting"}
                _ = store.update_session(report["login"], owner["session_id"], adopt)
                print(f"{owner['name']}: stopping current process under its existing shutdown request", flush=True)
        _ = begin(report["login"], requester or None, request_id=request_id, alerts="none", report=report,
                  stop_deadline=deadline)
        live = {session["session_id"]: session for session in report["sessions"] if session["kind"] != "worker"}
        for saved in store.session_records(report["login"]):
            session_id = saved["session"]["session_id"]
            if session_id not in live and (session_id in kept_ids
                    or store.finished_stop_request(saved) and not store.stop_incomplete(saved)):
                continue
            if session_id in live and not store.request_matches_process(saved, live[session_id]):
                refused = True
                continue
            def bound(current: SessionShutdown) -> None:
                if current["progress"]["kind"] == "restarting":
                    current["progress"] = {"kind": "waiting"}
                current["stop_deadline"] = deadline
                # This stop now covers the account, even when it adopts a selected restore.
                current["hold"] = "account"
                current["then"] = "stop"
            selected.append(store.update_session(saved["login"], session_id, bound))

    def tell(saved: SessionShutdown) -> None:
        try:
            with store.record_deadline(store.RecordDeadline(
                    started + stop_work.SHUTDOWN_GRACE_SECONDS + stop_work.TERM_GRACE_SECONDS + 1,
                    lambda: time.monotonic(), lambda seconds: time.sleep(seconds))):
                _send_settle_message(saved)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"shutdown: {saved['session']['name']}: pause delivery: {error}", file=sys.stderr, flush=True)

    def request_exit(saved: SessionShutdown) -> None:
        threading.Thread(target=tell, args=(saved,), daemon=True).start()
    deferred = [saved for saved in selected if saved["session"]["session_id"] == requester]
    stopped = stop_work.bounded_stop([saved for saved in selected if saved not in deferred],
        started=started, machine_wide=True, request_exit=request_exit,
        keep=tuple([*kept_sessions, *(saved["session"] for saved in deferred)]))
    for saved in deferred:
        stop_work.defer_requester(saved)
    for report in reports:
        _ = store.release_finished_account_hold(report["login"])
    return 0 if not refused and len(stopped) == len(selected) - len(deferred) and all(
        saved["progress"]["kind"] == "stopped" and not store.stop_incomplete(saved) for saved in stopped
    ) else 1


def _carry_trust(saved: SessionShutdown, target: str) -> None:
    """Before the stop, give the target account the source account's trust for the session's folder.

    Without it the restart on the target refuses; a folder the source never trusted stays untrusted.
    """
    from scripts.accounts import accounts
    session = saved["session"]
    config_dir = Path(session.get("config_dir", str(Path.home() / ".claude")))
    source = Path.home() / ".claude.json" if config_dir == Path.home() / ".claude" else config_dir / ".claude.json"
    try:
        destination = accounts.account_dir(target) / ".claude.json"
    except accounts.AccountsError:
        return
    outcome = herdr.copy_claude_trust(Path(session["cwd"]), source, destination)
    # An account with no file yet refuses at restart with its own trust command.
    if isinstance(outcome, herdr.UnreadableTrustFile) and outcome.error_class != "FileNotFoundError":
        print(f"{session['name']}: folder trust not copied to {target}: {outcome}", file=sys.stderr)


def _take_over_for_move(move: SessionShutdown) -> SessionShutdown | None:
    """Turn this session's waiting stop into the move: the later request wins.

    The record keeps its request, pause message and saved timers. A record that is already a move,
    or that has begun to stop, is left alone.
    """
    target, source_label, why = move.get("target", ""), move.get("source_label", ""), move.get("why", "")
    taken = False
    def change(current: SessionShutdown) -> None:
        nonlocal taken
        if (current.get("target") or current["progress"]["kind"] not in {"waiting", "ready", "held by usage limit"}
                or not store.request_matches_process(current, move["session"])):
            return
        current["then"] = move["then"]
        current["target"], current["source_label"], current["why"] = target, source_label, why
        if "move_id" in move:
            current["move_id"] = move["move_id"]
        taken = True
    try:
        updated = store.update_session(move["login"], move["session"]["session_id"], change)
    except store.NoLiveRecord:
        return None
    return updated if taken else None


def _is_requester(saved: SessionShutdown) -> bool:
    origin = saved["requested_by"]
    return origin["kind"] == "session" and origin["session_id"] == saved["session"]["session_id"]


def stoppable_status(status: str) -> bool:
    """Whether the owner is at its prompt, including with a background shell."""
    return status in _STOPPABLE_STATUSES


def _hold_details(owner: stop_work.FreshSession, progress: str, *, local_observation: bool = True,
                  moving: bool = False) -> list[str]:
    """Return observed hold conditions without delivery state."""
    if owner["kind"] == "session account unreadable":
        return ["account unreadable"]
    if owner["kind"] != "matching session":
        return [owner["kind"].removeprefix("session ")]
    session = owner["session"]
    details: list[str] = []
    if session["status"] not in _STOPPABLE_STATUSES:
        details.append(session["status"])
    if not moving and session["status"] != "busy" and any(server["busy_workers"] for server in session["codex_servers"]):
        details.append("busy")
    if local_observation and progress != "held by usage limit" and _showing_form(session):
        details.append(SHOWING_FORM)
    checkout = session["checkout"]
    if not moving and checkout["kind"] == "git" and checkout["upstream"]["kind"] == "tracking":
        ahead = checkout["upstream"]["ahead"]
        if ahead:
            head = checkout["head"]
            branch = head["name"] if head["kind"] == "branch" else head["commit"]
            details.append(f"ahead {ahead} on {branch}")
    if local_observation and merge_in_progress(session):
        details.append("merge in progress")
    if not moving:
        for server in session["codex_servers"]:
            details.extend(f"Codex worker {name} is still running" for name in server["busy_workers"])
    details.extend(f"Codex worker {worker['name']} is still running" for worker in owner["workers"]
                   if worker["status"] != "idle")
    return details


def hold_reason(saved: SessionShutdown, owner: stop_work.FreshSession, *, local_observation: bool = True) -> str:
    """Describe the condition preventing this owner from advancing."""
    details = _hold_details(owner, saved["progress"]["kind"], local_observation=local_observation,
                            moving=bool(saved.get("target")))
    message = saved["settle_message"]
    if owner["kind"] == "matching session" and message["kind"] == "queued":
        details.append(f"message queued: {message['reason']}")
    return ", ".join(details) or saved["progress"]["kind"]


def move_overdue(saved: SessionShutdown, now: datetime) -> bool:
    """Whether a move has waited its full answer time.

    The time counts from the delivered pause message, or from the request when no message was delivered.
    """
    if not saved.get("target"):
        return False
    message = saved["settle_message"]
    asked = message["at"] if message["kind"] == "sent" else saved["requested_at"]
    return now - datetime.fromisoformat(asked) >= MOVE_ANSWER_AFTER


def next_step(saved: SessionShutdown, owner: stop_work.FreshSession,
              others: list[SessionShutdown], now: datetime) -> Step:
    """Choose only this owner's next action from a single machine observation."""
    if reason := stop_work.stop_admission(owner):
        return Hold(reason)
    progress = saved["progress"]["kind"]
    if "stop_deadline" in saved:
        if store.bounded_attempt_finished(saved):
            return Finished()
        if progress in {"stopped", "already gone", "process identity lost"} and saved["then"] == "restart":
            return RelaunchSession()
        if now >= datetime.fromisoformat(saved["stop_deadline"]) + timedelta(seconds=stop_work.TERM_GRACE_SECONDS + 1):
            return StopSession()
        return Hold("bounded shutdown owns this stop")
    if progress == "restarted":
        return Finished()
    if progress in {"stopped", "already gone", "process identity lost"} and saved["then"] == "stop" and not _stop_retry_pending(saved):
        return Finished()
    if progress == "reaping":
        if owner["kind"] != "session account unreadable":
            return StopSession()
        return StopSession(forced=True) if move_overdue(saved, now) else Hold("account unreadable")
    # `now` stops the requester too, without waiting for the rest of its request.
    if saved["force"] != "now" and _is_requester(saved) and any(
        item["request_id"] == saved["request_id"]
        and item["session"]["session_id"] != saved["session"]["session_id"]
        and not _done(item) for item in others
    ):
        return Hold("requester goes last")
    if _stop_retry_pending(saved):
        return StopSession()
    if (progress in {"waiting", "ready", "held by usage limit"} and saved["force"] != "now"
            and owner["kind"] in {"matching session", "session account unreadable"} and move_overdue(saved, now)):
        return StopSession(forced=True, mid_turn=owner["kind"] == "matching session"
                           and owner["session"]["status"] == "busy")
    if owner["kind"] == "session account unreadable" and progress in {"waiting", "ready", "held by usage limit"}:
        return Hold("account unreadable")
    if progress in {"waiting", "ready", "held by usage limit"}:
        if owner["kind"] == "session absent":
            return StopSession()
        if owner["kind"] == "matching session":
            if saved["force"] == "now":
                return StopSession()
            if owner["session"]["status"] in _STOPPABLE_STATUSES and all(
                worker["status"] == "idle" for worker in owner["workers"]
            ) and blocker(owner["session"], moving=bool(saved.get("target")))["kind"] == "clear" and (progress == "held by usage limit" or not _showing_form(owner["session"])):
                if progress in {"ready", "held by usage limit"}:
                    return StopSession()
                message = saved["settle_message"]
                if message["kind"] in {"not sent", "queued"}:
                    return StopSession("idle; nothing in progress; a background shell was running"
                        if owner["session"]["status"] == "shell" else "idle; nothing in progress")
                if message["kind"] == "sent":
                    updated_at = datetime.fromtimestamp(owner["session"]["updated_at"] / 1000, timezone.utc)
                    if updated_at > datetime.fromisoformat(message["at"]):
                        return StopSession("idle after the pause message; no ready line; a background shell was running"
                            if owner["session"]["status"] == "shell" else "idle after the pause message; no ready line")
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


def request_pause(saved: SessionShutdown) -> None:
    """Ask a recorded owner to checkpoint before its bounded stop."""
    _send_settle_message(saved)


def ready(where: str) -> int:
    """Record this owner's next step without replacing its process identity."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for saved in store.all_session_records():
        if saved["session"]["session_id"] != session_id:
            continue
        if saved["progress"]["kind"] not in {"waiting", "ready", "held by usage limit"}:
            print(f"ready has nothing to check: this session's shutdown is {saved['progress']['kind']}; "
                  + f"in this terminal: {shutdown_command('status', saved['label'], '--here')}", file=sys.stderr)
            return 1
        owner = stop_work.fresh_session(saved["session"], run_inventory(saved["login"]))
        if owner["kind"] != "matching session":
            print(f"this session is not in a fresh inventory of {saved['label']}: {owner['kind']}", file=sys.stderr)
            return 2
        check = (store.ReadinessClear(kind="clear") if "stop_deadline" in saved
                 else blocker(owner["session"], moving=bool(saved.get("target"))))
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
            if "stop_deadline" in saved:
                print(f"ready for shutdown: {where}", flush=True)
                return 0
            try:
                _launch_conductor(saved["login"], saved["label"])
            except (OSError, RuntimeError, subprocess.SubprocessError):
                command = shutdown_command("conduct", saved["login"])
                print(f"ready recorded; conductor could not start; in a terminal on {saved['machine']}: {command}", file=sys.stderr)
                return 2
            print(f"ready for shutdown: {where}")
            return 0
    print("no shutdown in progress for this session: no stop or move request is open; "
          + "ready only answers a request, it does not start one. "
          + f"In this terminal: {shutdown_command('status', '--here')}", file=sys.stderr)
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
        case "legacy tmux session left running":
            command = shlex.join(["tmux", "kill-session", "-t", "=" + issue["session_name"]])
            return f"legacy tmux session {issue['session_name']} left running: {issue['cause']}; {command}"
        case "unit session left running":
            return f"{issue['host']} session {issue['session_name']} left running: {issue['cause']}"


def _restore_record(saved: SessionShutdown) -> None:
    for timer in saved["timers"]:
        if timer["was_enabled"]:
            run_notifier("start", timer["instance"])
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
            if owner["session_id"] in missing and owner["kind"] != "worker"}


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
        # Inventory proves registry session id and live process identity. A
        # resumed process withdraws the old request without inheriting its work.
        report = inventory(account.login, frozenset({current["session"]["session_id"]}))
        return any(owner["kind"] != "worker" and owner["session_id"] == current["session"]["session_id"]
                   for owner in report["sessions"])
    for saved in records:
        if ids and saved["session"]["session_id"] not in ids:
            continue
        # Suppression takes the watcher lock before withdrawal takes the account lock.
        # The open record keeps concurrent watcher ticks from replacing this request.
        if saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"} or (
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
            failed_stop = outcome.record["progress"]["kind"] == "stop failed"
            action = shutdown_command("now" if failed_stop else "restart", account.login, "--here", "--only", outcome.record["session"]["session_id"])
            effect = "finishes the stop" if failed_stop else "brings it back"
            print(f"{name} was not cancelled: {detail}; in a terminal on {outcome.record['machine']}: {action} {effect}")
        else:
            withdrawn_requests.append(outcome.record["request_id"])
            session = outcome.record["session"]
            if outcome.record.get("target"):
                from scripts.accounts import moves
                moves.append_move(moves.FailedMove(kind="failed", at=record_time(), tool="claude",
                    subject=session["session_id"] + ": " + session["name"],
                    move_id=outcome.record.get("move_id", outcome.record["request_id"]),
                    request_id=outcome.record["request_id"], reason="cancelled"))
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
            print(f"{other_machine()}: not reached; in a terminal on {other_machine()}: {shutdown_command(*arguments)}", file=sys.stderr)
            return 1
        retained = retained or status == 1
    return 1 if retained else 0


def now(account: Account, here: bool = False, only: Iterable[str] = ()) -> int:
    started = time.monotonic()
    deadline = store.RecordDeadline(started + stop_work.TERM_GRACE_SECONDS + 1,
                                    lambda: time.monotonic(), lambda seconds: time.sleep(seconds))
    try:
        with store.record_deadline(deadline):
            return _now(account, here, only, started=started)
    except store.RecordDeadlineExpired as error:
        print(f"shutdown: {error}", file=sys.stderr, flush=True)
        return 1


def _now(account: Account, here: bool, only: Iterable[str], *, started: float) -> int:
    """Force waiting or reaping owners and retry owners whose stop failed."""
    for line in store.migrate(account.login):
        print(line)
    changed = False
    refused = False
    restart_pending = False
    ids = tuple(only)
    records = store.session_records(account.login)
    live = run_inventory(account.login, ids)
    recorded_ids = {saved["session"]["session_id"] for saved in records}
    live_owners = [owner for owner in live["sessions"] if owner["kind"] != "worker"
                   and owner["session_id"] in recorded_ids and (not ids or owner["session_id"] in ids)]
    kept_ids = {owner["session_id"] for owner in live_owners if stop_work.keep_infrastructure(owner)}
    for saved in records:
        if saved["session"]["session_id"] in kept_ids:
            stop_work.retire_kept_request(saved)
            print(f"{saved['session']['name']}: kept (protected infrastructure)", flush=True)
    if kept_ids:
        _ = store.release_finished_account_hold(account.login)
    live_owners = [owner for owner in live_owners if owner["session_id"] not in kept_ids]
    protected_ids: set[str] = kept_ids.copy()
    changed = bool(kept_ids)
    for owner in live_owners:
        previous = next(saved for saved in records if saved["session"]["session_id"] == owner["session_id"])
        if not store.finished_stop_request(previous) and not store.request_matches_process(previous, owner):
            protected_ids.add(owner["session_id"])
            print(f"{owner['name']}: shutdown is in flight for another process; retained its record", flush=True)
            refused = True
    live_owners = [owner for owner in live_owners if owner["session_id"] not in protected_ids]
    bounded_ids = {owner["session_id"] for owner in live_owners}
    retries = [saved for saved in records if "stop_deadline" in saved
               and saved["session"]["session_id"] not in bounded_ids | protected_ids
               and (not ids or saved["session"]["session_id"] in ids)
               and store.stop_incomplete(saved)]
    bounded_ids.update(saved["session"]["session_id"] for saved in retries)
    if live_owners or retries:
        selected_report = live.copy()
        selected_report["sessions"] = [owner for owner in live["sessions"]
            if owner["session_id"] in bounded_ids or (owner["kind"] == "worker"
                and owner["owner"]["kind"] == "director" and owner["owner"]["session_id"] in bounded_ids)]
        if live_owners:
            fresh = begin(account.login, only=bounded_ids, request_id=str(uuid4()),
                          alerts="none", report=selected_report, stop_deadline=record_time())
            del fresh
        _revoke_launches(account.login, tuple(bounded_ids))
        active = [saved for saved in store.session_records(account.login)
                  if saved["session"]["session_id"] in bounded_ids]
        for saved in active:
            def bounded(current: SessionShutdown) -> None:
                current["force"] = "now"
                current["stop_deadline"] = record_time()
                if store.stop_incomplete(current):
                    current["progress"] = {"kind": "waiting"}
            _ = store.update_session(account.login, saved["session"]["session_id"], bounded)
        active = [current for saved in active
                  if (current := store.find_session(saved["login"], saved["session"]["session_id"])) is not None]
        if ids:
            for saved in active:
                print(f"{saved['session']['name']}: stopping now", flush=True)
        stopped = stop_work.bounded_stop(active, started=started, grace_seconds=0)
        refused = refused or len(stopped) != sum(not stop_work.keep_infrastructure(saved["session"]) for saved in active) or any(
            saved["progress"]["kind"] != "stopped" or store.stop_incomplete(saved) for saved in stopped
        )
        restart_pending = any(saved["then"] == "restart" and saved["progress"]["kind"] == "stopped"
                              and not store.stop_incomplete(saved) for saved in stopped)
        _ = store.release_finished_account_hold(account.login)
        changed = True
    missing = unrecorded_local_sessions(account.login, ids, records)
    local_ids = {saved["session"]["session_id"] for saved in records} | missing.keys()
    for name in missing.values():
        print(f"{name}: no shutdown in progress")
        refused = True
    for saved in records:
        if saved["session"]["session_id"] in bounded_ids | protected_ids:
            continue
        if ids and saved["session"]["session_id"] not in ids:
            continue
        if (not ids and saved["progress"]["kind"] not in {"waiting", "ready", "held by usage limit", "reaping"}
                and not store.stop_incomplete(saved)
                and not store.legacy_tmux_leftovers(saved)):
            continue
        if store.legacy_tmux_leftovers(saved):
            from restart import legacy_resume_command
            print(f"manual restart {saved['session']['name']}: {legacy_resume_command(saved)}")
            for issue in store.legacy_tmux_leftovers(saved):
                print(f"  {stop_issue_text(issue)}")
            refused = True
            continue
        forced = False
        def force(current: SessionShutdown) -> None:
            nonlocal forced
            if current["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"} or store.stop_incomplete(current):
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
    if restart_pending or changed and any(saved["session"]["session_id"] not in bounded_ids | kept_ids for saved in records):
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
            print(f"{other_machine()}: not reached; in a terminal on {other_machine()}: {shutdown_command(*arguments)}", file=sys.stderr)
            return 1
        refused = refused or status == 1
    return 1 if refused else 0


def _launch_conductor(login: str, label: str) -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "account"
    service = f"shutdown-{slug}-{os.getpid()}"
    command = ["env", "PYTHONUNBUFFERED=1", sys.executable, "-u", str(SHUTDOWN), "conduct", login]
    if os.environ.get("REFUSAL_WATCH_INVENTORY") == "read existing":
        # Service managers do not inherit the caller's environment automatically.
        command = ["env", "REFUSAL_WATCH_INVENTORY=read existing", *command]
    if sys.platform == "darwin":
        try:
            logs = shutdown_state_root() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=logs, prefix=service + "-", suffix=".log", delete=False) as output:
                log = Path(output.name)
        except OSError as error:
            raise RuntimeError(f"could not start shutdown conductor {service}: {error}") from error
        try:
            _ = detached.start_detached(command, within=15,
                ready=lambda: "shutdown conductor started\n" in log.read_text(encoding="utf-8"),
                output=detached.AppendLog(log),
                environment=detached.ExplicitEnvironment({**os.environ, detached.STARTUP_SIGNAL_ENV: "1"}))
        except (detached.DetachedStartFailure, OSError) as error:
            raise RuntimeError(f"could not start shutdown conductor {service}: {error}; log: {log}") from error
        return
    launch = ["systemd-run", "--user", "--collect", "--quiet", "--no-block", "--unit", service, *command]
    result = subprocess.run(launch, check=False, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"could not start shutdown conductor {service}")


def down(account: Account, here: bool = False, only: Iterable[str] = (), *,
         then: ShutdownContinuation = "stop", alerts: Literal["user", "none"] = "user",
         requested_by: str | None = None, request_id: str = "", target: str = "", why: str = "",
         report: Inventory | InventoryNeeded = _INVENTORY_NEEDED,
         origin: accounts.MoveOrigin = HAND_ORIGIN, move_id: str = "",
         checkout_move: CheckoutMove | None = None) -> int:
    """Start local work immediately and ask the peer to conduct its own owners."""
    from scripts.accounts import accounts
    origin_arguments = accounts.move_origin_arguments(origin)
    ids = tuple(only)
    # A move never waits on cold keys: the session keeps its commits on the same machine.
    if not target and not _warm():
        retry = ["down", account.login]
        if here:
            retry.append("--here")
        elif not ids:
            retry.extend(("--also", other_machine()))
        if ids:
            retry.extend(("--only", ",".join(ids)))
        if then == "restart":
            retry.append("--restart")
        if alerts == "none":
            retry.append("--no-alert")
        print(f"GitHub keys are cold; in a terminal on {_machine()}: github-warm-status; github-warmup. Then in a terminal on {_machine()}: {shutdown_command(*retry)}", file=sys.stderr)
        return 1
    request_origin = requested_by or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    identity = request_id or str(uuid4())
    if isinstance(report, InventoryNeeded):
        report = run_inventory(account.login, ids)
    conflicts = [owner for owner in report["sessions"] if owner["kind"] != "worker"
                 and (previous := store.find_session(account.login, owner["session_id"])) is not None
                 and not store.finished_stop_request(previous) and not store.request_matches_process(previous, owner)]
    if conflicts:
        for owner in conflicts:
            print(f"{owner['name']}: shutdown is in flight for another process; retained its record", flush=True)
        return 1
    local_ids: set[str] = ({saved["session"]["session_id"] for saved in store.session_records(account.login)}
                 | {session["session_id"] for session in report["sessions"]}) if ids else set()
    # Keep admission closed while cancelling launches admitted before this request.
    empty = report.copy()
    empty["sessions"] = []
    _ = begin(account.login, request_origin, ids, then=then, alerts=alerts,
              request_id=identity, report=empty)
    _revoke_launches(account.login, ids)
    # A launch can finish and remove its claim after the initial inventory.
    refreshed = run_inventory(account.login, ids)
    report = report.copy()
    report["sessions"] = list({owner["session_id"]: owner for owner in
        [*report["sessions"], *refreshed["sessions"]]}.values())
    added = begin(account.login, request_origin, ids, then=then, alerts=alerts, request_id=identity, target=target, why=why, move_id=move_id,
                  checkout_move=checkout_move, report=report)
    conflicts = [owner for owner in report["sessions"] if owner["kind"] != "worker"
                 and (previous := store.find_session(account.login, owner["session_id"])) is not None
                 and not store.request_matches_process(previous, owner)]
    if target:
        for saved in added:
            _carry_trust(saved, target)
    # Snapshot the selected requests before the conductor can archive them.
    selected = {session["session_id"] for session in report["sessions"] if session["kind"] != "worker"}
    selected.update(ids)
    requests: dict[str, list[SessionShutdown]] = {}
    for saved in store.session_records(account.login):
        if saved["session"]["session_id"] in selected:
            requests.setdefault(saved["request_id"], []).append(saved)
    try:
        _launch_conductor(account.login, account.label)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if target:
            for saved in added:
                _ = store.withdraw_session(account.login, saved["session"]["session_id"], _restore_record)
        raise
    if target:
        from scripts.accounts import moves
        for saved in added:
            session = saved["session"]
            request: moves.RequestedMove = {"kind": "requested", "at": saved["requested_at"],
                "tool": "claude", "subject": session["session_id"] + ": " + session["name"],
                "move_id": saved.get("move_id", saved["request_id"]), "request_id": saved["request_id"], "from": saved.get("source_label", account.label),
                "to": target, "why": saved.get("why", why), "window": "unknown"}
            fields = accounts.move_origin_fields(origin)
            request["cause"], request["window"] = fields["cause"], fields["window"]
            if "reset" in fields:
                request["reset"] = fields["reset"]
            moves.append_move(request)
    peer_ids = [session_id for session_id in ids if session_id not in local_ids]
    if not here and (not ids or peer_ids):
        arguments = ["down", account.login, "--here", "--request-id", identity]
        if request_origin:
            arguments.extend(("--requested-by", request_origin))
        if ids:
            arguments.extend(("--only", ",".join(peer_ids)))
        if then == "restart":
            arguments.append("--restart")
        if target:
            arguments.extend(("--to", target, "--why", why or f"{account.label} is out of usage", *origin_arguments))
            if move_id:
                arguments.extend(("--move-id", move_id))
        if alerts == "none":
            arguments.append("--no-alert")
        status, output = run_remote(arguments)
        if status != 0:
            print(f"{other_machine()}: not reached; in a terminal on {other_machine()}: {shutdown_command(*arguments)}", file=sys.stderr)
        elif output.strip():
            print(output.strip())
    if not request_origin and sys.stdout.isatty() and requests:
        follower = cast(RequestFollower, cast(object, import_module("shutdown")))
        for active_request in requests:
            result = follower.follow(account.login, active_request)
            if result != 0:
                return result
        return 0
    for active_request, records in requests.items():
        follow_command = shutdown_command("follow", account.login, "--request", active_request)
        selected_ids = ",".join(saved["session"]["session_id"] for saved in records)
        cancel_command = shutdown_command("cancel", account.login, "--here", "--only", selected_ids)
        action = "stop the move of the ones not yet stopped" if any(saved.get("target") for saved in records) else "stop the ones not yet stopped"
        print(f"shutdown of {account.label} started for {len(records)} sessions. To follow it, in a terminal on {report['machine']}: {follow_command}. To {action}, in a terminal on {report['machine']}: {cancel_command}.")
    if not requests:
        print(f"shutdown of {account.label}: no local sessions selected")
    return 1 if conflicts else 0

def _stop_retry_pending(saved: SessionShutdown) -> bool:
    return saved["force"] == "now" and store.stop_incomplete(saved)


def _done(saved: SessionShutdown) -> bool:
    return store.shutdown_finished(saved)


def shutdown_in_progress(saved: SessionShutdown) -> bool:
    """Exclude retained terminal outcomes from current shutdown work."""
    return not _done(saved)


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
        current["stop_issues"] = []
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
    restart_liveness = {saved["session"]["session_id"]: restart_work.restart_failed_liveness(saved)
                        for saved in records if saved["progress"]["kind"] == "restart failed"}
    running = {identity for identity, observed in restart_liveness.items()
               if isinstance(observed, restart_work.SessionLive)}

    def restart_line(saved: SessionShutdown) -> str:
        command = shutdown_command("restart", saved["login"], "--here", "--only", saved["session"]["session_id"])
        observed = restart_liveness.get(saved["session"]["session_id"])
        unknown = f"; liveness unknown: {observed.reason}" if isinstance(observed, restart_work.SessionLivenessUnknown) else ""
        return f"in a terminal on {saved['machine']}: {command}{unknown}"

    first = records[0]
    status, output = run_remote(["records", "--json", "--here"])
    request_ids = {saved["request_id"] for saved in records}
    lines = [f"{saved['machine']} {saved['session']['name']}: {saved['progress']['kind']}" for saved in records]
    if status == 0:
        try:
            peer = store.parse_session_records(output)
            lines.extend(f"{saved['machine']} {saved['session']['name']}: {saved['progress']['kind']}"
                         for saved in peer if saved["login"].casefold() == first["login"].casefold()
                         and saved["request_id"] in request_ids)
        except store.InvalidRecord:
            lines.append(f"{other_machine()}: shutdown state unreadable")
    else:
        lines.append(f"{other_machine()}: not reached")
    for saved in records:
        lines.extend(f"  {saved['session']['name']}: {stop_issue_text(issue)}" for issue in saved["stop_issues"])
    for saved in records:
        progress = saved["progress"]
        if saved["session"]["session_id"] in running:
            lines.append("  " + running_restart_line(saved))
        elif progress["kind"] == "stop failed" or progress["kind"] == "restart failed":
            lines.append(f"  {saved['session']['name']}: {progress['reason']}")
        elif progress["kind"] == "manual restart":
            lines.append(f"  {saved['session']['name']}: in a terminal on {saved['machine']}: {progress['command']}")
        if saved["machine"].casefold() == local_machine().casefold():
            lines.extend(failed_prompt_screen_lines(saved))
    failed = [saved for saved in records if saved["session"]["session_id"] not in running and store.stop_incomplete(saved)]
    restore_failed = [saved for saved in records
                      if saved["progress"]["kind"] in {"restart failed", "manual restart", "timers pending"}
                      and saved["session"]["session_id"] not in running]
    if failed:
        summary = f"{first['label']}: stop partial"
        for saved in failed:
            lines.append(f"in a terminal on {saved['machine']}: " + shutdown_command("now", saved["login"], "--here", "--only", saved["session"]["session_id"]))
    elif restore_failed:
        summary = f"{first['label']} is not fully back"
        for saved in restore_failed:
            lines.append(restart_line(saved))
    elif all(saved["progress"]["kind"] == "restarted" or saved["session"]["session_id"] in running for saved in records):
        summary = f"{first['label']} is back"
    else:
        summary = f"{first['label']} is down"
        for saved in records:
            if saved["session"]["session_id"] not in running:
                lines.append(restart_line(saved))
    tell_showrunners_outcome([saved for saved in records if saved["machine"] == local_machine()],
                            restart_observations=tuple(restart_liveness.items()))
    text = "\n".join(lines)
    origins = {saved["requested_by"]["session_id"] for saved in records if saved["requested_by"]["kind"] == "session"}
    route = "natedev" if local_machine().casefold() == "mac" else ""
    stop_due = [saved for saved in failed if saved["hold"] == "account" and saved["alerts"] == "user"]
    unpushed = [saved for saved in restore_failed
                if saved["progress"]["kind"] != "timers pending" and saved["alerts"] == "user"
                and saved.get("restore_alerted") != not_back_text(saved)]
    try:
        due = stop_due or [saved for saved in unpushed if saved["progress"]["kind"] == "restart failed"]
        for machine in dict.fromkeys(saved["machine"] for saved in due):
            items = [saved for saved in due if saved["machine"] == machine]
            if stop_due:
                _ = alert_user(render("shutdown.stop_failed", account=json.dumps(first["label"]), machine=machine,
                                      stopped=stopped_sessions_text(len(items))), machine=route)
                continue
            restarted = [restart_work.FailedSessionRestart(saved["session"]["name"], progress["reason"],
                                                          progress.get("trust_command", ""))
                         for saved in items if (progress := saved["progress"])["kind"] == "restart failed"]
            sent = restart_work.send_failure_pushes(first["label"], restarted, machine=machine,
                identities={saved["session"]["name"]: saved["session"]["session_id"] for saved in items},
                moved={saved["session"]["name"]: saved for saved in items if saved.get("target")},
                send=lambda push: alert_user(push, machine=route))
            for saved in items:
                if saved["session"]["name"] in sent:
                    mark_not_back_pushed(saved)
        if not stop_due:
            for saved in unpushed:
                if saved["progress"]["kind"] == "manual restart":
                    with outage_alerts.reserve((saved["session"]["session_id"],)) as claims:
                        if claims and alert_user(render("shutdown.manual_restart", session=saved["session"]["name"],
                                                       machine=saved["machine"]), machine=route):
                            outage_alerts.delivered(claims[saved["session"]["session_id"]])
                            mark_not_back_pushed(saved)
    finally:
        if not origins:
            print(text)
        for origin in sorted(origins):
            _ = send_message(f"session:{origin}", summary, text, key=f"shutdown-result-{first['login']}")


def not_back_text(saved: SessionShutdown) -> str:
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
        return showrunners.current_name(staff_sources.unit_parent(host["unit"], session["session_id"]).slug)
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
    text = (summary if why == RETIRE_REASON and saved["then"] == "stop" else
            f"{summary}; {back}. One more line follows when it is back or has not come back.")
    _ = send_message(showrunner, summary, text,
                     key=f"shutdown-unit-{saved['session']['session_id']}")


def tell_showrunners_outcome(records: list[SessionShutdown], *,
                            restart_observations: tuple[tuple[str, restart_work.SessionLive | restart_work.SessionNotLive | restart_work.SessionLivenessUnknown], ...] = ()) -> None:
    """The second line to a stopped unit's showrunner: back, or not back and why."""
    import restart as restart_work
    observed_restarts = dict(restart_observations)
    for saved in records:
        if saved.get("showrunner_told") is None:
            continue
        progress = saved["progress"]
        name = saved["session"]["name"]
        observed = observed_restarts.get(saved["session"]["session_id"])
        if progress["kind"] == "restart failed" and observed is None:
            observed = restart_work.restart_failed_liveness(saved)
        if progress["kind"] == "restart failed" and isinstance(observed, restart_work.SessionLive):
            told = SHOWRUNNER_TOLD_BACK
            summary = running_restart_line(saved, for_showrunner=True)
            text = summary
        elif progress["kind"] in {"restarted", "timers pending"}:
            told = SHOWRUNNER_TOLD_BACK
            summary = f"{name} is back on {saved.get('target') or saved['label']}"
            text = summary
        elif progress["kind"] in {"restart failed", "manual restart"}:
            told = not_back_text(saved)
            reason = progress["reason"] if progress["kind"] == "restart failed" else "it needs a manual restart"
            if isinstance(observed, restart_work.SessionLivenessUnknown):
                reason += f"; liveness unknown: {observed.reason}"
            summary = f"{name} did not come back: {reason}"
            command = shutdown_command("restart", saved["login"], "--here", "--only", saved["session"]["session_id"])
            text = f"{summary}. The user is told; in a terminal on {saved['machine']}: {command} retries it."
        else:
            continue
        # A batch reports when its last session ends; by then a later stop or forget can own this one.
        if saved.get("showrunner_told") == told or store.taken_over(saved):
            continue
        showrunner = _unit_showrunner(saved)
        # A record archived on success is no longer live, so it cannot be marked; it is reported once anyway.
        if showrunner and (progress["kind"] == "restarted" or _reserve_showrunner_line(saved, told)):
            _ = send_message(showrunner, summary, text, key=f"shutdown-unit-{saved['session']['session_id']}")


def mark_not_back_pushed(saved: SessionShutdown) -> None:
    def mark(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"] and current["progress"] == saved["progress"]:
            current["restore_alerted"] = not_back_text(saved)
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
                restart_work.confirm_entry(saved, claim["at"], saved["session"]["session_id"] in confirmation.live_ids,
                                           observed_at=confirmation.observed_at)
            except store.NoLiveRecord:
                continue
            except Exception as error:
                _action_failed(saved, error, stopping=False)
                continue
            confirmed.append(saved)
    confirmed_ids = {saved["session"]["session_id"] for saved in confirmed}
    control_records = restart_work.RegistryConfirmation(tuple(
        item for item in confirmation.records if item["sessionId"] in confirmed_ids))
    _ = restart_work.remote_control_missing(confirmed, control_records)
    return ConfirmedRelaunchPass(records)


def _holdout_due(saved: SessionShutdown, at: datetime) -> bool:
    """Alert once for an unanswered pause, whether or not its pane is readable."""
    message = saved["settle_message"]
    since = message["at"] if message["kind"] == "sent" else saved["requested_at"]
    return (saved["alerts"] == "user" and at - datetime.fromisoformat(since) >= NOT_ANSWERING_AFTER
        and saved["progress"]["kind"] == "waiting" and "alerted_at" not in saved)


def _reserve_holdout_alert(saved: SessionShutdown, at: datetime) -> bool:
    accepted = False
    def mark(current: SessionShutdown) -> None:
        nonlocal accepted
        if (current["request_id"] == saved["request_id"]
            and _holdout_due(current, at)):
            current["alerted_at"] = record_time()
            accepted = True
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], mark)
    except store.NoLiveRecord:
        pass
    return accepted


def _holdout_host(saved: SessionShutdown) -> str:
    host = saved["session"]["host"]
    if host["kind"] == "legacy tmux" or host["kind"] == "legacy tmux unit":
        return f"legacy host {host['session_name']}"
    if host["kind"] == "unit":
        return f"unit {host['unit']}"
    if host["kind"] == "ghostty" or host["kind"] == "zed":
        desktop = host["desktop"]
        window = "Ghostty window" if host["kind"] == "ghostty" else "Zed window"
        return window + (f", {desktop['name']} desktop" if desktop["kind"] == "named" else "")
    return f"{host['kind']} window for {saved['session']['name']}"


def holdout_line(saved: SessionShutdown) -> str:
    message = saved["settle_message"]
    since = message["at"] if message["kind"] == "sent" else saved["requested_at"]
    minutes = max(0, int((now_utc() - datetime.fromisoformat(since)).total_seconds() // 60))
    command = shutdown_command("now", saved["login"], "--here", "--only", saved["session"]["session_id"])
    request = "restart" if saved["then"] == "restart" else "shutdown"
    after_stop = "the restart brings it back." if saved["then"] == "restart" else "it stays stopped until restarted."
    if saved.get("why") == RETIRE_REASON and saved["then"] == "stop":
        after_stop = RETIRE_REASON
    return (f"{saved['session']['name']} has not answered the pause for {minutes} minutes "
            f"in {_holdout_host(saved)}. A {saved['label']} {request} asked it to pause at a safe point and say ready. "
            f"Do nothing: the {request} keeps waiting. Stop now (recommended): ends Claude mid-step so the {request} goes on. "
            f"Uncommitted work stays; {after_stop} "
            f"To stop it now, in a terminal on {saved['machine']}: {command}")


class HoldoutKind(Enum):
    FORM_SHOWN = "shutdown.holdout"
    UNANSWERED = "shutdown.holdout_unanswered"

    @classmethod
    def from_reason(cls, reason: str) -> HoldoutKind:
        details = (detail.strip() for detail in reason.split(","))
        return cls.FORM_SHOWN if SHOWING_FORM in details else cls.UNANSWERED


def _send_holdout_alert(held: list[tuple[SessionShutdown, str]]) -> None:
    """Push every unanswered pause and tell its requester where to act."""
    held = [(saved, reason) for saved, reason in held if saved["progress"]["kind"] == "waiting"]
    if not held:
        return
    summary = "; ".join(f"{'Restart' if saved['then'] == 'restart' else 'Shutdown'} stuck waiting on {saved['session']['name']} session" for saved, _ in held)
    text = "\n".join(holdout_line(saved) for saved, _ in held)
    try:
        for saved, reason in held:
            hold = HoldoutKind.from_reason(reason)
            command = shutdown_command("now", note_file.canonical_label(saved["label"]), "--here", "--only", saved["session"]["session_id"])
            _ = alert_user(render(hold.value, session=saved["session"]["name"], machine=saved["machine"], command=command,
                              operation="restart" if saved["then"] == "restart" else "shutdown"),
                       machine="natedev" if local_machine().casefold() == "mac" else "")
    finally:
        _tell_holdout_requesters(held, summary, text)


def _tell_holdout_requesters(held: list[tuple[SessionShutdown, str]], summary: str, text: str) -> None:
    origins = {saved["requested_by"]["session_id"] for saved, _ in held if saved["requested_by"]["kind"] == "session"}
    if not origins:
        print(text)
    for origin in sorted(origins):
        _ = send_message(f"session:{origin}", summary, text, key=f"shutdown-holdout-{held[0][0]['login']}")


def _stop_after_telling(saved: SessionShutdown, step: StopSession) -> SessionShutdown:
    if "stop_deadline" in saved:
        # The abandoned attempt must finish the bounded ownership proof, never
        # the legacy completion shortcut, with a fresh retry budget.
        return stop_work.bounded_stop([saved], started=time.monotonic(), grace_seconds=0)[0]
    try:
        tell_showrunner_stopping(saved)
    except Exception as error:
        print(f"{saved['session']['name']}: showrunner not told of the stop: {error}", file=sys.stderr)
    if step.mid_turn:
        _record_cut_off(saved)
    return stop_work.stop_session(saved, idle_reason=step.idle_reason, forced=step.forced)


def _record_cut_off(saved: SessionShutdown) -> None:
    """Save the last step of a session the move stops mid-turn, so its resume note names it."""
    session = saved["session"]
    config_dir = Path(session.get("config_dir", str(Path.home() / ".claude")))
    step = last_tool_call(config_dir, session["session_id"])
    def mark(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"]:
            current["cut_off"] = step
    try:
        _ = store.update_session(saved["login"], session["session_id"], mark)
    except store.NoLiveRecord:
        pass


def _archive_finished(saved: SessionShutdown) -> bool:
    try:
        # Live confirmation already cleared its outage; archive may follow a new crash.
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


def _clear_finished_hold(login: str) -> None:
    _ = store.release_finished_account_hold(login, conducting=True)


@dataclass(frozen=True)
class _ModuleFingerprint:
    """The running shutdown modules' names, modification nanoseconds and sizes."""

    modules: tuple[tuple[str, int, int], ...]


def _module_fingerprint() -> _ModuleFingerprint:
    directory = Path(__file__).parent
    modules: list[tuple[str, int, int]] = []
    for name in ("shutdown.py", "settle.py", "stop.py", "shutdown_record.py", "inventory.py", "restart.py"):
        metadata = (directory / name).stat()
        modules.append((name, metadata.st_mtime_ns, metadata.st_size))
    return _ModuleFingerprint(tuple(modules))


@dataclass(frozen=True)
class _ReloadRequested:
    """Changed modules require a fresh process after owned work drains."""

    modules: tuple[str, ...]


@dataclass(frozen=True)
class _ConductorFinished:
    """All owned work has reached an outcome without a reload request."""


def _conduct_archive_path(login: str) -> Path:
    if not login or "/" in login or "\0" in login or login.startswith("."):
        raise ValueError("login is unsafe for record storage")
    return shutdown_state_root() / login.casefold() / "conduct_archive.json"


def _read_conduct_archive(login: str) -> dict[str, SessionShutdown]:
    """Consume the previous process's outcomes while holding the conductor lock."""
    path = _conduct_archive_path(login)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    records = store.parse_session_records(text)
    if any(saved["login"].casefold() != login.casefold() for saved in records):
        raise store.InvalidRecord(f"{path}: conductor outcomes belong to another account")
    archived = {saved["session"]["session_id"]: saved for saved in records}
    path.unlink()
    return archived


def _write_conduct_archive(login: str, archived: dict[str, SessionShutdown]) -> None:
    """Atomically hand completed outcomes to the replacement conductor."""
    path = _conduct_archive_path(login)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".conduct_archive.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(list(archived.values()), output, indent=2, sort_keys=True)
            _ = output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def conduct(login: str) -> int:
    """Keep an owner available for work added while a conductor releases its lock."""
    if os.environ.pop(detached.STARTUP_SIGNAL_ENV, "") == "1":
        print("shutdown conductor started", file=sys.stderr, flush=True)
    fingerprint = _module_fingerprint()
    for line in store.migrate(login):
        print(line)
    session_kind = herdr.security_session()
    archived: dict[str, SessionShutdown] = {}
    handled_requests: set[str] = set()
    while True:
        with store.conductor_lock(login) as ownership:
            if isinstance(ownership, store.ConductorAlreadyRunning):
                return 0
            archived.update(_read_conduct_archive(login))
            handled_requests.update(saved["request_id"] for saved in archived.values())
            handled_requests.update(saved["request_id"] for saved in store.session_records(login) if not _done(saved))
            outcome = _conduct_owned(login, archived, fingerprint, session_kind, handled_requests)
            handled_requests.update(saved["request_id"] for saved in archived.values())
            if isinstance(outcome, _ReloadRequested):
                archived.update({saved["session"]["session_id"]: saved
                                 for saved in store.session_records(login)
                                 if saved["request_id"] in handled_requests and _done(saved)})
                _write_conduct_archive(login, archived)
        if isinstance(outcome, _ReloadRequested):
            os.execv(sys.executable, [sys.executable, "-u", *sys.argv])
        records = store.session_records(login)
        if any(not _done(saved) for saved in records):
            continue
        outcomes = {**archived, **{saved["session"]["session_id"]: saved for saved in records}}
        report_stop([saved for saved in outcomes.values() if saved["request_id"] in handled_requests])
        return 0


def _conduct_owned(login: str, archived: dict[str, SessionShutdown],
                   fingerprint: _ModuleFingerprint, session_kind: herdr.SecuritySession,
                   handled_requests: set[str]) -> _ConductorFinished | _ReloadRequested:
    """Advance one locked account while long actions run independently."""
    import restart as restart_work

    def archive(saved: SessionShutdown) -> None:
        if _archive_finished(saved):
            archived[saved["session"]["session_id"]] = saved

    desktop = restart_work.DesktopLaunchState(session_kind=session_kind)
    pending: dict[str, PendingSessionAction] = {}
    confirmations: list[PendingRegistryConfirmation] = []
    launched: list[SessionShutdown] = []
    observed_ids: set[str] = set()
    attempted: set[tuple[str, type[Step]]] = set()
    report: Inventory = {"machine": local_machine(), "login": login, "label": login,
                         "sessions": [], "unattributed": []}
    next_inventory_at = time.monotonic()
    reload_state: _ModuleFingerprint | _ReloadRequested = fingerprint
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
                saved = restart_work.move_checkout(saved, dry_run=False)
                if restart_work.drop_restore_without_folder(saved, dry_run=False):
                    return
                if restart_work.launches_window(saved["session"]["host"]):
                    try:
                        result = restart_work.relaunch_session(saved, dry_run=False, desktop_state=desktop)
                        accept_launch(saved, result)
                    except Exception as error:
                        _action_failed(saved, error, stopping=False)
                else:
                    future = executor.submit(restart_work.relaunch_session, saved, dry_run=False, desktop_state=desktop)
                    pending[saved["session"]["session_id"]] = PendingSessionAction(saved, RelaunchSession(), future)

            while True:
                if isinstance(reload_state, _ModuleFingerprint):
                    try:
                        observed = _module_fingerprint()
                    except OSError:
                        # An install may temporarily remove a module between passes.
                        pass
                    else:
                        if observed != reload_state:
                            reload_state = _ReloadRequested(tuple(
                                name for name, mtime, size in observed.modules
                                if (name, mtime, size) not in fingerprint.modules))
                            print(f"{_user_time(record_time())}: shutdown modules changed: {', '.join(reload_state.modules)}; draining before reload", flush=True)
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
                        except Exception as error:
                            print(f"shutdown pause message failed for {identity}: {error}", file=sys.stderr)
                for future in list(sends):
                    if future.done():
                        sends.remove(future)
                        try:
                            future.result()
                        except Exception as error:
                            print(f"shutdown notification failed: {error}", file=sys.stderr)

                records = store.session_records(login)
                handled_requests.update(saved["request_id"] for saved in records if not _done(saved))
                _clear_finished_hold(login)
                drained = not pending and not confirmations and not launched and not messages and not sends
                if drained and (isinstance(reload_state, _ReloadRequested) or not records or all(_done(saved) for saved in records)):
                    for saved in records:
                        if saved["progress"]["kind"] == "restarted":
                            archive(saved)
                    break
                current = time.monotonic()
                due = current >= next_inventory_at
                if due and not isinstance(reload_state, _ReloadRequested):
                    observed_ids = {saved["session"]["session_id"] for saved in records}
                    if observed_ids:
                        report = run_inventory(login, observed_ids, refresh_desktops=False)
                    attempted.clear()
                    next_inventory_at = current + SETTLE_INTERVAL_SECONDS
                if (due or changed) and not isinstance(reload_state, _ReloadRequested):
                    holdouts: list[tuple[SessionShutdown, str]] = []
                    confirming_ids = {saved["session"]["session_id"] for batch in confirmations for saved in batch.records}
                    confirming_ids.update(saved["session"]["session_id"] for saved in launched)
                    for saved in records:
                        session_id = saved["session"]["session_id"]
                        if session_id in pending or session_id in confirming_ids or session_id not in observed_ids:
                            continue
                        owner = stop_work.fresh_session(saved["session"], report)
                        try:
                            saved = _reap_refusal_during_wait(saved)
                        except store.NoLiveRecord:
                            continue
                        change = _usage_limit_change(saved, owner)
                        if not isinstance(change, NoUsageLimitChange):
                            updated = _apply_usage_limit_change(saved, change)
                            if isinstance(updated, UsageLimitChangeRefused):
                                continue
                            saved = updated
                        step = next_step(saved, owner, records, now_utc())
                        if due and not isinstance(step, StopSession) and _holdout_due(saved, now_utc()):
                            reason = hold_reason(saved, owner)
                            if _reserve_holdout_alert(saved, now_utc()):
                                holdouts.append((saved, reason))
                        if (session_id, type(step)) in attempted:
                            continue
                        if isinstance(step, SendSettleMessage):
                            attempted.add((session_id, type(step)))
                            if session_id not in messages:
                                messages[session_id] = notifications.submit(_send_settle_message, saved)
                        elif isinstance(step, StopSession):
                            attempted.add((session_id, type(step)))
                            future = executor.submit(_stop_after_telling, saved, step)
                            pending[session_id] = PendingSessionAction(saved, step, future)
                        elif isinstance(step, RelaunchSession):
                            attempted.add((session_id, type(step)))
                            start_launch(saved)
                        elif isinstance(step, Finished) and saved["progress"]["kind"] == "restarted":
                            archive(saved)
                    if holdouts:
                        sends.append(notifications.submit(_send_holdout_alert, holdouts))
                if launched and (isinstance(reload_state, _ReloadRequested) or due
                    or not any(isinstance(action.step, RelaunchSession) for action in pending.values())):
                    batch_records, launched = launched, []
                    future = registry.submit(_confirm_relaunch_pass, batch_records)
                    confirmations.append(PendingRegistryConfirmation(batch_records, future))
                # wait observes completion without setting either result type.
                futures = [cast(Future[object], cast(object, action.future)) for action in pending.values()]
                futures.extend(cast(Future[object], cast(object, batch.future)) for batch in confirmations)
                futures.extend(cast(Future[object], cast(object, future)) for future in messages.values())
                futures.extend(cast(Future[object], cast(object, future)) for future in sends)
                delay = SETTLE_INTERVAL_SECONDS if isinstance(reload_state, _ReloadRequested) else max(0.0, next_inventory_at - time.monotonic())
                if futures:
                    _ = wait(futures, timeout=delay, return_when=FIRST_COMPLETED)
                else:
                    time.sleep(delay)
        _clear_finished_hold(login)
        return reload_state if isinstance(reload_state, _ReloadRequested) else _ConductorFinished()
    finally:
        desktop.restore(dry_run=False)
