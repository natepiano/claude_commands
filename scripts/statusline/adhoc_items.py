"""Store the open review count for one Claude session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
from pathlib import Path

from scripts.lib.atomic_file import write_text_atomic


def _state_file(session_id: str) -> Path:
    return Path.home() / ".local/state/adhoc_review" / session_id


def row(session_id: str) -> str:
    """Read one session's count without reading a production."""
    if not session_id:
        return ""
    try:
        text = _state_file(session_id).read_text(encoding="utf-8").strip()
        remaining = int(text) if text.isdecimal() else 0
    except (OSError, ValueError):
        return ""
    if remaining <= 0:
        return ""
    return f"{remaining} adhoc item" + ("s" if remaining != 1 else "")


def main() -> None:
    remaining = int(sys.argv[1])
    path = _state_file(os.environ["CLAUDE_CODE_SESSION_ID"])
    if remaining == 0:
        path.unlink(missing_ok=True)
    else:
        write_text_atomic(path, str(remaining))


if __name__ == "__main__":
    main()
