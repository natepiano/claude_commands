#!/usr/bin/env python3
"""Archive the phase notes of retired units, for every production that has an update timer.

The agent-sessions snapshot timer on natedev runs this every 2 minutes, in its own process
(`/etc/nixos/modules/linux/agent-sessions-snapshot.py`); until 2026-10-13 a dailies did it,
so a production that never built one kept its retired units' notes (user, 2026-10-13: "Yes do
the part C"). It runs under nix's plain python3, so it uses the standard library alone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..direct import phase_table
    from . import showrunners
    from .production_units import Refusal
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.direct import phase_table
    from scripts.production import showrunners
    from production_units import Refusal


def prune_all() -> list[str]:
    """Prune each update timer's production, running or not; return one line per production that failed.

    The doc comes from the timer alone. No session record is read: whether a showrunner runs does
    not change which notes are retired. One production failing never stops the next.
    """
    failures: list[str] = []
    for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
        if not instance.is_dir():
            continue
        slug = instance.name.removeprefix("showrunner-")
        checked = showrunners.checked_doc(instance)
        if isinstance(checked, showrunners.NoCheckedDoc):
            failures.append(f"{slug}: {checked.reason}")
            continue
        try:
            phase_table.prune(checked.path)
        except (Refusal, OSError, UnicodeError) as error:
            failures.append(f"{slug}: {error}")
    return failures


def main() -> int:
    try:
        failures = prune_all()
    except OSError as error:
        print(f"phase notes not pruned: {error}", file=sys.stderr)
        return 1
    for failure in failures:
        print(f"phase notes not pruned for {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
