#!/usr/bin/env python3
"""Record when a session reply finishes during an update pause."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import NoReturn, TypedDict, cast


HOOK_BUDGET_SECONDS = 5


class HookBudgetSpent(Exception):
    pass


class ScheduledWakeup(TypedDict):
    id: str
    schedule: str
    recurring: bool
    prompt: str


class StopPayload(TypedDict, total=False):
    session_id: str
    agent_id: str
    stop_hook_active: bool
    session_crons: list[ScheduledWakeup]


def pause_record_path(session_id: str) -> Path:
    root = Path(os.environ.get("CONVERSATION_PAUSE_STATE_DIR")
                or Path.home() / ".local/state/conversation-pause")
    return root / f"{session_id}.json"


def scheduled_prompts_path(session_id: str) -> Path:
    return pause_record_path(session_id).parent / "scheduled-prompts" / f"{session_id}.json"


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    wakeups = payload.get("session_crons", [])
    records_scheduled_prompts = "session_crons" in payload and (
        bool(wakeups) or scheduled_prompts_path(session_id).exists()
    )
    marks_reply_ended = (
        not payload.get("stop_hook_active", False)
        and pause_record_path(session_id).exists()
    )
    if not records_scheduled_prompts and not marks_reply_ended:
        return

    budget = int(os.environ.get("CONVERSATION_PAUSE_HOOK_BUDGET", HOOK_BUDGET_SECONDS))

    def budget_spent(_signal_number: int, _frame: FrameType | None) -> NoReturn:
        raise HookBudgetSpent(f"gave up after {budget} seconds")

    _ = signal.signal(signal.SIGALRM, budget_spent)
    _ = signal.alarm(budget)
    try:
        import conversation_pause

        if records_scheduled_prompts:
            conversation_pause.record_scheduled_prompts(
                session_id,
                tuple(wakeup["prompt"] for wakeup in wakeups),
            )
        if marks_reply_ended:
            conversation_pause.mark_reply_ended(
                session_id, conversation_pause.now_epoch()
            )
    finally:
        _ = signal.alarm(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(f"conversation-pause: {type(error).__name__}: {detail}", file=sys.stderr)
    raise SystemExit(0)
