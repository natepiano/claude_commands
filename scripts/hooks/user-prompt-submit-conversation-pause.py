#!/usr/bin/env python3
"""Pause a session's automatic updates when a conversation begins."""

from __future__ import annotations

import json
import sys
from typing import TypedDict, cast


class PromptPayload(TypedDict, total=False):
    session_id: str
    prompt: str
    agent_id: str


def main() -> None:
    payload = cast(PromptPayload, json.loads(sys.stdin.read()))
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    prompt = payload.get("prompt", "")

    import conversation_pause

    source = conversation_pause.prompt_source(prompt, conversation_pause.scheduled_senders)
    if source in {conversation_pause.PromptSource.SCHEDULED, conversation_pause.PromptSource.NOTICE}:
        return
    showrunner_session = (source is conversation_pause.PromptSource.PEER
                          and conversation_pause.is_showrunner_session(session_id))
    if not conversation_pause.pauses(source, showrunner_session):
        return
    reply = conversation_pause.message_arrived(
        session_id, source, prompt, conversation_pause.now_epoch())
    if reply is None:
        return
    result: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reply.context,
        },
    }
    if reply.system_message is not None:
        result["systemMessage"] = reply.system_message
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(f"conversation-pause: {type(error).__name__}: {detail}", file=sys.stderr)
    raise SystemExit(0)
