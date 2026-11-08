#!/usr/bin/env python3
"""Archive the phase notes of retired units, for every production that has an update timer.

The agent-notes job on natedev runs this on its configured interval, in its own process
(`scripts/maintenance/agent_notes_job.py`); until 2026-10-13 a dailies did it,
so a production that never built one kept its retired units' notes (user, 2026-10-13: "Yes do
the part C"). It runs under nix's plain python3, so it uses the standard library alone.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys

from scripts.direct import phase_table
from scripts.production import showrunners, staff
from scripts.production.production_doc import Refusal

from scripts.shutdown import remote


def prune_all() -> list[str]:
    """Prune each update timer's production, running or not; return one line per production that failed.

    Read each timer owner from staff. Do not read session records.
    Continue after a production fails.
    """
    failures: list[str] = []
    record = staff.snapshot()
    for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
        if not instance.is_dir():
            continue
        owner_id = staff.StaffId(instance.name.removeprefix("showrunner-"))
        owner = record.by_id(owner_id)
        if owner is None or owner.kind != "showrunner":
            failures.append(f"{owner_id}: no showrunner staff row")
            continue
        if (not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
                or owner.doc.machine != remote.local_machine()):
            failures.append(f"{owner.name}: timer owner has no local staff document")
            continue
        try:
            phase_table.prune(owner.doc.path)
        except (Refusal, OSError, UnicodeError) as error:
            failures.append(f"{owner.name}: {error}")
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
