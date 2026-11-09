"""Send a continuation message for the quota watcher."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import Literal, TypedDict

from scripts.message import send as message_send, send_result, send_budget, session_send

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


def _send_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_SEND")
    return [override] if override else session_send.send_argv()


def _deliver_message(
    command: list[str], recipient: str, text: str
) -> MessageSendOutcome:
    timeout = send_budget.seconds()
    if recipient == "user":
        raise ValueError("user messages require the user delivery path")
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
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        parsed = outcome.header
        detail = parsed.detail
        match parsed.outcome:
            case "sent":
                return MessageSent(kind="sent")
            case "skipped":
                if message_send.is_acknowledged_skip(detail):
                    return MessageAcknowledged(kind="acknowledged", reason=detail)
                return MessageWindowSkipped(kind="window skipped", reason=detail)
            case "kept":
                return MessageKept(kind="kept", reason=detail)
            case "replaced":
                return MessageReplaced(kind="replaced", reason=detail)
            case "queued":
                return MessageQueued(kind="queued", reason=detail)
            case "retry":
                raise RuntimeError(f"send to {recipient} needs a retry: {detail}")
            case "failed" | "refused" | "held" | "pending":
                pass
    if isinstance(outcome, send_result.UnconfirmedSendOutcome):
        return MessageUnconfirmed(kind="unconfirmed", reason="send returned no confirmed outcome")
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
        raise ValueError("user messages require the user delivery path")
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
