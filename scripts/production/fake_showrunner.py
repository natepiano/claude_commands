"""Write staff-owned showrunner timers and session records for tests."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from scripts.lib.socket_test_support import ShortUnixSocket
from scripts.production.staff_test_support import recorded_showrunner, fixture_runtime_id
from scripts.production import staff


def write_timer(notifier_dir: Path, production_name: str, session_id: str, zone: str, doc: Path | str) -> Path:
    """Write the update timer of the production `production_name`, addressed to `session_id`.

    Older docs carry a User zone line, so the doc is given one unless `zone` is empty. It is not read.
    """
    row = recorded_showrunner(production_name, Path(doc))
    try:
        runtime = staff.native_id(session_id)
    except staff.StaffRefused:
        runtime = staff.native_id(fixture_runtime_id(session_id))
    if row.current_session != runtime:
        row = staff.update_current_session(row.current_session, runtime)
    instance = notifier_dir / f"showrunner-{row.id}"
    instance.mkdir(parents=True, exist_ok=True)
    path = Path(doc)
    held = path.read_text(encoding="utf-8") if path.is_file() else ""
    if zone and "- **User zone:** " not in held:
        _ = path.write_text(f"- **User zone:** {zone}\n{held}", encoding="utf-8")
    _ = (instance / "conf").write_text(
        f"TARGET=session:{row.id}\nPROMPT=scheduled\nCHECK=zsh /scripts/production_check.sh {row.id}\n",
        encoding="utf-8")
    return instance


def write_session(sessions_dir: Path, name: str, session_id: str, pid: int | None = None) -> socket.socket:
    """Record a live Claude session called `name`. The caller closes the returned socket at cleanup."""
    try:
        session_id = staff.native_id(session_id)
    except staff.StaffRefused:
        session_id = fixture_runtime_id(session_id)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    process = os.getpid() if pid is None else pid
    held = ShortUnixSocket()
    try:
        path = held.bind_socket()
        _ = (sessions_dir / f"{process}.json").write_text(json.dumps({
            "pid": process, "sessionId": session_id, "name": name, "messagingSocketPath": str(path),
            "updatedAt": 1, "tmux": ""}), encoding="utf-8")
    except BaseException:
        held.close()
        raise
    return held
