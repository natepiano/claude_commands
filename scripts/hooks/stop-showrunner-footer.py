#!/usr/bin/env python3
"""Block a showrunner reply whose final lines are not its current footer."""

from __future__ import annotations

import json
import os
import sys
from typing import TypedDict, cast


class StopPayload(TypedDict, total=False):
    session_id: str
    last_assistant_message: str
    stop_hook_active: bool
    agent_id: str


def notifier_root() -> str:
    return os.environ.get("NOTIFIER_STATE_DIR") or os.path.expanduser("~/.local/state/notifier")


def showrunner_instances(session_id: str) -> list[str]:
    instances: list[str] = []
    try:
        with os.scandir(notifier_root()) as entries:
            for entry in entries:
                if not entry.name.startswith("showrunner-"):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                    with open(os.path.join(entry.path, "conf"), encoding="utf-8") as conf:
                        if f"TARGET=session:{session_id}" in conf.read().splitlines():
                            instances.append(entry.path)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(instances)


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))
    if payload.get("stop_hook_active") or "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    reply = payload.get("last_assistant_message", "")
    if not session_id or not reply.strip():
        return
    instances = showrunner_instances(session_id)
    if not instances:
        return
    import showrunner_footer

    states: dict[str, showrunner_footer.FooterState] = {}
    for instance in instances:
        slug = os.path.basename(instance).removeprefix("showrunner-")
        states[slug] = showrunner_footer.footer_state(slug)
    reason = showrunner_footer.block_reason(instances, reply, states)
    if reason is not None:
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"stop-showrunner-footer: {type(error).__name__}: {error}", file=sys.stderr)
    sys.exit(0)
