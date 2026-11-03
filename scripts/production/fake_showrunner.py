"""A stand-in for one showrunner in tests: its update timer and, when running, its live session record.

A showrunner is recorded nowhere but in its update timer, `showrunner-<slug>` in the notifier's state
directory, which is addressed to the Claude session id and checks the production doc.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from scripts.lib.socket_test_support import ShortUnixSocket


def write_timer(notifier_dir: Path, slug: str, session_id: str, zone: str, doc: Path | str) -> Path:
    """Write the update timer of the production `slug`, addressed to `session_id`.

    Older docs carry a User zone line, so the doc is given one unless `zone` is empty. It is not read.
    """
    instance = notifier_dir / f"showrunner-{slug}"
    instance.mkdir(parents=True, exist_ok=True)
    prompt = instance / "prompt.txt"
    _ = prompt.write_text(f"Run `zsh unit_status.sh /scratch/unit_status {zone} --production {doc}`.\n",
                          encoding="utf-8")
    path = Path(doc)
    held = path.read_text(encoding="utf-8") if path.is_file() else ""
    if zone and "- **User zone:** " not in held:
        _ = path.write_text(f"- **User zone:** {zone}\n{held}", encoding="utf-8")
    _ = (instance / "conf").write_text(
        f"TARGET=session:{session_id}\nPROMPT_FILE={prompt}\nCHECK=zsh /scripts/production_check.sh {doc}\n",
        encoding="utf-8")
    return instance


def write_session(sessions_dir: Path, name: str, session_id: str, pid: int | None = None) -> socket.socket:
    """Record a live Claude session called `name`. The caller closes the returned socket at cleanup."""
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
