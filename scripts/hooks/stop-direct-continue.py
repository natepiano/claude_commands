#!/usr/bin/env python3
"""Wake an active /unit:direct run after its turn ends near compaction.

The separate asyncRewake Stop entry runs this hook in the background.
Exit 2 sends the reason from stderr to the agent in a system-reminder.
The guards permit declared waits, background tasks, and stops without context measurements.
The hook repeats the wake above the threshold until compaction reduces the context.
Exceptions permit the stop without output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from context_usage import (
    HookInput,
    auto_compact_window,
    handoff_threshold,
    measure,
    pending_background_tasks,
    trigger_tokens,
)
from direct_run import active_run

REASON = """\
/unit:direct run active, {tokens:,} / {trigger:,} tokens ({percent}%).
Compaction runs when the context reaches {trigger:,} tokens. It does not run while the session is idle.
Take the next workflow action now. Do not wait for compaction.
Read ~/.claude/commands/unit/direct.md after compaction.
If you need a user decision, state it in one line and end the turn."""


class StopPayload(HookInput, total=False):
    last_assistant_message: str


def build_reason(tokens: int, trigger: int) -> str:
    percent = round(100 * tokens / trigger) if trigger else 100
    return REASON.format(tokens=tokens, trigger=trigger, percent=percent)


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))

    # Subagents do not orchestrate /unit:direct runs; their stop is a real result.
    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return
    # A declared wait is a real stop, including a restated decision after a
    # compaction block. Record before the compaction and background-work guards.
    final_message = payload.get("last_assistant_message", "")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.direct.progress_history import record_turn_wait

    if record_turn_wait(session_dir, session_id, final_message):
        return
    # Measure first: the window is clamped to the model's own context window, and
    # only the transcript names the model.
    measurement = measure(payload)
    if measurement is None:
        return
    window = auto_compact_window(measurement["model"])
    if window is None:
        return
    tokens = measurement["tokens"]
    if tokens < handoff_threshold(window):
        return

    # Only live tasks reported by the CLI supply a later request. A worker's
    # heartbeat can be fresh even after its completion notice has arrived.
    if pending_background_tasks(payload):
        return

    print(build_reason(tokens, trigger_tokens(window)), file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never wedge the turn it guards.
        pass
    sys.exit(0)
