#!/usr/bin/env python3
"""Start a Bevy upgrade watch and record the user's crate-specific answers."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

import add_unit
import pins
import showrunners
import upgrade_record
import upgrade_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bevy_upgrade")
    _ = parser.add_argument("--production", type=Path)
    verbs = parser.add_subparsers(dest="verb")
    _ = verbs.add_parser("status")
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


def _production(args: argparse.Namespace) -> add_unit.Production:
    doc = cast(Path | None, args.production)
    if doc is None:
        doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if doc is None:
        raise add_unit.Refusal("this session runs no production; pass --production <doc>")
    return add_unit.read_production(doc)


def _run(args: argparse.Namespace, production: add_unit.Production, now: datetime) -> int:
    verb = cast(str | None, args.verb) or "status"
    pins_error = ""
    with upgrade_record.locked(production.slug):
        if verb == "start":
            current = upgrade_record.start(production, cast(str, args.target), pins.read_pins(production.checkout), now)
        else:
            current = upgrade_record.load(production.slug)
            if current is None:
                print(f"no Bevy upgrade is tracked for {production.slug}; start one with /showrunner:bevy_upgrade start <version>")
                return 0
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
        if not pins_error or verb != "status":
            upgrade_record.save(current)
    if pins_error:
        read_at = datetime.fromisoformat(current["pins_read"]).astimezone(production.zone)
        print(f"pins could not be re-read: {pins_error}; showing pins read {read_at:%H:%M %Z}")
    print(upgrade_report.table(current, production.zone, now))
    return 0


def main(argv: list[str] | None = None) -> int:
    """None uses the process arguments."""
    args = _parser().parse_args(argv)
    try:
        return _run(args, _production(args), datetime.now(timezone.utc))
    except (upgrade_record.RecordFailure, pins.PinsFailure, add_unit.Refusal) as error:
        print(f"bevy_upgrade: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
