#!/usr/bin/env python3
"""Record-driven restart messages and status, retained until the record store is removed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks", "lib", "herdr"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
from inventory import Session, inventory
import shutdown_record as store
from shutdown_record import SessionShutdown, SessionStopIssue
from remote import other_machine, run_remote
from scripts.production import staff_sources
import showrunners
import sessions
import inventory as stop_work
from user_zone import user_zone
from scripts.accounts import outage_alerts
if TYPE_CHECKING:
    from ..message import send as message_send
    from . import restart as restart_work
    from ..notify import review
    from ..notify.push_texts import RenderedPush, render, user_send_argv
else:
    import send as message_send
    import review
    from push_texts import RenderedPush, render, user_send_argv

NOTIFIER = SCRIPTS / "message" / "notifier.sh"


SEND = SCRIPTS / "message" / "send.py"


class MessageSent(TypedDict):
    kind: Literal["sent"]


class MessageQueued(TypedDict):
    kind: Literal["queued"]
    reason: str


class MessageAcknowledged(TypedDict):
    kind: Literal["acknowledged"]
    reason: str


class MessageWindowSkipped(TypedDict):
    kind: Literal["window skipped"]
    reason: str


class MessageKept(TypedDict):
    kind: Literal["kept"]
    reason: str


class MessageReplaced(TypedDict):
    kind: Literal["replaced"]
    reason: str


class MessageUnconfirmed(TypedDict):
    kind: Literal["unconfirmed"]
    reason: str


MessageDelivery = MessageSent | MessageQueued


MessageSendOutcome = (MessageDelivery | MessageAcknowledged | MessageWindowSkipped
                      | MessageKept | MessageReplaced | MessageUnconfirmed)


def message_was_delivered(outcome: Mapping[str, object]) -> bool:
    """Accept only a delivered send or the recipient's acknowledgement."""
    return outcome["kind"] in {"sent", "acknowledged"}


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


def retire_shutdown_messages(login: str, session_id: str) -> None:
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
) -> MessageSendOutcome:
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
    parsed = message_send.parse_outcome(result.stdout)
    if isinstance(parsed, message_send.OutcomeHeader):
        detail = parsed.detail
        if result.returncode == 0:
            if parsed.outcome == "sent":
                return MessageSent(kind="sent")
            if parsed.outcome == "skipped":
                if message_send.is_acknowledged_skip(detail):
                    return MessageAcknowledged(kind="acknowledged", reason=detail)
                return MessageWindowSkipped(kind="window skipped", reason=detail)
            if parsed.outcome == "kept":
                return MessageKept(kind="kept", reason=detail)
            if parsed.outcome == "replaced":
                return MessageReplaced(kind="replaced", reason=detail)
        elif result.returncode == 1 and parsed.outcome == "queued":
            return MessageQueued(kind="queued", reason=detail)
    if result.returncode == 0:
        return MessageUnconfirmed(kind="unconfirmed", reason="send returned no outcome header")
    if result.returncode == 1:
        return MessageQueued(kind="queued", reason="exit 1")
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
    repeat_minutes: int = 0,
) -> MessageSendOutcome:
    """Send to a session and return the observed send outcome."""
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
        if repeat_minutes:
            command.extend(("--repeat-minutes", str(repeat_minutes)))
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
        outcome = _deliver_message(command, "user", "")
        return message_was_delivered(outcome) or outcome["kind"] in {"kept", "replaced"}
    except (OSError, RuntimeError) as error:
        print(f"shutdown: user alert failed: {error}", file=sys.stderr)
        return False


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


def unrecorded_local_sessions(login: str, ids: tuple[str, ...], records: list[SessionShutdown]) -> dict[str, str]:
    missing = frozenset(ids) - {saved["session"]["session_id"] for saved in records}
    if not missing:
        return {}
    report = inventory(login, missing)
    return {owner["session_id"]: owner["name"] for owner in report["sessions"]
            if owner["session_id"] in missing and owner["kind"] != "worker"}


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
            lines.append(f"in a terminal on {saved['machine']}: " + shutdown_command("stop", saved["session"]["session_id"]))
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
    unpushed = [saved for saved in restore_failed
                if saved["progress"]["kind"] != "timers pending" and saved["alerts"] == "user"
                and saved.get("restore_alerted") != not_back_text(saved)]
    try:
        due = [saved for saved in unpushed if saved["progress"]["kind"] == "restart failed"]
        for machine in dict.fromkeys(saved["machine"] for saved in due):
            items = [saved for saved in due if saved["machine"] == machine]
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


def showrunner_was_told(saved: SessionShutdown) -> bool:
    """A delivered or queued line permits the next outcome line in order."""
    return "showrunner_told" in saved or any(
        line["kind"] in {"delivered", "queued"} for line in saved.get("showrunner_lines", []))


