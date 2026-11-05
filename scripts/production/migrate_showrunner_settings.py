#!/usr/bin/env python3
"""Move the legacy settings into the shared configuration file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import showrunners, staff


def migrate(source: Path, destination: Path, membership: staff.StaffSnapshot) -> None:
    """Resolve recipient identities before writing or deleting a file."""
    if not source.exists():
        _ = showrunners.load_settings_from(destination)
        return
    data = cast(dict[str, object], json.loads(source.read_text(encoding="utf-8")))

    def identity(reference: str) -> str:
        row = membership.by_id(staff.StaffId(reference))
        if row is not None:
            return str(row.id)
        matches = [row for row in membership.rows if row.name == reference]
        if len(matches) != 1:
            raise ValueError(f"{reference}: expected one staff row")
        return str(matches[0].id)

    settings = showrunners.ShowrunnerSettings(
        threshold_percent=cast(float, data["threshold_percent"]),
        repeat_minutes=cast(float, data["repeat_minutes"]),
        stall_minutes=cast(float, data["stall_minutes"]),
        faults_to=identity(cast(str, data["faults_to"])),
        always=[identity(name) for name in cast(list[str], data["always"])])
    text = "".join(f"# range: 0..\n{key}={settings[key]}\n"
                   for key in ("threshold_percent", "repeat_minutes", "stall_minutes"))
    text += "faults_to=" + settings["faults_to"] + "\n"
    text += "always=" + " ".join(settings["always"]) + "\n"
    if destination.exists():
        try:
            current = showrunners.load_settings_from(destination)
        except ValueError as error:
            raise ValueError(f"{destination}: settings differ; retain the source") from error
        if current != settings:
            raise ValueError(f"{destination}: settings differ; retain the source")
    else:
        write_text_atomic(destination, text)
        _ = showrunners.load_settings_from(destination)
    source.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("source", type=Path)
    _ = parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        migrate(cast(Path, args.source), cast(Path, args.destination), staff.snapshot())
    except (OSError, ValueError, KeyError) as error:
        print(f"migrate_showrunner_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
