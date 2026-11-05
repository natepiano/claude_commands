#!/usr/bin/env python3
"""Move timer folders to fixed staff ids. Repeat the command after an interruption."""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import showrunners, staff
from scripts.shutdown.remote import local_machine


def migrate(root: Path) -> list[str]:
    changed: list[str] = []
    for instance in sorted(root.glob("showrunner-*")):
        record = staff.snapshot()
        conf = instance / "conf"
        lines = conf.read_text(encoding="utf-8").splitlines(keepends=True)
        index = next(index for index, line in enumerate(lines) if line.startswith("CHECK="))
        command = shlex.split(lines[index].removeprefix("CHECK="))
        argument = next(index + 1 for index, token in enumerate(command)
                        if Path(token).name == "production_check.sh")
        row = (record.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
               or record.by_id(staff.StaffId(command[argument]))
               or record.by_doc(local_machine(), Path(command[argument])))
        if row is None:
            raise ValueError(f"{instance}: staff ownership is unavailable")
        command[argument] = row.id
        lines[index] = "CHECK=" + shlex.join(command) + "\n"
        text = "".join(lines)
        if text != conf.read_text(encoding="utf-8"):
            write_text_atomic(conf, text)
        destination = instance.with_name(f"showrunner-{row.id}")
        if destination != instance:
            _ = instance.rename(destination)
            changed.append(str(destination))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--state-dir", type=Path, default=showrunners.NOTIFIER_STATE_DIR)
    args = parser.parse_args()
    for path in migrate(cast(Path, args.state_dir)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