def _showrunner_line_identity(told: str, text: str) -> tuple[str, ...]:
    """Keep one back or stopping line; report changed failure facts."""
    return (told,) if told in {SHOWRUNNER_TOLD_BACK, SHOWRUNNER_TOLD_STOPPING} else (told, text)


def _complete_showrunner_send(saved: SessionShutdown, line: store.ShowrunnerLineDelivered | store.ShowrunnerLineQueued) -> SessionShutdown:
    """Keep the send result on the exact request, whether active or archived."""
    updated = saved
    def remember(current: SessionShutdown) -> None:
        nonlocal updated
        lines = current.get("showrunner_lines", [])
        for index, current_line in enumerate(lines):
            if _showrunner_line_identity(current_line["line"], current_line["text"]) == _showrunner_line_identity(line["line"], line["text"]):
                lines[index] = line
                break
        updated = current
    _ = store.update_showrunner_line(saved, remember)
    return updated


def send_showrunner_line(saved: SessionShutdown, told: str, showrunner: str, summary: str, text: str) -> SessionShutdown:
    """Claim, send and acknowledge each line once per request."""
    line_identity = _showrunner_line_identity(told, text)
    with store.showrunner_line_lock(saved):
        current = saved
        reserved = False
        offered: store.ShowrunnerLinePending | store.ShowrunnerLineQueued = store.ShowrunnerLinePending(
            kind="pending", line=told, summary=summary, text=text)
        def claim(record: SessionShutdown) -> None:
            nonlocal current, reserved, offered
            current = record
            if record.get("showrunner_told") == told:
                return
            lines = record.setdefault("showrunner_lines", [])
            for line in lines:
                if _showrunner_line_identity(line["line"], line["text"]) == line_identity:
                    reserved = line["kind"] != "delivered"
                    if line["kind"] != "delivered":
                        offered = line
                    return
            lines.append(offered)
            reserved = True
        _ = store.update_showrunner_line(saved, claim)
        if not reserved:
            return current
        identity = json.dumps([saved["login"], saved["request_id"], saved["requested_at"],
                               saved["session"]["session_id"], *line_identity], ensure_ascii=True)
        key = "shutdown-unit-" + hashlib.sha256(identity.encode()).hexdigest()
        outcome = send_message(showrunner, offered["summary"], offered["text"], key=key, repeat_minutes=5_256_000)
        if message_was_delivered(outcome):
            return _complete_showrunner_send(current, store.ShowrunnerLineDelivered(
                kind="delivered", line=told, text=offered["text"]))
        if outcome["kind"] == "queued":
            return _complete_showrunner_send(current, store.ShowrunnerLineQueued(
                kind="queued", line=told, summary=offered["summary"], text=offered["text"],
                reason=outcome["reason"], at=record_time()))
        return current


def tell_showrunners_outcome(records: list[SessionShutdown], *,
                            restart_observations: tuple[tuple[str, restart_work.SessionLive | restart_work.SessionNotLive | restart_work.SessionLivenessUnknown], ...] = ()) -> None:
    """Report each outcome, retaining a failed line without blocking later records."""
    import restart as restart_work
    observed_restarts = dict(restart_observations)
    failures: list[Exception] = []
    for saved in records:
        try:
            if store.taken_over(saved):
                continue
            showrunner = _unit_showrunner(saved)
            if not showrunner:
                continue
            for line in saved.get("showrunner_lines", []):
                if "summary" in line:
                    saved = send_showrunner_line(saved, line["line"], showrunner,
                                                 line["summary"], line["text"])
            if not showrunner_was_told(saved):
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
            if told == SHOWRUNNER_TOLD_BACK and any(
                claimed["line"] == SHOWRUNNER_TOLD_BACK for claimed in saved.get("showrunner_lines", [])
            ):
                continue
            _ = send_showrunner_line(saved, told, showrunner, summary, text)
        except Exception as error:
            print(f"{saved['session']['name']}: showrunner not told of the outcome: {error}", file=sys.stderr)
            failures.append(error)
    if failures:
        raise failures[0]


def mark_not_back_pushed(saved: SessionShutdown) -> None:
    def mark(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"] and current["progress"] == saved["progress"]:
            current["restore_alerted"] = not_back_text(saved)
    try:
        _ = store.update_session(saved["login"], saved["session"]["session_id"], mark)
    except store.NoLiveRecord:
        pass


def archive_finished(saved: SessionShutdown) -> bool:
    try:
        # Live confirmation already cleared its outage; archive may follow a new crash.
        store.archive_session(saved["login"], saved["session"]["session_id"])
    except store.NoLiveRecord:
        return False
    return True



_STOPPABLE_STATUSES = frozenset({"idle", "shell"})


SHOWING_FORM = "showing a form"


@dataclass(frozen=True)
class ScreenText:
    text: str


@dataclass(frozen=True)
class ScreenUnreadable:
    reason: Literal["no pane host", "no pane id", "capture failed", "session registry identity changed"]


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
