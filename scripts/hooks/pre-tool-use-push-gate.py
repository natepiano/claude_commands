#!/usr/bin/env python3
"""Allow a built-in push only with the recent failed send's reviewed text."""
from __future__ import annotations

import fcntl
import json
import math
import os
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
    from ..notify.review import ReleaseOutcome, posting_wait_seconds
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    try:
        from review import ReleaseOutcome, posting_wait_seconds
    except Exception as error:
        print(f"push gate: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(0)

MARKER_MAX_AGE_SECONDS = 600
ALREADY_SENT = "The held version of this push was already sent."


class GateOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    DELIVERED = "delivered"

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
    held_source: NotRequired[str]
    held_identity: NotRequired[str]
    held_machine: NotRequired[str]


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def object_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def text(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f"missing {key}")
    return value


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
    if not session_id or Path(session_id).name != session_id or session_id in (".", ".."):
        raise ValueError("invalid session_id")
    push = object_record(payload.get("tool_input"))
    message = text(push, "message")
    directory = Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify"))) / "failed"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = directory / f"{session_id}.json"
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
        if " ".join(message.split()) != " ".join(text(record, "fallback").split()):
            return GateOutcome.DENY
        marker.unlink()
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
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ALREADY_SENT if outcome is GateOutcome.DELIVERED else DENIAL}}
        print(json.dumps(response))


if __name__ == "__main__":
    main()
