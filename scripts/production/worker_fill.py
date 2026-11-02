#!/usr/bin/env python3
"""Admit workers through staff, and fill missing rows from existing worker readers."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
from pathlib import Path
import re
import sys
import time
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, staff_sources, unit_lookup
from scripts.direct import workers
from scripts.astra import design_roles
from scripts.agents import codex_mesh


from scripts.production.worker_admission import (
    AdmissionRefused as AdmissionRefused, WorkerAdmitted as WorkerAdmitted, admit as admit,
)


@dataclass(frozen=True)
class WorkerSource:
    path: Path
    key: str


class FillSources(staff_sources._WorkerSources):  # pyright: ignore[reportPrivateUsage]
    """Keep source locations around the shared collector's decoded observations."""

    def __init__(self, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources) -> None:
        self.source: WorkerSource = WorkerSource(Path("."), "record")
        self.locations: dict[tuple[str, str], WorkerSource] = {}
        self.thread_activity: dict[Path, set[str] | ValueError] = {}
        super().__init__(record, sources)

    @override
    def codex_thread_live(self, run: Path, thread: str) -> bool:
        if run not in self.thread_activity:
            for filename in (codex_mesh.SERVER_FILE, codex_mesh.RETIRED_FILE):
                self.dependencies(run / filename)
            deadline = time.monotonic() + codex_mesh.THREAD_RELEASE_TIMEOUT_SECS
            try:
                self.thread_activity[run] = codex_mesh.loaded_threads(run, deadline)
            except ValueError as error:
                self.thread_activity[run] = error
        activity = self.thread_activity[run]
        if isinstance(activity, ValueError):
            raise activity
        return thread in activity

    @override
    def add(self, observed: staff_sources.WorkerObservation) -> None:
        super().add(observed)
        self.locations[(observed.owner.key, observed.name)] = self.source

    @override
    def ledger(self, path: Path, owner: staff_sources.WorkerOwner | None = None) -> None:
        self.source = WorkerSource(path, "workers")
        super().ledger(path, owner)

    @override
    def roster(self, run: Path, owner: staff_sources.WorkerOwner | None = None) -> None:
        self.source = WorkerSource(run / workers.ROSTER, "workers")
        super().roster(run, owner)

    @override
    def claude_workers(self, run: Path) -> None:
        self.source = WorkerSource(run / "workers", "workers")
        super().claude_workers(run)

    @override
    def design(self, design_id: str) -> None:
        self.source = WorkerSource(design_roles.design_directory(design_id) / "design.json", "roles")
        super().design(design_id)

    @override
    def astra(self, path: Path) -> None:
        self.source = WorkerSource(path, "worker")
        super().astra(path)


def fill(*, dry_run: bool = False) -> int:
    problems: list[str] = []
    placed = 0
    with redirect_stderr(io.StringIO()) as errors:
        record = staff.snapshot()
        sources = FillSources(record, unit_lookup.member_sources())
        observations = sources.collect()
    for line in errors.getvalue().splitlines():
        line = line.removeprefix("staff sources: ")
        match = re.fullmatch(r"(.*) worker (.*?): (.*)", line)
        problems.append(f"{match[1]}:workers {match[2]}: {match[3]}" if match else f"{line}:record ?: source unavailable")
    active = {row.id for row in record.rows if row.kind in ("unit", "showrunner")
              and not isinstance(row.worktree, staff.RemovedWorktree) and not isinstance(row.doc, staff.WrappedDoc)}
    for observed in observations:
        location = sources.locations[(observed.owner.key, observed.name)]
        prefix = f"{location.path}:{location.key} {observed.name}"
        parent = observed.owner.match
        if not isinstance(parent, staff.StaffRow) or parent.kind not in ("unit", "showrunner"):
            problems.append(f"{prefix}: worker has no current staff parent")
            continue
        if parent.id not in active:
            problems.append(f"{prefix}: parent {parent.id} is not live")
            continue
        if any(row.name == observed.name for row in record.find(kind="worker", parent=parent.id)):
            continue
        try:
            if not dry_run:
                _ = staff.ensure_worker(observed.name, parent.id)
            placed += 1
            print(f"{'would place' if dry_run else 'placed'} {observed.name} under {parent.id}")
        except (staff.StaffRefused, staff.StaffUnreadable, OSError) as error:
            problems.append(f"{prefix}: {error}")
    for problem in dict.fromkeys(problems):
        print(problem, file=sys.stderr)
    skipped = len(sources.skipped_runs) + len(sources.skipped_ledgers)
    if skipped:
        print(f"skipped {skipped} not current")
    if not placed and not problems:
        print("unchanged")
    else:
        print(f"placed {placed}; listed {len(dict.fromkeys(problems))}")
    return 1 if problems else 0


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False
    name: str = ""
    run: Path = Path(".")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("fill", "admit"))
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "admit":
            _ = admit(args.name, args.run)
            return 0
        return fill(dry_run=args.dry_run)
    except (AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
