#!/usr/bin/env python3
"""Start a Bevy upgrade watch and record the user's crate-specific answers."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from production_units import Production, read_production
import production_units
import pins
from scripts.production import showrunners
import upstream
import upgrade_check
import upgrade_record
import upgrade_report
import upgrade_schedule


class UpgradeUpdate(TypedDict):
    """The watch's report sentence and whether a git pin awaits the user."""

    update: str
    needs_user: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bevy_upgrade")
    _ = parser.add_argument("--production", type=Path)
    verbs = parser.add_subparsers(dest="verb")
    _ = verbs.add_parser("status")
    line = verbs.add_parser("line")
    _ = line.add_argument("--json", action="store_true")
    _ = verbs.add_parser("stop")
    check = verbs.add_parser("check")
    _ = check.add_argument("--scheduled", action="store_true")
    start = verbs.add_parser("start")
    _ = start.add_argument("target")
    for verb in ("approve", "decline"):
        answer = verbs.add_parser(verb)
        _ = answer.add_argument("crate")
        _ = answer.add_argument("words")
    forget = verbs.add_parser("forget")
    _ = forget.add_argument("crate")
    note = verbs.add_parser("note")
    _ = note.add_argument("crate")
    _ = note.add_argument("text", nargs="?", default="")
    return parser


def _production(args: argparse.Namespace) -> Production:
    doc = cast(Path | None, args.production)
    if doc is None:
        doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if doc is None:
        raise production_units.Refusal("this session runs no production; pass --production <doc>")
    return read_production(doc)


def _baseline(record: upgrade_record.Record) -> None:
    record["announced"] = {name: upgrade_report.announcement(entry, record["target"])
                           for name, entry in record["dependencies"].items()}


def _line(production: Production, now: datetime, as_json: bool) -> int:
    current = upgrade_record.load(production.slug)
    if current is None or current["closed"] is not None:
        return 0
    pins_error = ""
    try:
        latest = pins.read_pins(production.checkout)
    except pins.PinsFailure as error:
        pins_error = str(error)
    else:
        current = upgrade_record.apply_pins(current, latest, now)
    upgrade_schedule.ensure_job(current)
    sentence = upgrade_report.line(current, production.zone, now,
                                   upgrade_schedule.next_check(production.slug), pins_error)
    if as_json:
        print(json.dumps(UpgradeUpdate(update=sentence, needs_user=upgrade_report.waiting(current) > 0)))
    else:
        print(sentence)
    return 0


def _scheduled(record: upgrade_record.Record, production: Production,
               now: datetime, differences: list[str], complete: bool) -> bool:
    """Return whether successful delivery closes the watch."""
    if complete:
        summary = f"Bevy {record['target']} upgrade: done"
        text = "\n".join([*differences, _closed_sentence(record)])
    elif record["announced"] is None:
        _baseline(record)
        return False
    elif differences:
        count = len(differences)
        summary = f"Bevy {record['target']} upgrade: {count} change{'s' if count != 1 else ''}"
        text = "\n".join([*differences,
                          upgrade_report.line(record, production.zone, now,
                                              upgrade_schedule.next_check(record["slug"]), ""),
                          "Full table: /showrunner:bevy_upgrade"])
    else:
        return False
    if not upgrade_schedule.tell_showrunner(record["slug"], summary, text):
        return False
    _baseline(record)
    if complete:
        record["closed"] = now.astimezone(timezone.utc).isoformat()
    return complete


def _closed_sentence(record: upgrade_record.Record) -> str:
    return (f"Bevy {record['target']} upgrade: every dependency is on Bevy {record['target']}. "
            "The watch is closed.")


