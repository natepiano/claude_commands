#!/usr/bin/env python3
"""Move Store 4 to fixed staff ids. Delete this migration in Phase 18."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic, write_text_atomic
from scripts.production import session_staff, showrunners, staff
from scripts.production.store_owner import showrunner_id

SHARED = frozenset(("reports", "outstanding", "counts", "footers-off", "review-paused"))


def unit_references(path: Path, owner: staff.StaffId, record: staff.StaffSnapshot) -> None:
    """Convert structured unit references and preserve free text."""
    if path.name not in {"showrunner_state.json", "dailies_judgment.json", "dailies_state.json",
                         "eta_seen.json", "decisions_seen", "blocks_open"}:
        return

    def identity(reference: str) -> staff.StaffId:
        row = record.by_member_reference(reference)
        if row is None:
            parent = record.by_id(owner)
            if parent is None:
                raise staff.StaffRefused(f"{path}: production ownership evidence is unavailable")
            found = session_staff.unit_match(session_staff.UnitMember("", reference), record,
                                             parent=session_staff.RequiredParent(parent))
            if isinstance(found, session_staff.NoSuchMember):
                raise staff.StaffRefused(f"{path}: unit {reference}: ownership evidence is unavailable")
            row = found.row
        if row.kind != "unit" or row.parent != staff.ReportsTo(owner):
            raise staff.StaffRefused(f"{path}: {reference}: expected a unit in this production")
        return row.id

    content = path.read_text()
    if path.name in {"decisions_seen", "blocks_open"}:
        delimiter = "|" if path.name == "decisions_seen" else "\t"
        lines: list[str] = []
        for line in content.splitlines(keepends=True):
            reference, separator, tail = line.partition(delimiter)
            if not separator:
                raise ValueError(f"{path}: expected a structured unit record")
            lines.append(f"{identity(reference)}{separator}{tail}")
        updated = "".join(lines)
    else:
        value = cast(object, json.loads(content))
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected a state object")
        fields = cast(dict[str, object], value)
        changed = False
        if path.name in {"showrunner_state.json", "dailies_judgment.json"}:
            units = fields.get("units", [])
            if not isinstance(units, list):
                raise ValueError(f"{path}: expected units list")
            seen: set[staff.StaffId] = set()
            for entry in cast(list[object], units):
                if not isinstance(entry, dict):
                    raise ValueError(f"{path}: expected a unit object")
                unit = cast(dict[str, object], entry)
                reference = unit.get("unit")
                if not reference:
                    continue
                if not isinstance(reference, str):
                    raise ValueError(f"{path}: expected unit text")
                fixed = identity(reference)
                if fixed in seen:
                    raise staff.StaffRefused(f"{path}: duplicate unit {fixed}")
                seen.add(fixed)
                if reference != fixed:
                    unit["unit"] = fixed
                    changed = True
        else:
            converted: dict[str, object] = {}
            for key, item in fields.items():
                reference, separator, tail = key.partition("|") if path.name == "eta_seen.json" else (key, "", "")
                fixed = f"{identity(reference)}{separator}{tail}"
                if fixed in converted and converted[fixed] != item:
                    raise staff.StaffRefused(f"{path}: unequal unit state for {fixed}")
                converted[fixed] = item
                changed = changed or fixed != key
            fields = converted
        updated = json.dumps(fields, indent=2) + "\n" if changed else content
    if updated != content:
        write_text_atomic(path, updated)


def migrate(*, stop_after: int = 0) -> int:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    completed = 0
    missing: list[str] = []
    with staff.operation():
        record = staff.snapshot()
        sources = session_staff.member_sources()
        folders = [path for path in sorted(root.glob("*")) if path.is_dir() and path.name not in SHARED]
        reports = sorted((root / "reports").glob("*.json"))
        for source_root in [*folders, *reports]:
            reference = source_root.stem if source_root.is_file() else source_root.name
            try:
                if source_root.is_symlink():
                    raise staff.StaffRefused(f"{source_root}: symbolic link needs ownership review")
                identity = showrunner_id(reference, record=record, sources=sources)
                destination = root / "reports" / f"{identity}.json" if source_root.is_file() else root / identity
                files = [source_root] if source_root.is_file() else sorted(source_root.rglob("*"))
                for source in files:
                    if source.is_symlink():
                        raise staff.StaffRefused(f"{source}: symbolic link needs ownership review")
                    if not source.is_file():
                        continue
                    current = staff.snapshot()
                    resolved = showrunner_id(reference, record=current, sources=session_staff.member_sources())
                    owner = current.by_id(identity)
                    if resolved != identity or owner is None or owner.kind != "showrunner":
                        raise staff.StaffRefused(f"{reference}: ownership evidence became unavailable")
                    unit_references(source, identity, current)
                    target = destination if source_root.is_file() else destination / source.relative_to(source_root)
                    if source == target:
                        continue
                    if target.is_symlink():
                        raise staff.StaffRefused(f"{target}: symbolic link needs ownership review")
                    content = source.read_bytes()
                    if target.exists():
                        if target.read_bytes() != content:
                            raise staff.StaffRefused(f"{target}: unequal migration destination")
                    else:
                        write_bytes_atomic(target, content)
                    source.unlink()
                    sync_folder(source.parent)
                    completed += 1
                    print(f"moved {source} to {target}")
                    if stop_after and completed >= stop_after:
                        return 75
                if source_root.is_dir():
                    current = staff.snapshot()
                    owner = current.by_id(identity)
                    if owner is None:
                        raise staff.StaffRefused(f"{identity}: ownership evidence became unavailable")
                    timer = showrunners.timer_for(owner, current, sources=sources.productions.values())
                    if isinstance(timer, showrunners.Timer):
                        conf = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{timer.slug}" / "conf"
                        content = conf.read_text()
                        old_prompt = root / reference / "prompt.txt"
                        new_prompt = root / identity / "prompt.txt"
                        updated = content.replace(f"PROMPT_FILE={old_prompt}\n", f"PROMPT_FILE={new_prompt}\n")
                        if updated != content:
                            write_text_atomic(conf, updated)
                if source_root.is_dir() and source_root != destination:
                    for folder in sorted(source_root.rglob("*"), reverse=True):
                        if folder.is_dir():
                            folder.rmdir()
                    source_root.rmdir()
                    sync_folder(root)
            except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                missing.append(str(error))
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
