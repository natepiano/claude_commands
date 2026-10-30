#!/usr/bin/env python3
"""The zone every time shown to the user is written in.

One setting, `config/user_zone.conf`, so a change of zone is one line. `USER_ZONE` in the
environment wins over the file: a test that compares against saved output pins its zone with it.

Usage: user_zone.py            print the zone's IANA name, for `TZ="$(…)" date '+%H:%M %Z'`
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = "America/New_York"
CONF = os.environ.get("USER_ZONE_CONF") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "config", "user_zone.conf")


def stored_zone(path: str) -> str:
    """The file's `zone=` value. Empty when the file or the line is missing."""
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return ""
    return next((line.split("=", 1)[1].strip() for line in lines if line.startswith("zone=")), "")


def user_zone_name() -> str:
    """The user's zone as an IANA name. Raises ValueError naming where an unknown one came from."""
    pinned = os.environ.get("USER_ZONE", "")
    name = pinned or stored_zone(CONF) or DEFAULT_ZONE
    try:
        _ = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        source = "USER_ZONE" if pinned else str(CONF)
        raise ValueError(f"{source}: {name!r} is not a zone name; use one like {DEFAULT_ZONE}") from error
    return name


def user_zone() -> ZoneInfo:
    """The user's zone. Read at each call, so an edit to the setting needs no restart."""
    return ZoneInfo(user_zone_name())


def zone_label(moment: datetime | None = None) -> str:
    """The short label beside a time in the user's zone (EDT, EST), as of `moment` or now."""
    at = moment.astimezone(user_zone()) if moment is not None else datetime.now(user_zone())
    return at.tzname() or user_zone_name()


def main() -> int:
    try:
        print(user_zone_name())
    except ValueError as error:
        print(f"user_zone: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