def _run(args: argparse.Namespace, production: Production, now: datetime) -> int:
    verb = cast(str | None, args.verb) or "status"
    if verb == "line":
        return _line(production, now, cast(bool, args.json))
    scheduled = verb == "check" and cast(bool, args.scheduled)
    pins_error = ""
    exit_code = 0
    remove = False
    differences: list[str] = []
    closing = ""
    latest: pins.Pins | None = None  # None means no successful lockfile read in this run.
    with upgrade_record.locked(production.slug):
        if verb == "start":
            latest = pins.read_pins(production.checkout)
            current = upgrade_record.start(production, cast(str, args.target), latest, now)
        else:
            current = upgrade_record.load(production.slug)
            if current is None:
                if not scheduled:
                    print(f"no Bevy upgrade is tracked for {production.slug}; start one with /showrunner:bevy_upgrade start <version>")
                return 0
            if verb == "check" and current["closed"] is not None:
                if scheduled:
                    upgrade_schedule.remove_job(production.slug)
                    return 0
                raise upgrade_record.RecordFailure("the watch is closed; start a new one")
            try:
                latest = pins.read_pins(production.checkout)
            except pins.PinsFailure as error:
                pins_error = str(error)
            else:
                current = upgrade_record.apply_pins(current, latest, now)
            if verb in ("approve", "decline"):
                answer = "approved" if verb == "approve" else "declined"
                current = upgrade_record.set_answer(current, cast(str, args.crate), answer,
                                            cast(str, args.words), now.astimezone(production.zone).date().isoformat())
            elif verb == "forget":
                current = upgrade_record.forget_answer(current, cast(str, args.crate))
            elif verb == "note":
                current = upgrade_record.set_note(current, cast(str, args.crate), cast(str, args.text))
            elif verb == "stop":
                remove = True
        if scheduled and pins_error:
            print(f"bevy_upgrade: pins could not be re-read: {pins_error}", file=sys.stderr)
            return 1
        if verb in ("start", "check"):
            if pins_error:
                exit_code = 1
            elif latest is not None:
                fixture = os.environ.get("BEVY_UPGRADE_FIXTURES")
                fetch = upstream.fixture_fetch(Path(fixture)) if fixture is not None else upstream.http_fetch
                result = upgrade_check.run_check(current, latest, fetch, lambda: datetime.now(timezone.utc))
                current = result.record
                if verb == "check" and result.asked > 0 and result.asked == result.failed:
                    exit_code = 1
        if current["closed"] is None:
            differences = upgrade_report.changes(current)
        complete = (verb == "check" and latest is not None and not pins_error
                    and all(upgrade_report.state(entry, current["target"]) == "done"
                            for entry in current["dependencies"].values()))
        if scheduled:
            remove = _scheduled(current, production, now, differences, complete)
        else:
            if verb == "stop":
                current["closed"] = current["closed"] or now.astimezone(timezone.utc).isoformat()
            if complete:
                current["closed"] = now.astimezone(timezone.utc).isoformat()
                remove = True
                closing = _closed_sentence(current)
            _baseline(current)
        upgrade_record.save(current)
        if remove:
            upgrade_schedule.remove_job(production.slug)
        elif current["closed"] is None and verb in ("start", "status", "check"):
            upgrade_schedule.ensure_job(current)
        if not scheduled:
            upgrade_schedule.retire_message(production.slug)
    if scheduled:
        return exit_code
    if pins_error:
        read_at = datetime.fromisoformat(current["pins_read"]).astimezone(production.zone)
        print(f"pins could not be re-read: {pins_error}; showing pins read {read_at:%H:%M %Z}")
    if differences:
        print("Changed since the last report:")
        print("\n".join(differences))
    if closing:
        print(closing)
    elif verb == "status" and current["closed"] is not None:
        closed_at = datetime.fromisoformat(current["closed"]).astimezone(production.zone)
        print(f"This watch closed {closed_at:%Y-%m-%d}.")
    print(upgrade_report.table(current, production.zone, now))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """None uses the process arguments."""
    args = _parser().parse_args(argv)
    try:
        return _run(args, _production(args), datetime.now(timezone.utc))
    except (upgrade_record.RecordFailure, pins.PinsFailure, production_units.Refusal) as error:
        print(f"bevy_upgrade: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
