#!/usr/bin/env python3
"""Pause a session's automatic updates when a conversation begins."""

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


class PromptPayload(TypedDict, total=False):
    session_id: str
    prompt: str
    agent_id: str


def _record_typed_prompt() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
        import escalate

        escalate.typed()
    except HookBudgetSpent:
        raise
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(
            f"conversation-pause: typed stamp: {type(error).__name__}: {detail}",
            file=sys.stderr,
        )


def main() -> None:
    payload = cast(PromptPayload, json.loads(sys.stdin.read()))
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    prompt = payload.get("prompt", "")

    budget = int(os.environ.get("CONVERSATION_PAUSE_HOOK_BUDGET", HOOK_BUDGET_SECONDS))

    def budget_spent(_signal_number: int, _frame: FrameType | None) -> NoReturn:
        raise HookBudgetSpent(f"gave up after {budget} seconds")

    _ = signal.signal(signal.SIGALRM, budget_spent)
    _ = signal.alarm(budget)
    try:
        import conversation_pause

        source = conversation_pause.prompt_source(prompt, conversation_pause.scheduled_senders)
        if source is conversation_pause.PromptSource.TYPED:
            _record_typed_prompt()
        return_question = conversation_pause.is_return_question(prompt)
        if (source in {conversation_pause.PromptSource.SCHEDULED,
                       conversation_pause.PromptSource.NOTICE}
                and not return_question):
            return
        if not return_question:
            showrunner_session = (source is conversation_pause.PromptSource.PEER
                                  and conversation_pause.is_showrunner_session(session_id))
            if not conversation_pause.pauses(source, showrunner_session):
                return
        reply = conversation_pause.message_arrived(
            session_id, source, prompt, conversation_pause.now_epoch())
        if reply is conversation_pause.NoReply.NOTHING:
            return
        result: dict[str, object] = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": reply.context,
            },
        }
        if isinstance(reply, conversation_pause.ShownToUser):
            result["systemMessage"] = reply.system_message
        print(json.dumps(result))
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
