#!/usr/bin/env python3
"""Move one production store to fixed staff ids. Delete this migration in Phase 18."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic
from scripts.production import session_staff, staff
from scripts.production.store_owner import showrunner_id


def move_file(source: Path, target: Path) -> None:
    """Accept equal destinations so an interrupted publication can finish."""
    content = source.read_bytes()
    if target.exists():
        if target.read_bytes() != content:
            raise staff.StaffRefused(f"{target}: unequal migration destination")
    else:
        write_bytes_atomic(target, content)
    source.unlink()
    sync_folder(source.parent)


def migrate(store: str, *, stop_after: int = 0) -> int:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    completed = 0
    missing: list[str] = []
    with staff.operation():
        try:
            record = staff.snapshot()
        except staff.StaffUnreadable as error:
            print(f"unavailable: {error}", file=sys.stderr)
            return 1
        if store == "outstanding":
            directories = tuple((root / "outstanding", suffix) for suffix in
                                (".json", ".answered.jsonl", ".history.jsonl", ".lock"))
        else:
            raise staff.StaffRefused(f"{store}: unsupported store")
        for directory, suffix in directories:
            for source in sorted(directory.glob("*")):
                if not source.is_file() or (suffix and not source.name.endswith(suffix)):
                    continue
                reference = source.name.removesuffix(suffix) if suffix else source.name
                try:
                    identity = showrunner_id(reference, record=record)
                    if reference == identity:
                        continue
                    # A clear can change current_session while the fixed owner stays the same.
                    current_record = staff.snapshot()
                    current_sources = session_staff.member_sources()
                    current_identity = showrunner_id(reference, record=current_record, sources=current_sources)
                    if current_identity != identity:
                        raise staff.StaffRefused(f"{reference}: ownership evidence became unavailable")
                    move_file(source, directory / f"{identity}{suffix}")
                except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                    missing.append(str(error))
                    continue
                completed += 1
                print(f"moved {source} to {identity}{suffix}")
                if stop_after and completed >= stop_after:
                    return 75
    for problem in missing:
        print(f"unavailable: {problem}", file=sys.stderr)
    print(f"moved {completed}; unavailable {len(missing)}")
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("store", choices=("outstanding",))
    _ = parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args()
    try:
        return migrate(cast(str, args.store), stop_after=cast(int, args.stop_after))
    except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"migration: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
