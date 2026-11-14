#!/usr/bin/env python3
"""Wake an active unit run after its turn ends near compaction.

The separate asyncRewake Stop entry runs this hook in the background.
Exit 2 sends the reason from stderr to the agent in a system-reminder.
The guards permit declared waits, background tasks, and stops without context measurements.
The hook wakes above the threshold only when the final line differs from the last wake.
A merge gate with no open order phase has no next action, so it stops without a wake.
Paused status lines permit the stop without a wake.
Exceptions permit the stop without output.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import re
import sys
from typing import cast

from scripts.hooks.context_usage import (
    HookInput,
    auto_compact_window,
    handoff_threshold,
    measure,
    pending_background_tasks,
    trigger_tokens,
)
from scripts.direct.recorder_state import read_open_phase_ids
from scripts.hooks.direct_run import active_run
from scripts.hooks.conversation_pause import DirectorTurn, director_turn
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff

REASON = """\
{command} run active, {tokens:,} / {trigger:,} tokens ({percent}%).
Compaction runs when the context reaches {trigger:,} tokens. It does not run while the session is idle.
Take the next workflow action now. Do not wait for compaction.
After compaction, read ~/.claude/commands/{command_file}.md in full.
Then read each stub file whose moment is live.
If you need a user decision, state it in one line and end the turn."""


class StopPayload(HookInput, total=False):
    last_assistant_message: str


def build_reason(tokens: int, trigger: int, command: str) -> str:
    percent = round(100 * tokens / trigger) if trigger else 100
    return REASON.format(tokens=tokens, trigger=trigger, percent=percent, command=command,
                         command_file=command.removeprefix("/").replace(":", "/"))


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))

    # Subagents return results instead of directing unit runs.
    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    if director_turn(session_id) is DirectorTurn.HOLD:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return
    row = staff.local_by_current_session(session_id)
    command = staff.unit_command(row if row is not None and row.kind == "unit" else "direct")
    # A declared wait is a real stop, including a restated decision after a
    # compaction block. Record before the compaction and background-work guards.
    final_message = payload.get("last_assistant_message", "")
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
    if command == "/unit:merge" and not read_open_phase_ids(session_dir):
        return

    from scripts.production.stall_watch import watch_config

    lines = final_message.strip().splitlines()
    status = lines[-1].strip().removeprefix("— ") if lines else ""
    if re.search(
        watch_config(status_defaults=True)["paused_status_pattern"].strip(), status, re.IGNORECASE
    ):
        return

    # Only live tasks reported by the CLI supply a later request. A worker's
    # heartbeat can be fresh even after its completion notice has arrived.
    if pending_background_tasks(payload):
        return

    wake_line = session_dir / "compaction_wake_line"
    try:
        if wake_line.read_text(encoding="utf-8") == status:
            return
    except FileNotFoundError:
        pass
    write_text_atomic(wake_line, status)

    print(build_reason(tokens, trigger_tokens(window), command), file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never wedge the turn it guards.
        pass
    sys.exit(0)
