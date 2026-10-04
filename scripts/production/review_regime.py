#!/usr/bin/env python3
"""Track whether the extra review seats pay for themselves.

Usage:
  review_regime.py add --unit <unit> --phase <N> --regime before|trial|after
                       --started <ISO> --merged <ISO> --holds <K> --merge-defects <D>
                       [--ux-findings <N>] [--code-findings <N>] [--review-minutes <M>]
                       [--note <text>]
  review_regime.py report [--since <ISO date>]
  review_regime.py watch
  review_regime.py ack

The trial (user decision 2026-10-01) added a UX reviewer to every phase that
changes the screen and a code-quality reviewer (the `craft` lens) to every
phase, both judging by the three gods in ~/.claude/docs/decision_criteria.md.
It ended 2026-10-04 (user decision): the code-quality reviewer was dropped, and
a phase started after that is `after`. The showrunner adds one row per merged
phase; `report` compares the phases before, under and after the trial:

- better: fewer holds at merge and fewer defects found by the merge design check;
- cost: longer phases (start to merge) and more review-seat minutes.

`holds` counts the checkpoints of the phase the showrunner held; `merge-defects`
counts the defect rows across all of that phase's merge design checks.

`watch` counts `after` phases toward WATCH_PHASES. Once the count is reached it
exits 3, the report waiting for the user, until `ack` records their
acknowledgment.
"""

import argparse
import json
import statistics
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

LEDGER = Path.home() / ".claude/data/review_regime.jsonl"
WATCH = Path.home() / ".claude/data/review_regime_watch.json"
REGIMES = ("before", "trial", "after")
# User decision 2026-10-04: the `after` phases watched before the report goes to the user.
WATCH_PHASES = 12
# `watch` exits with this while the finished report waits for the user's acknowledgment.
WAITING_FOR_ACKNOWLEDGMENT = 3


@dataclass(frozen=True)
class Row:
    unit: str
    phase: str
    regime: str
    started: str
    merged: str
    holds: int
    merge_defects: int
    ux_findings: int | None
    code_findings: int | None
    review_minutes: float | None
    note: str | None

    def hours(self) -> float:
        return (datetime.fromisoformat(self.merged) - datetime.fromisoformat(self.started)).total_seconds() / 3600


