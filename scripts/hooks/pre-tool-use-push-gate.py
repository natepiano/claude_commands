#!/usr/bin/env python3
"""Allow a built-in push only with the recent failed send's reviewed text."""
from __future__ import annotations

import fcntl
import json
import math
import sys
import shlex
import subprocess
import importlib
import time
from pathlib import Path
from enum import Enum
from collections.abc import Callable
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from ._push_fallback import fallback_matches, marker_path, object_record, text
    from ..notify.review import ReleaseOutcome, posting_wait_seconds
else:
    from _push_fallback import fallback_matches, marker_path, object_record, text
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    try:
        from review import ReleaseOutcome, posting_wait_seconds
    except Exception as error:
        print(f"push gate: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(0)

MARKER_MAX_AGE_SECONDS = 600
ALREADY_SENT = "The held version of this push was already sent."
UNREVIEWED_FALLBACK = ("This push was never reviewed, so it cannot go out as a fallback. "
    "Bring it to the user in your reply, or resend it with send.py once the failure is fixed.")


HELD_FALLBACK = "This push was not sent because it failed review. Revise it and resend it with send.py; no fallback is allowed."


class GateOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    DELIVERED = "delivered"
    UNREVIEWED = "unreviewed"
    HELD = "held"

DENIAL = ("PushNotification is only the fallback for a failed send. Send with "
          "~/.claude/scripts/message/send.py --to user (see /alert_user); it is reviewed for clarity first.")


class PushInput(TypedDict):
    message: str
    status: str


class HookPayload(TypedDict):
    session_id: str
    tool_name: str
    tool_input: PushInput


class FailedPushRecord(TypedDict):
    time: float
    title: str
    action: str | None
    body: str
    message: str
    fallback: str
    held: NotRequired[bool]
    reviewed: NotRequired[bool]
    need: NotRequired[str]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]
    held_machine: NotRequired[str]


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


class HoldReview(Protocol):
    def release_if_matches(self, source: str, identity: str, *, timeout: float,
                           clock: Callable[[], float], sleep: Callable[[float], None]) -> ReleaseOutcome: ...


def release_hold(source: str, identity: str, machine: str, timeout: float, *,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> ReleaseOutcome:
    if machine:
        command = ('~/.claude/scripts/lib/py ~/.claude/scripts/notify/review.py release --identity '
                   + shlex.quote(identity) + ' -- ' + shlex.quote(source))
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", machine, command],
                           capture_output=True, text=True, check=True, timeout=timeout)
        lines = result.stdout.strip().splitlines()
        return ReleaseOutcome(lines[-1] if lines else "")
    notify = str(Path(__file__).resolve().parents[1] / "notify")
    if notify not in sys.path:
        sys.path.insert(0, notify)
    reviewer = cast(HoldReview, cast(object, importlib.import_module("review")))
    return reviewer.release_if_matches(source, identity, timeout=timeout, clock=clock, sleep=sleep)


def matches_failed_push(raw: str, *, clock: Callable[[], float] = time.monotonic,
                        sleep: Callable[[float], None] = time.sleep) -> GateOutcome:
    payload = object_record(cast(object, json.loads(raw)))
    if text(payload, "tool_name") != "PushNotification":
        return GateOutcome.ALLOW
    session_id = text(payload, "session_id")
    marker = marker_path(session_id)
    push = object_record(payload.get("tool_input"))
    message = text(push, "message")
    directory = marker.parent
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            record = object_record(cast(object, json.loads(marker.read_text(encoding="utf-8"))))
        except FileNotFoundError:
            return GateOutcome.DENY
        stamp = record.get("time")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise ValueError("invalid marker time")
        age = time.time() - stamp
        if not 0 <= age < MARKER_MAX_AGE_SECONDS:
            return GateOutcome.DENY
        if not fallback_matches(message, record):
            return GateOutcome.DENY
        # PostToolUse consumes approval after delivery; an interrupted allow can retry.
        if record.get("held") is True and record.get("need") != "blocked":
            marker.unlink()
            return GateOutcome.HELD
        if record.get("reviewed", True) is not True and record.get("need") != "blocked":
            marker.unlink()
            return GateOutcome.UNREVIEWED
    source = record.get("held_source")
    identity = record.get("held_identity")
    if isinstance(source, str) and source and isinstance(identity, str) and identity:
        machine = record.get("held_machine", "")
        if not isinstance(machine, str):
            raise ValueError("invalid held_machine")
        wait = posting_wait_seconds()
        deadline = clock() + wait
        outcome = release_hold(source, identity, machine, wait, clock=clock, sleep=sleep)
        while outcome is ReleaseOutcome.POSTING and clock() < deadline:
            sleep(min(1, deadline - clock()))
            remaining = deadline - clock()
            if remaining <= 0:
                break
            outcome = release_hold(source, identity, machine, remaining, clock=clock, sleep=sleep)
        if outcome is ReleaseOutcome.DELIVERED:
            with (directory / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    if json.loads(marker.read_text(encoding="utf-8")) == record:
                        marker.unlink()
                except FileNotFoundError:
                    pass
            return GateOutcome.DELIVERED
    return GateOutcome.ALLOW


def main(*, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> None:
    try:
        outcome = matches_failed_push(sys.stdin.read(), clock=clock, sleep=sleep)
    except Exception as error:
        # A broken hook must never lose a push.
        print(f"push gate: {type(error).__name__}: {error}", file=sys.stderr)
        return
    if outcome is not GateOutcome.ALLOW:
        reason = {GateOutcome.DENY: DENIAL, GateOutcome.DELIVERED: ALREADY_SENT,
                  GateOutcome.UNREVIEWED: UNREVIEWED_FALLBACK, GateOutcome.HELD: HELD_FALLBACK}[outcome]
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        print(json.dumps(response))


if __name__ == "__main__":
    main()
