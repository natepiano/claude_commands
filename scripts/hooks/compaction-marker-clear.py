#!/usr/bin/env python3
"""Clear the input session's compaction marker without changing the hook result."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from typing import cast


def main() -> int:
    try:
        from scripts.hooks.compaction_marker import clear

        payload = cast(object, json.loads(sys.stdin.read()))
        if isinstance(payload, dict):
            session_id = cast(dict[str, object], payload).get("session_id")
            if isinstance(session_id, str):
                clear(session_id)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
