#!/usr/bin/env python3
"""Move Bevy records and notifier jobs to staff ids. Delete this migration in Phase 18."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.lib.atomic_file import sync_folder, write_bytes_atomic, write_text_atomic
from scripts.production import staff
from scripts.production.store_owner import document_id
import upgrade_record
import upgrade_schedule


def _owner(document: Path) -> staff.StaffId:
    # Read current ownership again for each file, including after a clear.
    return document_id(document, record=staff.snapshot())


def _record(source: Path) -> bool:
    value = cast(object, json.loads(source.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{source}: expected a record object")
    fields = cast(dict[str, object], value)
    document = fields.get("production")
    if not isinstance(document, str):
        raise ValueError(f"{source}: production document evidence is unavailable")
    identity = _owner(Path(document))
    reference = fields.get("staff_id")
    if reference is not None and reference != identity:
        raise staff.StaffRefused(f"{source}: the recorded owner differs from the document owner")
    legacy = fields.pop("slug", None)
    fields["staff_id"] = identity
    record = upgrade_record.validate(fields)
    # Keep the target version in each closed archive's file name.
    suffix = f".{record['target']}.closed.json" if source.name.endswith(".closed.json") else ".json"
    target = source.with_name(f"{identity}{suffix}")
    if source == target and legacy is None and reference == identity:
        return False
    if _owner(Path(document)) != identity:
        raise staff.StaffRefused(f"{source}: ownership evidence changed")
    if target != source and target.exists():
        existing = cast(object, json.loads(target.read_text(encoding="utf-8")))
        if existing != fields:
            raise staff.StaffRefused(f"{target}: unequal migration destination")
    else:
        write_text_atomic(target, json.dumps(fields, indent=2, ensure_ascii=False) + "\n")
    if source != target:
        source.unlink()
        sync_folder(source.parent)
    return True


def _job_owner(words: list[str], source: Path) -> staff.StaffId:
    if "--staff-id" in words:
        reference = words[words.index("--staff-id") + 1]
        row = staff.snapshot().by_id(staff.StaffId(reference))
        if row is None or row.kind != "showrunner":
            raise staff.StaffRefused(f"{source}: notifier ownership evidence is unavailable")
        return row.id
    if "--production" not in words:
        raise staff.StaffRefused(f"{source}: notifier production evidence is unavailable")
    return _owner(Path(words[words.index("--production") + 1]))


def _merge_job(source: Path, target: Path, identity: staff.StaffId, updated: str) -> None:
    values = dict(line.split("=", 1) for line in (target / "conf").read_text().splitlines() if "=" in line)
    if _job_owner(shlex.split(values.get("RUN", "")), target) != identity:
        raise staff.StaffRefused(f"{target}: notifier destination has another owner")
    files = [path for path in source.iterdir() if path.name != "conf"]
    for path in files:
        destination = target / path.name
        if not path.is_file() or (destination.exists() and not destination.is_file()):
            raise staff.StaffRefused(f"{path}: expected a notifier file")
        if (destination.exists() and path.name not in ("state", "lock")
                and not path.name.endswith(".log") and path.read_bytes() != destination.read_bytes()):
            raise staff.StaffRefused(f"{destination}: unequal notifier file")
    write_text_atomic(target / "conf", updated)
    for path in files:
        destination = target / path.name
        if not destination.exists() or path.name == "state":
            # Keep the legacy schedule, including its pause and stop state.
            _ = path.replace(destination)
        else:
            if path.name.endswith(".log"):
                previous, incoming = destination.read_bytes(), path.read_bytes()
                if not previous.endswith(incoming):
                    write_bytes_atomic(destination, previous + incoming)
            path.unlink()
    sync_folder(target)
    (source / "conf").unlink()
    source.rmdir()
    sync_folder(source.parent)


def _job(source: Path) -> bool:
    conf = source / "conf"
    if not conf.exists() and not any(source.iterdir()):
        source.rmdir()
        sync_folder(source.parent)
        return True
    text = conf.read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    words = shlex.split(values.get("RUN", ""))
    identity = _job_owner(words, source)
    if upgrade_record.load(identity) is None:
        raise staff.StaffRefused(f"{source}: the migrated watch is unavailable")
    target = source.with_name(upgrade_schedule.instance(identity))
    updated = "".join(
        f"RUN={upgrade_schedule.command(identity)}\n" if line.startswith("RUN=")
        else f"FROM={upgrade_schedule.instance(identity)}\n" if line.startswith("FROM=")
        else line for line in text.splitlines(keepends=True))
    if source == target and updated == text:
        return False
    if _job_owner(words, source) != identity:
        raise staff.StaffRefused(f"{source}: notifier ownership evidence changed")
    if source != target and target.exists():
        _merge_job(source, target, identity, updated)
        return True
    if updated != text:
        write_text_atomic(conf, updated)
    if source != target:
        _ = source.rename(target)
        sync_folder(source.parent)
    return True


def migrate(*, stop_after: int = 0) -> int:
    """Move records before jobs; retain missing ownership evidence for the next run."""
    notifier = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    completed = 0
    missing: list[str] = []
    with staff.operation():
        for source in sorted(upgrade_record.state_dir().glob("*.json")):
            try:
                changed = _record(source)
            except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                missing.append(str(error))
                continue
            if changed:
                completed += 1
                print(f"moved record {source}")
                if stop_after and completed >= stop_after:
                    return 75
        # A missing record prevents its job from migrating.
        for source in sorted(notifier.glob("bevy-upgrade-*")):
            if not source.is_dir():
                continue
            try:
                changed = _job(source)
            except (OSError, ValueError, IndexError, upgrade_record.RecordFailure,
                    staff.StaffRefused, staff.StaffUnreadable) as error:
                missing.append(str(error))
                continue
            if changed:
                completed += 1
                print(f"moved notifier {source}")
                if stop_after and completed >= stop_after:
                    return 75
    for problem in missing:
        print(f"unavailable: {problem}", file=sys.stderr)
    print(f"moved {completed}; unavailable {len(missing)}")
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args()
    try:
        return migrate(stop_after=cast(int, args.stop_after))
    except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"migration: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
