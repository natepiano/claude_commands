"""Resume restored Claude and Codex panes on their retained accounts.

herdr restores the layout without starting agents. A restore opens a tab on the retained account.
It keeps running sessions and occupied panes. It resumes intentionally stopped sessions when requested.

Run: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/pane_restore.py [--session NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

SCRIPTS = Path(__file__).resolve().parent.parent
for dependency in ("whoami", "message", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import herdr  # noqa: E402
import pane_record  # noqa: E402
from scripts.shutdown.launch_names import LaunchNames
from scripts.production import staff  # noqa: E402

SERVER_TIMEOUT_MS = 60_000


def restore(session: str, *, dry_run: bool) -> int:
    from scripts.shutdown import restart

    client = herdr.Herdr(session)
    names = LaunchNames.read()
    record = names.tab_record
    failures = 0
    try:
        for note in herdr.repair_retained_launches(record, pane_record.record_directory(), dry_run=dry_run):
            print(note)
    except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
        print(f"pane_restore: retained launch repair failed: {error}", file=sys.stderr)
        failures += 1
    selected: dict[staff.StaffId, staff.StaffRow] = {}
    entries = pane_record.read(session)
    for pane in client.tab_sessions():
        saved = entries.get(pane.pane_id)
        reference = saved["session_id"] if saved is not None else pane.session_id or ""
        row = record.by_member_reference(reference)
        if row is None:
            print(f"{pane.pane_id} {pane.label}: left as it is: unknown staff identity {reference}")
            continue
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = parent if parent is not None else row
        selected[holder.id] = holder
        if dry_run:
            entry = names.resume(saved) if saved is not None else None
            account = entry["config_dir"] if entry is not None else "retained account"
            print(f"{pane.pane_id} {row.name}: resume {row.current_session} on {account}")
    if dry_run:
        return 1 if failures else 0
    for holder in selected.values():
        try:
            with staff.row_operation(str(holder.id)):
                current = staff.initial_snapshot()
                row = current.by_id(holder.id)
                if row is not None:
                    with herdr.restore_staff(current):
                        _ = restart.start_row(row, current, client)
        except (herdr.HerdrFailure, OSError, ValueError, staff.StaffRefused) as error:
            failures += 1
            print(f"{holder.name}: not resumed: {error}", file=sys.stderr)
    return 1 if failures else 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("--session", default="")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments)
    session = cast(str, options.session) or herdr.shared_session()
    _ = herdr.start_server(session, SERVER_TIMEOUT_MS)
    return restore(session, dry_run=cast(bool, options.dry_run))


if __name__ == "__main__":
    sys.exit(main())
