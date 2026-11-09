"""Keep only the staff row IDs that still need a start."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import cast


from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, session_staff
from scripts.shutdown.stop_results import shutdown_state_root


def row_folder(row: staff.StaffRow, record: staff.StaffSnapshot, *,
               resolver: session_staff.RowResolver | None = None) -> tuple[str, Path]:
    facts = session_staff.RowResolver(record) if resolver is None else resolver
    # A recorded worker folder remains valid for older stopped worker rows.
    tree = row.worktree if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else facts.unit_worktree(row)
    if isinstance(tree, (staff.PresentWorktree, staff.RemovedWorktree)):
        return tree.machine, tree.path
    if row.kind == "unit":
        raise ValueError(f"{row.name}: the staff worktree is unavailable")
    if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return row.doc.machine, row.doc.path.parent
    if row.kind == "agent":
        from scripts.shutdown import remote

        return remote.local_machine(), Path.home()
    document = facts.production_doc(row)
    if isinstance(document, session_staff.ProductionDoc):
        return document.machine, document.path.parent
    raise ValueError(f"{row.name}: {document.reason}")


def record_path() -> Path:
    return shutdown_state_root() / "restart.json"


def read() -> list[str]:
    try:
        value = cast(object, json.loads(record_path().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("restart record must contain only staff row IDs")
    return list(dict.fromkeys(cast(list[str], value)))


def save(row_ids: list[str]) -> None:
    path = record_path()
    content = json.dumps(list(dict.fromkeys(row_ids))) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        write_text_atomic(path, content)


def migrate(*, stop_after_first: bool = False) -> tuple[str, ...]:
    """Replace legacy names with fixed row ids under the record lock."""
    path = record_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        references = read()
        record = staff.snapshot()
        rows: list[str] = []
        proven: set[staff.StaffId] = set()
        for reference in references:
            row = record.by_member_reference(reference) or record.by_path(reference)
            if row is None:
                matches = [item for item in record.rows if item.name == reference]
                if len(matches) != 1:
                    print(f"{' '.join(reference.split())}: migration anomaly: staff ownership is unavailable", file=sys.stderr)
                    rows.append(reference)
                    continue
                row = matches[0]
            rows.append(str(row.id))
            proven.add(row.id)
        if rows == references:
            return ()
        # Resolve ownership again before publication, including a concurrent clear.
        current = staff.snapshot()
        for row_id in proven:
            if current.by_id(row_id) != record.by_id(row_id):
                raise ValueError(f"{row_id}: staff ownership changed; rerun the migration")
        save(rows)
        return (str(record_path()) + ("; stopped after first file" if stop_after_first else ""),)


def update(*, started: list[str], pending: list[str]) -> None:
    """Apply this run's changes without replacing another run's pending rows."""
    path = record_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save([row for row in read() if row not in started] + pending)


class MigrationArguments(argparse.Namespace):
    stop_after_first: bool = False


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("migrate",))
    _ = parser.add_argument("--stop-after-first", action="store_true")
    options = parser.parse_args(namespace=MigrationArguments())
    changes = migrate(stop_after_first=options.stop_after_first)
    print("\n".join(changes) if changes else "unchanged")
