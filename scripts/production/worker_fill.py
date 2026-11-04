#!/usr/bin/env python3
"""Admit workers through staff, and fill missing rows from existing worker readers."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff


from scripts.production.worker_admission import (
    AdmissionRefused as AdmissionRefused, WorkerStartAllowed as WorkerStartAllowed, PublishedWorker as PublishedWorker, admit as admit, publish as publish,
)


def fill(*, dry_run: bool = False) -> int:
    """Staff already holds every published worker."""
    del dry_run
    print("unchanged")
    return 0


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False
    name: str = ""
    run: Path = Path(".")
    runtime_id: str = ""
    role: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("fill", "admit", "publish"))
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--runtime-id", default="")
    _ = parser.add_argument("--role")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command in ("admit", "publish"):
            allowed = admit(args.name, args.run, role=args.role or "")
            if args.command == "publish":
                _ = publish(allowed, args.runtime_id)
            return 0
        return fill(dry_run=args.dry_run)
    except (AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
