"""Supply explicit staff rows to a private command interpreter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.production import staff


def staff_child_command(script: Path, rows: list[staff.StaffRow], *arguments: str) -> list[str]:
    """Keep fake runtime identities separate from the live staff record."""
    source = """import json, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from scripts.production import staff
values = json.loads(sys.argv.pop(1))
rows = tuple(staff.StaffRow(staff.StaffId(row['id']), row['kind'], row['name'],
    staff.ReportsTo(staff.StaffId(row['parent'])) if row['parent'] else staff.StaffRoot(),
    current_session=staff.StaffId(row['current_session'])) for row in values)
staff.snapshot = lambda: staff.StaffSnapshot(rows)
staff.initial_snapshot = staff.snapshot
staff.local_snapshot = staff.snapshot
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    return [sys.executable, "-c", source, str(Path(__file__).resolve().parents[2]),
            json.dumps([row.to_json() for row in rows]), str(script), *arguments]
