#!/usr/bin/env python3
"""Read or send input to a production unit or live Claude session in herdr."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "herdr"))
sys.path.insert(0, str(SCRIPTS / "message"))
import herdr  # noqa: E402
from herdr import AgentBlocked, Herdr  # noqa: E402
from herdr import HerdrFailure as HerdrFailure  # noqa: E402
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402
from unit_lookup import Place, marked_units, sessions_dir  # noqa: E402

USAGE = ("usage: session_pane.py <read|type|keys|where> <unit <slug> <unit>|session <id>> "
         "[--lines N --joined --ansi | <text> | <key>...]")


class NoSuchTarget(Exception):
    """No live session or marked unit identifies the requested pane."""


def read_place(place: Place, lines: int = 60, *, joined: bool = False, ansi: bool = False) -> str:
    if lines <= 0:
        raise ValueError("lines must be positive")
    return Herdr(place.session).read(place.pane, lines, joined=joined, ansi=ansi)


def type_place(place: Place, text: str) -> None:
    Herdr(place.session).prompt(place.pane, text)


def keys_place(place: Place, *keys: str) -> None:
    Herdr(place.session).send_keys(place.pane, *keys)


def where_place(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


def session_place(session_id: str) -> Place:
    unread = False
    records: list[SessionRecord] = []
    for path in sessions_dir().iterdir():
        if path.suffix != ".json":
            continue
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            unread = True
        elif record["sessionId"] == session_id and live_session(record):
            records.append(record)
    if not records:
        if unread:
            raise OSError("one or more session records could not be read")
        raise NoSuchTarget(session_id)
    return record_place(max(records, key=lambda record: record["updatedAt"]))


def record_place(record: SessionRecord) -> Place:
    """Resolve the pane owned by this specific registry process."""
    place = herdr.herdr_place_of(record["pid"])
    if isinstance(place, herdr.NotInHerdr):
        raise NoSuchTarget(record["sessionId"])
    return place


def main(arguments: list[str]) -> int:
    target = ""
    try:
        if len(arguments) < 3 or arguments[0] not in ("read", "type", "keys", "where"):
            raise ValueError(USAGE)
        verb, kind = arguments[:2]
        if kind == "unit" and len(arguments) >= 4:
            slug, name = arguments[2:4]
            target, rest = f"unit {slug} {name}", arguments[4:]
        elif kind == "session":
            name = arguments[2]
            target, rest = f"session {name}", arguments[3:]
            slug = ""
        else:
            raise ValueError(USAGE)
        lines, joined, ansi = 60, False, False
        if verb == "read":
            while rest:
                option, rest = rest[0], rest[1:]
                if option == "--lines" and rest:
                    lines, rest = int(rest[0]), rest[1:]
                    if lines <= 0:
                        raise ValueError("lines must be positive")
                elif option == "--joined":
                    joined = True
                elif option == "--ansi":
                    ansi = True
                else:
                    raise ValueError(USAGE)
        elif (verb == "type" and len(rest) != 1 or verb == "keys" and not rest
              or verb == "where" and rest):
            raise ValueError(USAGE)
        if kind == "unit":
            unit = marked_units(slug).get(name)
            if unit is None:
                raise NoSuchTarget(target)
            place = unit.place
        else:
            place = session_place(name)
        if verb == "read":
            print(read_place(place, lines, joined=joined, ansi=ansi), end="")
        elif verb == "type":
            type_place(place, rest[0])
        elif verb == "keys":
            keys_place(place, *rest)
        else:
            print(where_place(place))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except NoSuchTarget:
        print(f"{target}: no such target", file=sys.stderr)
        return 1
    except AgentBlocked:
        print(f"{target}: blocked at a prompt; nothing typed", file=sys.stderr)
        return 4
    except (OSError, HerdrFailure) as error:
        print(f"{target}: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
