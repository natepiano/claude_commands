#!/usr/bin/env python3
"""Record compaction start without preventing compaction on an error."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, cast


class CompactionStart(TypedDict):
    session_id: str
    family: Literal["claude", "codex"]
    started_at: str


def main() -> int:
    try:
        from scripts.lib import conf
        from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, write_text_atomic
        from scripts.hooks.compaction_marker import SESSION_ID_PATTERN, marker_folder

        with conf.diagnostics(report=False) as diagnostics:
            retention = conf.read("compaction_marker").number("retention_hours")
        if diagnostics:
            return 0
        folder = marker_folder()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now = datetime.now(UTC)
            cutoff = (now - timedelta(hours=retention)).timestamp()
            for marker in folder.glob("*.json"):
                try:
                    if marker.is_file() and marker.stat().st_mtime < cutoff:
                        marker.unlink(missing_ok=True)
                except OSError:
                    continue

            if len(sys.argv) != 3 or sys.argv[1] != "--family" or sys.argv[2] not in {"claude", "codex"}:
                return 0
            payload = cast(object, json.loads(sys.stdin.read()))
            if not isinstance(payload, dict):
                return 0
            fields = cast(dict[str, object], payload)
            session_id = fields.get("session_id")
            if (fields.get("hook_event_name") != "PreCompact" or not isinstance(session_id, str)
                    or not SESSION_ID_PATTERN.fullmatch(session_id)):
                return 0
            start: CompactionStart = {"session_id": session_id,
                "family": cast(Literal["claude", "codex"], sys.argv[2]), "started_at": now.isoformat()}
            path = folder / f"{session_id}.json"
            content = json.dumps(start) + "\n"
            try:
                write_text_atomic(path, content)
            except AtomicDurabilityUnconfirmed:
                if path.read_text(encoding="utf-8") == content:
                    path.unlink()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
