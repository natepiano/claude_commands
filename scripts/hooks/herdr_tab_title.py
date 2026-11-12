"""Keep member titles on their staff names and preserve ad hoc session titles.

The hook never stops the turn.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import sys
from collections.abc import Mapping
from typing import TypedDict, cast

from scripts.herdr import herdr, worker_panes  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import staff
from scripts.agents import pane_record
from scripts.herdr import pane_launch


class StopPayload(TypedDict, total=False):
    session_id: str


def current_name(session_id: str) -> str:
    """The name in this session's record now, or empty when no record carries the id."""
    for path in sessions.sessions_dir().glob("*.json"):
        record = sessions.read_session(path)
        if isinstance(record, sessions.UnreadableSessionRecord):
            continue
        if record["sessionId"] == session_id and not sessions.headless(record):
            return record["name"]
    return ""


def _clear_handoff_pending(session_id: str, shared: str, pane: str, record: staff.StaffSnapshot) -> bool:
    """Match the current process to a retained member launch before reporting an incomplete clear."""
    try:
        entry = pane_record.read_strict(shared).get(pane)
    except (OSError, ValueError):
        return False
    if entry is None or pane_launch.reference_id(entry) == session_id or entry.get("ended"):
        return False
    pid, started = entry.get("pid"), entry.get("proc_start")
    if (not isinstance(pid, int) or isinstance(pid, bool) or not started
            or record.by_member_reference(pane_launch.reference_id(entry)) is None):
        return False
    for path in sessions.sessions_dir().glob("*.json"):
        current = sessions.read_session(path)
        if isinstance(current, sessions.UnreadableSessionRecord):
            continue
        if (current["sessionId"] == session_id and not sessions.headless(current)
                and current["pid"] == pid and current.get("procStart") == started
                and sessions.process_start(pid) == started):
            return True
    return False


def sync(payload: StopPayload, environment: Mapping[str, str]) -> bool:
    """Use the current runtime row for member titles. Return whether a title changed."""
    pane = environment.get("HERDR_PANE_ID", "")
    session_id = payload.get("session_id", "")
    if not pane or not session_id or environment.get("TMUX_PANE"):
        return False
    shared = herdr.shared_session()
    if environment.get("HERDR_SESSION") != shared:
        return False
    name = current_name(session_id)
    try:
        record = staff.snapshot()
    except staff.StaffAbsent:
        record = staff.StaffSnapshot(())
    except (OSError, ValueError) as error:
        print(f"herdr title: cannot read staff: {error}", file=sys.stderr)
        return False
    row = record.by_current_session(session_id)
    if row is not None:
        if row.kind == "unit" and name and name != row.name:
            print(f"herdr title: {name!r} differs from unit {row.name!r}; use /agents rename.", file=sys.stderr)
        client = herdr.Herdr(shared)
        title = (worker_panes.agent_name(record.rows_by_id[cast(staff.ReportsTo, row.parent).id], row.name)
                 if row.kind == "worker" else row.name)
        return client.retitle(pane, title, tab=row.kind != "worker")
    if _clear_handoff_pending(session_id, shared, pane, record):
        print(f"herdr title: no staff row for current session {session_id}.", file=sys.stderr)
    if not name:
        return False
    client = herdr.Herdr(shared)
    panes = client.panes()
    tab = next((item.get("tab_id") for item in panes if item["pane_id"] == pane), None)
    if any(item["pane_id"] != pane and item.get("tab_id") == tab for item in panes):
        return False
    return client.retitle(pane, name)


def main() -> int:
    try:
        payload = cast(StopPayload, json.loads(sys.stdin.read() or "{}"))
        _ = sync(payload, os.environ)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"herdr title: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
