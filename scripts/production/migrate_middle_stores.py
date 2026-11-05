#!/usr/bin/env python3
"""Move one production store to fixed staff ids. Delete this migration in Phase 18."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic
from scripts.production import session_staff, staff
from scripts.production.store_owner import showrunner_id


def dated_lines(content: bytes) -> list[tuple[datetime, bytes]]:
    """Keep undated lines after all earlier lines from the same file."""
    dated: list[tuple[datetime, bytes]] = []
    previous = datetime.min.replace(tzinfo=UTC)
    for line in content.splitlines(keepends=True):
        instant = previous
        try:
            value = cast(object, json.loads(line))
            if isinstance(value, dict):
                stamp = cast(dict[str, object], value).get("time")
                if isinstance(stamp, str):
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is not None:
                        instant = parsed
                        previous = max(previous, parsed)
        except (ValueError, UnicodeError):
            pass
        dated.append((instant, line if line.endswith(b"\n") else line + b"\n"))
    return dated


def move_file(source: Path, target: Path) -> None:
    """Merge dated logs and accept equal destinations before removing the source."""
    content = source.read_bytes()
    if source.suffix == ".lock" and not content:
        source.unlink()
        sync_folder(source.parent)
        return
    if target.exists():
        destination = target.read_bytes()
        if destination != content:
            if source.name.endswith((".history.jsonl", ".answered.jsonl")):
                lines = dated_lines(destination) + dated_lines(content)
                lines.sort(key=lambda entry: entry[0])
                write_bytes_atomic(target, b"".join(entry[1] for entry in lines))
            else:
                raise ValueError(f"unequal migration destination {target}; left in place")
    else:
        write_bytes_atomic(target, content)
    source.unlink()
    sync_folder(source.parent)


def migrate(store: str, *, stop_after: int = 0) -> int:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    completed = 0
    left = 0
    unavailable = 0
    with staff.operation():
        try:
            record = staff.snapshot()
        except staff.StaffUnreadable as error:
            print(f"anomaly: {error}")
            print("moved 0; left in place 0; unavailable 1")
            return 1
        if store == "outstanding":
            directories = tuple((root / "outstanding", suffix) for suffix in
                                (".json", ".answered.jsonl", ".history.jsonl", ".lock"))
        else:
            raise staff.StaffRefused(f"{store}: unsupported store")
        for directory, suffix in directories:
            for source in sorted(directory.glob("*")):
                if not source.is_file() or not source.name.endswith(suffix):
                    continue
                reference = source.name.removesuffix(suffix)
                try:
                    identity = showrunner_id(reference, record=record)
                except OSError as error:
                    print(f"anomaly: {source}: {error}")
                    owner = record.by_path(reference)
                    if owner is not None and owner.kind == "showrunner":
                        unavailable += 1
                    else:
                        left += 1
                    continue
                except (ValueError, staff.StaffRefused, staff.StaffUnreadable):
                    print(f"anomaly: {source}: no showrunner row; left in place")
                    left += 1
                    continue
                if reference == identity:
                    continue
                try:
                    # A clear can change the conversation while the fixed owner stays the same.
                    current_record = staff.snapshot()
                    current_sources = session_staff.member_sources()
                    current_identity = showrunner_id(reference, record=current_record, sources=current_sources)
                    if current_identity != identity:
                        raise staff.StaffRefused("ownership changed; left in place")
                    move_file(source, directory / f"{identity}{suffix}")
                except OSError as error:
                    print(f"anomaly: {source}: {error}")
                    unavailable += 1
                    continue
                except (ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                    print(f"anomaly: {source}: {error}")
                    left += 1
                    continue
                completed += 1
                print(f"moved {source} to {identity}{suffix}")
                if stop_after and completed >= stop_after:
                    print(f"moved {completed}; left in place {left}; unavailable {unavailable}")
                    return 75
    print(f"moved {completed}; left in place {left}; unavailable {unavailable}")
    return 1 if unavailable else 0


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
