"""Answer session socket lookups from fixture session records, in process or as the sessions script."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast


def socket_path(records: Path, target: str) -> str | None:
    """The socket of the record whose session matches target, or None when no record matches."""
    for path in records.glob("*.json"):
        record = cast(dict[str, str], json.loads(path.read_text()))
        if target == "session:" + record["sessionId"]:
            return record["messagingSocketPath"]
    return None


if __name__ == "__main__":
    command, target = sys.argv[1:]
    found = socket_path(Path(os.environ["NOTIFIER_SESSIONS_DIR"]), target) if command == "socket" else None
    if found is None:
        raise SystemExit(1)
    print(found)
