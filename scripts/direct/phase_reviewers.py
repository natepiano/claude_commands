"""Print the names of this run's workers whose last dispatch was a review."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.direct import progress_history
from scripts.production import staff, worker_admission


def main(arguments: list[str]) -> int:
    run = Path(arguments[0])
    os.environ["UNIT_RUN_DIR"] = str(run)
    record = staff.local_snapshot()
    parent = worker_admission.parent(record, run=run)
    for row in record.find(kind="worker", parent=parent.id):
        dispatch = progress_history.last_dispatch(row.id)
        if isinstance(dispatch, progress_history.RecordedDispatch) and dispatch.role == "review":
            print(row.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
