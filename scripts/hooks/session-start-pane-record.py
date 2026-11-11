#!/usr/bin/env python3
"""SessionStart hook: record the Claude launch of the herdr pane this session runs in.

The entry is what a restore needs to resume the pane on its own account: see
`scripts/agents/pane_record.py`. A session outside herdr gets nothing.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import sys
from typing import TypedDict, cast
from collections.abc import Mapping

from scripts.agents import pane_record  # noqa: E402
from scripts.production import session_staff, staff  # noqa: E402
from scripts.hooks import staff_session_identity  # noqa: E402


class HookInput(TypedDict, total=False):
    session_id: str
    cwd: str
    source: str


def main() -> None:
    environment = dict(os.environ)
    pane_id = environment.get("HERDR_PANE_ID", "")
    if environment.get("HERDR_ENV") != "1" or not pane_id:
        return
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    # One hook process owns this order; independent commands can run concurrently.
    identity = staff_session_identity.handle(cast(Mapping[str, object], payload), environment)
    if identity is staff_session_identity.ClearIdentityResult.STAFFED:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    session = environment.get("HERDR_SESSION") or "default"
    arguments = pane_record.claude_arguments(os.getpid())
    if not arguments:
        print("cannot read Claude arguments; retained launch stays unchanged", file=sys.stderr)
        return
    if "-p" in arguments or "--print" in arguments:
        return
    launch = pane_record.launch(pane_id, session_id, payload.get("cwd") or os.getcwd(), environment, arguments)
    pid = os.getppid()
    for depth in range(pane_record.ANCESTOR_LIMIT):
        del depth
        if staff.runtime_id(pid) == session_id:
            from scripts.message import sessions
            started = sessions.process_start(pid)
            if started:
                launch["pid"] = pid
                launch["proc_start"] = started
            break
        if pid <= 1:
            break
        pid = pane_record.process_parent(pid)
    pane_record.write(session, session_staff.record_membership(launch))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"session launch observation: {error}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