def optional_int(record: dict[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) else None


def read_rows() -> list[Row]:
    if not LEDGER.exists():
        return []
    rows: list[Row] = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        record = cast(dict[str, object], json.loads(line))
        minutes = record.get("review_minutes")
        note = record.get("note")
        rows.append(
            Row(
                unit=str(record["unit"]),
                phase=str(record["phase"]),
                regime=str(record["regime"]),
                started=str(record["started"]),
                merged=str(record["merged"]),
                holds=int(cast(int, record["holds"])),
                merge_defects=int(cast(int, record["merge_defects"])),
                ux_findings=optional_int(record, "ux_findings"),
                code_findings=optional_int(record, "code_findings"),
                review_minutes=float(minutes) if isinstance(minutes, int | float) else None,
                note=note if isinstance(note, str) else None,
            )
        )
    return rows


def add(arguments: argparse.Namespace) -> None:
    started = cast(str, arguments.started)
    merged = cast(str, arguments.merged)
    for label, moment in (("started", started), ("merged", merged)):
        parsed = datetime.fromisoformat(moment)
        if parsed.tzinfo is None:
            raise SystemExit(f"review_regime: --{label} {moment!r} needs a UTC offset")
    row = Row(
        unit=cast(str, arguments.unit),
        phase=cast(str, arguments.phase),
        regime=cast(str, arguments.regime),
        started=started,
        merged=merged,
        holds=cast(int, arguments.holds),
        merge_defects=cast(int, arguments.merge_defects),
        ux_findings=cast(int | None, arguments.ux_findings),
        code_findings=cast(int | None, arguments.code_findings),
        review_minutes=cast(float | None, arguments.review_minutes),
        note=cast(str | None, arguments.note),
    )
    if row.hours() < 0:
        raise SystemExit("review_regime: --merged is before --started")
    if any(existing.unit == row.unit and existing.phase == row.phase for existing in read_rows()):
        raise SystemExit(f"review_regime: {row.unit} phase {row.phase} already has a row")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as ledger:
        _ = ledger.write(json.dumps(asdict(row)) + "\n")
    print(f"added {row.unit} phase {row.phase} ({row.regime}): {row.holds} holds, {row.merge_defects} merge defects, {row.hours():.1f} h")


def mean_of(values: list[float]) -> str:
    return f"{statistics.mean(values):.1f}" if values else "-"


def median_of(values: list[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "-"


def report(since: str | None) -> None:
    rows = [row for row in read_rows() if since is None or row.merged >= since]
    lines = [f"| | {' | '.join(REGIMES)} |", "| --- " * (len(REGIMES) + 1) + "|"]
    groups = {regime: [row for row in rows if row.regime == regime] for regime in REGIMES}

    def line(name: str, value: Callable[[list[Row]], str]) -> None:
        lines.append(f"| {name} | {' | '.join(value(groups[regime]) for regime in REGIMES)} |")

    line("phases merged", lambda group: str(len(group)))
    line("holds per phase (mean)", lambda group: mean_of([float(row.holds) for row in group]))
    line("merge design-check defects per phase (mean)", lambda group: mean_of([float(row.merge_defects) for row in group]))
    line("hours, start to merge (median)", lambda group: median_of([row.hours() for row in group]))
    line(
        "review-seat minutes per phase (mean)",
        lambda group: mean_of([row.review_minutes for row in group if row.review_minutes is not None]),
    )
    line("UX reviewer findings per phase (mean)", lambda group: mean_of([float(row.ux_findings) for row in group if row.ux_findings is not None]))
    line("code reviewer findings per phase (mean)", lambda group: mean_of([float(row.code_findings) for row in group if row.code_findings is not None]))
    print("\n".join(lines))


def watched_phases() -> int:
    return sum(1 for row in read_rows() if row.regime == "after")


def acknowledged() -> str | None:
    if not WATCH.exists():
        return None
    moment = cast(dict[str, object], json.loads(WATCH.read_text())).get("acknowledged")
    return moment if isinstance(moment, str) else None


def watch() -> int:
    moment = acknowledged()
    if moment is not None:
        print(f"acknowledged {moment}")
        return 0
    merged = watched_phases()
    if merged < WATCH_PHASES:
        print(f"{merged} of {WATCH_PHASES} phases merged without the extra code reviewer")
        return 0
    print(f"{WATCH_PHASES} of {WATCH_PHASES} phases merged without the extra code reviewer: report ready, waiting for your acknowledgment")
    return WAITING_FOR_ACKNOWLEDGMENT


def ack() -> None:
    merged = watched_phases()
    if merged < WATCH_PHASES:
        raise SystemExit(f"review_regime: the watch has {merged} of {WATCH_PHASES} phases; nothing to acknowledge yet")
    moment = datetime.now().astimezone().isoformat(timespec="minutes")
    WATCH.parent.mkdir(parents=True, exist_ok=True)
    _ = WATCH.write_text(json.dumps({"acknowledged": moment}) + "\n")
    print(f"acknowledged {moment}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add")
    _ = adding.add_argument("--unit", required=True)
    _ = adding.add_argument("--phase", required=True)
    _ = adding.add_argument("--regime", choices=REGIMES, required=True)
    _ = adding.add_argument("--started", required=True)
    _ = adding.add_argument("--merged", required=True)
    _ = adding.add_argument("--holds", type=int, required=True)
    _ = adding.add_argument("--merge-defects", type=int, required=True)
    _ = adding.add_argument("--ux-findings", type=int)
    _ = adding.add_argument("--code-findings", type=int)
    _ = adding.add_argument("--review-minutes", type=float)
    _ = adding.add_argument("--note")
    reporting = commands.add_parser("report")
    _ = reporting.add_argument("--since", help="only phases merged on or after this ISO date; design checks began 2026-09-28")
    _ = commands.add_parser("watch", help=f"count `after` phases toward {WATCH_PHASES}; exit {WAITING_FOR_ACKNOWLEDGMENT} while the report waits for the user")
    _ = commands.add_parser("ack", help="record the user's acknowledgment of the finished watch")
    arguments = parser.parse_args()
    command = cast(str, arguments.command)
    if command == "add":
        add(arguments)
    elif command == "report":
        report(cast(str | None, arguments.since))
    elif command == "watch":
        return watch()
    else:
        ack()
    return 0


if __name__ == "__main__":
    sys.exit(main())
