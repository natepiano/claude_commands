"""Parse one positive window option before opening the index."""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta


def positive_window(args: list[str], option: str, default: float, command: str, *,
                    integer: bool = False) -> tuple[datetime, datetime, float] | None:
    """Return the requested UTC window, or print the command usage."""
    try:
        amount = default
        if args:
            if len(args) != 2 or args[0] != option:
                raise ValueError("unexpected arguments")
            amount = float(int(args[1])) if integer else float(args[1])
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("window must be positive")
        end = datetime.now(UTC)
        start = end - (timedelta(days=amount) if option == "--days" else timedelta(hours=amount))
        return start, end, amount
    except (ValueError, OverflowError):
        print(f"usage: buildlog {command} [{option} N] (N must be a positive number)", file=sys.stderr)
        return None
